"""Order book staleness check for Phase 0 (Task L).

Pure and offline: no network, no clock reads unless the caller omits ``now``,
no file access, no orders. RiskGate.evaluate uses it fail-closed, so a missing,
unparseable, future or old book timestamp can only veto.

MAX_BOOK_AGE_S is a code constant (Sentinel's 120 s proposal, COORDINATION risk
backlog items 2 and 9). It is deliberately not read from config/risk.toml;
moving it there is a proposal for the Operator.
"""
from __future__ import annotations

import math
from collections.abc import Callable, Hashable
from datetime import datetime, timezone

from src.thresholds import MAX_BOOK_AGE_S  # Task AC: value in src/thresholds.py

MISSING_BOOK_TS = "missing_book_ts"
INVALID_BOOK_TS = "invalid_book_ts"
FUTURE_BOOK_TS = "future_book_ts"
STALE_BOOK = "stale_book"
INVALID_MAX_BOOK_AGE = "invalid_max_book_age"
INVALID_NOW = "invalid_now"


def _finite_number(value: object) -> float | None:
    """Accept only a finite int or float. Bools, strings, nan and inf are rejected."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return number


def parse_book_ts(value: object) -> datetime | None:
    """Return an aware UTC datetime, or None when the value is not a usable timestamp.

    Accepted: an ISO 8601 string with a date, a time and a zone (``Z`` or an offset),
    an aware datetime, or finite epoch seconds as int/float. Naive times, date-only
    strings, numeric strings, bools, nan and inf are rejected rather than guessed.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            return None
        return _utc(value)
    if isinstance(value, (int, float)):
        number = _finite_number(value)
        if number is None:
            return None
        try:
            return datetime.fromtimestamp(number, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if "T" not in text:
            return None
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return _utc(parsed)
    return None


def _utc(value: datetime) -> datetime | None:
    """PR #342 review (Codex 6193266276 P2): an aware time whose UTC reading is out of range (0001-01-01T00:00:00+01:00,
    9999-12-31T23:59:59-01:00) is not a usable timestamp: None (fail closed), never an OverflowError."""
    try:
        return value.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        return None


def book_age_problem(
    book_ts: object,
    now: object = None,
    max_age_s: object = MAX_BOOK_AGE_S,
) -> str | None:
    """Return None when the book is fresh, else a veto reason.

    Fresh means ``0 <= now - book_ts <= max_age_s`` (the boundary itself is fresh,
    matching paper_cycle's ``age > stale_after_s``). A bad ``max_age_s`` or ``now``
    also returns a reason, so a broken caller can never make a book look fresh.
    """
    limit = _finite_number(max_age_s)
    if limit is None or limit <= 0:
        return INVALID_MAX_BOOK_AGE
    if now is None:
        current = datetime.now(timezone.utc)
    else:
        # Only an aware datetime is accepted for now; anything else is a broken caller.
        current = parse_book_ts(now) if isinstance(now, datetime) else None
        if current is None:
            return INVALID_NOW
    if book_ts is None or (isinstance(book_ts, str) and not book_ts.strip()):
        return MISSING_BOOK_TS
    stamp = parse_book_ts(book_ts)
    if stamp is None:
        return INVALID_BOOK_TS
    age = (current - stamp).total_seconds()
    if age < 0:
        return FUTURE_BOOK_TS
    if age > limit:
        return STALE_BOOK
    return None


MISSING_ROW_TS = "missing_row_ts"
INVALID_ROW_TS = "invalid_row_ts"


def latest_by_time(
    rows: list[dict],
    key: Callable[[dict], Hashable | None],
    field: str = "ts",
) -> tuple[dict, list[dict]]:
    """Pick the latest row per key by parsed time (Task T), never by string comparison.

    ``key(row)`` returning None (or "") ignores the row. A row whose ``field`` is missing or
    blank is skipped as missing_row_ts; one that parse_book_ts rejects (naive, date-only,
    numeric text, bool, ...) is skipped as invalid_row_ts. Skips come back as
    ``{"index", "key", "reason"}`` so the caller can record them. Equal times keep the later row.
    """
    chosen: dict = {}
    times: dict = {}
    skipped: list[dict] = []
    for index, row in enumerate(rows):
        name = key(row)
        if name is None or name == "":
            continue
        value = row.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            reason = MISSING_ROW_TS
            stamp = None
        else:
            stamp = parse_book_ts(value)
            reason = INVALID_ROW_TS
        if stamp is None:
            skipped.append({"index": index, "key": list(name) if isinstance(name, tuple) else name, "reason": reason})
            continue
        if name not in times or stamp >= times[name]:
            times[name] = stamp
            chosen[name] = row
    return chosen, skipped
