"""Offline tests: config validation, overrides and the decision rule (no network)."""
import json
import os
import sys
import tempfile
import unittest
from decimal import Decimal
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import guardian  # noqa: E402

CFG = os.path.join(os.path.dirname(__file__), "..", "assets", "guardian.rebalance.json")


class FakeChain:
    def block_number(self):
        return 100

    def block(self, n):
        return {"timestamp": hex(1_800_000_000)}


def fake_readers(values):
    def make(v):
        return lambda r, p, b, ts: (Decimal(v), "u", "d", "s")
    return {k: make(v) for k, v in values.items()}


class TestConfig(unittest.TestCase):
    def test_loads_and_overrides(self):
        cfg = guardian.load_config(CFG, ["price_move.threshold=5", "gas.op=lt", "price_move.lookback_blocks=100"])
        c = {x["id"]: x for x in cfg["conditions"]}
        self.assertEqual(c["price_move"]["threshold"], "5")
        self.assertEqual(c["gas"]["op"], "lt")
        self.assertEqual(c["price_move"]["params"]["lookback_blocks"], "100")

    def test_bad_inputs(self):
        for ov in ["nosuch.threshold=1", "gas.threshold=abc", "gas.op=~", "gas"]:
            with self.assertRaises(Exception, msg=ov):
                guardian.load_config(CFG, [ov])
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"conditions": [{"id": "a", "type": "bogus", "op": "lt", "threshold": 1}]}, f)
        with self.assertRaises(ValueError):
            guardian.load_config(f.name, [])


class TestDecision(unittest.TestCase):
    def run_with(self, values, mode="all"):
        cfg = {"mode": mode, "conditions": [
            {"id": k, "type": k, "op": "lt", "threshold": "10"} for k in values]}
        with mock.patch.dict(guardian.READERS, fake_readers(values), clear=True):
            return guardian.evaluate(cfg, FakeChain())

    def test_all_pass_fires(self):
        self.assertEqual(self.run_with({"a": 1, "b": 2, "c": 3})["decision"], "FIRE")

    def test_one_fail_holds(self):
        r = self.run_with({"a": 1, "b": 20, "c": 3})
        self.assertEqual(r["decision"], "HOLD")
        self.assertEqual([x["passed"] for x in r["conditions"]], [True, False, True])

    def test_k_of_n(self):
        self.assertEqual(self.run_with({"a": 1, "b": 20, "c": 3}, mode=2)["decision"], "FIRE")
        self.assertEqual(self.run_with({"a": 11, "b": 20, "c": 3}, mode=2)["decision"], "HOLD")

    def test_read_error_never_passes(self):
        def boom(*a):
            raise RuntimeError("rpc down")
        cfg = {"mode": "any", "conditions": [{"id": "x", "type": "x", "op": "lt", "threshold": "10"}]}
        with mock.patch.dict(guardian.READERS, {"x": boom}, clear=True):
            r = guardian.evaluate(cfg, FakeChain())
        self.assertEqual(r["decision"], "HOLD")
        self.assertIn("READ ERROR", r["conditions"][0]["detail"])


if __name__ == "__main__":
    unittest.main()
