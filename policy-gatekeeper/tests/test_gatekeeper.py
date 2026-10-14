"""Offline tests (no network): each rule type, deny never executes, audit hash chain."""
import datetime as dt
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import gatekeeper as gk  # noqa: E402
from skill_chain import calldata  # noqa: E402

POLICY = gk.load_policy(os.path.join(os.path.dirname(__file__), "..", "assets", "policy.json"))
ALICE = "0x1f3546a29eec2102c8d52e21a45114124014c85c"
USDC = "0xc879c018db60520f4355c26ed1a6d572cdac1815"
MORPHO = "0x18573fa18fd17ddfd790b4a5b5b2977aad3b4efb"
WED_NOON = dt.datetime(2026, 10, 14, 12, 0, tzinfo=dt.timezone.utc)
WED_NIGHT = dt.datetime(2026, 10, 14, 22, 0, tzinfo=dt.timezone.utc)
SAT_NOON = dt.datetime(2026, 10, 17, 12, 0, tzinfo=dt.timezone.utc)


class FakeExecutor:
    def __init__(self):
        self.calls = 0

    def execute(self, r):
        self.calls += 1
        return {"signingHash": "0x" + "ab" * 32, "castCommandForUser": "cast send …", "unsignedTx": {"nonce": "0x1"},
                "summary": "prepared"}


class Rules(unittest.TestCase):
    def setUp(self):
        self.log = tempfile.mktemp(suffix=".jsonl")
        self.ex = FakeExecutor()

    def run_req(self, req, now=WED_NOON):
        return gk.handle(POLICY, req, None, self.ex, self.log, now)

    def test_allow_and_daily_limit(self):
        self.assertEqual(self.run_req({"type": "transfer", "token": "USDC", "to": ALICE, "amount": "500"})["decision"], "ALLOW")
        self.assertEqual(self.run_req({"type": "transfer", "token": "USDC", "to": ALICE, "amount": "500"})["decision"], "ALLOW")
        e = self.run_req({"type": "transfer", "token": "USDC", "to": ALICE, "amount": "1"})
        self.assertEqual(e["decided_by"], "spend_limits.USDC.per_day")
        # 24 h later the window has rolled over
        later = WED_NOON + dt.timedelta(hours=24, seconds=1)
        self.assertEqual(self.run_req({"type": "transfer", "token": "USDC", "to": ALICE, "amount": "1"}, later)["decision"], "ALLOW")

    def test_per_tx(self):
        e = self.run_req({"type": "transfer", "token": "USDC", "to": ALICE, "amount": "500.000001"})
        self.assertEqual(e["decided_by"], "spend_limits.USDC.per_tx")

    def test_allowlist(self):
        e = self.run_req({"type": "transfer", "token": "USDC", "to": "0x" + "11" * 20, "amount": "1"})
        self.assertEqual(e["decided_by"], "allowlist.recipients")
        e = self.run_req({"type": "contract_call", "to": USDC,
                          "data": calldata("transferFrom(address,address,uint256)", ["address", "address", "uint256"], [ALICE, ALICE, 1])})
        self.assertEqual(e["decided_by"], "allowlist.contracts")

    def test_time_window(self):
        approve = {"type": "contract_call", "to": USDC,
                   "data": calldata("approve(address,uint256)", ["address", "uint256"], [MORPHO, 100_000_000])}
        self.assertEqual(self.run_req(approve, WED_NOON)["decision"], "ALLOW")
        self.assertEqual(self.run_req(approve, WED_NIGHT)["decided_by"], "time_windows.contract_calls_business_hours")
        self.assertEqual(self.run_req(approve, SAT_NOON)["decided_by"], "time_windows.contract_calls_business_hours")

    def test_approval_limits(self):
        big = {"type": "contract_call", "to": USDC,
               "data": calldata("approve(address,uint256)", ["address", "uint256"], [MORPHO, 2 ** 256 - 1])}
        self.assertEqual(self.run_req(big)["decided_by"], "spend_limits.USDC.max_approval")
        over = {"type": "contract_call", "to": USDC,
                "data": calldata("approve(address,uint256)", ["address", "uint256"], [MORPHO, 201_000_000])}
        self.assertEqual(self.run_req(over)["decided_by"], "spend_limits.USDC.max_approval")

    def test_validation(self):
        for bad in [{"type": "transfer", "token": "USDC", "to": "0x123", "amount": "1"},
                    {"type": "transfer", "token": "DOGE", "to": ALICE, "amount": "1"},
                    {"type": "transfer", "token": "USDC", "to": ALICE, "amount": "-5"},
                    {"type": "selfdestruct"}]:
            self.assertEqual(self.run_req(bad)["decided_by"], "request.validation", bad)

    def test_deny_never_executes_and_audit_chain(self):
        reqs = [{"type": "transfer", "token": "USDC", "to": ALICE, "amount": "10"},
                {"type": "transfer", "token": "USDC", "to": ALICE, "amount": "900"},
                {"type": "transfer", "token": "WPROS", "to": ALICE, "amount": "1"}]
        res = [self.run_req(r) for r in reqs]
        self.assertEqual([r["decision"] for r in res], ["ALLOW", "DENY", "DENY"])
        self.assertEqual(self.ex.calls, 1)
        self.assertFalse(any(r["executed"] for r in res if r["decision"] == "DENY"))
        self.assertTrue(gk.verify_audit(self.log)[0])
        lines = open(self.log).read().splitlines()
        e = json.loads(lines[1]); e["decision"] = "ALLOW"; lines[1] = json.dumps(e)
        open(self.log, "w").write("\n".join(lines) + "\n")
        self.assertFalse(gk.verify_audit(self.log)[0])


if __name__ == "__main__":
    unittest.main()
