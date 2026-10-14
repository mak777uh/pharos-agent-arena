#!/usr/bin/env python3
"""Read live state of a Pharos contract through the Pharos Chain MCP server
and print it in plain language.

Default target: USDC (Circle-deployed) on Pharos Pacific mainnet
  0xc879c018db60520f4355c26ed1a6d572cdac1815
Every on-chain value is read via MCP tools (eth_call, get_storage_at, get_code,
get_balance, get_chain_info, get_block) pinned to ONE block number, so the
snapshot is consistent.

Usage:
  python read_contract_state.py                       # USDC, latest block
  python read_contract_state.py --holder 0xabc...     # also show a balance
  python read_contract_state.py --address 0x... --json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from decimal import Decimal

from pharos_mcp import DEFAULT_ENDPOINT, MCPError, PharosMCP, check_address

USDC = "0xc879c018db60520f4355c26ed1a6d572cdac1815"
EXPLORER = "https://www.pharosscan.xyz"
ZERO = "0x" + "0" * 40
# Well-known Pharos mainnet contracts, from the official token registry:
# https://docs.pharos.xyz/getting-started/token-registry
KNOWN = {
    "0x52c48d4213107b20bc583832b0d951fb9ca8f0b0": "Wrapped PROS (WPROS) token contract",
    "0xc879c018db60520f4355c26ed1a6d572cdac1815": "USDC (Circle-deployed) token contract",
    "0x51e2a24742db77604b881d6781ee16b5b8fcbe29": "Chainlink LINK token contract",
    "0x1f4b7011ee3d53969bb67f59428a9ec0477856e9": "Wrapped ETH (WETH) token contract",
}

# 4-byte selectors = keccak256(signature)[:4]
SEL = {
    "name()": "0x06fdde03",
    "symbol()": "0x95d89b41",
    "decimals()": "0x313ce567",
    "totalSupply()": "0x18160ddd",
    "balanceOf(address)": "0x70a08231",
    "owner()": "0x8da5cb5b",
    "paused()": "0x5c975abb",
    "version()": "0x54fd4d50",
    "currency()": "0xe5a6b10f",
    "pauser()": "0x9fd0506d",
    "masterMinter()": "0x35d99f35",
    "blacklister()": "0xbd102430",
    "rescuer()": "0x38a63183",
}
# Proxy storage slots (raw storage reads via get_storage_at)
SLOTS = {
    # ZeppelinOS AdminUpgradeabilityProxy (used by Circle FiatToken proxies)
    "proxy implementation (zos)": "0x7050c9e0f4ca769c69bd3a8ef740bc37934f8e2c036e5a723fd8ee048ed3f8c3",
    "proxy admin (zos)": "0x10d6a54a4754c8869d6886b5f5d7fbfa5b4522237ea5c60d11bc4e7a1ff9390b",
    # EIP-1967
    "proxy implementation (EIP-1967)": "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc",
    "proxy admin (EIP-1967)": "0xb53127684a568b3173ae13b9f8a6016e243e63b6e8ee1178d6a717850b5d6103",
}
ROLE_FUNCS = ["owner()", "pauser()", "masterMinter()", "blacklister()", "rescuer()"]
ROLE_TEXT = {
    "owner()": "Owner (can reassign roles)",
    "pauser()": "Pauser (can freeze all transfers)",
    "masterMinter()": "Master minter (manages who may mint)",
    "blacklister()": "Blacklister (can block addresses)",
    "rescuer()": "Rescuer (can recover stuck tokens)",
}


def word_to_addr(hexword: str) -> str:
    return "0x" + hexword[-40:].lower()


def decode_string(h: str) -> str:
    raw = bytes.fromhex(h[2:])
    if len(raw) >= 64:
        n = int.from_bytes(raw[32:64], "big")
        return raw[64:64 + n].decode("utf-8", "replace")
    return raw.rstrip(b"\0").decode("utf-8", "replace")  # bytes32-style


def human_amount(raw: int, decimals: int) -> str:
    v = Decimal(raw) / (Decimal(10) ** decimals)
    return f"{v:,.{min(decimals, 6)}f}"


class Reader:
    def __init__(self, mcp: PharosMCP, address: str, block: int):
        self.mcp, self.address, self.block = mcp, address.lower(), block
        self._kind: dict[str, str] = {}

    def call(self, sig: str, arg_addr: str | None = None) -> str | None:
        data = SEL[sig] + ("" if arg_addr is None else arg_addr[2:].lower().rjust(64, "0"))
        try:
            out = self.mcp.eth_call(self.address, data, self.block)
        except MCPError:
            return None  # function not implemented / reverted
        return out if out and out != "0x" else None

    def slot(self, position: str) -> str:
        return self.mcp.call_tool("get_storage_at", {"address": self.address,
                                                      "position": position,
                                                      "block": self.block})["value"]

    def kind(self, addr: str) -> str:
        if addr == ZERO:
            return "zero address (unset)"
        if addr not in self._kind:
            code = self.mcp.call_tool("get_code", {"address": addr, "block": self.block})
            code = code.get("code", "0x") if isinstance(code, dict) else code
            self._kind[addr] = (f"contract, {(len(code) - 2) // 2:,} bytes of code"
                                if code not in ("0x", "", None) else "wallet (EOA)")
        return self._kind[addr]


def read_state(endpoint: str, address: str, holders: list[str]) -> dict:
    address = check_address(address)
    holders = [check_address(h) for h in holders]
    mcp = PharosMCP(endpoint)
    chain = mcp.call_tool("get_chain_info")
    block_no = int(chain["latestBlock"])
    blk = mcp.call_tool("get_block", {"block": block_no, "includeTransactions": False})
    ts = int(blk["timestamp"], 16) if isinstance(blk.get("timestamp"), str) else int(blk.get("timestamp", 0))
    r = Reader(mcp, address, block_no)

    if not chain.get("chainIdMatches", True):
        print(f"WARNING: MCP server reports chainId {chain.get('chainId')}, expected {chain.get('expectedChainId')}",
              file=sys.stderr)
    out: dict = {
        "mcp_server": f"{mcp.server_info.get('name')} {mcp.server_info.get('version')} @ {endpoint}",
        "network": {"name": chain["name"], "chainId": chain["chainId"]},
        "contract": address.lower(),
        "explorer": f"{EXPLORER}/address/{address.lower()}",
        "block": block_no,
        "block_hash": blk.get("hash"),
        "block_time_utc": datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC") if ts else None,
        "contract_kind": r.kind(address.lower()),
        "values": {},
        "roles": {},
        "proxy": {},
        "balances": [],
    }

    name = r.call("name()")
    symbol = r.call("symbol()")
    dec = r.call("decimals()")
    supply = r.call("totalSupply()")
    decimals = int(dec, 16) if dec else 18
    sym = decode_string(symbol) if symbol else ""
    v = out["values"]
    if name: v["name"] = decode_string(name)
    if symbol: v["symbol"] = sym
    if dec: v["decimals"] = decimals
    if supply:
        raw = int(supply, 16)
        v["totalSupply"] = {"raw": str(raw), "human": f"{human_amount(raw, decimals)} {sym}".strip()}
    cur = r.call("currency()")
    if cur: v["currency"] = decode_string(cur)
    ver = r.call("version()")
    if ver: v["version"] = decode_string(ver)
    p = r.call("paused()")
    if p is not None: v["paused"] = bool(int(p, 16))

    labels: dict[str, list[str]] = {address: [f"this contract ({v.get('symbol', 'target')})"]}
    for f in ROLE_FUNCS:
        h = r.call(f)
        if h is None:
            continue
        a = word_to_addr(h)
        out["roles"][f[:-2]] = {"address": a, "label": ROLE_TEXT[f], "kind": r.kind(a)}
        labels.setdefault(a, []).append(ROLE_TEXT[f].split(" (")[0])

    for label, pos in SLOTS.items():
        a = word_to_addr(r.slot(pos))
        if a != ZERO:
            out["proxy"][label] = {"address": a, "kind": r.kind(a)}
            labels.setdefault(a, []).append(label)

    targets = list(dict.fromkeys([x.lower() for x in holders] + [o["address"] for o in out["roles"].values()]
                                 + [address.lower()]))
    for a in targets:
        if a == ZERO:
            continue
        h = r.call("balanceOf(address)", a)
        if h is None:
            continue
        raw = int(h, 16)
        native = mcp.call_tool("get_balance", {"address": a, "block": block_no})
        out["balances"].append({
            "address": a,
            "label": ", ".join(labels.get(a) or [KNOWN.get(a, "holder you asked about")]),
            "token_raw": str(raw),
            "token_human": f"{human_amount(raw, decimals)} {sym}".strip(),
            "native": f"{native.get('balance')} {native.get('symbol', 'PROS')}",
        })
    out["mcp_tools_used"] = ["get_chain_info", "get_block", "eth_call", "get_storage_at", "get_code", "get_balance"]
    return out


def render(s: dict) -> str:
    v = s["values"]
    L = []
    L.append("=" * 72)
    L.append(f" PHAROS CONTRACT STATE  ·  {v.get('name', 'contract')} ({v.get('symbol', '?')})")
    L.append("=" * 72)
    L.append(f" Network      : {s['network']['name']} (chainId {s['network']['chainId']})")
    L.append(f" Contract     : {s['contract']}  [{s['contract_kind']}]")
    L.append(f" Explorer     : {s['explorer']}")
    L.append(f" Read at block: #{s['block']:,}  ({s['block_time_utc']})")
    L.append(f" Block hash   : {s['block_hash']}")
    L.append(f" Source       : Pharos Chain MCP — {s['mcp_server']}")
    L.append("-" * 72)
    L.append(" WHAT THIS TOKEN IS")
    if "name" in v: L.append(f"  • Name ............. {v['name']}")
    if "symbol" in v: L.append(f"  • Ticker ........... {v['symbol']}")
    if "decimals" in v: L.append(f"  • Decimals ......... {v['decimals']}  (1 {v.get('symbol','')} = 10^{v['decimals']} base units)")
    if "totalSupply" in v:
        L.append(f"  • Total supply ..... {v['totalSupply']['human']}")
        L.append(f"                       (raw on-chain value: {v['totalSupply']['raw']})")
    if "currency" in v: L.append(f"  • Backing currency . {v['currency']}")
    if "version" in v: L.append(f"  • Contract version . {v['version']}")
    if "paused" in v:
        L.append(f"  • Transfers ........ {'PAUSED — nobody can move tokens' if v['paused'] else 'active (not paused)'}")
    if s["roles"]:
        L.append("-" * 72)
        L.append(" WHO CONTROLS IT")
        for k, o in s["roles"].items():
            L.append(f"  • {o['label']}")
            L.append(f"      {o['address']}  [{o['kind']}]")
    if s["proxy"]:
        L.append("-" * 72)
        L.append(" UPGRADEABILITY (raw storage slots)")
        L.append("  This address is an upgradeable proxy: the logic lives elsewhere.")
        for k, o in s["proxy"].items():
            L.append(f"  • {k}: {o['address']}  [{o['kind']}]")
    if s["balances"]:
        L.append("-" * 72)
        L.append(" BALANCES")
        for b in s["balances"]:
            L.append(f"  • {b['label']}")
            L.append(f"      {b['address']}: {b['token_human']}   |   gas balance: {b['native']}")
    L.append("=" * 72)
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--address", default=USDC, help="contract to read (default: USDC on Pharos)")
    ap.add_argument("--holder", action="append", default=[], help="extra address to show a balance for (repeatable)")
    ap.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help="Chain MCP endpoint")
    ap.add_argument("--json", action="store_true", help="print machine-readable JSON instead")
    a = ap.parse_args()
    try:
        state = read_state(a.endpoint, a.address, a.holder)
    except ValueError as e:
        ap.error(str(e))
    print(json.dumps(state, indent=2) if a.json else render(state))
    return 0


if __name__ == "__main__":
    sys.exit(main())
