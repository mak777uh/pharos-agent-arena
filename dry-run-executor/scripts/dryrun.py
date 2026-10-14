#!/usr/bin/env python3
"""Dry-Run Executor — simulate a Pharos mainnet transaction before anyone signs it.

    python3 scripts/dryrun.py transfer --from 0x… --to 0x… --token USDC --amount 100
    python3 scripts/dryrun.py swap     --from 0x… --sell USDC --amount 10 [--min-out 17]
    python3 scripts/dryrun.py wrap     --from 0x… --amount 1          # PROS -> WPROS (deposit)
    python3 scripts/dryrun.py unwrap   --from 0x… --amount 1          # WPROS -> PROS (withdraw)
    python3 scripts/dryrun.py raw      --from 0x… --to 0x… --data 0x… [--value-wei N]
    add --json for a machine-readable report

1. SIMULATE  through the hosted Pharos Chain MCP (https://mcp.pharos.xyz/mcp): `eth_call` (success / revert +
   decoded reason) and `estimate_gas`; fees from `get_gas_price` + latest block base fee; PROS/USD from Chainlink.
2. PREDICT   what changes: `debug_traceCall` (callTracer with logs) on the official Pharos RPC from
   assets/networks.json returns the events the tx WOULD emit; ERC-20 Transfer / WPROS Deposit+Withdrawal and
   native value transfers become balance deltas, applied to balances read through Chain MCP at the same head.
3. GATE      success -> GO: an UNSIGNED EIP-1559 tx is prepared for the user to sign (never signed or sent here).
             revert, or not enough PROS for value + max fee -> BLOCKED: nothing is prepared, exit code 4.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from eth_abi import decode as abi_decode  # noqa: E402

from pay_core import Chain, PaymentError, check_address, to_units, TOKENS, CHAIN_ID  # noqa: E402
from skill_chain import (Reader, CHAINLINK_PROS_USD, calldata, prepare_tx, simulate, trace_call, trace_logs,  # noqa: E402
                         addr_link, network, utc)

ROUTER = "0xf38d34c8382b9079b0f85309578b43b8479cd875"   # SwapRouter (exactInputSingle) used by WPROS/USDC swaps
POOL_FEE = 100
WPROS = TOKENS["WPROS"]
T_TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
T_APPROVAL = "0x8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925"
T_DEPOSIT = "0xe1fffcc4923d04b559f4d29a8bfc6cda04eb5b0d3c460751c2402c5c5cc9109c"
T_WITHDRAW = "0x7fcf532c15f0a6db0bd6d0e038bea71d30d808c7d98cb3bf7268a95bf5081b65"
T_SWAP_V3 = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
NATIVE = "PROS"


# ---------------------------------------------------------------- building the tx to simulate
def build(a, r: Reader) -> tuple[dict, str]:
    frm = check_address(a.sender, "from")
    if a.cmd == "raw":
        to = check_address(a.to, "to")
        data = a.data or "0x"
        if not data.startswith("0x") or len(data) % 2 or any(ch not in "0123456789abcdefABCDEF" for ch in data[2:]):
            raise ValueError("--data must be 0x-prefixed hex")
        if a.value_wei < 0:
            raise ValueError("--value-wei must be >= 0")
        return {"from": frm, "to": to, "data": data.lower(), "value": a.value_wei}, f"raw call to {to}"
    amount = Decimal(a.amount)
    if amount <= 0:
        raise ValueError("--amount must be positive")
    if a.cmd == "transfer":
        to = check_address(a.to, "to")
        sym = a.token.upper()
        if sym == NATIVE:
            return {"from": frm, "to": to, "data": "0x", "value": to_units(amount, 18)}, f"send {amount} PROS to {to}"
        tok = TOKENS.get(sym) or check_address(a.token, "token")
        units = to_units(amount, r.decimals(tok))
        return ({"from": frm, "to": tok, "data": calldata("transfer(address,uint256)", ["address", "uint256"], [to, units]),
                 "value": 0}, f"transfer {amount} {r.symbol(tok)} to {to}")
    if a.cmd == "wrap":
        return {"from": frm, "to": WPROS, "data": calldata("deposit()"), "value": to_units(amount, 18)}, f"wrap {amount} PROS"
    if a.cmd == "unwrap":
        return ({"from": frm, "to": WPROS, "data": calldata("withdraw(uint256)", ["uint256"], [to_units(amount, 18)]),
                 "value": 0}, f"unwrap {amount} WPROS")
    if a.cmd == "swap":
        sell = a.sell.upper()
        if sell not in ("USDC", "WPROS"):
            raise ValueError("--sell must be USDC or WPROS")
        t_in, t_out = (TOKENS["USDC"], WPROS) if sell == "USDC" else (WPROS, TOKENS["USDC"])
        units = to_units(amount, r.decimals(t_in))
        min_out = to_units(Decimal(a.min_out), r.decimals(t_out)) if a.min_out else 0
        deadline = int(time.time()) + 600
        sig = "exactInputSingle((address,address,uint24,address,uint256,uint256,uint256,uint160))"
        data = calldata(sig, ["(address,address,uint24,address,uint256,uint256,uint256,uint160)"],
                        [(t_in, t_out, POOL_FEE, frm, deadline, units, min_out, 0)])
        return ({"from": frm, "to": ROUTER, "data": data, "value": 0},
                f"swap {amount} {sell} -> {'WPROS' if sell == 'USDC' else 'USDC'} via router {ROUTER} "
                f"(pool fee {POOL_FEE / 10000}%, amountOutMinimum {a.min_out or 0}, deadline +10 min)")
    raise ValueError(a.cmd)


# ---------------------------------------------------------------- predicting the effect
def native_moves(frame: dict, out: list) -> None:
    if frame.get("error"):
        return
    v = int(frame.get("value", "0x0") or "0x0", 16)
    if v and frame.get("type") in ("CALL", None):
        out.append((frame["from"].lower(), frame["to"].lower(), v))
    for c in frame.get("calls", []) or []:
        native_moves(c, out)


def predict(tx: dict, r: Reader) -> dict:
    tr = trace_call(tx)
    if tr is None:
        return {"available": False, "note": "debug_traceCall not available - no state prediction"}
    events, deltas = [], {}

    def add(token, who, amt):
        deltas[(token, who)] = deltas.get((token, who), 0) + amt

    for lg in trace_logs(tr):
        t = [x.lower() for x in lg.get("topics", [])]
        addr = lg["address"].lower()
        data = bytes.fromhex(lg.get("data", "0x")[2:])
        if not t:
            continue
        if t[0] == T_TRANSFER and len(t) == 3 and len(data) == 32:
            f, to, v = "0x" + t[1][-40:], "0x" + t[2][-40:], int.from_bytes(data, "big")
            add(addr, f, -v)
            add(addr, to, v)
            events.append(("Transfer", addr, f"{f} -> {to}", v))
        elif t[0] == T_APPROVAL and len(t) == 3:
            events.append(("Approval", addr, f"owner {'0x' + t[1][-40:]} spender {'0x' + t[2][-40:]}", int.from_bytes(data, "big")))
        elif t[0] == T_DEPOSIT and len(t) == 2:
            who, v = "0x" + t[1][-40:], int.from_bytes(data, "big")
            add(addr, who, v)
            events.append(("Deposit (mint)", addr, who, v))
        elif t[0] == T_WITHDRAW and len(t) == 2:
            who, v = "0x" + t[1][-40:], int.from_bytes(data, "big")
            add(addr, who, -v)
            events.append(("Withdrawal (burn)", addr, who, v))
        elif t[0] == T_SWAP_V3:
            a0, a1, sp, liq, tick = abi_decode(["int256", "int256", "uint160", "uint128", "int24"], data)
            events.append(("Swap", addr, f"amount0 {a0} amount1 {a1} tick-> {tick}", None))
        else:
            events.append(("other", addr, t[0][:10], None))
    moves: list = []
    native_moves(tr, moves)
    for f, to, v in moves:
        add(NATIVE, f, -v)
        add(NATIVE, to, v)
    return {"available": True, "events": events, "deltas": deltas, "gasUsedTrace": int(tr.get("gasUsed", "0x0"), 16)}


def token_meta(r: Reader, token: str) -> tuple[str, int]:
    if token == NATIVE:
        return NATIVE, 18
    try:
        return r.symbol(token), r.decimals(token)
    except Exception:  # noqa: BLE001
        return token[:10] + "…", 0


def balances(r: Reader, deltas: dict, head: int) -> list[dict]:
    rows = []
    for (token, who), d in sorted(deltas.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        if d == 0:
            continue
        sym, dec = token_meta(r, token)
        if token == NATIVE:
            before = r.c.native_balance(who)
        else:
            before = int.from_bytes(r.call(token, "balanceOf(address)", ["address"], [who], block=head)[:32], "big")
        sc = Decimal(10) ** dec
        rows.append({"account": who, "token": sym, "before": Decimal(before) / sc, "delta": Decimal(d) / sc,
                     "after": Decimal(before + d) / sc})
    return rows


# ---------------------------------------------------------------- report
def dry_run(tx: dict, label: str, chain: Chain, prepare: bool = True) -> dict:
    r = Reader(chain)
    info = chain.m.call_tool("get_chain_info")
    if int(info["chainId"]) != CHAIN_ID:
        raise RuntimeError(f"Chain MCP is on chainId {info['chainId']}, expected {CHAIN_ID}")
    head = chain.block_number()
    code = chain.code(tx["to"])
    sim = simulate(chain, tx)
    base, tip, max_fee = chain.fees()
    gas_price = int(chain.m.call_tool("get_gas_price")["gasPriceWei"])
    usd, _ = r.oracle(CHAINLINK_PROS_USD)
    rep = {"label": label, "network": "Pharos mainnet", "chainId": CHAIN_ID, "block": head, "time": utc(),
           "tx": {k: (hex(v) if isinstance(v, int) else v) for k, v in tx.items()},
           "toIsContract": code not in ("0x", ""), "simulation": sim, "prosUsd": str(usd)}
    sender_native = chain.native_balance(tx["from"])
    reasons = []
    if sim["ok"]:
        gas_limit = int(sim["gas"] * 1.2)
        exp_fee, max_cost = sim["gas"] * gas_price, gas_limit * max_fee
        rep["cost"] = {"estimateGas": sim["gas"], "gasLimit": gas_limit, "gasPriceGwei": str(Decimal(gas_price) / 10 ** 9),
                       "maxFeePerGasGwei": str(Decimal(max_fee) / 10 ** 9),
                       "expectedFeePROS": str(Decimal(exp_fee) / 10 ** 18),
                       "expectedFeeUSD": str((Decimal(exp_fee) / 10 ** 18 * usd).quantize(Decimal("0.000001"))),
                       "maxFeePROS": str(Decimal(max_cost) / 10 ** 18)}
        if sender_native < tx["value"] + max_cost:
            reasons.append(f"sender has {Decimal(sender_native) / 10 ** 18} PROS, needs value + max fee "
                           f"{Decimal(tx['value'] + max_cost) / 10 ** 18} PROS")
    else:
        reasons.append(f"simulation reverted: {sim['revertReason'] or 'no reason returned'}")
    pred = predict(tx, r) if sim["ok"] else {"available": False, "note": "reverting tx changes nothing (except gas if sent)"}
    if pred.get("available"):
        if sim["ok"]:  # the sender also pays the network fee
            d = pred["deltas"]
            d[(NATIVE, tx["from"])] = d.get((NATIVE, tx["from"]), 0) - rep["cost"]["estimateGas"] * gas_price
        pred["balances"] = balances(r, pred["deltas"], head)
        pred.pop("deltas")
    rep["prediction"] = pred
    rep["decision"] = "GO" if not reasons else "BLOCKED"
    rep["blockedBecause"] = reasons
    if rep["decision"] == "GO" and prepare:
        rep["prepared"] = prepare_tx(chain, tx["from"], tx["to"], tx["data"], tx["value"], gas=rep["cost"]["gasLimit"])
    return rep


def fmt(x: Decimal) -> str:
    s = f"{x:,.6f}".rstrip("0").rstrip(".")
    return s if s not in ("-0", "") else "0"


def render(rep: dict, r: Reader | None = None) -> str:
    s, t = rep["simulation"], rep["tx"]
    L = [f"Dry run · {rep['label']}",
         f"  network {rep['network']} (chainId {rep['chainId']}), head block #{rep['block']:,}, {rep['time']}",
         f"  from {t['from']} -> to {t['to']} ({'contract' if rep['toIsContract'] else 'EOA'}), value {int(t['value'], 16)} wei, "
         f"data {t['data'][:10]}{'…' if len(t['data']) > 10 else ''} ({(len(t['data']) - 2) // 2} bytes)",
         "[1] Simulation (Chain MCP eth_call + estimate_gas, nothing broadcast)"]
    if s["ok"]:
        c = rep["cost"]
        L += [f"  result : SUCCESS, return data {str(s['returnData'])[:66]}",
              f"  gas    : estimate {c['estimateGas']:,} -> limit {c['gasLimit']:,} (+20%); gas price {c['gasPriceGwei']} gwei, "
              f"maxFeePerGas {c['maxFeePerGasGwei']} gwei",
              f"  cost   : expected {c['expectedFeePROS']} PROS (~${c['expectedFeeUSD']} at Chainlink PROS/USD {Decimal(rep['prosUsd']):.4f}); "
              f"worst case {c['maxFeePROS']} PROS"]
    else:
        L += [f"  result : REVERT - {s['revertReason'] or 'no reason returned'}",
              f"  revert data: {s['revertData'] or '0x'}"]
    p = rep["prediction"]
    L.append("[2] Expected outcome (debug_traceCall on " + network("mainnet")["rpcUrl"] + " + balances via Chain MCP)")
    if p.get("available"):
        for name, addr, what, v in p["events"]:
            L.append(f"  event  : {name:<18} {addr} {what}" + (f" value {v}" if v is not None else ""))
        L.append(f"  {'account':<44} {'token':<6} {'before':>20} {'change':>18} {'after':>20}")
        for b in p["balances"]:
            L.append(f"  {b['account']:<44} {b['token']:<6} {fmt(b['before']):>20} {('+' if b['delta'] > 0 else '') + fmt(b['delta']):>18} "
                     f"{fmt(b['after']):>20}")
        L.append("  (native PROS change of the sender includes the expected network fee)")
    else:
        L.append(f"  {p.get('note')}")
    L.append(f"[3] Decision: {rep['decision']}")
    for why in rep["blockedBecause"]:
        L.append(f"  blocked: {why}")
    if rep["decision"] == "GO" and rep.get("prepared"):
        pr = rep["prepared"]
        L += [f"  prepared UNSIGNED EIP-1559 tx: nonce {int(pr['unsignedTx']['nonce'], 16)}, gas {int(pr['unsignedTx']['gas'], 16):,}, "
              f"signing hash {pr['signingHash']}",
              f"  wallet request: {json.dumps(pr['walletRequest'])}",
              f"  or sign yourself with Foundry: {pr['castCommandForUser']}",
              "  (this skill never signs or broadcasts)"]
    elif rep["decision"] == "BLOCKED":
        L.append("  execution blocked automatically - no transaction was prepared")
    L.append(f"  sender: {addr_link(t['from'])}")
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-prepare", action="store_true", help="simulate only, do not build the unsigned tx on GO")
    ap.add_argument("--endpoint", default=None, help="Chain MCP endpoint (https only)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("transfer", "swap", "wrap", "unwrap", "raw"):
        s = sub.add_parser(name)
        s.add_argument("--from", dest="sender", required=True)
        if name in ("transfer", "raw"):
            s.add_argument("--to", required=True)
        if name == "transfer":
            s.add_argument("--token", required=True, help="PROS, USDC, WPROS, LINK, WETH or an ERC-20 address")
        if name == "swap":
            s.add_argument("--sell", required=True, help="USDC or WPROS")
            s.add_argument("--min-out", default=None, help="amountOutMinimum in output-token units")
        if name == "raw":
            s.add_argument("--data", default="0x")
            s.add_argument("--value-wei", type=int, default=0)
        else:
            s.add_argument("--amount", required=True)
    a = ap.parse_args(argv)
    try:
        chain = Chain(a.endpoint)
        tx, label = build(a, Reader(chain))
        rep = dry_run(tx, label, chain, prepare=not a.no_prepare)
    except (ValueError, PaymentError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(json.dumps(rep, indent=2, default=str) if a.json else render(rep))
    return 0 if rep["decision"] == "GO" else 4


if __name__ == "__main__":
    sys.exit(main())
