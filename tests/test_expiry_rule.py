"""Task O unit tests: pure minimum time-to-expiry rule. Offline, fixed clock."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.expiry_rule import (
    EXPIRED,
    INVALID_END_DATE,
    INVALID_MIN_HOURS,
    INVALID_NOW,
    MIN_HOURS_TO_EXPIRY,
    MISSING_END_DATE,
    NEAR_EXPIRY,
    expiry_problem,
    hours_to_expiry,
    parse_end_date,
)
from src.podlib import RISK_PATH

NOW = datetime(2026, 10, 9, 0, 0, 0, tzinfo=timezone.utc)


def _iso(delta: timedelta) -> str:
    return (NOW + delta).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_default_is_24_hours_and_not_a_risk_toml_key():
    assert MIN_HOURS_TO_EXPIRY == 24.0
    text = RISK_PATH.read_text(encoding="utf-8")
    assert "expiry" not in text and "min_hours" not in text


@pytest.mark.parametrize(
    "delta,expected",
    [
        (timedelta(days=90), None),                       # far future
        (timedelta(hours=24, seconds=1), None),           # just above
        (timedelta(hours=24), None),                      # exact boundary passes
        (timedelta(hours=24, seconds=-1), NEAR_EXPIRY),   # just below
        (timedelta(hours=1), NEAR_EXPIRY),
        (timedelta(seconds=1), NEAR_EXPIRY),
        (timedelta(0), EXPIRED),                          # ends now
        (timedelta(days=-1), EXPIRED),                    # past
    ],
)
def test_threshold_boundaries(delta, expected):
    assert expiry_problem(_iso(delta), NOW) == expected


@pytest.mark.parametrize("value", [None, "", "   "])
def test_missing_end_date_fails_closed(value):
    assert expiry_problem(value, NOW) == MISSING_END_DATE


@pytest.mark.parametrize(
    "value",
    [
        "garbage",
        "tomorrow",
        "2026-13-01T00:00:00Z",       # month 13
        "2026-12-31T25:00:00Z",       # hour 25
        "2026-12-31T00:00:00",        # naive: no zone
        "2026-12-31 00:00:00",        # naive with a space
        "2026-12-31",                 # date only
        "31/12/2026 00:00 UTC",
        "1767225600",                 # numeric text
    ],
)
def test_unparseable_or_naive_end_date_fails_closed(value):
    assert expiry_problem(value, NOW) == INVALID_END_DATE
    assert parse_end_date(value) is None


@pytest.mark.parametrize(
    "value",
    [1767225600, 1767225600.0, float("inf"), float("nan"), True, False,
     ["2026-12-31T00:00:00Z"], {"endDate": "2026-12-31T00:00:00Z"},
     datetime(2026, 12, 31, tzinfo=timezone.utc)],
)
def test_non_string_end_date_fails_closed(value):
    assert expiry_problem(value, NOW) == INVALID_END_DATE


def test_zoned_forms_are_accepted_and_normalised_to_utc():
    # +08:00 at 08:00 next day is exactly 24 h after NOW: the boundary passes.
    assert expiry_problem("2026-10-10T08:00:00+08:00", NOW) is None
    assert expiry_problem("2026-10-10T07:59:59+08:00", NOW) == NEAR_EXPIRY
    assert expiry_problem("2026-12-31T00:00:00.123456Z", NOW) is None
    assert expiry_problem("2026-12-31T00:00:00z", NOW) is None
    assert expiry_problem("  2026-12-31T00:00:00Z  ", NOW) is None
    assert parse_end_date("2026-10-10T08:00:00+08:00") == datetime(2026, 10, 10, tzinfo=timezone.utc)
    assert hours_to_expiry("2026-10-10T00:00:00Z", NOW) == pytest.approx(24.0)


def test_custom_threshold_is_honoured():
    assert expiry_problem(_iso(timedelta(hours=30)), NOW, min_hours=48) == NEAR_EXPIRY
    assert expiry_problem(_iso(timedelta(hours=1)), NOW, min_hours=0) is None
    assert expiry_problem(_iso(timedelta(0)), NOW, min_hours=0) == EXPIRED


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1, True, "24", None])
def test_invalid_threshold_fails_closed(bad):
    assert expiry_problem(_iso(timedelta(days=90)), NOW, min_hours=bad) == INVALID_MIN_HOURS


@pytest.mark.parametrize("bad", [datetime(2026, 10, 9), "2026-10-09T00:00:00Z", 1767225600])
def test_invalid_now_fails_closed(bad):
    assert expiry_problem(_iso(timedelta(days=90)), bad) == INVALID_NOW


def test_default_now_is_the_clock():
    assert expiry_problem("2099-01-01T00:00:00Z") is None
    assert expiry_problem("2000-01-01T00:00:00Z") == EXPIRED
