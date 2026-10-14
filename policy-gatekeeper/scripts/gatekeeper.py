#!/usr/bin/env python3
"""Policy Gatekeeper — every action an agent wants to take passes through here first.

    python3 scripts/gatekeeper.py check  --policy assets/policy.json --request '{"type":"transfer","token":"USDC","to":"0x…","amount":"300"}'
    python3 scripts/gatekeeper.py check  --policy assets/policy.json --requests assets/demo_requests.json
    python3 scripts/gatekeeper.py audit  --log audit_log.jsonl            # table + hash-chain verification

Rule types (assets/policy.json): spend_limits (per-tx and rolling 24 h per token, plus a max approval),
allowlist (recipients for transfers, contracts + function selectors for contract calls) and time_windows
(UTC days/hours, optionally scoped to an action type or token). Every request gets ALLOW or DENY with the id of
the rule that decided it. Only ALLOWed requests reach the executor, which PREPARES an unsigned EIP-1559
transaction after on-chain pre-checks via the Pharos Chain MCP — it never signs or broadcasts. Denied requests
are never passed to the executor. Every decision is appended to a hash-chained JSONL audit log.

Exit codes for `check`: 0 = all allowed, 5 = at least one deny, 2 = error.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sys
from decimal import Decimal, InvalidOperation

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from pay_core import Chain, PaymentError, check_address, fmt_units, render, TOKENS  # noqa: E402
from pay_core import unsigned_eip1559  # noqa: E402
from skill_chain import cast_send_command, prepare_tx, sel  # noqa: E402

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
UNLIMITED = 2 ** 255
SEL_TRANSFER, SEL_APPROVE = sel("transfer(address,uint256)"), sel("approve(address,uint256)")
GENESIS = "0" * 64


class PolicyError(ValueError):
    pass


# ---------------------------------------------------------------- policy
def load_policy(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        p = json.load(f)
    r = p.get("rules")
    if not isinstance(r, dict):
        raise PolicyError("policy needs a 'rules' object")
    missing = [k for k in ("spend_limits", "allowlist", "time_windows") if k not in r]
    if missing:
        raise PolicyError(f"policy is missing rule types: {', '.join(missing)}")
    p["agent"] = check_address(p["agent"], "agent (signer)")
    for lim in r["spend_limits"]:
        lim["token"] = lim["token"].upper()
        for k in ("per_tx", "per_day", "max_approval"):
            if k in lim:
                lim[k] = Decimal(str(lim[k]))
    al = r["allowlist"]
    al["recipients"] = {check_address(a, "allowlisted recipient"): n for a, n in al.get("recipients", {}).items()}
    al["contracts"] = {check_address(a, "allowlisted contract"): v for a, v in al.get("contracts", {}).items()}
    for w in r["time_windows"]:
        for d in w["days"]:
            if d not in DAYS:
                raise PolicyError(f"time window {w.get('id')}: unknown day {d!r}")
        for k in ("start", "end"):
            dt.time.fromisoformat(w[k])
    return p


# ---------------------------------------------------------------- request normalisation
def normalise(req: dict) -> dict:
    """Validate the request and work out what it spends. Raises PolicyError for malformed requests."""
    if not isinstance(req, dict):
        raise PolicyError("request must be a JSON object")
    t = req.get("type")
    out = {"id": str(req.get("id", ""))[:64], "type": t, "note": str(req.get("note", ""))[:120]}
    if t == "transfer":
        tok = str(req.get("token", "")).upper()
        if tok != "PROS" and tok not in TOKENS:
            raise PolicyError(f"unknown token {req.get('token')!r}")
        try:
            amt = Decimal(str(req["amount"]))
        except (KeyError, InvalidOperation):
            raise PolicyError("transfer needs a numeric 'amount'")
        if amt <= 0:
            raise PolicyError("amount must be positive")
        out.update(token=tok, to=check_address(req.get("to", ""), "recipient"), amount=amt, spends={tok: amt})
    elif t == "contract_call":
        to = check_address(req.get("to", ""), "contract")
        data = str(req.get("data", "0x"))
        if not data.startswith("0x") or len(data) % 2 or any(c not in "0123456789abcdefABCDEF" for c in data[2:]):
            raise PolicyError("data must be 0x-prefixed hex")
        value = Decimal(str(req.get("value_pros", "0")))
        out.update(to=to, data=data.lower(), selector=data[2:10].lower(), value_pros=value, spends={})
        if value > 0:
            out["spends"]["PROS"] = value
        sym = next((s for s, a in TOKENS.items() if a == to), None)
        if sym and out["selector"] in (SEL_TRANSFER, SEL_APPROVE) and len(data) >= 138:
            raw = int(data[74:138], 16)
            out["erc20"] = {"token": sym, "fn": "transfer" if out["selector"] == SEL_TRANSFER else "approve",
                            "counterparty": "0x" + data[34:74], "raw": raw}
    else:
        raise PolicyError(f"unknown request type {t!r} (transfer | contract_call)")
    return out


# ---------------------------------------------------------------- the three rule types
def spent_last_24h(log_path: str, token: str, now: dt.datetime) -> Decimal:
    total = Decimal(0)
    if not os.path.exists(log_path):
        return total
    since = now - dt.timedelta(hours=24)
    with open(log_path, encoding="utf-8") as f:
        for line in f:
            e = json.loads(line)
            if e["decision"] == "ALLOW" and e.get("executed") and dt.datetime.fromisoformat(e["time"]) >= since:
                total += Decimal(e.get("spends", {}).get(token, "0"))
    return total


def rule_allowlist(p: dict, r: dict) -> tuple[bool, str, str]:
    al = p["rules"]["allowlist"]
    if r["type"] == "transfer":
        if r["to"] in al["recipients"]:
            return True, "allowlist.recipients", f"recipient {r['to']} is allowlisted ({al['recipients'][r['to']]})"
        return False, "allowlist.recipients", f"recipient {r['to']} is not on the recipient allowlist"
    c = al["contracts"].get(r["to"])
    if c is None:
        return False, "allowlist.contracts", f"contract {r['to']} is not on the contract allowlist"
    sels = {sel(s) if "(" in s else s.lower().removeprefix("0x"): s for s in c.get("functions", [])}
    if r["selector"] not in sels:
        return False, "allowlist.contracts", (f"function 0x{r['selector']} is not allowed on {c.get('label', r['to'])} "
                                               f"(allowed: {', '.join(sels.values())})")
    if "erc20" in r and r["erc20"]["counterparty"] not in al["recipients"] and \
            r["erc20"]["counterparty"] not in al["contracts"]:
        return False, "allowlist.recipients", (f"{r['erc20']['fn']} counterparty {r['erc20']['counterparty']} "
                                                "is neither an allowlisted recipient nor contract")
    return True, "allowlist.contracts", f"{c.get('label', r['to'])}.{sels[r['selector']]} is allowlisted"


def rule_time_window(p: dict, r: dict, now: dt.datetime) -> tuple[bool, str, str]:
    for w in p["rules"]["time_windows"]:
        scope = w.get("applies_to", {})
        if scope.get("type") and scope["type"] != r["type"]:
            continue
        if scope.get("token") and scope["token"].upper() not in r.get("spends", {}):
            continue
        day = DAYS[now.weekday()]
        t = now.time().replace(microsecond=0)
        start, end = dt.time.fromisoformat(w["start"]), dt.time.fromisoformat(w["end"])
        inside = day in w["days"] and start <= t <= end
        span = f"{','.join(w['days'])} {w['start']}-{w['end']} UTC"
        if not inside:
            return False, f"time_windows.{w['id']}", f"{day} {t} UTC is outside window {span}"
        return True, f"time_windows.{w['id']}", f"{day} {t} UTC is inside window {span}"
    return True, "time_windows", "no time window applies to this action"


def rule_spend(p: dict, r: dict, chain: Chain | None, log_path: str, now: dt.datetime) -> tuple[bool, str, str]:
    lims = {l["token"]: l for l in p["rules"]["spend_limits"]}
    if "erc20" in r:  # resolve the ERC-20 amount of a raw transfer/approve call with on-chain decimals
        e = r["erc20"]
        dec = chain.token(e["token"]).decimals if chain else 6
        lim = lims.get(e["token"])
        if e["fn"] == "approve":
            if e["raw"] >= UNLIMITED:
                return False, f"spend_limits.{e['token']}.max_approval", "unlimited approval is never allowed"
            amt = Decimal(e["raw"]) / Decimal(10) ** dec
            if lim is None or amt > lim.get("max_approval", Decimal(0)):
                return False, f"spend_limits.{e['token']}.max_approval", (
                    f"approval {amt} {e['token']} > max_approval {lim.get('max_approval', 0) if lim else 0}")
        else:
            r["spends"][e["token"]] = Decimal(e["raw"]) / Decimal(10) ** dec
    if not r["spends"]:
        return True, "spend_limits", "action moves no value"
    for tok, amt in r["spends"].items():
        lim = lims.get(tok)
        if lim is None:
            return False, f"spend_limits.{tok}", f"no spend limit configured for {tok} -> denied by default"
        if amt > lim["per_tx"]:
            return False, f"spend_limits.{tok}.per_tx", f"{amt} {tok} > per-transaction limit {lim['per_tx']} {tok}"
        used = spent_last_24h(log_path, tok, now)
        if used + amt > lim["per_day"]:
            return False, f"spend_limits.{tok}.per_day", (f"{used} {tok} already executed in the last 24 h + {amt} "
                                                          f"> daily limit {lim['per_day']} {tok}")
    t, a = next(iter(r["spends"].items()))
    return True, f"spend_limits.{t}", f"{a} {t} within per-tx {lims[t]['per_tx']} and 24 h {lims[t]['per_day']} limits"


def decide(p: dict, r: dict, chain: Chain | None, log_path: str, now: dt.datetime) -> dict:
    """Run the rule types in a fixed order; the first DENY decides. All evaluated rules are recorded."""
    trail = []
    for name, fn in (("allowlist", lambda: rule_allowlist(p, r)),
                     ("time_window", lambda: rule_time_window(p, r, now)),
                     ("spend_limit", lambda: rule_spend(p, r, chain, log_path, now))):
        ok, rid, why = fn()
        trail.append({"type": name, "rule": rid, "ok": ok, "reason": why})
        if not ok:
            return {"decision": "DENY", "decided_by": rid, "reason": why, "trail": trail}
    return {"decision": "ALLOW", "decided_by": "all rules passed", "reason": "; ".join(t["reason"] for t in trail),
            "trail": trail}


# ---------------------------------------------------------------- executor (prepare-only)
class Executor:
    """Turns an ALLOWed request into an unsigned transaction. The gatekeeper is the only caller."""

    def __init__(self, chain: Chain, agent: str):
        self.chain, self.agent, self.calls = chain, agent, 0
        self.next_nonce: int | None = None  # nonces of txs prepared in this session (none of them is sent)

    def _nonce(self, chain_nonce: int) -> int:
        n = chain_nonce if self.next_nonce is None else max(chain_nonce, self.next_nonce)
        self.next_nonce = n + 1
        return n

    def execute(self, r: dict) -> dict:
        self.calls += 1
        if r["type"] == "transfer":
            prep = self.chain.prepare(self.agent, r["to"], r["token"], r["amount"])
            n = self._nonce(prep.tx["nonce"])
            if n != prep.tx["nonce"]:  # several unsigned txs in one session: give each its own nonce
                prep.tx["nonce"] = n
                prep.unsigned_raw, prep.signing_hash = unsigned_eip1559(prep.tx)
                prep.checks.append((True, f"nonce {n} = pending nonce + txs already prepared this session"))
            out = prep.to_json()
            out["castCommandForUser"] = cast_send_command(prep.tx["to"], prep.tx["data"], prep.tx["value"])
            out["summary"] = render(prep, "Prepared (unsigned)")
            return out
        code = self.chain.code(r["to"])
        if code in ("0x", ""):
            raise PaymentError(f"{r['to']} has no contract code")
        value = int(r["value_pros"] * 10 ** 18)
        out = prepare_tx(self.chain, self.agent, r["to"], r["data"], value)
        t = out["unsignedTx"]
        n = self._nonce(int(t["nonce"], 16))
        if n != int(t["nonce"], 16):
            tx = {k: (int(v, 16) if k not in ("to", "data") else v) for k, v in t.items()}
            tx["nonce"] = n
            out["unsignedSerialized"], out["signingHash"] = unsigned_eip1559(tx)
            t["nonce"] = hex(n)
            out["walletRequest"]["params"][0]["nonce"] = hex(n)
        out["summary"] = f"Prepared (unsigned) contract call to {r['to']} selector 0x{r['selector']}, " \
                         f"gas {int(out['unsignedTx']['gas'], 16):,}, max fee {out['maxNetworkFeePROS']} PROS"
        return out


# ---------------------------------------------------------------- audit log (hash-chained JSONL)
def last_hash(log_path: str) -> str:
    if not os.path.exists(log_path):
        return GENESIS
    h = GENESIS
    with open(log_path, encoding="utf-8") as f:
        for line in f:
            h = json.loads(line)["hash"]
    return h


def append_audit(log_path: str, entry: dict) -> dict:
    entry["prev"] = last_hash(log_path)
    body = json.dumps({k: v for k, v in entry.items() if k != "hash"}, sort_keys=True, default=str)
    entry["hash"] = hashlib.sha256(body.encode()).hexdigest()
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")
    return entry


def verify_audit(log_path: str) -> tuple[bool, int, str]:
    prev, n = GENESIS, 0
    with open(log_path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            e = json.loads(line)
            body = json.dumps({k: v for k, v in e.items() if k != "hash"}, sort_keys=True, default=str)
            if e["prev"] != prev or hashlib.sha256(body.encode()).hexdigest() != e["hash"]:
                return False, n, f"entry {n} was modified or the chain is broken"
            prev = e["hash"]
    return True, n, "hash chain intact"


# ---------------------------------------------------------------- CLI
def handle(p: dict, raw: dict, chain: Chain, ex: Executor, log_path: str, now: dt.datetime | None = None) -> dict:
    now = now or dt.datetime.now(dt.timezone.utc)
    entry = {"time": now.isoformat(timespec="seconds"), "policy": p.get("name"), "agent": p["agent"],
             "request": raw, "executed": False}
    try:
        r = normalise(raw)
        d = decide(p, r, chain, log_path, now)
        entry["spends"] = {k: str(v) for k, v in r["spends"].items()}
    except (PolicyError, PaymentError) as e:  # malformed request / bad address -> deny, never execute
        d = {"decision": "DENY", "decided_by": "request.validation", "reason": str(e), "trail": []}
    entry.update(d)
    if d["decision"] == "ALLOW":
        try:
            res = ex.execute(r)
            entry["executed"] = True
            entry["prepared"] = {k: v for k, v in res.items() if k != "summary"}
            entry["summary"] = res["summary"]
        except PaymentError as e:
            entry["execution_error"] = f"allowed by policy but failed on-chain pre-check: {e}"
    out = append_audit(log_path, {k: v for k, v in entry.items() if k != "summary"})
    out["summary"] = entry.get("summary")
    return out


def print_entry(e: dict) -> None:
    rq = e["request"]
    what = (f"transfer {rq.get('amount')} {rq.get('token')} -> {rq.get('to')}" if rq.get("type") == "transfer"
            else f"{rq.get('type')} {rq.get('to')} data {str(rq.get('data', ''))[:10]}… value {rq.get('value_pros', '0')} PROS")
    print(f"[{e['decision']}] {rq.get('id', '')}: {what}")
    print(f"        decided by: {e['decided_by']} - {e['reason']}")
    for t in e.get("trail", []):
        print(f"          {'pass' if t['ok'] else 'DENY'} {t['rule']}: {t['reason']}")
    if e["decision"] == "ALLOW":
        if e.get("executed"):
            u = e["prepared"]
            nonce = int(u.get("unsignedTx", {}).get("nonce"), 16)
            print(f"        executor: prepared UNSIGNED tx (nonce {nonce}, signing hash {u['signingHash'][:18]}…) - "
                  "not signed, not sent")
            print(f"        sign it yourself: {u['castCommandForUser']}")
        else:
            print(f"        executor: {e.get('execution_error')}")
    else:
        print("        executor: NOT called (denied actions are never executed)")
    print(f"        audit #{e['hash'][:12]} (prev {e['prev'][:12]})")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--policy", required=True)
    g = c.add_mutually_exclusive_group(required=True)
    g.add_argument("--request", help="one request as JSON")
    g.add_argument("--requests", help="file with a JSON list of requests (processed in order)")
    c.add_argument("--log", default="audit_log.jsonl")
    c.add_argument("--endpoint", default=None)
    au = sub.add_parser("audit")
    au.add_argument("--log", default="audit_log.jsonl")
    a = ap.parse_args(argv)

    if a.cmd == "audit":
        if not os.path.exists(a.log):
            print(f"error: no audit log at {a.log}", file=sys.stderr)
            return 2
        with open(a.log, encoding="utf-8") as f:
            rows = [json.loads(x) for x in f]
        print(f"{'time (UTC)':<26} {'id':<16} {'decision':<8} {'executed':<8} decided by")
        for e in rows:
            print(f"{e['time']:<26} {str(e['request'].get('id', ''))[:15]:<16} {e['decision']:<8} "
                  f"{str(e['executed']):<8} {e['decided_by']}")
        ok, n, msg = verify_audit(a.log)
        allow = sum(1 for e in rows if e["decision"] == "ALLOW")
        print(f"\n{n} decisions: {allow} ALLOW, {n - allow} DENY; executed {sum(1 for e in rows if e['executed'])}; "
              f"denied-but-executed {sum(1 for e in rows if e['decision'] == 'DENY' and e['executed'])}. {msg}.")
        return 0 if ok else 1

    try:
        p = load_policy(a.policy)
        reqs = [json.loads(a.request)] if a.request else json.load(open(a.requests, encoding="utf-8"))
    except (PolicyError, ValueError, OSError, KeyError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    chain = Chain(a.endpoint)
    ex = Executor(chain, p["agent"])
    now0 = dt.datetime.now(dt.timezone.utc)
    print(f"Policy Gatekeeper · policy '{p.get('name')}' · agent {p['agent']} · Pharos mainnet · "
          f"{now0:%Y-%m-%d %H:%M:%S} UTC ({DAYS[now0.weekday()]})\n")
    denies = 0
    for raw in reqs:
        e = handle(p, raw, chain, ex, a.log)
        denies += e["decision"] == "DENY"
        print_entry(e)
        print()
    print(f"{len(reqs)} requests: {len(reqs) - denies} allowed, {denies} denied; executor called {ex.calls} times. "
          f"Audit log: {a.log}")
    return 5 if denies else 0


if __name__ == "__main__":
    sys.exit(main())
