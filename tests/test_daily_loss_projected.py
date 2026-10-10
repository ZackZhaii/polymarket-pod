"""Issue #127 (Task CA, F2c): RiskGate vetoes max_daily_loss_projected when today's loss plus this order's
worst-case loss would exceed max_daily_loss_pct * PAPER_EQUITY_USD. Offline; veto only."""
from __future__ import annotations

import math
from dataclasses import replace

import pytest

from src import risk_gate
from src.podlib import load_risk
from src.risk_gate import evaluate, worst_case_loss
from src.thresholds import PAPER_EQUITY_USD
from tests.test_daily_loss import _intent

CAP_USD = 0.03 * PAPER_EQUITY_USD  # $30


def _state(daily_loss):
    return {"open_positions": 0, "leg_already_open": False, "exposure_usd": 0.0, "leg_exposure_usd": 0.0,
            "leg_net_shares": 0.0, "daily_loss_usd": daily_loss, "recent_fills": [], "own_quotes": []}


def test_cap_is_the_pinned_three_percent():
    assert load_risk()["max_daily_loss_pct"] == 0.03 and CAP_USD == pytest.approx(30.0)


def _decide(daily_loss, **changes):
    return evaluate(replace(_intent(), **changes), load_risk(), _state(daily_loss))


def test_baseline_order_is_allowed_and_costs_about_ten_dollars():
    decision = _decide(0.0)
    assert decision.allow is True, decision.reasons
    worst = worst_case_loss("buy", decision.shares, decision.effective_price, decision.effective_price,
                            decision.fee_usd)
    assert 0 < worst <= 10.0 + 1e-9


def test_20_50_plus_a_ten_dollar_order_is_vetoed():
    decision = _decide(20.50)
    assert decision.allow is False and decision.reasons == ["max_daily_loss_projected"]


def test_19_plus_the_order_is_allowed():
    decision = _decide(19.0)
    assert decision.allow is True, decision.reasons


def test_boundary_exactly_at_the_cap_passes_and_a_cent_over_vetoes():
    decision = _decide(0.0)
    worst = worst_case_loss("buy", decision.shares, decision.effective_price, decision.effective_price,
                            decision.fee_usd)
    assert _decide(CAP_USD - worst - 1e-6).allow is True
    assert _decide(CAP_USD - worst + 0.01).reasons == ["max_daily_loss_projected"]


def test_existing_max_daily_loss_is_unchanged():
    decision = _decide(CAP_USD)
    assert decision.reasons == ["max_daily_loss"]  # not doubled with the projected reason


def test_hypothetical_intent_gets_the_same_check():
    decision = _decide(25.0, hypothetical=True, strategy="phase0-gate-test")
    assert decision.allow is False and "max_daily_loss_projected" in decision.reasons


@pytest.mark.parametrize(("side", "shares", "cost", "eff", "fee", "expected"), [
    ("buy", 20.0, 0.5, 0.5, 0.2, 10.2),
    ("sell", 20.0, 0.4, 0.4, 0.1, 12.1),
    ("buy", 0.0, 0.5, 0.5, 0.0, 0.0),
    ("buy", math.nan, 0.5, 0.5, 0.0, None),
    ("buy", 10.0, None, 0.5, 0.0, None),
    ("buy", 10.0, 0.5, 0.5, math.inf, None),
    ("buy", -1.0, 0.5, 0.5, 0.0, None),
    ("sell", 10.0, 0.5, 1.5, 0.0, None),
    ("hold", 10.0, 0.5, 0.5, 0.0, None),
])
def test_worst_case_loss(side, shares, cost, eff, fee, expected):
    result = worst_case_loss(side, shares, cost, eff, fee)
    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected)


def test_uncomputable_worst_case_vetoes(monkeypatch):
    monkeypatch.setattr(risk_gate, "worst_case_loss", lambda *args: None)
    decision = _decide(0.0)
    assert decision.allow is False and decision.reasons == ["max_daily_loss_projected"]
