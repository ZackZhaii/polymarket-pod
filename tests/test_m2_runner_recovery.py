"""Issue #120 (Task CJ, M2-C2): restart / suspend safety for the M2 paper runner.

Crash injection (built → commit → committed), unique_decision_id booking, suspend/resume detection,
clock skew, book_before_resume / stale_book, and recover(). Offline: injected clocks, FakeGetters.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import src.ledger_store as ledger_store
from src import book, intent_builder, m2_runner
from src.calibration_ids import make_decision_id
from src.thresholds import CLOCK_SKEW_MAX_S, MAX_BOOK_AGE_S
from tests import m2_runner_rig as rig


@pytest.fixture
def env(tmp_path, monkeypatch):
    paths = rig.isolate(monkeypatch, tmp_path)
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(m2_runner.forecaster, "utc_now", lambda: rig.stamp(now))
    return {**paths, "now": now, "tmp": tmp_path}


def _journal(env) -> list[dict]:
    return rig.read_jsonl(env["run"] / "journal.jsonl")


def _events(env) -> list[dict]:
    path = env["run"] / "events.jsonl"
    return rig.read_jsonl(path) if path.exists() else []


class _Clock:
    def __init__(self, start: datetime):
        self.wall = start
        self.mono = 1000.0

    def now(self) -> datetime:
        return self.wall

    def monotonic(self) -> float:
        return self.mono

    def advance_wall(self, seconds: float) -> None:
        self.wall = self.wall + timedelta(seconds=seconds)


# ---------------------------------------------------------------- unique_decision_id


def test_unique_decision_id_returns_already_booked_without_a_second_row(env):
    decision_id = make_decision_id("news-forecaster", "m", "t", "fc_x")
    row = {
        "fill_id": "fill-a", "ts": "2026-10-11T12:00:00Z", "strategy": "news-forecaster", "market": "m",
        "token_id": "t", "side": "buy", "shares": 10.0, "notional_usd": 5.0, "fee_usd": 0.0, "fee_rate": 0.0,
        "market_price": 0.5, "effective_price": 0.5, "hypothetical": False, "counts_toward_risk": True,
        "risk_decision": "allow", "decision_id": decision_id, "forecast_id": "fc_x",
    }
    first = ledger_store.book(env["paper"], row, unique_decision_id=True)
    assert first.get("already_booked") is not True and first["fill_id"] == "fill-a"
    second = ledger_store.book(env["paper"], dict(row, fill_id="fill-b"), unique_decision_id=True)
    assert second == {"already_booked": True, "fill": first}
    _, read = ledger_store.read_ledger(env["paper"])
    assert len(read.rows) == 1 and ledger_store.verify(read) == []


def test_unique_decision_id_refuses_a_missing_decision_id(env):
    row = {
        "fill_id": "fill-a", "ts": "2026-10-11T12:00:00Z", "strategy": "s", "market": "m", "token_id": "t",
        "side": "buy", "shares": 1.0, "notional_usd": 0.5, "fee_usd": 0.0, "fee_rate": 0.0,
        "market_price": 0.5, "effective_price": 0.5, "hypothetical": False, "counts_toward_risk": True,
        "risk_decision": "allow",
    }
    with pytest.raises(ledger_store.LedgerIntegrityError) as caught:
        ledger_store.book(env["paper"], row, unique_decision_id=True)
    assert any("missing_decision_id" in p for p in caught.value.problems)


def test_unique_decision_id_false_keeps_legacy_booking(env):
    row = {
        "ts": "2026-10-11T12:00:00Z", "strategy": "s", "market": "m", "token_id": "t", "side": "buy",
        "shares": 1.0, "notional_usd": 0.5, "fee_usd": 0.0, "fee_rate": 0.0, "market_price": 0.5,
        "effective_price": 0.5, "hypothetical": False, "counts_toward_risk": True, "risk_decision": "allow",
    }
    a = ledger_store.book(env["paper"], dict(row, fill_id="a"))
    b = ledger_store.book(env["paper"], dict(row, fill_id="b"))
    assert a["fill_id"] != b["fill_id"]
    _, read = ledger_store.read_ledger(env["paper"])
    assert len(read.rows) == 2


# ---------------------------------------------------------------- book.fetch_depth + book_snapshot_from_clob


def test_fetch_depth_returns_full_book_and_normalised_book_ts(env):
    now = env["now"]
    millis = int(now.timestamp() * 1000)

    def getter(url: str):
        assert "token_id=81001" in url
        return {"asset_id": "81001", "timestamp": str(millis), "tick_size": "0.01", "min_order_size": "5",
                "asks": [{"price": "0.55", "size": "100"}, {"price": "0.56", "size": "50"}],
                "bids": [{"price": "0.54", "size": "80"}, {"price": "0.53", "size": "40"}]}

    raw = book.fetch_depth("81001", getter=getter)
    assert raw is not None and raw["book_ts"].endswith("Z") and raw["tick_size"] == "0.01"
    snap = intent_builder.book_snapshot_from_clob(raw, "m", "81001")
    assert len(snap.asks) == 2 and len(snap.bids) == 2
    assert snap.asks[0][0] == 0.55 and snap.bids[0][0] == 0.54
    assert snap.book_ts == raw["book_ts"]


def test_book_snapshot_from_clob_rejects_another_asset():
    snap = intent_builder.book_snapshot_from_clob(
        {"asset_id": "other", "asks": [{"price": "0.5", "size": "1"}], "bids": [{"price": "0.4", "size": "1"}]},
        "m", "81001")
    assert snap.token_id == "" and snap.asks == () and snap.bids == ()


def test_m2_runner_reexports_book_snapshot_from_clob():
    raw = {"asks": [{"price": "0.5", "size": "1"}], "bids": [{"price": "0.4", "size": "1"}],
           "timestamp": "2026-10-11T12:00:00.000Z"}
    assert m2_runner.book_snapshot_from_clob(raw, "m", "t") == intent_builder.book_snapshot_from_clob(raw, "m", "t")


# ---------------------------------------------------------------- recover / crash


def test_recover_patches_built_when_ledger_already_has_the_decision(env):
    fake = rig.FakeGetters(env["now"])
    rig.deliver(env["run"], env["now"])
    assert rig.run_once(env["run"], fake, clock=lambda: env["now"]) == m2_runner.EXIT_OK
    fills = rig.read_jsonl(env["paper"])
    assert len(fills) == 1
    decision_id = fills[0]["decision_id"]
    built = next(r for r in _journal(env) if r["step"] == "built")
    env["run"].joinpath("journal.jsonl").write_text(json.dumps(built) + "\n", encoding="utf-8")
    state = m2_runner.recover(m2_runner.RunnerPaths.under(env["run"]), env["paper"], now=env["now"])
    patched = _journal(env)[-1]
    assert patched["step"] == "committed" and patched["decision_id"] == decision_id and patched.get("recovered")
    assert built.get("idempotency_key") in state.seen_keys
    _, read = ledger_store.read_ledger(env["paper"])
    assert len(read.rows) == 1 and ledger_store.verify(read) == []


def test_recover_leaves_built_alone_when_ledger_has_no_fill(env):
    paths = m2_runner.RunnerPaths.under(env["run"])
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    decision_id = make_decision_id("news-forecaster", "m2-alpha", rig.TOKENS[0], "fc_orphan")
    built = {"ts": "2026-10-11T12:00:00Z", "step": "built", "forecast_id": "fc_orphan",
             "intent_id": "in_x", "decision_id": decision_id, "idempotency_key": "ik_orphan"}
    paths.journal.write_text(json.dumps(built) + "\n", encoding="utf-8")
    state = m2_runner.recover(paths, env["paper"], now=env["now"])
    assert [r["step"] for r in _journal(env)] == ["built"]
    assert "ik_orphan" in state.seen_keys
    assert ledger_store.read_ledger(env["paper"])[1].rows == []


def test_crash_after_built_then_rerun_books_the_decision_only_once(env):
    fake = rig.FakeGetters(env["now"])
    rig.deliver(env["run"], env["now"])
    assert rig.run_once(env["run"], fake, clock=lambda: env["now"]) == m2_runner.EXIT_OK
    fills = rig.read_jsonl(env["paper"])
    decision_id = fills[0]["decision_id"]
    built = next(r for r in _journal(env) if r["step"] == "built")
    env["run"].joinpath("journal.jsonl").write_text(json.dumps(built) + "\n", encoding="utf-8")
    state = m2_runner.recover(m2_runner.RunnerPaths.under(env["run"]), env["paper"], now=env["now"])
    assert any(r.get("recovered") for r in _journal(env) if r["step"] == "committed")
    again = ledger_store.book(env["paper"], dict(fills[0], fill_id="retry-fill"), unique_decision_id=True)
    assert again["already_booked"] is True and again["fill"]["decision_id"] == decision_id
    _, read = ledger_store.read_ledger(env["paper"])
    assert len([r for r in read.rows if r.get("decision_id") == decision_id]) == 1
    assert ledger_store.verify(read) == []
    assert built["idempotency_key"] in state.seen_keys


# ---------------------------------------------------------------- suspend / resume / skew / stale


def test_wall_gap_triggers_resume_detected_and_skips_orders(env):
    clock = _Clock(env["now"])
    fake = rig.FakeGetters(env["now"])
    rig.deliver(env["run"], env["now"])
    paths = m2_runner.RunnerPaths.under(env["run"])
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    state = m2_runner.RunnerState(paths=paths, clock=clock.now, monotonic=clock.monotonic)
    report1 = m2_runner.iteration(state, now=clock.now(), getters=fake.getters())
    assert report1.action == m2_runner.ACT_RUN and state.last_iter_wall is not None
    clock.advance_wall(3600)
    fake.now = clock.now()  # the independent server clock advances through the simulated sleep too
    before_books = len(fake.book_urls)
    report2 = m2_runner.iteration(state, now=clock.now(), getters=fake.getters())
    assert "resume_detected" in report2.reasons
    assert report2.allows == 0 and report2.built == 0
    assert state.resume_count == 1 and state.last_resume_at is not None
    assert any(e.get("event") == "resume_detected" for e in _events(env))
    assert len(fake.book_urls) == before_books


def test_book_fetched_before_resume_is_refused(env):
    clock = _Clock(env["now"])
    fake = rig.FakeGetters(env["now"])
    rig.deliver(env["run"], env["now"])
    paths = m2_runner.RunnerPaths.under(env["run"])
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    state = m2_runner.RunnerState(paths=paths, clock=clock.now, monotonic=clock.monotonic)
    state.last_resume_at = clock.now() + timedelta(seconds=60)
    before = len(rig.read_jsonl(env["paper"]))
    report = m2_runner.iteration(state, now=clock.now(), getters=fake.getters())
    assert report.allows == 0
    assert len(rig.read_jsonl(env["paper"])) == before
    assert any(r.get("step") == "skipped" and "book_before_resume" in (r.get("reasons") or [])
               for r in _journal(env)), _journal(env)


def test_clock_skew_pauses_the_runner(env):
    clock = _Clock(env["now"])
    skewed = env["now"] - timedelta(seconds=CLOCK_SKEW_MAX_S + 30)

    class Skewed(rig.FakeGetters):
        def book(self, url: str):
            if url == m2_runner.podlib.TIME_URL:
                self.time_calls.append(url)
                return skewed.timestamp()
            body = dict(super().book(url))
            body["timestamp"] = str(int(skewed.timestamp() * 1000))
            return body

    fake = Skewed(env["now"])
    rig.deliver(env["run"], env["now"])
    paths = m2_runner.RunnerPaths.under(env["run"])
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    state = m2_runner.RunnerState(paths=paths, clock=clock.now, monotonic=clock.monotonic)
    report = m2_runner.iteration(state, now=clock.now(), getters=fake.getters())
    assert report.action == m2_runner.ACT_PAUSE and report.paused_reason == "clock_skew"
    assert any(e.get("event") == "paused" and e.get("reasons") == ["clock_skew"] for e in _events(env))
    assert fake.book_urls == []


# ---------------------------------------------------------------- issue #238: clock_skew keeps paused_since


class _Skewing(rig.FakeGetters):
    """#222 option B: the independent CLOB /time is ``skew_s`` behind the runner clock (``None`` = in sync);
    ``time_error`` makes /time fail (clock_unverified). Books are always fresh."""

    def __init__(self, now: datetime, clock: _Clock) -> None:
        super().__init__(now)
        self.clock = clock
        self.skew_s: float | None = CLOCK_SKEW_MAX_S + 30
        self.time_error: Exception | None = None

    def book(self, url: str):
        if url == m2_runner.podlib.TIME_URL:
            self.time_calls.append(url)
            if self.time_error is not None:
                raise self.time_error
            return self.clock.now().timestamp() - (self.skew_s or 0.0)
        body = dict(super().book(url))
        moment = self.clock.now()
        body["timestamp"] = str(int(moment.timestamp() * 1000))
        return body


def _skew_rig(env) -> tuple[_Clock, _Skewing, m2_runner.RunnerState]:
    clock = _Clock(env["now"])
    fake = _Skewing(env["now"], clock)
    rig.deliver(env["run"], env["now"])
    paths = m2_runner.RunnerPaths.under(env["run"])
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    return clock, fake, m2_runner.RunnerState(paths=paths, clock=clock.now, monotonic=clock.monotonic)


def _tick(state: m2_runner.RunnerState, clock: _Clock, fake: _Skewing, seconds: float = 0.0):
    """One iteration ``seconds`` later on both clocks (no suspend: wall and monotonic move together)."""
    clock.advance_wall(seconds)
    clock.mono += seconds
    return m2_runner.iteration(state, now=clock.now(), getters=fake.getters())


def test_clock_skew_keeps_paused_since_while_the_skew_holds(env):
    clock, fake, state = _skew_rig(env)
    report = _tick(state, clock, fake)
    assert report.action == m2_runner.ACT_PAUSE and report.paused_reason == "clock_skew"
    first = state.paused_since
    assert first is not None
    for _ in range(4):
        report = _tick(state, clock, fake, 60.0)
        assert report.action == m2_runner.ACT_PAUSE and report.paused_reason == "clock_skew"
        assert state.paused_since == first and state.paused_reason == "clock_skew"
        assert m2_runner.progress(state, clock.now())["paused_since"] == first
    # one "paused" event per pause (not one per tick)
    assert sum(1 for e in _events(env) if e.get("event") == "paused" and e.get("reasons") == ["clock_skew"]) == 1


def test_clock_unverified_keeps_paused_since_while_the_clock_stays_unverifiable(env):
    """#238 rule (PR #250) applied to #222 option B's clock_unverified pause."""
    clock, fake, state = _skew_rig(env)
    fake.time_error = RuntimeError("time down")
    report = _tick(state, clock, fake)
    assert report.action == m2_runner.ACT_PAUSE and report.paused_reason == "clock_unverified"
    first = state.paused_since
    assert first is not None
    for _ in range(4):
        report = _tick(state, clock, fake, 60.0)
        assert report.paused_reason == "clock_unverified" and state.paused_since == first
    assert sum(1 for e in _events(env) if e.get("event") == "paused"
               and e.get("reasons") == ["clock_unverified"]) == 1


def test_a_clock_skew_pause_held_past_the_alert_window_alerts_the_sentinel(env):
    import src.heartbeat as heartbeat
    import src.sentinel_tick as sentinel_tick
    from src.thresholds import RUNNER_PAUSED_ALERT_S, RUNNER_SUSPEND_GAP_S

    clock, fake, state = _skew_rig(env)
    _tick(state, clock, fake)
    first = state.paused_since
    step = RUNNER_SUSPEND_GAP_S - 10  # below the suspend gap, so no tick is a resume
    held = 0.0
    while held <= RUNNER_PAUSED_ALERT_S + 60:
        report = _tick(state, clock, fake, step)
        held += step
        assert report.action == m2_runner.ACT_PAUSE and report.paused_reason == "clock_skew", report
    assert state.paused_since == first
    now = clock.now()
    hb = heartbeat.Heartbeat(m2_runner.PROC, every_s=30.0, now=lambda: now)
    hb.beat(heartbeat.STATE_PAUSED, m2_runner.progress(state, now), force=True)
    result = sentinel_tick.tick(now=now, bus_path=env["tmp"] / "sentinel-bus.jsonl",
                                ledger_path=env["tmp"] / "sentinel-paper.jsonl")
    assert "runner_paused_too_long" in result["alerts"]


def test_clock_skew_paused_since_clears_on_a_sane_book_and_restamps_on_a_new_pause_or_reason(env):
    import src.halt_store as halt_store

    clock, fake, state = _skew_rig(env)
    _tick(state, clock, fake)
    first = state.paused_since
    assert first is not None
    # a sane book: the decide runs, the clock_skew pause clears
    fake.skew_s = None
    report = _tick(state, clock, fake, 60.0)
    assert report.action == m2_runner.ACT_RUN
    assert state.paused_reason is None and state.paused_since is None
    # a fresh skew pause gets a fresh stamp
    rig.deliver(env["run"], clock.now(), name="0002.json", as_of=rig.stamp(clock.now() - timedelta(minutes=5)))
    fake.skew_s = CLOCK_SKEW_MAX_S + 30
    report = _tick(state, clock, fake, 60.0)
    assert report.action == m2_runner.ACT_PAUSE and state.paused_reason == "clock_skew"
    second = state.paused_since
    assert second is not None and second != first
    # a different pause reason (preflight global halt) restamps paused_since
    halt_store.record_halt(scope=halt_store.SCOPE_GLOBAL, reason="test_global", source="Sentinel")
    report = _tick(state, clock, fake, 60.0)
    assert report.action == m2_runner.ACT_PAUSE and state.paused_reason == "global_halt"
    assert state.paused_since is not None and state.paused_since not in (first, second)


def test_stale_book_is_skipped(env):
    clock = _Clock(env["now"])
    old = env["now"] - timedelta(seconds=MAX_BOOK_AGE_S + 10)

    class Stale(rig.FakeGetters):
        def book(self, url: str):
            if url == m2_runner.podlib.TIME_URL:
                return super().book(url)
            body = dict(super().book(url))
            body["timestamp"] = str(int(old.timestamp() * 1000))
            return body

    fake = Stale(env["now"])
    rig.deliver(env["run"], env["now"])
    paths = m2_runner.RunnerPaths.under(env["run"])
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    state = m2_runner.RunnerState(paths=paths, clock=clock.now, monotonic=clock.monotonic)
    report = m2_runner.iteration(state, now=clock.now(), getters=fake.getters())
    assert report.allows == 0
    assert any(r.get("step") == "skipped" and "stale_book" in (r.get("reasons") or []) for r in _journal(env))


def test_progress_reports_resume_count(env):
    paths = m2_runner.RunnerPaths.under(env["run"])
    state = m2_runner.RunnerState(paths=paths, resume_count=3)
    assert m2_runner.progress(state, env["now"])["resume_count"] == 3


def test_alternating_clock_reasons_keep_paused_since_and_alert_the_sentinel(env):
    """PR #253 review (Poly哨兵 a): clock_unverified <-> clock_skew is one clock pause episode; switching between
    the two must not restamp paused_since, so a long flapping clock pause still reaches the 6 h alert."""
    import src.heartbeat as heartbeat
    import src.sentinel_tick as sentinel_tick
    from src.thresholds import RUNNER_PAUSED_ALERT_S, RUNNER_SUSPEND_GAP_S

    clock, fake, state = _skew_rig(env)
    report = _tick(state, clock, fake)
    assert report.paused_reason == "clock_skew"
    first = state.paused_since
    assert first is not None
    step = RUNNER_SUSPEND_GAP_S - 10
    held, seen, flip = 0.0, set(), False
    while held <= RUNNER_PAUSED_ALERT_S + 60:
        flip = not flip
        fake.time_error = RuntimeError("time down") if flip else None
        report = _tick(state, clock, fake, step)
        held += step
        assert report.action == m2_runner.ACT_PAUSE
        assert report.paused_reason == ("clock_unverified" if flip else "clock_skew")
        seen.add(report.paused_reason)
        assert state.paused_since == first
    assert seen == {"clock_unverified", "clock_skew"}
    now = clock.now()
    hb = heartbeat.Heartbeat(m2_runner.PROC, every_s=30.0, now=lambda: now)
    hb.beat(heartbeat.STATE_PAUSED, m2_runner.progress(state, now), force=True)
    result = sentinel_tick.tick(now=now, bus_path=env["tmp"] / "sentinel-bus.jsonl",
                                ledger_path=env["tmp"] / "sentinel-paper.jsonl")
    assert "runner_paused_too_long" in result["alerts"]


def test_clock_pause_clears_on_recovery_and_a_non_clock_reason_restamps(env):
    import src.halt_store as halt_store

    clock, fake, state = _skew_rig(env)
    _tick(state, clock, fake)
    first = state.paused_since
    fake.time_error = RuntimeError("time down")
    assert _tick(state, clock, fake, 60.0).paused_reason == "clock_unverified" and state.paused_since == first
    halt_store.record_halt(scope=halt_store.SCOPE_GLOBAL, reason="test_global", source="Sentinel")
    assert _tick(state, clock, fake, 60.0).paused_reason == "global_halt"
    assert state.paused_since not in (None, first)


# ---------------------------------------------------------------- Task DA (#225 follow-up): clock -> day start


def test_da_clock_skew_tick_then_next_preflight_evaluates_with_clock_not_ok(env, monkeypatch):
    """E2E: a clock_skew tick; the next tick's daily-loss evaluation gets clock_ok=False (day start late),
    after a clean decide it is True again (merged with #253's /time pause)."""
    from src import paper_ledger

    seen: list[bool] = []
    real = paper_ledger.daily_risk_loss

    def spy(rows, ids, now, *, clock_ok=True):
        seen.append(clock_ok)
        return real(rows, ids, now, clock_ok=clock_ok)

    monkeypatch.setattr(paper_ledger, "daily_risk_loss", spy)
    clock, fake, state = _skew_rig(env)
    report = _tick(state, clock, fake)
    assert report.paused_reason == "clock_skew" and state.clock_anomaly == "clock_skew"
    assert seen == []  # #253: the /time clock pause returns after settlement, before the daily-loss evaluation
    fake.skew_s = None
    _tick(state, clock, fake, 60.0)  # clock certified again: daily loss runs with the earlier anomaly (late)
    assert seen[0] is False  # the preflight call (a later commit_if_allowed call has its own evaluation)
    assert state.clock_anomaly is None  # that tick's clean decide clears the anomaly
    _tick(state, clock, fake, 60.0)
    assert seen[-1] is True


def test_da_clock_unverified_also_marks_the_day_start_late(env, monkeypatch):
    """#253 + Task DA: an unverifiable /time sets clock_anomaly too, so the next evaluated tick is clock_ok=False."""
    from src import paper_ledger

    seen: list[bool] = []
    real = paper_ledger.daily_risk_loss

    def spy(rows, ids, now, *, clock_ok=True):
        seen.append(clock_ok)
        return real(rows, ids, now, clock_ok=clock_ok)

    monkeypatch.setattr(paper_ledger, "daily_risk_loss", spy)
    clock, fake, state = _skew_rig(env)
    fake.skew_s = None
    fake.time_error = RuntimeError("time down")
    report = _tick(state, clock, fake)
    assert report.paused_reason == "clock_unverified" and state.clock_anomaly == "clock_unverified"
    assert seen == []
    fake.time_error = None
    _tick(state, clock, fake, 60.0)
    assert seen[0] is False and state.clock_anomaly is None
    _tick(state, clock, fake, 60.0)
    assert seen[-1] is True
