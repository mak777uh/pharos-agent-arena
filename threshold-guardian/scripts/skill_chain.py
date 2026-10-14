"""Shared on-chain helpers for the Pharos Agent Arena SkillEngine skills (Pharos Pacific mainnet, chainId 1672).

Every read goes through the hosted Pharos Chain MCP (https://mcp.pharos.xyz/mcp) via `pay_core.Chain`.
The only exception is `trace_call`, which asks the official Pharos RPC from assets/networks.json
(https://rpc.pharos.xyz, the same endpoint the official pharos-skill-engine passes to `cast`) for
`debug_traceCall`, because the Chain MCP has no tracing tool. Nothing here signs or broadcasts.
"""
from __future__ import annotations

import json
import os
import shlex
import time
from decimal import Decimal, getcontext

import requests
from eth_abi import decode as abi_decode, encode as abi_encode

from pay_core import Chain, MCPError, PaymentError, h2i, keccak256, to_checksum, unsigned_eip1559, CHAIN_ID, EXPLORER
from pharos_mcp import check_endpoint

getcontext().prec = 60
HERE = os.path.dirname(os.path.abspath(__file__))
NETWORKS_FILE = os.path.join(HERE, "..", "assets", "networks.json")

# Well-known contracts on Pharos mainnet, each re-checked on chain (code / token0 / decimals) before use.
WPROS_USDC_POOL = "0x912c9ade24d44d8922f0866d8dcb079f1363f647"   # Uniswap-V3-style pool, fee 100 (0.01%)
CHAINLINK_PROS_USD = "0x371ed36aee5bffbed1a2aa9b7ded7f4eb30f5466"
MORPHO_BLUE = "0x18573fa18fd17ddfd790b4a5b5b2977aad3b4efb"
MULTICALL3 = "0xca11bde05977b3631167028862be2a173976ca11"


def sel(sig: str) -> str:
    return keccak256(sig.encode()).hex()[:8]


def calldata(sig: str, types: list[str] | None = None, args: list | None = None) -> str:
    return "0x" + sel(sig) + (abi_encode(types, args).hex() if types else "")


def network(name: str = "mainnet") -> dict:
    """Network entry from assets/networks.json (same file/format as the official pharos-skill-engine)."""
    with open(NETWORKS_FILE, encoding="utf-8") as f:
        nets = json.load(f)["networks"]
    for n in nets:
        if n["name"] == name:
            check_endpoint(n["rpcUrl"])
            return n
    raise ValueError(f"unknown network {name!r}; supported: {', '.join(n['name'] for n in nets)}")


def addr_link(a: str) -> str:
    return f"{EXPLORER}/address/{a.lower()}"


def tx_link(h: str) -> str:
    return f"{EXPLORER}/tx/{h.lower()}"


# ---------------------------------------------------------------- revert decoding
PANIC = {0x01: "assert failed", 0x11: "arithmetic overflow/underflow", 0x12: "division by zero",
         0x21: "invalid enum value", 0x31: "pop on empty array", 0x32: "array index out of bounds",
         0x41: "out of memory", 0x51: "call to uninitialised function"}
KNOWN_ERRORS = {  # selector -> signature, for common custom errors (OpenZeppelin 5 ERC-20 etc.)
    sel(s): s for s in [
        "ERC20InsufficientBalance(address,uint256,uint256)", "ERC20InsufficientAllowance(address,uint256,uint256)",
        "ERC20InvalidReceiver(address)", "ERC20InvalidSender(address)", "OwnableUnauthorizedAccount(address)",
        "EnforcedPause()", "SafeERC20FailedOperation(address)", "FailedCall()",
    ]}


def clean_message(msg: str | None) -> str | None:
    if msg is None:
        return None
    m = str(msg).strip()
    if m.rstrip(":").strip() == "execution reverted":
        return "reverted without a reason (empty revert data)"
    return m


def decode_revert(data: str | None) -> str | None:
    if not data or data in ("0x", ""):
        return None
    b = bytes.fromhex(data[2:])
    s = b[:4].hex()
    try:
        if s == "08c379a0":
            return abi_decode(["string"], b[4:])[0]
        if s == "4e487b71":
            code = abi_decode(["uint256"], b[4:])[0]
            return f"Panic(0x{code:02x}): {PANIC.get(code, 'unknown panic')}"
        if s in KNOWN_ERRORS:
            sig = KNOWN_ERRORS[s]
            types = sig[sig.index("(") + 1:-1]
            vals = abi_decode(types.split(","), b[4:]) if types else []
            return f"{sig.split('(')[0]}({', '.join(str(v) for v in vals)})"
    except Exception:  # noqa: BLE001
        pass
    return f"custom error 0x{s} (raw {data[:74]}{'…' if len(data) > 74 else ''})"


# ---------------------------------------------------------------- simulation through Chain MCP
def simulate(chain: Chain, tx: dict, block="latest") -> dict:
    """eth_call + estimate_gas for {from,to,value,data}. Returns ok, returnData, revert reason, gas."""
    t = {k: v for k, v in tx.items() if k in ("from", "to", "data", "value") and v is not None}
    if isinstance(t.get("value"), int):
        t["value"] = hex(t["value"])
    err, text = chain.m.call_tool_raw("eth_call", {"transaction": t, "block": block})
    out = {"ok": not err, "returnData": None, "revertData": None, "revertReason": None, "gas": None,
           "mcpTools": ["eth_call"]}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = text
    if err:
        e = payload.get("error", {}) if isinstance(payload, dict) else {}
        out["revertData"] = e.get("data") if isinstance(e, dict) else None
        out["revertReason"] = decode_revert(out["revertData"]) or clean_message(e.get("message") if isinstance(e, dict) else str(payload)[:200])
        return out
    out["returnData"] = payload.get("result") if isinstance(payload, dict) else payload
    if block == "latest":
        err, text = chain.m.call_tool_raw("estimate_gas", {"transaction": t})
        out["mcpTools"].append("estimate_gas")
        p = json.loads(text)
        if err:
            out["ok"] = False
            e = p.get("error", {})
            out["revertData"] = e.get("data")
            out["revertReason"] = decode_revert(e.get("data")) or clean_message(e.get("message"))
        else:
            out["gas"] = int(p["gas"])
    return out


def trace_call(tx: dict, rpc_url: str | None = None, timeout: int = 30) -> dict | None:
    """debug_traceCall (callTracer, withLog) on the official Pharos RPC: the logs a tx WOULD emit, without sending it.
    Returns None when the RPC does not answer (the dry run then falls back to decoded calldata)."""
    url = check_endpoint(rpc_url or network("mainnet")["rpcUrl"])
    t = {k: (hex(v) if isinstance(v, int) else v) for k, v in tx.items() if k in ("from", "to", "data", "value") and v is not None}
    body = {"jsonrpc": "2.0", "id": 1, "method": "debug_traceCall",
            "params": [t, "latest", {"tracer": "callTracer", "tracerConfig": {"withLog": True}}]}
    try:
        r = requests.post(url, json=body, timeout=timeout)
        res = r.json().get("result")
    except Exception:  # noqa: BLE001
        return None
    return res


def trace_logs(frame: dict) -> list[dict]:
    """All logs of a callTracer frame tree in execution order (failed sub-frames' logs are dropped)."""
    out = []
    if frame.get("error"):
        return out
    out.extend(frame.get("logs", []))
    for c in frame.get("calls", []) or []:
        out.extend(trace_logs(c))
    return out


# ---------------------------------------------------------------- live data readers
class Reader:
    """Typed reads used by the skills' conditions. Each returns (value, source) where source names
    the contract, function and block, so every number in a decision trace can be re-checked."""

    def __init__(self, chain: Chain):
        self.c = chain
        self._dec: dict[str, int] = {}
        self._sym: dict[str, str] = {}
        self._pool_meta: dict[str, tuple] = {}

    def call(self, to: str, sig: str, types=None, args=None, block="latest") -> bytes:
        d = calldata(sig, types, args)
        out = self.c.m.call_tool("eth_call", {"transaction": {"to": to, "data": d}, "block": block})
        r = out["result"] if isinstance(out, dict) else out
        return bytes.fromhex(r[2:])

    def decimals(self, token: str) -> int:
        if token not in self._dec:
            self._dec[token] = abi_decode(["uint8"], self.call(token, "decimals()"))[0]
        return self._dec[token]

    def symbol(self, token: str) -> str:
        if token not in self._sym:
            self._sym[token] = abi_decode(["string"], self.call(token, "symbol()"))[0]
        return self._sym[token]

    def head(self) -> int:
        return self.c.block_number()

    # Uniswap-V3-style pool --------------------------------------------------
    def pool_meta(self, pool: str) -> tuple[str, str, int, int]:
        if pool not in self._pool_meta:
            t0 = to_checksum("0x" + self.call(pool, "token0()")[-20:].hex()).lower()
            t1 = to_checksum("0x" + self.call(pool, "token1()")[-20:].hex()).lower()
            self._pool_meta[pool] = (t0, t1, self.decimals(t0), self.decimals(t1))
        return self._pool_meta[pool]

    def pool_price(self, pool: str, block="latest") -> tuple[Decimal, dict]:
        """Spot price of token0 in token1 units from slot0().sqrtPriceX96, plus in-range liquidity."""
        t0, t1, d0, d1 = self.pool_meta(pool)
        s0 = self.call(pool, "slot0()", block=block)
        sqrt_p = int.from_bytes(s0[:32], "big")
        tick = int.from_bytes(s0[32:64], "big", signed=True)
        liq = int.from_bytes(self.call(pool, "liquidity()", block=block)[:32], "big")
        price = (Decimal(sqrt_p) / Decimal(2 ** 96)) ** 2 * Decimal(10) ** (d0 - d1)
        return price, {"pool": pool, "token0": self.symbol(t0), "token1": self.symbol(t1), "tick": tick,
                       "sqrtPriceX96": sqrt_p, "liquidity": liq, "block": block,
                       "source": f"{pool} slot0()+liquidity() @ {block}"}

    # Chainlink ------------------------------------------------------------------
    def oracle(self, feed: str, block="latest") -> tuple[Decimal, dict]:
        r = self.call(feed, "latestRoundData()", block=block)
        round_id, answer, _, updated, _ = abi_decode(["uint80", "int256", "uint256", "uint256", "uint80"], r)
        dec = abi_decode(["uint8"], self.call(feed, "decimals()", block=block))[0]
        return Decimal(answer) / Decimal(10) ** dec, {"feed": feed, "roundId": round_id, "updatedAt": updated,
                                                     "block": block, "source": f"{feed} latestRoundData() @ {block}"}

    # Gas --------------------------------------------------------------------------
    def gas_price_gwei(self) -> tuple[Decimal, dict]:
        g = self.c.m.call_tool("get_gas_price")
        return Decimal(g["gasPriceWei"]) / Decimal(10 ** 9), {"source": "Chain MCP get_gas_price",
                                                              "priorityFeeWei": g.get("maxPriorityFeePerGasWei")}

    # ERC-20 / native --------------------------------------------------------------
    def erc20_balance(self, token: str, owner: str, block="latest") -> tuple[Decimal, dict]:
        raw = int.from_bytes(self.call(token, "balanceOf(address)", ["address"], [owner], block=block)[:32], "big")
        d = self.decimals(token)
        return Decimal(raw) / Decimal(10) ** d, {"raw": raw, "token": self.symbol(token),
                                                 "source": f"{token} balanceOf({owner}) @ {block}"}

    def native_balance(self, owner: str) -> tuple[Decimal, dict]:
        wei = self.c.native_balance(owner)
        return Decimal(wei) / Decimal(10 ** 18), {"raw": wei, "source": f"Chain MCP get_balance({owner})"}

    # Morpho Blue ---------------------------------------------------------------------
    def morpho_market(self, market_id: str, block="latest") -> tuple[dict, dict]:
        r = self.call(MORPHO_BLUE, "market(bytes32)", ["bytes32"], [bytes.fromhex(market_id[2:])], block=block)
        ts, tss, tb, tbs, last, fee = abi_decode(["uint128"] * 6, r)
        p = self.call(MORPHO_BLUE, "idToMarketParams(bytes32)", ["bytes32"], [bytes.fromhex(market_id[2:])], block=block)
        loan, coll, oracle, irm, lltv = abi_decode(["address", "address", "address", "address", "uint256"], p)
        d = self.decimals(loan.lower())
        sc = Decimal(10) ** d
        m = {"loanToken": self.symbol(loan.lower()), "collateralToken": self.symbol(coll.lower()),
             "totalSupply": Decimal(ts) / sc, "totalBorrow": Decimal(tb) / sc,
             "liquidity": Decimal(ts - tb) / sc, "utilization": (Decimal(tb) / Decimal(ts)) if ts else Decimal(0),
             "lastUpdate": last}
        return m, {"source": f"Morpho Blue {MORPHO_BLUE} market({market_id[:10]}…) @ {block}"}


# ---------------------------------------------------------------- prepare-only output
def prepare_tx(chain: Chain, sender: str, to: str, data: str = "0x", value: int = 0, gas_buffer: float = 1.2,
               gas: int | None = None) -> dict:
    """Build an UNSIGNED EIP-1559 tx (nonce=pending, fees from Chain MCP). Never signs."""
    if gas is None:
        sim = simulate(chain, {"from": sender, "to": to, "data": data, "value": value})
        if not sim["ok"]:
            raise PaymentError(f"refusing to prepare: simulation reverted ({sim['revertReason']})")
        gas = int(sim["gas"] * gas_buffer)
    base, tip, max_fee = chain.fees()
    nonce = chain.nonce(sender, "pending")
    tx = {"chainId": CHAIN_ID, "nonce": nonce, "maxPriorityFeePerGas": tip, "maxFeePerGas": max_fee,
          "gas": gas, "to": to.lower(), "value": value, "data": data}
    raw, h = unsigned_eip1559(tx)
    return {"unsignedTx": {k: (hex(v) if isinstance(v, int) else v) for k, v in tx.items()},
            "unsignedSerialized": raw, "signingHash": h,
            "walletRequest": {"method": "eth_sendTransaction", "params": [{
                "from": to_checksum(sender), "to": to_checksum(to), "value": hex(value), "data": data,
                "gas": hex(gas), "maxFeePerGas": hex(max_fee), "maxPriorityFeePerGas": hex(tip),
                "nonce": hex(nonce), "chainId": hex(CHAIN_ID), "type": "0x2"}]},
            "castCommandForUser": cast_send_command(to, data, value),
            "maxNetworkFeePROS": str(Decimal(gas * max_fee) / Decimal(10 ** 18)),
            "signed": False, "broadcast": False}


def cast_send_command(to: str, data: str, value: int = 0) -> str:
    """The `cast send` line the USER can run with the official pharos-skill-engine (their key, their decision).
    This tool never runs it."""
    rpc = network("mainnet")["rpcUrl"]
    parts = ["cast", "send", to_checksum(to), data or "0x"]
    if value:
        parts += ["--value", str(value)]
    parts += ["--rpc-url", rpc, "--private-key", "$PRIVATE_KEY"]
    return " ".join(shlex.quote(p) if not p.startswith("$") else p for p in parts)


def utc(ts: int | float | None = None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(ts if ts is not None else time.time()))
