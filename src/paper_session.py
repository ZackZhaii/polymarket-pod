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

Task DR (#305): the pin check runs first, before anything reads risk.toml, through
src.pins.refuse_start (like m2_runner / paper_cycle / book): a missing or unreadable file,
a directory, or a sha computation that raises is risk_toml_pin_drift with a global halt
record and ok=False, never an uncaught exception. The unchanged-file snapshot (before),
the loaded config and the pin check all use the same path (risk_path, else RISK_PATH).

Issue #75 criterion 6: a cycle round refused on its input (input_invalid / input_pending)
propagates to the caller as ok=False with that reason and the cycle's input_problems (path,
line, problem code only), with zero gaps and fills and no success sessions record.
"""
from __future__ import annotations

import hashlib
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import paper_cycle, scanner  # noqa: E402
from src.book import record as record_books  # noqa: E402
from src.pins import (  # noqa: E402 - Task DH (#233), Task DR (#305)
    PIN_HALT_REASON,
    PINNED_RISK_SHA256,
    record_drift,
    refuse_start,
)
from src.podlib import (  # noqa: E402
    BOOKS_PATH,
    MARKETS_PATH,
    RISK_PATH,
    SESSIONS_PATH,
    append_jsonl,
    load_risk,  # noqa: F401 - kept importable as paper_session.load_risk (tests build cfg from it)
    parse_risk_toml,
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

    Task DR (#305): that check is the very first step (nothing reads risk.toml before it) and
    goes through pins.refuse_start, so a missing / unreadable file or a check that raises is
    drift too (halt recorded, ok=False). before, the loaded config and the check share one path.
    """
    path = Path(risk_path) if risk_path is not None else Path(RISK_PATH)
    empty = {"markets": 0, "books": 0, "gaps": 0, "stale": 0, "fills": 0}
    refused = {"ok": False, "reason": PIN_HALT_REASON, **empty}
    # Full file pins first (sha + every key), before any risk.toml read; then the in-memory five-pin check.
    if refuse_start("paper_session", risk_path=path, bus_path=bus_path):
        return refused
    reread = _pinned_snapshot(path)
    if isinstance(reread, list):  # the file went away or changed between the check and this read: still drift
        record_drift("paper_session", reread, phase="start", bus_path=bus_path)
        return refused
    before = reread
    try:
        loaded = cfg if cfg is not None else parse_risk_toml(before.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - pinned bytes that do not parse: drift (fail closed)
        record_drift("paper_session", [f"risk_toml_unparseable:{type(exc).__name__}"], phase="start",
                     bus_path=bus_path)
        return refused
    if pin_problems(loaded):
        _assert_risk_unchanged(path, before)
        return {"ok": False, "reason": "phase0_pin", **empty}
    try:
        limits = scanner.thresholds_from(loaded)
    except RuntimeError:
        _assert_risk_unchanged(path, before)
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
        _assert_risk_unchanged(path, before)
        return {"ok": False, "reason": "public_read_failed", **empty}
    fetch = result.get("fetch") or {}
    if fetch.get("complete") is not True:
        _assert_risk_unchanged(path, before)
        return {"ok": False, "reason": "markets_incomplete", **empty}
    if result["scanned"] < SESSION_MIN_SCANNED:
        _assert_risk_unchanged(path, before)
        return {"ok": False, "reason": "too_few_markets", **empty}
    rows = result["rows"]
    if not rows:
        _assert_risk_unchanged(path, before)
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
        _assert_risk_unchanged(path, before)
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
    _assert_risk_unchanged(path, before)
    summary = {
        "ok": bool(cycle["ok"]),
        "reason": "signals_only" if cycle["ok"] else cycle["reason"],
        "markets": market_count,
        "books": book_count,
        "gaps": cycle["gaps"],
        "stale": cycle["stale"],
        "fills": 0,
        # Issue #75 criterion 6: the refusal of an input-refused round (input_invalid /
        # input_pending) reaches the caller with the path / line / problem code it reported.
        "input_problems": list(cycle.get("input_problems") or []),
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


def _pinned_snapshot(path: Path) -> bytes | list[str]:
    """Task DR (#305): the bytes of the just-checked risk file, or drift problems. Never raises."""
    try:
        raw = path.read_bytes()
        if hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest() != PINNED_RISK_SHA256:
            return ["risk_sha256_not_pinned"]
        return raw
    except OSError as exc:
        return [f"risk_toml_unreadable:{type(exc).__name__}"]
    except Exception as exc:  # noqa: BLE001 - a broken re-read is drift (fail closed)
        return [f"pin_check_failed:{type(exc).__name__}"]


def _assert_risk_unchanged(path: Path, before: bytes) -> None:
    """Task DR (#305): compares the same file the pin check read (risk_path, else RISK_PATH)."""
    if Path(path).read_bytes() != before:
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
