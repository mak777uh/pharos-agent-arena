"""Uniswap-V3 exact-input swap simulation in integer math (SwapMath / SqrtPriceMath / TickBitmap ports).

The pool state (slot0, liquidity, tickBitmap words, ticks(t).liquidityNet) is read through a `src` object with
`call(to, data, block) -> bytes`, so the same code quotes from the hosted Pharos Chain MCP (mainnet, archive
block) or from a local fork. Ticks are traversed (liquidity changes at every initialized tick crossed), so the
quote is exact for any size, not only in-range. getSqrtRatioAtTick is computed with 80-digit Decimal and floored,
which can differ from Uniswap's TickMath bit-shift table by a few wei of sqrtPrice at a crossing — irrelevant for
USD-level quotes; every quote used for a decision is cross-checked by an on-fork eth_call of the router.
"""
from __future__ import annotations

from decimal import Decimal, getcontext

from eth_abi import decode as abi_decode, encode as abi_encode

from pay_core import keccak256

getcontext().prec = 80
Q96 = 2 ** 96
MIN_TICK, MAX_TICK = -887272, 887272
MIN_SQRT, MAX_SQRT = 4295128739, 1461446703485210103287273052203988822378723970342


def _sel(sig: str) -> str:
    return keccak256(sig.encode()).hex()[:8]


S_SLOT0, S_LIQ, S_BITMAP, S_TICKS = _sel("slot0()"), _sel("liquidity()"), _sel("tickBitmap(int16)"), _sel("ticks(int24)")
S_FEE, S_SPACING, S_T0, S_T1 = _sel("fee()"), _sel("tickSpacing()"), _sel("token0()"), _sel("token1()")


def sqrt_at_tick(t: int) -> int:
    return int((Decimal("1.0001") ** t).sqrt() * Q96)


def ceil_div(a: int, b: int) -> int:
    return -(-a // b)


def amount0_delta(a: int, b: int, liq: int, up: bool) -> int:
    if a > b:
        a, b = b, a
    n1, n2 = liq << 96, b - a
    return ceil_div(ceil_div(n1 * n2, b), a) if up else (n1 * n2 // b) // a


def amount1_delta(a: int, b: int, liq: int, up: bool) -> int:
    if a > b:
        a, b = b, a
    return ceil_div(liq * (b - a), Q96) if up else liq * (b - a) // Q96


def next_sqrt_from_input(sp: int, liq: int, amount: int, zero_for_one: bool) -> int:
    if zero_for_one:        # token0 in: price down, rounding up
        n1 = liq << 96
        return ceil_div(n1 * sp, n1 + amount * sp)
    return sp + (amount << 96) // liq   # token1 in: price up, rounding down


def swap_step(sp: int, target: int, liq: int, remaining: int, fee: int, zero_for_one: bool):
    less_fee = remaining * (10 ** 6 - fee) // 10 ** 6
    amt_in = amount0_delta(target, sp, liq, True) if zero_for_one else amount1_delta(sp, target, liq, True)
    if less_fee >= amt_in:
        nxt = target
    else:
        nxt = next_sqrt_from_input(sp, liq, less_fee, zero_for_one)
    reached = nxt == target
    if zero_for_one:
        amt_in = amt_in if reached else amount0_delta(nxt, sp, liq, True)
        amt_out = amount1_delta(nxt, sp, liq, False)
    else:
        amt_in = amt_in if reached else amount1_delta(sp, nxt, liq, True)
        amt_out = amount0_delta(sp, nxt, liq, False)
    fee_amt = remaining - amt_in if not reached else ceil_div(amt_in * fee, 10 ** 6 - fee)
    return nxt, amt_in, amt_out, fee_amt


def _msb(x: int) -> int:
    return x.bit_length() - 1


def _lsb(x: int) -> int:
    return (x & -x).bit_length() - 1


class Pool:
    """One V3 pool's state at one block, with lazily read bitmap words / ticks (cached)."""

    def __init__(self, src, address: str, block, meta: dict | None = None):
        """meta: immutable pool fields (fee, spacing, token0, token1) from an earlier read, to save 4 calls."""
        self.src, self.a, self.block = src, address.lower(), block
        self.calls = 0
        s0 = self._call(S_SLOT0)
        self.sqrt = int.from_bytes(s0[:32], "big")
        self.tick = int.from_bytes(s0[32:64], "big", signed=True)
        self.liq = int.from_bytes(self._call(S_LIQ)[:32], "big")
        if meta:
            self.fee, self.spacing, self.token0, self.token1 = meta["fee"], meta["spacing"], meta["token0"], meta["token1"]
        else:
            self.fee = int.from_bytes(self._call(S_FEE)[:32], "big")
            self.spacing = int.from_bytes(self._call(S_SPACING)[:32], "big", signed=True)
            self.token0 = "0x" + self._call(S_T0)[-20:].hex()
            self.token1 = "0x" + self._call(S_T1)[-20:].hex()
        self._words: dict[int, int] = {}
        self._nets: dict[int, int] = {}

    @property
    def meta(self) -> dict:
        return {"fee": self.fee, "spacing": self.spacing, "token0": self.token0, "token1": self.token1}

    def _call(self, sel: str, types=None, args=None) -> bytes:
        self.calls += 1
        data = "0x" + sel + (abi_encode(types, args).hex() if types else "")
        return self.src.call(self.a, data, self.block)

    def word(self, pos: int) -> int:
        if pos not in self._words:
            self._words[pos] = int.from_bytes(self._call(S_BITMAP, ["int16"], [pos])[:32], "big")
        return self._words[pos]

    def net(self, t: int) -> int:
        if t not in self._nets:
            r = self._call(S_TICKS, ["int24"], [t])
            self._nets[t] = abi_decode(["uint128", "int128"], r[:64])[1]
        return self._nets[t]

    def next_tick(self, tick: int, lte: bool) -> tuple[int, bool]:
        sp = self.spacing
        comp = tick // sp
        if lte:
            pos, bit = comp >> 8, comp % 256
            masked = self.word(pos) & ((1 << bit) - 1 + (1 << bit))
            if masked:
                return (comp - (bit - _msb(masked))) * sp, True
            return (comp - bit) * sp, False
        comp += 1
        pos, bit = comp >> 8, comp % 256
        masked = self.word(pos) & ~((1 << bit) - 1)
        if masked:
            return (comp + (_lsb(masked) - bit)) * sp, True
        return (comp + (255 - bit)) * sp, False

    def quote_exact_in(self, amount_in: int, zero_for_one: bool, max_steps: int = 200) -> dict:
        """Exact-input swap on a copy of the state. Returns amountOut, fee paid (input token), end sqrtPrice,
        ticks crossed."""
        sp, liq, tick = self.sqrt, self.liq, self.tick
        remaining, out, fees, crossed = amount_in, 0, 0, 0
        limit = MIN_SQRT + 1 if zero_for_one else MAX_SQRT - 1
        for _ in range(max_steps):
            if remaining <= 0 or sp == limit:
                break
            nxt, init = self.next_tick(tick, zero_for_one)
            nxt = max(MIN_TICK, min(MAX_TICK, nxt))
            target = sqrt_at_tick(nxt)
            target = max(target, limit) if zero_for_one else min(target, limit)
            if liq == 0:
                sp_new, a_in, a_out, f = target, 0, 0, 0
            else:
                sp_new, a_in, a_out, f = swap_step(sp, target, liq, remaining, self.fee, zero_for_one)
            remaining -= a_in + f
            out += a_out
            fees += f
            if sp_new == target:
                if init:
                    n = self.net(nxt)
                    liq += -n if zero_for_one else n
                    crossed += 1
                tick = nxt - 1 if zero_for_one else nxt
            else:
                tick = None  # stopped inside the range; not needed further
            sp = sp_new
            if tick is None:
                break
        return {"amountOut": out, "fee": fees, "sqrtEnd": sp, "ticksCrossed": crossed, "unfilled": max(0, remaining)}

    def price1per0(self, dec0: int, dec1: int) -> Decimal:
        return (Decimal(self.sqrt) / Q96) ** 2 * Decimal(10) ** (dec0 - dec1)
