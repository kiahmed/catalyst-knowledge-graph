"""Tests for src/story.py + the export's in_story tagging.

in_story = the card's OWN news text names the entity (relevance, not truth).
Background edges must be tagged, never dropped.

Run: python -m unittest tests/test_story.py -v   (inside the ingest image)
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import duckdb  # noqa: E402

from src import db, export, story  # noqa: E402


class TestMentioned(unittest.TestCase):
    A = {"Hanwha Aerospace": {"Hanwha Aerospace", "Hanwha"},
         "LG Electronics (066570.KS)": {"LG Electronics (066570.KS)", "LG Electronics"},
         "3M": {"3M"}}

    def test_name_alias_and_ticker_stripped_case_insensitive(self):
        self.assertTrue(story.mentioned("Hanwha Aerospace", "Krafton partners with HANWHA on drones", self.A))
        self.assertTrue(story.mentioned("LG Electronics (066570.KS)", "lg electronics invests", self.A))
        self.assertTrue(story.mentioned("NVIDIA", "Nvidia supplies chips", {}))

    def test_whole_word_only(self):
        self.assertFalse(story.mentioned("Arm", "The army bought pharma robots", {}))

    def test_common_word_names_need_proper_noun_case(self):
        self.assertTrue(story.mentioned("Figure", "Figure raised $1B", {}))
        self.assertFalse(story.mentioned("Figure", "a key figure in robotics", {}))
        self.assertFalse(story.mentioned("Arm", "a robotic arm picks parts", {}))
        self.assertTrue(story.mentioned("Apple", "Apple tests humanoids", {}))

    def test_short_names_exact_case(self):
        self.assertTrue(story.mentioned("3M", "3M deploys cobots", self.A))
        self.assertFalse(story.mentioned("3M", "a 3m-long arm", self.A))


class TestExportTagging(unittest.TestCase):
    def setUp(self):
        self.con = duckdb.connect(":memory:")
        db.init_schema(self.con)
        self.ids = {}

    def tearDown(self):
        self.con.close()

    def _ent(self, n):
        if n not in self.ids:
            self.ids[n] = db.insert_entity(self.con, n, None, "private_company")
        return self.ids[n]

    def _card(self, entry, headline, finding, edges):
        self.con.execute(
            """INSERT INTO catalysts (entry_id, sector, timestamp, raw_finding, headline, sentiment_label)
               VALUES (?, 'Robotics', CURRENT_DATE, ?, ?, 'bullish')""", [entry, finding, headline])
        cid = self.con.execute("SELECT catalyst_id FROM catalysts WHERE entry_id=?", [entry]).fetchone()[0]
        for a, rel, b in edges:
            self.con.execute(
                """INSERT INTO relationships (catalyst_id, entity_a_id, rel_type, entity_b_id, confidence, status)
                   VALUES (?, ?, ?, ?, 0.9, 'active')""", [cid, self._ent(a), rel, self._ent(b)])

    def _cards(self):
        return {c["card_id"]: c for c in export._load_cards(self.con, "Robotics", 100, 0, 0.0, False)}

    def test_story_vs_background_and_selfloops(self):
        self._card("ROB-S1", "Krafton partners with Hanwha Aerospace", "Krafton and Hanwha Aerospace sign $1B deal.",
                   [("Krafton", "partners_with", "Hanwha Aerospace"),
                    ("Krafton", "competes_with", "Hyundai Rotem"),       # story (Krafton named)
                    ("LIG Nex1", "partners_with", "Palantir"),           # background
                    ("NVIDIA", "built_on", "NVIDIA")])                   # self-loop → dropped
        c = self._cards()["ROB-S1"]
        tags = {(r["from"], r["to"]): r["in_story"] for r in c["relationships"]}
        self.assertEqual(tags, {("Krafton", "Hanwha Aerospace"): True,
                                ("Krafton", "Hyundai Rotem"): True,
                                ("LIG Nex1", "Palantir"): False})
        self.assertTrue(c["story_names_entities"])
        ents = {e["name"]: e["in_story"] for e in c["entities"]}
        self.assertTrue(ents["Krafton"]); self.assertFalse(ents["Palantir"])

    def test_market_report_names_no_company(self):
        self._card("ROB-S2", "Physical AI startups raise $47B in H1",
                   "Venture funding for Physical AI startups reached $47.4B.",
                   [("NVIDIA", "invests_in", "Figure")])
        c = self._cards()["ROB-S2"]
        self.assertFalse(c["story_names_entities"])
        self.assertEqual([r["in_story"] for r in c["relationships"]], [False])   # tagged, not dropped


class TestLayoutStability(unittest.TestCase):
    """Graph-tab positions must not reshuffle as the graph grows."""

    def setUp(self):
        self.t = TestExportTagging(); self.t.setUp()
        for i in range(12):
            self.t._card(f"ROB-L{i}", f"Co{i} deal", f"Co{i} partners with Co{i+1}",
                         [(f"Co{i}", "partners_with", f"Co{i+1}")])

    def tearDown(self):
        self.t.tearDown()

    def test_identical_rerun_and_growth_keep_known_nodes_fixed(self):
        a = export.full_history_layout(self.t.con, "Robotics")
        b = export.full_history_layout(self.t.con, "Robotics")
        self.assertEqual(a, b)                                    # identical data → zero movement
        self.t._card("ROB-LX", "Aardvark joins", "Aardvark partners with Co5",
                     [("Aardvark", "partners_with", "Co5")])      # sorts FIRST
        c = export.full_history_layout(self.t.con, "Robotics")
        self.assertEqual({k: c[k] for k in a}, a)                 # known nodes untouched
        self.assertIn("Aardvark", c)


if __name__ == "__main__":
    unittest.main()
