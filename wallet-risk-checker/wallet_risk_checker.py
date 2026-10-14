#!/usr/bin/env python3
"""Wallet Risk Checker for Pharos mainnet: a read-only pre-transaction check of a wallet or contract.

Run it on the address you are about to send funds to, approve, or call. It checks up to 8 signals and prints
a risk level (LOW / MEDIUM / HIGH) with every reason explained in plain language.

Chain facts come from the hosted Pharos Chain MCP (https://mcp.pharos.xyz/mcp), read-only tools:
  get_code               is it a contract? (and at creation_block-1, to confirm the creation block)
  get_transaction_count  how many transactions a wallet has sent (nonce)
  get_balance            native PROS balance
  get_block              timestamps of the creation / funding / first-activity blocks (ages)
  get_storage_at         EIP-1967 implementation slot (proxies)
  get_transaction        the exact calldata of every approval the wallet sent (spender + amount)
  eth_call               allowance(owner, spender) / isApprovedForAll(owner, operator) NOW -> is it still open?
Indexing (which a node cannot answer) comes from the public Pharosscan explorer API: verified-source status,
contract creator + creation tx, wallet funding tx and the address's transaction list.

Signals
  S1 age             contract creation age / wallet first-funding age
  S2 verified_code   contract source verified on Pharosscan (for proxies: the implementation too)
  S3 fresh_funding   wallet funded very recently and has sent only a few transactions
  S4 approvals       still-open approvals the wallet granted: unlimited, to an EOA, to an unverified or
                     brand-new contract, setApprovalForAll
  S5 counterparties  interacted with an address on your flag list (--flag / --flagged-file), or with
                     unverified contracts created in the last --fresh-days days
  S6 failed_txs      unusually high share of reverted transactions
  S7 bytecode        contract bytecode contains SELFDESTRUCT (real opcode, PUSH data skipped)
  S8 deployer        contract deployed by a wallet that was itself funded shortly before
Scoring: each flag is low (1) / medium (2) / high (3). HIGH if any high flag or score >= 5, MEDIUM if score >= 2,
else LOW. Nothing is signed or sent.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone

from Crypto.Hash import keccak

from pharos_explorer import EXPLORER_UI, PharosExplorer
from pharos_mcp import DEFAULT_ENDPOINT, MCPError, PharosMCP, check_address

__version__ = "1.0.0"
UINT256_MAX = 2 ** 256 - 1
UNLIMITED_MIN = 2 ** 255          # anything this large is "unlimited" in practice (MAX or MAX-ish)
EIP1967_IMPL = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
SEV = {"low": 1, "medium": 2, "high": 3}
# Well-known contracts deployed at the same deterministic (CREATE2) address on every EVM chain.
# Pharosscan shows Permit2 as unverified on Pharos; we label it so its approvals are not mistaken for a scam.
KNOWN = {
    "0x000000000022d473030f116ddee9f6b43ac78ba3": "Uniswap Permit2 (canonical CREATE2 address)",
    "0xca11bde05977b3631167028862be2a173976ca11": "Multicall3 (canonical address)",
}


def sel(sig: str) -> str:
    k = keccak.new(digest_bits=256)
    k.update(sig.encode())
    return "0x" + k.hexdigest()[:8]


M_APPROVE = sel("approve(address,uint256)")                    # 0x095ea7b3
M_INCREASE = sel("increaseAllowance(address,uint256)")         # 0x39509351
M_SET_ALL = sel("setApprovalForAll(address,bool)")             # 0xa22cb465
M_P2_APPROVE = sel("approve(address,address,uint160,uint48)")  # Permit2.approve 0x87517c45
S_ALLOWANCE = sel("allowance(address,address)")
S_IS_ALL = sel("isApprovedForAll(address,address)")
S_SYMBOL, S_DECIMALS = sel("symbol()"), sel("decimals()")


def h2i(v) -> int:
    if v is None:
        return 0
    if isinstance(v, int):
        return v
    s = str(v)
    return int(s, 16) if s.startswith("0x") else int(s)


def pad(a: str) -> str:
    return a[2:].lower().rjust(64, "0")


def utc(ts: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts))


def human_age(sec: float) -> str:
    d = sec / 86400
    if sec < 3600:
        return f"{max(sec, 0) / 60:.0f} minutes"
    if d < 1:
        return f"{sec / 3600:.1f} hours"
    return f"{d:.1f} days"


def has_selfdestruct(code_hex: str) -> bool:
    """Walk the bytecode opcode by opcode (skipping PUSH1..PUSH32 data) and look for SELFDESTRUCT (0xff)."""
    code = bytes.fromhex(code_hex[2:] if code_hex.startswith("0x") else code_hex)
    i = 0
    while i < len(code):
        op = code[i]
        if op == 0xFF:
            return True
        if 0x60 <= op <= 0x7F:
            i += op - 0x5F
        i += 1
    return False


class RiskChecker:
    def __init__(self, mcp: PharosMCP, ex: PharosExplorer, fresh_days: float, max_txs: int, flagged: dict[str, str]):
        self.m, self.ex = mcp, ex
        self.fresh = fresh_days * 86400
        self.max_txs = max_txs
        self.flagged = flagged
        self._ts: dict[int, int] = {}
        self._tok: dict[str, str] = {}
        self._iscode: dict[str, bool] = {}
        self.sources: list[str] = []

    # ---- chain helpers ---------------------------------------------------
    def ts(self, n: int) -> int:
        if n not in self._ts:
            self._ts[n] = h2i(self.m.call_tool("get_block", {"block": n})["timestamp"])
        return self._ts[n]

    def code(self, a: str, block: int | str = "latest") -> str:
        return self.m.call_tool("get_code", {"address": a, "block": block}).get("code", "0x")

    def is_contract(self, a: str) -> bool:
        if a not in self._iscode:
            self._iscode[a] = self.code(a) not in ("0x", "", None)
        return self._iscode[a]

    def symbol(self, token: str) -> str:
        if token not in self._tok:
            sym = token[:10] + "…"
            try:
                b = bytes.fromhex(self.m.eth_call(token, S_SYMBOL)[2:])
                if len(b) >= 96:
                    sym = b[64:64 + int.from_bytes(b[32:64], "big")].decode("utf-8", "replace") or sym
                elif len(b) == 32:
                    sym = b.rstrip(b"\0").decode("utf-8", "replace") or sym
            except (MCPError, ValueError):
                pass
            self._tok[token] = sym
        return self._tok[token]

    def contract_info(self, a: str) -> dict:
        """Verified? creation block/age? Explorer profile + Chain MCP timestamp."""
        if a in KNOWN:
            return {"address": a, "known": KNOWN[a], "verified": True, "age_s": None, "creation_block": None}
        p = self.ex.profile(a)
        cb = None
        tx = p.get("transaction_hash")
        if tx:
            try:
                cb = h2i(self.m.call_tool("get_transaction_receipt", {"hash": tx})["blockNumber"])
            except (MCPError, KeyError, TypeError):
                cb = None
        age = (time.time() - self.ts(cb)) if cb else None
        return {"address": a, "known": None, "verified": bool(p.get("is_verified")), "name": p.get("name"),
                "creation_block": cb, "creation_tx": tx, "creator": p.get("contract_creator"), "age_s": age,
                "is_proxy": bool(p.get("is_proxy"))}

    # ---- main --------------------------------------------------------------
    def check(self, addr: str) -> dict:
        head = self.m.block_number()
        now = self.ts(head)
        flags: list[dict] = []
        passed: list[str] = []
        facts: dict = {"address": addr, "block": head, "block_time": utc(now)}

        def flag(sig, sev, title, why, evidence=None):
            flags.append({"signal": sig, "severity": sev, "title": title, "why": why, "evidence": evidence or []})

        code = self.code(addr)
        is_c = code not in ("0x", "", None)
        facts["type"] = "contract" if is_c else "wallet (EOA)"
        bal = self.m.call_tool("get_balance", {"address": addr})
        facts["balance_pros"] = bal.get("balance")
        prof = self.ex.profile(addr)
        total, txs = self.ex.transactions(addr, self.max_txs)
        facts["tx_count_indexed"] = total
        facts["tx_scanned"] = len(txs)
        if addr in self.flagged:
            flag("S5", "high", "Address is on your flag list",
                 f"You (or your team) flagged this exact address: {self.flagged[addr]}. Do not interact unless you "
                 "know why it was flagged and that it no longer applies.")

        if is_c:
            self._check_contract(addr, code, prof, now, facts, flag, passed)
        else:
            self._check_wallet(addr, prof, txs, now, facts, flag, passed)
        self._check_counterparties(addr, txs, now, facts, flag, passed)
        self._check_failed(txs, facts, flag, passed)

        score = sum(SEV[f["severity"]] for f in flags)
        level = "HIGH" if any(f["severity"] == "high" for f in flags) or score >= 5 else "MEDIUM" if score >= 2 else "LOW"
        return {"facts": facts, "flags": flags, "passed": passed, "score": score, "level": level,
                "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}

    # S1/S2/S7/S8 for contracts
    def _check_contract(self, addr, code, prof, now, facts, flag, passed):
        info = self.contract_info(addr)
        facts["contract_name"] = info.get("name") or info.get("known")
        facts["verified"] = info["verified"]
        facts["code_bytes"] = (len(code) - 2) // 2
        if info.get("known"):
            passed.append(f"S1/S2 well-known contract: {info['known']}")
        # S1 age
        cb = info.get("creation_block")
        if cb:
            pre = self.code(addr, cb - 1)
            facts["creation_block"] = cb
            facts["creation_time"] = utc(self.ts(cb))
            facts["creation_confirmed_by_get_code"] = pre in ("0x", "", None)
            age = now - self.ts(cb)
            facts["age"] = human_age(age)
            ev = [f"{EXPLORER_UI}/tx/{info['creation_tx']}"]
            if age < 86400:
                flag("S1", "high", f"Contract is brand new ({human_age(age)} old)",
                     "It was deployed less than a day ago. Scams and rug-pulls are usually fresh contracts that "
                     "nobody has had time to review; wait or verify who deployed it.", ev)
            elif age < self.fresh:
                flag("S1", "medium", f"Contract is young ({human_age(age)} old)",
                     f"Deployed less than {self.fresh / 86400:g} days ago, so it has little track record.", ev)
            else:
                passed.append(f"S1 contract age {human_age(age)} (deployed {facts['creation_time']}, block {cb:,})")
        elif not info.get("known"):
            facts["age"] = "unknown"
            flag("S1", "low", "Contract age unknown",
                 "The explorer has no creation record for this contract, so its age could not be checked.")
        # S2 verified
        if not info.get("known"):
            if not info["verified"]:
                flag("S2", "high" if (info.get("age_s") or 1e12) < self.fresh else "medium",
                     "Source code is NOT verified",
                     "Nobody can read what this contract really does: Pharosscan has no verified source for it. "
                     "Verified code does not mean safe, but unverified code you are asked to approve or call is a "
                     "classic red flag.", [f"{EXPLORER_UI}/address/{addr}#code"])
            else:
                passed.append(f"S2 source verified on Pharosscan ({info.get('name')})")
            impl_word = self.m.call_tool("get_storage_at", {"address": addr, "position": EIP1967_IMPL}).get("value", "0x0")
            impl = "0x" + impl_word[-40:] if h2i(impl_word) else None
            if impl:
                facts["eip1967_implementation"] = impl
                ii = self.contract_info(impl)
                if not ii["verified"]:
                    flag("S2", "high", "Proxy points to an UNVERIFIED implementation",
                         "This is an upgradeable proxy (EIP-1967). The code that actually runs lives at "
                         f"{impl}, and that code is not verified, so the visible proxy source tells you nothing.",
                         [f"{EXPLORER_UI}/address/{impl}"])
                else:
                    passed.append(f"S2 proxy implementation {impl} verified ({ii.get('name')})")
                flag("S2", "low", "Upgradeable proxy",
                     "Whoever controls the proxy admin can swap the code later. Check who that is before trusting it "
                     "with large amounts.", [f"{EXPLORER_UI}/address/{impl}"])
        # S7 bytecode
        if has_selfdestruct(code):
            flag("S7", "medium", "Bytecode contains SELFDESTRUCT",
                 "The contract can call SELFDESTRUCT. Since the Cancun upgrade this no longer deletes code (except in "
                 "the deploy transaction), but it can still sweep the contract's whole PROS balance to any address "
                 "in one call. Unusual for a normal app contract.")
        else:
            passed.append("S7 no SELFDESTRUCT opcode in runtime bytecode")
        # S8 deployer freshness
        creator = info.get("creator")
        if creator:
            facts["deployer"] = creator
            fp = self.ex.profile(creator).get("funding_transaction") or {}
            fb = fp.get("block_number")
            if fb and cb:
                gap = self.ts(cb) - self.ts(fb)
                facts["deployer_funded_before_deploy"] = human_age(gap)
                if 0 <= gap < 86400:
                    flag("S8", "medium", "Deployed by a freshly funded wallet",
                         f"The deployer {creator} received its first PROS only {human_age(gap)} before deploying. "
                         "Throw-away deployer wallets are typical for scam tokens and drainers.",
                         [f"{EXPLORER_UI}/address/{creator}"])
                else:
                    passed.append(f"S8 deployer wallet was funded {human_age(gap)} before deploying")

    # S1/S3/S4 for wallets
    def _check_wallet(self, addr, prof, txs, now, facts, flag, passed):
        nonce = h2i(self.m.call_tool("get_transaction_count", {"address": addr})["nonce"])
        facts["nonce_sent_txs"] = nonce
        fund = prof.get("funding_transaction") or {}
        fb = fund.get("block_number")
        if fb:
            age = now - self.ts(fb)
            facts["first_funded"] = f"{utc(self.ts(fb))} (block {fb:,}) by {fund.get('from_address')}"
            facts["age"] = human_age(age)
            ev = [f"{EXPLORER_UI}/tx/{fund.get('transaction_hash')}"]
            if age < self.fresh and nonce < 20:
                flag("S3", "high" if age < 86400 else "medium",
                     f"Freshly funded wallet ({human_age(age)} old, {nonce} txs sent)",
                     "This wallet got its first PROS very recently and has almost no history. Fresh wallets are "
                     "what scammers, airdrop farmers and drainers use; a real counterparty usually has a longer past.",
                     ev)
            else:
                passed.append(f"S1/S3 wallet first funded {human_age(age)} ago, {nonce} txs sent")
        else:
            facts["age"] = "never funded / unknown"
            flag("S3", "medium", "No funding history found",
                 "The explorer shows no incoming PROS for this wallet. An empty, unused address may be a typo or a "
                 "look-alike (address-poisoning) address; double-check every character before sending.")
        self._check_approvals(addr, txs, now, facts, flag, passed)

    def _check_approvals(self, addr, txs, now, facts, flag, passed):
        seen: dict[tuple, dict] = {}
        for t in txs:
            if (t.get("from_address") or "").lower() != addr or t.get("method_id") not in (
                    M_APPROVE, M_INCREASE, M_SET_ALL, M_P2_APPROVE) or t.get("receipt_status") != 1:
                continue
            tx = self.m.call_tool("get_transaction", {"hash": t["hash"]})
            inp = tx["input"]
            m = inp[:10]
            target = tx["to"].lower()
            if m in (M_APPROVE, M_INCREASE):
                token, spender, amount = target, "0x" + inp[34:74], h2i("0x" + inp[74:138])
            elif m == M_SET_ALL:
                token, spender, amount = target, "0x" + inp[34:74], UINT256_MAX if h2i("0x" + inp[74:138]) else 0
            else:  # Permit2.approve(token, spender, amount, expiration)
                token, spender, amount = "0x" + inp[34:74], "0x" + inp[98:138], h2i("0x" + inp[138:202])
            key = (token, spender, m == M_SET_ALL, m == M_P2_APPROVE)
            if key not in seen:  # newest first -> keep the latest approval per pair
                seen[key] = {"token": token, "spender": spender, "kind": "setApprovalForAll" if m == M_SET_ALL else
                             "Permit2.approve" if m == M_P2_APPROVE else "approve", "granted": amount,
                             "tx": t["hash"], "block": h2i(tx["blockNumber"])}
        open_list = []
        for (token, spender, is_all, is_p2), a in seen.items():
            if is_p2:
                cur = a["granted"]  # Permit2 allowance lives inside Permit2; we report the granted amount
                a["current_note"] = "granted amount (Permit2 internal allowance, not re-read)"
            elif is_all:
                cur = UINT256_MAX if h2i(self.m.eth_call(token, S_IS_ALL + pad(addr) + pad(spender))) else 0
            else:
                try:
                    cur = h2i(self.m.eth_call(token, S_ALLOWANCE + pad(addr) + pad(spender)))
                except MCPError:
                    cur = a["granted"]
            a["current"] = cur
            if cur == 0:
                continue
            a["symbol"] = self.symbol(token)
            a["unlimited"] = cur >= UNLIMITED_MIN
            a["spender_is_contract"] = self.is_contract(spender)
            a["spender_info"] = self.contract_info(spender) if a["spender_is_contract"] else None
            open_list.append(a)
        facts["approvals_found"] = len(seen)
        facts["approvals_open"] = len(open_list)
        if not seen:
            passed.append(f"S4 no approval transactions in the last {len(txs)} txs scanned")
        verified_unl = []
        for a in open_list:
            si = a["spender_info"]
            what = "ALL tokens (setApprovalForAll)" if a["kind"] == "setApprovalForAll" else (
                "UNLIMITED " + a["symbol"] if a["unlimited"] else f"{a['current']} raw units of {a['symbol']}")
            ev = [f"{EXPLORER_UI}/tx/{a['tx']}", f"{EXPLORER_UI}/address/{a['spender']}"]
            if not a["spender_is_contract"]:
                flag("S4", "high", f"Open approval to a plain wallet: {what} → {a['spender']}",
                     "A normal person's wallet (not a contract) can move these tokens out of this wallet at any time. "
                     "Apps never need that; this is how approval-phishing drains work. Revoke it.", ev)
                continue
            fresh_spender = si and si.get("age_s") is not None and si["age_s"] < self.fresh
            if si and not si["verified"]:
                flag("S4", "high" if (a["unlimited"] and fresh_spender) else "medium",
                     f"Open {'unlimited ' if a['unlimited'] else ''}approval to an unverified contract: {what} → "
                     f"{a['spender']}" + (f" (deployed {human_age(si['age_s'])} ago)" if si.get("age_s") else ""),
                     "This wallet still lets a contract with unreadable code spend its tokens"
                     + (" without any limit" if a["unlimited"] else "")
                     + ". If that contract is malicious or gets exploited, the tokens can be taken without another "
                       "signature.", ev)
            elif a["unlimited"]:
                name = (si or {}).get("name") or (si or {}).get("known") or "contract"
                verified_unl.append((what, name, a))
        if verified_unl:
            names = sorted({n for _, n, _ in verified_unl})
            flag("S4", "low", f"{len(verified_unl)} open unlimited approval(s) to verified contracts ({', '.join(names)})",
                 "Common for DEX/bridge use, but an unlimited approval stays open forever; if one of these protocols "
                 "is ever exploited, the approved token balance is exposed. Approve exact amounts when you can, "
                 "and revoke what you no longer use.",
                 [f"{w} → {a['spender']} ({EXPLORER_UI}/tx/{a['tx']})" for w, _, a in verified_unl])
        limited_ok = sum(1 for a in open_list if a["spender_is_contract"] and a["spender_info"]
                         and a["spender_info"]["verified"] and not a["unlimited"])
        if limited_ok:
            passed.append(f"S4 {limited_ok} open approval(s) are limited amounts to verified contracts")
        closed = len(seen) - len(open_list)
        if seen and closed:
            passed.append(f"S4 {closed} of {len(seen)} approval(s) found are no longer open (allowance 0 now)")

    # S5 counterparties
    def _check_counterparties(self, addr, txs, now, facts, flag, passed):
        cps: dict[str, dict] = {}
        for t in txs:
            for side, other in (("from_addr", "to_addr"), ("to_addr", "from_addr")):
                me = (t.get(side) or {}).get("address", "").lower()
                o = t.get(other) or {}
                oa = (o.get("address") or "").lower()
                if me == addr and oa and oa != addr:
                    c = cps.setdefault(oa, {"n": 0, "is_contract": o.get("is_contract"), "contract": o.get("contract"),
                                            "tx": t["hash"]})
                    c["n"] += 1
        facts["counterparties"] = len(cps)
        hits = [(a, c) for a, c in cps.items() if a in self.flagged]
        for a, c in hits:
            flag("S5", "high", f"Interacted with a flagged address {a}",
                 f"{c['n']} transaction(s) with an address on your flag list ({self.flagged[a]}). Money that moved "
                 "through flagged addresses can be stolen funds or a scam operator's wallet.",
                 [f"{EXPLORER_UI}/tx/{c['tx']}"])
        young = []
        for a, c in cps.items():
            k = c.get("contract") or {}
            if c.get("is_contract") and a not in KNOWN and a not in self.flagged and not k.get("is_verified") \
                    and k.get("block_number"):
                if now - self.ts(int(k["block_number"])) < self.fresh:
                    young.append((a, c, now - self.ts(int(k["block_number"]))))
        for a, c, age in young[:5]:
            flag("S5", "medium", f"Interacted with a new unverified contract {a} ({human_age(age)} old)",
                 "Brand-new contracts with hidden code are where most scams and drainers live. Check what that "
                 "interaction was before trusting this address.", [f"{EXPLORER_UI}/tx/{c['tx']}"])
        if not hits and not young:
            passed.append(f"S5 none of {len(cps)} counterparties is on the flag list "
                          f"({len(self.flagged)} entries) or a new unverified contract")

    # S6 failed share
    def _check_failed(self, txs, facts, flag, passed):
        n = len(txs)
        failed = sum(1 for t in txs if t.get("receipt_status") == 0)
        facts["failed_txs"] = f"{failed}/{n}"
        if n >= 10 and failed / n > 0.3:
            flag("S6", "medium" if failed / n > 0.5 else "low", f"{failed / n:.0%} of its transactions failed",
                 "Many reverted transactions often mean a bot spamming calls, a broken contract, or someone probing for "
                 "exploits.")
        else:
            passed.append(f"S6 failed transactions {failed}/{n}")


def render(r: dict) -> str:
    f = r["facts"]
    L = [f"=== Wallet Risk Check · {f['address']} · Pharos mainnet block #{f['block']:,} ({f['block_time']})",
         f"type: {f['type']}" + (f" · {f['contract_name']}" if f.get("contract_name") else "")
         + f" · age: {f.get('age', 'n/a')} · balance {f['balance_pros']} PROS · indexed txs {f['tx_count_indexed']}"
         f" (scanned {f['tx_scanned']})",
         f"explorer: {EXPLORER_UI}/address/{f['address']}", "",
         f"RISK LEVEL: {r['level']}  (score {r['score']}; low=1 medium=2 high=3; HIGH if any high flag or score>=5,"
         " MEDIUM if score>=2)", ""]
    if r["flags"]:
        L.append("Red flags:")
        for i, x in enumerate(r["flags"], 1):
            L.append(f" {i}. [{x['severity'].upper()}] ({x['signal']}) {x['title']}")
            L.append(f"    why it matters: {x['why']}")
            for e in x["evidence"]:
                L.append(f"    evidence: {e}")
    else:
        L.append("Red flags: none found.")
    L.append("")
    L.append("Checks passed:")
    L += [f" ✓ {p}" for p in r["passed"]]
    L.append("")
    L.append("Not financial advice: these are heuristics over on-chain history; a LOW result is not a guarantee.")
    return "\n".join(L)


def load_flagged(path: str | None, extra: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    if path:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        for e in data.get("addresses", []):
            out[check_address(e["address"])] = e.get("reason", "flagged")
    for item in extra:
        a, _, why = item.partition(":")
        out[check_address(a.strip())] = why.strip() or "flagged on the command line"
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Pre-transaction risk check of a Pharos wallet or contract (read-only).")
    ap.add_argument("address", nargs="+", help="0x address(es) to check")
    ap.add_argument("--fresh-days", type=float, default=7.0, help="'new' means younger than this (default 7)")
    ap.add_argument("--max-txs", type=int, default=500, help="newest transactions to scan (default 500, max 5000)")
    ap.add_argument("--flagged-file", default="flagged_addresses.json", help="JSON flag list ('' to disable)")
    ap.add_argument("--flag", action="append", default=[], metavar="ADDR[:reason]", help="extra flagged address")
    ap.add_argument("--json", help="write the full result(s) to this JSON file")
    ap.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help="Chain MCP endpoint (https only)")
    a = ap.parse_args(argv)
    try:
        addrs = [check_address(x) for x in a.address]
        if not 0 < a.fresh_days <= 365:
            raise ValueError("--fresh-days must be in (0, 365]")
        if not 1 <= a.max_txs <= 5000:
            raise ValueError("--max-txs must be in [1, 5000]")
        flagged = load_flagged(a.flagged_file or None, a.flag)
    except (ValueError, OSError, KeyError, json.JSONDecodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    rc = RiskChecker(PharosMCP(a.endpoint), PharosExplorer(), a.fresh_days, a.max_txs, flagged)
    results = []
    for addr in addrs:
        r = rc.check(addr)
        results.append(r)
        print(render(r))
        print()
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(results, fh, indent=2, ensure_ascii=False, default=str)
    return 1 if any(r["level"] == "HIGH" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
