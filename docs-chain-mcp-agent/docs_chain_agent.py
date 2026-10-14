#!/usr/bin/env python3
"""Docs MCP + Chain MCP agent: explain a contract function from the Pharos docs, then call it on chain.

For one contract function the agent
  1. EXPLAIN  - asks the Pharos Developer Docs MCP (https://docs.pharos.xyz/~gitbook/mcp, tool `askQuestion`)
                what the function does, its parameters and return values; keeps the doc sources.
  2. CALL     - ABI-encodes the call and runs it read-only with the Pharos Chain MCP
                (https://mcp.pharos.xyz/mcp, tool `eth_call`) pinned to one block, then decodes the result.
  3. INTERPRET- reads the result in the light of the explanation: facts the docs state (decimals, units such
                as wei/PROS, fields that are "always 0", timestamps) are extracted from the explanation text and
                checked against / applied to the on-chain values; then the decoded values are sent back to the
                Docs MCP for a sourced interpretation.
Nothing is signed or sent: only eth_call / get_block / get_code are used on chain.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from decimal import Decimal

from Crypto.Hash import keccak

from pharos_mcp import DEFAULT_ENDPOINT, DOCS_ENDPOINT, MCPError, PharosMCP, check_address

__version__ = "1.0.0"
EXPLORER = "https://www.pharosscan.xyz"
DOCS_BASE = "https://docs.pharos.xyz"
TYPE_RE = re.compile(r"^(address|bool|string|bytes32|u?int(8|16|24|32|40|48|56|64|72|80|88|96|104|112|120|128|136|144|152|160|168|176|184|192|200|208|216|224|232|240|248|256)?)$")
SIG_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(([^)]*)\)\s*(?:returns\s*\(([^)]*)\))?\s*$")


# ---------------------------------------------------------------- ABI (static types + string)
def keccak256(b: bytes) -> bytes:
    k = keccak.new(digest_bits=256)
    k.update(b)
    return k.digest()


def parse_params(s: str) -> list[tuple[str, str]]:
    out = []
    for i, part in enumerate(p.strip() for p in s.split(",") if p.strip()):
        bits = part.split()
        typ = bits[0]
        if typ == "uint":
            typ = "uint256"
        if typ == "int":
            typ = "int256"
        if not TYPE_RE.match(typ):
            raise ValueError(f"unsupported ABI type {typ!r} (supported: address, bool, string, bytes32, uintN, intN)")
        out.append((typ, bits[-1] if len(bits) > 1 else f"out{i}"))
    return out


def parse_signature(sig: str) -> dict:
    m = SIG_RE.match(sig or "")
    if not m:
        raise ValueError(f"bad function signature {sig!r}; expected e.g. 'balanceOf(address owner) returns (uint256 balance)'")
    ins, outs = parse_params(m.group(2)), parse_params(m.group(3) or "")
    if not outs:
        raise ValueError("give the return types: '... returns (uint256)'")
    if any(t == "string" for t, _ in ins):
        raise ValueError("string arguments are not supported")
    canonical = f"{m.group(1)}({','.join(t for t, _ in ins)})"
    return {"name": m.group(1), "inputs": ins, "outputs": outs, "canonical": canonical,
            "selector": "0x" + keccak256(canonical.encode()).hex()[:8]}


def encode_arg(typ: str, val: str) -> str:
    if typ == "address":
        return check_address(val)[2:].rjust(64, "0")
    if typ == "bool":
        if val.lower() not in ("true", "false", "1", "0"):
            raise ValueError(f"bad bool {val!r}")
        return ("1" if val.lower() in ("true", "1") else "0").rjust(64, "0")
    if typ == "bytes32":
        if not re.fullmatch(r"0x[0-9a-fA-F]{64}", val):
            raise ValueError(f"bad bytes32 {val!r}")
        return val[2:].lower()
    n = int(val, 0)
    bits = int(typ.lstrip("uint") or 256)
    if typ.startswith("uint"):
        if not 0 <= n < 2 ** bits:
            raise ValueError(f"{val} out of range for {typ}")
        return format(n, "064x")
    if not -(2 ** (bits - 1)) <= n < 2 ** (bits - 1):
        raise ValueError(f"{val} out of range for {typ}")
    return format(n % 2 ** 256, "064x")


def decode(outputs: list[tuple[str, str]], data: str) -> dict:
    raw = bytes.fromhex(data[2:] if data.startswith("0x") else data)
    if len(raw) < 32 * len(outputs):
        raise ValueError(f"call returned {len(raw)} bytes, expected at least {32 * len(outputs)} (wrong signature or not a contract?)")
    word = lambda i: raw[32 * i:32 * i + 32]
    res = {}
    for i, (typ, name) in enumerate(outputs):
        w = word(i)
        if typ == "address":
            res[name] = "0x" + w[12:].hex()
        elif typ == "bool":
            res[name] = w[-1] == 1
        elif typ == "bytes32":
            res[name] = "0x" + w.hex()
        elif typ == "string":
            off = int.from_bytes(w, "big")
            ln = int.from_bytes(raw[off:off + 32], "big")
            res[name] = raw[off + 32:off + 32 + ln].decode("utf-8", errors="replace")
        elif typ.startswith("uint"):
            res[name] = int.from_bytes(w, "big")
        else:
            res[name] = int.from_bytes(w, "big", signed=True)
    return res


# ---------------------------------------------------------------- agent
class Agent:
    def __init__(self, chain: PharosMCP, docs: PharosMCP, ask_interpretation: bool = True):
        self.chain, self.docs, self.ask_interp = chain, docs, ask_interpretation
        self.contract_name = ""

    # Docs MCP --------------------------------------------------------------
    def ask_docs(self, question: str, goal: str | None = None) -> dict:
        args = {"question": question}
        if goal:
            args["goal"] = goal
        res = self.docs._request("tools/call", {"name": "askQuestion", "arguments": args})
        text = "".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
        if res.get("isError"):
            raise MCPError(f"Docs MCP askQuestion failed: {text[:200]}")
        body, sources = text, []
        m = re.search(r"^#{1,3}\s*Sources\s*$", text, flags=re.M)
        if m:
            body = text[:m.start()]
            for title, url in re.findall(r"^\s*[-*]\s*\[([^\]]+)\]\((\S+?)\)\s*$", text[m.end():], flags=re.M):
                url = DOCS_BASE + url if url.startswith("/") else url
                if url.startswith(DOCS_BASE + "/") and url not in [s["url"] for s in sources]:
                    sources.append({"title": title, "url": url})
        body = re.sub(r"\]\((/[^)\s]*)\)", lambda mm: f"]({DOCS_BASE}{mm.group(1)})", body).strip()
        return {"text": body, "sources": sources[:5]}

    # Chain MCP -------------------------------------------------------------
    def call(self, to: str, sig: dict, args: list[str], block: int) -> dict:
        if len(args) != len(sig["inputs"]):
            raise ValueError(f"{sig['canonical']} takes {len(sig['inputs'])} argument(s), got {len(args)}")
        data = sig["selector"] + "".join(encode_arg(t, v) for (t, _), v in zip(sig["inputs"], args))
        ret = self.chain.eth_call(to, data, block)
        return {"calldata": data, "raw": ret, "decoded": decode(sig["outputs"], ret)}

    def try_uint(self, to: str, fn: str, block: int) -> int | None:
        try:
            ret = self.chain.eth_call(to, "0x" + keccak256(fn.encode()).hex()[:8], block)
            return int(ret, 16) if ret not in ("0x", "") else None
        except (MCPError, ValueError):
            return None

    # Interpretation --------------------------------------------------------
    def interpret(self, to: str, sig: dict, decoded: dict, explanation: str, block: dict) -> list[str]:
        notes, expl = [], explanation.lower()
        bnum, bts = int(block["number"], 16), int(block["timestamp"], 16)
        onchain_dec = self.try_uint(to, "decimals()", bnum)
        m = re.search(r"(\d{1,2})\s+decimals", expl)
        doc_dec = int(m.group(1)) if m else None
        unit_wei = "wei" in expl
        unit_name = "PROS" if "pros" in expl and unit_wei else None
        scale = None
        if doc_dec is not None and onchain_dec is not None:
            ok = "matches" if doc_dec == onchain_dec else "DOES NOT match"
            notes.append(f"Docs say values use {doc_dec} decimals; decimals() on chain = {onchain_dec} -> {ok} the docs.")
            scale = onchain_dec
        elif onchain_dec is not None:
            notes.append(f"decimals() on chain = {onchain_dec} (the explanation does not state decimals); amounts are scaled by 10^{onchain_dec}.")
            scale = onchain_dec
        elif doc_dec is not None:
            notes.append(f"Contract has no decimals(); using {doc_dec} decimals as stated in the docs.")
            scale = doc_dec
        elif unit_wei:
            notes.append("Docs say the value is in wei -> divided by 10^18" + (f" ({unit_name})." if unit_name else "."))
            scale = 18
        symbol = None
        try:
            r = self.chain.eth_call(to, "0x95d89b41", bnum)  # symbol()
            symbol = decode([("string", "s")], r)["s"] if len(r) > 130 else None
        except (MCPError, ValueError):
            pass
        description = None
        try:
            r = self.chain.eth_call(to, "0x7284e416", bnum)  # description() (Chainlink feeds)
            description = decode([("string", "s")], r)["s"] if len(r) > 130 else None
        except (MCPError, ValueError):
            pass
        pair = re.fullmatch(r"\s*(\w+)\s*/\s*(\w+)\s*", description or "")
        if description:
            notes.append(f"description() on chain = {description!r}.")
        if not pair:  # e.g. Pharos self-managed feeds return a proxy name -> take the pair from the contract name
            pair = re.search(r"\b([A-Z][A-Z0-9]{1,9})\s*/\s*([A-Z][A-Z0-9]{1,9})\b", self.contract_name or "")
        if pair:
            notes.append(f"Price pair {pair.group(1)}/{pair.group(2)} -> a price value is {pair.group(2)} per 1 {pair.group(1)}.")
        for (typ, name), val in zip(sig["outputs"], decoded.values()):
            lname = name.lower()
            if typ == "address":
                code = self.chain.call_tool("get_code", {"address": val, "block": bnum})
                code = code.get("code", code) if isinstance(code, dict) else code
                kind = "a contract" if code not in ("0x", "", None) else "an externally-owned account (no code)"
                notes.append(f"{name} = {val} is {kind}: {EXPLORER}/address/{val}")
            elif typ == "bool":
                notes.append(f"{name} = {val}.")
            elif re.search(r"(at|time|timestamp)$", lname) and typ.startswith("uint") and 10 ** 9 < val < 10 ** 10:
                age = bts - val
                when = datetime.fromtimestamp(val, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                notes.append(f"{name} = {val} -> {when}, {age} s ({age / 60:.1f} min) before block #{bnum:,}"
                             + ("; the docs ask you to check freshness against your own staleness limit." if "stale" in expl or "fresh" in expl else "."))
            elif re.search(r"(round|id)", lname) and val == 0 and re.search(r"always\s+`?0`?|always zero", expl):
                notes.append(f"{name} = 0, exactly as the docs say for this contract (always 0).")
            elif typ.startswith(("uint", "int")) and scale is not None and not re.search(r"(round|id|decimals|count|nonce)", lname):
                human = Decimal(val) / (Decimal(10) ** scale)
                unit = (f"{pair.group(2)} per {pair.group(1)}" if pair and re.search(r"answer|price", lname) else unit_name or symbol or "")
                notes.append(f"{name} = {val} raw -> {human:,.{min(scale, 6)}f} {unit}".rstrip() + ".")
            else:
                notes.append(f"{name} = {val}.")
        return notes

    @staticmethod
    def heartbeat_check(out: dict) -> list[str]:
        """If the docs (explanation or interpretation) state a heartbeat in seconds, compare it with updatedAt."""
        text = out["explanation"]["text"] + " " + out.get("docs_interpretation", {}).get("text", "")
        m = re.search(r"heartbeat[^.\d]{0,40}?([\d,]{2,7})\s*(?:seconds|s\b)", text, flags=re.I)
        upd = out["result"].get("updatedAt")
        if not m or not isinstance(upd, int):
            return []
        hb = int(m.group(1).replace(",", ""))
        bts = int(datetime.strptime(out["block_time_utc"], "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc).timestamp())
        age = bts - upd
        verdict = "within" if age <= hb else "OLDER than"
        return [f"Docs state a {hb:,} s heartbeat; updatedAt is {age:,} s old -> {verdict} one heartbeat "
                f"({'fresh by that measure' if age <= hb else 'treat the price as stale'})."]

    def run(self, contract: str, name: str, signature: str, args: list[str], block: int | None = None) -> dict:
        contract = check_address(contract)
        sig = parse_signature(signature)
        self.contract_name = name
        for (t, _), v in zip(sig["inputs"], args):
            encode_arg(t, v)  # validate before any network call
        # 1) explain (Docs MCP)
        params = ", ".join(f"{t} {n}" for t, n in sig["inputs"]) or "none"
        q = (f"On Pharos, what does the function `{sig['canonical']}` of the {name} "
             f"(address {contract}) do? Explain its parameters ({params}) and its return values, including units or decimals.")
        expl = self.ask_docs(q, goal="explain a contract function before calling it read-only on Pharos mainnet")
        # 2) call (Chain MCP)
        bnum = block if block is not None else self.chain.block_number()
        blk = self.chain.call_tool("get_block", {"block": bnum})
        call = self.call(contract, sig, args, bnum)
        # 3) interpret
        notes = self.interpret(contract, sig, call["decoded"], expl["text"], blk)
        out = {"contract": contract, "contract_name": name, "function": sig["canonical"], "selector": sig["selector"],
               "args": args, "explanation": expl, "block": bnum,
               "block_time_utc": datetime.fromtimestamp(int(blk["timestamp"], 16), timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
               "calldata": call["calldata"], "result_raw": call["raw"], "result": call["decoded"],
               "interpretation": notes, "explorer": f"{EXPLORER}/address/{contract}"}
        if self.ask_interp:
            vals = ", ".join(f"{k}={v}" for k, v in call["decoded"].items())
            q2 = (f"I called `{sig['canonical']}` on the {name} ({contract}) on Pharos mainnet at block {bnum} "
                  f"({out['block_time_utc']}) and it returned: {vals}. Based on the documentation, what do these values mean?")
            out["docs_interpretation"] = self.ask_docs(q2, goal="interpret an on-chain read result using the Pharos docs")
        out["interpretation"] += self.heartbeat_check(out)
        return out


def render(r: dict) -> str:
    L = [f"=== {r['contract_name']} · {r['function']}  ({r['contract']})",
         f"    {r['explorer']}", "",
         "1) EXPLAIN — Pharos Developer Docs MCP (askQuestion)", ""]
    L += ["   " + ln for ln in r["explanation"]["text"].splitlines()]
    L += ["", "   Sources:"] + [f"   [{i}] {s['title']} — {s['url']}" for i, s in enumerate(r["explanation"]["sources"], 1)]
    L += ["", f"2) CALL — Pharos Chain MCP eth_call at block #{r['block']:,} ({r['block_time_utc']})",
          f"   args: {r['args'] or '(none)'}", f"   calldata: {r['calldata']}",
          f"   raw result: {r['result_raw'][:66] + ('…' if len(r['result_raw']) > 66 else '')}",
          "   decoded: " + json.dumps({k: (str(v) if isinstance(v, int) and not isinstance(v, bool) else v) for k, v in r["result"].items()}),
          "", "3) INTERPRET — result in the context of the explanation"]
    L += [f"   - {n}" for n in r["interpretation"]]
    if r.get("docs_interpretation"):
        L += ["", "   Docs MCP reading of these exact values:"]
        L += ["   " + ln for ln in r["docs_interpretation"]["text"].splitlines()]
        L += ["   Sources:"] + [f"   [{i}] {s['title']} — {s['url']}" for i, s in enumerate(r["docs_interpretation"]["sources"][:3], 1)]
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Explain a Pharos contract function from the docs (Docs MCP), call it (Chain MCP), interpret it.")
    ap.add_argument("--contract", help="contract address (0x…)")
    ap.add_argument("--name", help="what the contract is, used in the docs question, e.g. 'Chainlink PROS/USD price feed'")
    ap.add_argument("--function", dest="fn", help="signature with returns, e.g. 'balanceOf(address owner) returns (uint256 balance)'")
    ap.add_argument("--arg", action="append", default=[], help="function argument (repeat for several)")
    ap.add_argument("--examples", help="JSON file with a list of {contract,name,function,args} to run in sequence")
    ap.add_argument("--only", type=int, help="with --examples: run only entry N (1-based)")
    ap.add_argument("--block", type=int, help="block number to call at (default: latest)")
    ap.add_argument("--no-docs-interpretation", action="store_true", help="skip the 2nd Docs MCP question about the returned values")
    ap.add_argument("--json", help="also write all results to this JSON file")
    ap.add_argument("--chain-endpoint", default=DEFAULT_ENDPOINT)
    ap.add_argument("--docs-endpoint", default=DOCS_ENDPOINT)
    a = ap.parse_args(argv)
    try:
        if a.examples:
            jobs = json.load(open(a.examples))
            if not isinstance(jobs, list) or not jobs:
                raise ValueError("--examples must be a non-empty JSON list")
            if a.only is not None:
                if not 1 <= a.only <= len(jobs):
                    raise ValueError(f"--only must be between 1 and {len(jobs)}")
                jobs = [jobs[a.only - 1]]
        elif a.contract and a.fn and a.name:
            jobs = [{"contract": a.contract, "name": a.name, "function": a.fn, "args": a.arg}]
        else:
            raise ValueError("give --contract, --name and --function (or --examples FILE)")
        for j in jobs:
            check_address(j["contract"]); parse_signature(j["function"])
            if not str(j.get("name", "")).strip() or len(j["name"]) > 120:
                raise ValueError("each job needs a short 'name'")
        if a.block is not None and a.block < 0:
            raise ValueError("--block must be >= 0")
        chain, docs = PharosMCP(a.chain_endpoint), PharosMCP(a.docs_endpoint, timeout=90)
    except (ValueError, KeyError, OSError) as e:
        ap.error(str(e))
    agent, results, rc = Agent(chain, docs, not a.no_docs_interpretation), [], 0
    for j in jobs:
        try:
            r = agent.run(j["contract"], j["name"], j["function"], [str(x) for x in j.get("args", [])], a.block)
            results.append(r)
            print(render(r) + "\n")
        except (MCPError, ValueError) as e:
            print(f"!!! {j['name']} {j['function']}: {e}\n", file=sys.stderr)
            rc = 1
    if a.json:
        with open(a.json, "w") as f:
            json.dump(results, f, indent=2, default=str, ensure_ascii=False)
    return rc


if __name__ == "__main__":
    sys.exit(main())
