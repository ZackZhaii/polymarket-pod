"""Issue #163: unique temp files (m1_run manifest, loop_claim state), fsync, cleanup on failure, and the
loop_claim read-check-write under ``<state>.lock``. Offline; temp state files only (never docs/loop)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import src.halt_store as halt_store
import src.loop_claim as loop_claim
from src import m1_run

ROOT = Path(__file__).resolve().parents[1]
NOW = "2026-10-08T08:00:00Z"


def _state() -> dict:
    return {"schema": 1, "claim_hours": 3, "claim": None,
            "tasks": [{"id": "F", "status": "todo", "title": "a"}, {"id": "G", "status": "todo", "title": "b"}]}


def _write(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _temps(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir() if p.name.endswith(".tmp"))


# --- loop_claim ------------------------------------------------------------------------------------


def test_save_state_failure_leaves_no_tmp_and_the_old_file(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    _write(path, _state())
    before = path.read_bytes()

    def broken(fd):
        raise OSError("disk full")
    monkeypatch.setattr(loop_claim.os, "fsync", broken)
    with pytest.raises(OSError):
        loop_claim.save_state(path, {**_state(), "claim": {"surface": "x"}})
    assert path.read_bytes() == before and _temps(tmp_path) == []


def test_save_state_uses_a_unique_temp_name_and_fsyncs(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    names, synced = [], []
    real_replace, real_fsync = os.replace, os.fsync
    monkeypatch.setattr(loop_claim.os, "replace", lambda src, dst: (names.append(Path(src).name), real_replace(src, dst)))
    monkeypatch.setattr(loop_claim.os, "fsync", lambda fd: (synced.append(fd), real_fsync(fd)))
    loop_claim.save_state(path, _state())
    loop_claim.save_state(path, _state())
    assert len(set(names)) == 2 and all(n.startswith("state.json.") and n.endswith(".tmp") for n in names)
    assert len(synced) == 2 and json.loads(path.read_text(encoding="utf-8")) == _state()
    assert _temps(tmp_path) == []


def test_a_held_state_lock_means_busy_and_no_change(tmp_path, monkeypatch):
    monkeypatch.setattr(halt_store, "HALT_STATE_LOCK_TIMEOUT_S", 0.2)
    path = tmp_path / "state.json"
    _write(path, _state())
    before = path.read_bytes()
    held = halt_store.StateLock(path)
    assert held.acquire() and held.path == tmp_path / "state.json.lock"
    try:
        code = loop_claim.main(["claim", "--surface", "grok-bot", "--task", "F", "--state", str(path), "--now", NOW])
    finally:
        held.release()
    assert code == loop_claim.EXIT_BUSY and path.read_bytes() == before
    assert loop_claim.main(["claim", "--surface", "grok-bot", "--task", "F", "--state", str(path), "--now", NOW]) == 0
    assert json.loads(path.read_text(encoding="utf-8"))["claim"]["surface"] == "grok-bot"


def test_status_takes_no_lock(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    _write(path, _state())
    monkeypatch.setattr(loop_claim, "state_lock", lambda p: (_ for _ in ()).throw(AssertionError("locked")))
    assert loop_claim.main(["status", "--state", str(path)]) == loop_claim.EXIT_OK


_CHILD = r"""
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import src.loop_claim as lc
real = lc.load_state
def slow(path):
    data = real(path)
    time.sleep(0.4)  # widen the read-check-write window: without the lock both claims would win
    return data
lc.load_state = slow
go = Path(sys.argv[3])
while not go.exists():
    time.sleep(0.01)
sys.exit(lc.main(["claim", "--surface", sys.argv[4], "--task", "F", "--state", sys.argv[2], "--now", "2026-10-08T08:00:00Z"]))
"""


def test_two_processes_claiming_at_once_one_wins_one_is_busy(tmp_path):
    path = tmp_path / "state.json"
    _write(path, _state())
    go = tmp_path / "go"
    procs = [subprocess.Popen([sys.executable, "-c", _CHILD, str(ROOT), str(path), str(go), surface], cwd=tmp_path,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
             for surface in ("grok-bot", "grok-web")]
    time.sleep(0.5)
    go.write_text("1")
    codes = sorted(proc.wait(timeout=60) for proc in procs)
    assert codes == [loop_claim.EXIT_OK, loop_claim.EXIT_BUSY], [proc.communicate() for proc in procs]
    state = json.loads(path.read_text(encoding="utf-8"))
    assert state["claim"]["surface"] in {"grok-bot", "grok-web"} and state["tasks"][0]["status"] == "doing"
    assert _temps(tmp_path) == []


def test_the_lock_file_is_gitignored():
    assert "docs/loop/state.json.lock" in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()


# --- m1_run manifest -------------------------------------------------------------------------------


def test_manifest_write_failure_leaves_no_tmp_and_the_old_manifest(tmp_path, monkeypatch):
    m1_run.write_manifest(tmp_path, {"status": "running"})
    before = (tmp_path / "manifest.json").read_bytes()

    def broken(src, dst):
        raise PermissionError("target held open (Windows)")
    monkeypatch.setattr(m1_run.os, "replace", broken)
    with pytest.raises(OSError):
        m1_run.write_manifest(tmp_path, {"status": "completed"})
    assert (tmp_path / "manifest.json").read_bytes() == before and _temps(tmp_path) == []


def test_concurrent_manifest_writers_never_share_a_temp_file(tmp_path):
    errors: list[BaseException] = []

    def writer(tag):
        try:
            for index in range(40):
                m1_run.write_manifest(tmp_path, {"writer": tag, "index": index, "pad": "x" * 4096})
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(tag,)) for tag in ("a", "b", "c")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    if os.name == "nt":  # Windows may refuse a replace while another replace holds the target: no data loss
        errors = [exc for exc in errors if not isinstance(exc, PermissionError)]
    assert errors == []
    final = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert final["writer"] in {"a", "b", "c"} and len(final["pad"]) == 4096
    assert _temps(tmp_path) == []
