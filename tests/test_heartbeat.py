"""Task AP: heartbeat files (src/heartbeat.py), the consumers' lost-Sentinel halt (src/halt_store.py),
StaleWatch re-alarm after an Operator resume, and the Reconciler halt in the halt store. Offline."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src import halt_store, heartbeat, podlib, reconcile, risk_gate, sentinel, sentinel_tick
from src.podlib import read_jsonl
from src.thresholds import SENTINEL_HEARTBEAT_TIMEOUT_S

PIN = "0aea23af1e938d330b5b4fab2c6abdec9f475f16807d177a5860a4da32aebc04"
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)


def record(proc="m1_run", ts=NOW, **extra):
    if not isinstance(ts, datetime):
        extra["ts"], ts = ts, NOW
    data = {"proc": proc, "pid": 7, "host": "h", "started_at": heartbeat.utc_stamp(ts), "ts": heartbeat.utc_stamp(ts),
            "seq": 1, "git_sha": None, "risk_sha256": PIN, "live_enabled": False, "mode": "paper",
            "state": "running", "exit_code": None, "progress": {}}
    data.update(extra)
    return data


def write(proc, **extra):
    path = heartbeat.beat_path(proc)
    assert heartbeat.write_record(path, record(proc, **extra)) is None
    return path


# ---------------------------------------------------------------- heartbeat files

def test_default_dir_is_under_logs_and_tests_are_isolated():
    from src.podlib import LOG_DIR
    assert heartbeat.HEARTBEAT_DIR != LOG_DIR / "heartbeat"  # conftest isolation
    assert heartbeat.beat_path("scanner").name == "scanner.json"


@pytest.mark.parametrize("bad", ["", "../x", "a/b", "a.b", 3])
def test_bad_proc_names_are_refused(bad):
    with pytest.raises(ValueError):
        heartbeat.beat_path(bad)


def test_writer_writes_every_field_atomically_with_a_marker(tmp_path):
    hb = heartbeat.Heartbeat("ws_recorder", every_s=10, directory=tmp_path, now=lambda: NOW)
    assert hb.beat("running", {"rows": 3}, force=True) is None
    data = json.loads((tmp_path / "ws_recorder.json").read_text(encoding="utf-8"))
    assert set(data) == set(heartbeat.FIELDS)
    assert data["proc"] == "ws_recorder" and data["seq"] == 1 and data["progress"] == {"rows": 3}
    assert data["risk_sha256"] == PIN and data["live_enabled"] is False and data["mode"] == "paper"
    assert data["ts"] == "2026-10-09T12:00:00.000Z" and data["exit_code"] is None
    assert heartbeat.state_marker(tmp_path / "ws_recorder.json").exists()
    assert not list(tmp_path.glob("*.tmp"))
    assert heartbeat.read_beat(tmp_path / "ws_recorder.json", "ws_recorder")[1] is None


def test_beats_are_throttled_unless_forced(tmp_path):
    t = [0.0]
    hb = heartbeat.Heartbeat("m1_run", every_s=10, directory=tmp_path, monotonic=lambda: t[0])
    hb.beat(force=True)
    t[0] = 9.9
    hb.beat()
    assert hb.seq == 1
    t[0] = 10.0
    hb.beat()
    assert hb.seq == 2
    hb.beat(force=True)
    assert hb.seq == 3


def test_a_failed_write_is_reported_never_raised(tmp_path, monkeypatch):
    """Issue #79: the first write failure (no file, no .seen) halts globally at once, without the bus."""
    monkeypatch.setattr(podlib, "BUS_PATH", tmp_path / "bus.jsonl")
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    hb = heartbeat.Heartbeat("m1_run", every_s=1, directory=blocker / "sub")
    assert hb.beat(force=True) == heartbeat.HB_WRITE_FAILED
    assert hb.last_problem == heartbeat.HB_WRITE_FAILED
    assert not hb.path.exists() and not heartbeat.state_marker(hb.path).exists()
    assert hb.last_halt["persisted"] == halt_store.PERSISTED_STATE
    assert [h["id"] for h in halt_store.active_halts()] == [hb.last_halt["id"]]
    assert [(h["scope"], h["reason"], h["source"]) for h in halt_store.active_halts()] == [
        ("global", "heartbeat_write_failed:m1_run", "heartbeat")]
    assert not (tmp_path / "bus.jsonl").exists()  # the halt never goes through the bus
    # the persisted halt blocks the existing consumers
    assert [r.split(":", 1)[0] for r in halt_store.halt_problems("m1")] == ["halt_global"]
    vetoed = risk_gate.evaluate(_intent(), _cfg(), _state(), now=NOW)
    assert not vetoed.allow and any(r.startswith("halt_global:") for r in vetoed.reasons)


def test_a_replace_failure_halts_and_leaves_no_temp_file(tmp_path, monkeypatch):
    real_replace = os.replace

    def boom(src, dst, **kwargs):
        if Path(dst).name == "m1_run.json":
            raise OSError("replace refused")
        return real_replace(src, dst, **kwargs)

    monkeypatch.setattr(os, "replace", boom)
    hb = heartbeat.Heartbeat("m1_run", every_s=1, directory=tmp_path)
    assert hb.beat(force=True) == heartbeat.HB_WRITE_FAILED
    assert [(h["scope"], h["reason"], h["source"]) for h in halt_store.active_halts()] == [
        ("global", "heartbeat_write_failed:m1_run", "heartbeat")]
    assert not list(tmp_path.glob("*.tmp"))


def test_a_marker_write_failure_halts(tmp_path, monkeypatch):
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(heartbeat, "state_marker", lambda path: blocker / "sub" / path.name)
    hb = heartbeat.Heartbeat("m1_run", every_s=1, directory=tmp_path)
    assert hb.beat(force=True) == heartbeat.HB_WRITE_FAILED
    assert hb.path.exists() and not heartbeat.state_marker(hb.path).exists()  # only the marker step failed
    assert [(h["scope"], h["reason"], h["source"]) for h in halt_store.active_halts()] == [
        ("global", "heartbeat_write_failed:m1_run", "heartbeat")]


def test_repeat_failures_halt_once_and_a_good_write_does_not_resume(tmp_path, monkeypatch):
    real_replace = os.replace
    failing = [False]

    def boom(src, dst, **kwargs):
        if failing[0] and Path(dst).name == "m1_run.json":
            raise OSError("replace refused")
        return real_replace(src, dst, **kwargs)

    monkeypatch.setattr(os, "replace", boom)
    hb = heartbeat.Heartbeat("m1_run", every_s=1, directory=tmp_path)
    assert hb.beat(force=True) is None
    failing[0] = True
    assert hb.beat(force=True) == heartbeat.HB_WRITE_FAILED
    assert hb.beat(force=True) == heartbeat.HB_WRITE_FAILED
    assert hb.last_halt["duplicate"] is True  # the halt store dedupes
    assert [h["reason"] for h in halt_store.active_halts()] == ["heartbeat_write_failed:m1_run"]
    failing[0] = False
    assert hb.beat(force=True) is None and hb.last_halt is None
    assert [h["reason"] for h in halt_store.active_halts()] == ["heartbeat_write_failed:m1_run"]  # needs a resume


def test_a_halt_state_write_failure_falls_back_to_the_halt_file(tmp_path, monkeypatch):
    def boom(path, records):
        raise OSError("state write refused")

    monkeypatch.setattr(halt_store, "_atomic_write", boom)
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    hb = heartbeat.Heartbeat("m1_run", every_s=1, directory=blocker / "sub")
    assert hb.beat(force=True) == heartbeat.HB_WRITE_FAILED
    assert hb.last_halt["persisted"] == halt_store.PERSISTED_FALLBACK
    assert "heartbeat_write_failed:m1_run" in halt_store.HALT_FALLBACK_PATH.read_text(encoding="utf-8")
    assert halt_store.halt_problems("m1") == [halt_store.FALLBACK_ACTIVE]


def test_when_both_halt_paths_are_down_the_failure_result_is_kept(tmp_path, monkeypatch):
    def boom(path, records):
        raise OSError("state write refused")

    monkeypatch.setattr(halt_store, "_atomic_write", boom)
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(halt_store, "HALT_FALLBACK_PATH", blocker / "HALT")
    hb = heartbeat.Heartbeat("m1_run", every_s=1, directory=blocker / "sub")
    assert hb.beat(force=True) == heartbeat.HB_WRITE_FAILED  # the write failure is not swallowed
    assert hb.last_halt["persisted"] == halt_store.PERSISTED_NONE  # and nothing claims a persisted halt
    assert hb.beat(force=True) == heartbeat.HB_WRITE_FAILED and hb.last_halt["persisted"] == halt_store.PERSISTED_NONE


def test_a_broken_halt_store_never_raises_out_of_beat(tmp_path, monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("halt store down")

    monkeypatch.setattr(halt_store, "record_halt", boom)
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    hb = heartbeat.Heartbeat("m1_run", every_s=1, directory=blocker / "sub")
    assert hb.beat(force=True) == heartbeat.HB_WRITE_FAILED
    assert hb.last_halt is None


def test_a_plain_state_write_is_never_a_heartbeat_event(tmp_path):
    """write_record stays generic (sentinel_tick's tick state goes through it)."""
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    assert heartbeat.write_record(blocker / "sub" / "x.json", record("m1_run")) == heartbeat.HB_WRITE_FAILED
    assert halt_store.active_halts() == []
    state = {"version": sentinel_tick.TICK_STATE_VERSION, "raised": [], "handled": {}, "ticks": 0}
    assert sentinel_tick.save_state(blocker / "sub" / "tick.json", state) == heartbeat.HB_WRITE_FAILED
    assert halt_store.active_halts() == []


def test_first_run_lost_unreadable_corrupt_and_malformed(tmp_path):
    path = tmp_path / "scanner.json"
    assert heartbeat.read_beat(path, "scanner") == (None, None)
    heartbeat.write_record(path, record("scanner"))
    path.unlink()
    assert heartbeat.read_beat(path, "scanner") == (None, heartbeat.HB_MISSING)
    path.write_text("{", encoding="utf-8")
    assert heartbeat.read_beat(path, "scanner")[1] == heartbeat.HB_CORRUPT
    path.write_bytes(b"\xff\xfe")
    assert heartbeat.read_beat(path, "scanner")[1] == heartbeat.HB_UNREADABLE


@pytest.mark.parametrize("change", [
    {"ts": "2026-10-09T12:00:00"}, {"ts": "2026-10-09"}, {"ts": 1}, {"started_at": None}, {"pid": True},
    {"pid": 0}, {"seq": -1}, {"state": "sleeping"}, {"state": "finished"}, {"exit_code": "4"},
    {"live_enabled": "false"}, {"progress": []}, {"proc": "other"}, {"mode": 1}, {"risk_sha256": 5},
])
def test_malformed_fields_fail_closed(tmp_path, change):
    path = tmp_path / "scanner.json"
    data = record("scanner")
    data.update(change)
    path.write_text(json.dumps(data), encoding="utf-8")
    assert heartbeat.read_beat(path, "scanner") == (None, heartbeat.HB_MALFORMED)


def test_a_missing_field_is_malformed(tmp_path):
    data = record("scanner")
    del data["seq"]
    path = tmp_path / "scanner.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    assert heartbeat.read_beat(path, "scanner")[1] == heartbeat.HB_MALFORMED


def test_age_is_none_for_future_or_bad_times():
    assert heartbeat.beat_age_s(record(ts=NOW), NOW + timedelta(seconds=5)) == 5
    assert heartbeat.beat_age_s(record(ts=NOW + timedelta(seconds=1)), NOW) is None
    assert heartbeat.beat_age_s({"ts": "x"}, NOW) is None


def test_risk_identity_reads_the_pinned_file_and_fails_to_none(tmp_path):
    assert heartbeat.risk_identity() == {"risk_sha256": PIN, "live_enabled": False, "mode": "paper",
                                         "require_human_gate": True}
    missing = heartbeat.risk_identity(tmp_path / "nope.toml")
    assert missing["risk_sha256"] is None and missing["live_enabled"] is None


def test_run_main_writes_start_and_finish_and_carries_counters(tmp_path):
    codes = iter([3, 3, 0, 2])
    seen = []
    for _ in range(4):
        def main():
            seen.append(json.loads((tmp_path / "scanner.json").read_text(encoding="utf-8"))["state"])
            return next(codes)
        heartbeat.run_main("scanner", main, directory=tmp_path, now=lambda: NOW)
        final = json.loads((tmp_path / "scanner.json").read_text(encoding="utf-8"))
        seen.append((final["state"], final["exit_code"], final["progress"].get("exit_streak")))
    assert seen[0] == "running"
    assert seen[1] == ("finished", 3, 1) and seen[3] == ("finished", 3, 2)
    assert seen[5] == ("finished", 0, 1) and seen[7] == ("finished", 2, 1)
    assert final["progress"]["last_success_ts"] == "2026-10-09T12:00:00.000Z"


def test_run_main_records_a_crash_and_reraises(tmp_path):
    def boom():
        raise KeyError("x")
    with pytest.raises(KeyError):
        heartbeat.run_main("endgame_scan", boom, directory=tmp_path)
    data = json.loads((tmp_path / "endgame_scan.json").read_text(encoding="utf-8"))
    assert data["state"] == "crashed" and data["exit_code"] is None


# ---------------------------------------------------------------- consumers: a lost Sentinel is a global halt

def test_no_sentinel_ever_means_no_halt():
    assert halt_store.halt_problems("m1") == []


def test_a_fresh_sentinel_heartbeat_is_no_halt(monkeypatch):
    monkeypatch.setattr(heartbeat, "datetime", _frozen(NOW + timedelta(seconds=SENTINEL_HEARTBEAT_TIMEOUT_S)))
    write("sentinel", ts=NOW)
    assert halt_store.halt_problems("m1") == []


@pytest.mark.parametrize("setup,reason", [
    (lambda: write("sentinel", ts=NOW - timedelta(seconds=SENTINEL_HEARTBEAT_TIMEOUT_S + 1)), "sentinel_heartbeat_stale"),
    (lambda: write("sentinel", ts=NOW + timedelta(seconds=5)), "sentinel_heartbeat_stale"),
    (lambda: write("sentinel", ts=NOW, state="finished", exit_code=0), "sentinel_heartbeat_stopped"),
    (lambda: write("sentinel", ts=NOW, state="crashed"), "sentinel_heartbeat_stopped"),
    (lambda: write("sentinel").write_text("{", encoding="utf-8"), "sentinel_heartbeat_corrupt"),
    (lambda: write("sentinel").unlink(), "sentinel_heartbeat_missing"),
])
def test_a_lost_sentinel_is_a_stored_global_halt(monkeypatch, setup, reason):
    monkeypatch.setattr(heartbeat, "datetime", _frozen(NOW))
    setup()
    problems = halt_store.halt_problems("m1")
    assert len(problems) == 1 and problems[0].startswith("halt_global:")
    active = halt_store.active_halts()
    assert [(h["scope"], h["reason"], h["source"]) for h in active] == [("global", reason, "halt_store")]
    assert len(halt_store.active_halts()) == 1  # not stored twice
    vetoed = risk_gate.evaluate(_intent(), _cfg(), _state(), now=NOW)
    assert not vetoed.allow and any(r.startswith("halt_global:") for r in vetoed.reasons)


def test_resuming_the_lost_sentinel_halt_while_still_lost_halts_again(monkeypatch):
    monkeypatch.setattr(heartbeat, "datetime", _frozen(NOW))
    write("sentinel", ts=NOW - timedelta(seconds=SENTINEL_HEARTBEAT_TIMEOUT_S + 1))
    first = halt_store.active_halts()[0]
    halt_store.resume(from_role="Operator", scope="global", halt_id=first["id"], approved_by="op", root_cause="x",
                      postmortem="PR #0", bus_path=Path(heartbeat.HEARTBEAT_DIR).parent / "bus.jsonl")
    again = halt_store.active_halts()
    assert len(again) == 1 and again[0]["id"] != first["id"] and again[0]["reason"] == "sentinel_heartbeat_stale"
    write("sentinel", ts=NOW)  # the Sentinel is back: the stored halt still needs an Operator resume
    assert len(halt_store.active_halts()) == 1


def test_a_lost_sentinel_under_a_corrupt_halt_file_still_blocks(monkeypatch):
    monkeypatch.setattr(heartbeat, "datetime", _frozen(NOW))
    halt_store.HALT_STATE_PATH.write_text("{", encoding="utf-8")
    write("sentinel", ts=NOW - timedelta(seconds=SENTINEL_HEARTBEAT_TIMEOUT_S + 1))
    assert set(halt_store.halt_problems("m1")) == {"halt_state_corrupt", "sentinel_heartbeat_stale"}
    assert not halt_store.HALT_FALLBACK_PATH.exists()  # nothing written while the state is bad


def test_a_broken_sentinel_check_is_a_halt(monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("boom")
    monkeypatch.setattr(heartbeat, "read_beat", broken)
    assert halt_store.sentinel_heartbeat_problem() == "sentinel_heartbeat_check_failed"


# ---------------------------------------------------------------- StaleWatch: re-alarm after a resume

STALE = "2026-10-09T11:50:00Z"
FRESH = "2026-10-09T11:59:30Z"


def _watch(tmp_path):
    return sentinel.StaleWatch(bus_path=tmp_path / "bus.jsonl", quotes_path=tmp_path / "quotes.jsonl")


def _market_halts():
    return [h for h in halt_store.active_halts() if h["scope"] == "market"]


def test_a_resume_while_the_book_is_still_stale_re_alarms_on_the_next_check(tmp_path):
    watch = _watch(tmp_path)
    watch.check("m1", STALE, NOW)
    assert watch.check("m1", STALE, NOW)["halt"] is not None
    assert watch.check("m1", STALE, NOW)["halt"] is None  # still halted: no duplicate
    halt = _market_halts()[0]
    halt_store.resume(from_role="Operator", scope="market", halt_id=halt["id"], market="m1", approved_by="op",
                      root_cause="x", postmortem="PR #0", bus_path=tmp_path / "bus.jsonl")
    assert _market_halts() == []
    again = watch.check("m1", STALE, NOW)  # no fresh check needed in between
    assert again["halt"] is not None and again["streak"] == 4
    assert len(_market_halts()) == 1
    assert watch.check("m1", STALE, NOW)["halt"] is None


def test_a_fresh_check_after_the_resume_resets_as_before(tmp_path):
    watch = _watch(tmp_path)
    watch.check("m1", STALE, NOW)
    watch.check("m1", STALE, NOW)
    halt = _market_halts()[0]
    halt_store.resume(from_role="Operator", scope="market", halt_id=halt["id"], market="m1", approved_by="op",
                      root_cause="x", postmortem="PR #0", bus_path=tmp_path / "bus.jsonl")
    assert watch.check("m1", FRESH, NOW)["halt"] is None
    assert watch.check("m1", STALE, NOW)["halt"] is None  # streak 1 again
    assert watch.check("m1", STALE, NOW)["halt"] is not None


def test_an_unreadable_halt_state_writes_no_duplicate(tmp_path):
    watch = _watch(tmp_path)
    watch.check("m1", STALE, NOW)
    watch.check("m1", STALE, NOW)
    halt_store.HALT_STATE_PATH.write_text("{", encoding="utf-8")
    assert watch.check("m1", STALE, NOW)["halt"] is None
    assert not halt_store.HALT_FALLBACK_PATH.exists()


# ---------------------------------------------------------------- Reconciler halts land in the halt store

def test_a_failing_reconcile_run_is_a_stored_global_halt(tmp_path, capsys):
    ledger = tmp_path / "paper.jsonl"
    ledger.write_text("", encoding="utf-8")
    bus = tmp_path / "bus.jsonl"
    code = reconcile.main(["--user", "0x" + "1" * 40, "--ledger", str(ledger), "--bus", str(bus)],
                          getter=lambda url: {})
    assert code == 3
    active = halt_store.active_halts()
    assert [(h["scope"], h["source"], h["reason"]) for h in active] == [("global", "Reconciler", "reconcile_failed exit=3")]
    lines = read_jsonl(bus)
    assert len(lines) == 1 and lines[0]["market"] == "*" and lines[0]["payload"]["exit_code"] == 3
    assert lines[0]["payload"]["halt_id"] == active[0]["id"]
    assert not risk_gate.evaluate(_intent(), _cfg(), _state(), now=NOW).allow


# ---------------------------------------------------------------- helpers

def _frozen(moment):
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment.astimezone(tz) if tz else moment
    return Frozen


def _cfg():
    from src.podlib import load_risk
    return load_risk()


def _intent():
    return risk_gate.Intent(strategy="news", market="m1", side="buy", model_prob=0.6, market_price=0.4,
                            assumed_slippage=0.005, fee_rate=0.0, liquidity_usd=50_000, spread=0.01,
                            hypothetical=True, book_ts=heartbeat.utc_stamp(NOW))


def _state():
    return {"open_positions": 0, "daily_loss_usd": 0.0, "recent_fills": [], "own_quotes": []}
