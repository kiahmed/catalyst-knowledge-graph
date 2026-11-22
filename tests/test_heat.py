"""src/heat.py — entity heat v2 (repeat collapse, evidence weight, no rivalry heat)."""
import unittest

from src import heat


def e(a, rel, b, day, conf=1.0, ev="direct", status="active"):
    return {"from": a, "to": b, "rel": rel, "confidence": conf, "evidence_type": ev,
            "status": status, "catalyst_date": f"2026-{day}"}


class HeatTest(unittest.TestCase):
    def test_restated_within_window_counts_once(self):
        once = heat.compute([e("NVIDIA", "invests_in", "Figure", "03-01"), e("X", "partners_with", "Figure", "03-01")])
        many = heat.compute([e("NVIDIA", "invests_in", "Figure", f"03-{d:02d}") for d in range(1, 22)]
                            + [e("X", "partners_with", "Figure", "03-01")])
        self.assertEqual(once, many)

    def test_new_event_after_repeat_window_counts_again(self):
        h = heat.compute([e("A", "invests_in", "T", "01-05"), e("A", "invests_in", "T", "06-01"),
                          e("B", "invests_in", "U", "01-05")])
        self.assertEqual(h["T"], 1.0)
        self.assertEqual(h["U"], round(0.5 ** heat.CURVE, 3))

    def test_rivalry_and_invalidated_and_self_loops_carry_no_heat(self):
        h = heat.compute([e("A", "competes_with", "B", "03-01"), e("A", "benchmarks_against", "B", "03-01"),
                          e("A", "partners_with", "B", "03-01", status="invalidated"),
                          e("A", "deploys", "A", "03-01")])
        self.assertEqual(h, {})

    def test_evidence_weight(self):
        h = heat.compute([e("A", "supplies", "T1", "03-01", ev="direct"),
                          e("B", "supplies", "T2", "03-01", ev="speculative")])
        self.assertEqual(h["T1"], 1.0)
        self.assertEqual(h["T2"], round(0.3 ** heat.CURVE, 3))

    def test_symmetric_pair_in_either_direction_is_one_relationship(self):
        h = heat.compute([e("A", "partners_with", "B", "03-01"), e("B", "partners_with", "A", "03-02"),
                          e("C", "partners_with", "D", "03-01")])
        self.assertEqual(h["A"], h["C"])

    def test_normalized_to_hottest(self):
        h = heat.compute([e("A", "supplies", "B", "03-01", conf=0.5)])
        self.assertEqual(max(h.values()), 1.0)


if __name__ == "__main__":
    unittest.main()
