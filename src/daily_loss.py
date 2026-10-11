"""Task AR (backlog #4): the day's loss RiskGate's max_daily_loss check compares with the cap.

Before Task AR, ``daily_loss_usd`` (src/paper_ledger.py load_state) summed only today's booked
fees, so ``max_daily_loss`` was really a daily fee cap (SPEC_signal_to_intent section 8, item 12).
Now it is

    daily_loss_usd = today's booked fees + today's gross realized losses

Tightening only: every row the old sum counted is still counted with the same fee, and the
realized part is never negative. Realized gains are reported (``realized_gain_usd``) but never
offset a loss or the fees (no netting; netting would be a policy choice for the Operator).

Realized P&L, per leg (market + token_id, ledger_store.leg_key), replaying the counted rows
(ledger_store.counts) in ledger order with an average cost basis:

* a fill that grows the position (or opens one) adds its notional to the leg's basis;
* a fill against the position closes ``closed = min(fill shares, open shares)`` shares. Long
  closed by a sell: pnl = sell proceeds for those shares - average cost of those shares. Short
  closed by a buy: pnl = average proceeds received for those shares - buy cost of those shares.
  Any remainder opens the other side at the fill's price;
* fees are not in pnl (they are already counted in full, once);
* a counted row without notional_usd (legacy rows only; id'd rows need it) is valued at the worst
  price for the loss: a buy at 1 USD per share, a sell at 0.

A fill's realized loss belongs to the UTC day of its own ``ts`` (the close), whenever the position
was opened. Day boundary: the UTC calendar day of ``podlib.utc_now()`` (00:00:00Z to 24:00:00Z),
the convention load_state already used. A row counts toward today when its ts string starts with
today's date (the old rule, kept so nothing counted before drops out) or its parsed UTC date is
today or later (a future-dated row counts toward today, it is never skipped).

Fail closed: a counted row whose ts is missing or not a zoned ISO-8601 time / epoch, or whose side
is not buy/sell, or whose shares, notional or fee is not a finite number >= 0 (read with float(), as the ledger reads it), makes the result
invalid (``problems`` non-empty). load_state then gives ``daily_loss_usd = None``, RiskGate vetoes
``invalid_risk_state`` and paper_ledger escalates that to a global halt (Task AL).

Write-offs (issue #197, Task CK, KEY-approved "close-then-write-off"): a ``kind`` "writeoff" row
(src/ledger_store.py (f)) on an open leg of a confirmed closed market books the leg's whole worst-case
loss as realized, once, on the row's own UTC day: a long's average cost (``basis``), a short's
``shares x 1 - basis`` (the buy-back at 1). Its ``writeoff_usd`` must be exactly that amount and its
``writeoff_shares`` the whole open leg, else ``daily_loss_invalid_writeoff`` (invalid_risk_state). From
then on the leg is written off until it is flat: a later close (the settlement) books no further loss
(a losing leg's settlement pnl is the loss already written off; it is reported as
``writeoff_settled_loss_usd`` only), a winning leg's gain is reported as a gain and never offsets
anything, and a fill that would grow or flip a written-off leg is ``daily_loss_writeoff_leg_grew``.

Pure: no clock (``today`` is passed in), no file, no bus. No new threshold.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable

from src.book_staleness import parse_book_ts
from src.ledger_store import counts, is_writeoff, leg_key
from src.thresholds import LEDGER_OPEN_EPS

SIDES = ("buy", "sell")


@dataclass
class DailyLoss:
    """Today's loss parts. ``total_usd`` is what RiskGate compares with the cap (None when invalid)."""

    today: str
    fees_usd: float = 0.0
    realized_loss_usd: float = 0.0
    realized_gain_usd: float = 0.0
    closing_fills_today: int = 0
    writeoff_usd: float = 0.0              # issue #197: today's write-offs (already in realized_loss_usd)
    writeoffs_today: int = 0
    writeoff_settled_loss_usd: float = 0.0  # today's closes of written-off legs: not counted again
    legs: dict[tuple[str, str], dict[str, float]] = field(default_factory=dict)  # final average-cost state
    problems: list[str] = field(default_factory=list)

    @property
    def total_usd(self) -> float | None:
        if self.problems:
            return None
        return self.fees_usd + self.realized_loss_usd


def _number(value: object) -> float | None:
    """The ledger's own number rule (ledger_store.non_finite_ledger_rows / _apply use float())."""
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def counts_today(ts: object, today: str) -> bool | None:
    """True when a row with this ts counts toward ``today`` (UTC ``YYYY-MM-DD``); None if ts is unusable."""
    parsed = parse_book_ts(ts)
    if parsed is None:
        return None
    if isinstance(ts, str) and ts.strip().startswith(today):
        return True
    return parsed.strftime("%Y-%m-%d") >= today


def daily_loss(rows: Iterable[dict[str, Any]], today: str, ids: Iterable[str] | None = None) -> DailyLoss:
    """Today's fees plus gross realized losses over the counted rows, in ledger order."""
    result = DailyLoss(today=today)
    if not (isinstance(today, str) and len(today) == 10 and parse_book_ts(today + "T00:00:00Z") is not None):
        result.problems.append("invalid_today")
        return result
    rows = list(rows)
    labels = list(ids) if ids is not None else [str(i) for i in range(len(rows))]
    legs: dict[tuple[str, str], dict[str, float]] = {}
    for row, label in zip(rows, labels):
        if not counts(row):
            continue
        side = row.get("side")
        qty = _number(row.get("shares") if row.get("shares") is not None else 0.0)
        fee = _number(row.get("fee_usd") if row.get("fee_usd") is not None else 0.0)
        notional = row.get("notional_usd")
        usd = None if notional is None else _number(notional)
        is_today = counts_today(row.get("ts"), today)
        bad = []
        if side not in SIDES:
            bad.append("side")
        if qty is None or qty < 0:
            bad.append("shares")
        if fee is None or fee < 0:
            bad.append("fee_usd")
        if notional is not None and (usd is None or usd < 0):
            bad.append("notional_usd")
        if is_today is None:
            bad.append("ts")
        if bad:
            result.problems.extend(f"daily_loss_invalid_{name}:{label}" for name in bad)
            continue
        if usd is None:  # legacy row without notional: the worst price for the loss
            usd = qty * 1.0 if side == "buy" else 0.0
        if is_today:
            result.fees_usd += fee
        leg = legs.setdefault(leg_key(row.get("market"), row.get("token_id")),
                              {"shares": 0.0, "basis": 0.0, "written_off": 0.0})
        if is_writeoff(row):  # issue #197: the leg's whole worst-case loss, realized once
            amount = writeoff_amount(leg)
            booked = _number(row.get("writeoff_usd"))
            shares = _number(row.get("writeoff_shares"))
            if (amount is None or leg["written_off"] or booked is None or shares is None
                    or abs(shares - abs(leg["shares"])) > LEDGER_OPEN_EPS or abs(booked - amount) > LEDGER_OPEN_EPS):
                result.problems.append(f"daily_loss_invalid_writeoff:{label}")
                continue
            leg["written_off"] = 1.0
            if is_today:
                result.writeoff_usd += amount
                result.writeoffs_today += 1
                result.realized_loss_usd += amount
            continue
        held = leg["shares"]
        if leg["written_off"] and qty > 0 and (side == "buy") == (held > 0):
            result.problems.append(f"daily_loss_writeoff_leg_grew:{label}")
            continue
        pnl = _apply(leg, side, qty, usd)
        if leg["written_off"] and abs(leg["shares"]) > LEDGER_OPEN_EPS and (leg["shares"] > 0) != (held > 0):
            result.problems.append(f"daily_loss_writeoff_leg_grew:{label}")  # flipped through flat
            continue
        written_off = bool(leg["written_off"])
        if abs(leg["shares"]) <= LEDGER_OPEN_EPS:
            leg["written_off"] = 0.0
        if pnl is not None and is_today:
            result.closing_fills_today += 1
            if pnl < 0 and written_off:
                result.writeoff_settled_loss_usd += -pnl  # already realized by the write-off: never twice
            elif pnl < 0:
                result.realized_loss_usd += -pnl
            else:
                result.realized_gain_usd += pnl
    result.legs = legs
    return result


def writeoff_amount(leg: dict[str, float]) -> float | None:
    """Issue #197: an open leg's whole worst-case loss at average cost (None: the leg is flat).

    A long is worth 0 at worst: its cost. A short costs 1 per share to buy back: shares - proceeds.
    """
    held = leg.get("shares", 0.0)
    if abs(held) <= LEDGER_OPEN_EPS:
        return None
    basis = leg.get("basis", 0.0)
    return max(0.0, basis) if held > 0 else max(0.0, abs(held) * 1.0 - basis)


def _apply(leg: dict[str, float], side: str, qty: float, usd: float) -> float | None:
    """Book one fill into the leg; return the realized pnl of the part it closed (None: closed nothing).

    ``shares`` is signed (long > 0); ``basis`` is the USD paid for an open long, or received for
    an open short (always >= 0).
    """
    sign = 1.0 if side == "buy" else -1.0
    held = leg["shares"]
    if abs(held) <= LEDGER_OPEN_EPS:
        held, leg["basis"] = 0.0, 0.0
    if qty <= 0:
        return None
    if held == 0.0 or (held > 0) == (sign > 0):
        leg["shares"] = held + sign * qty
        leg["basis"] += usd
        return None
    open_qty = abs(held)
    closed = min(qty, open_qty)
    basis_part = leg["basis"] * closed / open_qty
    fill_part = usd * closed / qty
    pnl = (fill_part - basis_part) if held > 0 else (basis_part - fill_part)
    rest = qty - closed
    if rest > LEDGER_OPEN_EPS:
        leg["shares"] = sign * rest
        leg["basis"] = usd - fill_part
    else:
        leg["shares"] = held + sign * qty
        leg["basis"] -= basis_part
        if abs(leg["shares"]) <= LEDGER_OPEN_EPS:
            leg["shares"], leg["basis"] = 0.0, 0.0
    return pnl
