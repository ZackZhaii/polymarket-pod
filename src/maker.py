"""Maker stub. Records a paper quote signal. Does not post or cancel on an exchange.

Task AL (Sentinel design gap 1): no quote while a halt covers the market (market or global
scope, a bad halt state file or logs/HALT). The halt is checked again after the append; if one
landed in between, the Maker cancels its own quote atomically and refuses, so a Sentinel cancel
can never be undone by a racing quote.
"""
from __future__ import annotations

import math
from pathlib import Path

from src import halt_store, sentinel
from src.podlib import PAPER_EQUITY_USD, QUOTES_PATH, append_jsonl, bus, utc_now


class HaltActive(RuntimeError):
    """A halt covers this market; no paper quote."""

    def __init__(self, market: str, reasons: list[str]) -> None:
        self.reasons = reasons
        super().__init__(f"halt active for {market!r}: {', '.join(reasons)}")


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
    halted = halt_store.halt_problems(market)
    if halted:
        raise HaltActive(market, halted)
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
    path = quotes_path or QUOTES_PATH
    append_jsonl(path, row)
    halted = halt_store.halt_problems(market)
    if halted:
        # A halt landed between the check and the append: cancel this market's open quotes again.
        try:
            sentinel.cancel_open_quotes(path, utc_now(), market)
        except OSError:
            halt_store.write_fallback(f"{utc_now()} maker could not cancel a racing quote market={market}")
        raise HaltActive(market, halted)
    message = bus(
        "Maker",
        "RiskGate",
        "signal",
        market,
        row,
        path=bus_path,
    )
    return message
