"""Task AP: heartbeats from the loops (ws_recorder, m1_run, paper_cycle), the recorder escalation
window (gap 13), paper_cycle --recording-dir (gap 11) and the all-markets-stale halt (gap 14). Offline."""
from __future__ import annotations

import json

import pytest

from src import halt_store, heartbeat, paper_cycle, sentinel
from src import m1_run as r
from src import ws_recorder as w
from src.podlib import read_jsonl
from src.thresholds import (ALL_STALE_CONSECUTIVE_CYCLES, HEARTBEAT_PAPER_CYCLE_EVERY_S, HEARTBEAT_RECORDER_EVERY_S,
                            RECORDER_ESCALATION_WINDOW_S, RECORDER_GAP_RESYNC_HALT_COUNT,
                            RECORDER_RECONNECT_ALERT_COUNT)
from tests.test_m1_run import Clock as M1Clock, _main as m1_main, _rest_getter
from tests.test_ws_recorder import TS, A, B, Clock, FakeTransport, ListSink, _book


def read(proc):
    record, problem = heartbeat.read_beat(heartbeat.beat_path(proc), proc)
    assert problem is None
    return record


class Beats:
    """A fake Heartbeat that records every beat."""

    def __init__(self):
        self.beats: list[tuple[str, dict, bool]] = []
        self.seq = 0

    def beat(self, state="running", progress=None, *, force=False):
        self.seq += 1
        self.beats.append((state, dict(progress or {}), force))


# ---------------------------------------------------------------- escalation window

def test_escalation_counts_in_a_sliding_window():
    t = [0.0]
    esc = w.Escalation(monotonic=lambda: t[0])
    assert esc.window_s == RECORDER_ESCALATION_WINDOW_S
    hits = [esc.reconnect() for _ in range(RECORDER_RECONNECT_ALERT_COUNT)]
    assert hits[-1] is True and not any(hits[:-1])
    t[0] = RECORDER_ESCALATION_WINDOW_S + 1
    assert esc.snapshot()["reconnects_in_window"] == 0
    hits = [esc.gap_resync(A) for _ in range(RECORDER_GAP_RESYNC_HALT_COUNT)]
    assert hits[-1] is True and not any(hits[:-1])
    esc.gap_resync(B)
    assert esc.snapshot()["gap_resyncs_in_window"] == {A: RECORDER_GAP_RESYNC_HALT_COUNT, B: 1}
    t[0] += RECORDER_ESCALATION_WINDOW_S + 1
    assert esc.snapshot()["gap_resyncs_in_window"] == {}


def _recorder(sessions=(), *, heartbeat_=None, meta=None, runtime=100.0, stop=None):
    clock = Clock()
    rec = w.Recorder([A, B], transport=FakeTransport(list(sessions), clock), sink=ListSink(), max_runtime_s=runtime,
                     meta=meta, monotonic=clock.monotonic, sleep=clock.sleep, now=lambda: TS, stop=stop,
                     heartbeat=heartbeat_)
    return rec, clock


def test_gap_resyncs_count_but_disconnect_resyncs_do_not():
    beats = Beats()
    rec, _ = _recorder(heartbeat_=beats, meta={A: {"market_id": "m-a"}})
    rec._write(w.Outcome(resyncs=[(A, "disconnect_closed"), (B, "disconnect_timeout")]))
    assert rec.progress()["gap_resyncs_in_window"] == {}
    for _ in range(RECORDER_GAP_RESYNC_HALT_COUNT - 1):
        rec._write(w.Outcome(resyncs=[(A, "invalid_price")]))
    assert beats.beats == []
    rec._write(w.Outcome(resyncs=[(A, "timestamp_regression"), (B, "invalid_size")]))
    state, progress, force = beats.beats[-1]
    assert force and progress["gap_resyncs_in_window"] == {A: RECORDER_GAP_RESYNC_HALT_COUNT, B: 1}
    assert progress["asset_markets"] == {A: "m-a", B: ""}


def test_reconnects_beat_paused_while_backing_off_and_force_a_beat_at_the_count():
    beats = Beats()
    rec, clock = _recorder([], heartbeat_=beats, runtime=120.0)  # every connect fails
    report = rec.run()
    assert report.connect_failures >= RECORDER_RECONNECT_ALERT_COUNT
    states = [b[0] for b in beats.beats]
    assert states[0] == "running" and "paused" in states
    assert any(b[1]["reconnects_in_window"] >= RECORDER_RECONNECT_ALERT_COUNT and b[2] for b in beats.beats)
    assert max(clock.sleeps) <= HEARTBEAT_RECORDER_EVERY_S  # long backoffs are sliced so the beat keeps going


def test_without_a_heartbeat_the_backoff_sleeps_as_before():
    rec, clock = _recorder([], runtime=60.0)
    rec.run()
    assert clock.sleeps == rec.report.backoffs


def test_the_session_loop_beats():
    beats = Beats()
    rec, _ = _recorder([[_book(A), _book(B)]], heartbeat_=beats, runtime=60.0)
    rec.run()
    assert beats.beats and beats.beats[-1][1]["rows"] == 2 and beats.beats[-1][1]["assets_live"] == 2


def test_ws_recorder_main_writes_start_and_finish(tmp_path, capsys):
    clock = Clock()
    code = w.main(["--assets", A, "--max-runtime", "0", "--out", str(tmp_path / "b.jsonl")],
                  transport=FakeTransport([], clock))
    assert code == 2
    record = read("ws_recorder")
    assert record["state"] == "finished" and record["exit_code"] == 2


def test_ws_recorder_main_records_a_crash(monkeypatch):
    def boom(*args, **kwargs):
        raise MemoryError
    monkeypatch.setattr(w, "_main", boom)
    with pytest.raises(MemoryError):
        w.main(["--assets", A])
    assert read("ws_recorder")["state"] == "crashed"


# ---------------------------------------------------------------- m1_run

def test_m1_run_beats_both_heartbeats_and_finishes(tmp_path, capsys):
    calls: list[str] = []
    code, out, transport, calls, clock = m1_main(tmp_path, [], argv=["--max-runtime", "30"],
                                                 rest=_rest_getter(calls), install_signals=False)
    run, rest = read("m1_run"), read("m1_rest")
    assert run["state"] == "finished" and run["exit_code"] == code
    assert rest["state"] == "finished" and rest["progress"].get("samples", 0) >= 0
    assert rest["exit_code"] == (0 if code in (r.EXIT_OK, r.EXIT_NO_ROWS, r.EXIT_INTERRUPTED) else code)


def test_m1_run_usage_error_has_no_rest_heartbeat(tmp_path, capsys):
    assert r.main(["--bogus"]) == 2
    assert read("m1_run")["exit_code"] == 2
    assert not heartbeat.beat_path("m1_rest").exists()


def test_m1_run_crash_is_recorded(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError
    monkeypatch.setattr(r, "_main", boom)
    with pytest.raises(RuntimeError):
        r.main([])
    assert read("m1_run")["state"] == "crashed"


def test_rest_sampler_beats_after_every_sample():
    beats = Beats()
    sampler = r.RestSampler([], {}, ListSink(), ListSink(), every_s=10, deadline_s=60, getter=lambda url: {},
                            heartbeat=beats)
    sampler.sample_once()
    sampler.sample_once()
    assert [b[1]["samples"] for b in beats.beats] == [1, 2] and all(b[2] for b in beats.beats)


# ---------------------------------------------------------------- paper_cycle: recording dir (gap 11)

def _write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


NOW = "2026-10-09T00:10:00Z"
FRESH = "2026-10-09T00:09:55Z"
OLD = "2026-10-09T00:00:00Z"


@pytest.fixture()
def cycle(tmp_path):
    markets = tmp_path / "markets.jsonl"
    _write(markets, [{"ts": OLD, "id": "m1", "token_ids": ["t1", "t2"]}, {"ts": OLD, "id": "m2", "token_ids": ["t3"]}])

    def run(**kw):
        kw.setdefault("books_path", tmp_path / "books.jsonl")
        return paper_cycle.run(markets_path=markets, bus_path=tmp_path / "bus.jsonl", now=NOW,
                               sentinel_state_path=tmp_path / "state.json", quotes_path=tmp_path / "quotes.jsonl",
                               **kw)
    run.tmp = tmp_path
    return run


def _rows(ts, tokens=(("m1", "t1"), ("m1", "t2"), ("m2", "t3"))):
    return [{"ts": ts, "book_ts": ts, "market_id": m, "token_id": t, "best_ask": 0.4, "best_ask_size": 10}
            for m, t in tokens]


def test_a_recording_dir_reads_every_rotated_book_file(cycle):
    rec = cycle.tmp / "run"
    _write(rec / "books" / "books-000001.jsonl", _rows(OLD))
    _write(rec / "books" / "books-000002.jsonl", _rows(FRESH))
    _write(rec / "rest" / "rest-000001.jsonl", _rows(OLD))  # not a book file
    assert [p.name for p in paper_cycle.recording_book_files(rec)] == ["books-000001.jsonl", "books-000002.jsonl"]
    result = cycle(recording_dir=rec)
    assert result["stale"] == 0 and result["books_problem"] is None and result["sentinel"]["checked"] == 2


def test_an_empty_recording_dir_is_stale_not_skipped(cycle):
    result = cycle(recording_dir=cycle.tmp / "empty")
    assert result["books_problem"] == paper_cycle.RECORDING_EMPTY and result["stale"] == 2


# ---------------------------------------------------------------- paper_cycle: all markets stale (gap 14)

def test_all_markets_stale_for_two_cycles_is_a_global_halt_and_again_after_a_resume(cycle):
    _write(cycle.tmp / "books.jsonl", _rows(OLD))
    first = cycle()
    assert first["sentinel"]["all_stale_streak"] == 1 and not first["sentinel"]["all_stale_halt"]
    second = cycle()
    assert second["sentinel"]["all_stale_streak"] == ALL_STALE_CONSECUTIVE_CYCLES
    assert second["sentinel"]["all_stale_halt"]
    globals_ = [h for h in halt_store.active_halts() if h["scope"] == "global"]
    assert len(globals_) == 1 and globals_[0]["reason"].startswith(paper_cycle.ALL_STALE_ALARM)
    assert not cycle()["sentinel"]["all_stale_halt"]  # no duplicate
    halt_store.resume(from_role="Operator", scope="global", halt_id=globals_[0]["id"], approved_by="op",
                      root_cause="x", postmortem="PR #0", bus_path=cycle.tmp / "bus.jsonl")
    assert cycle()["sentinel"]["all_stale_halt"]  # still all stale: halted again on the next cycle


def test_one_fresh_market_resets_the_all_stale_streak(cycle):
    _write(cycle.tmp / "books.jsonl", _rows(OLD))
    cycle()
    _write(cycle.tmp / "books.jsonl", _rows(OLD, (("m1", "t1"), ("m1", "t2"))) + _rows(FRESH, (("m2", "t3"),)))
    result = cycle()
    assert result["sentinel"]["all_stale_streak"] == 0 and not result["sentinel"]["all_stale_halt"]
    assert not [h for h in halt_store.active_halts() if h["scope"] == "global"]


def test_state_without_the_streak_key_loads_as_zero_and_a_bad_one_is_corrupt(tmp_path):
    watch = sentinel.StaleWatch()
    watch.all_stale_streak = 1
    data = watch.to_dict()
    assert data["all_stale_streak"] == 1
    del data["all_stale_streak"]
    assert sentinel.StaleWatch.from_dict(data).all_stale_streak == 0
    for bad in (-1, True, "1", 1.5):
        data["all_stale_streak"] = bad
        with pytest.raises((ValueError, TypeError)):
            sentinel.StaleWatch.from_dict(data)


# ---------------------------------------------------------------- paper_cycle CLI

def test_paper_cycle_loop_beats_every_cycle(cycle, monkeypatch, capsys):
    _write(cycle.tmp / "books.jsonl", _rows(FRESH))
    monkeypatch.setattr(paper_cycle, "MARKETS_PATH", cycle.tmp / "markets.jsonl")
    seen = []
    real_run = paper_cycle.run

    def run(**kw):
        seen.append(read("paper_cycle")["progress"]["cycles"])
        return real_run(markets_path=cycle.tmp / "markets.jsonl", bus_path=cycle.tmp / "bus.jsonl", now=NOW,
                        sentinel_state_path=cycle.tmp / "state.json", quotes_path=cycle.tmp / "quotes.jsonl", **kw)
    monkeypatch.setattr(paper_cycle, "run", run)
    sleeps = []
    code = paper_cycle.main(["--books", str(cycle.tmp / "books.jsonl"), "--loop", "--max-cycles", "3"],
                            sleep=sleeps.append, monotonic=lambda: 0.0)
    assert code == 0 and seen == [0, 1, 2] and sleeps == [HEARTBEAT_PAPER_CYCLE_EVERY_S] * 2
    final = read("paper_cycle")
    assert final["state"] == "finished" and final["exit_code"] == 0 and final["progress"]["cycles"] == 3


def test_paper_cycle_crash_is_recorded(monkeypatch, capsys):
    def boom(**kw):
        raise ValueError
    monkeypatch.setattr(paper_cycle, "run", boom)
    with pytest.raises(ValueError):
        paper_cycle.main([])
    assert read("paper_cycle")["state"] == "crashed"


def test_paper_cycle_pin_failure_finishes_1(monkeypatch, capsys):
    monkeypatch.setattr(paper_cycle, "run", lambda **kw: {"ok": False, "reason": "phase0_pin", "signals": 0,
                                                         "gaps": 0, "stale": 0, "fills": 0})
    assert paper_cycle.main([]) == 1
    assert read("paper_cycle")["exit_code"] == 1


@pytest.mark.parametrize("argv", [["--every", "61"], ["--every", "0"], ["--max-cycles", "-1"],
                                  ["--books", "a", "--recording-dir", "b"], ["--nope"]])
def test_paper_cycle_usage_errors(argv, capsys):
    assert paper_cycle.main(argv) == 2
    assert not heartbeat.beat_path("paper_cycle").exists()
