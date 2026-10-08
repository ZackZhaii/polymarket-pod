"""Phase 0 public CLOB /book reader. Records best bid, ask, and spread. No orders."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.podlib import (  # noqa: E402
    BOOKS_PATH,
    MARKETS_PATH,
    append_jsonl,
    as_float,
    bus,
    load_risk,
    public_get,
    read_jsonl,
    utc_now,
)


def latest_markets(limit: int = 5) -> list[dict[str, Any]]:
    rows = read_jsonl(MARKETS_PATH)
    if not rows:
        return []
    latest_ts = max(str(row.get("ts") or "") for row in rows)
    chosen = [row for row in rows if str(row.get("ts") or "") == latest_ts]
    return chosen[:limit]


def level(levels: Any, pick: str) -> tuple[float | None, float | None]:
    parsed: list[tuple[float, float | None]] = []
    if not isinstance(levels, list):
        return None, None
    for item in levels:
        if not isinstance(item, dict):
            continue
        price = as_float(item.get("price"))
        size = as_float(item.get("size"))
        if price is None:
            continue
        parsed.append((price, size))
    if not parsed:
        return None, None
    price, size = (max if pick == "max" else min)(parsed, key=lambda pair: pair[0])
    return price, size


def snapshot(
    market: dict[str, Any],
    outcome: str,
    token_id: str,
    ts: str,
    getter=None,
) -> dict[str, Any] | None:
    url = "https://clob.polymarket.com/book?" + urlencode({"token_id": token_id})
    get = getter or public_get
    try:
        book = get(url)
    except Exception as exc:  # network or HTTP failure on one token
        print(f"book_skip token={token_id[:12]} reason={type(exc).__name__}", file=sys.stderr)
        return None
    if not isinstance(book, dict):
        return None
    best_bid, bid_size = level(book.get("bids"), "max")
    best_ask, ask_size = level(book.get("asks"), "min")
    spread = None if best_bid is None or best_ask is None else round(best_ask - best_bid, 6)
    if best_bid is not None:
        best_bid = round(best_bid, 6)
    if best_ask is not None:
        best_ask = round(best_ask, 6)
    return {
        "ts": ts,
        "market_id": str(market.get("id") or ""),
        "question": market.get("question") or "",
        "condition_id": market.get("condition_id") or book.get("market") or "",
        "token_id": token_id,
        "outcome": outcome,
        "best_bid": best_bid,
        "best_bid_size": bid_size,
        "best_ask": best_ask,
        "best_ask_size": ask_size,
        "spread": spread,
        "tick_size": as_float(book.get("tick_size")),
        "min_order_size": as_float(book.get("min_order_size")),
        "liquidity_usd": market.get("liquidity_usd"),
        "source": "clob_public_book",
    }


def record(
    markets: list[dict[str, Any]],
    ts: str,
    *,
    books_path: Path = BOOKS_PATH,
    bus_path: Path | None = None,
    getter=None,
    per_market: int = 2,
) -> int:
    written = 0
    for market in markets:
        tokens = [str(item) for item in market.get("token_ids") or []]
        outcomes = [str(item) for item in market.get("outcomes") or []]
        pairs = list(zip(outcomes, tokens)) or [(str(index), token) for index, token in enumerate(tokens)]
        for outcome, token_id in pairs[:per_market]:
            if not token_id:
                continue
            row = snapshot(market, outcome, token_id, ts, getter=getter)
            if row is None:
                continue
            append_jsonl(books_path, row)
            bus(
                "BookWatcher",
                "RiskGate",
                "signal",
                row["market_id"],
                {
                    "token_id": token_id,
                    "outcome": outcome,
                    "best_bid": row["best_bid"],
                    "best_ask": row["best_ask"],
                    "spread": row["spread"],
                    "source": "clob_public_book",
                },
                path=bus_path,
            )
            written += 1
    return written


def main() -> int:
    cfg = load_risk()
    markets = latest_markets()
    if not markets:
        print("no markets in data/markets.jsonl; run python src/scanner.py first", file=sys.stderr)
        return 2
    ts = utc_now()
    written = record(markets, ts)
    for row in read_jsonl(BOOKS_PATH):
        if str(row.get("ts") or "") != ts:
            continue
        print(
            f"- {row['question']} | {row['outcome']} | bid={row['best_bid']} "
            f"ask={row['best_ask']} spread={row['spread']}"
        )
    live = cfg.get("live_enabled")
    print(
        f"book_snapshots={written} mode={cfg.get('mode')} "
        f"live_enabled={str(live).lower()} orders=none"
    )
    print(f"wrote={BOOKS_PATH}")
    if written < 1:
        print("book failed: no public book snapshot", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
