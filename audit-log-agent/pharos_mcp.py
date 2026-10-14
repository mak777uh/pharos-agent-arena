"""Minimal Pharos Chain MCP client (Streamable HTTP, JSON-RPC 2.0).

Talks to the hosted Pharos Chain MCP server (https://mcp.pharos.xyz/mcp)
using the standard MCP handshake: initialize -> notifications/initialized ->
tools/call. No API key and no private key are needed: every tool used here is
read-only.
"""
from __future__ import annotations

import json
import re
import time
from urllib.parse import urlparse

import requests

__version__ = "1.0.0"

DEFAULT_ENDPOINT = "https://mcp.pharos.xyz/mcp"
# Protocol revision we offer in `initialize`; the server answers with the one it
# actually speaks and we use that for the rest of the session.
PROTOCOL_VERSION = "2025-03-26"
ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


def check_address(addr: str) -> str:
    """Validate a 20-byte hex address and return it lower-cased."""
    if not ADDRESS_RE.match(addr or ""):
        raise ValueError(f"not a valid EVM address: {addr!r}")
    return addr.lower()


def check_endpoint(url: str) -> str:
    """Only HTTPS endpoints are accepted (plain HTTP only for localhost self-hosting)."""
    u = urlparse(url)
    if u.scheme == "https" or (u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost", "::1")):
        return url
    raise ValueError(f"refusing insecure MCP endpoint: {url!r} (use https://)")


class MCPError(RuntimeError):
    pass


class PharosMCP:
    def __init__(self, endpoint: str = DEFAULT_ENDPOINT, timeout: int = 30):
        self.endpoint = check_endpoint(endpoint)
        self.timeout = timeout
        self._id = 0
        self._http = requests.Session()  # keeps the load-balancer cookie (session affinity)
        self._headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        self.server_info: dict = {}
        self.protocol_version: str | None = None
        self.tools: list[str] = []
        self._connect()

    # ---- transport -------------------------------------------------------
    def _post(self, body: dict) -> requests.Response:
        for attempt in range(5):
            r = self._http.post(self.endpoint, json=body, headers=self._headers, timeout=self.timeout)
            if r.status_code == 429:  # 120 req/min per IP
                time.sleep(float(r.headers.get("Retry-After", 2 ** attempt)))
                continue
            return r
        return r

    @staticmethod
    def _parse(r: requests.Response) -> dict:
        text = r.text
        if "text/event-stream" in r.headers.get("content-type", ""):
            for line in text.splitlines():
                if line.startswith("data:"):
                    return json.loads(line[5:])
            raise MCPError(f"empty SSE response: {text[:200]}")
        return json.loads(text)

    def _request(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        body = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            body["params"] = params
        r = self._post(body)
        if "mcp-session-id" in r.headers:
            self._headers["mcp-session-id"] = r.headers["mcp-session-id"]
        if r.status_code >= 400:
            raise MCPError(f"HTTP {r.status_code}: {r.text[:300]}")
        msg = self._parse(r)
        if "error" in msg:
            raise MCPError(str(msg["error"]))
        return msg["result"]

    def _connect(self) -> None:
        self._headers.pop("mcp-session-id", None)
        res = self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "pharos-arena-agent", "version": __version__},
        })
        self.server_info = res.get("serverInfo", {})
        self.protocol_version = res.get("protocolVersion")
        if self.protocol_version:
            self._headers["mcp-protocol-version"] = self.protocol_version
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.tools = [t["name"] for t in self._request("tools/list", {}).get("tools", [])]

    # ---- tools -----------------------------------------------------------
    def call_tool(self, name: str, arguments: dict | None = None):
        """Call an MCP tool and return its JSON-decoded text payload."""
        for attempt in range(3):
            try:
                res = self._request("tools/call", {"name": name, "arguments": arguments or {}})
                break
            except MCPError as e:  # session lost (e.g. server restart) -> reconnect once
                if "session" in str(e).lower() and attempt < 2:
                    self._connect()
                    continue
                raise
        text = "".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
        if res.get("isError"):
            raise MCPError(f"{name}: {text[:300]}")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return text
        if isinstance(data, dict) and "error" in data and "result" not in data:
            raise MCPError(f"{name}: {data['error']}")
        return data

    def call_tool_raw(self, name: str, arguments: dict | None = None) -> tuple[bool, str]:
        """Call an MCP tool and return (is_error, full_text) without raising on tool errors.

        Used where the error payload itself is the data we need (e.g. the revert data of a
        failed `eth_call`), so it must not be truncated or turned into an exception."""
        res = self._request("tools/call", {"name": name, "arguments": arguments or {}})
        text = "".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
        is_error = bool(res.get("isError"))
        if not is_error:
            try:
                data = json.loads(text)
                is_error = isinstance(data, dict) and "error" in data and "result" not in data
            except json.JSONDecodeError:
                pass
        return is_error, text

    # convenience wrappers
    def block_number(self) -> int:
        return int(self.call_tool("get_block_number")["blockNumber"])

    def eth_call(self, to: str, data: str, block: int | str = "latest") -> str:
        out = self.call_tool("eth_call", {"transaction": {"to": to, "data": data}, "block": block})
        return out["result"] if isinstance(out, dict) else out

    def get_logs(self, address, topics, from_block: int, to_block: int) -> list:
        out = self.call_tool("get_logs", {"address": address, "topics": topics,
                                          "fromBlock": from_block, "toBlock": to_block})
        return out if isinstance(out, list) else out.get("logs", out.get("result", []))
