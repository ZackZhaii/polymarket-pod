"""Issue #226 (Task CT): a reducing order never crosses zero, and the final size is re-checked against
Intent.min_order_size. Offline paper ledger only. No network, no credentials, no orders.

Poly指挥's decisions on #226 (all tightening):
- D1: a reverse (reducing) order bigger than the open leg is first shrunk to |leg_net_shares|; below
  min_order_size after the shrink vetoes ``reduce_exceeds_position``.
- D2: Intent.min_order_size is required; None (or not a finite number >= 0) vetoes ``missing_min_order_size``.
- D3: the FINAL booked shares (after the 1% clip, intent.shares and the reduce shrink) are re-checked:
  below min_order_size vetoes ``size_below_min_order``.
- D4: a sell with no open position on that leg (opening a short) vetoes ``sell_requires_position``; on
  Polymarket a short is a buy of the other token.
- D5: the shrink rounds DOWN to INTENT_SHARE_STEP (0.01 shares).

Ledger rows carry an old ts (2026-01-01) so wash_same_market (WASH_WINDOW_S=300) and today's daily loss do
not veto first and hide the sizing rule.
"""
from __future__ import annotations

import argparse
import math

import pytest

import src.intent_builder as ib
import src.paper_ledger as paper_ledger
from src.podlib import load_risk
from src.risk_gate import evaluate
from tests.test_intent_builder import build
from tests.test_single_leg_exposure import MARKET, NO, YES, _intent, _ledger, _row

OLD_TS = "2026-01-01T00:00:00Z"  # outside WASH_WINDOW_S and not today: isolates the sizing rule
NEW_CODES = {"reduce_exceeds_position", "missing_min_order_size", "size_below_min_order", "sell_requires_position"}


def _old(side: str, shares: float, notional: float, token: str = YES) -> dict:
    return _row(MARKET, token, side, shares, notional, ts=OLD_TS)


def _sell(**kw):
    return _intent(token_id=YES, side="sell", model_prob=0.10, **kw)


def _buy(**kw):
    return _intent(token_id=YES, side="buy", model_prob=0.70, **kw)


@pytest.fixture
def no_bus(monkeypatch):
    monkeypatch.setattr(paper_ledger, "bus", lambda *a, **k: None)


# --- D1 / D5: a reverse order is capped at the open position ----------------------------------------

def test_c1_poly_arb_case_long_1_sell_39_21_is_capped_at_1(tmp_path, monkeypatch):
    """Poly套利 on 8b5db69: long 1 YES, sell intent 39.21 -> allowed 25.31 -> net -24.31. Now: 1 share."""
    _ledger(tmp_path, monkeypatch, [_old("buy", 1.0, 0.4)])
    state = paper_ledger.load_state(MARKET, YES)
    d = evaluate(_sell(shares=39.21, min_order_size=1.0), load_risk(), state)
    assert d.allow is True and d.reasons == [], d.reasons
    assert d.shares == pytest.approx(1.0)


def test_c1b_long_1_clip_sized_sell_is_capped_at_1(tmp_path, monkeypatch):
    _ledger(tmp_path, monkeypatch, [_old("buy", 1.0, 0.4)])
    d = evaluate(_sell(min_order_size=1.0), load_risk(), paper_ledger.load_state(MARKET, YES))
    assert d.allow is True and d.shares == pytest.approx(1.0)
    assert d.notional_usd == pytest.approx(1.0 * d.effective_price)


def test_c1c_long_1_shrink_below_min_vetoes_reduce_exceeds_position(tmp_path, monkeypatch):
    _ledger(tmp_path, monkeypatch, [_old("buy", 1.0, 0.4)])
    d = evaluate(_sell(shares=39.21, min_order_size=5.0), load_risk(), paper_ledger.load_state(MARKET, YES))
    assert d.allow is False and "reduce_exceeds_position" in d.reasons, (d.allow, d.shares, d.reasons)
    assert "size_below_min_order" not in d.reasons  # one reason for one cause


def test_c2_commit_flattens_and_never_flips_the_ledger_sign(tmp_path, monkeypatch, no_bus):
    _ledger(tmp_path, monkeypatch, [_old("buy", 1.0, 0.4)])
    result = paper_ledger.commit_if_allowed(_sell(shares=39.21, min_order_size=1.0), load_risk())
    assert result["allow"] is True, result
    assert result["fill"]["shares"] == pytest.approx(1.0)
    after = paper_ledger.load_state(MARKET, YES)
    assert after["leg_net_shares"] == pytest.approx(0.0)
    assert after["leg_already_open"] is False


def test_c2b_commit_vetoed_reduce_books_nothing(tmp_path, monkeypatch, no_bus):
    _ledger(tmp_path, monkeypatch, [_old("buy", 1.0, 0.4)])
    result = paper_ledger.commit_if_allowed(_sell(shares=39.21, min_order_size=5.0), load_risk())
    assert result["allow"] is False and result.get("halted") is not True
    assert result["decision"]["reasons"] == ["reduce_exceeds_position"]
    assert paper_ledger.load_state(MARKET, YES)["leg_net_shares"] == pytest.approx(1.0)


def test_c3_short_5_oversized_buy_is_capped_at_5(tmp_path, monkeypatch):
    _ledger(tmp_path, monkeypatch, [_old("sell", 5.0, 2.0)])
    state = paper_ledger.load_state(MARKET, YES)
    assert state["leg_net_shares"] == pytest.approx(-5.0)
    d = evaluate(_buy(min_order_size=5.0), load_risk(), state)
    assert d.allow is True and d.shares == pytest.approx(5.0), (d.allow, d.shares, d.reasons)


def test_c4_partial_ledger_buy10_sell4_then_oversized_sell_is_capped_at_6(tmp_path, monkeypatch):
    _ledger(tmp_path, monkeypatch, [_old("buy", 10.0, 4.0), _old("sell", 4.0, 1.6)])
    state = paper_ledger.load_state(MARKET, YES)
    assert state["leg_net_shares"] == pytest.approx(6.0)
    d = evaluate(_sell(min_order_size=5.0), load_risk(), state)
    assert d.allow is True and d.shares == pytest.approx(6.0), (d.allow, d.shares, d.reasons)


def test_c5_exact_flatten_is_allowed(tmp_path, monkeypatch):
    _ledger(tmp_path, monkeypatch, [_old("buy", 20.0, 8.0)])
    d = evaluate(_sell(shares=20.0), load_risk(), paper_ledger.load_state(MARKET, YES))
    assert d.allow is True and d.reasons == []
    assert d.shares == pytest.approx(20.0)


def test_c6_reduce_within_position_is_allowed(tmp_path, monkeypatch):
    _ledger(tmp_path, monkeypatch, [_old("buy", 50.0, 20.0)])
    d = evaluate(_sell(shares=10.0), load_risk(), paper_ledger.load_state(MARKET, YES))
    assert d.allow is True and d.shares == pytest.approx(10.0)


def test_c7_same_direction_add_still_vetoes(tmp_path, monkeypatch):  # regression of #123
    _ledger(tmp_path, monkeypatch, [_old("buy", 1.0, 0.4)])
    d = evaluate(_buy(), load_risk(), paper_ledger.load_state(MARKET, YES))
    assert d.allow is False and "add_to_open_leg" in d.reasons


def test_c8_other_token_is_not_capped_by_the_reduce_rules(tmp_path, monkeypatch):
    """Only that the #226 rules leave a buy of the other token alone. Whether that buy is allowed at all is
    issue #237's call (structural_arb_disabled), so no allow is asserted here."""
    _ledger(tmp_path, monkeypatch, [_old("buy", 1.0, 0.4)])
    state = paper_ledger.load_state(MARKET, NO)
    assert state["leg_already_open"] is False
    d = evaluate(_intent(token_id=NO), load_risk(), state)
    assert not NEW_CODES & set(d.reasons), d.reasons
    assert d.shares > 1.0  # full clip size: no reduce cap on a new leg


def test_d5_shrink_rounds_down_to_the_share_step(tmp_path, monkeypatch):
    _ledger(tmp_path, monkeypatch, [_old("buy", 1.239, 0.5)])
    d = evaluate(_sell(shares=39.21, min_order_size=1.0), load_risk(), paper_ledger.load_state(MARKET, YES))
    assert d.allow is True and d.shares == pytest.approx(1.23)
    assert d.shares <= 1.239


def test_d5_a_position_under_one_step_cannot_be_reduced(tmp_path, monkeypatch):
    _ledger(tmp_path, monkeypatch, [_old("buy", 0.004, 0.002)])
    state = paper_ledger.load_state(MARKET, YES)
    assert state["leg_already_open"] is True
    d = evaluate(_sell(shares=39.21, min_order_size=0.0), load_risk(), state)
    assert d.allow is False and "reduce_exceeds_position" in d.reasons, (d.shares, d.reasons)


# --- D2 / D3: min_order_size required and re-checked on the FINAL size ------------------------------

def test_c9_clip_shrink_below_min_order_is_vetoed(tmp_path, monkeypatch):
    _ledger(tmp_path, monkeypatch, [])
    intent = _intent(token_id=YES, market_price=0.50, model_prob=0.80, shares=20.0, min_order_size=20.0)
    d = evaluate(intent, load_risk(), paper_ledger.load_state(MARKET, YES))
    assert d.shares < 20.0  # the 1% clip shrank 20 -> 19.80
    assert d.allow is False and d.reasons == ["size_below_min_order"], (d.allow, d.shares, d.reasons)


def test_c10_shrink_exactly_to_min_is_allowed(tmp_path, monkeypatch):
    _ledger(tmp_path, monkeypatch, [])
    cfg = load_risk()
    state = paper_ledger.load_state(MARKET, YES)
    probe = evaluate(_intent(token_id=YES, market_price=0.50, model_prob=0.80, shares=20.0, min_order_size=0.0),
                     cfg, state)
    assert probe.allow is True
    intent = _intent(token_id=YES, market_price=0.50, model_prob=0.80, shares=20.0, min_order_size=probe.shares)
    d = evaluate(intent, cfg, state)
    assert d.allow is True and d.shares == pytest.approx(probe.shares)


def test_c11_reduce_truncation_below_min_is_vetoed(tmp_path, monkeypatch):
    """D1 + D3 together: long 5, sell 20 with min 10. Truncating to 5 < 10 vetoes; 20 is never booked."""
    _ledger(tmp_path, monkeypatch, [_old("buy", 5.0, 2.0)])
    d = evaluate(_sell(shares=20.0, min_order_size=10.0), load_risk(), paper_ledger.load_state(MARKET, YES))
    assert d.allow is False and d.reasons == ["reduce_exceeds_position"], (d.allow, d.shares, d.reasons)


def test_c12_reduce_still_obeys_daily_loss_cap(tmp_path, monkeypatch):  # caps not bypassed
    _ledger(tmp_path, monkeypatch, [_old("buy", 50.0, 20.0)])
    state = dict(paper_ledger.load_state(MARKET, YES), daily_loss_usd=29.0)
    d = evaluate(_sell(shares=10.0), load_risk(), state)
    assert d.allow is False and "max_daily_loss_projected" in d.reasons


@pytest.mark.parametrize("value", [None, math.nan, math.inf, -1.0, True, "5", [5.0]])
def test_d2_missing_or_invalid_min_order_size_vetoes(tmp_path, monkeypatch, value):
    _ledger(tmp_path, monkeypatch, [])
    d = evaluate(_intent(token_id=YES, min_order_size=value), load_risk(), paper_ledger.load_state(MARKET, YES))
    assert d.allow is False and d.reasons == ["missing_min_order_size"], d.reasons


def test_d2_hypothetical_intents_need_it_too(tmp_path, monkeypatch):
    _ledger(tmp_path, monkeypatch, [])
    intent = paper_ledger._intent(min_order_size=None)
    d = evaluate(intent, load_risk(), paper_ledger.load_state(intent.market, intent.token_id))
    assert d.allow is False and "missing_min_order_size" in d.reasons


def test_d2_risk_intent_field_defaults_to_missing():
    fields = {f.name: f for f in __import__("dataclasses").fields(type(_intent()))}
    assert fields["min_order_size"].default is None


def test_d2_intent_builder_passes_the_book_min_order_size():
    oi = build().intent
    assert oi is not None and oi.min_order_size == 5.0
    assert ib.to_risk_intent(oi).min_order_size == 5.0


def test_d2_the_operator_cli_passes_min_order_size(tmp_path, monkeypatch, no_bus):
    _ledger(tmp_path, monkeypatch, [])
    base = dict(strategy="manual", market=MARKET, side="buy", model_prob=0.7, market_price=0.4, slippage=0.005,
                fee_rate=0.0, liquidity=20000.0, spread=0.01, token_id=YES, book_ts=paper_ledger.utc_now(),
                note="", tags="us")
    vetoed = paper_ledger.run_cli_intent(argparse.Namespace(**base), load_risk())
    assert vetoed["allow"] is False and vetoed["decision"]["reasons"] == ["missing_min_order_size"]
    allowed = paper_ledger.run_cli_intent(argparse.Namespace(**base, min_order_size=5.0), load_risk())
    assert allowed["allow"] is True, allowed
    args = paper_ledger.build_parser().parse_args(["--min-order-size", "5"])
    assert args.min_order_size == 5.0
    assert paper_ledger.build_parser().parse_args([]).min_order_size is None


# --- D4: no sell without a position ---------------------------------------------------------------

def test_d4_sell_with_no_position_vetoes(tmp_path, monkeypatch):
    _ledger(tmp_path, monkeypatch, [])
    d = evaluate(_sell(shares=10.0), load_risk(), paper_ledger.load_state(MARKET, YES))
    assert d.allow is False and d.reasons == ["sell_requires_position"], d.reasons


def test_d4_selling_the_other_token_is_not_a_reduce(tmp_path, monkeypatch):
    _ledger(tmp_path, monkeypatch, [_old("buy", 10.0, 4.0)])  # long YES; NO is not held
    state = paper_ledger.load_state(MARKET, NO)
    d = evaluate(_intent(token_id=NO, side="sell", model_prob=0.10, shares=5.0), load_risk(), state)
    assert d.allow is False and "sell_requires_position" in d.reasons


def test_d4_a_flattened_leg_cannot_be_sold_again(tmp_path, monkeypatch):
    _ledger(tmp_path, monkeypatch, [_old("buy", 5.0, 2.0), _old("sell", 5.0, 2.0)])
    state = paper_ledger.load_state(MARKET, YES)
    assert state["leg_already_open"] is False
    d = evaluate(_sell(shares=5.0), load_risk(), state)
    assert d.allow is False and "sell_requires_position" in d.reasons


def test_d4_hypothetical_sell_without_position_vetoes_too(tmp_path, monkeypatch):
    _ledger(tmp_path, monkeypatch, [])
    intent = paper_ledger._intent(side="sell", model_prob=0.2, market_price=0.5, min_order_size=5.0)
    d = evaluate(intent, load_risk(), paper_ledger.load_state(intent.market, intent.token_id))
    assert d.allow is False and "sell_requires_position" in d.reasons
