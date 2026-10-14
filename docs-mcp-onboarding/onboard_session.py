#!/usr/bin/env python3
"""Replayable Docs-MCP-only onboarding session for ai.pharos.xyz.

    python onboard_session.py                      # runs session_plan.json, writes transcript.json + transcript.md
    python onboard_session.py --plan my_plan.json  # your own list of steps

Every step is one call to the Pharos Developer Docs MCP (https://docs.pharos.xyz/~gitbook/mcp):
`askQuestion` (synthesised answer + sources), `searchDocumentation` (matching pages) or `getPage` (full page).
No other documentation source, search engine or community channel is used. The raw answers are saved verbatim
in transcript.json and rendered in transcript.md; the human-written analysis (gaps + fixes) is ONBOARDING_LOG.md.

The `sendFeedback` tool of the Docs MCP is NOT called: it would post a message to the Pharos docs team, which
this tool never does on its own. The suggested fixes are written in ONBOARDING_LOG.md instead.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

from pharos_mcp import DOCS_ENDPOINT, MCPError, PharosMCP

ALLOWED_TOOLS = {"askQuestion", "searchDocumentation", "getPage"}
MAX_TEXT = 800


def text_of(res: dict) -> str:
    return "".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")


def validate_step(s: dict) -> dict:
    if s.get("tool") not in ALLOWED_TOOLS:
        raise ValueError(f"step {s.get('id')}: tool must be one of {sorted(ALLOWED_TOOLS)}")
    arg = (s.get("query") or "").strip()
    if not 3 <= len(arg) <= MAX_TEXT:
        raise ValueError(f"step {s.get('id')}: query must be 3..{MAX_TEXT} characters")
    if s["tool"] == "getPage":
        u = urlparse(arg)
        if u.scheme != "https" or u.hostname != "docs.pharos.xyz":
            raise ValueError(f"step {s.get('id')}: getPage only accepts https://docs.pharos.xyz/... URLs")
    return s


def run_step(mcp: PharosMCP, s: dict) -> dict:
    args = {"askQuestion": {"question": s["query"]}, "searchDocumentation": {"query": s["query"]},
            "getPage": {"url": s["query"]}}[s["tool"]]
    if s["tool"] == "askQuestion" and s.get("goal"):
        args["goal"] = s["goal"]
    t0 = time.time()
    try:
        res = mcp._request("tools/call", {"name": s["tool"], "arguments": args})
        txt, err = text_of(res), bool(res.get("isError"))
    except MCPError as e:
        txt, err = f"MCP error: {e}", True
    links = sorted(set(re.findall(r"https://docs\.pharos\.xyz[^\s)\]>\"']*", txt)) |
                   {"https://docs.pharos.xyz" + p for p in re.findall(r"\]\((/[^)\s]*)\)", txt)})
    return {**s, "arguments": args, "seconds": round(time.time() - t0, 1), "is_error": err,
            "answer": txt, "doc_links": links}


def render_md(tr: dict) -> str:
    out = [f"# Docs MCP session transcript (raw)", "",
           f"Endpoint: `{tr['endpoint']}` · server `{tr['server']}` · protocol {tr['protocol']} · "
           f"started {tr['started_utc']} UTC · {len(tr['steps'])} calls", "",
           "Answers are copied verbatim from the Docs MCP (long `getPage` / search results are cut at 2,500 chars "
           "here; transcript.json has them in full).", ""]
    for s in tr["steps"]:
        out += [f"## Step {s['id']} — {s['intent']}", "",
                f"**Tool:** `{s['tool']}` · **Query:** `{s['query']}`" + (f" · **goal:** `{s['goal']}`" if s.get("goal") else ""),
                f" · {s['seconds']} s" + (" · **ERROR**" if s["is_error"] else ""), "", "```text",
                s["answer"][:2500] + (" …[cut]" if len(s["answer"]) > 2500 else ""), "```", ""]
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Replay a Docs-MCP-only onboarding session and save the transcript.")
    ap.add_argument("--plan", default="session_plan.json")
    ap.add_argument("--out", default="transcript")
    ap.add_argument("--only", help="comma-separated step ids to run (default: all)")
    ap.add_argument("--endpoint", default=DOCS_ENDPOINT)
    a = ap.parse_args(argv)
    try:
        steps = [validate_step(s) for s in json.load(open(a.plan, encoding="utf-8"))["steps"]]
        if a.only:
            keep = {x.strip() for x in a.only.split(",")}
            steps = [s for s in steps if str(s["id"]) in keep]
        mcp = PharosMCP(a.endpoint, timeout=120)
    except (ValueError, OSError, json.JSONDecodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    tr = {"endpoint": mcp.endpoint, "server": mcp.server_info.get("name"), "protocol": mcp.protocol_version,
          "tools_offered": mcp.tools, "started_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
          "steps": []}
    for s in steps:
        r = run_step(mcp, s)
        tr["steps"].append(r)
        print(f"[{r['id']}] {r['tool']}: {r['query'][:70]} -> {len(r['answer'])} chars, {r['seconds']} s"
              + (" ERROR" if r["is_error"] else ""))
    json.dump(tr, open(a.out + ".json", "w", encoding="utf-8"), indent=2, ensure_ascii=False)
    open(a.out + ".md", "w", encoding="utf-8").write(render_md(tr))
    print(f"wrote {a.out}.json and {a.out}.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
