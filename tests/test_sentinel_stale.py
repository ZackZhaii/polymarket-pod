"""Task Z: Sentinel alarms after 2 consecutive stale/failed book checks. Offline, paper only."""
from __future__ import annotations

import inspect
import json
from datetime import datetime, timedelta, timezone

import pytest

import src.book_staleness as book_staleness
import src.maker as maker
import src.paper_cycle as paper_cycle
import src.sentinel as sentinel
from src.podlib import load_risk, read_jsonl
from src.risk_gate import Intent, evaluate

NOW = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)
FRESH = (NOW - timedelta(seconds=30)).isoformat().replace("+00:00", "Z")
STALE = (NOW - timedelta(seconds=121)).isoformat().replace("+00:00", "Z")


@pytest.fixture()
def paper(tmp_path, fresh_book):
    quotes = tmp_path / "quotes.jsonl"
    bus = tmp_path / "bus.jsonl"
    for market, token in (("m1", "a"), ("m2", "b")):
        fresh_book(market, token)
        maker.quote(market=market, token_id=token, side="buy", price=0.40, size=5,
                    bus_path=bus, quotes_path=quotes)
    return {"quotes": quotes, "bus": bus}


def _watch(paper, **kwargs):
    return sentinel.StaleWatch(bus_path=paper["bus"], quotes_path=paper["quotes"], **kwargs)


def _halts(paper):
    return [row for row in read_jsonl(paper["bus"]) if row["type"] == "halt"]


def _status(paper):
    return {row["market"]: row["status"] for row in read_jsonl(paper["quotes"])}


def test_constants_are_the_proposal_and_match_riskgate():
    assert sentinel.SENTINEL_STALE_S == 120.0
    assert sentinel.SENTINEL_STALE_CONSECUTIVE == 2
    assert sentinel.SENTINEL_STALE_S == book_staleness.MAX_BOOK_AGE_S
    assert paper_cycle.STALE_AFTER_S == 120


def test_one_stale_check_reports_the_problem_but_does_not_alarm(paper):
    watch = _watch(paper)
    result = watch.check("m1", STALE, NOW)
    assert result == {"market": "m1", "stale": True, "problem": "stale_book", "streak": 1, "halt": None}
    assert _halts(paper) == []
    assert _status(paper) == {"m1": "open", "m2": "open"}


def test_two_consecutive_stale_checks_halt_that_market_once(paper):
    watch = _watch(paper)
    watch.check("m1", STALE, NOW)
    result = watch.check("m1", STALE, NOW + timedelta(seconds=30))
    assert result["streak"] == 2
    message = result["halt"]
    assert message["from"] == "Sentinel" and message["to"] == "Executor" and message["type"] == "halt"
    assert message["market"] == "m1"
    assert message["payload"]["exchange_action"] == "none"
    assert message["payload"]["paper_quotes_cancelled"] == 1
    assert message["payload"]["reason"].startswith("stale_book_consecutive: 2 consecutive")
    assert _status(paper) == {"m1": "cancelled", "m2": "open"}
    third = watch.check("m1", STALE, NOW + timedelta(seconds=60))
    assert third["streak"] == 3 and third["stale"] is True and third["halt"] is None
    assert len(_halts(paper)) == 1
    assert "fill" not in {row["type"] for row in read_jsonl(paper["bus"])}


def test_a_fresh_check_resets_the_streak(paper):
    watch = _watch(paper)
    watch.check("m1", STALE, NOW)
    fresh = watch.check("m1", FRESH, NOW)
    assert fresh == {"market": "m1", "stale": False, "problem": None, "streak": 0, "halt": None}
    assert watch.check("m1", STALE, NOW)["halt"] is None
    assert _halts(paper) == []


def test_alarm_re_arms_after_a_fresh_check(paper):
    watch = _watch(paper)
    watch.check("m1", STALE, NOW)
    assert watch.check("m1", STALE, NOW)["halt"] is not None
    watch.check("m1", FRESH, NOW)
    watch.check("m1", STALE, NOW)
    assert watch.check("m1", STALE, NOW)["halt"] is not None
    assert len(_halts(paper)) == 2


def test_streaks_are_per_market(paper):
    watch = _watch(paper)
    watch.check("m1", STALE, NOW)
    watch.check("m2", STALE, NOW)
    assert _halts(paper) == []
    assert watch.check("m2", STALE, NOW)["halt"]["market"] == "m2"
    assert _status(paper) == {"m1": "open", "m2": "cancelled"}


@pytest.mark.parametrize(
    ("book_ts", "problem"),
    [
        (None, "missing_book_ts"),
        ("", "missing_book_ts"),
        ("NaN", "invalid_book_ts"),
        ("inf", "invalid_book_ts"),
        (float("nan"), "invalid_book_ts"),
        (float("inf"), "invalid_book_ts"),
        (float("-inf"), "invalid_book_ts"),
        (True, "invalid_book_ts"),
        ("2026-10-09T11:59:30", "invalid_book_ts"),
        ("2026-10-09", "invalid_book_ts"),
        ((NOW + timedelta(seconds=5)).isoformat(), "future_book_ts"),
    ],
)
def test_missing_or_non_finite_timestamps_count_as_stale(paper, book_ts, problem):
    watch = _watch(paper)
    first = watch.check("m1", book_ts, NOW)
    assert first["stale"] is True and first["problem"] == problem and first["halt"] is None
    assert watch.check("m1", book_ts, NOW)["halt"] is not None


def test_a_failed_check_counts_as_stale_even_with_a_fresh_stamp(paper):
    watch = _watch(paper)
    assert watch.check("m1", FRESH, NOW, failed=True)["problem"] == sentinel.CHECK_FAILED
    result = watch.check("m1", None, NOW)
    assert result["streak"] == 2 and result["halt"] is not None
    assert "check_failed" not in result["halt"]["payload"]["reason"]  # last problem is named
    assert "missing_book_ts" in result["halt"]["payload"]["reason"]


@pytest.mark.parametrize("now", [None, "2026-10-09T12:00:00Z", datetime(2026, 10, 9, 12), float("nan")])
def test_a_broken_now_counts_as_stale(paper, now):
    watch = _watch(paper)
    assert watch.check("m1", FRESH, now)["problem"] == "invalid_now"


def test_boundary_matches_riskgate(paper):
    watch = _watch(paper)
    exactly = (NOW - timedelta(seconds=120)).isoformat()
    over = (NOW - timedelta(seconds=120, milliseconds=1)).isoformat()
    assert watch.check("m1", exactly, NOW)["stale"] is False
    assert watch.check("m1", over, NOW)["problem"] == "stale_book"


def _intent(book_ts: object) -> Intent:
    return Intent(strategy="paper-gap-check", market="m1", side="buy", model_prob=0.70, market_price=0.40,
                  assumed_slippage=0.005, fee_rate=0.0, liquidity_usd=20000.0, spread=0.01, book_ts=book_ts,
                  tags=("test-market",), min_order_size=5.0)  # issue #131, #226


def test_a_single_stale_check_is_still_blocked_per_check(paper):
    """The alarm does not replace the per-check block: RiskGate vetoes one stale book as before,
    while Sentinel has not alarmed yet (streak 1)."""
    watch = _watch(paper)
    assert watch.check("m1", STALE, NOW)["halt"] is None
    state = {"open_positions": 0, "market_already_open": False, "exposure_usd": 0.0, "leg_exposure_usd": 0.0, "opposite_leg_shares": 0.0, "daily_loss_usd": 0.0,
             "recent_fills": [], "own_quotes": []}
    assert evaluate(_intent(FRESH), load_risk(), state, now=NOW).allow is True
    decision = evaluate(_intent(STALE), load_risk(), state, now=NOW)
    assert decision.allow is False
    assert "stale_book" in decision.reasons


@pytest.mark.parametrize(
    "kwargs",
    [
        {"consecutive": 3},
        {"consecutive": 0},
        {"consecutive": True},
        {"consecutive": 2.0},
        {"max_age_s": 121},
        {"max_age_s": 0},
        {"max_age_s": float("nan")},
        {"max_age_s": float("inf")},
        {"max_age_s": "120"},
        {"max_age_s": True},
    ],
)
def test_limits_can_only_be_made_stricter(paper, kwargs):
    with pytest.raises(ValueError):
        _watch(paper, **kwargs)


def test_stricter_limits_are_allowed(paper):
    watch = _watch(paper, consecutive=1, max_age_s=60)
    assert watch.check("m1", (NOW - timedelta(seconds=61)).isoformat(), NOW)["halt"] is not None


def test_state_round_trips_across_runs(paper):
    watch = _watch(paper)
    watch.check("m1", STALE, NOW)
    saved = json.loads(json.dumps(watch.to_dict()))
    assert saved == {"max_age_s": 120.0, "consecutive": 2, "streaks": {"m1": 1}, "alarmed": [], "all_stale_streak": 0}
    again = sentinel.StaleWatch.from_dict(saved, bus_path=paper["bus"], quotes_path=paper["quotes"])
    assert again.check("m1", STALE, NOW)["halt"] is not None
    after = sentinel.StaleWatch.from_dict(again.to_dict(), bus_path=paper["bus"], quotes_path=paper["quotes"])
    assert after.check("m1", STALE, NOW)["halt"] is None  # still the same alarmed streak
    assert len(_halts(paper)) == 1


@pytest.mark.parametrize(
    "state",
    [
        None,
        [],
        {"max_age_s": 120.0, "consecutive": 2, "streaks": {"m1": 0}, "alarmed": []},
        {"max_age_s": 120.0, "consecutive": 2, "streaks": {"m1": "1"}, "alarmed": []},
        {"max_age_s": 120.0, "consecutive": 2, "streaks": {}, "alarmed": ["m1"]},
        {"max_age_s": 120.0, "consecutive": 2, "streaks": [], "alarmed": []},
        {"max_age_s": 300.0, "consecutive": 2, "streaks": {}, "alarmed": []},
        {"max_age_s": 120.0, "consecutive": 5, "streaks": {}, "alarmed": []},
        {"streaks": {}, "alarmed": []},
    ],
)
def test_bad_state_raises_instead_of_resetting(state):
    with pytest.raises(ValueError):
        sentinel.StaleWatch.from_dict(state)


def test_halt_and_cancel_are_unchanged_and_offline():
    source = inspect.getsource(sentinel)
    for banned in ("urlopen", "clob.polymarket", "py_clob_client", "requests", "public_get", "load_risk", "RISK_PATH"):
        assert banned not in source
    # Task AL added scope / source / to / extra / emit after the original four (all keyword-only).
    assert list(inspect.signature(sentinel.halt).parameters) == [
        "reason", "market", "bus_path", "quotes_path", "scope", "source", "to", "extra", "emit"]
