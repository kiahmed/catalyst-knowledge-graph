"""Tests for src/render_fp.py — the fingerprint changes exactly when what a card's images draw changes.

Run: python -m unittest tests.test_render_fp -v
"""
from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.render_fp import fingerprint  # noqa: E402

CARD = {
    "card_id": "ROB-1", "headline": "Tesla partners with NVIDIA", "date": "2026-11-01T00:00:00", "subtitle": "s",
    "story_names_entities": True, "event_type": "partnership", "hashtags": ["#a"], "internal_events": [],
    "entities": [{"name": "Tesla", "ticker": "TSLA", "type": "public_company", "in_story": True},
                 {"name": "NVIDIA", "ticker": "NVDA", "type": "public_company", "in_story": True}],
    "relationships": [{"from": "Tesla", "to": "NVIDIA", "rel": "partners_with", "confidence": 0.9,
                       "evidence_type": "direct", "status": "active", "in_story": True, "mechanism": "m",
                       "rel_subtype": "x"}],
}


def changed(fn) -> bool:
    c = copy.deepcopy(CARD)
    fn(c)
    return fingerprint(c) != fingerprint(CARD)


class TestRenderFp(unittest.TestCase):
    def test_drawn_changes_flip_it(self):
        self.assertTrue(changed(lambda c: c.update(headline="Tesla buys NVIDIA")))
        self.assertTrue(changed(lambda c: c["entities"][1].update(name="Nvidia Corp")))          # rename / merge
        self.assertTrue(changed(lambda c: c["relationships"][0].update(evidence_type="inferred")))
        self.assertTrue(changed(lambda c: c["relationships"][0].update(rel="supplies")))
        self.assertTrue(changed(lambda c: c["relationships"].clear()))                           # edge dropped
        self.assertTrue(changed(lambda c: c["relationships"].append(
            {"from": "NVIDIA", "to": "Tesla", "rel": "supplies", "confidence": 0.5})))           # edge added

    def test_undrawn_fields_and_order_dont(self):
        self.assertFalse(changed(lambda c: c.update(hashtags=["#b"], event_type="m_and_a", render_fp="x")))
        self.assertFalse(changed(lambda c: c["relationships"][0].update(mechanism="other", rel_subtype="y")))
        self.assertFalse(changed(lambda c: c["entities"].reverse()))
        self.assertFalse(changed(lambda c: c["relationships"][0].update(confidence=0.9001)))     # below display precision

    def test_format(self):
        self.assertRegex(fingerprint(CARD), r"^v2-[0-9a-f]{16}$")


if __name__ == "__main__":
    unittest.main()
