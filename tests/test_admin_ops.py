"""Tests for src/admin_ops.py — every edit, its revert, and the conflict guard.

Run: python -m unittest tests/test_admin_ops.py -v   (inside the ingest image)
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import duckdb  # noqa: E402

from src import admin_ops as ops, db, entities  # noqa: E402

VOCAB = {"competes_with", "deploys", "partners_with", "acquires"}
WHO = {"email": "info@solutionjet.net", "ip": "203.0.113.1", "vocab": VOCAB}


class _Fixture(unittest.TestCase):
    def setUp(self):
        self.con = duckdb.connect(":memory:")
        db.init_schema(self.con)
        ops.ensure_schema(self.con)
        e = lambda n: db.insert_entity(self.con, n, None, "private_company")  # noqa: E731
        self.aws, self.logi, self.dhl, self.robo = e("Amazon Web Services"), e("Amazon Logistics"), e("DHL"), \
            e("Amazon Robotics")
        db.insert_alias(self.con, self.aws, "Amazon")
        db.insert_alias(self.con, self.aws, "AWS")
        entities.link(self.con, self.aws, self.logi, "sister_of", confidence=1, reason="r", source="test")
        self.cids = []
        for i in range(6):   # Amazon delivery stories that landed on AWS through the "Amazon" alias
            self.con.execute("""INSERT INTO catalysts (entry_id, sector, timestamp, raw_finding, headline, sentiment_label)
                                VALUES (?, 'Robotics', CURRENT_DATE, 'Amazon expands drone delivery vs DHL.', 'h', 'bullish')""",
                             [f"ROB-{i}"])
            cid = self.con.execute("SELECT catalyst_id FROM catalysts WHERE entry_id = ?", [f"ROB-{i}"]).fetchone()[0]
            self.cids.append(cid)
            self.con.execute("""INSERT INTO relationships (catalyst_id, entity_a_id, rel_type, entity_b_id, confidence,
                                evidence_type) VALUES (?, ?, 'competes_with', ?, 0.8, 'speculative')""",
                             [cid, self.aws, self.dhl])
        self.rel = self.con.execute("SELECT MIN(rel_id) FROM relationships").fetchone()[0]

    def tearDown(self):
        self.con.close()

    def _edge(self):
        return self.con.execute("SELECT entity_a_id, entity_b_id, rel_type, status, evidence_type FROM relationships "
                                "WHERE rel_id = ?", [self.rel]).fetchone()


class TestAdminOps(_Fixture):

    def test_repoint_then_revert_restores_exactly(self):
        out = ops.apply_edit(self.con, "repoint_edge", {"rel_id": self.rel, "entity_a_id": self.logi,
                                                        "expect": {"entity_a_id": self.aws}}, **WHO)
        self.assertEqual(self._edge(), (self.logi, self.dhl, "competes_with", "active", "speculative"))  # evidence kept
        self.assertEqual(out["cards"], ["ROB-0"])
        others = self.con.execute("SELECT COUNT(*) FROM relationships WHERE entity_a_id = ?", [self.aws]).fetchone()[0]
        self.assertEqual(others, 5)                                          # only the chosen edge moved
        ops.revert_edit(self.con, out["edit_id"], email=WHO["email"], ip=WHO["ip"], vocab=VOCAB)
        self.assertEqual(self._edge(), (self.aws, self.dhl, "competes_with", "active", "speculative"))
        log = {e["edit_id"]: e for e in ops.edits(self.con)}
        self.assertIsNotNone(log[out["edit_id"]]["reverted_by"])
        with self.assertRaises(ops.EditError):                             # can't revert twice
            ops.revert_edit(self.con, out["edit_id"], email=WHO["email"], ip=WHO["ip"], vocab=VOCAB)

    def test_stale_screen_is_rejected(self):
        with self.assertRaises(ops.EditConflict):
            ops.apply_edit(self.con, "repoint_edge", {"rel_id": self.rel, "entity_a_id": self.logi,
                                                      "expect": {"entity_a_id": self.robo}}, **WHO)
        with self.assertRaises(ops.EditError):                             # expect is mandatory
            ops.apply_edit(self.con, "repoint_edge", {"rel_id": self.rel, "entity_a_id": self.logi}, **WHO)
        self.assertEqual(self._edge()[0], self.aws)                         # nothing changed

    def test_invalid_edits_change_nothing(self):
        for payload in ({"rel_id": self.rel, "rel_type": "made_up", "expect": {"rel_type": "competes_with"}},
                        {"rel_id": self.rel, "entity_a_id": 99999, "expect": {"entity_a_id": self.aws}},
                        {"rel_id": self.rel, "entity_a_id": self.dhl, "expect": {"entity_a_id": self.aws}}):  # self-loop
            with self.assertRaises(ops.EditError):
                ops.apply_edit(self.con, "repoint_edge", payload, **WHO)
        self.assertEqual(self._edge()[:3], (self.aws, self.dhl, "competes_with"))
        self.assertEqual(ops.edits(self.con), [])

    def test_repoint_refuses_to_duplicate_an_edge_on_the_same_card(self):
        cid = self.con.execute("SELECT catalyst_id FROM relationships WHERE rel_id = ?", [self.rel]).fetchone()[0]
        self.con.execute("""INSERT INTO relationships (catalyst_id, entity_a_id, rel_type, entity_b_id, confidence)
                            VALUES (?, ?, 'competes_with', ?, 0.7)""", [cid, self.logi, self.dhl])
        with self.assertRaises(ops.EditError):
            ops.apply_edit(self.con, "repoint_edge", {"rel_id": self.rel, "entity_a_id": self.logi,
                                                      "expect": {"entity_a_id": self.aws}}, **WHO)
        self.assertEqual(self._edge()[0], self.aws)

    def test_reviving_an_edge_refuses_to_make_a_live_twin(self):
        self.con.execute("UPDATE relationships SET status = 'rejected' WHERE rel_id = ?", [self.rel])
        cid = self.con.execute("SELECT catalyst_id FROM relationships WHERE rel_id = ?", [self.rel]).fetchone()[0]
        self.con.execute("""INSERT INTO relationships (catalyst_id, entity_a_id, rel_type, entity_b_id, confidence)
                            VALUES (?, ?, 'competes_with', ?, 0.7)""", [cid, self.aws, self.dhl])
        with self.assertRaises(ops.EditError):
            ops.apply_edit(self.con, "set_edge_status", {"rel_id": self.rel, "status": "active",
                                                         "expect": {"status": "rejected"}}, **WHO)
        self.assertEqual(self._edge()[3], "rejected")

    def test_spin_out_alias_carries_selected_edges_and_reverts_exactly(self):
        moved, kept = self.rel, self.rel + 1
        before = self.con.execute("SELECT rel_id, entity_a_id, entity_b_id FROM relationships ORDER BY rel_id").fetchall()
        out = ops.apply_edit(self.con, "spin_out", {"alias": "Amazon", "from_entity_id": self.aws,
                                                    "type": "public_company", "rel_ids": [moved]}, **WHO)
        new = out["after"]["entity_id"]
        self.assertEqual(self.con.execute("SELECT name FROM entities WHERE entity_id = ?", [new]).fetchone(), ("Amazon",))
        self.assertEqual(self._edge()[:2], (new, self.dhl))                 # only the chosen edge moved
        self.assertEqual(self.con.execute("SELECT entity_a_id FROM relationships WHERE rel_id = ?", [kept]).fetchone(),
                         (self.aws,))
        aliases = lambda e: {a for (a,) in self.con.execute(  # noqa: E731
            "SELECT alias FROM entity_aliases WHERE entity_id = ?", [e]).fetchall()}
        self.assertNotIn("Amazon", aliases(self.aws))
        self.assertEqual(out["cards"], ["ROB-0"])
        rev = ops.revert_edit(self.con, out["edit_id"], email=WHO["email"], ip=WHO["ip"], vocab=VOCAB)
        self.assertEqual(rev["cards"], ["ROB-0"])
        self.assertEqual(self.con.execute("SELECT rel_id, entity_a_id, entity_b_id FROM relationships ORDER BY rel_id")
                         .fetchall(), before)
        self.assertIn("Amazon", aliases(self.aws))
        self.assertIsNone(self.con.execute("SELECT 1 FROM entities WHERE entity_id = ?", [new]).fetchone())

    def test_spin_out_without_edges_and_its_guards(self):
        out = ops.apply_edit(self.con, "spin_out", {"alias": "AWS", "from_entity_id": self.aws, "name": "AWS Corp",
                                                    "type": "private_company"}, **WHO)
        self.assertEqual(out["cards"], [])
        self.assertEqual(self.con.execute("SELECT alias FROM entity_aliases WHERE entity_id = ?",
                                          [out["after"]["entity_id"]]).fetchall(), [("AWS",)])
        with self.assertRaises(ops.EditConflict):                          # alias already gone
            ops.apply_edit(self.con, "spin_out", {"alias": "AWS", "from_entity_id": self.aws, "type": "x"}, **WHO)
        with self.assertRaises(ops.EditError):                             # name taken
            ops.apply_edit(self.con, "spin_out", {"alias": "Amazon", "from_entity_id": self.aws, "name": "DHL",
                                                  "type": "x"}, **WHO)
        with self.assertRaises(ops.EditConflict):                          # edge not on the source entity
            ops.apply_edit(self.con, "spin_out", {"alias": "Amazon", "from_entity_id": self.logi, "type": "x",
                                                  "rel_ids": [self.rel]}, **WHO)

    def test_spin_out_revert_refuses_to_recreate_a_live_twin(self):
        out = ops.apply_edit(self.con, "spin_out", {"alias": "Amazon", "from_entity_id": self.aws,
                                                    "type": "public_company", "rel_ids": [self.rel]}, **WHO)
        cid = self.con.execute("SELECT catalyst_id FROM relationships WHERE rel_id = ?", [self.rel]).fetchone()[0]
        self.con.execute("""INSERT INTO relationships (catalyst_id, entity_a_id, rel_type, entity_b_id, confidence)
                            VALUES (?, ?, 'competes_with', ?, 0.7)""", [cid, self.aws, self.dhl])
        n = self.con.execute("SELECT COUNT(*) FROM relationships").fetchone()[0]
        with self.assertRaises(ops.EditConflict):
            ops.revert_edit(self.con, out["edit_id"], email=WHO["email"], ip=WHO["ip"], vocab=VOCAB)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM relationships").fetchone()[0], n)   # nothing deleted
        self.assertEqual(self._edge()[0], out["after"]["entity_id"])

    def test_spin_out_rejects_non_integer_rel_ids(self):
        with self.assertRaises(ops.EditError):
            ops.apply_edit(self.con, "spin_out", {"alias": "Amazon", "from_entity_id": self.aws, "type": "x",
                                                  "rel_ids": [self.rel, str(self.rel)]}, **WHO)

    def test_spin_out_revert_refuses_once_the_new_entity_is_used(self):
        out = ops.apply_edit(self.con, "spin_out", {"alias": "Amazon", "from_entity_id": self.aws,
                                                    "type": "public_company", "rel_ids": [self.rel]}, **WHO)
        new = out["after"]["entity_id"]
        ops.apply_edit(self.con, "repoint_edge", {"rel_id": self.rel + 2, "entity_a_id": new,
                                                  "expect": {"entity_a_id": self.aws}}, **WHO)
        with self.assertRaises(ops.EditConflict):
            ops.revert_edit(self.con, out["edit_id"], email=WHO["email"], ip=WHO["ip"], vocab=VOCAB)
        self.assertEqual(self._edge()[0], new)                              # nothing half-reverted

    def test_materialized_status_round_trip(self):
        self.con.execute("UPDATE relationships SET status = 'materialized' WHERE rel_id = ?", [self.rel])
        out = ops.apply_edit(self.con, "set_edge_status", {"rel_id": self.rel, "status": "rejected",
                                                           "expect": {"status": "materialized"}}, **WHO)
        ops.revert_edit(self.con, out["edit_id"], email=WHO["email"], ip=WHO["ip"], vocab=VOCAB)
        self.assertEqual(self._edge()[3], "materialized")

    def test_status_and_revert(self):
        out = ops.apply_edit(self.con, "set_edge_status", {"rel_id": self.rel, "status": "rejected",
                                                           "expect": {"status": "active"}}, **WHO)
        self.assertEqual(self._edge()[3], "rejected")
        ops.revert_edit(self.con, out["edit_id"], email=WHO["email"], ip=WHO["ip"], vocab=VOCAB)
        self.assertEqual(self._edge()[3], "active")

    def test_move_alias_and_revert_keeps_preexisting_alias(self):
        db.insert_alias(self.con, self.logi, "Amazon")                      # target already had it
        out = ops.apply_edit(self.con, "move_alias", {"alias": "Amazon", "from_entity_id": self.aws,
                                                      "to_entity_id": self.logi}, **WHO)
        self.assertEqual(db.find_entity_exact(self.con, "Amazon"), self.logi)
        ops.revert_edit(self.con, out["edit_id"], email=WHO["email"], ip=WHO["ip"], vocab=VOCAB)
        owners = {r[0] for r in self.con.execute("SELECT entity_id FROM entity_aliases WHERE alias = 'Amazon'").fetchall()}
        self.assertEqual(owners, {self.aws, self.logi})                     # restored, pre-existing one untouched

    def test_drop_alias_and_revert(self):
        out = ops.apply_edit(self.con, "move_alias", {"alias": "AWS", "from_entity_id": self.aws}, **WHO)
        self.assertIsNone(db.find_entity_exact(self.con, "AWS"))
        ops.revert_edit(self.con, out["edit_id"], email=WHO["email"], ip=WHO["ip"], vocab=VOCAB)
        self.assertEqual(db.find_entity_exact(self.con, "AWS"), self.aws)

    def test_create_entity_revert_only_when_unused(self):
        out = ops.apply_edit(self.con, "create_entity", {"name": "Amazon", "type": "public_company",
                                                         "ticker": "AMZN"}, **WHO)
        amzn = out["after"]["entity_id"]
        link = ops.apply_edit(self.con, "link", {"child_id": self.logi, "parent_id": amzn,
                                                 "relation": "subsidiary_of"}, **WHO)
        with self.assertRaises(ops.EditConflict):                          # in use → refuse
            ops.revert_edit(self.con, out["edit_id"], email=WHO["email"], ip=WHO["ip"], vocab=VOCAB)
        ops.revert_edit(self.con, link["edit_id"], email=WHO["email"], ip=WHO["ip"], vocab=VOCAB)   # unlink
        ops.revert_edit(self.con, out["edit_id"], email=WHO["email"], ip=WHO["ip"], vocab=VOCAB)
        self.assertIsNone(self.con.execute("SELECT 1 FROM entities WHERE name = 'Amazon'").fetchone())

    def test_merge_is_not_revertible(self):
        out = ops.apply_edit(self.con, "merge", {"src_id": self.robo, "dst_id": self.logi}, **WHO)
        self.assertFalse(out["revertible"])
        with self.assertRaises(ops.EditError):
            ops.revert_edit(self.con, out["edit_id"], email=WHO["email"], ip=WHO["ip"], vocab=VOCAB)

    def test_scan_flags_entity_reached_only_through_a_brand_alias(self):
        s = ops.scan(self.con)
        self.assertEqual([x["name"] for x in s["suspect_entities"]], ["Amazon Web Services"])
        self.assertEqual(s["suspect_entities"][0]["named_in"], 0)
        detail = ops.entity_detail(self.con, self.aws)
        self.assertTrue(next(a for a in detail["aliases"] if a["alias"] == "Amazon")["bare_brand"])
        self.assertFalse(detail["relationships"][0]["named_in_card"])
        # A top-level entity's own brand word counts as naming it.
        top = db.insert_entity(self.con, "Schaeffler AG", None, "public_company")
        db.insert_alias(self.con, top, "Schaeffler")
        kid = db.insert_entity(self.con, "Schaeffler Embodied", None, "private_company")
        entities.link(self.con, kid, top, "subsidiary_of", confidence=1, reason="r", source="test")
        for i in range(5):
            self.con.execute("""INSERT INTO catalysts (entry_id, sector, timestamp, raw_finding, headline, sentiment_label)
                                VALUES (?, 'Robotics', CURRENT_DATE, 'Schaeffler ships actuators.', 'h', 'bullish')""", [f"S-{i}"])
            cid = self.con.execute("SELECT catalyst_id FROM catalysts WHERE entry_id = ?", [f"S-{i}"]).fetchone()[0]
            self.con.execute("""INSERT INTO relationships (catalyst_id, entity_a_id, rel_type, entity_b_id, confidence)
                                VALUES (?, ?, 'partners_with', ?, 0.8)""", [cid, top, self.dhl])
        self.assertEqual([x["name"] for x in ops.scan(self.con)["suspect_entities"]], ["Amazon Web Services"])
        db.insert_alias(self.con, self.logi, "amazon")
        self.assertEqual(ops.scan(self.con)["shared_aliases"][0]["entities"], ["Amazon Logistics", "Amazon Web Services"])


class FakeLLM:
    def __init__(self, picks):
        self.models, self._picks = self, picks

    def generate_content(self, model, contents, config):
        schema = config.response_schema
        return type("R", (), {"parsed": schema(picks=[schema.model_fields["picks"].annotation.__args__[0](**p)
                                                      for p in self._picks]), "text": ""})()


class TestSuggest(_Fixture):
    CFG = type("C", (), {"extraction": type("E", (), {"model": "gemini-3.6-flash", "direction_thinking_level": "high"})()})()

    def test_suggestions_only_name_family_members_and_write_nothing(self):
        llm = FakeLLM([{"rel_id": self.rel, "entity": "Amazon Logistics", "confidence": 0.9, "reason": "delivery story"},
                       {"rel_id": self.rel + 1, "entity": "Walmart", "confidence": 0.9, "reason": "not a candidate"}])
        out = {s["rel_id"]: s for s in ops.suggest_repoints(self.con, llm, self.CFG, self.aws, [self.rel, self.rel + 1])}
        self.assertEqual((out[self.rel]["suggested_id"], out[self.rel]["side"]), (self.logi, "entity_a_id"))
        self.assertEqual(out[self.rel + 1]["suggested_id"], self.aws)       # outside the family → keep
        self.assertEqual(self._edge()[0], self.aws)                         # nothing written
        self.assertEqual(ops.edits(self.con), [])


if __name__ == "__main__":
    unittest.main()
