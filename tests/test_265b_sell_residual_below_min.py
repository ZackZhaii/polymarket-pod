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


def _apply_sell(tmp_path, monkeypatch, held, sells, min_order_size=5.0):
    """Book `held`, then each allowed sell in order through the real ledger; returns the decisions."""
    rows = [_row(MARKET, YES, "buy", held, round(held * 0.4, 6), ts=OLD_TS)]
    out = []
    for i, s in enumerate(sells, 1):
        step = tmp_path / f"step{i}"  # fresh dir: the daily-marks snapshot belongs to one ledger state
        step.mkdir()
        _ledger(step, monkeypatch, rows)
        monkeypatch.setattr(paper_ledger, "DAILY_MARKS_DIR", step / "daily_marks")
        d = evaluate(_sell(s, min_order_size), load_risk(), paper_ledger.load_state(MARKET, YES))
        out.append(d)
        if d.allow:
            rows.append(_row(MARKET, YES, "sell", d.shares, round(d.shares * 0.4, 6),
                             ts=f"2026-01-01T00:0{i}:00Z"))
    return out


def test_b8_reduce_then_full_close_sequence_is_allowed(tmp_path, monkeypatch):
    """Codex P2-1: 10.04 -> sell 5.03 (5.01 left) -> full close; the ledger nets 5.009999999999999."""
    d1, d2 = _apply_sell(tmp_path, monkeypatch, 10.04, [5.03, 39.21])
    assert d1.allow is True and d1.shares == pytest.approx(5.03), d1.reasons
    assert d2.allow is True and d2.reasons == [], d2.reasons


@pytest.mark.parametrize("held,first", [(10.04, 5.03), (20.0, 7.5), (15.07, 10.07)])
def test_b8b_every_allowed_sell_leaves_a_closable_leg(tmp_path, monkeypatch, held, first):
    d1, d2 = _apply_sell(tmp_path, monkeypatch, held, [first, 39.21])
    assert d1.allow is True, d1.reasons
    assert d2.allow is True, (d2.shares, d2.reasons)


def test_b9_min_just_above_five_is_strict(tmp_path, monkeypatch):
    """Codex P2-2: min=5.000000000000001, 10.01 sell 5.01 leaves 5.00 < min -> veto (no eps fail-open)."""
    d = evaluate(_sell(5.01, 5.000000000000001), load_risk(), _long(tmp_path, monkeypatch, 10.01))
    assert d.allow is False and d.reasons == [CODE], (d.shares, d.reasons)


@pytest.mark.parametrize("held,sell,allow", [
    (10.01, 10.0, False),   # one hundredth left (rem_c == 1) -> veto
    (10.0, 9.99, False),
    (10.0, 10.0, True),     # rem_c == 0
    (10.005, 10.0, True),   # sub-step dust; selling all of reduce_cap is a full close
    (11.0, 6.0, True),      # exactly min left
    (10.99, 6.0, False),    # 4.99 left
])
def test_b10_dust_branch_boundaries(tmp_path, monkeypatch, held, sell, allow):
    """Directly pins the hundredths / full-close branch (kills a `rem_c > 1` / `min - 1` style mutation)."""
    d = evaluate(_sell(sell), load_risk(), _long(tmp_path, monkeypatch, held))
    assert d.allow is allow and d.reasons == ([] if allow else [CODE]), (d.shares, d.reasons)


@pytest.mark.parametrize("held,sell", [(10.0, 9.995), (10.03, 5.029999999), (10.0, 9.999), (50.0, 5.005), (20.0, 5.000000001)])
def test_b11_off_grid_reduce_is_vetoed(tmp_path, monkeypatch, held, sell):
    """Codex P2 (9e49882) / Poly风控 6312297678 (a): a reduce off the 0.01 grid vetoes (the ledger books round(x, 8))."""
    d = evaluate(_sell(sell), load_risk(), _long(tmp_path, monkeypatch, held))
    assert d.allow is False and d.reasons == ["reduce_size_off_grid"], (d.shares, d.reasons)


@pytest.mark.parametrize("held,sell", [(20.0, 5.03), (20.0, 0.07 * 100), (20.0, 0.1 * 3 + 5.0)])
def test_b11b_on_grid_float_noise_is_not_off_grid(tmp_path, monkeypatch, held, sell):
    d = evaluate(_sell(sell), load_risk(), _long(tmp_path, monkeypatch, held))
    assert "reduce_size_off_grid" not in d.reasons and d.allow is True, (d.shares, d.reasons)


def test_b12_real_commit_path_off_grid_sell_books_nothing(tmp_path, monkeypatch):
    """Through paper_ledger.commit_if_allowed: 10.03 sell 5.029999999 is vetoed and no stranded leg is booked."""
    from src import halt_store
    _long(tmp_path, monkeypatch, 10.03)
    monkeypatch.setattr(halt_store, "HALT_STATE_PATH", tmp_path / "halt_state.json")
    monkeypatch.setattr(halt_store, "HALT_FALLBACK_PATH", tmp_path / "HALT")
    monkeypatch.setattr(paper_ledger, "bus", lambda *a, **k: None)
    before = paper_ledger.PAPER_PATH.read_text(encoding="utf-8")
    result = paper_ledger.commit_if_allowed(_sell(5.029999999), load_risk())
    assert result["allow"] is False and "reduce_size_off_grid" in result["decision"]["reasons"], result
    assert paper_ledger.PAPER_PATH.read_text(encoding="utf-8") == before
    assert paper_ledger.load_state(MARKET, YES)["leg_net_shares"] == pytest.approx(10.03)


def test_b12b_real_commit_path_reduce_then_full_close(tmp_path, monkeypatch):
    """Through commit_if_allowed (round(shares, 8) booking): 10.04 -> sell 5.03 -> full close leaves a flat leg."""
    from src import halt_store
    _long(tmp_path, monkeypatch, 10.04)
    monkeypatch.setattr(halt_store, "HALT_STATE_PATH", tmp_path / "halt_state.json")
    monkeypatch.setattr(halt_store, "HALT_FALLBACK_PATH", tmp_path / "HALT")
    monkeypatch.setattr(paper_ledger, "bus", lambda *a, **k: None)
    first = paper_ledger.commit_if_allowed(_sell(5.03), load_risk())
    assert first["allow"] is True, first
    monkeypatch.setattr(paper_ledger, "DAILY_MARKS_DIR", tmp_path / "marks2")
    second = paper_ledger.commit_if_allowed(_sell(39.21), load_risk())
    assert second["allow"] is True, second
    monkeypatch.setattr(paper_ledger, "DAILY_MARKS_DIR", tmp_path / "marks3")
    assert abs(paper_ledger.load_state(MARKET, YES)["leg_net_shares"] or 0.0) < 0.01
