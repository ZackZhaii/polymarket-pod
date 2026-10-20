"""Issue #265 (b): a sell (reduce / close) never leaves a remainder below the market's min_order_size.

家伟 chose d (a+b); (a) 全平仓豁免最小单量 is suspended (no official evidence), so only (b) is here.
A sell that would leave 0 < remainder < min_order_size vetoes fail-closed (reduce_leaves_residual_below_min);
it is never resized. A remainder under one INTENT_SHARE_STEP is unbookable dust and counts as a full close.
Missing / invalid min_order_size on a sell still vetoes (#226 D2).
"""
from __future__ import annotations

import math

import pytest

import src.paper_ledger as paper_ledger
from src.podlib import load_risk
from src.risk_gate import evaluate
from tests.test_single_leg_exposure import MARKET, YES, _intent, _ledger, _row

OLD_TS = "2026-01-01T00:00:00Z"
CODE = "reduce_leaves_residual_below_min"


def _long(tmp_path, monkeypatch, shares: float):
    _ledger(tmp_path, monkeypatch, [_row(MARKET, YES, "buy", shares, round(shares * 0.4, 6), ts=OLD_TS)])
    return paper_ledger.load_state(MARKET, YES)


def _sell(shares, min_order_size=5.0):
    return _intent(token_id=YES, side="sell", model_prob=0.10, shares=shares, min_order_size=min_order_size)


def test_b1_remainder_exactly_min_is_allowed(tmp_path, monkeypatch):
    d = evaluate(_sell(5.0), load_risk(), _long(tmp_path, monkeypatch, 10.0))
    assert d.allow is True and d.reasons == [], d.reasons
    assert d.shares == pytest.approx(5.0)


@pytest.mark.parametrize("held", [9.99, 8.0, 5.01])
def test_b2_remainder_under_min_is_vetoed(tmp_path, monkeypatch, held):
    """9.99 -> sell 5 leaves 4.99 (one tick under min); 8 leaves 3; 5.01 leaves 0.01."""
    d = evaluate(_sell(5.0), load_risk(), _long(tmp_path, monkeypatch, held))
    assert d.allow is False and d.reasons == [CODE], (d.allow, d.shares, d.reasons)


def test_b2b_clip_sized_sell_leaving_residual_is_vetoed(tmp_path, monkeypatch):
    """Poly套利 P2: no explicit shares, the $10 clip sizes the sell below the position, residual < min."""
    state = _long(tmp_path, monkeypatch, 24.69)
    d = evaluate(_intent(token_id=YES, side="sell", model_prob=0.10, market_price=0.48, min_order_size=5.0),
                 load_risk(), state)
    assert d.shares == pytest.approx(21.05)  # leaves 3.64 < 5
    assert d.allow is False and d.reasons == [CODE], (d.shares, d.reasons)


def test_b3_full_close_is_allowed(tmp_path, monkeypatch):
    d = evaluate(_sell(10.0), load_risk(), _long(tmp_path, monkeypatch, 10.0))
    assert d.allow is True and d.reasons == [] and d.shares == pytest.approx(10.0)


def test_b3b_oversized_sell_capped_to_full_close_is_allowed(tmp_path, monkeypatch):
    d = evaluate(_sell(39.21), load_risk(), _long(tmp_path, monkeypatch, 10.0))
    assert d.allow is True and d.shares == pytest.approx(10.0)


def test_b3c_sub_step_dust_counts_as_full_close(tmp_path, monkeypatch):
    """5.005 held: the close books 5.00; 0.005 is below one share step and can never be booked."""
    d = evaluate(_sell(39.21), load_risk(), _long(tmp_path, monkeypatch, 5.005))
    assert d.allow is True and d.shares == pytest.approx(5.0), d.reasons


def test_b4_large_remainder_is_allowed(tmp_path, monkeypatch):
    d = evaluate(_sell(10.0), load_risk(), _long(tmp_path, monkeypatch, 50.0))
    assert d.allow is True and d.shares == pytest.approx(10.0)


@pytest.mark.parametrize("value", [None, math.nan, math.inf, -1.0, True, "5", [5.0]])
def test_b5_missing_or_invalid_min_on_sell_is_vetoed(tmp_path, monkeypatch, value):
    d = evaluate(_sell(5.0, min_order_size=value), load_risk(), _long(tmp_path, monkeypatch, 10.0))
    assert d.allow is False and "missing_min_order_size" in d.reasons, d.reasons


def _short(tmp_path, monkeypatch, shares: float):
    _ledger(tmp_path, monkeypatch, [_row(MARKET, YES, "sell", shares, round(shares * 0.4, 6), ts=OLD_TS)])
    return paper_ledger.load_state(MARKET, YES)


@pytest.mark.parametrize("held,buy,allow", [(10.0, 5.01, False), (9.99, 5.0, False), (10.0, 5.0, True),
                                            (10.0, 10.0, True), (10.0, 39.21, True)])
def test_b6_short_buyback_leaving_residual_below_min_is_vetoed(tmp_path, monkeypatch, held, buy, allow):
    """Buying back a short leg follows the same rule (|net| - shares): 10->5.01 / 9.99->5 veto; 5 left or flat ok."""
    state = _short(tmp_path, monkeypatch, held)
    assert state["leg_net_shares"] < 0
    d = evaluate(_intent(token_id=YES, side="buy", model_prob=0.90, shares=buy, min_order_size=5.0), load_risk(), state)
    assert d.allow is allow and d.reasons == ([] if allow else [CODE]), (d.shares, d.reasons)


@pytest.mark.parametrize("held,sell", [(10.03, 5.03), (5.0 + 5.03, 5.03)])
def test_b7_residual_the_next_close_would_floor_below_min_is_vetoed(tmp_path, monkeypatch, held, sell):
    """Codex P2 (140d158): 10.03 - 5.03 = 4.999999999999999, which reduce_cap floors to 4.99 (< min, stuck leg)."""
    rest = held - sell
    d = evaluate(_sell(sell), load_risk(), _long(tmp_path, monkeypatch, held))
    from src.risk_gate import reduce_cap
    if reduce_cap("sell", {"leg_already_open": True, "leg_net_shares": rest}) < 5.0:
        assert d.allow is False and d.reasons == [CODE], (rest, d.reasons)
    else:
        assert d.allow is True, (rest, d.reasons)


def test_b7b_the_left_leg_closes_in_one_order(tmp_path, monkeypatch):
    """Whatever b allows to remain must itself be closable: a leg of 10.03 - 5.03 cannot be closed (stuck)."""
    rest = 10.03 - 5.03
    d = evaluate(_sell(39.21), load_risk(), _long(tmp_path, monkeypatch, rest))
    assert d.allow is False  # documents why b7 must veto the first sell
