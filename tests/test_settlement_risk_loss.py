"""C217: closed losses retain the day-start reference without netting gains; offline only."""
from __future__ import annotations

from datetime import datetime

import pytest

from src import daily_marks, leg_marks, ledger_store, m2_runner, paper_ledger, settlement
from src.podlib import load_risk
from tests import m2_runner_rig as rig
from tests.test_leg_marks import NOW, TODAY, YESTERDAY, _intent, _snap_leg, fill

NOW_DT = datetime.fromisoformat(NOW.replace("Z", "+00:00"))


@pytest.fixture
def env(tmp_path, monkeypatch):
    paths = rig.isolate(monkeypatch, tmp_path)
    paths["run"].mkdir(parents=True)
    marks = tmp_path / "daily_marks"
    clock = {"now": NOW}
    monkeypatch.setattr(paper_ledger, "DAILY_MARKS_DIR", marks)
    monkeypatch.setattr(paper_ledger, "utc_now", lambda: clock["now"])
    return {**paths, "marks": marks, "clock": clock}


def _book(env, *rows):
    offset = len(ledger_store.read_ledger(env["paper"])[1].rows)
    for index, row in enumerate(rows, offset):
        ledger_store.book(env["paper"], {"fill_id": f"c217-{index}", **row})


def _snapshot(env, mark=18.0, *, late=False, invalid=False, shares=20.0):
    daily_marks.create_snapshot(env["marks"], TODAY, NOW, late,
                                [_snap_leg(shares, 10.0, mark, invalid)])


def _settle(env, payout=0.0, now=NOW):
    read = ledger_store.read_ledger(env["paper"])[1]
    snap = {"condition_id": "c217-condition", "winning_token_id": "t" if payout else "other",
            "outcome_token_ids": ["t", "other"], "status": "resolved", "uma_checked": True,
            "outcome_prices_final": [payout, 1.0 - payout], "closed_ts": YESTERDAY}
    plan = settlement.plan(read.rows, read.ids, [snap], now)
    assert plan.problems == []
    for row in plan.rows:
        ledger_store.book(env["paper"], row)
    return []


def _assert_loss(env, risk, cost_loss, gain=0.0, unrealized=0.0, topup=0.0):
    state = paper_ledger.load_state("x", "x-token")
    assert state["ledger_problems"] == []
    assert state["daily_realized_loss_usd"] == pytest.approx(cost_loss)
    assert state["daily_realized_gain_usd"] == pytest.approx(gain)
    assert state["daily_unrealized_loss_usd"] == pytest.approx(unrealized)
    assert state["daily_loss_usd"] == pytest.approx(risk + state["daily_fees_usd"])
    assert state["daily_loss_usd"] - cost_loss - unrealized - state["daily_fees_usd"] == pytest.approx(topup)
    return state


@pytest.mark.parametrize("mark,risk,topup", [(18.0, 18.0, 8.0), (8.0, 10.0, 0.0)])
def test_a_settled_loser_keeps_the_higher_start_reference(env, mark, risk, topup):
    _book(env, fill("buy", 20, 0.5, YESTERDAY))
    _snapshot(env, mark)
    _settle(env)
    _assert_loss(env, risk, 10.0, topup=topup)
    assert _settle(env) == []  # a repeated settlement has no row to book
    _assert_loss(env, risk, 10.0, topup=topup)
    env["clock"]["now"] = "2026-09-02T00:00:01Z"
    _assert_loss(env, 0.0, 0.0)


def test_today_only_has_no_day_start_topup(env):
    _book(env, fill("buy", 20, 0.5))
    _settle(env)
    _assert_loss(env, 10.0, 10.0)


@pytest.mark.parametrize("partial", [False, True])
def test_mixed_old_and_today_costs_keep_the_original_start_reference(env, partial):
    _book(env, fill("buy", 20, 0.5, YESTERDAY), fill("buy", 20, 0.9))
    _snapshot(env)
    if partial:
        _book(env, fill("sell", 20, 0.0))
        _assert_loss(env, 36.0, 14.0, unrealized=18.0, topup=4.0)
    _settle(env)
    _assert_loss(env, 36.0, 28.0, topup=8.0)


def test_a_partial_gain_does_not_offset_the_remaining_loser_or_another_leg(env):
    _book(env, fill("buy", 20, 0.5, YESTERDAY), fill("buy", 20, 0.9), fill("sell", 20, 1.0))
    _snapshot(env)
    _settle(env)
    _assert_loss(env, 18.0, 14.0, gain=6.0, topup=4.0)
    _book(env, fill("buy", 30, 0.5, market="b", token="b-token"),
          fill("sell", 30, 0.0, market="b", token="b-token"))
    state = _assert_loss(env, 33.0, 29.0, gain=6.0, topup=4.0)
    assert "max_daily_loss" in paper_ledger.evaluate(_intent(), load_risk(), state).reasons
    result = m2_runner.preflight(m2_runner.RunnerPaths.under(env["run"]), now=NOW_DT)
    assert result.action == m2_runner.ACT_PAUSE
    assert result.reasons == ["daily_loss_breaker", "daily_loss_usd=33.0 cap_usd=30"]


def test_a_cost_gain_below_the_start_reference_still_retains_that_reference_loss(env):
    _book(env, fill("buy", 20, 0.5, YESTERDAY), fill("buy", 20, 0.9), fill("sell", 20, 0.8))
    _snapshot(env)
    _assert_loss(env, 20.0, 0.0, gain=2.0, unrealized=18.0, topup=2.0)
    _settle(env)
    _assert_loss(env, 20.0, 14.0, gain=2.0, topup=6.0)


def test_a_confirmed_winner_keeps_the_conservative_valuation_and_offsets_nothing(env):
    """Owner 2026-10-13: a settled winner must not credit back today's conservative (no-book) valuation."""
    _book(env, fill("buy", 20, 0.5, YESTERDAY), fill("buy", 20, 0.9))
    _snapshot(env)
    _assert_loss(env, 36.0, 0.0, unrealized=36.0)
    seen = []

    def settle():
        seen.append("settle")
        return _settle(env, 1.0)

    result = m2_runner.preflight(m2_runner.RunnerPaths.under(env["run"]), now=NOW_DT, settle=settle)
    assert seen == ["settle"]
    # The kept 36 is above the 30 cap: the breaker still pauses after the winner settles (before C217 it ran).
    assert result.action == m2_runner.ACT_PAUSE
    assert result.reasons[0] == "daily_loss_breaker"
    # The winner's gain is booked but the day-start reference loss (36) stays: nothing is credited back.
    _assert_loss(env, 36.0, 0.0, gain=12.0, topup=36.0)
    # Another leg's cost loss adds on top; the winner's gain never offsets it.
    _book(env, fill("buy", 30, 0.5, market="b", token="b-token"),
          fill("sell", 30, 0.0, market="b", token="b-token"))
    _assert_loss(env, 51.0, 15.0, gain=12.0, topup=36.0)


def test_a_confirmed_winner_still_counts_toward_the_runner_breaker(env, monkeypatch):
    _book(env, fill("buy", 20, 0.5, YESTERDAY), fill("buy", 20, 0.9))
    _snapshot(env)
    _settle(env, 1.0)
    risk = paper_ledger.daily_risk_loss(*_rows_ids(env), NOW)
    assert risk["problems"] == []
    assert risk["marks"]["realized_topup_usd"] == pytest.approx(36.0)
    assert risk["loss_usd"] >= 36.0


def _rows_ids(env):
    read = ledger_store.read_ledger(env["paper"])[1]
    return read.rows, list(read.ids)


@pytest.mark.parametrize("fallback", ["late", "invalid", "zero", "missing"])
def test_unusable_start_marks_fall_back_to_original_cost(env, fallback):
    _book(env, fill("buy", 20, 0.5, YESTERDAY), fill("buy", 20, 0.9))
    if fallback != "missing":
        _snapshot(env, mark=0.0 if fallback == "zero" else 18.0,
                  late=fallback == "late", invalid=fallback == "invalid")
    _settle(env)
    _assert_loss(env, 28.0, 28.0)


@pytest.mark.parametrize("damage", ["corrupt", "quantity"])
def test_closed_legs_still_require_valid_matching_start_snapshots(env, damage):
    _book(env, fill("buy", 20, 0.5, YESTERDAY))
    _snapshot(env, shares=19.0 if damage == "quantity" else 20.0)
    if damage == "corrupt":
        (env["marks"] / f"{TODAY}.json").write_text("{", encoding="utf-8")
    _settle(env)
    state = paper_ledger.load_state("x", "x-token")
    assert state["daily_loss_usd"] is None and state["ledger_problems"]
    result = m2_runner.preflight(m2_runner.RunnerPaths.under(env["run"]), now=NOW_DT)
    assert result.action == m2_runner.ACT_PAUSE and result.reasons[0] == "invalid_risk_state"


def test_two_partial_closes_use_remaining_carried_shares_and_keep_fees(env):
    _book(env, fill("buy", 20, 0.5, YESTERDAY), fill("sell", 10, 0.2, rate=0.02))
    _snapshot(env)
    _settle(env)
    state = _assert_loss(env, 16.0, 8.0, topup=8.0)
    assert state["daily_fees_usd"] > 0


def test_reversing_a_leg_assigns_the_new_short_no_old_day_start_reference(env):
    _book(env, fill("buy", 20, 0.5, YESTERDAY), fill("buy", 20, 0.9), fill("sell", 60, 0.0))
    _snapshot(env)
    _assert_loss(env, 56.0, 28.0, unrealized=20.0, topup=8.0)
    _book(env, fill("buy", 20, 1.0))
    _assert_loss(env, 56.0, 48.0, topup=8.0)


def test_a_short_keeps_signed_cost_proceeds_and_day_start_reference(env):
    _book(env, fill("sell", 20, 0.5, YESTERDAY))
    _snapshot(env, mark=-8.0, shares=-20.0)
    _settle(env, 1.0)
    _assert_loss(env, 12.0, 10.0, topup=2.0)


def test_load_state_uses_one_captured_utc_day_for_every_loss_part(env, monkeypatch):
    _book(env, fill("buy", 20, 0.5, YESTERDAY), fill("sell", 20, 0.0))
    _snapshot(env)
    calls = []

    def crossing_clock():
        calls.append(True)
        return NOW if len(calls) == 1 else "2026-09-02T00:00:01Z"

    monkeypatch.setattr(paper_ledger, "utc_now", crossing_clock)
    _assert_loss(env, 18.0, 10.0, topup=8.0)
    # _unsettled_closed_legs has no snapshots here and needs no second timestamp.
    assert len(calls) == 1


def test_runner_uses_the_same_closed_and_unrealized_loss_without_a_settlement_callback(env):
    _book(env, fill("buy", 20, 0.5, YESTERDAY), fill("buy", 20, 0.9), fill("sell", 20, 0.0))
    _snapshot(env)
    state = _assert_loss(env, 36.0, 14.0, unrealized=18.0, topup=4.0)
    assert "max_daily_loss" in paper_ledger.evaluate(_intent(), load_risk(), state).reasons
    result = m2_runner.preflight(m2_runner.RunnerPaths.under(env["run"]), now=NOW_DT)
    assert result.action == m2_runner.ACT_PAUSE
    assert result.reasons == ["daily_loss_breaker", "daily_loss_usd=36.0 cap_usd=30"]


@pytest.mark.parametrize("consumer", ["risk_gate", "runner"])
def test_a_closed_reference_exception_fails_closed_in_both_consumers(env, monkeypatch, consumer):
    _book(env, fill("buy", 20, 0.5, YESTERDAY))
    _snapshot(env)
    _settle(env)

    def broken_reference(*args):
        raise ZeroDivisionError

    monkeypatch.setattr(leg_marks, "closed_loss_topup", broken_reference)
    if consumer == "risk_gate":
        state = paper_ledger.load_state("x", "x-token")
        assert state["daily_loss_usd"] is None
        assert state["ledger_problems"] == ["leg_marks_error:ZeroDivisionError"]
        assert "invalid_risk_state" in paper_ledger.evaluate(_intent(), load_risk(), state).reasons
    else:
        result = m2_runner.preflight(m2_runner.RunnerPaths.under(env["run"]), now=NOW_DT)
        assert result.action == m2_runner.ACT_PAUSE
        assert result.reasons == ["invalid_risk_state", "leg_marks_error:ZeroDivisionError"]
