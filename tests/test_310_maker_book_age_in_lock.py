"""PR #310 review (Poly风控, P2): maker.quote re-checks book age inside the commit lock.

The book is 119 s old (fresh at MAX_BOOK_AGE_S=120) at the pre-lock check; waiting for commit.lock moves the
clock to 121 s. The buy must be refused with the staleness reason and the quotes file must get no new line.
Offline: tmp_path only, no network, no orders.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import src.halt_store as halt_store
import src.maker as maker
from src.book_staleness import MAX_BOOK_AGE_S, book_age_problem
from tests.test_237b_maker_interleave import _markets
from tests.test_single_leg_exposure import MARKET, NO, YES, _ledger

T0 = datetime(2026, 10, 14, 12, 0, 0, tzinfo=timezone.utc)
BOOK_TS = T0 - timedelta(seconds=119)


def _iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def _books(tmp_path):
    path = tmp_path / "books.jsonl"
    rows = [{"ts": _iso(BOOK_TS), "book_ts": _iso(BOOK_TS), "market_id": MARKET, "token_id": t,
             "best_ask": 0.41, "best_bid": 0.39} for t in (YES, NO)]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


def _setup(tmp_path, monkeypatch, wait_s):
    assert MAX_BOOK_AGE_S == 120
    _ledger(tmp_path, monkeypatch, [])
    clock = {"now": T0}
    monkeypatch.setattr(maker, "book_age_problem",
                        lambda ts, now, limit: book_age_problem(ts, clock["now"], limit))
    real_acquire = halt_store.StateLock.acquire

    def slow_acquire(self, *args, **kwargs):
        got = real_acquire(self, *args, **kwargs)
        if str(self.path).endswith(".commit.lock"):
            clock["now"] = T0 + timedelta(seconds=wait_s)  # the wait for the lock
        return got

    monkeypatch.setattr(halt_store.StateLock, "acquire", slow_acquire)
    quotes = tmp_path / "quotes.jsonl"
    quotes.write_text("", encoding="utf-8")
    return quotes


def _buy(tmp_path, quotes):
    return maker.quote(market=MARKET, token_id=YES, side="buy", price=0.40, size=5,
                       bus_path=tmp_path / "bus.jsonl", quotes_path=quotes,
                       books_path=_books(tmp_path), markets_path=_markets(tmp_path))


def test_book_goes_stale_while_waiting_for_commit_lock_refuses(tmp_path, monkeypatch):
    quotes = _setup(tmp_path, monkeypatch, wait_s=2)  # 119 s -> 121 s
    assert book_age_problem(_iso(BOOK_TS), T0, MAX_BOOK_AGE_S) is None
    stale = book_age_problem(_iso(BOOK_TS), T0 + timedelta(seconds=2), MAX_BOOK_AGE_S)
    assert stale is not None
    with pytest.raises(maker.QuoteRefused) as info:
        _buy(tmp_path, quotes)
    assert info.value.reason == stale
    assert quotes.read_text(encoding="utf-8") == ""


def test_control_book_still_fresh_after_lock_writes(tmp_path, monkeypatch):
    quotes = _setup(tmp_path, monkeypatch, wait_s=0)
    msg = _buy(tmp_path, quotes)
    assert msg["payload"]["status"] == "open"
    assert len(quotes.read_text(encoding="utf-8").splitlines()) == 1
