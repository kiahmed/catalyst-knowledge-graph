"""Tests for the ingest direct-evidence check (src/ingest.py story_text / checked_evidence;
docs/ticket-log.md#ticket-ingest-direct-evidence-check): a `direct` edge needs both ends named in the card's OWN text.

Run: python -m unittest tests/test_ingest_direct_check.py -v   (inside the ingest image)
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import duckdb  # noqa: E402

from src import db, ingest, story  # noqa: E402
from src.extract import ExtractionResult  # noqa: E402


def _result(finding_head: str, rels, ents, context: str, path: str, ok: bool = True) -> ExtractionResult:
    return ExtractionResult(entry_id="ROB-T", prompt_version="t", model="t", reasoning="", significance_score=0.9,
                            entities=ents, relationships=rels, sentiment_label="bullish", headline=finding_head,
                            research_sources=[], dropped_low_confidence=[], flagged_medium_confidence=[],
                            enrichment={"path": path, "ok": ok}, context_text=context)


class TestDirectCheck(unittest.TestCase):
    def setUp(self):
        self.con = duckdb.connect(":memory:")
        db.init_schema(self.con)
        e = lambda n, t="public_company": db.insert_entity(self.con, n, None, t)  # noqa: E731
        self.tesla, self.agility, self.nvidia = e("Tesla"), e("Agility Robotics", "private_company"), e("NVIDIA")
        self.group = e("Humanoid Robots", "group")
        db.insert_alias(self.con, self.nvidia, "Nvidia Corp")

    def tearDown(self):
        self.con.close()

    def _check(self, rel, a, b, text):
        return ingest.checked_evidence(self.con, rel, [(a, rel["entity_a"]), (b, rel["entity_b"])], text,
                                       story.load_aliases(self.con))

    def test_story_text_excludes_research_but_keeps_a_real_article(self):
        research = _result("Optimus trials begin", [], [], "Agility Robotics also builds humanoids.", "grounding")
        self.assertNotIn("Agility", ingest.story_text({"finding": "Tesla ramps Optimus."}, research))
        article = _result("Optimus trials begin", [], [], "The article: Agility Robotics responds.", "url_fetch")
        self.assertIn("Agility", ingest.story_text({"finding": "Tesla ramps Optimus."}, article))
        own = ingest.story_text({"finding": "Tesla ramps Optimus.", "sentiment_takeaways": "Bullish for Agility Robotics.",
                                 "guidance_play": "Watch NVIDIA.", "price_levels": "TSLA 250"}, research)
        self.assertTrue(all(w in own for w in ("Agility Robotics", "NVIDIA", "TSLA")))   # the finding's own text counts
        failed = _result("Optimus trials begin", [], [], "research text", "url_fetch+grounding", ok=False)
        self.assertNotIn("research", ingest.story_text({"finding": "x"}, failed))

    def test_rules(self):
        text = "Tesla ramps Optimus; Nvidia Corp chips inside."
        rel = lambda **k: {"entity_a": "Agility Robotics", "entity_b": "Tesla", "evidence_type": "direct", **k}  # noqa: E731
        self.assertEqual(self._check(rel(), self.agility, self.tesla, "Agility Robotics and Tesla compete."), ("direct", None))
        ev, why = self._check(rel(), self.agility, self.tesla, text)
        self.assertEqual(ev, "inferred")
        self.assertIn("Agility Robotics", why)
        self.assertEqual(self._check(rel(source_refs=[2]), self.agility, self.tesla, text)[0], "web_grounded")
        self.assertEqual(self._check({"entity_a": "NVIDIA", "entity_b": "Tesla", "evidence_type": "direct"},
                                     self.nvidia, self.tesla, text), ("direct", None))      # alias "Nvidia Corp" counts
        self.assertEqual(self._check(rel(evidence_type="speculative"), self.agility, self.tesla, text), ("speculative", None))
        self.assertEqual(self._check({"entity_a": "Tesla", "entity_b": "humanoid robots", "evidence_type": "direct"},
                                     self.tesla, self.group, "Tesla only."), ("direct", None))   # group bucket left alone

    def test_write_extraction_end_to_end(self):
        from src.config import load_config
        ents = [{"resolved": "Tesla", "mention": "Tesla", "type": "public_company", "kind": "organization"},
                {"resolved": "Agility Robotics", "mention": "Agility Robotics", "type": "private_company", "kind": "organization"}]
        rels = [{"entity_a": "Agility Robotics", "rel_type": "competes_with", "entity_b": "Tesla", "confidence": 0.9,
                 "evidence_type": "direct", "mechanism": "Both build humanoids for factories."}]
        res = _result("Tesla Optimus Gen 3 Begins Factory Trials", rels, ents,
                      "Research: Agility Robotics' Digit is a rival humanoid.", "grounding")
        entry = {"entry_id": "ROB-T", "timestamp": "2026-11-19T00:00:00Z", "category": "Robotics",
                 "finding": "Optimus Gen 3 began factory trials at Tesla's Fremont plant."}
        cid = ingest.write_extraction(self.con, entry, res, load_config())
        got = self.con.execute("SELECT evidence_type FROM relationships WHERE catalyst_id = ?", [cid]).fetchall()
        self.assertEqual(got, [("inferred",)])          # was "direct" from the extractor; Agility not in the story
        self.assertIn("not named", res.relationships[0]["evidence_downgraded"])
        # (competes_with is two-party: stored as Tesla→Agility by id order — the swap must not let "Tesla"
        #  vouch for Agility; this case failed before ends were paired up front)


if __name__ == "__main__":
    unittest.main()
