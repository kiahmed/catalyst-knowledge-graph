"""Tests for src/integrity.py + placeholder source_url cleanup (2026-11 dedup sweep).

Run: python -m unittest tests/test_integrity.py -v   (inside the ingest image)
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import duckdb  # noqa: E402

from src import db, integrity  # noqa: E402


def _cat(con, entry, url):
    con.execute(
        """INSERT INTO catalysts (entry_id, sector, timestamp, source_url, raw_finding, headline, sentiment_label)
           VALUES (?, 'Robotics', CURRENT_DATE, ?, 'raw', 'h', 'bullish')""", [entry, url])
    return con.execute("SELECT catalyst_id FROM catalysts WHERE entry_id = ?", [entry]).fetchone()[0]


class TestCleanUrl(unittest.TestCase):
    def test_placeholders_become_none(self):
        for v in (None, "", "None", "none", " N/A ", "null", "see article"):
            self.assertIsNone(db.clean_url(v), v)

    def test_real_urls_kept_trimmed(self):
        self.assertEqual(db.clean_url(" https://a.com/x "), "https://a.com/x")
        self.assertEqual(db.clean_url("http://b.org"), "http://b.org")
        self.assertEqual(db.clean_url("HTTPS://C.com/y\n"), "HTTPS://C.com/y")
        for v in ("https://", "https://a.com/x\ty", "javascript:alert(1)", "ftp://a.com"):
            self.assertIsNone(db.clean_url(v), v)


class TestIntegrity(unittest.TestCase):
    def setUp(self):
        self.con = duckdb.connect(":memory:")
        db.init_schema(self.con)

    def tearDown(self):
        self.con.close()

    def test_init_schema_nulls_placeholder_urls(self):
        _cat(self.con, "ROB-1", "None")
        _cat(self.con, "ROB-2", "")
        _cat(self.con, "ROB-3", "https://ok.com/a")
        _cat(self.con, "ROB-4", "HTTPS://Upper.com/a")       # SQL must agree with clean_url
        _cat(self.con, "ROB-5", "https://nl.com/a\n")         # trimmed, not wiped
        _cat(self.con, "ROB-6", "https://a.com/x\ty")
        self.assertEqual(integrity.check_db(self.con)["placeholder_source_url"], ["ROB-1", "ROB-2", "ROB-6"])
        db.init_schema(self.con)                            # the cloud ingest runs this every time
        rows = dict(self.con.execute("SELECT entry_id, source_url FROM catalysts").fetchall())
        self.assertEqual(rows, {"ROB-1": None, "ROB-2": None, "ROB-3": "https://ok.com/a",
                                "ROB-4": "HTTPS://Upper.com/a", "ROB-5": "https://nl.com/a", "ROB-6": None})
        self.assertEqual(integrity.check_db(self.con)["placeholder_source_url"], [])

    def test_sql_cleanup_matches_clean_url(self):
        raws = ["None", "", "https://ok.com/a", "HTTPS://Upper.com/a", "https://nl.com/a\n",
                "https://a.com/x\ty", "https://", "javascript:alert(1)", "ftp://a.com",
                "\u00a0https://nbsp.com/a", "https://nbsp.com/a\u00a0b", "\vhttps://vt.com/a\v"]
        for i, raw in enumerate(raws):
            _cat(self.con, f"ROB-{i:02d}", raw)
        db.init_schema(self.con)
        got = [r[0] for r in self.con.execute("SELECT source_url FROM catalysts ORDER BY entry_id").fetchall()]
        self.assertEqual(got, [db.clean_url(r) for r in raws])
        self.assertEqual(integrity.check_db(self.con)["placeholder_source_url"], [])

    def test_clean_db_reports_nothing(self):
        cid = _cat(self.con, "ROB-1", None)
        a = db.insert_entity(self.con, "A", None, "private_company")
        b = db.insert_entity(self.con, "B", None, "private_company")
        self.con.execute("""INSERT INTO relationships (catalyst_id, entity_a_id, rel_type, entity_b_id, confidence)
                            VALUES (?, ?, 'partners_with', ?, 0.9)""", [cid, a, b])
        self.assertTrue(all(v == [] for v in integrity.check_db(self.con).values()))

    def test_dangling_lifecycle_ref_is_caught(self):
        cid = _cat(self.con, "ROB-1", None)
        a = db.insert_entity(self.con, "A", None, "private_company")
        self.con.execute("""INSERT INTO relationships (catalyst_id, entity_a_id, rel_type, entity_b_id,
                                confidence, invalidated_by_catalyst_id)
                            VALUES (?, ?, 'partners_with', ?, 0.9, 999)""", [cid, a, a])
        self.assertEqual(len(integrity.check_db(self.con)["rel_missing_lifecycle_catalyst"]), 1)


if __name__ == "__main__":
    unittest.main()
