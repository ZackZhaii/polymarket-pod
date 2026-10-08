"""Task AE (backlog #19): characterisation of today's fees, before they are unified.

RiskGate charges fees in two places: the edge stress (``fee_rate * market_price`` per share,
subtracted from the edge) and the booked fee (``Decision.fee_usd = notional_usd * fee_rate``),
which paper_ledger writes as ``round(fee_usd, 8)`` and sums into daily_loss_usd.

RECORDED holds outputs captured from dev before any fee change. The frozen copies
below (old_stress_fee, old_ledger_fee, old_edge) are checked against RECORDED and against
evaluate() on a grid of prices (including near 0 and 1), sizes, fee rates, slippages and
sides, valid and invalid. Offline, no credentials.
"""
from __future__ import annotations

import itertools
import math
from datetime import datetime, timezone

import pytest

import src.paper_ledger as paper_ledger
from src.podlib import load_risk, utc_now
from src.risk_gate import Intent, evaluate

NOW = "2026-10-08T08:00:00Z"
NOW_DT = datetime(2026, 10, 8, 8, tzinfo=timezone.utc)
STATE = {"open_positions": 0, "leg_already_open": False, "daily_loss_usd": 0.0,
         "recent_fills": [], "own_quotes": []}
NAN, INF = float("nan"), float("inf")

# (side, model_prob, market_price, assumed_slippage, fee_rate, max_order_pct, edge, fee_usd, allow)
# captured from dev before Task AE (10a3d93; risk_gate and paper_ledger are the same at b83f8e0).
RECORDED = [
    ("buy", 0.43, 0.4, 0.005, 0.02, 0.01, 0.016999999999999967, 0.2, False),
    ("buy", 0.7, 0.4, 0.005, 0.02, 0.01, 0.2869999999999999, 0.2, True),
    ("sell", 0.3, 0.4, 0.005, 0.02, 0.01, 0.08700000000000002, 0.2, True),
    ("sell", 0.1, 0.4, 0.05, 0.1, 0.01, 0.21, 1.0, True),
    ("buy", 0.99, 0.95, 0.005, 0.1, 0.01, -0.05999999999999997, 1.0, False),
    ("buy", 0.05, 0.01, 0.005, 0.02, 0.01, 0.034800000000000005, 0.2, True),
    ("sell", 0.001, 0.01, 0.005, 0.02, 0.01, 0.0038, 0.2, False),
    ("buy", 0.9999, 0.99, 0.005, 0.02, 0.01, -0.014899999999999986, 0.2, False),
    ("sell", 0.9, 0.99, 0.005, 1.0, 0.01, -0.905, 10.0, False),
    ("buy", 0.7, 0.4, 0.005, 0.0, 0.01, 0.29499999999999993, 0.0, True),
    ("buy", 0.7, 0.4, 0.005, 0.02, 0.003, 0.2869999999999999, 0.06, False),
    ("sell", 0.3, 0.4, 0.005, 0.02, 0.0, 0.08700000000000002, 0.0, False),
    ("buy", 0.7, 0.4, 0.005, NAN, 0.01, -1.0, 0.0, False),
    ("sell", 0.3, 0.4, 0.005, INF, 0.01, -1.0, 0.0, False),
    ("buy", 0.7, 0.4, 0.005, -0.02, 0.01, 0.30299999999999994, -0.2, False),
    ("buy", 0.7, NAN, 0.005, 0.02, 0.01, -1.0, 0.2, False),
    ("hold", 0.7, 0.4, 0.005, 0.02, 0.01, -1.0, 0.2, False),
    ("sell", 0.3, 0.4, 0.5, 0.02, 0.01, -0.408, 0.2, False),
    ("buy", 0.7, 0.4, 0.005, "0.02", 0.01, -1.0, 0.0, False),
    ("sell", 0.3, 0.4, 0.0, 0.02, 0.01, 0.09200000000000003, 0.2, True),
]

# The grid: prices near 0 and 1 and out of range, sizes (max_order_pct, clipped at 1%),
# fee rates (zero, tiny, typical, huge, negative, non-finite, non-number), sides.
PRICES = [1e-9, 1e-4, 0.01, 0.03, 0.4, 0.5, 0.97, 0.99, 0.9999, 1 - 1e-9,
          0.0, 1.0, -0.1, 1.5, NAN, INF, -INF, "0.5", None]
FEE_RATES = [0.0, 1e-9, 0.001, 0.02, 0.1, 1.0, -0.02, NAN, INF, -INF, "0.02", None]
SLIPPAGES = [0.0, 0.005, 0.05]
SIDES = ["buy", "sell", "hold"]
MODEL_PROBS = [0.5, 0.995]
SIZES = [0.01, 0.003, 1e-7, 0.0, 0.5, "x"]
GRID = list(itertools.product(SIDES, MODEL_PROBS, PRICES, SLIPPAGES, FEE_RATES, SIZES))


def _num(value: object) -> float:
    """risk_gate._as_float: a non-number is NaN."""
    return float(value) if isinstance(value, (int, float)) else math.nan


def _bad(value: object) -> bool:
    """risk_gate Task U: a bool, non-number or non-finite number is a number problem."""
    return isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value))


# --- frozen copies of the two old fee paths ----------------------------------------------


def old_stress_fee(fee_rate: object, market_price: object) -> float:
    """Old risk_gate edge stress: fee_per_share = fee_rate * market_price."""
    return _num(fee_rate) * _num(market_price)


def old_ledger_fee(notional_usd: float, fee_rate: object) -> float:
    """Old risk_gate Decision.fee_usd (booked by paper_ledger)."""
    rate = _num(fee_rate)
    return notional_usd * rate if notional_usd > 0 and math.isfinite(rate) else 0.0


def old_booked_fee(fee_usd: float) -> float:
    """Old paper_ledger fill fee_usd."""
    return round(fee_usd, 8)


def old_edge(side, model_prob, market_price, assumed_slippage, fee_rate, effective) -> float:
    """Old risk_gate edge, including Task U's fail-closed -1."""
    fee = old_stress_fee(fee_rate, market_price)
    p = _num(model_prob)
    if side == "buy":
        edge = p - effective - fee
    elif side == "sell":
        edge = effective - p - fee
    else:
        edge = -1.0
    if any(_bad(v) for v in (model_prob, market_price, assumed_slippage, fee_rate)) or not math.isfinite(edge):
        edge = -1.0
    return edge


def _evaluate(side, model_prob, market_price, slippage, fee_rate, size, cfg=None):
    cfg = dict(cfg or load_risk())
    cfg["max_order_pct"] = size
    intent = Intent(strategy="task-ae", market="ae", side=side, model_prob=model_prob,
                    market_price=market_price, assumed_slippage=slippage, fee_rate=fee_rate,
                    liquidity_usd=20000.0, spread=0.01, token_id="t", book_ts=NOW)
    return evaluate(intent, cfg, STATE, now=NOW_DT)


def _same(a: float, b: float) -> bool:
    return a == b or (math.isnan(a) and math.isnan(b))


# --- the frozen copies are today's code ------------------------------------------------------


@pytest.mark.parametrize("point", RECORDED, ids=[f"rec{i}" for i in range(len(RECORDED))])
def test_frozen_copies_reproduce_the_recorded_values(point):
    side, p, m, s, f, size, edge, fee_usd, _allow = point
    decision = _evaluate(side, p, m, s, f, size)
    assert _same(old_edge(side, p, m, s, f, decision.effective_price), edge)
    assert old_ledger_fee(decision.notional_usd, f) == fee_usd


@pytest.mark.parametrize("point", RECORDED, ids=[f"rec{i}" for i in range(len(RECORDED))])
def test_recorded_values_are_todays_values(point):
    side, p, m, s, f, size, edge, fee_usd, allow = point
    decision = _evaluate(side, p, m, s, f, size)
    assert (decision.edge, decision.fee_usd, decision.allow) == (edge, fee_usd, allow), decision.reasons


def test_grid_matches_the_frozen_copies():
    cfg = load_risk()
    assert len(GRID) == 24624
    for point in GRID:
        side, p, m, s, f, size = point
        decision = _evaluate(side, p, m, s, f, size, cfg)
        assert _same(decision.edge, old_edge(side, p, m, s, f, decision.effective_price)), point
        assert decision.fee_usd == old_ledger_fee(decision.notional_usd, f), point
        assert math.isfinite(decision.edge) and math.isfinite(decision.fee_usd), point


@pytest.mark.parametrize("point", [r for r in RECORDED if r[-1]], ids=lambda r: f"{r[0]}-{r[2]}-{r[4]}")
def test_ledger_books_todays_fee(tmp_path, monkeypatch, point):
    monkeypatch.setattr(paper_ledger, "PAPER_PATH", tmp_path / "paper.jsonl")
    monkeypatch.setattr(paper_ledger, "QUOTES_PATH", tmp_path / "quotes.jsonl")
    monkeypatch.setattr(paper_ledger, "bus", lambda *args, **kwargs: None)
    side, p, m, s, f, size, _edge, fee_usd, _allow = point
    intent = Intent(strategy="task-ae", market="ae", side=side, model_prob=p, market_price=m,
                    assumed_slippage=s, fee_rate=f, liquidity_usd=20000.0, spread=0.01,
                    token_id="t", book_ts=utc_now())
    result = paper_ledger.commit_if_allowed(intent, load_risk())
    assert result["allow"] is True
    assert result["fill"]["fee_usd"] == old_booked_fee(fee_usd)
    assert paper_ledger.load_state("ae", "t")["daily_loss_usd"] == old_booked_fee(fee_usd)
