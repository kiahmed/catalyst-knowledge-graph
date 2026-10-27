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
        self.store.docs[self.path] = data


class _FakeCol:
    def __init__(self, store, path):
        self.store, self.path = store, path

    def document(self, name):
        return _FakeDoc(self.store, f"{self.path}/{name}")


class _FakeBatch:
    def __init__(self, store):
        self.store, self.ops = store, []

    def set(self, doc, data, merge=False):
        self.ops.append((doc, data))

    def commit(self):
        if self.store.fail_commit:
            raise RuntimeError("firestore unavailable")
        for doc, data in self.ops:
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

    def test_hash_is_order_independent(self):
        a = {"card_id": "X", "a": 1, "b": [1, 2]}
        b = {"b": [1, 2], "a": 1, "card_id": "X"}
        self.assertEqual(fx._card_hash(a), fx._card_hash(b))


if __name__ == "__main__":
    unittest.main()
