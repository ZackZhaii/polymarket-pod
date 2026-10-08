"""Sentinel stub. Emits halt only and cancels local paper quotes. No CLOB call."""
from __future__ import annotations

import json
from pathlib import Path

from src.podlib import QUOTES_PATH, bus, read_jsonl, utc_now


def halt(
    *,
    reason: str,
    market: str = "",
    bus_path: Path | None = None,
    quotes_path: Path | None = None,
) -> dict:
    cleaned = " ".join(reason.split())
    if not cleaned:
        raise ValueError("halt reason required")
    ts = utc_now()
    cancelled = cancel_open_quotes(quotes_path or QUOTES_PATH, ts, market)
    return bus(
        "Sentinel",
        "Executor",
        "halt",
        market,
        {
            "reason": cleaned,
            "paper_quotes_cancelled": cancelled,
            "exchange_action": "none",
        },
        path=bus_path,
    )


def cancel_open_quotes(path: Path, ts: str, market: str = "") -> int:
    rows = read_jsonl(path)
    cancelled = 0
    for row in rows:
        if row.get("status") != "open":
            continue
        if market and str(row.get("market") or "") != market:
            continue
        row["status"] = "cancelled"
        row["cancelled_ts"] = ts
        row["cancelled_by"] = "Sentinel"
        cancelled += 1
    if cancelled:
        path.parent.mkdir(parents=True, exist_ok=True)
        body = "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows)
        path.write_text(body, encoding="utf-8")
    return cancelled
