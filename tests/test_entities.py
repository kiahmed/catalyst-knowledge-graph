"""Tests for src/entities.py — candidate groups + merge/link mechanics (no live LLM).

Run: python -m unittest tests/test_entities.py -v   (inside the ingest image)
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import duckdb  # noqa: E402

from src import db, entities  # noqa: E402


class TestCandidateGroups(unittest.TestCase):
    def test_brand_and_suffix_variants_grouped_without_chaining(self):
        names = dict(enumerate([
            "Hyundai Motor Group", "Hyundai Rotem", "HD Hyundai",           # brand word "hyundai"
            "Schaeffler Group", "Schaeffler AG",                            # same after suffix strip
            "Boston Dynamics", "Boston Consulting Group",                   # brand "boston"
            "Figure", "Apptronik"]))                                        # unrelated singles
        groups = [sorted(names[i] for i in g) for g in entities.candidate_groups(names)]
        self.assertIn(["HD Hyundai", "Hyundai Motor Group", "Hyundai Rotem"], groups)
        self.assertIn(["Schaeffler AG", "Schaeffler Group"], groups)
        self.assertIn(["Boston Consulting Group", "Boston Dynamics"], groups)
        self.assertFalse(any("Figure" in g or "Apptronik" in g for g in groups))

    def test_no_transitive_chaining(self):
        # A~B share "alpha", B~C share "omega": two small groups, never one A-B-C blob.
        names = dict(enumerate(["Alpha Robots", "Alpha Omega Labs", "Omega Drives"]))
        groups = [sorted(names[i] for i in g) for g in entities.candidate_groups(names)]
        self.assertIn(["Alpha Omega Labs", "Alpha Robots"], groups)
        self.assertIn(["Alpha Omega Labs", "Omega Drives"], groups)
        self.assertNotIn(sorted(names.values()), groups)

    def test_resolve_merges_chains_and_cycles(self):
        self.assertEqual(entities.resolve_merges({1: 2, 2: 3}), {1: 3, 2: 3})
        self.assertEqual(entities.resolve_merges({1: 2, 2: 1}), {2: 1})     # cycle → smallest id survives


class TestPlan(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "dev-utils"))
        import entity_audit  # noqa: PLC0415
        self.plan = entity_audit.plan
        self.ids = {"A Co": 1, "A Corp": 2, "Parent": 3, "Sis": 4}

    def _row(self, **kw):
        base = {"entity": "A Co", "same_as": None, "parent": None, "sister_of": [], "wrong_aliases": [],
                "confidence": 0.9, "reason": "r", "group": ["A Co", "A Corp", "Parent", "Sis"]}
        return {**base, **kw}

    def test_thresholds_split_apply_and_review(self):
        p = self.plan([self._row(same_as="A Corp", confidence=0.9, wrong_aliases=["Bad"]),
                       self._row(entity="Sis", parent="Parent", sister_of=["A Co", "Outsider"], confidence=0.7,
                                 wrong_aliases=["Maybe"])], self.ids, 0.85, 0.80)
        self.assertEqual(p["final"], {1: 2})
        self.assertEqual(p["alias_drops"], [(1, "Bad")])
        self.assertEqual(sorted((r["claim"], r["other"]) for r in p["review"]),
                         [("sister_of", "A Co"), ("subsidiary_of", "Parent"), ("wrong_alias", "Maybe")])

    def test_names_gone_since_the_report_are_skipped(self):
        p = self.plan([self._row(entity="Merged Away", same_as="A Corp")], self.ids, 0.85, 0.80)
        self.assertEqual((p["final"], p["review"]), ({}, []))


class TestMergeAndLink(unittest.TestCase):
    def setUp(self):
        self.con = duckdb.connect(":memory:")
        db.init_schema(self.con)
        self.con.execute("""INSERT INTO catalysts (entry_id, sector, timestamp, raw_finding, headline, sentiment_label)
                            VALUES ('ROB-1', 'Robotics', CURRENT_DATE, 'Hyundai Motors deploys Atlas', 'h', 'bullish')""")
        self.cid = self.con.execute("SELECT catalyst_id FROM catalysts").fetchone()[0]
        e = lambda n: db.insert_entity(self.con, n, None, "public_company")  # noqa: E731
        self.hmg, self.hm, self.bd, self.rotem = e("Hyundai Motor Group"), e("Hyundai Motors"), \
            e("Boston Dynamics"), e("Hyundai Rotem")
        db.insert_alias(self.con, self.hm, "Hyundai Motor Co")
        for a in (self.hmg, self.hm):   # the same fact under both spellings on one card
            self.con.execute("""INSERT INTO relationships (catalyst_id, entity_a_id, rel_type, entity_b_id, confidence)
                                VALUES (?, ?, 'deploys', ?, 0.8)""", [self.cid, a, self.bd])
        entities.link(self.con, self.rotem, self.hm, "subsidiary_of", confidence=0.9, reason="r", source="test")

    def tearDown(self):
        self.con.close()

    def test_merge_repoints_everything_and_dedupes(self):
        out = entities.merge(self.con, self.hm, self.hmg, confidence=0.95, reason="same firm", source="test")
        self.assertEqual(out, {"cards": 1, "deduped": 1})
        self.assertIsNone(self.con.execute("SELECT 1 FROM entities WHERE name = 'Hyundai Motors'").fetchone())
        self.assertEqual(self.con.execute("SELECT entity_a_id, COUNT(*) FROM relationships GROUP BY 1").fetchall(),
                         [(self.hmg, 1)])
        aliases = {r[0] for r in self.con.execute("SELECT alias FROM entity_aliases WHERE entity_id = ?",
                                                  [self.hmg]).fetchall()}
        self.assertTrue({"Hyundai Motors", "Hyundai Motor Co"} <= aliases)       # name + aliases carried over
        self.assertEqual(db.find_entity_exact(self.con, "hyundai motors"), self.hmg)   # future mentions resolve
        self.assertEqual(self.con.execute("SELECT child_id, parent_id FROM entity_links").fetchall(),
                         [(self.rotem, self.hmg)])                               # link follows the merge
        self.assertEqual(self.con.execute("SELECT src_name, dst_name FROM entity_merges").fetchall(),
                         [("Hyundai Motors", "Hyundai Motor Group")])

    def test_merge_keeps_ticker_handles_b_side_and_unresolved(self):
        self.con.execute("UPDATE entities SET ticker = 'HYMTF' WHERE entity_id = ?", [self.hm])
        self.con.execute("""INSERT INTO relationships (catalyst_id, entity_a_id, rel_type, entity_b_id, confidence)
                            VALUES (?, ?, 'supplies', ?, 0.7)""", [self.cid, self.bd, self.hm])        # b side
        self.con.execute("INSERT INTO unresolved_entities (mention, catalyst_id, resolved_to) VALUES ('HM', ?, ?)",
                         [self.cid, self.hm])
        self.con.execute("""INSERT INTO entity_handles (name_key, channel, handle, source) VALUES
                            ('hyundai motor group', 'x', NULL, 'abstain'),
                            ('hyundai motors', 'x', '@Hyundai', 'linkedin_verified')""")
        entities.merge(self.con, self.hm, self.hmg, confidence=0.95, reason="same firm", source="test")
        self.assertEqual(self.con.execute("SELECT ticker FROM entities WHERE entity_id = ?", [self.hmg]).fetchone(),
                         ("HYMTF",))
        self.assertEqual(self.con.execute("SELECT entity_b_id FROM relationships WHERE rel_type = 'supplies'")
                         .fetchone(), (self.hmg,))
        self.assertEqual(self.con.execute("SELECT resolved_to FROM unresolved_entities").fetchone(), (self.hmg,))
        self.assertEqual(self.con.execute("SELECT handle FROM entity_handles WHERE name_key = 'hyundai motor group'")
                         .fetchone(), ("@Hyundai",))                   # verified beats abstain
        self.assertEqual(self.con.execute("SELECT src_ticker FROM entity_merges").fetchone(), ("HYMTF",))

    def test_sister_order_renormalized_after_merge(self):
        late = db.insert_entity(self.con, "Hyundai Mobis", None, "public_company")   # highest id
        entities.link(self.con, self.bd, late, "sister_of", confidence=0.9, reason="r", source="test")
        # Fold the low-id side into an entity with a higher id than `late`: the
        # re-pointed pair (other, late) must be stored low-id-first again.
        other = db.insert_entity(self.con, "Zeta", None, "public_company")
        entities.merge(self.con, self.bd, other, confidence=0.9, reason="r", source="test")
        rows = self.con.execute("SELECT child_id, parent_id FROM entity_links WHERE relation = 'sister_of'").fetchall()
        self.assertEqual(rows, [(min(late, other), max(late, other))])

    def test_entity_docs_for_consumers(self):
        from src import firestore_export
        entities.link(self.con, self.bd, self.rotem, "sister_of", confidence=0.9, reason="r", source="test")
        orphan = db.insert_entity(self.con, "No Cards Inc", None, "private_company")  # noqa: F841
        docs = firestore_export.entity_docs(self.con, "Robotics")
        by_name = {d["name"]: d for d in docs.values()}
        self.assertNotIn("No Cards Inc", by_name)                 # only entities on live cards
        self.assertNotIn("Hyundai Rotem", by_name)                # linked but on no card
        hm = by_name["Hyundai Motors"]
        self.assertEqual((hm["aliases"], hm["aliases_lower"]), (["Hyundai Motor Co"], ["hyundai motor co"]))
        self.assertEqual(hm["children"], ["Hyundai Rotem"])
        self.assertEqual(by_name["Boston Dynamics"]["sisters"], ["Hyundai Rotem"])
        self.assertEqual(hm["card_count"], 1)
        self.assertEqual(hm["name_lower"], "hyundai motors")

    def test_sister_link_stored_once(self):
        entities.link(self.con, self.bd, self.rotem, "sister_of", confidence=0.9, reason="r", source="test")
        entities.link(self.con, self.rotem, self.bd, "sister_of", confidence=0.9, reason="r", source="test")
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM entity_links WHERE relation = 'sister_of'")
                         .fetchone()[0], 1)
        self.assertFalse(entities.link(self.con, self.bd, self.bd, "sister_of", confidence=1, reason="r",
                                       source="test"))


if __name__ == "__main__":
    unittest.main()
