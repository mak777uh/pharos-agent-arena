#!/usr/bin/env python3
"""Continuously watch a Pharos contract for one ERC-20 event via the Pharos
Chain MCP server (`get_logs` filtered by contract address + event signature
topic) and report every NEW occurrence exactly once.

Default: `Transfer` events of USDC (Circle-deployed) on Pharos Pacific mainnet
  0xc879c018db60520f4355c26ed1a6d572cdac1815

How it works
  * a block cursor advances over [next_block, head - confirmations] in chunks of
    at most 1000 blocks (the MCP server's get_logs limit);
  * every log is keyed by (transactionHash, logIndex); keys already reported are
    skipped, so overlapping/re-polled ranges never print duplicates;
  * optional --state-file persists the cursor + recent keys, so a restart
    continues where it stopped instead of re-reporting.

Usage:
  python event_log_monitor.py                           # follow new USDC transfers forever
  python event_log_monitor.py --from-block -200         # also replay the last 200 blocks first
  python event_log_monitor.py --event Approval --address 0x...
  python event_log_monitor.py --max-events 5 --jsonl events.jsonl
"""
from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from collections import deque
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from pharos_mcp import DEFAULT_ENDPOINT, MCPError, PharosMCP, check_address

USDC = "0xc879c018db60520f4355c26ed1a6d572cdac1815"
EXPLORER = "https://www.pharosscan.xyz"
MAX_RANGE = 1000  # get_logs: max 1000 blocks per call
ZERO = "0x" + "0" * 40

# topic0 = keccak256(event signature)
EVENTS = {
    "Transfer": {
        "signature": "Transfer(address,address,uint256)",
        "topic0": "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
        "fields": ("from", "to", "amount"),
    },
    "Approval": {
        "signature": "Approval(address,address,uint256)",
        "topic0": "0x8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925",
        "fields": ("owner", "spender", "amount"),
    },
}

_stop = False


def _handle_stop(*_):
    global _stop
    _stop = True


def decode_string(h: str) -> str:
    raw = bytes.fromhex(h[2:])
    if len(raw) >= 64:
        n = int.from_bytes(raw[32:64], "big")
        return raw[64:64 + n].decode("utf-8", "replace")
    return raw.rstrip(b"\0").decode("utf-8", "replace")


def token_meta(mcp: PharosMCP, address: str) -> tuple[str, int]:
    try:
        sym = decode_string(mcp.eth_call(address, "0x95d89b41"))  # symbol()
    except (MCPError, ValueError):
        sym = "?"
    try:
        dec = int(mcp.eth_call(address, "0x313ce567"), 16)  # decimals()
    except (MCPError, ValueError):
        dec = 18
    return sym, dec


def hexint(x) -> int:
    return int(x, 16) if isinstance(x, str) else int(x)


def decode_log(log: dict, ev: dict, symbol: str, decimals: int) -> dict:
    topics = log["topics"]
    a_name, b_name, amt_name = ev["fields"]
    a = "0x" + topics[1][-40:]
    b = "0x" + topics[2][-40:]
    raw = int(log["data"], 16) if log.get("data") not in (None, "0x") else 0
    human = Decimal(raw) / (Decimal(10) ** decimals)
    ts = log.get("blockTimestamp")
    kind = ""
    if ev is EVENTS["Transfer"]:
        kind = "mint" if a == ZERO else "burn" if b == ZERO else "transfer"
    return {
        "event": ev["signature"].split("(")[0],
        "kind": kind,
        a_name: a,
        b_name: b,
        amt_name: f"{human:,.{min(decimals, 6)}f} {symbol}",
        "amount_raw": str(raw),
        "tx_hash": log["transactionHash"],
        "log_index": hexint(log["logIndex"]),
        "block": hexint(log["blockNumber"]),
        "time_utc": datetime.fromtimestamp(hexint(ts), timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if ts else None,
        "explorer": f"{EXPLORER}/tx/{log['transactionHash']}",
    }


def fmt(e: dict) -> str:
    keys = [k for k in e if k not in ("event", "kind", "amount_raw", "tx_hash", "log_index", "block",
                                      "time_utc", "explorer")]
    a, b, amt = keys
    tag = f" [{e['kind']}]" if e["kind"] else ""
    return (f"[NEW {e['event']}{tag}] block #{e['block']:,}  {e['time_utc'] or ''} UTC\n"
            f"    {a:<7}: {e[a]}\n"
            f"    {b:<7}: {e[b]}\n"
            f"    {amt:<7}: {e[amt]}  (raw {e['amount_raw']})\n"
            f"    tx     : {e['tx_hash']}  (log #{e['log_index']})\n"
            f"    link   : {e['explorer']}")


class State:
    """Cursor + bounded set of already-reported (tx_hash, log_index) keys."""

    def __init__(self, path: Path | None, keep: int = 50_000):
        self.path, self.next_block = path, None
        self.order: deque[str] = deque(maxlen=keep)
        self.seen: set[str] = set()
        if path and path.exists():
            d = json.loads(path.read_text())
            self.next_block = d.get("next_block")
            for k in d.get("seen", []):
                self.add(k)

    def add(self, key: str) -> bool:
        if key in self.seen:
            return False
        if len(self.order) == self.order.maxlen:
            self.seen.discard(self.order[0])
        self.order.append(key)
        self.seen.add(key)
        return True

    def save(self) -> None:
        if self.path:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"next_block": self.next_block, "seen": list(self.order)[-5000:]}))
            tmp.replace(self.path)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--address", default=USDC, help="contract to watch (default: USDC on Pharos)")
    ap.add_argument("--event", choices=sorted(EVENTS), default="Transfer")
    ap.add_argument("--from-block", type=int, default=None,
                    help="start block; negative = that many blocks before head (default: head, i.e. only new events)")
    ap.add_argument("--interval", type=float, default=3.0, help="seconds between polls (default 3)")
    ap.add_argument("--confirmations", type=int, default=0, help="stay N blocks behind head (default 0)")
    ap.add_argument("--state-file", type=Path, default=None, help="persist cursor + seen keys here (resume on restart)")
    ap.add_argument("--jsonl", type=Path, default=None, help="also append each decoded event as a JSON line")
    ap.add_argument("--max-events", type=int, default=0, help="stop after N new events (0 = run forever)")
    ap.add_argument("--duration", type=float, default=0, help="stop after N seconds (0 = run forever)")
    ap.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help="Chain MCP endpoint")
    a = ap.parse_args()
    try:
        address = check_address(a.address)
        mcp = PharosMCP(a.endpoint)
    except ValueError as e:
        ap.error(str(e))

    ev = EVENTS[a.event]
    symbol, decimals = token_meta(mcp, address)
    st = State(a.state_file)
    head = mcp.block_number()
    if st.next_block is None:
        if a.from_block is None:
            st.next_block = head + 1
        elif a.from_block < 0:
            st.next_block = max(0, head + a.from_block)
        else:
            st.next_block = a.from_block

    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)
    print(f"Pharos event monitor · server {mcp.server_info.get('name')} {mcp.server_info.get('version')} "
          f"@ {a.endpoint}\n"
          f"  contract : {address} ({symbol}, {decimals} decimals)  {EXPLORER}/address/{address}\n"
          f"  event    : {ev['signature']}  topic0 {ev['topic0']}\n"
          f"  head     : #{head:,}   watching from block #{st.next_block:,}   (Ctrl+C to stop)\n", flush=True)

    started, reported, polls = time.time(), 0, 0
    jsonl = a.jsonl.open("a") if a.jsonl else None
    try:
        while not _stop:
            polls += 1
            try:
                head = mcp.block_number()
                target = head - a.confirmations
                while st.next_block <= target and not _stop:
                    to_block = min(st.next_block + MAX_RANGE - 1, target)
                    logs = mcp.get_logs(address, [ev["topic0"]], st.next_block, to_block)
                    logs.sort(key=lambda l: (hexint(l["blockNumber"]), hexint(l["logIndex"])))
                    for log in logs:
                        if log.get("removed") or len(log.get("topics", [])) < 3:
                            continue
                        if log["address"].lower() != address or log["topics"][0].lower() != ev["topic0"]:
                            continue  # defensive: never trust the filter blindly
                        key = f"{log['transactionHash'].lower()}:{hexint(log['logIndex'])}"
                        if not st.add(key):
                            continue  # duplicate -> already reported
                        e = decode_log(log, ev, symbol, decimals)
                        print(fmt(e), flush=True)
                        if jsonl:
                            jsonl.write(json.dumps(e) + "\n")
                            jsonl.flush()
                        reported += 1
                        if a.max_events and reported >= a.max_events:
                            raise StopIteration
                    st.next_block = to_block + 1
                    st.save()
            except (MCPError, OSError) as err:  # transient network / server error: back off and retry
                print(f"  ! poll error: {err} — retrying", file=sys.stderr, flush=True)
            if a.duration and time.time() - started >= a.duration:
                break
            time.sleep(a.interval)
    except StopIteration:
        pass
    finally:
        st.save()
        if jsonl:
            jsonl.close()
    print(f"\nStopped. {reported} new event(s) reported over {polls} poll(s); "
          f"cursor at block #{st.next_block:,}.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
