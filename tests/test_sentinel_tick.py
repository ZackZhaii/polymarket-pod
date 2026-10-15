"""Task AP: the resident Sentinel tick (src/sentinel_tick.py) and its event matrix. Offline."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src import halt_store, heartbeat, sentinel_tick as st
from src.podlib import read_jsonl
from src.thresholds import (HEARTBEAT_CONSUMER_TIMEOUT_S, HEARTBEAT_PAPER_CYCLE_TIMEOUT_S,
                            HEARTBEAT_RECORDER_TIMEOUT_S, HEARTBEAT_REST_TIMEOUT_S, REST_ERROR_STREAK_ALERT,
                            SCANNER_HANG_S, SCANNER_MAX_SUCCESS_AGE_S, SENTINEL_TICK_S)

PIN = "0aea23af1e938d330b5b4fab2c6abdec9f475f16807d177a5860a4da32aebc04"
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def env(tmp_path):
    ledger = tmp_path / "paper.jsonl"
    ledger.write_text("", encoding="utf-8")
    bus = tmp_path / "bus.jsonl"

    def run(now=NOW, **kw):
        kw.setdefault("bus_path", bus)
        kw.setdefault("quotes_path", tmp_path / "quotes.jsonl")
        kw.setdefault("ledger_path", ledger)
        return st.tick(now=now, **kw)
    run.bus = bus
    run.tmp = tmp_path
    return run


def beat(proc, *, age_s=0.0, started_age_s=None, state="running", exit_code=None, pid=7, **fields):
    ts = NOW - timedelta(seconds=age_s)
    started = NOW - timedelta(seconds=started_age_s if started_age_s is not None else age_s)
    data = {"proc": proc, "pid": pid, "host": "h", "started_at": heartbeat.utc_stamp(started),
            "ts": heartbeat.utc_stamp(ts), "seq": 1, "git_sha": None, "risk_sha256": PIN,
            "live_enabled": False, "mode": "paper", "state": state, "exit_code": exit_code, "progress": {}}
    data.update(fields)
    assert heartbeat.write_record(heartbeat.beat_path(proc), data) is None


def finished(proc, code, **fields):
    beat(proc, state="finished", exit_code=code, **fields)


def active():
    return sorted((h["scope"], h["market"], h["reason"]) for h in halt_store.active_halts())


def resume_all(bus):
    for h in halt_store.active_halts():
        halt_store.resume(from_role="Operator", scope=h["scope"], halt_id=h["id"], market=h["market"],
                          approved_by="op", root_cause="x", postmortem="PR #0", bus_path=bus)


def alerts():
    return [r["alert"] for r in read_jsonl(st.ALERTS_PATH)] if Path(st.ALERTS_PATH).exists() else []


def test_specs_follow_the_thresholds():
    assert st.PROC_SPECS["ws_recorder"].timeout_s == HEARTBEAT_RECORDER_TIMEOUT_S == 120
    assert st.PROC_SPECS["m1_rest"].timeout_s == HEARTBEAT_REST_TIMEOUT_S
    assert st.PROC_SPECS["paper_cycle"].halt_after_s == 2 * HEARTBEAT_PAPER_CYCLE_TIMEOUT_S
    assert st.PROC_SPECS["halt_consumer"].timeout_s == HEARTBEAT_CONSUMER_TIMEOUT_S
    assert {p for p, s in st.PROC_SPECS.items() if s.clock == "started_at"} == {"scanner", "endgame_scan", "loop_claim", "resolutions"}


def test_a_quiet_world_raises_nothing(env):
    result = env()
    assert result["halts"] == [] and result["alerts"] == []
    assert set(result["checked"].values()) == {"never_ran"}
    assert active() == []


def test_fresh_and_finished_heartbeats_are_fine(env):
    beat("ws_recorder", age_s=HEARTBEAT_RECORDER_TIMEOUT_S)
    finished("m1_run", 0, age_s=10_000)
    finished("scanner", 0, progress={"last_success_ts": heartbeat.utc_stamp(NOW)})
    result = env()
    assert result["halts"] == [] and result["alerts"] == []


def test_a_stale_recorder_halts_globally_once_and_again_after_a_resume(env):
    beat("ws_recorder", age_s=HEARTBEAT_RECORDER_TIMEOUT_S + 1)
    assert env()["halts"] == ["global:*:heartbeat_stale:ws_recorder"]
    assert active() == [("global", "", "heartbeat_stale:ws_recorder")]
    assert alerts() == ["heartbeat_stale"]
    second = env()
    assert second["halts"] == [] and second["alerts"] == []  # deduped while halted
    resume_all(env.bus)
    assert env()["halts"] == ["global:*:heartbeat_stale:ws_recorder"]  # still stale: halted again at once
    beat("ws_recorder")
    resume_all(env.bus)
    assert env()["halts"] == []


@pytest.mark.parametrize("text,problem", [("{", "heartbeat_corrupt"), (b"\xff\xfe", "heartbeat_unreadable"),
                                          ('{"proc": "m1_run"}', "heartbeat_malformed")])
def test_unreadable_heartbeats_are_stale_and_halt(env, text, problem):
    beat("m1_rest")
    path = heartbeat.beat_path("m1_rest")
    path.write_bytes(text if isinstance(text, bytes) else text.encode())
    result = env()
    assert result["halts"] == [f"global:*:{problem}:m1_rest"] and result["checked"]["m1_rest"] == problem


def test_a_lost_heartbeat_after_its_marker_halts(env):
    beat("scanner")
    heartbeat.beat_path("scanner").unlink()
    assert env()["halts"] == ["global:*:heartbeat_missing:scanner"]


def test_a_future_heartbeat_halts(env):
    beat("m1_run", age_s=-5)
    assert env()["halts"] == ["global:*:heartbeat_bad_time:m1_run"]


@pytest.mark.parametrize("field,value", [("live_enabled", True), ("mode", "live"), ("risk_sha256", "0" * 64)])
def test_a_live_heartbeat_off_the_pins_halts(env, field, value):
    beat("paper_cycle", **{field: value})
    assert env()["halts"] == ["global:*:heartbeat_pin_drift:paper_cycle"]


def test_rest_staleness_alerts_without_halting(env):
    beat("m1_rest", age_s=HEARTBEAT_REST_TIMEOUT_S + 1)
    result = env()
    assert result["halts"] == [] and result["alerts"] == ["heartbeat_stale"]
    assert env()["alerts"] == []  # once while it holds
    beat("m1_rest")
    env()
    beat("m1_rest", age_s=HEARTBEAT_REST_TIMEOUT_S + 1)
    assert env()["alerts"] == ["heartbeat_stale"]  # re-armed after it cleared



def test_rest_error_streak_alerts_at_threshold_once_and_rearms(env):
    """Issue #138: m1_rest progress.error_streak >= REST_ERROR_STREAK_ALERT -> warn rest_error_streak;
    exactly one alert while the condition holds; a success (streak 0) clears and later failures re-arm."""
    assert REST_ERROR_STREAK_ALERT == 3
    beat("m1_rest", progress={"samples": 1, "rows": 0, "errors": 2, "error_streak": 1})
    assert env()["alerts"] == [] and alerts() == []
    beat("m1_rest", progress={"samples": 2, "rows": 0, "errors": 4, "error_streak": 2})
    assert env()["alerts"] == [] and alerts() == []
    beat("m1_rest", progress={"samples": 3, "rows": 0, "errors": 6, "error_streak": 3})
    result = env()
    assert result["halts"] == [] and result["alerts"] == ["rest_error_streak"]
    assert alerts() == ["rest_error_streak"]
    beat("m1_rest", progress={"samples": 4, "rows": 0, "errors": 8, "error_streak": 4})
    assert env()["alerts"] == [] and alerts() == ["rest_error_streak"]  # once while it holds
    beat("m1_rest", progress={"samples": 5, "rows": 2, "errors": 8, "error_streak": 0})
    env()  # success clears the latch
    beat("m1_rest", progress={"samples": 6, "rows": 2, "errors": 10, "error_streak": 3})
    assert env()["alerts"] == ["rest_error_streak"]  # re-armed


def test_rest_error_streak_missing_field_counts_as_zero(env):
    """Issue #138 / Task EE: an older m1_rest heartbeat without progress.error_streak is streak 0:
    no alert, no halt; a later real streak still alerts at the threshold."""
    beat("m1_rest", progress={"samples": 9, "rows": 0, "errors": 18})
    result = env()
    assert result["halts"] == [] and result["alerts"] == [] and alerts() == []
    beat("m1_rest", progress={})
    result = env()
    assert result["halts"] == [] and result["alerts"] == []
    beat("m1_rest", progress={"samples": 10, "rows": 0, "errors": 20, "error_streak": REST_ERROR_STREAK_ALERT})
    assert env()["alerts"] == ["rest_error_streak"]


@pytest.mark.parametrize("bad", ["3", True, 1.5, None], ids=["str", "bool", "float", "none"])
def test_malformed_rest_error_streak_halts(env, bad):
    """Issue #138: a non-int (or bool) error_streak is heartbeat_malformed (fail closed)."""
    beat("m1_rest", progress={"samples": 1, "rows": 0, "errors": 1, "error_streak": bad})
    assert env()["halts"] == ["global:*:heartbeat_malformed:m1_rest"]


def test_paper_cycle_alerts_at_181_and_halts_at_361(env):
    beat("paper_cycle", age_s=181)
    assert env()["halts"] == [] and alerts() == ["heartbeat_stale"]
    beat("paper_cycle", age_s=361)
    assert env()["halts"] == ["global:*:heartbeat_stale_escalated:paper_cycle"]


def test_a_stale_halt_consumer_halts(env):
    beat("halt_consumer", age_s=31)
    assert env()["halts"] == ["global:*:heartbeat_stale:halt_consumer"]


def test_a_hung_scanner_alerts_from_its_start(env):
    beat("scanner", age_s=1, started_age_s=SCANNER_HANG_S + 1)
    result = env()
    assert result["halts"] == [] and result["alerts"] == ["process_hung"]


def test_scanner_success_age(env):
    finished("scanner", 2)
    assert env()["alerts"] == ["exit_2", "scanner_no_success"]
    old = heartbeat.utc_stamp(NOW - timedelta(seconds=SCANNER_MAX_SUCCESS_AGE_S + 1))
    finished("scanner", 0, pid=8, progress={"last_success_ts": old})
    assert env()["alerts"] == ["scanner_success_too_old"]
    finished("scanner", 0, pid=9, progress={"last_success_ts": "bad"})
    assert env()["alerts"] == []  # still raised: once


@pytest.mark.parametrize("proc,code,halts,alert", [
    ("ws_recorder", 4, True, "exit_4"), ("m1_run", 4, True, "exit_4"), ("endgame_scan", 4, True, "exit_4"),
    ("loop_claim", 2, True, "exit_2"), ("scanner", 1, False, "exit_1"), ("m1_run", 3, False, "exit_3"),
    ("ws_recorder", 2, False, "exit_2"), ("loop_claim", 1, False, "exit_1"),
    # Task AS (gaps 4 and 5): pin drift codes are global halts; loop_claim usage moved to 5 (an alert)
    ("paper_cycle", 1, True, "exit_1"), ("scanner", 6, True, "exit_6"), ("ws_recorder", 6, True, "exit_6"),
    ("m1_run", 6, True, "exit_6"), ("paper_cycle", 2, False, "exit_2"), ("loop_claim", 5, False, "exit_5"),
    # Issue #75: stable corrupt/unreadable markets/books input is its own code, still a global halt
    ("paper_cycle", 3, True, "exit_3"),
    # Task AV: resolutions' old 4 split into 6 (pin drift) and 5 (bad snapshot clock), both global halts
    ("resolutions", 6, True, "exit_6"), ("resolutions", 5, True, "exit_5"), ("resolutions", 3, False, "exit_3"),
    ("resolutions", 4, False, "exit_4"),
])
def test_the_exit_matrix(env, proc, code, halts, alert):
    progress = {"last_success_ts": heartbeat.utc_stamp(NOW)} if proc == "scanner" else {}
    finished(proc, code, progress=progress)
    result = env()
    assert result["halts"] == ([f"global:*:exit_{code}:{proc}"] if halts else [])
    assert result["alerts"] == [alert]
    assert env()["halts"] == [] and env()["alerts"] == []  # handled once per run
    resume_all(env.bus)
    assert env()["halts"] == []  # a one-off event is not re-halted after a resume


def test_m1_run_exit_5_is_only_logged(env):
    finished("m1_run", 5)
    result = env()
    assert result["halts"] == [] and result["alerts"] == [] and result["logs"] == ["m1_run:exit_5"]


def test_scanner_exit_3_alerts_only_when_repeated(env):
    ok = heartbeat.utc_stamp(NOW)
    finished("scanner", 3, progress={"exit_streak": 1, "last_success_ts": ok})
    assert env()["alerts"] == [] and alerts() == []
    finished("scanner", 3, pid=8, progress={"exit_streak": 2, "last_success_ts": ok})
    assert env()["alerts"] == ["scanner_incomplete_repeated"]
    finished("scanner", 3, pid=9, progress={"exit_streak": "x", "last_success_ts": ok})
    assert env()["alerts"] == ["scanner_incomplete_repeated"]  # malformed streak: fail closed


def test_a_crashed_recorder_halts_and_a_crashed_scanner_alerts(env):
    beat("ws_recorder", state="crashed")
    beat("scanner", state="crashed", progress={"last_success_ts": heartbeat.utc_stamp(NOW)})
    result = env()
    assert result["halts"] == ["global:*:process_crashed:ws_recorder"]
    assert sorted(result["alerts"]) == ["process_crashed", "process_crashed"]


def test_a_new_run_is_a_new_event(env):
    finished("ws_recorder", 4)
    env()
    resume_all(env.bus)
    finished("ws_recorder", 4, pid=8)
    assert env()["halts"] == ["global:*:exit_4:ws_recorder"]


def test_a_corrupt_tick_state_alerts_and_handles_events_again(env):
    finished("ws_recorder", 1)
    assert env()["alerts"] == ["exit_1"]
    Path(st.TICK_STATE_PATH).write_text("{", encoding="utf-8")
    assert env()["alerts"] == ["tick_state_corrupt", "exit_1"]
    assert env()["alerts"] == []


def test_handled_events_are_bounded(env):
    for pid in range(1, st.HANDLED_KEEP + 5):
        finished("m1_run", 1, pid=pid)
        env()
    state = json.loads(Path(st.TICK_STATE_PATH).read_text(encoding="utf-8"))
    assert len(state["handled"]["m1_run"]) == st.HANDLED_KEEP


def test_recorder_gap_storm_halts_its_market(env):
    beat("ws_recorder", progress={"reconnects_in_window": 4, "gap_resyncs_in_window": {"a1": 3, "a2": 2, "a3": 9},
                                  "asset_markets": {"a1": "m-one", "a2": "m-two"}})
    result = env()
    assert result["halts"] == ["market:m-one:recorder_gap_resyncs:a1"]
    assert sorted(result["alerts"]) == ["gap_resync_storm", "gap_resync_storm", "reconnect_storm"]
    assert active() == [("market", "m-one", "recorder_gap_resyncs:a1")]
    resume_all(env.bus)
    assert env()["halts"] == ["market:m-one:recorder_gap_resyncs:a1"]  # storm still in the window


def test_malformed_recorder_progress_halts(env):
    beat("m1_run", progress={"reconnects_in_window": "4"})
    assert env()["halts"] == ["global:*:heartbeat_malformed:m1_run"]


def test_risk_toml_drift_halts(env, tmp_path):
    bad = tmp_path / "risk.toml"
    bad.write_text((Path(__file__).resolve().parents[1] / "config" / "risk.toml").read_text(encoding="utf-8")
                   .replace("live_enabled = false", "live_enabled = true"), encoding="utf-8")
    result = env(risk_path=bad)
    assert result["halts"] == ["global:*:risk_toml_pin_drift"] and result["alerts"] == ["risk_toml_pin_drift"]
    assert env(risk_path=tmp_path / "missing.toml")["halts"] == []  # already halted


def test_an_unknown_risk_key_or_type_change_halts_too(env, tmp_path):
    # Task AS (gap 6): every key of the file is pinned, its type too, and no key may be added
    text = (Path(__file__).resolve().parents[1] / "config" / "risk.toml").read_text(encoding="utf-8")
    bad = tmp_path / "risk.toml"
    bad.write_text(text + "live_mode = 1\n", encoding="utf-8")
    result = env(risk_path=bad)
    assert result["halts"] == ["global:*:risk_toml_pin_drift"] and result["alerts"] == ["risk_toml_pin_drift"]
    details = [json.loads(x)["payload"].get("detail", "") for x in env.bus.read_text(encoding="utf-8").splitlines()]
    assert any("unknown_key:live_mode" in d for d in details)


def test_an_invalid_ledger_halts(env, tmp_path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text("{not json\n", encoding="utf-8")
    assert env(ledger_path=bad)["halts"] == ["global:*:ledger_invalid"]


def test_a_failed_alert_bus_write_writes_logs_halt(env, tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    beat("m1_rest", age_s=HEARTBEAT_REST_TIMEOUT_S + 1)
    env(bus_path=blocker / "bus.jsonl")
    assert halt_store.fallback_active()
    assert "bus_write_failed" in halt_store.HALT_FALLBACK_PATH.read_text(encoding="utf-8")


def test_a_broken_check_halts(env, monkeypatch):
    real = st.Tick.check_proc

    def broken(self, proc, spec, directory):
        if proc == "scanner":
            raise RuntimeError("x")
        return real(self, proc, spec, directory)
    monkeypatch.setattr(st.Tick, "check_proc", broken)
    assert env()["halts"] == ["global:*:heartbeat_check_failed:scanner"]


def test_the_sentinel_writes_its_own_heartbeat_first(env):
    own = heartbeat.Heartbeat("sentinel", every_s=SENTINEL_TICK_S)
    env(own=own)
    record, problem = heartbeat.read_beat(heartbeat.beat_path("sentinel"), "sentinel")
    assert problem is None and record["state"] == "running"


# ---------------------------------------------------------------- CLI

def test_once_writes_no_sentinel_heartbeat(capsys, no_sentinel):  # issue #129: start without the fixture beat
    assert st.main(["--once"]) in (st.EXIT_OK, st.EXIT_RAISED)
    assert not heartbeat.beat_path("sentinel").exists()
    assert not heartbeat.state_marker(heartbeat.beat_path("sentinel")).exists()
    assert "exchange_action=none" in capsys.readouterr().out


def test_once_exits_3_when_it_raised(capsys):
    finished("m1_run", 2)
    assert st.main(["--once"]) == st.EXIT_RAISED


def test_resident_loop_beats_ticks_and_finishes(capsys):
    sleeps = []
    assert st.main(["--max-ticks", "3"], sleep=sleeps.append, monotonic=lambda: 0.0) == st.EXIT_OK
    assert sleeps == [SENTINEL_TICK_S, SENTINEL_TICK_S]
    record, _ = heartbeat.read_beat(heartbeat.beat_path("sentinel"), "sentinel")
    assert record["state"] == "finished" and record["progress"]["ticks"] == 3
    # a stopped Sentinel is a lost Sentinel for the consumers
    assert halt_store.sentinel_heartbeat_problem() == "sentinel_heartbeat_stopped"


def test_resident_loop_records_a_crash(capsys):
    def boom(_):
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        st.main([], sleep=boom, monotonic=lambda: 0.0)
    record, _ = heartbeat.read_beat(heartbeat.beat_path("sentinel"), "sentinel")
    assert record["state"] == "crashed"


@pytest.mark.parametrize("argv", [["--max-ticks", "-1"], ["--bogus"], ["--max-ticks", "x"]])
def test_usage_errors(argv, capsys):
    assert st.main(argv) == st.EXIT_USAGE
