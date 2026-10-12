"""Offline RiskGate veto tests. No network, no .env, no order SDK."""
from __future__ import annotations

import sys

import pytest

import src.gaps as gaps
import src.risk_gate as risk_gate
from src.podlib import RISK_PATH, load_risk, utc_now
from src.risk_gate import Intent, evaluate, gate_thresholds

STATE = {"open_positions": 0, "market_already_open": False, "exposure_usd": 0.0, "leg_exposure_usd": 0.0, "daily_loss_usd": 0.0, "recent_fills": [], "own_quotes": []}


def _intent(**overrides: object) -> Intent:
    fields: dict[str, object] = {
        "strategy": "paper-gap-check",
        "market": "test-market",
        "side": "buy",
        "model_prob": 0.70,
        "market_price": 0.40,
        "assumed_slippage": 0.005,
        "fee_rate": 0.0,
        "liquidity_usd": 20000.0,
        "spread": 0.01,
        "hypothetical": False,
        "book_ts": utc_now(),
    }
    fields.update(overrides)
    fields.setdefault("tags", ("test-market",))  # issue #131: a tagged market (missing tags veto)
    fields.setdefault("min_order_size", 5.0)  # issue #226: a book minimum (missing vetoes)
    return Intent(**fields)  # type: ignore[arg-type]


def test_modules_do_not_pull_an_order_sdk_or_env_file():
    assert "py_clob_client" not in sys.modules
    assert "polymarket" not in sys.modules
    assert not hasattr(gaps, "urlopen")
    assert not hasattr(risk_gate, "urlopen")
    # Stat only. Never open a secrets file.
    assert RISK_PATH.name == "risk.toml"


def test_clean_paper_intent_can_pass_and_risk_file_stays_put():
    before = RISK_PATH.read_bytes()
    cfg = load_risk()
    decision = evaluate(_intent(), cfg, STATE)
    assert decision.allow is True
    assert decision.reasons == []
    # issue #104: shares fit the all-in 1% clip (fee-free here): 24.69 * 0.405 <= 10
    assert decision.shares == 24.69 and decision.notional_usd == pytest.approx(24.69 * 0.405)
    assert decision.notional_usd + decision.fee_usd <= 10.0
    assert RISK_PATH.read_bytes() == before
    assert b'live_enabled = false' in before
    assert b'mode = "paper"' in before


def test_pin_drift_is_vetoed_without_writing_config():
    before = RISK_PATH.read_bytes()
    cfg = load_risk()
    cfg["live_enabled"] = True
    decision = evaluate(_intent(), cfg, STATE)
    assert decision.allow is False
    assert any(reason.startswith("phase0_pin:") for reason in decision.reasons)
    assert RISK_PATH.read_bytes() == before


def test_non_positive_edge_is_vetoed():
    cfg = load_risk()
    equal = evaluate(_intent(model_prob=0.50, market_price=0.50), cfg, STATE)
    assert equal.allow is False
    assert "non_positive_edge" in equal.reasons
    zero_slip = evaluate(
        _intent(model_prob=0.40, market_price=0.40, assumed_slippage=0.0),
        cfg,
        STATE,
    )
    assert zero_slip.allow is False
    assert "non_positive_edge" in zero_slip.reasons
    assert zero_slip.edge == 0.0


def test_wide_spread_is_vetoed():
    cfg = load_risk()
    decision = evaluate(_intent(spread=0.20), cfg, STATE)
    assert decision.allow is False
    assert "spread_above_max" in decision.reasons
    assert float(cfg["max_spread"]) == 0.04


def test_banned_strategy_names_are_vetoed():
    cfg = load_risk()
    for name in ("wash-probe", "self-trade-bid", "self_trade", "spoof-quotes", "layering-quotes"):
        decision = evaluate(_intent(strategy=name), cfg, STATE)
        assert decision.allow is False
        assert "banned_behavior" in decision.reasons


_BAD_GATE_VALUES = (
    True,
    False,
    "0.04",
    None,
    float("inf"),
    float("-inf"),
    float("nan"),
)


def _assert_gate_rejected(cfg: dict, key: str) -> None:
    decision = evaluate(_intent(), cfg, STATE)
    assert decision.allow is False
    assert "invalid_gate_threshold" in decision.reasons
    with pytest.raises(RuntimeError) as caught:
        gate_thresholds(cfg)
    assert key in str(caught.value)


@pytest.mark.parametrize("key", ("min_liquidity_usd", "max_spread"))
def test_missing_gate_key_fails_closed(key: str) -> None:
    before = RISK_PATH.read_bytes()
    cfg = dict(load_risk())
    del cfg[key]
    _assert_gate_rejected(cfg, key)
    with pytest.raises(RuntimeError, match="missing gate key"):
        gate_thresholds(cfg)
    assert RISK_PATH.read_bytes() == before


@pytest.mark.parametrize("key", ("min_liquidity_usd", "max_spread"))
@pytest.mark.parametrize("bad", _BAD_GATE_VALUES)
def test_non_finite_gate_values_fail_closed(key: str, bad: object) -> None:
    before = RISK_PATH.read_bytes()
    cfg = dict(load_risk())
    cfg[key] = bad
    _assert_gate_rejected(cfg, key)
    assert RISK_PATH.read_bytes() == before


@pytest.mark.parametrize(
    ("key", "bad"),
    (
        ("max_spread", 0),
        ("max_spread", -0.01),
        ("min_liquidity_usd", -1),
    ),
)
def test_out_of_range_gate_bounds_fail_closed(key: str, bad: float) -> None:
    before = RISK_PATH.read_bytes()
    cfg = dict(load_risk())
    cfg[key] = bad
    _assert_gate_rejected(cfg, key)
    assert RISK_PATH.read_bytes() == before


def test_finite_int_and_float_thresholds_still_allow() -> None:
    before = RISK_PATH.read_bytes()
    cfg = dict(load_risk())
    cfg["min_liquidity_usd"] = 10000
    cfg["max_spread"] = 0.04
    assert gate_thresholds(cfg) == {"min_liquidity_usd": 10000.0, "max_spread": 0.04}
    decision = evaluate(_intent(), cfg, STATE)
    assert decision.allow is True
    assert decision.reasons == []
    assert RISK_PATH.read_bytes() == before


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    (
        ("spread", float("nan"), "invalid_spread"),
        ("liquidity_usd", float("nan"), "invalid_liquidity"),
        ("spread", True, "invalid_spread"),
    ),
)
def test_bad_intent_quote_fails_closed(field: str, value: object, reason: str) -> None:
    before = RISK_PATH.read_bytes()
    cfg = dict(load_risk())
    decision = evaluate(_intent(**{field: value}), cfg, STATE)
    assert decision.allow is False
    assert reason in decision.reasons
    assert "spread_above_max" not in decision.reasons
    assert "negative_spread" not in decision.reasons
    assert "liquidity_below_min" not in decision.reasons
    assert RISK_PATH.read_bytes() == before


def test_negative_intent_spread_is_vetoed_even_with_bad_config() -> None:
    cfg = dict(load_risk())
    cfg["max_spread"] = float("nan")
    decision = evaluate(_intent(spread=-0.01), cfg, STATE)
    assert decision.allow is False
    assert "invalid_gate_threshold" in decision.reasons
    assert "negative_spread" in decision.reasons
