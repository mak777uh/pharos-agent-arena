"""Offline tests (no network): V3 math against closed-form results, tick traversal on a synthetic pool,
route cost decomposition, atomic path encoding."""
import os
import sys
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import v3math as v  # noqa: E402
import arb  # noqa: E402
from eth_abi import encode  # noqa: E402

USDC = "0xc879c018db60520f4355c26ed1a6d572cdac1815"
WPROS = "0x52c48d4213107b20bc583832b0d951fb9ca8f0b0"


class FakePoolSrc:
    """A pool with liquidity L in [lo, hi) and 2L above hi (one initialized tick crossed going up)."""

    def __init__(self, tick, liq, spacing, fee, lo, hi):
        self.tick, self.liq, self.spacing, self.fee, self.lo, self.hi = tick, liq, spacing, fee, lo, hi
        self.calls = 0

    def call(self, to, data, block):
        sel, arg = data[2:10], data[10:]
        w = lambda *vals: b"".join(x.to_bytes(32, "big", signed=True) if isinstance(x, int) and x < 0 else int(x).to_bytes(32, "big") for x in vals)
        if sel == v.S_SLOT0:
            return w(v.sqrt_at_tick(self.tick), self.tick, 0, 0, 0, 0, 1)
        if sel == v.S_LIQ:
            return w(self.liq)
        if sel == v.S_FEE:
            return w(self.fee)
        if sel == v.S_SPACING:
            return w(self.spacing)
        if sel == v.S_T0:
            return b"\0" * 12 + bytes.fromhex(WPROS[2:])
        if sel == v.S_T1:
            return b"\0" * 12 + bytes.fromhex(USDC[2:])
        if sel == v.S_BITMAP:
            pos = int.from_bytes(bytes.fromhex(arg), "big", signed=True)
            word = 0
            for t in (self.lo, self.hi):
                c = t // self.spacing
                if c >> 8 == pos:
                    word |= 1 << (c % 256)
            return word.to_bytes(32, "big")
        if sel == v.S_TICKS:
            t = int.from_bytes(bytes.fromhex(arg), "big", signed=True)
            net = self.liq if t == self.lo else (self.liq if t == self.hi else 0)
            return encode(["uint128", "int128"], [self.liq, net])
        raise AssertionError(sel)


class Math(unittest.TestCase):
    def test_in_range_matches_closed_form(self):
        src = FakePoolSrc(tick=0, liq=10 ** 18, spacing=10, fee=3000, lo=-1000, hi=1000)
        p = v.Pool(src, "0x" + "11" * 20, "latest")
        amt = 10 ** 15
        q = p.quote_exact_in(amt, zero_for_one=False)          # token1 in, in range
        less = amt * (10 ** 6 - 3000) // 10 ** 6
        sp_new = p.sqrt + (less << 96) // p.liq
        expected = v.amount0_delta(p.sqrt, sp_new, p.liq, False)
        self.assertEqual(q["amountOut"], expected)
        self.assertEqual(q["ticksCrossed"], 0)
        self.assertAlmostEqual(q["fee"], amt - less, delta=1)

    def test_crosses_tick_and_liquidity_doubles(self):
        src = FakePoolSrc(tick=990, liq=10 ** 18, spacing=10, fee=500, lo=-1000, hi=1000)
        p = v.Pool(src, "0x" + "22" * 20, "latest")
        q_small = p.quote_exact_in(10 ** 12, zero_for_one=False)
        q_big = p.quote_exact_in(10 ** 16, zero_for_one=False)
        self.assertEqual(q_small["ticksCrossed"], 0)
        self.assertEqual(q_big["ticksCrossed"], 1)               # passed tick 1000 where liquidity is added

    def test_next_tick_down(self):
        src = FakePoolSrc(tick=5, liq=1, spacing=10, fee=500, lo=-20, hi=1000)
        p = v.Pool(src, "0x" + "33" * 20, "latest")
        self.assertEqual(p.next_tick(5, True), (0, False))      # word boundary for lte when nothing initialized in word at/below
        t, init = p.next_tick(-5, True)
        self.assertTrue(init and t == -20)


class Route(unittest.TestCase):
    def test_path_encoding(self):
        cfg = {"base_token": USDC, "quote_token": WPROS, "thresholds": {"min_net_profit_usd": 1, "min_net_profit_bps": 5}}
        sc = arb.Scanner.__new__(arb.Scanner)
        sc.usdc, sc.wpros = USDC, WPROS
        path = sc.path(100, 3000)
        self.assertEqual(len(path), 20 + 3 + 20 + 3 + 20)
        self.assertEqual(path[20:23], (100).to_bytes(3, "big"))
        self.assertEqual(path[43:46], (3000).to_bytes(3, "big"))
        self.assertEqual(path[:20], path[-20:])

    def test_cost_decomposition_adds_up(self):
        a = FakePoolSrc(tick=-1000, liq=10 ** 22, spacing=10, fee=100, lo=-5000, hi=5000)
        b = FakePoolSrc(tick=-900, liq=10 ** 22, spacing=10, fee=3000, lo=-5000, hi=5000)
        A = {"pool": v.Pool(a, "0x" + "44" * 20, "latest")}
        B = {"pool": v.Pool(b, "0x" + "55" * 20, "latest")}
        A["mid"], B["mid"] = A["pool"].price1per0(18, 6), B["pool"].price1per0(18, 6)
        sc = arb.Scanner.__new__(arb.Scanner)
        r = sc.route(A, B, 100 * 10 ** 6, Decimal("0.58"), 10 * 10 ** 9, 200_000)
        lhs = r["grossUSD"] - r["feesUSD"] - r["slippageUSD"] - r["gasUSD"]
        self.assertAlmostEqual(float(lhs), float(r["netUSD"]), places=9)
        self.assertGreater(r["grossUSD"], 0)


if __name__ == "__main__":
    unittest.main()
