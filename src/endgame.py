"""Endgame signal for a near-resolution price band.

A price between 0.98 and 0.99 is not locked profit.
This module never emits a fill or an order.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.podlib import bus

# Task AC: values in src/thresholds.py; the old names stay for callers and tests.
from src.thresholds import ENDGAME_PRICE_MAX as PRICE_MAX  # noqa: E402
from src.thresholds import ENDGAME_PRICE_MIN as PRICE_MIN  # noqa: E402
from src.thresholds import ENDGAME_WINDOW_HOURS as WINDOW_HOURS  # noqa: E402


def hours_until(end_date: str, now: str) -> float | None:
    ending = _parse(end_date)
    current = _parse(now)
    if ending is None or current is None:
        return None
    return (ending - current).total_seconds() / 3600.0


def emit(
    price: float,
    end_date: str,
    market: str,
    *,
    now: str,
    bus_path: Path | None = None,
    outcome_index: int | None = None,
    outcome: str | None = None,
    token_id: str | None = None,
) -> dict | None:
    """Signal to RiskGate only. With outcome_index (Task AD) the payload names the outcome
    and its token; a selection without a token id emits nothing (fail closed)."""
    if not PRICE_MIN <= float(price) <= PRICE_MAX:
        return None
    remaining = hours_until(end_date, now)
    if remaining is None or remaining < 0 or remaining > WINDOW_HOURS:
        return None
    payload = {
        "price": float(price),
        "end_date": end_date,
        "hours_to_end": round(remaining, 6),
        "band_low": PRICE_MIN,
        "band_high": PRICE_MAX,
        "locked_profit": False,
        "one_leg_meaning": "directional",
        "exchange_action": "none",
    }
    if outcome_index is not None:
        if isinstance(outcome_index, bool) or not isinstance(outcome_index, int) or outcome_index < 0:
            return None
        if not isinstance(token_id, str) or not token_id:
            return None
        payload["outcome_index"] = outcome_index
        payload["outcome"] = outcome
        payload["token_id"] = token_id
    return bus("Endgame", "RiskGate", "signal", market, payload, path=bus_path)


def _parse(value: str) -> datetime | None:
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1]
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None
