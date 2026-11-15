"""Scanner minimum time-to-expiry rule (Task O, risk-gap backlog item 8). Pure and offline.

A market passes only when its end date is an ISO 8601 string with a date, a time and an
explicit zone (``Z`` or an offset), and it is at least MIN_HOURS_TO_EXPIRY hours after
``now``. Anything else fails closed: missing, unparseable, naive (no zone), date-only,
non-string (numbers, bools, None), already past, or too close.

MIN_HOURS_TO_EXPIRY is a code constant (src/thresholds.py since Task AC). It is not read from config/risk.toml; moving it
there is a proposal for the Operator. No network, no keys, no orders.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone

# 24 h: a market resolving within a day has little time left to re-check its book, and
# resolution / oracle timing risk dominates. Conservative; the Operator may tune it.
from src.thresholds import MIN_HOURS_TO_EXPIRY  # noqa: E402 - Task AC: value in src/thresholds.py

MISSING_END_DATE = "missing_end_date"
INVALID_END_DATE = "invalid_end_date"
EXPIRED = "expired"
NEAR_EXPIRY = "near_expiry"
INVALID_MIN_HOURS = "invalid_min_hours_to_expiry"
INVALID_NOW = "invalid_now"

# Date, then T or space, then at least HH:MM. Date-only strings are rejected, not guessed.
_HAS_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}")


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _aware(value: object) -> bool:
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None


def parse_end_date(value: object) -> datetime | None:
    """Return the end date as an aware UTC datetime, or None when it is not a zoned ISO string.

    A zoned timestamp whose UTC conversion leaves datetime's range (``0001-01-01T00:00:00+01:00``,
    ``9999-12-31T23:59:59-01:00``) is None too: unparseable, never an OverflowError for the caller."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not _HAS_TIME.match(text):
        return None
    if text[-1] in "Zz":
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
        if not _aware(parsed):
            return None
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):  # PR #342 (Codex 6185588835 P2): an out-of-range offset date is invalid
        return None


def hours_to_expiry(end_date: object, now: datetime) -> float | None:
    end = parse_end_date(end_date)
    if end is None or not _aware(now):
        return None
    return (end - now).total_seconds() / 3600.0


def expiry_problem(
    end_date: object,
    now: object = None,
    min_hours: object = MIN_HOURS_TO_EXPIRY,
) -> str | None:
    """Return None when the market may pass, else the reason it fails closed.

    Passes when ``end_date - now >= min_hours`` (the boundary passes). ``now`` defaults to
    the clock and must be an aware datetime; a bad ``now`` or ``min_hours`` is a reason too.
    """
    hours = _finite_number(min_hours)
    if hours is None or hours < 0:
        return INVALID_MIN_HOURS
    if now is None:
        now = datetime.now(timezone.utc)
    if not _aware(now):
        return INVALID_NOW
    if end_date is None or (isinstance(end_date, str) and not end_date.strip()):
        return MISSING_END_DATE
    remaining = hours_to_expiry(end_date, now)  # type: ignore[arg-type]
    if remaining is None:
        return INVALID_END_DATE
    if remaining <= 0:
        return EXPIRED
    if remaining < hours:
        return NEAR_EXPIRY
    return None
