"""Offline paper cycle.

Reads local market and book jsonl. Emits gap and staleness signals only.
No network, no orders, no paper fills, no edits to config/risk.toml.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import arb_binary, neg_risk  # noqa: E402
from src.podlib import (  # noqa: E402
    BOOKS_PATH,
    MARKETS_PATH,
    RISK_PATH,
    bus,
    load_risk,
    pin_problems,
    read_jsonl,
    utc_now,
)

STALE_AFTER_S = 120


def run(
    *,
    markets_path: Path = MARKETS_PATH,
    books_path: Path = BOOKS_PATH,
    bus_path: Path | None = None,
    now: str | None = None,
    stale_after_s: int = STALE_AFTER_S,
    cfg: dict | None = None,
) -> dict:
    before = RISK_PATH.read_bytes()
    loaded = cfg if cfg is not None else load_risk()
    if pin_problems(loaded):
        _assert_risk_unchanged(before)
        return {"ok": False, "reason": "phase0_pin", "signals": 0, "gaps": 0, "stale": 0, "fills": 0}
    now_s = now or utc_now()
    markets = _latest_by(read_jsonl(markets_path), "id")
    books = _latest_books(read_jsonl(books_path))
    gaps = 0
    stale = 0
    for market_id, market in markets.items():
        tokens = [str(token) for token in market.get("token_ids") or []]
        asks: list[float] = []
        depths: list[float | None] = []
        market_stale = False
        for token in tokens:
            book = books.get((market_id, token))
            if book is None:
                continue
            if _age_seconds(book.get("ts"), now_s) > stale_after_s:
                market_stale = True
            ask = book.get("best_ask")
            if isinstance(ask, (int, float)) and 0.0 < float(ask) < 1.0:
                asks.append(float(ask))
                size = book.get("best_ask_size")
                depths.append(float(size) if isinstance(size, (int, float)) else None)
        if market_stale:
            bus(
                "BookWatcher",
                "Sentinel",
                "signal",
                market_id,
                {
                    "stale": True,
                    "stale_after_s": stale_after_s,
                    "exchange_action": "none",
                },
                path=bus_path,
            )
            stale += 1
        message = _gap_message(asks, market_id, depths, bus_path)
        if message is not None:
            if message["type"] != "signal":
                raise RuntimeError("paper cycle refused a non-signal")
            gaps += 1
    _assert_risk_unchanged(before)
    return {
        "ok": True,
        "reason": "signals_only",
        "signals": gaps + stale,
        "gaps": gaps,
        "stale": stale,
        "fills": 0,
    }


def _gap_message(asks: list[float], market_id: str, depths: list[float | None], bus_path: Path | None) -> dict | None:
    depth = min(depths) if depths and all(item is not None for item in depths) else None
    if len(asks) == 2:
        return arb_binary.emit_gap(asks[0], asks[1], market_id, bus_path=bus_path, displayed_depth=depth)
    if len(asks) >= 3:
        return neg_risk.emit_gap(asks, market_id, bus_path=bus_path, displayed_depth=depth)
    return None


def _latest_by(rows: list[dict], key: str) -> dict[str, dict]:
    chosen: dict[str, dict] = {}
    for row in rows:
        name = str(row.get(key) or "")
        if not name:
            continue
        prev = chosen.get(name)
        if prev is None or str(row.get("ts") or "") >= str(prev.get("ts") or ""):
            chosen[name] = row
    return chosen


def _latest_books(rows: list[dict]) -> dict[tuple[str, str], dict]:
    chosen: dict[tuple[str, str], dict] = {}
    for row in rows:
        key = (str(row.get("market_id") or ""), str(row.get("token_id") or ""))
        if not key[0] or not key[1]:
            continue
        prev = chosen.get(key)
        if prev is None or str(row.get("ts") or "") >= str(prev.get("ts") or ""):
            chosen[key] = row
    return chosen


def _age_seconds(ts: object, now_s: str) -> float:
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    try:
        left = datetime.strptime(str(ts), fmt).replace(tzinfo=timezone.utc)
        right = datetime.strptime(now_s, fmt).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return float("inf")
    return (right - left).total_seconds()


def _assert_risk_unchanged(before: bytes) -> None:
    if RISK_PATH.read_bytes() != before:
        raise RuntimeError("risk.toml changed during paper cycle")


def main() -> int:
    result = run()
    print(
        f"ok={str(result['ok']).lower()} reason={result['reason']} "
        f"gaps={result['gaps']} stale={result['stale']} fills={result['fills']} orders=none"
    )
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
