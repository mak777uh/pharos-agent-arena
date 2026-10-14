"""Offline state-machine tests with an in-memory stub chain (no network).

The stub stands in for the Chain MCP so the transitions that are hard to trigger on demand on mainnet can be
tested deterministically: pending -> failed (nonce used elsewhere) -> new attempt, revert handling, no second
attempt while one is pending, and a transfer settling only one period."""
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pay_core as pc
import recurring_agent as ra

PAYER = "0x1f3546a29eec2102c8d52e21a45114124014c85c"
TO = "0x4ffd9c7e8641947662a43af298b734ca93530002"


class StubPrepared:
    def __init__(self, nonce):
        self.tx = {"nonce": nonce, "gas": 70000}
        self.signing_hash = "0x" + "ab" * 32
        self.block = 100
        self.amount_str = "20"
        self.token = pc.Token("USDC", "0xc879c018db60520f4355c26ed1a6d572cdac1815", 6)

    def to_json(self):
        return {"stub": True}


class StubChain:
    def __init__(self):
        self.nonce_latest = 5
        self.balance_ok = True
        self.transfers_list = []
        self.prepared = 0

    def token(self, _):
        return pc.Token("USDC", "0xc879c018db60520f4355c26ed1a6d572cdac1815", 6)

    def block_number(self):
        return 1000

    def block_at(self, ts, hi=None):
        return 0

    def transfers(self, tok, frm, to, a, b):
        return [t for t in self.transfers_list if a <= t["block"] <= b]

    def nonce(self, addr, block="pending"):
        return self.nonce_latest

    def prepare(self, *a, **k):
        if not self.balance_ok:
            raise pc.InsufficientBalance("insufficient USDC: payer has 1, needs 20")
        self.prepared += 1
        return StubPrepared(self.nonce_latest)

    class m:  # noqa: N801
        @staticmethod
        def call_tool(name, args):
            return {"from": PAYER, "nonce": "0x5"}


def schedule():
    start = (datetime.now(timezone.utc) - timedelta(hours=2)).replace(microsecond=0)
    return {"id": "sub", "payer": PAYER, "to": TO, "signer": PAYER, "token": "USDC", "amount": "20",
            "amount_units": "20000000", "every": "weekly", "start": start.isoformat(), "active": True, "periods": {}}


class StateMachineTests(unittest.TestCase):
    def setUp(self):
        ra.log = lambda msg: None  # keep test output quiet
        self.st = {"schedules": {"sub": schedule()}, "used_txs": []}
        self.c = StubChain()
        self.ag = ra.Agent(self.st, self.c, tempfile.mkdtemp(), 20000)
        self.now = datetime.now(timezone.utc)

    def per(self):
        return next(iter(self.st["schedules"]["sub"]["periods"].values()))

    def test_pending_is_not_prepared_twice(self):
        self.ag.run(self.now, timedelta(hours=1))
        self.ag.run(self.now, timedelta(hours=1))
        self.assertEqual(self.c.prepared, 1)
        self.assertEqual(self.per()["status"], "pending")

    def test_dropped_then_new_attempt(self):
        self.ag.run(self.now, timedelta(hours=1))
        self.c.nonce_latest = 6  # nonce 5 used by some other tx, no matching transfer
        self.ag.run(self.now, timedelta(hours=1))
        p = self.per()
        self.assertEqual([a["status"] for a in p["attempts"]], ["failed", "pending"])
        self.assertEqual(p["attempts"][1]["nonce"], 6)

    def test_insufficient_then_paid_on_chain(self):
        self.c.balance_ok = False
        self.ag.run(self.now, timedelta(hours=1))
        self.assertEqual(self.per()["status"], "insufficient_funds")
        self.c.transfers_list = [{"tx": "0x" + "11" * 32, "block": 500, "amount": 20_000_000}]
        self.ag.run(self.now, timedelta(hours=1))
        self.assertEqual(self.per()["status"], "paid")
        self.assertEqual(self.c.prepared, 0)

    def test_pending_attempt_mined(self):
        self.ag.run(self.now, timedelta(hours=1))
        self.c.transfers_list = [{"tx": "0x" + "22" * 32, "block": 150, "amount": 20_000_000}]
        self.c.nonce_latest = 6
        self.ag.run(self.now, timedelta(hours=1))
        self.assertEqual(self.per()["status"], "paid")
        self.assertEqual(self.per()["attempts"][0]["status"], "mined")

    def test_transfer_settles_one_period_only(self):
        self.c.transfers_list = [{"tx": "0x" + "33" * 32, "block": 500, "amount": 20_000_000}]
        self.st["schedules"]["sub"]["start"] = (self.now - timedelta(days=8)).isoformat()  # two periods due
        self.ag.run(self.now, timedelta(hours=1))
        statuses = sorted(p["status"] for p in self.st["schedules"]["sub"]["periods"].values())
        self.assertEqual(statuses, ["paid", "pending"])


if __name__ == "__main__":
    unittest.main()
