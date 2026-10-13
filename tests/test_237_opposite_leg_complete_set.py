"""Issue #237: after only the YES leg of a binary market filled, a buy of the same market's NO completes the
full set leg by leg and gets around #141. RiskGate must reject it as structural_arb_disabled.
Offline paper ledger only. No network, no credentials, no orders.

Ledger rows carry an old ts (2026-01-01) so wash_same_market (WASH_WINDOW_S=300) and today's daily loss do not
veto first and hide the rule (same as tests/test_226_reverse_cross_min_order.py).
"""
from __future__ import annotations

import pytest

import src.paper_ledger as paper_ledger
from src.podlib import load_risk
from src.risk_gate import evaluate
from tests.test_single_leg_exposure import MARKET, NO, YES, _intent, _ledger, _row

OLD_TS = "2026-01-01T00:00:00Z"
STRUCTURAL = "structural_arb_disabled"


def _old(token: str, side: str, shares: float, notional: float, market: str = MARKET) -> dict:
    return _row(market, token, side, shares, notional, ts=OLD_TS)


def _buy_no(**kw):
    # Poly套利 minimal vector (cf01ae1): NO at 0.40, model 0.70, min 5 -> was allowed 24.69 shares.
    return _intent(token_id=NO, side="buy", model_prob=0.70, market_price=0.40, min_order_size=5.0, **kw)


@pytest.fixture
def no_bus(monkeypatch):
    monkeypatch.setattr(paper_ledger, "bus", lambda *a, **k: None)


@pytest.mark.parametrize("held", [1.0, 10.0])
def test_buy_no_after_yes_only_fill_rejects_structural(tmp_path, monkeypatch, held):
    _ledger(tmp_path, monkeypatch, [_old(YES, "buy", held, held * 0.45)])
    state = paper_ledger.load_state(MARKET, NO)
    decision = evaluate(_buy_no(), load_risk(), state)
    assert decision.allow is False
    assert STRUCTURAL in decision.reasons, decision.reasons


def test_commit_books_nothing_on_the_opposite_leg(tmp_path, monkeypatch, no_bus):
    _ledger(tmp_path, monkeypatch, [_old(YES, "buy", 10.0, 4.5)])
    result = paper_ledger.commit_if_allowed(_buy_no(), load_risk())
    assert result["allow"] is False
    assert STRUCTURAL in result["decision"]["reasons"]
    assert paper_ledger.load_state(MARKET, NO)["leg_net_shares"] == 0.0
    assert paper_ledger.load_state(MARKET, YES)["leg_net_shares"] == pytest.approx(10.0)


def test_buy_yes_after_no_only_fill_rejects_structural(tmp_path, monkeypatch):
    """Symmetric: NO held, buy YES."""
    _ledger(tmp_path, monkeypatch, [_old(NO, "buy", 10.0, 4.0)])
    decision = evaluate(_intent(token_id=YES, side="buy", model_prob=0.70, market_price=0.40, min_order_size=5.0),
                        load_risk(), paper_ledger.load_state(MARKET, YES))
    assert decision.allow is False
    assert STRUCTURAL in decision.reasons, decision.reasons


# --- controls: these pass today and must keep passing (no over-tightening) --------------------------------

def test_control_buy_no_with_no_position_in_market_is_allowed(tmp_path, monkeypatch):
    _ledger(tmp_path, monkeypatch, [])
    decision = evaluate(_buy_no(), load_risk(), paper_ledger.load_state(MARKET, NO))
    assert decision.allow is True, decision.reasons


def test_control_yes_fully_closed_then_buy_no_is_allowed(tmp_path, monkeypatch):
    _ledger(tmp_path, monkeypatch, [_old(YES, "buy", 10.0, 4.5), _old(YES, "sell", 10.0, 4.0)])
    decision = evaluate(_buy_no(), load_risk(), paper_ledger.load_state(MARKET, NO))
    assert decision.allow is True, decision.reasons


def test_control_yes_held_in_another_market_does_not_block(tmp_path, monkeypatch):
    _ledger(tmp_path, monkeypatch, [_old(YES, "buy", 10.0, 4.5, market="other-market")])
    decision = evaluate(_buy_no(), load_risk(), paper_ledger.load_state(MARKET, NO))
    assert decision.allow is True, decision.reasons


def test_control_selling_the_held_yes_leg_is_still_a_reduce(tmp_path, monkeypatch):
    _ledger(tmp_path, monkeypatch, [_old(YES, "buy", 10.0, 4.5)])
    decision = evaluate(_intent(token_id=YES, side="sell", model_prob=0.10, shares=10.0, min_order_size=5.0),
                        load_risk(), paper_ledger.load_state(MARKET, YES))
    assert decision.allow is True, decision.reasons
    assert decision.shares == pytest.approx(10.0)
