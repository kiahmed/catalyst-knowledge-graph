"""Tests for src/subtypes.py — the deterministic rel_subtype rules and their refresh.

Cases are real prod mechanisms from the 2026-11-11 eval (data/subtype_rules_eval_20261111.txt),
including every false positive found while tuning.
Run: python -m unittest tests/test_subtypes.py -v   (inside the ingest image)
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import duckdb  # noqa: E402

from src import admin_ops as ops, db, hashtags, subtypes  # noqa: E402

C = subtypes.classify


class TestRules(unittest.TestCase):
    def test_every_subtype_rolls_up_to_its_parent(self):
        for parent, subs in subtypes.SUBTYPES.items():
            for s in subs:
                self.assertEqual(subtypes.parent_of(s), parent)
        self.assertIsNone(subtypes.parent_of("nope"))

    def test_other_types_never_get_a_subtype(self):
        self.assertIsNone(C("deploys", "Ford deploys Atlas under a joint venture licensing grant.", "direct",
                            a_name="Ford", b_name="Boston Dynamics"))

    def test_competes(self):
        self.assertEqual(C("competes_with", "Xiaomi's CyberOne directly competes with Agility Robotics' Digit.",
                           "inferred"), "rivals")
        self.assertEqual(C("competes_with", "Pai Technology is explicitly identified as a competitor.", "direct"),
                         "rivals")                                 # stated in the story → rivals
        self.assertEqual(C("competes_with", "Tesla's Optimus will intensify competition with 1X.", "speculative"),
                         "pressures")
        self.assertIsNone(C("competes_with", None, "direct"))

    def test_funds(self):
        self.assertEqual(C("invests_in", "Defra provided grants through its Farming Innovation Programme.", "direct",
                           a_type="government"), "funds")
        self.assertEqual(C("invests_in", "The district provides access to research facilities.", "direct",
                           a_type="government"), "funds")          # a government source is public money
        self.assertEqual(C("invests_in", "METI's project is designed to subsidize robotics makers.", "inferred"),
                         "funds")
        self.assertNotEqual(C("invests_in", "Waymo is a subsidiary of Alphabet.", "direct"), "funds")
        self.assertIsNone(C("invests_in", "Agility is part of the Amazon Industrial Innovation Fund.", "direct",
                            a_type="public_company"))
        self.assertIsNone(C("invests_in", "Noetra, a government-backed company, has investments from NEC.", "direct",
                            a_type="public_company"))              # describes the target, not this edge

    def test_owns(self):
        self.assertEqual(C("invests_in", "Cruise operates as a subsidiary of General Motors.", "direct"), "owns")
        self.assertEqual(C("invests_in", "Ant Group, as the parent company, commits AI compute to Robbyant.", "direct"),
                         "owns")
        self.assertIsNone(C("invests_in", "Google (parent company of DeepMind) led Apptronik's Series A.", "direct",
                            a_name="Alphabet", b_name="Apptronik"))
        self.assertEqual(C("invests_in", "Hyundai is the parent company of Boston Dynamics.", "direct",
                           a_name="Hyundai Motor Group", b_name="Boston Dynamics"), "owns")

    def test_partners(self):
        self.assertEqual(C("partners_with", "SKF and Leaderdrive formed a joint venture in China.", "direct"),
                         "joint_venture")
        self.assertEqual(C("partners_with", "Fujitsu and CMU launched a joint Physical AI Research Center.", "direct"),
                         "research_collaboration")
        self.assertEqual(C("partners_with", "Fanuc and Google will jointly develop industrial robot systems.", "direct"),
                         "co_development")
        self.assertIsNone(C("partners_with", "Infineon and Microsoft co-founded an accelerator.", "direct"))

    def test_manufactures_for(self):
        k = dict(a_name="Samsung SDI", b_name="Tesla")
        self.assertEqual(C("supplies", "Samsung manufactures AI memory chips for Tesla.", "direct", **k),
                         "manufactures_for")
        self.assertEqual(C("supplies", "TSMC is involved in the fabrication of Tesla's AI5 chip.", "direct",
                           a_name="TSMC", b_name="Tesla"), "manufactures_for")
        self.assertEqual(C("supplies", "Foxconn serves as primary contract manufacturer for Digit humanoids.",
                           "direct", a_name="Foxconn", b_name="Agility Robotics"), "manufactures_for")
        # a supplier selling its own catalogue is plain supplies
        self.assertIsNone(C("supplies", "Ouster manufactures digital lidar sensors that are essential for the advanced "
                                        "navigation of humanoid robots like UBTECH's U1.", "direct",
                            a_name="Ouster", b_name="UBTECH"))
        # the TARGET is the manufacturer → not "source manufactures for target"
        self.assertIsNone(C("supplies", "Qualcomm chips power modules that SECO manufactures for its customers.",
                            "direct", a_name="Qualcomm", b_name="SECO"))
        # direction can't be checked without names → no subtype
        self.assertIsNone(C("supplies", "X manufactures chips for Y.", "direct"))

    def test_licenses_and_sells_through(self):
        self.assertEqual(C("supplies", "Ceva licenses its NeuPro NPU IP to Mobilint.", "direct",
                           a_name="Ceva", b_name="Mobilint"), "licenses_to")
        self.assertEqual(C("supplies", "RobotLab has distributed Unitree's robots in the U.S.", "direct",
                           a_name="Unitree", b_name="RobotLab"), "sells_through")
        self.assertIsNone(C("supplies", "NVIDIA powers distributed robotics environments at Coherent.", "direct",
                            a_name="NVIDIA", b_name="Coherent"))
        self.assertIsNone(C("supplies", "Toyota is distributing 250,000 ELEY humanoid robots across its group "
                                        "companies including Denso.", "direct", a_name="Toyota", b_name="Denso"))
        self.assertIsNone(C("supplies", "Hyundai Glovis will manage logistics for the production and distribution "
                                        "of Atlas robots.", "direct", a_name="Hyundai Glovis", b_name="Boston Dynamics"))


class TestRefresh(unittest.TestCase):
    def setUp(self):
        self.con = duckdb.connect(":memory:")
        db.init_schema(self.con)
        ops.ensure_schema(self.con)
        e = lambda n, t="private_company": db.insert_entity(self.con, n, None, t)  # noqa: E731
        self.gm, self.cruise, self.gov, self.waymo = e("General Motors"), e("Cruise"), e("UK Government", "government"), \
            e("Waymo")
        self.con.execute("""INSERT INTO catalysts (entry_id, sector, timestamp, raw_finding, headline, sentiment_label)
                            VALUES ('ROB-1', 'Robotics', CURRENT_DATE, 'x', 'h', 'bullish')""")
        self.cid = self.con.execute("SELECT catalyst_id FROM catalysts").fetchone()[0]
        for a, t, b, m in ((self.gm, "invests_in", self.cruise, "Cruise is a subsidiary of General Motors."),
                           (self.gm, "competes_with", self.waymo, "GM's robotaxi will add pressure on Waymo."),
                           (self.gm, "deploys", self.waymo, "plain")):
            self.con.execute("""INSERT INTO relationships (catalyst_id, entity_a_id, rel_type, entity_b_id, confidence,
                                evidence_type, mechanism) VALUES (?, ?, ?, ?, 0.8, 'inferred', ?)""",
                             [self.cid, a, t, b, m])

    def tearDown(self):
        self.con.close()

    def _subs(self):
        return dict(self.con.execute("SELECT rel_type, rel_subtype FROM relationships").fetchall())

    def test_backfill_is_idempotent_and_never_touches_rel_type(self):
        types = self.con.execute("SELECT rel_id, rel_type, status FROM relationships ORDER BY 1").fetchall()
        self.assertEqual(subtypes.refresh(self.con), 2)
        self.assertEqual(self._subs(), {"invests_in": "owns", "competes_with": "pressures", "deploys": None})
        self.assertEqual(subtypes.refresh(self.con), 0)                     # second run: nothing to write
        self.assertEqual(self.con.execute("SELECT rel_id, rel_type, status FROM relationships ORDER BY 1").fetchall(),
                         types)
        self.assertEqual(subtypes.refresh(self.con, []), 0)

    def test_bulk_path(self):
        for i in range(60):    # > 50 changes in one call
            self.con.execute("""INSERT INTO relationships (catalyst_id, entity_a_id, rel_type, entity_b_id, confidence,
                                evidence_type, mechanism) VALUES (?, ?, 'competes_with', ?, 0.8, 'direct', ?)""",
                             [self.cid, self.gm, self.waymo, f"m{i}"])
        self.assertEqual(subtypes.refresh(self.con), 62)
        self.assertEqual(self.con.execute("SELECT count(*) FROM relationships WHERE rel_subtype = 'rivals'").fetchone()[0],
                         60)

    def test_card_hook_refreshes(self):
        hashtags.store_for_catalyst(self.con, self.cid)                     # every edge-changing path calls this
        self.assertEqual(self._subs()["invests_in"], "owns")

    def test_admin_repoint_recomputes_and_revert_restores(self):
        subtypes.refresh(self.con)
        rel = self.con.execute("SELECT rel_id FROM relationships WHERE rel_type = 'invests_in'").fetchone()[0]
        who = {"email": "info@solutionjet.net", "ip": "203.0.113.1", "vocab": {"invests_in", "competes_with", "deploys"}}
        out = ops.apply_edit(self.con, "repoint_edge", {"rel_id": rel, "entity_a_id": self.gov,
                                                        "expect": {"entity_a_id": self.gm}}, **who)
        self.assertEqual(self._subs()["invests_in"], "funds")               # a government source now
        ops.revert_edit(self.con, out["edit_id"], email=who["email"], ip=who["ip"], vocab=who["vocab"])
        self.assertEqual(self._subs()["invests_in"], "owns")


if __name__ == "__main__":
    unittest.main()
