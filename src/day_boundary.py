"""Task CX (#225): the one UTC day-boundary rule for start-of-day references.

Poly指挥/Poly风控 rulings on issue #225 (D1, D2):

* the start-of-day reference of a UTC day is on time only when it is taken within
  ``DAY_START_MAX_LAG_S`` (= ``MAX_BOOK_AGE_S``) of that day's UTC 00:00; the lag is ``now - 00:00``
  of ``now``'s own UTC day, never "did this process evaluate yesterday";
* exactly the threshold is on time; more is late;
* a clock anomaly (``clock_ok=False``) or a timestamp that is missing or cannot be parsed is late.

Used by ``paper_ledger.daily_unrealized`` (the start-of-day snapshot's ``late`` flag); the #197/#255
write-off path must reuse ``day_start_late`` instead of writing its own boundary rule.
"""
from __future__ import annotations

from datetime import datetime, timezone

from src.book_staleness import parse_book_ts
from src.thresholds import DAY_START_MAX_LAG_S


def day_start_lag_s(now: object) -> float | None:
    """Seconds since 00:00 UTC of ``now``'s UTC day; None when ``now`` is not a usable timestamp."""
    if isinstance(now, datetime):
        stamp = now if now.tzinfo is not None else None
    elif isinstance(now, str) and now:
        try:
            stamp = parse_book_ts(now)
        except Exception:  # noqa: BLE001 - an unparseable stamp is a clock anomaly
            stamp = None
    else:
        stamp = None
    if stamp is None:
        return None
    try:
        stamp = stamp.astimezone(timezone.utc)
    except (OverflowError, ValueError):  # PR #342 (Codex 6193266276): out of range in UTC: fail closed
        return None
    midnight = stamp.replace(hour=0, minute=0, second=0, microsecond=0)
    lag = (stamp - midnight).total_seconds()
    return lag if lag >= 0 else None


def day_start_late(now: object, *, clock_ok: bool = True, max_lag_s: float = DAY_START_MAX_LAG_S) -> bool:
    """True when a start-of-day reference taken at ``now`` is late (fail closed on any doubt)."""
    if clock_ok is not True:
        return True
    lag = day_start_lag_s(now)
    return lag is None or lag > max_lag_s
