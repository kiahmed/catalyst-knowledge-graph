"""Tests for src/resolve.py — exact tier + LLM decision tier (fake client, no live calls).

Run: python -m unittest tests/test_resolve.py -v   (inside the ingest image)
"""
from __future__ import annotations

import os
import sys
import unittest
from types import SimpleNamespace as N

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import duckdb  # noqa: E402

from src import db, entities, resolve  # noqa: E402

CFG = N(resolution=N(llm=True, min_confidence=0.8),
        extraction=N(model="gemini-3.6-flash", direction_thinking_level="high"))


class FakeClient:
    """client.models.generate_content(...) → .parsed = the canned NameDecisions."""
    def __init__(self, decisions):
        self.calls = []
        self.models = self
        self._d = resolve.NameDecisions(names=[resolve.NameDecision(**d) for d in decisions])

    def generate_content(self, **kw):
        self.calls.append(kw["contents"])
        return N(parsed=self._d, text="")


class TestResolve(unittest.TestCase):
    def setUp(self):
        self.con = duckdb.connect(":memory:")
        db.init_schema(self.con)
        self.con.execute("""INSERT INTO catalysts (entry_id, sector, timestamp, raw_finding, headline, sentiment_label)
                            VALUES ('ROB-1', 'Robotics', CURRENT_DATE, 'raw', 'h', 'bullish')""")
        self.cid = self.con.execute("SELECT catalyst_id FROM catalysts").fetchone()[0]
        self.hmg = db.insert_entity(self.con, "Hyundai Motor Group", None, "public_company")
        self.rotem = db.insert_entity(self.con, "Hyundai Rotem", None, "public_company")
        self.eu = db.insert_entity(self.con, "European Union", None, "government")
        entities.link(self.con, self.rotem, self.hmg, "subsidiary_of", confidence=1, reason="r", source="test")
        # Shares no word with any test name: reachable as a candidate ONLY via the family lookup.
        self.bd = db.insert_entity(self.con, "Boston Dynamics", None, "private_company")
        entities.link(self.con, self.bd, self.hmg, "subsidiary_of", confidence=1, reason="r", source="test")

    def tearDown(self):
        self.con.close()

    def _resolve(self, name, decision=None, mention=None):
        return resolve.resolve_entity(self.con, resolved_name=name, mention=mention or name, ticker=None,
                                      type_="public_company", catalyst_id=self.cid, cfg=CFG, decision=decision)

    def test_lookalike_without_llm_verdict_is_new_not_merged(self):
        # The real bug: the old fuzzy tier scored "NIO" vs "European Union" above
        # its 88 threshold and merged them. Guard that this pair really is above
        # the old bar, then that the new resolver keeps NIO separate.
        from thefuzz import process as fuzz_process
        self.assertGreaterEqual(fuzz_process.extractOne("NIO", ["European Union"])[1], 88)
        r = self._resolve("NIO")
        self.assertEqual(r.tier, 3)
        self.assertNotEqual(r.entity_id, self.eu)
        self.assertIsNone(self.con.execute("SELECT 1 FROM entity_aliases WHERE entity_id = ? AND alias = 'NIO'",
                                           [self.eu]).fetchone())

    def test_llm_same_as_reuses_and_records_alias(self):
        d = resolve.Decision(same_as=self.hmg, confidence=0.95, reason="legal name")
        r = self._resolve("Hyundai Motor Co", d, mention="Hyundai Motor")
        self.assertEqual((r.entity_id, r.tier, r.matched_name), (self.hmg, 2, "Hyundai Motor Group"))
        self.assertEqual(db.find_entity_exact(self.con, "hyundai motor"), self.hmg)

    def test_new_entity_gets_llm_links(self):
        d = resolve.Decision(links=[(self.hmg, "subsidiary_of")], confidence=0.9, reason="owned by HMG")
        r = self._resolve("Hyundai WIA", d)
        self.assertEqual(r.tier, 3)
        self.assertEqual(self.con.execute("SELECT parent_id, relation, source FROM entity_links WHERE child_id = ?",
                                          [r.entity_id]).fetchone(), (self.hmg, "subsidiary_of", "ingest"))

    def test_decide_skips_exact_and_applies_confidence_bar(self):
        client = FakeClient([
            {"name": "Hyundai Motor Co", "same_as": "Hyundai Motor Group", "confidence": 0.95, "reason": "same"},
            {"name": "Hyundai WIA", "parent": "Hyundai Motor Group", "confidence": 0.6, "reason": "maybe"},
        ])
        out = resolve.decide_entities(self.con, CFG, client, text="news", entities=[
            {"resolved": "Hyundai Motor Co"}, {"resolved": "Hyundai WIA"},
            {"resolved": "European Union"},                  # exact → never sent to the LLM
            {"resolved": "Zzyzx Quantum"}])                  # nothing close → plainly new, not sent
        self.assertEqual(len(client.calls), 1)
        self.assertNotIn("European Union\n", client.calls[0])
        self.assertNotIn("Zzyzx", client.calls[0])
        self.assertIn("Boston Dynamics", client.calls[0])     # family-only candidate (no name overlap) shown
        self.assertEqual(out["Hyundai Motor Co"].same_as, self.hmg)
        self.assertEqual((out["Hyundai WIA"].links, out["Hyundai WIA"].review),
                         ([], ["subsidiary_of Hyundai Motor Group"]))          # unsure → review, not applied

    def test_raw_mention_alias_hit_wins_over_new(self):
        # resolved "Toyota Motor China Co" is unknown, but the raw mention exact-matches an
        # alias: tier 1 takes it (why bad aliases must be removed, not just ignored).
        china = db.insert_entity(self.con, "China", None, "government")
        db.insert_alias(self.con, china, "Toyota Motor China")
        r = self._resolve("Toyota Motor China Co", mention="Toyota Motor China")
        self.assertEqual((r.entity_id, r.tier), (china, 1))
        r = self._resolve("  European Union ")                       # stray whitespace → same entity
        self.assertEqual(r.entity_id, self.eu)

    def test_llm_cannot_pick_a_non_candidate(self):
        # European Union exists in the DB but is not among this name's candidates.
        client = FakeClient([{"name": "Hyundai Motor Co", "same_as": "European Union", "confidence": 1,
                              "reason": "made up"}])
        out = resolve.decide_entities(self.con, CFG, client, text="news", entities=[{"resolved": "Hyundai Motor Co"}])
        self.assertIsNone(out["Hyundai Motor Co"].same_as)


if __name__ == "__main__":
    unittest.main()
