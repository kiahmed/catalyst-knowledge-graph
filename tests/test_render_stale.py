"""Tests for dev-utils/render_stale.py — orphan selection, the delete safety guard, and the per-id delete path
(with an in-memory fake of Firestore + the bucket; no GCP).

Run: python -m unittest tests/test_render_stale.py -v
"""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("render_stale", ROOT / "dev-utils" / "render_stale.py")
rs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rs)


class TestOrphans(unittest.TestCase):
    def test_orphans_skip_live_and_merged_duplicates(self):
        imgs = {"ROB-1", "ROB-2", "ROB-3", "ROB-4"}
        self.assertEqual(rs.orphans_of(imgs, live={"ROB-1"}, merged={"ROB-2"}), ["ROB-3", "ROB-4"])
        self.assertEqual(rs.orphans_of(imgs, live=imgs, merged=set()), [])

    def test_safety_guard(self):
        self.assertTrue(rs.safe_to_delete(900, 1800, 30)[0])
        self.assertFalse(rs.safe_to_delete(50, 1800, 30)[0])        # too few live cards read
        self.assertFalse(rs.safe_to_delete(900, 1800, 1000)[0])     # more than half the images
        self.assertTrue(rs.safe_to_delete(900, 0, 0)[0])


class _Snap:
    def __init__(self, exists, data=None, id_=""):
        self.exists, self._d, self.id, self.update_time = exists, data or {}, id_, None

    def to_dict(self):
        return dict(self._d)


class _Items:
    """Fake catalysts/items: docs {id: data}; a hook runs on every per-id get (to simulate a card coming back)."""
    def __init__(self, docs):
        self.docs, self.on_get = docs, None

    def select(self, _fields):
        return self

    def stream(self):
        return [_Snap(True, d, i) for i, d in self.docs.items()]

    def document(self, cid):
        items = self
        class D:
            def get(self_inner):
                if items.on_get:
                    items.on_get(cid)
                return _Snap(cid in items.docs, items.docs.get(cid), cid)
        return D()

    def where(self, field, op, value):
        hits = [_Snap(True, d, i) for i, d in self.docs.items() if value in (d.get(field) or [])]
        class Q:
            def limit(self_inner, n):
                return self_inner
            def stream(self_inner):
                return hits
        return Q()


class _Bucket:
    def __init__(self, names):
        self.names = set(names)

    def list_blobs(self, prefix=""):
        return [type("B", (), {"name": n})() for n in sorted(self.names) if n.startswith(prefix)]

    def blob(self, name):
        bucket = self
        class B:
            def exists(self_inner):
                return name in bucket.names
            def delete(self_inner):
                bucket.names.discard(name)
        return B()


class TestDeletePath(unittest.TestCase):
    def _run(self, docs, names, orphan_ids, hook=None):
        import tempfile
        from types import SimpleNamespace
        f = Path(tempfile.mkdtemp()) / "orphans.ids"
        f.write_text(",".join(orphan_ids))
        items, bucket = _Items(docs), _Bucket(names)
        items.on_get = hook and (lambda cid: hook(items, cid))
        rc = rs.delete_orphans(SimpleNamespace(delete_orphans=str(f)), items=items, bucket=bucket)
        return rc, bucket.names

    def _live_cards(self, n=120):
        return {f"ROB-L{i}": {"headline": "h"} for i in range(n)}

    def test_deletes_both_images_of_a_real_orphan(self):
        names = {f"cards/ROB-L{i}.png" for i in range(120)} | {"cards/ROB-X.png", "graphs/ROB-X.png"}
        rc, left = self._run(self._live_cards(), names, ["ROB-X"])
        self.assertEqual(rc, 0)
        self.assertNotIn("cards/ROB-X.png", left)
        self.assertNotIn("graphs/ROB-X.png", left)

    def test_per_id_recheck_keeps_a_card_that_came_back_mid_run(self):
        names = {f"cards/ROB-L{i}.png" for i in range(120)} | {"cards/ROB-X.png", "cards/ROB-Y.png"}

        def hook(items, cid):              # while ROB-X is being handled, ROB-Y's card is restored
            if cid == "ROB-X":
                items.docs["ROB-Y"] = {"headline": "back"}
        rc, left = self._run(self._live_cards(), names, ["ROB-X", "ROB-Y"], hook)
        self.assertNotIn("cards/ROB-X.png", left)
        self.assertIn("cards/ROB-Y.png", left)                     # fresh per-id read saw it

    def test_merged_from_keeps_and_guard_blocks(self):
        docs = self._live_cards()
        docs["ROB-L0"]["merged_from"] = ["ROB-M"]
        names = {f"cards/ROB-L{i}.png" for i in range(120)} | {"cards/ROB-M.png"}
        self.assertIn("cards/ROB-M.png", self._run(docs, names, ["ROB-M"])[1])
        rc, left = self._run({"ROB-L0": {}}, {"cards/ROB-L0.png", "cards/ROB-Z.png"}, ["ROB-Z"])
        self.assertEqual(rc, 1)                                     # too few live cards → nothing deleted
        self.assertIn("cards/ROB-Z.png", left)


if __name__ == "__main__":
    unittest.main()
