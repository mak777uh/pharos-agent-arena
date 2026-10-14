#!/usr/bin/env python3
"""Natural-Language Payment Agent for Pharos mainnet (prepare-only).

Turns "send 100 USDC to alice" into a validated, UNSIGNED EIP-1559 transaction for the user to sign in their
own wallet. Pipeline: parse free text -> ask a follow-up for anything missing or ambiguous -> validate on chain
via the Pharos Chain MCP (recipient, token contract, balances, simulation, nonce, fees) -> human-readable
summary -> (after confirmation) unsigned transaction + wallet signing request. Never signs, never sends.

Usage:
  python nl_pay.py --from 0xYourWallet "send 100 USDC to alice"
  python nl_pay.py --from 0xYourWallet --contacts contacts.json --yes --out tx.json "pay bob 25 usdc"
  python nl_pay.py --non-interactive "send 50 to alice"        # prints the follow-up questions as JSON, exit 3
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from decimal import Decimal, InvalidOperation

import pay_core as pc

__version__ = "1.0.0"

TOKEN_WORDS = {  # word in the text -> token symbol (None = ambiguous, needs a question)
    "usdc": "USDC", "usd coin": "USDC", "usdc.e": "USDC",
    "pros": "PROS", "pharos": "PROS", "phrs": "PROS",
    "wpros": "WPROS", "wrapped pros": "WPROS",
    "link": "LINK", "chainlink": "LINK",
    "weth": "WETH", "wrapped eth": "WETH",
}
AMBIGUOUS_TOKENS = {  # words that name a currency but not a unique Pharos token
    "$": "USD is not a token: did you mean USDC (Circle, the only USD stablecoin in the Pharos mainnet registry)?",
    "usd": "USD is not a token: did you mean USDC?", "dollar": "Dollars: did you mean USDC?",
    "dollars": "Dollars: did you mean USDC?", "bucks": "Dollars: did you mean USDC?",
    "usdt": "USDT is not in the Pharos mainnet token registry. Pay in USDC instead?",
    "tether": "USDT is not in the Pharos mainnet token registry. Pay in USDC instead?",
    "eth": "ETH on Pharos is the bridged ERC-20 WETH. Pay in WETH?",
    "ether": "ETH on Pharos is the bridged ERC-20 WETH. Pay in WETH?",
}
NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
             "ten": 10, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "hundred": 100, "a hundred": 100,
             "one hundred": 100, "thousand": 1000, "a thousand": 1000, "one thousand": 1000}
VERBS = r"(send|pay|transfer|give|tip|wire)"
ADDR_ANY = re.compile(r"0x[0-9a-fA-F]{6,}")


class Intent(dict):
    """amount (Decimal), token (symbol), recipient (address), recipient_label, questions (list)."""


def load_contacts(path: str | None) -> dict[str, str]:
    if not path:
        return {}
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    book = {}
    for name, addr in raw.items():
        if name.startswith("_"):
            continue
        book[name.lower()] = pc.check_address(addr, f"contact {name!r}")
    return book


def parse(text: str, book: dict[str, str]) -> Intent:
    t = " " + text.strip() + " "
    low = t.lower()
    it = Intent(amount=None, token=None, recipient=None, recipient_label=None, questions=[], notes=[])
    if not re.search(VERBS, low):
        it["notes"].append("no payment verb (send/pay/transfer) found - treating the text as a payment anyway")

    # --- addresses (anything that looks like 0x...)
    addrs = ADDR_ANY.findall(t)
    # --- amounts: numbers with optional thousands separators / decimals / k suffix, or number words
    work = ADDR_ANY.sub(" ", low)
    amounts = []
    for m in re.finditer(r"(?<![\w.])\$?\s?(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?\s?(k|m)?(?![\w])", work):
        v = Decimal(m.group(1).replace(",", "") + (m.group(2) or ""))
        v *= {"k": 1000, "m": 1_000_000}.get(m.group(3) or "", 1)
        amounts.append(v)
    for w in sorted(NUM_WORDS, key=len, reverse=True):
        if re.search(rf"\b{w}\b", work) and not amounts:
            amounts.append(Decimal(NUM_WORDS[w]))
            break
    uniq = sorted(set(amounts))
    if len(uniq) == 1:
        it["amount"] = uniq[0]
    elif len(uniq) > 1:
        it["questions"].append(("amount", f"I found several amounts ({', '.join(map(str, uniq))}). Which one should I send?"))
    else:
        it["questions"].append(("amount", "How much should I send?"))

    # --- token
    found, amb = [], []
    for w, sym in sorted(TOKEN_WORDS.items(), key=lambda kv: -len(kv[0])):
        if re.search(rf"(?<![\w.]){re.escape(w)}(?![\w])", work) and sym not in found:
            found.append(sym)
            work = re.sub(rf"(?<![\w.]){re.escape(w)}(?![\w])", " ", work)
    for w, q in AMBIGUOUS_TOKENS.items():
        pat = r"\$" if w == "$" else rf"(?<![\w.]){re.escape(w)}(?![\w])"
        if re.search(pat, work):
            amb.append(q)
    if len(found) == 1 and not amb:
        it["token"] = found[0]
    elif len(found) > 1:
        it["questions"].append(("token", f"You mentioned several tokens ({', '.join(found)}). Which one?"))
    elif amb:
        it["questions"].append(("token", amb[0]))
    else:
        it["questions"].append(("token", "Which token? (USDC, PROS, WPROS, LINK, WETH)"))

    # --- recipient: explicit address, or a name after "to"/"pay <name>"
    names = []
    for m in re.finditer(r"\bto\s+([a-z0-9_.\-]+)", low):
        names.append(m.group(1))
    m = re.match(rf"\s*{VERBS}\s+([a-z][a-z0-9_.\-]*)\b", low)
    if m and m.group(2) not in TOKEN_WORDS and m.group(2) not in NUM_WORDS and m.group(2) not in ("me", "the", "a"):
        names.append(m.group(2))
    names = [n.rstrip(".") for n in names if not n.startswith("0x") and n not in TOKEN_WORDS]
    if len(addrs) > 1 or (addrs and names and any(n in book for n in names)):
        it["questions"].append(("recipient", "I see more than one recipient. Who exactly should receive it (one address or contact)?"))
    elif addrs:
        try:
            it["recipient"] = pc.check_address(addrs[0], "recipient")
            it["recipient_label"] = pc.to_checksum(addrs[0])
        except pc.PaymentError as e:
            it["questions"].append(("recipient", f"{e}. Please re-type the recipient address."))
    elif len(set(names)) > 1 and sum(n in book for n in set(names)) != 1:
        it["questions"].append(("recipient", f"Several possible recipients ({', '.join(sorted(set(names)))}). Which one?"))
    elif names:
        n = next((x for x in names if x in book), names[0])
        if n in book:
            it["recipient"], it["recipient_label"] = book[n], n
        elif n.endswith(".eth") or "." in n:
            it["questions"].append(("recipient", f"'{n}' is a name-service name. ENS does not resolve on Pharos "
                                                 f"(chainId 1672) and it is not in your address book. What is the 0x address?"))
        else:
            close = difflib.get_close_matches(n, list(book), n=1, cutoff=0.75)
            hint = f" Did you mean '{close[0]}'?" if close else ""
            it["questions"].append(("recipient", f"'{n}' is not in your address book.{hint} Who should receive it (contact name or 0x address)?"))
    else:
        it["questions"].append(("recipient", "Who should receive it (contact name or 0x address)?"))
    return it


def apply_answer(it: Intent, slot: str, ans: str, book: dict[str, str]) -> str | None:
    """Fill one slot from a follow-up answer; returns an error/next question or None when filled."""
    a = ans.strip()
    if slot == "amount":
        try:
            v = Decimal(a.replace(",", "").lstrip("$"))
        except InvalidOperation:
            return "Please give just a number, e.g. 25 or 0.5."
        if v <= 0:
            return "The amount must be positive."
        it["amount"] = v
    elif slot == "token":
        al = a.lower()
        if al in ("y", "yes", "ok", "sure"):  # yes to the suggestion in the question
            q = dict(it["questions"]).get("token", "")
            sym = next((s for s in ("USDC", "WETH") if s in q), None)
            if not sym:
                return "Which token? (USDC, PROS, WPROS, LINK, WETH)"
            it["token"] = sym
        elif al in TOKEN_WORDS:
            it["token"] = TOKEN_WORDS[al]
        elif al.upper() in pc.TOKENS or al.upper() == pc.NATIVE:
            it["token"] = al.upper()
        else:
            return f"'{a}' is not a supported Pharos token. Choose one of USDC, PROS, WPROS, LINK, WETH."
    elif slot == "recipient":
        if a.lower() in book:
            it["recipient"], it["recipient_label"] = book[a.lower()], a.lower()
        else:
            try:
                it["recipient"] = pc.check_address(a, "recipient")
                it["recipient_label"] = pc.to_checksum(a)
            except pc.PaymentError as e:
                return f"{e}. Please give a contact name or a valid 0x address."
    elif slot == "from":
        try:
            it["from"] = pc.check_address(a, "your wallet")
        except pc.PaymentError as e:
            return f"{e}. Which wallet will sign (0x address)?"
    return None


def ask(question: str) -> str:
    print(f"? {question}")
    ans = sys.stdin.readline()
    if ans == "":
        raise SystemExit("no answer on stdin - stopping without preparing anything")
    ans = ans.rstrip("\n")
    if not sys.stdin.isatty():
        print(f"> {ans}")  # echo piped answers so a transcript reads naturally
    return ans


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("text", help="payment instruction in plain language, e.g. 'send 100 USDC to alice'")
    ap.add_argument("--from", dest="sender", help="your wallet address (the signer); asked for if missing")
    ap.add_argument("--contacts", default="contacts.json", help="address book JSON {name: 0xaddress} ('' = none)")
    ap.add_argument("--yes", action="store_true", help="skip the final 'prepare?' confirmation")
    ap.add_argument("--non-interactive", action="store_true", help="never prompt: print open questions as JSON, exit 3")
    ap.add_argument("--out", help="write the prepared transaction JSON here")
    ap.add_argument("--endpoint", default=None, help="Chain MCP endpoint (https only), default https://mcp.pharos.xyz/mcp")
    a = ap.parse_args(argv)

    try:
        book = load_contacts(a.contacts) if a.contacts else {}
    except FileNotFoundError:
        book = {}
    except pc.PaymentError as e:
        print(f"address book error: {e}", file=sys.stderr)
        return 2
    if len(a.text) > 500:
        print("instruction too long (max 500 characters)", file=sys.stderr)
        return 2

    it = parse(a.text, book)
    it["from"] = None
    if a.sender:
        try:
            it["from"] = pc.check_address(a.sender, "--from")
        except pc.PaymentError as e:
            print(e, file=sys.stderr)
            return 2
    else:
        it["questions"].append(("from", "Which wallet will sign this payment (your 0x address)?"))
    print(f"Instruction: {a.text!r}")
    print(f"Parsed     : amount={it['amount']} token={it['token']} recipient={it['recipient_label'] or it['recipient']}")
    for n in it["notes"]:
        print(f"note       : {n}")

    if it["questions"] and a.non_interactive:
        print(json.dumps({"status": "needs_input", "questions": [{"slot": s, "question": q} for s, q in it["questions"]]}, indent=2))
        return 3
    for slot, q in list(it["questions"]):
        for _ in range(3):
            err = apply_answer(it, slot, ask(q), book)
            if err is None:
                break
            q = err
        else:
            print(f"could not resolve {slot} after 3 tries - nothing prepared", file=sys.stderr)
            return 2

    try:
        chain = pc.Chain(a.endpoint)
        p = chain.prepare(it["from"], it["recipient"], it["token"], it["amount"])
    except pc.PaymentError as e:
        for ok, c in getattr(e, "checks", []):
            print(f"    [{'ok' if ok else 'FAIL'}] {c}")
        print(f"REFUSED: {e}. No transaction was prepared.")
        return 1
    if it["recipient_label"] and it["recipient_label"] in book:
        p.warnings.insert(0, f"recipient resolved from your address book: '{it['recipient_label']}'")
    print(pc.render(p))
    if not a.yes:
        if a.non_interactive:
            print("(non-interactive: pass --yes to emit the unsigned transaction)")
            return 3
        if ask("Prepare this as an unsigned transaction for your wallet? [y/N]").strip().lower() not in ("y", "yes"):
            print("Cancelled - nothing prepared.")
            return 0
    print(pc.render_tx(p))
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump({"instruction": a.text, **p.to_json()}, f, indent=2)
        print(f"  saved {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
