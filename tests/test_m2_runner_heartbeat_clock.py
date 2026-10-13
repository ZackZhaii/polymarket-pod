"""Issue #289 (Task DJ): the runner heartbeat ``progress`` exposes the clock state read-only.

``clock_anomaly`` / ``clock_anomaly_since`` / ``last_time_check`` are observation fields only: they never feed a
decision, a pause or a halt. Offline: injected clocks and fake getters, no network, no credentials, no orders.
"""
from __future__ import annotations

import copy
import dataclasses
import json
from datetime import datetime, timedelta, timezone

import pytest

from src import heartbeat, m2_runner, paper_ledger
from tests import m2_runner_rig as rig

TIME_URL = "https://clob.polymarket.com/time"
NEW_KEYS = {"clock_anomaly", "clock_anomaly_since", "last_time_check"}
OLD_KEYS = {"iteration", "last_scan_ts", "scan_age_s", "forecasts_sealed_total", "forecasts_rejected_total",
            "intents_built_total", "allows_total", "vetoes_total", "missing_fee_total", "scans_total",
            "scan_failures_total", "last_book_age_s_max", "resume_count", "paused_reason", "paused_since",
            "disk_free_gb", "ledger_rows", "pending_signals", "executor_enabled"}


def stamp(moment: datetime) -> str:
    return m2_runner._stamp(moment)


class TimeGetters(rig.FakeGetters):
    """/time answers ``now - offset`` (or raises); books are fresh at ``now``."""

    def __init__(self, now, *, offset=0.0, error=None):
        super().__init__(now)
        self.offset, self.error = offset, error

    def book(self, url):
        if url == TIME_URL:
            self.time_calls.append(url)
            if self.error is not None:
                raise self.error
            return self.now.timestamp() - self.offset
        self.book_urls.append(url)
        token = rig.parse_qs(rig.urlsplit(url).query)["token_id"][0]
        return rig.clob_book(token, self.now)


@pytest.fixture
def env(tmp_path, monkeypatch):
    paths = rig.isolate(monkeypatch, tmp_path)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    paths["run"].mkdir(parents=True)
    monkeypatch.setattr(m2_runner.forecaster, "utc_now", lambda: rig.stamp(now))
    state = m2_runner.RunnerState(paths=m2_runner.RunnerPaths.under(paths["run"]), clock=lambda: now,
                                  monotonic=lambda: now.timestamp())
    return {**paths, "start": now, "now": now, "state": state, "monkeypatch": monkeypatch}


def tick(env, *, at_s=0.0, offset=0.0, error=None, signal=False):
    """One iteration ``at_s`` seconds after the start, wall and monotonic together (never a suspend)."""
    now = env["start"] + timedelta(seconds=at_s)
    env["now"] = now
    env["state"].clock = lambda: now
    env["state"].monotonic = lambda: now.timestamp()
    env["monkeypatch"].setattr(m2_runner.forecaster, "utc_now", lambda: rig.stamp(now))
    if signal:
        rig.deliver(env["run"], now, f"{int(at_s):06d}.json")
    fake = TimeGetters(now, offset=offset, error=error)
    report = m2_runner.iteration(env["state"], now=now, getters=fake.getters())
    return report, m2_runner.progress(env["state"], now)


def test_recovered_state_starts_clock_unverified_with_since_at_startup(env):
    """Task DC: the runner starts fail closed; the starting heartbeat already shows it, stamped at startup."""
    paths = m2_runner.RunnerPaths.under(env["run"])
    state = m2_runner.recover(paths, env["paper"], now=env["now"], clock=lambda: env["now"],
                              monotonic=lambda: 0.0)
    beat = m2_runner.progress(state, env["now"])
    assert beat["clock_anomaly"] == "clock_unverified"
    assert beat["clock_anomaly_since"] == stamp(env["now"])
    assert beat["last_time_check"] is None
    assert beat["paused_reason"] is None and beat["paused_since"] is None


def test_starting_heartbeat_written_by_main_carries_the_clock_fields(env, monkeypatch):
    beats = []
    real = heartbeat.Heartbeat.beat

    def spy(self, state, progress=None, *args, **kwargs):
        beats.append((state, dict(progress or {})))
        return real(self, state, progress, *args, **kwargs)

    monkeypatch.setattr(heartbeat.Heartbeat, "beat", spy)
    fake = TimeGetters(env["now"])
    assert rig.run_once(env["run"], fake, clock=lambda: env["now"], monotonic=lambda: 0.0) == m2_runner.EXIT_OK
    starting = [progress for state, progress in beats if state == heartbeat.STATE_STARTING]
    assert starting and starting[0]["clock_anomaly"] == "clock_unverified"
    assert starting[0]["clock_anomaly_since"] == stamp(env["now"]) and starting[0]["last_time_check"] is None
    # the written heartbeat file is plain JSON with the three keys
    record = json.loads(heartbeat.beat_path(m2_runner.PROC).read_text(encoding="utf-8"))
    assert NEW_KEYS <= set(record["progress"])
    assert record["progress"]["last_time_check"]["result"] == "ok"


def test_direct_state_gets_since_on_the_first_tick_and_stays_unverified_while_not_paused(env):
    """Issue #289: a clean /time with no book to verify against leaves clock_unverified set while the runner
    is NOT paused; the heartbeat makes that visible (paused_reason stays None)."""
    first, beat = tick(env)
    assert first.action == m2_runner.ACT_RUN and beat["paused_reason"] is None
    assert beat["clock_anomaly"] == "clock_unverified" and beat["clock_anomaly_since"] == stamp(env["start"])
    for minutes in (1, 2, 3):
        report, beat = tick(env, at_s=60.0 * minutes)
        assert report.action == m2_runner.ACT_RUN and beat["paused_reason"] is beat["paused_since"] is None
        assert beat["clock_anomaly"] == "clock_unverified"
        assert beat["clock_anomaly_since"] == stamp(env["start"])  # the first stamp holds


def test_a_verified_tick_clears_the_anomaly_and_both_fields(env):
    report, beat = tick(env, signal=True)
    assert report.action == m2_runner.ACT_RUN and report.allows == 1
    assert beat["clock_anomaly"] is None and beat["clock_anomaly_since"] is None
    assert beat["last_time_check"]["result"] == "ok"


def test_since_survives_a_reason_switch_and_restamps_after_clearing(env):
    """clock_skew <-> clock_unverified is one anomaly episode (like paused_since, #253); a new episode after a
    clear gets a new stamp."""
    _, beat = tick(env, signal=True)
    assert beat["clock_anomaly"] is None
    report, beat = tick(env, at_s=60.0, offset=7.0)
    assert report.paused_reason == "clock_skew" and beat["clock_anomaly"] == "clock_skew"
    episode = stamp(env["start"] + timedelta(seconds=60))
    assert beat["clock_anomaly_since"] == episode
    report, beat = tick(env, at_s=120.0, error=RuntimeError("time down"))
    assert report.paused_reason == "clock_unverified" and beat["clock_anomaly"] == "clock_unverified"
    assert beat["clock_anomaly_since"] == episode  # a reason switch keeps the episode's first stamp
    _, beat = tick(env, at_s=180.0, signal=True)
    assert beat["clock_anomaly"] is None and beat["clock_anomaly_since"] is None
    _, beat = tick(env, at_s=240.0, offset=-7.0)
    assert beat["clock_anomaly"] == "clock_skew"
    assert beat["clock_anomaly_since"] == stamp(env["start"] + timedelta(seconds=240))


def test_last_time_check_records_each_check(env):
    _, beat = tick(env)
    assert beat["last_time_check"] == {"ts": stamp(env["start"]), "result": "ok", "low_s": -1.0, "high_s": 1.0,
                                       "rtt_s": 0.0}
    _, beat = tick(env, at_s=60.0, offset=7.0)
    assert beat["last_time_check"] == {"ts": stamp(env["start"] + timedelta(seconds=60)), "result": "clock_skew",
                                       "low_s": 6.0, "high_s": 8.0, "rtt_s": 0.0}
    _, beat = tick(env, at_s=120.0, offset=-5.0)
    assert beat["last_time_check"] == {"ts": stamp(env["start"] + timedelta(seconds=120)),
                                       "result": "clock_unverified", "low_s": -6.0, "high_s": -4.0, "rtt_s": 0.0}
    _, beat = tick(env, at_s=180.0, error=TimeoutError("offline"))
    assert beat["last_time_check"] == {"ts": stamp(env["start"] + timedelta(seconds=180)),
                                       "result": "clock_unverified", "low_s": None, "high_s": None, "rtt_s": None}


def test_request_time_shows_as_rtt(env):
    moments = iter([env["start"], env["start"] + timedelta(seconds=2)])
    env["state"].clock = lambda: next(moments)
    fake = TimeGetters(env["start"], offset=-1.0)  # server = start + 1 s, mid-request
    m2_runner.iteration(env["state"], now=env["start"], getters=fake.getters())
    check = m2_runner.progress(env["state"], env["start"])["last_time_check"]
    assert check == {"ts": stamp(env["start"]), "result": "ok", "low_s": -2.0, "high_s": 2.0, "rtt_s": 2.0}


def test_a_tick_that_stops_before_the_clock_check_keeps_the_last_check(env):
    _, beat = tick(env)
    before = beat["last_time_check"]
    (env["run"] / "KILL").write_text("stop", encoding="utf-8")
    report, beat = tick(env, at_s=60.0)
    assert report.action == m2_runner.ACT_FINISH
    assert beat["last_time_check"] == before


def test_existing_heartbeat_keys_are_unchanged(env):
    _, beat = tick(env, signal=True)
    assert set(beat) == OLD_KEYS | NEW_KEYS
    without = {key: value for key, value in beat.items() if key not in NEW_KEYS}
    state = env["state"]
    assert without["iteration"] == 1 and without["paused_reason"] is None and without["paused_since"] is None
    assert without["executor_enabled"] is False and without["pending_signals"] == len(state.pending)
    assert without["allows_total"] == 1 and without["resume_count"] == 0


def test_progress_is_read_only(env):
    tick(env, at_s=0.0, offset=7.0)
    skip = {"clock", "monotonic", "fee_cache", "fee_getter"}

    def snap():
        return {f.name: copy.deepcopy(getattr(env["state"], f.name))
                for f in dataclasses.fields(env["state"]) if f.name not in skip}

    snapshot = snap()
    first = m2_runner.progress(env["state"], env["now"] + timedelta(seconds=30))
    second = m2_runner.progress(env["state"], env["now"] + timedelta(seconds=30))
    assert first == second
    assert snap() == snapshot
    first["last_time_check"]["result"] = "tampered"  # a copy: editing the beat never edits the state
    assert m2_runner.progress(env["state"], env["now"])["last_time_check"]["result"] == "clock_skew"
    assert m2_runner.runner_clock_ok(env["state"]) is False


@pytest.mark.parametrize("offset,reason", [(0, None), (4.000001, "clock_unverified"), (7, "clock_skew")])
def test_observation_does_not_change_the_clock_decision(env, monkeypatch, offset, reason):
    actions = []
    monkeypatch.setattr(m2_runner, "_settle_tick", lambda *a: actions.append("settle") or [])
    monkeypatch.setattr(paper_ledger, "commit_if_allowed", lambda *a, **k: actions.append("commit"))
    report, beat = tick(env, offset=offset)
    if reason is None:
        assert report.action == m2_runner.ACT_RUN and report.reasons == []
    else:
        assert report.action == m2_runner.ACT_PAUSE and report.reasons == [reason] and actions == ["settle"]
    assert beat["last_time_check"]["result"] == (reason or "ok")
    assert beat["clock_anomaly"] == (reason or "clock_unverified")  # an empty clean tick never clears it
