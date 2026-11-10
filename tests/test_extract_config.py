"""Tests for the Gemini 3 request config (src/extract.py) and thinking_level validation.

Run: python -m unittest tests/test_extract_config.py -v   (inside the ingest image)
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from src import config, extract  # noqa: E402


class TestGeminiConfig(unittest.TestCase):
    def test_gemini3_detection_is_positive_match(self):
        for m in ("gemini-3.6-flash", "gemini-3-flash-preview", "models/gemini-3.8-flash", "Gemini-3.6-Flash",
                  "gemini-10-pro"):
            self.assertTrue(extract._is_gemini3(m), m)
        for m in ("gemini-2.5-flash", "models/gemini-2.5-flash", "Gemini-2.5-Flash", "gemini-1.5-pro",
                  "some-other-model"):
            self.assertFalse(extract._is_gemini3(m), m)

    def test_gemini3_sends_thinking_level_not_temperature(self):
        c = extract._gen_config("gemini-3.6-flash", "medium", temperature=0.2, max_output_tokens=100)
        self.assertIsNone(c.temperature)
        self.assertEqual(str(c.thinking_config.thinking_level).lower().split(".")[-1], "medium")
        self.assertEqual(c.max_output_tokens, 100)

    def test_gemini2_keeps_temperature_and_no_thinking_level(self):
        c = extract._gen_config("gemini-2.5-flash", "medium", temperature=0.2, max_output_tokens=100)
        self.assertEqual(c.temperature, 0.2)
        self.assertIsNone(c.thinking_config)

    def test_thinking_level_validated_at_load(self):
        self.assertEqual(config._thinking_level(" Medium "), "medium")
        with self.assertRaises(ValueError):
            config._thinking_level("med")

    @unittest.skipUnless(os.path.exists(config._CONFIG_PATH), "config/config.yaml is gitignored (absent in CI)")
    def test_loaded_config_uses_valid_levels(self):
        cfg = config.load_config()
        self.assertIn(cfg.extraction.thinking_level, config._THINKING_LEVELS)
        self.assertIn(cfg.extraction.grounding.thinking_level, config._THINKING_LEVELS)


if __name__ == "__main__":
    unittest.main()


class TestEntityKind(unittest.TestCase):
    def test_kind_is_a_required_schema_field(self):
        f = extract.EntityOut.model_fields["kind"]
        self.assertTrue(f.is_required())
        with self.assertRaises(Exception):
            extract.EntityOut(mention="x", resolved="x", type="organization", kind="product")


class TestEvidenceBar(unittest.TestCase):
    """2026-11-09: one vocab-driven rule for every relationship type."""

    def setUp(self):
        self.vocab = config.load_config().vocab

    def test_every_type_declares_a_bar_and_a_valid_fallback(self):
        names = {v["name"] for v in self.vocab["vocabulary"]}
        for v in self.vocab["vocabulary"]:
            self.assertIn(v.get("min_evidence"), ("speculative", "inferred", "direct"), v["name"])
            fb = v.get("fallback")
            self.assertTrue(fb is None or fb == "drop" or fb in names, (v["name"], fb))

    def test_fact_claims_need_more_than_a_speculation(self):
        bar = lambda t, e: extract.evidence_bar(t, e, self.vocab)  # noqa: E731
        self.assertEqual(bar("displaces", "speculative"), "competes_with")   # XPeng "displaces" NVIDIA
        self.assertIsNone(bar("displaces", "web_grounded"))                  # cited replacement: kept
        self.assertIsNone(bar("displaces", "direct"))
        self.assertEqual(bar("acquires", "speculative"), "drop")             # a guessed acquisition: gone
        self.assertIsNone(bar("acquires", "inferred"))
        self.assertIsNone(bar("competes_with", "speculative"))               # soft relation: fine
        self.assertIsNone(bar("unknown_type", "speculative"))

    def test_prompt_states_the_bar(self):
        text = extract._format_vocab(self.vocab)
        line = next(l for l in text.splitlines() if l.startswith("- **displaces**"))
        self.assertIn("EVIDENCE BAR", line)
        self.assertIn("use competes_with instead", line)
        self.assertNotIn("EVIDENCE BAR", next(l for l in text.splitlines() if l.startswith("- **competes_with**")))
