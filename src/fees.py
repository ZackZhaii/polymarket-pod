"""Task AE (backlog #19): the one place paper fees are computed.

RiskGate used to charge fees in two places with two price bases:
- edge stress: ``fee_rate * market_price`` per share, subtracted from the edge;
- booked fee: ``Decision.fee_usd = notional_usd * fee_rate``, i.e. ``fee_rate * effective_price``
  per share (paper_ledger writes it rounded to 8 decimals and sums it into daily_loss_usd).

Both now use one per-share fee, the higher of the two:

    fee_per_share = max(fee_rate * market_price, fee_rate * effective_price, 0)

(for a valid fee_rate >= 0 that is fee_rate * max(market_price, effective_price)), so the
more conservative (higher) result wins, a fee is never a rebate, and no fee is ever lower than before
(tests/test_fee_characterisation.py checks this on a grid against frozen copies of the
old formulas). Two names keep the uses apart:

- stress_fee: USD per share that RiskGate subtracts from the edge.
- ledger_fee: USD booked for a simulated fill (Decision.fee_usd -> paper ledger fee_usd).
  It is ``max(notional_usd * fee_rate, shares * fee_per_share)``, so it is never below the
  old notional-based fee, even when shares is 0 or unusable.
- booked_fee_usd: the rounding paper_ledger writes (8 decimals, unchanged).

Fail closed (Task U): stress_fee returns None when fee_rate, market_price or effective_price
is not a finite number; RiskGate then forces edge = -1 (veto), exactly as before. ledger_fee
returns None when fee_rate or notional_usd is not a finite number; RiskGate then reports 0.0 in a
decision that is already vetoed and adds invalid_fee if nothing else did. A negative fee_rate
still vetoes as negative_cost_assumption; its fee is no longer negative.

No new threshold: fee_rate comes from the intent. There is no fee constant in code and no
fee key in config/risk.toml. Pure functions: no I/O, no orders.
"""
from __future__ import annotations

import math

FEE_PRICE_BASIS = "max(market_price, effective_price)"


def _finite(value: object) -> float | None:
    """A finite int or float; bools, strings, None, NaN and inf are None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def fee_per_share(fee_rate: object, market_price: object, effective_price: object) -> float | None:
    """max(fee_rate * market_price, fee_rate * effective_price, 0) in USD per share; None fails closed."""
    rate = _finite(fee_rate)
    market = _finite(market_price)
    effective = _finite(effective_price)
    if rate is None or market is None or effective is None:
        return None
    return max(rate * market, rate * effective, 0.0)


def stress_fee(fee_rate: object, market_price: object, effective_price: object) -> float | None:
    """RiskGate edge stress: USD per share subtracted from the edge. None means veto (edge -1)."""
    return fee_per_share(fee_rate, market_price, effective_price)


def ledger_fee(
    fee_rate: object,
    market_price: object,
    effective_price: object,
    notional_usd: object,
    shares: object,
) -> float | None:
    """USD fee booked for a simulated fill: max(notional_usd * fee_rate, shares * fee_per_share).

    0.0 when there is no size (notional_usd <= 0); never negative. None (fail closed) when
    fee_rate or notional_usd is not a finite number.
    """
    rate = _finite(fee_rate)
    notional = _finite(notional_usd)
    if rate is None or notional is None:
        return None
    if notional <= 0:
        return 0.0
    fee = max(notional * rate, 0.0)
    per_share = fee_per_share(rate, market_price, effective_price)
    qty = _finite(shares)
    if per_share is not None and qty is not None and qty > 0:
        fee = max(fee, qty * per_share)
    return fee


def booked_fee_usd(fee_usd: float) -> float:
    """The fee_usd paper_ledger writes: rounded to 8 decimals, as before (monotonic)."""
    return round(fee_usd, 8)
