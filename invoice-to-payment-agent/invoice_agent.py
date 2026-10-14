#!/usr/bin/env python3
"""Invoice-to-Payment Agent for Pharos mainnet (prepare-only).

Reads an invoice or payment request — plain text, PDF, structured JSON, or an EIP-681 payment URI — extracts
payee, wallet, amount, token/currency, reference, due date and network, cross-checks it (line items vs total,
supported token, chain, address checksum, known-vendor wallet, duplicates, payer), and only when nothing is
missing or mismatched prepares the matching UNSIGNED EIP-1559 transaction with the invoice reference embedded in
the calldata. Never signs, never sends.

Usage:
  python invoice_agent.py --from 0xYourWallet samples/invoices/inv_text_lumen.txt
  python invoice_agent.py --from 0xYourWallet --vendors vendors.json --ledger ledger.jsonl --record inv.pdf
  python invoice_agent.py --from 0xYourWallet "ethereum:0xC879…@1672/transfer?address=0x…&uint256=1000000"
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qs, urlparse

import pay_core as pc

__version__ = "1.0.0"

SUPPORTED = {"USDC", "PROS", "WPROS", "LINK", "WETH"}
FIAT = {"USD", "EUR", "GBP", "RUB", "JPY", "CNY", "$", "€", "£"}
PHAROS_NETWORK_WORDS = ("pharos", "1672", "pacific")
MAX_INPUT = 200_000  # bytes


# ------------------------------------------------------------------ extraction
def num(s: str) -> Decimal | None:
    s = s.strip().replace(",", "").replace(" ", "")
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def blank() -> dict:
    return {"format": None, "reference": None, "payee_name": None, "payee_wallet": None, "amount": None,
            "currency": None, "due_date": None, "network": None, "payer_wallet": None, "line_items": [],
            "subtotal": None, "tax": None, "discount": None, "raw_wallets": []}


AMT = r"([0-9][0-9,]*(?:\.[0-9]+)?)"
CUR = r"(USDC|USDT|PROS|WPROS|LINK|WETH|ETH|USD|EUR|GBP|RUB|\$|€|£)"


def parse_text(text: str, fmt: str = "text") -> dict:
    inv = blank()
    inv["format"] = fmt
    lines = [l.rstrip() for l in text.splitlines()]
    joined = "\n".join(lines)

    def field(pattern: str):
        m = re.search(pattern, joined, re.I | re.M)
        return m.group(m.lastindex).strip() if m else None

    inv["reference"] = field(r"^\s*(invoice\s*(?:no\.?|number|#|id)|reference|ref\.?|payment\s+reference|po\s*(?:no\.?|number|#))\s*[:#]?\s*([A-Za-z0-9][\w\-/.]*)")
    inv["payee_name"] = field(r"^\s*(from|payee|vendor|bill\s+from|seller|supplier)\s*:\s*(.+)$")
    inv["due_date"] = field(r"^\s*(due\s*date|payment\s+due|due)\s*:\s*(.+)$")
    inv["network"] = field(r"^\s*(network|chain|blockchain)\s*:\s*(.+)$")
    cur_line = field(r"^\s*(currency|token|pay\s+in)\s*:\s*([A-Za-z$€£]+)")
    # wallets: label "pay to"/"wallet"/"address" near 0x..., and "bill to wallet" / "payer wallet"
    for m in re.finditer(r"^(.*?)(0x[0-9a-fA-F]{40})\b", joined, re.M):
        label = m.group(1).lower()
        inv["raw_wallets"].append(m.group(2))
        if re.search(r"(bill\s*to|payer|customer|from\s+wallet)", label):
            inv["payer_wallet"] = m.group(2)
        elif re.search(r"(pay\s*to|wallet|address|remit|beneficiary|payee)", label) and not inv["payee_wallet"]:
            inv["payee_wallet"] = m.group(2)
        elif re.search(r"(pay\s*to|wallet|address|remit|beneficiary|payee)", label):
            inv.setdefault("conflicting_wallets", []).append(m.group(2))
    # broken / short addresses that look like one (to flag instead of ignore)
    bad = [w for w in re.findall(r"\b0x[0-9a-zA-Z]{20,}\b", joined) if not re.fullmatch(r"0x[0-9a-fA-F]{40}", w)]
    if bad:
        inv["malformed_wallets"] = bad
    # totals
    for key, pat in (("amount", r"^\s*(total\s+due|amount\s+due|balance\s+due|total|amount)\b[^0-9\n$€£]*?" + rf"{CUR}?\s*{AMT}\s*{CUR}?"),
                     ("subtotal", rf"^\s*(sub\s*-?total)\b[^0-9\n$€£]*?{CUR}?\s*{AMT}\s*{CUR}?"),
                     ("tax", rf"^\s*(tax|vat|gst)\b(?:\s*\([^)]*\))?[^0-9\n$€£(]*?{CUR}?\s*{AMT}\s*{CUR}?"),
                     ("discount", rf"^\s*(discount)\b(?:\s*\([^)]*\))?[^0-9\n$€£(]*?{CUR}?\s*-?\s*{AMT}\s*{CUR}?")):
        found = [m for m in re.finditer(pat, joined, re.I | re.M)
                 if not (key == "amount" and re.match(r"\s*sub", m.group(0), re.I))]
        if found:
            m = found[-1] if key == "amount" else found[0]
            inv[key] = num(m.group(3))
            c = (m.group(2) or m.group(4) or "").upper()
            if key == "amount" and c:
                inv["currency"] = c
    if cur_line:
        c = cur_line.upper()
        if inv["currency"] and inv["currency"] != c:
            inv["currency_conflict"] = [inv["currency"], c]
        inv["currency"] = inv["currency"] or c
    # line items: lines in the block after a header containing "description"/"item" and before subtotal/total
    in_items = False
    for l in lines:
        if re.search(r"\b(description|item)s?\b", l, re.I) and not re.search(AMT + r"\s*$", l):
            in_items = True
            continue
        if in_items and re.match(r"^\s*(sub\s*-?total|total|tax|vat|discount|amount\s+due)\b", l, re.I):
            break
        if in_items:
            m = re.match(rf"^\s*(.+?)\s{{2,}}(?:(\d+(?:\.\d+)?)\s*x\s*{AMT}\s+)?{CUR}?\s*{AMT}\s*{CUR}?\s*$", l)
            if m:
                inv["line_items"].append({"description": m.group(1).strip(), "quantity": num(m.group(2)) if m.group(2) else None,
                                          "unit_price": num(m.group(3)) if m.group(3) else None, "amount": num(m.group(5))})
    return inv


def parse_json(obj: dict) -> dict:
    inv = blank()
    inv["format"] = "json"
    g = lambda *ks: next((obj[k] for k in ks if k in obj and obj[k] not in (None, "")), None)  # noqa: E731
    inv["reference"] = g("reference", "invoice_number", "invoiceNumber", "invoice_id", "id")
    payee = obj.get("payee") or obj.get("seller") or obj.get("vendor") or {}
    if isinstance(payee, dict):
        inv["payee_name"] = payee.get("name")
        inv["payee_wallet"] = payee.get("wallet") or payee.get("address")
    else:
        inv["payee_name"] = payee
    inv["payee_wallet"] = inv["payee_wallet"] or g("payee_wallet", "pay_to", "wallet")
    payer = obj.get("payer") or obj.get("bill_to") or {}
    if isinstance(payer, dict):
        inv["payer_wallet"] = payer.get("wallet") or payer.get("address")
    amt = g("total", "amount_due", "amountDue", "amount")
    inv["amount"] = num(str(amt)) if amt is not None else None
    inv["currency"] = (g("currency", "token") or "").upper() or None
    inv["due_date"] = g("due_date", "dueDate", "due")
    net = g("network", "chain", "chain_id", "chainId")
    inv["network"] = str(net) if net is not None else None
    for k in ("subtotal", "tax", "discount"):
        if obj.get(k) is not None:
            inv[k] = num(str(obj[k]))
    for it in obj.get("line_items") or obj.get("items") or []:
        inv["line_items"].append({"description": it.get("description"), "quantity": num(str(it["quantity"])) if it.get("quantity") is not None else None,
                                  "unit_price": num(str(it["unit_price"])) if it.get("unit_price") is not None else None,
                                  "amount": num(str(it["amount"])) if it.get("amount") is not None else None})
    inv["raw_wallets"] = [w for w in (inv["payee_wallet"], inv["payer_wallet"]) if w]
    return inv


def parse_eip681(uri: str, chain: pc.Chain | None) -> dict:
    """ethereum:<token>@<chainId>/transfer?address=<to>&uint256=<units>  or  ethereum:<to>@<chainId>?value=<wei>"""
    inv = blank()
    inv["format"] = "eip681"
    m = re.fullmatch(r"ethereum:(?:pay-)?(0x[0-9a-fA-F]{40})(?:@(\d+))?(?:/(\w+))?(?:\?(.*))?", uri.strip())
    if not m:
        raise pc.PaymentError("not a valid EIP-681 URI")
    target, cid, fn, q = m.group(1), m.group(2), m.group(3), parse_qs(m.group(4) or "")
    inv["network"] = cid or "(not given)"
    inv["reference"] = (q.get("ref") or q.get("reference") or q.get("memo") or [None])[0]
    if fn == "transfer":
        inv["payee_wallet"] = (q.get("address") or [None])[0]
        units = (q.get("uint256") or [None])[0]
        tok = chain.token(target) if chain else None
        inv["currency"] = tok.symbol.upper() if tok else target
        inv["token_address"] = target
        if units is not None and tok:
            inv["amount"] = Decimal(int(float(units)) if "e" in units.lower() else int(units)) / (Decimal(10) ** tok.decimals)
    elif fn is None:
        inv["payee_wallet"] = target
        inv["currency"] = "PROS"
        v = (q.get("value") or [None])[0]
        if v is not None:
            inv["amount"] = Decimal(int(float(v)) if "e" in v.lower() else int(v)) / Decimal(10**18)
    else:
        raise pc.PaymentError(f"EIP-681 function '{fn}' is not a payment (only transfer / plain value)")
    inv["raw_wallets"] = [w for w in (inv["payee_wallet"],) if w]
    return inv


def pdf_text(path: str) -> str:
    try:
        from pypdf import PdfReader  # optional dependency
        return "\n".join(p.extract_text() or "" for p in PdfReader(path).pages)
    except ImportError:
        out = subprocess.run(["pdftotext", "-layout", path, "-"], capture_output=True, text=True, check=True)
        return out.stdout


def load_invoice(src: str, chain: pc.Chain | None) -> dict:
    if src.startswith("ethereum:"):
        return parse_eip681(src, chain)
    if not os.path.isfile(src):
        raise pc.PaymentError(f"no such invoice file: {src}")
    if os.path.getsize(src) > MAX_INPUT:
        raise pc.PaymentError(f"invoice file too large (> {MAX_INPUT} bytes)")
    with open(src, "rb") as f:
        head = f.read(5)
    if head == b"%PDF-":
        return parse_text(pdf_text(src), "pdf")
    with open(src, encoding="utf-8") as f:
        body = f.read()
    if body.lstrip().startswith("{"):
        return parse_json(json.loads(body))
    if body.strip().startswith("ethereum:"):
        return parse_eip681(body.strip(), chain)
    return parse_text(body, "text")


# ------------------------------------------------------------------ checks
def check(inv: dict, sender: str | None, vendors: dict, ledger: set, today: date, accept_usd: bool) -> tuple[list, list]:
    """Return (problems, warnings). Any problem blocks the transaction."""
    P, W = [], []
    for f, label in (("reference", "invoice reference / number"), ("payee_wallet", "payee wallet address"),
                     ("amount", "amount / total due"), ("currency", "currency or token")):
        if not inv.get(f):
            P.append(f"MISSING {label}")
    if inv.get("malformed_wallets"):
        P.append(f"MALFORMED wallet address(es) in the invoice: {', '.join(inv['malformed_wallets'])}")
    if inv.get("conflicting_wallets"):
        P.append(f"AMBIGUOUS payee: more than one pay-to address ({inv['payee_wallet']}, {', '.join(inv['conflicting_wallets'])})")
    if inv.get("payee_wallet"):
        try:
            inv["payee_wallet"] = pc.check_address(inv["payee_wallet"], "payee wallet")
        except pc.PaymentError as e:
            P.append(f"INVALID {e}")
    if inv.get("amount") is not None and inv["amount"] <= 0:
        P.append(f"INVALID amount {inv['amount']}")
    cur = (inv.get("currency") or "").upper()
    if inv.get("currency_conflict"):
        P.append(f"MISMATCH currency: total is in {inv['currency_conflict'][0]} but the invoice says {inv['currency_conflict'][1]}")
    if cur in ("USD", "$"):
        if accept_usd:
            W.append("invoice is in USD; paying in USDC 1:1 because --accept-usd-as-usdc was given")
            inv["token"] = "USDC"
        else:
            P.append("CURRENCY is fiat USD, not a token: confirm paying in USDC 1:1 with --accept-usd-as-usdc")
    elif cur in FIAT:
        P.append(f"CURRENCY {cur} is fiat with no token on Pharos mainnet - cannot pay it on chain without a quote/FX")
    elif cur in ("USDT", "ETH"):
        P.append(f"TOKEN {cur} is not in the Pharos mainnet registry (USDT absent; ETH is WETH) - ask the payee")
    elif cur and cur not in SUPPORTED and not cur.startswith("0X"):
        P.append(f"TOKEN {cur} is not supported (supported: {', '.join(sorted(SUPPORTED))})")
    elif cur:
        inv["token"] = cur
    net = (inv.get("network") or "").lower()
    if net and not any(w in net for w in PHAROS_NETWORK_WORDS):
        P.append(f"MISMATCH network: invoice asks for '{inv['network']}', this agent pays on Pharos mainnet (chainId 1672)")
    elif not net:
        W.append("invoice does not name a network; assuming Pharos mainnet (chainId 1672)")
    items = [i for i in inv.get("line_items", []) if i.get("amount") is not None]
    for i in items:
        if i.get("quantity") is not None and i.get("unit_price") is not None and i["quantity"] * i["unit_price"] != i["amount"]:
            P.append(f"MISMATCH line '{i['description']}': {i['quantity']} x {i['unit_price']} != {i['amount']}")
    if items:
        s = sum(i["amount"] for i in items)
        sub = inv.get("subtotal")
        if sub is not None and sub != s:
            P.append(f"MISMATCH subtotal {sub} != sum of line items {s}")
        expected = (sub if sub is not None else s) + (inv.get("tax") or 0) - (inv.get("discount") or 0)
        if inv.get("amount") is not None and expected != inv["amount"]:
            P.append(f"MISMATCH total {inv['amount']} != line items {s}" + (f" + tax {inv['tax']}" if inv.get("tax") else "")
                     + (f" - discount {inv['discount']}" if inv.get("discount") else "") + f" = {expected}")
    elif inv["format"] != "eip681":
        W.append("no line items found - total could not be cross-checked")
    if inv.get("payee_name") and vendors:
        known = vendors.get(inv["payee_name"].strip().lower())
        if known and inv.get("payee_wallet") and known != inv["payee_wallet"].lower():
            P.append(f"MISMATCH payee wallet: '{inv['payee_name']}' is on file with {known}, invoice says {inv['payee_wallet']} (possible invoice fraud - verify out of band)")
        elif known:
            W.append(f"payee wallet matches the vendor book entry for '{inv['payee_name']}'")
        else:
            W.append(f"payee '{inv['payee_name']}' is not in the vendor book - first payment to this wallet")
    if inv.get("payer_wallet") and sender:
        try:
            if pc.check_address(inv["payer_wallet"], "bill-to wallet") != sender:
                P.append(f"MISMATCH payer: invoice is billed to {inv['payer_wallet']}, you are paying from {sender}")
        except pc.PaymentError as e:
            P.append(f"INVALID {e}")
    if inv.get("reference") and inv["reference"] in ledger:
        P.append(f"DUPLICATE invoice reference {inv['reference']} was already prepared/paid (ledger) - not paying twice")
    if inv.get("due_date"):
        d = parse_date(inv["due_date"])
        if d is None:
            W.append(f"could not read due date '{inv['due_date']}'")
        elif d < today:
            W.append(f"invoice is OVERDUE (due {d.isoformat()})")
        else:
            inv["due_iso"] = d.isoformat()
    return P, W


def parse_date(s: str) -> date | None:
    s = str(s).strip()
    for f in ("%Y-%m-%d", "%d %B %Y", "%B %d, %Y", "%d %b %Y", "%b %d, %Y", "%d.%m.%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, f).date()
        except ValueError:
            continue
    return None


def show_extracted(inv: dict) -> str:
    rows = [("format", inv["format"]), ("reference", inv["reference"]), ("payee", inv["payee_name"]),
            ("payee wallet", inv["payee_wallet"]), ("amount", inv["amount"]), ("currency/token", inv["currency"]),
            ("due date", inv["due_date"]), ("network", inv["network"]), ("bill-to wallet", inv.get("payer_wallet")),
            ("line items", len(inv["line_items"])), ("subtotal/tax/discount", f"{inv['subtotal']}/{inv['tax']}/{inv['discount']}")]
    return "\n".join(f"  {k:<22}: {'-' if v in (None, '') else v}" for k, v in rows)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("invoice", help="invoice file (.txt / .pdf / .json) or an EIP-681 'ethereum:' URI")
    ap.add_argument("--from", dest="sender", required=True, help="your wallet address (the signer)")
    ap.add_argument("--vendors", default="vendors.json", help="known vendor wallets {name: address} ('' = none)")
    ap.add_argument("--ledger", default="ledger.jsonl", help="local ledger of prepared invoice refs (duplicate guard)")
    ap.add_argument("--record", action="store_true", help="append the prepared invoice to the ledger")
    ap.add_argument("--accept-usd-as-usdc", action="store_true", help="allow a USD-denominated invoice to be paid 1:1 in USDC")
    ap.add_argument("--today", help="override today's date (YYYY-MM-DD) for overdue checks")
    ap.add_argument("--out", help="write payment summary + unsigned tx JSON here")
    ap.add_argument("--endpoint", default=None, help="Chain MCP endpoint (https only)")
    a = ap.parse_args(argv)
    try:
        sender = pc.check_address(a.sender, "--from")
    except pc.PaymentError as e:
        print(e, file=sys.stderr)
        return 2
    vendors = {}
    if a.vendors and os.path.isfile(a.vendors):
        with open(a.vendors, encoding="utf-8") as f:
            vendors = {k.lower(): pc.check_address(v, f"vendor {k}") for k, v in json.load(f).items() if not k.startswith("_")}
    ledger = set()
    if a.ledger and os.path.isfile(a.ledger):
        with open(a.ledger, encoding="utf-8") as f:
            ledger = {json.loads(l)["reference"] for l in f if l.strip()}
    today = date.fromisoformat(a.today) if a.today else datetime.now(timezone.utc).date()

    chain = pc.Chain(a.endpoint)
    try:
        inv = load_invoice(a.invoice, chain)
    except (pc.PaymentError, json.JSONDecodeError, subprocess.CalledProcessError) as e:
        print(f"could not read invoice: {e}", file=sys.stderr)
        return 2
    print(f"Invoice: {a.invoice}")
    print("Extracted fields:")
    print(show_extracted(inv))
    problems, warns = check(inv, sender, vendors, ledger, today, a.accept_usd_as_usdc)
    for w in warns:
        print(f"  [note] {w}")
    if problems:
        print("FLAGGED - not preparing a payment until these are resolved with the payee:")
        for p in problems:
            print(f"  [!] {p}")
        return 1
    try:
        p = chain.prepare(sender, inv["payee_wallet"], inv.get("token_address") or inv["token"], inv["amount"],
                          reference=inv["reference"])
    except pc.PaymentError as e:
        for ok, c in getattr(e, "checks", []):
            print(f"    [{'ok' if ok else 'FAIL'}] {c}")
        print(f"REFUSED: {e}. No transaction was prepared.")
        return 1
    title = f"Payment for invoice {inv['reference']}" + (f" ({inv['payee_name']})" if inv.get("payee_name") else "")
    if inv.get("due_iso"):
        title += f", due {inv['due_iso']}"
    print(pc.render(p, title))
    print(f"  Calldata reference check: decode_memo(data) = {pc.decode_memo(p.tx['data'])!r}")
    print(pc.render_tx(p))
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump({"invoice": {k: (str(v) if isinstance(v, Decimal) else v) for k, v in inv.items()
                                   if k not in ("line_items",)},
                       "line_items": [{k: (str(v) if isinstance(v, Decimal) else v) for k, v in i.items()} for i in inv["line_items"]],
                       **p.to_json(), "invoiceNotes": warns}, f, indent=2)
        print(f"  saved {a.out}")
    if a.record:
        with open(a.ledger, "a", encoding="utf-8") as f:
            f.write(json.dumps({"reference": inv["reference"], "payee": inv["payee_wallet"], "amount": str(inv["amount"]),
                                "token": p.token.symbol, "signingHash": p.signing_hash, "status": "prepared",
                                "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}) + "\n")
        print(f"  recorded {inv['reference']} in {a.ledger} (status: prepared - duplicates will be flagged)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
