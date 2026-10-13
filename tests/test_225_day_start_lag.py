
"""Issue #225 draft: the day's start-of-day reference must be a mark near/before the UTC day roll; a first
evaluation far from the roll (even if this process evaluated yesterday) must not use the current price as
the reference (fallback: cost / worst case, as #124's late rule). Reuses tests/test_leg_marks.py's env.
Offline. No network, no credentials, no orders.

Task CX (#225): R1-R7 / G1-G7 are Poly执行's draft (dev cf01ae1); the A*/H*/P* cases are the
acceptance items of the issue's D1/D3 rulings."""
from __future__ import annotations

import json

import pytest

from src import paper_ledger
from src.podlib import load_risk
from src.risk_gate import evaluate
from src import day_boundary
from src.thresholds import DAY_START_MAX_LAG_S, MAX_BOOK_AGE_S
from tests.test_leg_marks import CAP_USD, NOW, TODAY, _book_fills, _books, _intent, book, env, fill  # noqa: F401

YDAY = "2026-08-31"
FIVE = [f"m{i}" for i in range(5)]


def _five_legs_yesterday(env):
    _book_fills(env["ledger"], [fill("buy", 20, 0.5, "2026-08-31T10:00:00Z", market=m) for m in FIVE])


# --- RED: first tick of the UTC day, far from the roll, no start-of-day valuation ---------------------

def test_r1_issue_repro_five_legs_noon_drop_to_010_counts_40(env, monkeypatch):
    """#225 repro: 5 legs x $10 cost, evaluated yesterday, first read today at 12:00 at 0.10 -> loss >= $40."""
    _five_legs_yesterday(env)
    monkeypatch.setattr(paper_ledger, "_LAST_EVAL_DAY", YDAY)  # evaluated yesterday (any time)
    _books(env["books"], *[book(0.10, ask=0.12, market=m) for m in FIVE])
    state = paper_ledger.load_state("x", "t")
    assert state["daily_unrealized_loss_usd"] >= 40.0 - 1e-6, (state["daily_unrealized_loss_usd"], state["daily_marks_late"])


def test_r2_the_repro_reaches_riskgate_as_max_daily_loss(env, monkeypatch):
    _five_legs_yesterday(env)
    monkeypatch.setattr(paper_ledger, "_LAST_EVAL_DAY", YDAY)
    _books(env["books"], *[book(0.10, ask=0.12, market=m) for m in FIVE])
    state = paper_ledger.load_state("x", "t")
    assert state["daily_loss_usd"] >= CAP_USD  # 40 > 30
    assert "max_daily_loss" in evaluate(_intent(), load_risk(), state).reasons


def test_r3_noon_first_snapshot_is_marked_late(env, monkeypatch):
    _book_fills(env["ledger"], [fill("buy", 100, 0.45, "2026-08-31T20:00:00Z")])
    monkeypatch.setattr(paper_ledger, "_LAST_EVAL_DAY", YDAY)
    _books(env["books"], book(0.40, ask=0.42))
    state = paper_ledger.load_state("x", "t")
    snap = json.loads((env["marks"] / f"{TODAY}.json").read_text())
    assert snap["late"] is True and state["daily_marks_late"] is True
    assert state["daily_unrealized_loss_usd"] == pytest.approx(5.0)  # cost 45 vs 40, like a restart


def test_r4_day_roll_book_exists_but_first_valuation_is_delayed_to_noon(env, monkeypatch):
    """A 00:00:05 book (0.50) was recorded, but the first evaluation only runs at 12:00 (0.40):
    the reference must not be the 12:00 price."""
    _book_fills(env["ledger"], [fill("buy", 100, 0.45, "2026-08-31T20:00:00Z")])
    monkeypatch.setattr(paper_ledger, "_LAST_EVAL_DAY", YDAY)
    _books(env["books"], book(0.50, ask=0.52, ts="2026-09-01T00:00:05Z"), book(0.40, ask=0.42))
    state = paper_ledger.load_state("x", "t")
    assert state["daily_unrealized_loss_usd"] >= 5.0 - 1e-6  # >= cost ref (5); 10 if the 00:00 mark is used


def test_r5_previous_days_valuation_arrives_late_after_midnight(env, monkeypatch):
    """Yesterday's last valuation ran at 10:00; the process stalled; first tick today 11:59. The yesterday
    evaluation is 26h old, so it says nothing about the 00:00 price."""
    _book_fills(env["ledger"], [fill("buy", 100, 0.5, "2026-08-31T09:00:00Z")])
    env["clock"]["now"] = "2026-08-31T10:00:00Z"
    _books(env["books"], book(0.50, ask=0.52, ts="2026-08-31T09:59:55Z"))
    paper_ledger.load_state("x", "t")  # sets _LAST_EVAL_DAY = 08-31 at 10:00
    env["clock"]["now"] = NOW
    _books(env["books"], book(0.30, ask=0.32))
    state = paper_ledger.load_state("x", "t")
    assert state["daily_unrealized_loss_usd"] >= 20.0 - 1e-6, state["daily_marks"]


def test_r6_multi_leg_mixed_states(env, monkeypatch):
    """A held since yesterday (noon first tick, 0.5 -> 0.2, cost 10 -> >= 6), B opened today
    (cost ref 0.5 -> 0.4: 2), C held since yesterday and gaining (0.5 -> 0.7: 0). Total >= 8."""
    _book_fills(env["ledger"], [fill("buy", 20, 0.5, "2026-08-31T10:00:00Z", market="A"),
                                fill("buy", 20, 0.5, "2026-08-31T10:00:00Z", market="C"),
                                fill("buy", 20, 0.5, "2026-09-01T11:00:00Z", market="B")])
    monkeypatch.setattr(paper_ledger, "_LAST_EVAL_DAY", YDAY)
    _books(env["books"], book(0.20, ask=0.22, market="A"), book(0.40, ask=0.42, market="B"),
           book(0.70, ask=0.72, market="C"))
    state = paper_ledger.load_state("x", "t")
    legs = {leg["leg_key"].split("|")[0] if "|" in leg["leg_key"] else leg["leg_key"]: leg for leg in state["daily_marks"]}
    assert state["daily_unrealized_loss_usd"] >= 8.0 - 1e-6, legs


def test_r7_multi_leg_one_stale_book_still_uses_cost_for_the_others(env, monkeypatch):
    """Mixed: A has a fresh 0.10 book at noon, B has no book (worst case). Loss >= 8 (A) + 10 (B)."""
    _book_fills(env["ledger"], [fill("buy", 20, 0.5, "2026-08-31T10:00:00Z", market="A"),
                                fill("buy", 20, 0.5, "2026-08-31T10:00:00Z", market="B")])
    monkeypatch.setattr(paper_ledger, "_LAST_EVAL_DAY", YDAY)
    _books(env["books"], book(0.10, ask=0.12, market="A"))
    state = paper_ledger.load_state("x", "t")
    assert state["daily_unrealized_loss_usd"] >= 18.0 - 1e-6


# --- GREEN: must stay green after the fix -----------------------------------------------------------

def test_g1_on_time_snapshot_at_the_roll_is_not_late(env, monkeypatch):
    _book_fills(env["ledger"], [fill("buy", 100, 0.45, "2026-08-31T20:00:00Z")])
    monkeypatch.setattr(paper_ledger, "_LAST_EVAL_DAY", YDAY)
    env["clock"]["now"] = "2026-09-01T00:00:10Z"
    _books(env["books"], book(0.50, ask=0.52, ts="2026-09-01T00:00:05Z"))
    first = paper_ledger.load_state("x", "t")
    assert first["daily_marks_late"] is False and first["daily_unrealized_loss_usd"] == 0.0
    env["clock"]["now"] = NOW
    _books(env["books"], book(0.40, ask=0.42))
    assert paper_ledger.load_state("x", "t")["daily_unrealized_loss_usd"] == pytest.approx(10.0)


def test_g2_restart_without_yesterday_is_late_and_uses_cost(env):
    _book_fills(env["ledger"], [fill("buy", 100, 0.45, "2026-08-31T20:00:00Z")])
    _books(env["books"], book(0.40, ask=0.42))
    state = paper_ledger.load_state("x", "t")
    assert state["daily_marks_late"] is True and state["daily_unrealized_loss_usd"] == pytest.approx(5.0)


def test_g3_midnight_cross_does_not_count_yesterday_twice(env):
    _book_fills(env["ledger"], [fill("buy", 100, 0.5, "2026-08-31T10:00:00Z")])
    env["clock"]["now"] = "2026-08-31T23:59:50Z"
    _books(env["books"], book(0.40, ask=0.42, ts="2026-08-31T23:59:45Z"))
    paper_ledger.load_state("x", "t")
    env["clock"]["now"] = "2026-09-01T00:00:10Z"
    _books(env["books"], book(0.40, ask=0.42, ts="2026-09-01T00:00:05Z"))
    assert paper_ledger.load_state("x", "t")["daily_unrealized_loss_usd"] == 0.0


def test_g4_leg_opened_today_uses_todays_cost(env, monkeypatch):
    _book_fills(env["ledger"], [fill("buy", 100, 0.5, "2026-09-01T11:00:00Z")])
    monkeypatch.setattr(paper_ledger, "_LAST_EVAL_DAY", YDAY)
    _books(env["books"], book(0.45, ask=0.47))
    assert paper_ledger.load_state("x", "t")["daily_unrealized_loss_usd"] == pytest.approx(5.0)


def test_g5_the_snapshot_is_written_once_and_reused(env, monkeypatch):
    _book_fills(env["ledger"], [fill("buy", 100, 0.45, "2026-08-31T20:00:00Z")])
    monkeypatch.setattr(paper_ledger, "_LAST_EVAL_DAY", YDAY)
    env["clock"]["now"] = "2026-09-01T00:00:10Z"
    _books(env["books"], book(0.50, ask=0.52, ts="2026-09-01T00:00:05Z"))
    paper_ledger.load_state("x", "t")
    raw = (env["marks"] / f"{TODAY}.json").read_text()
    env["clock"]["now"] = NOW
    _books(env["books"], book(0.40, ask=0.42))
    paper_ledger.load_state("x", "t")
    assert (env["marks"] / f"{TODAY}.json").read_text() == raw


def test_g6_no_open_legs_is_zero(env, monkeypatch):
    monkeypatch.setattr(paper_ledger, "_LAST_EVAL_DAY", YDAY)
    s = paper_ledger.load_state("x", "t")
    assert (s["daily_loss_usd"], s["daily_unrealized_loss_usd"]) == (0.0, 0.0)


def test_g7_a_gain_never_offsets_a_loss(env, monkeypatch):
    _book_fills(env["ledger"], [fill("buy", 20, 0.5, "2026-08-31T20:00:00Z", market="A"),
                                fill("buy", 20, 0.5, "2026-08-31T20:00:00Z", market="B")])
    monkeypatch.setattr(paper_ledger, "_LAST_EVAL_DAY", YDAY)
    env["clock"]["now"] = "2026-09-01T00:00:10Z"
    _books(env["books"], book(0.50, ask=0.52, market="A", ts="2026-09-01T00:00:05Z"),
           book(0.50, ask=0.52, market="B", ts="2026-09-01T00:00:05Z"))
    paper_ledger.load_state("x", "t")
    env["clock"]["now"] = NOW
    _books(env["books"], book(0.40, ask=0.42, market="A"), book(0.70, ask=0.72, market="B"))
    assert paper_ledger.load_state("x", "t")["daily_unrealized_loss_usd"] == pytest.approx(2.0)


# --- acceptance items of the #225 rulings (D1, D3) ---------------------------------------------------

def test_a1_threshold_references_max_book_age():
    assert DAY_START_MAX_LAG_S == MAX_BOOK_AGE_S == 120.0


@pytest.mark.parametrize("now,late", [
    ("2026-09-01T00:00:00Z", False),
    ("2026-09-01T00:01:59Z", False),
    ("2026-09-01T00:02:00Z", False),   # exactly the threshold is not late
    ("2026-09-01T00:02:01Z", True),
    ("2026-09-01T12:00:00Z", True),
    ("2026-09-01T23:59:59Z", True),
])
def test_h1_day_start_late_boundary(now, late):
    assert day_boundary.day_start_late(now) is late


@pytest.mark.parametrize("now", [None, "", "garbage", "2026-13-01T00:00:00Z", 12345])
def test_h2_clock_anomaly_or_missing_timestamp_is_late(now):
    assert day_boundary.day_start_late(now) is True


def test_h3_unverified_clock_is_late_even_at_the_roll():
    assert day_boundary.day_start_late("2026-09-01T00:00:10Z", clock_ok=False) is True


def test_h4_lag_is_measured_from_that_days_utc_midnight():
    assert day_boundary.day_start_lag_s("2026-09-01T00:01:00Z") == pytest.approx(60.0)
    assert day_boundary.day_start_lag_s("2026-09-01T08:01:00+08:00") == pytest.approx(60.0)  # 00:01 UTC
    assert day_boundary.day_start_lag_s(None) is None


@pytest.mark.parametrize("now,late", [("2026-09-01T00:01:59Z", False), ("2026-09-01T00:02:01Z", True)])
def test_a2_snapshot_written_at_0159_is_on_time_and_at_0201_is_late(env, monkeypatch, now, late):
    _book_fills(env["ledger"], [fill("buy", 100, 0.45, "2026-08-31T20:00:00Z")])
    monkeypatch.setattr(paper_ledger, "_LAST_EVAL_DAY", YDAY)
    env["clock"]["now"] = now
    _books(env["books"], book(0.50, ask=0.52, ts="2026-09-01T00:01:00Z"))
    state = paper_ledger.load_state("x", "t")
    snap = json.loads((env["marks"] / f"{TODAY}.json").read_text())
    assert snap["late"] is late and state["daily_marks_late"] is late


def test_a3_issue_acceptance_five_legs_late_40_and_halts_the_day(env, monkeypatch):
    """5 legs x $10, first evaluation at noon with bid 0.10: late=True, loss $40 -> max_daily_loss."""
    _five_legs_yesterday(env)
    monkeypatch.setattr(paper_ledger, "_LAST_EVAL_DAY", YDAY)
    _books(env["books"], *[book(0.10, ask=0.12, market=m) for m in FIVE])
    state = paper_ledger.load_state("x", "t")
    assert state["daily_marks_late"] is True
    assert state["daily_unrealized_loss_usd"] == pytest.approx(40.0)
    assert "max_daily_loss" in evaluate(_intent(), load_risk(), state).reasons


def _yesterday_eval(env, price, at="2026-08-31T22:00:00Z"):
    env["clock"]["now"] = at
    _books(env["books"], book(price, ask=price + 0.02, ts=at))
    paper_ledger.load_state("x", "t")
    env["clock"]["now"] = NOW


def test_p1_late_reference_is_previous_days_last_valid_mark_when_above_cost(env):
    """Cost 0.45 (45); yesterday's last valid mark 0.60 (60); today noon 0.40 (40): loss 60-40=20."""
    _book_fills(env["ledger"], [fill("buy", 100, 0.45, "2026-08-31T20:00:00Z")])
    _yesterday_eval(env, 0.55, at="2026-08-31T21:00:00Z")
    _yesterday_eval(env, 0.60)          # the last one of the day wins
    _books(env["books"], book(0.40, ask=0.42))
    state = paper_ledger.load_state("x", "t")
    assert state["daily_marks_late"] is True
    assert state["daily_unrealized_loss_usd"] == pytest.approx(20.0)
    assert state["daily_marks"][0]["ref_source"] == "prev_day_last_mark"


def test_p2_late_reference_stays_cost_when_previous_mark_is_below_cost(env):
    _book_fills(env["ledger"], [fill("buy", 100, 0.45, "2026-08-31T20:00:00Z")])
    _yesterday_eval(env, 0.30)
    _books(env["books"], book(0.20, ask=0.22))
    state = paper_ledger.load_state("x", "t")
    assert state["daily_unrealized_loss_usd"] == pytest.approx(25.0)  # max(45, 30) - 20


def test_p3_invalid_previous_mark_is_not_used(env):
    """Yesterday's only evaluation saw a stale book: no valid mark, so the cost is the reference."""
    _book_fills(env["ledger"], [fill("buy", 100, 0.45, "2026-08-31T20:00:00Z")])
    env["clock"]["now"] = "2026-08-31T22:00:00Z"
    _books(env["books"], book(0.90, ask=0.92, ts="2026-08-31T21:00:00Z"))  # 1h old: invalid
    paper_ledger.load_state("x", "t")
    env["clock"]["now"] = NOW
    _books(env["books"], book(0.40, ask=0.42))
    assert paper_ledger.load_state("x", "t")["daily_unrealized_loss_usd"] == pytest.approx(5.0)


def test_p4_short_leg_uses_the_tighter_signed_reference(env):
    """Short 100 @0.50 (received 50). Yesterday's last buy-back cost 0.40 (-40) is tighter than cost
    (-50); today buy-back 0.60 (-60): loss = -40 - (-60) = 20 (cost alone would give 10)."""
    _book_fills(env["ledger"], [fill("sell", 100, 0.50, "2026-08-31T20:00:00Z")])
    env["clock"]["now"] = "2026-08-31T22:00:00Z"
    _books(env["books"], book(0.38, ask=0.40, ts="2026-08-31T22:00:00Z"))
    paper_ledger.load_state("x", "t")
    env["clock"]["now"] = NOW
    _books(env["books"], book(0.58, ask=0.60))
    assert paper_ledger.load_state("x", "t")["daily_unrealized_loss_usd"] == pytest.approx(20.0)


def test_p5_previous_mark_survives_a_restart(env, monkeypatch):
    _book_fills(env["ledger"], [fill("buy", 100, 0.45, "2026-08-31T20:00:00Z")])
    _yesterday_eval(env, 0.60)
    monkeypatch.setattr(paper_ledger, "_LAST_EVAL_DAY", None)  # process restarted
    _books(env["books"], book(0.40, ask=0.42))
    assert paper_ledger.load_state("x", "t")["daily_unrealized_loss_usd"] == pytest.approx(20.0)


def test_p6_corrupt_previous_marks_file_fails_closed(env):
    _book_fills(env["ledger"], [fill("buy", 100, 0.45, "2026-08-31T20:00:00Z")])
    _yesterday_eval(env, 0.60)
    path = env["marks"] / "last_marks" / f"{YDAY}.json"
    path.write_text("{not json", encoding="utf-8")
    _books(env["books"], book(0.40, ask=0.42))
    state = paper_ledger.load_state("x", "t")
    assert state["daily_loss_usd"] is None
    assert any(p.startswith("daily_marks_last_") for p in state["daily_loss_problems"])
