"""Phase 0 paper ledger.

Simulated fills are written only after RiskGate.evaluate returns allow.
No CLOB authentication, no order POST, no withdraw, no size-up.
A single-leg fill is directional exposure, not risk-free arbitrage.
Positions are netted per leg (market + token_id); the tokens of an arb pair never
offset each other (backlog item 5, Task P).
Task AF (backlog #20): the ledger is read strictly, fills carry ids and are deduped, writes are
atomic and cash / positions are conservation-checked on every load and write (src/ledger_store.py).
Any integrity problem is an invalid risk state and halts booking.
Task AL (Sentinel design gap 7): invalid_risk_state and the Executor's ledger_integrity /
ledger_write_failed halt are now persisted global halts (src/halt_store.py): every later intent
vetoes until the Operator resumes it.
Issue #63: the CLI now checks the whole risk file (sha256 and every key, ``src.pins.refuse_start``)
right after argument parsing and before the config is read. A file that drifted before the run was
only caught by the five in-memory ``podlib.PINS`` RiskGate holds, so a changed max_open_positions
could still book a fill while the file stayed byte-identical during the run. Drift is refused with
the existing EXIT_FAILED and a recorded global halt; the two entry points (operator intent and the
default self-check) both stop there. ``evaluate(cfg)`` still takes an in-memory config.
Issue #56: the quotes history that RiskGate's structural wash check reads is read strictly
(``read_quotes``), for every ``load_state`` return path. The generic ``podlib.read_jsonl`` keeps its
loose behaviour for every other caller; here a quotes file that cannot be read in full is never a
short history: ``own_quotes`` is None and RiskGate vetoes the existing ``wash_input_invalid``.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.podlib import (  # noqa: E402
    BOOKS_PATH,
    MARKETS_PATH,
    PAPER_EQUITY_USD,
    PAPER_PATH,
    QUOTES_PATH,
    RESOLUTIONS_PATH,
    RISK_PATH,
    bus,
    load_risk,
    read_jsonl,
    utc_now,
)
from src import calibration_ids  # noqa: E402 - Task AX (M3 step 2)
from src import category  # noqa: E402 - issue #131: category buckets on the fill
from src.book_staleness import latest_by_time, parse_book_ts  # noqa: E402
from src.fees import booked_fee_usd, ledger_fee  # noqa: E402 - Task AE: one fee module
from src import ledger_store  # noqa: E402 - Task AF: ledger integrity
from src import sentinel  # noqa: E402 - Task AL: ledger halts and invalid_risk_state are global halts
from src.thresholds import ASSUMED_SLIPPAGE  # noqa: E402 - Task AT: the one assumed slippage
from src.ledger_store import (  # noqa: E402,F401 - Task AF: moved here, re-exported unchanged
    LEDGER_NUMBER_FIELDS,
    LedgerIntegrityError,
    leg_key,
    non_finite_ledger_rows,
)
from src.ledger_store import counts as _counts  # noqa: E402 - Task AF: moved, same rule
from src.risk_gate import Decision, Intent, evaluate  # noqa: E402
from src.daily_loss import daily_loss  # noqa: E402 - Task AR: fees + realized losses
from src.expiry_rule import hours_to_expiry, parse_end_date  # noqa: E402 - Task BA: hours at entry
from src.structural_gate import STRUCTURAL_ARB_DISABLED  # noqa: E402 - Task DD (#237)
from src import wash_detect  # noqa: E402 - Task CH (#145): the market's token pair
from src import day_boundary  # noqa: E402 - Task CX (#225): the one day-boundary rule
from src import daily_marks, leg_marks  # noqa: E402 - Task CF (#124): unrealized loss in the daily loss

# Task CF (#124): the start-of-day leg marks (one file per UTC day, never overwritten) and the books the
# open legs are valued from (the recorder's books file; a leg without a fresh usable book is worth 0).
DAILY_MARKS_DIR = ROOT / "runs" / "m2" / "daily_marks"
MARK_BOOKS_PATH = BOOKS_PATH
# Issue #126 (Task CG): the confirmed settlement snapshots open legs are settled from (src/settlement.py).
SETTLEMENT_RESOLUTIONS_PATH = RESOLUTIONS_PATH
# Issue #145 (Task CH): the scanner rows the complementary-leg pair check reads a market's two tokens from.
PAIR_MARKETS_PATH = MARKETS_PATH
# The UTC day of this process's last risk evaluation. A day's snapshot is on time (late=False) only when
# this process also evaluated on the previous UTC day, i.e. it was running at the day roll.
_LAST_EVAL_DAY: str | None = None

DIRECTIONAL = "Single-leg fill is directional exposure, not risk-free arbitrage."


from src.thresholds import LEDGER_OPEN_EPS as OPEN_EPS  # noqa: E402 - Task AC


def leg_positions(rows: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, float]]:
    """Net shares per leg, plus exposure at the leg's average fill price.

    Only a buy and a sell of the same token offset each other (closing). The two
    tokens of an arb pair are separate legs and never offset each other, so a
    single filled leg is a full directional position (backlog item 5).
    Exposure is abs(net shares) * (gross notional / gross shares) over that leg's
    rows; a row without notional_usd is valued at 1 USD per share, the most a
    binary token can pay out.
    """
    legs: dict[tuple[str, str], dict[str, float]] = {}
    for row in rows:
        if not _counts(row):
            continue
        qty = float(row.get("shares") or 0.0)
        notional = row.get("notional_usd")
        gross_usd = abs(qty) * 1.0 if notional is None else abs(float(notional))
        leg = legs.setdefault(leg_key(row.get("market"), row.get("token_id")),
                              {"shares": 0.0, "gross_shares": 0.0, "gross_usd": 0.0})
        leg["shares"] += qty if row.get("side") == "buy" else -qty
        leg["gross_shares"] += abs(qty)
        leg["gross_usd"] += gross_usd
    for leg in legs.values():
        open_qty = abs(leg["shares"])
        if open_qty <= OPEN_EPS or leg["gross_shares"] <= 0:
            leg["exposure_usd"] = 0.0
        else:
            leg["exposure_usd"] = open_qty * leg["gross_usd"] / leg["gross_shares"]
    return legs


def read_quotes(path: Path) -> tuple[list[dict[str, Any]] | None, list[str]]:
    """Read the paper quotes file strictly (issue #56): the whole history, or nothing.

    ``podlib.read_jsonl`` silently skips a bad JSON or non-object line, so one damaged uncancelled
    opposite quote could turn the history into an empty one and let RiskGate's structural wash check
    (``src/wash_detect.py``) allow a fill. This reader is used only for the quotes boundary of
    ``load_state``; the generic reader keeps its behaviour for every other caller.

    Returns ``(rows, [])`` for a file that reads cleanly (a missing file, an empty file and blank
    lines stay an empty history, as before). Any other outcome is unusable: ``(None, problems)``
    with one entry per defect, naming the line number and the reason only (a line is never echoed).
    A defect is a non-empty line that is not one JSON object (bad or truncated JSON,
    ``malformed_row:<line>``; an array, null, number or string, ``non_object_row:<line>``), a last
    row without its closing newline (`unterminated_last_row:<line>`, the torn append of
    ``podlib.append_jsonl``), an unreadable file (``unreadable_quotes:<error>``) and bytes that are
    not UTF-8 (``undecodable_quotes``). No threshold, no halt: RiskGate vetoes the existing
    ``wash_input_invalid``, so nothing is booked.
    """
    try:
        data = path.read_bytes() if path.exists() else b""
    except OSError as exc:
        return None, [f"unreadable_quotes:{type(exc).__name__}"]
    if not data:
        return [], []
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None, ["undecodable_quotes"]
    lines = text.split("\n")
    rows: list[dict[str, Any]] = []
    problems: list[str] = []
    for number, line in enumerate(lines, start=1):
        line = line.rstrip("\r")
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            problems.append(f"malformed_row:{number}")
            continue
        if not isinstance(row, dict):
            problems.append(f"non_object_row:{number}")
            continue
        rows.append(row)
    if lines[-1].strip():
        problems.append(f"unterminated_last_row:{len(lines)}")
    if problems:
        return None, problems
    return rows, []


def mark_books(path: Path) -> dict[tuple[str, str], dict[str, Any]]:
    """Task CF: the newest book row per leg (by the recorder's parsed ``ts``). Unreadable: no books (all 0)."""
    try:
        rows = read_jsonl(path)
    except (OSError, UnicodeDecodeError):
        return {}
    latest, _ = latest_by_time(rows, lambda row: (str(row.get("market_id") or ""), str(row.get("token_id") or "")))
    return {leg_key(key[0], key[1]): row for key, row in latest.items()}


def _previous_day(day: str) -> str | None:
    stamp = parse_book_ts(day + "T00:00:00Z")
    if stamp is None:
        return None
    from datetime import timedelta

    return (stamp - timedelta(days=1)).strftime("%Y-%m-%d")


def _previous_day_marks(today: str, start: dict, current: dict) -> tuple[dict, list[str]]:
    """Task CX (#225, D3): for a late snapshot, each start leg's previous-UTC-day last valid mark, if any."""
    prev = _previous_day(today)
    if prev is None:
        return current, []
    legs, problems = daily_marks.read_last_marks(DAILY_MARKS_DIR, prev)
    if problems or not legs:
        return current, problems
    out = dict(current)
    for key, leg in start.items():
        entry = legs.get(leg_marks.leg_id(key))
        if entry is not None and abs(leg.shares) > OPEN_EPS:
            out[key] = leg_marks.Mark(float(entry["per_share_usd"]) * leg.shares, leg_marks.SOURCE_PREV_DAY_LAST,
                                      entry["book_ts"], False)
    return out, []


def _record_last_marks(today: str, now: str, marks: dict) -> list[str]:
    """Task CX (#225): keep today's last valid book mark per leg (read back as the next day's late reference)."""
    fresh = {leg_marks.leg_id(key): {"per_share_usd": abs(mark.value_usd) / abs(shares), "book_ts": mark.book_ts, "at": now}
             for key, (mark, shares) in marks.items()
             if not mark.mark_invalid and mark.source in (leg_marks.SOURCE_BID_LADDER, leg_marks.SOURCE_ASK_LADDER)
             and abs(shares) > OPEN_EPS and 0.0 < abs(mark.value_usd) / abs(shares) <= 1.0}
    if not fresh:
        return []
    legs, problems = daily_marks.read_last_marks(DAILY_MARKS_DIR, today)
    if problems:
        return problems
    merged = dict(legs or {})
    merged.update(fresh)
    try:
        daily_marks.write_last_marks(DAILY_MARKS_DIR, today, now, merged)
    except (OSError, ValueError) as exc:
        return [f"daily_marks_last_write_failed:{type(exc).__name__}"]
    return []


def ensure_daily_snapshot(pos: leg_marks.Positions, today: str, now: str, now_dt: Any,
                          book_map: dict[tuple[str, str], dict[str, Any]], *,
                          clock_ok: bool = False) -> tuple[dict[str, Any] | None, list[str]]:
    """The day's start-of-day snapshot: read it, or write it once (Task CF #124). Shared by daily_unrealized and,
    issue #255, apply_writeoffs (a write-off on the day's first tick must not book its uplift before the snapshot).
    Task CX (#225) / Task DA: lateness is src/day_boundary.day_start_late (the one UTC day-boundary rule, with
    the caller's ``clock_ok``); a late snapshot's reference is max(cost, previous day's last valid mark).
    Returns (snapshot, problems); any problem is fail closed for the caller."""
    snap, problems = daily_marks.read_snapshot(DAILY_MARKS_DIR, today)
    if problems or snap is not None:
        return snap, problems
    start_marks = {key: leg_marks.liquidation_mark(leg.shares, book_map.get(key), now_dt)
                   for key, leg in pos.start.items()}
    # Task CX (#225, D1/D2): late = written more than DAY_START_MAX_LAG_S after today's 00:00 UTC
    # (or a clock anomaly), whatever this process did yesterday.
    late = day_boundary.day_start_late(now, clock_ok=clock_ok)
    if late:
        start_marks, problems = _previous_day_marks(today, pos.start, start_marks)
        if problems:
            return None, problems
    try:
        return daily_marks.create_snapshot(DAILY_MARKS_DIR, today, now, late,
                                           leg_marks.snapshot_legs(pos.start, start_marks)), []
    except daily_marks.SnapshotExists:  # another process wrote it first: use that one
        snap, problems = daily_marks.read_snapshot(DAILY_MARKS_DIR, today)
        return snap, problems or ([] if snap is not None else ["daily_marks_missing_after_create"])
    except (OSError, ValueError) as exc:
        return None, [f"daily_marks_write_failed:{type(exc).__name__}"]


def daily_unrealized(rows: list[dict[str, Any]], ids: list[str], now: str,
                     books: dict[tuple[str, str], dict[str, Any]] | None = None, *,
                     clock_ok: bool = False) -> dict[str, Any]:
    """Task CF (#124): today's unrealized loss on open legs (Poly风控's ruling, src/leg_marks.py).

    Reads (or, on the day's first evaluation, writes once) the start-of-day snapshot, also for closed parts.
    ``clock_ok`` (Task DA, #225 follow-up): the caller's clock state; anything but True (a clock anomaly such as
    the runner's clock_skew, or #253's clock_unverified) makes a snapshot written now late (fail closed). It defaults
    to False (Poly风控 on #274): a caller that has not verified the clock writes a late snapshot.
    Returns ``{"loss_usd", "realized_topup_usd", "legs", "late", "problems"}``; any problem (positions,
    valuation, corrupt or unwritable snapshot, an exception) makes the loss unavailable: invalid_risk_state.
    """
    global _LAST_EVAL_DAY
    result: dict[str, Any] = {"loss_usd": None, "realized_topup_usd": None, "legs": [], "late": None, "problems": []}
    try:
        today = now[:10]
        now_dt = parse_book_ts(now)
        if now_dt is None:
            result["problems"] = ["leg_marks_invalid_now"]
            return result
        pos = leg_marks.positions(rows, today, ids)
        if pos.problems:
            result["problems"] = pos.problems
            return result
        book_map = mark_books(MARK_BOOKS_PATH) if books is None else books
        snap, problems = ensure_daily_snapshot(pos, today, now, now_dt, book_map, clock_ok=clock_ok)
        if problems or snap is None:
            result["problems"] = problems or ["daily_marks_missing_after_create"]
            return result
        _LAST_EVAL_DAY = today
        result["snapshot"] = snap
        marks = {key: leg_marks.liquidation_mark(leg.shares, book_map.get(key), now_dt) for key, leg in pos.now.items()}
        problems = _record_last_marks(today, now, {key: (marks[key], leg.shares) for key, leg in pos.now.items()})
        if problems:
            result["problems"] = problems
            return result
        unrealized = leg_marks.unrealized_loss(pos, marks, snap)
        result["late"] = snap.get("late")
        if unrealized.problems:
            result["problems"] = unrealized.problems
            return result
        result["realized_topup_usd"] = leg_marks.closed_loss_topup(pos, snap)
        result["loss_usd"] = unrealized.loss_usd
        result["legs"] = unrealized.legs
        return result
    except Exception as exc:  # noqa: BLE001 - a valuation that fails is an invalid risk state, never a pass
        result["loss_usd"] = None
        result["problems"] = [f"leg_marks_error:{type(exc).__name__}"]
        return result


def daily_risk_loss(rows: list[dict[str, Any]], ids: list[str], now: str, *,
                    clock_ok: bool = False) -> dict[str, Any]:
    """One UTC evaluation: fees + gross cost losses + closed reference topups + open-leg losses.

    ``clock_ok`` is passed to daily_unrealized (Task DA): False while the caller knows of a clock anomaly."""
    day = daily_loss(rows, now[:10], ids)
    marks = (daily_unrealized(rows, ids, now, clock_ok=clock_ok) if not day.problems
             else {"loss_usd": None, "realized_topup_usd": None, "legs": [], "late": None, "problems": []})
    problems = list(day.problems) + list(marks["problems"])
    if not problems:  # issue #255: every write-off booked today, its uplift recomputed from the day's snapshot
        problems = leg_marks.writeoff_uplift_problems(rows, ids, now[:10], marks.get("snapshot"))
    total = None if problems else day.total_usd + marks["realized_topup_usd"] + marks["loss_usd"]
    return {"loss_usd": total, "day": day, "marks": marks, "problems": problems}


def read_settlements() -> tuple[list[dict[str, Any]] | None, list[str]]:
    """Issue #126: the settlement snapshots, read strictly (resolutions.read_history). Absent file: none."""
    from src import resolutions  # local: keeps paper_ledger's import time unchanged

    try:
        return resolutions.read_history(Path(SETTLEMENT_RESOLUTIONS_PATH)), []
    except resolutions.HistoryError as exc:
        return None, [f"resolutions_unreadable:{type(exc).__name__}"]
    except OSError as exc:
        return None, [f"resolutions_unreadable:{type(exc).__name__}"]


def settlement_blocked_markets() -> frozenset[str]:
    """PR #211: markets under a market halt whose reason says their own close / settlement data contradicts
    itself (src/settlement.blocks_settlement). Other market halts do not block settlement."""
    from src import halt_store, settlement

    return frozenset(str(record.get("market")) for record in halt_store.active_halts()
                     if record.get("scope") == halt_store.SCOPE_MARKET and record.get("market")
                     and settlement.blocks_settlement(record.get("reason")))


def apply_settlements(now: str | None = None, *, skip_markets: frozenset[str] | None = None) -> dict[str, Any]:
    """Issue #126 (Task CG): book a settlement row for every open leg of a market with a confirmed settlement.

    Idempotent (the row's fill_id is derived from the settlement; a booked one is never booked again). A ledger
    that does not verify is left to load_state (invalid_risk_state, as before) and nothing is booked here.
    Returns {"booked": [fill rows], "unsettled_closed_legs": [...], "problems": [...]}; any problem (an
    unreadable resolutions file, two confirmed snapshots that disagree, a failed or refused write) must stop the
    caller from booking anything else.
    """
    from src import settlement

    out: dict[str, Any] = {"booked": [], "unsettled_closed_legs": [], "problems": []}
    if skip_markets is None:  # PR #211: the commit path honours the same blocking market halts as the runner
        try:
            skip_markets = settlement_blocked_markets()
        except Exception as exc:  # noqa: BLE001 - an unreadable halt state: settle nothing (the caller halts)
            out["problems"] = [f"settlement_halt_state_unreadable:{type(exc).__name__}"]
            return out
    snaps, problems = read_settlements()
    if problems:
        out["problems"] = problems
        return out
    if not snaps:
        return out  # nothing can settle: the ledger is not even read (the commit path reads it as before)
    _, ledger = ledger_store.read_ledger(PAPER_PATH)
    if ledger_store.verify(ledger) or non_finite_ledger_rows(ledger.rows):
        return out
    planned = settlement.plan(ledger.rows, list(ledger.ids), snaps or [], now or utc_now())
    out["unsettled_closed_legs"] = planned.unsettled_closed_legs
    if planned.problems:
        out["problems"] = planned.problems
        return out
    for row in planned.rows:
        if str(row.get("market")) in skip_markets:
            # PR #211: the market's own close / settlement data is under a market halt (settlement.blocks_settlement)
            out.setdefault("skipped_markets", []).append(str(row.get("market")))
            continue
        try:
            out["booked"].append(ledger_store.book(PAPER_PATH, row))
        except LedgerIntegrityError as exc:
            out["problems"] = [f"settlement_refused:{problem}" for problem in exc.problems]
            return out
        except OSError as exc:
            out["problems"] = [f"settlement_write_failed:{type(exc).__name__}"]
            return out
    return out


def apply_writeoffs(closes: list[Any], now: str | None = None,
                    books: dict[tuple[str, str], dict[str, Any]] | None = None, *,
                    clock_ok: bool = False) -> dict[str, Any]:
    """PR #342 review (Codex 6189524237 P1): the whole read -> plan -> book path runs under the same
    ``<PAPER_PATH>.commit.lock`` as commit_if_allowed, so no fill is written between a RiskGate check and its
    book (the daily loss it checked cannot change underneath it). A lock that cannot be taken books nothing and
    returns ``{"booked": [], "problems": [], "deferred": WRITEOFF_LOCK_UNAVAILABLE}``: the caller retries next tick
    (no halt). See ``_apply_writeoffs_locked``."""
    lock = _commit_lock()
    if not lock.acquire(timeout_s=COMMIT_LOCK_TIMEOUT_S):
        return {"booked": [], "problems": [], "deferred": WRITEOFF_LOCK_UNAVAILABLE}
    try:
        return _apply_writeoffs_locked(closes, now, books, clock_ok=clock_ok)
    finally:
        lock.release()


def _apply_writeoffs_locked(closes: list[Any], now: str | None = None,
                            books: dict[tuple[str, str], dict[str, Any]] | None = None, *,
                            clock_ok: bool = False) -> dict[str, Any]:
    """Issue #197 (Task CK, KEY-approved): book one write-off row per open leg of each confirmed closed market.

    ``closes`` are src/market_close.Close values (confirmed by src/m2_runner._writeoff_hook). Idempotent: a leg
    already written off is never written off again (the row's fill_id is the leg key, src/market_close.writeoff_fill_id),
    so a restart repeats nothing. Returns {"booked": [rows], "problems": [...]}; any problem (a
    ledger that does not verify, a contradiction, a refused or failed write) must make the caller record
    invalid_risk_state as a global halt.
    """
    from src import market_close

    out: dict[str, Any] = {"booked": [], "problems": []}
    try:
        _, ledger = ledger_store.read_ledger(PAPER_PATH)
    except OSError as exc:
        out["problems"] = [f"writeoff_ledger_unreadable:{type(exc).__name__}"]
        return out
    problems = ledger_store.verify(ledger)
    if problems or non_finite_ledger_rows(ledger.rows):
        out["problems"] = [f"writeoff_ledger_invalid:{problem}" for problem in problems] or ["writeoff_ledger_invalid"]
        return out
    now = now or utc_now()
    # #197 correction (issuecomment-6109537257): start-of-day shares at max(cost, start-of-day mark). Issue #255:
    # on the day's first tick the snapshot is written here first (the same code path as #124's daily_unrealized),
    # never an uplift of 0 for want of it; a damaged or unwritable one books nothing (fail closed).
    now_dt = parse_book_ts(now)
    pos = leg_marks.positions(ledger.rows, now[:10], list(ledger.ids))
    if now_dt is None or pos.problems:
        out["problems"] = [f"writeoff_daily_marks_invalid:{p}" for p in (pos.problems or ["invalid_now"])]
        return out
    snap, snap_problems = ensure_daily_snapshot(pos, now[:10], now, now_dt,
                                                mark_books(MARK_BOOKS_PATH) if books is None else books,
                                                clock_ok=clock_ok)
    if snap_problems or snap is None:
        snap_problems = snap_problems or ["daily_marks_missing_after_create"]
        out["problems"] = [f"writeoff_daily_marks_invalid:{problem}" for problem in snap_problems]
        return out
    planned = market_close.plan_writeoffs(ledger.rows, list(ledger.ids), list(closes), now, snap)
    if planned.problems:
        out["problems"] = planned.problems
        return out
    for row in planned.rows:
        try:
            out["booked"].append(ledger_store.book(PAPER_PATH, row))
        except LedgerIntegrityError as exc:
            out["problems"] = [f"writeoff_refused:{problem}" for problem in exc.problems]
            return out
        except OSError as exc:
            out["problems"] = [f"writeoff_write_failed:{type(exc).__name__}"]
            return out
    return out


def _unsettled_closed_legs(rows: list[dict[str, Any]]) -> list[str] | None:
    """Issue #126: open legs whose market closed but whose settlement is not confirmed (None: unreadable)."""
    from src import settlement

    snaps, problems = read_settlements()
    if problems or not snaps:
        return None if problems else []
    return settlement.plan(rows, [], snaps, utc_now()).unsettled_closed_legs


def pair_of(market: str) -> tuple[str, str] | None:
    """Issue #145: the two token ids of ``market`` from the newest scanner row (None: unknown -> fail closed)."""
    try:
        rows = read_jsonl(PAIR_MARKETS_PATH)
    except (OSError, UnicodeDecodeError):
        return None
    found = [row for row in rows if isinstance(row, dict) and str(row.get("id")) == str(market)]
    return wash_detect.binary_pair(found[-1]) if found else None


def _opposite_pending_buys(market: str, token_id: str, quotes: Any) -> int | None:
    """Task DD (#237): own open (not cancelled) buy quotes in ``market`` on a token other than ``token_id``.

    None (RiskGate refuses) when the history is not a list, or any record is not a dict or lacks a str market,
    status, side or token_id (Poly风控 on #280: a malformed record is not skipped, and nothing is str()-coerced).
    """
    if not isinstance(quotes, list):
        return None
    count = 0
    for row in quotes:
        if not isinstance(row, dict) or not wash_detect._quote_fields_are_str(row, unknown_token_ok=False):
            return None
        if row["market"] != market or row["status"] in wash_detect.CLOSED_STATUSES:
            continue
        if row["side"] == "buy" and row["token_id"] != token_id:
            count += 1
    return count


def load_state(market: str, token_id: str = "", *, clock_ok: bool = False,
               quotes_path: Path | None = None) -> dict[str, Any]:
    """Risk state for an intent on one leg (market + token_id).

    open_positions counts open legs, not markets. leg_already_open is True only
    when this exact token already has an open position; another leg of the same
    market does not count. exposure_usd is the gross sum over open legs. leg_net_shares is this
    leg's net shares (buys minus sells; issue #123: RiskGate reads its sign to tell an add from a
    reduction, and vetoes a same-direction add as add_to_open_leg; issue #226: a reduction books at most |net|,
    and a sell on a leg that is not open vetoes sell_requires_position).
    recent_fills (every ledger row) and own_quotes (every paper quote row) are the
    history RiskGate's structured wash / self-trade check reads (Task S).
    Task AF: rows come from the strict ledger reader, deduped by fill_id. Any integrity problem
    (parse, ids, Task U numbers, conservation) is listed in ledger_problems and gives the same
    invalid state as Task U, so RiskGate vetoes invalid_risk_state and nothing more is booked.
    Issue #68: a modern fill whose risk-control fields (hypothetical, counts_toward_risk,
    risk_decision) are not exactly valid is an integrity problem too, rejected before it is
    replayed into cash, positions or the daily loss.
    Issue #56: own_quotes comes from the strict quotes reader, once, for every return path. A
    quotes file that cannot be read in full is not a short history: own_quotes is None,
    quote_problems names the lines, and RiskGate vetoes the existing wash_input_invalid.
    Poly风控 on #310 (Task DO): quotes_path (default QUOTES_PATH) names the quotes file own_quotes is read from, so
    maker.quote checks the opposite-leg veto on the same file it writes to.
    """
    _, ledger = ledger_store.read_ledger(PAPER_PATH)
    rows = ledger.rows
    # PR #310 review: the maker passes the quotes file it actually checks and writes (default QUOTES_PATH).
    own_quotes, quote_problems = read_quotes(Path(quotes_path) if quotes_path is not None else QUOTES_PATH)
    if quote_problems:
        own_quotes = None  # issue #56: a partial quote history is not this history (fail closed)
    # Issue #145 (Task CH): the market's two tokens, read only when an own open quote rests on another token.
    try:
        market_pair = pair_of(market) if own_quotes is not None and wash_detect.pair_quotes(market, token_id, own_quotes) \
            else None
    except TypeError:
        # Task DD (#280) backstop: a quote history the checks cannot read is unusable -> wash_input_invalid.
        own_quotes, quote_problems, market_pair = None, list(quote_problems) + ["quote_type_error"], None
    bad_rows = non_finite_ledger_rows(rows)
    problems = ledger_store.verify(ledger)
    if bad_rows or problems:
        # Fail closed (Task U / AF): RiskGate vetoes invalid_risk_state; nothing is netted.
        return {
            "open_positions": None,
            "leg_already_open": False,
            "exposure_usd": None,
            "leg_exposure_usd": None,
            "leg_net_shares": None,
            "daily_loss_usd": None,
            "non_finite_ledger_rows": bad_rows,
            "ledger_problems": problems,
            "duplicate_fill_rows": ledger.duplicates_ignored,
            "recent_fills": rows,
            "own_quotes": own_quotes,
            "market_pair": market_pair,  # issue #145
            "quote_problems": quote_problems,
        }
    legs = leg_positions(rows)
    risk = daily_risk_loss(rows, list(ledger.ids), utc_now(), clock_ok=clock_ok)  # Task DC: fail closed by default
    day, marks = risk["day"], risk["marks"]
    if risk["problems"]:
        problems_today = risk["problems"]
        return {
            "open_positions": None,
            "leg_already_open": False,
            "exposure_usd": None,
            "leg_exposure_usd": None,
            "leg_net_shares": None,
            "daily_loss_usd": None,
            "daily_loss_problems": problems_today,
            "ledger_problems": list(problems_today),
            "daily_marks": marks["legs"],
            "duplicate_fill_rows": ledger.duplicates_ignored,
            "recent_fills": rows,
            "own_quotes": own_quotes,
            "market_pair": market_pair,  # issue #145
            "quote_problems": quote_problems,
        }
    own = legs.get(leg_key(market, token_id))
    return {
        "open_positions": sum(1 for leg in legs.values() if abs(leg["shares"]) > OPEN_EPS),
        "leg_already_open": own is not None and abs(own["shares"]) > OPEN_EPS,
        "exposure_usd": sum(leg["exposure_usd"] for leg in legs.values()),
        "leg_exposure_usd": 0.0 if own is None else own["exposure_usd"],
        "leg_net_shares": 0.0 if own is None else own["shares"],  # issue #123: the open leg's direction
        # Issue #237: long holdings on the same market's other tokens (a buy here would assemble a complete set).
        "opposite_leg_shares": sum(leg["shares"] for key, leg in legs.items()
                                   if key[0] == leg_key(market, token_id)[0] and key != leg_key(market, token_id)
                                   and leg["shares"] > OPEN_EPS),
        # Task DD (#237, Codex P1): own open buy quotes on the other tokens are pending opposite holdings
        # (None: the quote history is unreadable -> RiskGate fails closed).
        "opposite_pending_buys": _opposite_pending_buys(market, token_id, own_quotes),
        "daily_loss_usd": risk["loss_usd"],
        "daily_fees_usd": day.fees_usd,
        "daily_unrealized_loss_usd": marks["loss_usd"],  # Task CF: diagnosis (already in daily_loss_usd)
        "daily_marks": marks["legs"],
        "daily_marks_late": marks["late"],
        "unsettled_closed_legs": _unsettled_closed_legs(rows),  # issue #126: diagnosis only
        "daily_realized_loss_usd": day.realized_loss_usd,
        "daily_realized_gain_usd": day.realized_gain_usd,
        "daily_writeoff_usd": day.writeoff_usd,  # issue #197: diagnosis (already in daily_realized_loss_usd)
        "ledger_problems": [],
        "duplicate_fill_rows": ledger.duplicates_ignored,
        "recent_fills": rows,
        "own_quotes": own_quotes,
        "market_pair": market_pair,  # issue #145
        "quote_problems": quote_problems,
    }


from src.thresholds import COMMIT_LOCK_TIMEOUT_S  # noqa: E402,F401 - Task DD: the commit mutex wait (THRESHOLDS.md)


def _commit_lock() -> Any:
    """The commit mutex ``<PAPER_PATH>.commit.lock`` (Task DD), shared by commit_if_allowed and apply_writeoffs."""
    from src import halt_store

    lock = halt_store.StateLock(PAPER_PATH)
    lock.path = Path(str(PAPER_PATH) + ".commit.lock")  # not ledger_store's <ledger>.lock (taken inside book)
    return lock


WRITEOFF_LOCK_UNAVAILABLE = "writeoff_commit_lock_unavailable"


def commit_if_allowed(intent: Intent, cfg: dict[str, Any], *, unique_decision_id: bool = False) -> dict[str, Any]:
    """Task DD (#237, Codex P1): the whole load_state -> RiskGate -> book path runs under one cross-process
    mutex on ``<PAPER_PATH>.commit.lock``, so two interleaved commits (e.g. YES and NO of one market) cannot both read
    a state without the other's fill. A mutex that cannot be taken refuses the order (fail closed)."""
    lock = _commit_lock()
    if not lock.acquire(timeout_s=COMMIT_LOCK_TIMEOUT_S):
        summary = {"forecast_id": intent.forecast_id, "strategy": intent.strategy,
                   "reasons": [STRUCTURAL_ARB_DISABLED, "commit_lock_unavailable"],
                   "hypothetical": intent.hypothetical, "risk_decision": "veto", "decision": "veto",
                   "note": DIRECTIONAL}
        bus("RiskGate", "Executor", "veto", intent.market, summary)
        return {"allow": False, "decision": summary}
    try:
        return _commit_locked(intent, cfg, unique_decision_id=unique_decision_id)
    finally:
        lock.release()


def _commit_locked(intent: Intent, cfg: dict[str, Any], *, unique_decision_id: bool = False) -> dict[str, Any]:
    """Ask RiskGate. Append a paper fill only when the same call allows it.

    Task AF: the fill gets a fill_id and the post-fill cash / leg state, and is written atomically
    by ledger_store.book after the whole ledger is re-verified. Issue #120: ``unique_decision_id`` is
    passed through (default False); when True and the decision_id is already booked, returns
    ``allow True`` with the existing fill and ``already_booked True``, without writing again. An integrity problem halts (a halt
    bus message, allow False, nothing booked); a failed write leaves the old ledger intact.
    """
    # Issue #126 (Task CG): settle resolved legs first, so a settled loss is realized once and the leg is gone.
    settled = apply_settlements()
    if settled["problems"]:
        problems = [str(p) for p in settled["problems"]]
        summary = {"forecast_id": intent.forecast_id, "strategy": intent.strategy, "reasons": ["invalid_risk_state"],
                   "hypothetical": intent.hypothetical, "risk_decision": "veto", "decision": "halt",
                   "settlement_problems": problems, "note": DIRECTIONAL}
        sentinel.halt(reason="invalid_risk_state", market=intent.market, scope="global",
                      quotes_path=QUOTES_PATH, emit=_emit,
                      extra={"ledger_problems": problems, "strategy": intent.strategy, "token_id": intent.token_id})
        return {"allow": False, "decision": summary, "halted": True, "halt_reason": "invalid_risk_state",
                "ledger_problems": problems}
    state = load_state(intent.market, intent.token_id)
    decision = evaluate(intent, cfg, state)
    summary = _decision_payload(intent, decision)
    # Task CF (#124): this evaluation's leg marks go on the RiskGate bus line (review only, not a risk input).
    summary["daily_unrealized_loss_usd"] = state.get("daily_unrealized_loss_usd")
    summary["daily_marks"] = state.get("daily_marks") or []
    if not decision.allow:
        bus("RiskGate", "Executor", "veto", intent.market, summary)
        if "invalid_risk_state" in decision.reasons:
            # Task AL gap 7: an unusable ledger state is no longer a one-off veto but a global halt.
            problems = [str(p) for p in state.get("ledger_problems") or []]
            if state.get("non_finite_ledger_rows"):
                problems.append(f"non_finite_ledger_rows={state.get('non_finite_ledger_rows')}")
            sentinel.halt(reason="invalid_risk_state", market=intent.market, scope="global",
                          quotes_path=QUOTES_PATH, emit=_emit,
                          extra={"ledger_problems": problems, "strategy": intent.strategy,
                                 "token_id": intent.token_id})
            return {"allow": False, "decision": summary, "halted": True, "halt_reason": "invalid_risk_state",
                    "ledger_problems": problems}
        veto: dict[str, Any] = {"allow": False, "decision": summary}
        # Issue #56: a quotes file that could not be read in full shows up as wash_input_invalid.
        # Name its per-line problems on the veto too (diagnosis only; nothing is booked either way).
        if state.get("quote_problems"):
            veto["quote_problems"] = list(state["quote_problems"])
        return veto

    summary["decision"] = "allow"
    bus("RiskGate", "Executor", "signal", intent.market, summary)
    # Re-check the in-memory decision. A bus line is not enough on its own.
    if decision.allow is not True:
        raise RuntimeError("refusing fill without a fresh RiskGate allow")
    effective_price = round(decision.effective_price, 8)
    notional_usd = round(decision.notional_usd, 8)
    shares = round(decision.shares, 8)
    # Task AF: the booked fee is also never below ledger_fee recomputed from the values the row
    # stores (they are rounded), so the conservation check can recompute it exactly. Higher wins.
    stored_fee = ledger_fee(intent.fee_rate, intent.market_price, effective_price, notional_usd, shares)
    fee_usd = booked_fee_usd(decision.fee_usd)
    if stored_fee is not None:
        fee_usd = max(fee_usd, booked_fee_usd(stored_fee))
    fill = {
        "fill_id": ledger_store.new_fill_id(),
        "ts": utc_now(),
        "strategy": intent.strategy,
        "market": intent.market,
        "token_id": intent.token_id,
        "side": intent.side,
        "model_prob": intent.model_prob,
        "market_price": intent.market_price,
        "edge": round(decision.edge, 8),
        "edge_formula": decision.formula,
        "assumed_slippage": intent.assumed_slippage,
        "fee_rate": intent.fee_rate,
        "fee_usd": fee_usd,
        "effective_price": effective_price,
        "notional_usd": notional_usd,
        "shares": shares,
        "spread": intent.spread,
        "liquidity_usd": intent.liquidity_usd,
        "book_ts": intent.book_ts,
        "hypothetical": intent.hypothetical,
        "counts_toward_risk": not intent.hypothetical,
        "risk_decision": "allow",
        "note": f"{DIRECTIONAL} {intent.note}".strip(),
    }
    # Task AX (M3 step 2): identity fields, then the join check. A fill that cannot be joined is still
    # booked (RiskGate already decided; nothing here changes that) but says why in calibration_problems.
    fill.update(identity_fields(intent))
    # Task BA: market context at commit (mid, top of book, hours to expiry, shadow Kelly multiplier).
    # A value that cannot be computed stays None and is named in calibration_problems.
    market, market_problems = market_fields(intent, fill["ts"])
    fill.update(market)
    # Task BC: which path built the intent (intent_builder / operator_cli / gate_check), so the calibration
    # report can tell a fill without book context from one with genuinely missing data.
    context, context_problems = fill_context(intent)
    fill["fill_context"] = context
    # Task BF: the market's Polymarket event, for calibration clustering. None when unknown or unusable;
    # never a calibration problem (the report falls back to condition_id).
    fill.update(event_fields(intent))
    # Issue #119 (Task CE): the M2 runner's fee provenance (src/fee_source), recorded only. Older intents carry
    # none and their fills keep their old shape.
    if getattr(intent, "fee_source", None) is not None:
        fill["fee_source"] = intent.fee_source
        fill["fee_gamma_fetched_at"] = intent.fee_gamma_fetched_at
        fill["fee_schedule_sha"] = intent.fee_schedule_sha
    # Issue #131: the category buckets RiskGate classified the market into (for M3 calibration grouping).
    fill["category_buckets"] = category.bucket_label(intent.tags)
    fill["calibration_problems"] = sorted(set(calibration_ids.fill_problems(fill)) | set(market_problems)
                                          | set(context_problems))
    try:
        booked = ledger_store.book(PAPER_PATH, fill, unique_decision_id=unique_decision_id)
    except LedgerIntegrityError as exc:
        return _halt(intent, summary, "ledger_integrity", exc.problems)
    except OSError as exc:
        return _halt(intent, summary, "ledger_write_failed", [f"{type(exc).__name__}: {exc}"])
    if booked.get("already_booked") is True:  # issue #120: same decision_id already on the ledger
        existing = booked["fill"]
        return {"allow": True, "fill": existing, "already_booked": True}
    fill = booked
    bus("Executor", "Sentinel", "fill", intent.market, fill)
    return {"allow": True, "fill": fill}


def _halt(intent: Intent, summary: dict[str, Any], reason: str, problems: list[str]) -> dict[str, Any]:
    """Task AF: no fill. Report a halt (paper only, no exchange action) and return allow False.

    Task AL gap 7: the halt is also persisted as a global halt (Sentinel's halt path, written first)."""
    sentinel.halt(reason=reason, market=intent.market, scope="global", source="Executor", to="Sentinel",
                  quotes_path=QUOTES_PATH, emit=_emit, extra={"ledger_problems": problems, "strategy": intent.strategy, "token_id": intent.token_id})
    decision = dict(summary, decision="halt")
    return {"allow": False, "halted": True, "halt_reason": reason, "ledger_problems": problems,
            "decision": decision}


def _emit(*args: Any, **kwargs: Any) -> Any:
    """This module's bus writer, looked up at call time (callers and tests may replace ``bus``)."""
    return bus(*args, **kwargs)


def identity_fields(intent: Intent) -> dict[str, Any]:
    """Task AX: the calibration identity of a fill (calibration_ids.FILL_ID_FIELDS), from the intent.

    decision_id is the intent's own when given (it must then match the derivation, or the fill is
    flagged decision_id_mismatch); otherwise derived from (strategy, market, token_id, forecast_id),
    or None when any part is missing. Nothing is invented.
    """
    decision_id = intent.decision_id or calibration_ids.make_decision_id(
        intent.strategy, intent.market, intent.token_id, intent.forecast_id)
    return {
        "decision_id": decision_id,
        "forecast_id": intent.forecast_id,
        "forecast_ts": intent.forecast_ts,
        "condition_id": intent.condition_id,
        "outcome_index": intent.outcome_index,
        "outcome": intent.outcome,
        "category": intent.category,
        "tags": None if intent.tags is None else list(intent.tags),
        "end_date": intent.end_date,
        "model_version": intent.model_version,
    }


def event_fields(intent: Intent) -> dict[str, Any]:
    """Task BF: calibration_ids.FILL_EVENT_FIELDS from the intent, each calibration_ids.event_value
    (a non-blank string, or None). Never raises, never a problem, never read by RiskGate."""
    return {"event_id": calibration_ids.event_value(getattr(intent, "event_id", None)),
            "event_slug": calibration_ids.event_value(getattr(intent, "event_slug", None))}


def _price(value: object) -> tuple[float | None, str | None]:
    """A top-of-book price: a finite number strictly between 0 and 1 (bool and str refused)."""
    if value is None:
        return None, "missing"
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value < 1:
        return None, "invalid"
    return float(value), None


def market_fields(intent: Intent, fill_ts: str) -> tuple[dict[str, Any], list[str]]:
    """Task BA: calibration_ids.FILL_MARKET_FIELDS for a fill committed at ``fill_ts``, and their problems.

    best_bid / best_ask: the intent's own top of book (the data RiskGate's caller already had), each a
    finite price in (0, 1), else None (missing_ / invalid_<field>). market_mid = (bid + ask) / 2 when
    both are usable and not crossed (bid <= ask); else None (missing_market_mid if a side is unusable,
    invalid_market_mid if crossed). hours_to_expiry_at_entry = end_date - fill ts in hours (negative
    when already past, as computed), None if end_date is missing or not a zoned ISO time.
    kelly_fraction_used: intent_builder's shadow Kelly multiplier as given, a finite number in (0, 1],
    else None. Task BC: kelly_frac_raw, intent_builder's per-trade raw (full) Kelly fraction, same rule.
    Recorded only: nothing reads these for a decision or a size. Never invented.
    """
    problems: list[str] = []
    bid, bid_problem = _price(intent.best_bid)
    ask, ask_problem = _price(intent.best_ask)
    for name, problem in (("best_bid", bid_problem), ("best_ask", ask_problem)):
        if problem:
            problems.append(f"{problem}_{name}")
    mid = None
    if bid is None or ask is None:
        problems.append("missing_market_mid")
    elif bid > ask:
        problems.append("invalid_market_mid")
    else:
        mid = round((bid + ask) / 2, 8)
    hours = None
    if intent.end_date is None:
        problems.append("missing_hours_to_expiry_at_entry")
    else:
        now = parse_end_date(fill_ts)
        hours = hours_to_expiry(intent.end_date, now) if now is not None else None
        if hours is None or not math.isfinite(hours):
            hours = None
            problems.append("invalid_hours_to_expiry_at_entry")
        else:
            hours = round(hours, 6)
    kelly, kelly_problem = _fraction(intent.kelly_fraction_used)
    if kelly_problem:
        problems.append(f"{kelly_problem}_kelly_fraction_used")
    kelly_raw, raw_problem = _fraction(intent.kelly_frac_raw)
    if raw_problem:
        problems.append(f"{raw_problem}_kelly_frac_raw")
    return ({"market_mid": mid, "best_bid": bid, "best_ask": ask, "hours_to_expiry_at_entry": hours,
             "kelly_fraction_used": kelly, "kelly_frac_raw": kelly_raw}, problems)


def _fraction(value: object) -> tuple[float | None, str | None]:
    """A Kelly fraction: a finite number in (0, 1] (bool and str refused); else None and missing / invalid."""
    if value is None:
        return None, "missing"
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= 1:
        return None, "invalid"
    return float(value), None


def fill_context(intent: Intent) -> tuple[str | None, list[str]]:
    """Task BC: the fill's fill_context (calibration_ids.FILL_CONTEXTS) as the caller stamped it on the intent.

    Never inferred from missing fields: None -> missing_fill_context, an unknown value -> None +
    invalid_fill_context. Recorded only; RiskGate never reads it.
    """
    value = intent.fill_context
    if value is None:
        return None, ["missing_fill_context"]
    if value not in calibration_ids.FILL_CONTEXTS:
        return None, ["invalid_fill_context"]
    return value, []


def _decision_payload(intent: Intent, decision: Decision) -> dict[str, Any]:
    return {
        "forecast_id": intent.forecast_id,  # Task AX: joins a veto or allow back to its sealed forecast
        "strategy": intent.strategy,
        "model_prob": intent.model_prob,
        "market_price": intent.market_price,
        "edge": round(decision.edge, 8),
        "edge_formula": decision.formula,
        "assumed_slippage": decision.assumed_slippage,
        "fee_rate": decision.fee_rate,
        "fee_usd": booked_fee_usd(decision.fee_usd),
        "reasons": decision.reasons,
        "hypothetical": intent.hypothetical,
        "risk_decision": "allow" if decision.allow else "veto",
        "note": DIRECTIONAL,
    }


def _intent(**kwargs: Any) -> Intent:
    defaults: dict[str, Any] = {
        "strategy": "phase0-gate-test",
        "market": "phase0-self-check",
        "side": "buy",
        "model_prob": 0.5,
        "market_price": 0.5,
        "assumed_slippage": ASSUMED_SLIPPAGE,
        "fee_rate": 0.0,
        "liquidity_usd": 20000.0,
        "spread": 0.01,
        "token_id": "",
        "hypothetical": True,
        "note": "Gate test only. Not a forecast and not a claim of edge.",
        "fill_context": calibration_ids.FILL_CONTEXT_GATE_CHECK,  # Task BC: no book context
        # Issue #131: the synthetic gate-test market's tag, so only the check under test can veto.
        "tags": ("phase0-gate-test",),
        # Synthetic gate-test touch, stamped now so only the check under test can veto.
        "book_ts": utc_now(),
        # Issue #226: the synthetic gate-test book's minimum order size (RiskGate requires one).
        "min_order_size": 5.0,
    }
    defaults.update(kwargs)
    return Intent(**defaults)


def run_self_check(cfg: dict[str, Any]) -> dict[str, Any]:
    """Prove vetoes fire, and that one labeled hypothetical fill needs allow.

    The hypothetical allow is checked against the real fills' open positions and daily
    loss like any other intent (Task M), so a full book vetoes it.
    """
    before = len(read_jsonl(PAPER_PATH))
    drifted = dict(cfg)
    drifted["live_enabled"] = True
    pin_result = commit_if_allowed(
        _intent(market="phase0-self-check:pin", note="In-memory pin drift. File not modified."),
        drifted,
    )
    no_edge = commit_if_allowed(
        _intent(
            market="phase0-self-check:no-edge",
            model_prob=0.50,
            market_price=0.50,
            note="Model probability equals the touch. Costs make edge non-positive.",
        ),
        cfg,
    )
    wide = commit_if_allowed(
        _intent(
            market="phase0-self-check:spread",
            model_prob=0.80,
            market_price=0.40,
            spread=0.20,
            note="Spread wider than max_spread.",
        ),
        cfg,
    )
    banned = commit_if_allowed(
        _intent(
            strategy="wash-probe",
            market="phase0-self-check:banned",
            model_prob=0.80,
            market_price=0.40,
            hypothetical=False,
            note="Banned behavior must not fill.",
        ),
        cfg,
    )
    allowed = commit_if_allowed(
        _intent(
            market="phase0-self-check:hypothetical-allow",
            model_prob=0.70,
            market_price=0.40,
            note="Hypothetical probability to prove the allow path. Not a forecast.",
        ),
        cfg,
    )
    after = len(read_jsonl(PAPER_PATH))
    return {
        "pin_blocked": not pin_result["allow"],
        "no_edge_blocked": not no_edge["allow"],
        "wide_spread_blocked": not wide["allow"],
        "banned_blocked": not banned["allow"],
        "hypothetical_allowed": allowed["allow"],
        "new_fills": after - before,
        "allowed": allowed,
    }


def realistic_no_edge(cfg: dict[str, Any]) -> dict[str, Any] | None:
    """Use a public book price with model_prob == price. Must veto. Not a forecast.

    Task T: candidates are the latest row per leg by the recorder's parsed ``ts`` (times, not
    strings), newest first; rows with a missing or unparseable ``ts`` are skipped and returned in
    ``skipped_rows``. Book age is the exchange's ``book_ts`` on the row, never ``ts``; a row
    without one reaches RiskGate as book_ts=None and vetoes missing_book_ts (fail closed).
    """
    books = read_jsonl(BOOKS_PATH)
    markets = {str(row.get("id")): row for row in read_jsonl(MARKETS_PATH)}
    latest, skipped = latest_by_time(
        books, lambda row: (str(row.get("market_id") or ""), str(row.get("token_id") or ""))
    )
    ordered = sorted(
        latest.values(), key=lambda row: parse_book_ts(row.get("ts")), reverse=True  # type: ignore[arg-type,return-value]
    )
    fallback = None
    chosen = None
    for book in ordered:
        ask = book.get("best_ask")
        spread = book.get("spread")
        if not isinstance(ask, (int, float)) or not isinstance(spread, (int, float)):
            continue
        if not 0.0 < float(ask) < 1.0:
            continue
        market = markets.get(str(book.get("market_id")))
        liquidity = None if market is None else market.get("liquidity_usd")
        if not isinstance(liquidity, (int, float)):
            continue
        candidate = (book, float(liquidity))
        if fallback is None:
            fallback = candidate
        # Prefer a touch that stays inside (0, 1) after the default slippage.
        if 0.0 < float(ask) + ASSUMED_SLIPPAGE < 1.0:
            chosen = candidate
            break
    if chosen is None:
        chosen = fallback
    if chosen is None:
        return None
    book, liquidity = chosen
    result = commit_if_allowed(
        Intent(
            strategy="no-edge-check",
            market=str(book.get("market_id") or ""),
            side="buy",
            model_prob=float(book["best_ask"]),
            market_price=float(book["best_ask"]),
            assumed_slippage=ASSUMED_SLIPPAGE,
            fee_rate=0.0,
            liquidity_usd=float(liquidity),
            spread=float(book["spread"]),
            token_id=str(book.get("token_id") or ""),
            hypothetical=False,
            # The exchange's own book time (Task R/T), never the recorder's ts.
            book_ts=book.get("book_ts"),
            # Task BA: the same book row's top of book (recorded on the fill only; RiskGate never reads it).
            best_bid=book.get("best_bid"),
            best_ask=book.get("best_ask"),
            fill_context=calibration_ids.FILL_CONTEXT_GATE_CHECK,  # Task BC: a gate check, never a sample
            min_order_size=book.get("min_order_size"),  # issue #226: the recorded book's; missing vetoes
            note="Public ask used as both model and price. Must not fill. Not a forecast.",
        ),
        cfg,
    )
    result["skipped_rows"] = skipped
    return result


def run_cli_intent(args: argparse.Namespace, cfg: dict[str, Any]) -> dict[str, Any]:
    """Issue #210 / #147 (Task DZ, Poly风控 6178561328 / Codex P1 6178323003): the operator CLI carries no
    ask levels or depth evidence, so it always refuses with missing_book_levels before RiskGate (tighten-only).
    No args.asks escape hatch: True, strings, zero depth or NaN must never reach commit. The early veto is
    written to the bus like any RiskGate veto. Nothing is booked."""
    reasons: list[str] = []
    if getattr(args, "fee_rate", None) is None:
        reasons.append("missing_fee_data")
    reasons.append("missing_book_levels")
    summary = {
        "strategy": getattr(args, "strategy", None) or "",
        "reasons": reasons,
        "hypothetical": False,
        "risk_decision": "veto",
        "decision": "veto",
        "note": "Operator CLI refused: fee-rate and book depth/levels required (#210).",
    }
    bus("RiskGate", "Executor", "veto", str(getattr(args, "market", None) or ""), summary)
    return {"allow": False, "decision": summary}


def slippage_arg(text: str) -> float:
    """Task AU: --slippage must be a finite number >= ASSUMED_SLIPPAGE (the floor RiskGate also enforces)."""
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
    if not math.isfinite(value):
        raise argparse.ArgumentTypeError(f"must be finite: {text!r}")
    if value < ASSUMED_SLIPPAGE:
        raise argparse.ArgumentTypeError(f"{value} is below the slippage floor ASSUMED_SLIPPAGE={ASSUMED_SLIPPAGE}")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Phase 0 paper ledger. RiskGate required. No live orders.")
    parser.add_argument("--strategy", default="")
    parser.add_argument("--market", default="")
    parser.add_argument("--side", choices=("buy", "sell"), default="buy")
    parser.add_argument("--model-prob", type=float, default=None)
    parser.add_argument("--market-price", type=float, default=None)
    parser.add_argument("--slippage", type=slippage_arg, default=ASSUMED_SLIPPAGE,
                        help=f"Assumed slippage, price units per share; at least ASSUMED_SLIPPAGE={ASSUMED_SLIPPAGE}.")
    parser.add_argument("--fee-rate", type=float, default=None,
                        help="Fee rate [0,1]. Required for operator intents (#210); no silent 0.0. Explicit 0 is allowed for free markets when book levels are also supplied.")
    parser.add_argument("--liquidity", type=float, default=None)
    parser.add_argument("--spread", type=float, default=None)
    parser.add_argument("--token-id", default="")
    parser.add_argument(
        "--book-ts",
        default=None,
        help="UTC ISO time of the book snapshot, e.g. 2026-10-09T01:00:00Z. Missing or older than 120 s vetoes.",
    )
    parser.add_argument("--tags", default=None,
                        help="comma-separated Gamma tag slugs of the market (issue #131: none vetoes missing_category)")
    parser.add_argument("--min-order-size", type=float, default=None,
                        help="the market book's min_order_size in shares (issue #226: none vetoes missing_min_order_size)")
    parser.add_argument("--note", default="")
    parser.add_argument(
        "--self-check",
        action="store_true",
        help="Run gate tests. This is the default when no operator intent is given.",
    )
    return parser


EXIT_OK = 0
# Issue #63: also the refusal when config/risk.toml is off its file pins at start (global halt recorded).
EXIT_FAILED = 1  # halted (ledger integrity / write), risk file pin drift, risk.toml changed during the run, or self-check failed
EXIT_USAGE = 2


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return EXIT_OK if exc.code == 0 else EXIT_USAGE
    from src.pins import refuse_start  # issue #63: the risk file pins (sha256 + every key) before any read

    if refuse_start("paper_ledger"):
        return EXIT_FAILED
    before_bytes = RISK_PATH.read_bytes()
    cfg = load_risk()
    live = cfg.get("live_enabled")
    print(
        f"paper_equity_usd={PAPER_EQUITY_USD} mode={cfg.get('mode')} "
        f"live_enabled={str(live).lower()} require_human_gate={str(cfg.get('require_human_gate')).lower()} "
        f"max_order_pct={cfg.get('max_order_pct')} max_daily_loss_pct={cfg.get('max_daily_loss_pct')} "
        "orders=none"
    )
    operator_intent = args.model_prob is not None or args.market_price is not None or bool(args.strategy)
    if operator_intent:
        missing = [
            name
            for name, value in (
                ("strategy", args.strategy),
                ("market", args.market),
                ("model_prob", args.model_prob),
                ("market_price", args.market_price),
                ("liquidity", args.liquidity),
                ("spread", args.spread),
            )
            if value is None or value == ""
        ]
        if missing:
            print("missing operator fields: " + ", ".join(missing), file=sys.stderr)
            return EXIT_USAGE
        result = run_cli_intent(args, cfg)
        label = "ALLOW" if result["allow"] else "HALT" if result.get("halted") else "VETO"
        print(label, result.get("fill") or result.get("decision"))
        if result.get("halted"):
            print(f"paper ledger halted: {result['halt_reason']} {result['ledger_problems']}", file=sys.stderr)
            return EXIT_FAILED
        if RISK_PATH.read_bytes() != before_bytes:
            print("risk.toml changed during paper run; refusing", file=sys.stderr)
            return EXIT_FAILED
        return EXIT_OK if result["allow"] or result.get("decision", {}).get("reasons") else EXIT_FAILED

    report = run_self_check(cfg)
    realistic = realistic_no_edge(cfg)
    print(
        "self_check "
        f"pin_blocked={report['pin_blocked']} "
        f"no_edge_blocked={report['no_edge_blocked']} "
        f"wide_spread_blocked={report['wide_spread_blocked']} "
        f"banned_blocked={report['banned_blocked']} "
        f"hypothetical_allowed={report['hypothetical_allowed']} "
        f"new_fills={report['new_fills']}"
    )
    if report["hypothetical_allowed"]:
        fill = report["allowed"]["fill"]
        print(
            "hypothetical_fill "
            f"strategy={fill['strategy']} model_prob={fill['model_prob']} "
            f"market_price={fill['market_price']} edge={fill['edge']} "
            f"assumed_slippage={fill['assumed_slippage']} fee_usd={fill['fee_usd']}"
        )
    if realistic is None:
        print("realistic_no_edge=skipped no complete public book yet")
    else:
        reasons = realistic.get("decision", {}).get("reasons")
        print(f"realistic_no_edge_blocked={not realistic['allow']} reasons={reasons}")
    unchanged = RISK_PATH.read_bytes() == before_bytes
    print(f"risk_toml_unchanged={str(unchanged).lower()} path={RISK_PATH}")
    ok = (
        report["pin_blocked"]
        and report["no_edge_blocked"]
        and report["wide_spread_blocked"]
        and report["banned_blocked"]
        and report["hypothetical_allowed"]
        and report["new_fills"] == 1
        and unchanged
        and (realistic is None or realistic["allow"] is False)
    )
    if not ok:
        print("paper ledger self-check failed", file=sys.stderr)
        return EXIT_FAILED
    print(DIRECTIONAL)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
