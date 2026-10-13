"""Task AI (M1 acceptance): the long-run recorder entry src.m1_run. Offline only.

A fake transport and a fake clock drive the real Recorder; a fake getter stands in for the public
CLOB /book; SIGTERM/SIGINT are delivered by calling the installed handler from inside the fake
transport (works on every OS). Nothing here opens a socket or reads the network.
"""
from __future__ import annotations

import ast
import json
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src import m1_run as r
from src import ws_recorder as w
from src.podlib import read_jsonl
from src.thresholds import (
    M1_REST_ASSETS,
    M1_REST_EVERY_S,
    M1_ROTATE_BYTES,
    M1_ROTATE_S,
    M1_TOP_MARKETS,
    WS_RESYNC_TIMEOUT_S,
    WS_STOP_POLL_S,
)

ROOT = Path(__file__).resolve().parents[1]
A, B, C, D = "111", "222", "333", "444"
M = "0xcond"
BASE = datetime(2026, 10, 9, 0, 0, tzinfo=timezone.utc)
BASE_MS = int(BASE.timestamp() * 1000)


class Clock:
    """One fake clock for monotonic, sleep and the recorder's ISO now()."""

    def __init__(self):
        self.t = 0.0
        self.sleeps: list[float] = []
        self.lock = threading.Lock()

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        assert seconds >= 0
        self.sleeps.append(seconds)
        self.t += seconds

    def now(self):
        stamp = BASE + timedelta(seconds=self.t)
        return stamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _book(asset=A, bid="0.48", ask="0.52", at=1.0):
    return {"event_type": "book", "asset_id": asset, "market": M,
            "bids": [{"price": bid, "size": "30"}], "asks": [{"price": ask, "size": "25"}],
            "timestamp": str(BASE_MS + int(at * 1000)), "hash": "0xabc"}


class FakeConnection:
    """Plays a script and answers every PING with a PONG (Task AW: an unanswered PING disconnects)."""

    def __init__(self, script, clock, sent, timeouts):
        self.script = list(script)
        self.clock = clock
        self.sent = sent
        self.timeouts = timeouts
        self.pending: list[str] = []

    def send(self, text):
        self.sent.append(text)
        if text == "PING":
            self.pending.append("PONG")

    def recv(self, timeout):
        assert timeout >= 0
        if self.pending:
            self.clock.t += 0.01
            return self.pending.pop(0)
        self.timeouts.append(timeout)
        if not self.script:
            self.clock.t += timeout
            return None
        step = self.script.pop(0)
        if callable(step):
            step()
            self.clock.t += timeout
            return None
        if step == "CLOSE":
            raise w.TransportClosed("server closed")
        self.clock.t += 0.01
        return json.dumps(step)

    def close(self):
        pass


class FakeTransport:
    def __init__(self, sessions, clock):
        self.sessions = list(sessions)
        self.clock = clock
        self.sent: list[str] = []
        self.urls: list[str] = []
        self.timeouts: list[float] = []

    def connect(self, url, timeout):
        self.urls.append(url)
        assert 0 < timeout <= WS_RESYNC_TIMEOUT_S
        if not self.sessions:
            raise OSError("no route")
        session = self.sessions.pop(0)
        if isinstance(session, BaseException):
            raise session
        return FakeConnection(session, self.clock, self.sent, self.timeouts)


def _market(market_id, tokens, ts="2026-10-09T00:00:00Z", **extra):
    row = {"ts": ts, "id": market_id, "question": f"Q {market_id}?", "condition_id": f"0x{market_id}",
           "token_ids": list(tokens), "version": "v2", "token_id_source": "clobTokenIds", "token_ids_problem": "",
           "outcomes": ["Yes", "No"], "liquidity_usd": 5000.0, "volume_24h_usd": 100.0}
    row.update(extra)
    return row


def _markets_file(tmp_path, rows=None):
    path = tmp_path / "markets.jsonl"
    rows = rows if rows is not None else [_market("m1", [A, B]), _market("m2", [C, D])]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def _rest_getter(calls, bid="0.48", ask="0.52"):
    def get(url):
        calls.append(url)
        return {"bids": [{"price": bid, "size": "30"}], "asks": [{"price": ask, "size": "25"}],
                "timestamp": str(BASE_MS + 1500), "market": M, "tick_size": "0.01", "min_order_size": "5"}
    return get


def _clock_getter(lead_ms=-200, calls=None):
    """Task CM: the start-up clock check's /book reader (kept apart from the REST sampler's calls). The book's
    timestamp is ``lead_ms`` from the fake clock's start (negative: local clock ahead of it, as on a synced host)."""
    def get(url):
        if calls is not None:
            calls.append(url)
        return {"bids": [{"price": "0.48", "size": "30"}], "asks": [{"price": "0.52", "size": "25"}],
                "timestamp": str(BASE_MS + lead_ms), "market": M, "tick_size": "0.01"}
    return get


def _git():
    return {"sha": "f" * 40, "dirty": False}


def _main(tmp_path, sessions, argv=(), *, rest=None, clock=None, markets=None, **kwargs):
    clock = clock or Clock()
    transport = FakeTransport(sessions, clock)
    calls: list[str] = []
    out = tmp_path / "run"
    full = ["--out", str(out), "--markets", str(markets or _markets_file(tmp_path)), "--max-runtime", "60", *argv]
    kwargs.setdefault("clock_getter", _clock_getter())  # Task CM
    code = r.main(full, transport=transport, rest_getter=rest or _rest_getter(calls), monotonic=clock.monotonic,
                  sleep=clock.sleep, now=clock.now, git=_git, **kwargs)
    return code, out, transport, calls, clock


def _manifest(out):
    return json.loads((out / "manifest.json").read_text(encoding="utf-8"))


def _rows(out, stream):
    manifest = _manifest(out)
    rows = []
    for rel in manifest["files"][stream]:
        rows += read_jsonl(out / rel)
    return rows


# --- asset selection ------------------------------------------------------------------------------


def test_selection_takes_the_latest_batch_by_parsed_time_in_file_order():
    old = [_market("old", ["9", "8"], ts="2026-10-09T08:00:00+08:00")]  # = 00:00Z, older than 00:30Z
    # As strings "2026-10-09T08:..." > "2026-10-09T00:30...": a string max would pick the old batch.
    new = [_market("m2", [C, D], ts="2026-10-09T00:30:00Z"), _market("m1", [A, B], ts="2026-10-09T00:30:00Z")]
    picked = r.select_assets(old + new, top=5)
    assert picked["assets"] == [C, D, A, B] and picked["scan_ts"] == "2026-10-09T00:30:00Z"
    assert [m["market_id"] for m in picked["markets"]] == ["m2", "m1"]
    assert picked["meta"][A]["outcome"] == "Yes" and picked["meta"][B]["outcome"] == "No"


def test_selection_top_n_takes_both_tokens_of_each_market():
    rows = [_market(f"m{i}", [f"{i}1", f"{i}2"]) for i in range(1, 8)]
    assert r.select_assets(rows, top=2)["assets"] == ["11", "12", "21", "22"]


@pytest.mark.parametrize("bad", [
    {"token_ids_problem": "token_ids_version_mismatch"}, {"version": None}, {"version": ""}, {"version": 2},
    {"token_ids": ["1"]}, {"token_ids": ["1", "2", "3"]}, {"token_ids": [1, 2]}, {"token_ids": ["1", "1"]},
    {"token_ids": ["1", ""]}, {"token_ids": [" 1", "2"]}, {"token_ids": None}, {"id": ""},
])
def test_selection_skips_markets_without_usable_version_aware_tokens(bad):
    rows = [_market("bad", ["1", "2"], **bad), _market("m1", [A, B])]
    picked = r.select_assets(rows, top=5)
    assert picked["assets"] == [A, B] and sum(picked["skipped"].values()) == 1


def test_selection_skips_duplicate_markets_and_shared_tokens():
    rows = [_market("m1", [A, B]), _market("m1", [C, D]), _market("m3", [B, C])]
    picked = r.select_assets(rows, top=5)
    assert picked["assets"] == [A, B]
    assert picked["skipped"] == {"duplicate_market": 1, "token_in_two_markets": 1}


def test_selection_of_nothing_is_empty():
    assert r.select_assets([], 5)["assets"] == []
    assert r.select_assets([{"id": "m1", "ts": "yesterday"}], 5)["assets"] == []


# --- rotating JSONL ---------------------------------------------------------------------------------


def test_rotation_by_size_keeps_every_line_whole_and_in_order(tmp_path):
    row = {"n": 0, "pad": "x" * 40}
    size = r._line_bytes(row)
    sink = r.RotatingJsonl(tmp_path / "books", "books", max_bytes=size * 3, max_age_s=1e9, monotonic=lambda: 0.0)
    for n in range(7):
        sink.write({"n": n, "pad": "x" * 40})
    sink.close()
    assert sink.files == ["books/books-000001.jsonl", "books/books-000002.jsonl", "books/books-000003.jsonl"]
    sizes = [(tmp_path / rel).stat().st_size for rel in sink.files]
    assert sizes == [size * 3, size * 3, size] and all(s <= size * 3 for s in sizes)
    rows = [row for rel in sink.files for row in read_jsonl(tmp_path / rel)]
    assert [row["n"] for row in rows] == list(range(7)) and sink.rows == 7


def test_rotation_writes_an_oversized_line_alone(tmp_path):
    sink = r.RotatingJsonl(tmp_path / "rest", "rest", max_bytes=10, max_age_s=1e9, monotonic=lambda: 0.0)
    sink.write({"big": "y" * 50})
    sink.write({"big": "z" * 50})
    sink.close()
    assert len(sink.files) == 2


def test_rotation_by_age(tmp_path):
    clock = Clock()
    sink = r.RotatingJsonl(tmp_path / "events", "events", max_bytes=10**9, max_age_s=60, monotonic=clock.monotonic)
    sink.write({"a": 1})
    clock.t = 59.9
    sink.write({"a": 2})
    clock.t = 60.0
    sink.write({"a": 3})
    sink.close()
    assert [len(read_jsonl(tmp_path / rel)) for rel in sink.files] == [2, 1]


def test_rotated_lines_are_byte_identical_to_the_ws_recorder_appender(tmp_path):
    row = {"q": "Will \u00e9 win?", "p": 0.5, "n": None}
    plain = w.JsonlAppender(tmp_path / "plain.jsonl")
    plain.write(row)
    plain.close()
    sink = r.RotatingJsonl(tmp_path / "books", "books", max_bytes=10**6, max_age_s=10, monotonic=lambda: 0.0)
    sink.write(row)
    sink.close()
    assert (tmp_path / sink.files[0]).read_bytes() == (tmp_path / "plain.jsonl").read_bytes()


@pytest.mark.parametrize("limits", [(0, 10), (10, 0), (10, -1), (1.5, 10)])
def test_rotation_limits_must_be_positive(tmp_path, limits):
    with pytest.raises(ValueError):
        r.RotatingJsonl(tmp_path, "x", *limits)


def test_rotation_refuses_nan_before_writing(tmp_path):
    sink = r.RotatingJsonl(tmp_path / "books", "books", max_bytes=100, max_age_s=10, monotonic=lambda: 0.0)
    with pytest.raises(ValueError):
        sink.write({"p": float("nan")})
    assert sink.files == []


def _snapshot(folder):
    return {p.name: p.read_bytes() for p in sorted(folder.glob("*"))} if folder.exists() else None


def test_rotation_close_is_idempotent_and_permanent(tmp_path, monkeypatch):
    """Issue #82 (2): after close a write fails explicitly; files, bytes, rows and the list stay unchanged."""
    clock = Clock()
    sink = r.RotatingJsonl(tmp_path / "books", "books", max_bytes=10**6, max_age_s=60, monotonic=clock.monotonic)
    sink.write({"n": 1})
    sink.write({"n": 2})
    sink.close()
    before = _snapshot(tmp_path / "books")
    assert sink.closed and sink.files == ["books/books-000001.jsonl"] and sink.rows == 2
    sink.close()  # idempotent: a second close is a no-op, not an error

    def no_reopen(self):
        raise AssertionError("_rotate called after close")
    monkeypatch.setattr(r.RotatingJsonl, "_rotate", no_reopen)
    clock.t = 3600.0  # past max_age_s: an open sink would rotate into a new file here
    for row in ({"n": 3}, {"big": "x" * 10**6}, {"p": float("nan")}):
        with pytest.raises(r.SinkClosed):
            sink.write(row)
    assert sink.closed and sink.files == ["books/books-000001.jsonl"] and sink.rows == 2
    assert _snapshot(tmp_path / "books") == before
    assert [row["n"] for row in read_jsonl(tmp_path / sink.files[0])] == [1, 2]


def test_rotation_close_before_any_write_never_opens_a_file(tmp_path):
    sink = r.RotatingJsonl(tmp_path / "rest", "rest", max_bytes=100, max_age_s=10, monotonic=lambda: 0.0)
    sink.close()
    with pytest.raises(r.SinkClosed):
        sink.write({"a": 1})
    sink.close()
    assert sink.files == [] and sink.rows == 0 and not (tmp_path / "rest").exists()


def test_rotation_stays_closed_when_closing_the_file_fails(tmp_path, monkeypatch):
    sink = r.RotatingJsonl(tmp_path / "books", "books", max_bytes=10**6, max_age_s=60, monotonic=lambda: 0.0)
    sink.write({"n": 1})

    def broken(self):
        raise OSError("close failed")
    monkeypatch.setattr(w.JsonlAppender, "close", broken)
    with pytest.raises(OSError):
        sink.close()
    monkeypatch.undo()
    assert sink.closed
    with pytest.raises(r.SinkClosed):
        sink.write({"n": 2})
    sink.close()  # still a no-op: nothing is left open to close again
    assert sink.files == ["books/books-000001.jsonl"] and sink.rows == 1


def test_sink_closed_is_a_write_failure_re_exported_by_m1_run():
    assert r.SinkClosed is w.SinkClosed  # one class: m1_run re-exports the ws_recorder writer's error
    assert issubclass(r.SinkClosed, OSError)  # every existing write-failure path maps it to EXIT_WRITE_FAILED=4


def test_raw_book_sink_refuses_every_write_after_close(tmp_path):
    events: list[dict] = []
    row = {"ts": "t", "asset_id": A, "frame": {"n": 0}}
    line = r._line_bytes(row)
    sink = r.RawBookSink(tmp_path / "raw_books", file_max_bytes=line * 2, run_max_bytes=line * 2, max_age_s=3600,
                         on_event=events.append, now=lambda: "t")
    sink.write(row)
    sink.close()
    before = _snapshot(tmp_path / "raw_books")
    state = (list(sink.files.files), sink.files.rows, sink.saved, sink.bytes, dict(sink.dropped))
    # a fitting frame, an unserialisable one, an oversized one and one past the run cap: all refused, none counted
    for late in (row, {"frame": float("nan")}, {"frame": "x" * (line * 3)}, row, row):
        with pytest.raises(r.SinkClosed):
            sink.write(late)
    sink.close()
    assert (list(sink.files.files), sink.files.rows, sink.saved, sink.bytes, dict(sink.dropped)) == state
    assert _snapshot(tmp_path / "raw_books") == before and events == []


@pytest.mark.parametrize("stream", ["books", "rest"])
def test_a_write_to_a_closed_sink_exits_4(tmp_path, monkeypatch, stream):
    real = r.RotatingJsonl.write

    def write(self, row):
        if self.prefix == stream:
            self.close()  # the sink was closed under the writer: the write must fail, never reopen a file
        real(self, row)
    monkeypatch.setattr(r.RotatingJsonl, "write", write)
    code, out, _, _, _ = _main(tmp_path, [[_book()]], ["--assets", A])
    manifest = _manifest(out)
    assert code == r.EXIT_WRITE_FAILED and manifest["status"] == "failed" and manifest["exit_code"] == 4
    assert manifest["files"][stream] == [] and not (out / stream).exists()


# --- REST sampler -----------------------------------------------------------------------------------


class ListSink:
    def __init__(self):
        self.rows: list[dict] = []

    def write(self, row):
        json.dumps(row, allow_nan=False)
        self.rows.append(row)


def test_rest_sampler_samples_at_start_then_every_k_seconds_until_the_deadline():
    clock = Clock()
    calls: list[str] = []
    sink, events = ListSink(), ListSink()
    meta = {A: {"market_id": "m1", "question": "Q?", "outcome": "Yes", "condition_id": "0xc", "liquidity_usd": 1.0}}
    sampler = r.RestSampler([A, B], meta, sink, events, every_s=600, deadline_s=1800, getter=_rest_getter(calls),
                            now=clock.now, monotonic=clock.monotonic)

    def wait(seconds):
        assert seconds > 0
        clock.t += seconds
        return False

    sampler.run(threading.Event(), wait)
    assert sampler.samples == 3 and len(sink.rows) == 6 and sampler.failure is None  # t = 0, 600, 1200
    assert all(url == f"https://clob.polymarket.com/book?token_id={sink.rows[i]['token_id']}" for i, url in enumerate(calls))
    first = sink.rows[0]
    assert first["market_id"] == "m1" and first["outcome"] == "Yes" and first["sample"] == 1
    assert first["book_ts"] == "2026-10-09T00:00:01.500Z" and first["best_bid"] == 0.48 and first["best_ask"] == 0.52
    assert sink.rows[1]["market_id"] == "" and sink.rows[-1]["sample"] == 3


def test_rest_sampler_logs_a_failed_read_and_keeps_going():
    clock = Clock()
    sink, events = ListSink(), ListSink()

    def get(url):
        raise OSError("timeout")
    sampler = r.RestSampler([A], {}, sink, events, every_s=10, deadline_s=5, getter=get, now=clock.now,
                            monotonic=clock.monotonic)
    sampler.run(threading.Event(), lambda s: (setattr(clock, "t", clock.t + s), False)[1])
    assert sink.rows == [] and sampler.errors == 1 and events.rows[0]["event"] == "rest_error"


def test_rest_sampler_stops_on_the_event():
    clock = Clock()
    stop = threading.Event()
    sampler = r.RestSampler([A], {}, ListSink(), ListSink(), every_s=10, deadline_s=1e6,
                            getter=_rest_getter([]), now=clock.now, monotonic=clock.monotonic)
    sampler.run(stop, lambda s: True)
    assert sampler.samples == 1


def test_rest_sampler_keeps_a_sink_failure_for_main():
    class Broken:
        def write(self, row):
            raise OSError("disk full")
    clock = Clock()
    sampler = r.RestSampler([A], {}, Broken(), ListSink(), every_s=10, deadline_s=100, getter=_rest_getter([]),
                            now=clock.now, monotonic=clock.monotonic)
    sampler.run(threading.Event(), lambda s: False)
    assert isinstance(sampler.failure, OSError)


class BeatLog:
    """A heartbeat stand-in that records every publish (issue #82: a sealed sampler publishes none)."""

    def __init__(self):
        self.beats: list[tuple] = []

    def beat(self, state="running", progress=None, *, force=False):
        self.beats.append((state, dict(progress or {}), force))


def test_rest_sampler_a_pre_stopped_run_makes_zero_requests():
    clock = Clock()
    calls: list[str] = []
    stop = threading.Event()
    stop.set()  # stopped before the sampler ever starts
    sampler = r.RestSampler([A, B], {}, ListSink(), ListSink(), every_s=10, deadline_s=1e6,
                            getter=_rest_getter(calls), now=clock.now, monotonic=clock.monotonic)
    sampler.run(stop, lambda s: True)
    assert calls == [] and sampler.samples == 0 and sampler.rows == 0 and sampler.errors == 0


def test_rest_sampler_a_sealed_sampler_makes_zero_requests():
    clock = Clock()
    calls: list[str] = []
    sampler = r.RestSampler([A, B], {}, ListSink(), ListSink(), every_s=10, deadline_s=1e6,
                            getter=_rest_getter(calls), now=clock.now, monotonic=clock.monotonic)
    sampler.seal()  # the outputs are already sealed: nothing may be requested or published
    sampler.sample_once()
    assert calls == [] and sampler.samples == 0 and sampler.rows == 0


def test_rest_sampler_drops_a_result_that_returns_after_the_seal():
    """Issue #82: a request that returns after the stop-seal publishes nothing and asks for nothing more."""
    clock = Clock()
    calls: list[str] = []
    sink, events, beats = ListSink(), ListSink(), BeatLog()
    fetch = _rest_getter(calls)

    def sealing_getter(url):
        sampler.seal()  # the run seals the outputs while this request is still in flight
        return fetch(url)

    sampler = r.RestSampler([A, B], {}, sink, events, every_s=10, deadline_s=1e6, getter=sealing_getter,
                            now=clock.now, monotonic=clock.monotonic, heartbeat=beats)
    sampler.sample_once()
    assert calls == [f"https://clob.polymarket.com/book?token_id={A}"]  # B is never requested
    assert sink.rows == [] and events.rows == [] and beats.beats == []  # row, event and heartbeat unchanged
    assert sampler.samples == 1 and sampler.rows == 0 and sampler.errors == 0


def test_rest_sampler_drops_a_failed_read_that_returns_after_the_seal():
    clock = Clock()
    calls: list[str] = []
    events, beats = ListSink(), BeatLog()

    def failing_getter(url):
        calls.append(url)
        sampler.seal()
        raise OSError("timeout")

    sampler = r.RestSampler([A], {}, ListSink(), events, every_s=10, deadline_s=1e6, getter=failing_getter,
                            now=clock.now, monotonic=clock.monotonic, heartbeat=beats)
    sampler.sample_once()
    assert calls != [] and sampler.errors == 0 and events.rows == [] and beats.beats == []


class BlockingGetter:
    """Task CW (issue #82): a fake /book reader whose ``block_on``-th request blocks in flight until the
    test releases it (threading.Event, no sleeps, no network); ``fail`` makes that request fail instead."""

    def __init__(self, block_on, *, fail=False):
        self.calls: list[str] = []
        self.block_on = block_on
        self.fail = fail
        self.entered = threading.Event()
        self.release = threading.Event()
        self._fetch = _rest_getter(self.calls)

    def __call__(self, url):
        book = self._fetch(url)
        if len(self.calls) == self.block_on:
            self.entered.set()
            assert self.release.wait(30), "the test never released the in-flight request"
            if self.fail:
                raise OSError("timeout")
        return book


def _tree(root):
    """Every file under ``root`` with its exact bytes."""
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


@pytest.mark.parametrize("fail", [False, True], ids=["book", "failed_read"])
def test_rest_sampler_a_blocked_request_returning_after_the_seal_changes_nothing(tmp_path, fail):
    """Issue #82 acceptance 1 + 5 (Task CW): a real sampler thread, real rotating outputs and a real
    heartbeat. The second sample's first request is still in flight when the run seals, closes the
    outputs and writes the finished heartbeat; when it returns afterwards, file bytes, file list,
    counters and the finished heartbeat stay exactly as sealed, and the next asset is never requested."""
    from src.heartbeat import Heartbeat  # local: as in m1_run.main

    clock = Clock()
    run_dir, beat_dir = tmp_path / "run", tmp_path / "heartbeat"
    rest = w.RotatingJsonl(run_dir / "rest", "rest", 1 << 20, 3600.0)
    events = w.RotatingJsonl(run_dir / "events", "events", 1 << 20, 3600.0)
    beats = Heartbeat("m1_rest", every_s=1.0, directory=beat_dir)
    getter = BlockingGetter(3, fail=fail)  # sample 1 reads A and B; sample 2 blocks on A
    sampler = r.RestSampler([A, B], {}, rest, events, every_s=10, deadline_s=1e6, getter=getter,
                            now=clock.now, monotonic=clock.monotonic, heartbeat=beats)

    def wait(seconds):
        clock.t += seconds  # only the sampler thread moves the fake clock
        return False

    thread = threading.Thread(target=sampler.run, args=(threading.Event(), wait), daemon=True)
    thread.start()
    assert getter.entered.wait(30)
    assert sampler.rows == 2 and rest.rows == 2 and beats.seq == 1  # sample 1 was really published

    sealer = threading.Thread(target=sampler.seal, daemon=True)
    sealer.start()
    sealer.join(30)
    assert not sealer.is_alive()  # the seal never waits for the HTTP request in flight
    rest.close()
    events.close()
    assert beats.finish(0) is None
    sealed = (_tree(tmp_path), list(rest.files), list(events.files),
              (sampler.samples, sampler.rows, sampler.errors, rest.rows, events.rows))
    finished = json.loads(beats.path.read_text(encoding="utf-8"))
    assert finished["state"] == "finished" and finished["exit_code"] == 0

    getter.release.set()  # the in-flight request returns only now, after the seal
    thread.join(30)
    assert not thread.is_alive()
    assert getter.calls == [f"https://clob.polymarket.com/book?token_id={token}" for token in (A, B, A)]  # B: never
    assert (_tree(tmp_path), list(rest.files), list(events.files),
            (sampler.samples, sampler.rows, sampler.errors, rest.rows, events.rows)) == sealed
    assert sealed[-1] == (2, 2, 0, 2, 0)
    assert json.loads(beats.path.read_text(encoding="utf-8")) == finished
    assert sampler.failure is None  # nothing even tried to write to the closed outputs


def test_rest_sampler_a_pre_stopped_thread_makes_zero_requests_and_publishes_nothing(tmp_path):
    """Issue #82 acceptance 5 (Task CW): stopped before the sampler thread starts: zero GETs, no
    rows, no events, no heartbeat, no file."""
    from src.heartbeat import Heartbeat

    run_dir = tmp_path / "run"
    rest = w.RotatingJsonl(run_dir / "rest", "rest", 1 << 20, 3600.0)
    events = w.RotatingJsonl(run_dir / "events", "events", 1 << 20, 3600.0)
    beats = Heartbeat("m1_rest", every_s=1.0, directory=tmp_path / "heartbeat")
    getter = BlockingGetter(1)
    stop = threading.Event()
    stop.set()
    sampler = r.RestSampler([A, B], {}, rest, events, every_s=10, deadline_s=1e6, getter=getter,
                            heartbeat=beats)
    thread = threading.Thread(target=sampler.run, args=(stop,), daemon=True)
    thread.start()
    thread.join(30)
    assert not thread.is_alive() and getter.calls == []
    assert (sampler.samples, sampler.rows, sampler.errors, rest.rows, events.rows, beats.seq) == (0, 0, 0, 0, 0, 0)
    assert rest.files == [] and events.files == [] and _tree(tmp_path) == {}


def test_main_seals_the_rest_sampler_before_the_finish_event(tmp_path, monkeypatch):
    seen: list[int] = []
    real_seal = r.RestSampler.seal

    def seal(self):
        seen.append(self.events.rows)  # events published so far: sealing must precede "finish"
        real_seal(self)

    monkeypatch.setattr(r.RestSampler, "seal", seal)
    code, out, transport, calls, clock = _main(tmp_path, [[_book()], []], ["--rest-assets", "1"])
    assert code == r.EXIT_OK and len(seen) == 1
    events = _rows(out, "events")
    assert events[-1]["event"] == "finish" and seen[0] == len(events) - 1


def test_the_rest_join_keeps_the_120_second_wait():
    assert r.REST_JOIN_TIMEOUT_S == 120  # Issue #82: the existing wait is kept; tests shorten it, production never


def test_a_rest_sampler_still_alive_after_the_join_fails_the_run_with_exit_4(tmp_path, monkeypatch, capsys):
    """Issue #82 acceptance 3: the finishing join times out while the REST thread is still in a request.

    The run is failed with EXIT_WRITE_FAILED=4 and an explicit reason (stderr and a ``rest_join_timeout``
    event), never completed / exit 0; the thread is not killed. The join wait is shortened by patching
    REST_JOIN_TIMEOUT_S, so nothing waits 120 seconds.
    """
    monkeypatch.setattr(r, "REST_JOIN_TIMEOUT_S", 0.05)
    entered, release = threading.Event(), threading.Event()
    calls: list[str] = []
    fetch = _rest_getter(calls)

    def blocking_getter(url):
        entered.set()
        release.wait(30)  # stays in flight past the join; released by the test only after main returned
        return fetch(url)

    started = time.monotonic()
    try:
        code, out, _, _, _ = _main(tmp_path, [[lambda: entered.wait(5), _book()], []], ["--rest-assets", "1"],
                                   rest=blocking_getter)
        elapsed = time.monotonic() - started
        alive = [t for t in threading.enumerate() if t.name == "m1-rest-sampler" and t.is_alive()]
        assert alive, "the REST thread must not be killed"
    finally:
        release.set()
    assert elapsed < 30  # the 120 s wait was not served
    assert code == r.EXIT_WRITE_FAILED
    manifest = _manifest(out)
    assert manifest["status"] == "failed" and manifest["exit_code"] == r.EXIT_WRITE_FAILED
    events = _rows(out, "events")
    timeouts = [e for e in events if e["event"] == "rest_join_timeout"]
    assert len(timeouts) == 1 and timeouts[0]["timeout_s"] == 0.05
    assert events[-1] == {"ts": events[-1]["ts"], "event": "finish", "status": "failed",
                          "exit_code": r.EXIT_WRITE_FAILED}
    err = capsys.readouterr().err
    assert "REST sampler still alive" in err
    rest_bytes = {rel: (out / rel).read_bytes() for rel in manifest["files"]["rest"]}
    for thread in alive:
        thread.join(5)  # the late request returns into a sealed sampler: nothing is published
    assert {rel: (out / rel).read_bytes() for rel in manifest["files"]["rest"]} == rest_bytes
    assert manifest["counts"]["rest_rows"] == 0 and _manifest(out) == manifest


def test_a_rest_thread_that_ends_within_the_join_keeps_the_run_completed(tmp_path, monkeypatch):
    monkeypatch.setattr(r, "REST_JOIN_TIMEOUT_S", 5.0)  # returns as soon as the thread ends
    code, out, _, calls, _ = _main(tmp_path, [[_book()], []], ["--rest-assets", "1"])
    assert code == r.EXIT_OK and _manifest(out)["status"] == "completed"
    assert all(e["event"] != "rest_join_timeout" for e in _rows(out, "events"))


# --- recorder hooks (stop, on_event) -----------------------------------------------------------------


def _hooked(sessions, *, runtime=60.0, stop=None):
    clock = Clock()
    transport = FakeTransport(sessions, clock)
    sink, events = ListSink(), ListSink()
    recorder = w.Recorder([A], transport=transport, sink=sink, max_runtime_s=runtime, monotonic=clock.monotonic,
                          sleep=clock.sleep, now=clock.now, stop=stop or (lambda: False), on_event=events.write)
    return recorder.run(), sink, events, clock, transport


def test_events_cover_connects_disconnects_backoff_resyncs_and_live():
    report, sink, events, _, _ = _hooked([[_book(), "CLOSE"], [_book(at=9.0)]], runtime=40)
    kinds = [event["event"] for event in events.rows]
    assert kinds[:3] == ["connected", "live", "disconnected"]
    assert {"backoff", "resync", "end"} <= set(kinds) and kinds.count("connected") == 2 and kinds.count("live") == 2
    resync = next(event for event in events.rows if event["event"] == "resync")
    assert resync == {"ts": resync["ts"], "event": "resync", "asset": A, "reason": "disconnect_closed"}
    assert events.rows[-1]["event"] == "end" and events.rows[-1]["ended_by"] == "deadline"
    assert report.ended_by == "deadline" and all(event["ts"].endswith("Z") for event in events.rows)


def test_a_connect_failure_is_an_event_with_the_error_type():
    _, _, events, _, _ = _hooked([OSError("refused"), [_book()]], runtime=20)
    assert events.rows[0]["event"] == "connect_failed" and events.rows[0]["error"] == "OSError"


def test_gap_resync_and_resubscribe_are_events():
    gap = {"event_type": "price_change", "market": M, "timestamp": str(BASE_MS + 2000),
           "price_changes": [{"asset_id": A, "price": "0.40", "size": "0", "side": "BUY", "hash": "h"}]}
    _, _, events, _, _ = _hooked([[_book(), gap, _book(at=3.0)]], runtime=20)
    kinds = [(event["event"], event.get("reason") or event.get("assets")) for event in events.rows]
    assert kinds[1:5] == [("live", None), ("resync", "unknown_level_removed"), ("resubscribe", [A]), ("live", None)]


def test_stop_ends_the_session_without_invalidation_rows():
    flag = {"stop": False}
    report, sink, events, clock, transport = _hooked([[_book(), lambda: flag.update(stop=True)]], runtime=1000,
                                                     stop=lambda: flag["stop"])
    assert report.ended_by == "stopped" and report.disconnects == {}
    assert [row["ws_state"] for row in sink.rows] == ["live"]
    assert clock.t < 10 and events.rows[-1] == {"ts": events.rows[-1]["ts"], "event": "end", "ended_by": "stopped"}


def test_with_a_stop_callback_receive_waits_are_capped():
    _, _, _, _, transport = _hooked([[_book()]], runtime=30)
    assert transport.timeouts and max(transport.timeouts) <= WS_STOP_POLL_S


def test_stop_during_a_long_backoff_returns_within_the_poll_interval():
    clock_box = {}
    flag = {"n": 0}

    def stop():
        clock = clock_box.get("clock")
        return clock is not None and clock.t >= 3.0

    clock = Clock()
    clock_box["clock"] = clock
    transport = FakeTransport([OSError("x")] * 20, clock)
    recorder = w.Recorder([A], transport=transport, sink=ListSink(), max_runtime_s=10_000, monotonic=clock.monotonic,
                          sleep=clock.sleep, now=clock.now, stop=stop, on_event=lambda e: flag.update(n=flag["n"] + 1))
    report = recorder.run()
    assert report.ended_by == "stopped" and clock.t < 3.0 + WS_STOP_POLL_S + 1e-9
    assert all(s <= WS_STOP_POLL_S for s in clock.sleeps)


def test_without_hooks_the_recorder_still_sleeps_whole_backoffs():
    clock = Clock()
    transport = FakeTransport([OSError("x"), OSError("x"), OSError("x")], clock)
    recorder = w.Recorder([A], transport=transport, sink=ListSink(), max_runtime_s=6.5, monotonic=clock.monotonic,
                          sleep=clock.sleep, now=clock.now)
    report = recorder.run()
    assert clock.sleeps[:2] == [1.0, 2.0] and report.ended_by == "deadline"


# --- the run (CLI) -----------------------------------------------------------------------------------


def test_a_full_run_writes_books_events_rest_and_a_finished_manifest(tmp_path, capsys):
    sessions = [[_book(A), _book(B, bid="0.40", ask="0.60"), "CLOSE"], [_book(A, at=9.0), _book(B, at=9.0)]]
    code, out, transport, calls, clock = _main(tmp_path, sessions, ["--top", "1", "--rest-assets", "1"])
    assert code == r.EXIT_OK and transport.urls == [w.MARKET_WS_URL, w.MARKET_WS_URL]
    manifest = _manifest(out)
    assert manifest["schema"] == "m1_run/1" and manifest["status"] == "completed" and manifest["exit_code"] == 0
    assert [asset["token_id"] for asset in manifest["assets"]] == [A, B]
    assert manifest["assets"][0]["market_id"] == "m1" and manifest["assets"][1]["outcome"] == "No"
    assert manifest["rest_assets"] == [A] and calls == [f"https://clob.polymarket.com/book?token_id={A}"]
    assert manifest["started_at"] == "2026-10-09T00:00:00.000Z" and manifest["ended_at"] >= "2026-10-09T00:01:00"
    assert manifest["elapsed_s"] == pytest.approx(60.0, abs=0.1) and manifest["max_runtime_s"] == 60
    assert manifest["git"] == _git() and manifest["orders"] == "none"
    assert manifest["risk"]["pinned_sha256"] == r.PINNED_RISK_SHA256
    assert manifest["risk"]["start"]["sha256"] == r.PINNED_RISK_SHA256 == manifest["risk"]["end"]["sha256"]
    assert manifest["risk"]["start"]["live_enabled"] is False
    assert manifest["version"]["schema"] == "m1_run/1" and manifest["version"]["python"]
    assert manifest["selection"]["scan_ts"] == "2026-10-09T00:00:00Z" and manifest["selection"]["top"] == 1
    assert manifest["thresholds"]["MAX_BOOK_AGE_S"] == 120.0 and "M1_MIN_COVERAGE" in manifest["thresholds"]
    books = _rows(out, "books")
    assert [row["ws_state"] for row in books] == ["live", "live", "resync", "resync", "live", "live"]
    assert manifest["counts"]["book_rows"] == 6 and manifest["counts"]["connects"] == 2
    assert manifest["counts"]["disconnects"] == {"closed": 1} and manifest["counts"]["ended_by"] == "deadline"
    events = _rows(out, "events")
    assert events[0]["event"] == "start" and events[-1] == {"ts": events[-1]["ts"], "event": "finish",
                                                            "status": "completed", "exit_code": 0}
    assert sum(1 for event in events if event["event"] == "resync") == 2
    rest = _rows(out, "rest")
    assert len(rest) == 1 and rest[0]["token_id"] == A and rest[0]["source"] != w.SOURCE
    assert not list(out.glob(".*.tmp")) and "orders=none" in capsys.readouterr().out


def test_the_manifest_says_running_while_the_run_is_going(tmp_path):
    seen = {}

    def peek():
        seen.update(json.loads((tmp_path / "run" / "manifest.json").read_text(encoding="utf-8")))
    _main(tmp_path, [[_book(A), peek]], ["--assets", A])
    assert seen["status"] == "running" and seen["ended_at"] is None and seen["exit_code"] is None


@pytest.mark.parametrize("signame", ["SIGTERM", "SIGINT"])
def test_a_signal_stops_the_run_gracefully_and_restores_handlers(tmp_path, signame):
    signum = getattr(signal, signame)
    before = signal.getsignal(signum)

    def deliver():
        handler = signal.getsignal(signum)
        assert callable(handler) and handler is not before
        handler(signum, None)

    code, out, _, _, clock = _main(tmp_path, [[_book(A), _book(B), deliver]], ["--max-runtime", "86400"])
    assert code == r.EXIT_INTERRUPTED and clock.t < 10
    manifest = _manifest(out)
    assert manifest["status"] == "interrupted" and manifest["stop_signal"] == signame
    assert manifest["counts"]["ended_by"] == "stopped" and manifest["exit_code"] == r.EXIT_INTERRUPTED
    assert [row["ws_state"] for row in _rows(out, "books")] == ["live", "live"]  # no invalidation on a stop
    assert _rows(out, "events")[-1]["status"] == "interrupted"
    assert signal.getsignal(signum) is before


def test_a_stop_event_without_signal_handlers(tmp_path):
    stop = threading.Event()
    code, out, _, _, _ = _main(tmp_path, [[_book(A), stop.set]], ["--assets", A], stop_event=stop,
                               install_signals=False)
    assert code == r.EXIT_INTERRUPTED and _manifest(out)["stop_signal"] is None


def test_a_pre_stopped_run_makes_zero_rest_requests(tmp_path):
    """Issue #82 acceptance 5 (Task CW): a run stopped before it starts sends no REST /book request,
    writes no REST row and no m1_rest heartbeat."""
    from src import heartbeat as hb_module

    stop = threading.Event()
    stop.set()
    code, out, _, calls, _ = _main(tmp_path, [[_book(A)]], ["--assets", A, "--rest-assets", "1"],
                                   stop_event=stop, install_signals=False)
    counts = _manifest(out)["counts"]
    assert code == r.EXIT_INTERRUPTED and calls == []
    assert counts["rest_samples"] == 0 and counts["rest_rows"] == 0 and counts["rest_errors"] == 0
    assert _rows(out, "rest") == [] and not hb_module.beat_path("m1_rest").exists()


def test_files_rotate_during_a_run(tmp_path):
    many = [_book(A, bid=f"0.{40 + i % 10}", at=1.0 + i) for i in range(30)]
    code, out, _, _, _ = _main(tmp_path, [many], ["--assets", A, "--rotate-mb", "0.001", "--rotate-s", "5"])
    manifest = _manifest(out)
    assert code == r.EXIT_OK and len(manifest["files"]["books"]) > 1
    assert len(_rows(out, "books")) == manifest["counts"]["book_rows"] > 1
    assert manifest["settings"]["rotate_bytes"] == 1048 and manifest["settings"]["rotate_s"] == 5


def test_explicit_assets_override_the_selection_and_keep_metadata(tmp_path):
    code, out, transport, _, _ = _main(tmp_path, [[_book(C)]], ["--assets", f"{C}", "--rest-assets", "0"])
    manifest = _manifest(out)
    assert code == r.EXIT_OK and json.loads(transport.sent[0])["assets_ids"] == [C]
    assert manifest["assets"][0]["market_id"] == "m2" and manifest["selection"]["explicit_assets"] is True
    assert manifest["rest_assets"] == [] and manifest["counts"]["rest_rows"] == 0


def test_no_snapshot_exits_1(tmp_path):
    code, out, _, _, _ = _main(tmp_path, [], ["--assets", A])
    assert code == r.EXIT_NO_ROWS and _manifest(out)["status"] == "no_snapshot"


@pytest.mark.parametrize("argv", [["--max-runtime", "0"], ["--max-runtime", "nan"], ["--max-runtime", "inf"],
                                  ["--top", "0"], ["--rest-every", "0"], ["--rest-assets", "-1"],
                                  ["--rotate-mb", "0"], ["--rotate-s", "-1"], ["--assets", ","], ["--bogus"]])
def test_usage_errors_exit_2_before_anything_is_written(tmp_path, argv):
    code, out, transport, calls, _ = _main(tmp_path, [[_book()]], argv)
    assert code == r.EXIT_USAGE and transport.urls == [] and calls == [] and not out.exists()


def test_no_usable_assets_exit_2(tmp_path):
    markets = _markets_file(tmp_path, [_market("m1", [A, B], token_ids_problem="x")])
    code, out, transport, _, _ = _main(tmp_path, [[_book()]], markets=markets)
    assert code == r.EXIT_USAGE and transport.urls == [] and not out.exists()


def test_a_non_empty_out_dir_is_refused(tmp_path):
    (tmp_path / "run").mkdir()
    (tmp_path / "run" / "old.txt").write_text("x", encoding="utf-8")
    code, _, transport, _, _ = _main(tmp_path, [[_book()]], ["--assets", A])
    assert code == r.EXIT_USAGE and transport.urls == []


def test_an_empty_existing_out_dir_is_fine(tmp_path):
    (tmp_path / "run").mkdir()
    code, _, _, _, _ = _main(tmp_path, [[_book()]], ["--assets", A])
    assert code == r.EXIT_OK


def test_missing_websockets_exits_3_before_creating_the_run_dir(tmp_path, monkeypatch):
    for name in ("websockets", "websockets.sync", "websockets.sync.client", "websockets.exceptions"):
        monkeypatch.setitem(sys.modules, name, None)
    out = tmp_path / "run"
    code = r.main(["--out", str(out), "--markets", str(_markets_file(tmp_path)), "--max-runtime", "5"])
    assert code == r.EXIT_NO_WEBSOCKETS and not out.exists()


def test_a_transport_that_turns_out_unavailable_exits_3(tmp_path):
    code, out, _, _, _ = _main(tmp_path, [w.TransportUnavailable("no websockets")], ["--assets", A])
    assert code == r.EXIT_NO_WEBSOCKETS and _manifest(out)["status"] == "failed"


def test_a_book_write_failure_exits_4(tmp_path, monkeypatch):
    real = r.RotatingJsonl.write

    def write(self, row):
        if self.prefix == "books":
            raise OSError("disk full")
        real(self, row)
    monkeypatch.setattr(r.RotatingJsonl, "write", write)
    code, out, _, _, _ = _main(tmp_path, [[_book()]], ["--assets", A])
    assert code == r.EXIT_WRITE_FAILED and _manifest(out)["status"] == "failed"


def test_a_rest_write_failure_exits_4(tmp_path, monkeypatch):
    real = r.RotatingJsonl.write

    def write(self, row):
        if self.prefix == "rest":
            raise OSError("disk full")
        real(self, row)
    monkeypatch.setattr(r.RotatingJsonl, "write", write)
    code, out, _, _, _ = _main(tmp_path, [[_book()]], ["--assets", A])
    assert code == r.EXIT_WRITE_FAILED and _manifest(out)["status"] == "failed"


def test_risk_state_normalizes_line_endings_and_reads_live_enabled(tmp_path):
    lf = (ROOT / "config" / "risk.toml").read_bytes().replace(b"\r\n", b"\n")
    crlf = tmp_path / "crlf.toml"
    crlf.write_bytes(lf.replace(b"\n", b"\r\n"))
    assert r.risk_state(crlf) == {"sha256": r.PINNED_RISK_SHA256, "live_enabled": False}
    assert r.risk_state(tmp_path / "missing.toml")["sha256"] is None
    edited = tmp_path / "edited.toml"
    edited.write_bytes(lf.replace(b"live_enabled = false", b"live_enabled = true"))
    state = r.risk_state(edited)
    assert state["sha256"] != r.PINNED_RISK_SHA256 and state["live_enabled"] is True


def test_git_state_reads_a_real_repo_or_says_unknown(tmp_path):
    assert r.git_state(tmp_path) == {"sha": None, "dirty": None}


def test_utc_now_ms_is_parseable_with_milliseconds():
    from src.book_staleness import parse_book_ts
    stamp = r.utc_now_ms()
    assert stamp.endswith("Z") and len(stamp.split(".")[1]) == 4 and parse_book_ts(stamp) is not None


# --- read-only by construction --------------------------------------------------------------------------


def test_defaults_come_from_thresholds():
    defaults = vars(r.build_parser().parse_args(["--out", "x", "--max-runtime", "1"]))
    assert defaults["top"] == M1_TOP_MARKETS and defaults["rest_every"] == M1_REST_EVERY_S
    assert defaults["rest_assets"] == M1_REST_ASSETS and defaults["rotate_s"] == M1_ROTATE_S
    assert int(defaults["rotate_mb"] * 1048576) == M1_ROTATE_BYTES


def test_max_runtime_defaults_to_the_6h_target():
    """KEY-approved 2026-10-11 (家伟批准): the acceptance run is 6 h; M1_MIN_RUN_S is the target minus 60 s."""
    from src.thresholds import M1_MIN_RUN_S, M1_RUN_TARGET_S
    assert r.build_parser().parse_args(["--out", "x"]).max_runtime == M1_RUN_TARGET_S == 21600.0
    assert M1_MIN_RUN_S == M1_RUN_TARGET_S - 60.0 == 21540.0


@pytest.mark.parametrize("module", ["m1_run", "m1_report"])
def test_m1_modules_have_no_urls_no_websockets_import_and_no_order_code(module):
    text = (ROOT / "src" / f"{module}.py").read_text(encoding="utf-8")
    tree = ast.parse(text)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert "websockets" not in imported and not imported & {"requests", "urllib", "http", "socket", "py_clob_client"}
    literals = [node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)]
    assert not any("://" in value for value in literals)
    lowered = text.lower()
    for word in ("private_key", "api_key", "secret", "passphrase", "post_order", "create_order", "signature"):
        assert word not in lowered, word


def test_importing_m1_modules_does_not_import_websockets():
    code = "import sys; import src.m1_run, src.m1_report; print('websockets' in sys.modules)"
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert result.stdout.strip() == "False", result.stderr


def test_module_entry_point_help_runs():
    result = subprocess.run([sys.executable, "-m", "src.m1_run", "--help"], cwd=ROOT, capture_output=True,
                            text=True, timeout=60)
    assert result.returncode == 0 and "Exit codes" in result.stdout and "no orders" in result.stdout


def test_live_is_an_event_only_on_the_transition():
    delta = {"event_type": "price_change", "market": M, "timestamp": str(BASE_MS + 2000),
             "price_changes": [{"asset_id": A, "price": "0.49", "size": "10", "side": "BUY", "hash": "h"}]}
    _, sink, events, _, _ = _hooked([[_book(), delta, delta]], runtime=20)
    kinds = [event["event"] for event in events.rows if event["event"] not in ("ping", "pong")]  # Task BL: per frame
    assert len(sink.rows) == 2 and kinds == ["connected", "live", "ping_latency", "end"]


def test_an_event_write_failure_propagates_and_still_closes_the_connection():
    clock = Clock()
    closed = []

    class Conn(FakeConnection):
        def close(self):
            closed.append(True)

    class Transport(FakeTransport):
        def connect(self, url, timeout):
            return Conn([_book()], self.clock, self.sent, self.timeouts)

    def broken(event):
        raise OSError("disk full")
    recorder = w.Recorder([A], transport=Transport([], clock), sink=ListSink(), max_runtime_s=10,
                          monotonic=clock.monotonic, sleep=clock.sleep, now=clock.now, on_event=broken)
    with pytest.raises(OSError):
        recorder.run()
    assert closed == [True]


def test_the_risk_pin_is_the_guard_pin():
    from scripts import guard  # Task AH: the M0 guard owns the pin; M1 code keeps a checked copy (src never imports scripts)
    assert r.PINNED_RISK_SHA256 == guard.PINNED_RISK_SHA256


# --- Task AS (gap 5): the runtime pin check -------------------------------------------------------


def _drift_after(monkeypatch, tmp_path, clean_checks):
    import src.podlib as podlib
    from src import pins

    calls = {"n": 0}

    def checked(risk_path):
        calls["n"] += 1
        return [] if calls["n"] <= clean_checks else ["unknown_key:extra"]
    monkeypatch.setattr(pins, "_checked", checked)
    monkeypatch.setattr(podlib, "BUS_PATH", tmp_path / "bus.jsonl")
    return calls


def test_a_risk_drift_mid_run_stops_the_run_with_6(tmp_path, monkeypatch):
    import src.halt_store as halt_store
    from src.thresholds import PIN_RECHECK_S

    calls = _drift_after(monkeypatch, tmp_path, 2)
    code, out, _, _, clock = _main(tmp_path, [[_book()]], ["--assets", A, "--max-runtime", "600"])
    assert code == r.EXIT_PIN_DRIFT == 6 and calls["n"] == 3
    assert PIN_RECHECK_S <= clock.t < 600
    manifest = _manifest(out)
    assert manifest["status"] == "pin_drift" and manifest["exit_code"] == 6 and manifest["orders"] == "none"
    assert len(_rows(out, "books")) == 1
    assert [x["reason"] for x in halt_store.active_halts()] == ["risk_toml_pin_drift"]


def test_a_risk_drift_keeps_6_when_the_rest_thread_also_outlives_the_join(tmp_path, monkeypatch):
    """Issue #82 acceptance 3 does not mask a pin drift: the stuck REST thread is recorded, the run stays 6."""
    _drift_after(monkeypatch, tmp_path, 2)
    monkeypatch.setattr(r, "REST_JOIN_TIMEOUT_S", 0.05)
    entered, release = threading.Event(), threading.Event()

    def blocking_getter(url):
        entered.set()
        release.wait(30)
        return None

    try:
        code, out, _, _, _ = _main(tmp_path, [[lambda: entered.wait(5), _book()]],
                                   ["--assets", A, "--max-runtime", "600", "--rest-assets", "1"], rest=blocking_getter)
    finally:
        release.set()
    manifest = _manifest(out)
    assert code == r.EXIT_PIN_DRIFT and manifest["status"] == "pin_drift" and manifest["exit_code"] == 6
    assert [e["event"] for e in _rows(out, "events")].count("rest_join_timeout") == 1


def test_a_risk_drift_at_start_creates_no_run_dir(tmp_path, monkeypatch):
    _drift_after(monkeypatch, tmp_path, 0)
    code, out, transport, calls, _ = _main(tmp_path, [[_book()]], ["--assets", A])
    assert code == r.EXIT_PIN_DRIFT and not out.exists() and transport.urls == [] and calls == []
