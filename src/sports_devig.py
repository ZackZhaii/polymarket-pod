"""SportsDevig. Proportional de-vig of a two-way price. Signal only.

A positive edge is not locked profit. One leg would be directional.
This module never emits a fill or an order and does not use the network.
"""
from __future__ import annotations

from pathlib import Path

from src.podlib import bus

ASSUMED_SLIPPAGE = 0.005


def fair_two_way(odds_a: float, odds_b: float) -> tuple[float, float] | None:
    """Remove the overround in proportion to each side's raw implied probability."""
    if not isinstance(odds_a, (int, float)) or isinstance(odds_a, bool):
        return None
    if not isinstance(odds_b, (int, float)) or isinstance(odds_b, bool):
        return None
    if float(odds_a) <= 1.0 or float(odds_b) <= 1.0:
        return None
    raw_a = 1.0 / float(odds_a)
    raw_b = 1.0 / float(odds_b)
    total = raw_a + raw_b
    if total <= 0.0:
        return None
    return raw_a / total, raw_b / total


def emit(
    odds_a: float,
    odds_b: float,
    market_price: float,
    market: str,
    *,
    slippage: float = ASSUMED_SLIPPAGE,
    bus_path: Path | None = None,
) -> dict | None:
    fair = fair_two_way(odds_a, odds_b)
    if fair is None or not isinstance(market_price, (int, float)) or isinstance(market_price, bool):
        return None
    if not 0.0 < float(market_price) < 1.0 or float(slippage) < 0.0:
        return None
    fair_a = fair[0]
    edge = fair_a - (float(market_price) + float(slippage))
    if edge <= 0.0:
        return None
    return bus(
        "SportsDevig",
        "RiskGate",
        "signal",
        market,
        {
            "fair_prob": round(fair_a, 6),
            "market_price": float(market_price),
            "assumed_slippage": float(slippage),
            "edge": round(edge, 6),
            "method": "proportional",
            "locked_profit": False,
            "one_leg_meaning": "directional",
            "exchange_action": "none",
        },
        path=bus_path,
    )
