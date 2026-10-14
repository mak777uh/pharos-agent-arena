# Pharos Agent Arena · Doc-Search CLI (`pharos-docs`)

Ask the Pharos developer docs a question from your terminal and get a concise answer **with links to the
doc sections it came from**:

```bash
pharos-docs "how do I deploy a contract?"
```

It is a small wrapper on top of the **Pharos Developer Docs MCP** — the MCP server of
[docs.pharos.xyz](https://docs.pharos.xyz/) at **`https://docs.pharos.xyz/~gitbook/mcp`** (Streamable HTTP,
public, no API key; server name `docs.pharos.xyz/~gitbook/mcp`). It exposes `searchDocumentation`, `getPage`,
`askQuestion` and `sendFeedback`; this CLI uses only the read-only `askQuestion` and `searchDocumentation`
(it never calls `sendFeedback`).

Built for the Pharos *Season of AI · Agent Arena* task
[Doc-Search CLI](https://pharos-agent-arena.vercel.app/tasks/doc-search-cli).

## How it works

1. The question is validated (3–500 characters after whitespace normalisation) and sent to the Docs MCP tool
   `askQuestion`, which answers only from docs.pharos.xyz and appends a `# Sources` list.
2. The CLI splits the reply into the answer and the sources, turns relative doc links (`/getting-started/...`)
   into absolute `https://docs.pharos.xyz/...` links, keeps only `https://docs.pharos.xyz` sources, removes
   duplicates, puts pages the answer links inline first, and prints at most `--sources` (default 5) as `[n] Title — URL`.
3. **No good answer → no invented answer.** If the reply has no sources, or reads as a refusal/"not covered"
   (regexes in `NO_ANSWER_PATTERNS`, checked on the first 400 characters), the CLI prints
   `No good answer found in the Pharos docs for this question.`, quotes what the server said, lists up to 3
   closest pages from `searchDocumentation` clearly marked *may be unrelated*, and exits with code **2**.
4. `--search` skips the answer and lists matching doc pages with a snippet (`searchDocumentation`).

Exit codes: `0` answer found · `2` no good answer / bad input · `1` Docs MCP unreachable or failed.

## Install

Python 3.10+ (tested with Python 3.12).

```bash
git clone https://github.com/mak777uh/pharos-agent-arena.git
cd pharos-agent-arena/doc-search-cli
python3 -m venv .venv && . .venv/bin/activate
pip install .            # installs the `pharos-docs` command (dependency: requests)
```

Without installing: `pip install -r requirements.txt` and run `python pharos_docs.py "<question>"`.

## Usage

```bash
pharos-docs "how do I deploy a contract?"
pharos-docs --sources 3 "What is the chain ID and RPC URL of Pharos mainnet?"
pharos-docs --json "What does the get_logs tool of the Pharos MCP do?"   # machine-readable
pharos-docs --search "gas price"                                          # list matching pages only
```

Options: `--sources N` (1–10, default 5) · `--search` · `--json` · `--endpoint URL` (default
`https://docs.pharos.xyz/~gitbook/mcp`; only `https://`, or `http://` to localhost) · `--version`.
An answer takes ~5–12 s (the docs server synthesises it).

## Example queries (real runs, 2026-10-14)

Full transcript: [`sample_output.txt`](sample_output.txt); JSON example: [`sample_output.json`](sample_output.json).
The Docs MCP generates the wording, so a re-run can phrase the answer slightly differently.

### 1. `pharos-docs "how do I deploy a contract?"`

````
For a Solidity contract on Pharos, one documented option is **Foundry**:

1. Compile and test your contract:

   ```sh
   forge build
   forge test
   ```
2. Create a deployment script that loads your private key with `vm.envUint("PRIVATE_KEY")`, starts broadcasting, and deploys your contract.
3. Set the key and deploy:

   ```sh
   export PRIVATE_KEY=<your-private-key>
   forge script script/DeployToken.s.sol --rpc-url <PHAROS_RPC_URL> --broadcast
   ```

Use a funded testnet wallet, and pass any required constructor arguments in the deployment script. The [Write Your First Token](https://docs.pharos.xyz/developer-guide/foundry/write-your-first-token) includes a complete deployment script and verification commands.

Sources:
  [1] Write Your First Token — https://docs.pharos.xyz/developer-guide/foundry/write-your-first-token
  [2] Write Your First Uniswap Contract — https://docs.pharos.xyz/developer-guide/foundry/write-your-first-uniswap-contract
  [3] Write Your First Uniswap Contract — https://docs.pharos.xyz/developer-guide/hardhat/write-your-first-uniswap-contract
  [4] Write Your First NFT — https://docs.pharos.xyz/developer-guide/hardhat/write-your-first-nft
  [5] Write Your First NFT — https://docs.pharos.xyz/developer-guide/foundry/write-your-first-nft
[exit code 0]
````

### 2. `pharos-docs "What is the chain ID and RPC URL of Pharos mainnet?"`

```
Pharos Pacific Mainnet uses **chain ID `1672`** and RPC URL **`https://rpc.pharos.xyz`**. See [Pacific Mainnet](https://docs.pharos.xyz/getting-started/network/pacific-mainnet).

Sources:
  [1] Pacific Mainnet — https://docs.pharos.xyz/getting-started/network/pacific-mainnet
  [2] Node Debugging & Configuration — https://docs.pharos.xyz/node-and-validator-guide/node-debugging-and-configuration
  [3] ZAN RPC — https://docs.pharos.xyz/tooling-and-infrastructure/rpc/zan
  [4] Atlantic Testnet — https://docs.pharos.xyz/getting-started/network/atlantic-testnet
  [5] RPC Infrastructure — https://docs.pharos.xyz/tooling-and-infrastructure/rpc
[exit code 0]
```

### 3. `pharos-docs "How do I connect Claude Desktop to the Pharos MCP server?"`

````
Add the Pharos server to Claude Desktop’s MCP configuration:

```json
{
  "mcpServers": {
    "pharos": {
      "type": "http",
      "url": "https://mcp.pharos.xyz/mcp"
    }
  }
}
```

If your configuration already has an `mcpServers` object, add the `pharos` entry inside it. Save the configuration and reconnect Claude Desktop. No API key is required.
...
Sources:
  [1] Pharos MCP — https://docs.pharos.xyz/tooling-and-infrastructure/mcp
  ...
[exit code 0]
````

### "No good answer found" — `pharos-docs "how to bake a chocolate cake"`

```
No good answer found in the Pharos docs for this question.
(Nothing was made up: the Docs MCP returned no grounded answer.)
Docs MCP said: I can help with questions about the topics covered in this documentation, but I can’t provide a chocolate cake recipe.

Closest doc pages (may be unrelated):
  - Pharos Skill Engine — https://docs.pharos.xyz/tooling-and-infrastructure/pharos-skill-engine-guide
  - Piggy Bank Tutorial — https://docs.pharos.xyz/tooling-and-infrastructure/pharos-skill-engine-guide/piggybank-skill-guide
  - Publish Service Agent — https://docs.pharos.xyz/tooling-and-infrastructure/overview/publish-skill-af

Try rephrasing with Pharos terms (e.g. 'RPC', 'chain ID', 'Foundry', 'MCP'),
or browse https://docs.pharos.xyz/
[exit code 2]
```

A subtler case — the docs simply don't cover it — is in [`sample_no_answer.txt`](sample_no_answer.txt)
(`"Is Morpho deployed on Pharos mainnet? …"` → the Docs MCP says it *cannot confirm* → no answer, exit 2).

Input validation (also in the transcript): `pharos-docs "?"` → `error: question is empty or too short`;
`--endpoint http://docs.pharos.xyz/...` → `refusing insecure MCP endpoint`.

## How it covers the task requirements

| # | Requirement | Implementation |
|---|-------------|----------------|
| 01 | Natural-language question as input, e.g. `pharos-docs "how do I deploy a contract?"` | `pharos-docs` console script (`pyproject.toml`), question as positional argument(s) |
| 02 | Concise answer + links/references to source doc sections | `askQuestion` answer with absolute links + numbered `Sources:` list (only `https://docs.pharos.xyz` URLs) |
| 03 | Handles "no good answer found" gracefully, no invention | `looks_like_no_answer()` → explicit message, server's own reply quoted, closest pages marked as possibly unrelated, exit code 2 |
| 04 | README with install, usage and 3 example queries with outputs | this file + [`sample_output.txt`](sample_output.txt) |

## Files

* `pharos_docs.py` — the CLI. · `pharos_mcp.py` — minimal MCP Streamable-HTTP client (shared with the other
  builds in this repo; decodes MCP responses as UTF-8). · `pyproject.toml` — installs `pharos-docs`.

## Security

Read-only: no wallet, no private key, no API key, no transactions. Only HTTPS endpoints are accepted.

License: MIT.
