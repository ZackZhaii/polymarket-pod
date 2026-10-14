"""Poly风控 on #310 (Task DO): maker.quote's buy path checked the opposite-leg veto on paper_ledger.load_state, which
read only the module-level QUOTES_PATH, while the pair check and the write use ``quotes_path or QUOTES_PATH``. With an
explicit quotes_path != QUOTES_PATH, an own open NO buy in that file did not block a YES buy (fail open).
Expected (tightening): the veto sees the same quotes file the quote is written to; nothing is written.
Offline: tmp_path files only, no network, no orders.
"""
from __future__ import annotations

import json

import pytest

import src.maker as maker
import src.paper_ledger as paper_ledger
from src.podlib import read_jsonl
from tests.test_237b_maker_interleave import STRUCTURAL, _books, _markets
from tests.test_single_leg_exposure import MARKET, NO, YES, _ledger


def _open_buy_row(token: str) -> dict:
    return {"ts": "2026-10-14T00:00:00.000Z", "market": MARKET, "token_id": token, "side": "buy", "price": 0.40,
            "size": 5, "notional_usd": 2.0, "status": "open", "venue": "paper", "exchange_action": "none"}


def _write_quotes(path, rows) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _buy(tmp_path, token, quotes_path):
    return maker.quote(market=MARKET, token_id=token, side="buy", price=0.40, size=5,
                       bus_path=tmp_path / "bus.jsonl", quotes_path=quotes_path,
                       books_path=_books(tmp_path), markets_path=_markets(tmp_path))


def _open_buys(path, token):
    if not path.exists():
        return []
    return [q for q in read_jsonl(path) if q.get("token_id") == token and q.get("side") == "buy"
            and q.get("status") == "open"]


def test_opposite_open_buy_in_explicit_quotes_path_refuses_buy(tmp_path, monkeypatch):
    _ledger(tmp_path, monkeypatch, [])  # QUOTES_PATH -> tmp_path/quotes.jsonl (absent: no quotes there)
    alt = tmp_path / "alt_quotes.jsonl"
    assert alt != paper_ledger.QUOTES_PATH
    _write_quotes(alt, [_open_buy_row(NO)])
    with pytest.raises(maker.QuoteRefused) as info:
        _buy(tmp_path, YES, alt)
    assert info.value.reason == STRUCTURAL
    assert _open_buys(alt, YES) == []
    assert len(read_jsonl(alt)) == 1  # nothing written
    assert not paper_ledger.QUOTES_PATH.exists()


def test_opposite_open_buy_in_default_quotes_still_refuses_buy_to_explicit_path(tmp_path, monkeypatch):
    """Tightening only: the default quotes file (the one commit_if_allowed reads) still vetoes too."""
    _ledger(tmp_path, monkeypatch, [])
    _write_quotes(paper_ledger.QUOTES_PATH, [_open_buy_row(NO)])
    alt = tmp_path / "alt_quotes.jsonl"
    with pytest.raises(maker.QuoteRefused) as info:
        _buy(tmp_path, YES, alt)
    assert info.value.reason == STRUCTURAL
    assert _open_buys(alt, YES) == []


def test_load_state_reads_given_quotes_path(tmp_path, monkeypatch):
    _ledger(tmp_path, monkeypatch, [])
    alt = tmp_path / "alt_quotes.jsonl"
    _write_quotes(alt, [_open_buy_row(NO)])
    assert paper_ledger.load_state(MARKET, YES)["opposite_pending_buys"] == 0
    assert paper_ledger.load_state(MARKET, YES, quotes_path=alt)["opposite_pending_buys"] == 1


# --- control (expected GREEN before and after) ----------------------------------------------------------

def test_control_explicit_quotes_path_empty_market_is_written(tmp_path, monkeypatch):
    _ledger(tmp_path, monkeypatch, [])
    alt = tmp_path / "alt_quotes.jsonl"
    assert _buy(tmp_path, YES, alt)["payload"]["status"] == "open"
    assert len(_open_buys(alt, YES)) == 1
