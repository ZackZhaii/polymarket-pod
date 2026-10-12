"""Endgame scan: a second, read-only path that hands Endgame its 0.98-0.99 band.

COORDINATION backlog #10: the normal scanner drops prices above scanner_max_price (0.97)
as long_tail_price, so markets in Endgame's band never reached it. This path:

- fetches the same public Gamma keyset pages through scanner.fetch_all_markets (GET only,
  no keys); an incomplete walk writes nothing (exit 3), fewer than 5 markets exit 1;
- runs every scanner rule except the long-tail price band (closed, version, 24 h expiry,
  accepting orders, bid/ask, liquidity, spread, price), so nothing else is loosened;
- fails closed on any non-finite number: the shared scanner.rejection_reasons gives Task Y's
  non_finite_<field> (bid, ask, spread, liquidity, volumes, outcome prices, price), and
  band_problems re-checks price, bid, ask, spread and liquidity under the same names (counted once);
- keeps only rows where exactly one of the two outcomes is inside ENDGAME_PRICE_MIN..ENDGAME_PRICE_MAX
  (inclusive, the same check endgame.emit makes). Outcome 0 is the scanner's YES / first-outcome
  price (scanner.yes_price); outcome 1 is Gamma's outcomePrices[1] (Task AD). Both prices are
  needed for the decision: a missing one rejects as missing_price, a non-finite one as
  non_finite_price / non_finite_outcome_prices. Both in the band is an inconsistent book and
  rejects as both_outcomes_in_band; one in the band with the other above it rejects as
  other_outcome_above_band. The selected outcome must have its own token id (version-aware
  scanner.select_token_ids), else the scanner's token problem name rejects it;
- hands each row to endgame.emit, which only emits a signal to RiskGate (48 h window); the
  signal names the selected outcome index, outcome label and token id.
  No fill, no order, no ledger write; RiskGate still decides every intent.

The scanner itself is untouched: its default output, filters, exit codes and
data/markets.jsonl are unchanged; this path writes only Endgame bus signals.
It refuses to run unless the paper pins hold (mode=paper, live_enabled=false, ...).
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

from src import endgame  # noqa: E402
from src.thresholds import MIN_HOURS_TO_EXPIRY, MIN_SCANNED_MARKETS  # noqa: E402 - Task AC
from src.thresholds import ENDGAME_PRICE_MAX, ENDGAME_PRICE_MIN  # noqa: E402 - Task AD
from src.podlib import load_risk, pin_problems, utc_now  # noqa: E402
from src.scanner import (  # noqa: E402
    EXIT_INCOMPLETE,
    EXIT_OK,
    EXIT_TOO_FEW_SCANNED,
    MAX_MARKETS,
    days_to_expiry,
    fetch_all_markets,
    normalize,
    parse_utc,
    rejection_reasons,
    thresholds_from,
)

# The only scanner rule this path does not apply: the long-tail band is what hid Endgame's band.
SKIPPED_SCANNER_REASON = "long_tail_price"
OUTSIDE_BAND = "outside_endgame_band"
# Task AD: second outcome. No new numbers: the band is src/thresholds.py ENDGAME_PRICE_MIN..MAX.
BOTH_OUTCOMES_IN_BAND = "both_outcomes_in_band"
OTHER_OUTCOME_ABOVE_BAND = "other_outcome_above_band"
MISSING_PRICE = "missing_price"  # the scanner's reason name, reused
NON_FINITE_OUTCOME_PRICES = "non_finite_outcome_prices"  # the scanner's reason name, reused
MISSING_TOKEN_IDS = "missing_token_ids"  # scanner.select_token_ids problem name, reused
FINITE_FIELDS = ("price", "best_bid", "best_ask", "spread", "liquidity_usd")
EXIT_NOT_PAPER = 4


def band_problems(row: dict[str, Any]) -> list[str]:
    """Endgame-only checks: every number finite (fail closed), exactly one outcome in the band."""
    return band_decision(row)[0]


def band_decision(row: dict[str, Any]) -> tuple[list[str], dict[str, Any] | None]:
    """(problems, selection). selection names the one outcome inside the band and its token.

    Outcome 0 is row["price"] (scanner.yes_price), outcome 1 is row["outcome_prices"][1].
    Fail closed: any problem means no selection.
    """
    problems = [
        f"non_finite_{key}"
        for key in FINITE_FIELDS
        if isinstance(row.get(key), float) and not math.isfinite(row[key])
    ]
    prices = row.get("outcome_prices")
    raw_second = prices[1] if isinstance(prices, list) and len(prices) >= 2 else None
    if isinstance(raw_second, float) and not math.isfinite(raw_second):
        problems.append(NON_FINITE_OUTCOME_PRICES)
    if problems:
        return problems, None
    first, second = _price(row.get("price")), _price(raw_second)
    if first is None or second is None:
        return [MISSING_PRICE], None
    first_in, second_in = _in_band(first), _in_band(second)
    if first_in and second_in:
        return [BOTH_OUTCOMES_IN_BAND], None
    if not (first_in or second_in):
        return [OUTSIDE_BAND], None
    index = 0 if first_in else 1
    other = second if index == 0 else first
    if other > ENDGAME_PRICE_MAX:
        return [OTHER_OUTCOME_ABOVE_BAND], None
    tokens = row.get("token_ids") if isinstance(row.get("token_ids"), list) else []
    token = tokens[index] if len(tokens) > index else None
    if not isinstance(token, str) or not token:
        return [row.get("token_ids_problem") or MISSING_TOKEN_IDS], None
    outcomes = row.get("outcomes") if isinstance(row.get("outcomes"), list) else []
    label = outcomes[index] if len(outcomes) > index else None
    selection = {
        "outcome_index": index,
        "outcome": label,
        "token_id": token,
        "price": first if index == 0 else second,
    }
    return [], selection


def _price(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _in_band(price: float) -> bool:
    return ENDGAME_PRICE_MIN <= price <= ENDGAME_PRICE_MAX


def endgame_candidates(
    raw_markets: list[dict],
    limits: dict[str, float],
    ts: str,
    now: datetime | None = None,
    min_hours_to_expiry: float = MIN_HOURS_TO_EXPIRY,
) -> dict[str, Any]:
    """Rows that pass every scanner rule except the long-tail band and sit in Endgame's band.

    ``limits`` is validated with scanner.thresholds_from (missing or bad keys raise).
    The inputs are not modified. Rows are sorted by 24h volume, like the scanner's.
    """
    limits = thresholds_from(dict(limits))
    now_dt = now or parse_utc(ts) or datetime.now(timezone.utc)
    rows: list[dict[str, Any]] = []
    first_reason: Counter = Counter()
    any_reason: Counter = Counter()
    selected: Counter = Counter()
    scanned = 0
    for market in raw_markets:
        row = normalize(market, ts)
        if not (row["question"] or row["slug"] or row["id"]):
            continue
        scanned += 1
        reasons = [
            reason
            for reason in rejection_reasons(row, limits, now_dt, min_hours_to_expiry)
            if reason != SKIPPED_SCANNER_REASON
        ]
        band, selection = band_decision(row)
        reasons += [problem for problem in band if problem not in reasons]
        if reasons or selection is None:
            reasons = reasons or [OUTSIDE_BAND]
            first_reason[reasons[0]] += 1
            any_reason.update(reasons)
            continue
        row["days_to_expiry"] = days_to_expiry(row["end_date"], now_dt)
        row["endgame_outcome_index"] = selection["outcome_index"]
        row["endgame_outcome"] = selection["outcome"]
        row["endgame_token_id"] = selection["token_id"]
        row["endgame_price"] = selection["price"]
        selected[str(selection["outcome_index"])] += 1
        rows.append(row)
    rows.sort(key=lambda item: item["volume_24h_usd"], reverse=True)
    return {
        "ts": ts,
        "scanned": scanned,
        "candidates": len(rows),
        "rows": rows,
        "first_reason": dict(first_reason),
        "any_reason": dict(any_reason),
        "selected_outcomes": dict(selected),
        "band": [endgame.PRICE_MIN, endgame.PRICE_MAX],
        "min_hours_to_expiry": min_hours_to_expiry,
    }


def _stamp(value: Any) -> str | None:
    parsed = parse_utc(value)
    return None if parsed is None else parsed.strftime("%Y-%m-%dT%H:%M:%SZ")


def emit_signals(rows: list[dict[str, Any]], ts: str, *, bus_path: Path | None = None) -> list[dict]:
    """Hand each candidate to endgame.emit. Signals to RiskGate only; never a fill or order.

    Each signal names the selected outcome and token (Task AD). A row without a selection
    (not from endgame_candidates) is skipped: fail closed.
    """
    now = _stamp(ts)
    if now is None:
        return []
    signals = []
    for row in rows:
        end = _stamp(row.get("end_date"))
        if end is None or "endgame_outcome_index" not in row:
            continue
        message = endgame.emit(
            row["endgame_price"],
            end,
            row["id"],
            now=now,
            bus_path=bus_path,
            outcome_index=row["endgame_outcome_index"],
            outcome=row.get("endgame_outcome"),
            token_id=row.get("endgame_token_id"),
        )
        if message is not None:
            signals.append(message)
    return signals


def main(
    *,
    getter: Callable[[str], Any] | None = None,
    bus_path: Path | None = None,
    max_markets: int = MAX_MARKETS,
    ts: str | None = None,
    min_hours_to_expiry: float = MIN_HOURS_TO_EXPIRY,
) -> int:
    from src.pins import refuse_start  # Task AS (gap 5): the risk file pins (sha + every key) at start

    if refuse_start("endgame_scan", bus_path=bus_path):
        return EXIT_NOT_PAPER
    cfg = load_risk()
    drifted = pin_problems(cfg)
    if drifted:
        print("endgame_scan refused: paper pins drifted: " + "; ".join(drifted), file=sys.stderr)
        return EXIT_NOT_PAPER
    limits = thresholds_from(cfg)
    stamp = ts or utc_now()
    raw, fetch = fetch_all_markets(
        getter, max_markets=max_markets, server_min_liquidity=limits["min_liquidity_usd"]
    )
    result = endgame_candidates(raw, limits, stamp, min_hours_to_expiry=min_hours_to_expiry)
    print(
        f"endgame_scan scanned={result['scanned']} candidates={result['candidates']} "
        f"band={endgame.PRICE_MIN:g}-{endgame.PRICE_MAX:g} stop={fetch['stop_reason']} "
        f"min_hours_to_expiry={min_hours_to_expiry:g} mode={cfg.get('mode')} "
        f"live_enabled={str(cfg.get('live_enabled')).lower()} keys=none orders=none"
    )
    if not fetch["complete"]:
        print(f"endgame_scan failed: pagination incomplete ({fetch['stop_reason']}); nothing written", file=sys.stderr)
        return EXIT_INCOMPLETE
    if result["scanned"] < MIN_SCANNED_MARKETS:
        print(f"endgame_scan failed: fewer than {MIN_SCANNED_MARKETS} public markets", file=sys.stderr)
        return EXIT_TOO_FEW_SCANNED
    signals = emit_signals(result["rows"], stamp, bus_path=bus_path)
    for row in result["rows"][:10]:
        print(
            f"- {row['slug']} outcome={row['endgame_outcome_index']}:{row['endgame_outcome']} "
            f"token={str(row['endgame_token_id'])[:12]} price={row['endgame_price']:.4f} days={row['days_to_expiry']:.2f}"
        )
    print(f"endgame_signals={len(signals)} (signal only, RiskGate decides; no fills)")
    return EXIT_OK


if __name__ == "__main__":
    # Task AP: start/finish heartbeat (logs/heartbeat/endgame_scan.json) for the resident Sentinel.
    from src.heartbeat import run_main

    raise SystemExit(run_main("endgame_scan", main))
