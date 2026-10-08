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
- keeps only rows whose first-outcome price is inside endgame.PRICE_MIN..PRICE_MAX
  (inclusive, the same check endgame.emit makes);
- hands each row to endgame.emit, which only emits a signal to RiskGate (48 h window).
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
from src.expiry_rule import MIN_HOURS_TO_EXPIRY  # noqa: E402
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
FINITE_FIELDS = ("price", "best_bid", "best_ask", "spread", "liquidity_usd")
EXIT_NOT_PAPER = 4


def band_problems(row: dict[str, Any]) -> list[str]:
    """Endgame-only checks: every number finite (fail closed), price inside the band."""
    problems = [
        f"non_finite_{key}"
        for key in FINITE_FIELDS
        if isinstance(row.get(key), float) and not math.isfinite(row[key])
    ]
    price = row.get("price")
    if not problems and isinstance(price, (int, float)) and not isinstance(price, bool):
        if not endgame.PRICE_MIN <= float(price) <= endgame.PRICE_MAX:
            problems.append(OUTSIDE_BAND)
    return problems


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
        reasons += [problem for problem in band_problems(row) if problem not in reasons]
        if reasons:
            first_reason[reasons[0]] += 1
            any_reason.update(reasons)
            continue
        row["days_to_expiry"] = days_to_expiry(row["end_date"], now_dt)
        rows.append(row)
    rows.sort(key=lambda item: item["volume_24h_usd"], reverse=True)
    return {
        "ts": ts,
        "scanned": scanned,
        "candidates": len(rows),
        "rows": rows,
        "first_reason": dict(first_reason),
        "any_reason": dict(any_reason),
        "band": [endgame.PRICE_MIN, endgame.PRICE_MAX],
        "min_hours_to_expiry": min_hours_to_expiry,
    }


def _stamp(value: Any) -> str | None:
    parsed = parse_utc(value)
    return None if parsed is None else parsed.strftime("%Y-%m-%dT%H:%M:%SZ")


def emit_signals(rows: list[dict[str, Any]], ts: str, *, bus_path: Path | None = None) -> list[dict]:
    """Hand each candidate to endgame.emit. Signals to RiskGate only; never a fill or order."""
    now = _stamp(ts)
    if now is None:
        return []
    signals = []
    for row in rows:
        end = _stamp(row.get("end_date"))
        if end is None:
            continue
        message = endgame.emit(row["price"], end, row["id"], now=now, bus_path=bus_path)
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
    if result["scanned"] < 5:
        print("endgame_scan failed: fewer than 5 public markets", file=sys.stderr)
        return EXIT_TOO_FEW_SCANNED
    signals = emit_signals(result["rows"], stamp, bus_path=bus_path)
    for row in result["rows"][:10]:
        print(f"- {row['slug']} price={row['price']:.4f} days={row['days_to_expiry']:.2f}")
    print(f"endgame_signals={len(signals)} (signal only, RiskGate decides; no fills)")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
