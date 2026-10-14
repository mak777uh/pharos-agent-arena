"""Offline tests for invoice extraction and checks (no network)."""
import os
import unittest
from datetime import date

import invoice_agent as ia

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "samples", "invoices")
ME = "0x84dbd23178ba7fc6a2a396559139aef69d0f072a"


def load(name):
    return ia.load_invoice(os.path.join(HERE, name), None)


class InvoiceTests(unittest.TestCase):
    def test_text(self):
        inv = load("inv_text_lumen.txt")
        self.assertEqual((inv["reference"], str(inv["amount"]), inv["currency"]), ("INV-2026-0042", "250.00", "USDC"))
        self.assertEqual(len(inv["line_items"]), 2)
        p, _ = ia.check(inv, ME, {}, set(), date(2026, 10, 14), False)
        self.assertEqual(p, [])

    def test_json(self):
        inv = load("inv_json_northwind.json")
        self.assertEqual((inv["reference"], str(inv["amount"])), ("NW-7781", "129.50"))
        self.assertEqual(ia.check(inv, ME, {}, set(), date(2026, 10, 14), False)[0], [])

    def test_pdf(self):
        inv = load("inv_pdf_orbit.pdf")
        self.assertEqual((inv["reference"], str(inv["amount"]), inv["payee_name"]), ("ORB-1009", "90.00", "Orbit Translations"))

    def test_flags(self):
        p, _ = ia.check(load("inv_text_mismatch.txt"), ME, {}, set(), date(2026, 10, 14), False)
        self.assertTrue(any(x.startswith("MISMATCH total") for x in p))
        self.assertTrue(any(x.startswith("MISMATCH network") for x in p))
        p, _ = ia.check(load("inv_text_missing.txt"), ME, {}, set(), date(2026, 10, 14), False)
        self.assertIn("MISSING payee wallet address", p)
        self.assertTrue(any("EUR" in x for x in p))

    def test_duplicate_and_payer(self):
        inv = load("inv_text_lumen.txt")
        p, _ = ia.check(inv, "0x4ffd9c7e8641947662a43af298b734ca93530002", {}, {"INV-2026-0042"}, date(2026, 10, 14), False)
        self.assertTrue(any(x.startswith("DUPLICATE") for x in p))
        self.assertTrue(any(x.startswith("MISMATCH payer") for x in p))

    def test_two_payto_wallets(self):
        inv = ia.parse_text("Invoice No: A-1\nTotal: 5 USDC\nPay to: 0x1F3546a29EeC2102c8d52E21a45114124014c85C\n"
                            "Wallet: 0x4ffd9c7e8641947662a43af298b734ca93530002\n")
        p, _ = ia.check(inv, ME, {}, set(), date(2026, 10, 14), False)
        self.assertTrue(any(x.startswith("AMBIGUOUS payee") for x in p))


if __name__ == "__main__":
    unittest.main()
