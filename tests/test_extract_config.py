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

    def test_loaded_config_uses_valid_levels(self):
        cfg = config.load_config()
        self.assertIn(cfg.extraction.thinking_level, config._THINKING_LEVELS)
        self.assertIn(cfg.extraction.grounding.thinking_level, config._THINKING_LEVELS)


if __name__ == "__main__":
    unittest.main()
