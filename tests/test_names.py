"""Tests for src/names.py — deterministic name keys and the generic-phrase gate.

Run: python -m unittest tests/test_names.py -v
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from src import names  # noqa: E402


class TestKeys(unittest.TestCase):
    def test_strict_key_ignores_case_punctuation_and_legal_suffix_but_keeps_order(self):
        self.assertEqual(names.strict_key("Samsung SDS, Inc."), names.strict_key("samsung sds"))
        self.assertEqual(names.strict_key("Humanoid Robots"), names.strict_key("humanoid robots"))
        self.assertNotEqual(names.strict_key("Bank of China"), names.strict_key("China Bank"))
        self.assertNotEqual(names.strict_key("SEER Robotics"), names.strict_key("Serve Robotics"))

    def test_loose_key_folds_plural_filler_demonym_and_order(self):
        self.assertEqual(names.loose_key("leading logistics companies in China"),
                         names.loose_key("Chinese leading logistic company"))
        self.assertEqual(names.loose_key("Humanoid robots"), names.loose_key("humanoid robot"))


class TestGeneric(unittest.TestCase):
    CASES = {
        # descriptors
        "Chinese telecom company": True, "leading logistics companies in China": True, "Customers": True,
        "Humanoid robots": True, "humanoid robot makers": True, "Gulf investors": True, "Physical AI Sector": True,
        "Robotics Manufacturers": True, "European robotics companies": True, "Embodied AI Companies": True,
        "Companies deploying humanoid robots": True, "Companies Deploying Humanoid Robots": True,
        "companies deploying humanoid robot": True, "Startups building humanoid robots": True,
        "VC-backed robotics startups": True, "Applied Intuition": False, "Advanced Robotics": False,
        "Kindred Ventures": False, "Traditional Staffing Agencies": True,
        # named organizations made of common words — casing / singular keeps them names
        "China Telecom": False, "Humanoid": False, "Bank of China": False, "Venture Capital": False,
        "Field AI": False, "Space AI": False, "US Venture Partners": False, "Physical Intelligence": False,
        "SEER Robotics": False, "Leading Logistics Company China": False, "Robotics": False,
        # government phrases are named bodies → normal lookup
        "Chinese government": False, "U.S. government": False,
    }

    def test_cases(self):
        wrong = {n: e for n, e in self.CASES.items() if names.is_generic(n) != e}
        self.assertEqual(wrong, {})

    def test_mentioned_in_is_case_insensitive_whole_words(self):
        t = "XPeng unveils Turing chips; tesla shares fell."
        self.assertTrue(names.mentioned_in(t, "Tesla"))
        self.assertTrue(names.mentioned_in(t, "Tesla, Inc."))
        self.assertTrue(names.mentioned_in(t, "xpeng"))
        self.assertFalse(names.mentioned_in(t, "NVIDIA"))
        self.assertFalse(names.mentioned_in("Physical Intelligence raises", "Intel"))

    def test_place_of(self):
        self.assertEqual(names.place_of("Chinese telecom company"), "china")
        self.assertIsNone(names.place_of("Customers"))


if __name__ == "__main__":
    unittest.main()
