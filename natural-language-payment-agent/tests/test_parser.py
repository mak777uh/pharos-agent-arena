import unittest
from decimal import Decimal

import nl_pay

BOOK = {"alice": "0x1f3546a29eec2102c8d52e21a45114124014c85c", "bob": "0x4ffd9c7e8641947662a43af298b734ca93530002"}


class ParserTests(unittest.TestCase):
    def p(self, s):
        return nl_pay.parse(s, BOOK)

    def test_full(self):
        it = self.p("send 100 USDC to alice")
        self.assertEqual((it["amount"], it["token"], it["recipient"]), (Decimal(100), "USDC", BOOK["alice"]))
        self.assertEqual(it["questions"], [])

    def test_variants(self):
        self.assertEqual(self.p("pay bob 2.5k usdc")["amount"], Decimal(2500))
        self.assertEqual(self.p("transfer 0.5 pros to bob")["token"], "PROS")
        self.assertEqual(self.p("send fifty usdc to alice")["amount"], Decimal(50))

    def test_ambiguous(self):
        slots = lambda s: [q[0] for q in self.p(s)["questions"]]
        self.assertEqual(slots("pay bob 25"), ["token"])
        self.assertEqual(slots("send $50 to alice"), ["token"])
        self.assertEqual(slots("send 20 or 30 usdc to alice"), ["amount"])
        self.assertEqual(slots("send 10 usdc to alice.eth"), ["recipient"])
        self.assertEqual(slots("send 10 usdc to alic"), ["recipient"])
        self.assertEqual(slots("send 10 usdc and 3 link to alice"), ["amount", "token"])
        self.assertIn("checksum", self.p("send 1 usdc to 0x1F3546a29EeC2102c8d52E21a45114124014c85c")["questions"][0][1])


if __name__ == "__main__":
    unittest.main()
