"""Tests for the admin Rejected list (bulk restore / delete permanently) and the delete-entity name guard
(docs/ticket-admin-rejected-list-delete-guard.md), plus internal-event review fixes.

Run: python -m unittest tests/test_admin_rejected.py -v   (inside the ingest image)
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import duckdb  # noqa: E402

from src import admin_ops as ops, db, entities  # noqa: E402

WHO = {"email": "info@solutionjet.net", "ip": "203.0.113.1",
       "vocab": {"deploys", "partners_with", "competes_with", "supplies"}}


class _Fixture(unittest.TestCase):
    def setUp(self):
        self.con = duckdb.connect(":memory:")
        db.init_schema(self.con)
        ops.ensure_schema(self.con)
        e = lambda n, t="public_company": db.insert_entity(self.con, n, None, t)  # noqa: E731
        self.tesla, self.figure, self.fake = e("Tesla"), e("Figure"), e("Labor Shortage", "organization")
        self.con.execute("""INSERT INTO catalysts (entry_id, sector, timestamp, raw_finding, headline, sentiment_label)
                            VALUES ('ROB-1', 'Robotics', CURRENT_DATE, 'x', 'h', 'bullish')""")
        self.cid = self.con.execute("SELECT catalyst_id FROM catalysts").fetchone()[0]

    def _rel(self, a, t, b, status="active"):
        self.con.execute("""INSERT INTO relationships (catalyst_id, entity_a_id, rel_type, entity_b_id, confidence,
                            evidence_type, mechanism, status) VALUES (?, ?, ?, ?, 0.9, 'direct', 'm', ?)""",
                         [self.cid, a, t, b, status])
        return self.con.execute("SELECT max(rel_id) FROM relationships").fetchone()[0]

    def _status(self, rel_id):
        row = self.con.execute("SELECT status FROM relationships WHERE rel_id = ?", [rel_id]).fetchone()
        return row[0] if row else None

    def tearDown(self):
        self.con.close()


class TestRejectedList(_Fixture):
    def test_list_restore_and_delete(self):
        edge = self._rel(self.figure, "competes_with", self.tesla, "rejected")
        loop = self._rel(self.tesla, "deploys", self.tesla, "rejected")
        junk = self._rel(self.fake, "deploys", self.tesla, "rejected")
        self._rel(self.figure, "supplies", self.tesla)                       # live: not listed
        rows = {r["rel_id"]: r for r in ops.rejected_edges(self.con)}
        self.assertEqual(set(rows), {edge, loop, junk})
        self.assertTrue(rows[loop]["self_loop"])
        out = ops.restore_many(self.con, rel_ids=[edge, loop], **WHO)
        self.assertEqual((self._status(edge), self._status(loop)), ("active", "internal"))   # loop → internal event
        self.assertEqual(out["count"], 2)
        ops.delete_edge_many(self.con, rel_ids=[junk], **WHO)
        self.assertIsNone(self._status(junk))
        for eid in out["edit_ids"]:                                          # each is its own revertible edit
            ops.revert_edit(self.con, eid, email=WHO["email"], ip=WHO["ip"], vocab=WHO["vocab"])
        self.assertEqual((self._status(edge), self._status(loop)), ("rejected", "rejected"))

    def test_all_or_nothing_and_only_rejected(self):
        a = self._rel(self.figure, "competes_with", self.tesla, "rejected")
        live = self._rel(self.figure, "supplies", self.tesla)
        with self.assertRaises(ops.EditConflict):
            ops.delete_edge_many(self.con, rel_ids=[a, live], **WHO)       # a live row is never purged here
        with self.assertRaises(ops.EditError):
            ops.restore_many(self.con, rel_ids=[], **WHO)


class TestDeleteGuard(_Fixture):
    def test_active_edges_need_the_typed_name(self):
        self._rel(self.fake, "deploys", self.tesla)
        p = {"entity_id": self.fake, "expect_name": "Labor Shortage", "with_edges": True, "expect_edges": 1}
        with self.assertRaises(ops.EditError):
            ops.apply_edit(self.con, "delete_entity", p, **WHO)
        with self.assertRaises(ops.EditError):
            ops.apply_edit(self.con, "delete_entity", {**p, "confirm_name": "labor shortage"}, **WHO)   # exact
        ops.apply_edit(self.con, "delete_entity", {**p, "confirm_name": "Labor Shortage"}, **WHO)
        self.assertIsNone(self.con.execute("SELECT 1 FROM entities WHERE entity_id = ?", [self.fake]).fetchone())

    def test_only_rejected_edges_keep_the_simple_confirm(self):
        self._rel(self.fake, "deploys", self.tesla, "rejected")
        ops.apply_edit(self.con, "delete_entity", {"entity_id": self.fake, "expect_name": "Labor Shortage",
                                                   "with_edges": True, "expect_edges": 1}, **WHO)


class TestInternalEventFixes(_Fixture):
    def test_internal_row_cannot_be_repointed(self):
        loop = self._rel(self.tesla, "deploys", self.tesla, "internal")
        with self.assertRaises(ops.EditError):
            ops.apply_edit(self.con, "repoint_edge", {"rel_id": loop, "entity_b_id": self.figure,
                                                      "expect": {"entity_b_id": self.tesla}}, **WHO)

    def test_symmetric_loop_cannot_be_internal(self):
        loop = self._rel(self.tesla, "partners_with", self.tesla, "rejected")
        with self.assertRaises(ops.EditError):
            ops.apply_edit(self.con, "set_edge_status", {"rel_id": loop, "status": "internal",
                                                         "expect": {"status": "rejected"}}, **WHO)

    def test_two_merges_leave_one_internal_event_per_type(self):
        opt = db.insert_entity(self.con, "Optimus", None, "organization")
        ai5 = db.insert_entity(self.con, "AI5", None, "organization")
        self._rel(self.tesla, "deploys", opt)
        self._rel(self.tesla, "deploys", ai5)
        entities.merge(self.con, opt, self.tesla, confidence=None, reason="t", source="test")
        entities.merge(self.con, ai5, self.tesla, confidence=None, reason="t", source="test")
        self.assertEqual(self.con.execute("""SELECT count(*) FROM relationships WHERE status = 'internal'
                                             AND entity_a_id = entity_b_id""").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
