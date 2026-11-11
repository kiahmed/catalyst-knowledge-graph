"""Tests for src/events.py — the card event_type rules (headlines from prod, data/event_type_eval_20261111.txt).

Run: python -m unittest tests/test_events.py -v   (inside the ingest image)
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import duckdb  # noqa: E402

from src import db, events  # noqa: E402

C = events.classify


class TestRules(unittest.TestCase):
    def test_cases(self):
        cases = {
            "Humanoid Secures $152M Series A, Achieves Unicorn Status for Robot Deployment": "funding",
            "Figure AI and Apptronik Secure Billions, Accelerating Humanoid Robotics Commercialization": "funding",
            "Unitree Obtains Regulatory Approval for $620M STAR Market Humanoid IPO": "ipo_listing",
            "FF EAI Robotics Ecosystem Launches on Nasdaq, Acquiring Faraday Future's Robotics Business": "ipo_listing",
            "Mobileye Acquires Mentee Robotics, Partners with Aumovio for Humanoid Production": "m_and_a",
            "SoftBank Prepares $100 Billion Roze AI and Robotics Spin-Off": "m_and_a",
            "UBTech Launches U1 Humanoid, Reshaping Companion and Industrial Robotics Markets": "product_launch",
            "Shanghai Electric Unveils AI-Native Smart Factory, Embodied AI Robots at WAIC": "production_facility",
            "Tesla Begins Optimus Humanoid Robot Production, Targets Massive Scale": "production_facility",
            "Tesla Optimus Production Ramps Up, Intensifying Humanoid Robot Competition": "production_facility",
            "SoftBank Launches $100B AI Fund, Leverages Roze Robotics for Industrial Transformation": "funding",
            "South Korea Launches Five Hundred Million Dollar Physical AI Initiative": "regulation",
            "U.S. Navy Quadruples Gecko Robotics Contract to $300 Million for Corrosion Inspection": "deployment",
            "Onconetix and Realbotix Deliver AI Humanoid to Ericsson, Reshaping Robotics Market": "deployment",
            "SKF, Leaderdrive Form China JV for Humanoid Robot Transmission Components": "partnership",
            "US FCC Bans Foreign Humanoid Robots, Boosting Domestic Manufacturers": "regulation",
            "China Designates Embodied AI Top Priority, Reshaping Global Robotics Landscape": "regulation",
            "Boston Dynamics Appoints Former Amazon AI Chief as CEO, Pivots to Humanoid Manufacturing": "leadership",
            "Fujitsu and Carnegie Mellon University Launch Joint Physical AI Research Center": "research",
        }
        for head, want in cases.items():
            self.assertEqual(C(head), want, head)

    def test_false_positives_from_the_eval(self):
        self.assertEqual(C("Standard Bots Secures $200M to Scale AI-Native Industrial Robots"), "funding")
        self.assertEqual(C("Vention Launches AI for Unstructured Manufacturing, Intensifying Competition"), "product_launch")
        self.assertEqual(C("NVIDIA Unveils DSX POD, Accelerating Physical AI Factory Deployment"), "product_launch")
        self.assertIsNone(C("Goldman Sachs Forecasts Massive Humanoid Robot Market Growth by 2035"))
        self.assertIsNone(C("Faraday Future Pivots to Embodied AI, Targets 1,000 Robot Shipments"))
        self.assertIsNone(C(None))
        self.assertIsNone(C(""))


class TestRefresh(unittest.TestCase):
    def test_backfill_idempotent(self):
        con = duckdb.connect(":memory:")
        db.init_schema(con)
        for i, h in enumerate(["Pudu Robotics Raises $150M", "Goldman Sachs Forecasts Growth"]):
            con.execute("""INSERT INTO catalysts (entry_id, sector, timestamp, raw_finding, headline, sentiment_label)
                           VALUES (?, 'Robotics', CURRENT_DATE, 'x', ?, 'bullish')""", [f"ROB-{i}", h])
        self.assertEqual(events.refresh(con), 1)
        self.assertEqual(events.refresh(con), 0)
        self.assertEqual(events.refresh(con, []), 0)
        self.assertEqual(dict(con.execute("SELECT entry_id, event_type FROM catalysts").fetchall()),
                         {"ROB-0": "funding", "ROB-1": None})
        con.close()


if __name__ == "__main__":
    unittest.main()
