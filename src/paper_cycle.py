"""Offline paper cycle.

Reads local market and book jsonl. Emits gap and staleness signals only.
Book age uses src/book_staleness.py, the same parser and boundary as RiskGate, and is read
from the exchange's book_ts (book.book_timestamp), never the recorder's ts (Task T).
A stale market gets no gap signal: the BookWatcher stale signal is its skip record.
The latest market/book row is chosen by parsed time; unparseable rows are skipped and recorded.
Task AA: every market with at least one recorded book is also fed to Sentinel's StaleWatch, whose
streaks persist in logs/sentinel_stale.json (SENTINEL_STATE_PATH, written atomically). Two stale or
failed checks in a row halt that market's paper quotes. A missing (after a prior run), unreadable,
corrupt or malformed state file, or a failed write, counts as one more stale check (fail closed).
This only adds the alarm: the per-check stale signal and the skipped gap signal are unchanged.
Task AL (Sentinel design gaps 1 and 10): a leg with no recorded book row (or a market with no
tokens) is stale (missing_book), so a market without a book is checked by StaleWatch instead of
skipped. A market covered by an active halt (src/halt_store.py: its own, a global one, a bad
halt state file or logs/HALT) gets no gap signal; the skip is recorded as halted_market.
No network, no orders, no paper fills, no edits to config/risk.toml.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import arb_binary, halt_store, neg_risk, sentinel  # noqa: E402
from src.book_staleness import (  # noqa: E402
    INVALID_BOOK_TS,
    INVALID_NOW,
    MISSING_BOOK_TS,
    book_age_problem,
    latest_by_time,
    parse_book_ts,
)
from src.podlib import (  # noqa: E402
    BOOKS_PATH,
    MARKETS_PATH,
    QUOTES_PATH,
    RISK_PATH,
    SENTINEL_STATE_PATH,
    bus,
    load_risk,
    pin_problems,
    read_jsonl,
    utc_now,
)

# Same value as book_staleness.MAX_BOOK_AGE_S (RiskGate). Not a risk.toml key.
from src.thresholds import PAPER_CYCLE_STALE_AFTER_S as STALE_AFTER_S  # noqa: E402 - Task AC

# Task AL gap 10: a leg (or a market) with no recorded book row counts as a stale check.
MISSING_BOOK = "missing_book"


def run(
    *,
    markets_path: Path = MARKETS_PATH,
    books_path: Path = BOOKS_PATH,
    bus_path: Path | None = None,
    now: str | None = None,
    stale_after_s: int = STALE_AFTER_S,
    cfg: dict | None = None,
    sentinel_state_path: Path | None = None,
    quotes_path: Path | None = None,
) -> dict:
    before = RISK_PATH.read_bytes()
    loaded = cfg if cfg is not None else load_risk()
    if pin_problems(loaded):
        _assert_risk_unchanged(before)
        return {"ok": False, "reason": "phase0_pin", "signals": 0, "gaps": 0, "stale": 0, "fills": 0}
    now_s = now or utc_now()
    now_at = parse_book_ts(now_s)
    skips: list[dict] = []
    markets = _latest_by(read_jsonl(markets_path), "id", skips, bus_path)
    books = _latest_books(read_jsonl(books_path), skips, bus_path)
    state_path = sentinel_state_path if sentinel_state_path is not None else SENTINEL_STATE_PATH
    watch, state_problem = sentinel.load_watch(
        state_path,
        bus_path=bus_path,
        quotes_path=quotes_path if quotes_path is not None else QUOTES_PATH,
    )
    checked: list[str] = []
    halts: list[str] = []
    halted_markets: list[str] = []
    gaps = 0
    stale = 0
    for market_id, market in markets.items():
        tokens = [str(token) for token in market.get("token_ids") or []]
        asks: list[float] = []
        depths: list[float | None] = []
        stale_reasons: set[str] = set()
        sentinel_reasons: set[str] = set()
        for token in tokens:
            book = books.get((market_id, token))
            if book is None:
                stale_reasons.add(MISSING_BOOK)
                sentinel_reasons.add(MISSING_BOOK)
                continue
            problem = _row_book_problem(book, now_at, stale_after_s)
            if problem is not None:
                stale_reasons.add(problem)
            # Sentinel judges at its own 120 s too, so a looser stale_after_s never resets a streak.
            sentinel_problem = problem or _row_book_problem(book, now_at, sentinel.SENTINEL_STALE_S)
            if sentinel_problem is not None:
                sentinel_reasons.add(sentinel_problem)
            ask = book.get("best_ask")
            if isinstance(ask, (int, float)) and 0.0 < float(ask) < 1.0:
                asks.append(float(ask))
                size = book.get("best_ask_size")
                depths.append(float(size) if isinstance(size, (int, float)) else None)
        if not tokens:
            stale_reasons.add(MISSING_BOOK)
            sentinel_reasons.add(MISSING_BOOK)
        # Task AL gap 10: every market is checked; a missing book is a stale check, never a skip.
        checked.append(market_id)
        if state_problem is not None:
            _sentinel_record(watch, market_id, state_problem, halts)
        _sentinel_record(watch, market_id, min(sentinel_reasons) if sentinel_reasons else None, halts)
        if stale_reasons:
            bus(
                "BookWatcher",
                "Sentinel",
                "signal",
                market_id,
                {
                    "stale": True,
                    "stale_after_s": stale_after_s,
                    "stale_reasons": sorted(stale_reasons),
                    "gap_signal": "skipped",
                    "skip_reason": "stale_book",
                    "exchange_action": "none",
                },
                path=bus_path,
            )
            skips.append({"market": market_id, "kind": "stale_market", "reason": "stale_book",
                          "stale_reasons": sorted(stale_reasons)})
            stale += 1
            # A stale market gets no gap/spread signal at all (Task T).
            continue
        halted = halt_store.halt_problems(market_id)
        if halted:
            # Task AL gap 1: a halted market (or any market under a global halt) gets no gap signal.
            skips.append({"market": market_id, "kind": "halted_market", "reason": "halt_active",
                          "halt_reasons": halted})
            halted_markets.append(market_id)
            continue
        message = _gap_message(asks, market_id, depths, bus_path)
        if message is not None:
            if message["type"] != "signal":
                raise RuntimeError("paper cycle refused a non-signal")
            gaps += 1
    write_problem = sentinel.save_watch(watch, state_path)
    if write_problem is not None:
        # This run's streaks are lost, so the failed write is one more stale check now (fail closed).
        for market_id in checked:
            _sentinel_record(watch, market_id, write_problem, halts)
    _assert_risk_unchanged(before)
    return {
        "ok": True,
        "reason": "signals_only",
        "signals": gaps + stale,
        "gaps": gaps,
        "stale": stale,
        "fills": 0,
        "skipped": len(skips),
        "skips": skips,
        "halted_markets": halted_markets,
        "sentinel": {
            "checked": len(checked),
            "state_problem": state_problem,
            "write_problem": write_problem,
            "halts": halts,
            "state_path": str(state_path),
        },
    }


def _sentinel_record(watch: sentinel.StaleWatch, market_id: str, problem: str | None, halts: list[str]) -> None:
    result = watch.record(market_id, problem)
    if result["halt"] is not None:
        halts.append(market_id)


def _gap_message(asks: list[float], market_id: str, depths: list[float | None], bus_path: Path | None) -> dict | None:
    depth = min(depths) if depths and all(item is not None for item in depths) else None
    if len(asks) == 2:
        return arb_binary.emit_gap(asks[0], asks[1], market_id, bus_path=bus_path, displayed_depth=depth)
    if len(asks) >= 3:
        return neg_risk.emit_gap(asks, market_id, bus_path=bus_path, displayed_depth=depth)
    return None


def _record_skipped_rows(skipped: list[dict], rows: list[dict], source: str, skips: list[dict],
                         bus_path: Path | None) -> None:
    """Record each row dropped for an unusable ts: one skip entry and one BookWatcher signal."""
    for item in skipped:
        row = rows[item["index"]]
        market = str((row.get("market_id") if source == "books" else row.get("id")) or "")
        token = str(row.get("token_id") or "") if source == "books" else ""
        entry = {"kind": "skipped_row", "source": source, "market": market, "token_id": token, "reason": item["reason"]}
        skips.append(entry)
        bus(
            "BookWatcher",
            "Sentinel",
            "signal",
            market,
            {"skipped_row": True, "source": source, "token_id": token, "reason": item["reason"],
             "exchange_action": "none"},
            path=bus_path,
        )


def _latest_by(rows: list[dict], key: str, skips: list[dict] | None = None,
               bus_path: Path | None = None) -> dict[str, dict]:
    """Latest row per ``key`` by parsed ``ts`` (not string order). Unparseable rows are skipped."""
    chosen, skipped = latest_by_time(rows, lambda row: str(row.get(key) or ""))
    if skips is not None:
        _record_skipped_rows(skipped, rows, "markets", skips, bus_path)
    return chosen


def _latest_books(rows: list[dict], skips: list[dict] | None = None,
                  bus_path: Path | None = None) -> dict[tuple[str, str], dict]:
    """Latest book row per (market, token) by the recorder's parsed ``ts``.

    The newest recording wins even when its book_ts is missing, so a newer snapshot with
    no usable book time makes the leg stale instead of letting an older one look fresh.
    """

    def _key(row: dict) -> tuple[str, str] | None:
        pair = (str(row.get("market_id") or ""), str(row.get("token_id") or ""))
        return pair if pair[0] and pair[1] else None

    chosen, skipped = latest_by_time(rows, _key)
    if skips is not None:
        _record_skipped_rows(skipped, rows, "books", skips, bus_path)
    return chosen


def _row_book_problem(book: dict, now_at: datetime | None, stale_after_s: object) -> str | None:
    """Book age from the exchange's book_ts on the row, never the recorder ts (Task T).

    A recorded book_ts_problem always counts as stale: missing_book_ts / invalid_book_ts as
    recorded, any other value as invalid_book_ts. No book_ts key is missing_book_ts.
    """
    if now_at is None:
        return INVALID_NOW
    if "book_ts_problem" in book and book["book_ts_problem"] is not None:
        problem = book["book_ts_problem"]
        return problem if problem in (MISSING_BOOK_TS, INVALID_BOOK_TS) else INVALID_BOOK_TS
    return _book_problem(book.get("book_ts"), now_at, stale_after_s)


def _book_problem(ts: object, now_at: datetime | None, stale_after_s: object) -> str | None:
    """None when the book is fresh, else the book_staleness reason (RiskGate's parser and boundary).

    Fresh means 0 <= age <= stale_after_s, so exactly 120 s is fresh and a future stamp is not.
    Missing or unparseable values, an unparseable ``now`` and a broken limit all count as stale.
    ``now`` never falls back to the wall clock here: book_age_problem reads the clock when given None.
    """
    if now_at is None:
        return INVALID_NOW
    return book_age_problem(ts, now_at, stale_after_s)


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
