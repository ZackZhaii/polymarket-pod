"""PR #342 x #359 combined tree: a 10**400 fee input must still fail closed (Poly风控 draft, not pushed).

ledger_store._finite returns None for an int too large for a float (PR #342), so _fee_problems may not rely on
an OverflowError. Copy to tests/test_342x359_fee_overflow_combined.py on the head that carries both PRs.
"""
from __future__ import annotations

import json

import pytest

from src import halt_store, ledger_store, paper_ledger
from src.podlib import load_risk
from src.risk_gate import Intent

HUGE = 10**400
ROW = {"ts": "2026-10-16T08:00:00Z", "fill_id": "f", "market": "m", "token_id": "yes", "side": "buy", "shares": 10.0,
       "notional_usd": 5.0, "fee_usd": 0.5, "fee_rate": 0.1, "market_price": 0.5, "effective_price": 0.5,
       "hypothetical": False, "counts_toward_risk": True, "risk_decision": "allow", "cash_after_usd": 994.5,
       "leg_shares_after": 10.0, "leg_cost_basis_after_usd": 5.0}
CASES = [pytest.param(f, s * HUGE, id=f"{f}-{'plus' if s > 0 else 'minus'}")
         for f in ("fee_rate", "market_price", "effective_price") for s in (1, -1)]


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(halt_store, "HALT_STATE_PATH", tmp_path / "halt_state.json")
    monkeypatch.setattr(halt_store, "HALT_FALLBACK_PATH", tmp_path / "HALT")
    monkeypatch.setattr(paper_ledger, "PAPER_PATH", tmp_path / "paper.jsonl")
    monkeypatch.setattr(paper_ledger, "QUOTES_PATH", tmp_path / "quotes.jsonl")
    monkeypatch.setattr(paper_ledger, "bus", lambda *a, **k: None)


def _write(field, value):
    row = dict(ROW, **{field: value})
    paper_ledger.PAPER_PATH.write_text(json.dumps(row) + "\n")
    return paper_ledger.PAPER_PATH.read_bytes()


def _intent():
    return Intent(strategy="regress-342x359", market="m2", side="buy", model_prob=0.7, market_price=0.4,
                  assumed_slippage=0.005, fee_rate=0.02, liquidity_usd=20000.0, spread=0.01, token_id="t",
                  book_ts=paper_ledger.utc_now(), tags=("test-market",), min_order_size=5.0)


def test_clean_ledger_control_still_allows():
    before = _write("fee_rate", 0.1)
    result = paper_ledger.commit_if_allowed(_intent(), load_risk())
    assert result["allow"] is True and paper_ledger.PAPER_PATH.read_bytes() != before


@pytest.mark.parametrize("field,value", CASES)
def test_overflow_fee_input_is_invalid_fee(field, value):
    _write(field, value)
    _, ledger = ledger_store.read_ledger(paper_ledger.PAPER_PATH)
    assert ledger_store.verify(ledger) == ["invalid_fee:f"]
    state = paper_ledger.load_state("m2", "t")
    assert state["daily_loss_usd"] is None and state["ledger_problems"] == ["invalid_fee:f"]


@pytest.mark.parametrize("field,value", CASES)
def test_overflow_fee_input_refuses_the_next_commit_and_books_nothing(field, value):
    before = _write(field, value)
    result = paper_ledger.commit_if_allowed(_intent(), load_risk())
    assert result["allow"] is False and "invalid_risk_state" in result["decision"]["reasons"]
    assert paper_ledger.PAPER_PATH.read_bytes() == before
