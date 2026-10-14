"""Offline tests (no network): parser + unsigned EIP-1559 serialization.

The serialization vector below was cross-checked with eth-account (TypedTransaction.from_dict(...).hash())
when this was written; eth-account is NOT a runtime dependency."""
import unittest
from decimal import Decimal

import pay_core as pc

VECTOR = {
    "tx": {"chainId": 1672, "nonce": 2164, "maxPriorityFeePerGas": 0, "maxFeePerGas": 20_000_000_000, "gas": 70344,
           "to": "0xc879c018db60520f4355c26ed1a6d572cdac1815", "value": 0,
           "data": "0xa9059cbb0000000000000000000000001f3546a29eec2102c8d52e21a45114124014c85c"
                   "0000000000000000000000000000000000000000000000000000000005f5e100"},
    "raw": "0x02f86e820688820874808504a817c800830112c894c879c018db60520f4355c26ed1a6d572cdac181580b844a9059cbb"
           "0000000000000000000000001f3546a29eec2102c8d52e21a45114124014c85c"
           "0000000000000000000000000000000000000000000000000000000005f5e100c0",
    "hash": "0x9f3be79dc53cc7bec6681b8517477bf1349bd18263fb7be34c15da008c28e711",
}


class CoreTests(unittest.TestCase):
    def test_unsigned_vector(self):
        raw, h = pc.unsigned_eip1559(VECTOR["tx"])
        self.assertEqual(raw, VECTOR["raw"])
        self.assertEqual(h, VECTOR["hash"])

    def test_checksum(self):
        self.assertEqual(pc.to_checksum("0x1f3546a29eec2102c8d52e21a45114124014c85c"),
                         "0x1F3546a29EeC2102c8d52E21a45114124014c85C")
        with self.assertRaises(pc.PaymentError):
            pc.check_address("0x1F3546a29EeC2102c8d52E21a45114124014c85c")  # one wrong-case letter

    def test_units(self):
        self.assertEqual(pc.to_units(Decimal("1.5"), 6), 1_500_000)
        with self.assertRaises(pc.PaymentError):
            pc.to_units(Decimal("0.0000001"), 6)

    def test_memo_roundtrip(self):
        data = "0x" + pc.SEL["transfer"] + "00" * 64 + pc.memo_bytes("INV-2026-0042").hex()
        self.assertEqual(pc.decode_memo(data), "INV-2026-0042")
