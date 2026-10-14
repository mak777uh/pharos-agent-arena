"""Prepare-only payment core for Pharos Pacific mainnet (chainId 1672).

Builds UNSIGNED EIP-1559 (type 0x02) transactions for native PROS or ERC-20 payments and checks them on chain
through the hosted Pharos Chain MCP (https://mcp.pharos.xyz/mcp): chain id, token contract (code, symbol,
decimals), sender balances (balanceOf via eth_call, native PROS via get_balance), allowance (for transferFrom),
USDC blacklist, nonce (get_transaction_count "pending"), gas (estimate_gas) and fee fields (get_gas_price +
latest block baseFeePerGas).

It never signs, never broadcasts and never touches a private key. The output is
  * an EIP-1193 `eth_sendTransaction` request any wallet can sign,
  * the canonical unsigned serialization 0x02 || rlp([chainId, nonce, maxPriorityFeePerGas, maxFeePerGas,
    gasLimit, to, value, data, accessList]) and its keccak256 signing hash (what the wallet will sign),
  * an EIP-681 payment URI when the payment carries no reference memo.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_DOWN

from Crypto.Hash import keccak

from pharos_mcp import PharosMCP, MCPError

CHAIN_ID = 1672
EXPLORER = "https://www.pharosscan.xyz"
ZERO = "0x" + "00" * 20
ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

# Pacific mainnet token registry (https://docs.pharos.xyz/getting-started/token-registry).
# Symbol and decimals are re-read from the contract on every run; the list only says which contracts are known.
TOKENS = {
    "USDC": "0xc879c018db60520f4355c26ed1a6d572cdac1815",
    "WPROS": "0x52c48d4213107b20bc583832b0d951fb9ca8f0b0",
    "LINK": "0x51e2a24742db77604b881d6781ee16b5b8fcbe29",
    "WETH": "0x1f4b7011ee3d53969bb67f59428a9ec0477856e9",
}
NATIVE = "PROS"

SEL = {  # 4-byte selectors
    "transfer": "a9059cbb", "transferFrom": "23b872dd", "balanceOf": "70a08231", "allowance": "dd62ed3e",
    "decimals": "313ce567", "symbol": "95d89b41", "isBlacklisted": "fe575a87",
}
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


class PaymentError(ValueError):
    """A request that must not become a transaction (invalid input or failed on-chain check)."""


# ---------------------------------------------------------------- helpers
def keccak256(b: bytes) -> bytes:
    return keccak.new(digest_bits=256, data=b).digest()


def to_checksum(addr: str) -> str:
    a = addr.lower()[2:]
    h = keccak256(a.encode()).hex()
    return "0x" + "".join(c.upper() if int(h[i], 16) >= 8 else c for i, c in enumerate(a))


def check_address(addr: str, what: str = "address") -> str:
    """Validate format and, when mixed-case, the EIP-55 checksum. Returns lower-case."""
    if not isinstance(addr, str) or not ADDR_RE.match(addr):
        raise PaymentError(f"{what} {addr!r} is not a 0x-prefixed 20-byte hex address")
    body = addr[2:]
    if body != body.lower() and body != body.upper() and to_checksum(addr) != addr:
        raise PaymentError(f"{what} {addr} fails the EIP-55 checksum (typo?) - expected {to_checksum(addr)}")
    return addr.lower()


def h2i(v) -> int:
    if isinstance(v, int):
        return v
    v = str(v)
    return int(v, 16) if v.startswith("0x") else int(v)


def word(x) -> str:
    if isinstance(x, str):
        return x.lower().replace("0x", "").rjust(64, "0")
    return format(int(x), "064x")


def to_units(amount: Decimal, decimals: int) -> int:
    q = amount * (Decimal(10) ** decimals)
    if q != q.to_integral_value():
        raise PaymentError(f"amount {amount} has more than {decimals} decimal places")
    return int(q)


def fmt_units(v: int, decimals: int) -> str:
    d = (Decimal(v) / (Decimal(10) ** decimals)).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_DOWN)
    s = f"{d:,.{decimals}f}".rstrip("0").rstrip(".")
    return s or "0"


def gwei(wei: int) -> str:
    s = f"{Decimal(wei) / Decimal(10**9):f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s + " gwei"


# ---------------------------------------------------------------- RLP / EIP-1559
def _rlp_bytes(b: bytes) -> bytes:
    if len(b) == 1 and b[0] < 0x80:
        return b
    return _rlp_len(len(b), 0x80) + b


def _rlp_len(n: int, off: int) -> bytes:
    if n < 56:
        return bytes([off + n])
    nb = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([off + 55 + len(nb)]) + nb


def rlp(x) -> bytes:
    if isinstance(x, list):
        body = b"".join(rlp(i) for i in x)
        return _rlp_len(len(body), 0xC0) + body
    if isinstance(x, int):
        return _rlp_bytes(b"" if x == 0 else x.to_bytes((x.bit_length() + 7) // 8, "big"))
    return _rlp_bytes(x)


def unsigned_eip1559(tx: dict) -> tuple[str, str]:
    """Return (unsigned serialized tx, keccak256 signing hash), both 0x-hex."""
    fields = [tx["chainId"], tx["nonce"], tx["maxPriorityFeePerGas"], tx["maxFeePerGas"], tx["gas"],
              bytes.fromhex(tx["to"][2:]), tx["value"], bytes.fromhex(tx["data"][2:]), []]
    raw = b"\x02" + rlp(fields)
    return "0x" + raw.hex(), "0x" + keccak256(raw).hex()


# ---------------------------------------------------------------- memo (invoice reference) in calldata
MEMO_TAG = b"PHAROS-REF:"


def memo_bytes(ref: str) -> bytes:
    if not ref or len(ref) > 64 or not re.fullmatch(r"[\x20-\x7e]+", ref):
        raise PaymentError("reference must be 1-64 printable ASCII characters")
    return MEMO_TAG + ref.encode()


def decode_memo(data_hex: str) -> str | None:
    b = bytes.fromhex(data_hex[2:] if data_hex.startswith("0x") else data_hex)
    i = b.find(MEMO_TAG)
    return b[i + len(MEMO_TAG):].decode("ascii", "replace") if i >= 0 else None


# ---------------------------------------------------------------- chain access
@dataclass
class Token:
    symbol: str
    address: str | None   # None = native PROS
    decimals: int


@dataclass
class Prepared:
    token: Token
    sender: str
    recipient: str
    amount_units: int
    tx: dict                      # unsigned fields
    unsigned_raw: str
    signing_hash: str
    checks: list = field(default_factory=list)   # (ok, text)
    warnings: list = field(default_factory=list)
    reference: str | None = None
    block: int = 0
    fee_max_wei: int = 0

    @property
    def amount_str(self) -> str:
        return fmt_units(self.amount_units, self.token.decimals)

    def wallet_request(self) -> dict:
        t = self.tx
        return {"method": "eth_sendTransaction", "params": [{
            "from": to_checksum(self.sender), "to": to_checksum(t["to"]), "value": hex(t["value"]),
            "data": t["data"], "gas": hex(t["gas"]), "maxFeePerGas": hex(t["maxFeePerGas"]),
            "maxPriorityFeePerGas": hex(t["maxPriorityFeePerGas"]), "nonce": hex(t["nonce"]),
            "chainId": hex(t["chainId"]), "type": "0x2"}]}

    def eip681(self) -> str | None:
        if self.reference:
            return None  # EIP-681 has no field for extra calldata; use the wallet request instead
        if self.token.address is None:
            return f"ethereum:{to_checksum(self.recipient)}@{CHAIN_ID}?value={self.amount_units}"
        return (f"ethereum:{to_checksum(self.token.address)}@{CHAIN_ID}/transfer"
                f"?address={to_checksum(self.recipient)}&uint256={self.amount_units}")

    def to_json(self) -> dict:
        return {"chainId": CHAIN_ID, "token": self.token.symbol, "tokenAddress": self.token.address,
                "from": self.sender, "to": self.recipient, "amount": self.amount_str,
                "amountUnits": str(self.amount_units), "reference": self.reference, "preparedAtBlock": self.block,
                "unsignedTx": {k: (hex(v) if isinstance(v, int) else v) for k, v in self.tx.items()},
                "unsignedSerialized": self.unsigned_raw, "signingHash": self.signing_hash,
                "walletRequest": self.wallet_request(), "eip681": self.eip681(),
                "maxNetworkFeePROS": fmt_units(self.fee_max_wei, 18),
                "checks": [{"ok": ok, "check": c} for ok, c in self.checks], "warnings": self.warnings,
                "signed": False, "broadcast": False}


class Chain:
    def __init__(self, endpoint: str | None = None):
        self.m = PharosMCP(endpoint) if endpoint else PharosMCP()
        self._tok: dict[str, Token] = {}
        self._at: dict[int, int] = {}

    # basic reads
    def call(self, to: str, data: str, frm: str | None = None) -> str:
        tx = {"to": to, "data": data}
        if frm:
            tx["from"] = frm
        out = self.m.call_tool("eth_call", {"transaction": tx, "block": "latest"})
        return out["result"] if isinstance(out, dict) else out

    def code(self, addr: str) -> str:
        c = self.m.call_tool("get_code", {"address": addr})
        return c.get("code", "0x") if isinstance(c, dict) else c

    def native_balance(self, addr: str) -> int:
        return int(self.m.call_tool("get_balance", {"address": addr})["balanceWei"])

    def nonce(self, addr: str, block: str = "pending") -> int:
        return int(self.m.call_tool("get_transaction_count", {"address": addr, "block": block})["nonce"])

    def block(self, b="latest") -> dict:
        return self.m.call_tool("get_block", {"block": b})

    def block_number(self) -> int:
        return self.m.block_number()

    def chain_id(self) -> int:
        return int(self.m.call_tool("get_chain_info")["chainId"])

    def token(self, symbol_or_addr: str) -> Token:
        key = symbol_or_addr.upper() if not symbol_or_addr.startswith("0x") else symbol_or_addr.lower()
        if key in self._tok:
            return self._tok[key]
        if key == NATIVE:
            t = Token(NATIVE, None, 18)
        else:
            addr = TOKENS.get(key) or (check_address(key, "token contract") if key.startswith("0x") else None)
            if not addr:
                raise PaymentError(f"unknown token {symbol_or_addr!r}; known on Pharos mainnet: "
                                   f"{', '.join([NATIVE, *TOKENS])} or pass the token contract address")
            if self.code(addr) in ("0x", "", None):
                raise PaymentError(f"token {addr} has no contract code on chain {CHAIN_ID}")
            try:
                dec = h2i(self.call(addr, "0x" + SEL["decimals"]))
                raw = self.call(addr, "0x" + SEL["symbol"])
                b = bytes.fromhex(raw[2:])
                sym = b[64:64 + int.from_bytes(b[32:64], "big")].decode() if len(b) >= 96 else b.rstrip(b"\0").decode()
            except Exception as e:  # noqa: BLE001
                raise PaymentError(f"{addr} does not behave like an ERC-20 (symbol/decimals failed: {e})")
            if key in TOKENS and sym.upper() != key:
                raise PaymentError(f"registry says {key} at {addr} but the contract reports symbol {sym!r}")
            t = Token(sym, addr, dec)
        self._tok[key] = t
        return t

    def erc20_balance(self, tok: Token, owner: str) -> int:
        return h2i(self.call(tok.address, "0x" + SEL["balanceOf"] + word(owner)))

    def allowance(self, tok: Token, owner: str, spender: str) -> int:
        return h2i(self.call(tok.address, "0x" + SEL["allowance"] + word(owner) + word(spender)))

    def blacklisted(self, tok: Token, addr: str) -> bool | None:
        try:
            return h2i(self.call(tok.address, "0x" + SEL["isBlacklisted"] + word(addr))) == 1
        except MCPError:
            return None  # token has no blacklist

    def fees(self, tip_wei: int | None = None) -> tuple[int, int, int]:
        """(baseFee, maxPriorityFeePerGas, maxFeePerGas) — maxFee = 2*baseFee + tip."""
        g = self.m.call_tool("get_gas_price")
        base = h2i(self.block("latest").get("baseFeePerGas") or g["gasPriceWei"])
        tip = int(g.get("maxPriorityFeePerGasWei", 0)) if tip_wei is None else tip_wei
        return base, tip, 2 * base + tip

    def transfers(self, tok: Token, frm: str, to: str, from_block: int, to_block: int) -> list[dict]:
        """ERC-20 Transfer(from,to) logs in [from_block, to_block], chunked to the 1000-block MCP limit."""
        out = []
        b = from_block
        while b <= to_block:
            e = min(b + 999, to_block)
            for l in self.m.get_logs(tok.address, [TRANSFER_TOPIC, "0x" + word(frm), "0x" + word(to)], b, e):
                out.append({"tx": l["transactionHash"], "block": h2i(l["blockNumber"]), "amount": h2i(l["data"])})
            b = e + 1
        return out

    def block_at(self, ts: int, hi: int | None = None) -> int:
        """First block with timestamp >= ts: estimate from the measured block rate, then binary-search a narrow
        bracket around the estimate (keeps it to ~10 get_block calls instead of ~25)."""
        if ts in self._at:
            return self._at[ts]
        hi = hi or self.block_number()
        t_hi = h2i(self.block(hi)["timestamp"])
        if t_hi < ts:
            return hi + 1
        ref = max(0, hi - 10_000)
        t_ref = h2i(self.block(ref)["timestamp"])
        rate = max((t_hi - t_ref) / max(hi - ref, 1), 0.01)  # seconds per block
        guess = int(hi - (t_hi - ts) / rate)
        lo, up, step = max(0, guess - 600), min(hi, guess + 600), 1200
        while lo > 0 and h2i(self.block(lo)["timestamp"]) >= ts:   # widen until lo is before ts
            lo, step = max(0, lo - step), step * 2
        while up < hi and h2i(self.block(up)["timestamp"]) < ts:   # widen until up is at/after ts
            up, step = min(hi, up + step), step * 2
        while lo < up:
            mid = (lo + up) // 2
            if h2i(self.block(mid)["timestamp"]) < ts:
                lo = mid + 1
            else:
                up = mid
        self._at[ts] = lo
        return lo

    # ------------------------------------------------------------ the one entry point that builds a tx
    def prepare(self, sender: str, recipient: str, token: str, amount: Decimal, reference: str | None = None,
                pull_owner: str | None = None, tip_gwei: Decimal | None = None, gas_buffer: float = 1.2) -> Prepared:
        """Validate a payment and build the unsigned EIP-1559 transaction. Raises PaymentError on any failed check.

        sender      the account that will sign (pays gas). For a plain transfer it also pays the tokens.
        pull_owner  optional: tokens are pulled from this owner with transferFrom(owner, recipient, amount)
                    using the sender's allowance (payroll/operator pattern)."""
        sender = check_address(sender, "sender")
        recipient = check_address(recipient, "recipient")
        if pull_owner:
            pull_owner = check_address(pull_owner, "token owner")
        if not isinstance(amount, Decimal) or amount <= 0:
            raise PaymentError("amount must be a positive number")
        checks, warns = [], []
        cid = self.chain_id()
        if cid != CHAIN_ID:
            raise PaymentError(f"endpoint is on chainId {cid}, expected Pharos mainnet {CHAIN_ID}")
        checks.append((True, f"Chain MCP is on Pharos mainnet (chainId {cid})"))
        tok = self.token(token)
        units = to_units(amount, tok.decimals)
        payer = pull_owner or sender
        if recipient == ZERO:
            raise PaymentError("recipient is the zero address (tokens would be burned)")
        if recipient == payer:
            raise PaymentError("recipient is the same as the payer")
        if tok.address and recipient == tok.address:
            raise PaymentError("recipient is the token contract itself (funds would be stuck)")
        if tok.address:
            checks.append((True, f"token {tok.symbol} {tok.address} has code, decimals {tok.decimals} (read on chain)"))
        rcode = self.code(recipient)
        if rcode not in ("0x", ""):
            kind = "an EIP-7702 delegated account" if rcode.startswith("0xef0100") else "a smart contract"
            warns.append(f"recipient is {kind} - make sure it can receive {tok.symbol}")
        else:
            checks.append((True, "recipient is a regular account (no contract code)"))
        if tok.address:
            for who, a in (("payer", payer), ("recipient", recipient)):
                bl = self.blacklisted(tok, a)
                if bl:
                    raise PaymentError(f"{who} {a} is blacklisted by the {tok.symbol} contract")
                if bl is False:
                    checks.append((True, f"{who} not blacklisted by {tok.symbol}"))
            bal = self.erc20_balance(tok, payer)
            ok = bal >= units
            checks.append((ok, f"payer {tok.symbol} balance {fmt_units(bal, tok.decimals)} >= {amount} (balanceOf)"))
            if not ok:
                raise InsufficientBalance(f"insufficient {tok.symbol}: payer has {fmt_units(bal, tok.decimals)}, "
                                          f"needs {amount}", checks)
            if pull_owner:
                alw = self.allowance(tok, pull_owner, sender)
                ok = alw >= units
                checks.append((ok, f"allowance(owner -> operator) {fmt_units(alw, tok.decimals) if alw < 2**255 else 'unlimited'}"
                                    f" >= {amount} (allowance)"))
                if not ok:
                    raise InsufficientBalance(f"insufficient allowance: operator may pull "
                                              f"{fmt_units(alw, tok.decimals)} {tok.symbol}, needs {amount}", checks)
                data = "0x" + SEL["transferFrom"] + word(pull_owner) + word(recipient) + word(units)
            else:
                data = "0x" + SEL["transfer"] + word(recipient) + word(units)
            to, value = tok.address, 0
        else:
            if pull_owner:
                raise PaymentError("pull (transferFrom) mode needs an ERC-20 token, not native PROS")
            data, to, value = "0x", recipient, units
        if reference:
            data += memo_bytes(reference).hex()  # trailing bytes: ignored by the ERC-20 ABI decoder, kept on chain
        base, tip, max_fee = self.fees(None if tip_gwei is None else int(tip_gwei * 10**9))
        try:
            est = int(self.m.call_tool("estimate_gas", {"transaction": {
                "from": sender, "to": to, "data": data, "value": hex(value)}})["gas"])
        except MCPError as e:
            raise PaymentError(f"the transaction would revert (estimate_gas): {str(e)[:200]}")
        gas = int(est * gas_buffer)
        checks.append((True, f"simulation OK: estimate_gas {est:,} -> gas limit {gas:,} (+{round((gas_buffer-1)*100)}%)"))
        fee_max = gas * max_fee
        nat = self.native_balance(sender)
        ok = nat >= fee_max + value
        checks.append((ok, f"signer PROS balance {fmt_units(nat, 18)} >= value + max gas fee "
                           f"{fmt_units(fee_max + value, 18)} (get_balance)"))
        if not ok:
            raise InsufficientBalance(f"insufficient PROS for value + gas: signer has {fmt_units(nat, 18)}, "
                                      f"needs up to {fmt_units(fee_max + value, 18)}", checks)
        nonce = self.nonce(sender, "pending")
        checks.append((True, f"nonce {nonce} = get_transaction_count(sender, 'pending')"))
        tx = {"chainId": CHAIN_ID, "nonce": nonce, "maxPriorityFeePerGas": tip, "maxFeePerGas": max_fee,
              "gas": gas, "to": to, "value": value, "data": data}
        raw, h = unsigned_eip1559(tx)
        return Prepared(tok, sender, recipient, units, tx, raw, h, checks, warns, reference,
                        self.block_number(), fee_max)


class InsufficientBalance(PaymentError):
    def __init__(self, msg: str, checks: list | None = None):
        super().__init__(msg)
        self.checks = checks or []


def render(p: Prepared, title: str = "Payment summary") -> str:
    t = p.tx
    lines = [f"=== {title} (review before signing) ===",
             f"  Pay        : {p.amount_str} {p.token.symbol}" + (f"  (contract {p.token.address})" if p.token.address else " (native)"),
             f"  To         : {to_checksum(p.recipient)}  {EXPLORER}/address/{p.recipient}",
             f"  From/signer: {to_checksum(p.sender)}",
             ]
    if t["data"][2:10] == SEL["transferFrom"]:
        lines.append(f"  Pulled from: {to_checksum('0x' + t['data'][34:74])} via transferFrom (allowance)")
    if p.reference:
        lines.append(f"  Reference  : {p.reference}  (embedded in calldata as 'PHAROS-REF:{p.reference}')")
    lines += [f"  Network    : Pharos mainnet, chainId {CHAIN_ID}, nonce {t['nonce']}, gas limit {t['gas']:,}",
              f"  Fees       : maxFeePerGas {gwei(t['maxFeePerGas'])}, tip {gwei(t['maxPriorityFeePerGas'])}"
              f" -> at most {fmt_units(p.fee_max_wei, 18)} PROS network fee",
              "  Checks:"]
    lines += [f"    [{'ok' if ok else 'FAIL'}] {c}" for ok, c in p.checks]
    lines += [f"    [warn] {w}" for w in p.warnings]
    return "\n".join(lines)


def render_tx(p: Prepared) -> str:
    import json
    out = ["=== Unsigned transaction (NOT signed, NOT sent) ===",
           "  Wallet signing request (EIP-1193):",
           "  " + json.dumps(p.wallet_request()),
           f"  Unsigned EIP-1559 serialization: {p.unsigned_raw}",
           f"  Signing hash (keccak256 of the above): {p.signing_hash}"]
    if p.eip681():
        out.append(f"  EIP-681 URI: {p.eip681()}")
    out.append("  Sign it in your own wallet; this tool holds no key and never broadcasts.")
    return "\n".join(out)
