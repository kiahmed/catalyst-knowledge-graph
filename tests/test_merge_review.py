"""Tests for admin_ops.merge_review_items — what an admin merge makes suspicious goes to the Review queue.

Run: python -m unittest tests.test_merge_review -v   (inside the ingest image)
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import duckdb  # noqa: E402

from src import admin_ops as ops, db, review  # noqa: E402

VOCAB = {"deploys", "partners_with", "supplies", "competes_with"}
WHO = {"email": "t@example.com", "ip": "203.0.113.1", "vocab": VOCAB}


class TestMergeReview(unittest.TestCase):
    def setUp(self):
        self.con = duckdb.connect(":memory:")
        db.init_schema(self.con)
        ops.ensure_schema(self.con)
        e = lambda n, t="private_company": db.insert_entity(self.con, n, None, t)  # noqa: E731
        self.optimus, self.tesla, self.acme = e("Optimus", "product"), e("Tesla", "public_company"), e("Acme")
        self.r = {}
        for i, (a, rel, b) in enumerate([(self.tesla, "deploys", self.optimus),      # → internal event
                                         (self.optimus, "partners_with", self.tesla),  # → rejected self-loop
                                         (self.optimus, "supplies", self.acme),        # → Tesla supplies Acme …
                                         (self.acme, "supplies", self.tesla),          # … opposite this one
                                         (self.acme, "competes_with", self.optimus)]):  # unrelated, symmetric
            card = f"ROB-{min(i, 2)}"                                                  # rows 2-4 share a card
            if not self.con.execute("SELECT 1 FROM catalysts WHERE entry_id = ?", [card]).fetchone():
                self.con.execute("""INSERT INTO catalysts (entry_id, sector, timestamp, raw_finding, headline,
                                    sentiment_label) VALUES (?, 'Robotics', CURRENT_DATE, 'x', 'h', 'bullish')""", [card])
            cid = self.con.execute("SELECT catalyst_id FROM catalysts WHERE entry_id = ?", [card]).fetchone()[0]
            self.con.execute("""INSERT INTO relationships (catalyst_id, entity_a_id, rel_type, entity_b_id, confidence)
                                VALUES (?, ?, ?, ?, 0.8)""", [cid, a, rel, b])
            self.r[i] = self.con.execute("SELECT MAX(rel_id) FROM relationships").fetchone()[0]

    def test_merge_flags_internal_rejected_and_direction_conflict(self):
        out = ops.apply_edit(self.con, "merge", {"src_id": self.optimus, "dst_id": self.tesla}, **WHO)
        got = {(i["kind"], i["rel_id"]) for i in out["review"]}
        self.assertEqual(got, {("merge_internal_event", self.r[0]), ("merge_self_loop_rejected", self.r[1]),
                               ("merge_direction_conflict", self.r[2])})
        conflict = next(i for i in out["review"] if i["kind"] == "merge_direction_conflict")
        self.assertEqual(conflict["other_rel_id"], self.r[3])
        self.assertEqual(conflict["entry_id"], "ROB-2")
        self.assertTrue(all(i["reason"] and "Optimus" in i["reason"] for i in out["review"]))
        stored = self.con.execute("SELECT after FROM admin_edits WHERE edit_id = ?", [out["edit_id"]]).fetchone()[0]
        self.assertIn("merge_internal_event", stored)                    # kept on the audit row too

    def test_clean_merge_flags_nothing(self):
        dup = db.insert_entity(self.con, "Acme Inc", None, "private_company")
        out = ops.apply_edit(self.con, "merge", {"src_id": dup, "dst_id": self.acme}, **WHO)
        self.assertEqual(out["review"], [])

    def test_editing_the_edge_settles_the_flag(self):
        items = [{"id": "x", "kind": "merge_internal_event", "rel_id": self.r[0]},
                 {"id": "y", "kind": "merge_direction_conflict", "rel_id": self.r[2]}]
        self.assertEqual(review.matching(items, rel_ids=[self.r[0]]), ["x"])


if __name__ == "__main__":
    unittest.main()
