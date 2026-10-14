# Onboarding onto ai.pharos.xyz with the Developer Docs MCP only — log

**Rule I kept:** the only source of information was the Pharos Developer Docs MCP
(`https://docs.pharos.xyz/~gitbook/mcp`, tools `askQuestion`, `searchDocumentation`, `getPage`). No web search,
no GitHub reading, no community chat, no prior knowledge of addresses. The one exception is the Docs MCP endpoint
itself, which I had to know to start (see Gap 1 — the docs give a wrong one).

**Session:** 15 calls on 2026-10-14 22:13 UTC, replayable with `python onboard_session.py`
(plan in [`session_plan.json`](session_plan.json)). Every answer is saved verbatim in
[`transcript.json`](transcript.json) / [`transcript.md`](transcript.md); the quotes below are copied from there.
The facts I learnt were then checked against the live Chain MCP by [`final_run.py`](final_run.py)
(output: [`sample_final_run.txt`](sample_final_run.txt), block #20,255,962).

---

## 1. Step-by-step log (requirement 01)

| # | What I was trying to do | Tool · exact query | What the Docs MCP answered (short) | Verdict |
|---|---|---|---|---|
| 1 | Find out what ai.pharos.xyz is, where to start | `askQuestion` · *What is ai.pharos.xyz and how do I get started with it as a developer?* (goal: *Onboard onto ai.pharos.xyz and build a first read-only agent on Pharos*) | "I cannot find information in the docs identifying **ai.pharos.xyz** itself." Then pointed me to the Pharos MCP: config JSON with `https://mcp.pharos.xyz/mcp`, no API key, `curl …/health`. 14 sources, incl. validator pages ("Using Binary", "Network Snapshots"). | ⚠️ honest, but ai.pharos.xyz is undocumented → **Gap 2** |
| 2 | Find the AI / agent section | `searchDocumentation` · *ai.pharos.xyz agent center MCP SkillEngine* | Hits: Pharos MCP, Publish Service Agent, x402. Nothing named ai.pharos.xyz / Agent Center. | ⚠️ |
| 3 | Get the Chain MCP endpoint + client config | `askQuestion` · *What is the URL of the hosted Pharos Chain MCP server and how do I connect an MCP client to it?* | `https://mcp.pharos.xyz/mcp`, Streamable HTTP, no key; HTTP config + `mcp-remote` stdio bridge; health URL; 120 req/min/IP. | ✅ unblocked me |
| 4 | Read the MCP page in full | `getPage` · `https://docs.pharos.xyz/tooling-and-infrastructure/mcp` | Full page: endpoint table, 14 tools in 4 tables, block params, get_logs cap 1000, non-custodial signing flow, self-hosting env vars (`ENABLE_SEND_RAW_TRANSACTION` default `false`). | ✅ best page in the docs |
| 5 | API key / rate limit | `askQuestion` · *Does the hosted Pharos Chain MCP require an API key, and what are its rate limits?* | No key; 120 req/min/IP; 429 + `Retry-After`. | ✅ (my client already retries on 429) |
| 6 | Can the hosted server broadcast? | `askQuestion` · *Can I send a signed transaction through the hosted Pharos Chain MCP with send_raw_transaction?* | "**Yes.** The hosted Pharos MCP exposes `send_raw_transaction`…" | ❓ the page only says it is *disabled by default in self-hosted* deployments → **Gap 6** |
| 7 | Find the Docs MCP endpoint (to configure my client properly) | `askQuestion` · *What is the endpoint URL of the Pharos Developer Docs MCP server?* | "The Pharos Developer Docs MCP server endpoint is **`https://mcp.pharos.xyz/mcp`**." | ❌ **wrong** — that is the Chain MCP; I was talking to `https://docs.pharos.xyz/~gitbook/mcp` → **Gap 1** |
| 8 | SkillEngine: what it is, how to install | `askQuestion` · *What is Pharos SkillEngine and how do I install or use a skill from it?* | Toolkit of `SKILL.md` + Foundry `cast/forge`; "`unzip pharos-skill-engine-0.1.0.zip`"; `export PRIVATE_KEY=…`; Piggy Bank tutorial. | ⚠️ never says where to download the zip → **Gap 7** |
| 9 | Make a share link on ai.pharos.xyz (the Arena's preferred proof) | `askQuestion` · *How do I create a share link for my agent session on ai.pharos.xyz?* | "I cannot find information about creating a share link for an agent session in the docs." | ❌ → **Gap 3** |
| 10 | Mainnet parameters | `askQuestion` · *What is the chain ID, RPC URL and block explorer of Pharos mainnet?* | 1672 · `https://rpc.pharos.xyz` · `https://www.pharosscan.xyz` | ✅ (chain id confirmed live) |
| 11 | Testnet + faucet | `askQuestion` · *What is the chain ID and RPC URL of the Pharos Atlantic testnet and where is the faucet?* | 688689 · `https://atlantic.dplabs-internal.com` · Faucet: "The Piggy Bank Tutorial is referenced, but its URL isn't specified… tokens can be requested directly from the Pharos team." | ⚠️ no faucet → **Gap 5** |
| 12 | Tool names + exact arguments, to write a client | `askQuestion` · *List the tools exposed by the Pharos Chain MCP server and their arguments.* | "The MCP page lists **13 tools**… `get_block` — `block`; optionally `fullTransactions`… `estimate_gas` — `transaction`; optionally `block`… `get_logs` — `filter` object…" | ❌ count and 3 argument names are wrong vs the live `tools/list` → **Gap 4** |
| 13 | An official contract to read | `askQuestion` · *What is the USDC contract address on Pharos mainnet?* | `0xc879c018db60520f4355c26ed1a6d572cdac1815` (Token Registry) | ✅ (`totalSupply()` works on it) |
| 14 | Block-range limit for logs | `askQuestion` · *What is the maximum block range for get_logs on the Pharos Chain MCP or RPC?* | MCP: 1,000 blocks/call. JSON-RPC: "100 blocks when rate limiting is enabled; 10,000 blocks as the fallback cap." | ✅ MCP cap verified live (1,001 rejected); the RPC part is hard to act on → **Gap 8** |
| 15 | Last try: any docs on ai.pharos.xyz sign-in / workspace / share | `searchDocumentation` · *ai.pharos.xyz sign in workspace share* | "About Pharos", "x402" — nothing about ai.pharos.xyz. | ❌ confirms Gap 2/3 |

Where the docs got me unstuck: steps 3–5, 10, 13, 14 — everything needed to talk to the chain through the hosted
Chain MCP was there and correct, and the full MCP page (step 4) is excellent. Where they failed: the AI-platform
side (ai.pharos.xyz, share links, SkillEngine download, Docs MCP endpoint) and machine-level details (tool schemas).

---

## 2. Gaps and breakdowns, each with the exact query (requirement 02) and a suggested fix (requirement 03)

### Gap 1 — the Docs MCP gives the wrong endpoint for itself  *(wrong answer, high impact)*
* **Query (step 7, `askQuestion`):** `What is the endpoint URL of the Pharos Developer Docs MCP server?`
* **Answer:** "The Pharos Developer Docs MCP server endpoint is **`https://mcp.pharos.xyz/mcp`**."
* **Why it is wrong:** `https://mcp.pharos.xyz/mcp` is the *Chain* MCP (`serverInfo.name = pharos-mcp-server`, chain
  tools only). The Docs MCP that produced this answer runs at `https://docs.pharos.xyz/~gitbook/mcp`
  (`serverInfo.name = docs.pharos.xyz/~gitbook/mcp`, tools `searchDocumentation/getPage/askQuestion/sendFeedback`) —
  recorded in `transcript.json` (`endpoint`, `server`, `tools_offered`). A newcomer following the answer would
  connect the chain server and never find doc tools.
* **Fix — what the docs should say:** add a "Developer Docs MCP" section (on the MCP page or its own page):
  *"Docs MCP endpoint: `https://docs.pharos.xyz/~gitbook/mcp` (Streamable HTTP, no key). Tools:
  `searchDocumentation`, `getPage`, `askQuestion`, `sendFeedback`. This is different from the Chain MCP
  `https://mcp.pharos.xyz/mcp`, which reads chain data."* plus a config snippet that registers both servers.

### Gap 2 — ai.pharos.xyz itself is not documented  *(missing)*
* **Queries:** step 1 `askQuestion` *What is ai.pharos.xyz and how do I get started with it as a developer?*;
  step 2 `searchDocumentation` *ai.pharos.xyz agent center MCP SkillEngine*; step 15 `searchDocumentation`
  *ai.pharos.xyz sign in workspace share*.
* **Answer:** "I cannot find information in the docs identifying **ai.pharos.xyz** itself." Searches return MCP /
  x402 / "About Pharos" pages.
* **Fix:** a "Pharos AI toolkit (ai.pharos.xyz)" landing page: what the site is, how to sign in, what the three
  pieces are (Chain MCP, Docs MCP, SkillEngine) with one link each, and a 5-minute "first agent" walkthrough.
  The MCP page and Skill Engine guide should link back to it.

### Gap 3 — no docs on share links  *(missing; blocks the Arena's preferred proof format)*
* **Query (step 9, `askQuestion`):** `How do I create a share link for my agent session on ai.pharos.xyz?`
* **Answer:** "I cannot find information about creating a share link for an agent session in the docs. The Anvita
  Flow links to the chat interface, but doesn't describe session sharing."
* **Fix:** on the ai.pharos.xyz page: *"To share a session: open the chat → Share → copy link; the link is public
  read-only and shows the tool calls and results"* (or whatever the real flow is), including whether a login is
  needed to open it.

### Gap 4 — tool count and argument names for the Chain MCP are wrong  *(wrong answer, verified live)*
* **Query (step 12, `askQuestion`):** `List the tools exposed by the Pharos Chain MCP server and their arguments.`
* **Answer vs live `tools/list`** (checked by `final_run.py`, check E):

  | Docs MCP said | Live server schema |
  |---|---|
  | "The MCP page lists **13 tools**" | 14 tools (the page's own tables also list 14) |
  | `get_block` — `block`; optionally `fullTransactions` | `block`, `includeTransactions` |
  | `get_logs` — `filter` object with `fromBlock`, `toBlock`, `address`, `topics` | flat `address`, `topics`, `fromBlock`, `toBlock` (no `filter`) |
  | `estimate_gas` — `transaction`; optionally `block` | `transaction` only |

  The answer itself admits "It doesn't publish their exact MCP JSON schemas, so the arguments below summarize the
  corresponding JSON-RPC inputs" — i.e. it guessed from JSON-RPC.
* **Fix:** add an "Arguments" column to each tool table on the MCP page, generated from the server's `inputSchema`
  (e.g. `get_logs(address?, topics?, fromBlock, toBlock)`, `get_block(block, includeTransactions=false)`,
  `estimate_gas(transaction)`), so the answer engine has the real schema to quote.

### Gap 5 — testnet faucet missing / confusing  *(missing)*
* **Query (step 11, `askQuestion`):** `What is the chain ID and RPC URL of the Pharos Atlantic testnet and where is the faucet?`
* **Answer:** "Faucet: The Piggy Bank Tutorial is referenced, but its URL isn't specified. The guide also says testnet
  tokens can be requested directly from the Pharos team."
* **Fix:** Atlantic Testnet page: *"Faucet: `<url>` (limit X PROS / 24 h). If it is empty, ask in `<official
  channel>`."* — one sentence with a link, not a pointer to a tutorial.

### Gap 6 — is broadcasting enabled on the hosted server? Answers contradict the page  *(confusing)*
* **Query (step 6, `askQuestion`):** `Can I send a signed transaction through the hosted Pharos Chain MCP with send_raw_transaction?`
* **Answer:** "**Yes.** The hosted Pharos MCP exposes `send_raw_transaction`…". Step 1 said the same ("The hosted
  endpoint also exposes a transaction-broadcasting tool"). The MCP page (step 4) only says it is "disabled by
  default in self-hosted deployments" and never states the hosted setting.
* **Live check:** the tool is in `tools/list`; a probe with the single byte `0x00` (not a transaction) returns
  "rawTransaction is too short to be a signed EVM transaction" — input validation runs before any enable check, so
  the probe cannot settle it, and I did not send a real signed transaction (no funds, no keys). **Unresolved.**
* **Fix:** one line in the endpoint table: *"Broadcasting (`send_raw_transaction`): enabled / disabled on the hosted
  endpoint"*, and the error text returned when it is disabled.

### Gap 7 — SkillEngine install starts from a zip with no download link  *(missing)*
* **Query (step 8, `askQuestion`):** `What is Pharos SkillEngine and how do I install or use a skill from it?`
* **Answer:** "1. Unzip the Skill Engine package… `unzip pharos-skill-engine-0.1.0.zip`" — no URL, no checksum.
  It also tells the reader to `export PRIVATE_KEY=0x…` before saying which operations actually need it.
* **Fix:** *"Download `pharos-skill-engine-<version>.zip` from `<official URL>` (SHA-256 `<hash>`)"*; say which
  skills are read-only and need no key, and recommend a fresh testnet key / hardware wallet for the rest.

### Gap 8 — log-range answer for JSON-RPC is hard to act on; noisy sources  *(confusing, minor)*
* **Query (step 14):** `What is the maximum block range for get_logs on the Pharos Chain MCP or RPC?` → "JSON-RPC
  `eth_getLogs`: 100 blocks when rate limiting is enabled; 10,000 blocks as the fallback cap." A user of the public
  RPC cannot know whether "rate limiting is enabled".
* **Fix:** *"Public RPC `https://rpc.pharos.xyz`: max N blocks per `eth_getLogs`."*
* Also: answers list 10–19 "sources", often unrelated (step 1 cites validator "Using Binary" and "Network
  Snapshots" pages for an AI-onboarding question). Fix: return only pages actually used in the answer.

---

## 3. What I built and ran by the end (requirement 04)

Using only the facts above I wrote [`final_run.py`](final_run.py) — a read-only agent check against the hosted
Chain MCP — and ran it on Pharos mainnet ([`sample_final_run.txt`](sample_final_run.txt)):

* handshake with `https://mcp.pharos.xyz/mcp` (server `pharos-mcp-server 0.1.0`, protocol 2025-03-26, 14 tools);
  `get_chain_info` → chainId **1672** ✅; `/health` → `{"status":"ok",…}` ✅
* `eth_call` USDC `totalSupply()` at block #20,255,962 → **23,219,860.017406 USDC** ✅
* `get_logs` USDC `Transfer` over exactly 1,000 blocks → **78 transfers**, latest
  [`0x0bda0dc0…f5430`](https://www.pharosscan.xyz/tx/0x0bda0dc022e9f28ebd59e2e1f89ccf95fca0303e2c4d530c0fd1c9bc099f5430);
  a 1,001-block window is rejected exactly as documented ✅
* schema comparison → 3 argument names from the Docs MCP answer don't exist on the live server (Gap 4) ❌
* broadcasting probe → inconclusive without a real signed tx (Gap 6) ❓

Not done: I did not use ai.pharos.xyz's web UI or produce a share link (no docs for it — Gaps 2/3), did not run
SkillEngine (no download link — Gap 7), and did not send `sendFeedback` to the docs team (that posts a message on
my behalf; the fixes are written here instead, ready to be submitted by a human).
