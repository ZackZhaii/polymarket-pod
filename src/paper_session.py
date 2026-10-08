"""One public paper session.

Reads Gamma and CLOB /book, then runs the offline signal cycle.
No .env, no orders, no paper fills, no edits to config/risk.toml.
A gap is a signal, not locked profit.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import paper_cycle  # noqa: E402
from src.book import record as record_books  # noqa: E402
from src.podlib import (  # noqa: E402
    BOOKS_PATH,
    MARKETS_PATH,
    RISK_PATH,
    SESSIONS_PATH,
    append_jsonl,
    load_risk,
    pin_problems,
    utc_now,
)
from src.scanner import fetch_markets, persist_markets, visible_rows  # noqa: E402

BOOK_LIMIT = 5


def run(
    *,
    raw_markets: list[dict] | None = None,
    book_getter=None,
    markets_path: Path = MARKETS_PATH,
    books_path: Path = BOOKS_PATH,
    bus_path: Path | None = None,
    sessions_path: Path = SESSIONS_PATH,
    now: str | None = None,
    cfg: dict | None = None,
    book_limit: int = BOOK_LIMIT,
) -> dict[str, Any]:
    before = RISK_PATH.read_bytes()
    loaded = cfg if cfg is not None else load_risk()
    empty = {"markets": 0, "books": 0, "gaps": 0, "stale": 0, "fills": 0}
    if pin_problems(loaded):
        _assert_risk_unchanged(before)
        return {"ok": False, "reason": "phase0_pin", **empty}
    ts = now or utc_now()
    try:
        raw = fetch_markets() if raw_markets is None else raw_markets
    except Exception:
        _assert_risk_unchanged(before)
        return {"ok": False, "reason": "public_read_failed", **empty}
    rows = visible_rows(raw, ts)
    if not rows:
        _assert_risk_unchanged(before)
        return {"ok": False, "reason": "no_markets", **empty}
    market_count = persist_markets(rows, markets_path, bus_path)
    book_count = record_books(
        rows[:book_limit],
        ts,
        books_path=books_path,
        bus_path=bus_path,
        getter=book_getter,
    )
    if book_count < 1:
        _assert_risk_unchanged(before)
        return {
            "ok": False,
            "reason": "no_books",
            "markets": market_count,
            "books": 0,
            "gaps": 0,
            "stale": 0,
            "fills": 0,
        }
    cycle = paper_cycle.run(
        markets_path=markets_path,
        books_path=books_path,
        bus_path=bus_path,
        now=ts,
        cfg=loaded,
    )
    if cycle["fills"] != 0:
        raise RuntimeError("paper session refused a fill")
    _assert_risk_unchanged(before)
    summary = {
        "ok": bool(cycle["ok"]),
        "reason": "signals_only" if cycle["ok"] else cycle["reason"],
        "markets": market_count,
        "books": book_count,
        "gaps": cycle["gaps"],
        "stale": cycle["stale"],
        "fills": 0,
    }
    if summary["ok"]:
        append_jsonl(
            sessions_path,
            {
                "ts": ts,
                "mode": "paper",
                "live_enabled": False,
                "markets": market_count,
                "books": book_count,
                "gaps": cycle["gaps"],
                "stale": cycle["stale"],
                "fills": 0,
                "orders": "none",
                "keys": "none",
            },
        )
    return summary


def _assert_risk_unchanged(before: bytes) -> None:
    if RISK_PATH.read_bytes() != before:
        raise RuntimeError("risk.toml changed during paper session")


def main() -> int:
    result = run()
    print(
        f"ok={str(result['ok']).lower()} reason={result['reason']} "
        f"markets={result['markets']} books={result['books']} "
        f"gaps={result['gaps']} stale={result['stale']} fills={result['fills']} "
        "orders=none"
    )
    return 0 if result["ok"] and result["fills"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
