"""Tests for dev-utils/cleanup_checklist.py — tick carry-over, auto ticks that re-open, resolved carry-forward.

Run: python -m unittest tests/test_cleanup_checklist.py -v   (inside the ingest image)
"""
from __future__ import annotations

import importlib.util
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import duckdb  # noqa: E402

from src import db  # noqa: E402

_spec = importlib.util.spec_from_file_location("cleanup_checklist", ROOT / "dev-utils" / "cleanup_checklist.py")
cc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cc)


class TestChecklist(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        cc.OUT, cc.SEED = self.tmp / "open_items.md", self.tmp / "concept_entities_labels.txt"
        self.con = duckdb.connect(":memory:")
        db.init_schema(self.con)
        e = lambda n, t="organization": db.insert_entity(self.con, n, None, t)  # noqa: E731
        self.empty, self.busy, self.done, self.tesla = e("Retail Chains"), e("Venture Capital"), e("Labor Shortage"), \
            e("Tesla", "public_company")
        self.con.execute("""INSERT INTO catalysts (entry_id, sector, timestamp, raw_finding, headline, sentiment_label)
                            VALUES ('ROB-1', 'Robotics', CURRENT_DATE, 'Big funds back robots.', 'h', 'bullish')""")
        self.cid = self.con.execute("SELECT catalyst_id FROM catalysts").fetchone()[0]
        self._edge(self.busy, self.tesla)
        db.insert_alias(self.con, self.tesla, "humanoid robot makers")        # a generic alias (section C)
        cc.SEED.write_text("\n".join([
            f"  [concept] Retail Chains (id {self.empty}, organization) — x",
            f"  [concept] Venture Capital (id {self.busy}, organization) — x",
            f"  [done] Labor Shortage (id {self.done}, organization) — x",
            "  [concept] broken line without an id"]))

    def _edge(self, a, b):
        self.con.execute("""INSERT INTO relationships (catalyst_id, entity_a_id, rel_type, entity_b_id, confidence)
                            VALUES (?, ?, 'invests_in', ?, 0.8)""", [self.cid, a, b])

    def _run(self):
        text, _ = cc.build(self.con)
        cc._write_atomic(cc.OUT, text)
        return text

    def _line(self, text, key):
        return next(line for line in text.splitlines() if f"key:{key}" in line)

    def _tick(self, key):
        t = cc.OUT.read_text()
        cc.OUT.write_text(re.sub(rf"^- \[ \](.*key:{re.escape(key)} -->)$", r"- [x]\1", t, flags=re.M))

    def test_auto_tick_reopens_when_the_entity_gains_an_edge(self):
        t = self._run()
        self.assertTrue(self._line(t, f"concept:{self.empty}").startswith("- [x]"))   # no live edges → auto
        self.assertIn(" auto -->", self._line(t, f"concept:{self.empty}"))
        self._edge(self.empty, self.tesla)                                           # the regression
        t = self._run()
        self.assertTrue(self._line(t, f"concept:{self.empty}").startswith("- [ ]"))   # re-opens, not stuck ticked

    def test_your_tick_is_permanent_and_seed_done_counts_as_yours(self):
        self._run()
        self._tick(f"concept:{self.busy}")
        t = self._run()
        line = self._line(t, f"concept:{self.busy}")
        self.assertTrue(line.startswith("- [x]"))
        self.assertNotIn(" auto", line)
        seed = self._line(t, f"concept:{self.done}")                               # [done] in the seed list
        self.assertTrue(seed.startswith("- [x]"))
        self.assertNotIn(" auto", seed)
        self.assertIn("couldn't be read", t)                                         # malformed seed line counted

    def test_multiword_alias_tick_survives(self):
        t = self._run()
        key = re.search(r"key:(generic:\d+:[0-9a-f]{10})", t).group(1)
        self._tick(key)
        self.assertTrue(self._line(self._run(), key).startswith("- [x]"))

    def test_resolved_b_item_is_carried_ticked_and_reopens_if_it_comes_back(self):
        self._run()
        fake = "alias:999999:0123456789"
        t = cc.OUT.read_text()
        t = re.sub(r"(## B\..*\n\n.*\n\n)", lambda m: m.group(1) + f"- [ ] **X** ← alias “y z” — 1 card(s) <!-- key:{fake} -->\n",
                   t, count=1)
        cc.OUT.write_text(t)
        t = self._run()
        line = self._line(t, fake)
        self.assertTrue(line.startswith("- [x]") and " auto -->" in line and "resolved" in line)
        self.assertEqual(self._run().count(fake), 1)                                  # no duplicate on re-runs

    def test_resolved_c_item_reopens_when_it_comes_back(self):
        self._run()
        self.con.execute("DELETE FROM entity_aliases WHERE alias = 'humanoid robot makers'")
        t = self._run()
        key = re.search(r"key:(generic:\d+:[0-9a-f]{10}) auto", t).group(1)            # carried, ticked, auto
        db.insert_alias(self.con, self.tesla, "humanoid robot makers")                 # it comes back
        line = self._line(self._run(), key)
        self.assertTrue(line.startswith("- [ ]") and "resolved" not in line)          # re-opened

    def test_note_after_marker_keeps_the_tick_and_file_is_0644(self):
        self._run()
        self._tick(f"concept:{self.busy}")
        cc.OUT.write_text(cc.OUT.read_text().replace(f"key:concept:{self.busy} -->", f"key:concept:{self.busy} --> my note"))
        self.assertTrue(self._line(self._run(), f"concept:{self.busy}").startswith("- [x]"))   # n11
        self.assertEqual(oct(cc.OUT.stat().st_mode & 0o777), "0o644")                          # n12

    def test_section_e_is_kept_verbatim_across_rebuilds(self):
        self._run()
        cc.OUT.write_text(cc.OUT.read_text() + "\n- [ ] deploy the thing\n- [x] done thing — note\nfree text line\n")
        t = self._run()
        e = t.split("## E.", 1)[1]
        self.assertIn("- [ ] deploy the thing", e)
        self.assertIn("- [x] done thing — note", e)
        self.assertIn("free text line", e)
        self.assertEqual(self._run().split("## E.", 1)[1], e)                       # stable on a second rebuild

    def test_aliases_differing_by_case_get_their_own_keys_and_legacy_ticks_survive(self):
        self.assertNotEqual(cc._alias_key("generic", 1, "Gulf investors"), cc._alias_key("generic", 1, "gulf investors"))  # n13
        self.assertEqual(cc._legacy_key("generic", 1, "Gulf investors"), cc._legacy_key("generic", 1, "gulf investors"))
        db.insert_alias(self.con, self.tesla, "Gulf investors")                       # generic, mixed case
        self._run()
        legacy = cc._legacy_key("generic", self.tesla, "Gulf investors")                # a tick made before the re-key
        cc.OUT.write_text(cc.OUT.read_text() + f"\n- [x] old tick <!-- key:{legacy} -->\n")
        t = self._run()
        self.assertTrue(self._line(t, cc._alias_key("generic", self.tesla, "Gulf investors")).startswith("- [x]"))
        self.assertNotIn("old tick", t)                                               # re-keyed, not "resolved"

    def test_seed_missing_keeps_section_a(self):
        self._run()
        self._tick(f"concept:{self.busy}")
        os.remove(cc.SEED)
        t = self._run()
        self.assertIn("missing — section A kept", t)
        self.assertTrue(self._line(t, f"concept:{self.busy}").startswith("- [x]"))

if __name__ == "__main__":
    unittest.main()
