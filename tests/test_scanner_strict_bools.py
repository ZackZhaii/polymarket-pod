"""Task CR (#232): the scanner reads Gamma's ``acceptingOrders`` and ``closed`` only as real JSON booleans.

Before: ``bool(market.get("acceptingOrders"))`` turned the string "false" into True (fail open), and
``rejection_reasons`` only rejected an explicit False, so a missing flag passed too. ``closed`` used the same
``bool(...)`` and passed 0, "" and a missing key as "open". Now any non-bool (string, number, list, None,
missing) is unknown (None in the row) and rejects the market: ``not_accepting_orders`` / ``closed``. Only
tightening; the reason names, order and exit codes are unchanged. Offline fixtures only.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

import src.scanner as scanner

TS = "2026-10-08T08:00:00Z"
NOW = datetime(2026, 10, 8, 8, 0, 0, tzinfo=timezone.utc)
LIMITS = {"min_liquidity_usd": 10000.0, "max_spread": 0.04, "min_price": 0.03, "max_price": 0.97}
MISSING = object()
NON_BOOLS = ["false", "False", "true", "0", "1", "", 0, 1, 0.0, None, [], {}, MISSING]
NON_BOOL_IDS = ["str_false", "str_False", "str_true", "str_0", "str_1", "empty", "int_0", "int_1",
                "float_0", "none", "list", "dict", "missing"]


def _market(**extra: object) -> dict:
    row = {
        "id": "m0", "slug": "m0", "question": "Market 0",
        "liquidityNum": 20000, "volumeNum": 50000, "volume24hr": 1000,
        "bestBid": 0.49, "bestAsk": 0.51, "spread": 0.02, "lastTradePrice": 0.5,
        "outcomes": '["Yes", "No"]', "outcomePrices": '["0.5", "0.5"]',
        "endDate": "2026-12-31T00:00:00Z", "closed": False, "acceptingOrders": True,
        "version": "v1", "clobTokenIds": '["1", "2"]',
    }
    for key, value in extra.items():
        if value is MISSING:
            row.pop(key, None)
        else:
            row[key] = value
    return row


def _reasons(market: dict) -> list[str]:
    return scanner.rejection_reasons(scanner.normalize(market, TS), LIMITS, NOW)


def test_clean_market_still_passes():
    row = scanner.normalize(_market(), TS)
    assert row["accepting_orders"] is True and row["closed"] is False
    assert _reasons(_market()) == []


def test_real_false_accepting_orders_still_rejects():
    assert _reasons(_market(acceptingOrders=False)) == ["not_accepting_orders"]


def test_real_true_closed_still_rejects():
    assert _reasons(_market(closed=True)) == ["closed"]


@pytest.mark.parametrize("value", NON_BOOLS, ids=NON_BOOL_IDS)
def test_non_bool_accepting_orders_is_unknown_and_rejects(value):
    row = scanner.normalize(_market(acceptingOrders=value), TS)
    assert row["accepting_orders"] is None
    assert _reasons(_market(acceptingOrders=value)) == ["not_accepting_orders"]


def test_string_false_accepting_orders_no_longer_reads_as_true():
    # The #215 audit case (scanner.py:408): bool("false") is True.
    row = scanner.normalize(_market(acceptingOrders="false"), TS)
    assert row["accepting_orders"] is not True
    assert "not_accepting_orders" in _reasons(_market(acceptingOrders="false"))


@pytest.mark.parametrize("value", NON_BOOLS, ids=NON_BOOL_IDS)
def test_non_bool_closed_is_unknown_and_rejects(value):
    row = scanner.normalize(_market(closed=value), TS)
    assert row["closed"] is None
    assert _reasons(_market(closed=value)) == ["closed"]


def test_filter_markets_drops_non_bool_flags():
    markets = [
        _market(),
        dict(_market(acceptingOrders="false"), id="m1", slug="m1", question="Market 1"),
        dict(_market(closed=0), id="m2", slug="m2", question="Market 2"),
        dict(_market(acceptingOrders=MISSING), id="m3", slug="m3", question="Market 3"),
    ]
    markets[3].pop("acceptingOrders", None)
    out = scanner.filter_markets(markets, LIMITS, TS, now=NOW)
    assert [row["id"] for row in out["rows"]] == ["m0"]
    assert out["scanned"] == 4 and out["passed"] == 1
    assert out["any_reason"] == {"not_accepting_orders": 2, "closed": 1}


def test_strict_bool_helper():
    assert scanner.strict_bool(True) is True
    assert scanner.strict_bool(False) is False
    for value in NON_BOOLS:
        assert scanner.strict_bool(None if value is MISSING else value) is None
