"""Task O integration: the scanner drops markets that expire within MIN_HOURS_TO_EXPIRY.

Mocked Gamma rows and pages only. No network, no orders, no risk-file edits.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import src.scanner as scanner
from src.podlib import RISK_PATH, read_jsonl

TS = "2026-10-08T08:00:00Z"
NOW = datetime(2026, 10, 8, 8, 0, 0, tzinfo=timezone.utc)
LIMITS = {"min_liquidity_usd": 10000.0, "max_spread": 0.04, "min_price": 0.03, "max_price": 0.97}


def _iso(delta: timedelta) -> str:
    return (NOW + delta).strftime("%Y-%m-%dT%H:%M:%SZ")


def _market(market_id: str, end_date: object, volume: float = 1.0) -> dict:
    return {
        "id": market_id,
        "slug": market_id,
        "question": f"Synthetic market {market_id}",
        "liquidityNum": 20000,
        "volume24hr": volume,
        "bestBid": 0.49,
        "bestAsk": 0.51,
        "spread": 0.02,
        "endDate": end_date,
        "closed": False,
        "acceptingOrders": True,
        "version": "v1",
    }


MIXED = [
    _market("far", _iso(timedelta(days=60)), volume=9),
    _market("above", _iso(timedelta(hours=25)), volume=8),
    _market("boundary", _iso(timedelta(hours=24)), volume=7),
    _market("below", _iso(timedelta(hours=23, minutes=59)), volume=6),
    _market("hour", _iso(timedelta(hours=1)), volume=5),
    _market("past", _iso(timedelta(hours=-1)), volume=4),
    _market("naive", "2026-12-31T00:00:00", volume=3),
    _market("date-only", "2026-12-31", volume=2),
    _market("numeric", 1798675200, volume=1),
    _market("garbage", "soon", volume=1),
    _market("missing", None, volume=1),
]


def test_filter_drops_near_expiry_and_bad_end_dates():
    result = scanner.filter_markets(MIXED, LIMITS, TS, now=NOW)
    assert [row["id"] for row in result["rows"]] == ["far", "above", "boundary"]
    assert result["scanned"] == len(MIXED)
    assert result["first_reason"] == {
        "near_expiry": 2,
        "expired": 1,
        "invalid_end_date": 4,
        "missing_end_date": 1,
    }
    assert sum(result["first_reason"].values()) == result["scanned"] - result["passed"]
    assert all(row["days_to_expiry"] >= 1.0 for row in result["rows"])
    assert result["min_hours_to_expiry"] == scanner.MIN_HOURS_TO_EXPIRY == 24.0


def test_now_defaults_to_the_scan_timestamp():
    result = scanner.filter_markets(MIXED, LIMITS, TS)
    assert [row["id"] for row in result["rows"]] == ["far", "above", "boundary"]


def test_threshold_is_injectable_but_still_fails_closed():
    loose = scanner.filter_markets(MIXED, LIMITS, TS, now=NOW, min_hours_to_expiry=0)
    assert [row["id"] for row in loose["rows"]] == ["far", "above", "boundary", "below", "hour"]
    broken = scanner.filter_markets(MIXED, LIMITS, TS, now=NOW, min_hours_to_expiry=float("nan"))
    assert broken["passed"] == 0
    assert broken["first_reason"] == {"invalid_min_hours_to_expiry": len(MIXED)}


def test_scan_passes_now_and_threshold_through():
    result = scanner.scan(raw_markets=MIXED, limits=LIMITS, ts=TS, now=NOW + timedelta(hours=2))
    # Two hours later, "above" (25 h) has 23 h left and drops; "boundary" drops too.
    assert [row["id"] for row in result["rows"]] == ["far"]
    tight = scanner.scan(raw_markets=MIXED, limits=LIMITS, ts=TS, min_hours_to_expiry=24 * 30)
    assert [row["id"] for row in tight["rows"]] == ["far"]


def _pager(rows: list[dict]):
    def getter(url: str) -> dict:
        assert url.startswith("https://gamma-api.polymarket.com/markets/keyset?")
        return {"markets": rows, "next_cursor": ""}

    return getter


def test_main_writes_only_markets_with_enough_time_left(tmp_path: Path, capsys):
    before = RISK_PATH.read_bytes()
    markets = tmp_path / "markets.jsonl"
    bus_path = tmp_path / "bus.jsonl"
    code = scanner.main(getter=_pager(MIXED), markets_path=markets, bus_path=bus_path, ts=TS)
    assert code == scanner.EXIT_OK
    assert [row["id"] for row in read_jsonl(markets)] == ["far", "above", "boundary"]
    out = capsys.readouterr().out
    assert "min_hours_to_expiry=24" in out
    assert "near_expiry:2" in out
    assert RISK_PATH.read_bytes() == before


def test_main_exits_2_when_everything_is_near_expiry(tmp_path: Path):
    rows = [_market(f"soon-{n}", _iso(timedelta(hours=n + 1))) for n in range(6)]
    markets = tmp_path / "markets.jsonl"
    bus_path = tmp_path / "bus.jsonl"
    code = scanner.main(getter=_pager(rows), markets_path=markets, bus_path=bus_path, ts=TS)
    assert code == scanner.EXIT_NONE_PASSED
    assert not markets.exists() and not bus_path.exists()
