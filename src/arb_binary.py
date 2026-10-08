"""ArbBinary stub. Emits a gap signal only. Never emits a fill or an order."""
from __future__ import annotations

from pathlib import Path

from src.gaps import binary_ask_sum
from src.podlib import bus


def emit_gap(
    yes_ask: float,
    no_ask: float,
    market: str,
    *,
    bus_path: Path | None = None,
) -> dict | None:
    signal = binary_ask_sum(yes_ask, no_ask)
    if not signal["is_gap"]:
        return None
    return bus(
        "ArbBinary",
        "RiskGate",
        "signal",
        market,
        {
            "ask_sum": signal["ask_sum"],
            "gap": signal["gap"],
            "locked_profit": False,
            "one_leg_meaning": "directional",
            "note": signal["note"],
        },
        path=bus_path,
    )
