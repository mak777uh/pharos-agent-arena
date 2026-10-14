"""Offline tests (no network): the trend rule, persistence/dedup and exactly-once triggers."""
import json
import os
import sys
import tempfile
import unittest
from decimal import Decimal as D

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import trend  # noqa: E402

DOWN3 = {"id": "d3", "type": "consecutive", "direction": "down", "moves": 3, "min_step_pct": "0.02",
         "min_total_pct": "0.15", "max_step_pct": "2", "max_gap_blocks": 1200}


def hist(values, step=600, start=1000):
    return [{"block": start + i * step, "ts": 1_800_000_000 + i * 600, "value": D(str(v)), "mode": "test", "src": "t"}
            for i, v in enumerate(values)]


class Rule(unittest.TestCase):
    def test_three_declines_trigger_once(self):
        rows, trig = trend.evaluate(hist([1.00, 0.99, 0.98, 0.97, 0.96, 0.95]), DOWN3)
        self.assertEqual(len(trig), 1)                       # streak 4, 5 do not re-trigger
        self.assertEqual(trig[0]["block"], 1000 + 3 * 600)   # the 4th observation completes it
        self.assertEqual(trig[0]["observations"], 4)

    def test_single_spike_does_not_trigger(self):
        _, trig = trend.evaluate(hist([1.00, 1.00, 0.90, 1.00, 1.00]), DOWN3)   # -10% once
        self.assertEqual(trig, [])
        _, trig = trend.evaluate(hist([1.00, 0.995, 1.00, 0.995, 1.00]), DOWN3)  # noise
        self.assertEqual(trig, [])

    def test_outlier_and_gap_reset(self):
        _, trig = trend.evaluate(hist([1.00, 0.99, 0.95, 0.94, 0.93]), DOWN3)   # -4% step is an outlier
        self.assertEqual(trig, [])
        h = hist([1.00, 0.99, 0.98, 0.97])
        h[3]["block"] += 5000                                                      # stale gap before the 3rd move
        _, trig = trend.evaluate(h, DOWN3)
        self.assertEqual(trig, [])

    def test_min_total(self):
        r = dict(DOWN3, min_total_pct="5")
        rows, trig = trend.evaluate(hist([1.00, 0.99, 0.98, 0.97]), r)
        self.assertEqual(trig, [])
        self.assertIn("min_total_pct", rows[-1]["note"])

    def test_sustained(self):
        r = {"id": "s", "type": "sustained", "op": "gt", "threshold": "90", "observations": 3}
        _, trig = trend.evaluate(hist([91, 95, 89, 91, 92, 93, 94]), r)
        self.assertEqual([t["block"] for t in trig], [1000 + 5 * 600])
        _, trig = trend.evaluate(hist([80, 99, 80, 80]), r)                        # one spike above 90
        self.assertEqual(trig, [])

    def test_config_needs_three_observations(self):
        for bad in [{"type": "consecutive", "direction": "down", "moves": 1},
                    {"type": "sustained", "op": "gt", "threshold": 1, "observations": 2},
                    {"type": "consecutive", "direction": "sideways", "moves": 3}]:
            f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
            json.dump({"series": "x", "source": {"type": "pool_price"}, "rule": bad}, f)
            f.close()
            with self.assertRaises(ValueError):
                trend.load_config(f.name)


class Store(unittest.TestCase):
    def test_dedup_and_exactly_once(self):
        path = tempfile.mktemp(suffix=".db")
        s = trend.Store(path)
        for h in hist([1.00, 0.99, 0.98, 0.97]):
            self.assertTrue(s.add("x", h["block"], h["ts"], h["value"], "t", "test"))
        self.assertFalse(s.add("x", 1000, 1, D(5), "t", "test"))      # same block again: ignored
        s2 = trend.Store(path)                                            # "restart"
        self.assertEqual(len(s2.history("x")), 4)
        self.assertEqual(s2.history("x")[0]["value"], D("1.0"))
        _, trig = trend.evaluate(s2.history("x"), DOWN3)
        self.assertTrue(s2.record_trigger("x", "d3", trig[0], True))
        self.assertFalse(trend.Store(path).record_trigger("x", "d3", trig[0], True))  # never twice


if __name__ == "__main__":
    unittest.main()
