"""Offline paper-session tests. No network, no fills, no risk-file edits.

Since Task AB the session uses scanner.scan, which needs at least 5 scanned markets, so the
fixture adds four markets the scanner rejects; book age is judged at an injected clock.
"""
from __future__ import annotations

import inspect
import json

import src.paper_session as paper_session
from src.podlib import RISK_PATH, read_jsonl


RAW = {
    "id": "m1",
    "question": "Paper session fixture",
    "slug": "paper-session-fixture",
    "conditionId": "cond-1",
    "clobTokenIds": json.dumps(["yes-token", "no-token"]),
    "outcomes": json.dumps(["Yes", "No"]),
    "outcomePrices": json.dumps(["0.40", "0.40"]),
    "liquidityNum": 25000,
    "volumeNum": 1000,
    "endDate": "2026-12-01T00:00:00Z",
    "bestBid": 0.39,
    "bestAsk": 0.41,
    "spread": 0.02,
    "feesEnabled": False,
    "closed": False,
    "version": "v1",
}

# Rejected by the current scanner path (closed, wide spread, missing version, near expiry):
# they only make up the 5-market floor and must never be persisted or booked.
REJECTED = [
    dict(RAW, id="r-closed", slug="r-closed", closed=True),
    dict(RAW, id="r-wide", slug="r-wide", spread=0.30, bestBid=0.20, bestAsk=0.50),
    {key: value for key, value in dict(RAW, id="r-nover", slug="r-nover").items() if key != "version"},
    dict(RAW, id="r-soon", slug="r-soon", endDate="2026-10-08T12:00:00Z"),
]
CLOCK = "2026-10-08T06:00:00.500Z"


def _book(url: str) -> dict:
    assert url.startswith("https://clob.polymarket.com/book?")
    banned = ("/order", "api-key", "secret", "withdraw")
    assert not any(token in url.lower() for token in banned)
    return {
        "bids": [{"price": "0.30", "size": "10"}],
        "asks": [{"price": "0.40", "size": "12"}],
        "tick_size": "0.01",
        "min_order_size": "5",
        "market": "cond-1",
        # The exchange's own /book time (Unix ms) = the session's now; age is read from this (Task T).
        "timestamp": "1791439200000",
    }


def test_session_records_signals_without_fills(tmp_path):
    before = RISK_PATH.read_bytes()
    markets = tmp_path / "markets.jsonl"
    books = tmp_path / "books.jsonl"
    bus_path = tmp_path / "bus.jsonl"
    sessions = tmp_path / "sessions.jsonl"
    result = paper_session.run(
        raw_markets=[RAW, *REJECTED],
        book_getter=_book,
        markets_path=markets,
        books_path=books,
        bus_path=bus_path,
        sessions_path=sessions,
        now="2026-10-08T06:00:00Z",
        clock=lambda: CLOCK,
    )
    assert result["ok"] is True
    assert result["reason"] == "signals_only"
    assert result["markets"] == 1
    assert result["books"] == 2
    assert result["gaps"] == 1
    assert result["fills"] == 0
    kinds = [row["type"] for row in read_jsonl(bus_path)]
    assert "fill" not in kinds
    assert "signal" in kinds
    session = read_jsonl(sessions)[0]
    assert session["fills"] == 0
    assert session["orders"] == "none"
    assert session["keys"] == "none"
    assert session["live_enabled"] is False
    assert RISK_PATH.read_bytes() == before
    source = inspect.getsource(paper_session)
    assert "load_dotenv" not in source
    assert "os.environ" not in source
    assert "POLY_PRIVATE_KEY" not in source
    assert "polymarket-client" not in source
    assert "withdraw" not in source


def test_pin_drift_does_not_read_the_book(tmp_path):
    before = RISK_PATH.read_bytes()
    cfg = paper_session.load_risk()
    cfg["live_enabled"] = True
    called = {"n": 0}

    def getter(url: str) -> dict:
        called["n"] += 1
        raise AssertionError(url)

    result = paper_session.run(
        raw_markets=[RAW, *REJECTED],
        book_getter=getter,
        markets_path=tmp_path / "markets.jsonl",
        books_path=tmp_path / "books.jsonl",
        bus_path=tmp_path / "bus.jsonl",
        sessions_path=tmp_path / "sessions.jsonl",
        cfg=cfg,
    )
    assert result["ok"] is False
    assert result["reason"] == "phase0_pin"
    assert result["fills"] == 0
    assert called["n"] == 0
    assert not (tmp_path / "sessions.jsonl").exists()
    assert RISK_PATH.read_bytes() == before
