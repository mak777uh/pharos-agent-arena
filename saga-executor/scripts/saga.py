#!/usr/bin/env python3
"""Saga Executor — approve -> swap -> approve -> deposit on Pharos mainnet state, with retry and compensation.

    python3 scripts/saga.py run                                   # happy path (assets/saga.json)
    python3 scripts/saga.py run --inject drop@swap                # dropped tx -> retried with the same nonce
    python3 scripts/saga.py run --inject spike@approve_usdc       # base-fee spike, tx stuck -> fee-bumped replacement
    python3 scripts/saga.py run --inject frontrun@swap            # competing swap lands first -> "Too little received"
                                                                  #   (mined, status 0) -> re-quote and retry
    python3 scripts/saga.py run --inject revert@deposit           # permanent failure -> compensation (unwind swap)
    python3 scripts/saga.py run --inject spike-persist@deposit    # fee above cap, retries exhausted -> cancel the
                                                                  #   stuck nonce, then compensation
    python3 scripts/saga.py status --log runs/<saga_id>.jsonl     # which step the saga is in, and its history
    python3 scripts/saga.py verify-receipt --plan <plan.json> --step approve_usdc --tx 0x…   # mainnet, Chain MCP

Where it runs (honest):
  * Pharos MAINNET is only READ, through the hosted Pharos Chain MCP (https://mcp.pharos.xyz/mcp): chain id, contract
    code, vault asset(), pool tokens, balances, pending nonce, fees, pool price, Chainlink PROS/USD, and a real
    eth_call + estimate_gas of step 1. The output is a set of UNSIGNED EIP-1559 transactions for the user to sign.
  * The saga itself (send -> wait for the receipt -> verify -> next step, retries, compensation) is EXECUTED on a
    local Foundry `anvil` fork of Pharos mainnet (forked from https://rpc.pharos.xyz, the endpoint in
    assets/networks.json) with the public sender address impersonated. Real contracts, real mainnet state, real
    receipts — but on a private copy of the chain: nothing is signed with a key and nothing reaches mainnet.
    Fork tx hashes therefore do NOT exist on pharosscan.
  * Compensation transactions are simulated (eth_call + estimate_gas) against the stranded fork state, prepared as
    unsigned mainnet-format txs, and then executed on the fork to prove the funds really come back
    (--no-execute-compensation stops after simulate+prepare).

Exit codes: 0 completed · 5 failed and compensated · 4 failed, compensation incomplete (manual action) · 2 bad input.
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
import uuid
from decimal import Decimal, ROUND_DOWN

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import requests  # noqa: E402
from eth_abi import decode as abi_decode  # noqa: E402

from pay_core import Chain, check_address, keccak256, to_checksum, to_units, unsigned_eip1559, CHAIN_ID  # noqa: E402
from skill_chain import (CHAINLINK_PROS_USD, Reader, addr_link, calldata, cast_send_command, clean_message,  # noqa: E402
                         decode_revert, network, prepare_tx, simulate, tx_link, utc)

ROOT = os.path.dirname(HERE)
T_TRANSFER = "0x" + keccak256(b"Transfer(address,address,uint256)").hex()
T_APPROVAL = "0x" + keccak256(b"Approval(address,address,uint256)").hex()
T_DEPOSIT4626 = "0x" + keccak256(b"Deposit(address,address,uint256,uint256)").hex()
SWAP_SIG = "exactInputSingle((address,address,uint24,address,uint256,uint256,uint256,uint160))"
SWAP_T = ["(address,address,uint24,address,uint256,uint256,uint256,uint160)"]
MAX_UINT = 2 ** 256 - 1
FRONTRUNNER = "0x15c25ed222fc498f672af5c3adb5ed0a96e1aff1"   # SharpByte USDC vault: a real USDC holder on mainnet
INJECT_KINDS = {"drop", "spike", "spike-persist", "frontrun", "revert"}
STEP_NAMES = ["approve_usdc", "swap", "approve_wpros", "deposit"]


class RPCError(RuntimeError):
    def __init__(self, err: dict):
        self.err = err or {}
        super().__init__(str(err)[:300])

    @property
    def reason(self) -> str:
        return decode_revert(self.err.get("data")) or clean_message(self.err.get("message")) or "unknown error"


def w(addr: str) -> str:
    return "0x" + addr.lower()[2:].rjust(64, "0")


def fmt(v: int, d: int, places: int = 6) -> str:
    q = Decimal(10) ** -places
    return str((Decimal(v) / Decimal(10) ** d).quantize(q, rounding=ROUND_DOWN))


# ====================================================================== local fork of Pharos mainnet
class Fork:
    """A local anvil fork of Pharos mainnet with manual block production (no automine), so the executor can
    model a mempool: drop a pending tx, spike the base fee, order a competing tx first."""

    def __init__(self, upstream: str, url: str | None = None, anvil: str | None = None):
        self.s = requests.Session()
        self.s.trust_env = False          # localhost must not go through any proxy
        self.proc = None
        self.spike_wei: int | None = None
        if url:
            self.url = url
        else:
            binary = anvil or shutil.which("anvil") or os.path.join(os.environ.get("FOUNDRY_BIN", ""), "anvil")
            if not binary or not os.path.exists(binary):
                raise SystemExit("anvil (Foundry) not found: install Foundry or pass --anvil / --fork-url")
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            sock.close()
            self.url = f"http://127.0.0.1:{port}"
            self.proc = subprocess.Popen([binary, "--fork-url", upstream, "--port", str(port), "--no-mining",
                                          "--silent"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(90):
            try:
                self.rpc("eth_chainId")
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.5)
        else:
            raise SystemExit("anvil fork did not come up")
        self.rpc("evm_setAutomine", [False])
        self.fork_block = int(self.rpc("eth_blockNumber"), 16)

    def rpc(self, method: str, params: list | None = None):
        r = self.s.post(self.url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}, timeout=120)
        j = r.json()
        if "error" in j:
            raise RPCError(j["error"])
        return j["result"]

    def close(self):
        if self.proc:
            self.proc.terminate()

    def base_fee(self) -> int:
        return int(self.rpc("eth_getBlockByNumber", ["latest", False])["baseFeePerGas"], 16)

    def mine(self):
        if self.spike_wei:
            self.rpc("anvil_setNextBlockBaseFeePerGas", [hex(self.spike_wei)])
        self.rpc("evm_mine")

    def call(self, tx: dict, block="latest") -> bytes:
        t = {k: (hex(v) if isinstance(v, int) else v) for k, v in tx.items() if k in ("from", "to", "data", "value")}
        return bytes.fromhex(self.rpc("eth_call", [t, block])[2:])

    def simulate(self, tx: dict) -> dict:
        """eth_call + eth_estimateGas on the fork's current state (what the Chain MCP does on mainnet)."""
        t = {k: (hex(v) if isinstance(v, int) else v) for k, v in tx.items() if k in ("from", "to", "data", "value")}
        try:
            ret = self.rpc("eth_call", [t, "latest"])
            gas = int(self.rpc("eth_estimateGas", [t]), 16)
            return {"ok": True, "returnData": ret, "gas": gas, "revertReason": None}
        except RPCError as e:
            return {"ok": False, "returnData": None, "gas": None, "revertReason": e.reason}

    def nonce(self, a: str, tag="pending") -> int:
        return int(self.rpc("eth_getTransactionCount", [a, tag]), 16)

    def erc20(self, token: str, owner: str, block="latest") -> int:
        return int.from_bytes(self.call({"to": token, "data": calldata("balanceOf(address)", ["address"], [owner])}, block)[:32], "big")

    def allowance(self, token: str, owner: str, spender: str) -> int:
        d = calldata("allowance(address,address)", ["address", "address"], [owner, spender])
        return int.from_bytes(self.call({"to": token, "data": d})[:32], "big")

    def native(self, a: str) -> int:
        return int(self.rpc("eth_getBalance", [a, "latest"]), 16)


# ====================================================================== state log
class StateLog:
    """Append-only JSONL: one line per state transition, flushed immediately, so the current step is always
    known (also after a crash): `saga.py status --log <file>` reads the last line."""

    def __init__(self, path: str, saga_id: str, quiet: bool = False):
        self.path, self.saga_id, self.seq, self.quiet = path, saga_id, 0, quiet
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.f = open(path, "a", encoding="utf-8")

    def __call__(self, phase: str, step: str, idx: str, state: str, detail: str = "", **kw):
        self.seq += 1
        rec = {"seq": self.seq, "ts": utc(), "saga_id": self.saga_id, "phase": phase, "step": step, "index": idx,
               "state": state, "detail": detail, **{k: v for k, v in kw.items() if v is not None}}
        self.f.write(json.dumps(rec) + "\n")
        self.f.flush()
        if not self.quiet:
            extra = "".join(f" {k}={v}" for k, v in kw.items() if v is not None and k in ("attempt", "tx", "block", "gas"))
            print(f"  #{self.seq:02d} [{phase:<12}] {idx:>3} {step:<20} {state:<14}{extra}  {detail}")
        return rec


# ====================================================================== the saga
class Saga:
    def __init__(self, cfg: dict, fork: Fork, log: StateLog, inject: dict | None, execute_comp: bool, tip: int):
        c = cfg["contracts"]
        self.cfg, self.fork, self.log, self.inject, self.execute_comp, self.tip = cfg, fork, log, inject, execute_comp, tip
        self.sender = check_address(cfg["sender"], "sender").lower()
        self.usdc, self.wpros = c["usdc"].lower(), c["wpros"].lower()
        self.router, self.pool, self.vault, self.fee = c["router"].lower(), c["pool"].lower(), c["vault"].lower(), int(c["pool_fee"])
        self.retry = cfg["retry"]
        self.slip = int(cfg["slippage_bps"])
        self.amount_in = to_units(Decimal(cfg["amount_usdc"]), 6)
        self.ctx: dict = {}
        self.done: list[str] = []            # forward steps verified, in order
        self.receipts: list[dict] = []       # every mined receipt (gas accounting)
        self.prepared: list[dict] = []       # unsigned mainnet-format txs for every compensation step
        self.pending: dict | None = None     # a sent tx still in the mempool after giving up (must be cancelled)
        self.frontrun_done = False

    # ------------------------------------------------------------ forward step definitions
    def steps(self):
        return [
            ("approve_usdc", self.b_approve_usdc, self.v_approve_usdc),
            ("swap", self.b_swap, self.v_swap),
            ("approve_wpros", self.b_approve_wpros, self.v_approve_wpros),
            ("deposit", self.b_deposit, self.v_deposit),
        ]

    def b_approve_usdc(self, attempt):
        return {"to": self.usdc, "data": calldata("approve(address,uint256)", ["address", "uint256"], [self.router, self.amount_in])}, \
            f"approve router {self.amount_in / 1e6} USDC (exact amount, no infinite approval)"

    def v_approve_usdc(self, r):
        ev = self.find(r, self.usdc, T_APPROVAL, [self.sender, self.router])
        allow = self.fork.allowance(self.usdc, self.sender, self.router)
        ok = ev is not None and ev == self.amount_in and allow >= self.amount_in
        return ok, f"Approval(owner,router)={ev} · allowance now {allow}"

    def swap_tx(self, t_in, t_out, amt, min_out):
        data = calldata(SWAP_SIG, SWAP_T, [(t_in, t_out, self.fee, self.sender, int(time.time()) + 600, amt, min_out, 0)])
        return {"to": self.router, "data": data}

    def quote(self, t_in, t_out, amt) -> int:
        out = self.fork.call({"from": self.sender, **self.swap_tx(t_in, t_out, amt, 0)})
        return int.from_bytes(out[:32], "big")

    def b_swap(self, attempt):
        q = self.quote(self.usdc, self.wpros, self.amount_in)
        min_out = q * (10_000 - self.slip) // 10_000
        self.ctx["swap_quote"], self.ctx["swap_min_out"] = q, min_out
        return self.swap_tx(self.usdc, self.wpros, self.amount_in, min_out), \
            f"exactInputSingle {self.amount_in / 1e6} USDC -> WPROS, quote {fmt(q, 18, 4)}, minOut {fmt(min_out, 18, 4)} ({self.slip} bps)"

    def v_swap(self, r):
        got = self.find(r, self.wpros, T_TRANSFER, [None, self.sender])
        paid = self.find(r, self.usdc, T_TRANSFER, [self.sender, None])
        ok = got is not None and got >= self.ctx["swap_min_out"] and paid == self.amount_in
        if ok:
            self.ctx["wpros_received"] = got
        return ok, f"paid {paid} USDC units, received {fmt(got or 0, 18, 6)} WPROS (>= minOut {fmt(self.ctx['swap_min_out'], 18, 4)})"

    def b_approve_wpros(self, attempt):
        amt = self.ctx["wpros_received"]                     # depends on the swap receipt, not on the quote
        return {"to": self.wpros, "data": calldata("approve(address,uint256)", ["address", "uint256"], [self.vault, amt])}, \
            f"approve vault {fmt(amt, 18, 6)} WPROS (exactly what the swap receipt delivered)"

    def v_approve_wpros(self, r):
        ev = self.find(r, self.wpros, T_APPROVAL, [self.sender, self.vault])
        return ev == self.ctx["wpros_received"], f"Approval(owner,vault)={fmt(ev or 0, 18, 6)} WPROS"

    def b_deposit(self, attempt):
        amt = self.ctx["wpros_received"]
        note = ""
        if self.injected("revert"):
            amt *= 2                                         # forced permanent failure: more than approved/held
            note = " [INJECTED: amount doubled -> must revert]"
        self.ctx["shares_before"] = self.fork.erc20(self.vault, self.sender)
        return {"to": self.vault, "data": calldata("deposit(uint256,address)", ["uint256", "address"], [amt, self.sender])}, \
            f"Vishwa WPROS Vault deposit({fmt(amt, 18, 6)} WPROS, receiver=sender){note}"

    def v_deposit(self, r):
        logs = [lg for lg in r["logs"] if lg["address"].lower() == self.vault and lg["topics"][0].lower() == T_DEPOSIT4626]
        if not logs:
            return False, "no ERC-4626 Deposit event"
        assets, shares = abi_decode(["uint256", "uint256"], bytes.fromhex(logs[0]["data"][2:]))
        now = self.fork.erc20(self.vault, self.sender)
        ok = assets == self.ctx["wpros_received"] and shares > 0 and now - self.ctx["shares_before"] == shares
        self.ctx["shares"] = shares
        return ok, f"Deposit assets={fmt(assets, 18, 6)} WPROS shares={fmt(shares, 18, 6)} · vault balance +{fmt(now - self.ctx['shares_before'], 18, 6)}"

    # ------------------------------------------------------------ compensation definitions
    def compensations(self):
        """Reverse order of what was verified. Each entry is skipped when the chain shows nothing to undo."""
        out = []
        if "approve_wpros" in self.done and "deposit" not in self.done:
            out.append(("revoke_wpros_vault", lambda a: ({"to": self.wpros, "data": calldata("approve(address,uint256)", ["address", "uint256"], [self.vault, 0])},
                                                         "approve(vault, 0): remove the unused WPROS allowance"),
                        lambda r: (self.fork.allowance(self.wpros, self.sender, self.vault) == 0, "WPROS allowance to vault = 0"),
                        lambda: self.fork.allowance(self.wpros, self.sender, self.vault) > 0))
        if "swap" in self.done and "deposit" not in self.done:
            amt = self.ctx["wpros_received"]
            out.append(("approve_wpros_router", lambda a: ({"to": self.wpros, "data": calldata("approve(address,uint256)", ["address", "uint256"], [self.router, amt])},
                                                           f"approve router {fmt(amt, 18, 6)} WPROS for the unwind"),
                        lambda r: (self.fork.allowance(self.wpros, self.sender, self.router) >= amt, "WPROS allowance to router set"),
                        lambda: True))
            out.append(("swap_back", self.b_swap_back, self.v_swap_back, lambda: True))
        if "approve_usdc" in self.done:
            out.append(("revoke_usdc_router", lambda a: ({"to": self.usdc, "data": calldata("approve(address,uint256)", ["address", "uint256"], [self.router, 0])},
                                                         "approve(router, 0): remove any USDC allowance left"),
                        lambda r: (self.fork.allowance(self.usdc, self.sender, self.router) == 0, "USDC allowance to router = 0"),
                        lambda: self.fork.allowance(self.usdc, self.sender, self.router) > 0))
        return out

    def b_swap_back(self, attempt):
        amt = self.ctx["wpros_received"]
        q = self.quote(self.wpros, self.usdc, amt)
        mo = q * (10_000 - self.slip) // 10_000
        self.ctx["back_min_out"], self.ctx["back_usdc_before"] = mo, self.fork.erc20(self.usdc, self.sender)
        return self.swap_tx(self.wpros, self.usdc, amt, mo), f"unwind: {fmt(amt, 18, 6)} WPROS -> USDC, quote {fmt(q, 6)} minOut {fmt(mo, 6)}"

    def v_swap_back(self, r):
        got = self.find(r, self.usdc, T_TRANSFER, [None, self.sender])
        self.ctx["usdc_recovered"] = got or 0
        return got is not None and got >= self.ctx["back_min_out"], f"recovered {fmt(got or 0, 6)} USDC"

    # ------------------------------------------------------------ helpers
    @staticmethod
    def find(r, token, topic, who):
        for lg in r["logs"]:
            t = [x.lower() for x in lg["topics"]]
            if lg["address"].lower() != token or t[0] != topic:
                continue
            if all(x is None or t[i + 1] == w(x) for i, x in enumerate(who)):
                return int(lg["data"], 16)
        return None

    def injected(self, kind, step=None):
        i = self.inject
        step = step or self.ctx.get("_step")
        return bool(i and i["kind"] == kind and i["step"] == step and i.get("_active", True))

    def fees(self, prev_max: int | None, base_seen: int | None) -> int:
        base = self.fork.base_fee()
        fresh = 2 * base + self.tip
        if prev_max is None:
            return fresh
        bump = prev_max * (100 + int(self.retry["fee_bump_pct"])) // 100
        need = 2 * max(base, base_seen or 0) + self.tip
        return max(bump, need, fresh)

    def wait(self, h: str):
        for _ in range(int(self.retry["confirm_blocks"])):
            self.fork.mine()
            r = self.fork.rpc("eth_getTransactionReceipt", [h])
            if r:
                return "MINED", r
            if self.fork.rpc("eth_getTransactionByHash", [h]) is None:
                return "DROPPED", None
        return "STUCK", None

    # ------------------------------------------------------------ one transaction with the retry policy
    def run_tx(self, phase, name, idx, build, verify, inject_ok=True):
        log, R = self.log, self.retry
        self.ctx["_step"] = name
        nonce = self.fork.nonce(self.sender, "latest")
        prev_max = None
        base_seen = None
        spike = inject_ok and self.inject and self.inject["step"] == name and self.inject["kind"] in ("spike", "spike-persist")
        if spike:
            base_before = self.fork.base_fee()
            self.fork.spike_wei = (400 if self.inject["kind"] == "spike-persist" else 40) * 10 ** 9
            log(phase, name, idx, "INJECT", f"base fee spike to {self.fork.spike_wei // 10**9} gwei while this step is pending")
        try:
            for attempt in range(1, int(R["max_attempts"]) + 1):
                tx, desc = build(attempt)
                tx = {"from": self.sender, "value": 0, **tx}
                force_send = inject_ok and self.injected("revert", name)
                sim = self.fork.simulate(tx)
                log(phase, name, idx, "SIMULATED" if sim["ok"] else "SIM_REVERT",
                    desc + ("" if sim["ok"] else f" · revert: {sim['revertReason']}"), attempt=attempt, gas=sim["gas"])
                if not sim["ok"] and not force_send:
                    return False, f"simulation reverts: {sim['revertReason']} (deterministic -> permanent, not sent)"
                gas = int(sim["gas"] * 1.2) if sim["ok"] else 400_000
                max_fee = self.fees(prev_max, base_seen)
                if max_fee > int(R["max_fee_gwei"]) * 10 ** 9:
                    return False, (f"retry budget: needed maxFeePerGas {max_fee / 1e9:.1f} gwei > cap {R['max_fee_gwei']} gwei "
                                   f"(base fee {self.fork.base_fee() / 1e9:.1f} gwei) -> give up")
                send = {**tx, "gas": hex(gas), "nonce": hex(nonce), "maxFeePerGas": hex(max_fee),
                        "maxPriorityFeePerGas": hex(self.tip), "type": "0x2", "value": hex(tx["value"])}
                h = self.fork.rpc("eth_sendTransaction", [send])
                prev_max = max_fee
                self.pending = {"hash": h, "nonce": nonce, "maxFee": max_fee, "step": name}
                log(phase, name, idx, "SENT", f"nonce {nonce} maxFee {max_fee / 1e9:.2f} gwei tip {self.tip / 1e9:.2f} gwei gas {gas}",
                    attempt=attempt, tx=h)
                if inject_ok and self.injected("drop", name) and attempt == 1:
                    self.fork.rpc("anvil_dropTransaction", [h])
                    log(phase, name, idx, "INJECT", "tx evicted from the mempool (simulated drop)", attempt=attempt, tx=h)
                if inject_ok and self.injected("frontrun", name) and not self.frontrun_done:
                    self.frontrun()
                status, r = self.wait(h)
                if status == "MINED":
                    self.pending = None
                    self.receipts.append({"step": name, "phase": phase, "tx": h, "gasUsed": int(r["gasUsed"], 16),
                                          "price": int(r["effectiveGasPrice"], 16), "status": int(r["status"], 16)})
                    blk = int(r["blockNumber"], 16)
                    if int(r["status"], 16) != 1:
                        why = self.replay_reason(tx, blk, h)
                        log(phase, name, idx, "RECEIPT_FAIL", f"receipt status 0 · {why}", attempt=attempt, tx=h, block=blk)
                        if "Too little received" in why and R.get("requote_on_slippage") and attempt < int(R["max_attempts"]):
                            log(phase, name, idx, "RETRYING", "slippage (price moved before inclusion) is transient -> re-quote, new tx", attempt=attempt)
                            nonce = self.fork.nonce(self.sender, "latest")      # failed tx consumed the nonce
                            prev_max = None
                            time.sleep(float(R["backoff_seconds"]))
                            continue
                        return False, f"receipt status 0: {why} (permanent)"
                    ok, facts = verify(r)
                    log(phase, name, idx, "VERIFIED" if ok else "VERIFY_FAIL", facts, attempt=attempt, tx=h, block=blk)
                    return (True, facts) if ok else (False, f"post-condition failed: {facts}")
                if status == "DROPPED":
                    base_now = self.fork.base_fee()
                    if base_now > max_fee:      # evicted because underpriced -> needs a fee bump
                        base_seen = base_now
                        log(phase, name, idx, "RETRYING", f"evicted from the mempool, no receipt: base fee {base_now / 1e9:.1f} gwei > "
                                                           f"maxFee {max_fee / 1e9:.2f} gwei (transient, underpriced) -> resend nonce {nonce} with fee bump",
                            attempt=attempt, tx=h)
                    else:
                        log(phase, name, idx, "RETRYING", f"tx not in mempool and no receipt after mining -> dropped (transient); "
                                                           f"nonce {nonce} still unused, resend", attempt=attempt, tx=h)
                        prev_max = None
                else:
                    base_seen = self.fork.base_fee()
                    log(phase, name, idx, "RETRYING", f"not mined in {R['confirm_blocks']} blocks: base fee {base_seen / 1e9:.1f} gwei > "
                                                       f"maxFee {max_fee / 1e9:.2f} gwei (transient) -> replace same nonce, fee bump",
                        attempt=attempt, tx=h)
                time.sleep(float(R["backoff_seconds"]))
            return False, f"retries exhausted ({R['max_attempts']} attempts)"
        finally:
            if spike:   # the spike ends; a tx we gave up on is still pending and gets cancelled first in compensation
                self.fork.spike_wei = None
                self.fork.rpc("anvil_setNextBlockBaseFeePerGas", [hex(base_before)])
                self.fork.mine()
                log(phase, name, idx, "INJECT", f"spike over: base fee back to {self.fork.base_fee() / 1e9:.2f} gwei")

    def replay_reason(self, tx, blk, h=None) -> str:
        if h:   # exact reason inside the block (after any tx ordered before ours)
            try:
                tr = self.fork.rpc("debug_traceTransaction", [h, {"tracer": "callTracer"}])
                if tr.get("revertReason"):
                    return tr["revertReason"]
                if tr.get("output") and tr["output"] != "0x":
                    return decode_revert(tr["output"]) or tr.get("error", "reverted")
            except RPCError:
                pass
        try:
            self.fork.call(tx, hex(blk - 1))
            return "replay at block-1 succeeds (state changed inside the block)"
        except RPCError as e:
            return e.reason

    def frontrun(self):
        """A competing 3,000 USDC -> WPROS buy from a real USDC holder, impersonated ON THE FORK ONLY, with a higher
        tip so it is ordered before our pending swap in the same block."""
        self.frontrun_done = True
        fr, amt = FRONTRUNNER, 3_000 * 10 ** 6
        f = self.fork
        f.rpc("anvil_impersonateAccount", [fr])
        f.rpc("anvil_setBalance", [fr, hex(10 ** 18)])
        base = f.base_fee()
        common = {"from": fr, "type": "0x2", "maxPriorityFeePerGas": hex(self.tip + 10 ** 9), "maxFeePerGas": hex(2 * base + self.tip + 10 ** 9)}
        n = f.nonce(fr)
        f.rpc("eth_sendTransaction", [{**common, "to": self.usdc, "nonce": hex(n), "gas": hex(120_000),
                                       "data": calldata("approve(address,uint256)", ["address", "uint256"], [self.router, amt])}])
        d = calldata(SWAP_SIG, SWAP_T, [(self.usdc, self.wpros, self.fee, fr, int(time.time()) + 600, amt, 0, 0)])
        h = f.rpc("eth_sendTransaction", [{**common, "to": self.router, "nonce": hex(n + 1), "gas": hex(400_000), "data": d}])
        self.log("forward", "swap", "", "INJECT", f"competing 3,000 USDC->WPROS swap from {fr[:10]}… (real holder, fork-only "
                                                   f"impersonation) with higher tip, same block", tx=h)

    # ------------------------------------------------------------ orchestration
    def run(self) -> int:
        log = self.log
        for i, (name, b, v) in enumerate(self.steps(), 1):
            idx = f"{i}/4"
            log("forward", name, idx, "STARTED", "depends on: " + (self.done[-1] if self.done else "preflight"))
            ok, info = self.run_tx("forward", name, idx, b, v)
            if ok:
                self.done.append(name)
                continue
            log("forward", name, idx, "FAILED", info)
            if self.pending and self.fork.rpc("eth_getTransactionReceipt", [self.pending["hash"]]):
                self.pending = None
            if self.inject and self.inject["step"] == name:
                self.inject["_active"] = False
            return self.compensate(name, info)
        log("saga", "-", "", "COMPLETED", "all 4 steps verified by receipt")
        return 0

    def compensate(self, failed: str, why: str) -> int:
        log = self.log
        log("compensation", failed, "", "COMPENSATING", f"verified steps to undo: {', '.join(self.done) or 'none'}")
        plan = []
        if self.pending:            # a sent tx without receipt: supersede its nonce before anything else
            p = self.pending
            plan.append(("cancel_pending", lambda a, p=p: ({"to": self.sender, "data": "0x", "_cancel": p},
                                                           f"0-PROS self-transfer at nonce {p['nonce']} supersedes the abandoned "
                                                           f"{p['hash'][:12]}… (it may still sit in some node's mempool)"),
                         lambda r, p=p: (self.fork.rpc("eth_getTransactionReceipt", [p["hash"]]) is None
                                         and self.fork.nonce(self.sender, "latest") > p["nonce"],
                                         f"abandoned tx {p['hash'][:12]}… has no receipt and nonce {p['nonce']} is now used -> it can never be mined"),
                         lambda: True))
        plan += self.compensations()
        all_ok = True
        for j, (name, b, v, needed) in enumerate(plan, 1):
            idx = f"C{j}"
            try:
                if not needed():
                    log("compensation", name, idx, "SKIPPED", "nothing to undo on chain")
                    continue
                ok = self.cancel(idx, b, v) if name == "cancel_pending" else self._comp_step(name, idx, b, v)
            except Exception as e:  # noqa: BLE001 - a compensation error must never crash the saga
                log("compensation", name, idx, "FAILED", f"error: {str(e)[:200]}")
                ok = False
            all_ok = all_ok and ok
        if not self.execute_comp:
            log("saga", "-", "", "COMPENSATION_PREPARED", "compensation simulated + prepared, not executed (--no-execute-compensation)")
            return 5 if all_ok else 4
        state = "COMPENSATED" if all_ok else "NEEDS_MANUAL_ACTION"
        log("saga", "-", "", state, f"failed at {failed}: {why}")
        return 5 if all_ok else 4

    def _comp_step(self, name, idx, b, v) -> bool:
        """simulate (eth_call + estimate_gas on the stranded state) -> prepare unsigned -> execute on the fork."""
        log = self.log
        tx, desc = b(1)
        tx = {"from": self.sender, "value": 0, **tx}
        sim = self.fork.simulate(tx)
        if not sim["ok"]:
            log("compensation", name, idx, "SIM_REVERT", f"{desc} · revert: {sim['revertReason']}")
            return False
        prep = self.prepare_unsigned(tx, sim["gas"])
        self.prepared.append({"step": name, "description": desc, **prep})
        log("compensation", name, idx, "PREPARED", f"{desc} · eth_call ok, estimate_gas {sim['gas']} · unsigned tx prepared "
                                                   f"(nonce {int(prep['unsignedTx']['nonce'], 16)})", gas=sim["gas"])
        if not self.execute_comp:
            return True
        ok, info = self.run_tx("compensation", name, idx, lambda a, b=b: b(a), v, inject_ok=False)
        if not ok:
            log("compensation", name, idx, "FAILED", info)
        return ok

    def cancel(self, idx, b, v) -> bool:
        tx, desc = b(1)
        p = tx.pop("_cancel")
        max_fee = max(p["maxFee"] * (100 + int(self.retry["fee_bump_pct"])) // 100, 2 * self.fork.base_fee() + self.tip)
        tip = max(self.tip, max_fee // 2)
        send = {"from": self.sender, "to": self.sender, "value": "0x0", "data": "0x", "gas": hex(21_000), "nonce": hex(p["nonce"]),
                "maxFeePerGas": hex(max_fee), "maxPriorityFeePerGas": hex(min(tip, max_fee)), "type": "0x2"}
        self.prepared.append({"step": "cancel_pending", "description": desc,
                              **self.prepare_unsigned({"to": self.sender, "data": "0x", "value": 0}, 21_000, nonce=p["nonce"], max_fee=max_fee)})
        h = self.fork.rpc("eth_sendTransaction", [send])
        self.log("compensation", "cancel_pending", idx, "SENT", desc + f" · maxFee {max_fee / 1e9:.2f} gwei", tx=h)
        st, r = self.wait(h)
        if st != "MINED" or int(r["status"], 16) != 1:
            self.log("compensation", "cancel_pending", idx, "FAILED", f"cancel tx {st}")
            return False
        self.receipts.append({"step": "cancel_pending", "phase": "compensation", "tx": h, "gasUsed": int(r["gasUsed"], 16),
                              "price": int(r["effectiveGasPrice"], 16), "status": 1})
        ok, facts = v(r)
        self.pending = None
        self.log("compensation", "cancel_pending", idx, "VERIFIED" if ok else "VERIFY_FAIL", facts, tx=h, block=int(r["blockNumber"], 16))
        return ok

    def prepare_unsigned(self, tx: dict, gas: int, nonce: int | None = None, max_fee: int | None = None) -> dict:
        """Unsigned mainnet-format (chainId 1672) EIP-1559 tx for a compensation step, built from the fork state."""
        n = self.fork.nonce(self.sender, "pending") if nonce is None else nonce
        mf = max_fee or 2 * self.fork.base_fee() + self.tip
        g = int(gas * 1.2) if nonce is None else gas
        t = {"chainId": CHAIN_ID, "nonce": n, "maxPriorityFeePerGas": self.tip, "maxFeePerGas": mf, "gas": g,
             "to": tx["to"].lower(), "value": int(tx.get("value", 0)), "data": tx["data"]}
        raw, h = unsigned_eip1559(t)
        return {"unsignedTx": {k: (hex(v) if isinstance(v, int) else v) for k, v in t.items()}, "unsignedSerialized": raw,
                "signingHash": h, "castCommandForUser": cast_send_command(tx["to"], tx["data"], int(tx.get("value", 0))),
                "signed": False, "broadcast": False}


# ====================================================================== mainnet preflight + plan (Chain MCP)
def preflight(chain: Chain, rd: Reader, cfg: dict) -> dict:
    c, s = cfg["contracts"], cfg["sender"].lower()
    print("[1] Mainnet preflight — hosted Pharos Chain MCP (read-only)")
    cid = chain.chain_id()
    assert cid == CHAIN_ID, f"Chain MCP reports chainId {cid}, expected {CHAIN_ID}"
    for k in ("usdc", "wpros", "router", "pool", "vault"):
        if chain.code(c[k]) in ("0x", "", None):
            raise SystemExit(f"{k} {c[k]} has no code on mainnet")
    asset = "0x" + rd.call(c["vault"], "asset()")[-20:].hex()
    t0, t1, _, _ = rd.pool_meta(c["pool"])
    vname = abi_decode(["string"], rd.call(c["vault"], "name()"))[0]
    fee = int.from_bytes(rd.call(c["pool"], "fee()")[:32], "big")
    if asset != c["wpros"].lower() or {t0, t1} != {c["usdc"].lower(), c["wpros"].lower()} or fee != int(c["pool_fee"]):
        raise SystemExit("contract set does not match on chain (vault asset / pool tokens / pool fee)")
    head = rd.head()
    price, _ = rd.pool_price(c["pool"], head)
    pros_usd, ometa = rd.oracle(CHAINLINK_PROS_USD, head)
    usdc_bal = chain.erc20_balance(chain.token("USDC"), s)
    pros_bal = chain.native_balance(s)
    nonce = chain.nonce(s, "pending")
    base, tip, max_fee = chain.fees()
    need = to_units(Decimal(cfg["amount_usdc"]), 6)
    print(f"    chainId {cid} · head block {head} · all 5 contracts have code")
    print(f"    vault  {vname} {addr_link(c['vault'])} · asset() = WPROS ✓")
    print(f"    pool   WPROS/USDC fee {fee / 10000:.2f}% {addr_link(c['pool'])} · spot 1 WPROS = {price:.6f} USDC")
    print(f"    oracle Chainlink PROS/USD = {pros_usd:.6f} (round {ometa['roundId']})")
    print(f"    sender {to_checksum(s)}: {fmt(usdc_bal, 6, 2)} USDC (need {cfg['amount_usdc']}) · {fmt(pros_bal, 18, 4)} PROS · pending nonce {nonce}")
    print(f"    fees   base {base / 1e9:.2f} gwei · tip {tip / 1e9:.4f} gwei · maxFee {max_fee / 1e9:.2f} gwei")
    if usdc_bal < need:
        raise SystemExit(f"sender holds {fmt(usdc_bal, 6)} USDC < {cfg['amount_usdc']} — saga refused before step 1")
    return {"head": head, "price": price, "pros_usd": pros_usd, "nonce": nonce, "base": base, "tip": tip, "max_fee": max_fee}


def mainnet_plan(chain: Chain, cfg: dict, saga: Saga, pf: dict) -> dict:
    """Unsigned mainnet txs for all 4 forward steps (nonces n..n+3). Step 1 is simulated on MAINNET through the
    Chain MCP; steps 2-4 depend on step 1's state, so their gas comes from the fork receipts."""
    s = saga.sender
    gas_by_step = {r["step"]: r["gasUsed"] for r in saga.receipts if r["phase"] == "forward" and r["status"] == 1}
    builds = {"approve_usdc": saga.b_approve_usdc}
    steps = []
    p1_tx, _ = saga.b_approve_usdc(1)
    sim = simulate(chain, {"from": s, **p1_tx, "value": 0})
    first = prepare_tx(chain, s, p1_tx["to"], p1_tx["data"]) if sim["ok"] else None
    steps.append({"step": "approve_usdc", "mainnetSimulation": {"ok": sim["ok"], "gas": sim["gas"], "revert": sim["revertReason"],
                                                                "via": "Chain MCP eth_call + estimate_gas"}, **(first or {})})
    n0 = int(first["unsignedTx"]["nonce"], 16) if first else pf["nonce"]
    for i, name in enumerate(STEP_NAMES[1:], 1):
        tx = {"swap": lambda: saga.swap_tx(saga.usdc, saga.wpros, saga.amount_in, saga.ctx["swap_min_out"]),
              "approve_wpros": lambda: saga.b_approve_wpros(1)[0],
              "deposit": lambda: {"to": saga.vault, "data": calldata("deposit(uint256,address)", ["uint256", "address"], [saga.ctx["wpros_received"], s])}}[name]()
        g = int(gas_by_step[name] * 1.3)
        t = {"chainId": CHAIN_ID, "nonce": n0 + i, "maxPriorityFeePerGas": pf["tip"], "maxFeePerGas": pf["max_fee"], "gas": g,
             "to": tx["to"], "value": 0, "data": tx["data"]}
        raw, h = unsigned_eip1559(t)
        steps.append({"step": name, "mainnetSimulation": {"ok": None, "via": "depends on previous step; simulated on the fork",
                                                          "forkGasUsed": gas_by_step[name]},
                      "unsignedTx": {k: (hex(v) if isinstance(v, int) else v) for k, v in t.items()}, "unsignedSerialized": raw,
                      "signingHash": h, "castCommandForUser": cast_send_command(tx["to"], tx["data"]), "signed": False, "broadcast": False,
                      "verifyAfterSigning": f"python3 scripts/saga.py verify-receipt --plan <this file> --step {name} --tx <hash>"})
    del builds
    return {"saga": cfg["saga"], "chainId": CHAIN_ID, "sender": to_checksum(s), "preparedAtBlock": pf["head"],
            "note": "UNSIGNED. Sign and send one step at a time; run verify-receipt before signing the next step. "
                    "Re-run the saga if the price moved (swap minOut is from the fork quote).", "steps": steps}


# ====================================================================== commands
def parse_inject(s: str | None):
    if not s:
        return None
    kind, _, step = s.partition("@")
    if kind not in INJECT_KINDS or step not in STEP_NAMES:
        raise SystemExit(f"--inject must be <{'|'.join(sorted(INJECT_KINDS))}>@<{'|'.join(STEP_NAMES)}>")
    if kind == "frontrun" and step != "swap":
        raise SystemExit("frontrun only applies to the swap step")
    return {"kind": kind, "step": step}


def balances(fork: Fork, s: Saga) -> dict:
    return {"USDC": fork.erc20(s.usdc, s.sender), "WPROS": fork.erc20(s.wpros, s.sender),
            "vault shares": fork.erc20(s.vault, s.sender), "PROS": fork.native(s.sender),
            "USDC allowance->router": fork.allowance(s.usdc, s.sender, s.router),
            "WPROS allowance->router": fork.allowance(s.wpros, s.sender, s.router),
            "WPROS allowance->vault": fork.allowance(s.wpros, s.sender, s.vault)}


DEC = {"USDC": 6, "USDC allowance->router": 6}


def cmd_run(a) -> int:
    cfg = json.load(open(a.config, encoding="utf-8"))
    if a.amount:
        cfg["amount_usdc"] = a.amount
    if a.sender:
        cfg["sender"] = a.sender
    inject = parse_inject(a.inject)
    chain = Chain()
    rd = Reader(chain)
    up = network("mainnet")["rpcUrl"]
    saga_id = a.saga_id or f"saga-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-{uuid.uuid4().hex[:4]}"
    print(f"Saga {saga_id}: {cfg['amount_usdc']} USDC -> WPROS -> Vishwa WPROS Vault · Pharos mainnet (chainId {CHAIN_ID}) · {utc()}")
    print(f"inject: {a.inject or 'none'} · retry policy: {json.dumps(cfg['retry'])}\n")
    pf = preflight(chain, rd, cfg)
    print(f"\n[2] Local fork of Pharos mainnet (anvil, upstream {up}, no automine; sender impersonated — no key)")
    fork = Fork(up, url=a.fork_url, anvil=a.anvil)
    try:
        print(f"    forked at mainnet block {fork.fork_block}: https://www.pharosscan.xyz/block/{fork.fork_block}")
        fork.rpc("anvil_impersonateAccount", [cfg["sender"].lower()])
        log = StateLog(os.path.join(a.runs_dir, f"{saga_id}.jsonl"), saga_id)
        saga = Saga(cfg, fork, log, inject, not a.no_execute_compensation, pf["tip"])
        before = balances(fork, saga)
        print(f"\n[3] Saga state log (also appended to {os.path.relpath(log.path, ROOT)})")
        code = saga.run()
        after = balances(fork, saga)
        print("\n[4] Balances on the fork, before -> after")
        print(f"    {'asset':<26}{'before':>22}{'after':>22}{'change':>22}")
        for k in before:
            d = DEC.get(k, 18)
            print(f"    {k:<26}{fmt(before[k], d):>22}{fmt(after[k], d):>22}{fmt(after[k] - before[k], d):>22}")
        gas_pros = sum(r["gasUsed"] * r["price"] for r in saga.receipts)
        print(f"    gas paid by {len(saga.receipts)} mined txs: {fmt(gas_pros, 18, 8)} PROS ≈ ${Decimal(gas_pros) / Decimal(10**18) * pf['pros_usd']:.6f}"
              f" (Chainlink PROS/USD {pf['pros_usd']:.4f})")
        if code == 5:
            loss = before["USDC"] - after["USDC"]
            print(f"    round-trip cost of the compensation: {fmt(loss, 6)} USDC (2× 0.01% pool fee + price impact) + gas;"
                  f" no WPROS, shares or allowances left behind" if after["WPROS"] == before["WPROS"] else "")
        out = {"saga_id": saga_id, "exit": code, "inject": a.inject, "forkBlock": fork.fork_block, "done": saga.done,
               "compensationPrepared": saga.prepared, "receipts": saga.receipts,
               "balancesBefore": {k: str(v) for k, v in before.items()}, "balancesAfter": {k: str(v) for k, v in after.items()}}
        if code == 0:
            plan = mainnet_plan(chain, cfg, saga, pf)
            out["mainnetPlan"] = plan
            s1 = plan["steps"][0]
            print("\n[5] Mainnet plan — UNSIGNED, prepare-only")
            print(f"    step 1 approve_usdc simulated on MAINNET via Chain MCP: ok={s1['mainnetSimulation']['ok']} gas={s1['mainnetSimulation']['gas']}")
            for st in plan["steps"]:
                u = st.get("unsignedTx", {})
                print(f"    {st['step']:<14} nonce {int(u.get('nonce', '0x0'), 16)} gas {int(u.get('gas', '0x0'), 16):>7} to {u.get('to')} signingHash {st.get('signingHash', '')[:18]}…")
            print(f"    user runs e.g.: {s1.get('castCommandForUser')}")
            print("    nothing signed, nothing broadcast; each next step only after verify-receipt on the previous one")
        elif saga.prepared:
            print("\n[5] Compensation txs prepared (unsigned, chainId 1672, built from the stranded fork state)")
            for p in saga.prepared:
                u = p["unsignedTx"]
                print(f"    {p['step']:<20} nonce {int(u['nonce'], 16)} gas {int(u['gas'], 16):>7} to {u['to']} signingHash {p['signingHash'][:18]}…")
        if a.json_out:
            os.makedirs(os.path.dirname(os.path.abspath(a.json_out)), exist_ok=True)
            json.dump(out, open(a.json_out, "w"), indent=2, default=str)
            print(f"\n    JSON report: {a.json_out}")
        print(f"\nRESULT: {({0: 'COMPLETED', 5: 'FAILED -> COMPENSATED', 4: 'FAILED -> NEEDS MANUAL ACTION'})[code]} (exit {code})")
        return code
    finally:
        fork.close()


def cmd_status(a) -> int:
    recs = [json.loads(x) for x in open(a.log, encoding="utf-8") if x.strip()]
    if not recs:
        print("empty log")
        return 2
    last = recs[-1]
    cur = next((r for r in reversed(recs) if r["step"] != "-"), last)
    print(f"saga {last['saga_id']} · {len(recs)} transitions · last update {last['ts']}")
    print(f"CURRENT: phase={last['phase']} state={last['state']} · last step {cur['index']} {cur['step']} -> {cur['state']}")
    if a.history:
        for r in recs:
            print(f"  #{r['seq']:02d} {r['ts'][11:19]} {r['phase']:<12} {r['index']:>3} {r['step']:<20} {r['state']:<14} {r.get('tx', '')[:14]}")
    return 0


def cmd_verify(a) -> int:
    """Mainnet half of the loop: after the USER signed and sent a planned step, check its receipt through the Chain
    MCP before the next step is allowed. Checks: receipt exists, status 1, from/to/calldata equal the plan."""
    plan = json.load(open(a.plan, encoding="utf-8"))
    plan = plan.get("mainnetPlan", plan)
    st = next((s for s in plan["steps"] if s["step"] == a.step), None)
    if not st:
        raise SystemExit(f"step {a.step} not in plan")
    chain = Chain()
    tx = chain.m.call_tool("get_transaction", {"hash": a.tx})
    rc = chain.m.call_tool("get_transaction_receipt", {"hash": a.tx})
    exp = st["unsignedTx"]
    checks = []
    t = tx if isinstance(tx, dict) else {}
    r = rc if isinstance(rc, dict) else {}
    checks.append((bool(r), "receipt exists (Chain MCP get_transaction_receipt)"))
    status = r.get("status")
    checks.append((str(status) in ("1", "0x1", "success"), f"status = {status}"))
    frm = (t.get("from") or "").lower()
    checks.append((frm == plan["sender"].lower(), f"from {frm} == planned sender {plan['sender'].lower()}"))
    to = (t.get("to") or "").lower()
    checks.append((to == exp["to"].lower(), f"to {to} == planned {exp['to'].lower()}"))
    inp = (t.get("input") or t.get("data") or "").lower()
    checks.append((inp[:10] == exp["data"][:10].lower(), f"selector {inp[:10]} == planned {exp['data'][:10]}"))
    checks.append((inp == exp["data"].lower(), "full calldata == planned calldata"))
    print(f"verify-receipt · step {a.step} · {tx_link(a.tx)}")
    for ok, c in checks:
        print(f"  [{'OK' if ok else 'XX'}] {c}")
    good = all(ok for ok, _ in checks)
    print(f"RESULT: {'VERIFIED - next step may be prepared' if good else 'NOT VERIFIED - saga stays at ' + a.step + '; do not sign the next step'}")
    return 0 if good else 3


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--config", default=os.path.join(ROOT, "assets", "saga.json"))
    r.add_argument("--amount")
    r.add_argument("--sender")
    r.add_argument("--inject")
    r.add_argument("--no-execute-compensation", action="store_true")
    r.add_argument("--fork-url", help="use an already running anvil fork (no automine) instead of starting one")
    r.add_argument("--anvil", help="path to the anvil binary")
    r.add_argument("--runs-dir", default=os.path.join(ROOT, "runs"))
    r.add_argument("--saga-id")
    r.add_argument("--json-out")
    s = sub.add_parser("status")
    s.add_argument("--log", required=True)
    s.add_argument("--history", action="store_true")
    v = sub.add_parser("verify-receipt")
    v.add_argument("--plan", required=True)
    v.add_argument("--step", required=True, choices=STEP_NAMES)
    v.add_argument("--tx", required=True)
    a = p.parse_args(argv)
    try:
        return {"run": cmd_run, "status": cmd_status, "verify-receipt": cmd_verify}[a.cmd](a)
    except (ValueError, KeyError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
