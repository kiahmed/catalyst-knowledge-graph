"""Tests for src/upstream_dedup.py — reacting to Arboryx dedup_runs deletions.

Firestore is faked in-memory; DuckDB is a temp file (process_feed opens by path).
Run: python -m unittest tests/test_upstream_dedup.py -v   (inside the ingest image)
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from types import SimpleNamespace as N

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import duckdb  # noqa: E402

from src import db, export, integrity, upstream_dedup  # noqa: E402


class _Doc:
    def __init__(self, store, path):
        self.store, self.path, self.id = store, path, path[-1]

    def get(self):
        d = self.store.get(self.path)
        return N(exists=d is not None, to_dict=lambda: d, id=self.id)

    def set(self, data, merge=False):
        cur = dict(self.store.get(self.path) or {}) if merge else {}
        for k, v in data.items():
            vals = getattr(v, "values", None)          # firestore.ArrayUnion
            cur[k] = (cur.get(k) or []) + [x for x in vals if x not in (cur.get(k) or [])] if vals is not None else v
        self.store[self.path] = cur

    def create(self, data):
        from google.api_core.exceptions import AlreadyExists
        if self.path in self.store:
            raise AlreadyExists("exists")
        self.store[self.path] = dict(data)

    def delete(self):
        self.store.pop(self.path, None)

    def collection(self, name):
        return _Col(self.store, self.path + (name,))


class _Col:
    def __init__(self, store, path):
        self.store, self.path = store, path

    def document(self, doc_id):
        return _Doc(self.store, self.path + (doc_id,))

    def select(self, _fields):
        return self

    def where(self, **_kw):   # tests keep one category per collection
        return self

    def stream(self):
        n = len(self.path) + 1
        return [N(id=p[-1]) for p in list(self.store) if len(p) == n and p[:-1] == self.path]


class FakeFS:
    def __init__(self):
        self.store = {}

    def collection(self, name):
        return _Col(self.store, (name,))


def _row(target, matched, cat="Robotics"):
    return {"category": cat, "target_id": target, "matched_id": matched, "reason": "llm_same_event"}


class TestUpstreamDedup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "t.duckdb")
        con = duckdb.connect(self.path)
        db.init_schema(con)
        self.cid = {}
        for e in ("ROB-A", "ROB-B", "ROB-C", "ROB-D"):
            con.execute("""INSERT INTO catalysts (entry_id, sector, timestamp, raw_finding, headline, sentiment_label)
                           VALUES (?, 'Robotics', CURRENT_DATE, ?, 'h', 'bullish')""", [e, f"{e} news"])
            self.cid[e] = con.execute("SELECT catalyst_id FROM catalysts WHERE entry_id=?", [e]).fetchone()[0]
        x = db.insert_entity(con, "X", None, "private_company")
        y = db.insert_entity(con, "Y", None, "private_company")
        for e in ("ROB-A", "ROB-B"):
            con.execute("""INSERT INTO relationships (catalyst_id, entity_a_id, rel_type, entity_b_id, confidence)
                           VALUES (?, ?, 'partners_with', ?, 0.9)""", [self.cid[e], x, y])
        # another card's edge was invalidated by ROB-B → must be re-pointed, not left dangling
        con.execute("""INSERT INTO relationships (catalyst_id, entity_a_id, rel_type, entity_b_id, confidence,
                           invalidated_by_catalyst_id) VALUES (?, ?, 'competes_with', ?, 0.5, ?)""",
                    [self.cid["ROB-C"], x, y, self.cid["ROB-B"]])
        con.close()
        self.cfg = N(duckdb_path=self.path, sector="Robotics", firestore=N(collection="findings"),
                     firestore_export=N(enabled=True, collection="CKG-Robotics", catalysts_subpath="catalysts"))
        self.fs = FakeFS()
        self.items = ("CKG-Robotics", "catalysts", "items")
        for e in ("ROB-A", "ROB-B", "ROB-C", "ROB-D"):
            self.fs.store[self.items + (e,)] = {"card_id": e}

    def _run(self, run_id, rows):
        self.fs.store[("dedup_runs", run_id)] = {"deleted": rows}

    def _attention(self):
        return {p[-1]: d for p, d in self.fs.store.items() if p[:3] == upstream_dedup.ATTENTION_PATH}

    def test_merge_removes_target_and_redirects(self):
        self.fs.store[("dedup_runs", "state")] = {"watermark": "x"}            # never treated as a run
        self._run("2026-11-03T02:00:00+00:00", [_row("ROB-B", "ROB-A"), _row("PWE-1", "PWE-0", "Power & Energy")])
        s = upstream_dedup.process_feed(self.cfg, self.fs)
        self.assertEqual(s["runs"], 1)
        self.assertEqual([m["target"] for m in s["merged"]], ["ROB-B"])
        self.assertNotIn(self.items + ("ROB-B",), self.fs.store)               # Firestore card gone
        base = self.fs.store[self.items[:-1]]                                   # open pages learn of the delete
        self.assertEqual([e.split("@")[0] for e in base["deleted_ids"]], ["ROB-B"])   # "<id>@<stamp>"
        self.assertTrue(base["version"])
        con = duckdb.connect(self.path)
        self.assertIsNone(con.execute("SELECT 1 FROM catalysts WHERE entry_id='ROB-B'").fetchone())
        self.assertEqual(con.execute("SELECT invalidated_by_catalyst_id FROM relationships WHERE catalyst_id=?",
                                     [self.cid["ROB-C"]]).fetchone()[0], self.cid["ROB-A"])
        self.assertTrue(all(v == [] for k, v in integrity.check_db(con).items()))   # nothing dangling
        cards = {c["card_id"]: c for c in export._load_cards(con, "Robotics", 100, 0, 0.0, False)}
        con.close()
        self.assertEqual(cards["ROB-A"]["merged_from"], ["ROB-B"])
        self.assertNotIn("merged_from", cards["ROB-D"])                         # untouched cards unchanged

    def test_rerun_and_old_runs_never_act_twice(self):
        self._run("2026-11-03T02:00:00+00:00", [_row("ROB-B", "ROB-A")])
        upstream_dedup.process_feed(self.cfg, self.fs)
        self.fs.store[self.items + ("ROB-B",)] = {"card_id": "ROB-B"}          # would be wrongly deleted again
        s = upstream_dedup.process_feed(self.cfg, self.fs)
        self.assertEqual(s["runs"], 0)
        self.assertIn(self.items + ("ROB-B",), self.fs.store)

    def test_missing_kept_card_goes_to_admin_attention(self):
        self._run("2026-11-03T02:00:00+00:00", [_row("ROB-D", "ROB-ZZZ"), _row("ROB-NEVER", "ROB-A")])
        s = upstream_dedup.process_feed(self.cfg, self.fs)
        self.assertEqual(s["attention"], ["ROB-D"])
        self.assertEqual(s["no_card"], 1)
        self.assertIn(self.items + ("ROB-D",), self.fs.store)                   # kept, not auto-removed
        att = self._attention()
        self.assertEqual(len(att), 1)
        entry = next(iter(att.values()))
        self.assertEqual((entry["source"], entry["doc_id"], entry["resolved"]), ("robotics_CKG", "ROB-D", False))
        self.assertEqual(entry["matched_id"], "ROB-ZZZ")

    def test_chained_merges_land_on_final_card(self):
        self._run("2026-11-03T02:00:00+00:00", [_row("ROB-C", "ROB-B")])
        self._run("2026-11-17T02:00:00+00:00", [_row("ROB-B", "ROB-A")])
        upstream_dedup.process_feed(self.cfg, self.fs)
        con = duckdb.connect(self.path)
        self.assertEqual(upstream_dedup.merged_from_map(con), {"ROB-A": ["ROB-B", "ROB-C"]})
        con.close()

    def test_firestore_failure_retries_cleanly_next_run(self):
        self._run("2026-11-03T02:00:00+00:00", [_row("ROB-B", "ROB-A")])
        orig = _Doc.delete
        def boom(doc):
            raise RuntimeError("firestore unavailable")
        _Doc.delete = boom
        try:
            with self.assertRaises(RuntimeError):
                upstream_dedup.process_feed(self.cfg, self.fs)
        finally:
            _Doc.delete = orig
        con = duckdb.connect(self.path)
        self.assertIsNotNone(con.execute("SELECT 1 FROM catalysts WHERE entry_id='ROB-B'").fetchone())  # untouched
        self.assertEqual(con.execute("SELECT COUNT(*) FROM upstream_merges").fetchone()[0], 0)
        con.close()
        s = upstream_dedup.process_feed(self.cfg, self.fs)                      # retry → merged, not 'no_card'
        self.assertEqual([m["target"] for m in s["merged"]], ["ROB-B"])
        self.assertNotIn(self.items + ("ROB-B",), self.fs.store)

    def test_same_batch_chain_resolves_to_surviving_card(self):
        self._run("2026-11-03T02:00:00+00:00", [_row("ROB-B", "ROB-A"), _row("ROB-C", "ROB-B")])
        s = upstream_dedup.process_feed(self.cfg, self.fs)
        self.assertEqual([(m["target"], m["kept"]) for m in s["merged"]], [("ROB-B", "ROB-A"), ("ROB-C", "ROB-A")])
        self.assertEqual(s["attention"], [])

    def test_same_target_in_two_runs_acts_once(self):
        self._run("2026-11-03T02:00:00+00:00", [_row("ROB-B", "ROB-A")])
        self._run("2026-11-17T02:00:00+00:00", [_row("ROB-B", "ROB-A")])
        s = upstream_dedup.process_feed(self.cfg, self.fs)
        self.assertEqual(s["runs"], 2)
        self.assertEqual(len(s["merged"]), 1)

    def test_callback_error_keeps_integrity_summary(self):
        cfg = N(**{**vars(self.cfg), "firestore_export": N(enabled=False)})
        self.fs.store[("findings", "ROB-A")] = {}                                # others look upstream-deleted
        def boom(ids):
            raise RuntimeError("create failed")
        out = integrity.run_integrity_check(cfg, self.fs, on_upstream_deleted=boom)
        self.assertIn("upstream_deleted_callback_error", out)
        self.assertEqual(out["upstream_deleted_count"], 3)
        self.assertEqual(out["issue_count"], 1)                                  # the callback error counts

    def test_unexpected_deletion_flagged_once(self):
        self.assertEqual(upstream_dedup.flag_unexpected(self.fs, ["ROB-D"], self.cfg), 1)
        self.assertEqual(upstream_dedup.flag_unexpected(self.fs, ["ROB-D"], self.cfg), 0)   # no daily pile-up
        self.assertEqual(next(iter(self._attention().values()))["kind"], "upstream_deleted_unexpected")

    def test_feed_explained_deletion_not_double_flagged(self):
        self._run("2026-11-03T02:00:00+00:00", [_row("ROB-D", "ROB-ZZZ")])     # → attention via feed
        upstream_dedup.process_feed(self.cfg, self.fs)
        self.assertEqual(upstream_dedup.flag_unexpected(self.fs, ["ROB-D"], self.cfg), 0)
        self.assertEqual(len(self._attention()), 1)


if __name__ == "__main__":
    unittest.main()
