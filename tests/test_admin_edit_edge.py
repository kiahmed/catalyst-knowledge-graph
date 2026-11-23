"""Tests for the admin edit_edge op — evidence type, mechanism, locked subtype
(docs/ticket-log.md#ticket-admin-edit-edge-details).

Run: python -m unittest tests/test_admin_edit_edge.py -v   (inside the ingest image)
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import duckdb  # noqa: E402

from src import admin_ops as ops, db, subtypes  # noqa: E402

WHO = {"email": "info@solutionjet.net", "ip": "203.0.113.1",
       "vocab": {"supplies", "pilots", "partners_with", "competes_with"}}


class TestEditEdge(unittest.TestCase):
    def setUp(self):
        self.con = duckdb.connect(":memory:")
        db.init_schema(self.con)
        ops.ensure_schema(self.con)
        self.tsmc = db.insert_entity(self.con, "TSMC", None, "public_company")
        self.tesla = db.insert_entity(self.con, "Tesla", None, "public_company")
        self.con.execute("""INSERT INTO catalysts (entry_id, sector, timestamp, raw_finding, headline, sentiment_label)
                            VALUES ('ROB-1', 'Robotics', CURRENT_DATE, 'x', 'h', 'bullish')""")
        cid = self.con.execute("SELECT catalyst_id FROM catalysts").fetchone()[0]
        self.con.execute("""INSERT INTO relationships (catalyst_id, entity_a_id, rel_type, entity_b_id, confidence,
                            evidence_type, mechanism) VALUES (?, ?, 'supplies', ?, 0.8, 'direct',
                            'TSMC manufactures Tesla''s AI5 chips.')""", [cid, self.tsmc, self.tesla])
        self.rel = self.con.execute("SELECT max(rel_id) FROM relationships").fetchone()[0]
        subtypes.refresh(self.con)

    def tearDown(self):
        self.con.close()

    def _row(self):
        return self.con.execute("""SELECT evidence_type, mechanism, rel_subtype, coalesce(rel_subtype_locked, false), rel_type
                                   FROM relationships WHERE rel_id = ?""", [self.rel]).fetchone()

    def _edit(self, **p):
        return ops.apply_edit(self.con, "edit_edge", {"rel_id": self.rel, **p}, **WHO)

    def test_evidence_and_mechanism_then_revert(self):
        self.assertEqual(self._row()[2], "manufactures_for")
        out = self._edit(evidence_type="web_grounded", mechanism="TSMC is reported to supply Tesla with chips.",
                         expect={"evidence_type": "direct", "mechanism": "TSMC manufactures Tesla's AI5 chips."})
        self.assertEqual(self._row()[:3], ("web_grounded", "TSMC is reported to supply Tesla with chips.", None))  # rules re-ran
        ops.revert_edit(self.con, out["edit_id"], email=WHO["email"], ip=WHO["ip"], vocab=WHO["vocab"])
        self.assertEqual(self._row()[:3], ("direct", "TSMC manufactures Tesla's AI5 chips.", "manufactures_for"))

    def test_locked_subtype_survives_rules_and_auto_restores_them(self):
        self._edit(rel_subtype="licenses_to", expect={"rel_subtype": "auto"})
        self.assertEqual(self._row()[2:4], ("licenses_to", True))
        subtypes.refresh(self.con)                                          # the rules never overwrite a lock
        self._edit(mechanism="TSMC fabricates chips for Tesla under contract.",
                   expect={"mechanism": "TSMC manufactures Tesla's AI5 chips."})
        self.assertEqual(self._row()[2:4], ("licenses_to", True))
        self._edit(rel_subtype="auto", expect={"rel_subtype": "licenses_to"})
        self.assertEqual(self._row()[2:4], ("manufactures_for", False))     # back to the rules

    def test_retype_lifts_the_lock(self):
        self._edit(rel_subtype="licenses_to", expect={"rel_subtype": "auto"})
        ops.apply_edit(self.con, "repoint_edge", {"rel_id": self.rel, "rel_type": "partners_with",
                                                  "expect": {"rel_type": "supplies"}}, **WHO)
        evidence, mech, sub, locked, rt = self._row()
        self.assertEqual((rt, locked), ("partners_with", False))
        self.assertNotEqual(sub, "licenses_to")

    def _revert(self, out):
        return ops.revert_edit(self.con, out["edit_id"], email=WHO["email"], ip=WHO["ip"], vocab=WHO["vocab"])

    def test_ci_review_revert_restores_null_and_short_mechanisms(self):
        self.con.execute("UPDATE relationships SET mechanism = NULL WHERE rel_id = ?", [self.rel])
        out = self._edit(evidence_type="inferred", mechanism="TSMC reportedly supplies Tesla's chips.",
                         expect={"evidence_type": "direct", "mechanism": None})
        self._revert(out)
        self.assertEqual(self._row()[:2], ("direct", None))                 # both fields really restored
        out = self._edit(mechanism="Only the mechanism changes here.", expect={"mechanism": None})
        self._revert(out)                                                   # used to raise "nothing to change"
        self.assertIsNone(self._row()[1])
        self.con.execute("UPDATE relationships SET mechanism = 'short' WHERE rel_id = ?", [self.rel])
        out = self._edit(mechanism="A proper sentence about the supply.", expect={"mechanism": "short"})
        self._revert(out)                                                   # used to hit the 10-char guard
        self.assertEqual(self._row()[1], "short")
        with self.assertRaises(ops.EditError):                              # already reverted
            self._revert(out)

    def test_ci_review_revert_of_retype_restores_the_locked_subtype(self):
        self._edit(rel_subtype="licenses_to", expect={"rel_subtype": "auto"})
        out = ops.apply_edit(self.con, "repoint_edge", {"rel_id": self.rel, "rel_type": "partners_with",
                                                        "expect": {"rel_type": "supplies"}}, **WHO)
        self.assertFalse(self._row()[3])
        self._revert(out)
        self.assertEqual(self._row()[2:], ("licenses_to", True, "supplies"))

    def test_guards(self):
        for p in ({"evidence_type": "rumour", "expect": {"evidence_type": "direct"}},
                  {"rel_subtype": "funds", "expect": {"rel_subtype": "auto"}},          # belongs to invests_in
                  {"mechanism": "short", "expect": {"mechanism": "TSMC manufactures Tesla's AI5 chips."}},
                  {}):
            with self.assertRaises(ops.EditError, msg=str(p)):
                self._edit(**p)
        with self.assertRaises(ops.EditError):                              # expect must name the current value
            self._edit(evidence_type="inferred")
        with self.assertRaises(ops.EditConflict):
            self._edit(evidence_type="inferred", expect={"evidence_type": "speculative"})

    def test_add_edge_with_evidence_and_locked_subtype(self):
        out = ops.apply_edit(self.con, "add_edge", {"entry_id": "ROB-1", "entity_a_id": self.tesla, "rel_type": "supplies",
                                                    "entity_b_id": self.tsmc, "mechanism": "Tesla licenses its FSD stack to TSMC.",
                                                    "evidence_type": "speculative", "rel_subtype": "sells_through"}, **WHO)
        row = self.con.execute("""SELECT evidence_type, rel_subtype, rel_subtype_locked FROM relationships WHERE rel_id = ?""",
                               [out["after"]["rel_id"]]).fetchone()
        self.assertEqual(row, ("speculative", "sells_through", True))
        with self.assertRaises(ops.EditError):
            ops.apply_edit(self.con, "add_edge", {"entry_id": "ROB-1", "entity_a_id": self.tesla, "rel_type": "pilots",
                                                  "entity_b_id": self.tsmc, "mechanism": "Tesla pilots TSMC robots here.",
                                                  "rel_subtype": "funds"}, **WHO)


if __name__ == "__main__":
    unittest.main()
