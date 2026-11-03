"""Tests for src/direction.py — direction check plumbing (no live LLM calls).

Run: python -m unittest tests/test_direction.py -v   (inside the ingest image)
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import duckdb  # noqa: E402

from src import db, direction  # noqa: E402

V = direction.DirectionVerdict


def _v(i, verdict):
    return V(index=i, statement="Hyundai deploys Atlas", verdict=verdict, reason="per the item text")


class TestApply(unittest.TestCase):
    def _kept(self):
        return [
            {"entity_a": "Boston Dynamics", "rel_type": "deploys", "entity_b": "Hyundai Motor Group"},
            {"entity_a": "Figure", "rel_type": "competes_with", "entity_b": "Tesla"},       # symmetric, not checked
            {"entity_a": "Hyundai Motor Group", "rel_type": "acquires", "entity_b": "Boston Dynamics"},
            {"entity_a": "NVIDIA", "rel_type": "supplies", "entity_b": "Hyundai Motor Group"},
        ]

    def test_flip_swaps_missing_keeps(self):
        kept = self._kept()
        checked = [0, 2, 3]                                   # indexes into kept of directional edges
        stats = direction.apply_to_extraction(kept, {0: _v(0, "flip"), 1: _v(1, "correct")}, checked)
        self.assertEqual(stats, {"checked": 3, "flipped": 1, "unclear": 0, "missing": 1, "deduped": 0})
        self.assertEqual((kept[0]["entity_a"], kept[0]["entity_b"]), ("Hyundai Motor Group", "Boston Dynamics"))
        self.assertEqual(len(kept), 4)                        # missing verdict → kept as-is (fail-open)

    def test_unclear_never_removes_or_changes(self):
        kept = self._kept()
        before = [dict(r) for r in kept]
        stats = direction.apply_to_extraction(kept, {0: _v(0, "unclear"), 1: _v(1, "unclear"),
                                                     2: _v(2, "unclear")}, [0, 2, 3])
        self.assertEqual(stats["unclear"], 3)
        self.assertEqual([(r["entity_a"], r["entity_b"]) for r in kept],
                         [(r["entity_a"], r["entity_b"]) for r in before])

    def test_flip_into_existing_edge_is_deduped(self):
        kept = [{"entity_a": "Boston Dynamics", "rel_type": "deploys", "entity_b": "Hyundai Motor Group", "confidence": 0.8},
                {"entity_a": "Hyundai Motor Group", "rel_type": "deploys", "entity_b": "Boston Dynamics", "confidence": 0.6}]
        stats = direction.apply_to_extraction(kept, {0: _v(0, "flip"), 1: _v(1, "correct")}, [0, 1])
        self.assertEqual(stats["deduped"], 1)
        self.assertEqual(kept, [{"entity_a": "Hyundai Motor Group", "rel_type": "deploys",
                                 "entity_b": "Boston Dynamics", "confidence": 0.8,
                                 "direction_statement": "Hyundai deploys Atlas"}])

    def test_only_directional_types_are_rules(self):
        vocab = {"vocabulary": [{"name": "deploys", "description": "A = customer", "directional": True},
                                {"name": "competes_with", "description": "rivals", "directional": False}]}
        self.assertEqual(direction.directional_types(vocab), {"deploys": "A = customer"})


class TestDb(unittest.TestCase):
    def setUp(self):
        self.con = duckdb.connect(":memory:")
        db.init_schema(self.con)
        self.con.execute("""INSERT INTO catalysts (entry_id, sector, timestamp, raw_finding, headline, sentiment_label)
                            VALUES ('ROB-1', 'Robotics', CURRENT_DATE, 'raw', 'h', 'bullish')""")
        self.cid = self.con.execute("SELECT catalyst_id FROM catalysts").fetchone()[0]
        self.h = db.insert_entity(self.con, "Hyundai Motor Group", None, "public_company")
        self.b = db.insert_entity(self.con, "Boston Dynamics", None, "private_company")
        self.con.execute("INSERT INTO entity_aliases (entity_id, alias) VALUES (?, 'Hyundai')", [self.h])
        self.con.execute("""INSERT INTO relationships (catalyst_id, entity_a_id, rel_type, entity_b_id, confidence)
                            VALUES (?, ?, 'acquires', ?, 0.9)""", [self.cid, self.h, self.b])

    def tearDown(self):
        self.con.close()

    def test_on_record_matches_aliases(self):
        lines = direction.on_record_lookup(self.con)(["Hyundai", "boston dynamics"])
        self.assertEqual(lines, ["Hyundai Motor Group --acquires--> Boston Dynamics (1 card)"])

    def test_invalidated_rows_not_on_record(self):
        self.con.execute("UPDATE relationships SET status = 'invalidated'")
        self.assertEqual(direction.on_record_lookup(self.con)(["Hyundai", "Boston Dynamics"]), [])

    def test_dedupe_rows_keeps_highest_confidence_per_card(self):
        for conf in (0.5, 0.95):   # a flipped copy now equals the original edge on the same card
            self.con.execute("""INSERT INTO relationships (catalyst_id, entity_a_id, rel_type, entity_b_id, confidence)
                                VALUES (?, ?, 'acquires', ?, ?)""", [self.cid, self.h, self.b, conf])
        self.assertEqual(direction.dedupe_rows(self.con, [self.cid]), 2)
        self.assertAlmostEqual(self.con.execute("SELECT confidence FROM relationships").fetchone()[0], 0.95, places=5)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM relationships").fetchone()[0], 1)
        self.assertEqual(direction.dedupe_rows(self.con, []), 0)

    def test_reextract_delete_clears_direction_audit(self):
        self.con.execute("INSERT INTO direction_audits (entry_id, model, checked, flipped, unclear, source) "
                         "VALUES ('ROB-1', 'm', 1, 0, 0, 'ingest')")
        self.assertEqual(db.delete_by_entry_id(self.con, "ROB-1"), 1)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM direction_audits").fetchone()[0], 0)

    def test_in_place_swap_on_indexed_columns(self):
        # direction_audit flips with one UPDATE; entity columns are indexed + FK'd.
        rid = self.con.execute("SELECT rel_id FROM relationships").fetchone()[0]
        self.con.execute("UPDATE relationships SET entity_a_id = entity_b_id, entity_b_id = entity_a_id "
                         "WHERE rel_id = ?", [rid])
        self.assertEqual(self.con.execute("SELECT entity_a_id, entity_b_id FROM relationships").fetchone(),
                         (self.b, self.h))


if __name__ == "__main__":
    unittest.main()
