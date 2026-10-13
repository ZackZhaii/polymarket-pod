"""Issue #247 (Task DK, split from KEY #243): the M2 runner writes one alert line on KILL.

When ``<run dir>/KILL`` stops the runner it appends one row to the Sentinel's ``logs/alerts.jsonl``
(``sentinel_tick.ALERTS_PATH``; conftest keeps it in the test's folder). Nothing else changes: the exit code stays
0, no getter is called, recovery is untouched and nothing restarts the runner (auto-restart is KEY #243). A failed
alert write is a write failure like a journal / events one (exit 4), never a silent drop. Offline: fake getters
that fail the test if called.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

import src.heartbeat as heartbeat
import src.sentinel_tick as sentinel_tick
from src import m2_runner, podlib
from tests import m2_runner_rig as rig


@pytest.fixture
def env(tmp_path, monkeypatch):
    paths = rig.isolate(monkeypatch, tmp_path)
    return {**paths, "now": datetime.now(timezone.utc), "tmp": tmp_path}


def _alerts() -> list[dict]:
    return rig.read_jsonl(sentinel_tick.ALERTS_PATH)


def _runner_alerts() -> list[dict]:
    return [row for row in _alerts() if row.get("from") == m2_runner.PROC]


def _beat() -> dict:
    record, problem = heartbeat.read_beat(heartbeat.beat_path(m2_runner.PROC), m2_runner.PROC)
    assert problem is None and record is not None
    return record


def _kill(env) -> None:
    env["run"].mkdir(parents=True, exist_ok=True)
    (env["run"] / "KILL").write_text("stop", encoding="utf-8")


def test_kill_writes_one_alert_line_and_still_exits_0(env):
    fake = rig.FakeGetters(env["now"], fail=True)
    pending = rig.deliver(env["run"], env["now"])
    _kill(env)
    assert m2_runner.main(["--run-dir", str(env["run"])], getters=fake.getters()) == m2_runner.EXIT_OK
    assert fake.calls == 0 and pending.exists()
    rows = _alerts()
    assert len(rows) == 1
    row = rows[0]
    assert row["from"] == m2_runner.PROC and row["proc"] == m2_runner.PROC
    assert row["alert"] == m2_runner.KILL_ALERT == "kill_file"
    assert row["severity"] == "info" and row["exchange_action"] == "none"
    assert str(env["run"] / "KILL") in row["detail"] and "exit_code=0" in row["detail"]
    assert datetime.fromisoformat(row["ts"].replace("Z", "+00:00")).tzinfo is not None
    beat = _beat()
    assert beat["state"] == heartbeat.STATE_FINISHED and beat["exit_code"] == 0


def test_the_kill_alert_row_matches_the_sentinel_alert_shape(env):
    _kill(env)
    assert rig.run_once(env["run"], rig.FakeGetters(env["now"], fail=True)) == m2_runner.EXIT_OK
    (row,) = _runner_alerts()
    assert set(row) == {"ts", "from", "alert", "severity", "proc", "detail", "exchange_action"}


def test_no_kill_no_alert(env):
    fake = rig.FakeGetters(env["now"])
    rig.deliver(env["run"], env["now"])
    assert rig.run_once(env["run"], fake) == m2_runner.EXIT_OK
    assert _runner_alerts() == []
    assert not (env["run"] / "KILL").exists()


def test_max_runtime_stop_writes_no_kill_alert(env):
    ticks = iter(range(0, 10_000, 5))
    assert m2_runner.main(["--run-dir", str(env["run"]), "--max-runtime", "1"],
                          getters=rig.FakeGetters(env["now"]).getters(), sleep=lambda _s: None,
                          monotonic=lambda: float(next(ticks))) == m2_runner.EXIT_OK
    assert _runner_alerts() == []


def test_the_alert_is_written_at_most_once_per_stop(env):
    _kill(env)
    state = m2_runner.RunnerState(paths=m2_runner.RunnerPaths.under(env["run"]))
    getters = rig.FakeGetters(env["now"], fail=True).getters()
    first = m2_runner.iteration(state, now=env["now"], getters=getters)
    again = m2_runner.iteration(state, now=env["now"], getters=getters)
    assert first.action == again.action == m2_runner.ACT_FINISH
    assert len(_runner_alerts()) == 1


def test_each_start_that_stops_on_kill_writes_its_own_line(env):
    _kill(env)
    for _ in range(2):
        assert rig.run_once(env["run"], rig.FakeGetters(env["now"], fail=True)) == m2_runner.EXIT_OK
    assert len(_runner_alerts()) == 2


def test_earlier_alert_lines_are_kept(env):
    old = {"ts": "2026-10-13T00:00:00.000Z", "from": "Sentinel", "alert": "runner_paused_too_long",
           "severity": "warn", "proc": m2_runner.PROC, "detail": "", "exchange_action": "none"}
    podlib.append_jsonl(sentinel_tick.ALERTS_PATH, old)
    _kill(env)
    assert rig.run_once(env["run"], rig.FakeGetters(env["now"], fail=True)) == m2_runner.EXIT_OK
    rows = _alerts()
    assert rows[0] == old and [row["alert"] for row in rows] == ["runner_paused_too_long", "kill_file"]


def test_a_failed_alert_write_exits_4_and_is_not_dropped_silently(env, monkeypatch):
    def broken(path, row):
        raise OSError("disk gone")

    monkeypatch.setattr(podlib, "append_jsonl", broken)
    fake = rig.FakeGetters(env["now"], fail=True)
    _kill(env)
    assert rig.run_once(env["run"], fake) == m2_runner.EXIT_WRITE_FAILED
    assert fake.calls == 0
    beat = _beat()
    assert beat["state"] == heartbeat.STATE_FINISHED and beat["exit_code"] == m2_runner.EXIT_WRITE_FAILED


def test_kill_with_a_live_flag_problem_still_only_finishes(env, monkeypatch):
    """KILL is checked first: the KILL path never reaches the live / pin checks, so no halt and exit 0."""
    monkeypatch.setattr(m2_runner, "live_flag_problems", lambda *_a, **_k: pytest.fail("live check reached"))
    _kill(env)
    assert rig.run_once(env["run"], rig.FakeGetters(env["now"], fail=True)) == m2_runner.EXIT_OK
    assert len(_runner_alerts()) == 1
