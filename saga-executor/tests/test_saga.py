"""Offline tests (no network, no anvil): inject parsing, log matching, state log, fee-bump policy, status."""
import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import saga  # noqa: E402

A = "0x84dbd23178ba7fc6a2a396559139aef69d0f072a"
ROUTER = "0xf38d34c8382b9079b0f85309578b43b8479cd875"
USDC = "0xc879c018db60520f4355c26ed1a6d572cdac1815"


class Inject(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(saga.parse_inject("drop@swap"), {"kind": "drop", "step": "swap"})
        self.assertIsNone(saga.parse_inject(None))
        for bad in ("boom@swap", "drop@nowhere", "frontrun@deposit"):
            with self.assertRaises(SystemExit):
                saga.parse_inject(bad)


class Logs(unittest.TestCase):
    def test_find_approval(self):
        r = {"logs": [{"address": USDC, "topics": [saga.T_APPROVAL, saga.w(A), saga.w(ROUTER)], "data": "0x" + format(25_000_000, "064x")}]}
        self.assertEqual(saga.Saga.find(r, USDC, saga.T_APPROVAL, [A, ROUTER]), 25_000_000)
        self.assertIsNone(saga.Saga.find(r, USDC, saga.T_APPROVAL, [ROUTER, A]))
        self.assertIsNone(saga.Saga.find(r, USDC, saga.T_TRANSFER, [A, None]))


class FakeFork:
    def __init__(self, base):
        self.b = base

    def base_fee(self):
        return self.b


class Fees(unittest.TestCase):
    def mk(self, base):
        s = saga.Saga.__new__(saga.Saga)
        s.fork, s.tip, s.retry = FakeFork(base), 0, {"fee_bump_pct": 25}
        return s

    def test_fresh_and_bump(self):
        s = self.mk(10 * 10**9)
        self.assertEqual(s.fees(None, None), 20 * 10**9)                     # 2*base + tip
        self.assertEqual(s.fees(20 * 10**9, None), 25 * 10**9)               # +25 % replacement bump
        self.assertEqual(s.fees(20 * 10**9, 40 * 10**9), 80 * 10**9)         # must clear the base fee that evicted it


class StateLogStatus(unittest.TestCase):
    def test_log_and_status(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "runs", "x.jsonl")
            log = saga.StateLog(p, "x", quiet=True)
            log("forward", "approve_usdc", "1/4", "STARTED")
            log("forward", "approve_usdc", "1/4", "SENT", tx="0xabc", attempt=1)
            recs = [json.loads(x) for x in open(p)]
            self.assertEqual([r["seq"] for r in recs], [1, 2])
            self.assertEqual(recs[-1]["state"], "SENT")
            self.assertEqual(saga.cmd_status(SimpleNamespace(log=p, history=False)), 0)


if __name__ == "__main__":
    unittest.main()
