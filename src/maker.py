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

from src import halt_store, sentinel, wash_detect
from src.book_staleness import INVALID_BOOK_TS, MISSING_BOOK_TS, book_age_problem, latest_by_time
from src.podlib import PAPER_EQUITY_USD, QUOTES_PATH, append_jsonl, bus, utc_now
from src.podlib import BOOKS_PATH, MARKETS_PATH, read_jsonl
from src.risk_gate import opposite_leg_blocked
from src.structural_gate import STRUCTURAL_ARB_DISABLED
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


def pair_problem(market: str, token_id: str, side: str, price: float, *, quotes_path: Path,
                 markets_path: Path | None = None) -> str | None:
    """Issue #145 (Task CH): the same complementary-leg check RiskGate runs (wash_detect.pair_cross_problems).

    A new quote that would mint-pair (buy) or merge-pair (sell) with our own resting quote on the other token of
    the market is refused, and so is one whose pairing cannot be decided (quotes not readable in full, the
    market's two tokens unknown): fail closed, nothing written. Returns the reason or None.
    """
    from src import paper_ledger  # local: paper_ledger imports much more than the Maker needs at import time

    quotes, problems = paper_ledger.read_quotes(Path(quotes_path))
    if problems or quotes is None:
        return wash_detect.WASH_INPUT_INVALID
    pair = None
    if wash_detect.pair_quotes(market, token_id, quotes):
        try:
            rows = read_jsonl(Path(markets_path) if markets_path is not None else MARKETS_PATH)
        except (OSError, UnicodeDecodeError):
            rows = []
        found = [row for row in rows if isinstance(row, dict) and str(row.get("id")) == str(market)]
        pair = wash_detect.binary_pair(found[-1]) if found else None
    reasons = wash_detect.pair_cross_problems(market=market, token_id=token_id, side=side, price=price,
                                             quotes=quotes, market_pair=pair)
    return reasons[0] if reasons else None


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
    markets_path: Path | None = None,
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
    path = quotes_path or QUOTES_PATH
    if side == "buy":
        # Issue #237 (Task DO): the opposite-leg veto runs under the ledger's commit lock (outer), the quotes lock
        # (append_jsonl) inside it, the same order as paper_ledger.commit_if_allowed. Lock or state trouble refuses.
        from src import paper_ledger

        lock = halt_store.StateLock(paper_ledger.PAPER_PATH)
        lock.path = Path(str(paper_ledger.PAPER_PATH) + ".commit.lock")
        try:
            acquired = lock.acquire(timeout_s=paper_ledger.COMMIT_LOCK_TIMEOUT_S)
        except Exception:  # noqa: BLE001 - a lock that cannot be taken refuses (fail closed)
            acquired = False
        if not acquired:
            raise QuoteRefused(market, token_id, STRUCTURAL_ARB_DISABLED)
        try:
            paired = pair_problem(market, token_id, side, price, quotes_path=path, markets_path=markets_path)
            if paired is not None:
                raise QuoteRefused(market, token_id, paired)  # Issue #145 reason kept first
            # Poly风控 on #310: the veto reads the quotes file this quote is written to, and the default
            # QUOTES_PATH too when they differ (the one commit_if_allowed reads). Either blocking refuses.
            veto_paths = [Path(path)]
            if Path(paper_ledger.QUOTES_PATH) != Path(path):
                veto_paths.append(Path(paper_ledger.QUOTES_PATH))
            try:
                blocked = any(opposite_leg_blocked(paper_ledger.load_state(market, token_id, quotes_path=p))
                              for p in veto_paths)
            except Exception:  # noqa: BLE001 - an unreadable state refuses (fail closed)
                blocked = True
            if blocked:
                raise QuoteRefused(market, token_id, STRUCTURAL_ARB_DISABLED)
            return _write_quote(market, token_id, side, price, size, notional, path, markets_path, bus_path)
        finally:
            lock.release()
    return _write_quote(market, token_id, side, price, size, notional, path, markets_path, bus_path)


def _write_quote(market: str, token_id: str, side: str, price: float, size: float, notional: float, path: Path,
                 markets_path: Path | None, bus_path: Path | None) -> dict:
    paired = pair_problem(market, token_id, side, price, quotes_path=path, markets_path=markets_path)
    if paired is not None:
        raise QuoteRefused(market, token_id, paired)  # Issue #145: would pair with our own resting quote
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
