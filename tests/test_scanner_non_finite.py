"""Task Y: the scanner rejects any market whose parsed numbers are NaN or +/-inf.

Before: as_float("NaN") is nan, and every `<` / `>` / `<=` with nan is False, so a NaN
spread, liquidity or bid/ask passed the filters. Now each non-finite value the scanner
parses rejects the market with non_finite_<field>, ranked before the ordinary
bid/ask, liquidity, spread and price reasons. Exit codes and output format are unchanged.
Offline fixtures only.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone

import pytest

import src.scanner as scanner
from src.podlib import RISK_PATH, read_jsonl

TS = "2026-10-08T08:00:00Z"
NOW = datetime(2026, 10, 8, 8, 0, 0, tzinfo=timezone.utc)
LIMITS = {"min_liquidity_usd": 10000.0, "max_spread": 0.04, "min_price": 0.03, "max_price": 0.97}
VALUES = ["NaN", "nan", "inf", "-Infinity", float("nan")]
VALUE_IDS = ["NaN", "nan", "inf", "-Infinity", "float_nan"]


def _market(index: int = 0, **extra: object) -> dict:
    row = {
        "id": f"m{index}", "slug": f"m{index}", "question": f"Market {index}",
        "liquidityNum": 20000, "volumeNum": 50000, "volume24hr": 1000 + index,
        "bestBid": 0.49, "bestAsk": 0.51, "spread": 0.02, "lastTradePrice": 0.5,
        "outcomes": '["Yes", "No"]', "outcomePrices": '["0.5", "0.5"]',
        "endDate": "2026-12-31T00:00:00Z", "closed": False, "acceptingOrders": True,
        "version": "v1", "clobTokenIds": '["1", "2"]',
    }
    row.update(extra)
    return row


def _reasons(market: dict) -> list[str]:
    return scanner.rejection_reasons(scanner.normalize(market, TS), LIMITS, NOW)


def test_clean_market_still_passes():
    assert _reasons(_market()) == []


# (Gamma key, extra fields so that key is the value the scanner actually parses, reason)
FIELDS = [
    ("bestBid", {}, "non_finite_best_bid"),
    ("bestAsk", {}, "non_finite_best_ask"),
    ("spread", {}, "non_finite_spread"),
    ("liquidityNum", {}, "non_finite_liquidity_usd"),
    ("liquidity", {"liquidityNum": None}, "non_finite_liquidity_usd"),
    ("volumeNum", {}, "non_finite_volume_usd"),
    ("volume", {"volumeNum": None}, "non_finite_volume_usd"),
    ("volume24hr", {}, "non_finite_volume_24h_usd"),
]


@pytest.mark.parametrize("value", VALUES, ids=VALUE_IDS)
@pytest.mark.parametrize(("key", "extra", "reason"), FIELDS, ids=[f[0] for f in FIELDS])
def test_non_finite_field_rejects_with_its_reason_first(key, extra, reason, value):
    market = _market(**extra, **{key: value})
    reasons = _reasons(market)
    assert reasons, f"{key}={value!r} passed the scanner"
    assert reasons[0] == reason, reasons
    result = scanner.filter_markets([market], LIMITS, TS, now=NOW)
    assert result["passed"] == 0 and result["first_reason"] == {reason: 1}


@pytest.mark.parametrize("value", VALUES, ids=VALUE_IDS)
def test_non_finite_outcome_price_rejects(value):
    prices = json.dumps(["0.5", value]) if isinstance(value, str) else [0.5, value]
    reasons = _reasons(_market(outcomePrices=prices))
    assert reasons[:1] == ["non_finite_outcome_prices"], reasons


@pytest.mark.parametrize("value", VALUES, ids=VALUE_IDS)
def test_non_finite_last_trade_price_used_as_price_rejects(value):
    # No usable bid/ask, so the scanner prices the market from lastTradePrice.
    market = _market(bestBid=None, bestAsk=None, spread=None, lastTradePrice=value)
    reasons = _reasons(market)
    assert "non_finite_price" in reasons, reasons
    assert reasons.index("non_finite_price") < reasons.index("missing_bid_ask")
    assert scanner.yes_price(market)[1] == "last_trade"


@pytest.mark.parametrize("value", VALUES, ids=VALUE_IDS)
def test_non_finite_first_outcome_price_used_as_price_rejects(value):
    prices = json.dumps([value, "0.5"]) if isinstance(value, str) else [value, 0.5]
    market = _market(bestBid=None, bestAsk=None, spread=None, lastTradePrice=None, outcomePrices=prices)
    reasons = _reasons(market)
    assert "non_finite_price" in reasons and "non_finite_outcome_prices" in reasons, reasons


def test_reasons_are_registered_and_ranked_before_ordinary_filters():
    names = [r for r in scanner.REASONS if r.startswith("non_finite_")]
    assert names == list(scanner.NON_FINITE_REASONS)
    first_plain = scanner.REASONS.index("missing_bid_ask")
    assert all(scanner.REASONS.index(name) < first_plain for name in names)
    assert scanner.REASONS.index("not_accepting_orders") < scanner.REASONS.index(names[0])
    # structural reasons keep their place
    assert scanner.REASONS[:11] == (
        "closed", "missing_version", "unsupported_version", "expired", "missing_end_date",
        "invalid_end_date", "near_expiry", "invalid_min_hours_to_expiry", "invalid_now",
        "market_expired_or_resolving",  # issue #141 (Task BX)
        "not_accepting_orders",
    )
    assert scanner.REASONS[-5:] == ("missing_bid_ask", "low_liquidity", "wide_spread", "missing_price", "long_tail_price")


def test_finite_but_bad_values_keep_their_old_reasons():
    assert _reasons(_market(spread=0.2)) == ["wide_spread"]
    assert _reasons(_market(liquidityNum=50)) == ["low_liquidity"]
    assert _reasons(_market(bestAsk=1)) == ["missing_bid_ask"]


# --- main(): exit codes and output format unchanged -----------------------------------------


def _pager(batch):
    return lambda url: {"markets": batch, "next_cursor": ""}


def test_main_skips_non_finite_rows_and_writes_only_finite_ones(tmp_path, capsys):
    before = RISK_PATH.read_bytes()
    raw = [_market(i) for i in range(5)] + [_market(10, spread="NaN"), _market(11, liquidityNum="inf")]
    markets, bus_path = tmp_path / "markets.jsonl", tmp_path / "bus.jsonl"
    code = scanner.main(getter=_pager(raw), markets_path=markets, bus_path=bus_path, ts=TS)
    assert code == scanner.EXIT_OK
    rows = read_jsonl(markets)
    assert sorted(row["id"] for row in rows) == [f"m{i}" for i in range(5)]
    for row in rows:
        for key in ("best_bid", "best_ask", "spread", "liquidity_usd", "volume_usd", "volume_24h_usd", "price"):
            assert row[key] is None or math.isfinite(row[key])
    assert set(rows[0]) == set(scanner.normalize(_market(), TS)) | {"days_to_expiry"}
    out = capsys.readouterr().out
    assert "rejected_first=non_finite_liquidity_usd:1, non_finite_spread:1" in out
    assert out.startswith("scanned=7 passed=5 ")
    assert RISK_PATH.read_bytes() == before


def test_main_all_non_finite_is_none_passed(tmp_path):
    raw = [_market(i, bestBid="NaN") for i in range(6)]
    markets = tmp_path / "markets.jsonl"
    code = scanner.main(getter=_pager(raw), markets_path=markets, bus_path=tmp_path / "bus.jsonl", ts=TS)
    assert code == scanner.EXIT_NONE_PASSED
    assert not markets.exists()
