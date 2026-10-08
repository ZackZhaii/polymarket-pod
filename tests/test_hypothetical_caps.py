"""Task M: hypothetical intents no longer skip max_open_positions / max_daily_loss.

Offline. No network, no credentials. config/risk.toml is read, never written.
"""
from __future__ import annotations

import json

import pytest

import src.paper_ledger as paper_ledger
from src.podlib import PAPER_EQUITY_USD, RISK_PATH, load_risk, utc_now
from src.risk_gate import Intent, evaluate, risk_caps

FRESH = {"open_positions": 0, "market_already_open": False, "daily_loss_usd": 0.0}


def _intent(**overrides: object) -> Intent:
    fields: dict[str, object] = {
        "strategy": "phase0-gate-test",
        "market": "task-m-market",
        "side": "buy",
        "model_prob": 0.70,
        "market_price": 0.40,
        "assumed_slippage": 0.005,
        "fee_rate": 0.0,
        "liquidity_usd": 20000.0,
        "spread": 0.01,
        "hypothetical": True,
        "book_ts": utc_now(),
    }
    fields.update(overrides)
    return Intent(**fields)  # type: ignore[arg-type]


def _full_state(cfg: dict) -> dict:
    return {"open_positions": int(cfg["max_open_positions"]), "market_already_open": False, "daily_loss_usd": 0.0}


def _loss_state(cfg: dict) -> dict:
    return {"open_positions": 0, "market_already_open": False,
            "daily_loss_usd": PAPER_EQUITY_USD * float(cfg["max_daily_loss_pct"])}


def test_hypothetical_happy_path_still_allows():
    decision = evaluate(_intent(), load_risk(), FRESH)
    assert decision.allow is True, decision.reasons


def test_hypothetical_intent_hits_max_open_positions():
    cfg = load_risk()
    decision = evaluate(_intent(), cfg, _full_state(cfg))
    assert decision.allow is False
    assert "max_open_positions" in decision.reasons


def test_hypothetical_intent_hits_max_daily_loss():
    cfg = load_risk()
    decision = evaluate(_intent(), cfg, _loss_state(cfg))
    assert decision.allow is False
    assert "max_daily_loss" in decision.reasons


@pytest.mark.parametrize("state_fn", [_full_state, _loss_state])
def test_hypothetical_and_real_intents_get_the_same_cap_reasons(state_fn):
    cfg = load_risk()
    state = state_fn(cfg)
    real = evaluate(_intent(strategy="task-m-real", hypothetical=False), cfg, state)
    hypo = evaluate(_intent(), cfg, state)
    caps = {"max_open_positions", "max_daily_loss"}
    assert set(real.reasons) & caps == set(hypo.reasons) & caps != set()


def test_already_open_market_is_exempt_from_the_position_cap_for_both():
    cfg = load_risk()
    state = dict(_full_state(cfg), market_already_open=True)
    assert evaluate(_intent(), cfg, state).allow is True
    assert evaluate(_intent(strategy="task-m-real", hypothetical=False), cfg, state).allow is True


def test_hypothetical_outside_gate_test_strategy_still_vetoed():
    decision = evaluate(_intent(strategy="task-m-real"), load_risk(), FRESH)
    assert "hypothetical_strategy_not_allowed" in decision.reasons


def test_risk_caps_reads_the_committed_file():
    caps = risk_caps(load_risk())
    assert caps["max_open_positions"] == 5
    assert caps["max_daily_loss_pct"] == pytest.approx(0.03)


@pytest.mark.parametrize("key", ["max_open_positions", "max_daily_loss_pct"])
def test_missing_cap_vetoes_instead_of_defaulting(key):
    cfg = dict(load_risk())
    del cfg[key]
    with pytest.raises(RuntimeError, match=key):
        risk_caps(cfg)
    for hypothetical, strategy in ((True, "phase0-gate-test"), (False, "task-m-real")):
        decision = evaluate(_intent(hypothetical=hypothetical, strategy=strategy), cfg, FRESH)
        assert decision.allow is False
        assert "invalid_risk_cap" in decision.reasons


@pytest.mark.parametrize(
    "key,value",
    [
        ("max_open_positions", True),
        ("max_open_positions", "5"),
        ("max_open_positions", None),
        ("max_open_positions", float("inf")),
        ("max_open_positions", float("nan")),
        ("max_open_positions", -1),
        ("max_open_positions", 2.5),
        ("max_daily_loss_pct", False),
        ("max_daily_loss_pct", "0.03"),
        ("max_daily_loss_pct", float("inf")),
        ("max_daily_loss_pct", float("nan")),
        ("max_daily_loss_pct", 0),
        ("max_daily_loss_pct", -0.03),
    ],
)
def test_invalid_cap_vetoes(key, value):
    cfg = dict(load_risk())
    cfg[key] = value
    with pytest.raises(RuntimeError):
        risk_caps(cfg)
    decision = evaluate(_intent(), cfg, FRESH)
    assert decision.allow is False
    assert "invalid_risk_cap" in decision.reasons


def test_integral_float_position_cap_is_accepted():
    cfg = dict(load_risk(), max_open_positions=5.0)
    assert risk_caps(cfg)["max_open_positions"] == 5


@pytest.mark.parametrize(
    "key,value",
    [
        ("open_positions", None),
        ("open_positions", True),
        ("open_positions", "0"),
        ("open_positions", -1),
        ("open_positions", 1.5),
        ("open_positions", float("nan")),
        ("daily_loss_usd", None),
        ("daily_loss_usd", "0"),
        ("daily_loss_usd", False),
        ("daily_loss_usd", float("inf")),
        ("daily_loss_usd", float("nan")),
        ("daily_loss_usd", -1.0),
    ],
)
def test_invalid_state_vetoes(key, value):
    state = dict(FRESH)
    state[key] = value
    for hypothetical, strategy in ((True, "phase0-gate-test"), (False, "task-m-real")):
        decision = evaluate(_intent(hypothetical=hypothetical, strategy=strategy), load_risk(), state)
        assert decision.allow is False
        assert "invalid_risk_state" in decision.reasons


@pytest.mark.parametrize("key", ["open_positions", "daily_loss_usd"])
def test_missing_state_key_vetoes_instead_of_reading_zero(key):
    state = dict(FRESH)
    del state[key]
    decision = evaluate(_intent(), load_risk(), state)
    assert decision.allow is False
    assert "invalid_risk_state" in decision.reasons


def test_ledger_self_check_intent_is_capped_by_real_fills(tmp_path, monkeypatch):
    """The ledger's hypothetical gate-test intent now sees the real book of fills."""
    before = RISK_PATH.read_bytes()
    cfg = load_risk()
    ledger = tmp_path / "paper.jsonl"
    rows = [
        {"ts": utc_now(), "market": f"real-{n}", "side": "buy", "shares": 1.0, "fee_usd": 0.0,
         "hypothetical": False, "counts_toward_risk": True}
        for n in range(int(cfg["max_open_positions"]))
    ]
    ledger.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    monkeypatch.setattr(paper_ledger, "PAPER_PATH", ledger)
    market = "phase0-self-check:hypothetical-allow"
    state = paper_ledger.load_state(market)
    assert state["open_positions"] == int(cfg["max_open_positions"])
    intent = paper_ledger._intent(market=market, model_prob=0.70, market_price=0.40)
    assert intent.hypothetical is True
    decision = evaluate(intent, cfg, state)
    assert decision.allow is False
    assert "max_open_positions" in decision.reasons
    assert RISK_PATH.read_bytes() == before
