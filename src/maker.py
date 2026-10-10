"""Maker stub. Records a paper quote signal. Does not post or cancel on an exchange.

Task AL (Sentinel design gap 1): no quote while a halt covers the market (market or global
scope, a bad halt state file or logs/HALT). The halt is checked again after the append; if one
landed in between, the Maker cancels its own quote atomically and refuses, so a Sentinel cancel
can never be undone by a racing quote.

Issue #125 (Task BN): no quote without a usable book for the quoted leg. The latest recorded book row
for (market, token_id) (by the recorder's ts, book_staleness.latest_by_time) must exist and its exchange
book_ts must pass book_staleness.book_age_problem at MAX_BOOK_AGE_S (the RiskGate / Sentinel rule, no new
threshold); a missing, unreadable, future-dated or stale book refuses the quote (QuoteRefused, with the
reason). Books come from ``books_path`` (default data/books.jsonl) or, with ``recording_dir``, from that M1
run's rotated book files; then the market must also be in the run's manifest (paper_cycle.recording_markets:
a missing / bad / empty manifest refuses every quote, an unlisted market is ``not_recorded``). Nothing is
persisted for this; the scope is read from the manifest the run already writes.
"""
from __future__ import annotations

import math
from pathlib import Path

from src import halt_store, sentinel
from src.book_staleness import INVALID_BOOK_TS, MISSING_BOOK_TS, book_age_problem, latest_by_time
from src.podlib import BOOKS_PATH, PAPER_EQUITY_USD, QUOTES_PATH, append_jsonl, bus, read_jsonl, utc_now
from src.thresholds import MAX_BOOK_AGE_S

MISSING_BOOK = "missing_book"  # no recorded book row for the quoted leg (same word as paper_cycle)
BOOKS_UNREADABLE = "books_unreadable"


class HaltActive(RuntimeError):
    """A halt covers this market; no paper quote."""

    def __init__(self, market: str, reasons: list[str]) -> None:
        self.reasons = reasons
        super().__init__(f"halt active for {market!r}: {', '.join(reasons)}")


class QuoteRefused(RuntimeError):
    """Issue #125: no usable book (or not in the recording's scope) for this leg; no paper quote."""

    def __init__(self, market: str, token_id: str, reason: str) -> None:
        self.reason = reason
        super().__init__(f"quote refused for {market!r} token {token_id!r}: {reason}")


def book_problem(market: str, token_id: str, *, books_path: Path | None = None,
                 recording_dir: Path | None = None) -> str | None:
    """Issue #125: None when the leg's latest recorded book is fresh, else why the quote is refused."""
    try:
        if recording_dir is not None:
            from src import paper_cycle  # local: paper_cycle is the heavier module

            recorded, problem = paper_cycle.recording_markets(recording_dir)
            if problem is not None:
                return problem
            if market not in recorded:
                return paper_cycle.NOT_RECORDED
            rows, problem = paper_cycle.read_books(Path(recording_dir), recording_dir)
            if problem is not None:
                return problem
        else:
            rows = read_jsonl(books_path if books_path is not None else BOOKS_PATH)
    except Exception:  # noqa: BLE001 - books that cannot be read are no book (fail closed)
        return BOOKS_UNREADABLE
    leg = (str(market), str(token_id))
    chosen, _ = latest_by_time(
        rows, lambda row: "leg" if (str(row.get("market_id") or ""), str(row.get("token_id") or "")) == leg else None)
    book = chosen.get("leg")
    if book is None or not market or not token_id:
        return MISSING_BOOK
    if "book_ts_problem" in book and book["book_ts_problem"] is not None:
        recorded_problem = book["book_ts_problem"]
        return recorded_problem if recorded_problem in (MISSING_BOOK_TS, INVALID_BOOK_TS) else INVALID_BOOK_TS
    return book_age_problem(book.get("book_ts"), None, MAX_BOOK_AGE_S)


def quote(
    *,
    market: str,
    token_id: str,
    side: str,
    price: float,
    size: float,
    bus_path: Path | None = None,
    quotes_path: Path | None = None,
    books_path: Path | None = None,
    recording_dir: Path | None = None,
) -> dict:
    if side not in {"buy", "sell"}:
        raise ValueError("side must be one paper leg")
    if not isinstance(price, (int, float)) or isinstance(price, bool) or not math.isfinite(price):
        raise ValueError("price must be a finite number")
    if not isinstance(size, (int, float)) or isinstance(size, bool) or not math.isfinite(size):
        raise ValueError("size must be a finite number")
    if not 0.0 < price < 1.0:
        raise ValueError("price out of range")
    if size <= 0:
        raise ValueError("size must be positive")
    halted = halt_store.halt_problems(market)
    if halted:
        raise HaltActive(market, halted)
    notional = price * size
    if notional > PAPER_EQUITY_USD * 0.01 + 1e-9:
        raise ValueError("quote exceeds 1% clip")
    refused = book_problem(market, token_id, books_path=books_path, recording_dir=recording_dir)
    if refused is not None:
        raise QuoteRefused(market, token_id, refused)  # Issue #125: fail closed, nothing written
    row = {
        "ts": utc_now(),
        "market": market,
        "token_id": token_id,
        "side": side,
        "price": price,
        "size": size,
        "notional_usd": round(notional, 8),
        "status": "open",
        "venue": "paper",
        "exchange_action": "none",
    }
    path = quotes_path or QUOTES_PATH
    append_jsonl(path, row)
    halted = halt_store.halt_problems(market)
    if halted:
        # A halt landed between the check and the append: cancel this market's open quotes again.
        try:
            sentinel.cancel_open_quotes(path, utc_now(), market)
        except OSError:
            halt_store.write_fallback(f"{utc_now()} maker could not cancel a racing quote market={market}")
        raise HaltActive(market, halted)
    message = bus(
        "Maker",
        "RiskGate",
        "signal",
        market,
        row,
        path=bus_path,
    )
    return message
