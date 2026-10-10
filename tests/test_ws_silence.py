"""Task BI: the inbound-silence watchdog (WS_SILENCE_TIMEOUT_S) and the PING / PONG latency summary.
Task BK (Poly风控 F6): the market-data window (WS_DATA_SILENCE_S, ``data_silent``), which PONG never restarts.

No inbound frame of any kind (book data or PONG) for WS_SILENCE_TIMEOUT_S closes the connection
(``silent``), resyncs every asset and reconnects; any frame restarts the window. It is a backstop beside
Task AW's PONG deadline: with PINGs every WS_PING_INTERVAL_S a dead peer misses a PONG first, so
``pong_timeout`` normally fires earlier; ``silent`` fires on its own when the PONG path cannot (the PONG
deadline disabled here, or a process paused past the window, see tests/test_ws_clock_jump.py).
Offline: fake clock and a fake connection only.
"""
from __future__ import annotations

import json

import pytest

from src import m1_report
from src import ws_recorder as w
from src.thresholds import (MAX_BOOK_AGE_S, WS_DATA_SILENCE_S, WS_LATENCY_SUMMARY_PINGS, WS_PING_INTERVAL_S,
                            WS_PONG_TIMEOUT_S, WS_SILENCE_OBSERVED_HEALTHY_MAX_S, WS_SILENCE_TIMEOUT_S)
from tests.test_ws_recorder import A, T0, ListSink, _book, _trade


class Clock:
    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def wall(self):
        return 1.7e9 + self.t

    def sleep(self, seconds):
        self.t += seconds

    def now(self):
        return f"2026-10-10T00:00:00.000Z+{self.t:.2f}"


class ScriptedConnection:
    """Frames arrive at fixed offsets from connect (``frames``: list of (offset_s, frame)); a PING is answered
    by a PONG after ``pong_after_s`` while the connection is before ``pongs_until_s`` (None: never)."""

    def __init__(self, clock, frames, pong_after_s, pongs_until_s, sent):
        self.clock = clock
        self.start = clock.t
        self.frames = sorted(frames, key=lambda item: item[0])
        self.pong_after_s = pong_after_s
        self.pongs_until_s = pongs_until_s
        self.pongs: list[float] = []
        self.sent = sent

    def send(self, text):
        self.sent.append((self.clock.t, text))
        age = self.clock.t - self.start
        if text == "PING" and self.pong_after_s is not None and (self.pongs_until_s is None or age < self.pongs_until_s):
            self.pongs.append(self.clock.t + self.pong_after_s)

    def recv(self, timeout):
        assert timeout >= 0
        due = []
        if self.pongs:
            due.append((self.pongs[0], "PONG"))
        if self.frames:
            offset, frame = self.frames[0]
            due.append((self.start + offset, frame))
        if due:
            at, frame = min(due, key=lambda item: item[0])
            if at <= self.clock.t + timeout:
                self.clock.t = max(self.clock.t, at)
                if frame == "PONG" and self.pongs and self.pongs[0] == at:
                    self.pongs.pop(0)
                else:
                    self.frames.pop(0)
                return frame if isinstance(frame, str) else json.dumps(frame)
        self.clock.t += timeout
        return None

    def close(self):
        pass


class Transport:
    def __init__(self, clock, sessions):
        self.clock = clock
        self.sessions = list(sessions)
        self.sent: list = []
        self.connect_times: list[float] = []

    def connect(self, url, timeout):
        self.connect_times.append(self.clock.t)
        if not self.sessions:
            raise OSError("no route")
        frames, pong_after_s, pongs_until_s = self.sessions.pop(0)
        return ScriptedConnection(self.clock, frames, pong_after_s, pongs_until_s, self.sent)


def _run(sessions, runtime):
    clock = Clock()
    transport = Transport(clock, sessions)
    events: list[dict] = []
    sink = ListSink()
    rec = w.Recorder([A], transport=transport, sink=sink, max_runtime_s=runtime, monotonic=clock.monotonic,
                     sleep=clock.sleep, now=clock.now, on_event=events.append, wall=clock.wall)
    return rec.run(), transport, sink, events


def _books(offsets):
    return [(offset, _book(ts=T0 + int(offset * 1000) + 1)) for offset in offsets]


def _trades(offsets):
    return [(offset, _trade(ts=T0 + int(offset * 1000) + 1)) for offset in offsets]


LIVE_DATA = [20.0 * k for k in range(1, 400)]  # Task BK: a live market, a trade every 20 s
NEXT = (_books([0.05]) + _trades(LIVE_DATA), 0.2, None)  # the replacement connection: live and answering


@pytest.fixture
def no_pong_deadline(monkeypatch):
    """Take Task AW's PONG deadline out of the way so the watchdog is tested on its own."""
    monkeypatch.setattr(w, "WS_PONG_TIMEOUT_S", 1e9)


# --- the watchdog ----------------------------------------------------------------------------------


def test_a_connection_that_goes_quiet_is_closed_as_silent_after_the_window(no_pong_deadline):
    report, transport, sink, events = _run([(_books([0.05, 5.0]), None, None), NEXT], runtime=200.0)
    assert report.disconnects == {"silent": 1} and report.connects == 2
    detected = transport.connect_times[1] - w.backoff_delay(0)
    assert detected == pytest.approx(5.0 + WS_SILENCE_TIMEOUT_S, abs=0.01)
    gone = next(e for e in events if e["event"] == "disconnected")
    assert {k: v for k, v in gone.items() if k != "ts"} == {"event": "disconnected", "reason": "silent", "healthy": True}
    assert [row["ws_state"] for row in sink.rows][-3:] == ["live", "resync", "live"]
    assert any(row.get("resync_reason") == "disconnect_silent" for row in sink.rows)


def test_with_the_pong_deadline_a_dead_peer_is_still_replaced_within_the_silence_window():
    """AW's deadline is armed by the first unanswered PING, so it fires first; the watchdog never later."""
    report, transport, _, _ = _run([(_books([0.05]), 0.2, 5.0), NEXT], runtime=200.0)
    assert report.disconnects == {"pong_timeout": 1}
    last_inbound = 0.05  # the first PING goes out at 10 s, after the PONG cut-off at 5 s: no PONG ever arrives
    detected = transport.connect_times[1] - w.backoff_delay(0)
    assert detected - last_inbound <= WS_SILENCE_TIMEOUT_S
    assert detected == pytest.approx(WS_PING_INTERVAL_S + WS_PONG_TIMEOUT_S, abs=0.01)


def test_pongs_alone_keep_the_any_frame_window_open(no_pong_deadline, monkeypatch):
    monkeypatch.setattr(w, "WS_DATA_SILENCE_S", 1e9)  # Task BK's data window tested below
    report, _, _, _ = _run([(_books([0.05]), 0.2, None)], runtime=600.0)
    assert report.disconnects == {} and report.connects == 1
    assert report.pings >= 59 and report.pongs == report.pings


# --- Task BK: market-data silence (Poly风控 F6) ------------------------------------------------------


def test_pongs_but_no_market_data_reconnect_as_data_silent_at_90_s():
    """The server answers every PING (so neither pong_timeout nor the any-frame window fires) but sends no
    market event after the first book: data_silent at WS_DATA_SILENCE_S, full resync, reconnect."""
    report, transport, sink, events = _run([(_books([0.05]), 0.2, None), NEXT], runtime=300.0)
    assert report.disconnects == {"data_silent": 1} and report.connects == 2
    detected = transport.connect_times[1] - w.backoff_delay(0)
    assert detected == pytest.approx(0.05 + WS_DATA_SILENCE_S, abs=0.01)
    gone = next(e for e in events if e["event"] == "disconnected")
    assert {k: v for k, v in gone.items() if k != "ts"} == {"event": "disconnected", "reason": "data_silent",
                                                           "healthy": True}
    summary = next(e for e in events if e["event"] == "ping_latency")
    assert summary["pongs"] >= 8 and summary["unanswered"] <= 1  # PONGs kept coming the whole time
    assert any(row.get("resync_reason") == "disconnect_data_silent" for row in sink.rows)
    assert m1_report.summarize_events(events, [A])["disconnects"] == {"data_silent": 1}  # no cap exemption


def test_market_events_restart_the_data_window():
    offsets = [0.05 + 80.0 * k for k in range(8)]  # a trade every 80 s (< 90 s), PONGs as usual
    report, _, _, _ = _run([(_books([0.05]) + _trades(offsets[1:]), 0.2, None)], runtime=offsets[-1] + 85.0)
    assert report.disconnects == {}


@pytest.mark.parametrize("kind", ["price_change", "tick_size_change", "last_trade_price", "book", "new_kind"])
def test_every_event_type_counts_as_market_data(kind):
    out = w.Outcome()
    rec = w.Recorder([A], transport=None, sink=ListSink(), max_runtime_s=1.0)
    rec.core.handle({"event_type": kind, "asset_id": A, "market": "0xcond"}, "2030-01-01T00:00:00Z", 0.0, out)
    assert out.business == 1


@pytest.mark.parametrize("text", ["PONG", " PONG\n", "not json", "[]", "{}", '{"event_type": ""}', "42"])
def test_control_and_malformed_frames_do_not_count(text):
    rec = w.Recorder([A], transport=None, sink=ListSink(), max_runtime_s=1.0)
    assert rec.core.handle_text(text, "2030-01-01T00:00:00Z", 0.0).business == 0


def test_the_observed_healthy_gap_never_trips_the_data_window():
    gap = WS_SILENCE_OBSERVED_HEALTHY_MAX_S
    offsets = [0.05 + gap * k for k in range(15)]
    report, _, _, _ = _run([(_books(offsets), 0.2, None)], runtime=gap * 14)
    assert report.disconnects == {}


def test_data_window_relations():
    assert WS_SILENCE_TIMEOUT_S <= WS_DATA_SILENCE_S <= MAX_BOOK_AGE_S


def test_data_alone_keeps_the_connection(no_pong_deadline):
    offsets = [0.05 + 50.0 * k for k in range(13)]  # a frame every 50 s, never a PONG
    report, _, _, _ = _run([(_books(offsets), None, None)], runtime=600.0)
    assert report.disconnects == {} and report.pongs == 0


def test_the_observed_healthy_silence_never_trips_it(no_pong_deadline):
    gap = WS_SILENCE_OBSERVED_HEALTHY_MAX_S
    assert gap == 38.7
    offsets = [0.05 + gap * k for k in range(15)]
    report, _, _, _ = _run([(_books(offsets), None, None)], runtime=gap * 14)
    assert report.disconnects == {}


def test_the_window_counts_from_the_last_frame_not_from_connect(no_pong_deadline):
    report, transport, _, _ = _run([(_books([0.05, 30.0, 59.0]), None, None), NEXT], runtime=300.0)
    assert report.disconnects == {"silent": 1}
    assert transport.connect_times[1] - w.backoff_delay(0) == pytest.approx(59.0 + WS_SILENCE_TIMEOUT_S, abs=0.01)


def test_silent_counts_as_a_reconnect_in_the_m1_report(no_pong_deadline):
    report, _, _, events = _run([(_books([0.05]), None, None), NEXT], runtime=200.0)
    assert report.disconnects == {"silent": 1}
    summary = m1_report.summarize_events(events, [A])
    assert summary["disconnects"] == {"silent": 1}
    assert summary["reconnect_attempts"] == 1 and summary["reconnects"] == 1  # no exemption from the cap
    assert summary["gap_resyncs_total"] == 0


# --- the latency summary -----------------------------------------------------------------------------


def test_the_periodic_latency_summary():
    runtime = WS_PING_INTERVAL_S * (WS_LATENCY_SUMMARY_PINGS + 0.5)
    frames = _books([0.05]) + _trades(LIVE_DATA) + [(1.0, "PONG")]  # one PONG nobody asked for
    report, _, _, events = _run([(frames, 0.25, None)], runtime=runtime)
    summaries = [e for e in events if e["event"] == "ping_latency"]
    periodic, end = summaries
    assert periodic["scope"] == "periodic" and end["scope"] == "connection_end"
    assert periodic["pings"] == WS_LATENCY_SUMMARY_PINGS
    assert periodic["pongs"] == WS_LATENCY_SUMMARY_PINGS - 1 and periodic["unanswered"] == 1
    assert periodic["unexpected_pongs"] == 1
    assert periodic["rtt_n"] == WS_LATENCY_SUMMARY_PINGS - 1
    assert periodic["rtt_p50_ms"] == periodic["rtt_p99_ms"] == periodic["rtt_max_ms"] == pytest.approx(250.0)
    sent_at = WS_PING_INTERVAL_S * WS_LATENCY_SUMMARY_PINGS
    assert periodic["last_ping_ts"].endswith(f"+{sent_at:.2f}")
    assert periodic["last_pong_ts"].endswith(f"+{sent_at - WS_PING_INTERVAL_S + 0.25:.2f}")
    # the end-of-connection summary covers the PONGs since the periodic one (RTTs reset, counts do not)
    assert end["pings"] == report.pings and end["unanswered"] == 0
    assert end["pongs"] + end["unexpected_pongs"] == report.pongs  # report.pongs: every PONG frame received
    assert end["rtt_n"] == 1 and end["rtt_max_ms"] == pytest.approx(250.0)
    assert [e["event"] for e in events][-2:] == ["ping_latency", "end"]


def test_a_dead_connection_summary_shows_the_unanswered_pings(no_pong_deadline):
    _, _, _, events = _run([(_books([0.05]), 0.3, 25.0), NEXT], runtime=200.0)
    kinds = [e["event"] for e in events]
    first_gone = kinds.index("disconnected")
    assert kinds[first_gone + 1] == "ping_latency"
    summary = events[first_gone + 1]
    assert events[first_gone]["reason"] == "silent"
    assert summary["scope"] == "connection_end" and summary["pongs"] == 2 and summary["unanswered"] >= 1
    assert summary["rtt_p50_ms"] == pytest.approx(300.0)


def test_nearest_rank():
    assert w._nearest_rank([], 0.5) is None
    assert w._nearest_rank([3.0, 1.0, 2.0], 0.5) == 2.0
    assert w._nearest_rank([float(k) for k in range(1, 101)], 0.99) == 99.0
