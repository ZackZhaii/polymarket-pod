"""Issue #255 (#197 follow-up, Poly风控 review of d6dfd3a): the write-off's start-of-day uplift.

1) On a UTC day's first tick no start-of-day snapshot exists yet: apply_writeoffs writes the day's snapshot first
   (the same code path as #124's daily_unrealized) instead of booking the uplift as 0.
2) load_state recomputes every write-off-today row's uplift from the day's snapshot and fails closed
   (invalid_risk_state) on a mismatch, or when the snapshot was written after the write-off.
"""
from __future__ import annotations

import pytest

from src import daily_marks, ledger_store, market_close, paper_ledger
from tests.test_writeoff import _book, _mark_books, _rows, _top, close, env, fill  # noqa: F401

FIRST_TICK = "2026-09-01T00:00:10Z"


def _first_tick(env, monkeypatch, bid=0.8, on_time=True):
    monkeypatch.setattr(paper_ledger, "_LAST_EVAL_DAY", "2026-08-31" if on_time else None)
    env["clock"]["now"] = FIRST_TICK
    _mark_books(env, _top(bid, "2026-09-01T00:00:05Z"))


def test_the_days_first_tick_writes_the_snapshot_before_the_writeoff_and_books_the_uplift(env, monkeypatch):
    _book(env["ledger"], [fill("buy", 20.0, 0.5, market="m", token="t")])  # cost 10, held since yesterday
    _first_tick(env, monkeypatch)
    assert daily_marks.read_snapshot(paper_ledger.DAILY_MARKS_DIR, "2026-09-01") == (None, [])
    out = paper_ledger.apply_writeoffs([close("m")], FIRST_TICK, clock_ok=True)
    assert out["problems"] == []
    row = out["booked"][0]
    assert row["writeoff_sod_uplift_usd"] == pytest.approx(6.0) and row["writeoff_usd"] == pytest.approx(16.0)
    snap, problems = daily_marks.read_snapshot(paper_ledger.DAILY_MARKS_DIR, "2026-09-01")
    assert problems == [] and snap is not None and snap["late"] is False
    state = paper_ledger.load_state("x", "t")  # the same snapshot: consistent, the day loss is the write-off
    assert state["ledger_problems"] == [] and state["daily_loss_usd"] == pytest.approx(16.0)


def test_an_unwritable_snapshot_on_the_first_tick_books_nothing(env, monkeypatch):
    _book(env["ledger"], [fill("buy", 20.0, 0.5, market="m", token="t")])
    _first_tick(env, monkeypatch)

    def boom(*args, **kwargs):
        raise OSError("disk")

    monkeypatch.setattr(daily_marks, "create_snapshot", boom)
    out = paper_ledger.apply_writeoffs([close("m")], FIRST_TICK, clock_ok=True)
    assert out["booked"] == [] and out["problems"] and out["problems"][0].startswith("writeoff_daily_marks_invalid")
    assert not any(ledger_store.is_writeoff(r) for r in _rows(env["ledger"]))


def _book_writeoff(env, uplift):
    row = market_close.plan_writeoffs(_rows(env["ledger"]), ["ck-0000"], [close("m")], env["clock"]["now"]).rows[0]
    snap, _ = daily_marks.read_snapshot(paper_ledger.DAILY_MARKS_DIR, env["clock"]["now"][:10])
    late = True if snap is None else snap.get("late") is True  # #197 option A: the snapshot's lateness
    row.update(writeoff_sod_uplift_usd=uplift, writeoff_usd=row["writeoff_cost_usd"] + uplift, writeoff_sod_late=late)
    ledger_store.book(env["ledger"], row)


def test_load_state_recomputes_the_uplift_from_the_snapshot_and_fails_closed_on_a_mismatch(env, monkeypatch):
    _book(env["ledger"], [fill("buy", 20.0, 0.5, market="m", token="t")])
    _first_tick(env, monkeypatch)
    assert paper_ledger.load_state("x", "t", clock_ok=True)["daily_marks_late"] is False  # snapshot: carried mark 16
    _book_writeoff(env, 0.0)  # self-consistent (cost 10 + 0) but the snapshot says uplift 6
    state = paper_ledger.load_state("x", "t")
    assert state["daily_loss_usd"] is None
    assert any(p.startswith("writeoff_sod_uplift_mismatch") for p in state["ledger_problems"]), state["ledger_problems"]


def test_load_state_accepts_the_uplift_the_snapshot_gives(env, monkeypatch):
    _book(env["ledger"], [fill("buy", 20.0, 0.5, market="m", token="t")])
    _first_tick(env, monkeypatch)
    paper_ledger.load_state("x", "t", clock_ok=True)  # verified clock (Task DC): on-time snapshot
    _book_writeoff(env, 6.0)
    state = paper_ledger.load_state("x", "t")
    assert state["ledger_problems"] == [] and state["daily_loss_usd"] == pytest.approx(16.0)


def test_a_writeoff_booked_before_the_days_snapshot_existed_is_invalid(env, monkeypatch):
    _book(env["ledger"], [fill("buy", 20.0, 0.5, market="m", token="t")])
    _first_tick(env, monkeypatch)
    _book_writeoff(env, 0.0)  # the #255 bug: uplift 0 with no snapshot yet; load_state then writes one
    env["clock"]["now"] = "2026-09-01T00:01:00Z"
    state = paper_ledger.load_state("x", "t")
    assert state["daily_loss_usd"] is None
    assert any(p.startswith(("writeoff_before_daily_marks", "writeoff_sod_uplift_mismatch"))
               for p in state["ledger_problems"]), state["ledger_problems"]
