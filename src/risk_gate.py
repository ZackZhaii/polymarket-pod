"""RiskGate for Phase 0 paper intents.

Vetoes before any simulated fill. A missing, unparseable, future or stale
book timestamp vetoes too (src/book_staleness.py, 120 s code default).
max_open_positions and max_daily_loss apply to every intent, hypothetical ones
included; a missing or invalid cap or state value vetoes. Open positions are legs
(market + token), so each filled leg of an arb pair takes its own slot.
Wash / self-trade: the strategy-name keyword ban stays, and src/wash_detect.py adds
structured checks on top (opposite resting quote or recent opposite fill on the same leg,
own counterparty). Either firing vetoes; missing or invalid history vetoes too.
Does not place orders, sign messages, read secrets, or edit config/risk.toml.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from src.book_staleness import MAX_BOOK_AGE_S, book_age_problem
from src.podlib import PAPER_EQUITY_USD, pin_problems
from src.wash_detect import wash_problems

BANNED_STRATEGY_TOKENS = ("wash", "self-trade", "self_trade", "spoof", "layering")
EDGE_FORMULA_BUY = "model_prob - (market_price + assumed_slippage) - fee_rate * market_price"
EDGE_FORMULA_SELL = "(market_price - assumed_slippage) - model_prob - fee_rate * market_price"


@dataclass
class Intent:
    strategy: str
    market: str
    side: str
    model_prob: float
    market_price: float
    assumed_slippage: float
    fee_rate: float
    liquidity_usd: float | None = None
    spread: float | None = None
    token_id: str = ""
    hypothetical: bool = False
    note: str = ""
    # UTC time of the order book snapshot behind market_price/spread. Missing means veto.
    book_ts: str | float | None = None
    # Who is on the other side of the fill (counterparty or maker), when known. Paper fills
    # against the public book have none (None). Our own account/role/strategy means veto.
    counterparty: str | None = None


@dataclass
class Decision:
    allow: bool
    reasons: list[str]
    edge: float
    notional_usd: float
    shares: float
    fee_usd: float
    effective_price: float
    assumed_slippage: float
    fee_rate: float
    formula: str


def _finite_number(value: object) -> float | None:
    """Accept only a finite int or float. Bools, strings, nan, and inf are rejected."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return number


def gate_thresholds(cfg: dict) -> dict[str, float]:
    """Return min_liquidity_usd and max_spread, or raise RuntimeError naming the key.

    Fails closed like the scanner thresholds: a missing key, a bool, a string,
    None, inf or nan raises. There is no default. evaluate turns the error into
    the invalid_gate_threshold veto, so a bad config can never allow.
    """
    checked: dict[str, float] = {}
    for key in ("min_liquidity_usd", "max_spread"):
        if key not in cfg:
            raise RuntimeError(f"risk.toml missing gate key: {key}")
        number = _finite_number(cfg[key])
        if number is None:
            raise RuntimeError(f"risk.toml gate key must be a finite number: {key}")
        checked[key] = number
    if checked["min_liquidity_usd"] < 0:
        raise RuntimeError("risk.toml gate key out of range: min_liquidity_usd must be >= 0")
    if checked["max_spread"] <= 0:
        raise RuntimeError("risk.toml gate key out of range: max_spread must be > 0")
    return checked


def _cap_number(cfg: dict, key: str) -> float:
    if key not in cfg:
        raise RuntimeError(f"risk.toml missing cap key: {key}")
    number = _finite_number(cfg[key])
    if number is None:
        raise RuntimeError(f"risk.toml cap key must be a finite number: {key}")
    return number


def risk_caps(cfg: dict) -> dict[str, float]:
    """Return max_open_positions and max_daily_loss_pct, or raise RuntimeError naming the key.

    Same fail-closed rule as gate_thresholds: missing, bool, string, None, inf or nan
    raises, and there is no default. max_open_positions must be a whole number >= 0;
    max_daily_loss_pct must be > 0. evaluate turns the error into invalid_risk_cap.
    """
    positions = _cap_number(cfg, "max_open_positions")
    if positions < 0 or not positions.is_integer():
        raise RuntimeError("risk.toml cap key out of range: max_open_positions must be a whole number >= 0")
    loss_pct = _cap_number(cfg, "max_daily_loss_pct")
    if loss_pct <= 0:
        raise RuntimeError("risk.toml cap key out of range: max_daily_loss_pct must be > 0")
    return {"max_open_positions": int(positions), "max_daily_loss_pct": loss_pct}


NUMERIC_INTENT_FIELDS = ("model_prob", "market_price", "assumed_slippage", "fee_rate")


def _number_problem(field: str, value: object) -> str | None:
    """Task U: NaN / +inf / -inf -> non_finite_<field>; a non-number -> invalid_<field>."""
    if _finite_number(value) is not None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"non_finite_{field}"
    return f"invalid_{field}"


def _as_float(value: object) -> float:
    """A number for the unchanged range checks below; a non-number becomes NaN (it never passes)."""
    return float(value) if isinstance(value, (int, float)) else math.nan


def _risk_state(state: dict) -> tuple[int, float] | None:
    """Return (open_positions, daily_loss_usd), or None when either is missing or invalid."""
    positions = _finite_number(state.get("open_positions"))
    loss = _finite_number(state.get("daily_loss_usd"))
    if positions is None or positions < 0 or not positions.is_integer():
        return None
    if loss is None or loss < 0:
        return None
    return int(positions), loss


def evaluate(
    intent: Intent,
    cfg: dict,
    state: dict,
    *,
    now: object = None,
    max_book_age_s: object = MAX_BOOK_AGE_S,
) -> Decision:
    """Return allow only when every Phase 0 check passes.

    The book must be fresh: book_ts present, parseable, not in the future and at most
    max_book_age_s old at ``now`` (default: the clock). Otherwise it vetoes; it never
    raises for this. max_book_age_s is a code default, not a risk.toml key.

    max_open_positions and max_daily_loss are checked for every intent; hypothetical=True
    no longer skips them. A missing or invalid cap (invalid_risk_cap) or state value
    (invalid_risk_state) vetoes instead of reading 0.

    The position-cap exemption needs state["leg_already_open"] is True: this exact
    token is already held. Another open leg of the same market (the other side of
    an arb pair) does not exempt it; that is a new directional position (Task P).

    Wash / self-trade (Task S): besides the keyword ban, state must carry the paper
    history ``recent_fills`` and ``own_quotes`` (paper_ledger.load_state does). An
    uncancelled own quote on the opposite side of the same leg (cross_side_same_leg), an
    own counterparty (self_trade_structure) or an opposite fill on the same leg within
    WASH_WINDOW_S (wash_same_market) vetoes. Missing history is wash_input_missing,
    malformed history wash_input_invalid; both veto.

    Size is the 1% clip. kelly_fraction is not applied and cannot increase size.
    A single-leg fill is directional exposure, not risk-free arbitrage.
    """
    reasons: list[str] = []
    drifted = pin_problems(cfg)
    if drifted:
        reasons.append("phase0_pin:" + ";".join(drifted))

    strategy = intent.strategy.lower()
    if any(token in strategy for token in BANNED_STRATEGY_TOKENS):
        reasons.append("banned_behavior")
    # Structured check on top of the keyword ban (Task S). A clean name does not exempt.
    reasons.extend(
        wash_problems(
            market=intent.market,
            token_id=intent.token_id,
            side=intent.side,
            strategy=intent.strategy,
            counterparty=intent.counterparty,
            state=state,
            now=now,
        )
    )
    if intent.hypothetical and intent.strategy != "phase0-gate-test":
        reasons.append("hypothetical_strategy_not_allowed")
    if intent.side not in {"buy", "sell"}:
        reasons.append("side_must_be_single_leg")
    # Task U: every number in the edge math must be finite. NaN compares False with
    # everything, so without this a NaN fee_rate made the edge NaN and skipped
    # non_positive_edge. A non-finite or non-number input vetoes and the edge fails closed.
    number_problems = [
        problem
        for field in NUMERIC_INTENT_FIELDS
        if (problem := _number_problem(field, getattr(intent, field))) is not None
    ]
    reasons.extend(number_problems)
    model_prob = _as_float(intent.model_prob)
    market_price = _as_float(intent.market_price)
    assumed_slippage = _as_float(intent.assumed_slippage)
    fee_rate = _as_float(intent.fee_rate)

    if not 0.0 < model_prob < 1.0:
        reasons.append("model_prob_out_of_range")
    if not 0.0 < market_price < 1.0:
        reasons.append("market_price_out_of_range")
    if assumed_slippage < 0 or fee_rate < 0:
        reasons.append("negative_cost_assumption")

    try:
        thresholds = gate_thresholds(cfg)
    except RuntimeError:
        reasons.append("invalid_gate_threshold")
        thresholds = None

    liquidity = None if intent.liquidity_usd is None else _finite_number(intent.liquidity_usd)
    if intent.liquidity_usd is None:
        reasons.append("missing_liquidity")
    elif liquidity is None:
        reasons.append("invalid_liquidity")
    elif thresholds is not None and liquidity < thresholds["min_liquidity_usd"]:
        reasons.append("liquidity_below_min")

    spread = None if intent.spread is None else _finite_number(intent.spread)
    if intent.spread is None:
        reasons.append("missing_spread")
    elif spread is None:
        reasons.append("invalid_spread")
    elif spread < 0:
        reasons.append("negative_spread")
    elif thresholds is not None and spread > thresholds["max_spread"]:
        reasons.append("spread_above_max")

    book_problem = book_age_problem(intent.book_ts, now, max_book_age_s)
    if book_problem is not None:
        reasons.append(book_problem)

    if intent.side == "buy":
        effective = market_price + assumed_slippage
        formula = EDGE_FORMULA_BUY
    elif intent.side == "sell":
        effective = market_price - assumed_slippage
        formula = EDGE_FORMULA_SELL
    else:
        effective = market_price
        formula = ""

    if not 0.0 < effective < 1.0:
        reasons.append("effective_price_out_of_range")

    fee_per_share = fee_rate * market_price
    if intent.side == "buy":
        edge = model_prob - effective - fee_per_share
    elif intent.side == "sell":
        edge = effective - model_prob - fee_per_share
    else:
        edge = -1.0
    if number_problems or not math.isfinite(edge):
        edge = -1.0  # fail closed: a non-finite input never yields a usable edge
    if edge <= 0:
        reasons.append("non_positive_edge")

    # Hard clip at 1% even if a caller passes a looser in-memory config.
    try:
        max_order_pct = float(cfg.get("max_order_pct", 0.0))
    except (TypeError, ValueError):
        max_order_pct = math.nan
    if not math.isfinite(max_order_pct):
        reasons.append("invalid_max_order_pct")
        max_order_pct = 0.0
    clip = min(max_order_pct, 0.01)
    notional = PAPER_EQUITY_USD * clip
    if notional <= 0:
        reasons.append("zero_notional")

    # Caps apply to hypothetical intents too (backlog item 4); there is no bypass flag.
    try:
        caps = risk_caps(cfg)
    except RuntimeError:
        reasons.append("invalid_risk_cap")
        caps = None
    current = _risk_state(state)
    if current is None:
        reasons.append("invalid_risk_state")
    if caps is not None and current is not None:
        open_positions, daily_loss = current
        already_open = state.get("leg_already_open") is True
        if open_positions >= caps["max_open_positions"] and not already_open:
            reasons.append("max_open_positions")
        if PAPER_EQUITY_USD <= 0 or daily_loss / PAPER_EQUITY_USD >= caps["max_daily_loss_pct"]:
            reasons.append("max_daily_loss")

    fee_usd = notional * fee_rate if notional > 0 and math.isfinite(fee_rate) else 0.0
    shares = (notional / effective) if math.isfinite(effective) and effective > 0 else 0.0
    return Decision(
        allow=not reasons,
        reasons=reasons,
        edge=edge,
        notional_usd=notional,
        shares=shares,
        fee_usd=fee_usd,
        effective_price=effective,
        assumed_slippage=intent.assumed_slippage,
        fee_rate=intent.fee_rate,
        formula=formula,
    )
