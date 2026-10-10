"""Issue #162: multi-process JSONL appends take ``<file>.lock`` (fail closed when it is not taken), lines
are written in binary with an explicit ``\\n``, and read_jsonl WARNs about the lines it skips. Offline."""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

import src.halt_store as halt_store
from src import forecaster, podlib, reflector_proposals, sentinel
from src.calibration_ids import ForecastLogError

ROOT = Path(__file__).resolve().parents[1]
N = 200


@pytest.fixture
def short_lock(monkeypatch):
    monkeypatch.setattr(halt_store, "HALT_STATE_LOCK_TIMEOUT_S", 0.2)


def _held(path: Path):
    lock = halt_store.StateLock(path)
    assert lock.acquire() and lock.path == path.with_name(path.name + ".lock")
    return lock


_CHILD = r"""
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from src.podlib import append_jsonl
path, tag, go = Path(sys.argv[2]), sys.argv[3], Path(sys.argv[4])
while not go.exists():
    time.sleep(0.005)
for i in range(int(sys.argv[5])):
    append_jsonl(path, {"writer": tag, "i": i, "pad": "x" * 3000})
"""


def test_two_processes_append_2n_whole_lines(tmp_path):
    path = tmp_path / "bus.jsonl"
    go = tmp_path / "go"
    procs = [subprocess.Popen([sys.executable, "-c", _CHILD, str(ROOT), str(path), tag, str(go), str(N)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for tag in ("a", "b")]
    time.sleep(0.3)
    go.write_text("1")
    for proc in procs:
        assert proc.wait(timeout=120) == 0, proc.communicate()
    lines = path.read_bytes().split(b"\n")
    assert lines[-1] == b"" and b"\r" not in path.read_bytes()
    rows = [json.loads(line) for line in lines[:-1]]  # every line parses: none torn or interleaved
    assert len(rows) == 2 * N
    for tag in ("a", "b"):
        assert [row["i"] for row in rows if row["writer"] == tag] == list(range(N))


def test_append_writes_lf_bytes(tmp_path):
    path = tmp_path / "x.jsonl"
    podlib.append_jsonl(path, {"a": "é"})
    assert path.read_bytes() == '{"a":"é"}\n'.encode("utf-8")


def test_a_held_lock_fails_closed_and_writes_nothing(tmp_path, short_lock):
    path = tmp_path / "quotes.jsonl"
    podlib.append_jsonl(path, {"n": 1})
    before = path.read_bytes()
    lock = _held(path)
    try:
        with pytest.raises(podlib.AppendLockError) as info:
            podlib.append_jsonl(path, {"n": 2})
        assert isinstance(info.value, OSError) and "quotes.jsonl.lock" in str(info.value)
        with pytest.raises(OSError):
            podlib.bus("Scanner", "Sentinel", "signal", "m", {"x": 1}, path=path)
    finally:
        lock.release()
    assert path.read_bytes() == before
    podlib.append_jsonl(path, {"n": 2})
    assert len(podlib.read_jsonl(path)) == 2


def test_cancel_open_quotes_takes_the_quotes_lock(tmp_path, short_lock):
    path = tmp_path / "quotes.jsonl"
    podlib.append_jsonl(path, {"market": "m", "status": "open"})
    before = path.read_bytes()
    lock = _held(path)
    try:
        with pytest.raises(OSError):
            sentinel.cancel_open_quotes(path, "2026-10-11T00:00:00Z")
    finally:
        lock.release()
    assert path.read_bytes() == before
    assert sentinel.cancel_open_quotes(path, "2026-10-11T00:00:00Z") == 1
    assert podlib.read_jsonl(path)[0]["status"] == "cancelled"


def test_reflector_proposal_append_takes_the_lock(tmp_path, short_lock):
    path = tmp_path / "proposals.jsonl"
    lock = _held(path)
    try:
        with pytest.raises(OSError):
            reflector_proposals._append_line(path, {"proposal_id": "p"})
    finally:
        lock.release()
    assert not path.exists() or path.read_bytes() == b""
    reflector_proposals._append_line(path, {"proposal_id": "p"})
    assert podlib.read_jsonl(path) == [{"proposal_id": "p"}]


def test_forecast_seal_takes_the_lock_and_fails_closed(tmp_path, short_lock):
    forecasts, bus_path = tmp_path / "forecasts.jsonl", tmp_path / "bus.jsonl"
    kw = dict(market="m1", model_prob=0.6, token_id="t-yes", outcome_index=0, outcome="Yes", condition_id="0xc1",
              bus_path=bus_path, forecasts_path=forecasts)
    lock = _held(forecasts)
    try:
        with pytest.raises(ForecastLogError):
            forecaster.emit_probability(**kw)
    finally:
        lock.release()
    assert not forecasts.exists() and not bus_path.exists()  # sealed nothing, sent nothing


def test_read_jsonl_warns_about_skipped_lines(tmp_path, capsys):
    path = tmp_path / "bus.jsonl"
    path.write_text('{"a":1}\nnot json\n[1,2]\n\n{"b":2}\n{"torn', encoding="utf-8")
    assert podlib.read_jsonl(path) == [{"a": 1}, {"b": 2}]
    err = capsys.readouterr().err
    assert f"WARN read_jsonl {path}: 3 unreadable line(s) skipped (line 2, 3, 6)" in err


def test_read_jsonl_lists_at_most_the_limit(tmp_path, capsys):
    path = tmp_path / "x.jsonl"
    path.write_text("bad\n" * (podlib.READ_JSONL_WARN_LINES + 3), encoding="utf-8")
    assert podlib.read_jsonl(path) == []
    err = capsys.readouterr().err
    assert f"{podlib.READ_JSONL_WARN_LINES + 3} unreadable line(s)" in err and "... (3 more)" in err


def test_read_jsonl_is_quiet_on_a_clean_file(tmp_path, capsys):
    path = tmp_path / "x.jsonl"
    podlib.append_jsonl(path, {"a": 1})
    assert podlib.read_jsonl(path) == [{"a": 1}] and capsys.readouterr().err == ""


def test_lock_files_are_gitignored():
    assert "*.jsonl.lock" in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
