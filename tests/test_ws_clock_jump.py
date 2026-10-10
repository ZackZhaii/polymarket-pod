"""Task BH: a host suspend / wake leaves the WS connection dead but silent; the recorder must notice.

Run m1-20261008T2259Z (git f760e51, before Task AW) stayed on dead connections for minutes after each of
18 wakes. Now: a wall clock that ran ahead of the monotonic clock by more than WS_PING_INTERVAL_S
reconnects at once (``clock_jump``, ``jump_s``); where the monotonic clock ran through the suspend, AW's
PONG deadline (WS_PONG_TIMEOUT_S) catches it. Either way: a reconnect within 30 s of waking.
Offline: fake clocks and a fake connection only.
"""
from __future__ import annotations

import json
import math

import pytest

from src import m1_report
from src import ws_recorder as w
from src.thresholds import M1_MAX_RECONNECTS, WS_PING_INTERVAL_S, WS_PONG_TIMEOUT_S
from tests.test_ws_recorder import A, TS, T0, ListSink, _book

WAKE_BOUND_S = 30.0  # Poly指挥: a post-wake dead connection must be replaced within 30 s


class Clocks:
    """Monotonic t and a wall clock = 1.7e9 + t + offset (offset grows during a suspend)."""

    def __init__(self):
        self.t = 0.0
        self.offset = 0.0
        self.sleeps: list[float] = []

    def monotonic(self):
        return self.t

    def wall(self):
        return 1.7e9 + self.t + self.offset

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds


class SuspendConnection:
    """Live (answers PING with PONG) until the scripted SUSPEND step, which pauses the process for
    ``pause_s``: the wall clock always advances, the monotonic one only if ``mono_runs``. After waking the
    socket is dead but silent: sends still succeed, nothing ever arrives (no PONG, no close)."""

    def __init__(self, script, clocks, sent, pause_s, mono_runs, log):
        self.script = list(script)
        self.clocks = clocks
        self.sent = sent
        self.pause_s = pause_s
        self.mono_runs = mono_runs
        self.log = log
        self.dead = False
        self.pending: list[str] = []

    def send(self, text):
        self.sent.append((self.clocks.t, text))
        if text == "PING" and not self.dead:
            self.pending.append("PONG")

    def recv(self, timeout):
        assert timeout >= 0
        if self.pending:
            self.clocks.t += 0.01
            return self.pending.pop(0)
        if self.script and self.script[0] == "SUSPEND":
            self.script.pop(0)
            # suspended inside recv(); on wake the wait has expired and the peer is gone
            if self.mono_runs:
                self.clocks.t += self.pause_s
            else:
                self.clocks.offset += self.pause_s
                self.clocks.t += min(timeout, 0.5)
            self.dead = True
            self.log["woke"] = self.clocks.t
            return None
        if self.script and not self.dead:
            step = self.script.pop(0)
            if step == "IDLE":  # a quiet stretch on a live connection (PINGs still answered)
                self.clocks.t += min(timeout, 4.0)
                return None
            self.clocks.t += 0.01
            return step if isinstance(step, str) else json.dumps(step)
        self.clocks.t += timeout
        return None

    def close(self):
        self.dead = True


class Transport:
    def __init__(self, sessions, clocks, pause_s, mono_runs):
        self.sessions = list(sessions)
        self.clocks = clocks
        self.pause_s = pause_s
        self.mono_runs = mono_runs
        self.sent: list = []
        self.log: dict = {}
        self.connect_times: list[float] = []

    def connect(self, url, timeout):
        self.connect_times.append(self.clocks.t)
        if not self.sessions:
            raise OSError("no route")
        return SuspendConnection(self.sessions.pop(0), self.clocks, self.sent, self.pause_s, self.mono_runs,
                                 self.log)


def _run(first, *, pause_s=600.0, mono_runs=False, runtime=None):
    clocks = Clocks()
    transport = Transport([first, [_book(ts=T0 + 9000)]], clocks, pause_s, mono_runs)
    events: list[dict] = []
    sink = ListSink()
    runtime = runtime or (pause_s if mono_runs else 0.0) + 200.0
    rec = w.Recorder([A], transport=transport, sink=sink, max_runtime_s=runtime, monotonic=clocks.monotonic,
                     sleep=clocks.sleep, now=lambda: TS, on_event=events.append, wall=clocks.wall)
    report = rec.run()
    return report, transport, sink, events, rec


# --- the jump measure ----------------------------------------------------------------------------


def test_clock_jump_measure():
    assert w.clock_jump_s(0.0, 600.0, 0.0, 1.0) == pytest.approx(599.0)
    assert w.clock_jump_s(0.0, WS_PING_INTERVAL_S + 1.0, 0.0, 1.0) is None  # exactly the interval: not a jump
    assert w.clock_jump_s(0.0, WS_PING_INTERVAL_S + 1.5, 0.0, 1.0) == pytest.approx(WS_PING_INTERVAL_S + 0.5)
    assert w.clock_jump_s(0.0, 5.0, 0.0, 5.0) is None  # both clocks ran: no jump
    assert w.clock_jump_s(0.0, -3600.0, 0.0, 1.0) is None  # wall stepped back (NTP): ignored
    assert w.clock_jump_s(0.0, math.nan, 0.0, 1.0) is None
    assert w.clock_jump_s(0.0, math.inf, 0.0, 1.0) is None


# --- the required replay: paused N minutes, resumes with the socket already dead -------------------


@pytest.mark.parametrize("pause_s", [60.0, 600.0, 1500.0, 7200.0])
@pytest.mark.parametrize("before", [[], ["IDLE"]], ids=["right-after-pong", "mid-ping-cycle"])
def test_monotonic_paused_by_the_suspend_reconnects_at_once_with_clock_jump(pause_s, before):
    report, transport, sink, events, _ = _run([_book(), *before, "SUSPEND"], pause_s=pause_s)
    woke = transport.log["woke"]
    assert report.disconnects == {"clock_jump": 1} and report.connects == 2
    assert transport.connect_times[1] - woke <= WAKE_BOUND_S
    assert transport.connect_times[1] - woke <= 1.0 + w.backoff_delay(0)  # next loop pass, then backoff
    gone = next(e for e in events if e["event"] == "disconnected")
    assert gone["reason"] == "clock_jump" and gone["jump_s"] == pytest.approx(pause_s, abs=1.0)
    assert gone["healthy"] is True
    # every asset resyncs at once, then the new session goes live again
    assert [row["ws_state"] for row in sink.rows] == ["live", "resync", "live"]
    assert sink.rows[1]["resync_reason"] == "disconnect_clock_jump"


@pytest.mark.parametrize("pause_s", [60.0, 600.0, 1500.0])
@pytest.mark.parametrize("before", [[], ["IDLE"]], ids=["right-after-pong", "mid-ping-cycle"])
def test_monotonic_running_through_a_long_suspend_is_caught_at_once_as_silent(pause_s, before):
    """Task BI: once the monotonic pause reaches WS_SILENCE_TIMEOUT_S nothing has arrived for that long, so the
    silence watchdog closes the dead socket on the first pass after waking, before any PING goes out."""
    report, transport, sink, events, _ = _run([_book(), *before, "SUSPEND"], pause_s=pause_s, mono_runs=True)
    woke = transport.log["woke"]
    assert report.disconnects == {"silent": 1} and report.connects == 2
    detected = transport.connect_times[1] - w.backoff_delay(0)
    assert detected - woke < 0.1 <= WAKE_BOUND_S
    assert sink.rows[1]["resync_reason"] == "disconnect_silent"


@pytest.mark.parametrize("pause_s", [15.0, 45.0])
@pytest.mark.parametrize("before", [[], ["IDLE"]], ids=["right-after-pong", "mid-ping-cycle"])
def test_monotonic_running_through_a_short_suspend_is_caught_by_the_aw_pong_deadline(pause_s, before):
    report, transport, sink, events, _ = _run([_book(), *before, "SUSPEND"], pause_s=pause_s, mono_runs=True)
    woke = transport.log["woke"]
    assert sum(report.disconnects.values()) == 1 and report.connects == 2
    assert set(report.disconnects) <= {"pong_timeout", "silent"}
    first_ping_after_wake = min(t for t, text in transport.sent if text == "PING" and t >= woke)
    assert first_ping_after_wake - woke < 0.1  # the overdue PING goes out on the first pass after waking
    detected = transport.connect_times[1] - w.backoff_delay(0)
    assert detected - woke <= WS_PONG_TIMEOUT_S <= WAKE_BOUND_S
    reason = next(iter(report.disconnects))
    assert sink.rows[1]["resync_reason"] == f"disconnect_{reason}"


def test_without_a_suspend_nothing_disconnects():
    clocks = Clocks()
    transport = Transport([[_book()]], clocks, 0.0, False)
    rec = w.Recorder([A], transport=transport, sink=ListSink(), max_runtime_s=WS_PONG_TIMEOUT_S * 6,
                     monotonic=clocks.monotonic, sleep=clocks.sleep, now=lambda: TS, wall=clocks.wall)
    report = rec.run()
    assert report.disconnects == {} and report.connects == 1 and report.pings >= 15


@pytest.mark.parametrize("step, expect", [(-3600.0, {}), (WS_PING_INTERVAL_S * 0.9, {})])
def test_a_wall_step_back_or_a_small_drift_keeps_the_connection(step, expect):
    clocks = Clocks()

    class Drift(SuspendConnection):
        def recv(self, timeout):
            if self.script and self.script[0] == "STEP":
                self.script.pop(0)
                self.clocks.offset += step
                self.clocks.t += 0.01
                return None
            return super().recv(timeout)

    class T(Transport):
        def connect(self, url, timeout):
            self.connect_times.append(self.clocks.t)
            return Drift(self.sessions.pop(0), self.clocks, self.sent, 0.0, False, self.log)

    rec = w.Recorder([A], transport=T([[_book(), "STEP"]], clocks, 0.0, False), sink=ListSink(),
                     max_runtime_s=60.0, monotonic=clocks.monotonic, sleep=clocks.sleep, now=lambda: TS,
                     wall=clocks.wall)
    assert rec.run().disconnects == expect


def test_the_default_wall_clock_is_time_time():
    rec = w.Recorder([A], transport=Transport([], Clocks(), 0.0, False), sink=ListSink(), max_runtime_s=1.0)
    import time
    assert rec.wall is time.time


# --- M1 accounting: no exemption ---------------------------------------------------------------


def test_clock_jumps_count_as_reconnects_in_the_m1_report():
    events = [{"event": "connected"}]
    for _ in range(18):  # the 18 wakes of m1-20261008T2259Z
        events += [{"event": "disconnected", "reason": "clock_jump", "jump_s": 1296.0},
                   {"event": "resync", "asset": A, "reason": "disconnect_clock_jump"},
                   {"event": "connected"}]
    summary = m1_report.summarize_events(events, [A])
    assert summary["disconnects"] == {"clock_jump": 18}
    assert summary["reconnect_attempts"] == 18 and summary["reconnects"] == 18
    assert summary["gap_resyncs_total"] == 0  # a disconnect resync is not a gap
    assert M1_MAX_RECONNECTS == 48  # unchanged; 18 wakes leave 30 for everything else
