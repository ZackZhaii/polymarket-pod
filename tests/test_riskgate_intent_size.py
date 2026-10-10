"""Issue #104: RiskGate books at most intent_builder's shares, and cost plus fee never passes the 1% clip.

Before: intent_builder sized 20 shares @ 0.50 ($10), to_risk_intent passed no size, and RiskGate booked its own
$10 / 0.505 = 19.80 shares plus a $0.40 fee: $10.40, 1.04% of equity (Poly预测 review B2, hand case 1).
Now: booked shares = min(intent.shares, the shares whose cost at max(effective, intent.limit_price) plus the
booked fee fits max_order_pct * PAPER_EQUITY_USD), on the INTENT_SHARE_STEP grid; an invalid size vetoes.
Offline.
"""
from __future__ import annotations

import copy
import math
import random

import pytest

import src.intent_builder as ib
import src.risk_gate as risk_gate
from src import fees
from src.podlib import PAPER_EQUITY_USD
from src.risk_gate import evaluate
from src.thresholds import INTENT_SHARE_STEP
from tests.test_intent_builder import CFG, NOW, STATE, book, market, signal
from tests.test_risk_gate import STATE as GATE_STATE
from tests.test_risk_gate import _intent

CAP_USD = PAPER_EQUITY_USD * min(float(CFG["max_order_pct"]), 0.01)


def _all_in(decision, limit_price=None) -> float:
    price = decision.effective_price if limit_price is None else max(decision.effective_price, limit_price)
    return decision.shares * price + decision.fee_usd


def _on_grid(shares: float) -> bool:
    return abs(round(shares / INTENT_SHARE_STEP) * INTENT_SHARE_STEP - shares) < 1e-9


def _ib_case(p, fee_rate, yes_ask, no_ask):
    result = ib.build_order_intent(signal(p=p), market(fee_rate=fee_rate),
                                   {"t-yes": book("t-yes", yes_ask), "t-no": book("t-no", no_ask)}, CFG, now=NOW,
                                   strategy="news_paper")
    assert result.intent is not None, result.reasons
    oi = result.intent
    return oi, evaluate(ib.to_risk_intent(oi), CFG, copy.deepcopy(STATE), now=NOW)


def test_the_review_repro_no_longer_spends_above_the_clip():
    oi, decision = _ib_case(0.60, 0.04, 0.50, 0.52)  # hand case 1: p 0.60, YES ask 0.50, fee 4%
    assert (oi.shares, oi.limit_price) == (20.0, 0.5)
    assert decision.allow and decision.reasons == []
    assert decision.effective_price == pytest.approx(0.505)
    # before: 19.80 shares, notional 10.00, fee 0.40 -> 10.40
    assert decision.shares == 19.04 and decision.shares <= oi.shares
    assert decision.notional_usd == pytest.approx(19.04 * 0.505)
    assert decision.fee_usd == pytest.approx(fees.ledger_fee(0.04, 0.50, 0.505, decision.notional_usd, 19.04))
    assert _all_in(decision, oi.limit_price) <= CAP_USD
    assert _all_in(decision) == pytest.approx(9.999808)


def test_to_risk_intent_passes_the_intent_size():
    oi, _ = _ib_case(0.60, 0.04, 0.50, 0.52)
    risk_intent = ib.to_risk_intent(oi)
    assert (risk_intent.shares, risk_intent.limit_price) == (oi.shares, oi.limit_price)


def test_never_more_shares_than_the_intent_asked_for():
    decision = evaluate(_intent(shares=5.0, limit_price=0.40), {**CFG}, dict(GATE_STATE))
    assert decision.allow and decision.shares == 5.0
    assert decision.notional_usd == pytest.approx(5.0 * 0.405)


def test_a_limit_above_the_effective_price_is_what_the_cap_is_checked_at():
    decision = evaluate(_intent(shares=100.0, limit_price=0.60, fee_rate=0.02), {**CFG}, dict(GATE_STATE))
    assert decision.allow
    assert _all_in(decision, 0.60) <= CAP_USD
    assert _all_in(decision, 0.60) > CAP_USD - 0.6 * INTENT_SHARE_STEP - 0.01  # one grid step below at most


@pytest.mark.parametrize("fee_rate", [0.0, 0.02, 0.04, 0.05])
@pytest.mark.parametrize("price", [0.05, 0.40, 0.50, 0.70, 0.94])
def test_a_legacy_intent_without_a_size_also_fits_all_in(fee_rate, price):
    decision = evaluate(_intent(market_price=price, model_prob=min(price + 0.05, 0.99), fee_rate=fee_rate,
                                spread=0.01), {**CFG}, dict(GATE_STATE))
    assert decision.shares > 0 and _on_grid(decision.shares)
    assert _all_in(decision) <= CAP_USD
    assert decision.notional_usd == pytest.approx(decision.shares * decision.effective_price)


@pytest.mark.parametrize("shares", [0.0, -1.0, math.nan, math.inf, "20", True])
def test_an_invalid_intent_shares_vetoes(shares):
    decision = evaluate(_intent(shares=shares), {**CFG}, dict(GATE_STATE))
    assert not decision.allow and "invalid_intent_shares" in decision.reasons


@pytest.mark.parametrize("limit", [0.0, 1.0, -0.1, math.nan, "0.4"])
def test_an_invalid_intent_limit_price_vetoes(limit):
    decision = evaluate(_intent(shares=10.0, limit_price=limit), {**CFG}, dict(GATE_STATE))
    assert not decision.allow and "invalid_intent_limit_price" in decision.reasons


def test_nothing_fits_vetoes_order_cost_above_clip(monkeypatch):
    monkeypatch.setattr(risk_gate, "PAPER_EQUITY_USD", 0.3)  # cap 0.003 USD: not one 0.01-share step at 0.405
    decision = evaluate(_intent(), {**CFG}, dict(GATE_STATE))
    assert not decision.allow and decision.reasons == ["order_cost_above_clip"] and decision.shares == 0.0


def test_fuzz_intent_builder_intents_never_spend_above_the_clip():
    rng = random.Random(20261010)
    checked = 0
    for _ in range(3000):
        fee = rng.choice([0.0, 0.0, 0.02, 0.04, 0.05])
        yes_ask = round(rng.uniform(0.05, 0.95), 2)
        no_ask = round(min(0.99, 1.0 - yes_ask + rng.uniform(0.01, 0.05)), 2)
        p = round(rng.uniform(0.02, 0.98), 3)
        result = ib.build_order_intent(signal(p=p), market(fee_rate=fee),
                                       {"t-yes": book("t-yes", yes_ask), "t-no": book("t-no", no_ask)}, CFG,
                                       now=NOW, strategy="news_paper")
        if result.intent is None:
            continue
        oi = result.intent
        decision = evaluate(ib.to_risk_intent(oi), CFG, copy.deepcopy(STATE), now=NOW)
        if not decision.allow:
            continue
        checked += 1
        assert decision.shares <= oi.shares + 1e-12
        assert _on_grid(decision.shares)
        assert _all_in(decision, oi.limit_price) <= CAP_USD
    assert checked > 200
