"""Task BL (Poly盘口 follow-ups to BI / BK): the recorder's connection telemetry.

- every PING and every PONG is an event (``ping`` with this connection's sequence number; ``pong`` with its
  round trip, or ``unexpected`` when no PING was outstanding);
- inbound frames are counted (all frames, and the market-data ones) per run and per connection;
- ``disconnected`` says what the connection had seen: ``silence_s`` / ``data_silence_s``, ``last_inbound_ts``,
  ``last_data_ts``, ``pong_wait_since_ts`` and the connection's frame counts;
- the m1_run manifest records PONGs (not only PINGs), the frame counts and backoff clock jumps;
- a suspend / wake during a backoff wait is detected and written (``clock_jump``, phase ``backoff``); in a
  session the existing ``clock_jump`` disconnect also writes the event (phase ``session``).
Logging only: no window, deadline or threshold changes. Offline: fake clocks and connections.
"""
from __future__ import annotations

import pytest

from src import m1_report
from src import ws_recorder as w
from src.thresholds import WS_PING_INTERVAL_S, WS_PONG_TIMEOUT_S
from tests import test_m1_run as m1
from tests import test_ws_clock_jump as cj
from tests import test_ws_silence as ws
from tests.test_ws_recorder import A, ListSink

STAMP = "2026-10-10T00:00:00.000Z+"


def _kinds(events, *names):
    return [e for e in events if e["event"] in names]


# --- individual PING / PONG events ---------------------------------------------------------------


def test_every_ping_and_pong_is_an_event_with_its_round_trip():
    frames = ws._books([0.05]) + ws._trades(ws.LIVE_DATA) + [(1.0, "PONG")]  # one PONG nobody asked for
    report, _, _, events = ws._run([(frames, 0.25, None)], runtime=WS_PING_INTERVAL_S * 6.5)
    pings, pongs = _kinds(events, "ping"), _kinds(events, "pong")
    assert [e["seq"] for e in pings] == list(range(1, report.pings + 1)) and report.pings == 6
    assert len(pongs) == report.pongs == 7
    assert pongs[0] == {"ts": STAMP + "1.00", "event": "pong", "rtt_ms": None, "unexpected": True}
    answered = pongs[1:]
    assert all(e["rtt_ms"] == pytest.approx(250.0) and e["unexpected"] is False for e in answered)
    assert [e["ts"] for e in pings] == [STAMP + f"{WS_PING_INTERVAL_S * k:.2f}" for k in range(1, 7)]
    assert [e["ts"] for e in answered] == [STAMP + f"{WS_PING_INTERVAL_S * k + 0.25:.2f}" for k in range(1, 7)]


def test_the_ping_sequence_restarts_with_each_connection(monkeypatch):
    monkeypatch.setattr(w, "WS_PONG_TIMEOUT_S", 1e9)
    _, _, _, events = ws._run([(ws._books([0.05]), None, None), ws.NEXT], runtime=100.0)
    kinds = [e["event"] for e in events]
    second = kinds.index("connected", 1)
    first_seqs = [e["seq"] for e in events[:second] if e["event"] == "ping"]
    later_seqs = [e["seq"] for e in events[second:] if e["event"] == "ping"]
    assert first_seqs == list(range(1, len(first_seqs) + 1)) and later_seqs[:2] == [1, 2]


# --- inbound frame counts ----------------------------------------------------------------------------


def test_inbound_frames_count_everything_and_data_frames_only_market_events():
    # the malformed frames come last (an undecodable frame resyncs every asset; that is not under test here)
    frames = ws._books([0.05]) + ws._trades([20.0, 40.0]) + [(5.0, "PONG"), (41.0, "not json"), (42.0, "{}")]
    report, _, _, _ = ws._run([(frames, 0.25, None)], runtime=43.0)
    assert report.data_frames == 3  # the book and two trades
    assert report.pongs == report.pings + 1 == 5  # every PING answered, plus the stray one
    assert report.inbound_frames == report.data_frames + report.pongs + 2  # + the two malformed frames


# --- the disconnected event --------------------------------------------------------------------------


def test_pong_timeout_reports_since_when_a_pong_was_awaited():
    report, _, _, events = ws._run([(ws._books([0.05]), 0.2, 5.0), ws.NEXT], runtime=200.0)
    assert report.disconnects == {"pong_timeout": 1}
    gone = _kinds(events, "disconnected")[0]
    end = WS_PING_INTERVAL_S + WS_PONG_TIMEOUT_S
    assert gone["pong_wait_since_ts"] == STAMP + f"{WS_PING_INTERVAL_S:.2f}"  # the first unanswered PING
    assert gone["last_inbound_ts"] == gone["last_data_ts"] == STAMP + "0.05"
    assert gone["silence_s"] == gone["data_silence_s"] == pytest.approx(end - 0.05, abs=0.001)
    assert gone["inbound_frames"] == gone["data_frames"] == 1


def test_a_server_close_before_any_ping_reports_no_pong_wait():
    clock = m1.Clock()
    transport = m1.FakeTransport([[m1._book(A), "CLOSE"], [m1._book(A, at=9.0)]], clock)
    events: list[dict] = []
    rec = w.Recorder([A], transport=transport, sink=ListSink(), max_runtime_s=30.0, monotonic=clock.monotonic,
                     sleep=clock.sleep, now=clock.now, on_event=events.append)
    rec.run()
    gone = _kinds(events, "disconnected")[0]
    assert gone["reason"] == "closed" and gone["pong_wait_since_ts"] is None
    assert gone["inbound_frames"] == gone["data_frames"] == 1 and gone["last_data_ts"] == gone["last_inbound_ts"]
    assert gone["silence_s"] == gone["data_silence_s"] == 0.0  # closed right after the book arrived


def test_no_frame_at_all_leaves_the_last_times_empty(monkeypatch):
    monkeypatch.setattr(w, "WS_PONG_TIMEOUT_S", 1e9)
    _, _, _, events = ws._run([([], None, None), ws.NEXT], runtime=200.0)
    gone = _kinds(events, "disconnected")[0]
    assert gone["last_inbound_ts"] is None and gone["last_data_ts"] is None
    assert gone["inbound_frames"] == gone["data_frames"] == 0
    assert gone["silence_s"] > 0 and gone["pong_wait_since_ts"] == STAMP + f"{WS_PING_INTERVAL_S:.2f}"


def test_the_m1_report_still_reads_the_richer_events(monkeypatch):
    monkeypatch.setattr(w, "WS_PONG_TIMEOUT_S", 1e9)
    _, _, _, events = ws._run([(ws._books([0.05]), None, None), ws.NEXT], runtime=200.0)
    summary = m1_report.summarize_events(events, [A])
    assert summary["disconnects"] == {"silent": 1} and summary["reconnects"] == 1


# --- clock jumps -------------------------------------------------------------------------------------


def test_a_session_clock_jump_writes_the_clock_jump_event_before_disconnected():
    report, _, _, events, _ = cj._run([cj._book(), "SUSPEND"], pause_s=600.0)
    kinds = [e["event"] for e in events]
    jump = events[kinds.index("clock_jump")]
    assert jump["phase"] == "session" and jump["jump_s"] == pytest.approx(600.0, abs=1.0)
    assert kinds[kinds.index("clock_jump") + 1] == "disconnected"
    assert report.clock_jumps_in_backoff == 0
    assert _kinds(events, "disconnected")[0]["pong_wait_since_ts"] is None  # suspended before the first PING


class _SuspendingSleep(cj.Clocks):
    """The host sleeps for ``pause_s`` (wall clock only) during the ``nth`` sleep call."""

    def __init__(self, pause_s, nth=1):
        super().__init__()
        self.pause_s = pause_s
        self.nth = nth

    def sleep(self, seconds):
        super().sleep(seconds)
        if len(self.sleeps) == self.nth:
            self.offset += self.pause_s


class _Refuse:
    def connect(self, url, timeout):
        raise OSError("no route")


@pytest.mark.parametrize("sliced", [False, True], ids=["one-sleep", "sliced-wait"])
def test_a_suspend_during_a_backoff_wait_is_logged(sliced):
    clocks = _SuspendingSleep(900.0)
    events: list[dict] = []
    rec = w.Recorder([A], transport=_Refuse(), sink=ListSink(), max_runtime_s=30.0, monotonic=clocks.monotonic,
                     sleep=clocks.sleep, now=lambda: cj.TS, on_event=events.append, wall=clocks.wall,
                     stop=(lambda: False) if sliced else None)
    report = rec.run()
    jumps = _kinds(events, "clock_jump")
    assert len(jumps) == 1 == report.clock_jumps_in_backoff
    assert jumps[0]["phase"] == "backoff" and jumps[0]["attempt"] == 0
    assert jumps[0]["jump_s"] == pytest.approx(900.0, abs=0.001)
    kinds = [e["event"] for e in events]
    assert kinds.index("backoff") < kinds.index("clock_jump") < kinds.index("connect_failed", 1)
    assert report.connect_failures >= 2  # the reconnect attempts go on as before


def test_backoff_waits_without_a_suspend_log_no_jump():
    clocks = cj.Clocks()
    events: list[dict] = []
    rec = w.Recorder([A], transport=_Refuse(), sink=ListSink(), max_runtime_s=60.0, monotonic=clocks.monotonic,
                     sleep=clocks.sleep, now=lambda: cj.TS, on_event=events.append, wall=clocks.wall)
    report = rec.run()
    assert not _kinds(events, "clock_jump") and report.clock_jumps_in_backoff == 0 and report.connect_failures > 2


def test_a_small_drift_or_a_wall_step_back_during_backoff_is_no_jump():
    for step in (WS_PING_INTERVAL_S * 0.9, -3600.0):
        clocks = _SuspendingSleep(step)
        report = w.Recorder([A], transport=_Refuse(), sink=ListSink(), max_runtime_s=30.0,
                            monotonic=clocks.monotonic, sleep=clocks.sleep, now=lambda: cj.TS,
                            wall=clocks.wall).run()
        assert report.clock_jumps_in_backoff == 0


# --- the m1_run manifest -----------------------------------------------------------------------------


def test_the_manifest_records_pongs_frames_and_backoff_jumps(tmp_path):
    sessions = [[m1._book(A), m1._book(m1.B, bid="0.40", ask="0.60"), "CLOSE"],
                [m1._book(A, at=9.0), m1._book(m1.B, at=9.0)]]
    code, out, _, _, _ = m1._main(tmp_path, sessions, ["--top", "1"])
    assert code == 0
    counts = m1._manifest(out)["counts"]
    events = m1._rows(out, "events")
    assert counts["pings"] == sum(1 for e in events if e["event"] == "ping") > 0
    assert counts["pongs"] == sum(1 for e in events if e["event"] == "pong") == counts["pings"]
    assert counts["data_frames"] == 4
    assert counts["inbound_frames"] == counts["data_frames"] + counts["pongs"]
    assert counts["clock_jumps_in_backoff"] == 0
