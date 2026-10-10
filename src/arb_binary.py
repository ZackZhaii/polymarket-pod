"""ArbBinary stub. Records a YES+NO gap as a structural_arb_disabled reject (issue #141). Never a fill or an order."""
from __future__ import annotations

from pathlib import Path

from src.gaps import binary_ask_sum
from src.podlib import bus
from src.structural_gate import STRUCTURAL_ARB_DISABLED


def emit_gap(
    yes_ask: float,
    no_ask: float,
    market: str,
    *,
    bus_path: Path | None = None,
    displayed_depth: float | None = None,
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
            "displayed_depth": displayed_depth,
            "depth_is_not_an_order": True,
            "note": signal["note"],
            # Issue #141 (Task BX): a complete set needs every leg filled together; it is rejected, recorded only.
            "gap_signal": "rejected",
            "rejected": True,
            "reject_reason": STRUCTURAL_ARB_DISABLED,
            "exchange_action": "none",
        },
        path=bus_path,
    )
