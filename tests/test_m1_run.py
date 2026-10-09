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
    def __init__(self, script, clock, sent, timeouts):
        self.script = list(script)
        self.clock = clock
        self.sent = sent
        self.timeouts = timeouts

    def send(self, text):
        self.sent.append(text)

    def recv(self, timeout):
        assert timeout >= 0
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


def _git():
    return {"sha": "f" * 40, "dirty": False}


def _main(tmp_path, sessions, argv=(), *, rest=None, clock=None, markets=None, **kwargs):
    clock = clock or Clock()
    transport = FakeTransport(sessions, clock)
    calls: list[str] = []
    out = tmp_path / "run"
    full = ["--out", str(out), "--markets", str(markets or _markets_file(tmp_path)), "--max-runtime", "60", *argv]
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
    assert len(sink.rows) == 2 and [event["event"] for event in events.rows] == ["connected", "live", "end"]


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


def test_a_risk_drift_at_start_creates_no_run_dir(tmp_path, monkeypatch):
    _drift_after(monkeypatch, tmp_path, 0)
    code, out, transport, calls, _ = _main(tmp_path, [[_book()]], ["--assets", A])
    assert code == r.EXIT_PIN_DRIFT and not out.exists() and transport.urls == [] and calls == []
