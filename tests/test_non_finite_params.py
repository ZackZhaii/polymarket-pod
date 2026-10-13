"""Task U: non-finite numeric inputs (NaN, +inf, -inf) veto, fail closed.

Before: fee_rate=NaN made the edge NaN, and `NaN <= 0` is False, so non_positive_edge never
fired. Pure tightening: no risk.toml change, no threshold change. Offline, no credentials.
"""
from __future__ import annotations

import json
import math

import pytest

import src.maker as maker
import src.paper_ledger as paper_ledger
import src.sports_devig as sports_devig
from src.podlib import RISK_PATH, load_risk, read_jsonl, utc_now
from src.risk_gate import Intent, evaluate

STATE = {"open_positions": 0, "leg_already_open": False, "exposure_usd": 0.0, "leg_exposure_usd": 0.0, "opposite_leg_shares": 0.0, "opposite_pending_buys": 0, "daily_loss_usd": 0.0,
         "recent_fills": [], "own_quotes": []}
NON_FINITE = [float("nan"), float("inf"), float("-inf")]
FIELDS = ["model_prob", "market_price", "assumed_slippage", "fee_rate"]


def _intent(**overrides: object) -> Intent:
    fields: dict[str, object] = {
        "strategy": "task-u-check", "market": "task-u-market", "token_id": "tok", "side": "buy",
        "model_prob": 0.70, "market_price": 0.40, "assumed_slippage": 0.005, "fee_rate": 0.0,
        "liquidity_usd": 20000.0, "spread": 0.01, "hypothetical": False, "book_ts": utc_now(),
    }
    fields.update(overrides)
    fields.setdefault("tags", ("test-market",))  # issue #131: a tagged market (missing tags veto)
    fields.setdefault("min_order_size", 5.0)  # issue #226: a book minimum (missing vetoes)
    return Intent(**fields)  # type: ignore[arg-type]


def test_clean_intent_still_allows():
    decision = evaluate(_intent(), load_risk(), STATE)
    assert decision.allow is True, decision.reasons


@pytest.mark.parametrize("value", NON_FINITE, ids=["nan", "inf", "-inf"])
def test_fee_rate_non_finite_vetoes(value):
    decision = evaluate(_intent(fee_rate=value), load_risk(), STATE)
    assert decision.allow is False
    assert "non_finite_fee_rate" in decision.reasons
    assert math.isfinite(decision.edge) and math.isfinite(decision.fee_usd)


def test_fee_rate_nan_no_longer_skips_the_edge_check():
    decision = evaluate(_intent(fee_rate=float("nan")), load_risk(), STATE)
    assert "non_positive_edge" in decision.reasons
    assert decision.edge == -1.0


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("value", NON_FINITE, ids=["nan", "inf", "-inf"])
def test_every_core_number_non_finite_vetoes(field, value):
    before = RISK_PATH.read_bytes()
    decision = evaluate(_intent(**{field: value}), load_risk(), STATE)
    assert decision.allow is False
    assert f"non_finite_{field}" in decision.reasons
    assert "non_positive_edge" in decision.reasons
    assert math.isfinite(decision.edge) and math.isfinite(decision.fee_usd) and math.isfinite(decision.shares)
    assert RISK_PATH.read_bytes() == before


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("value", ["0.5", None, [0.5]], ids=["str", "none", "list"])
def test_non_numbers_veto_instead_of_crashing(field, value):
    decision = evaluate(_intent(**{field: value}), load_risk(), STATE)
    assert decision.allow is False
    assert f"invalid_{field}" in decision.reasons


@pytest.mark.parametrize("value", NON_FINITE + ["0.01x", None], ids=["nan", "inf", "-inf", "garbage", "none"])
def test_non_finite_max_order_pct_vetoes(value):
    cfg = dict(load_risk())
    cfg["max_order_pct"] = value
    decision = evaluate(_intent(), cfg, STATE)
    assert decision.allow is False
    assert "invalid_max_order_pct" in decision.reasons
    assert decision.notional_usd == 0.0 and math.isfinite(decision.fee_usd)


@pytest.mark.parametrize("value", NON_FINITE, ids=["nan", "inf", "-inf"])
def test_commit_if_allowed_never_writes_a_non_finite_fill(tmp_path, monkeypatch, value):
    monkeypatch.setattr(paper_ledger, "PAPER_PATH", tmp_path / "paper.jsonl")
    monkeypatch.setattr(paper_ledger, "QUOTES_PATH", tmp_path / "quotes.jsonl")
    monkeypatch.setattr(paper_ledger, "bus", lambda *args, **kwargs: None)
    result = paper_ledger.commit_if_allowed(_intent(fee_rate=value), load_risk())
    assert result["allow"] is False
    assert "non_finite_fee_rate" in result["decision"]["reasons"]
    assert not (tmp_path / "paper.jsonl").exists()


# --- paper_ledger fee / position math --------------------------------------------------


def _ledger(tmp_path, monkeypatch, rows):
    path = tmp_path / "paper.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    monkeypatch.setattr(paper_ledger, "PAPER_PATH", path)
    monkeypatch.setattr(paper_ledger, "QUOTES_PATH", tmp_path / "quotes.jsonl")


def _row(**extra):
    row = {"ts": utc_now(), "market": "m", "token_id": "t", "side": "buy", "shares": 10.0,
           "notional_usd": 4.0, "fee_usd": 0.0, "hypothetical": False, "counts_toward_risk": True}
    row.update(extra)
    return row


@pytest.mark.parametrize("field", ["shares", "notional_usd", "fee_usd"])
@pytest.mark.parametrize("value", NON_FINITE, ids=["nan", "inf", "-inf"])
def test_non_finite_ledger_numbers_make_risk_state_invalid(tmp_path, monkeypatch, field, value):
    _ledger(tmp_path, monkeypatch, [_row(**{field: value})])
    state = paper_ledger.load_state("other", "x")
    decision = evaluate(_intent(market="other", token_id="x"), load_risk(), state)
    assert decision.allow is False
    assert "invalid_risk_state" in decision.reasons


def test_nan_shares_cannot_hide_an_open_leg(tmp_path, monkeypatch):
    # Before: abs(nan) > eps is False, so a NaN row looked like a closed leg.
    _ledger(tmp_path, monkeypatch, [_row(shares=float("nan"))])
    assert paper_ledger.load_state("m", "t")["open_positions"] is None


def test_finite_ledger_still_counts_normally(tmp_path, monkeypatch):
    _ledger(tmp_path, monkeypatch, [_row()])
    state = paper_ledger.load_state("m", "t")
    assert state["open_positions"] == 1 and state["daily_fees_usd"] + state["daily_realized_loss_usd"] == 0.0
    # Task CF (#124): the open leg has no book, so its cost is today's unrealized loss.
    assert state["daily_loss_usd"] == pytest.approx(state["daily_unrealized_loss_usd"]) and state["daily_loss_usd"] > 0


def test_non_finite_numbers_in_non_counting_rows_are_ignored(tmp_path, monkeypatch):
    _ledger(tmp_path, monkeypatch, [_row(shares=float("nan"), hypothetical=True, counts_toward_risk=False), _row()])
    assert paper_ledger.load_state("m", "t")["open_positions"] == 1


# --- signal helpers ------------------------------------------------------------------------


@pytest.mark.parametrize("odds", NON_FINITE, ids=["nan", "inf", "-inf"])
def test_sports_devig_rejects_non_finite_odds(tmp_path, odds):
    assert sports_devig.fair_two_way(odds, 2.0) is None
    assert sports_devig.fair_two_way(2.0, odds) is None
    assert sports_devig.emit(odds, 2.0, 0.30, "m", bus_path=tmp_path / "bus.jsonl") is None
    assert not (tmp_path / "bus.jsonl").exists()


@pytest.mark.parametrize("value", NON_FINITE, ids=["nan", "inf", "-inf"])
def test_sports_devig_rejects_non_finite_price_and_slippage(tmp_path, value):
    bus_path = tmp_path / "bus.jsonl"
    assert sports_devig.emit(1.5, 3.0, value, "m", bus_path=bus_path) is None
    assert sports_devig.emit(1.5, 3.0, 0.30, "m", slippage=value, bus_path=bus_path) is None
    assert not bus_path.exists()


def test_sports_devig_finite_signal_still_emits(tmp_path):
    assert sports_devig.emit(1.5, 3.0, 0.30, "m", bus_path=tmp_path / "bus.jsonl") is not None


@pytest.mark.parametrize("field", ["price", "size"])
@pytest.mark.parametrize("value", NON_FINITE, ids=["nan", "inf", "-inf"])
def test_maker_quote_rejects_non_finite(tmp_path, field, value):
    args = {"market": "m", "token_id": "t", "side": "buy", "price": 0.40, "size": 5.0}
    args[field] = value
    with pytest.raises(ValueError):
        maker.quote(**args, bus_path=tmp_path / "bus.jsonl", quotes_path=tmp_path / "quotes.jsonl")
    assert read_jsonl(tmp_path / "quotes.jsonl") == []
