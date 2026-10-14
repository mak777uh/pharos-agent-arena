#!/usr/bin/env python3
"""Doc-Grounded Builder Agent: "how do I X on Pharos?" → plan from the Docs MCP (cited) → do X via the Chain MCP → verify.

    python builder_agent.py "How do I check the PROS balance of an address?" --address 0xADDR
    python builder_agent.py "How do I find the USDC contract address and read its total supply?"
    python builder_agent.py "How do I send USDC to another address?" --from 0xME --to 0xYOU --amount 25
    python builder_agent.py "How do I send USDC to another address?" --from 0xA --to 0xB --amount 5 --tx 0xHASH   # verify after signing
    python builder_agent.py --examples examples.json                                     # run a batch

1. PLAN   askQuestion on the Pharos Developer Docs MCP (https://docs.pharos.xyz/~gitbook/mcp). The answer and its
          sources are shown, and the facts the action needs (contract address, chain id, RPC method, event topic)
          are EXTRACTED FROM THAT ANSWER — the recipes contain no hard-coded addresses. If the docs don't cover the
          request ("can't find", "can't provide reliable steps", no sources), the agent says so and stops.
2. ACT    the matching recipe runs on the hosted Pharos Chain MCP (https://mcp.pharos.xyz/mcp). Anything that moves
          funds is only PREPARED: an unsigned transaction + eth_call simulation from the sender + estimate_gas.
3. VERIFY independent on-chain checks of the outcome against the plan (second read path, receipts, code/symbol),
          and a SUCCESS / FAILED / PREPARED verdict. For a prepared transfer, pass --tx after you signed it and the
          receipt is checked against the plan (status, token, from, to, amount).
No key, no signing, no broadcasting.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from decimal import Decimal
from urllib.parse import urljoin

from Crypto.Hash import keccak

from pharos_mcp import DEFAULT_ENDPOINT, DOCS_ENDPOINT, MCPError, PharosMCP, check_address

__version__ = "1.0.0"
DOCS_BASE = "https://docs.pharos.xyz/"
EXPLORER = "https://www.pharosscan.xyz"
ADDR_RE = re.compile(r"0x[0-9a-fA-F]{40}(?![0-9a-fA-F])")
TOPIC_RE = re.compile(r"0x[0-9a-fA-F]{64}(?![0-9a-fA-F])")
NO_ANSWER = [
    r"\b(couldn['’]?t|could not|can['’]?t|cannot|unable to) (find|locate|answer|provide|confirm)\b",
    r"\bdo(n['’]?t|es ?n['’]?t|es not| not) (specify|document|cover|describe|mention)\b",
    r"\bno (relevant |specific )?(information|documentation|instructions)\b",
    r"\b(is|are) not (covered|documented)\b",
]


def sel(sig: str) -> str:
    return keccak.new(digest_bits=256, data=sig.encode()).hexdigest()[:8]


def h2i(v) -> int:
    s = str(v)
    return int(s, 16) if s.startswith("0x") else int(s)


def word(x) -> str:
    return (x[2:].lower() if isinstance(x, str) else format(x, "x")).rjust(64, "0")


# ------------------------------------------------------------------ docs side --
class Docs:
    def __init__(self, endpoint: str):
        self.m = PharosMCP(endpoint, timeout=120)

    def ask(self, question: str) -> dict:
        res = self.m._request("tools/call", {"name": "askQuestion", "arguments": {"question": question}})
        text = "".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
        body, sources = text, []
        mm = re.search(r"^#{1,3}\s*Sources\s*$", text, flags=re.M)
        if mm:
            body = text[:mm.start()]
            for t, u in re.findall(r"^\s*[-*]\s*\[([^\]]+)\]\((\S+?)\)\s*$", text[mm.end():], flags=re.M):
                u = urljoin(DOCS_BASE, u)
                if u.startswith(DOCS_BASE) and u not in [s["url"] for s in sources]:
                    sources.append({"title": t, "url": u})
        body = re.sub(r"\]\((/[^)\s]*)\)", lambda x: f"]({urljoin(DOCS_BASE, x.group(1))})", body).strip()
        inline = set(re.findall(r"\]\((https://docs\.pharos\.xyz[^)\s]*)\)", body))
        sources.sort(key=lambda s: s["url"] not in inline)
        head = body[:500].replace("’", "'")
        covered = bool(sources) and not any(re.search(p, head, flags=re.I) for p in NO_ANSWER)
        return {"question": question, "answer": body, "sources": sources, "inline": sorted(inline), "covered": covered}


def mainnet_address(answer: str) -> str | None:
    """The address the docs give for Pacific Mainnet. Network of an address = a keyword right after it
    ("0x… on Pacific Mainnet") or else the nearest keyword before it within 120 chars ("Mainnet: 0x…",
    "the Mainnet USDC contract is 0x…"). If the answer names no network at all, its single address is used."""
    kw = r"(mainnet|pacific|testnet|atlantic)"
    named = re.search(kw, answer, flags=re.I)
    plain = []
    for m in ADDR_RE.finditer(answer):
        after = re.match(r"`?\)?,?\s*(on|for|in)\s+(the\s+)?(pharos\s+)?(\w+\s+)?" + kw, answer[m.end():m.end() + 45], flags=re.I)
        if after:
            kind = after.group(5).lower()
        else:
            before = list(re.finditer(kw, answer[max(0, m.start() - 120):m.start()], flags=re.I))
            if not before:
                plain.append(m.group(0).lower())
                continue
            kind = before[-1].group(1).lower()
        if kind in ("mainnet", "pacific"):
            return m.group(0).lower()
    return plain[0] if len(set(plain)) == 1 and not named else None


def chain_id(answer: str) -> int | None:
    m = re.search(r"chain ID[^0-9`]{0,20}`?(\d{3,7})", answer, flags=re.I)
    return int(m.group(1)) if m else None


def summary_lines(answer: str, n: int = 9) -> list[str]:
    """Docs answer without code blocks, first n non-empty lines."""
    no_code = re.sub(r"```.*?```", "[code example in the docs answer]", answer, flags=re.S)
    return [ln.rstrip() for ln in no_code.splitlines() if ln.strip()][:n]


# ------------------------------------------------------------------ recipes --
class Run:
    def __init__(self, req: dict, docs: Docs, chain: PharosMCP):
        self.req, self.docs, self.c = req, docs, chain
        self.plan: list[dict] = []      # {"step", "fact", "source"}
        self.acts: list[str] = []
        self.checks: list[dict] = []
        self.verdict = None
        self.extra_docs: list[dict] = []
        self.proof = None

    def fact(self, step: str, value, src: list[dict] | dict | None):
        s = src if isinstance(src, dict) else (src[0] if src else None)
        self.plan.append({"step": step, "fact": value, "source": s["url"] if s else None})

    def need(self, value, what: str):
        if value in (None, "", []):
            raise LookupError(f"the docs answer did not give {what}; not improvising")
        return value

    def follow_up(self, question: str) -> dict:
        r = self.docs.ask(question)
        self.extra_docs.append(r)
        return r

    def check(self, name: str, ok: bool, detail: str):
        self.checks.append({"check": name, "ok": bool(ok), "detail": detail})

    def call(self, to: str, sig_or_data: str, args: str = "", block="latest", frm: str | None = None) -> str:
        data = sig_or_data if sig_or_data.startswith("0x") else "0x" + sel(sig_or_data) + args
        tx = {"to": to, "data": data}
        if frm:
            tx["from"] = frm
        out = self.c.call_tool("eth_call", {"transaction": tx, "block": block})
        return out["result"] if isinstance(out, dict) else out

    def string(self, to: str, sig: str, block) -> str:
        b = bytes.fromhex(self.call(to, sig, block=block)[2:])
        return b[64:64 + int.from_bytes(b[32:64], "big")].decode("utf-8", "replace") if len(b) >= 96 else b.rstrip(b"\0").decode()

    # 1 — native balance
    def r_balance(self, d: dict):
        addr = check_address(self.need(self.req.get("address"), "--address (whose balance)"))
        method = "eth_getBalance" if "eth_getBalance" in d["answer"] else None
        self.fact("RPC method for a native balance", self.need(method, "the RPC method"), d["sources"])
        cid = chain_id(d["answer"])
        self.fact("Pharos mainnet chain id", cid, d["sources"])
        self.fact("unit conversion", "wei / 10^18 = PROS" if re.search(r"10\^18|1e18|10\*\*18", d["answer"]) else "not stated", d["sources"])
        mc = self.follow_up("What is the Multicall3 contract address on Pharos mainnet?")
        mc_addr = mainnet_address(mc["answer"])
        self.fact("Multicall3 address (second, independent read path for verification)", mc_addr, mc["sources"])
        info = self.c.call_tool("get_chain_info")
        blk = int(info["latestBlock"])
        bal = h2i(self.c.call_tool("get_balance", {"address": addr, "block": blk})["balanceWei"])
        self.acts.append(f"Chain MCP get_balance({addr}, block {blk:,}) → {bal} wei = {Decimal(bal) / Decimal(10**18):,.6f} PROS")
        if cid:
            self.check("connected chain = documented chain id", info["chainId"] == cid, f"get_chain_info chainId {info['chainId']} vs docs {cid}")
        if mc_addr:
            v = h2i(self.call(mc_addr, "getEthBalance(address)", word(addr), blk))
            self.acts.append(f"Chain MCP eth_call Multicall3.getEthBalance({addr}) @ {blk:,} → {v} wei")
            self.check("balance matches an independent read (Multicall3.getEthBalance, same block)", v == bal, f"{bal} vs {v}")
        self.verdict = "SUCCESS" if all(c["ok"] for c in self.checks) else "FAILED"
        self.proof = f"{EXPLORER}/address/{addr}"

    # 2 — token address + total supply
    def r_supply(self, d: dict):
        tok = self.need(mainnet_address(d["answer"]), "a mainnet token address")
        self.fact("token contract on Pacific Mainnet", tok, d["sources"])
        fn = "totalSupply()" if "totalSupply" in d["answer"] else None
        self.fact("read function", self.need(fn, "the read function"), d["sources"])
        self.fact("scale by", "decimals()" if "decimals" in d["answer"] else "not stated", d["sources"])
        blk = self.c.block_number()
        sup, dec = h2i(self.call(tok, "totalSupply()", block=blk)), h2i(self.call(tok, "decimals()", block=blk))
        sym = self.string(tok, "symbol()", blk)
        self.acts.append(f"Chain MCP eth_call {tok}.totalSupply() @ {blk:,} → {sup} raw; decimals() → {dec}; symbol() → {sym}")
        self.acts.append(f"result: total supply = {Decimal(sup) / Decimal(10 ** dec):,.6f} {sym}")
        code = self.c.call_tool("get_code", {"address": tok})
        code = code.get("code", code) if isinstance(code, dict) else code
        self.check("address from the docs has contract code on mainnet", bool(code and code != "0x"), f"get_code → {len(code or '') // 2 - 1} bytes")
        want = (self.req.get("token") or "").upper()
        if want:
            self.check(f"symbol() is the token the request asked about ({want})", sym.upper() == want, f"symbol() = {sym!r}")
        self.check("total supply is a positive number", sup > 0, f"{sup}")
        self.verdict = "SUCCESS" if all(c["ok"] for c in self.checks) else "FAILED"
        self.proof = f"{EXPLORER}/address/{tok}"

    # 3 — chain id
    def r_chain(self, d: dict):
        cid = self.need(chain_id(d["answer"]), "the chain id")
        self.fact("Pacific Mainnet chain id", cid, d["sources"])
        hexid = re.search(r"0x[0-9a-fA-F]{1,6}\b", d["answer"])
        self.fact("expected eth_chainId result", hexid.group(0) if hexid else hex(cid), d["sources"])
        info = self.c.call_tool("get_chain_info")
        self.acts.append(f"Chain MCP get_chain_info → chainId {info['chainId']} ({info.get('chainIdHex')}), latest block {int(info['latestBlock']):,}")
        self.check("chain id equals the documented value", info["chainId"] == cid, f"{info['chainId']} vs {cid}")
        self.check("hex form matches", info.get("chainIdHex", "").lower() == hex(cid), f"{info.get('chainIdHex')} vs {hex(cid)}")
        time.sleep(4)
        b = self.c.call_tool("get_block", {"block": "latest"})
        n2 = int(str(b["number"]), 0)
        self.check("chain is producing blocks (new block ~4 s later)", n2 > int(info["latestBlock"]), f"#{int(info['latestBlock']):,} → #{n2:,}")
        self.verdict = "SUCCESS" if all(c["ok"] for c in self.checks) else "FAILED"
        self.proof = f"{EXPLORER}/block/{int(str(b['number']), 0)}"

    # 4 — Transfer events
    def r_logs(self, d: dict):
        sig_hash = "0x" + keccak.new(digest_bits=256, data=b"Transfer(address,address,uint256)").hexdigest()
        lit = TOPIC_RE.search(d["answer"])
        if lit:
            self.fact("Transfer event topic0 (given literally in the docs answer)", lit.group(0).lower(), d["sources"])
            self.need(lit.group(0).lower() == sig_hash or None, "a Transfer topic that equals keccak256('Transfer(address,address,uint256)')")
        else:
            self.need(re.search(r"Transfer`?\s*(event|topic|signature)|(topic|signature|event)[^.\n]{0,60}Transfer", d["answer"], flags=re.I),
                      "the instruction to filter by the Transfer event topic")
            self.fact("Transfer event topic0 (docs: 'the Transfer event signature hash' → computed keccak256 of the standard "
                      "ERC-20 signature)", sig_hash, d["sources"])
        self.fact("method", "eth_getLogs filtered by address + topic" if "eth_getLogs" in d["answer"] else "not stated", d["sources"])
        sym = (self.req.get("token") or "USDC").upper()
        t = self.follow_up(f"What is the {sym} contract address on Pharos mainnet?")
        tok = self.need(mainnet_address(t["answer"]), f"the {sym} mainnet address")
        self.fact(f"{sym} contract on Pacific Mainnet", tok, t["sources"])
        cap = self.follow_up("What is the maximum block range for get_logs on the Pharos Chain MCP?")
        m = re.search(r"([\d,]{3,6})\s*blocks", cap["answer"])
        span = int(m.group(1).replace(",", "")) if m else None
        self.fact("Chain MCP get_logs block-range cap", self.need(span, "the get_logs block cap"), cap["sources"])
        head = self.c.block_number()
        logs = self.c.get_logs(tok, [sig_hash], head - span + 1, head)
        self.acts.append(f"Chain MCP get_logs(address={tok}, topics=[Transfer], blocks {head - span + 1:,}..{head:,}) → {len(logs)} logs")
        dec = h2i(self.call(tok, "decimals()"))
        for lg in logs[-3:]:
            self.acts.append(f"  {lg['transactionHash']}: {('0x' + lg['topics'][1][-40:])} → {('0x' + lg['topics'][2][-40:])} "
                             f"{Decimal(h2i(lg['data'])) / Decimal(10 ** dec):,.6f} {sym}")
        if logs:
            lg = logs[-1]
            rc = self.c.call_tool("get_transaction_receipt", {"hash": lg["transactionHash"]})
            hit = [x for x in rc["logs"] if x["address"].lower() == tok and x["topics"][0] == sig_hash
                   and x["data"] == lg["data"]]
            self.check("latest log is really in its tx receipt (status 1, same token/topic/amount)",
                       bool(hit) and h2i(rc["status"]) == 1, f"receipt {lg['transactionHash']} status {rc['status']}, {len(hit)} matching log(s)")
            self.proof = f"{EXPLORER}/tx/{lg['transactionHash']}"
        else:
            self.check("found at least one Transfer in the window", False, "0 logs (quiet token?)")
        self.verdict = "SUCCESS" if all(c["ok"] for c in self.checks) else "FAILED"

    # 5 — send tokens (prepare only, verify after signing with --tx)
    def r_send(self, d: dict):
        frm = check_address(self.need(self.req.get("from"), "--from (sender)"))
        to = check_address(self.need(self.req.get("to"), "--to (recipient)"))
        amount = Decimal(str(self.need(self.req.get("amount"), "--amount")))
        if amount <= 0:
            raise ValueError("amount must be positive")
        sym = (self.req.get("token") or "USDC").upper()
        tok = self.need(mainnet_address(d["answer"]), f"the {sym} mainnet address")
        self.fact(f"{sym} contract on Pacific Mainnet", tok, d["sources"])
        gas_note = "keep PROS for fees" if re.search(r"PROS[^.]*(fee|gas)", d["answer"]) else "not stated"
        self.fact("fees", gas_note, d["sources"])
        self.fact("same-network check", "sender and recipient on the same Pharos network" if "same" in d["answer"].lower() else "not stated", d["sources"])
        blk = self.c.block_number()
        dec = h2i(self.call(tok, "decimals()", block=blk))
        onchain_sym = self.string(tok, "symbol()", blk)
        self.check(f"documented address really is {sym} (symbol() on chain)", onchain_sym.upper() == sym, f"symbol() = {onchain_sym!r}")
        raw = int(amount * (Decimal(10) ** dec))
        data = "0x" + sel("transfer(address,uint256)") + word(to) + word(raw)
        tx = {"chainId": 1672, "from": frm, "to": tok, "value": "0x0", "data": data}
        self.acts.append(f"prepared UNSIGNED tx: {json.dumps(tx)}")
        if self.req.get("tx"):
            return self.verify_sent(tok, frm, to, raw, dec, sym)
        bal = h2i(self.call(tok, "balanceOf(address)", word(frm), blk))
        pros = h2i(self.c.call_tool("get_balance", {"address": frm, "block": blk})["balanceWei"])
        is_err, text = self.c.call_tool_raw("eth_call", {"transaction": {"from": frm, "to": tok, "data": data}, "block": blk})
        gas = None
        if not is_err:
            try:
                g = self.c.call_tool("estimate_gas", {"transaction": {"from": frm, "to": tok, "data": data}})
                gas = h2i(g.get("gas", g.get("gasEstimate")) if isinstance(g, dict) else g)
            except (MCPError, TypeError, ValueError):
                gas = None
        gp = h2i(self.c.call_tool("get_gas_price")["gasPriceWei"])
        self.acts.append(f"eth_call simulation from the sender @ {blk:,} → {'ok (returns true)' if not is_err else 'REVERT: ' + text[:120]}"
                         + (f"; estimate_gas {gas:,}" if gas else ""))
        self.check(f"sender holds ≥ {amount} {sym}", bal >= raw, f"balance {Decimal(bal) / Decimal(10 ** dec):,.6f} {sym}")
        fee = (gas or 65_000) * gp
        self.check("sender has PROS for the fee (docs: keep PROS for fees)", pros >= fee,
                   f"{Decimal(pros) / Decimal(10**18):,.6f} PROS vs fee ≈ {Decimal(fee) / Decimal(10**18):.6f} PROS")
        self.check("transfer simulates successfully from the sender", not is_err, "eth_call" + ("" if not is_err else f": {text[:100]}"))
        self.check("recipient is not the token contract itself", to != tok, to)
        ok = all(c["ok"] for c in self.checks)
        self.verdict = "PREPARED — sign and send it in your own wallet, then re-run with --tx <hash> to verify" if ok else "FAILED (would not succeed as planned)"
        self.proof = f"{EXPLORER}/address/{frm}"

    def verify_sent(self, tok, frm, to, raw, dec, sym):
        h = self.req["tx"]
        if not re.fullmatch(r"0x[0-9a-fA-F]{64}", h):
            raise ValueError("--tx must be a 32-byte transaction hash")
        rc = self.c.call_tool("get_transaction_receipt", {"hash": h})
        self.acts.append(f"Chain MCP get_transaction_receipt({h}) → status {rc['status']}, block {h2i(rc['blockNumber']):,}, {len(rc['logs'])} logs")
        T = "0x" + keccak.new(digest_bits=256, data=b"Transfer(address,address,uint256)").hexdigest()
        logs = [x for x in rc["logs"] if x["address"].lower() == tok and x["topics"][0] == T]
        self.check("transaction succeeded (status 1)", h2i(rc["status"]) == 1, f"status {rc['status']}")
        self.check(f"a {sym} Transfer was emitted by the documented token contract", bool(logs), f"{len(logs)} Transfer log(s) from {tok}")
        m = [x for x in logs if "0x" + x["topics"][1][-40:] == frm and "0x" + x["topics"][2][-40:] == to]
        self.check("from / to match the plan", bool(m), f"plan {frm} → {to}")
        got = h2i(m[0]["data"]) if m else None
        self.check("amount matches the plan", got == raw, f"plan {Decimal(raw) / Decimal(10 ** dec)} vs on-chain "
                   f"{'—' if got is None else Decimal(got) / Decimal(10 ** dec)} {sym}")
        self.verdict = "SUCCESS (transfer verified on chain)" if all(c["ok"] for c in self.checks) else "FAILED (on-chain result differs from the plan)"
        self.proof = f"{EXPLORER}/tx/{h}"


RECIPES = [  # (name, regex on the request, method)
    ("token_events", r"\b(events?|logs?)\b", "r_logs"),
    ("send_token", r"\b(send|transfer|pay)\b", "r_send"),
    ("token_supply", r"\b(total supply|supply|contract address)\b", "r_supply"),
    ("native_balance", r"\bbalance\b", "r_balance"),
    ("chain_check", r"\b(chain id|chain-id|chainid|connected to|which network|mainnet\?)\b", "r_chain"),
]


def run_request(req: dict, docs: Docs, chain: PharosMCP) -> dict:
    q = " ".join((req.get("question") or "").split())
    if not 8 <= len(q) <= 300:
        raise ValueError("question must be 8–300 characters")
    d = docs.ask(q)
    r = Run(req, docs, chain)
    out = {"question": q, "docs": d, "params": {k: v for k, v in req.items() if k != "question"}}
    if not d["covered"]:
        out.update(status="NOT COVERED BY THE DOCS", recipe=None,
                   message="The Pharos docs don't document this, so I won't improvise steps or send anything.")
        return out
    recipe = next(((n, m) for n, rx, m in RECIPES if re.search(rx, q, flags=re.I)), None)
    if recipe is None:
        out.update(status="EXPLAINED ONLY", recipe=None,
                   message="The docs explain it (above), but I have no safe executable recipe for this request, so I did nothing on chain.")
        return out
    out["recipe"] = recipe[0]
    try:
        getattr(r, recipe[1])(d)
        out["status"] = r.verdict
    except LookupError as e:
        out["status"], out["message"] = "STOPPED", str(e)
    except (ValueError, MCPError) as e:
        out["status"], out["message"] = "FAILED", str(e)[:300]
    out.update(plan=r.plan, actions=r.acts, checks=r.checks, follow_up_docs=r.extra_docs, proof=r.proof)
    return out


def render(o: dict) -> str:
    L = [f"━━━ Q: {o['question']}"]
    if o["params"]:
        L.append(f"    inputs: {json.dumps(o['params'])}")
    d = o["docs"]
    L.append("1) PLAN — Pharos Developer Docs MCP askQuestion:")
    L += [f"   │ {ln}" for ln in summary_lines(d["answer"])]
    L.append("   sources: " + (" · ".join(s["url"] for s in d["sources"][:3]) or "none"))
    if o.get("recipe") is None and o["status"] in ("NOT COVERED BY THE DOCS", "EXPLAINED ONLY"):
        L.append(f"⇒ {o['status']}: {o['message']}")
        return "\n".join(L)
    for fd in o.get("follow_up_docs", []):
        L.append(f"   follow-up docs question: {fd['question']} → sources: {' · '.join(s['url'] for s in fd['sources'][:2])}")
    L.append(f"   plan facts taken from the docs (recipe '{o['recipe']}'):")
    for i, p in enumerate(o.get("plan", []), 1):
        L.append(f"     {i}. {p['step']}: {p['fact']}  [{p['source'] or 'no source'}]")
    L.append("2) ACT — Pharos Chain MCP:")
    L += [f"   - {a}" for a in o.get("actions", [])]
    L.append("3) VERIFY:")
    L += [f"   [{'PASS' if c['ok'] else 'FAIL'}] {c['check']} — {c['detail']}" for c in o.get("checks", [])]
    L.append(f"⇒ {o['status']}" + (f": {o['message']}" if o.get("message") else ""))
    if o.get("proof"):
        L.append(f"   proof: {o['proof']}")
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Answer 'how do I X on Pharos' from the Docs MCP, then do X (read-only / prepare-only) and verify it.")
    ap.add_argument("question", nargs="*")
    ap.add_argument("--address"); ap.add_argument("--from", dest="frm"); ap.add_argument("--to")
    ap.add_argument("--amount"); ap.add_argument("--token", help="token symbol the request is about (default USDC)")
    ap.add_argument("--tx", help="after signing a prepared transfer: its hash, to verify the receipt against the plan")
    ap.add_argument("--examples", help="JSON file with a list of requests to run")
    ap.add_argument("--json", metavar="FILE")
    ap.add_argument("--docs-endpoint", default=DOCS_ENDPOINT); ap.add_argument("--chain-endpoint", default=DEFAULT_ENDPOINT)
    a = ap.parse_args(argv)
    try:
        if a.examples:
            reqs = json.load(open(a.examples, encoding="utf-8"))
        elif a.question:
            reqs = [{"question": " ".join(a.question), **{k: v for k, v in {"address": a.address, "from": a.frm, "to": a.to,
                     "amount": a.amount, "token": a.token, "tx": a.tx}.items() if v}}]
        else:
            ap.error("give a question or --examples")
        docs, chain = Docs(a.docs_endpoint), PharosMCP(a.chain_endpoint)
    except (ValueError, OSError, MCPError, json.JSONDecodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    results = []
    for req in reqs:
        try:
            o = run_request(req, docs, chain)
        except ValueError as e:
            print(f"━━━ Q: {req.get('question')}\n⇒ REJECTED INPUT: {e}")
            continue
        results.append(o)
        print(render(o) + "\n", flush=True)
    if a.json:
        json.dump(results, open(a.json, "w"), indent=2, default=str, ensure_ascii=False)
    return 0 if all(not str(o["status"]).startswith("FAILED") for o in results) else 2


if __name__ == "__main__":
    sys.exit(main())
