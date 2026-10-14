#!/usr/bin/env python3
"""pharos-docs — search the Pharos developer docs from the terminal via the Developer Docs MCP.

    pharos-docs "how do I deploy a contract?"

The question is sent to the Pharos Developer Docs MCP server (https://docs.pharos.xyz/~gitbook/mcp,
tool `askQuestion`), which answers only from docs.pharos.xyz and lists the pages it used. This CLI
prints that answer with absolute links and a numbered source list. When the server gives no
grounded answer (no sources, or an explicit "can't help / not documented" reply) the CLI does NOT
make one up: it says so, shows the closest pages from `searchDocumentation` (if any) and exits 2.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from urllib.parse import urljoin, urlparse

from pharos_mcp import DOCS_ENDPOINT, MCPError, PharosMCP

__version__ = "1.0.0"
DOCS_BASE = "https://docs.pharos.xyz/"
MAX_QUESTION = 500

# Replies of the docs server that mean "this is not answered by the docs".
NO_ANSWER_PATTERNS = [
    r"\bI can help with (information|questions) about\b",
    r"\b(couldn['’]?t|could not|can['’]?t|cannot|unable to) (find|locate|answer|provide)\b",
    r"\bno (relevant |specific )?(information|documentation|mention|details?)\b",
    r"\b(is|are) not (covered|documented|mentioned)\b",
    r"\bdoes(n['’]?t| not) (contain|cover|mention|document|provide)\b",
    r"\bnot (available|found) in (the|this) documentation\b",
]
SOURCE_LINK_RE = re.compile(r"^\s*[-*]\s*\[([^\]]+)\]\((\S+?)\)\s*$")
MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((/[^)\s]*)\)")


def validate_question(q: str) -> str:
    q = " ".join((q or "").split())
    if len(q) < 3:
        raise ValueError("question is empty or too short")
    if len(q) > MAX_QUESTION:
        raise ValueError(f"question is too long ({len(q)} > {MAX_QUESTION} characters)")
    return q


def is_docs_url(url: str) -> bool:
    u = urlparse(url)
    return u.scheme == "https" and u.hostname == "docs.pharos.xyz"


def split_answer(text: str) -> tuple[str, list[dict]]:
    """Split the askQuestion reply into (answer markdown, [ {title,url} ]) and absolutise links."""
    body, sources = text, []
    m = re.search(r"^#{1,3}\s*Sources\s*$", text, flags=re.M)
    if m:
        body = text[:m.start()]
        seen = set()
        for line in text[m.end():].splitlines():
            lm = SOURCE_LINK_RE.match(line)
            if not lm:
                continue
            url = urljoin(DOCS_BASE, lm.group(2))
            if url not in seen:
                seen.add(url)
                sources.append({"title": lm.group(1), "url": url})
    body = MD_LINK_RE.sub(lambda mm: f"[{mm.group(1)}]({urljoin(DOCS_BASE, mm.group(2))})", body).strip()
    return body, sources


def looks_like_no_answer(body: str, sources: list) -> bool:
    if not body.strip() or not sources:
        return True
    head = body[:400].replace("’", "'")
    return any(re.search(p, head, flags=re.I) for p in NO_ANSWER_PATTERNS)


def cited_urls(body: str) -> set[str]:
    return {u for _, u in re.findall(r"\[([^\]]+)\]\((https://[^)\s]+)\)", body)}


def search_pages(mcp: PharosMCP, query: str, limit: int) -> list[dict]:
    """searchDocumentation -> unique [{title,url,snippet}] (only docs.pharos.xyz links)."""
    res = mcp._request("tools/call", {"name": "searchDocumentation", "arguments": {"query": query}})
    pages, seen = [], set()
    for c in res.get("content", []):
        t = c.get("text", "")
        title = re.search(r"^Title:\s*(.+)$", t, flags=re.M)
        link = re.search(r"^Link:\s*(\S+)$", t, flags=re.M)
        content = t.split("Content:", 1)[1].strip() if "Content:" in t else ""
        if not link or not is_docs_url(link.group(1)) or link.group(1) in seen:
            continue
        seen.add(link.group(1))
        pages.append({"title": title.group(1).strip() if title else link.group(1), "url": link.group(1),
                      "snippet": " ".join(content.split())[:200]})
        if len(pages) >= limit:
            break
    return pages


def ask(mcp: PharosMCP, question: str, max_sources: int) -> dict:
    res = mcp._request("tools/call", {"name": "askQuestion", "arguments": {"question": question}})
    text = "".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
    if res.get("isError"):
        raise MCPError(f"askQuestion failed: {text[:300]}")
    body, sources = split_answer(text)
    sources = [s for s in sources if is_docs_url(s["url"])]
    # Pages the answer links inline come first, then the rest of the server's source list.
    inline = cited_urls(body)
    sources.sort(key=lambda s: s["url"] not in inline)
    out = {"question": question, "endpoint": mcp.endpoint, "server": mcp.server_info.get("name"),
           "found": not looks_like_no_answer(body, sources), "answer": body,
           "sources": sources[:max_sources]}
    if not out["found"]:
        out["server_reply"] = body
        out["answer"] = None
        out["sources"] = []
        out["closest_pages"] = search_pages(mcp, question, 3)
    return out


def render(r: dict) -> str:
    lines = [f"Q: {r['question']}", ""]
    if r["found"]:
        lines += [r["answer"], "", "Sources:"]
        lines += [f"  [{i}] {s['title']} — {s['url']}" for i, s in enumerate(r["sources"], 1)]
    else:
        lines += ["No good answer found in the Pharos docs for this question.",
                  "(Nothing was made up: the Docs MCP returned no grounded answer.)"]
        if r.get("server_reply"):
            lines += [f"Docs MCP said: {r['server_reply'][:300]}"]
        if r.get("closest_pages"):
            lines += ["", "Closest doc pages (may be unrelated):"]
            lines += [f"  - {p['title']} — {p['url']}" for p in r["closest_pages"]]
        lines += ["", "Try rephrasing with Pharos terms (e.g. 'RPC', 'chain ID', 'Foundry', 'MCP'),",
                  "or browse https://docs.pharos.xyz/"]
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="pharos-docs",
                                 description="Ask the Pharos developer docs a question (via the Developer Docs MCP).")
    ap.add_argument("question", nargs="+", help="natural-language question, e.g. \"how do I deploy a contract?\"")
    ap.add_argument("--sources", type=int, default=5, help="max sources to list (1-10, default 5)")
    ap.add_argument("--search", action="store_true", help="list matching doc pages (searchDocumentation) instead of an answer")
    ap.add_argument("--json", action="store_true", help="print machine-readable JSON")
    ap.add_argument("--endpoint", default=DOCS_ENDPOINT, help=f"Docs MCP endpoint (default {DOCS_ENDPOINT}; https only)")
    ap.add_argument("--version", action="version", version=f"pharos-docs {__version__}")
    a = ap.parse_args(argv)
    try:
        question = validate_question(" ".join(a.question))
        if not 1 <= a.sources <= 10:
            raise ValueError("--sources must be between 1 and 10")
        mcp = PharosMCP(a.endpoint, timeout=60)
    except ValueError as e:
        ap.error(str(e))
    except (MCPError, OSError) as e:
        print(f"error: cannot reach the Docs MCP at {a.endpoint}: {e}", file=sys.stderr)
        return 1
    if "askQuestion" not in mcp.tools and not a.search:
        print("error: this MCP server has no askQuestion tool; use --search", file=sys.stderr)
        return 1
    try:
        if a.search:
            pages = search_pages(mcp, question, a.sources)
            r = {"question": question, "found": bool(pages), "pages": pages}
            if a.json:
                print(json.dumps(r, indent=2, ensure_ascii=False))
            elif pages:
                print(f"Q: {question}\n\nMatching doc pages:")
                for p in pages:
                    print(f"  - {p['title']} — {p['url']}\n      {p['snippet']}")
            else:
                print(f"Q: {question}\n\nNo matching doc pages found.")
            return 0 if pages else 2
        r = ask(mcp, question, a.sources)
    except (MCPError, OSError) as e:
        print(f"error: Docs MCP request failed: {e}", file=sys.stderr)
        return 1
    print(json.dumps(r, indent=2, ensure_ascii=False) if a.json else render(r))
    return 0 if r["found"] else 2


if __name__ == "__main__":
    sys.exit(main())
