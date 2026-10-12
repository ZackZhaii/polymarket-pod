"""Task BT: m1_run finishes its manifest on a console close / signal and records progress while running.

The console handler is called directly (``console=`` hands it to the test instead of SetConsoleCtrlHandler);
the Windows-only registration test skips elsewhere. Offline: the fake transport of test_m1_run.
"""
from __future__ import annotations

import json
import os
import signal
import threading
import time

import pytest

import src.heartbeat as heartbeat
from src import m1_run as r
from src import thresholds
from tests.test_m1_run import A, B, _book, _main, _manifest, _rows


class Console:
    """Stands in for SetConsoleCtrlHandler: keeps the handler, counts removals."""

    def __init__(self):
        self.handler = None
        self.removed = 0

    def __call__(self, handler):
        self.handler = handler

        def remove():
            self.removed += 1
        return remove


def _peek(tmp_path):
    return json.loads((tmp_path / "run" / "manifest.json").read_text(encoding="utf-8"))


def _m1_beat():
    record, problem = heartbeat.read_beat(heartbeat.beat_path("m1_run"), "m1_run")
    assert problem is None and record is not None
    return record


@pytest.fixture
def short_wait(monkeypatch):
    monkeypatch.setattr(r, "M1_CONSOLE_CLOSE_WAIT_S", 0.2)


@pytest.mark.parametrize("event", [r.CTRL_CLOSE_EVENT, r.CTRL_LOGOFF_EVENT, r.CTRL_SHUTDOWN_EVENT])
def test_a_console_close_while_blocked_writes_the_interrupted_manifest_at_once(tmp_path, short_wait, event):
    """The OS calls the handler while the main path cannot finish (here: the same thread is blocked):
    after the bounded wait the handler itself writes status interrupted, ended_at, elapsed_s, exit 5,
    the reason and the files written so far, plus a crashed heartbeat."""
    console = Console()
    seen = {}

    def close():
        assert console.handler(event) is True
        seen["manifest"] = _peek(tmp_path)
        seen["beat"] = _m1_beat()

    code, out, _, _, _ = _main(tmp_path, [[_book(A), _book(B), close]], ["--max-runtime", "86400"],
                               console=console, progress_every_s=3600.0)
    name = r.CTRL_EVENT_NAMES[event]
    fast = seen["manifest"]
    assert fast["status"] == "interrupted" and fast["exit_code"] == r.EXIT_INTERRUPTED
    assert fast["ended_at"] and isinstance(fast["elapsed_s"], float)
    assert fast["stop_reason"] == f"console:{name}" and fast["stop_signal"] == name
    assert fast["files"]["books"] and all(rel.startswith("books/") for rel in fast["files"]["books"])
    assert fast["counts"]["partial"] is True and "book_rows" not in fast["counts"]  # no false integrity count
    assert fast["risk"]["end"]["sha256"] == r.PINNED_RISK_SHA256
    assert seen["beat"]["state"] == heartbeat.STATE_CRASHED
    # the process survived here, so the normal end replaces it with the complete manifest
    assert code == r.EXIT_INTERRUPTED
    final = _manifest(out)
    assert final["status"] == "interrupted" and final["stop_reason"] == f"console:{name}"
    assert final["counts"]["book_rows"] == 2 and "partial" not in final["counts"]
    assert _m1_beat()["state"] == heartbeat.STATE_CRASHED  # a console close is never log-only exit 5
    assert console.removed == 1 and not list(out.glob(".*.tmp"))


def test_a_console_close_waits_for_a_normal_finish_on_another_thread(tmp_path, short_wait, monkeypatch):
    """Handler on an OS thread while the main path finishes: it returns once the main path is done and
    does not overwrite the complete manifest."""
    monkeypatch.setattr(r, "M1_CONSOLE_CLOSE_WAIT_S", 4.0)
    console = Console()
    result = {}
    worker = {}

    entered = threading.Event()

    def call():
        entered.set()
        result.update(ok=console.handler(r.CTRL_CLOSE_EVENT))

    def close():
        worker["t"] = threading.Thread(target=call)
        worker["t"].start()
        entered.wait(5)
        time.sleep(0.05)  # the handler has set stop and is waiting for the main path

    started = time.monotonic()
    code, out, _, _, _ = _main(tmp_path, [[_book(A), close]], ["--assets", A, "--max-runtime", "86400"],
                               console=console, progress_every_s=3600.0)
    worker["t"].join(10)
    assert result == {"ok": True} and time.monotonic() - started < 4.0
    final = _manifest(out)
    assert code == r.EXIT_INTERRUPTED and final["stop_reason"] == "console:CTRL_CLOSE_EVENT"
    assert final["counts"]["book_rows"] == 1 and "partial" not in final["counts"]
    assert _m1_beat()["state"] == heartbeat.STATE_CRASHED


@pytest.mark.parametrize("event", [r.CTRL_C_EVENT, r.CTRL_BREAK_EVENT])
def test_ctrl_c_and_break_are_an_operator_stop(tmp_path, event):
    console = Console()
    seen = {}

    def stop():
        before = time.monotonic()
        seen["ok"] = console.handler(event)
        seen["took"] = time.monotonic() - before

    code, out, _, _, _ = _main(tmp_path, [[_book(A), stop]], ["--assets", A, "--max-runtime", "86400"],
                               console=console, progress_every_s=3600.0)
    assert seen["ok"] is True and seen["took"] < 0.5  # no waiting: the main path finishes
    manifest = _manifest(out)
    assert code == r.EXIT_INTERRUPTED and manifest["status"] == "interrupted"
    assert manifest["stop_reason"] == f"console:{r.CTRL_EVENT_NAMES[event]}"
    beat = _m1_beat()
    assert beat["state"] == heartbeat.STATE_FINISHED and beat["exit_code"] == r.EXIT_INTERRUPTED


def test_sigterm_records_the_reason(tmp_path):
    def deliver():
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)

    code, out, _, _, _ = _main(tmp_path, [[_book(A), deliver]], ["--assets", A, "--max-runtime", "86400"],
                               progress_every_s=3600.0)
    manifest = _manifest(out)
    assert code == r.EXIT_INTERRUPTED and manifest["stop_reason"] == "signal:SIGTERM"
    assert manifest["ended_at"] and manifest["files"]["books"]


def test_the_manifest_records_progress_while_running(tmp_path, monkeypatch):
    seen = {}
    real_write_manifest = r.write_manifest

    def write_and_observe(run_dir, manifest):
        real_write_manifest(run_dir, manifest)
        if manifest["status"] == "running" and manifest.get("progress"):
            # Observe the real disk while ManifestWriter still holds its lock, so this test's
            # read handle is closed before another os.replace (Windows disallows that overlap).
            seen.update(_peek(tmp_path))

    monkeypatch.setattr(r, "write_manifest", write_and_observe)

    def wait_for_progress():
        deadline = time.monotonic() + 5
        while not seen and time.monotonic() < deadline:
            time.sleep(0.01)

    code, out, _, _, _ = _main(tmp_path, [[_book(A), wait_for_progress]], ["--assets", A], progress_every_s=0.02)
    progress = seen["progress"]
    assert seen["status"] == "running" and progress["last_flush_at"].startswith("2026-10-09T")
    assert progress["files"]["books"] and progress["book_rows"] == 1 and progress["writes"] >= 1
    assert progress["write_failures"] == 0 and isinstance(progress["elapsed_s"], float)
    assert code == r.EXIT_OK and _manifest(out)["status"] == "completed"


def test_a_failed_progress_write_is_counted_and_logged_not_fatal(tmp_path, monkeypatch):
    real = r.write_manifest
    state = {"fail": True, "failed": 0}

    def flaky(run_dir, manifest):
        if state["fail"] and manifest.get("progress") is not None and manifest["status"] == "running":
            state["failed"] += 1
            raise OSError("disk busy")
        real(run_dir, manifest)

    def wait():
        deadline = time.monotonic() + 5
        while state["failed"] < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        state["fail"] = False

    monkeypatch.setattr(r, "write_manifest", flaky)
    code, out, _, _, _ = _main(tmp_path, [[_book(A), wait]], ["--assets", A], progress_every_s=0.02)
    assert code == r.EXIT_OK and state["failed"] >= 2
    manifest = _manifest(out)
    assert manifest["status"] == "completed" and manifest["progress"]["write_failures"] >= 2
    assert any(event["event"] == "manifest_progress_write_failed" for event in _rows(out, "events"))


def test_manifest_writer_final_wins_and_fast_finish_never_raises(tmp_path, monkeypatch):
    manifest = {"status": "running", "progress": None}
    writer = r.ManifestWriter(tmp_path, manifest)
    writer.write()
    writer.finish({"status": "completed"})
    assert writer.fast_finish({"status": "interrupted"}, 0.1) is True
    assert writer.progress({"last_flush_at": "x"}) is True
    assert json.loads((tmp_path / "manifest.json").read_text())["status"] == "completed"

    other = r.ManifestWriter(tmp_path / "b", {"status": "running"})
    (tmp_path / "b").mkdir()
    with other.lock:  # a writer stuck mid-write: the console path gives up instead of hanging
        assert other.fast_finish({"status": "interrupted"}, 0.05) is False

    def broken(run_dir, data):
        raise OSError("gone")
    monkeypatch.setattr(r, "write_manifest", broken)
    third = r.ManifestWriter(tmp_path / "c", {"status": "running"})
    assert third.fast_finish({"status": "interrupted"}, 0.1) is False and not third.final.is_set()


def test_console_close_wait_fits_inside_the_os_kill_window():
    assert 0 < thresholds.M1_CONSOLE_CLOSE_WAIT_S < 5 and thresholds.M1_MANIFEST_PROGRESS_S > 0
    assert r.CTRL_OPERATOR_STOP == {r.CTRL_C_EVENT, r.CTRL_BREAK_EVENT}


@pytest.mark.skipif(os.name == "nt", reason="POSIX behaviour")
def test_no_console_handler_off_windows():
    assert r.install_console_handler(lambda ctrl: True) is None


@pytest.mark.skipif(os.name != "nt", reason="Windows only: real SetConsoleCtrlHandler")
def test_windows_registers_and_removes_a_real_console_handler():
    remove = r.install_console_handler(lambda ctrl: True)
    assert callable(remove)
    remove()
    assert hasattr(signal, "SIGBREAK")
