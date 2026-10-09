"""Phase 0 public CLOB /book reader. Records best bid, ask, and spread. No orders.

Each snapshot also carries ``book_ts``: the book's own timestamp from the /book response,
normalised by book_timestamp() to ISO 8601 UTC. It is never replaced by the recorder's clock
(``ts``). When the response has no usable timestamp, ``book_ts`` is null and ``book_ts_problem``
says why (missing_book_ts or invalid_book_ts); RiskGate vetoes a null book_ts (fail closed).
"""
from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.book_staleness import INVALID_BOOK_TS, MISSING_BOOK_TS, parse_book_ts  # noqa: E402
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


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_EPOCH_MS = re.compile(r"[0-9]+")


def book_timestamp(raw: Any) -> tuple[str | None, str | None]:
    """Return ``(book_ts, None)`` or ``(None, reason)`` for the /book ``timestamp`` field.

    The public CLOB /book response gives the snapshot time as Unix milliseconds in a string,
    e.g. "1782753357257" (docs.polymarket.com, Get order book); a non-negative int is read the
    same way. An aware ISO 8601 string is accepted through book_staleness.parse_book_ts.
    Output is ISO 8601 UTC with milliseconds and a Z. Nothing is guessed: floats (unit
    unknown), bools, signs, decimals, non-ASCII digits and naive times are invalid_book_ts;
    an absent or blank value is missing_book_ts.
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None, MISSING_BOOK_TS
    stamp: datetime | None = None
    if isinstance(raw, bool):
        return None, INVALID_BOOK_TS
    if isinstance(raw, int):
        stamp = _from_millis(raw)
    elif isinstance(raw, str):
        text = raw.strip()
        if _EPOCH_MS.fullmatch(text):
            stamp = _from_millis(int(text))
        elif "T" in text:
            stamp = parse_book_ts(text)
    if stamp is None:
        return None, INVALID_BOOK_TS
    return stamp.isoformat(timespec="milliseconds").replace("+00:00", "Z"), None


def _from_millis(value: int) -> datetime | None:
    if value < 0:
        return None
    try:
        return _EPOCH + timedelta(milliseconds=value)
    except OverflowError:
        return None


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
    book_ts, book_ts_problem = book_timestamp(book.get("timestamp"))
    if book_ts_problem is not None:
        print(f"book_ts_problem token={token_id[:12]} reason={book_ts_problem}", file=sys.stderr)
    return {
        "ts": ts,
        "book_ts": book_ts,
        "book_ts_problem": book_ts_problem,
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
                    "book_ts": row["book_ts"],
                    "book_ts_problem": row["book_ts_problem"],
                    "source": "clob_public_book",
                },
                path=bus_path,
            )
            written += 1
    return written


EXIT_OK = 0
EXIT_NO_SNAPSHOT = 1
EXIT_NO_MARKETS = 2
EXIT_PIN_DRIFT = 6  # Task AS: config/risk.toml off its pins at start (global halt recorded; nothing fetched)


def main() -> int:
    from src.pins import refuse_start  # Task AS (gap 5): runtime pin check at start

    if refuse_start("book"):
        return EXIT_PIN_DRIFT
    cfg = load_risk()
    markets = latest_markets()
    if not markets:
        print("no markets in data/markets.jsonl; run python src/scanner.py first", file=sys.stderr)
        return EXIT_NO_MARKETS
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
        return EXIT_NO_SNAPSHOT
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
