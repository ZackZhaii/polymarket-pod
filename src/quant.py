"""Offline Brier score for a local fixture. At most one proposal.

The score is mean((p - y)^2). It is not a promise of profit.
This module does not edit config/risk.toml or any size cap.
"""
from __future__ import annotations

from pathlib import Path

from src.podlib import bus, read_jsonl

# Local fixture only. No network.
FIXTURE = (
    {"p": 0.70, "y": 1},
    {"p": 0.20, "y": 0},
    {"p": 0.55, "y": 1},
)


def brier(rows: list[dict] | tuple[dict, ...]) -> float:
    if not rows:
        raise ValueError("empty fixture")
    total = 0.0
    for row in rows:
        probability = row.get("p")
        outcome = row.get("y")
        if isinstance(probability, bool) or not isinstance(probability, (int, float)):
            raise ValueError("bad probability")
        if not 0.0 <= float(probability) <= 1.0:
            raise ValueError("bad probability")
        if isinstance(outcome, bool) or outcome not in (0, 1):
            raise ValueError("bad outcome")
        total += (float(probability) - float(outcome)) ** 2
    return total / len(rows)


def propose(
    rows: list[dict] | tuple[dict, ...] | None = None,
    *,
    bus_path: Path | None = None,
) -> dict:
    """Score the fixture and emit one proposal. A second call on the same bus is refused."""
    sample = FIXTURE if rows is None else rows
    score = brier(sample)
    if bus_path is not None and bus_path.exists():
        for message in read_jsonl(bus_path):
            if message.get("from") == "QuantResearcher" and message.get("type") == "proposal":
                return {"accepted": False, "reason": "already_proposed", "brier": score}
    text = (
        f"Offline Brier is {score:.6f} on a local fixture of {len(sample)} rows. "
        "This does not change config/risk.toml or any size cap."
    )
    message = bus(
        "QuantResearcher",
        "Operator",
        "proposal",
        "",
        {
            "brier": score,
            "n": len(sample),
            "text": text,
            "effect": "proposal_only",
            "changes_risk_toml": False,
            "changes_size_caps": False,
            "exchange_action": "none",
        },
        path=bus_path,
    )
    return {
        "accepted": True,
        "reason": "written",
        "brier": score,
        "bus_type": message["type"],
    }
