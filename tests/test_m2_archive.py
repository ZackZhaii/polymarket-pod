"""EW C: synthetic-only single-new-file persistence."""
import base64
import copy
import hashlib
import importlib
import json
import os
from pathlib import Path

import pytest

from src.m2_report import capture_heartbeat_once


def _api():
    try:
        module = importlib.import_module("src.m2_archive")
    except ModuleNotFoundError as exc:
        if exc.name != "src.m2_archive":
            raise
        module = None
    fn = getattr(module, "archive_capture_once", None)
    assert callable(fn), "unique-new-file archive not implemented"
    return module, fn


def _report(tmp_path, raw=None):
    payload = {"proc": "m2_runner", "pid": 7, "host": "owned-fixture",
        "started_at": "2026-11-04T00:00:00Z", "ts": "2026-11-04T00:00:01Z",
        "seq": 0, "git_sha": None, "risk_sha256": None, "live_enabled": None,
        "mode": None, "state": "paused", "exit_code": None,
        "progress": {"status": "UNKNOWN"}, "extra": {"雪": True}}
    if raw is None:
        raw = (" \r\n" + json.dumps(payload, ensure_ascii=False, indent=1) + "\r\n").encode()
    path = tmp_path / "source.json"
    path.write_bytes(raw)
    return capture_heartbeat_once({"source": str(path), "format": "json"},
        expected_proc="m2_runner", capture_id="explicit"), raw, path


def _line(report):
    envelope = {"schema": "m2_heartbeat_archive/1", "status": "NOT_EVALUATED",
        "declaration_status": "UNVERIFIED", "capture_report": report,
        "metrics": None, "denominator": None, "healthy_online_us": None}
    return (json.dumps(envelope, sort_keys=True, ensure_ascii=False, allow_nan=False,
        separators=(",", ":")) + "\n").encode("utf-8")


def test_archive_exact_new_record_and_memory_binding(tmp_path, monkeypatch):
    module, fn = _api()
    report, raw, source = _report(tmp_path)
    assert report["declaration"] is not None
    original = copy.deepcopy(report)
    before = source.stat().st_mtime_ns
    target = tmp_path / "hb-one.jsonl"
    calls = []
    real_open = module._open_file
    def only_target(path, mode):
        assert Path(path) == target and mode in ("xb", "rb")
        calls.append(mode)
        return real_open(path, mode)
    with monkeypatch.context() as patch:
        patch.setattr(module, "_open_file", only_target)
        result = fn(report, owned_root=str(tmp_path), file_name="hb-one.jsonl")
    expected = _line(original)
    assert result["status"] == "NOT_EVALUATED" and result["write_state"] == "WRITTEN_READ_BACK"
    assert result["metrics"] is result["denominator"] is result["healthy_online_us"] is None
    assert target.read_bytes() == expected and expected.count(b"\n") == 1
    assert result["readback_sha256"] == result["expected_sha256"] == hashlib.sha256(expected).hexdigest()
    assert result["readback_bytes"] == result["expected_bytes"] == len(expected)
    assert calls == ["xb", "rb"] and report == original
    assert (source.read_bytes(), source.stat().st_mtime_ns) == (raw, before)


def test_archive_existing_is_not_overwritten(tmp_path):
    _, fn = _api()
    report, _, _ = _report(tmp_path)
    target = tmp_path / "hb-existing.jsonl"
    target.write_bytes(b"sealed partial original")
    result = fn(report, owned_root=str(tmp_path), file_name=target.name)
    assert result["write_state"] == "REFUSED" and result["created"] is False
    assert target.read_bytes() == b"sealed partial original"


@pytest.mark.parametrize("raw", [b"", b"\xff", b'{"x":NaN}', b'{"x":1e999}',
    b'{"x":1,"x":2}', b"[]", b'{"proc":"wrong"}'])
def test_archive_diagnostic_raw_is_preserved_not_repaired(raw, tmp_path):
    _, fn = _api()
    report, _, _ = _report(tmp_path, raw)
    assert report["declaration"] is None
    result = fn(report, owned_root=str(tmp_path), file_name="hb-diagnostic.jsonl")
    assert result["write_state"] == "WRITTEN_READ_BACK"
    row = json.loads((tmp_path / "hb-diagnostic.jsonl").read_bytes())
    assert row["capture_report"] == report and row["declaration_status"] == "UNVERIFIED"
    assert base64.b64decode(row["capture_report"]["capture"]["raw_base64"]) == raw


@pytest.mark.parametrize("bad", ["raw-none", "base64-space", "base64-unicode", "pad-bits",
    "sha", "bytes-bool", "bytes-negative", "status", "metrics", "completeness",
    "declaration-type", "label", "stamp", "extra-key", "custom"])
def test_archive_invalid_capture_rejects_before_output_io(bad, tmp_path, monkeypatch):
    module, fn = _api()
    report, _, _ = _report(tmp_path, b"f" if bad == "pad-bits" else None)
    if bad == "raw-none": report["capture"]["raw_base64"] = None
    elif bad == "base64-space": report["capture"]["raw_base64"] += "\n"
    elif bad == "base64-unicode": report["capture"]["raw_base64"] = "é"
    elif bad == "pad-bits": report["capture"]["raw_base64"] = "Zh=="
    elif bad == "sha": report["input"]["sha256"] = "A" * 64
    elif bad == "bytes-bool": report["input"]["bytes"] = True
    elif bad == "bytes-negative": report["input"]["bytes"] = -1
    elif bad == "status": report["status"] = "PASS"
    elif bad == "metrics": report["metrics"] = {}
    elif bad == "completeness": report["completeness"] = "INVALID"
    elif bad == "declaration-type": report["declaration"]["extra"]["雪"] = 1
    elif bad == "label": report["capture"]["capture_id"] = None
    elif bad == "stamp": report["capture"]["started_at_utc"] = "2026-11-04"
    elif bad == "extra-key": report["raw_override"] = "override"
    else: report["declaration"]["extra"] = object()
    calls = []
    def forbidden(*args, **kwargs):
        calls.append(1)
        raise AssertionError("invalid input reached output IO")
    with monkeypatch.context() as patch:
        patch.setattr(module, "_open_file", forbidden)
        patch.setattr(Path, "lstat", forbidden)
        patch.setattr(Path, "resolve", forbidden)
        result = fn(report, owned_root=str(tmp_path), file_name="hb-bad.jsonl")
    assert result["write_state"] == "REFUSED" and result["phase"] == "validate"
    assert not calls and not (tmp_path / "hb-bad.jsonl").exists()


@pytest.mark.parametrize("field,value", [("owned_root", "relative"), ("owned_root", r"\\server\share"),
    ("owned_root", r"\\?\C:\owned"), ("owned_root", "/owned/.env.case"),
    ("owned_root", "/owned/../escape"), ("owned_root", "/owned/NUL.txt"),
    ("file_name", "../escape.jsonl"), ("file_name", "CON.jsonl"),
    ("file_name", "hb-X.jsonl"), ("file_name", "hb-x.jsonl:ads"), ("file_name", "hb-.jsonl"),
    ("file_name", "hb-x.jsonl/other")])
def test_archive_path_schema_rejects_before_metadata(field, value, tmp_path, monkeypatch):
    module, fn = _api()
    report, _, _ = _report(tmp_path)
    args = {"owned_root": str(tmp_path), "file_name": "hb-good.jsonl"}
    args[field] = value
    calls = []
    def forbidden(*args, **kwargs):
        calls.append(1)
        raise AssertionError("bad path reached metadata")
    with monkeypatch.context() as patch:
        patch.setattr(Path, "lstat", forbidden)
        patch.setattr(Path, "resolve", forbidden)
        patch.setattr(module, "_open_file", forbidden)
        result = fn(report, **args)
    assert result["write_state"] == "REFUSED" and not calls


def test_archive_invalid_path_object_is_never_coerced(tmp_path):
    _, fn = _api()
    report, _, _ = _report(tmp_path)
    class Explosive:
        def __fspath__(self):
            raise AssertionError("fspath")
        def __str__(self):
            raise AssertionError("str")
    result = fn(report, owned_root=Explosive(), file_name="hb-x.jsonl")
    assert result["write_state"] == "REFUSED" and result["error_type"] == "ValueError"


def test_archive_relative_source_is_negative_control(tmp_path, monkeypatch):
    module, fn = _api()
    report, _, _ = _report(tmp_path)
    report["input"]["source"] = "relative-source.json"
    def forbidden(*args, **kwargs):
        raise AssertionError("relative source reached metadata")
    monkeypatch.setattr(Path, "lstat", forbidden)
    result = fn(report, owned_root=str(tmp_path), file_name="hb-x.jsonl")
    assert result["write_state"] == "REFUSED" and result["error_type"] == "ValueError"


@pytest.mark.parametrize("kind", ["missing", "symlink", "reparse", "canonical"])
def test_archive_untrusted_root_does_not_create(kind, tmp_path, monkeypatch):
    from types import SimpleNamespace
    import stat
    module, fn = _api()
    report, _, _ = _report(tmp_path)
    root = tmp_path / "missing" if kind == "missing" else tmp_path
    def forbidden(*args, **kwargs):
        raise AssertionError("bad root created a file")
    with monkeypatch.context() as patch:
        patch.setattr(module, "_open_file", forbidden)
        if kind in ("symlink", "reparse"):
            patch.setattr(Path, "lstat", lambda self: SimpleNamespace(
                st_mode=stat.S_IFLNK if kind == "symlink" else stat.S_IFDIR,
                st_file_attributes=0x400 if kind == "reparse" else 0))
        elif kind == "canonical":
            patch.setattr(Path, "resolve", lambda *a, **k: tmp_path / "different")
        result = fn(report, owned_root=str(root), file_name="hb-x.jsonl")
    assert result["write_state"] == "REFUSED" and result["phase"] == "root"
    assert not (tmp_path / "hb-x.jsonl").exists()


def test_archive_lexical_source_target_alias_refuses(tmp_path):
    _, fn = _api()
    report, _, _ = _report(tmp_path)
    report["input"]["source"] = str(tmp_path / "hb-alias.jsonl")
    result = fn(report, owned_root=str(tmp_path), file_name="hb-alias.jsonl")
    assert result["write_state"] == "REFUSED" and not (tmp_path / "hb-alias.jsonl").exists()


def test_archive_snapshot_is_fixed_before_root_io(tmp_path, monkeypatch):
    module, fn = _api()
    report, _, _ = _report(tmp_path)
    original = copy.deepcopy(report)
    real_lstat = Path.lstat
    def mutate_after_snapshot(path, *args, **kwargs):
        report["capture"]["capture_id"] = "changed by caller"
        return real_lstat(path, *args, **kwargs)
    monkeypatch.setattr(Path, "lstat", mutate_after_snapshot)
    result = fn(report, owned_root=str(tmp_path), file_name="hb-fixed.jsonl")
    assert result["write_state"] == "WRITTEN_READ_BACK"
    assert (tmp_path / "hb-fixed.jsonl").read_bytes() == _line(original)


def test_archive_two_creates_have_at_most_one_winner(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    module, fn = _api()
    report, _, _ = _report(tmp_path)
    real_open = module._open_file
    barrier = Barrier(2)
    def racing(path, mode):
        if mode == "xb":
            barrier.wait(timeout=5)
        return real_open(path, mode)
    monkeypatch.setattr(module, "_open_file", racing)
    def call():
        return fn(copy.deepcopy(report), owned_root=str(tmp_path), file_name="hb-race.jsonl")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: call(), range(2)))
    assert sorted(r["write_state"] for r in results) == ["REFUSED", "WRITTEN_READ_BACK"]
    assert (tmp_path / "hb-race.jsonl").read_bytes() == _line(report)


@pytest.mark.parametrize("fault,phase", [("create", "create"), ("created-then-error", "create"),
    ("write", "write"), ("short", "write"), ("postcreate-exists", "write"), ("flush", "flush"),
    ("fsync", "fsync"), ("close", "close"), ("readback_open", "readback_open"),
    ("readback_read", "readback_read"), ("readback_close", "readback_close"),
    ("mismatch", "readback_compare"), ("cleanup", "write")])
def test_archive_io_failures_keep_unknown_and_never_retry(fault, phase, tmp_path, monkeypatch):
    module, fn = _api()
    report, _, _ = _report(tmp_path)
    target = tmp_path / "hb-fault.jsonl"
    expected = _line(report)
    real_open = module._open_file
    opened, closes = [], {"xb": 0, "rb": 0}
    class Handle:
        def __init__(self, handle, mode):
            self.handle, self.mode = handle, mode
        def write(self, data):
            if fault in ("write", "cleanup", "postcreate-exists"):
                self.handle.write(data[:3])
                raise FileExistsError("aftercreate") if fault == "postcreate-exists" else OSError("write")
            if fault == "short":
                return self.handle.write(data[:3])
            return self.handle.write(data)
        def flush(self):
            if fault == "flush":
                raise OSError("flush")
            return self.handle.flush()
        def fileno(self):
            return self.handle.fileno()
        def read(self):
            if fault == "readback_read":
                raise OSError("read")
            return b"wrong" if fault == "mismatch" else self.handle.read()
        def close(self):
            closes[self.mode] += 1
            self.handle.close()
            if fault == "cleanup" and self.mode == "xb":
                raise ValueError("cleanup")
            if (fault == "close" and self.mode == "xb") or (fault == "readback_close" and self.mode == "rb"):
                raise OSError("close")
    def failing_open(path, mode):
        assert Path(path) == target
        opened.append(mode)
        if mode == "xb" and fault == "create":
            raise PermissionError("uncertain create")
        if mode == "xb" and fault == "created-then-error":
            handle = real_open(path, mode)
            handle.close()
            raise OSError("created before raised open")
        if mode == "rb" and fault == "readback_open":
            raise OSError("readback open")
        return Handle(real_open(path, mode), mode)
    with monkeypatch.context() as patch:
        patch.setattr(module, "_open_file", failing_open)
        if fault == "fsync":
            def fsync(fd):
                raise OSError("fsync")
            patch.setattr(os, "fsync", fsync)
        result = fn(report, owned_root=str(tmp_path), file_name=target.name)
    assert result["status"] == "NOT_EVALUATED" and result["write_state"] == "UNKNOWN"
    assert result["phase"] == phase and result["error_type"]
    assert opened.count("xb") == 1 and opened.count("rb") <= 1
    assert all(n <= 1 for n in closes.values())
    assert result["created"] is (None if phase == "create" else True)
    assert result["cleanup_error_type"] == ("ValueError" if fault == "cleanup" else None)
    if fault == "create":
        assert not target.exists()
    else:
        actual = target.read_bytes()
        if fault == "created-then-error": assert actual == b""
        elif fault in ("write", "short", "cleanup", "postcreate-exists"): assert actual == expected[:3]
        else: assert actual == expected
    assert not list(tmp_path.glob("*.lock"))


def test_archive_baseexception_propagates_after_one_cleanup(tmp_path, monkeypatch):
    module, fn = _api()
    report, _, _ = _report(tmp_path)
    real_open = module._open_file
    closes = []
    class Handle:
        def __init__(self, handle): self.handle = handle
        def write(self, data):
            self.handle.write(data[:2])
            raise SystemExit("owned cancellation")
        def close(self):
            closes.append(1)
            self.handle.close()
    monkeypatch.setattr(module, "_open_file", lambda path, mode: Handle(real_open(path, mode)))
    with pytest.raises(SystemExit, match="owned cancellation"):
        fn(report, owned_root=str(tmp_path), file_name="hb-cancel.jsonl")
    assert closes == [1] and (tmp_path / "hb-cancel.jsonl").read_bytes() == _line(report)[:2]


def test_archive_source_fs_is_never_used(tmp_path, monkeypatch):
    module, fn = _api()
    report, raw, source = _report(tmp_path)
    before = source.stat().st_mtime_ns
    real_stat, real_lstat, real_resolve = Path.stat, Path.lstat, Path.resolve
    def protect(method):
        def call(path, *args, **kwargs):
            assert path != source, "original source metadata touched"
            return method(path, *args, **kwargs)
        return call
    with monkeypatch.context() as patch:
        patch.setattr(Path, "stat", protect(real_stat))
        patch.setattr(Path, "lstat", protect(real_lstat))
        patch.setattr(Path, "resolve", protect(real_resolve))
        result = fn(report, owned_root=str(tmp_path), file_name="hb-nosource.jsonl")
    assert result["write_state"] == "WRITTEN_READ_BACK"
    assert (source.read_bytes(), source.stat().st_mtime_ns) == (raw, before)


def test_archive_cold_import_has_no_application_io_or_runtime_import(monkeypatch):
    import builtins
    import socket
    import subprocess
    import sys
    import time
    original_import = builtins.__import__
    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        assert name not in ("src.podlib", "src.halt_store", "src.heartbeat", "src.secret_scan")
        assert not (name == "src" and any(x in ("podlib", "halt_store", "heartbeat", "secret_scan") for x in fromlist))
        return original_import(name, globals, locals, fromlist, level)
    def forbidden(*args, **kwargs):
        raise AssertionError("cold import performed application IO")
    with monkeypatch.context() as patch:
        for name in ("src.m2_archive", "src.m2_report", "src.book_staleness", "src.podlib",
                     "src.halt_store", "src.heartbeat", "src.secret_scan"):
            patch.delitem(sys.modules, name, raising=False)
        patch.setattr(builtins, "__import__", guarded_import)
        patch.setattr(builtins, "open", forbidden)
        for attr in ("resolve", "stat", "lstat", "open", "read_bytes", "write_bytes", "mkdir", "glob", "rglob"):
            patch.setattr(Path, attr, forbidden)
        patch.setattr(os, "open", forbidden)
        patch.setattr(os, "scandir", forbidden)
        patch.setattr(socket, "socket", forbidden)
        patch.setattr(subprocess, "Popen", forbidden)
        patch.setattr(time, "sleep", forbidden)
        module = importlib.import_module("src.m2_archive")
        assert callable(module.archive_capture_once)
