"""Tests for src/review.py matching + the Pass-3 'unclear' marker (no Firestore).

Run: python -m unittest tests/test_review.py -v
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from src import direction, review  # noqa: E402

ITEMS = [
    {"id": "a", "kind": "direction_unclear", "rel_id": 4242, "doc_id": "rel4242"},
    {"id": "b", "kind": "entity_wrong_alias", "entity": "Meta Platforms", "other": "Meta's Llama"},
    {"id": "c", "kind": "entity_ingest_unsure", "doc_id": "SEER Robotics~ROB-1"},
    {"id": "d", "kind": "dedup_matched_missing", "doc_id": "ROB-9"},
]


class TestMatching(unittest.TestCase):
    def test_relationship_item_settled_by_editing_that_edge(self):
        self.assertEqual(review.matching(ITEMS, rel_ids=[4242]), ["a"])
        llm = [{"id": "h", "kind": "humanoid_entity_check", "rel_id": 77, "entity": "Humanoid"}]
        self.assertEqual(review.matching(llm, rel_ids=[77]), ["h"])
        self.assertEqual(review.matching(llm, entity_names=["Humanoid"]), [])     # only by its relationship
        self.assertEqual(review.matching(ITEMS, rel_ids=[1]), [])

    def test_alias_item_settled_by_moving_that_alias(self):
        self.assertEqual(review.matching(ITEMS, aliases=[("meta platforms", "META'S LLAMA")]), ["b"])

    def test_ingest_unsure_item_settled_by_an_edit_on_that_entity(self):
        self.assertEqual(review.matching(ITEMS, entity_names=["seer robotics"]), ["c"])

    def test_alias_item_is_not_settled_by_unrelated_edits_on_the_entity(self):
        # moving a DIFFERENT alias off Meta, or repointing edges into Meta, must not close it
        self.assertEqual(review.matching(ITEMS, entity_names=["Meta Platforms"],
                                         aliases=[("Meta Platforms", "Meta AI")]), [])

    def test_audit_merge_item_only_by_joining_exactly_those_two(self):
        items = [{"id": "m", "kind": "entity_merge", "entity": "Hyundai Motors", "other": "Hyundai Motor Group"}]
        self.assertEqual(review.matching(items, entity_names=["Hyundai Motors"]), [])
        self.assertEqual(review.matching(items, entity_pairs=[("Hyundai Motors", "Kia")]), [])
        self.assertEqual(review.matching(items, entity_pairs=[("hyundai motor group", "Hyundai Motors")]), ["m"])
        link = [{"id": "l", "kind": "entity_subsidiary_of", "entity": "Hyundai Rotem", "other": "Hyundai Motor Group"}]
        self.assertEqual(review.matching(link, entity_pairs=[("Hyundai Rotem", "Hyundai Motor Group")]), ["l"])

    def test_dedup_and_unknown_kinds_are_never_auto_settled_even_with_matchable_fields(self):
        items = [{"id": "d", "kind": "dedup_matched_missing", "doc_id": "ROB-9", "rel_id": 5, "entity": "X", "other": "Y"},
                 {"id": "u", "kind": "some_future_kind", "rel_id": 5, "entity": "X"}]
        self.assertEqual(review.matching(items, rel_ids=[5], entity_names=["X"], aliases=[("X", "Y")],
                                         entity_pairs=[("X", "Y")]), [])


class _FakeDoc:
    def __init__(self, store, i): self.store, self.id = store, i
    def get(self): return type("S", (), {"exists": self.id in self.store, "to_dict": lambda _s: dict(self.store[self.id])})()
    @property
    def reference(self): return self


class _FakeCol:
    def __init__(self, store, flt=None): self.store, self.flt = store, flt
    def document(self, i): return _FakeDoc(self.store, i)
    def where(self, f, op, v): return _FakeCol(self.store, (f, v))
    def stream(self):
        for i, d in self.store.items():
            if self.flt is None or d.get(self.flt[0]) == self.flt[1]:
                yield type("D", (), {"id": i, "reference": _FakeDoc(self.store, i), "to_dict": lambda _s, d=d: dict(d)})()


class _FakeFs:
    def __init__(self, store): self.store = store
    def collection(self, *_a): return self
    def document(self, *_a): return self
    def batch(self):
        fs = self
        class B:
            def __init__(self): self.ops = []
            def update(self, ref, data): self.ops.append((ref.id, data))
            def commit(self):
                for i, data in self.ops: fs.store[i].update(data)
        return B()


class TestRevertReopen(unittest.TestCase):
    def test_revert_of_any_edit_in_a_large_bulk_reopens(self):
        store = {"x": {"kind": "direction_unclear", "rel_id": 1, "resolved": False}}
        fs = _FakeFs(store)
        fs.collection = lambda *_a: fs; fs.document = lambda *_a: fs
        orig = review._entries
        review._entries = lambda _fs: _FakeCol(store)
        self.addCleanup(setattr, review, "_entries", orig)
        ids = [f"{n:012x}" for n in range(30)]
        review.resolve(fs, ["x"], by="t", resolution=review.SETTLED_BY + ",".join(ids), edit_ids=ids)
        self.assertTrue(store["x"]["resolved"])
        self.assertEqual(review.reopen_for_edits(fs, [ids[25]]), 1)          # beyond the 300-char display text
        self.assertFalse(store["x"]["resolved"])
        self.assertEqual(review.reopen_for_edits(fs, [ids[25]]), 0)          # nothing left to reopen


class TestUnclearMarker(unittest.TestCase):
    def test_unclear_verdict_marks_the_edge_and_correct_does_not(self):
        V = direction.DirectionVerdict
        kept = [{"entity_a": "A", "rel_type": "supplies", "entity_b": "B"},
                {"entity_a": "C", "rel_type": "deploys", "entity_b": "D"}]
        verdicts = {0: V(index=0, statement="A supplies B", verdict="unclear", reason="test reason"),
                    1: V(index=1, statement="C deploys D", verdict="correct", reason="test reason")}
        direction.apply_to_extraction(kept, verdicts, [0, 1])
        self.assertTrue(kept[0].get("direction_unclear"))
        self.assertFalse(kept[1].get("direction_unclear"))


if __name__ == "__main__":
    unittest.main()
