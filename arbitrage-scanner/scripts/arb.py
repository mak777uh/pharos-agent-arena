#!/usr/bin/env python3
"""Arbitrage Scanner — WPROS/USDC across the Uniswap-V3-style pools on Pharos mainnet (+ Chainlink reference).

    python3 scripts/arb.py scan                              # one live scan on mainnet (Chain MCP), log + decision
    python3 scripts/arb.py scan --count 3 --interval 20      # repeated scans
    python3 scripts/arb.py scan --block 20254429             # scan a past block (archive eth_call)
    python3 scripts/arb.py backscan --from 20249000 --to 20261000 --step 400
    python3 scripts/arb.py scan --simulate                   # if it acts: execute the prepared tx on a local fork
    python3 scripts/arb.py fork-scenario --whale-usdc 15000 --pool-fee 3000
                                                             # a whale buy dislocates one pool on a FORK, then scan
                                                             # the fork, prepare and simulate the arbitrage there
    add --threshold-usd / --threshold-bps to change the profit threshold, --json for machine output

Sources read in ONE scan, all pinned to the same block:
  * every WPROS/USDC pool the factory knows (fee tiers 0.01 %, 0.05 %, 0.3 %, 1 %): slot0 + liquidity, and for
    quotes the tickBitmap words and ticks() actually crossed (exact V3 math, scripts/v3math.py);
  * Chainlink PROS/USD latestRoundData (reference: sanity check of every pool price, not a tradable leg).
Reads go through the hosted Pharos Chain MCP (https://mcp.pharos.xyz/mcp); in fork mode through the local fork.

For every ordered pool pair (buy WPROS on A with USDC, sell it on B) and every size in the grid (refined around
the best), it computes: gross spread from mid prices, swap fees actually charged (per V3 step), slippage =
price impact of both legs, gas (units x base fee + tip, PROS -> USD by Chainlink), and the net result.
Decision: PREPARE when net >= --threshold-usd AND net bps >= --threshold-bps, else SKIP with the reason.
PREPARE builds ONE atomic unsigned tx: router.exactInput(path USDC -fee A-> WPROS -fee B-> USDC) with
amountOutMinimum = expected out - slippage buffer (and never below amountIn + gas), so on chain it reverts
instead of losing money on the swap legs. Never signs, never broadcasts.

Exit codes: 0 scan(s) done (any decision) · 2 bad input.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import requests  # noqa: E402
from eth_abi import decode as abi_decode, encode as abi_encode  # noqa: E402

from pay_core import Chain, check_address, to_checksum, unsigned_eip1559, CHAIN_ID  # noqa: E402
from skill_chain import addr_link, calldata, cast_send_command, decode_revert, clean_message, network, tx_link, utc  # noqa: E402
from v3math import Pool  # noqa: E402

ROOT = os.path.dirname(HERE)
EXACT_INPUT = "exactInput((bytes,address,uint256,uint256,uint256))"
WHALE = "0x15c25ed222fc498f672af5c3adb5ed0a96e1aff1"   # SharpByte USDC vault: real USDC holder, used on a FORK only
D = Decimal


# ====================================================================== data sources
class McpSrc:
    """Hosted Pharos Chain MCP (mainnet). block: int or 'latest'."""

    def __init__(self, chain: Chain):
        self.chain, self.calls = chain, 0

    def call(self, to, data, block):
        self.calls += 1
        out = self.chain.m.call_tool("eth_call", {"transaction": {"to": to, "data": data}, "block": block})
        r = out["result"] if isinstance(out, dict) else out
        return bytes.fromhex(r[2:])

    def head(self):
        return self.chain.block_number()

    def base_fee(self, block):
        b = self.chain.m.call_tool("get_block", {"block": block})
        v = b.get("baseFeePerGas")
        return int(v, 16) if isinstance(v, str) and v.startswith("0x") else int(v or 0)

    def tip(self):
        return int(self.chain.m.call_tool("get_gas_price").get("maxPriorityFeePerGasWei", 0))

    label = "Pharos Chain MCP (mainnet)"


class RpcSrc:
    """Official Pharos RPC from assets/networks.json — used only as an independent second reader to verify the pool
    state the Chain MCP returned for the same block (the hosted MCP has been seen returning a stale archive slot0)."""

    label = "official Pharos RPC"

    def __init__(self, url: str):
        self.url, self.calls = url, 0
        self.s = requests.Session()

    def call(self, to, data, block):
        self.calls += 1
        b = hex(block) if isinstance(block, int) else block
        body = {"jsonrpc": "2.0", "id": 1, "method": "eth_call", "params": [{"to": to, "data": data}, b]}
        for attempt in range(4):   # transient network errors: retry with backoff
            try:
                j = self.s.post(self.url, json=body, timeout=30).json()
                break
            except (requests.RequestException, ValueError):
                if attempt == 3:
                    raise
                time.sleep(1.5 * (attempt + 1))
        if "error" in j:
            raise RuntimeError(str(j["error"])[:200])
        return bytes.fromhex(j["result"][2:])


class Fork:
    """Local anvil fork of Pharos mainnet (automine), used for simulated execution and the fork scenario."""

    label = "local anvil fork of Pharos mainnet"

    def __init__(self, upstream: str, block: int | None = None, anvil: str | None = None):
        self.s = requests.Session()
        self.s.trust_env = False
        self.calls = 0
        binary = anvil or shutil.which("anvil") or os.path.join(os.environ.get("FOUNDRY_BIN", ""), "anvil")
        if not binary or not os.path.exists(binary):
            raise SystemExit("anvil (Foundry) not found: install Foundry or pass --anvil")
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        self.url = f"http://127.0.0.1:{port}"
        cmd = [binary, "--fork-url", upstream, "--port", str(port), "--silent"]
        if block:
            cmd += ["--fork-block-number", str(block)]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(90):
            try:
                self.rpc("eth_chainId")
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.5)
        else:
            raise SystemExit("anvil fork did not come up")
        self.fork_block = int(self.rpc("eth_blockNumber"), 16)

    def rpc(self, method, params=None):
        j = self.s.post(self.url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}, timeout=120).json()
        if "error" in j:
            e = j["error"]
            raise RuntimeError(decode_revert(e.get("data")) or clean_message(e.get("message")) or str(e))
        return j["result"]

    def call(self, to, data, block):
        self.calls += 1
        b = hex(block) if isinstance(block, int) else block
        return bytes.fromhex(self.rpc("eth_call", [{"to": to, "data": data}, b])[2:])

    def head(self):
        return int(self.rpc("eth_blockNumber"), 16)

    def base_fee(self, block):
        return int(self.rpc("eth_getBlockByNumber", [hex(block), False])["baseFeePerGas"], 16)

    def tip(self):
        return 0

    def send(self, frm, to, data, value=0, gas=None):
        tx = {"from": frm, "to": to, "data": data, "value": hex(value)}
        if gas:
            tx["gas"] = hex(gas)
        h = self.rpc("eth_sendTransaction", [tx])
        for i in range(20):
            r = self.rpc("eth_getTransactionReceipt", [h])
            if r:
                return h, r
            if i == 5:
                self.rpc("evm_mine")
            time.sleep(0.3)
        raise RuntimeError(f"no receipt for fork tx {h}")

    def close(self):
        self.proc.terminate()


def erc20_bal(src, token, who, block) -> int:
    return int.from_bytes(src.call(token, calldata("balanceOf(address)", ["address"], [who]), block)[:32], "big")


# ====================================================================== one scan
class Scanner:
    def __init__(self, cfg: dict, src, thr_usd: float | None, thr_bps: float | None, log_path: str, verifier=None):
        self.cfg, self.src, self.verifier = cfg, src, verifier
        self.usdc, self.wpros = cfg["base_token"].lower(), cfg["quote_token"].lower()
        self.thr_usd = D(str(thr_usd if thr_usd is not None else cfg["thresholds"]["min_net_profit_usd"]))
        self.thr_bps = D(str(thr_bps if thr_bps is not None else cfg["thresholds"]["min_net_profit_bps"]))
        self.log_path = log_path
        self.pools = self.discover()
        self.meta: dict[str, dict] = {}
        self.oracle_dec: int | None = None

    def discover(self) -> dict[int, str]:
        out = {}
        for fee in self.cfg["fee_tiers"]:
            d = calldata("getPool(address,address,uint24)", ["address", "address", "uint24"], [self.usdc, self.wpros, fee])
            a = "0x" + self.src.call(self.cfg["factory"], d, "latest")[-20:].hex()
            if int(a, 16):
                out[fee] = a
        return out

    def oracle(self, block) -> tuple[D, dict]:
        r = self.src.call(self.cfg["oracle"], calldata("latestRoundData()"), block)
        rid, ans, _, upd, _ = abi_decode(["uint80", "int256", "uint256", "uint256", "uint80"], r)
        if self.oracle_dec is None:
            self.oracle_dec = abi_decode(["uint8"], self.src.call(self.cfg["oracle"], calldata("decimals()"), block))[0]
        dec = self.oracle_dec
        return D(ans) / D(10) ** dec, {"updatedAt": upd, "roundId": rid}

    def load_pools(self, block) -> tuple[dict, list]:
        """Pool state at `block`; a pool whose mid price is > max_oracle_deviation_pct off Chainlink is re-read once
        (the archive node has been seen returning an inconsistent slot0) and excluded if still off."""
        px, _ = self.oracle(block)
        pools, notes = {}, []
        for fee, a in self.pools.items():
            p = Pool(self.src, a, block, self.meta.get(a))
            self.meta[a] = p.meta
            if self.verifier is not None:
                p = self.verify(p, a, block, notes)
                if p is None:
                    continue
            if p.token0.lower() != self.wpros or p.token1.lower() != self.usdc:
                raise SystemExit(f"pool {a} token order unexpected")
            mid = p.price1per0(18, 6)
            dev = (mid - px) / px * 100
            if abs(dev) > self.cfg["max_oracle_deviation_pct"]:
                p2 = Pool(self.src, a, block, self.meta[a])
                mid2 = p2.price1per0(18, 6)
                dev2 = (mid2 - px) / px * 100
                if abs(dev2) > self.cfg["max_oracle_deviation_pct"]:
                    notes.append(f"pool {fee / 10000:.2f}% excluded: mid {mid2:.6f} is {dev2:+.2f}% off Chainlink after a re-read")
                    continue
                notes.append(f"pool {fee / 10000:.2f}% first read {mid:.6f} ({dev:+.2f}% off oracle) looked inconsistent; re-read {mid2:.6f}")
                p, mid, dev = p2, mid2, dev2
            if p.liq == 0:
                notes.append(f"pool {fee / 10000:.2f}% has zero in-range liquidity")
            pools[fee] = {"pool": p, "mid": mid, "dev": dev, "addr": a}
        return {"pools": pools, "oracle": px}, notes

    def verify(self, p: Pool, a: str, block, notes: list) -> Pool | None:
        """Second, independent reader for the same block (official RPC): slot0 + liquidity must match the Chain MCP.
        On a mismatch both are re-read; an answer confirmed by two reads wins; otherwise the pool is excluded from
        this scan (no trade decision on a price nobody can confirm)."""
        v = Pool(self.verifier, a, block, self.meta[a])
        key = lambda x: (x.sqrt, x.liq)   # noqa: E731
        if key(v) == key(p):
            return p
        fee = self.meta[a]["fee"] / 10000
        p2 = Pool(self.src, a, block, self.meta[a])
        v2 = Pool(self.verifier, a, block, self.meta[a])
        first = f"first reads disagreed at block {block}: Chain MCP tick {p.tick} L {p.liq:.4e} vs official RPC tick {v.tick} L {v.liq:.4e}"
        if key(p2) == key(v2):
            notes.append(f"pool {fee:.2f}%: {first}; re-reads agree (tick {p2.tick}) -> using it")
            return p2
        if key(p2) == key(v):
            notes.append(f"pool {fee:.2f}%: {first}; Chain MCP re-read confirms the RPC (tick {p2.tick}) -> using it")
            return p2
        if key(v2) == key(p):
            notes.append(f"pool {fee:.2f}%: {first}; RPC re-read confirms the first Chain MCP answer (tick {p.tick}) -> using it")
            return p
        notes.append(f"pool {fee:.2f}%: {first}; re-reads still inconsistent -> pool excluded from this scan")
        return None

    def route(self, A: dict, B: dict, x: int, pros_usd: D, gas_price: int, gas_units: int) -> dict:
        """Buy WPROS on A with x USDC units, sell all of it on B. Every cost in USD."""
        pa, pb = A["pool"], B["pool"]
        leg1 = pa.quote_exact_in(x, zero_for_one=False)          # USDC (token1) in -> WPROS out
        w = leg1["amountOut"]
        leg2 = pb.quote_exact_in(w, zero_for_one=True) if w else {"amountOut": 0, "fee": 0, "unfilled": 0}
        y = leg2["amountOut"]
        xin = D(x) / D(10 ** 6)
        out = D(y) / D(10 ** 6)
        ideal = xin * B["mid"] / A["mid"]
        gross = ideal - xin
        fees = D(leg1["fee"]) / D(10 ** 6) + D(leg2["fee"]) / D(10 ** 18) * B["mid"]
        gas_usd = D(gas_units * gas_price) / D(10 ** 18) * pros_usd
        slip = gross - fees - (out - xin)
        net = out - xin - gas_usd
        return {"sizeUSDC": xin, "wpros": w, "outUSDC": out, "outUnits": y, "grossUSD": gross, "feesUSD": fees,
                "slippageUSD": slip, "gasUSD": gas_usd, "netUSD": net, "netBps": net / xin * 10_000 if xin else D(0),
                "unfilled": leg1["unfilled"] + leg2["unfilled"], "ticksCrossed": leg1["ticksCrossed"] + leg2.get("ticksCrossed", 0)}

    def best_size(self, A, B, cap_usdc: D, pros_usd, gas_price, gas_units) -> dict:
        sizes = [D(s) for s in self.cfg["sizes_usdc"] if D(s) <= cap_usdc] or [cap_usdc]
        res = [self.route(A, B, int(s * 10 ** 6), pros_usd, gas_price, gas_units) for s in sizes]
        i = max(range(len(res)), key=lambda k: res[k]["netUSD"])
        lo = sizes[i - 1] if i > 0 else sizes[i]
        hi = sizes[i + 1] if i + 1 < len(sizes) else sizes[i]
        best = res[i]
        for _ in range(12):   # golden-section refine between the grid neighbours (net is unimodal in size)
            if hi - lo < D("0.5"):
                break
            m1, m2 = lo + (hi - lo) * D("0.382"), lo + (hi - lo) * D("0.618")
            r1 = self.route(A, B, int(m1 * 10 ** 6), pros_usd, gas_price, gas_units)
            r2 = self.route(A, B, int(m2 * 10 ** 6), pros_usd, gas_price, gas_units)
            if r1["netUSD"] >= r2["netUSD"]:
                hi = m2
                best = max(best, r1, key=lambda r: r["netUSD"])
            else:
                lo = m1
                best = max(best, r2, key=lambda r: r["netUSD"])
        return best

    def scan(self, block="latest", wallet: str | None = None, gas_units: int | None = None) -> dict:
        t0 = time.time()
        calls0 = self.src.calls
        if block == "latest":
            block = self.src.head()
        state, notes = self.load_pools(block)
        pools, px = state["pools"], state["oracle"]
        base = self.src.base_fee(block)
        tip = self.src.tip()
        gas_price = base + tip
        gas_units = gas_units or int(self.cfg["gas_units_fallback"])
        cap = D(self.cfg["max_size_usdc"])
        wallet_usdc = None
        if wallet:
            wallet_usdc = D(erc20_bal(self.src, self.usdc, wallet, block)) / D(10 ** 6)
            cap = min(cap, wallet_usdc)
        pairs = []
        for fa, A in pools.items():
            for fb, B in pools.items():
                if fa == fb or A["pool"].liq == 0 or B["pool"].liq == 0:
                    continue
                gross_bps = (B["mid"] - A["mid"]) / A["mid"] * 10_000
                floor_bps = D(fa + fb) / 100
                entry = {"buyOn": fa, "sellOn": fb, "grossBps": gross_bps, "feeFloorBps": floor_bps}
                if gross_bps <= 0:
                    continue
                entry["_A"], entry["_B"] = A, B
                pairs.append(entry)
        # Branch and bound: fees can never be below the fee-tier floor and slippage is >= 0, so
        #   UB = size x (gross bps - floor bps) - gas   (at the size cap when the spread beats the floor)
        # is an upper bound of a pair's net. Pairs are fully quoted (tick traversal, best size) in descending UB order
        # until the next UB cannot beat the best net found; the rest are reported with their bound.
        gas_usd = D(gas_units * gas_price) / D(10 ** 18) * px
        x_min = D(self.cfg["sizes_usdc"][0])
        for e in pairs:
            margin = e["grossBps"] - e["feeFloorBps"]
            e["ub"] = (cap if margin > 0 else x_min) * margin / 10_000 - gas_usd
        pairs.sort(key=lambda e: e["ub"], reverse=True)
        best_net = None
        for e in pairs:
            A, B = e.pop("_A"), e.pop("_B")
            if best_net is None or e["ub"] > best_net:
                e["best"] = self.best_size(A, B, cap, px, gas_price, gas_units)
                best_net = e["best"]["netUSD"] if best_net is None else max(best_net, e["best"]["netUSD"])
            else:
                e["screened"] = True
                xs = cap if e["grossBps"] > e["feeFloorBps"] else x_min
                e["best"] = {"sizeUSDC": xs, "grossUSD": xs * e["grossBps"] / 10_000, "feesUSD": xs * e["feeFloorBps"] / 10_000,
                             "slippageUSD": None, "gasUSD": gas_usd, "netUSD": e["ub"], "netBps": e["ub"] / xs * 10_000,
                             "upperBound": True}
        pairs.sort(key=lambda e: (not e.get("screened"), e["best"]["netUSD"]), reverse=True)
        best = pairs[0] if pairs else None
        if not best:
            decision, reason = "SKIP", "no pool pair has a positive gross spread"
        else:
            b = best["best"]
            if b["netUSD"] >= self.thr_usd and b["netBps"] >= self.thr_bps:
                decision = "PREPARE"
                reason = f"net ${b['netUSD']:.4f} ({b['netBps']:.2f} bps) >= threshold ${self.thr_usd} and {self.thr_bps} bps"
            else:
                decision = "SKIP"
                why = []
                if best["grossBps"] <= best["feeFloorBps"]:
                    why.append(f"gross spread {best['grossBps']:.2f} bps <= swap-fee floor {best['feeFloorBps']:.2f} bps")
                elif b["netUSD"] <= 0:
                    why.append(f"spread {best['grossBps']:.2f} bps > fee floor {best['feeFloorBps']:.2f} bps but slippage + gas eat it at every size")
                if b["netUSD"] < self.thr_usd:
                    why.append(f"best net ${b['netUSD']:.4f} < threshold ${self.thr_usd}")
                if b["netBps"] < self.thr_bps:
                    why.append(f"best net {b['netBps']:.2f} bps < threshold {self.thr_bps} bps")
                reason = "; ".join(why)
        rec = {"ts": utc(), "block": block, "source": self.src.label, "oracle": px, "gasPriceWei": gas_price, "gasUnits": gas_units,
               "pools": {f"{fee / 10000:.2f}%": {"address": s["addr"], "midUSDCperWPROS": s["mid"], "oracleDevPct": s["dev"],
                                                  "inRangeLiquidity": s["pool"].liq, "tick": s["pool"].tick} for fee, s in pools.items()},
               "pairs": pairs, "best": best, "decision": decision, "reason": reason, "notes": notes,
               "walletUSDC": wallet_usdc, "sizeCapUSDC": cap, "readCalls": self.src.calls - calls0, "seconds": round(time.time() - t0, 1),
               "thresholds": {"usd": self.thr_usd, "bps": self.thr_bps}}
        self.write_log(rec)
        return rec

    def write_log(self, rec):
        os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
        slim = {"ts": rec["ts"], "block": rec["block"], "source": rec["source"], "decision": rec["decision"], "reason": rec["reason"],
                "oracle": str(rec["oracle"]), "pools": {k: str(v["midUSDCperWPROS"])[:12] for k, v in rec["pools"].items()},
                "notes": rec["notes"]}
        if rec["best"]:
            b = rec["best"]
            slim.update({"route": f"buy {b['buyOn'] / 10000:.2f}% -> sell {b['sellOn'] / 10000:.2f}%", "grossBps": f"{b['grossBps']:.2f}",
                         "sizeUSDC": f"{b['best']['sizeUSDC']:.2f}", "grossUSD": f"{b['best']['grossUSD']:.6f}",
                         "feesUSD": f"{b['best']['feesUSD']:.6f}", "slippageUSD": f"{b['best']['slippageUSD']:.6f}",
                         "gasUSD": f"{b['best']['gasUSD']:.6f}", "netUSD": f"{b['best']['netUSD']:.6f}", "netBps": f"{b['best']['netBps']:.2f}"})
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(slim) + "\n")

    # ------------------------------------------------------------------ prepare (unsigned)
    def path(self, fa, fb) -> bytes:
        u, w = bytes.fromhex(self.usdc[2:]), bytes.fromhex(self.wpros[2:])
        return u + fa.to_bytes(3, "big") + w + fb.to_bytes(3, "big") + u

    def build(self, rec, wallet) -> dict:
        b = rec["best"]
        r = b["best"]
        x = int(r["sizeUSDC"] * 10 ** 6)
        buf = int(self.cfg["slippage_buffer_bps"])
        min_out = r["outUnits"] * (10_000 - buf) // 10_000
        gas_units_usdc = int(r["gasUSD"] * 10 ** 6) + 1
        min_out = max(min_out, x + gas_units_usdc)
        data = calldata(EXACT_INPUT, ["(bytes,address,uint256,uint256,uint256)"],
                        [(self.path(b["buyOn"], b["sellOn"]), wallet, int(time.time()) + 300, x, min_out)])
        return {"amountIn": x, "amountOutMinimum": min_out, "data": data, "to": self.cfg["router"].lower()}


def prepare_unsigned(chain: Chain, wallet: str, built: dict, cfg: dict) -> dict:
    """Unsigned EIP-1559 txs for mainnet: approve (if the USDC allowance is short) + the atomic exactInput."""
    usdc, router = cfg["base_token"], cfg["router"]
    allow = int(chain.call(usdc, calldata("allowance(address,address)", ["address", "address"], [wallet, router]))[2:], 16)
    base, tip, max_fee = chain.fees()
    n = chain.nonce(wallet, "pending")
    txs = []
    if allow < built["amountIn"]:
        txs.append(("approve", usdc, calldata("approve(address,uint256)", ["address", "uint256"], [router, built["amountIn"]]), 70_000))
    txs.append(("exactInput", router, built["data"], int(cfg["gas_units_fallback"] * 1.3)))
    out = []
    for i, (name, to, data, gas) in enumerate(txs):
        t = {"chainId": CHAIN_ID, "nonce": n + i, "maxPriorityFeePerGas": tip, "maxFeePerGas": max_fee, "gas": gas,
             "to": to.lower(), "value": 0, "data": data}
        raw, h = unsigned_eip1559(t)
        out.append({"step": name, "unsignedTx": {k: (hex(v) if isinstance(v, int) else v) for k, v in t.items()},
                    "unsignedSerialized": raw, "signingHash": h, "castCommandForUser": cast_send_command(to, data),
                    "signed": False, "broadcast": False})
    return {"allowanceNow": allow, "txs": out}


def simulate_on_fork(cfg, wallet, built, block, anvil=None) -> dict:
    """Execute the prepared approve + exactInput from the (impersonated) wallet on a fork pinned at `block`."""
    f = Fork(network("mainnet")["rpcUrl"], block=block, anvil=anvil)
    try:
        return run_on_fork(f, cfg, wallet, built)
    finally:
        f.close()


def run_on_fork(f: Fork, cfg, wallet, built) -> dict:
    usdc = cfg["base_token"]
    at_block = f.head()
    f.rpc("anvil_impersonateAccount", [wallet])
    before = erc20_bal(f, usdc, wallet, "latest")
    _, ra = f.send(wallet, usdc, calldata("approve(address,uint256)", ["address", "uint256"], [cfg["router"], built["amountIn"]]))
    try:
        est = int(f.rpc("eth_estimateGas", [{"from": wallet, "to": built["to"], "data": built["data"]}]), 16)
    except RuntimeError as e:
        return {"ok": False, "forkBlock": at_block, "reason": f"estimate reverted: {e}"}
    h, r = f.send(wallet, built["to"], built["data"], gas=int(est * 1.2))
    after = erc20_bal(f, usdc, wallet, "latest")
    gas_used = int(r["gasUsed"], 16)
    price = int(r["effectiveGasPrice"], 16)
    return {"ok": int(r["status"], 16) == 1, "forkBlock": at_block, "forkTx": h, "gasUsed": gas_used, "gasEstimate": est,
            "approveGasUsed": int(ra["gasUsed"], 16), "effectiveGasPrice": price,
            "usdcBefore": before, "usdcAfter": after, "realizedUSDC": D(after - before) / D(10 ** 6),
            "gasPROS": D(gas_used * price) / D(10 ** 18)}


# ====================================================================== rendering
def render(rec, n=None) -> str:
    L = []
    hdr = f"SCAN{(' #' + str(n)) if n else ''} · block {rec['block']} · {rec['ts']} · {rec['source']} · all reads pinned to this block"
    L.append(hdr)
    L.append("  sources")
    for k, s in rec["pools"].items():
        L.append(f"    pool {k:<6} {s['address']}  1 WPROS = {s['midUSDCperWPROS']:.6f} USDC  vs oracle {s['oracleDevPct']:+.3f}%"
                 f"  in-range L {s['inRangeLiquidity']:.3e}")
    L.append(f"    Chainlink PROS/USD {rec['oracle']:.6f}  (reference only, not a tradable leg)")
    for note in rec["notes"]:
        L.append(f"    note: {note}")
    gp = rec["gasPriceWei"]
    L.append(f"  gas: {gp / 1e9:.2f} gwei x {rec['gasUnits']} units (2-hop exactInput)   size cap {rec['sizeCapUSDC']:.2f} USDC"
             + (f" (wallet holds {rec['walletUSDC']:.2f})" if rec["walletUSDC"] is not None else ""))
    if rec["pairs"]:
        L.append(f"  {'route (buy -> sell)':<22}{'gross bps':>10}{'fee floor':>10}{'best size $':>13}{'gross $':>11}{'fees $':>10}"
                 f"{'slippage $':>12}{'gas $':>10}{'net $':>11}{'net bps':>9}")
        for p in rec["pairs"]:
            b = p["best"]
            L.append(f"  {p['buyOn'] / 10000:.2f}% -> {p['sellOn'] / 10000:.2f}%{'':<8}{p['grossBps']:>10.2f}{p['feeFloorBps']:>10.2f}"
                     f"{b['sizeUSDC']:>13.2f}{b['grossUSD']:>11.4f}{b['feesUSD']:>10.4f}"
                     + (f"{b['slippageUSD']:>12.4f}" if b["slippageUSD"] is not None else f"{'(screened)':>12}")
                     + f"{b['gasUSD']:>10.6f}{b['netUSD']:>11.4f}{b['netBps']:>9.2f}" + ("  upper bound, not quoted (cannot beat the best)" if b.get("upperBound") else ""))
    L.append(f"  DECISION: {rec['decision']} — {rec['reason']}")
    L.append(f"  ({rec['readCalls']} reads, {rec['seconds']} s; logged to runs/scans.jsonl)")
    return "\n".join(L)


def jdump(o):
    return json.dumps(o, indent=2, default=lambda v: str(v) if isinstance(v, D) else str(v))


# ====================================================================== commands
def load_cfg(a):
    cfg = json.load(open(a.config, encoding="utf-8"))
    if getattr(a, "wallet", None):
        cfg["wallet"] = a.wallet
    check_address(cfg["wallet"], "wallet")
    return cfg


def act(rec, sc: Scanner, cfg, chain: Chain | None, a, fork: Fork | None = None) -> dict:
    wallet = cfg["wallet"].lower()
    built = sc.build(rec, wallet)
    out = {"built": {k: v for k, v in built.items() if k != "data"}}
    print(f"  PREPARE: atomic router.exactInput  path USDC -{rec['best']['buyOn']}-> WPROS -{rec['best']['sellOn']}-> USDC")
    print(f"    amountIn {built['amountIn'] / 1e6:.6f} USDC · amountOutMinimum {built['amountOutMinimum'] / 1e6:.6f} USDC "
          f"(expected {rec['best']['best']['outUSDC']:.6f} − {cfg['slippage_buffer_bps']} bps buffer, floor amountIn + gas) — reverts instead of losing on the legs")
    if chain is not None:
        prep = prepare_unsigned(chain, wallet, built, cfg)
        out["mainnetUnsigned"] = prep
        if fork is not None:
            print("    NOTE: this opportunity exists on the FORK only (after the simulated whale trade); the unsigned txs below use the"
                  " wallet's real mainnet nonce/fees to show the exact output format — do not sign them for mainnet")
        for t in prep["txs"]:
            u = t["unsignedTx"]
            print(f"    unsigned {t['step']:<10} nonce {int(u['nonce'], 16)} gas {int(u['gas'], 16)} to {u['to']} signingHash {t['signingHash'][:18]}…  (not signed, not sent)")
        print(f"    user would run: {prep['txs'][-1]['castCommandForUser'][:120]}…")
    if a.simulate or fork is not None:
        sim = run_on_fork(fork, cfg, wallet, built) if fork is not None else simulate_on_fork(cfg, wallet, built, rec["block"], a.anvil)
        out["simulation"] = sim
        if sim.get("ok"):
            gas_usd = sim["gasPROS"] * rec["oracle"]
            print(f"    SIMULATED EXECUTION on the fork, state of block {sim['forkBlock']} (wallet impersonated, no key): status 1, "
                  f"USDC {sim['realizedUSDC']:+.6f} (predicted {rec['best']['best']['outUSDC'] - rec['best']['best']['sizeUSDC']:+.6f}), "
                  f"gas {sim['gasUsed']} = {sim['gasPROS']:.8f} PROS ≈ ${gas_usd:.6f} → realized net ${sim['realizedUSDC'] - gas_usd:.6f}")
            print("    (fork tx hash exists only on the local fork, not on pharosscan)")
        else:
            print(f"    SIMULATED EXECUTION failed: {sim.get('reason')}")
    return out


def cmd_scan(a) -> int:
    cfg = load_cfg(a)
    chain = Chain()
    src = McpSrc(chain)
    sc = Scanner(cfg, src, a.threshold_usd, a.threshold_bps, a.log, verifier=RpcSrc(network("mainnet")["rpcUrl"]))
    print(f"Arbitrage scanner · WPROS/USDC · Pharos mainnet (chainId {chain.chain_id()}) · pools found by factory "
          f"{cfg['factory']}: " + ", ".join(f"{f / 10000:.2f}% {p}" for f, p in sc.pools.items()))
    print(f"threshold: net >= ${sc.thr_usd} and >= {sc.thr_bps} bps · wallet {to_checksum(cfg['wallet'])}\n")
    outs = []
    for i in range(a.count):
        rec = sc.scan(int(a.block) if a.block else "latest", wallet=cfg["wallet"].lower())
        print(render(rec, i + 1 if a.count > 1 else None))
        extra = act(rec, sc, cfg, chain if not a.block else None, a) if rec["decision"] == "PREPARE" else None
        outs.append({**rec, "action": extra})
        print()
        if i + 1 < a.count:
            time.sleep(a.interval)
    if a.json:
        print(jdump(outs))
    if a.json_out:
        open(a.json_out, "w").write(jdump(outs))
    return 0


def cmd_backscan(a) -> int:
    cfg = load_cfg(a)
    chain = Chain()
    sc = Scanner(cfg, McpSrc(chain), a.threshold_usd, a.threshold_bps, a.log, verifier=RpcSrc(network("mainnet")["rpcUrl"]))
    print(f"Backscan (archive eth_call through Chain MCP) blocks {a.frm}..{a.to} step {a.step} · threshold ${sc.thr_usd} / {sc.thr_bps} bps"
          f" · size cap = wallet USDC at each block\n")
    print(f"{'block':>10} {'0.01%':>9} {'0.05%':>9} {'0.30%':>9} {'1.00%':>9} {'oracle':>9}  {'best route':<16}{'gross bps':>10}{'size $':>10}{'net $':>10}  decision")
    hits = []
    for b in range(a.frm, a.to + 1, a.step):
        try:
            rec = sc.scan(b, wallet=cfg["wallet"].lower())
        except Exception as e:  # noqa: BLE001
            print(f"{b:>10} read error: {str(e)[:80]}")
            continue
        mids = {k: v["midUSDCperWPROS"] for k, v in rec["pools"].items()}
        col = " ".join(f"{float(mids[k]):>9.5f}" if k in mids else f"{'excl.':>9}" for k in ("0.01%", "0.05%", "0.30%", "1.00%"))
        best = rec["best"]
        route = f"{best['buyOn'] / 10000:.2f}->{best['sellOn'] / 10000:.2f}%" if best else "-"
        bb = best["best"] if best else None
        print(f"{b:>10} {col} {float(rec['oracle']):>9.5f}  {route:<16}{(best['grossBps'] if best else 0):>10.2f}"
              f"{(bb['sizeUSDC'] if bb else 0):>10.2f}{(bb['netUSD'] if bb else 0):>10.4f}  {rec['decision']}"
              + (f"  [{'; '.join(rec['notes'])}]" if rec["notes"] else ""))
        if rec["decision"] == "PREPARE":
            hits.append(rec)
    print(f"\n{len(hits)} block(s) above threshold out of {len(range(a.frm, a.to + 1, a.step))} scanned")
    for rec in hits[: a.simulate_top]:
        print(f"\nblock {rec['block']}: would PREPARE —")
        act(rec, sc, cfg, None, argparse.Namespace(simulate=True, anvil=a.anvil))
    return 0


def cmd_fork_scenario(a) -> int:
    cfg = load_cfg(a)
    chain = Chain()
    up = network("mainnet")["rpcUrl"]
    print(f"FORK SCENARIO (local anvil fork of Pharos mainnet; nothing here touches mainnet)")
    f = Fork(up, anvil=a.anvil)
    try:
        print(f"  forked at mainnet block {f.fork_block}: https://www.pharosscan.xyz/block/{f.fork_block}")
        sc = Scanner(cfg, f, a.threshold_usd, a.threshold_bps, a.log)
        pool = sc.pools[a.pool_fee]
        rec0 = sc.scan(f.head(), wallet=cfg["wallet"].lower())
        print("\n  BEFORE the whale trade:")
        print("  " + render(rec0).replace("\n", "\n  "))
        amt = int(D(str(a.whale_usdc)) * 10 ** 6)
        f.rpc("anvil_impersonateAccount", [WHALE])
        f.rpc("anvil_setBalance", [WHALE, hex(10 ** 18)])
        f.send(WHALE, cfg["base_token"], calldata("approve(address,uint256)", ["address", "uint256"], [cfg["router"], amt]))
        sig = "exactInputSingle((address,address,uint24,address,uint256,uint256,uint256,uint160))"
        d = calldata(sig, ["(address,address,uint24,address,uint256,uint256,uint256,uint160)"],
                     [(cfg["base_token"], cfg["quote_token"], a.pool_fee, WHALE, int(time.time()) + 600, amt, 0, 0)])
        h, r = f.send(WHALE, cfg["router"], d, gas=600_000)
        print(f"\n  WHALE TRADE on the fork: {a.whale_usdc} USDC -> WPROS on the {a.pool_fee / 10000:.2f}% pool {pool} "
              f"from {WHALE} (a real USDC holder, impersonated on the fork) · status {int(r['status'], 16)} · fork tx {h[:18]}…")
        rec = sc.scan(f.head(), wallet=cfg["wallet"].lower())
        print("\n  AFTER:")
        print("  " + render(rec).replace("\n", "\n  "))
        res = {"before": rec0, "after": rec}
        if rec["decision"] == "PREPARE":
            res["action"] = act(rec, sc, cfg, chain, a, fork=f)
        if a.json:
            print(jdump(res))
        if a.json_out:
            open(a.json_out, "w").write(jdump(res))
    finally:
        f.close()
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(s):
        s.add_argument("--config", default=os.path.join(ROOT, "assets", "arb.json"))
        s.add_argument("--wallet")
        s.add_argument("--threshold-usd", type=float)
        s.add_argument("--threshold-bps", type=float)
        s.add_argument("--log", default=os.path.join(ROOT, "runs", "scans.jsonl"))
        s.add_argument("--anvil")
        s.add_argument("--json", action="store_true")
        s.add_argument("--json-out", help="write the full machine-readable result to this file")

    s = sub.add_parser("scan")
    common(s)
    s.add_argument("--count", type=int, default=1)
    s.add_argument("--interval", type=float, default=15)
    s.add_argument("--block")
    s.add_argument("--simulate", action="store_true")
    b = sub.add_parser("backscan")
    common(b)
    b.add_argument("--from", dest="frm", type=int, required=True)
    b.add_argument("--to", type=int, required=True)
    b.add_argument("--step", type=int, default=200)
    b.add_argument("--simulate-top", type=int, default=1)
    fs = sub.add_parser("fork-scenario")
    common(fs)
    fs.add_argument("--whale-usdc", type=float, default=15000)
    fs.add_argument("--pool-fee", type=int, default=3000)
    fs.add_argument("--simulate", action="store_true", default=True)
    a = p.parse_args(argv)
    if a.cmd == "backscan" and (a.to < a.frm or a.step <= 0 or (a.to - a.frm) // a.step > 2000):
        print("error: bad block range (max 2000 scans)", file=sys.stderr)
        return 2
    try:
        return {"scan": cmd_scan, "backscan": cmd_backscan, "fork-scenario": cmd_fork_scenario}[a.cmd](a)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
