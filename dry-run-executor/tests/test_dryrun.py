"""Offline tests (no network): revert decoding, trace -> balance deltas, input validation."""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import dryrun  # noqa: E402
import skill_chain as sc  # noqa: E402

A, B = "0x" + "aa" * 20, "0x" + "bb" * 20
USDC = "0xc879c018db60520f4355c26ed1a6d572cdac1815"


def w(a):
    return "0x" + a[2:].rjust(64, "0")


class Revert(unittest.TestCase):
    def test_reasons(self):
        err = ("0x08c379a0" + "20".rjust(64, "0") + "26".rjust(64, "0")
               + b"ERC20: transfer amount exceeds balance".hex().ljust(128, "0"))
        self.assertEqual(sc.decode_revert(err), "ERC20: transfer amount exceeds balance")
        self.assertEqual(sc.decode_revert("0x4e487b71" + "11".rjust(64, "0")), "Panic(0x11): arithmetic overflow/underflow")
        self.assertIsNone(sc.decode_revert("0x"))
        self.assertEqual(sc.clean_message("execution reverted: "), "reverted without a reason (empty revert data)")


class Predict(unittest.TestCase):
    def test_trace_to_deltas(self):
        trace = {"from": A, "to": USDC, "value": "0x0", "type": "CALL", "gasUsed": "0x5208",
                 "logs": [{"address": USDC, "topics": [dryrun.T_TRANSFER, w(A), w(B)], "data": "0x" + format(5_000_000, "064x")}],
                 "calls": [{"from": USDC, "to": B, "value": hex(10 ** 18), "type": "CALL"},
                           {"from": USDC, "to": A, "value": hex(10 ** 18), "type": "CALL", "error": "reverted"}]}
        with mock.patch.object(dryrun, "trace_call", return_value=trace):
            p = dryrun.predict({"from": A, "to": USDC}, None)
        self.assertEqual(p["deltas"][(USDC, A)], -5_000_000)
        self.assertEqual(p["deltas"][(USDC, B)], 5_000_000)
        self.assertEqual(p["deltas"][("PROS", B)], 10 ** 18)          # value moved by a successful sub-call
        self.assertNotIn(("PROS", A), p["deltas"])                     # failed sub-call moves nothing

    def test_no_trace(self):
        with mock.patch.object(dryrun, "trace_call", return_value=None):
            self.assertFalse(dryrun.predict({}, None)["available"])


class Build(unittest.TestCase):
    def args(self, **kw):
        base = dict(cmd="raw", sender=A, to=B, data="0x", value_wei=0)
        base.update(kw)
        return SimpleNamespace(**base)

    def test_validation(self):
        for bad in [self.args(sender="0x12"), self.args(data="0xzz"), self.args(value_wei=-1), self.args(data="0xabc")]:
            with self.assertRaises(Exception):
                dryrun.build(bad, None)
        tx, _ = dryrun.build(self.args(data="0xABCD"), None)
        self.assertEqual(tx["data"], "0xabcd")


if __name__ == "__main__":
    unittest.main()
