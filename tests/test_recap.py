"""Tests for src/recap.py — weekly recap chart cards (spec §2.9b).

Cards are published as images next to the digest, so assertions check the
CLAIMS: counts match the headline window, inferred is never counted as
reported, generic category nodes never top a chart, ties aren't "leads".

Run: python -m unittest tests/test_recap.py -v   (inside the ingest image)
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import duckdb  # noqa: E402

from src import db, recap  # noqa: E402


class _Fx:
    def __init__(self, con):
        self.con, self._ents, self._cat = con, {}, 0
        db.init_schema(con)

    def _e(self, name):
        if name not in self._ents:
            self._ents[name] = db.insert_entity(self.con, name, None, "private_company")
        return self._ents[name]

    def edge(self, a, b, rel, days_ago, evidence="direct"):
        ea, eb = self._e(a), self._e(b)
        self._cat += 1
        entry = f"ROB-R{self._cat:05d}"
        self.con.execute(
            f"""INSERT INTO catalysts (entry_id, sector, timestamp, raw_finding, headline, sentiment_label)
                VALUES (?, 'Robotics', CURRENT_DATE - INTERVAL {int(days_ago)} DAY, 'raw', 'h', 'bullish')""",
            [entry])
        cid = self.con.execute("SELECT catalyst_id FROM catalysts WHERE entry_id = ?", [entry]).fetchone()[0]
        self.con.execute(
            """INSERT INTO relationships (catalyst_id, entity_a_id, rel_type, entity_b_id,
                   confidence, evidence_type, status) VALUES (?, ?, ?, ?, 0.9, ?, 'active')""",
            [cid, ea, rel, eb, evidence])


def _ins_entity(name, recent, base):
    return {"type": "chokepoint", "entity": name, "window_days": 30, "baseline_days": 90,
            "recent_count": recent, "baseline_count": base, "headline": f"{name}: h"}


def _ins_trend(rel, recent, base):
    return {"type": "velocity", "rel_type": rel, "window_days": 30, "baseline_days": 90,
            "recent_count": recent, "baseline_count": base, "headline": f"{rel}: h"}


class TestRecap(unittest.TestCase):
    def setUp(self):
        self.con = duckdb.connect(":memory:")
        self.fx = _Fx(self.con)

    def tearDown(self):
        self.con.close()

    def _cards(self, insights):
        r = recap.build_recap(self.con, "Robotics", insights, today=date(2026, 10, 26))
        return r, {c["id"]: c for c in r["cards"]}

    def test_mix_counts_match_window_and_group_types(self):
        for i in range(5):
            self.fx.edge("Boost", f"R{i}", "competes_with", 5)
        for i in range(2):
            self.fx.edge("Boost", f"D{i}", "displaces", 5)       # same group
        self.fx.edge("Boost", "P0", "partners_with", 5)
        for i in range(3):
            self.fx.edge("Boost", f"O{i}", "partners_with", 60)   # baseline
        self.fx.edge("Boost", "X", "partners_with", 200)          # outside both windows
        r, cards = self._cards([_ins_entity("Boost", 8, 3)])
        mix = cards["entity-boost"]
        self.assertEqual(mix["kind"], "entity")
        self.assertFalse(mix["hedged"])                         # all direct → firm verdict
        self.assertEqual(r["week"], "2026-W44")
        self.assertEqual(r["schema"], 2)
        self.assertEqual(mix["data"]["mix"]["now"], {"Competition": 7, "Partnership": 1})
        self.assertEqual(mix["data"]["mix"]["before"], {"Partnership": 3})
        self.assertEqual(mix["big"], "8 new connections")
        self.assertEqual(mix["sentence"], "Mostly rivalry: 7 of 8.")
        self.assertEqual(mix["title"], "What's behind Boost's surge?")
        self.assertEqual(mix["verdict"], "Boost is now a target: rivals are lining up.")
        self.assertLessEqual(len(mix["verdict"].split()), 10)
        self.assertEqual(mix["hashtags"], ["#Boost", "#Competition", "#Robotics"])

    def test_grounding_never_counts_inferred_as_reported(self):
        self.fx.edge("Neura", "A", "partners_with", 3, "direct")
        self.fx.edge("Neura", "B", "partners_with", 3, "web_grounded")
        self.fx.edge("Neura", "C", "partners_with", 3, "inferred")
        self.fx.edge("Neura", "D", "partners_with", 3, "speculative")
        _, cards = self._cards([_ins_entity("Neura", 4, 0)])
        card = cards["entity-neura"]                    # confirmation lives ON the company card
        g = card["confirmation"]
        self.assertEqual(g["big"], "50% confirmed")
        self.assertEqual(g["sentence"], "2 are reported or cited by sources; 2 are our model's inference.")
        self.assertEqual(card["data"]["grounding"], {"direct": 1, "web_grounded": 1, "inferred": 1, "speculative": 1})
        self.assertEqual(g["verdict"], "Mixed: about half is our own inference.")
        self.assertNotIn("grounding-neura", cards)                # no standalone confirmation card

    def test_concentration_skips_generic_nodes_and_counts_edges_not_ends(self):
        for i in range(4):
            self.fx.edge(f"Bot{i}", "Physical AI Sector", "built_on", 4)   # generic → never ranked
        for i in range(3):
            self.fx.edge(f"Arm{i}", "NVIDIA", "built_on", 4)
        for i in range(4):
            self.fx.edge(f"Old{i}", "NVIDIA", "built_on", 60)
        _, cards = self._cards([_ins_trend("built_on", 7, 4)])
        c = cards["sector-built_on"]
        self.assertEqual(c["data"]["total"], 7)
        self.assertEqual(c["data"]["top"][0]["entity"], "NVIDIA")
        self.assertNotIn("Physical AI Sector", [t["entity"] for t in c["data"]["top"]])
        self.assertEqual(c["big"], "NVIDIA 43%")
        self.assertIn("down from 100% before", c["sentence"])
        self.assertEqual(c["verdict"], "NVIDIA is becoming the default: a dependency risk.")
        self.assertEqual(c["hashtags"], ["#NVIDIA", "#TechStack", "#Robotics"])

    def test_two_sided_rel_shares_use_edge_total(self):
        for i in range(4):
            self.fx.edge("Hub", f"S{i}", "integrates_with", 4)
        _, cards = self._cards([_ins_trend("integrates_with", 4, 0)])
        c = cards["sector-integrates_with"]
        self.assertEqual(c["data"]["total"], 4)          # edges, not 8 endpoints
        self.assertEqual(c["big"], "Hub 100%")

    def test_ties_are_not_called_a_lead(self):
        for i in range(2):
            self.fx.edge(f"A{i}", "Alpha", "built_on", 4)
            self.fx.edge(f"B{i}", "Beta", "built_on", 4)
        _, cards = self._cards([_ins_trend("built_on", 4, 0)])
        c = cards["sector-built_on"]
        self.assertEqual(c["big"], "2-way tie at 50%")
        self.assertIn("Alpha and Beta are tied at 50% each", c["sentence"])
        self.assertEqual(c["verdict"], "Shared control: 2 names lead this together.")

    def test_scope_is_one_card_per_entity_plus_one_trend(self):
        names = ("E1", "E2", "E3", "E4", "E5", "E6")
        for name in names:
            self.fx.edge(name, "Z", "partners_with", 3)
        ins = [_ins_entity(n, 1, 0) for n in names] + [_ins_trend("partners_with", 3, 0)]
        r, cards = self._cards(ins)
        kinds = [c["kind"] for c in r["cards"]]
        self.assertEqual(kinds.count("entity"), 5)                 # covers digest picks past the top 2
        self.assertEqual(kinds.count("sector"), 1)
        self.assertNotIn("entity-e6", cards)                        # MAX_ENTITY_CARDS = 5
        self.assertEqual(r["cards"][0]["id"], "entity-e1")          # lead company first
        self.assertEqual(r["cards"][1]["kind"], "sector")
        self.assertEqual(set(kinds), {"entity", "sector"})          # no standalone grounding cards

    def test_insight_rows_and_digest_get_hashtags(self):
        self.fx.edge("Neura Robotics", "Z", "partners_with", 3)
        ins = [dict(_ins_entity("Neura Robotics", 1, 0), top_rel="partners_with"),
               _ins_trend("built_on", 1, 0)]
        recap.tag_insights(ins, "Robotics")
        self.assertEqual(ins[0]["hashtags"], ["#NeuraRobotics", "#Partnership", "#Robotics"])
        self.assertEqual(ins[1]["hashtags"], ["#TechStack", "#Robotics"])
        r, _ = self._cards(ins)
        self.assertEqual(r["hashtags"], ["#NeuraRobotics", "#TechStack", "#Robotics"])

    def test_no_prior_activity_is_not_called_a_rate(self):
        self.fx.edge("Fresh", "Z", "partners_with", 3)
        _, cards = self._cards([_ins_entity("Fresh", 1, 0)])
        self.assertIn("none in the prior 90 days", cards["entity-fresh"]["sub"])

    def test_verdict_hedged_when_mostly_inferred(self):
        for i in range(3):
            self.fx.edge("Infr", f"P{i}", "partners_with", 3, "inferred")
        self.fx.edge("Infr", "Q", "partners_with", 3, "direct")       # 25% confirmed
        _, cards = self._cards([_ins_entity("Infr", 4, 0)])
        c = cards["entity-infr"]
        self.assertTrue(c["hedged"])
        self.assertEqual(c["verdict"], "Infr may be becoming a hub others join.")
        self.assertLessEqual(len(c["verdict"].split()), 10)

    def test_hedge_uses_the_same_rounded_pct_as_the_panel(self):
        # 79 inferred + 52 direct = 39.69% → panel shows "40% confirmed", so
        # the verdict must NOT be hedged (the <40% rule reads the shown value).
        for i in range(52):
            self.fx.edge("Edge", f"D{i}", "partners_with", 3, "direct")
        for i in range(79):
            self.fx.edge("Edge", f"I{i}", "partners_with", 3, "inferred")
        _, cards = self._cards([_ins_entity("Edge", 131, 0)])
        c = cards["entity-edge"]
        self.assertEqual(c["confirmation"]["big"], "40% confirmed")
        self.assertFalse(c["hedged"])

    def test_no_insights_means_no_recap(self):
        self.assertIsNone(recap.build_recap(self.con, "Robotics", []))


if __name__ == "__main__":
    unittest.main()
