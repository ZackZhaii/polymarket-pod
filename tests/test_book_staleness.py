"""Offline order book staleness tests (Task L). No network, no .env, no order SDK."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import src.book_staleness as staleness
import src.risk_gate as risk_gate
from src.book_staleness import MAX_BOOK_AGE_S, book_age_problem, parse_book_ts
from src.podlib import RISK_PATH, load_risk, utc_now
from src.risk_gate import Intent, evaluate

NOW = datetime(2026, 10, 9, 1, 0, 0, tzinfo=timezone.utc)
STATE = {"open_positions": 0, "market_already_open": False, "exposure_usd": 0.0, "leg_exposure_usd": 0.0, "daily_loss_usd": 0.0, "recent_fills": [], "own_quotes": []}


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _ago(seconds: float) -> str:
    return _iso(NOW - timedelta(seconds=seconds))


def _intent(**overrides: object) -> Intent:
    fields: dict[str, object] = {
        "strategy": "paper-gap-check",
        "market": "test-market",
        "side": "buy",
        "model_prob": 0.70,
        "market_price": 0.40,
        "assumed_slippage": 0.005,
        "fee_rate": 0.0,
        "liquidity_usd": 20000.0,
        "spread": 0.01,
        "hypothetical": False,
        "book_ts": _ago(5),
    }
    fields.update(overrides)
    fields.setdefault("tags", ("test-market",))  # issue #131: a tagged market (missing tags veto)
    return Intent(**fields)  # type: ignore[arg-type]


# --- pure helper ---------------------------------------------------------------------------


def test_default_max_age_is_120_seconds_and_is_not_a_risk_toml_key():
    assert MAX_BOOK_AGE_S == 120
    text = RISK_PATH.read_text(encoding="utf-8")
    assert "book_age" not in text
    assert "stale" not in text
    assert not hasattr(staleness, "urlopen")


def test_fresh_book_passes():
    assert book_age_problem(_ago(0), NOW) is None
    assert book_age_problem(_ago(30), NOW) is None


def test_boundary_exactly_max_age_is_fresh_one_second_more_is_stale():
    assert book_age_problem(_ago(120), NOW) is None
    assert book_age_problem(_ago(121), NOW) == "stale_book"
    assert book_age_problem(NOW - timedelta(seconds=120.001), NOW) == "stale_book"


def test_stale_book_is_flagged():
    assert book_age_problem(_ago(600), NOW) == "stale_book"
    assert book_age_problem(_ago(86400 * 3), NOW) == "stale_book"


@pytest.mark.parametrize("value", [None, "", "   "])
def test_missing_timestamp(value):
    assert book_age_problem(value, NOW) == "missing_book_ts"


@pytest.mark.parametrize(
    "value",
    [
        "not-a-time",
        "2026-13-40T00:00:00Z",
        "2026-10-09",  # date only
        "2026-10-09T00:59:30",  # no zone
        "1760000000",  # epoch as text is ambiguous
        True,
        False,
        [],
        {"ts": _iso(NOW)},
        object(),
    ],
)
def test_unparseable_timestamp(value):
    assert book_age_problem(value, NOW) == "invalid_book_ts"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), 1e300])
def test_non_finite_or_absurd_numeric_timestamp(value):
    assert book_age_problem(value, NOW) == "invalid_book_ts"


def test_future_timestamp_is_flagged():
    assert book_age_problem(_iso(NOW + timedelta(seconds=1)), NOW) == "future_book_ts"
    assert book_age_problem(NOW + timedelta(hours=1), NOW) == "future_book_ts"


def test_accepted_formats():
    assert book_age_problem("2026-10-09T00:59:30.123Z", NOW) is None
    assert book_age_problem("2026-10-09T08:59:30+08:00", NOW) is None  # 00:59:30Z
    assert book_age_problem(NOW - timedelta(seconds=10), NOW) is None
    assert book_age_problem(NOW.timestamp() - 10, NOW) is None
    assert book_age_problem(int(NOW.timestamp()) - 10, NOW) is None


def test_offset_timestamp_is_compared_in_utc():
    # 09:01 at +08:00 is 01:01Z: one minute in the future, not eight hours old.
    assert book_age_problem("2026-10-09T09:01:00+08:00", NOW) == "future_book_ts"


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), 0, -1, True, "120", None])
def test_invalid_max_age_fails_closed_even_for_a_fresh_book(bad):
    assert book_age_problem(_ago(1), NOW, max_age_s=bad) == "invalid_max_book_age"


@pytest.mark.parametrize("bad", [datetime(2026, 10, 9, 1, 0, 0), float("nan"), "later", True])
def test_invalid_now_fails_closed(bad):
    assert book_age_problem(_ago(1), bad) == "invalid_now"


def test_custom_max_age():
    assert book_age_problem(_ago(30), NOW, max_age_s=10) == "stale_book"
    assert book_age_problem(_ago(30), NOW, max_age_s=30.0) is None


def test_default_now_reads_the_clock():
    assert book_age_problem(utc_now()) is None
    assert book_age_problem("2020-01-01T00:00:00Z") == "stale_book"


def test_parse_book_ts_returns_aware_utc_or_none():
    parsed = parse_book_ts("2026-10-09T08:59:30+08:00")
    assert parsed == datetime(2026, 10, 9, 0, 59, 30, tzinfo=timezone.utc)
    assert parsed.tzinfo is timezone.utc
    assert parse_book_ts("nope") is None
    assert parse_book_ts(None) is None


# --- RiskGate wiring (fail closed) ---------------------------------------------------------


def test_evaluate_allows_a_fresh_book():
    decision = evaluate(_intent(), load_risk(), STATE, now=NOW)
    assert decision.allow is True
    assert decision.reasons == []


def test_intent_without_book_ts_is_vetoed():
    fields = dict(
        strategy="paper-gap-check",
        market="test-market",
        side="buy",
        model_prob=0.70,
        market_price=0.40,
        assumed_slippage=0.005,
        fee_rate=0.0,
        liquidity_usd=20000.0,
        spread=0.01,
    )
    decision = evaluate(Intent(**fields), load_risk(), STATE, now=NOW)  # type: ignore[arg-type]
    assert decision.allow is False
    assert "missing_book_ts" in decision.reasons


@pytest.mark.parametrize(
    "book_ts, reason",
    [
        (_ago(121), "stale_book"),
        (_iso(NOW + timedelta(seconds=5)), "future_book_ts"),
        ("garbage", "invalid_book_ts"),
        (float("nan"), "invalid_book_ts"),
        (float("inf"), "invalid_book_ts"),
        ("", "missing_book_ts"),
    ],
)
def test_evaluate_vetoes_bad_book_timestamps(book_ts, reason):
    decision = evaluate(_intent(book_ts=book_ts), load_risk(), STATE, now=NOW)
    assert decision.allow is False
    assert reason in decision.reasons


def test_evaluate_boundary_and_custom_max_age():
    cfg = load_risk()
    assert evaluate(_intent(book_ts=_ago(120)), cfg, STATE, now=NOW).allow is True
    tight = evaluate(_intent(book_ts=_ago(30)), cfg, STATE, now=NOW, max_book_age_s=10)
    assert tight.allow is False and "stale_book" in tight.reasons
    broken = evaluate(_intent(), cfg, STATE, now=NOW, max_book_age_s=float("nan"))
    assert broken.allow is False and "invalid_max_book_age" in broken.reasons


def test_hypothetical_intents_are_checked_too():
    decision = evaluate(
        _intent(strategy="phase0-gate-test", hypothetical=True, book_ts=_ago(600)), load_risk(), STATE, now=NOW
    )
    assert decision.allow is False
    assert "stale_book" in decision.reasons


def test_evaluate_default_now_uses_the_clock():
    assert evaluate(_intent(book_ts=utc_now()), load_risk(), STATE).allow is True


def test_gate_default_matches_the_helper_and_risk_file_stays_put():
    before = RISK_PATH.read_bytes()
    evaluate(_intent(book_ts=_ago(500)), load_risk(), STATE, now=NOW)
    assert RISK_PATH.read_bytes() == before
    assert risk_gate.MAX_BOOK_AGE_S == MAX_BOOK_AGE_S


def test_paper_ledger_self_check_intent_carries_a_fresh_book_ts():
    from src.paper_ledger import _intent as ledger_intent

    assert book_age_problem(ledger_intent().book_ts) is None
