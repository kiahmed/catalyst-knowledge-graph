"""Tests for the delta card export (src/firestore_export._write_catalysts).

Only new/changed cards may be written; change detection must come from the
local export_state table (no Firestore reads); a failed commit must leave
state untouched so the next run retries.

Run: python -m unittest tests/test_firestore_delta.py -v   (inside the ingest image)
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from src import firestore_export as fx  # noqa: E402


class _FakeDoc:
    def __init__(self, store, path):
        self.store, self.path = store, path

    def collection(self, name):
        return _FakeCol(self.store, f"{self.path}/{name}")

    def set(self, data, merge=False):
        self.store.writes.append(self.path)
        self.store.docs[self.path] = {**(self.store.docs.get(self.path) or {}), **data} if merge else data


class _FakeCol:
    def __init__(self, store, path):
        self.store, self.path = store, path

    def document(self, name):
        return _FakeDoc(self.store, f"{self.path}/{name}")

    def stream(self):
        pre = self.path + "/"
        for k, v in list(self.store.docs.items()):
            if k.startswith(pre) and "/" not in k[len(pre):]:
                yield SimpleNamespace(id=k[len(pre):], to_dict=lambda v=v: v)


class _FakeBatch:
    def __init__(self, store):
        self.store, self.ops = store, []

    def set(self, doc, data, merge=False):
        self.ops.append((doc, data))

    def delete(self, doc):
        self.ops.append((doc, None))

    def commit(self):
        if self.store.fail_commit:
            raise RuntimeError("firestore unavailable")
        for doc, data in self.ops:
            if data is None:
                self.store.docs.pop(doc.path, None)
            else:
                doc.set(data, merge=True)


class _FakeClient:
    def __init__(self):
        self.docs, self.writes, self.reads, self.fail_commit = {}, [], 0, False

    def collection(self, name):
        return _FakeCol(self, name)

    def batch(self):
        return _FakeBatch(self)


def _cfg(db_path):
    return SimpleNamespace(
        duckdb_path=db_path, sector="Robotics",
        firestore_export=SimpleNamespace(collection="CKG-Robotics", catalysts_subpath="catalysts"),
    )


def _cards(n, **override):
    out = [{"card_id": f"ROB-T{i:03d}", "headline": f"h{i}", "hashtags": ["#Robotics"]} for i in range(n)]
    for cid, field in override.items():
        for c in out:
            if c["card_id"] == cid:
                c.update(field)
    return out


class TestDeltaExport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = _cfg(os.path.join(self.tmp, "t.duckdb"))
        self.client = _FakeClient()
        self.p = mock.patch.object(fx, "_firestore_client", return_value=self.client)
        self.p.start()

    def tearDown(self):
        self.p.stop()
        os.environ.pop("FIRESTORE_EXPORT_FULL_SYNC", None)

    def _item_writes(self):
        return [w for w in self.client.writes if "/items/" in w]

    def test_first_run_writes_all_then_nothing(self):
        self.assertEqual(fx._write_catalysts(self.cfg, _cards(5)),
                         {"written": 5, "unchanged": 0, "total": 5})
        self.client.writes.clear()
        self.assertEqual(fx._write_catalysts(self.cfg, _cards(5)),
                         {"written": 0, "unchanged": 5, "total": 5})
        self.assertEqual(self._item_writes(), [])
        # summary doc reports the export size, not this run's writes
        self.assertEqual(self.client.docs["CKG-Robotics/catalysts"]["count"], 5)

    def test_silent_refresh_markers(self):
        # docs/ticket-frontend-silent-refresh.md: written cards carry synced_at (outside the hash), the
        # parent's version moves only when something was written, and an admin refresh keeps the count.
        fx._write_catalysts(self.cfg, _cards(5))
        base = self.client.docs["CKG-Robotics/catalysts"]
        v1 = base["version"]
        self.assertTrue(all(self.client.docs[f"CKG-Robotics/catalysts/items/ROB-T{i:03d}"]["synced_at"] == v1
                            for i in range(5)))
        fx._write_catalysts(self.cfg, _cards(5))                                  # nothing changed
        self.assertEqual(self.client.docs["CKG-Robotics/catalysts"]["version"], v1)
        fx._write_catalysts(self.cfg, _cards(1, **{"ROB-T000": {"headline": "new"}}), force=True, partial=True)
        base = self.client.docs["CKG-Robotics/catalysts"]
        self.assertEqual(base["count"], 5)                                        # partial: count untouched
        self.assertGreaterEqual(base["version"], v1)
        self.assertEqual(self.client.docs["CKG-Robotics/catalysts/items/ROB-T000"]["synced_at"], base["version"])

    def test_only_changed_and_new_cards_written(self):
        fx._write_catalysts(self.cfg, _cards(5))
        self.client.writes.clear()
        cards = _cards(6, **{"ROB-T002": {"hashtags": ["#Robotics", "#NVIDIA"]}})
        self.assertEqual(fx._write_catalysts(self.cfg, cards)["written"], 2)
        self.assertEqual(sorted(w.rsplit("/", 1)[1] for w in self._item_writes()),
                         ["ROB-T002", "ROB-T005"])

    def test_failed_commit_is_retried_next_run(self):
        self.client.fail_commit = True
        with self.assertRaises(RuntimeError):
            fx._write_catalysts(self.cfg, _cards(3))
        self.client.fail_commit = False
        self.assertEqual(fx._write_catalysts(self.cfg, _cards(3))["written"], 3)

    def test_full_sync_env_rewrites_everything(self):
        fx._write_catalysts(self.cfg, _cards(4))
        os.environ["FIRESTORE_EXPORT_FULL_SYNC"] = "true"
        self.assertEqual(fx._write_catalysts(self.cfg, _cards(4))["written"], 4)

    def test_invalidated_cards_are_rewritten_next_run(self):
        # Admin edit path: state marked stale on the pushed copy, inline refresh failed.
        import duckdb
        fx._write_catalysts(self.cfg, _cards(4))
        con = duckdb.connect(self.cfg.duckdb_path)
        con.execute("INSERT INTO export_state (doc_key, content_hash) VALUES ('entities/7', 'h')")
        fx.invalidate_state(con, ["ROB-T001"])
        self.assertEqual(con.execute("SELECT content_hash FROM export_state WHERE doc_key = 'entities/7'").fetchone(),
                         ("",))                                    # kept (delete detection), just stale
        con.close()
        self.client.writes.clear()
        self.assertEqual(fx._write_catalysts(self.cfg, _cards(4))["written"], 1)
        self.assertEqual([w.rsplit("/", 1)[1] for w in self._item_writes()], ["ROB-T001"])

    def test_admin_alias_drop_without_cards_still_refreshes_entity_docs(self):
        import duckdb
        from src import db
        con = duckdb.connect(self.cfg.duckdb_path)
        db.init_schema(con)
        serve, fox = db.insert_entity(con, "Serve Robotics", None, "public_company"), \
            db.insert_entity(con, "Foxconn", None, "public_company")
        db.insert_alias(con, serve, "SEER Robotics")
        con.execute("""INSERT INTO catalysts (entry_id, sector, timestamp, raw_finding, headline, sentiment_label)
                       VALUES ('ROB-E', 'Robotics', CURRENT_DATE, 'f', 'h', 'bullish')""")
        cid = con.execute("SELECT catalyst_id FROM catalysts").fetchone()[0]
        con.execute("INSERT INTO relationships (catalyst_id, entity_a_id, rel_type, entity_b_id, confidence) "
                    "VALUES (?, ?, 'supplies', ?, 0.8)", [cid, fox, serve])
        con.close()
        fx.refresh_entities(self.cfg)
        doc = lambda: self.client.docs[f"CKG-Robotics/entities/items/{serve}"]   # noqa: E731
        self.assertEqual(doc()["aliases"], ["SEER Robotics"])
        con = duckdb.connect(self.cfg.duckdb_path)
        con.execute("DELETE FROM entity_aliases WHERE entity_id = ?", [serve])   # the admin alias drop
        con.close()
        out = fx.refresh_entities(self.cfg)
        self.assertEqual(out["entities"]["written"], 1)                         # only the changed doc
        self.assertEqual(doc()["aliases"], [])

    def test_hash_is_order_independent(self):
        a = {"card_id": "X", "a": 1, "b": [1, 2]}
        b = {"b": [1, 2], "a": 1, "card_id": "X"}
        self.assertEqual(fx._card_hash(a), fx._card_hash(b))


if __name__ == "__main__":
    unittest.main()
