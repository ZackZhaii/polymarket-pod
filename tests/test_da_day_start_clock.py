"""Task DA (#225 follow-up, PR #273 risk review): the runner's clock state reaches the day-start lateness rule.

day_boundary.day_start_late takes clock_ok, but daily_unrealized called it without it, and the runner's clock
check runs after the daily-loss evaluation. Now preflight passes ``clock_ok`` (False while the runner holds a
clock anomaly: clock_skew today, #253's clock_unverified later) into daily_risk_loss -> daily_unrealized.
Offline. No network, no credentials, no orders."""
from __future__ import annotations

import json

import pytest

from src import ledger_store, m2_runner, paper_ledger
from tests.test_leg_marks import TODAY, YESTERDAY, _book_fills, _books, book, env, fill  # noqa: F401

ROLL_0001 = "2026-09-01T00:01:00Z"


def _rows(env):
    _, read = ledger_store.read_ledger(env["ledger"])
    return read.rows, list(read.ids)


def _snap(env):
    return json.loads((env["marks"] / f"{TODAY}.json").read_text())


@pytest.fixture
def leg(env, monkeypatch):
    _book_fills(env["ledger"], [fill("buy", 100, 0.45, YESTERDAY)])
    monkeypatch.setattr(paper_ledger, "_LAST_EVAL_DAY", "2026-08-31")
    _books(env["books"], book(0.50, ask=0.52, ts="2026-09-01T00:00:55Z"))
    return env


def test_d1_clock_anomaly_snapshot_at_0001_is_late(leg):
    rows, ids = _rows(leg)
    risk = paper_ledger.daily_risk_loss(rows, ids, ROLL_0001, clock_ok=False)
    assert not risk["problems"]
    assert _snap(leg)["late"] is True and risk["marks"]["late"] is True


def test_d2_sane_clock_snapshot_at_0001_is_on_time(leg):
    rows, ids = _rows(leg)
    risk = paper_ledger.daily_risk_loss(rows, ids, ROLL_0001, clock_ok=True)
    assert _snap(leg)["late"] is False and risk["marks"]["late"] is False


def test_d3_daily_unrealized_accepts_generic_clock_flag(leg):
    rows, ids = _rows(leg)
    marks = paper_ledger.daily_unrealized(rows, ids, ROLL_0001, clock_ok=False)
    assert marks["late"] is True


@pytest.mark.parametrize("bad", [None, 0, "no"])
def test_d4_non_true_clock_flag_is_late(leg, bad):
    rows, ids = _rows(leg)
    paper_ledger.daily_unrealized(rows, ids, ROLL_0001, clock_ok=bad)
    assert _snap(leg)["late"] is True


def test_d5_preflight_takes_a_clock_ok_flag_default_true():
    import inspect
    assert "clock_ok" in inspect.signature(m2_runner.preflight).parameters
    assert inspect.signature(m2_runner.preflight).parameters["clock_ok"].default is True


@pytest.mark.parametrize("reason,ok", [(None, True), ("clock_skew", False), ("clock_unverified", False)])
def test_d6_runner_clock_ok_from_clock_anomaly(tmp_path, reason, ok):
    state = m2_runner.RunnerState(paths=m2_runner.RunnerPaths.under(tmp_path))
    state.clock_anomaly = reason
    assert m2_runner.runner_clock_ok(state) is ok
