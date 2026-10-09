"""Task AO: src/intent_builder.py (signal -> OrderIntent). Offline, stdlib + pytest, no network or keys.

Covers the SPEC_signal_to_intent test plan: T-WALK, T-EDGE, T-EQ, T-SIDE, T-BAND, T-MINEDGE, T-ROUND,
T-STALE, T-ORDER, T-CONFIG, T-ENDGAME, T-IDEMP, T-PURE, the blend rules, and the properties P1-P5
(fixed-seed random cases, no hypothesis).
"""
from __future__ import annotations

import ast
import copy
import dataclasses
import math
import random
import socket
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import src.intent_builder as ib
from src import fees, risk_gate
from src.podlib import parse_risk_toml, RISK_PATH
from src.thresholds import (INTENT_ASSUMED_SLIPPAGE, MAX_SIGNAL_AGE_NEWS_S, MAX_SIGNAL_AGE_SPORTS_DEVIG_S,
                            MIN_EDGE, PAPER_EQUITY_USD)

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
NOW_S = "2026-09-01T12:00:00Z"
BOOK_TS = "2026-09-01T11:59:55.000Z"
CFG = parse_risk_toml(RISK_PATH.read_text(encoding="utf-8"))
STATE = {"open_positions": 0, "daily_loss_usd": 0.0, "recent_fills": [], "own_quotes": []}


class _AnyDatetime(type):
    """isinstance(real_datetime, ClockTrap) stays True, so the patched modules still accept real datetimes."""

    def __instancecheck__(cls, obj):
        return isinstance(obj, datetime)

    def __subclasscheck__(cls, sub):
        return issubclass(sub, datetime)


def clock_trap(touched: list[str]) -> type:
    class ClockTrap(datetime, metaclass=_AnyDatetime):
        @classmethod
        def now(cls, tz=None):
            touched.append("datetime.now")
            raise AssertionError("clock read")

        @classmethod
        def utcnow(cls):
            touched.append("datetime.utcnow")
            raise AssertionError("clock read")
    return ClockTrap


def signal(**kw) -> ib.ProbabilitySignal:
    base = dict(signal_id="sig-1", source="news", market_id="m1", outcome_index=0, p=0.47, signal_ts=NOW_S,
                note="test forecast")
    base.update(kw)
    return ib.ProbabilitySignal(**base)


def market(**kw) -> ib.MarketContext:
    base = dict(market_id="m1", condition_id="0xm1", token_ids=("t-yes", "t-no"), outcomes=("Yes", "No"),
                end_date="2026-10-15T00:00:00Z", liquidity_usd=52000.0, fee_rate=0.0, accepting_orders=True,
                closed=False)
    base.update(kw)
    return ib.MarketContext(**base)


def book(token: str, ask: float, bid: float | None = None, *, asks=None, size: float = 400.0, **kw) -> ib.BookSnapshot:
    bid = round(ask - 0.02, 6) if bid is None else bid
    base = dict(market_id="m1", token_id=token, asks=tuple(asks) if asks is not None else ((ask, size),),
                bids=((bid, size),), best_bid=bid, best_ask=ask, spread=round(ask - bid, 6), book_ts=BOOK_TS,
                tick_size=0.01, min_order_size=5.0)
    base.update(kw)
    return ib.BookSnapshot(**base)


def books(yes_ask=0.41, no_ask=0.61, **kw) -> dict[str, ib.BookSnapshot]:
    return {"t-yes": book("t-yes", yes_ask, **kw), "t-no": book("t-no", no_ask, **kw)}


def build(sig=None, mkt=None, bks=None, cfg=None, **kw) -> ib.IntentResult:
    return ib.build_order_intent(sig if sig is not None else signal(), mkt or market(),
                                 bks if bks is not None else books(), cfg if cfg is not None else CFG,
                                 now=kw.pop("now", NOW), strategy=kw.pop("strategy", "news-forecaster"), **kw)


def gate(oi: ib.OrderIntent, cfg=None, tmp: Path | None = None) -> risk_gate.Decision:
    kwargs = {} if tmp is None else {"halt_path": tmp / "halt_state.json", "halt_fallback_path": tmp / "HALT"}
    return risk_gate.evaluate(ib.to_risk_intent(oi), cfg or CFG, copy.deepcopy(STATE), now=NOW, **kwargs)


# ---------------------------------------------------------------- happy path

def test_alpha_like_signal_builds_a_buy_intent_on_outcome_zero():
    result = build()
    assert result.reasons == () and result.first_reason is None
    oi = result.intent
    assert oi is not None
    assert (oi.side, oi.outcome_index, oi.token_id, oi.outcome) == ("buy", 0, "t-yes", "Yes")
    assert oi.exec_price == 0.41 and oi.limit_price == 0.41
    assert oi.edge == pytest.approx(0.055) and oi.edge_yes == oi.edge
    assert oi.edge_no == pytest.approx(0.53 - 0.615)
    assert oi.size_pct == 0.01 and oi.shares == pytest.approx(24.39) and oi.size_usd <= 10.0
    assert oi.paper is True and oi.counterparty is None and oi.assumed_slippage == INTENT_ASSUMED_SLIPPAGE
    assert oi.created_at == "2026-09-01T12:00:00.000Z"
    assert oi.expires_at == "2026-09-01T12:01:00.000Z"  # now + INTENT_TTL_S is the earliest bound
    assert oi.reasons == ("top_of_book_only",)  # kelly_frac (~0.0141) > 1%: no kelly mark
    assert oi.kelly_fraction == 0.15 and oi.kelly_frac == pytest.approx(0.15 * 0.055 / (1 - 0.415))
    assert oi.intent_id == oi.idempotency_key[:32] and len(oi.idempotency_key) == 64


def test_to_risk_intent_matches_the_harness_shape():
    oi = build().intent
    intent = ib.to_risk_intent(oi)
    assert isinstance(intent, risk_gate.Intent)
    assert dataclasses.asdict(intent) == {
        "strategy": "news-forecaster", "market": "m1", "side": "buy", "model_prob": 0.47, "market_price": 0.41,
        "assumed_slippage": 0.005, "fee_rate": 0.0, "liquidity_usd": 52000.0, "spread": 0.02, "token_id": "t-yes",
        "hypothetical": False, "note": "test forecast", "book_ts": BOOK_TS, "counterparty": None,
        # Task AX: identity. The test signal is a legacy one (no forecast_id), so no decision_id is invented.
        "decision_id": None, "forecast_id": None, "forecast_ts": None, "condition_id": oi.condition_id,
        "outcome_index": 0, "outcome": oi.outcome, "category": None, "tags": None, "end_date": oi.end_date,
        "model_version": None,
        # Task BA: the chosen token's top of book and the shadow Kelly multiplier, recorded on the fill only.
        "best_bid": 0.39, "best_ask": 0.41, "kelly_fraction_used": 0.15,
    }
    assert intent.kelly_fraction_used == oi.kelly_fraction and intent.best_ask == oi.best_ask


def test_intent_builder_slippage_meets_the_riskgate_floor(tmp_path):
    """Task AU: INTENT_ASSUMED_SLIPPAGE is the floor itself (inclusive), so builder intents still pass."""
    oi = build().intent
    assert oi.assumed_slippage == risk_gate.ASSUMED_SLIPPAGE
    decision = gate(oi, tmp=tmp_path)
    assert "slippage_below_floor" not in decision.reasons and decision.allow is True, decision.reasons


# ---------------------------------------------------------------- T-WALK

def test_walk_single_level_gives_the_level_price_exactly():
    walk = ib.walk_asks(((0.41, 400.0),), 10.0)
    assert walk.complete and walk.valid and walk.avg_price == 0.41 and walk.worst_price == 0.41
    assert walk.filled_usd == pytest.approx(10.0) and walk.shares == pytest.approx(10 / 0.41)


def test_walk_multi_level_average_and_worst():
    walk = ib.walk_asks(((0.40, 10.0), (0.50, 100.0)), 10.0)  # $4 at 0.40, $6 at 0.50
    assert walk.complete and walk.worst_price == 0.50
    assert walk.shares == pytest.approx(10 + 12)
    assert walk.avg_price == pytest.approx(10.0 / 22.0)


def test_walk_exactly_consumed():
    walk = ib.walk_asks(((0.5, 20.0),), 10.0)
    assert walk.complete and walk.shares == pytest.approx(20.0)


def test_walk_insufficient_depth_is_incomplete():
    walk = ib.walk_asks(((0.5, 10.0), (0.6, 1.0)), 10.0)
    assert walk.valid and not walk.complete


@pytest.mark.parametrize("levels", [(), ((math.nan, 10.0),), ((0.4, math.inf),), ((0.4, -1.0),), ((1.0, 10.0),),
                                    ((0.0, 10.0),), (("0.4", 10.0),), ((True, 10.0),), ((0.5, 10.0), (0.4, 50.0)),
                                    ((0.4,),)])
def test_walk_bad_levels_never_complete(levels):
    walk = ib.walk_asks(levels, 10.0)
    assert not walk.complete
    if levels:
        assert not walk.valid


# ---------------------------------------------------------------- T-EDGE

def test_side_edge_matches_hand_math_and_uses_stress_fee():
    edge = ib.side_edge(0.7, 0.61, 0.02)
    assert edge.fee_per_share == fees.stress_fee(0.02, 0.61, 0.615) == pytest.approx(0.0123)
    assert edge.edge == 0.7 - (0.61 + 0.005) - edge.fee_per_share
    assert edge.effective_price == 0.61 + 0.005


@pytest.mark.parametrize("fee_rate", [None, math.nan, "x", math.inf])
def test_side_edge_bad_fee_fails_closed(fee_rate):
    edge = ib.side_edge(0.7, 0.61, fee_rate)
    assert edge.fee_per_share is None and edge.edge == -1.0


@pytest.mark.parametrize(("fee_rate", "reason"), [(None, "missing_fee_data"), (math.nan, "invalid_fee_rate"),
                                                  ("x", "invalid_fee_rate"), (-0.01, "negative_cost_assumption")])
def test_builder_bad_fee_rate_rejects(fee_rate, reason):
    result = build(mkt=market(fee_rate=fee_rate))
    assert result.intent is None and reason in result.reasons


# ---------------------------------------------------------------- T-EQ (bit-for-bit with RiskGate)

@pytest.mark.parametrize(("p", "yes_ask", "fee_rate"), [(0.47, 0.41, 0.0), (0.7, 0.61, 0.02), (0.55, 0.46, 0.0),
                                                        (0.425 + 0.405 - 0.405, 0.40, 0.0), (0.9, 0.80, 0.05)])
def test_riskgate_edge_equals_builder_edge_exactly(p, yes_ask, fee_rate, tmp_path):
    result = build(sig=signal(p=p), mkt=market(fee_rate=fee_rate), bks=books(yes_ask=yes_ask, no_ask=0.97 - yes_ask))
    assert result.intent is not None, result.reasons
    decision = gate(result.intent, tmp=tmp_path)
    assert decision.edge == result.intent.edge  # same expression, same stress_fee call: bitwise equal
    assert decision.allow, decision.reasons


# ---------------------------------------------------------------- T-SIDE

def test_no_side_is_chosen_when_it_has_the_better_edge():
    result = build(sig=signal(p=0.30), bks=books(yes_ask=0.41, no_ask=0.61))
    oi = result.intent
    assert oi is not None and oi.outcome_index == 1 and oi.token_id == "t-no" and oi.outcome == "No"
    assert oi.p_side == pytest.approx(0.70) and oi.edge == pytest.approx(0.70 - 0.615)


def test_both_sides_negative_is_non_positive_edge():
    result = build(sig=signal(p=0.5), bks=books(yes_ask=0.52, no_ask=0.52))
    assert result.intent is None and result.reasons == ("non_positive_edge",)


def test_tie_takes_outcome_zero():
    result = build(sig=signal(p=0.5), bks=books(yes_ask=0.45, no_ask=0.45))
    assert result.intent is not None and result.intent.outcome_index == 0
    assert result.intent.edge_yes == result.intent.edge_no


def test_outcome_one_signal_is_mirrored_to_outcome_zero():
    a = build(sig=signal(p=0.47, outcome_index=0))
    b = build(sig=signal(p=0.53, outcome_index=1))
    assert a.intent.p_model == pytest.approx(b.intent.p_model)
    assert a.intent.edge == pytest.approx(b.intent.edge) and b.intent.token_id == "t-yes"


def test_interval_uses_the_conservative_end_and_a_wide_interval_kills_both_sides():
    tight = build(sig=signal(p=0.47, p_low=0.46, p_high=0.48)).intent
    assert tight is not None and tight.p_side == 0.46 and tight.edge == pytest.approx(0.045)
    wide = build(sig=signal(p=0.47, p_low=0.30, p_high=0.70))  # interval wraps both prices
    assert wide.intent is None and "non_positive_edge" in wide.reasons


# ---------------------------------------------------------------- T-BAND

@pytest.mark.parametrize(("ask", "inside"), [(0.029, False), (0.03, True), (0.97, True), (0.971, False)])
def test_news_price_band(ask, inside):
    p = min(ask + 0.03, 0.999)
    result = build(sig=signal(p=p), bks={"t-yes": book("t-yes", ask, bid=ask - 0.01, tick_size=0.001,
                                                       size=1e6),
                                         "t-no": book("t-no", 0.99, bid=0.98)})
    assert ("price_out_of_band" in result.reasons) is (not inside), result.reasons


@pytest.mark.parametrize(("ask", "inside"), [(0.979, False), (0.98, True), (0.99, True), (0.991, False)])
def test_endgame_confirm_band(ask, inside):
    result = build(sig=signal(source="endgame_confirm", p=0.9999 if ask < 0.99 else 0.99999),
                   bks={"t-yes": book("t-yes", ask, bid=ask - 0.01, tick_size=0.001, size=1e6),
                        "t-no": book("t-no", 0.5, bid=0.49)})
    assert ("price_out_of_band" in result.reasons) is (not inside), result.reasons


def test_config_band_override_only_narrows():
    loose = dict(CFG, scanner_min_price=0.0, scanner_max_price=1.0)
    assert ib._price_band("news", loose) == (0.03, 0.97)
    tight = dict(CFG, scanner_min_price=0.05, scanner_max_price=0.9)
    assert ib._price_band("news", tight) == (0.05, 0.9)


# ---------------------------------------------------------------- T-MINEDGE

@pytest.mark.parametrize(("p", "reason"), [(0.4349999, "edge_below_min"), (0.435, None), (0.4350001, None)])
def test_min_edge_boundary(p, reason):
    result = build(sig=signal(p=p), bks=books(yes_ask=0.41, no_ask=0.61))  # edge = p - 0.415
    assert (reason in result.reasons) if reason else result.intent is not None, result.reasons


def test_min_edge_float_tolerance_like_riskgate(tmp_path):
    # 0.425 - 0.405 = 0.01999...96 passes in RiskGate (EDGE_TOLERANCE); it must pass here too.
    result = build(sig=signal(p=0.425), bks=books(yes_ask=0.40, no_ask=0.70))
    assert result.intent is not None and result.intent.edge < MIN_EDGE
    assert gate(result.intent, tmp=tmp_path).allow


# ---------------------------------------------------------------- T-ROUND

def test_shares_round_down_and_limit_sits_on_the_tick():
    result = build(bks=books(yes_ask=0.41, no_ask=0.61))
    oi = result.intent
    assert oi.shares == 24.39 and oi.shares * oi.limit_price <= 10.0
    assert round(oi.limit_price / 0.01, 9) == round(oi.limit_price / 0.01)


def test_below_min_order_size_is_rejected_not_rounded_up():
    result = build(bks=books(yes_ask=0.41, no_ask=0.61, min_order_size=25.0))
    assert result.intent is None and result.reasons == ("size_below_min_order",)


@pytest.mark.parametrize(("field", "reason"), [("tick_size", "missing_tick_size"),
                                               ("min_order_size", "missing_min_order_size")])
@pytest.mark.parametrize("value", [None, math.nan, "0.01"])
def test_missing_tick_or_min_order(field, reason, value):
    result = build(bks=books(**{field: value}))
    assert result.intent is None and reason in result.reasons


def test_insufficient_depth_top_of_book():
    result = build(bks=books(size=10.0))  # 10 shares x 0.41 = $4.10 < $10
    assert result.intent is None and "insufficient_depth" in result.reasons


def test_limit_is_the_worst_touched_level():
    deep = {"t-yes": book("t-yes", 0.41, asks=((0.41, 10.0), (0.42, 800.0))), "t-no": book("t-no", 0.61)}
    oi = build(bks=deep).intent
    assert oi is not None and oi.limit_price == 0.42 and oi.exec_price < 0.42
    assert "top_of_book_only" not in oi.reasons


# ---------------------------------------------------------------- T-STALE

@pytest.mark.parametrize(("book_ts", "reason"), [(None, "missing_book_ts"), ("", "missing_book_ts"),
                                                 ("not a time", "invalid_book_ts"), ("2026-09-01T11:59:55", "invalid_book_ts"),
                                                 ("2026-09-01T12:00:01Z", "future_book_ts"),
                                                 ("2026-09-01T11:57:59Z", "stale_book")])
def test_book_staleness_reasons(book_ts, reason):
    result = build(bks=books(book_ts=book_ts))
    assert result.intent is None and reason in result.reasons


def test_book_exactly_120s_old_is_fresh():
    assert build(bks=books(book_ts="2026-09-01T11:58:00Z")).intent is not None


@pytest.mark.parametrize(("ts", "reason"), [(None, "missing_signal_ts"), ("x", "invalid_signal_ts"),
                                            ("2026-09-01T12:00:01Z", "future_signal_ts")])
def test_signal_ts_reasons(ts, reason):
    result = build(sig=signal(signal_ts=ts))
    assert result.intent is None and reason in result.reasons


@pytest.mark.parametrize(("source", "limit"), [("news", MAX_SIGNAL_AGE_NEWS_S),
                                               ("sports_devig", MAX_SIGNAL_AGE_SPORTS_DEVIG_S)])
def test_signal_age_limit_per_source(source, limit):
    edge_ts = (NOW - timedelta(seconds=limit)).strftime("%Y-%m-%dT%H:%M:%SZ")
    old_ts = (NOW - timedelta(seconds=limit + 1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert "stale_signal" not in build(sig=signal(source=source, signal_ts=edge_ts)).reasons
    assert "stale_signal" in build(sig=signal(source=source, signal_ts=old_ts)).reasons


@pytest.mark.parametrize("now", [datetime(2026, 9, 1, 12), "2026-09-01T12:00:00Z", None])
def test_invalid_now_rejects_and_never_reads_the_clock(now, monkeypatch):
    import src.book_staleness as bs
    import src.expiry_rule as er
    touched: list[str] = []
    monkeypatch.setattr(bs, "datetime", clock_trap(touched))
    monkeypatch.setattr(er, "datetime", clock_trap(touched))
    result = build(now=now)
    assert result.intent is None and "invalid_now" in result.reasons and touched == []


# ---------------------------------------------------------------- T-ORDER

def test_reasons_follow_the_canonical_order():
    result = build(sig=signal(signal_ts="x"), mkt=market(liquidity_usd=100.0, closed=True),
                   bks=books(book_ts="2026-09-01T11:00:00Z"), paper=False)
    assert result.reasons[0] == "live_not_permitted" and result.first_reason == "live_not_permitted"
    ranks = [ib._RANK[r.split(":")[0]] for r in result.reasons]
    assert ranks == sorted(ranks)
    for expected in ("invalid_signal_ts", "closed", "stale_book", "liquidity_below_min"):
        assert expected in result.reasons


def test_every_reason_code_is_ranked_once():
    assert len(ib.REASON_ORDER) == len(set(ib.REASON_ORDER))
    source = (ROOT / "src" / "intent_builder.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    appended = {node.args[0].value for node in ast.walk(tree)
                if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "append"
                and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)}
    appended -= set(ib.INFO_MARKS)
    assert appended <= set(ib.REASON_ORDER), appended - set(ib.REASON_ORDER)


# ---------------------------------------------------------------- T-CONFIG

@pytest.mark.parametrize("value", [None, math.nan, 0, 0.0, 1.5, -0.1, "0.15", True])
def test_bad_kelly_fraction(value):
    cfg = dict(CFG)
    if value is None:
        cfg.pop("kelly_fraction")
    else:
        cfg["kelly_fraction"] = value
    result = build(cfg=cfg)
    assert result.intent is None and "invalid_kelly_fraction" in result.reasons


def test_kelly_fraction_one_is_allowed():
    assert build(cfg=dict(CFG, kelly_fraction=1.0)).intent is not None


def test_pin_drift_rejects():
    result = build(cfg=dict(CFG, live_enabled=True))
    assert result.intent is None and result.reasons[0].startswith("phase0_pin:")


def test_live_request_is_not_permitted():
    result = build(paper=False)
    assert result.intent is None and "live_not_permitted" in result.reasons


@pytest.mark.parametrize(("cfg_edit", "reason"), [({"max_order_pct": math.nan}, "invalid_max_order_pct"),
                                                  ({"max_order_pct": "0.01"}, "invalid_max_order_pct"),
                                                  ({"max_spread": None}, "invalid_gate_threshold"),
                                                  ({"min_liquidity_usd": math.inf}, "invalid_gate_threshold")])
def test_bad_config_numbers(cfg_edit, reason):
    result = build(cfg=dict(CFG, **cfg_edit))
    assert result.intent is None and reason in result.reasons


def test_banned_strategy_name():
    result = build(strategy="wash-test")
    assert result.intent is None and "banned_behavior" in result.reasons


def test_looser_max_order_pct_is_still_clipped_at_one_percent():
    loose = dict(CFG, max_order_pct=0.5)
    result = build(cfg=loose)  # pin_problems flags the drift, but the size math must clip regardless
    assert result.detail["size_pct"] == 0.01 and result.detail["size_usd"] <= 10.0


# ---------------------------------------------------------------- T-ENDGAME and other signal sources

def test_raw_endgame_signal_has_no_probability():
    result = build(sig=signal(source="endgame", p=None))
    assert result.intent is None and "missing_model_prob" in result.reasons


def test_endgame_confirm_on_a_fee_market_is_killed_by_the_linear_fee():
    hotel = {"t-yes": book("t-yes", 0.985, bid=0.975, tick_size=0.001, size=1e6), "t-no": book("t-no", 0.5)}
    paid = build(sig=signal(source="endgame_confirm", p=0.999), mkt=market(fee_rate=0.04), bks=hotel)
    assert paid.intent is None and "non_positive_edge" in paid.reasons
    free = build(sig=signal(source="endgame_confirm", p=0.999), mkt=market(fee_rate=0.0), bks=hotel)
    assert "price_out_of_band" not in free.reasons and "non_positive_edge" not in free.reasons


@pytest.mark.parametrize(("p", "reason"), [(math.nan, "non_finite_model_prob"), ("0.5", "invalid_model_prob"),
                                           (0.0, "model_prob_out_of_range"), (1.0, "model_prob_out_of_range"),
                                           (True, "invalid_model_prob")])
def test_bad_probability(p, reason):
    result = build(sig=signal(p=p))
    assert result.intent is None and reason in result.reasons


def test_interval_must_contain_the_point():
    result = build(sig=signal(p=0.47, p_low=0.48, p_high=0.5))
    assert result.intent is None and "model_prob_out_of_range" in result.reasons


@pytest.mark.parametrize("source", ["", "Scanner", "maker"])
def test_unsupported_source(source):
    assert "unsupported_signal_source" in build(sig=signal(source=source)).reasons


def test_missing_signal():
    result = ib.build_order_intent(None, market(), books(), CFG, now=NOW, strategy="news-forecaster")
    assert result.intent is None and "missing_signal" in result.reasons


# ---------------------------------------------------------------- market checks

@pytest.mark.parametrize(("edit", "reason"), [
    ({"token_ids": ()}, "missing_token_ids"), ({"token_ids": ("a", "b", "c")}, "not_binary_market"),
    ({"condition_id": ""}, "missing_condition_id"), ({"closed": True}, "closed"),
    ({"accepting_orders": False}, "not_accepting_orders"), ({"accepting_orders": None}, "not_accepting_orders"),
    ({"end_date": ""}, "missing_end_date"), ({"end_date": "2026-10-15"}, "invalid_end_date"),
    ({"end_date": "2026-09-01T00:00:00Z"}, "expired"), ({"end_date": "2026-09-02T06:00:00Z"}, "near_expiry"),
    ({"resolution_flag": "ambiguous"}, "ambiguous_resolution"), ({"liquidity_usd": None}, "missing_liquidity"),
    ({"liquidity_usd": math.nan}, "invalid_liquidity"), ({"liquidity_usd": 9999.0}, "liquidity_below_min"),
    ({"market_id": "other"}, "market_mismatch"),
])
def test_market_reasons(edit, reason):
    result = build(mkt=market(**edit))
    assert result.intent is None and reason in result.reasons, result.reasons


def test_category_ban_and_token_mismatch():
    assert "category_banned" in build(mkt=market(category="x"), banned_categories=frozenset({"x"})).reasons
    assert "token_mismatch" in build(sig=signal(token_id="t-no")).reasons
    assert build(sig=signal(token_id="t-yes")).intent is not None


@pytest.mark.parametrize(("spread", "reason"), [(None, "missing_spread"), (math.nan, "invalid_spread"),
                                                (-0.01, "negative_spread"), (0.041, "spread_above_max")])
def test_spread_reasons(spread, reason):
    result = build(bks=books(spread=spread))
    assert result.intent is None and reason in result.reasons


def test_missing_or_mismatched_book():
    assert "missing_book" in build(bks={}).reasons
    # Only the YES book is missing: the NO side is still judged on its own book (here: no edge).
    assert build(bks={"t-no": book("t-no", 0.61)}).reasons == ("non_positive_edge",)
    wrong = {"t-yes": book("t-yes", 0.41, market_id="other"), "t-no": book("t-no", 0.61, token_id="t-yes")}
    assert "token_book_mismatch" in build(bks=wrong).reasons


def test_nan_ask_on_the_wanted_side_is_rejected():
    bad = {"t-yes": book("t-yes", math.nan, bid=0.49, spread=math.nan), "t-no": book("t-no", 0.51, bid=0.49)}
    result = build(sig=signal(p=0.6), bks=bad)
    assert result.intent is None and "non_positive_edge" in result.reasons


def test_sell_is_not_built_here():
    assert build(side="sell").reasons == ("sell_requires_position",)
    assert build(side="sell", held_shares=10.0).reasons == ("sell_not_supported",)
    assert build(side="sell", held_shares=math.nan).reasons == ("sell_requires_position",)
    assert build(side="hold").reasons == ("sell_not_supported",)
    assert build(side="sell", held_shares=10.0).intent is None
    # config / signal / market reasons still come first
    assert build(side="sell", mkt=market(closed=True)).reasons == ("closed", "sell_requires_position")


# ---------------------------------------------------------------- Kelly is shadow only

def test_kelly_below_clip_is_marked_and_does_not_change_size():
    oi = build(sig=signal(p=0.4351)).intent  # edge ~0.0201 -> kelly_frac ~0.5%
    assert oi is not None and oi.kelly_frac < 0.01
    assert oi.reasons[0] == "kelly_not_enforced_by_riskgate"
    assert oi.size_pct == 0.01 and oi.kelly_size_pct == oi.kelly_frac
    assert oi.kelly_size_usd == pytest.approx(PAPER_EQUITY_USD * oi.kelly_frac)
    assert oi.size_usd == build().intent.size_usd  # same 1% clip as the strong signal


def test_kelly_fraction_does_not_change_size():
    low = build(cfg=dict(CFG, kelly_fraction=0.01)).intent
    high = build(cfg=dict(CFG, kelly_fraction=1.0)).intent
    assert low.size_usd == high.size_usd and low.shares == high.shares
    assert low.kelly_frac < high.kelly_frac


# ---------------------------------------------------------------- T-IDEMP

def test_same_input_same_key_and_seen_key_is_duplicate():
    a, b = build().intent, build().intent
    assert a.idempotency_key == b.idempotency_key and a == b
    dup = build(seen_keys=frozenset({a.idempotency_key}))
    assert dup.intent is None and dup.reasons == ("duplicate_intent",)
    other = build(sig=signal(signal_id="sig-2")).intent
    assert other.idempotency_key != a.idempotency_key


# ---------------------------------------------------------------- T-PURE

def test_builder_is_pure(monkeypatch):
    touched: list[str] = []

    def trip(label):
        def blocked(*a, **k):
            touched.append(label)
            raise AssertionError(label)
        return blocked

    monkeypatch.setattr(socket, "socket", trip("socket"))
    monkeypatch.setattr(socket, "create_connection", trip("create_connection"))
    monkeypatch.setattr(urllib.request, "urlopen", trip("urlopen"))
    monkeypatch.setattr("builtins.open", trip("open"))
    import src.book_staleness as bs
    import src.expiry_rule as er

    monkeypatch.setattr(bs, "datetime", clock_trap(touched))
    monkeypatch.setattr(er, "datetime", clock_trap(touched))
    cfg, sig, mkt, bks = dict(CFG), signal(), market(), books()
    before = (copy.deepcopy(cfg), sig, mkt, dict(bks))
    result = ib.build_order_intent(sig, mkt, bks, cfg, now=NOW, strategy="news-forecaster")
    assert result.intent is not None and touched == []
    assert (cfg, sig, mkt, bks) == before


def test_module_imports_only_the_allowed_modules():
    tree = ast.parse((ROOT / "src" / "intent_builder.py").read_text(encoding="utf-8"))
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            mods.add(node.module if node.level == 0 else "." + (node.module or ""))
            if node.module == "src":
                mods |= {f"src.{alias.name}" for alias in node.names}
    allowed = {"__future__", "hashlib", "json", "math", "collections.abc", "dataclasses", "datetime", "src",
               "src.fees", "src.book_staleness", "src.expiry_rule", "src.podlib", "src.risk_gate", "src.thresholds",
               "src.calibration_ids"}  # Task AX: stdlib-only id helpers
    assert mods <= allowed, mods - allowed
    text = (ROOT / "src" / "intent_builder.py").read_text(encoding="utf-8")
    for banned in ("datetime.now", "utcnow", "open(", "load_risk", "urlopen", "requests", "py_clob_client"):
        assert banned not in text, banned


# ---------------------------------------------------------------- blend / override

def test_single_signal_passes_through():
    sig = signal()
    assert ib.combine_probability([sig]) == (sig, [])


def test_override_wins_alone_and_needs_a_note():
    override = signal(source="forecaster_override", p=0.6, note="resolution already known")
    assert ib.combine_probability([signal()], override) == (override, [])
    assert ib.combine_probability([signal()], signal(source="forecaster_override", note=""))[0] is None


def test_blend_equal_weights_on_logit_and_union_interval():
    a = signal(signal_id="a", p=0.40, signal_ts="2026-09-01T11:59:00Z")
    b = signal(signal_id="b", source="sports_devig", p=0.50, signal_ts="2026-09-01T11:58:00Z")
    blended, reasons = ib.combine_probability([a, b])
    assert reasons == [] and blended.source == "blend"
    expected = 1 / (1 + math.exp(-(math.log(0.4 / 0.6) + 0.0) / 2))
    assert blended.p == pytest.approx(expected)
    assert (blended.p_low, blended.p_high) == (0.40, 0.50)
    assert blended.signal_ts == "2026-09-01T11:58:00Z"  # the oldest input


def test_blend_conflict():
    a, b = signal(signal_id="a", p=0.30), signal(signal_id="b", source="sports_devig", p=0.60)
    assert ib.combine_probability([a, b]) == (None, ["source_conflict"])


def test_blend_precision_weights():
    a = signal(signal_id="a", p=0.40, p_low=0.39, p_high=0.41)
    b = signal(signal_id="b", source="sports_devig", p=0.50, p_low=0.40, p_high=0.60)
    blended, _ = ib.combine_probability([a, b])
    assert abs(blended.p - 0.40) < abs(blended.p - 0.50)  # the tighter source dominates


def test_blend_rejects_mixed_markets_and_empty():
    assert ib.combine_probability([]) == (None, ["missing_signal"])
    assert "market_mismatch" in ib.combine_probability([signal(signal_id="a"), signal(signal_id="b", market_id="x")])[1]


def test_blend_freshness_is_the_strictest():
    assert ib.MAX_SIGNAL_AGE_BY_SOURCE["blend"] == min(ib.MAX_SIGNAL_AGE_BY_SOURCE[s]
                                                       for s in ("news", "sports_devig", "endgame_confirm"))


# ---------------------------------------------------------------- adapters

def test_adapters_from_bus_and_rows():
    row = {"ts": NOW_S, "from": "NewsForecaster", "to": "RiskGate", "type": "signal", "market": "m1",
           "payload": {"model_prob": 0.47, "order": False, "note": "n"}}
    sig = ib.signal_from_bus(row)
    assert (sig.source, sig.p, sig.outcome_index, sig.signal_ts) == ("news", 0.47, 0, NOW_S)
    assert sig.signal_id == ib.signal_id_for(row) and len(sig.signal_id) == 64
    endgame = ib.signal_from_bus({**row, "from": "Endgame", "payload": {"price": 0.985}})
    assert endgame.p is None and "missing_model_prob" in build(sig=endgame).reasons
    devig = ib.signal_from_bus({**row, "from": "SportsDevig", "payload": {"fair_prob": 0.6, "edge": 0.5}})
    assert devig.source == "sports_devig" and devig.p == 0.6
    assert ib.signal_from_bus({**row, "from": "Scanner"}) is None
    snap = ib.book_snapshot_from_row({"market_id": "m1", "token_id": "t", "best_ask": 0.41, "best_ask_size": 400.0,
                                      "best_bid": 0.39, "best_bid_size": 9.0, "spread": 0.02, "book_ts": BOOK_TS,
                                      "tick_size": 0.01, "min_order_size": 5.0})
    assert snap.asks == ((0.41, 400.0),) and snap.bids == ((0.39, 9.0),)
    ctx = ib.market_context_from_row({"id": "m1", "condition_id": "0x", "token_ids": ["1", "2"], "outcomes": ["Yes", "No"],
                                      "end_date": "2026-10-15T00:00:00Z", "liquidity_usd": 1.0,
                                      "accepting_orders": True, "closed": False}, fee_rate=0.0)
    assert ctx.token_ids == ("1", "2") and ctx.closed is False
    assert ib.market_context_from_row({"id": "m1"}, fee_rate=0.0).closed is True  # unknown counts as closed


# ---------------------------------------------------------------- properties (fixed seed, stdlib random)

def _random_case(rng: random.Random):
    yes = round(rng.uniform(0.02, 0.98), 2)
    no = round(rng.uniform(0.02, 0.98), 2)
    p = rng.uniform(0.01, 0.99)
    half = rng.choice([0.0, 0.0, rng.uniform(0.0, 0.1)])
    low, high = max(0.001, p - half), min(0.999, p + half)
    size = rng.choice([5.0, 20.0, 50.0, 400.0, 5000.0])
    second = rng.random() < 0.5
    def lvl(ask):
        return ((ask, size), (min(round(ask + 0.01, 2), 0.99), 1000.0)) if second and ask < 0.98 else ((ask, size),)
    bks = {"t-yes": book("t-yes", yes, asks=lvl(yes), min_order_size=rng.choice([0.0, 5.0, 15.0, 30.0])),
           "t-no": book("t-no", no, asks=lvl(no), min_order_size=rng.choice([0.0, 5.0, 15.0, 30.0]))}
    return (signal(p=p, p_low=low, p_high=high), market(fee_rate=rng.choice([0.0, 0.0, 0.02, 0.05])), bks,
            dict(CFG, kelly_fraction=rng.choice([0.05, 0.15, 0.5, 1.0])))


CASES = 10_000


def test_properties_p2_p3_p5(tmp_path):
    rng = random.Random(20261009)
    produced = 0
    for _ in range(CASES):
        sig, mkt, bks, cfg = _random_case(rng)
        result = ib.build_order_intent(sig, mkt, bks, cfg, now=NOW, strategy="news-forecaster")
        again = ib.build_order_intent(sig, mkt, bks, cfg, now=NOW, strategy="news-forecaster")
        assert result == again  # P5 determinism (including the key)
        best = max(result.detail.get("edge_yes", -1.0), result.detail.get("edge_no", -1.0))
        if best <= 0:  # P2: no positive edge on either side -> no intent
            assert result.intent is None and "non_positive_edge" in result.reasons
        if result.intent is not None:
            produced += 1
            assert result.intent.size_usd <= PAPER_EQUITY_USD * 0.01 + 1e-12
            if produced <= 400:  # P3: RiskGate on the same intent never vetoes for edge/price/book reasons
                decision = gate(result.intent, cfg=cfg, tmp=tmp_path)
                assert decision.edge == result.intent.edge
                assert decision.allow, decision.reasons
    assert produced > 100


def test_property_p3_riskgate_vetoes_imply_builder_rejects(tmp_path):
    """Feed RiskGate the harness-style intent for outcome 0; when it vetoes for an edge/book/price
    reason, the builder (which would pick outcome 0 for a buy at least as good) must not produce an
    intent on that same token."""
    rng = random.Random(7)
    checked = 0
    gate_reasons = {"non_positive_edge", "edge_below_min", "spread_above_max", "liquidity_below_min", "stale_book",
                    "market_price_out_of_range", "effective_price_out_of_range"}
    for _ in range(3000):
        sig, mkt, bks, cfg = _random_case(rng)
        harness = risk_gate.Intent(strategy="news-forecaster", market="m1", side="buy", model_prob=sig.p_low,
                                   market_price=bks["t-yes"].best_ask, assumed_slippage=0.005, fee_rate=mkt.fee_rate,
                                   liquidity_usd=mkt.liquidity_usd, spread=bks["t-yes"].spread, token_id="t-yes",
                                   book_ts=BOOK_TS)
        decision = risk_gate.evaluate(harness, cfg, copy.deepcopy(STATE), now=NOW,
                                      halt_path=tmp_path / "h.json", halt_fallback_path=tmp_path / "HALT")
        result = ib.build_order_intent(sig, mkt, bks, cfg, now=NOW, strategy="news-forecaster")
        if set(decision.reasons) & gate_reasons:
            checked += 1
            assert result.intent is None or result.intent.token_id != "t-yes"
    assert checked > 100


def test_property_p1_tightening_never_increases_size():
    rng = random.Random(11)
    for _ in range(2000):
        sig, mkt, bks, cfg = _random_case(rng)
        base = ib.build_order_intent(sig, mkt, bks, cfg, now=NOW, strategy="news-forecaster")
        if base.intent is None:
            continue
        tighter = [
            (dict(cfg, kelly_fraction=cfg["kelly_fraction"] / 2), bks),
            (cfg, {k: dataclasses.replace(v, min_order_size=(v.min_order_size or 0) + 10) for k, v in bks.items()}),
            (cfg, {k: dataclasses.replace(v, asks=tuple((p, s / 2) for p, s in v.asks)) for k, v in bks.items()}),
        ]
        for cfg2, bks2 in tighter:
            other = ib.build_order_intent(sig, mkt, bks2, cfg2, now=NOW, strategy="news-forecaster")
            if other.intent is not None:
                # The notional target is the fixed 1% clip; what may only shrink is the share count on the
                # same token (a shallower book walks to a higher limit, so fewer shares fit). The rounded
                # notional is always within the clip.
                assert other.intent.size_usd <= PAPER_EQUITY_USD * 0.01 + 1e-12
                if other.intent.token_id == base.intent.token_id:
                    assert other.intent.shares <= base.intent.shares + 1e-12
                    assert other.intent.edge <= base.intent.edge + 1e-12
        # a smaller max_order_pct: shrinks the clip (pin_problems also rejects the drift)
        small = ib.build_order_intent(sig, mkt, bks, dict(cfg, max_order_pct=0.005), now=NOW, strategy="news-forecaster")
        if "shares" in small.detail and small.detail["limit_price"] == base.intent.limit_price:
            assert small.detail["shares"] <= base.intent.shares
            assert small.detail["size_usd"] <= base.intent.size_usd + 1e-12


def test_property_p4_wider_interval_never_increases_edge_or_size():
    rng = random.Random(13)
    for _ in range(3000):
        sig, mkt, bks, cfg = _random_case(rng)
        base = ib.build_order_intent(sig, mkt, bks, cfg, now=NOW, strategy="news-forecaster")
        wider = dataclasses.replace(sig, p_low=max(0.0005, sig.p_low - 0.02), p_high=min(0.9995, sig.p_high + 0.02))
        other = ib.build_order_intent(wider, mkt, bks, cfg, now=NOW, strategy="news-forecaster")
        assert max(other.detail["edge_yes"], other.detail["edge_no"]) <= max(base.detail["edge_yes"],
                                                                            base.detail["edge_no"]) + 1e-12
        if other.intent is not None:
            assert base.intent is not None
            if other.intent.token_id == base.intent.token_id:
                assert other.intent.shares <= base.intent.shares and other.intent.edge <= base.intent.edge + 1e-12
