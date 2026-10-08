"""Maker stub. Records a paper quote signal. Does not post or cancel on an exchange."""
from __future__ import annotations

import math
from pathlib import Path

from src.podlib import PAPER_EQUITY_USD, QUOTES_PATH, append_jsonl, bus, utc_now


def quote(
    *,
    market: str,
    token_id: str,
    side: str,
    price: float,
    size: float,
    bus_path: Path | None = None,
    quotes_path: Path | None = None,
) -> dict:
    if side not in {"buy", "sell"}:
        raise ValueError("side must be one paper leg")
    if not isinstance(price, (int, float)) or isinstance(price, bool) or not math.isfinite(price):
        raise ValueError("price must be a finite number")
    if not isinstance(size, (int, float)) or isinstance(size, bool) or not math.isfinite(size):
        raise ValueError("size must be a finite number")
    if not 0.0 < price < 1.0:
        raise ValueError("price out of range")
    if size <= 0:
        raise ValueError("size must be positive")
    notional = price * size
    if notional > PAPER_EQUITY_USD * 0.01 + 1e-9:
        raise ValueError("quote exceeds 1% clip")
    row = {
        "ts": utc_now(),
        "market": market,
        "token_id": token_id,
        "side": side,
        "price": price,
        "size": size,
        "notional_usd": round(notional, 8),
        "status": "open",
        "venue": "paper",
        "exchange_action": "none",
    }
    append_jsonl(quotes_path or QUOTES_PATH, row)
    message = bus(
        "Maker",
        "RiskGate",
        "signal",
        market,
        row,
        path=bus_path,
    )
    return message
