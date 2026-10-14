"""One public paper session.

Reads Gamma through the current scanner path, records public CLOB /book snapshots, then
runs the offline signal cycle. No .env, no orders, no paper fills, no edits to config/risk.toml.
A gap is a signal, not locked profit.

Task AB: markets come from scanner.scan (keyset paging, risk.toml thresholds via
thresholds_from, version-aware outcome IDs, non_finite_* and expiry rejections), not the
legacy single unfiltered page. An incomplete fetch, fewer than SESSION_MIN_SCANNED scanned
markets, or a bad threshold writes nothing (fail closed, like scanner exits 1-3).
Book age is judged by paper_cycle from each row's book_ts (the CLOB /book timestamp, never
the recorder ts) against an evaluation clock read after the books are recorded, not the
session start stamp. A missing, unparseable or future book_ts stays stale.

Task DH (#233): start also verifies the full committed risk.toml sha256 and
RISK_FILE_PINS via src.pins.risk_file_problems (not only podlib.PINS); drift
refuses before any markets/books/sessions write.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import paper_cycle, scanner  # noqa: E402
from src.book import record as record_books  # noqa: E402
from src.pins import record_drift, risk_file_problems  # noqa: E402 - Task DH (#233)
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

BOOK_LIMIT = 5
# Same floor as scanner.main (EXIT_TOO_FEW_SCANNED): fewer public markets means a broken read.
from src.thresholds import MIN_SCANNED_MARKETS as SESSION_MIN_SCANNED  # noqa: E402 - Task AC


def eval_clock() -> str:
    """Wall-clock UTC with milliseconds, the same precision as a CLOB /book timestamp."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def run(
    *,
    raw_markets: list[dict] | None = None,
    book_getter=None,
    market_getter=None,
    markets_path: Path = MARKETS_PATH,
    books_path: Path = BOOKS_PATH,
    bus_path: Path | None = None,
    sessions_path: Path = SESSIONS_PATH,
    now: str | None = None,
    cfg: dict | None = None,
    book_limit: int = BOOK_LIMIT,
    clock: Callable[[], str] | None = None,
    max_markets: int = scanner.MAX_MARKETS,
    risk_path: Path | None = None,
) -> dict[str, Any]:
    """now stamps the session (market rows and the recorder ts on book rows).

    clock is read once, after the books are recorded, and is the time paper_cycle judges
    book age against. It defaults to the wall clock; tests inject it.

    Task DH (#233): before any public read or write, verify the committed risk.toml
    sha256 and the full RISK_FILE_PINS set via src.pins.risk_file_problems — the same
    check m2_runner / paper_cycle / book already use. Pre-start drift refuses with
    reason=risk_toml_pin_drift and writes nothing.
    """
    before = RISK_PATH.read_bytes()
    loaded = cfg if cfg is not None else load_risk()
    empty = {"markets": 0, "books": 0, "gaps": 0, "stale": 0, "fills": 0}
    # Full file pins first (sha + every key); then the in-memory five-pin check.
    drift = risk_file_problems(risk_path)
    if drift:
        record_drift("paper_session", drift, phase="start", bus_path=bus_path)
        _assert_risk_unchanged(before)
        return {"ok": False, "reason": "risk_toml_pin_drift", **empty}
    if pin_problems(loaded):
        _assert_risk_unchanged(before)
        return {"ok": False, "reason": "phase0_pin", **empty}
    try:
        limits = scanner.thresholds_from(loaded)
    except RuntimeError:
        _assert_risk_unchanged(before)
        return {"ok": False, "reason": "invalid_scanner_threshold", **empty}
    ts = now or utc_now()
    try:
        result = scanner.scan(
            raw_markets=raw_markets,
            getter=market_getter,
            limits=limits,
            ts=ts,
            max_markets=max_markets,
        )
    except Exception:
        _assert_risk_unchanged(before)
        return {"ok": False, "reason": "public_read_failed", **empty}
    fetch = result.get("fetch") or {}
    if fetch.get("complete") is not True:
        _assert_risk_unchanged(before)
        return {"ok": False, "reason": "markets_incomplete", **empty}
    if result["scanned"] < SESSION_MIN_SCANNED:
        _assert_risk_unchanged(before)
        return {"ok": False, "reason": "too_few_markets", **empty}
    rows = result["rows"]
    if not rows:
        _assert_risk_unchanged(before)
        return {"ok": False, "reason": "no_markets", **empty}
    market_count = scanner.persist_markets(rows, markets_path, bus_path, bus_limit=scanner.BUS_TOP_N)
    book_rows = [row for row in rows if row.get("token_ids")][:book_limit]
    book_count = record_books(
        book_rows,
        ts,
        books_path=books_path,
        bus_path=bus_path,
        getter=book_getter,
    )
    if book_count < 1:
        _assert_risk_unchanged(before)
        return {**empty, "ok": False, "reason": "no_books", "markets": market_count}
    evaluated_at = (clock or eval_clock)()
    cycle = paper_cycle.run(
        markets_path=markets_path,
        books_path=books_path,
        bus_path=bus_path,
        now=evaluated_at,
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
        "scanned": result["scanned"],
        "evaluated_at": evaluated_at,
    }
    if summary["ok"]:
        append_jsonl(
            sessions_path,
            {
                "ts": ts,
                "evaluated_at": evaluated_at,
                "mode": "paper",
                "live_enabled": False,
                "scanned": result["scanned"],
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


EXIT_OK = 0
EXIT_FAILED = 1  # the session did not run cleanly (pins, scan) or booked a fill (Phase 0 books none)


def main() -> int:
    result = run()
    print(
        f"ok={str(result['ok']).lower()} reason={result['reason']} "
        f"markets={result['markets']} books={result['books']} "
        f"gaps={result['gaps']} stale={result['stale']} fills={result['fills']} "
        "orders=none"
    )
    return EXIT_OK if result["ok"] and result["fills"] == 0 else EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
