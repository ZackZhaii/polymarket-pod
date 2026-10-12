"""Task CF (#124, F2a): today's unrealized loss on open legs, valued at the conservative liquidation price.

Poly风控's ruling (issue #124, issuecomment-6102130981). Tightening only: no risk.toml key, no new threshold
(MAX_BOOK_AGE_S, max_daily_loss_pct and PAPER_EQUITY_USD are reused), the UTC day stays the day boundary.

Valuation (``liquidation_mark``), per open leg, never at mid:

* long: walk the bid ladder from the best bid down, selling the whole position; shares the depth cannot
  absorb are worth 0. A book row with only a top of book (``best_bid`` / ``best_bid_size``, the M1
  recorder's row) is a one-level ladder; a top of book without a size absorbs nothing (0).
* short (M2 opens none; kept as a defence): walk the ask ladder buying the position back; shares the depth
  cannot supply cost 1 USD each. A short's value is negative (minus the buy-back cost).
* settled: the payout (1 or 0 per share). Closed but not confirmed settled: 0 for a long (1 to buy back a
  short); settlement booking is #126.
* the book is missing, its ``book_ts`` is missing, unparseable, in the future or older than
  MAX_BOOK_AGE_S, the book is crossed (best bid >= best ask), or a level's price is not inside (0, 1) or its
  size is not a finite number > 0: the leg is worth its worst case (long 0, short -1 per share) and is
  marked ``mark_invalid``. That is not a whole veto: the loss counts and the daily-loss breaker stops
  trading at the cap.

Daily loss (``unrealized_loss``), no netting: each open leg adds ``max(0, ref - value)``. A leg's gain never
offsets another leg, the realized loss or the fees. ``ref`` is

* for the part of the position held at the start of the UTC day: the start-of-day snapshot's mark for it,
  or its cost when the snapshot is late, its mark is 0 or ``mark_invalid``, or it has no mark;
* for the part opened today: today's cost (notional, fees excluded: they are counted already).

Written-off legs (issue #197, Task CK, KEY-approved): a leg with a write-off row (src/ledger_store.py (f))
has its whole cost realized already (src/daily_loss.py), once (RiskGate's correction, issuecomment-6109537257:
its start-of-day part at max(cost, start-of-day mark), ``writeoff_sod_uplift``, the same ``ref`` rule as
below, so the write-off day's loss never drops below this module's loss). From then until it is flat it adds no
unrealized loss on any day (``ref_source`` "written_off", ``loss_usd`` 0); it stays an open leg, so it
still holds every limit until it settles.
Closed parts retain the same reference through ``closed_loss_topup``: add only the amount by which each
reference loss exceeds its already-counted average-cost loss. A part closed by settlement keeps its
conservative (zero) valuation for the rest of the UTC day: a confirmed winner credits nothing back.
A written-off leg's closed part adds no top-up (issue #197): its write-off, cost + ``writeoff_sod_uplift``, is
already at least this reference loss, and src/daily_loss.py books no further loss on its close.

Pure: no clock, no file. Any position or snapshot problem is returned in ``problems``; the caller gives
``daily_loss_usd = None`` and RiskGate vetoes invalid_risk_state (Task AL: a global halt).
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Iterable, Mapping

from src.book_staleness import book_age_problem, parse_book_ts
from src.daily_loss import SIDES, counts_today
from src.ledger_store import KIND_FIELD, SETTLEMENT_KIND, counts, is_writeoff, leg_key
from src.thresholds import LEDGER_OPEN_EPS, MAX_BOOK_AGE_S

SOURCE_BID_LADDER = "bid_ladder"
SOURCE_ASK_LADDER = "ask_ladder"
SOURCE_SETTLED = "settled"
SOURCE_CLOSED_UNRESOLVED = "closed_unresolved"
SOURCE_INVALID = "invalid_book"
SOURCES = (SOURCE_BID_LADDER, SOURCE_ASK_LADDER, SOURCE_SETTLED, SOURCE_CLOSED_UNRESOLVED, SOURCE_INVALID)

NO_BOOK = "no_book"
CROSSED_BOOK = "crossed_book"
BAD_LEVEL = "bad_level"
NO_LEVELS = "no_levels"


@dataclass(frozen=True)
class Mark:
    """One leg's value now. ``value_usd`` is signed: a long's sale proceeds, minus a short's buy-back cost."""

    value_usd: float
    source: str
    book_ts: str | None = None
    mark_invalid: bool = False
    reason: str | None = None


def _finite(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _level(entry: object) -> tuple[float, float] | None:
    """A ``{"price", "size"}`` object or a ``[price, size]`` pair; None when unusable."""
    if isinstance(entry, Mapping):
        price, size = _finite(entry.get("price")), _finite(entry.get("size"))
    elif isinstance(entry, (list, tuple)) and len(entry) == 2:
        price, size = _finite(entry[0]), _finite(entry[1])
    else:
        return None
    if price is None or size is None or not 0.0 < price < 1.0 or size <= 0.0:
        return None
    return price, size


def _ladder(book: Mapping[str, Any], side: str) -> tuple[list[tuple[float, float]] | None, str | None]:
    """The levels on one side of the book, best first. ``side`` is "bids" or "asks"."""
    raw = book.get(side)
    if raw is None:  # the recorder's top-of-book row: one level, or none without a size
        top = "best_bid" if side == "bids" else "best_ask"
        if book.get(top) is None:
            return [], None
        level = _level([book.get(top), book.get(top + "_size")])
        if level is None:
            price = _finite(book.get(top))
            if price is None or not 0.0 < price < 1.0:
                return None, BAD_LEVEL
            return [], None  # a usable price without a size absorbs nothing
        return [level], None
    if not isinstance(raw, (list, tuple)):
        return None, BAD_LEVEL
    levels = []
    for entry in raw:
        level = _level(entry)
        if level is None:
            return None, BAD_LEVEL
        levels.append(level)
    levels.sort(key=lambda pair: pair[0], reverse=(side == "bids"))
    return levels, None


def _best(book: Mapping[str, Any], side: str, levels: list[tuple[float, float]]) -> float | None:
    if levels:
        return levels[0][0]
    return _finite(book.get("best_bid" if side == "bids" else "best_ask"))


def worst_value(shares: float) -> float:
    """A long is worth 0; a short costs 1 USD per share to buy back."""
    return 0.0 if shares > 0 else -abs(shares)


def liquidation_mark(
    shares: float,
    book: Mapping[str, Any] | None,
    now: datetime,
    *,
    settled_payout: float | None = None,
    closed: bool = False,
    max_age_s: float = MAX_BOOK_AGE_S,
) -> Mark:
    """Value ``shares`` (signed, long > 0) of one leg now. Never raises for a bad book: it marks it invalid."""
    if abs(shares) <= LEDGER_OPEN_EPS:
        return Mark(0.0, SOURCE_SETTLED if settled_payout is not None else SOURCE_BID_LADDER)
    if settled_payout is not None:
        payout = _finite(settled_payout)
        if payout not in (0.0, 1.0):
            return Mark(worst_value(shares), SOURCE_INVALID, None, True, "bad_payout")
        return Mark(shares * payout, SOURCE_SETTLED)
    if closed:
        return Mark(worst_value(shares), SOURCE_CLOSED_UNRESOLVED)
    if not isinstance(book, Mapping):
        return Mark(worst_value(shares), SOURCE_INVALID, None, True, NO_BOOK)
    book_ts = book.get("book_ts")
    stamp = book_ts if isinstance(book_ts, str) else None
    age = book_age_problem(book_ts, now, max_age_s)
    if age is not None:
        return Mark(worst_value(shares), SOURCE_INVALID, stamp, True, age)
    bids, bid_problem = _ladder(book, "bids")
    asks, ask_problem = _ladder(book, "asks")
    if bids is None or asks is None:
        return Mark(worst_value(shares), SOURCE_INVALID, stamp, True, bid_problem or ask_problem)
    best_bid, best_ask = _best(book, "bids", bids), _best(book, "asks", asks)
    if best_bid is not None and best_ask is not None and best_bid >= best_ask:
        return Mark(worst_value(shares), SOURCE_INVALID, stamp, True, CROSSED_BOOK)
    levels = bids if shares > 0 else asks
    if not levels:
        return Mark(worst_value(shares), SOURCE_INVALID, stamp, True, NO_LEVELS)
    left = abs(shares)
    usd = 0.0
    for price, size in levels:
        take = min(left, size)
        usd += take * price
        left -= take
        if left <= LEDGER_OPEN_EPS:
            left = 0.0
            break
    if shares > 0:
        return Mark(usd, SOURCE_BID_LADDER, stamp)
    return Mark(-(usd + left * 1.0), SOURCE_ASK_LADDER, stamp)


@dataclass
class LegPosition:
    """One leg after replaying the counted rows (average cost, as src/daily_loss.py)."""

    shares: float = 0.0       # signed, long > 0
    basis: float = 0.0        # USD paid for a long / received for a short (>= 0)
    carried: float = 0.0      # of abs(shares): held since the start of the day
    new_basis: float = 0.0    # USD of today's part (abs(shares) - carried)
    written_off: bool = False  # issue #197: its whole cost is realized already (no unrealized loss)


@dataclass
class Closing:
    key: tuple[str, str]
    part: LegPosition
    proceeds_usd: float      # signed: long sale / negative short buy-back
    settlement: bool = False  # closed by a settlement row (a market with no live book)
    written_off: bool = False  # issue #197: the leg's loss is realized by its write-off already


@dataclass
class Positions:
    start: dict[tuple[str, str], LegPosition] = field(default_factory=dict)
    now: dict[tuple[str, str], LegPosition] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    closed: list[Closing] = field(default_factory=list)


def _apply(leg: LegPosition, side: str, qty: float, usd: float, today: bool) -> None:
    sign = 1.0 if side == "buy" else -1.0
    if abs(leg.shares) <= LEDGER_OPEN_EPS:
        leg.shares = leg.basis = leg.carried = leg.new_basis = 0.0
        leg.written_off = False
    if qty <= 0:
        return
    held = leg.shares
    if held == 0.0 or (held > 0) == (sign > 0):
        leg.shares = held + sign * qty
        leg.basis += usd
        if today:
            leg.new_basis += usd
        else:
            leg.carried = abs(leg.shares)
        return
    open_qty = abs(held)
    closed = min(qty, open_qty)
    keep = (open_qty - closed) / open_qty
    rest = qty - closed
    if rest > LEDGER_OPEN_EPS:  # flips: the remainder opens the other side at the fill's price
        leg.shares = sign * rest
        leg.basis = usd * rest / qty
        leg.carried = 0.0 if today else rest
        leg.new_basis = leg.basis if today else 0.0
        return
    leg.shares = held + sign * qty
    leg.basis *= keep
    leg.carried *= keep
    leg.new_basis *= keep
    if abs(leg.shares) <= LEDGER_OPEN_EPS:
        leg.shares = leg.basis = leg.carried = leg.new_basis = 0.0


def positions(rows: Iterable[dict[str, Any]], today: str, ids: Iterable[str] | None = None) -> Positions:
    """Open legs at the start of ``today`` (UTC) and now, with the part of each held since the start."""
    result = Positions()
    rows = list(rows)
    labels = list(ids) if ids is not None else [str(i) for i in range(len(rows))]
    legs: dict[tuple[str, str], LegPosition] = {}
    started = False
    for row, label in zip(rows, labels):
        if not counts(row):
            continue
        side = row.get("side")
        qty = _finite(row.get("shares") if row.get("shares") is not None else 0.0)
        notional = row.get("notional_usd")
        usd = None if notional is None else _finite(notional)
        is_today = counts_today(row.get("ts"), today)
        if side not in SIDES or qty is None or qty < 0 or is_today is None or (notional is not None and (usd is None or usd < 0)):
            result.problems.append(f"leg_marks_invalid_row:{label}")
            continue
        if usd is None:  # legacy row without notional: the worst price for the loss (as daily_loss)
            usd = qty * 1.0 if side == "buy" else 0.0
        if is_today and not started:
            started = True
            result.start = {key: LegPosition(leg.shares, leg.basis, abs(leg.shares), 0.0) for key, leg in legs.items()}
            for leg in legs.values():
                leg.carried, leg.new_basis = abs(leg.shares), 0.0
        key = leg_key(row.get("market"), row.get("token_id"))
        leg = legs.setdefault(key, LegPosition())
        if is_writeoff(row):  # issue #197: moves nothing; the leg's cost is realized (src/daily_loss.py)
            leg.written_off = abs(leg.shares) > LEDGER_OPEN_EPS
            continue
        if is_today and qty > 0 and abs(leg.shares) > LEDGER_OPEN_EPS and (leg.shares > 0) != (side == "buy"):
            closed = min(qty, abs(leg.shares))
            fraction = closed / abs(leg.shares)
            part = LegPosition(leg.shares * fraction, leg.basis * fraction,
                               leg.carried * fraction, leg.new_basis * fraction)
            proceeds = (1.0 if leg.shares > 0 else -1.0) * usd * closed / qty
            result.closed.append(Closing(key, part, proceeds, row.get(KIND_FIELD) == SETTLEMENT_KIND, leg.written_off))
        _apply(leg, side, qty, usd, bool(is_today))
    if not started:
        result.start = {key: LegPosition(leg.shares, leg.basis, abs(leg.shares), 0.0) for key, leg in legs.items()}
    result.start = {key: leg for key, leg in result.start.items() if abs(leg.shares) > LEDGER_OPEN_EPS}
    result.now = {key: leg for key, leg in legs.items() if abs(leg.shares) > LEDGER_OPEN_EPS}
    return result


def leg_id(key: tuple[str, str]) -> str:
    """The snapshot's ``leg_key`` string for a (market, token_id) leg."""
    return f"{key[0]}|{key[1]}"


def snapshot_legs(start: Mapping[tuple[str, str], LegPosition], marks: Mapping[tuple[str, str], Mark]) -> list[dict[str, Any]]:
    """The start-of-day snapshot's ``legs`` list, in a stable order."""
    legs = []
    for key in sorted(start):
        leg, mark = start[key], marks[key]
        legs.append({"leg_key": leg_id(key), "shares": leg.shares, "cost_basis_usd": leg.basis,
                     "mark_usd": mark.value_usd, "mark_source": mark.source, "book_ts": mark.book_ts,
                     "mark_invalid": mark.mark_invalid})
    return legs


def _snapshot_legs(snapshot: Mapping[str, Any] | None) -> tuple[dict[str, Mapping[str, Any]], bool]:
    """The snapshot's legs by leg_key and whether it is late (no snapshot counts as late)."""
    snap_legs: dict[str, Mapping[str, Any]] = {}
    if snapshot is None:
        return snap_legs, True
    for entry in snapshot.get("legs") or []:
        snap_legs[str(entry.get("leg_key"))] = entry
    return snap_legs, snapshot.get("late") is not False


def _usable_start_mark(entry: Mapping[str, Any] | None, late: bool) -> float | None:
    """A start-of-day mark usable as the carried part's ``ref`` (on time, valid, not 0), else None (cost)."""
    snap_mark = None if entry is None else _finite(entry.get("mark_usd"))
    usable = (not late and entry is not None and entry.get("mark_invalid") is False
              and snap_mark is not None and snap_mark != 0.0)
    return snap_mark if usable else None


def snapshot_problems(pos: Positions, snapshot: Mapping[str, Any] | None) -> list[str]:
    """The snapshot must describe the same start-of-day legs (none: no problem)."""
    if snapshot is None:
        return []
    snap_legs, _ = _snapshot_legs(snapshot)
    problems = []
    for key, start in pos.start.items():
        entry = snap_legs.get(leg_id(key))
        if (entry is None or _finite(entry.get("shares")) is None
                or abs(float(entry["shares"]) - start.shares) > LEDGER_OPEN_EPS):
            problems.append(f"daily_marks_leg_mismatch:{leg_id(key)}")
    if len(snap_legs) != len(pos.start):
        problems.append("daily_marks_leg_count_mismatch")
    return problems


def writeoff_sod_uplift(pos: Positions, snapshot: Mapping[str, Any] | None, key: tuple[str, str]) -> float:
    """Issue #197, RiskGate's correction (issuecomment-6109537257): the start-of-day mark above cost, in USD.

    A leg's write-off is ``cost + uplift``: its part held since the start of the UTC day at max(cost, start-of-day
    mark), today's part at cost. The start mark is used exactly when ``unrealized_loss`` uses it as ``ref`` (an on
    time snapshot, a valid mark, not 0, the same side); otherwise, or with no snapshot yet today, 0 (the cost, as
    ``ref`` is then). A long: max(0, mark - cost) of the carried shares. A short (M2 opens none): max(0, proceeds -
    start-of-day buy-back cost) of the carried shares. Call ``snapshot_problems`` first.
    """
    leg, start = pos.now.get(key), pos.start.get(key)
    if (leg is None or start is None or leg.written_off or leg.carried <= LEDGER_OPEN_EPS
            or abs(start.shares) <= LEDGER_OPEN_EPS or (start.shares > 0) != (leg.shares > 0)):
        return 0.0
    snap_legs, late = _snapshot_legs(snapshot)
    snap_mark = _usable_start_mark(snap_legs.get(leg_id(key)), late)
    if snap_mark is None:
        return 0.0
    carried_cost = start.basis / abs(start.shares) * leg.carried
    carried_mark = abs(snap_mark) / abs(start.shares) * leg.carried
    return max(0.0, carried_mark - carried_cost) if leg.shares > 0 else max(0.0, carried_cost - carried_mark)


@dataclass
class Unrealized:
    loss_usd: float = 0.0
    legs: list[dict[str, Any]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def _reference(leg: LegPosition, start: LegPosition | None, entry: Mapping[str, Any] | None,
               late: bool) -> tuple[float, str]:
    carried_ref = 0.0
    source = "cost"
    if leg.carried > LEDGER_OPEN_EPS and start is not None and (start.shares > 0) == (leg.shares > 0):
        snap_mark = _usable_start_mark(entry, late)  # the same rule as writeoff_sod_uplift (issue #197)
        if snap_mark is not None:
            carried_ref = abs(snap_mark) / abs(start.shares) * leg.carried
            source = "start_mark"
        else:
            carried_ref = start.basis / abs(start.shares) * leg.carried
    elif leg.carried > LEDGER_OPEN_EPS:
        carried_ref = leg.basis - leg.new_basis
    return (1.0 if leg.shares > 0 else -1.0) * (carried_ref + leg.new_basis), source


def closed_loss_topup(pos: Positions, snapshot: Mapping[str, Any] | None) -> float:
    """Extra loss for today's closed parts; caller validates the snapshot with ``unrealized_loss`` first."""
    entries = {str(entry.get("leg_key")): entry for entry in (snapshot or {}).get("legs") or []}
    late = snapshot is None or snapshot.get("late") is not False
    total = 0.0
    for close in pos.closed:
        if close.written_off:  # issue #197: the write-off (>= this reference loss) is realized; never twice
            continue
        ref, _ = _reference(close.part, pos.start.get(close.key), entries.get(leg_id(close.key)), late)
        cost = (1.0 if close.part.shares > 0 else -1.0) * close.part.basis
        # Codex回迁 (Owner 2026-10-13, tightening only): a part closed by a settlement row had no live book, so
        # today it was valued conservatively (zero liquidation value). A confirmed winner does NOT credit that
        # back within the UTC day: its reference loss is kept as if it exited at the worst no-book value. The winner's gain is
        # still booked as a realized gain (never netted against losses); the next UTC day starts from zero.
        # Worst exit with no book: a long is worth 0, a short is bought back at 1 per share.
        worst = 0.0 if close.part.shares > 0 else -abs(close.part.shares)
        exit_value = min(close.proceeds_usd, worst) if close.settlement else close.proceeds_usd
        total += max(0.0, max(0.0, ref - exit_value) - max(0.0, cost - close.proceeds_usd))
    return total


def unrealized_loss(
    pos: Positions,
    marks: Mapping[tuple[str, str], Mark],
    snapshot: Mapping[str, Any] | None,
) -> Unrealized:
    """Σ over open legs of max(0, ref - value); no leg's gain offsets anything. See the module docstring."""
    result = Unrealized()
    snap_legs, late = _snapshot_legs(snapshot)
    result.problems = snapshot_problems(pos, snapshot)
    if result.problems:
        return result
    for key in sorted(pos.now):
        leg = pos.now[key]
        mark = marks[key]
        if leg.written_off:  # issue #197: realized once by the write-off; never an unrealized loss again
            result.legs.append(dict(asdict(mark), leg_key=leg_id(key), shares=leg.shares, ref_usd=0.0,
                                    ref_source="written_off", loss_usd=0.0))
            continue
        ref, ref_source = _reference(leg, pos.start.get(key), snap_legs.get(leg_id(key)), late)
        loss = max(0.0, ref - mark.value_usd)
        result.loss_usd += loss
        result.legs.append(dict(asdict(mark), leg_key=leg_id(key), shares=leg.shares, ref_usd=ref,
                                ref_source=ref_source, loss_usd=loss))
    return result
