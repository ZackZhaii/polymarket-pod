"""Ask-sum gap math. Signal only. No orders and no profit claim.

A sum below 1 is a gap signal. It is not locked profit.
One filled leg is directional exposure.
"""
from __future__ import annotations

from decimal import Decimal


def _price(value: float) -> Decimal:
    price = Decimal(str(value))
    if not Decimal("0") < price < Decimal("1"):
        raise ValueError("ask must be inside (0, 1)")
    return price


def ask_sum(asks: list[float]) -> Decimal:
    if len(asks) < 2:
        raise ValueError("need at least two outcome asks")
    total = Decimal("0")
    for ask in asks:
        total += _price(ask)
    return total


def fill_meaning(outcome_count: int, filled_legs: int) -> str:
    """Say what a partial fill is. Never call any fill locked profit."""
    if outcome_count < 2:
        raise ValueError("need at least two outcomes")
    if filled_legs < 0 or filled_legs > outcome_count:
        raise ValueError("filled_legs out of range")
    if filled_legs == 0:
        return "no_fill"
    if filled_legs < outcome_count:
        return "directional"
    return "all_legs_shown_still_not_a_profit_promise"


def gap_signal(asks: list[float], shape: str) -> dict:
    total = ask_sum(asks)
    raw_gap = Decimal("1") - total
    is_gap = raw_gap > 0
    return {
        "shape": shape,
        "outcome_count": len(asks),
        "ask_sum": float(total),
        "gap": float(raw_gap) if is_gap else 0.0,
        "is_gap": is_gap,
        "locked_profit": False,
        "single_leg_is_directional": True,
        "one_leg_meaning": fill_meaning(len(asks), 1),
        "note": (
            "Ask-sum gap is a signal only. One filled leg is directional exposure, "
            "not locked profit."
        ),
    }


def binary_ask_sum(yes_ask: float, no_ask: float) -> dict:
    return gap_signal([yes_ask, no_ask], "yes_no")


def multi_ask_sum(asks: list[float]) -> dict:
    if len(asks) < 3:
        raise ValueError("multi-outcome needs at least three asks")
    return gap_signal(asks, "multi")
