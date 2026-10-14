#!/usr/bin/env python3
"""dApp Support Agent for Pharos: explain, in plain language, why a transaction failed.

Input: one or more transaction hashes (Pharos Pacific mainnet by default).
Everything on-chain is read through the hosted Pharos Chain MCP server
(https://mcp.pharos.xyz/mcp) with read-only tools:

  get_transaction / get_transaction_receipt  - the tx and its outcome
  get_balance (block N-1)                    - did the sender even have the PROS it tried to send?
  eth_call   (block N-1)                     - replay the exact call to recover the revert data
  get_code                                   - is the target a contract at all?

The revert data is decoded (Error(string), Panic(uint256), built-in custom errors, optionally the
public Sourcify 4byte signature database for unknown selectors), classified into a failure type and
turned into an explanation plus a suggested fix. Nothing is signed or sent.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time

import requests

from pharos_mcp import DEFAULT_ENDPOINT, PharosMCP

EXPLORER = "https://www.pharosscan.xyz"
SIG_LOOKUP = "https://api.4byte.sourcify.dev/signature-database/v1/lookup"
TX_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
WEI = 10 ** 18

# 4-byte selector -> signature. Selectors are keccak256(signature)[:4], precomputed.
KNOWN_ERRORS = {
    "0x08c379a0": "Error(string)",
    "0x4e487b71": "Panic(uint256)",
    "0x82b42900": "Unauthorized()",
    "0x118cdaa7": "OwnableUnauthorizedAccount(address)",
    "0xe2517d3f": "AccessControlUnauthorizedAccount(address,bytes32)",
    "0xe450d38c": "ERC20InsufficientBalance(address,uint256,uint256)",
    "0xfb8f41b2": "ERC20InsufficientAllowance(address,uint256,uint256)",
    "0xcf479181": "InsufficientBalance(uint256,uint256)",
    "0xcd786059": "AddressInsufficientBalance(address)",
    "0x13be252b": "InsufficientAllowance()",
    "0x5274afe7": "SafeERC20FailedOperation(address)",
    "0x7939f424": "TransferFromFailed()",
    "0xd93c0665": "EnforcedPause()",
    "0x3ee5aeb5": "ReentrancyGuardReentrantCall()",
    "0x39d35496": "V3TooLittleReceived()",
    "0xc9f52c71": "TooLittleReceived()",
    "0x911bb2c9": "InsufficientOutputAmount(uint256,uint256,uint256)",
    "0x5bf6f916": "TransactionDeadlinePassed()",
    "0x1ab7da6b": "DeadlineExpired()",
    "0x203d82d8": "Expired()",
    "0xbd2a913c": "ExpiredSignature(uint256)",
    "0x4ef1d5c5": "NoRedeemRequest()",
    "0xd6bda275": "FailedCall()",
}

PANIC_CODES = {
    0x01: "an internal consistency check (assert) failed",
    0x11: "a calculation overflowed or underflowed (number got too big or went below zero)",
    0x12: "the contract tried to divide by zero",
    0x21: "an invalid value was converted to an enum",
    0x22: "corrupted storage byte array",
    0x31: "pop() on an empty array",
    0x32: "an array was read outside its bounds",
    0x41: "the contract ran out of memory",
    0x51: "an uninitialised internal function was called",
}

# Failure types: (title, plain explanation, suggested fix)
TYPES = {
    "insufficient_native_balance": (
        "Not enough PROS in the wallet",
        "The transaction tried to send more PROS than the wallet held at that moment, so the network "
        "could not carry it out. The network fee for the attempt was still charged.",
        "Lower the amount (leave some PROS for fees) or top up the wallet first, then send again.",
    ),
    "out_of_gas": (
        "Ran out of gas",
        "The transaction was given a gas limit that was too small for the work it had to do, so it stopped "
        "half-way and every change was undone. The fee for the gas that was used is not refunded.",
        "Send it again with a higher gas limit (let the wallet estimate gas, or raise the limit ~30-50%).",
    ),
    "insufficient_allowance": (
        "Token spending permission (allowance) too low",
        "The app tried to move your tokens but you had not approved it to spend that many. "
        "Token contracts block that until you give permission.",
        "Approve the app's contract (the spender) for at least the amount you want to use, wait for the "
        "approval to confirm, then retry. Approve only what you need rather than an unlimited amount.",
    ),
    "insufficient_token_balance": (
        "Not enough tokens",
        "The action needed more tokens than were available (in your wallet, or in the contract that had to pay "
        "out), so the token transfer was refused.",
        "Check your token balance and use a smaller amount. If you were claiming/withdrawing, the contract may "
        "not hold enough funds right now - try later or contact the app's support.",
    ),
    "slippage": (
        "Price moved too much (slippage protection)",
        "The swap would have given you less than the minimum you agreed to accept, because the price moved "
        "between signing and execution. The contract cancelled it to protect you.",
        "Retry with a fresh quote, a smaller trade size, or a slightly higher slippage tolerance (e.g. 0.5% -> 1%).",
    ),
    "deadline": (
        "Transaction expired",
        "The transaction carried a deadline and was executed after it, so the contract refused it.",
        "Submit it again promptly (or with a longer deadline in the app's settings).",
    ),
    "permission": (
        "Not allowed to call this function",
        "The function is restricted (owner / admin / specific role) and the sending wallet does not have that permission.",
        "Use the wallet that holds the required role, or ask the contract owner to grant it. "
        "If you are a regular user, this action is not meant for you - check you are on the right page.",
    ),
    "paused": (
        "Contract is paused",
        "The contract's operators have temporarily paused it, so all such actions are rejected.",
        "Wait until the app announces it is unpaused, then retry.",
    ),
    "nothing_to_do": (
        "Nothing to act on",
        "The action refers to something that does not exist (yet) - e.g. there was no pending request to cancel "
        "or nothing to claim.",
        "Refresh the app and check the current state of your position/request before retrying.",
    ),
    "panic": (
        "Contract hit an internal error",
        "The contract's code stopped on an internal safety check ({detail}).",
        "Usually caused by unusual input (e.g. zero or very large amounts). Retry with normal values; if it keeps "
        "failing, report it to the app's team with this transaction hash.",
    ),
    "contract_error": (
        "The contract rejected the transaction",
        "The contract refused the call with the message: {detail}",
        "This is an app-specific condition. Check the inputs/state the message refers to; if unclear, "
        "send this transaction hash to the app's support.",
    ),
    "silent_revert": (
        "Rejected without a reason",
        "The contract cancelled the transaction but did not say why (empty revert data).",
        "Check that you are calling the right contract and function with valid inputs, and that the app is not "
        "paused or restricted; if it persists, contact the app's support with this hash.",
    ),
    "no_contract": (
        "Target address is not a contract",
        "The transaction called a function on an address that has no contract code.",
        "Check the address - you may be on the wrong network or using an outdated contract address.",
    ),
    "state_changed": (
        "Failed at the time, but would succeed now",
        "Replaying the same transaction now does not fail, so it most likely failed because of conditions at that "
        "exact moment (another transaction in the same block changed prices, balances or limits first).",
        "Simply retry. If it was a trade, refresh the quote first.",
    ),
    "unknown": (
        "Could not determine the cause",
        "The transaction failed but the replay did not give enough information to tell why.",
        "Open the transaction in the explorer and share the hash with the app's support team.",
    ),
}

KEYWORDS = [  # checked in order against the lower-cased revert reason / error name
    ("insufficient_allowance", ["allowance", "insufficientallowance", "not approved", "approve"]),
    ("insufficient_token_balance", ["exceeds balance", "insufficient balance", "insufficientbalance",
                                    "not enough balance", "balance too low"]),
    ("slippage", ["slippage", "too little received", "toolittlereceived", "return amount is not enough",
                  "insufficient output", "insufficientoutput", "insufficient_output", "min return",
                  "minimum", "price impact", "excessive input"]),
    ("deadline", ["expired", "deadline"]),
    ("paused", ["paused", "enforcedpause"]),
    ("permission", ["unauthorized", "not owner", "caller is not", "only owner", "onlyowner", "access",
                    "forbidden", "not allowed", "not authorized", "permission"]),
    ("nothing_to_do", ["noredeemrequest", "nothing to claim", "already claimed", "no reward", "not found"]),
]


# ---------------------------------------------------------------- helpers
def h2i(v) -> int:
    if v is None:
        return 0
    if isinstance(v, int):
        return v
    s = str(v)
    return int(s, 16) if s.startswith("0x") else int(s)


def fmt_pros(wei: int) -> str:
    return f"{wei / WEI:,.6f} PROS"


def split_types(sig: str) -> list[str]:
    inner = sig[sig.index("(") + 1: sig.rindex(")")]
    return [t for t in inner.split(",") if t] if inner else []


def decode_static_args(types: list[str], data_hex: str) -> list | None:
    """Decode static ABI types (uintN/intN/address/bool/bytes32). Returns None if a type is unsupported."""
    words = [data_hex[i:i + 64] for i in range(0, len(data_hex), 64)]
    if len(words) < len(types):
        return None
    out = []
    for t, w in zip(types, words):
        if t.startswith("uint"):
            out.append(int(w, 16))
        elif t.startswith("int"):
            v = int(w, 16)
            out.append(v - (1 << 256) if v >> 255 else v)
        elif t == "address":
            out.append("0x" + w[24:])
        elif t == "bool":
            out.append(bool(int(w, 16)))
        elif t == "bytes32":
            out.append("0x" + w)
        else:
            return None
    return out


def decode_error_string(data_hex: str) -> str:
    """ABI-decode the single `string` argument of Error(string)."""
    off = int(data_hex[:64], 16) * 2
    ln = int(data_hex[off:off + 64], 16)
    raw = bytes.fromhex(data_hex[off + 64: off + 64 + ln * 2])
    return raw.decode("utf-8", errors="replace")


class SignatureLookup:
    """Optional lookup of unknown 4-byte selectors in the public Sourcify signature database."""

    def __init__(self, enabled: bool):
        self.enabled = enabled
        self.cache: dict[str, str | None] = {}

    def __call__(self, selector: str) -> str | None:
        if not self.enabled:
            return None
        if selector not in self.cache:
            try:
                r = requests.get(SIG_LOOKUP, params={"function": selector, "filter": "true"}, timeout=15)
                hits = (r.json().get("result", {}).get("function", {}) or {}).get(selector) or []
                self.cache[selector] = hits[0]["name"] if hits else None
            except (requests.RequestException, ValueError, KeyError):
                self.cache[selector] = None
        return self.cache[selector]


def parse_rpc_error(text: str) -> dict:
    """The MCP server returns node errors as JSON text: {"error": {"message", "code", "data"}}."""
    try:
        obj = json.loads(text)
        err = obj.get("error", obj)
        return err if isinstance(err, dict) else {"message": str(err)}
    except (json.JSONDecodeError, AttributeError):
        return {"message": text}


def decode_revert(data: str, lookup: SignatureLookup) -> dict:
    """Turn raw revert data into {kind, selector, signature, args, reason}."""
    data = (data or "0x").lower()
    if data in ("0x", ""):
        return {"kind": "empty", "reason": None}
    sel, body = data[:10], data[10:]
    if len(data) < 10:
        return {"kind": "raw", "selector": None, "reason": data}
    if sel == "0x08c379a0":
        try:
            return {"kind": "error_string", "selector": sel, "signature": "Error(string)",
                    "reason": decode_error_string(body)}
        except (ValueError, IndexError):
            return {"kind": "raw", "selector": sel, "reason": data}
    if sel == "0x4e487b71":
        code = int(body[:64] or "0", 16)
        return {"kind": "panic", "selector": sel, "signature": "Panic(uint256)", "code": code,
                "reason": PANIC_CODES.get(code, f"panic code 0x{code:02x}")}
    sig, source = KNOWN_ERRORS.get(sel), "built-in table"
    if not sig:
        sig, source = lookup(sel), "Sourcify 4byte database"
    if sig:
        args = decode_static_args(split_types(sig), body)
        return {"kind": "custom_error", "selector": sel, "signature": sig, "signature_source": source,
                "args": args, "reason": sig.split("(")[0]}
    return {"kind": "custom_error", "selector": sel, "signature": None, "args": None,
            "reason": f"unknown custom error {sel}", "raw": data}


def classify_reason(text: str) -> str | None:
    low = text.lower()
    for kind, words in KEYWORDS:
        if any(w in low for w in words):
            return kind
    return None


# ---------------------------------------------------------------- agent
class SupportAgent:
    def __init__(self, mcp: PharosMCP, lookup: SignatureLookup):
        self.m = mcp
        self.lookup = lookup

    def replay(self, tx: dict, block: int, gas: int | None) -> dict:
        call = {"from": tx["from"], "data": tx.get("input") or "0x", "value": hex(h2i(tx.get("value")))}
        if tx.get("to"):
            call["to"] = tx["to"]
        if gas:
            call["gas"] = hex(gas)
        if not tx.get("to"):  # contract creation: the hosted eth_call requires `to`, use estimate_gas instead
            is_err, text = self.m.call_tool_raw("estimate_gas", {"transaction": call})
            tool = "estimate_gas"
        else:
            is_err, text = self.m.call_tool_raw("eth_call", {"transaction": call, "block": block})
            tool = "eth_call"
        if not is_err:
            return {"tool": tool, "reverted": False, "block": block if tool == "eth_call" else "latest"}
        err = parse_rpc_error(text)
        return {"tool": tool, "reverted": True, "block": block if tool == "eth_call" else "latest",
                "message": err.get("message"), "data": err.get("data")}

    def diagnose(self, tx_hash: str) -> dict:
        if not TX_RE.match(tx_hash or ""):
            raise ValueError(f"not a valid transaction hash: {tx_hash!r}")
        tx_hash = tx_hash.lower()
        rep: dict = {"tx": tx_hash, "link": f"{EXPLORER}/tx/{tx_hash}"}
        tx = self.m.call_tool("get_transaction", {"hash": tx_hash})
        if not tx:
            rep.update(status="not_found", title="Transaction not found",
                       explanation="Pharos mainnet has no transaction with this hash.",
                       fix="Check the hash and the network (mainnet vs Atlantic testnet).")
            return rep
        rc = self.m.call_tool("get_transaction_receipt", {"hash": tx_hash})
        if not rc:
            rep.update(status="pending", title="Not mined yet",
                       explanation="The transaction is known but has not been included in a block yet.",
                       fix="Wait a few seconds and ask again; if it stays pending, speed it up or replace it in your wallet.")
            return rep
        block = h2i(rc["blockNumber"])
        gas_limit, gas_used = h2i(tx.get("gas")), h2i(rc.get("gasUsed"))
        price = h2i(rc.get("effectiveGasPrice") or tx.get("gasPrice"))
        value = h2i(tx.get("value"))
        rep.update(block=block, sender=tx["from"], to=tx.get("to"),
                   contract_created=rc.get("contractAddress") if not tx.get("to") else None,
                   method_selector=(tx.get("input") or "0x")[:10], value=fmt_pros(value),
                   gas_limit=gas_limit, gas_used=gas_used,
                   fee_paid=fmt_pros(gas_used * price))
        if h2i(rc.get("status")) == 1:
            rep.update(status="success", title="This transaction did not fail",
                       explanation="It was executed successfully; nothing needs fixing.",
                       fix="If the app shows something unexpected, the issue is off-chain - contact the app's support.")
            return rep
        rep["status"] = "failed"
        evidence: list[str] = [f"receipt status 0 in block #{block:,}",
                               f"gas used {gas_used:,} of limit {gas_limit:,} ({gas_used / max(gas_limit, 1):.1%})"]

        # 1) value larger than the sender's PROS balance right before the block
        bal = h2i(self.m.call_tool("get_balance", {"address": tx["from"], "block": block - 1})["balanceWei"])
        rep["sender_balance_before"] = fmt_pros(bal)
        if value > bal:
            evidence.append(f"tx value {fmt_pros(value)} > sender balance {fmt_pros(bal)} at block #{block - 1:,}")
            return self._finish(rep, "insufficient_native_balance", evidence)

        # 2) replay the identical call on the state of block N-1 to get the revert data
        rp = self.replay(tx, block - 1, gas_limit)
        rep["replay"] = {k: v for k, v in rp.items() if k != "data"} | {"data": (rp.get("data") or "")[:202] or None}
        out_of_gas_suspect = gas_limit > 0 and gas_used >= gas_limit * 0.99

        if rp["reverted"] and rp.get("data") is None and "funds" in (rp.get("message") or "").lower():
            evidence.append(f"replay: {rp['message']}")
            return self._finish(rep, "insufficient_native_balance", evidence)

        if out_of_gas_suspect:
            # same call with a much higher gas limit: if it now passes, the limit was the problem
            big = self.replay(tx, block - 1, min(gas_limit * 4, 30_000_000))
            rep["replay_high_gas"] = {"gas": min(gas_limit * 4, 30_000_000), "reverted": big["reverted"],
                                      "message": big.get("message")}
            if not big["reverted"]:
                evidence.append("all gas consumed; replay with 4x gas limit succeeds")
                return self._finish(rep, "out_of_gas", evidence)
            if rp["reverted"] and (rp.get("data") in (None, "0x")) and "out of gas" in (rp.get("message") or "").lower():
                evidence.append(f"all gas consumed; replay error: {rp['message']}")
                return self._finish(rep, "out_of_gas", evidence)

        if not rp["reverted"]:
            evidence.append(f"replay of the same call on block #{block - 1:,} state does not revert")
            # The tx ran mid-block: earlier txs in block N changed the state it saw. Replaying on the
            # state *after* block N usually reproduces the condition and returns the revert data.
            post = self.replay(tx, block, gas_limit)
            rep["replay_post_block"] = {k: v for k, v in post.items() if k != "data"} | {
                "data": (post.get("data") or "")[:202] or None}
            if not (post["reverted"] and post.get("data") not in (None, "0x")):
                return self._finish(rep, "state_changed", evidence)
            evidence.append(f"replay on block #{block:,} state (after the block) reverts -> most likely an earlier tx in the "
                            f"same block changed the state first")
            rp = post
            rep["changed_within_block"] = True

        dec = decode_revert(rp.get("data") or "0x", self.lookup)
        rep["decoded_revert"] = dec
        msg = rp.get("message") or ""
        evidence.append(f"replay reverted: {msg!r}" + (f", data selector {dec.get('selector')}" if dec.get("selector") else ""))
        if dec["kind"] == "empty":
            if tx.get("to"):
                code = self.m.call_tool("get_code", {"address": tx["to"], "block": block - 1})
                code_hex = code.get("code", code) if isinstance(code, dict) else code
                if code_hex in ("0x", "", None):
                    evidence.append("target has no bytecode")
                    return self._finish(rep, "no_contract", evidence)
            if "out of gas" in msg.lower():
                return self._finish(rep, "out_of_gas", evidence)
            return self._finish(rep, "silent_revert", evidence)
        if dec["kind"] == "panic":
            return self._finish(rep, "panic", evidence, detail=dec["reason"])
        text = dec.get("reason") or ""
        if dec["kind"] == "custom_error" and dec.get("signature"):
            evidence.append(f"custom error {dec['signature']} (from {dec['signature_source']})"
                            + (f" args={dec['args']}" if dec.get("args") else ""))
        kind = classify_reason(text) or ("contract_error" if text and dec.get("signature") is not None
                                          or dec["kind"] == "error_string" else "unknown")
        if kind == "unknown" and dec["kind"] == "custom_error":
            kind = "contract_error"
        return self._finish(rep, kind, evidence, detail=repr(text))

    @staticmethod
    def _finish(rep: dict, kind: str, evidence: list[str], detail: str = "") -> dict:
        title, expl, fix = TYPES[kind]
        rep.update(failure_type=kind, title=title, explanation=expl.format(detail=detail),
                   fix=fix, evidence=evidence)
        if rep.get("changed_within_block"):
            rep["explanation"] += (" Most likely another transaction in the same block got in first and changed the state "
                                   "this one relied on.")
        dec = rep.get("decoded_revert") or {}
        if dec.get("reason") and kind not in ("contract_error", "panic"):
            rep["explanation"] += f" (Contract message: {dec['reason']!r}.)"
        args = dec.get("args")
        if (dec.get("signature") or "").startswith("InsufficientOutputAmount") and args and len(args) == 3:
            rep["explanation"] += f" Numbers from the error: {args[0]:,} / {args[1]:,} / {args[2]:,} (raw units)."
        if (dec.get("signature") or "").startswith(("ERC20InsufficientAllowance", "ERC20InsufficientBalance")) and args:
            rep["explanation"] += f" Had {args[1]:,}, needed {args[2]:,} (raw token units)."
        return rep


def render(rep: dict) -> str:
    lines = [f"=== {rep['tx']}", f"    {rep['link']}"]
    if rep.get("status") in ("not_found", "pending", "success"):
        lines += [f"  Result : {rep['title']}", f"  Why    : {rep['explanation']}", f"  Fix    : {rep['fix']}"]
        return "\n".join(lines)
    target = rep["to"] or f"(contract creation -> {rep.get('contract_created')})"
    lines += [
        f"  Block  : #{rep['block']:,}   from {rep['sender']}  to {target}",
        f"  Call   : selector {rep['method_selector']}  value {rep['value']}  fee paid {rep['fee_paid']}",
        f"  Type   : {rep['failure_type']}  ->  {rep['title']}",
        f"  What happened : {rep['explanation']}",
        f"  What to do    : {rep['fix']}",
        "  Evidence:",
    ] + [f"    - {e}" for e in rep["evidence"]]
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Explain why a Pharos transaction failed (read-only, via Chain MCP).")
    ap.add_argument("tx", nargs="*", help="transaction hash(es), 0x + 64 hex chars")
    ap.add_argument("--file", help="text file with one tx hash per line")
    ap.add_argument("--json", dest="json_out", help="also write the full diagnoses to this JSON file")
    ap.add_argument("--offline-signatures", action="store_true",
                    help="do not query the Sourcify 4byte database for unknown error selectors")
    ap.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help="Chain MCP endpoint (https)")
    a = ap.parse_args(argv)
    hashes = list(a.tx)
    if a.file:
        with open(a.file, encoding="utf-8") as fh:
            hashes += [ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")]
    if not hashes:
        ap.error("give at least one transaction hash")
    bad = [h for h in hashes if not TX_RE.match(h)]
    if bad:
        ap.error(f"invalid transaction hash(es): {', '.join(bad)}")

    mcp = PharosMCP(a.endpoint)
    agent = SupportAgent(mcp, SignatureLookup(not a.offline_signatures))
    info = mcp.server_info
    print(f"Pharos dApp Support Agent · {info.get('name')} {info.get('version')} @ {a.endpoint}")
    print(f"Run at {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}\n")
    reports = []
    for h in hashes:
        try:
            rep = agent.diagnose(h)
        except Exception as e:  # report and continue with the next hash
            rep = {"tx": h, "status": "error", "error": str(e)}
            print(f"=== {h}\n  ERROR: {e}\n")
            reports.append(rep)
            continue
        reports.append(rep)
        print(render(rep) + "\n")
    failed = [r for r in reports if r.get("status") == "failed"]
    if failed:
        kinds = sorted({r["failure_type"] for r in failed})
        print(f"Summary: {len(failed)} failed tx diagnosed, {len(kinds)} distinct failure types: {', '.join(kinds)}")
    if a.json_out:
        with open(a.json_out, "w", encoding="utf-8") as fh:
            json.dump(reports, fh, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
