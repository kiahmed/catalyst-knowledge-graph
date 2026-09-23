"""Tests for src/hashtags.py — per-catalyst hashtags (spec §2.4 catalysts.hashtags).

Run: python -m unittest tests/test_hashtags.py -v   (inside the ingest image)
"""
from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import duckdb  # noqa: E402

from src import db, hashtags  # noqa: E402


def _rel(a, b, rel, evidence="direct", conf=0.9):
    return {"from": a, "to": b, "rel": rel, "confidence": conf, "evidence_type": evidence}


class TestCamelTag(unittest.TestCase):
    def test_strips_ticker_and_legal_suffix(self):
        self.assertEqual(hashtags.camel_tag("LG Electronics Inc. (066570.KS)"), "#LGElectronics")

    def test_rejects_generic_plural_names(self):
        self.assertIsNone(hashtags.camel_tag("Chinese Robotics Companies"))

    def test_rejects_overlong(self):
        self.assertIsNone(hashtags.camel_tag("A Very Long Organization Name Indeed Corp"))


class TestTagsFor(unittest.TestCase):
    types = {"Neura Robotics": "private_company", "Tesla": "public_company",
             "Germany": "country"}

    def test_priority_order_entity_topic_theme_sector(self):
        tags = hashtags.tags_for(
            "Robotics", "Neura Robotics raises $1.4B for physical AI",
            "Embodied AI platform funding.",
            [_rel("Neura Robotics", "Tesla", "competes_with", "inferred")],
            self.types)
        self.assertEqual(tags, ["#NeuraRobotics", "#Tesla", "#Funding",
                                "#PhysicalAI", "#Robotics"])

    def test_headline_event_beats_weak_edge(self):
        tags = hashtags.tags_for(
            "Robotics", "Acme acquires Beta", "",
            [_rel("Acme", "Beta", "competes_with", "speculative")],
            {"Acme": "private_company", "Beta": "private_company"})
        self.assertIn("#Acquisition", tags)
        self.assertNotIn("#Competition", tags)

    def test_non_company_entities_skipped(self):
        tags = hashtags.tags_for(
            "Robotics", "Germany backs robotics", "",
            [_rel("Germany", "Tesla", "regulates")], self.types)
        self.assertNotIn("#Germany", tags)

    def test_sector_floor_and_cap(self):
        tags = hashtags.tags_for("Robotics", "Nothing notable", "", [], {})
        self.assertEqual(tags, ["#Robotics"])
        many = hashtags.tags_for(
            "Robotics", "A partners with B on humanoid warehouse drones",
            "humanoid physical ai warehouse drones cobots", [
                _rel("A1", "B1", "partners_with"), _rel("C1", "D1", "partners_with")],
            {n: "private_company" for n in ("A1", "B1", "C1", "D1")})
        self.assertLessEqual(len(many), hashtags.MAX_TAGS)
        self.assertEqual(len(many), len(set(many)))
        self.assertEqual(many[-1], "#Robotics")

    def test_farm_metaphor_not_agtech(self):
        tags = hashtags.tags_for("Robotics", "Data learning farms scale up", "", [], {})
        self.assertNotIn("#AgTech", tags)


class TestStorage(unittest.TestCase):
    def setUp(self):
        self.con = duckdb.connect(":memory:")
        db.init_schema(self.con)
        self.con.execute(
            """INSERT INTO catalysts (entry_id, sector, timestamp, raw_finding,
                   headline, sentiment_label)
               VALUES ('ROB-T1', 'Robotics', CURRENT_DATE, 'raw',
                       'Cognex acquires RealSense', 'bullish')""")
        self.cid = self.con.execute("SELECT catalyst_id FROM catalysts").fetchone()[0]
        a = db.insert_entity(self.con, "Cognex", "CGNX", "public_company")
        b = db.insert_entity(self.con, "RealSense", None, "private_company")
        self.con.execute(
            """INSERT INTO relationships (catalyst_id, entity_a_id, rel_type,
                   entity_b_id, confidence, status)
               VALUES (?, ?, 'acquires', ?, 0.95, 'active')""", [self.cid, a, b])

    def tearDown(self):
        self.con.close()

    def _stored(self):
        return self.con.execute(
            "SELECT hashtags FROM catalysts WHERE catalyst_id = ?", [self.cid]).fetchone()[0]

    def test_init_schema_adds_column_idempotently(self):
        db.init_schema(self.con)
        self.assertIsNone(self._stored())

    def test_backfill_tags_null_rows_then_noops(self):
        self.assertEqual(hashtags.backfill(self.con), 1)
        self.assertEqual(json.loads(self._stored()),
                         ["#Cognex", "#RealSense", "#Acquisition", "#Robotics"])
        self.assertEqual(hashtags.backfill(self.con), 0)
        self.assertEqual(hashtags.backfill(self.con, force=True), 1)

    def test_invalidated_edges_ignored(self):
        self.con.execute("UPDATE relationships SET status = 'invalidated'")
        self.assertEqual(hashtags.store_for_catalyst(self.con, self.cid),
                         ["#Acquisition", "#Robotics"])


if __name__ == "__main__":
    unittest.main()
