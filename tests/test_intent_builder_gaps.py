"""Task AZ: the untested fail-closed branches of src/intent_builder.py (signal -> OrderIntent).

Gap found by walking ``intent_builder.REASON_ORDER`` against the suite: the codes
``invalid_outcome_index``, ``invalid_book_levels``, ``zero_notional`` and ``kelly_non_positive`` are
appended by the builder but no test in tests/ asserted any of them. A signal may name outcome 2 or a
boolean index (it must never be read as outcome 0), an ask ladder may be non-ascending or hold a
level outside (0, 1) (it must never be walked to a bogus average price), a drifted ``max_order_pct``
of 0 must not size, and the shadow Kelly must report a non-positive result without sizing.

Boundaries covered next to those branches: outcome index 0/1 only, a probability interval touching 0
or 1, a non-positive / non-finite / non-numeric walk budget, a book row whose top of book is
incomplete, and a blend member with an unsupported source.

Offline and pure: no clock read, no file write, no network, no order. Nothing in src/ changes.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

import pytest

import src.intent_builder as ib
from src.podlib import RISK_PATH, parse_risk_toml
from src.thresholds import PAPER_EQUITY_USD

NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
NOW_S = "2026-09-01T12:00:00Z"
BOOK_TS = "2026-09-01T11:59:55.000Z"
CFG = parse_risk_toml(RISK_PATH.read_text(encoding="utf-8"))
YES, NO = "t-yes", "t-no"


def signal(**kw) -> ib.ProbabilitySignal:
    base = dict(signal_id="sig-1", source="news", market_id="m1", outcome_index=0, p=0.47, signal_ts=NOW_S,
                note="gap test forecast")
    base.update(kw)
    return ib.ProbabilitySignal(**base)


def market(**kw) -> ib.MarketContext:
    base = dict(market_id="m1", condition_id="0xm1", token_ids=(YES, NO), outcomes=("Yes", "No"),
                end_date="2026-10-15T00:00:00Z", liquidity_usd=52000.0, fee_rate=0.0, accepting_orders=True,
                closed=False)
    base.update(kw)
    base.setdefault("tags", ("test-market",))  # issue #131: a tagged market (missing tags veto)
    return ib.MarketContext(**base)


def book(token: str, ask: float, *, asks=None, size: float = 400.0, bid: float | None = None) -> ib.BookSnapshot:
    bid = round(ask - 0.02, 6) if bid is None else bid
    return ib.BookSnapshot(market_id="m1", token_id=token,
                           asks=tuple(asks) if asks is not None else ((ask, size),), bids=((bid, size),),
                           best_bid=bid, best_ask=ask, spread=round(ask - bid, 6), book_ts=BOOK_TS,
                           tick_size=0.01, min_order_size=5.0)


def books(*, yes_asks=None, no_asks=None, yes_ask: float = 0.41, no_ask: float = 0.61
          ) -> dict[str, ib.BookSnapshot]:
    return {YES: book(YES, yes_ask, asks=yes_asks), NO: book(NO, no_ask, asks=no_asks)}


def build(sig=None, mkt=None, bks=None, cfg=None) -> ib.IntentResult:
    return ib.build_order_intent(sig if sig is not None else signal(), mkt or market(),
                                 books() if bks is None else bks, CFG if cfg is None else cfg,
                                 now=NOW, strategy="news-forecaster")


# ---------------------------------------------------------------- invalid_outcome_index

@pytest.mark.parametrize("index", [2, 3, -1, True, False, None, 1.0, "1"])
def test_an_outcome_index_outside_0_1_is_rejected_and_never_read_as_outcome_zero(index):
    result = build(sig=signal(outcome_index=index))
    assert result.intent is None
    assert result.first_reason == "invalid_outcome_index" and "invalid_outcome_index" in result.reasons
    # no side got a probability, so neither side produced an edge from the unusable index
    assert result.detail["edge_yes"] == -1.0 and result.detail["edge_no"] == -1.0


@pytest.mark.parametrize("index", [0, 1])
def test_the_two_real_outcome_indices_still_build(index):
    result = build(sig=signal(outcome_index=index))
    assert result.reasons == () and result.intent is not None


@pytest.mark.parametrize("index", [2, -1, True, None])
def test_a_blend_member_with_an_outcome_index_outside_0_1_is_rejected(index):
    blended, reasons = ib.combine_probability([signal(signal_id="a"),
                                               signal(signal_id="b", source="sports_devig",
                                                      outcome_index=index, p=0.5)])
    assert blended is None and reasons == ["invalid_outcome_index"]


# ---------------------------------------------------------------- invalid_book_levels

@pytest.mark.parametrize("ladder", [
    ((0.50, 400.0), (0.40, 400.0)),  # not ascending
    ((1.0, 400.0),),                 # a level at 1 cannot be bought
    ((0.50, 0.0),),                  # no size on the only level
])
def test_a_usable_book_with_a_bad_ladder_is_invalid_book_levels_not_a_thin_book(ladder):
    ask = ladder[0][0]
    result = build(bks=books(yes_asks=ladder, no_asks=ladder, yes_ask=ask, no_ask=ask))
    assert result.intent is None
    assert "invalid_book_levels" in result.reasons
    assert "insufficient_depth" not in result.reasons  # a bad ladder is a bad book, not a shallow one


@pytest.mark.parametrize("level", [
    (1.0, 400.0), (0.0, 400.0), (-0.1, 400.0), (0.5, 0.0), (0.5, -1.0),
    (math.nan, 400.0), (math.inf, 400.0), ("0.41", 400.0), (0.41, math.nan), (0.41, True),
])
def test_a_level_outside_the_valid_shape_makes_the_walk_invalid(level):
    walk = ib.walk_asks((level,), 10.0)
    assert walk.valid is False and walk.complete is False
    assert walk.shares == 0.0 and walk.filled_usd == 0.0 and math.isnan(walk.avg_price)


@pytest.mark.parametrize("missing", ["best_ask", "best_ask_size"])
def test_a_book_row_with_an_incomplete_top_of_book_cannot_be_walked(missing):
    rows = {token: {"market_id": "m1", "token_id": token, "best_ask": 0.41, "best_ask_size": 400.0,
                    "best_bid": 0.39, "best_bid_size": 900.0, "spread": 0.02, "book_ts": BOOK_TS,
                    "tick_size": 0.01, "min_order_size": 5.0} for token in (YES, NO)}
    for row in rows.values():
        row[missing] = None
    snapshots = {token: ib.book_snapshot_from_row(row) for token, row in rows.items()}
    assert all(snapshot.asks == () for snapshot in snapshots.values())
    result = build(bks=snapshots)
    assert result.intent is None and "invalid_book_levels" in result.reasons


# ---------------------------------------------------------------- walk budget

@pytest.mark.parametrize("budget", [0, 0.0, -1.0, math.nan, math.inf, -math.inf, True, "10", None])
def test_a_non_positive_or_non_finite_walk_budget_cannot_fill(budget):
    walk = ib.walk_asks(((0.41, 400.0),), budget)
    assert walk.valid is False and walk.complete is False
    assert walk.shares == 0.0 and walk.filled_usd == 0.0 and math.isnan(walk.avg_price)


def test_a_valid_budget_still_walks():
    walk = ib.walk_asks(((0.41, 400.0),), 10.0)
    assert walk.valid and walk.complete and walk.shares == pytest.approx(10 / 0.41)


# ---------------------------------------------------------------- zero_notional

def test_a_zero_clip_reports_zero_notional_and_never_sizes():
    result = build(cfg=dict(CFG, max_order_pct=0))
    assert result.intent is None and "zero_notional" in result.reasons
    assert result.reasons[0].startswith("phase0_pin:")  # a pinned config can never reach this branch
    assert "shares" not in result.detail and "size_usd" not in result.detail
    assert "zero_notional" not in build().reasons  # the pinned 1% clip is unaffected


# ---------------------------------------------------------------- shadow Kelly

def test_kelly_non_positive_is_reported_and_the_shadow_size_stays_zero():
    # A positive edge with an unusable (nan) all-in cost: Kelly cannot be computed, so it must not size.
    edge = ib.EdgeBreakdown(p_side=0.9, exec_price=0.5, slippage=0.005, fee_per_share=0.02, edge=0.3,
                            effective_price=math.nan)
    size, reasons = ib.kelly_size(edge, kelly_fraction=0.15, max_order_pct=0.01, equity_usd=PAPER_EQUITY_USD,
                                  worst_price=0.5, tick_size=0.01, min_order_size=5.0)
    assert reasons == ["kelly_non_positive"]
    assert size is not None and math.isnan(size.kelly_raw) and size.kelly_frac == 0.0
    assert size.kelly_size_pct == 0.0 and size.kelly_size_usd == 0.0
    assert size.kelly_size_pct <= size.size_pct
    assert size.size_usd == pytest.approx(PAPER_EQUITY_USD * 0.01)  # the 1% clip, never Kelly


# ---------------------------------------------------------------- probability interval boundaries

@pytest.mark.parametrize(("p_low", "p_high"), [(0.0, 0.5), (0.5, 1.0), (0.0, 1.0), (-0.1, 0.5), (0.5, 1.5)])
def test_interval_endpoints_at_0_and_1_are_out_of_range(p_low, p_high):
    result = build(sig=signal(p=0.5, p_low=p_low, p_high=p_high))
    assert result.intent is None and "model_prob_out_of_range" in result.reasons


def test_an_interval_inside_0_1_is_not_out_of_range():
    result = build(sig=signal(p=0.5, p_low=0.4, p_high=0.5))
    assert "model_prob_out_of_range" not in result.reasons


# ---------------------------------------------------------------- blend sources

@pytest.mark.parametrize("source", ["", "maker", "blend", "Scanner", "endgame"])
def test_a_blend_member_with_an_unsupported_source_is_rejected(source):
    blended, reasons = ib.combine_probability([signal(signal_id="a"),
                                               signal(signal_id="b", source=source, p=0.5)])
    assert blended is None and "unsupported_signal_source" in reasons
