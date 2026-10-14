"""Offline tests (no network): request parsing and the in-range V3 quote against a REAL swap.

Vector: pool 0x912c9ade… state at block 20,255,987 (sqrtPriceX96, liquidity) and the swap in block 20,255,988,
tx 0x89e61062b36f2152cf5f9a485a1d9d6431faa57443390f40d0655465a57d022c: 531594390949576704000 wei WPROS in -> 310325563 (6 dp) USDC out (amounts from its Swap log)."""
import os
import sys
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import flow  # noqa: E402

STATE = {"liquidity": 801386629611202861, "sqrtPriceX96": 60552200577036182098370, "fee": 100,
         "token0": "WPROS", "token1": "USDC", "dec0": 18, "dec1": 6}


class Parse(unittest.TestCase):
    def test_variants(self):
        p = flow.parse_request("Is now a good time to swap 500 USDC to WPROS?")
        self.assertEqual((p["amount"], p["tokenIn"], p["tokenOut"]), (Decimal(500), "USDC", "WPROS"))
        p = flow.parse_request("swap 100k USDC for WPROS")
        self.assertEqual(p["amount"], Decimal(100000))
        p = flow.parse_request("Should I buy WPROS with $250 now?")
        self.assertEqual((p["amount"], p["tokenIn"]), (Decimal(250), "USDC"))
        p = flow.parse_request("sell 1,234.5 pros")
        self.assertEqual((p["amount"], p["tokenIn"], p["tokenOut"]), (Decimal("1234.5"), "WPROS", "USDC"))

    def test_rejects(self):
        for t in ["hello", "", "swap 5 USDC to USDC", "x" * 400]:
            with self.assertRaises(ValueError, msg=t):
                flow.parse_request(t)


class Quote(unittest.TestCase):
    def test_matches_real_swap(self):
        q = flow.quote(STATE, {"amount": Decimal(531594390949576704000) / Decimal(10) ** 18, "tokenIn": "WPROS", "tokenOut": "USDC"})
        self.assertAlmostEqual(float(q["amountOut"]), 310.325563, places=5)
        self.assertLess(q["impactPct"], Decimal("0.1"))

    def test_direction(self):
        q = flow.quote(STATE, {"amount": Decimal(100), "tokenIn": "USDC", "tokenOut": "WPROS"})
        self.assertGreater(q["priceAfter"], Decimal("0.58"))
        self.assertGreater(q["tickAfter"], -281800)


if __name__ == "__main__":
    unittest.main()
