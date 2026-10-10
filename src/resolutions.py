"""Read-only Gamma settlement snapshot for M3 calibration (Task AN). No keys. No orders.

Walks public Gamma /markets/keyset?closed=true. A market is written to
data/resolutions.jsonl only after it has been closed RESOLUTION_HOLD_HOURS (48 h)
and every required field is present. Missing or unusable fields skip that row
(fail closed). Incomplete paging or paper-pin drift writes nothing. No bus
message, no fill, no order, no risk.toml edit.

issue #57 revises AN's first-write-wins for one case only: a condition_id whose stored row is the
unconfirmed snapshot (uma_checked false) may be confirmed once, when this walk sees the same
settlement as exactly resolved with the same winning token, final prices and closed_ts, and a
snapshot clock that did not go backwards. The stored bytes are kept and the confirmation row is
appended after them. Every other repeat appends nothing: a condition that already has a confirmed
row, a repeated confirmation, a status regression, a repeat within one walk, and any disagreement
about the winner, the final prices or the close time (the newest winner is never chosen). The
stored history is read strictly (read_history): a row this module could not have written, a torn
last line or bad JSON is exit 3 with nothing fetched or appended, never skipped by a lenient
reader.
"""
from __future__ import annotations

import json
import math
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

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

# Task AV: exit 4 used to mean both pin drift and a bad snapshot clock. Now split; 4 is retired.
EXIT_BAD_CLOCK = 5  # the snapshot clock is not a zoned ISO timestamp; nothing fetched (global halt)
EXIT_PIN_DRIFT = 6  # same code as scanner/book/ws_recorder/m1_run: risk.toml / paper pins drifted (global halt)
ROW_FIELDS = (
    "condition_id",
    "winning_token_id",
    "outcome_prices_final",
    "closed_ts",
    "snapshot_ts",
    "status",
    "uma_checked",
    # issue #81: every outcome token of the market, in select_token_ids order, so a later label can
    # prove a sample's token really belongs to this market instead of trusting a bare index.
    "outcome_token_ids",
)
# issue #81: rows written before this key existed. They stay readable (and byte for byte unchanged);
# only the two schemas above are ever accepted, anything else is still exit 3.
LEGACY_ROW_FIELDS = ROW_FIELDS[:-1]
UMA_RESOLVED = "resolved"
STATUS_RESOLVED = "resolved"  # the only status a confirmation upgrade may append
STATUS_CLOSED = "closed"  # unique 1/0 winner after the hold, UMA not yet exactly resolved


class HistoryError(RuntimeError):
    """data/resolutions.jsonl is not exactly what this module writes; nothing is fetched or appended."""


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


def _reject_constant(value: str) -> None:
    raise ValueError(f"{value} is not valid JSON")


def outcome_token_problem(tokens: Any, price_count: int) -> str | None:
    """Why a token list cannot identify this settlement's outcomes, else None (issue #81).

    The producer checks the list it is about to write and read_history checks every stored list, so
    a foreign, repeated, empty or short mapping is never silently kept. Same rules in both places.
    """
    if not isinstance(tokens, list) or not tokens:
        return "outcome_token_ids is not a non-empty list"
    if len(tokens) != price_count:
        return "outcome_token_ids is not as long as outcome_prices_final"
    seen: set[str] = set()
    for token in tokens:
        if not isinstance(token, str) or not token or token != token.strip():
            return "outcome_token_ids holds a value that is not a non-empty stripped string"
        if token in seen:
            return "outcome_token_ids repeats a token"
        seen.add(token)
    return None


def _snapshot_problem(row: Any) -> str | None:
    """Why one stored row is not a snapshot this module wrote, else None.

    issue #81: exactly two schemas are ours, the legacy seven fields and today's eight. A legacy row
    is accepted as stored (its missing mapping is not proven here); an eight-field row must carry a
    usable mapping whose winner position is the recorded winning_token_id.
    """
    if not isinstance(row, dict):
        return "not a JSON object"
    if set(row) == set(LEGACY_ROW_FIELDS):
        tokens = None
    elif set(row) == set(ROW_FIELDS):
        tokens = row["outcome_token_ids"]
    else:
        return "keys differ from the snapshot schema"
    for field in ("condition_id", "winning_token_id"):
        value = row[field]
        if not isinstance(value, str) or not value or value != value.strip():
            return f"{field} is not a non-empty stripped string"
    prices = row["outcome_prices_final"]
    if not isinstance(prices, list) or not prices:
        return "outcome_prices_final is not a non-empty list"
    numbers: list[float] = []
    for price in prices:
        if isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(price):
            return "outcome_prices_final holds a non-finite price"
        numbers.append(float(price))
    if unique_winner_index(numbers) is None:
        return "outcome_prices_final has no unique 1.0/0.0 winner"
    if tokens is not None:
        problem = outcome_token_problem(tokens, len(numbers))
        if problem is not None:
            return problem
        if tokens[unique_winner_index(numbers)] != row["winning_token_id"]:
            return "outcome_token_ids and winning_token_id disagree"
    if parse_end_date(row["closed_ts"]) is None:
        return "closed_ts is not a zoned ISO timestamp"
    if parse_end_date(row["snapshot_ts"]) is None:
        return "snapshot_ts is not a zoned ISO timestamp"
    checked = row["uma_checked"]
    if not isinstance(checked, bool):
        return "uma_checked is not a JSON boolean"
    if row["status"] != (STATUS_RESOLVED if checked else STATUS_CLOSED):
        return "status and uma_checked disagree"
    return None


def read_history(path: Path) -> list[dict[str, Any]]:
    """Read an existing data/resolutions.jsonl strictly, else raise HistoryError.

    Every complete line must be a JSON object holding exactly ROW_FIELDS (or exactly the legacy seven
    fields written before issue #81) with the values classify_market writes. read_jsonl would
    silently skip a line it cannot parse, and a lost row
    is exactly what a later append would duplicate, so a torn last line (no closing newline), a
    blank line, bad JSON, NaN/Infinity, a non-object, another key or a wrong value fails the run
    (exit 3) with nothing appended. An absent file is an empty history.
    """
    if not path.exists():
        return []
    try:
        text = path.read_bytes().decode("utf-8")
    except OSError as exc:
        raise HistoryError(f"unreadable ({type(exc).__name__})") from exc
    except UnicodeDecodeError as exc:
        raise HistoryError(f"not UTF-8 ({exc})") from exc
    if not text:
        return []
    lines = text.split("\n")
    if lines[-1] != "":
        raise HistoryError(f"line {len(lines)}: no closing newline (torn write)")
    lines.pop()
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(lines, 1):
        if not line.strip():
            raise HistoryError(f"line {number}: blank line")
        try:
            row = json.loads(line, parse_constant=_reject_constant)
        except ValueError as exc:
            raise HistoryError(f"line {number}: not JSON ({exc})") from exc
        problem = _snapshot_problem(row)
        if problem is not None:
            raise HistoryError(f"line {number}: {problem}")
        rows.append(row)
    return rows


def content_conflict(prior: dict[str, Any], row: dict[str, Any]) -> str | None:
    """Why two rows disagree about the settlement, else None (same winner, prices and close time).

    issue #81: when both rows carry the outcome token mapping it must be identical too, so a
    confirmation can never quietly replace one mapping with another.
    """
    if prior["winning_token_id"] != row["winning_token_id"]:
        return "upgrade_winner_conflict"
    if [float(price) for price in prior["outcome_prices_final"]] != [
        float(price) for price in row["outcome_prices_final"]
    ]:
        return "upgrade_prices_conflict"
    if parse_end_date(prior["closed_ts"]) != parse_end_date(row["closed_ts"]):
        return "upgrade_closed_ts_conflict"
    # issue #81: last, so the older names keep their meaning; when both rows carry the mapping it
    # must be identical, a confirmation never quietly replaces one mapping with another.
    prior_tokens = prior.get("outcome_token_ids")
    row_tokens = row.get("outcome_token_ids")
    if prior_tokens is not None and row_tokens is not None and list(prior_tokens) != list(row_tokens):
        return "upgrade_token_mapping_conflict"
    return None


def upgrade_problem(prior: dict[str, Any], row: dict[str, Any]) -> str | None:
    """Why this resolved row may not confirm the stored unconfirmed row, else None (append it).

    The settlement content must be identical and the snapshot clock may not go backwards. The
    newest winner is never chosen: any disagreement refuses the upgrade and is reported by name.
    """
    conflict = content_conflict(prior, row)
    if conflict is not None:
        return conflict
    prior_ts = parse_end_date(prior["snapshot_ts"])
    stamp = parse_end_date(row["snapshot_ts"])
    if prior_ts is None or stamp is None or stamp < prior_ts:
        return "upgrade_clock_regression"
    return None


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
    # issue #81: the producer checks its own mapping before writing it (the scanner is not widened).
    if outcome_token_problem(tokens, len(prices)) is not None:
        return None, "invalid_outcome_token_ids"
    winning_token_id = tokens[winner]
    if not isinstance(winning_token_id, str) or not winning_token_id:
        return None, "token_winner_mismatch"
    row = {
        "condition_id": condition_id,
        "winning_token_id": winning_token_id,
        "outcome_prices_final": prices,
        "closed_ts": _iso_z(closed),
        "snapshot_ts": snapshot_ts,
        "status": STATUS_RESOLVED if uma_checked else STATUS_CLOSED,
        "uma_checked": uma_checked,
        "outcome_token_ids": list(tokens),
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
    history: Sequence[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Classify fetched Gamma rows. Does not write.

    ``history`` are the stored rows (read_history, already validated). A condition_id already in
    the history gets no new row, except one confirmation upgrade of the stored unconfirmed snapshot
    (upgrade_problem). Every other repeat is counted under a named reason: ``already_confirmed``,
    ``duplicate`` (unconfirmed again, or a repeat within this walk) or the ``upgrade_*`` conflict.
    """
    seen: set[str] = set()
    stored_first: dict[str, dict[str, Any]] = {}
    stored_confirmed: dict[str, dict[str, Any]] = {}
    for stored in history or ():
        cid = stored["condition_id"]
        stored_first.setdefault(cid, stored)
        if stored["uma_checked"] is True:
            stored_confirmed.setdefault(cid, stored)
    rows: list[dict[str, Any]] = []
    first_reason: Counter[str] = Counter()
    any_reason: Counter[str] = Counter()
    upgrades = 0
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
        if cid not in stored_first:
            rows.append(row)
            continue
        confirmed = stored_confirmed.get(cid)
        if confirmed is not None:
            # at most one confirmation per condition: a repeat, a regression or a disagreement
            conflict = content_conflict(confirmed, row) if row["uma_checked"] is True else None
            name = conflict or "already_confirmed"
            first_reason[name] += 1
            any_reason[name] += 1
            continue
        if row["uma_checked"] is not True:
            # unconfirmed again: the stored first row stands (AN's first write wins)
            first_reason["duplicate"] += 1
            any_reason["duplicate"] += 1
            continue
        problem = upgrade_problem(stored_first[cid], row)
        if problem is not None:
            first_reason[problem] += 1
            any_reason[problem] += 1
            continue
        rows.append(row)
        upgrades += 1
    return {
        "scanned": scanned,
        "eligible": len(rows),
        "upgrades": upgrades,
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
    from src.pins import refuse_start  # Task AS (gap 5): the risk file pins (sha + every key) at start

    if refuse_start("resolutions"):
        return EXIT_PIN_DRIFT
    cfg = load_risk()
    drifted = pin_problems(cfg)
    if drifted:
        print("resolutions refused: paper pins drifted: " + "; ".join(drifted), file=sys.stderr)
        return EXIT_PIN_DRIFT
    stamp = ts or utc_now()
    now_dt = now if _aware(now) else parse_end_date(stamp)
    if now_dt is None or not _aware(now_dt):
        print("resolutions refused: snapshot clock is not a zoned ISO timestamp", file=sys.stderr)
        return EXIT_BAD_CLOCK
    path = resolutions_path or RESOLUTIONS_PATH
    try:
        history = read_history(path)
    except HistoryError as exc:
        print(
            f"resolutions failed: existing {path.name} history unusable ({exc}); nothing fetched or written",
            file=sys.stderr,
        )
        return EXIT_INCOMPLETE
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
        history=history,
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
    print(f"written={written} upgrades={result['upgrades']} path={path} (read-only snapshot; no fills)")
    return EXIT_OK


if __name__ == "__main__":
    # Task AV: start/finish heartbeat (logs/heartbeat/resolutions.json) so the resident Sentinel acts
    # on exit 5 / 6 (docs/EXIT_CODES.md).
    from src.heartbeat import run_main

    raise SystemExit(run_main("resolutions", main))
