"""PR #310 review (Poly风控): maker.quote buy path must fail closed when the ledger is unusable.

opposite_leg_blocked returns False when open_positions is None and ledger_problems /
non_finite_ledger_rows are set (it defers to RiskGate evaluate's invalid_risk_state).
maker.quote only calls opposite_leg_blocked, so without an extra check a damaged ledger
would allow a buy quote to be written (fail open). Expected: QuoteRefused(structural_arb_disabled),
nothing written. Offline: tmp_path only, no network, no orders.
"""
from __future__ import annotations

import json
import math

import pytest

import src.maker as maker
import src.paper_ledger as paper_ledger
import src.risk_gate as risk_gate
from src.podlib import read_jsonl, utc_now
from tests.test_237b_maker_interleave import STRUCTURAL, _books, _markets
from tests.test_single_leg_exposure import MARKET, YES, _ledger


def _buy(tmp_path, token=YES, quotes_path=None):
    path = quotes_path or (tmp_path / "quotes.jsonl")
    return maker.quote(
        market=MARKET,
        token_id=token,
        side="buy",
        price=0.40,
        size=5,
        bus_path=tmp_path / "bus.jsonl",
        quotes_path=path,
        books_path=_books(tmp_path),
        markets_path=_markets(tmp_path),
    )


def _open_buys(path, token):
    if not path.exists():
        return []
    return [
        q
        for q in read_jsonl(path)
        if q.get("token_id") == token and q.get("side") == "buy" and q.get("status") == "open"
    ]


def _non_finite_fee_row() -> dict:
    return {
        "ts": utc_now(),
        "market": MARKET,
        "token_id": "tok-other",
        "side": "buy",
        "shares": 1.0,
        "notional_usd": 0.4,
        "fee_usd": math.nan,
        "hypothetical": False,
    }


def test_maker_buy_refuses_when_ledger_has_non_finite_rows(tmp_path, monkeypatch):
    """Damaged ledger (non_finite fee) -> load_state sets open_positions None + non_finite_ledger_rows."""
    _ledger(tmp_path, monkeypatch, [_non_finite_fee_row()])
    quotes = tmp_path / "quotes.jsonl"
    state = paper_ledger.load_state(MARKET, YES)
    assert state.get("open_positions") is None
    assert state.get("non_finite_ledger_rows") or state.get("ledger_problems")
    # Documents the gap: opposite_leg_blocked alone would fail-open.
    assert risk_gate.opposite_leg_blocked(state) is False
    with pytest.raises(maker.QuoteRefused) as info:
        _buy(tmp_path, YES, quotes)
    assert info.value.reason == STRUCTURAL
    assert _open_buys(quotes, YES) == []
    assert not quotes.exists() or quotes.read_text(encoding="utf-8") == ""


def test_maker_buy_refuses_when_load_state_reports_ledger_problems(tmp_path, monkeypatch):
    """Explicit ledger_problems (mock) still refuses even if opposite_leg fields look clean."""
    _ledger(tmp_path, monkeypatch, [])
    quotes = tmp_path / "quotes.jsonl"
    real = paper_ledger.load_state

    def damaged(market, token_id="", *, clock_ok=False, quotes_path=None):
        state = dict(real(market, token_id, clock_ok=clock_ok, quotes_path=quotes_path))
        state["open_positions"] = None
        state["ledger_problems"] = ["missing_fill_id:1"]
        state["opposite_leg_shares"] = 0.0
        state["opposite_pending_buys"] = 0
        return state

    monkeypatch.setattr(paper_ledger, "load_state", damaged)
    assert risk_gate.opposite_leg_blocked(damaged(MARKET, YES)) is False
    with pytest.raises(maker.QuoteRefused) as info:
        _buy(tmp_path, YES, quotes)
    assert info.value.reason == STRUCTURAL
    assert _open_buys(quotes, YES) == []


def test_control_healthy_ledger_still_writes_buy_quote(tmp_path, monkeypatch):
    _ledger(tmp_path, monkeypatch, [])
    quotes = tmp_path / "quotes.jsonl"
    msg = _buy(tmp_path, YES, quotes)
    assert msg["payload"]["status"] == "open"
    assert len(_open_buys(quotes, YES)) == 1
