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

    def _resolve(self, name, decision=None, mention=None, context=None):
        return resolve.resolve_entity(self.con, resolved_name=name, mention=mention or name, ticker=None,
                                      type_="public_company", catalyst_id=self.cid, cfg=CFG, decision=decision,
                                      context=context)

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
        # both spellings resolve next time, trusted (verified), via the name key
        self.assertEqual(resolve.classify(self.con, "hyundai motor", "hyundai motor"), ("trusted", self.hmg))
        self.assertEqual(resolve.classify(self.con, "Hyundai Motor Co", "Hyundai Motor Co"), ("trusted", self.hmg))

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

    # ── 2026-11-09 resolution fix (docs/ticket-ingest-entity-resolution.md) ──

    def _serve_with_legacy_seer_alias(self):
        serve = db.insert_entity(self.con, "Serve Robotics", None, "public_company")
        self.con.execute("INSERT INTO entity_aliases (entity_id, alias) VALUES (?, 'SEER Robotics')", [serve])  # legacy
        return serve

    def test_legacy_alias_is_sent_to_the_llm_not_trusted(self):
        serve = self._serve_with_legacy_seer_alias()
        client = FakeClient([{"name": "SEER Robotics", "confidence": 0.95, "reason": "different company"}])
        out = resolve.decide_entities(self.con, CFG, client, text="SEER Robotics shows a Foxconn robot",
                                      entities=[{"resolved": "SEER Robotics", "mention": "SEER Robotics"}])
        self.assertEqual(len(client.calls), 1)
        self.assertIn("Serve Robotics", client.calls[0])                # the alias's entity is a candidate
        r = self._resolve("SEER Robotics", out["SEER Robotics"])
        self.assertEqual(r.tier, 3)                                     # contradicted → its own entity
        self.assertNotEqual(r.entity_id, serve)

    def test_legacy_alias_confirmed_by_llm_becomes_verified(self):
        serve = self._serve_with_legacy_seer_alias()
        r = self._resolve("SEER Robotics", resolve.Decision(same_as=serve, confidence=0.95, reason="same"))
        self.assertEqual((r.entity_id, r.tier), (serve, 2))
        self.assertEqual(self.con.execute("SELECT verified FROM entity_aliases WHERE alias = 'SEER Robotics'").fetchone(),
                         (True,))
        # now trusted: never sent again
        client = FakeClient([])
        resolve.decide_entities(self.con, CFG, client, text="x", entities=[{"resolved": "SEER Robotics"}])
        self.assertEqual(client.calls, [])

    def test_legacy_alias_without_a_verdict_keeps_todays_match(self):
        serve = self._serve_with_legacy_seer_alias()
        r = self._resolve("SEER Robotics")                               # LLM failed / absent
        self.assertEqual((r.entity_id, r.tier), (serve, 1))

    def test_safe_legacy_aliases_stay_trusted(self):
        bd = self.bd
        self.con.execute("INSERT INTO entity_aliases (entity_id, alias) VALUES (?, 'BD')", [bd])            # initials
        self.con.execute("INSERT INTO entity_aliases (entity_id, alias) VALUES (?, 'Boston Dynamics Inc.')", [bd])
        self.assertEqual(resolve.classify(self.con, "Boston Dynamics", "BD"), ("trusted", bd))
        self.assertEqual(resolve.classify(self.con, "Boston Dynamics Inc.", "Boston Dynamics Inc."), ("trusted", bd))

    def test_extractor_rewrite_is_checked_and_saves_no_alias(self):
        # mention "Hyundai" rewritten to Hyundai Motor Group: the LLM must confirm; nothing saved by tier 1
        kind, hit = resolve.classify(self.con, "Hyundai Motor Group", "Hyundai")
        self.assertEqual((kind, hit), ("check", self.hmg))
        r = self._resolve("Hyundai Motor Group", mention="Hyundai")      # no verdict → today's match
        self.assertEqual(r.entity_id, self.hmg)
        self.assertIsNone(self.con.execute("SELECT 1 FROM entity_aliases WHERE alias = 'Hyundai'").fetchone())

    def test_generic_phrase_without_a_curated_bucket_is_discarded_never_matched(self):
        humanoid = db.insert_entity(self.con, "Humanoid", None, "private_company")
        n0 = self.con.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        r = self._resolve("Humanoid", mention="humanoid")                 # lowercase = the robot category
        self.assertEqual((r.entity_id, r.tier), (None, 0))
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM entities").fetchone()[0], n0)   # no entity created
        self.assertIsNone(self.con.execute("SELECT 1 FROM entity_aliases WHERE entity_id = ?", [humanoid]).fetchone())
        self.assertEqual(self.con.execute("SELECT suggested_type FROM unresolved_entities WHERE mention = 'humanoid'")
                         .fetchone(), ("generic_discarded",))
        self.assertEqual(self._resolve("Humanoid").entity_id, humanoid)  # the company itself still resolves
        # extractor rewrite onto a real company: discarded, the company is never captured
        r = self._resolve("China Telecom", mention="Chinese telecom companies")
        self.assertIsNone(r.entity_id)
        self.assertEqual(resolve.classify(self.con, "China Telecom", "China Telecom")[0], "new")
        client = FakeClient([])
        resolve.decide_entities(self.con, CFG, client, text="x", entities=[
            {"resolved": "China", "mention": "Chinese telecom companies"}])
        self.assertEqual(client.calls, [])                               # generic: never sent to the LLM

    def test_generic_phrases_tag_to_a_curated_group_bucket(self):
        china = db.insert_entity(self.con, "China", None, "government")
        bucket = db.insert_entity(self.con, "Leading logistics companies in China", None, "group")
        r1 = self._resolve("China", mention="leading logistics companies in China")
        r2 = self._resolve("China", mention="Chinese leading logistic company")
        self.assertEqual((r1.entity_id, r2.entity_id), (bucket, bucket))
        self.assertIsNone(self.con.execute("SELECT 1 FROM entity_aliases WHERE entity_id = ?", [china]).fetchone())
        self.assertIn(("Chinese leading logistic company", True, "generic"), self.con.execute(
            "SELECT alias, verified, source FROM entity_aliases WHERE entity_id = ?", [bucket]).fetchall())

    def test_curated_group_alias_beats_a_generic_named_legacy_entity(self):
        bucket = db.insert_entity(self.con, "Logistics operators", None, "group")
        db.insert_alias(self.con, bucket, "Chinese logistics companies", verified=True, source="admin")
        legacy = db.insert_entity(self.con, "Chinese logistics company", None, "organization")
        self.assertNotEqual(legacy, bucket)
        self.assertEqual(self._resolve("China", mention="China's logistics companies").entity_id, bucket)

    def test_contradicted_legacy_alias_is_flagged_for_review(self):
        self._serve_with_legacy_seer_alias()
        client = FakeClient([{"name": "SEER Robotics", "confidence": 0.95, "reason": "different company"}])
        out = resolve.decide_entities(self.con, CFG, client, text="SEER Robotics demo",
                                      entities=[{"resolved": "SEER Robotics", "mention": "SEER Robotics"}])
        self.assertTrue(any("Serve Robotics" in r for r in out["SEER Robotics"].review))

    def test_new_entity_only_when_named_in_the_cards_text(self):
        story = "XPeng pivots to a Physical AI company with its own chips."
        r = self._resolve("Traditional Automotive OEMs", context=story)       # only a speculation names it
        self.assertEqual((r.entity_id, r.tier), (None, 0))
        self.assertIsNone(self.con.execute("SELECT 1 FROM entities WHERE name = 'Traditional Automotive OEMs'").fetchone())
        self.assertEqual(self.con.execute("SELECT suggested_type FROM unresolved_entities").fetchone(),
                         ("ungrounded_discarded",))
        r = self._resolve("XPeng Inc.", mention="Xpeng", context=story)        # case-insensitive, legal suffix ok
        self.assertEqual(r.tier, 3)
        # an EXISTING entity is kept even when the story never names it (the cross-company edges)
        self.assertEqual(self._resolve("Boston Dynamics", context=story).entity_id, self.bd)
        self.assertEqual(self._resolve("Hyundai Motor Co", resolve.Decision(same_as=self.hmg, confidence=0.95,
                                                                            reason="same"), context=story).entity_id,
                         self.hmg)

    def test_extraction_descriptor_label_is_never_an_entity(self):
        figure = db.insert_entity(self.con, "Figure", None, "private_company")
        n0 = self.con.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        # a descriptor the word gate would miss: it contains a real name
        r = resolve.resolve_entity(self.con, resolved_name="Startups backed by SoftBank",
                                   mention="startups backed by SoftBank", ticker=None, type_="organization",
                                   catalyst_id=self.cid, cfg=CFG, descriptor=True)
        self.assertEqual((r.entity_id, r.tier), (None, 0))
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM entities").fetchone()[0], n0)
        # mislabel backstop: an exact existing name stays that entity
        self.assertEqual(resolve.classify(self.con, "Figure", "Figure", descriptor=True), ("trusted", figure))
        # but lowercase never matches a company
        humanoid = db.insert_entity(self.con, "Humanoid", None, "private_company")
        self.assertEqual(resolve.classify(self.con, "Humanoid", "humanoid", descriptor=True), ("generic", None))
        self.assertNotEqual(humanoid, None)
        # descriptor names are never sent to the LLM
        client = FakeClient([])
        resolve.decide_entities(self.con, CFG, client, text="x", entities=[
            {"resolved": "Startups backed by SoftBank", "mention": "startups backed by SoftBank", "kind": "descriptor"}])
        self.assertEqual(client.calls, [])

    def test_named_company_of_common_words_is_not_generic(self):
        self.assertEqual(resolve.classify(self.con, "China Telecom", "China Telecom")[0], "new")

    def test_insert_alias_skips_case_and_punctuation_variants_only(self):
        db.insert_alias(self.con, self.hmg, "Hyundai Motor")
        db.insert_alias(self.con, self.hmg, "hyundai motor")
        db.insert_alias(self.con, self.hmg, "Hyundai-Motor")
        db.insert_alias(self.con, self.hmg, "hyundai motor group")            # the name itself, other case
        db.insert_alias(self.con, self.hmg, "Hyundai Motor Co")               # legal-suffix variant: kept
        self.assertEqual(sorted(a for (a,) in self.con.execute("SELECT alias FROM entity_aliases WHERE entity_id = ?",
                                                               [self.hmg]).fetchall()), ["Hyundai Motor", "Hyundai Motor Co"])

    def test_unsure_decision_carries_its_trail(self):
        client = FakeClient([{"name": "Hyundai WIA", "parent": "Hyundai Motor Group", "confidence": 0.6, "reason": "maybe owned"}])
        out = resolve.decide_entities(self.con, CFG, client, text="news", entities=[{"resolved": "Hyundai WIA"}])
        path = out["Hyundai WIA"].path
        self.assertTrue(path[0].startswith("1. Name lookup"))
        self.assertIn("Hyundai Motor Group", path[1])                      # candidates shown
        self.assertIn("confidence 0.60", path[2]); self.assertIn("0.8", path[2])
        self.assertIn("maybe owned", path[3])

    def test_llm_cannot_pick_a_non_candidate(self):
        # European Union exists in the DB but is not among this name's candidates.
        client = FakeClient([{"name": "Hyundai Motor Co", "same_as": "European Union", "confidence": 1,
                              "reason": "made up"}])
        out = resolve.decide_entities(self.con, CFG, client, text="news", entities=[{"resolved": "Hyundai Motor Co"}])
        self.assertIsNone(out["Hyundai Motor Co"].same_as)


if __name__ == "__main__":
    unittest.main()
