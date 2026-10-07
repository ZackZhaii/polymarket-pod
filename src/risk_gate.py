"""RiskGate for Phase 0 paper intents.

Vetoes before any simulated fill. Does not place orders, sign messages,
read secrets, or edit config/risk.toml.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.podlib import PAPER_EQUITY_USD, pin_problems

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


def evaluate(intent: Intent, cfg: dict, state: dict) -> Decision:
    """Return allow only when every Phase 0 check passes.

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
    if intent.hypothetical and intent.strategy != "phase0-gate-test":
        reasons.append("hypothetical_strategy_not_allowed")
    if intent.side not in {"buy", "sell"}:
        reasons.append("side_must_be_single_leg")
    if not 0.0 < intent.model_prob < 1.0:
        reasons.append("model_prob_out_of_range")
    if not 0.0 < intent.market_price < 1.0:
        reasons.append("market_price_out_of_range")
    if intent.assumed_slippage < 0 or intent.fee_rate < 0:
        reasons.append("negative_cost_assumption")

    min_liquidity = float(cfg.get("min_liquidity_usd", 0))
    if intent.liquidity_usd is None:
        reasons.append("missing_liquidity")
    elif intent.liquidity_usd < min_liquidity:
        reasons.append("liquidity_below_min")

    max_spread = float(cfg.get("max_spread", 1))
    if intent.spread is None:
        reasons.append("missing_spread")
    elif intent.spread < 0:
        reasons.append("negative_spread")
    elif intent.spread > max_spread:
        reasons.append("spread_above_max")

    if intent.side == "buy":
        effective = intent.market_price + intent.assumed_slippage
        formula = EDGE_FORMULA_BUY
    elif intent.side == "sell":
        effective = intent.market_price - intent.assumed_slippage
        formula = EDGE_FORMULA_SELL
    else:
        effective = intent.market_price
        formula = ""

    if not 0.0 < effective < 1.0:
        reasons.append("effective_price_out_of_range")

    fee_per_share = intent.fee_rate * intent.market_price
    if intent.side == "buy":
        edge = intent.model_prob - effective - fee_per_share
    elif intent.side == "sell":
        edge = effective - intent.model_prob - fee_per_share
    else:
        edge = -1.0
    if edge <= 0:
        reasons.append("non_positive_edge")

    # Hard clip at 1% even if a caller passes a looser in-memory config.
    clip = min(float(cfg.get("max_order_pct", 0.0)), 0.01)
    notional = PAPER_EQUITY_USD * clip
    if notional <= 0:
        reasons.append("zero_notional")

    if not intent.hypothetical:
        open_positions = int(state.get("open_positions", 0))
        already_open = bool(state.get("market_already_open"))
        if open_positions >= int(cfg.get("max_open_positions", 0)) and not already_open:
            reasons.append("max_open_positions")
        daily_loss = float(state.get("daily_loss_usd", 0.0))
        if PAPER_EQUITY_USD > 0 and daily_loss / PAPER_EQUITY_USD >= float(cfg.get("max_daily_loss_pct", 0.0)):
            reasons.append("max_daily_loss")

    fee_usd = notional * intent.fee_rate if notional > 0 else 0.0
    shares = (notional / effective) if effective > 0 else 0.0
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
