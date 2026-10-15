"""#197 (Task CK, KEY) x clock: option A (家伟, 2026-10-14 22:25) and Poly风控's three tightenings.

Option A: a failed clock check still writes the closed leg off, at the late start-of-day reference.
1. The write-off row carries ``writeoff_sod_late`` (the snapshot's lateness) and #255's recompute
   (src/leg_marks.writeoff_uplift_problems) fails closed when it differs from the day's snapshot.
2. K1-K4: Poly执行's clock x write-off tests (/workspace/pod-bots/scratch/ck-clock-writeoff, merge checks
   poly-exec-ck-merge-check-1fc68b7 / -b415952), merged here unchanged except for the writeoff_sod_late asserts.
3. End to end through m2_runner.preflight: with the clock paused (clock_check fails), the settle step still writes
   the leg off, and the daily loss counts it.
"""
from __future__ import annotations

import inspect
from datetime import datetime, timezone

import pytest

from src import daily_marks, ledger_store, m2_runner, paper_ledger, podlib
from tests.test_writeoff import _book, _mark_books, _rows, _top, close, env, fill  # noqa: F401

ROLL = "2026-09-01T00:00:10Z"


class _S:
    def __init__(self, anomaly):
        self.clock_anomaly = anomaly


def _roll(env, bid=0.8):
    _book(env["ledger"], [fill("buy", 20.0, 0.5, market="m", token="t")])  # cost 10, held since yesterday
    env["clock"]["now"] = ROLL
    _mark_books(env, _top(bid, "2026-09-01T00:00:05Z"))


@pytest.mark.parametrize("anomaly", ["clock_unverified", "clock_skew", "clock_time_unreachable"])
def test_k1_any_clock_anomaly_makes_the_writeoff_snapshot_late(env, anomaly):
    _roll(env)
    ok = m2_runner.runner_clock_ok(_S(anomaly))
    assert ok is False
    out = paper_ledger.apply_writeoffs([close("m")], ROLL, clock_ok=ok)
    snap, problems = daily_marks.read_snapshot(paper_ledger.DAILY_MARKS_DIR, "2026-09-01")
    assert problems == [] and snap["late"] is True
    assert out["problems"] == []
    row = out["booked"][0]  # option A: booked, at the late reference (cost: no previous-day mark), never 00:00's
    assert row["writeoff_sod_uplift_usd"] == 0.0 and row["writeoff_usd"] == pytest.approx(10.0)
    assert row["writeoff_sod_late"] is True
    state = paper_ledger.load_state("x", "t")
    assert state["ledger_problems"] == [] and state["daily_loss_usd"] == pytest.approx(10.0)


def test_k2_verified_clock_on_time_roll_uses_the_start_mark(env):
    _roll(env)
    ok = m2_runner.runner_clock_ok(_S(None))
    out = paper_ledger.apply_writeoffs([close("m")], ROLL, clock_ok=ok)
    row = out["booked"][0]
    assert ok is True and row["writeoff_sod_uplift_usd"] == pytest.approx(6.0) and row["writeoff_sod_late"] is False


def test_k3_the_hook_passes_runner_clock_ok():
    # PR #342 (Codex 6184412258): this tick's clock result AND the runner's held state; behaviour is covered by
    # the cross-day runner test in tests/test_writeoff.py
    source = inspect.getsource(m2_runner._writeoff_hook)
    assert "clock is not None and clock() is None and runner_clock_ok(state)" in source
    assert "clock_ok=clock_ok" in source


@pytest.mark.parametrize("asks", [pytest.param([], id="ask_side_empty"),
                                  pytest.param([[1.0, 100.0]], id="ask_side_broken_price_1"),
                                  pytest.param([[0.9, float("nan")]], id="ask_side_broken_size_nan")])
def test_k4_one_sided_book_long_uses_the_bid_start_mark(env, asks):
    """#195 (Task CI, KEY, merged c8f796d): a long only needs the bid side -> start mark 16, uplift 6."""
    _book(env["ledger"], [fill("buy", 20.0, 0.5, market="m", token="t")])
    env["clock"]["now"] = ROLL
    _mark_books(env, {"ts": "2026-09-01T00:00:05Z", "book_ts": "2026-09-01T00:00:05Z", "market_id": "m",
                      "token_id": "t", "bids": [[0.8, 1000.0]], "asks": asks})
    out = paper_ledger.apply_writeoffs([close("m")], ROLL, clock_ok=True)
    assert out["problems"] == [], out["problems"]
    row = out["booked"][0]
    assert row["writeoff_sod_uplift_usd"] == pytest.approx(6.0) and row["writeoff_usd"] == pytest.approx(16.0)
    state = paper_ledger.load_state("x", "t")
    assert state["ledger_problems"] == [] and state["daily_loss_usd"] == pytest.approx(16.0)


# ---------------------------------------------------------------- tightening 1: writeoff_sod_late in #255's check

def _forged(env, late):
    out = paper_ledger.apply_writeoffs([close("m")], ROLL, clock_ok=False)  # late snapshot, the real row
    row = dict(out["booked"][0])
    assert row["writeoff_sod_late"] is True
    env["ledger"].unlink()
    _book(env["ledger"], [fill("buy", 20.0, 0.5, market="m", token="t")])
    if late is _MISSING:
        row.pop("writeoff_sod_late")
    else:
        row["writeoff_sod_late"] = late
    ledger_store.book(env["ledger"], row)


_MISSING = object()


@pytest.mark.parametrize("late", [False, _MISSING], ids=["flipped_to_on_time", "missing"])
def test_a_writeoff_sod_late_that_is_not_the_snapshots_fails_closed(env, late):
    _roll(env)
    _forged(env, late)
    state = paper_ledger.load_state("x", "t")
    assert state["daily_loss_usd"] is None
    assert any(p.startswith("writeoff_sod_late_mismatch") for p in state["ledger_problems"]), state["ledger_problems"]


@pytest.mark.parametrize("bad", [None, 0, 1, 0.0, "true", "false"])
def test_a_non_bool_writeoff_sod_late_is_an_invalid_row(env, bad):
    _roll(env)
    row = dict(paper_ledger.apply_writeoffs([close("m")], ROLL, clock_ok=False)["booked"][0])
    row["writeoff_sod_late"] = bad
    assert ledger_store.writeoff_field_problems(row, row["fill_id"]) == [f"invalid_writeoff:{row['fill_id']}"]


# ---------------------------------------------------------------- tightening 3: end to end through preflight

def test_e2e_clock_paused_preflight_still_writes_off_and_the_daily_loss_counts_it(env, tmp_path, monkeypatch):
    monkeypatch.setattr(podlib, "BUS_PATH", tmp_path / "bus.jsonl")  # never the real logs/bus.jsonl
    _roll(env)
    paths = m2_runner.RunnerPaths.under(tmp_path / "run")
    paths.run_dir.mkdir(parents=True)
    now = datetime(2026, 9, 1, 0, 0, 10, tzinfo=timezone.utc)
    state = _S("clock_unverified")  # the runner holds a clock anomaly: runner_clock_ok is False
    calls = []

    def settle():  # the runner's settle step: _settle_tick -> _writeoff_hook -> apply_writeoffs(clock_ok=...)
        calls.append("settle")
        out = paper_ledger.apply_writeoffs([close("m")], ROLL, clock_ok=m2_runner.runner_clock_ok(state))
        return out["problems"]

    def clock_check():
        calls.append("clock")
        return "clock_unverified"

    check = m2_runner.preflight(paths, now=now, clock_ok=m2_runner.runner_clock_ok(state), ledger_path=env["ledger"],
                                settle=settle, clock_check=clock_check)
    assert check.action == m2_runner.ACT_PAUSE and check.reasons == ["clock_unverified"]
    assert calls == ["settle", "clock"]  # the write-off ran before the clock pause, never deferred by it
    rows = [r for r in _rows(env["ledger"]) if ledger_store.is_writeoff(r)]
    assert len(rows) == 1 and rows[0]["writeoff_sod_late"] is True and rows[0]["writeoff_usd"] == pytest.approx(10.0)
    read = ledger_store.read_ledger(env["ledger"])[1]
    risk = paper_ledger.daily_risk_loss(read.rows, list(read.ids), ROLL, clock_ok=False)
    assert risk["problems"] == [] and risk["loss_usd"] == pytest.approx(10.0)  # the write-off is the day's loss
    state_view = paper_ledger.load_state("x", "t")
    assert state_view["ledger_problems"] == [] and state_view["daily_loss_usd"] == pytest.approx(10.0)
    assert state_view["daily_writeoff_usd"] == pytest.approx(10.0)
    # the next tick (clock still paused, no clock_check): the breaker reads the same ledger, no invalid_risk_state
    again = m2_runner.preflight(paths, now=now, clock_ok=False, ledger_path=env["ledger"])
    assert m2_runner.LEDGER_HALT_REASON not in again.reasons, again.reasons


def test_no_snapshot_omits_writeoff_sod_late_instead_of_writing_null(env):
    """PR #342 review item 1: the writer never produces null (no snapshot: the key is absent)."""
    from src import market_close
    _roll(env)
    read = ledger_store.read_ledger(env["ledger"])[1]
    row = market_close.plan_writeoffs(read.rows, list(read.ids), [close("m")], ROLL, None).rows[0]
    assert "writeoff_sod_late" not in row
    assert ledger_store.writeoff_field_problems(row, row["fill_id"]) == []


@pytest.mark.parametrize("raw", ["null", "0"])
def test_a_null_or_zero_writeoff_sod_late_fails_closed_on_a_later_day(env, raw):
    """PR #342 review item 1, cross-day: #255's recompute checks the write-off day only, so the row's own field
    check must reject an explicit null / 0 on every later day too (verify, load_state: invalid_risk_state)."""
    _roll(env)
    assert paper_ledger.apply_writeoffs([close("m")], ROLL, clock_ok=False)["problems"] == []
    text = env["ledger"].read_text(encoding="utf-8")
    assert text.count('"writeoff_sod_late":true') == 1
    env["ledger"].write_text(text.replace('"writeoff_sod_late":true', f'"writeoff_sod_late":{raw}'), encoding="utf-8")
    env["clock"]["now"] = "2026-09-03T12:00:00Z"  # two days later
    state = paper_ledger.load_state("x", "t")
    assert state["daily_loss_usd"] is None
    assert any(p.startswith("invalid_writeoff") for p in state["ledger_problems"]), state["ledger_problems"]


# ---------------------------------------------------------------- PR #342 review item 2: unhashable kind

@pytest.mark.parametrize("kind", [[], {}, ["writeoff"], {"k": 1}])
def test_an_unhashable_kind_is_invalid_and_never_raises(env, kind):
    row = {"fill_id": "bad-0001", "kind": kind, "hypothetical": False, "counts_toward_risk": True,
           "risk_decision": "writeoff"}
    problems = ledger_store.control_field_problems(row, "bad-0001")
    assert "invalid_kind:bad-0001" in problems
    _book(env["ledger"], [fill("buy", 20.0, 0.5, market="m", token="t")])
    import json as _json
    with env["ledger"].open("a", encoding="utf-8") as handle:  # a damaged line on disk
        handle.write(_json.dumps({**row, "ts": "2026-09-01T00:00:00Z", "market": "m", "token_id": "t"}) + "\n")
    state = paper_ledger.load_state("x", "t")  # must not raise TypeError
    assert state["daily_loss_usd"] is None and state["ledger_problems"]


# ---------------------------------------------------------------- PR #342 review 6184715521 (Codex 6184412258)

def _cross_day(env):
    """Cost 10 (20 @ 0.5) held since yesterday; yesterday's last valid mark 18 (bid 0.9); today's day-roll
    book bid 0.5 (start mark 10). Late reference: max(cost, 18) = 18 -> write-off 18; on time: 10 + 0 = 10."""
    _book(env["ledger"], [fill("buy", 20.0, 0.5, market="m", token="t")])
    _mark_books(env, _top(0.9, "2026-08-31T23:59:30Z"))
    env["clock"]["now"] = "2026-08-31T23:59:40Z"
    paper_ledger.load_state("x", "t", clock_ok=True)  # yesterday's evaluation records its last valid mark (18)
    _mark_books(env, _top(0.5, "2026-09-01T00:00:05Z"))
    env["clock"]["now"] = ROLL


def _hook_state(tmp_path, anomaly):
    state = m2_runner.RunnerState(paths=m2_runner.RunnerPaths.under(tmp_path / "run"))
    state.paths.run_dir.mkdir(parents=True, exist_ok=True)
    state.clock_anomaly = anomaly  # what the PREVIOUS tick left behind
    state.writeoff_events_resent = True
    return state


def _hook(state, monkeypatch, clock):
    from src import market_close
    monkeypatch.setattr(market_close, "detect", lambda *a, **k: market_close.Step(closes=[close("m")]))
    now = datetime(2026, 9, 1, 0, 0, 10, tzinfo=timezone.utc)
    return m2_runner._writeoff_hook(state, now, lambda url: [], clock)


def test_the_first_tick_of_a_day_uses_this_ticks_failed_clock_not_the_previous_ticks_ok(env, tmp_path, monkeypatch):
    """P1: the previous tick's clock was fine (no anomaly held), this tick's /time check fails. The write-off
    runs before preflight's clock step, so it must ask for this tick's result: late -> 18, never 10."""
    monkeypatch.setattr(podlib, "BUS_PATH", tmp_path / "bus.jsonl")
    _cross_day(env)
    state = _hook_state(tmp_path, None)
    assert m2_runner.runner_clock_ok(state) is True  # the stale state the old code used
    assert _hook(state, monkeypatch, lambda: "clock_unverified") == []
    row = [r for r in _rows(env["ledger"]) if ledger_store.is_writeoff(r)][0]
    assert row["writeoff_sod_late"] is True and row["writeoff_usd"] == pytest.approx(18.0)


def test_no_clock_step_means_not_verified(env, tmp_path, monkeypatch):
    monkeypatch.setattr(podlib, "BUS_PATH", tmp_path / "bus.jsonl")
    _cross_day(env)
    assert _hook(_hook_state(tmp_path, None), monkeypatch, None) == []
    row = [r for r in _rows(env["ledger"]) if ledger_store.is_writeoff(r)][0]
    assert row["writeoff_sod_late"] is True and row["writeoff_usd"] == pytest.approx(18.0)


def test_a_verified_clock_this_tick_books_the_on_time_reference(env, tmp_path, monkeypatch):
    monkeypatch.setattr(podlib, "BUS_PATH", tmp_path / "bus.jsonl")
    monkeypatch.setattr(paper_ledger, "_LAST_EVAL_DAY", "2026-08-31")
    _cross_day(env)
    assert _hook(_hook_state(tmp_path, None), monkeypatch, lambda: None) == []
    row = [r for r in _rows(env["ledger"]) if ledger_store.is_writeoff(r)][0]
    assert row["writeoff_sod_late"] is False and row["writeoff_usd"] == pytest.approx(10.0)


def test_the_tick_clock_is_checked_once_and_shared(tmp_path, monkeypatch):
    state = _hook_state(tmp_path, None)
    calls = []
    monkeypatch.setattr(m2_runner, "_clock_check", lambda s, g, n: calls.append(1) or "clock_skew")
    now = datetime(2026, 9, 1, tzinfo=timezone.utc)
    assert m2_runner._tick_clock(state, None, now) == "clock_skew" == m2_runner._tick_clock(state, None, now)
    assert calls == [1]


def test_a_failed_post_write_reread_rearms_the_event_resend(env, tmp_path, monkeypatch):
    """P2: the row is on disk but ledger_store.book's post-write re-read fails (LedgerIntegrityError): not in
    ``booked``, no event sent. The re-send flag is reset so the next settle step sends it."""
    from src.ledger_store import LedgerIntegrityError
    monkeypatch.setattr(podlib, "BUS_PATH", tmp_path / "bus.jsonl")
    _cross_day(env)
    real_book = ledger_store.book

    def write_then_fail(path, row, **kwargs):
        real_book(path, row, **kwargs)
        raise LedgerIntegrityError(["post_write_mismatch:x"])

    monkeypatch.setattr(ledger_store, "book", write_then_fail)
    state = _hook_state(tmp_path, None)
    problems = _hook(state, monkeypatch, lambda: None)
    monkeypatch.setattr(ledger_store, "book", real_book)
    assert problems and problems[0].startswith("writeoff_refused")
    assert [r for r in _rows(env["ledger"]) if ledger_store.is_writeoff(r)]  # on disk
    assert state.writeoff_events_resent is False
    assert m2_runner._resend_writeoff_events(state, datetime(2026, 9, 1, 0, 1, tzinfo=timezone.utc)) == []
    events = (state.paths.events).read_text(encoding="utf-8").splitlines()
    assert any('"resent": true' in line.replace('"resent":true', '"resent": true') for line in events)
