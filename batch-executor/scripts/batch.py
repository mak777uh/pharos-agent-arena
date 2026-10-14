#!/usr/bin/env python3
"""Batch Executor — bundle several independent actions into ONE prepared Multicall3 transaction.

    python3 scripts/batch.py --actions assets/payouts.json
    python3 scripts/batch.py --actions assets/payouts_with_bad.json                   # partial failure -> drop
    python3 scripts/batch.py --actions assets/payouts_with_bad.json --on-failure abort
    add --json for a machine-readable report

Actions (JSON list, >= 3):
  {"id": "pay-alice", "type": "send_pros", "to": "0x…", "amount": "0.02"}
  {"id": "ping", "type": "call", "to": "0x…contract", "data": "0x…", "value": "0"}   # msg.sender will be Multicall3
  {"id": "accrue-1", "type": "morpho_accrue", "market_id": "0x…32 bytes"}            # permissionless keeper call

Steps (all reads/simulations through the hosted Pharos Chain MCP, https://mcp.pharos.xyz/mcp):
  1. validate every action; ERC-20 transfers are refused (through Multicall3 they would need an approval to
     Multicall3, which anyone could then spend);
  2. simulate each action on its own (eth_call + estimate_gas from the sender) -> the cost of N separate txs;
  3. simulate the batch with allowFailure=true (eth_call to Multicall3.aggregate3Value) -> per-action
     success/failure inside the batch, with decoded revert reasons;
  4. partial failure: --on-failure drop (default) removes the failed actions and re-simulates the rest;
     --on-failure abort prepares nothing;
  5. the final batch is prepared with allowFailure=false for every call (all-or-nothing on chain, so PROS can
     never be left behind in Multicall3) as an UNSIGNED EIP-1559 tx; never signed or broadcast here;
  6. report gas and fee saving vs running the actions one by one, and txs/signatures/nonces saved.

Exit codes: 0 = batch prepared with every action, 6 = batch prepared without the failed actions,
4 = nothing prepared (abort, or nothing left), 2 = invalid input.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from decimal import Decimal, InvalidOperation

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from eth_abi import decode as abi_decode, encode as abi_encode  # noqa: E402

from pay_core import Chain, PaymentError, check_address, to_units, TOKENS, CHAIN_ID  # noqa: E402
from skill_chain import (MULTICALL3, MORPHO_BLUE, CHAINLINK_PROS_USD, calldata, Reader, decode_revert, prepare_tx, sel, simulate,  # noqa: E402
                         addr_link, utc)

AGG3V = "aggregate3Value((address,bool,uint256,bytes)[])"
MAX_ACTIONS = 100
ERC20_SELECTORS = {sel("transfer(address,uint256)"), sel("transferFrom(address,address,uint256)"),
                   sel("approve(address,uint256)")}


# ---------------------------------------------------------------- input
def load_actions(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    acts = raw["actions"] if isinstance(raw, dict) else raw
    if not isinstance(acts, list) or len(acts) < 3:
        raise ValueError("need a list of at least 3 actions")
    if len(acts) > MAX_ACTIONS:
        raise ValueError(f"at most {MAX_ACTIONS} actions per batch")
    out, ids = [], set()
    for i, a in enumerate(acts, 1):
        aid = str(a.get("id") or f"action-{i}")[:40]
        if aid in ids:
            raise ValueError(f"duplicate action id {aid!r}")
        ids.add(aid)
        t = a.get("type")
        try:
            if t == "send_pros":
                amt = Decimal(str(a["amount"]))
                if amt <= 0:
                    raise ValueError
                out.append({"id": aid, "type": t, "to": check_address(a["to"], f"{aid}.to"), "value": to_units(amt, 18),
                            "data": "0x", "label": f"send {amt} PROS"})
            elif t == "call":
                to = check_address(a["to"], f"{aid}.to")
                data = str(a.get("data", "0x")).lower()
                if not data.startswith("0x") or len(data) % 2 or any(c not in "0123456789abcdef" for c in data[2:]):
                    raise ValueError(f"{aid}: data must be 0x-prefixed hex")
                if to in TOKENS.values() and data[2:10] in ERC20_SELECTORS:
                    raise ValueError(f"{aid}: ERC-20 transfer/approve cannot be batched through Multicall3 - the tokens "
                                     "would have to be approved to Multicall3, which anyone can call. Use a distributor "
                                     "contract (pharos-skill-engine assets/airdrop) or separate transactions.")
                val = Decimal(str(a.get("value", "0")))
                if val < 0:
                    raise ValueError
                out.append({"id": aid, "type": t, "to": to, "value": to_units(val, 18), "data": data,
                            "label": f"call 0x{data[2:10]}" + (f" + {val} PROS" if val else "")})
            elif t == "morpho_accrue":
                mid = str(a["market_id"]).lower()
                if not (mid.startswith("0x") and len(mid) == 66 and all(c in "0123456789abcdef" for c in mid[2:])):
                    raise ValueError(f"{aid}: market_id must be a 0x-prefixed 32-byte hex id")
                out.append({"id": aid, "type": t, "to": MORPHO_BLUE, "value": 0, "market_id": mid, "data": None,
                            "label": f"accrueInterest {mid[:10]}…"})
            else:
                raise ValueError(f"{aid}: unknown type {t!r} (send_pros | call | morpho_accrue)")
        except (KeyError, InvalidOperation):
            raise ValueError(f"{aid}: missing or invalid field")
        except PaymentError as e:
            raise ValueError(str(e))
    return out


# ---------------------------------------------------------------- multicall
def encode_batch(actions: list[dict], allow_failure: bool) -> tuple[str, int]:
    calls = [(a["to"], allow_failure, a["value"], bytes.fromhex(a["data"][2:])) for a in actions]
    data = "0x" + sel(AGG3V) + abi_encode(["(address,bool,uint256,bytes)[]"], [calls]).hex()
    return data, sum(a["value"] for a in actions)


def simulate_batch(chain: Chain, sender: str, actions: list[dict]) -> tuple[list[dict], dict]:
    """allowFailure=true eth_call: which actions fail INSIDE the batch."""
    data, total = encode_batch(actions, True)
    err, text = chain.m.call_tool_raw("eth_call", {"transaction": {"from": sender, "to": MULTICALL3, "data": data,
                                                                   "value": hex(total)}, "block": "latest"})
    p = json.loads(text)
    if err:
        e = p.get("error", {})
        return [], {"ok": False, "reason": decode_revert(e.get("data")) or e.get("message")}
    res = abi_decode(["(bool,bytes)[]"], bytes.fromhex(p["result"][2:]))[0]
    rows = [{"id": a["id"], "success": ok, "reason": None if ok else (decode_revert("0x" + rd.hex()) or
                                                                      "reverted without a reason (empty revert data)")}
            for a, (ok, rd) in zip(actions, res)]
    return rows, {"ok": True}


def run(path: str, sender: str, on_failure: str, chain: Chain) -> dict:
    actions = load_actions(path)
    sender = check_address(sender, "sender")
    if int(chain.m.call_tool("get_chain_info")["chainId"]) != CHAIN_ID:
        raise RuntimeError("Chain MCP is not on Pharos mainnet")
    if chain.code(MULTICALL3) in ("0x", ""):
        raise RuntimeError("Multicall3 has no code on this chain")
    head = chain.block_number()
    rd = Reader(chain)
    for a in actions:  # Morpho Blue accrueInterest(MarketParams): params read on chain from idToMarketParams(id)
        if a["type"] == "morpho_accrue":
            p = abi_decode(["address", "address", "address", "address", "uint256"],
                           rd.call(MORPHO_BLUE, "idToMarketParams(bytes32)", ["bytes32"], [bytes.fromhex(a["market_id"][2:])]))
            a["data"] = calldata("accrueInterest((address,address,address,address,uint256))",
                                 ["(address,address,address,address,uint256)"], [p])
            a["marketParams"] = {"loanToken": p[0].lower(), "collateralToken": p[1].lower(), "lltv": str(p[4])}
    gas_price = int(chain.m.call_tool("get_gas_price")["gasPriceWei"])
    usd, _ = Reader(chain).oracle(CHAINLINK_PROS_USD)
    rep = {"block": head, "time": utc(), "sender": sender, "multicall3": MULTICALL3, "onFailure": on_failure,
           "gasPriceWei": gas_price, "prosUsd": str(usd), "actions": []}

    # 2. each action alone
    for a in actions:
        s = simulate(chain, {"from": sender, "to": a["to"], "data": a["data"], "value": a["value"]})
        a["solo"] = {"ok": s["ok"], "gas": s["gas"], "reason": s["revertReason"]}
    # 3. the batch with allowFailure=true
    inbatch, meta = simulate_batch(chain, sender, actions)
    if not meta["ok"]:
        raise RuntimeError(f"the batch itself reverts: {meta['reason']}")
    for a, b in zip(actions, inbatch):
        a["inBatch"] = b
        rep["actions"].append({k: a[k] for k in ("id", "type", "to", "value", "label", "solo", "inBatch")})
    failed = [a for a in actions if not a["inBatch"]["success"]]
    keep = [a for a in actions if a["inBatch"]["success"]]
    rep["failed"] = [{"id": a["id"], "reason": a["inBatch"]["reason"]} for a in failed]

    # 4. partial failure policy
    if failed and on_failure == "abort":
        rep["outcome"] = "ABORTED"
        rep["note"] = (f"{len(failed)} action(s) fail in the batch; --on-failure abort -> nothing prepared, "
                       f"the other {len(keep)} actions were not executed either")
        return rep
    if len(keep) == 0:
        rep["outcome"] = "NOTHING_LEFT"
        rep["note"] = "every action fails - nothing prepared"
        return rep

    # 5. final all-or-nothing batch with the remaining actions
    data, total = encode_batch(keep, False)
    final = simulate(chain, {"from": sender, "to": MULTICALL3, "data": data, "value": total})
    if not final["ok"]:
        rep["outcome"] = "ABORTED"
        rep["note"] = f"re-simulation of the remaining batch reverted: {final['revertReason']}"
        return rep
    gas_limit = int(final["gas"] * 1.2)
    rep["prepared"] = prepare_tx(chain, sender, MULTICALL3, data, total, gas=gas_limit)
    rep["batch"] = {"actions": [a["id"] for a in keep], "totalValuePROS": str(Decimal(total) / 10 ** 18),
                    "estimateGas": final["gas"], "gasLimit": gas_limit}
    # 6. savings (only over the actions that are actually in the batch)
    solo_gas = sum(a["solo"]["gas"] or 0 for a in keep)
    rep["saving"] = {"individualTxs": len(keep), "batchTxs": 1, "signaturesSaved": len(keep) - 1,
                     "individualGas": solo_gas, "batchGas": final["gas"], "gasSaved": solo_gas - final["gas"],
                     "gasSavedPct": str((Decimal(solo_gas - final["gas"]) / solo_gas * 100).quantize(Decimal("0.1"))),
                     "individualFeePROS": str(Decimal(solo_gas * gas_price) / 10 ** 18),
                     "batchFeePROS": str(Decimal(final["gas"] * gas_price) / 10 ** 18),
                     "feeSavedUSD": str((Decimal((solo_gas - final["gas"]) * gas_price) / 10 ** 18 * usd).quantize(Decimal("0.000001")))}
    native = chain.native_balance(sender)
    need = total + gas_limit * int(rep["prepared"]["unsignedTx"]["maxFeePerGas"], 16)
    rep["senderBalanceOK"] = native >= need
    rep["outcome"] = "PARTIAL" if failed else "ALL"
    if not rep["senderBalanceOK"]:
        rep["outcome"] = "INSUFFICIENT_PROS"
        rep["note"] = f"sender has {Decimal(native) / 10 ** 18} PROS, batch needs up to {Decimal(need) / 10 ** 18}"
        rep.pop("prepared")
    return rep


def render(rep: dict) -> str:
    L = [f"Batch Executor · sender {rep['sender']} · Pharos mainnet block #{rep['block']:,} ({rep['time']})",
         f"Multicall3 {rep['multicall3']} aggregate3Value · on-failure: {rep['onFailure']}",
         "", f"{'#':>2} {'id':<20} {'action':<24} {'to':<44} {'alone (eth_call)':<28} in batch (allowFailure=true)"]
    for i, a in enumerate(rep["actions"], 1):
        solo = f"ok, {a['solo']['gas']:,} gas" if a["solo"]["ok"] else f"REVERT: {a['solo']['reason']}"[:27]
        inb = "ok" if a["inBatch"]["success"] else f"FAILED: {a['inBatch']['reason']}"
        L.append(f"{i:>2} {a['id']:<20} {a['label']:<24} {a['to']:<44} {solo:<28} {inb}")
    L.append("")
    for f in rep.get("failed", []):
        L.append(f"partial failure: action '{f['id']}' fails ({f['reason']})")
    if rep["outcome"] in ("ABORTED", "NOTHING_LEFT", "INSUFFICIENT_PROS"):
        L.append(f"OUTCOME: {rep['outcome']} - {rep['note']}")
        return "\n".join(L)
    b, s, pr = rep["batch"], rep["saving"], rep["prepared"]
    if rep["failed"]:
        L.append(f"  -> dropped from the batch; the remaining {len(b['actions'])} actions were re-simulated "
                 f"together and go ahead: {', '.join(b['actions'])}")
    L += [f"OUTCOME: {rep['outcome']} - 1 unsigned transaction bundles {len(b['actions'])} actions "
          f"(total value {b['totalValuePROS']} PROS, allowFailure=false: all-or-nothing on chain)",
          "",
          "Saving vs one transaction per action:",
          f"  transactions : {s['individualTxs']} -> 1   (signatures/nonces saved: {s['signaturesSaved']})",
          (f"  gas          : {s['individualGas']:,} -> {s['batchGas']:,}   (saved {s['gasSaved']:,} = {s['gasSavedPct']}%)"
           if s["gasSaved"] >= 0 else
           f"  gas          : {s['individualGas']:,} -> {s['batchGas']:,}   (batch costs {-s['gasSaved']:,} MORE gas = "
           f"{s['gasSavedPct'].lstrip('-')}%: each value transfer through a contract costs ~9,000 gas, while a plain "
           "transfer has no execution cost beyond its 21,000 base)"),
          f"  fee @ {Decimal(rep['gasPriceWei']) / 10 ** 9} gwei: {s['individualFeePROS']} PROS -> {s['batchFeePROS']} PROS "
          f"({'saved' if s['gasSaved'] >= 0 else 'extra'} ~${s['feeSavedUSD'].lstrip('-')} at PROS/USD {Decimal(rep['prosUsd']):.4f})",
          "",
          f"Prepared UNSIGNED tx to Multicall3: nonce {int(pr['unsignedTx']['nonce'], 16)}, gas limit {b['gasLimit']:,}, "
          f"value {b['totalValuePROS']} PROS, signing hash {pr['signingHash']}",
          f"  calldata: {pr['unsignedTx']['data'][:74]}… ({(len(pr['unsignedTx']['data']) - 2) // 2} bytes)",
          f"  sign it yourself: {pr['castCommandForUser'][:120]}…",
          "  (never signed or broadcast by this skill)",
          f"  Multicall3: {addr_link(rep['multicall3'])}"]
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--actions", required=True, help="JSON file: list of actions or {sender, actions}")
    ap.add_argument("--from", dest="sender", help="signer address (default: 'sender' in the actions file)")
    ap.add_argument("--on-failure", choices=["drop", "abort"], default="drop")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--endpoint", default=None)
    a = ap.parse_args(argv)
    try:
        sender = a.sender
        if not sender:
            with open(a.actions, encoding="utf-8") as f:
                j = json.load(f)
            sender = j.get("sender") if isinstance(j, dict) else None
        if not sender:
            raise ValueError("no sender: pass --from or put 'sender' in the actions file")
        rep = run(a.actions, sender, a.on_failure, Chain(a.endpoint))
    except (ValueError, OSError, RuntimeError, json.JSONDecodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(json.dumps(rep, indent=2, default=str) if a.json else render(rep))
    return {"ALL": 0, "PARTIAL": 6}.get(rep["outcome"], 4)


if __name__ == "__main__":
    sys.exit(main())
