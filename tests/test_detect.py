"""Tests for src/detect.py — graph_insights[] detectors (spec §2.9a).

Headlines here are published verbatim by soljet-postiz, so the assertions
care about the CLAIM being true (right counts, right ratio, right window),
not just that a dict came back.

Run: python -m unittest tests/test_detect.py -v   (inside the ingest image)
"""
from __future__ import annotations

import os
import sys
import unittest
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import duckdb  # noqa: E402

from src import db, detect  # noqa: E402


@dataclass
class _Ins:
    enabled: bool = True
    window_days: int = 30
    baseline_days: int = 90
    min_recent: int = 3
    min_recent_rel: int = 5
    min_growth_ratio: float = 1.5
    min_baseline: int = 3
    top_n_entities: int = 5
    top_n_rel_types: int = 3


@dataclass
class _Cfg:
    sector: str = "Robotics"
    insights: _Ins = None

    def __post_init__(self):
        if self.insights is None:
            self.insights = _Ins()


class _Fixture:
    """Tiny graph builder: entities, catalysts at N days ago, edges."""

    def __init__(self, con):
        self.con = con
        db.init_schema(con)
        self._cat = 0
        # insert_entity upserts, and DuckDB's upsert is delete+insert — calling
        # it again for an already-referenced entity trips the FK. Prod never
        # does that (resolve_entity returns the existing id), so cache here.
        self._ents: dict[str, int] = {}

    def _entity(self, name: str) -> int:
        if name not in self._ents:
            self._ents[name] = db.insert_entity(self.con, name, None, "private_company")
        return self._ents[name]

    def edge(self, a: str, b: str, rel: str, days_ago: int,
             status: str = "active", conf: float = 0.9) -> None:
        ea, eb = self._entity(a), self._entity(b)
        self._cat += 1
        entry = f"ROB-T{self._cat:05d}"
        # DuckDB won't bind a parameter inside INTERVAL — inline the int
        # (days_ago is test-controlled, never user input).
        self.con.execute(
            f"""INSERT INTO catalysts (entry_id, sector, timestamp, raw_finding,
                   headline, sentiment_label)
               VALUES (?, 'Robotics',
                       CURRENT_DATE - INTERVAL {int(days_ago)} DAY, 'raw', ?, 'bullish')""",
            [entry, f"headline {entry}"],
        )
        cid = self.con.execute(
            "SELECT catalyst_id FROM catalysts WHERE entry_id = ?", [entry]
        ).fetchone()[0]
        self.con.execute(
            """INSERT INTO relationships (catalyst_id, entity_a_id, rel_type,
                   entity_b_id, confidence, status)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [cid, ea, rel, eb, conf, status],
        )


class TestEntityVelocity(unittest.TestCase):
    def setUp(self):
        self.con = duckdb.connect(":memory:")
        self.fx = _Fixture(self.con)

    def tearDown(self):
        self.con.close()

    def _run(self, **kw):
        ins = _Ins(**kw)
        return detect.entity_velocity(
            self.con, "Robotics", ins.window_days, ins.baseline_days,
            ins.min_recent, ins.min_growth_ratio, ins.top_n_entities,
            ins.min_baseline)

    def test_growth_ratio_compares_rates_not_counts(self):
        # 6 edges in 30d = 0.2/day; 6 in the 90d baseline = 0.0667/day → 3.0x
        for i in range(6):
            self.fx.edge("NVIDIA", f"Partner{i}", "partners_with", days_ago=5)
        for i in range(6):
            self.fx.edge("NVIDIA", f"Old{i}", "partners_with", days_ago=60)
        out = self._run()
        nv = next(i for i in out if i["entity"] == "NVIDIA")
        self.assertEqual(nv["recent_count"], 6)
        self.assertEqual(nv["baseline_count"], 6)
        self.assertAlmostEqual(nv["growth_ratio"], 3.0, places=1)
        self.assertIn("3.0x prior 90-day rate", nv["headline"])
        self.assertIn("6 partnerships in 30 days", nv["headline"])

    def test_below_min_growth_ratio_is_not_a_story(self):
        for i in range(4):
            self.fx.edge("Flat Corp", f"P{i}", "partners_with", days_ago=5)
        for i in range(12):                      # same rate → ratio 1.0
            self.fx.edge("Flat Corp", f"O{i}", "partners_with", days_ago=60)
        self.assertEqual([i for i in self._run() if i["entity"] == "Flat Corp"], [])

    def test_min_recent_floor_filters_noise(self):
        self.fx.edge("Tiny", "P1", "partners_with", days_ago=3)
        self.assertEqual(self._run(), [])

    def test_newcomer_has_no_ratio_but_is_reported_when_substantial(self):
        for i in range(6):                       # >= min_recent*2, no baseline
            self.fx.edge("Newco", f"P{i}", "invests_in", days_ago=4)
        out = self._run()
        nc = next(i for i in out if i["entity"] == "Newco")
        self.assertIsNone(nc["growth_ratio"])
        self.assertIn("first activity on record", nc["headline"])

    def test_thin_baseline_never_claims_a_multiplier(self):
        # Live data (2026-09-20) produced "JD.com: 50 supply agreements
        # (150.0x prior 90-day rate)" off a baseline of ONE edge.
        for i in range(10):
            self.fx.edge("JD.com", f"P{i}", "supplies", days_ago=5)
        self.fx.edge("JD.com", "Old", "supplies", days_ago=60)
        out = self._run()
        jd = next(i for i in out if i["entity"] == "JD.com")
        self.assertIsNone(jd["growth_ratio"])
        self.assertNotIn("x prior", jd["headline"])
        self.assertIn("vs 1 in the prior 90", jd["headline"])

    def test_rel_type_thin_baseline_is_dropped(self):
        for i in range(9):
            self.fx.edge(f"A{i}", f"B{i}", "regulates", days_ago=5)
        self.fx.edge("A99", "B99", "regulates", days_ago=60)      # baseline of 1
        out = detect.relationship_velocity(self.con, "Robotics", 30, 90, 5, 1.5, 3, 3)
        self.assertEqual([i for i in out if i["rel_type"] == "regulates"], [])

    def test_invalidated_edges_never_inflate_a_claim(self):
        for i in range(6):
            self.fx.edge("Ghost", f"P{i}", "partners_with", days_ago=5,
                         status="invalidated")
        self.assertEqual(self._run(), [])

    def test_real_accelerations_outrank_newcomers(self):
        for i in range(6):
            self.fx.edge("Accel", f"A{i}", "partners_with", days_ago=5)
        for i in range(6):
            self.fx.edge("Accel", f"B{i}", "partners_with", days_ago=60)
        for i in range(8):
            self.fx.edge("Newco", f"C{i}", "partners_with", days_ago=5)
        self.assertEqual(self._run()[0]["entity"], "Accel")

    def test_headline_uses_human_phrasing_for_rel_type(self):
        # "4 acquisitions", never "4 acquires".
        for i in range(4):
            self.fx.edge("BuyCo", f"P{i}", "acquires", days_ago=5)
        for i in range(3):                       # meets min_baseline
            self.fx.edge("BuyCo", f"O{i}", "acquires", days_ago=60)
        out = self._run()
        h = next(i for i in out if i["entity"] == "BuyCo")["headline"]
        self.assertIn("acquisitions", h)
        self.assertNotIn("acquires", h)

    def test_singular_phrasing_helper(self):
        self.assertEqual(detect._phrase("acquires", 1), "acquisition")
        self.assertEqual(detect._phrase("acquires", 2), "acquisitions")


class TestRelationshipVelocity(unittest.TestCase):
    def setUp(self):
        self.con = duckdb.connect(":memory:")
        self.fx = _Fixture(self.con)

    def tearDown(self):
        self.con.close()

    def test_sector_wide_type_trend(self):
        for i in range(9):
            self.fx.edge(f"A{i}", f"B{i}", "acquires", days_ago=5)
        for i in range(9):
            self.fx.edge(f"C{i}", f"D{i}", "acquires", days_ago=60)
        out = detect.relationship_velocity(self.con, "Robotics", 30, 90, 5, 1.5, 3)
        acq = next(i for i in out if i["rel_type"] == "acquires")
        self.assertAlmostEqual(acq["growth_ratio"], 3.0, places=1)
        self.assertIn("Acquisitions across the sector: 9 in 30 days", acq["headline"])

    def test_no_baseline_yields_no_comparative_claim(self):
        for i in range(9):
            self.fx.edge(f"A{i}", f"B{i}", "pilots", days_ago=5)
        self.assertEqual(detect.relationship_velocity(
            self.con, "Robotics", 30, 90, 5, 1.5, 3), [])


class TestGraphInsightsEntryPoint(unittest.TestCase):
    def setUp(self):
        self.con = duckdb.connect(":memory:")
        self.fx = _Fixture(self.con)
        for i in range(6):
            self.fx.edge("NVIDIA", f"P{i}", "partners_with", days_ago=5)
        for i in range(6):
            self.fx.edge("NVIDIA", f"O{i}", "partners_with", days_ago=60)

    def tearDown(self):
        self.con.close()

    def test_disabled_returns_empty(self):
        self.assertEqual(detect.graph_insights(self.con, _Cfg(insights=_Ins(enabled=False))), [])

    def test_enabled_returns_insights(self):
        out = detect.graph_insights(self.con, _Cfg())
        self.assertTrue(out)
        self.assertEqual(out[0]["entity"], "NVIDIA")
        for key in ("type", "growth_ratio", "headline"):
            self.assertIn(key, out[0])

    def test_detector_failure_is_non_fatal(self):
        self.con.close()                          # force an error inside
        self.assertEqual(detect.graph_insights(self.con, _Cfg()), [])


if __name__ == "__main__":
    unittest.main()
