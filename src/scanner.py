"""Phase 0 read-only Gamma scanner. No keys. No orders."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.podlib import (  # noqa: E402
    MARKETS_PATH,
    append_jsonl,
    as_float,
    bus,
    json_list,
    load_risk,
    public_get,
    utc_now,
)

GAMMA = (
    "https://gamma-api.polymarket.com/markets"
    "?closed=false&limit=20&order=liquidityNum&ascending=false"
)


def fetch_markets() -> list[dict]:
    data = public_get(GAMMA)
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        rows = data.get("data", [])
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    return []


def normalize(market: dict[str, Any], ts: str) -> dict[str, Any]:
    tokens = [str(item) for item in json_list(market.get("clobTokenIds"))]
    outcomes = [str(item) for item in json_list(market.get("outcomes"))]
    prices = [as_float(item) for item in json_list(market.get("outcomePrices"))]
    best_bid = as_float(market.get("bestBid"))
    best_ask = as_float(market.get("bestAsk"))
    spread = as_float(market.get("spread"))
    if spread is None and best_bid is not None and best_ask is not None:
        spread = best_ask - best_bid
    return {
        "ts": ts,
        "id": str(market.get("id") or ""),
        "question": market.get("question") or "",
        "slug": market.get("slug") or "",
        "condition_id": market.get("conditionId") or "",
        "token_ids": tokens,
        "outcomes": outcomes,
        "outcome_prices": prices,
        "liquidity_usd": as_float(market.get("liquidityNum") if market.get("liquidityNum") is not None else market.get("liquidity")),
        "volume_usd": as_float(market.get("volumeNum") if market.get("volumeNum") is not None else market.get("volume")),
        "end_date": market.get("endDate") or "",
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread": spread,
        "fees_enabled": bool(market.get("feesEnabled")),
        "closed": bool(market.get("closed")),
        "source": "gamma_public",
    }


def visible_rows(raw_markets: list[dict], ts: str) -> list[dict[str, Any]]:
    rows = []
    for market in raw_markets:
        row = normalize(market, ts)
        if row["question"] or row["slug"] or row["id"]:
            rows.append(row)
    return rows


def persist_markets(
    rows: list[dict[str, Any]],
    markets_path: Path = MARKETS_PATH,
    bus_path: Path | None = None,
) -> int:
    for row in rows:
        append_jsonl(markets_path, row)
        bus(
            "Scanner",
            "BookWatcher",
            "signal",
            row["id"],
            {
                "question": row["question"],
                "slug": row["slug"],
                "liquidity_usd": row["liquidity_usd"],
                "spread": row["spread"],
                "source": "gamma_public",
            },
            path=bus_path,
        )
    return len(rows)


def main() -> int:
    cfg = load_risk()
    ts = utc_now()
    rows = visible_rows(fetch_markets(), ts)
    persist_markets(rows)
    mode = cfg.get("mode")
    live = cfg.get("live_enabled")
    print(
        f"active_sample={len(rows)} mode={mode} live_enabled={str(live).lower()} "
        "keys=none orders=none"
    )
    for row in rows[:10]:
        print("-", row["question"] or row["slug"] or row["id"])
    print(f"wrote={MARKETS_PATH}")
    if len(rows) < 5:
        print("scanner failed: fewer than 5 public markets", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
