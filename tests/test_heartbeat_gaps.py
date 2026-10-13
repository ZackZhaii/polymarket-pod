"""Offline gaps in the Sentinel heartbeat path (src/heartbeat.py): the fail-closed branches Task AP's
tests/test_heartbeat.py does not reach.

``read_beat`` is what src/sentinel_tick.py reads to decide that a heartbeat is lost (a global halt), and
``Heartbeat.beat`` must never raise into a running loop (issue #79). Every branch below therefore has to
end in a problem string, a stored halt, or a ValueError at construction -- never in a silent pass, an
exception out of ``beat``, or a written file that claims success.

Offline: tmp_path only, no network, no real logs/, no real ledger, no halt state outside the test folder
(tests/conftest.py redirects halt_store and heartbeat.HEARTBEAT_DIR).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from src import halt_store, heartbeat

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)


def record(proc="m1_run", ts=NOW, **extra):
    data = {"proc": proc, "pid": 7, "host": "h", "started_at": heartbeat.utc_stamp(ts),
            "ts": heartbeat.utc_stamp(ts), "seq": 1, "git_sha": None, "risk_sha256": None,
            "live_enabled": False, "mode": "paper", "state": "running", "exit_code": None, "progress": {}}
    data.update(extra)
    return data


# --------------------------------------------------------------- a beat file the Sentinel must never trust

@pytest.mark.parametrize("text", ["[]", "null", "3", '"running"', "true"])
def test_a_non_object_beat_file_is_malformed(tmp_path, text):
    """Valid JSON that is not an object: fail closed as malformed, not as "no heartbeat". """
    path = tmp_path / "scanner.json"
    path.write_text(text, encoding="utf-8")
    assert heartbeat.read_beat(path, "scanner") == (None, heartbeat.HB_MALFORMED)


def test_a_directory_where_a_beat_file_belongs_is_unreadable(tmp_path):
    path = tmp_path / "scanner.json"
    path.mkdir()
    assert heartbeat.read_beat(path, "scanner") == (None, heartbeat.HB_UNREADABLE)


@pytest.mark.parametrize("change", [{"exit_code": True}, {"exit_code": 1.5}, {"exit_code": []},
                                    {"host": 7}, {"started_at": "2026-10-09T12:00:00"}, {"proc": ""}])
def test_more_malformed_fields_fail_closed(tmp_path, change):
    """A bool where an exit code belongs is not an exit code (``_int`` refuses bools)."""
    path = tmp_path / "scanner.json"
    data = record("scanner", state="finished", exit_code=0)
    data.update(change)
    path.write_text(json.dumps(data), encoding="utf-8")
    assert heartbeat.read_beat(path, "scanner") == (None, heartbeat.HB_MALFORMED)


# --------------------------------------------------------------- beat_age_s: an unusable input is "no age"

def test_beat_age_s_refuses_a_now_that_is_not_an_aware_datetime():
    """A naive or non-datetime ``now`` cannot date a beat: None (the caller fails closed)."""
    beat = record(ts=NOW)
    assert heartbeat.beat_age_s(beat, NOW.replace(tzinfo=None)) is None
    assert heartbeat.beat_age_s(beat, "2026-10-09T12:00:00Z") is None
    assert heartbeat.beat_age_s(beat, None) is None


def test_beat_age_s_reads_the_named_field_and_refuses_an_unusable_one():
    beat = record(ts=NOW)
    assert heartbeat.beat_age_s(beat, NOW + timedelta(seconds=5), field="started_at") == 5
    assert heartbeat.beat_age_s(beat, NOW, field="progress") is None  # a dict is not a stamp
    assert heartbeat.beat_age_s(beat, NOW, field="exit_code") is None  # null is not a stamp
    assert heartbeat.beat_age_s({}, NOW) is None  # a missing ts is not a stamp


# --------------------------------------------------------------- an unwritable beat must halt, never raise

@pytest.mark.parametrize("progress", [{"nan": float("nan")}, {"inf": float("inf")},
                                      {"obj": object()}, {"set": {1}}])
def test_a_non_serialisable_record_is_a_write_failure(tmp_path, progress):
    """``allow_nan=False`` (and json's own TypeError) is a failed write, not a crash or a partial file."""
    path = tmp_path / "m1_run.json"
    assert heartbeat.write_record(path, record("m1_run", progress=progress)) == heartbeat.HB_WRITE_FAILED
    assert not path.exists() and not heartbeat.state_marker(path).exists()  # no half-written beat
    assert not list(tmp_path.glob("*.tmp"))  # and no temp file left behind


def test_a_non_serialisable_progress_halts_through_beat(tmp_path):
    hb = heartbeat.Heartbeat("m1_run", every_s=1, directory=tmp_path, now=lambda: NOW)
    assert hb.beat(force=True, progress={"nan": float("nan")}) == heartbeat.HB_WRITE_FAILED
    assert hb.last_problem == heartbeat.HB_WRITE_FAILED
    assert [h["reason"] for h in halt_store.active_halts()] == ["heartbeat_write_failed:m1_run"]


def test_a_raising_monotonic_clock_is_a_write_failure_and_a_halt(tmp_path):
    """The cadence clock is inside the try: a broken monotonic() must halt, not break the loop."""

    def boom():
        raise RuntimeError("monotonic clock down")

    hb = heartbeat.Heartbeat("m1_run", every_s=1, directory=tmp_path, monotonic=boom, now=lambda: NOW)
    assert hb.beat(force=True) == heartbeat.HB_WRITE_FAILED
    assert hb.last_halt is not None and hb.last_halt["persisted"] != halt_store.PERSISTED_NONE
    assert [h["reason"] for h in halt_store.active_halts()] == ["heartbeat_write_failed:m1_run"]
    assert not (tmp_path / "m1_run.json").exists()


@pytest.mark.parametrize("every_s", [0, 0.0, -1, -0.5, True, False, "1", None, float("nan")])
def test_a_cadence_that_is_not_a_positive_number_is_refused(tmp_path, every_s):
    with pytest.raises(ValueError):
        heartbeat.Heartbeat("m1_run", every_s=every_s, directory=tmp_path)


# --------------------------------------------------------------- run_main's exit code is always an int

@pytest.mark.parametrize("returned", [None, "0", True, 0.0, [], {}])
def test_run_main_coerces_a_non_int_result_to_1(tmp_path, returned):
    """A CLI whose main() returns something that is not an int exits 1, never 0 and never a bool."""
    assert heartbeat.run_main("scanner", lambda: returned, directory=tmp_path, now=lambda: NOW) == 1
    finished = json.loads((tmp_path / "scanner.json").read_text(encoding="utf-8"))
    assert finished["state"] == heartbeat.STATE_FINISHED and finished["exit_code"] == 1
    assert finished["progress"]["last_exit_code"] == 1
    assert finished["progress"]["exit_streak"] == 1
    assert "last_success_ts" not in finished["progress"]


def test_run_main_resets_a_corrupt_carried_streak(tmp_path):
    """A carried exit_streak that is not a counter restarts at 1 instead of being trusted."""
    path = tmp_path / "scanner.json"
    assert heartbeat.write_record(path, record("scanner", state="finished", exit_code=3,
                                               progress={"last_exit_code": 3, "exit_streak": "many"})) is None
    assert heartbeat.run_main("scanner", lambda: 3, directory=tmp_path, now=lambda: NOW) == 3
    finished = json.loads(path.read_text(encoding="utf-8"))
    assert finished["progress"]["last_exit_code"] == 3 and finished["progress"]["exit_streak"] == 1
