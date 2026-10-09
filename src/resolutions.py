"""Read-only Gamma settlement snapshot for M3 calibration (Task AN). No keys. No orders.

Walks public Gamma /markets/keyset?closed=true. A market is written to
data/resolutions.jsonl only after it has been closed RESOLUTION_HOLD_HOURS (48 h)
and every required field is present. Missing or unusable fields skip that row
(fail closed). Incomplete paging or paper-pin drift writes nothing. No bus
message, no fill, no order, no risk.toml edit.
"""
from __future__ import annotations

import math
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.expiry_rule import parse_end_date  # noqa: E402
from src.podlib import (  # noqa: E402
    RESOLUTIONS_PATH,
    append_jsonl,
    as_float,
    json_list,
    load_risk,
    pin_problems,
    read_jsonl,
    utc_now,
)
from src.scanner import (  # noqa: E402
    EXIT_INCOMPLETE,
    EXIT_OK,
    MAX_MARKETS,
    fetch_all_markets,
    select_token_ids,
)
from src.thresholds import RESOLUTION_HOLD_HOURS  # noqa: E402

# Closed universe. No liquidity_num_min (settled books need no live depth).
GAMMA_CLOSED_KEYSET = (
    "https://gamma-api.polymarket.com/markets/keyset"
    "?closed=true&order=closedTime&ascending=false"
)

EXIT_PIN = 4
ROW_FIELDS = (
    "condition_id",
    "winning_token_id",
    "outcome_prices_final",
    "closed_ts",
    "snapshot_ts",
    "status",
    "uma_checked",
)
UMA_RESOLVED = "resolved"


def _nonempty_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _finite_price(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    number = as_float(value)
    if number is None:
        return None
    return number


def _iso_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _aware(value: object) -> bool:
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None


def unique_winner_index(prices: list[float]) -> int | None:
    """Exactly one price is 1.0 and every other is 0.0. Otherwise None (fail closed)."""
    if not prices:
        return None
    ones = [index for index, price in enumerate(prices) if price == 1.0]
    zeros = [index for index, price in enumerate(prices) if price == 0.0]
    if len(ones) == 1 and len(zeros) == len(prices) - 1:
        return ones[0]
    return None


def existing_condition_ids(path: Path) -> set[str]:
    ids: set[str] = set()
    for row in read_jsonl(path):
        cid = row.get("condition_id")
        if isinstance(cid, str) and cid:
            ids.add(cid)
    return ids


def classify_market(
    market: dict[str, Any],
    now: datetime,
    snapshot_ts: str,
    hold_hours: float = RESOLUTION_HOLD_HOURS,
) -> tuple[dict[str, Any] | None, str | None]:
    """Return (row, None) or (None, skip_reason). Missing fields fail closed (skip the row)."""
    if not _aware(now):
        return None, "invalid_now"
    hours = _finite_price(hold_hours)
    if hours is None or hours <= 0:
        return None, "invalid_hold_hours"
    if market.get("closed") is not True:
        return None, "not_closed"
    raw_closed = market.get("closedTime")
    if raw_closed is None or raw_closed == "":
        return None, "missing_closed_ts"
    closed = parse_end_date(raw_closed)
    if closed is None:
        return None, "invalid_closed_ts"
    if closed > now:
        return None, "future_closed_ts"
    elapsed_h = (now - closed).total_seconds() / 3600.0
    if elapsed_h < hours:
        return None, "hold_pending"
    condition_id = _nonempty_str(market.get("conditionId"))
    if condition_id is None:
        return None, "missing_condition_id"
    uma = market.get("umaResolutionStatus")
    if not isinstance(uma, str) or not uma.strip():
        return None, "missing_uma_status"
    uma_checked = uma.strip() == UMA_RESOLVED
    raw_prices = market.get("outcomePrices")
    if raw_prices is None or raw_prices == "":
        return None, "missing_outcome_prices"
    listed = json_list(raw_prices)
    if not listed:
        return None, "missing_outcome_prices"
    prices: list[float] = []
    for item in listed:
        parsed = _finite_price(item)
        if parsed is None:
            return None, "missing_outcome_prices"
        if not math.isfinite(parsed):
            return None, "non_finite_outcome_prices"
        prices.append(parsed)
    winner = unique_winner_index(prices)
    if winner is None:
        return None, "no_unique_winner"
    tokens, _field, token_problem = select_token_ids(market)
    if token_problem:
        return None, token_problem
    if len(tokens) != len(prices) or winner >= len(tokens):
        return None, "token_winner_mismatch"
    winning_token_id = tokens[winner]
    if not isinstance(winning_token_id, str) or not winning_token_id:
        return None, "token_winner_mismatch"
    row = {
        "condition_id": condition_id,
        "winning_token_id": winning_token_id,
        "outcome_prices_final": prices,
        "closed_ts": _iso_z(closed),
        "snapshot_ts": snapshot_ts,
        "status": "resolved" if uma_checked else "closed",
        "uma_checked": uma_checked,
    }
    return row, None


def persist_resolutions(path: Path, rows: list[dict[str, Any]]) -> int:
    for row in rows:
        append_jsonl(path, row)
    return len(rows)


def snapshot(
    raw_markets: list[dict],
    now: datetime,
    snapshot_ts: str,
    *,
    hold_hours: float = RESOLUTION_HOLD_HOURS,
    known_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Classify fetched Gamma rows. Does not write. known_ids are existing condition_id values."""
    seen = set(known_ids or ())
    rows: list[dict[str, Any]] = []
    first_reason: Counter[str] = Counter()
    any_reason: Counter[str] = Counter()
    scanned = 0
    for market in raw_markets:
        if not isinstance(market, dict):
            continue
        scanned += 1
        row, reason = classify_market(market, now, snapshot_ts, hold_hours=hold_hours)
        if reason is not None or row is None:
            name = reason or "unclassified"
            first_reason[name] += 1
            any_reason[name] += 1
            continue
        cid = row["condition_id"]
        if cid in seen:
            first_reason["duplicate"] += 1
            any_reason["duplicate"] += 1
            continue
        seen.add(cid)
        rows.append(row)
    return {
        "scanned": scanned,
        "eligible": len(rows),
        "rows": rows,
        "first_reason": dict(first_reason),
        "any_reason": dict(any_reason),
    }


def main(
    *,
    getter: Callable[[str], Any] | None = None,
    resolutions_path: Path | None = None,
    max_markets: int = MAX_MARKETS,
    ts: str | None = None,
    now: datetime | None = None,
    hold_hours: float = RESOLUTION_HOLD_HOURS,
) -> int:
    cfg = load_risk()
    drifted = pin_problems(cfg)
    if drifted:
        print("resolutions refused: paper pins drifted: " + "; ".join(drifted), file=sys.stderr)
        return EXIT_PIN
    stamp = ts or utc_now()
    now_dt = now if _aware(now) else parse_end_date(stamp)
    if now_dt is None or not _aware(now_dt):
        print("resolutions refused: snapshot clock is not a zoned ISO timestamp", file=sys.stderr)
        return EXIT_PIN
    path = resolutions_path or RESOLUTIONS_PATH
    raw, fetch = fetch_all_markets(
        getter,
        max_markets=max_markets,
        server_min_liquidity=None,
        base_url=GAMMA_CLOSED_KEYSET,
    )
    result = snapshot(
        raw,
        now_dt,
        stamp,
        hold_hours=hold_hours,
        known_ids=existing_condition_ids(path),
    )
    skipped = sum(result["first_reason"].values())
    print(
        f"resolutions fetched={fetch['fetched']} scanned={result['scanned']} "
        f"eligible={result['eligible']} skipped={skipped} stop={fetch['stop_reason']} "
        f"hold_hours={hold_hours:g} mode={cfg.get('mode')} "
        f"live_enabled={str(cfg.get('live_enabled')).lower()} keys=none orders=none"
    )
    if result["first_reason"]:
        parts = [f"{name}:{count}" for name, count in sorted(result["first_reason"].items())]
        print("skipped_first=" + ",".join(parts))
    if not fetch["complete"]:
        print(
            f"resolutions failed: pagination incomplete ({fetch['stop_reason']}); nothing written",
            file=sys.stderr,
        )
        return EXIT_INCOMPLETE
    written = persist_resolutions(path, result["rows"]) if result["rows"] else 0
    print(f"written={written} path={path} (read-only snapshot; no fills)")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
