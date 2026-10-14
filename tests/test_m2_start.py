"""Issue #133 (Task BR): the M2 start orchestration (src/m2_start.py, scripts/m2_start.ps1).

The resident Sentinel starts first; nothing else starts until a fresh heartbeat from the Sentinel just
launched is verified (bounded by SENTINEL_HEARTBEAT_TIMEOUT_S), and each later process only after the
one before passed its own check. Paper only: no resume, no credential, no live flag, no scheduled task.
Offline: a fake launcher writes the heartbeats a real child would; one test starts real stub children
detached and stops them cleanly.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import src.halt_store as halt_store
import src.heartbeat as heartbeat
import src.m2_start as m2_start
import src.pins as pins
import src.sentinel_tick as sentinel_tick
from src.thresholds import SENTINEL_HEARTBEAT_TIMEOUT_S

ROOT = Path(__file__).resolve().parents[1]
END = "2099-01-01T00:00:00Z"


def _beat(proc: str, *, state: str = "running", started: datetime | None = None,
          ts: datetime | None = None) -> None:
    clock = (lambda: ts) if ts is not None else (lambda: datetime.now(timezone.utc))
    beat = heartbeat.Heartbeat(proc, every_s=1.0, now=clock)
    if started is not None:
        beat.started_at = heartbeat.utc_stamp(started)
    assert beat.beat(state, force=True) is None


def _recording(run_dir: Path, *, books: bool = True) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "manifest.json").write_text(json.dumps(
        {"schema": "m1_run/1", "assets": [{"market_id": "m1", "token_id": "t1"}]}), encoding="utf-8")
    if books:
        (run_dir / "books").mkdir(exist_ok=True)
        (run_dir / "books" / "books-000001.jsonl").write_text("{}\n", encoding="utf-8")


class FakeHandle:
    def __init__(self, pid: int, code: int | None = None) -> None:
        self.pid = pid
        self.code = code

    def poll(self) -> int | None:
        return self.code


def _default_scan(root: Path) -> None:
    """One older scanner batch so every boot start has usable assets (later batches in a test win)."""
    path = root / "data" / "markets.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"ts": "2026-10-01T00:00:00Z", "id": "m-d", "question": "q", "version": "v2",
           "token_id_source": "clob", "token_ids": ["tD1", "tD2"],
           "end_date": "2099-01-01T00:00:00Z",  # Task DI (#137): the scanner always sets end_date
           "tags": ["politics"], "category": "politics"}  # and a classification (unknown = dropped)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")


class Rig:
    """Launcher stand-in: records each start; ``on[proc]`` plays what that child would write."""

    def __init__(self, root: Path) -> None:
        self.root = root
        _default_scan(root)  # Task DE (#276): the boot start selects its assets from data/markets.jsonl
        self.launched: list[tuple[str, list[str], Path]] = []
        self.exit: dict[str, int] = {}
        self.on = {
            "sentinel": lambda argv: _beat("sentinel"),
            "m1_run": lambda argv: (_beat("m1_run", state="starting"),
                                    _recording(self.root / argv[argv.index("--out") + 1])),
            "paper_cycle": lambda argv: _beat("paper_cycle"),
            "m2_runner": lambda argv: _beat("m2_runner"),
        }
        self.slept = 0.0

    def proc(self, argv: list[str]) -> str:
        return argv[argv.index("-m") + 1].split(".")[-1].replace("sentinel_tick", "sentinel")

    def launcher(self, argv, log_path, cwd):
        proc = self.proc(argv)
        assert cwd == self.root and log_path.parent.is_dir()
        self.launched.append((proc, list(argv), log_path))
        action = self.on.get(proc)
        if action is not None:
            action(argv)
        return FakeHandle(1000 + len(self.launched), self.exit.get(proc))

    def sleep(self, seconds: float) -> None:
        self.slept += seconds

    def monotonic(self) -> float:
        return self.slept

    def order(self) -> list[str]:
        return [proc for proc, _, _ in self.launched]


def _start(rig: Rig, lines: list[str] | None = None, **kwargs):
    lines = lines if lines is not None else []
    options = {"reason": "boot", "m2_end": END, "dry_run": False, "root": rig.root, "launcher": rig.launcher,
               "python": "python", "sleep": rig.sleep, "monotonic": rig.monotonic,
               "runner_available": lambda root: False, "out": lines.append}
    options.update(kwargs)
    return m2_start.start(**options)


def _chain(root: Path) -> list[dict]:
    path = root / "runs" / "m2-chain.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path / "pod")


# ---------------------------------------------------------------- the start order

def test_start_order_sentinel_first_then_m1_run_then_paper_cycle(rig, no_sentinel):
    lines: list[str] = []
    assert _start(rig, lines) == m2_start.EXIT_OK
    assert rig.order() == ["sentinel", "m1_run", "paper_cycle"]
    sentinel, m1, cycle = (argv for _, argv, _ in rig.launched)
    assert sentinel == ["python", "-m", "src.sentinel_tick"]  # resident: no --once
    run_dir = m1[m1.index("--out") + 1]
    assert run_dir.startswith("runs/m2/m1-") and run_dir.endswith("Z")
    assert cycle == ["python", "-m", "src.paper_cycle", "--loop", "--recording-dir", run_dir]
    assert 0 < int(m1[m1.index("--max-runtime") + 1]) <= (
        datetime(2099, 1, 1, tzinfo=timezone.utc) - datetime.now(timezone.utc)).total_seconds() + 1
    (row,) = _chain(rig.root)
    assert set(row) == set(m2_start.CHAIN_FIELDS)
    assert row["run_dir"] == run_dir and row["reason"] == "boot" and row["m2_end"] == "2099-01-01T00:00:00.000Z"
    assert row["max_runtime_s"] == int(m1[m1.index("--max-runtime") + 1])
    assert row["sentinel_pid"] == os.getpid()  # the pid the (fake) Sentinel's heartbeat carries
    text = "\n".join(lines)
    assert "Existing halts are NOT resumed" in text and "§6.3" in text and "halts_active=" not in text  # printed via stdout
    for _, argv, log in rig.launched:
        assert log.parent == rig.root / "runs" / "m2" / "start-logs" / run_dir.split("m1-")[1]
        assert not any("live" in part.lower() for part in argv)


def test_the_chain_line_is_written_before_m1_run_starts(rig, no_sentinel):
    seen: list[int] = []
    rig.on["m1_run"] = lambda argv: (seen.append(len(_chain(rig.root))), _beat("m1_run"),
                                     _recording(rig.root / argv[argv.index("--out") + 1]))
    assert _start(rig) == m2_start.EXIT_OK
    assert seen == [1]


def test_a_fresh_running_sentinel_is_kept_never_a_second_one(rig):
    assert halt_store.sentinel_heartbeat_problem() is None  # conftest's resident Sentinel
    lines: list[str] = []
    assert _start(rig, lines) == m2_start.EXIT_OK
    assert rig.order() == ["m1_run", "paper_cycle"]
    assert any("already running and fresh" in line for line in lines)


def test_a_second_start_reads_the_m2_end_from_the_chain_and_gets_a_new_run_dir(rig, no_sentinel):
    assert _start(rig) == m2_start.EXIT_OK
    for proc in ("m1_run", "paper_cycle"):  # the first chain's processes ended
        _beat(proc, state="finished")
    time.sleep(1.1)  # the run dir is stamped to the second
    rig.launched.clear()
    assert _start(rig, m2_end=None, reason="reboot") == m2_start.EXIT_OK
    assert rig.order() == ["m1_run", "paper_cycle"]  # the first start's Sentinel is still fresh: kept
    one, two = _chain(rig.root)
    assert two["reason"] == "reboot" and two["m2_end"] == one["m2_end"]
    assert two["run_dir"] != one["run_dir"] and two["max_runtime_s"] < one["max_runtime_s"]


def test_m2_runner_starts_last_once_it_exists_and_is_watched(rig, no_sentinel, monkeypatch):
    monkeypatch.setitem(sentinel_tick.PROC_SPECS, "m2_runner", sentinel_tick.ProcSpec(120.0, sentinel_tick.GLOBAL))
    assert _start(rig, runner_available=lambda root: True) == m2_start.EXIT_OK
    assert rig.order() == ["sentinel", "m1_run", "paper_cycle", "m2_runner"]
    runner = rig.launched[-1][1]
    assert runner[:5] == ["python", "-m", "src.m2_runner", "--run-dir", "runs/m2"]


def test_m2_runner_present_but_not_watched_refuses_everything(rig, no_sentinel, monkeypatch):
    # Issue #119 (Task CE) added the watch; a checkout without it must still refuse everything.
    monkeypatch.delitem(sentinel_tick.PROC_SPECS, "m2_runner", raising=False)
    assert "m2_runner" not in sentinel_tick.PROC_SPECS
    assert _start(rig, runner_available=lambda root: True) == m2_start.EXIT_REFUSED
    assert rig.launched == [] and _chain(rig.root) == []


def test_runner_detection_reads_the_checkout(tmp_path, no_sentinel):
    rig = Rig(tmp_path)
    assert _start(rig, runner_available=None) == m2_start.EXIT_OK  # no src/m2_runner.py in tmp
    assert "m2_runner" not in rig.order()


# ---------------------------------------------------------------- runs/m2/current.json

def _current(root: Path) -> dict:
    return json.loads((root / "runs" / "m2" / "current.json").read_text(encoding="utf-8"))


def test_current_json_names_the_run_dir_and_every_launched_process(rig, no_sentinel):
    assert _start(rig) == m2_start.EXIT_OK
    current = _current(rig.root)
    assert current["schema"] == "m2_current/1" and current["status"] == "started" and current["failure"] is None
    m1_argv = rig.launched[1][1]
    assert current["run_dir"] == m1_argv[m1_argv.index("--out") + 1] == _chain(rig.root)[0]["run_dir"]
    assert current["reason"] == "boot" and current["m2_end"] == "2099-01-01T00:00:00.000Z"
    assert [p["name"] for p in current["processes"]] == ["sentinel", "m1_run", "paper_cycle"]
    assert [p["pid"] for p in current["processes"]] == [1001, 1002, 1003]  # the launched handles
    for entry in current["processes"]:
        assert entry["state"] == "ready" and entry["heartbeat_pid"] == os.getpid()
        assert heartbeat.parse_stamp(entry["started_ts"]) is not None
    assert heartbeat.parse_stamp(current["updated_ts"]) is not None
    leftovers = [p.name for p in (rig.root / "runs" / "m2").iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_current_json_is_updated_as_each_process_starts(rig, no_sentinel):
    seen: dict[str, dict] = {}
    path = rig.root / "runs" / "m2" / "current.json"
    real = rig.on["paper_cycle"]
    rig.on["sentinel"] = lambda argv: (seen.__setitem__("sentinel", json.loads(path.read_text()) if path.exists()
                                                         else None), _beat("sentinel"))
    rig.on["paper_cycle"] = lambda argv: (seen.__setitem__("paper_cycle", _current(rig.root)), real(argv))
    assert _start(rig) == m2_start.EXIT_OK
    assert seen["sentinel"] is None  # written right after the launch, not before
    at_cycle = seen["paper_cycle"]
    assert [(p["name"], p["state"]) for p in at_cycle["processes"]] == [("sentinel", "ready"), ("m1_run", "ready")]
    assert at_cycle["status"] == "starting" and at_cycle["run_dir"] is not None


def test_current_json_is_written_through_the_atomic_writer(rig, no_sentinel, monkeypatch):
    writes: list[str] = []
    real = heartbeat.write_record

    def spy(path, record):
        if path.name == "current.json":
            writes.append(record["status"])
        return real(path, record)

    monkeypatch.setattr(heartbeat, "write_record", spy)
    assert _start(rig) == m2_start.EXIT_OK
    # sentinel launched, ready+run dir, m1_run starting, ready, paper_cycle starting, ready, started
    assert writes == ["starting"] * 6 + ["started"]


def test_a_kept_sentinel_is_listed_as_kept(rig):
    assert _start(rig) == m2_start.EXIT_OK
    sentinel = _current(rig.root)["processes"][0]
    assert sentinel["name"] == "sentinel" and sentinel["state"] == "kept" and sentinel["pid"] == os.getpid()


def test_current_json_records_a_sentinel_that_never_got_fresh(rig, no_sentinel):
    rig.on["sentinel"] = lambda argv: None
    assert _start(rig) == m2_start.EXIT_SENTINEL_NOT_FRESH
    current = _current(rig.root)
    assert current["status"] == "failed" and current["failure"].startswith("sentinel: timeout_90s")
    assert current["run_dir"] is None and [(p["name"], p["state"]) for p in current["processes"]] == [
        ("sentinel", "not_ready")]


def test_current_json_records_a_process_that_was_not_ready(rig, no_sentinel):
    rig.on["paper_cycle"] = lambda argv: None
    assert _start(rig) == m2_start.EXIT_NOT_READY
    current = _current(rig.root)
    assert current["status"] == "failed" and current["failure"].startswith("paper_cycle: timeout_180s")
    assert [(p["name"], p["state"]) for p in current["processes"]] == [
        ("sentinel", "ready"), ("m1_run", "ready"), ("paper_cycle", "not_ready")]


@pytest.mark.parametrize("fail_at", [1, 2, 3])
def test_a_failed_current_json_write_stops_the_order(rig, no_sentinel, monkeypatch, fail_at):
    real = heartbeat.write_record
    count = {"n": 0}

    def flaky(path, record):
        if path.name == "current.json":
            count["n"] += 1
            if count["n"] == fail_at:
                return heartbeat.HB_WRITE_FAILED
        return real(path, record)

    monkeypatch.setattr(heartbeat, "write_record", flaky)
    lines: list[str] = []
    assert _start(rig, lines) == m2_start.EXIT_NOT_READY
    expected = {1: ["sentinel"], 2: ["sentinel"], 3: ["sentinel", "m1_run"]}[fail_at]
    assert rig.order() == expected
    assert any("cannot write runs/m2/current.json" in line for line in lines)


def test_a_second_start_rewrites_current_json(rig, no_sentinel):
    assert _start(rig) == m2_start.EXIT_OK
    first = _current(rig.root)
    for proc in ("m1_run", "paper_cycle"):
        _beat(proc, state="finished")
    time.sleep(1.1)
    assert _start(rig, m2_end=None, reason="reboot") == m2_start.EXIT_OK
    second = _current(rig.root)
    assert second["run_dir"] != first["run_dir"] and second["reason"] == "reboot"
    assert [(p["name"], p["state"]) for p in second["processes"]] == [
        ("sentinel", "kept"), ("m1_run", "ready"), ("paper_cycle", "ready")]


# ---------------------------------------------------------------- the Sentinel gate (fail closed)

def test_sentinel_never_fresh_starts_nothing_else_within_the_bounded_wait(rig, no_sentinel):
    rig.on["sentinel"] = lambda argv: None
    lines: list[str] = []
    assert _start(rig, lines) == m2_start.EXIT_SENTINEL_NOT_FRESH
    assert rig.order() == ["sentinel"]
    assert _chain(rig.root) == []
    assert SENTINEL_HEARTBEAT_TIMEOUT_S <= rig.slept <= SENTINEL_HEARTBEAT_TIMEOUT_S + m2_start.START_POLL_S
    assert any("never became fresh" in line and "timeout_90s:no_heartbeat" in line for line in lines)


def test_sentinel_that_exits_fails_at_once(rig, no_sentinel):
    rig.on["sentinel"] = lambda argv: None
    rig.exit["sentinel"] = 2
    lines: list[str] = []
    assert _start(rig, lines) == m2_start.EXIT_SENTINEL_NOT_FRESH
    assert rig.order() == ["sentinel"] and rig.slept == 0
    assert any("exited_2" in line for line in lines)


@pytest.mark.parametrize("state", ["finished", "crashed", "starting"])
def test_a_sentinel_beat_that_is_not_running_is_not_fresh(rig, no_sentinel, state):
    rig.on["sentinel"] = lambda argv: _beat("sentinel", state=state)
    assert _start(rig) == m2_start.EXIT_SENTINEL_NOT_FRESH
    assert rig.order() == ["sentinel"]


def test_a_sentinel_beat_off_the_paper_pins_is_not_fresh(rig, no_sentinel, monkeypatch):
    real = heartbeat.risk_identity
    monkeypatch.setattr(heartbeat, "risk_identity",
                        lambda path=None: {**real(path), "live_enabled": True})
    assert _start(rig) == m2_start.EXIT_SENTINEL_NOT_FRESH
    assert rig.order() == ["sentinel"]


def test_an_old_heartbeat_never_counts_as_the_launched_sentinel():
    launched = datetime.now(timezone.utc)
    _beat("sentinel", started=launched - timedelta(seconds=5))
    record, why = m2_start.fresh_beat("sentinel", timeout_s=90.0, now=datetime.now(timezone.utc),
                                      launched_at=launched)
    assert record is None and why == "older_than_launch"
    _beat("sentinel")
    assert m2_start.fresh_beat("sentinel", timeout_s=90.0, now=datetime.now(timezone.utc),
                               launched_at=launched)[1] is None


def test_a_stale_beat_is_not_fresh():
    old = datetime.now(timezone.utc) - timedelta(seconds=SENTINEL_HEARTBEAT_TIMEOUT_S + 1)
    _beat("sentinel", ts=old, started=old)
    assert m2_start.sentinel_fresh(now=datetime.now(timezone.utc))[1] == "stale"


# ---------------------------------------------------------------- later steps (fail closed)

def test_m1_run_without_a_books_file_is_not_ready_and_paper_cycle_never_starts(rig, no_sentinel):
    rig.on["m1_run"] = lambda argv: (_beat("m1_run"), _recording(rig.root / argv[argv.index("--out") + 1],
                                                                 books=False))
    lines: list[str] = []
    assert _start(rig, lines) == m2_start.EXIT_NOT_READY
    assert rig.order() == ["sentinel", "m1_run"]
    assert len(_chain(rig.root)) == 1  # the run dir is in the chain: its gap is accounted for
    assert any("recording_has_no_book_file" in line for line in lines)
    assert rig.slept >= sentinel_tick.PROC_SPECS["m1_run"].timeout_s


def test_m1_run_that_exits_is_not_ready(rig, no_sentinel):
    rig.on["m1_run"] = lambda argv: None
    rig.exit["m1_run"] = 2
    assert _start(rig) == m2_start.EXIT_NOT_READY
    assert rig.order() == ["sentinel", "m1_run"]


def test_paper_cycle_without_a_heartbeat_is_not_ready(rig, no_sentinel):
    rig.on["paper_cycle"] = lambda argv: None
    assert _start(rig) == m2_start.EXIT_NOT_READY
    assert rig.order() == ["sentinel", "m1_run", "paper_cycle"]
    assert rig.slept >= sentinel_tick.PROC_SPECS["paper_cycle"].timeout_s


def test_a_sentinel_lost_while_m1_run_starts_stops_the_order(rig, no_sentinel):
    def m1(argv):
        heartbeat.beat_path("sentinel").unlink()  # lost after it was seen
        _beat("m1_run")
        _recording(rig.root / argv[argv.index("--out") + 1])

    rig.on["m1_run"] = m1
    lines: list[str] = []
    assert _start(rig, lines) == m2_start.EXIT_NOT_READY
    assert rig.order() == ["sentinel", "m1_run"]
    assert any("sentinel_not_fresh" in line for line in lines)


def test_a_failed_chain_append_starts_nothing_after_the_sentinel(rig, no_sentinel):
    (rig.root / "runs" / "m2-chain.jsonl").mkdir(parents=True)  # a directory: the read refuses first
    assert _start(rig) == m2_start.EXIT_REFUSED
    assert rig.launched == []


def test_a_chain_write_error_after_the_sentinel_is_exit_5(rig, no_sentinel, monkeypatch):
    real_open = Path.open

    def broken(self, mode="r", *args, **kwargs):
        if self.name == "m2-chain.jsonl" and "a" in mode:
            raise PermissionError("locked")
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", broken)
    assert _start(rig) == m2_start.EXIT_NOT_READY
    assert rig.order() == ["sentinel"]


# ---------------------------------------------------------------- refusals: nothing starts

@pytest.mark.parametrize("value", ["", "   "])
def test_blank_reason_is_usage(rig, value):
    assert _start(rig, reason=value) == m2_start.EXIT_USAGE and rig.launched == []


def test_first_start_needs_an_m2_end(rig):
    assert _start(rig, m2_end=None) == m2_start.EXIT_USAGE
    assert _start(rig, m2_end="2099-01-01") == m2_start.EXIT_USAGE  # no time, no zone
    assert rig.launched == [] and not (rig.root / "runs").exists()


def test_an_m2_end_that_differs_from_the_chain_is_usage(rig, no_sentinel):
    assert _start(rig) == m2_start.EXIT_OK
    for proc in ("m1_run", "paper_cycle"):
        _beat(proc, state="finished")
    rig.launched.clear()
    assert _start(rig, m2_end="2098-01-01T00:00:00Z") == m2_start.EXIT_USAGE
    assert rig.launched == [] and len(_chain(rig.root)) == 1


def test_the_m2_window_over_refuses(rig):
    assert _start(rig, m2_end="2020-01-01T00:00:00Z") == m2_start.EXIT_REFUSED and rig.launched == []


@pytest.mark.parametrize("content", ["not json\n", '{"ts": "x"}\n', "[]\n"])
def test_a_bad_chain_refuses(rig, content):
    (rig.root / "runs").mkdir(parents=True)
    (rig.root / "runs" / "m2-chain.jsonl").write_text(content, encoding="utf-8")
    assert _start(rig) == m2_start.EXIT_REFUSED and rig.launched == []


def test_mixed_m2_ends_in_the_chain_refuse(rig):
    (rig.root / "runs").mkdir(parents=True)
    base = {field: "x" for field in m2_start.CHAIN_FIELDS}
    rows = [{**base, "m2_end": "2099-01-01T00:00:00Z"}, {**base, "m2_end": "2099-01-02T00:00:00Z"}]
    (rig.root / "runs" / "m2-chain.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    assert _start(rig, m2_end=None) == m2_start.EXIT_REFUSED and rig.launched == []


def test_a_kill_file_refuses(rig):
    (rig.root / "runs" / "m2").mkdir(parents=True)
    (rig.root / "runs" / "m2" / "KILL").write_text("", encoding="utf-8")
    assert _start(rig) == m2_start.EXIT_REFUSED and rig.launched == []


@pytest.mark.parametrize("proc", ["m1_run", "paper_cycle"])
def test_an_already_running_process_refuses(rig, proc):
    _beat(proc)
    assert _start(rig) == m2_start.EXIT_REFUSED and rig.launched == []


def test_an_existing_run_dir_is_never_reused(rig):
    moment = datetime.now(timezone.utc)
    (rig.root / "runs" / "m2" / f"m1-{m2_start.stamp(moment)}").mkdir(parents=True)
    assert _start(rig, now=lambda: moment) == m2_start.EXIT_REFUSED and rig.launched == []


def test_pin_drift_refuses_with_exit_6(rig, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(pins, "refuse_start", lambda proc, **kw: calls.append(proc) or ["risk_sha256_not_pinned"])
    assert _start(rig) == m2_start.EXIT_PIN_DRIFT and rig.launched == []
    assert calls == ["m2_start"]


def test_dry_run_pin_drift_records_nothing(rig, monkeypatch):
    monkeypatch.setattr(pins, "refuse_start", lambda *a, **k: pytest.fail("dry-run must not record a halt"))
    monkeypatch.setattr(pins, "risk_file_problems", lambda *a, **k: ["risk_sha256_not_pinned"])
    assert _start(rig, dry_run=True) == m2_start.EXIT_PIN_DRIFT


@pytest.mark.parametrize("root, inside", [
    ("D:\\AI_Workspace\\pod-m1", True), ("d:/ai_workspace/POD-M1/polymarket-pod", True),
    ("D:\\AI_Workspace\\pod-m1\\", True), ("D:\\AI_Workspace\\pod-m10", False),
    ("D:\\AI_Workspace\\Temp\\pod-bi", False), ("/workspace/ar", False)])
def test_the_m1_clone_is_recognised(root, inside):
    assert m2_start.in_m1_clone(Path(root) if "/" in root and ":" not in root else root) is inside


def test_running_from_the_m1_clone_refuses(rig, monkeypatch):
    monkeypatch.setattr(m2_start, "in_m1_clone", lambda root: True)
    assert _start(rig) == m2_start.EXIT_REFUSED and rig.launched == []


# ---------------------------------------------------------------- dry run

def test_dry_run_prints_the_plan_in_order_and_touches_nothing(rig, no_sentinel):
    lines: list[str] = []
    assert _start(rig, lines, dry_run=True) == m2_start.EXIT_OK
    assert rig.launched == [] and sorted(p.name for p in rig.root.iterdir()) == ["data"]  # only the scan input
    text = "\n".join(lines)
    order = [text.index(key) for key in ("step 1 sentinel: would start python -m src.sentinel_tick",
                                          "step 2 chain: would append", "step 3 m1_run: would start",
                                          "step 4 paper_cycle: would start")]
    assert order == sorted(order)
    assert "wait up to 90 s (SENTINEL_HEARTBEAT_TIMEOUT_S)" in text
    assert "--recording-dir runs/m2/m1-" in text and '"reason": "boot"' in text
    assert "not in this checkout (#119)" in text


def test_cli_parses_and_dry_runs(monkeypatch, capsys):
    seen = {}
    monkeypatch.setattr(m2_start, "start", lambda **kw: seen.update(kw) or 0)
    assert m2_start.main(["--reason", "boot", "--m2-end", END, "--dry-run"]) == 0
    assert seen == {"reason": "boot", "m2_end": END, "dry_run": True}
    assert m2_start.main([]) == m2_start.EXIT_USAGE
    assert m2_start.main(["--help"]) == m2_start.EXIT_OK


# ---------------------------------------------------------------- paper only (static)

def test_the_script_and_wrapper_stay_paper_only():
    module = (ROOT / "src" / "m2_start.py").read_text(encoding="utf-8")
    wrapper = (ROOT / "scripts" / "m2_start.ps1").read_text(encoding="utf-8")
    code = "\n".join(line for line in module.splitlines() if not line.lstrip().startswith("#"))
    for text in (code.split('"""', 2)[2], wrapper):
        lowered = text.lower()
        for banned in ("register-scheduledtask", "schtasks", "new-service", "sc.exe create", "nssm",
                       "--live", "live_enabled = true", "py_clob", "web3", "eth_account", "private_key",
                       "load_dotenv", "get-content .env", "resume("):
            assert banned not in lowered, banned
        assert "open(\".env" not in lowered and "'.env'" not in lowered and '".env"' not in lowered
    assert "-m', 'src.m2_start'" in wrapper or "'-m','src.m2_start'" in wrapper
    assert "--dry-run" in wrapper and "pod-m1" not in wrapper.lower()


# ---------------------------------------------------------------- real detached children (start, then stop)

STUB = r'''
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from src import heartbeat
proc, hb_dir, stop = sys.argv[2], Path(sys.argv[3]), Path(sys.argv[4])
if proc == "m1_run":
    out = Path(sys.argv[5])
    (out / "books").mkdir(parents=True)
    (out / "manifest.json").write_text('{"schema": "m1_run/1", "assets": [{"market_id": "m1"}]}')
    (out / "books" / "books-000001.jsonl").write_text("{}\n")
beat = heartbeat.Heartbeat(proc, every_s=0.1, directory=hb_dir)
deadline = time.monotonic() + 60
while not stop.exists() and time.monotonic() < deadline:
    beat.beat("running", force=True)
    time.sleep(0.1)
beat.finish(0)
'''


def test_real_detached_children_start_in_order_and_stop_cleanly(tmp_path, no_sentinel):
    root = tmp_path / "pod"
    root.mkdir()
    _default_scan(root)
    stub = tmp_path / "stub.py"
    stub.write_text(STUB, encoding="utf-8")
    stop = tmp_path / "STOP"
    handles: list = []

    def argv_for(proc: str, argv: list[str]) -> list[str]:
        stubbed = [sys.executable, str(stub), str(ROOT), proc, str(heartbeat.HEARTBEAT_DIR), str(stop)]
        return stubbed + ([str(root / argv[argv.index("--out") + 1])] if proc == "m1_run" else [])

    def launcher(argv, log_path, cwd):
        handle = m2_start.popen_launcher(argv, log_path, cwd)
        handles.append(handle)
        return handle

    lines: list[str] = []
    try:
        code = m2_start.start(reason="test", m2_end=END, dry_run=False, root=root, launcher=launcher,
                              runner_available=lambda r: False, argv_for=argv_for, out=lines.append)
        assert code == m2_start.EXIT_OK, "\n".join(lines)
        assert [h.poll() for h in handles] == [None, None, None]  # all three still running
        procs = [json.loads(heartbeat.beat_path(p).read_text())["pid"] for p in ("sentinel", "m1_run", "paper_cycle")]
        assert len(set(procs)) == 3
        current = _current(root)
        assert [p["pid"] for p in current["processes"]] == [h.pid for h in handles]
        assert [p["heartbeat_pid"] for p in current["processes"]] == procs
    finally:
        stop.write_text("", encoding="utf-8")
        for handle in handles:
            try:
                assert handle.wait(timeout=30) == 0
            except subprocess.TimeoutExpired:
                handle.kill()
                raise
    for proc in ("sentinel", "m1_run", "paper_cycle"):
        record, problem = heartbeat.read_beat(heartbeat.beat_path(proc), proc)
        assert problem is None and record["state"] == "finished" and record["exit_code"] == 0
    assert len(_chain(root)) == 1
