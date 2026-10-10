"""Task X (COORDINATION #6): RiskGate requires edge >= MIN_EDGE (0.02), not just edge > 0.

MIN_EDGE is a code constant in src/risk_gate.py, not a risk.toml key (threshold proposal for
the user to decide at M2, like MIN_HOURS_TO_EXPIRY and WASH_WINDOW_S). 0.02 is inclusive
(with a 1e-12 float tolerance). non_positive_edge is kept for edge <= 0. Offline only.
"""
from __future__ import annotations

import math

import pytest

import src.risk_gate as risk_gate
from src.podlib import RISK_PATH, load_risk, utc_now
from src.risk_gate import Intent, evaluate

STATE = {"open_positions": 0, "leg_already_open": False, "exposure_usd": 0.0, "leg_exposure_usd": 0.0, "daily_loss_usd": 0.0,
         "recent_fills": [], "own_quotes": []}


def _intent(side: str = "buy", **overrides: object) -> Intent:
    fields: dict[str, object] = {
        "strategy": "task-x-check", "market": "task-x-market", "token_id": "tok", "side": side,
        "model_prob": 0.70, "market_price": 0.40, "assumed_slippage": 0.005, "fee_rate": 0.0,
        "liquidity_usd": 20000.0, "spread": 0.01, "hypothetical": False, "book_ts": utc_now(),
    }
    fields.update(overrides)
    return Intent(**fields)  # type: ignore[arg-type]


def test_min_edge_is_a_code_constant_not_in_risk_toml():
    assert risk_gate.MIN_EDGE == 0.02
    assert "min_edge" not in RISK_PATH.read_text(encoding="utf-8").lower()


# buy: effective = 0.40 + 0.005 = 0.405; sell: effective = 0.40 - 0.005 = 0.395
@pytest.mark.parametrize(
    ("side", "model_prob", "edge", "allowed"),
    [
        ("buy", 0.4249, 0.0199, False),
        ("buy", 0.425, 0.02, True),
        ("buy", 0.455, 0.05, True),
        ("sell", 0.3751, 0.0199, False),
        ("sell", 0.375, 0.02, True),
        ("sell", 0.345, 0.05, True),
    ],
)
def test_edge_threshold(side, model_prob, edge, allowed):
    before = RISK_PATH.read_bytes()
    decision = evaluate(_intent(side, model_prob=model_prob), load_risk(), STATE)
    assert decision.edge == pytest.approx(edge, abs=1e-9)
    assert decision.allow is allowed, decision.reasons
    assert ("edge_below_min" in decision.reasons) is (not allowed)
    assert "non_positive_edge" not in decision.reasons
    assert RISK_PATH.read_bytes() == before


def test_fee_counts_toward_the_minimum():
    # Task AE: the fee is charged on max(market_price, effective_price) (src/fees.py).
    # 0.43 - 0.405 - 0.02 * 0.405 = 0.0169 < 0.02 (was 0.017 with the fee on 0.40)
    decision = evaluate(_intent(model_prob=0.43, fee_rate=0.02), load_risk(), STATE)
    assert decision.edge == pytest.approx(0.0169)
    assert decision.reasons == ["edge_below_min"]


@pytest.mark.parametrize("model_prob", [0.405, 0.40, 0.30])
def test_non_positive_edge_is_kept_and_not_doubled(model_prob):
    decision = evaluate(_intent(model_prob=model_prob), load_risk(), STATE)
    assert decision.allow is False
    assert "non_positive_edge" in decision.reasons
    assert "edge_below_min" not in decision.reasons


@pytest.mark.parametrize("field", ["model_prob", "market_price", "assumed_slippage", "fee_rate"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), None])
def test_non_finite_or_missing_fails_closed(field, value):
    decision = evaluate(_intent(**{field: value}), load_risk(), STATE)
    assert decision.allow is False
    assert "non_positive_edge" in decision.reasons
    assert math.isfinite(decision.edge)


def test_config_cannot_lower_the_minimum():
    cfg = dict(load_risk())
    cfg["min_edge"] = 0.0
    decision = evaluate(_intent(model_prob=0.4249), cfg, STATE)
    assert decision.allow is False and "edge_below_min" in decision.reasons
