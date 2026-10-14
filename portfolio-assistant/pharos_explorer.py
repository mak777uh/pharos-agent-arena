"""Tiny read-only client for the public Pharosscan explorer API (backend of https://www.pharosscan.xyz).

The explorer indexes what plain JSON-RPC cannot answer cheaply: verified-source status, contract creator and
creation tx, a wallet's funding tx and its transaction list. Base URL (public, no key):
    https://api.socialscan.io/pharos-mainnet/v1/explorer
Every figure the tools report that comes from here is labelled "explorer"; chain facts (code, nonce, balances,
allowances, block timestamps) are re-read from the Pharos Chain MCP.
"""
from __future__ import annotations

import time
from urllib.parse import urlparse

import requests

EXPLORER_API = "https://api.socialscan.io/pharos-mainnet/v1/explorer"
EXPLORER_UI = "https://www.pharosscan.xyz"


class ExplorerError(RuntimeError):
    pass


class PharosExplorer:
    def __init__(self, base: str = EXPLORER_API, timeout: int = 30):
        if urlparse(base).scheme != "https":
            raise ValueError(f"refusing non-HTTPS explorer API: {base!r}")
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.http = requests.Session()
        self._profile: dict[str, dict] = {}

    def _get(self, path: str, params: dict | None = None):
        for attempt in range(4):
            r = self.http.get(f"{self.base}/{path}", params=params, timeout=self.timeout)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            if r.status_code >= 400:
                raise ExplorerError(f"explorer {path}: HTTP {r.status_code} {r.text[:200]}")
            return r.json()
        raise ExplorerError(f"explorer {path}: HTTP {r.status_code}")

    def profile(self, addr: str) -> dict:
        if addr not in self._profile:
            self._profile[addr] = self._get(f"address/{addr}/profile")
        return self._profile[addr]

    def transactions(self, addr: str, max_txs: int = 500) -> tuple[int, list[dict]]:
        """Newest-first transaction list of an address (incoming and outgoing), up to max_txs."""
        out, page, total = [], 1, 0
        while len(out) < max_txs:
            d = self._get(f"address/{addr}/transactions", {"page": page, "size": 100})
            total = d.get("total", 0)
            rows = d.get("data") or []
            out.extend(rows)
            if len(rows) < 100:
                break
            page += 1
        return total, out[:max_txs]

    def token_transfers(self, addr: str, max_rows: int = 500) -> tuple[int, list[dict]]:
        out, page, total = [], 1, 0
        while len(out) < max_rows:
            d = self._get(f"address/{addr}/token_transfers", {"page": page, "size": 100})
            total = d.get("total", 0)
            rows = d.get("data") or []
            out.extend(rows)
            if len(rows) < 100:
                break
            page += 1
        return total, out[:max_rows]
