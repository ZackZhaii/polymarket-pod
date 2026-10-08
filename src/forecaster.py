"""Forecaster stub. Probability only. Never emits an order or a fill."""
from __future__ import annotations

from pathlib import Path

from src.podlib import bus


def emit_probability(
    *,
    market: str,
    model_prob: float,
    note: str = "",
    bus_path: Path | None = None,
) -> dict:
    if not 0.0 < model_prob < 1.0:
        raise ValueError("model_prob out of range")
    return bus(
        "Forecaster",
        "RiskGate",
        "signal",
        market,
        {
            "model_prob": model_prob,
            "order": False,
            "note": note or "Probability only. Not an order.",
        },
        path=bus_path,
    )
