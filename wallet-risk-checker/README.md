# Pharos Agent Arena · Wallet Risk Checker (pre-transaction check, Pharos mainnet)

Before you send funds to, approve, or call an address, run it through this checker. It reads the address's
on-chain history on **Pharos Pacific mainnet (chainId 1672)**, checks **8 signals**, and prints a
**risk level (LOW / MEDIUM / HIGH)** with every red flag explained in plain language and linked to evidence on
Pharosscan. Read-only: it never signs or sends anything.

Built for the Pharos *Season of AI · Agent Arena* task
[Wallet Risk Checker](https://pharos-agent-arena.vercel.app/tasks/wallet-risk-checker).

## Data sources

| Source | Used for |
|---|---|
| **Pharos Chain MCP** `https://mcp.pharos.xyz/mcp` (hosted, read-only, no key) | `get_code` (contract or wallet; code at `creation_block − 1` must be empty → creation block confirmed), `get_transaction_count` (txs sent), `get_balance`, `get_block` (timestamps → all ages), `get_storage_at` (EIP-1967 implementation slot), `get_transaction` (exact calldata of each approval: spender + amount), `get_transaction_receipt` (creation block), `eth_call` `allowance(owner,spender)` / `isApprovedForAll` / `symbol()` (is the approval **still open now**?) |
| **Pharosscan explorer API** `https://api.socialscan.io/pharos-mainnet/v1/explorer` (public backend of [pharosscan.xyz](https://www.pharosscan.xyz)) | what a node cannot index: verified-source flag, contract creator + creation tx, wallet funding tx, the address's transaction list (newest `--max-txs`, default 500) |

## Signals (requirement 01 — at least 4)

| # | Signal | Flags |
|---|---|---|
| S1 | **Contract age** | < 1 day → HIGH, < `--fresh-days` (7) → MEDIUM, unknown → LOW |
| S2 | **Verified code** | unverified source → MEDIUM (HIGH if also younger than `--fresh-days`); EIP-1967 proxy → LOW, proxy whose implementation is unverified → HIGH |
| S3 | **Freshly funded wallet** | first PROS < `--fresh-days` ago **and** < 20 txs sent → MEDIUM (HIGH if < 1 day); never funded → MEDIUM (typo / look-alike address?) |
| S4 | **Unusual approval patterns** | every `approve` / `increaseAllowance` / `setApprovalForAll` / Permit2 `approve` the wallet sent, re-checked with `eth_call` **now**: open approval to a plain wallet (EOA) → HIGH; open approval to an unverified contract → MEDIUM (HIGH if unlimited and the spender is younger than `--fresh-days`); unlimited approvals to verified contracts → one LOW flag listing them |
| S5 | **Interaction with flagged addresses** | any tx with an address on your flag list (`flagged_addresses.json` or `--flag ADDR:reason`) → HIGH; the checked address itself on the list → HIGH; tx with an unverified contract created < `--fresh-days` ago → MEDIUM |
| S6 | Failed-tx share | > 30 % of ≥ 10 scanned txs reverted → LOW (> 50 % → MEDIUM) |
| S7 | Contract bytecode | runtime code contains a real `SELFDESTRUCT` opcode (PUSH data skipped) → MEDIUM |
| S8 | Deployer | contract deployed < 1 day after its deployer wallet was first funded → MEDIUM |

Wallets get S3, S4, S5, S6; contracts get S1, S2, S5, S6, S7, S8.

## Risk level (requirements 02, 03)

Each flag is low = 1, medium = 2, high = 3 points. **HIGH** if any high flag or score ≥ 5, **MEDIUM** if
score ≥ 2, else **LOW**. Each flag prints `[SEVERITY] (signal) title`, a plain-language
`why it matters:` sentence, and `evidence:` Pharosscan links; passed checks are listed too (`✓`). Exit code 1 if
any checked address is HIGH, 2 on bad input, else 0 — so it can gate a script before a transaction.

**About the flag list:** Pharosscan exposes no public scam/phishing labels for Pharos mainnet (when we checked on
2026-10-14 the only address tags in ~2,900 recent transactions were ENS names), and we found no other public
Pharos threat feed. So `flagged_addresses.json` ships **empty** rather than with invented entries; you add the
addresses you or your team decided to avoid (format inside the file), or pass `--flag 0x…:reason`.
`0x000000000022d473030f116ddee9f6b43ac78ba3` (Permit2) and Multicall3 are recognised as well-known contracts by their canonical
deterministic addresses, because Pharosscan shows Permit2 as unverified.

## Run it

Python 3.10+ (tested with Python 3.12).

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt               # requests, pycryptodome (keccak for selectors)

python wallet_risk_checker.py 0x18573fa18fd17ddfd790b4a5b5b2977aad3b4efb            # one address
python wallet_risk_checker.py 0xA… 0xB… --json result.json                           # several, + JSON
python wallet_risk_checker.py 0xA… --fresh-days 30 --max-txs 2000
python wallet_risk_checker.py 0xA… --flag "0xBAD…:drained my friend" --flagged-file my_list.json
```

Inputs are validated (0x + 40 hex addresses, `--fresh-days` in (0, 365], `--max-txs` in [1, 5000]); the MCP
endpoint must be `https://` (or `http://` to localhost). One check takes ~5–20 s (more for wallets with many
approvals: one `get_transaction` + one `eth_call` per approval).

## Sample output (real runs on Pharos mainnet, 2026-10-14 ~21:19 UTC)

[`sample_output.txt`](sample_output.txt) (+ [`sample_output.json`](sample_output.json)) — 1 clean and 3 risky addresses:

| Address | Result | Why |
|---|---|---|
| [`0x18573fa1…4efb`](https://www.pharosscan.xyz/address/0x18573fa18fd17ddfd790b4a5b5b2977aad3b4efb) Morpho Blue | **LOW** (clean) | 211 days old, verified "Morpho", no SELFDESTRUCT, deployer funded 54 days earlier, 2/129 failed |
| [`0x50fbb865…8ab2`](https://www.pharosscan.xyz/address/0x50fbb8651e332696ba14b04e17175f960ec18ab2) wallet | **HIGH** | funded 3.5 h earlier with 7 txs sent; still-open **unlimited WPROS approval** to unverified contract `0xb300000b…028d` ([tx](https://www.pharosscan.xyz/tx/0xd260f86d7ec8f8a78dcbd4c2aac1a1c6f9b8e4d92c06d44258a445c6f7d8a14e), re-read with `allowance()`); 1 of 3 approvals already used up |
| [`0x9e461507…5731`](https://www.pharosscan.xyz/address/0x9e46150765de4c40eebc2fe8bf6330d5a0b55731) contract | **HIGH** | deployed 25 minutes before the check, source not verified |
| [`0x1f735797…528c`](https://www.pharosscan.xyz/address/0x1f735797c030e7780f803706c5ed8e2b269a528c) active DeFi wallet | **MEDIUM** | 124 days old, but an open unlimited WPROS approval to unverified `0x6ce7a0c9…3e73` + 9 unlimited approvals to verified DEX contracts |

```
=== Wallet Risk Check · 0x50fbb8651e332696ba14b04e17175f960ec18ab2 · Pharos mainnet block #20,252,840 (2026-10-14 21:19 UTC)
type: wallet (EOA) · age: 3.5 hours · balance 10.428801734538193945 PROS · indexed txs 8 (scanned 8)

RISK LEVEL: HIGH  (score 5; low=1 medium=2 high=3; HIGH if any high flag or score>=5, MEDIUM if score>=2)

Red flags:
 1. [HIGH] (S3) Freshly funded wallet (3.5 hours old, 7 txs sent)
    why it matters: This wallet got its first PROS very recently and has almost no history. Fresh wallets are what scammers, airdrop farmers and drainers use; a real counterparty usually has a longer past.
    evidence: https://www.pharosscan.xyz/tx/0x826a275f003f8d9b3d47bbc10f341b27c8fd2e7547179cb2f117f9f372c64756
 2. [MEDIUM] (S4) Open unlimited approval to an unverified contract: UNLIMITED WPROS → 0xb300000b72deaeb607a12d5f54773d1c19c7028d (deployed 102.8 days ago)
    why it matters: This wallet still lets a contract with unreadable code spend its tokens without any limit. ...
    evidence: https://www.pharosscan.xyz/tx/0xd260f86d7ec8f8a78dcbd4c2aac1a1c6f9b8e4d92c06d44258a445c6f7d8a14e
```

[`sample_flag_list_and_validation.txt`](sample_flag_list_and_validation.txt) — the S5 flag list: the 268-day-old
deployer wallet `0x062f05cd…1316` is MEDIUM on its own (it called the 27-minute-old unverified contract above) and
HIGH once that contract is put on the flag list with `--flag` (a demo entry, labelled as such); plus the
input-validation errors.

"Risky" here means the heuristics fire — we do **not** claim any of these addresses is a scam. The fresh wallet
and the unverified spender may well be legitimate (e.g. a solver/aggregator contract); the point is that a user
should look closer before interacting.

## How it covers the task requirements

| # | Requirement | Implementation |
|---|---|---|
| 01 | ≥ 4 signals (age, verified code, flagged addresses, approvals, fresh funding) | 8 signals S1–S8 above, all five examples from the task included |
| 02 | Risk level low / medium / high with specific reasons | `RISK LEVEL: LOW/MEDIUM/HIGH` + score + numbered flags with evidence links |
| 03 | Each flag explained in plain language | `why it matters:` line on every flag |
| 04 | ≥ 1 clean and ≥ 1 risky example | Morpho (LOW) vs fresh wallet / 25-minute-old unverified contract (HIGH), real mainnet runs |

## Limits / honesty notes

* Approvals are found from the newest `--max-txs` transactions the wallet sent; older approvals beyond that
  window are not seen. Permit2 sub-allowances are reported as granted (not re-read inside Permit2).
* Ages and verification flags depend on Pharosscan's index; creation blocks are double-checked on chain
  (`get_code` empty one block earlier, `creation_confirmed_by_get_code` in the JSON).
* Heuristics only — LOW is not a guarantee and HIGH is not an accusation. Not financial advice.

## Security

Read-only: no wallet, private key, or API key; no transaction is ever built, signed, or sent. HTTPS endpoints only.

License: MIT.
