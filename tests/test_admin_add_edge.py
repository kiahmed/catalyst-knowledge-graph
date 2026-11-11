"""Tests for the admin add_edge op (docs/ticket-admin-add-edge.md).

Run: python -m unittest tests/test_admin_add_edge.py -v   (inside the ingest image)
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import duckdb  # noqa: E402

from src import admin_ops as ops, db  # noqa: E402

WHO = {"email": "info@solutionjet.net", "ip": "203.0.113.1",
       "vocab": {"deploys", "partners_with", "competes_with", "supplies"}}


class TestAddEdge(unittest.TestCase):
    def setUp(self):
        self.con = duckdb.connect(":memory:")
        db.init_schema(self.con)
        ops.ensure_schema(self.con)
        self.tesla = db.insert_entity(self.con, "Tesla", None, "public_company")
        self.samsung = db.insert_entity(self.con, "Samsung SDI", None, "public_company")
        self.con.execute("""INSERT INTO catalysts (entry_id, sector, timestamp, raw_finding, headline, sentiment_label)
                            VALUES ('ROB-1', 'Robotics', TIMESTAMP '2026-11-10 08:00:00', 'x', 'h', 'bullish')""")
        self.p = {"entry_id": "ROB-1", "entity_a_id": self.samsung, "rel_type": "supplies", "entity_b_id": self.tesla,
                  "mechanism": "Samsung manufactures AI5 chips for Tesla."}

    def tearDown(self):
        self.con.close()

    def _rows(self):
        return self.con.execute("""SELECT entity_a_id, rel_type, entity_b_id, status, evidence_type, confidence,
                                   rel_subtype, CAST(first_flagged_at AS VARCHAR) FROM relationships""").fetchall()

    def test_add_then_revert(self):
        out = ops.apply_edit(self.con, "add_edge", self.p, **WHO)
        self.assertEqual(out["cards"], ["ROB-1"])
        self.assertEqual(self._rows(), [(self.samsung, "supplies", self.tesla, "active", "direct", 1.0,
                                         "manufactures_for", "2026-11-10")])           # subtype refreshed too
        ops.revert_edit(self.con, out["edit_id"], email=WHO["email"], ip=WHO["ip"], vocab=WHO["vocab"])
        self.assertEqual(self._rows(), [])

    def test_guards(self):
        bad = [{**self.p, "entry_id": "ROB-404"}, {**self.p, "entity_b_id": self.samsung},
               {**self.p, "rel_type": "loves"}, {**self.p, "mechanism": "short"}, {**self.p, "entity_a_id": 999}]
        for p in bad:
            with self.assertRaises(ops.EditError, msg=str(p)):
                ops.apply_edit(self.con, "add_edge", p, **WHO)
        ops.apply_edit(self.con, "add_edge", self.p, **WHO)
        with self.assertRaises(ops.EditError):                                         # already live on the card
            ops.apply_edit(self.con, "add_edge", self.p, **WHO)

    def test_two_party_type_is_stored_ordered_and_twin_checked_both_ways(self):
        p = {**self.p, "rel_type": "partners_with", "entity_a_id": self.samsung, "entity_b_id": self.tesla,
             "mechanism": "Samsung and Tesla partner on chips."}
        ops.apply_edit(self.con, "add_edge", p, **WHO)
        a, b = sorted([self.samsung, self.tesla])
        self.assertEqual(self._rows()[0][:3], (a, "partners_with", b))
        with self.assertRaises(ops.EditError):
            ops.apply_edit(self.con, "add_edge", {**p, "entity_a_id": self.tesla, "entity_b_id": self.samsung}, **WHO)


if __name__ == "__main__":
    unittest.main()
