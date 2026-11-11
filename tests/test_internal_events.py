"""Tests for internal events (docs/ticket-internal-events.md): self-loops kept as status 'internal',
outside every live view and count; shown only as a card's `internal_events`.

Run: python -m unittest tests/test_internal_events.py -v   (inside the ingest image)
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import duckdb  # noqa: E402

from src import admin_ops as ops, db, entities, export, ingest  # noqa: E402

WHO = {"email": "info@solutionjet.net", "ip": "203.0.113.1",
       "vocab": {"deploys", "partners_with", "competes_with", "supplies"}}


class _Fixture(unittest.TestCase):
    def setUp(self):
        self.con = duckdb.connect(":memory:")
        db.init_schema(self.con)
        ops.ensure_schema(self.con)
        e = lambda n, t="public_company": db.insert_entity(self.con, n, None, t)  # noqa: E731
        self.tesla, self.optimus, self.figure = e("Tesla"), e("Optimus", "organization"), e("Figure")
        self.group = e("Humanoid Robots", "group")
        self.con.execute("""INSERT INTO catalysts (entry_id, sector, timestamp, raw_finding, headline, sentiment_label)
                            VALUES ('ROB-1', 'Robotics', CURRENT_DATE, 'Tesla deploys Optimus in its factories.',
                                    'Tesla deploys Optimus', 'bullish')""")
        self.cid = self.con.execute("SELECT catalyst_id FROM catalysts").fetchone()[0]

    def _rel(self, a, t, b, status="active", conf=0.9):
        self.con.execute("""INSERT INTO relationships (catalyst_id, entity_a_id, rel_type, entity_b_id, confidence,
                            evidence_type, mechanism, status) VALUES (?, ?, ?, ?, ?, 'direct', 'm', ?)""",
                         [self.cid, a, t, b, conf, status])
        return self.con.execute("SELECT max(rel_id) FROM relationships").fetchone()[0]

    def _status(self, rel_id):
        return self.con.execute("SELECT status FROM relationships WHERE rel_id = ?", [rel_id]).fetchone()[0]

    def tearDown(self):
        self.con.close()


class TestMerge(_Fixture):
    def test_directional_self_loop_becomes_internal_symmetric_rejected(self):
        dep = self._rel(self.tesla, "deploys", self.optimus)
        par = self._rel(self.tesla, "partners_with", self.optimus)
        other = self._rel(self.figure, "competes_with", self.tesla)
        entities.merge(self.con, self.optimus, self.tesla, confidence=None, reason="t", source="test")
        self.assertEqual(self._status(dep), "internal")
        self.assertEqual(self._status(par), "rejected")
        self.assertEqual(self._status(other), "active")               # between-entity edge untouched

    def test_merge_does_not_revive_a_dead_row(self):
        dead = self._rel(self.tesla, "deploys", self.optimus, status="rejected")
        entities.merge(self.con, self.optimus, self.tesla, confidence=None, reason="t", source="test")
        self.assertEqual(self._status(dead), "rejected")


class TestIngestEndToEnd(_Fixture):
    def test_write_extraction_drops_alias_self_loops(self):
        # CI audit W1: ingest can't tell "Optimus" → Tesla from a bad alias ("Samsung SDS" → Samsung SDI),
        # so it drops every self-loop; only a deliberate admin merge creates internal events.
        from src.config import load_config
        from src.extract import ExtractionResult
        self.con.execute("INSERT INTO entity_aliases (entity_id, alias, verified, source) VALUES (?, 'Optimus Gen 3', true, 'admin')",
                         [self.tesla])
        ents = [{"resolved": "Tesla", "mention": "Tesla", "type": "public_company", "kind": "organization"},
                {"resolved": "Optimus Gen 3", "mention": "Optimus Gen 3", "type": "public_company", "kind": "organization"}]
        rels = [{"entity_a": "Tesla", "rel_type": "deploys", "entity_b": "Optimus Gen 3", "confidence": 0.9,
                 "evidence_type": "direct", "mechanism": "Tesla deploys Optimus Gen 3 in its factories."},
                {"entity_a": "Tesla", "rel_type": "partners_with", "entity_b": "Optimus Gen 3", "confidence": 0.9,
                 "evidence_type": "direct", "mechanism": "noise"}]
        res = ExtractionResult(entry_id="ROB-2", prompt_version="t", model="t", reasoning="", significance_score=0.5,
                               entities=ents, relationships=rels, sentiment_label="bullish",
                               headline="Tesla deploys Optimus Gen 3", research_sources=[], dropped_low_confidence=[],
                               flagged_medium_confidence=[], context_text="Tesla deploys Optimus Gen 3 in its factories.")
        entry = {"entry_id": "ROB-2", "timestamp": "2026-11-10T00:00:00Z", "category": "Robotics",
                 "headline": "Tesla deploys Optimus Gen 3", "raw_finding": "Tesla deploys Optimus Gen 3 in its factories."}
        cid = ingest.write_extraction(self.con, entry, res, load_config())
        rows = self.con.execute("SELECT entity_a_id, rel_type, entity_b_id, status FROM relationships WHERE catalyst_id = ?",
                                [cid]).fetchall()
        self.assertEqual(rows, [])


class TestExportAndCounts(_Fixture):
    def test_card_lists_internal_events_and_counts_are_unchanged(self):
        self._rel(self.figure, "competes_with", self.tesla)
        self._rel(self.optimus, "competes_with", self.tesla)
        self._rel(self.optimus, "supplies", self.tesla)   # no LIMIT 1 ties: Tesla 3 > Optimus 2; competes 2 > supplies 1

        def snapshot():
            cards = export._load_cards(self.con, "Robotics", 100, time_window_days=0, min_edge_confidence=0.0,
                                       show_invalidated=False)
            stats = export._compute_stats(self.con, "Robotics")
            return cards, stats

        before_cards, before_stats = snapshot()
        loop = self._rel(self.tesla, "deploys", self.tesla, status="internal")
        self._rel(self.tesla, "deploys", self.tesla, status="internal", conf=0.5)   # restated: listed once
        cards, stats = snapshot()
        self.assertEqual(cards[0]["relationships"], before_cards[0]["relationships"])   # edge list identical
        self.assertEqual(stats, before_stats)                                           # counts identical
        self.assertEqual([(x["entity"], x["rel"]) for x in cards[0]["internal_events"]], [("Tesla", "deploys")])
        graph = export._build_graph_projection(cards)
        self.assertFalse(any(e["from"] == e["to"] for e in graph["edges"]))
        self.assertEqual(before_cards[0]["internal_events"], [])
        self.assertTrue(loop)


class TestAdminStatus(_Fixture):
    def test_internal_only_for_self_loops_and_loops_never_live(self):
        loop = self._rel(self.tesla, "deploys", self.tesla, status="rejected")
        edge = self._rel(self.figure, "competes_with", self.tesla)
        out = ops.apply_edit(self.con, "set_edge_status", {"rel_id": loop, "status": "internal",
                                                           "expect": {"status": "rejected"}}, **WHO)
        self.assertEqual(self._status(loop), "internal")
        with self.assertRaises(ops.EditError):
            ops.apply_edit(self.con, "set_edge_status", {"rel_id": edge, "status": "internal"}, **WHO)
        with self.assertRaises(ops.EditError):
            ops.apply_edit(self.con, "set_edge_status", {"rel_id": loop, "status": "active"}, **WHO)
        ops.revert_edit(self.con, out["edit_id"], email=WHO["email"], ip=WHO["ip"], vocab=WHO["vocab"])
        self.assertEqual(self._status(loop), "rejected")


if __name__ == "__main__":
    unittest.main()
