"""Task DD (#237, Codex P1s on #280). Offline paper ledger only: no network, no credentials, no orders.

a) a missing or invalid opposite_leg_shares fails closed on a buy (structural_arb_disabled).
b) interleaved commits / pending opposite buys cannot bypass the veto.
"""
from __future__ import annotations

import json
import math

import pytest

import src.paper_ledger as paper_ledger
import src.risk_gate as risk_gate
from src.podlib import load_risk
from src.risk_gate import evaluate
from tests.test_single_leg_exposure import MARKET, NO, YES, _intent, _ledger

STRUCTURAL = "structural_arb_disabled"


def _buy(token: str = NO):
    return _intent(token_id=token, side="buy", model_prob=0.70, market_price=0.40, min_order_size=5.0)


@pytest.fixture
def no_bus(monkeypatch):
    monkeypatch.setattr(paper_ledger, "bus", lambda *a, **k: None)


# ---------------------------------------------------------------- a) fail closed


@pytest.mark.parametrize("bad", ["missing", None, math.nan, math.inf, -math.inf, -1.0, "abc", True])
def test_invalid_opposite_leg_shares_refuses_the_buy(tmp_path, monkeypatch, bad):
    _ledger(tmp_path, monkeypatch, [])
    state = paper_ledger.load_state(MARKET, NO)
    assert evaluate(_buy(), load_risk(), state).allow is True  # control: the valid zero allows
    if bad == "missing":
        state.pop("opposite_leg_shares")
    else:
        state["opposite_leg_shares"] = bad
    decision = evaluate(_buy(), load_risk(), state)
    assert decision.allow is False
    assert STRUCTURAL in decision.reasons, decision.reasons


@pytest.mark.parametrize("bad", [None, math.nan, -1, "x"])
def test_invalid_opposite_pending_buys_refuses_the_buy(tmp_path, monkeypatch, bad):
    _ledger(tmp_path, monkeypatch, [])
    state = paper_ledger.load_state(MARKET, NO)
    state["opposite_pending_buys"] = bad
    decision = evaluate(_buy(), load_risk(), state)
    assert decision.allow is False and STRUCTURAL in decision.reasons, decision.reasons


# ---------------------------------------------------------------- b) pending + interleaving


def test_open_buy_quote_on_the_other_token_counts_as_opposite(tmp_path, monkeypatch):
    _ledger(tmp_path, monkeypatch, [])
    quote = {"market": MARKET, "token_id": YES, "side": "buy", "price": 0.45, "status": "open",
             "cancelled_by": None, "cancelled_ts": None}
    (tmp_path / "quotes.jsonl").write_text(json.dumps(quote) + "\n", encoding="utf-8")
    state = paper_ledger.load_state(MARKET, NO)
    assert state["opposite_pending_buys"] == 1
    decision = evaluate(_buy(), load_risk(), state)
    assert decision.allow is False and STRUCTURAL in decision.reasons, decision.reasons


def test_cancelled_quote_on_the_other_token_does_not_count(tmp_path, monkeypatch):
    _ledger(tmp_path, monkeypatch, [])
    quote = {"market": MARKET, "token_id": YES, "side": "buy", "price": 0.45, "status": "cancelled",
             "cancelled_by": "Sentinel", "cancelled_ts": "2026-01-01T00:00:00Z"}
    (tmp_path / "quotes.jsonl").write_text(json.dumps(quote) + "\n", encoding="utf-8")
    assert paper_ledger.load_state(MARKET, NO)["opposite_pending_buys"] == 0


def test_interleaved_opposite_commits_cannot_both_book(tmp_path, monkeypatch, no_bus):
    """Deterministic interleaving: while commit #1 (buy NO) sits between its RiskGate allow and its write,
    commit #2 (buy YES) runs. #2 must not book; afterwards the ledger holds one leg only."""
    _ledger(tmp_path, monkeypatch, [])
    monkeypatch.setattr(paper_ledger, "COMMIT_LOCK_TIMEOUT_S", 0.2)
    real_evaluate = paper_ledger.evaluate
    inner: dict = {}

    def interleaving_evaluate(intent, cfg, state):
        decision = real_evaluate(intent, cfg, state)
        if intent.token_id == NO and "result" not in inner:
            inner["result"] = None  # guard against recursion
            inner["result"] = paper_ledger.commit_if_allowed(_buy(YES), cfg)
        return decision

    monkeypatch.setattr(paper_ledger, "evaluate", interleaving_evaluate)
    first = paper_ledger.commit_if_allowed(_buy(NO), load_risk())
    assert inner["result"]["allow"] is False
    assert first["allow"] is True, first
    assert paper_ledger.load_state(MARKET, YES)["leg_net_shares"] == 0.0
    assert paper_ledger.load_state(MARKET, NO)["leg_net_shares"] > 0
    # and a later YES buy is now refused by the held NO leg
    monkeypatch.setattr(paper_ledger, "evaluate", real_evaluate)
    later = paper_ledger.commit_if_allowed(_buy(YES), load_risk())
    assert later["allow"] is False and STRUCTURAL in later["decision"]["reasons"]
