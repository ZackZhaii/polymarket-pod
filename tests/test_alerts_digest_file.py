"""#244-B: selected synthetic file capture through the real byte kernel."""
from __future__ import annotations

import copy
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile

import pytest


def _core():
    assert importlib.util.find_spec("src.alerts_digest_file") is not None, "missing actual selected-file digest API"
    return importlib.import_module("src.alerts_digest_file")


@pytest.fixture
def owned():
    # Short owned case names avoid Windows path-length fixture failures.
    root = Path(r"C:/Users/Noise/.codex/visualizations/2026/10/21/01a15d32-5ffb-7373-8ca9-d94e8e1f04ca/project-advance-20261103/alerts-digest-file-source-owned-20261105/inputs") if os.name == "nt" else Path("/tmp/pod-alerts-digest-file-ci-owned/inputs")
    root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="fc-", dir=root))


def _line(**changes):
    row = {"ts": "2026-11-05T00:00:00+09:00", "from": "Sentinel", "alert": "reconnect_storm",
           "severity": "sev1", "proc": "paper", "detail": "\u6c49\U0001f600", "exchange_action": "none"}
    row.update(changes)
    return json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\r\n"


def test_real_capture_matches_kernel_and_returned_cursor_on_append(owned):
    mod = _core()
    kernel = importlib.import_module("src.alerts_digest")
    path = owned / "a.jsonl"
    payload = _line() + _line(alert="settlement_conflict", severity="warn")
    path.write_bytes(payload)
    before = path.stat()
    first = mod.digest_file_once(str(path))
    assert first == kernel.digest_snapshot(payload)
    assert len(first) == 11 and first["input_bytes"] == len(payload)
    assert first["input_sha256"] == hashlib.sha256(payload).hexdigest()
    assert first["delivery"] == "NOT_DELIVERED" and first["runtime_health"] == "NOT_EVALUATED"
    assert path.read_bytes() == payload and path.stat().st_mtime_ns == before.st_mtime_ns
    cursor = first["next_cursor"]
    saved = copy.deepcopy(cursor)
    appended = payload + _line(alert="scan_too_old", severity="info")
    path.write_bytes(appended)
    second = mod.digest_file_once(str(path), cursor=cursor)
    assert second == kernel.digest_snapshot(appended, cursor=saved)
    assert second["new_rows"] == 1 and second["rows_total"] == 3 and cursor == saved


def test_invalid_snapshot_propagates_real_kernel_failure_not_empty_digest(owned):
    mod = _core()
    kernel = importlib.import_module("src.alerts_digest")
    path = owned / "a.jsonl"
    path.write_bytes(b"{broken}\n")
    with pytest.raises(kernel.DigestInputError, match="^json$"):
        mod.digest_file_once(str(path))


def _install(monkeypatch, mod, path, faults=None):
    """Trace actual selected-file I/O; kernel positive control stays real."""
    from types import SimpleNamespace

    faults = {} if faults is None else faults
    g = mod.digest_file_once.__globals__
    original = {name: g[name] for name in ("_lstat", "_fstat", "_open", "digest_snapshot")}
    events = []
    counts = {"lstat": 0, "fstat": 0}

    def invoke(name, operation):
        events.append(name)
        effect = faults.get(name)
        if isinstance(effect, BaseException):
            raise effect
        value = operation()
        return effect(value) if effect is not None else value

    class Handle:
        def __init__(self, actual):
            self.actual = actual

        def fileno(self):
            return self.actual.fileno()

        def read(self):
            return invoke("read", self.actual.read)

        def close(self):
            # Close the real owned handle even when injecting a cleanup failure.
            self.actual.close()
            return invoke("close", lambda: None)

    def lstat(selected):
        assert selected == str(path)
        counts["lstat"] += 1
        return invoke("lstat" + str(counts["lstat"]), lambda: original["_lstat"](selected))

    def fstat(fd):
        counts["fstat"] += 1
        return invoke("fstat" + str(counts["fstat"]), lambda: original["_fstat"](fd))

    def opened(selected, mode):
        assert selected == str(path) and mode == "rb"
        return invoke("open", lambda: Handle(original["_open"](selected, mode)))

    def kernel(payload, *, cursor=None):
        return invoke("kernel", lambda: original["digest_snapshot"](payload, cursor=cursor))

    monkeypatch.setitem(g, "_lstat", lstat)
    monkeypatch.setitem(g, "_fstat", fstat)
    monkeypatch.setitem(g, "_open", opened)
    monkeypatch.setitem(g, "digest_snapshot", kernel)
    return events


def _changed(info, **changes):
    from types import SimpleNamespace

    fields = ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")
    result = {name: getattr(info, name) for name in fields}
    result["st_file_attributes"] = getattr(info, "st_file_attributes", 0)
    result.update(changes)
    return SimpleNamespace(**result)


def test_actual_order_single_read_close_then_real_kernel(owned, monkeypatch):
    mod = _core()
    path = owned / "a.jsonl"
    payload = _line()
    path.write_bytes(payload)
    events = _install(monkeypatch, mod, path)
    result = mod.digest_file_once(str(path))
    assert events == ["lstat1", "open", "fstat1", "read", "fstat2", "lstat2", "close", "kernel"]
    assert result["input_sha256"] == hashlib.sha256(payload).hexdigest()
    assert result["new_rows"] == 1 and len(result) == 11


def test_empty_file_and_eof_cursor_are_real_normal_returns(owned):
    mod = _core()
    path = owned / "a.jsonl"
    path.write_bytes(b"")
    first = mod.digest_file_once(str(path))
    second = mod.digest_file_once(str(path), cursor=first["next_cursor"])
    assert first == second and first["status"] == "NO_NEW_ALERTS"
    assert first["input_bytes"] == first["rows_total"] == first["new_rows"] == 0


@pytest.mark.parametrize("suffix", [
    "/./a", "/../a", "/.env", "/.ENV.local/a", "/$HOME/a", "/%TEMP%/a", "/~/a",
    "/CON", "/nul.txt", "/COM1.log", "/LPT9", "/COM\u00b9.txt", "/a.", "/a ", "/a:stream",
    "/a\x00", "/a\x1f", "/a\ud800",
])
def test_raw_lexical_rejections_do_no_application_io(owned, monkeypatch, suffix):
    mod = _core()
    events = []
    def denied(*args, **kwargs):
        events.append(args)
        raise AssertionError("lexically rejected path reached application I/O")
    g = mod.digest_file_once.__globals__
    for name in ("_lstat", "_open", "_fstat", "digest_snapshot"):
        monkeypatch.setitem(g, name, denied)
    with pytest.raises(mod.DigestFileError, match="^path$"):
        mod.digest_file_once(str(owned) + suffix)
    assert events == []


@pytest.mark.parametrize("value", [
    None, True, 1, b"/a", "", "relative", "C:relative", r"\\server\share\a",
    r"\\?\C:\a", r"\\.\C:\a", "//server/share/a",
])
def test_wrong_type_relative_and_device_paths_are_pre_io(value, monkeypatch):
    mod = _core()
    def denied(*args, **kwargs):
        raise AssertionError("path rejection reached I/O")
    for name in ("_lstat", "_open", "_fstat", "digest_snapshot"):
        monkeypatch.setitem(mod.digest_file_once.__globals__, name, denied)
    with pytest.raises(mod.DigestFileError, match="^path$"):
        mod.digest_file_once(value)


def test_pathlike_and_str_subclass_are_not_coerced(owned):
    mod = _core()
    class Str(str):
        pass
    for value in (owned / "a.jsonl", Str(str(owned / "a.jsonl"))):
        with pytest.raises(mod.DigestFileError, match="^path$"):
            mod.digest_file_once(value)


@pytest.mark.parametrize("stage,phase", [
    ("lstat1", "metadata"), ("open", "open"), ("fstat1", "metadata"),
    ("read", "read"), ("fstat2", "metadata"), ("lstat2", "metadata"), ("close", "close"),
])
def test_ordinary_file_failures_are_sanitized_and_no_kernel(owned, monkeypatch, stage, phase):
    mod = _core()
    path = owned / "a.jsonl"
    path.write_bytes(_line())
    events = _install(monkeypatch, mod, path, {stage: OSError("PRIVATE source path and data")})
    with pytest.raises(mod.DigestFileError) as caught:
        mod.digest_file_once(str(path))
    assert caught.value.args == (phase,) and caught.value.__suppress_context__
    assert "PRIVATE" not in str(caught.value) and "kernel" not in events
    assert events.count("open") == (0 if stage == "lstat1" else 1)
    assert events.count("close") == (0 if stage in ("lstat1", "open") else 1)


@pytest.mark.parametrize("field,value", [
    ("st_dev", None), ("st_ino", 0), ("st_size", False), ("st_mtime_ns", None),
    ("st_mode", 0), ("st_file_attributes", 0x400),
])
def test_unknown_nonregular_or_reparse_metadata_refuses_before_open(owned, monkeypatch, field, value):
    mod = _core()
    path = owned / "a.jsonl"
    path.write_bytes(_line())
    events = _install(monkeypatch, mod, path, {"lstat1": lambda info: _changed(info, **{field: value})})
    with pytest.raises(mod.DigestFileError, match="^metadata$"):
        mod.digest_file_once(str(path))
    assert events == ["lstat1"]


def test_actual_directory_is_not_opened(owned, monkeypatch):
    mod = _core()
    events = _install(monkeypatch, mod, owned)
    with pytest.raises(mod.DigestFileError, match="^metadata$"):
        mod.digest_file_once(str(owned))
    assert events == ["lstat1"]


def test_actual_symlink_is_not_followed(owned, monkeypatch):
    mod = _core()
    target, link = owned / "a.jsonl", owned / "s.jsonl"
    target.write_bytes(_line())
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("platform does not permit owned synthetic symlink creation")
    events = _install(monkeypatch, mod, link)
    with pytest.raises(mod.DigestFileError, match="^metadata$"):
        mod.digest_file_once(str(link))
    assert events == ["lstat1"]


@pytest.mark.parametrize("stage,field", [
    ("fstat1", "st_ino"), ("fstat2", "st_size"), ("fstat2", "st_mtime_ns"),
    ("fstat2", "st_ctime_ns"), ("lstat2", "st_ino"), ("lstat2", "st_mode"),
])
def test_identity_or_change_window_mismatch_fails_closed(owned, monkeypatch, stage, field):
    mod = _core()
    path = owned / "a.jsonl"
    path.write_bytes(_line())
    def alter(info):
        return _changed(info, **{field: getattr(info, field) + (0o100 if field == "st_mode" else 1)})
    events = _install(monkeypatch, mod, path, {stage: alter})
    with pytest.raises(mod.DigestFileError, match="^changed$"):
        mod.digest_file_once(str(path))
    assert events.count("read") == (0 if stage == "fstat1" else 1)
    assert events.count("close") == 1 and "kernel" not in events


def test_real_file_growth_after_read_is_rejected_without_reread(owned, monkeypatch):
    mod = _core()
    path = owned / "a.jsonl"
    payload = _line()
    path.write_bytes(payload)
    def grow(captured):
        path.write_bytes(payload + _line(alert="scan_too_old"))
        return captured
    events = _install(monkeypatch, mod, path, {"read": grow})
    with pytest.raises(mod.DigestFileError, match="^changed$"):
        mod.digest_file_once(str(path))
    assert events.count("read") == events.count("close") == 1 and "kernel" not in events


@pytest.mark.parametrize("replacement", [b"", b"x", bytearray(b"x"), None, "text"])
def test_short_or_nonbytes_capture_yields_no_summary(owned, monkeypatch, replacement):
    mod = _core()
    path = owned / "a.jsonl"
    path.write_bytes(_line())
    events = _install(monkeypatch, mod, path, {"read": lambda raw: replacement})
    with pytest.raises(mod.DigestFileError, match="^read$"):
        mod.digest_file_once(str(path))
    assert events.count("read") == events.count("close") == 1 and "kernel" not in events


@pytest.mark.parametrize("stage", ["read", "fstat2", "close"])
@pytest.mark.parametrize("kind", [MemoryError, RecursionError, KeyboardInterrupt, SystemExit])
def test_resource_and_base_objects_escape_unchanged(owned, monkeypatch, stage, kind):
    mod = _core()
    path = owned / "a.jsonl"
    path.write_bytes(_line())
    failure = kind("injected interruption")
    events = _install(monkeypatch, mod, path, {stage: failure})
    with pytest.raises(BaseException) as caught:
        mod.digest_file_once(str(path))
    assert caught.value is failure and events.count("close") == 1 and "kernel" not in events


def test_ordinary_primary_and_ordinary_close_keep_primary_phase(owned, monkeypatch):
    mod = _core()
    path = owned / "a.jsonl"
    path.write_bytes(_line())
    events = _install(monkeypatch, mod, path, {"read": OSError("private read"), "close": OSError("private close")})
    with pytest.raises(mod.DigestFileError, match="^read$"):
        mod.digest_file_once(str(path))
    assert events.count("close") == 1 and "kernel" not in events


@pytest.mark.parametrize("kind", [MemoryError, RecursionError, KeyboardInterrupt, SystemExit])
def test_primary_resource_identity_survives_ordinary_cleanup(owned, monkeypatch, kind):
    mod = _core()
    path = owned / "a.jsonl"
    path.write_bytes(_line())
    primary = kind("primary")
    events = _install(monkeypatch, mod, path, {"read": primary, "close": OSError("cleanup")})
    with pytest.raises(BaseException) as caught:
        mod.digest_file_once(str(path))
    assert caught.value is primary and events.count("close") == 1 and "kernel" not in events


@pytest.mark.parametrize("kind", [MemoryError, RecursionError, KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("primary", [OSError("read failed"), MemoryError("primary resource")])
def test_cleanup_resource_base_itself_takes_precedence(owned, monkeypatch, kind, primary):
    mod = _core()
    path = owned / "a.jsonl"
    path.write_bytes(_line())
    cleanup = kind("cleanup interruption")
    events = _install(monkeypatch, mod, path, {"read": primary, "close": cleanup})
    with pytest.raises(BaseException) as caught:
        mod.digest_file_once(str(path))
    assert caught.value is cleanup and events.count("close") == 1 and "kernel" not in events


def test_invalid_cursor_is_rejected_by_real_kernel_after_capture(owned, monkeypatch):
    mod = _core()
    kernel = importlib.import_module("src.alerts_digest")
    path = owned / "a.jsonl"
    path.write_bytes(_line())
    events = _install(monkeypatch, mod, path)
    with pytest.raises(kernel.DigestInputError, match="^cursor$"):
        mod.digest_file_once(str(path), cursor={"invalid": True})
    assert events == ["lstat1", "open", "fstat1", "read", "fstat2", "lstat2", "close", "kernel"]


@pytest.mark.parametrize("code", [0, 5])
def test_kernel_systemexit_is_not_adopted_as_normal_return(owned, monkeypatch, code):
    mod = _core()
    path = owned / "a.jsonl"
    path.write_bytes(_line())
    failure = SystemExit(code)
    events = _install(monkeypatch, mod, path, {"kernel": failure})
    adopted = None
    with pytest.raises(SystemExit) as caught:
        adopted = mod.digest_file_once(str(path))
    assert adopted is None and caught.value is failure and events[-2:] == ["close", "kernel"]


def test_fresh_namespace_import_has_no_application_io(owned, monkeypatch):
    import builtins
    import socket
    import subprocess
    import time

    mod = _core()
    source = Path(mod.__file__).read_text(encoding="utf-8")
    code = compile(source, mod.__file__, "exec")  # code I/O is deliberately before tripwire
    importlib.import_module("ntpath")
    importlib.import_module("stat")
    events = []
    def denied(*args, **kwargs):
        events.append((args, kwargs))
        raise AssertionError("cold namespace performed application I/O")
    namespace = {"__name__": "src._cold_file_digest", "__package__": "src"}
    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", denied)
        patch.setattr(builtins, "print", denied)
        patch.setattr(os, "lstat", denied)
        patch.setattr(os, "fstat", denied)
        patch.setattr(os, "open", denied)
        patch.setattr(Path, "write_bytes", denied)
        patch.setattr(Path, "write_text", denied)
        patch.setattr(socket, "socket", denied)
        patch.setattr(subprocess, "Popen", denied)
        patch.setattr(time, "time", denied)
        patch.setattr(time, "monotonic", denied)
        exec(code, namespace)
    assert events == [] and callable(namespace["digest_file_once"])


def test_module_has_no_cli_or_exit_registry_surface():
    import ast
    mod = _core()
    tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
    assert not any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in ("main", "__main__") for node in ast.walk(tree))
    assert not any(isinstance(node, ast.Name) and node.id.startswith("EXIT_") for node in ast.walk(tree))


def test_stable_per_interface_ctime_difference_does_not_fake_replacement(owned, monkeypatch):
    mod = _core()
    path = owned / "a.jsonl"
    payload = _line()
    path.write_bytes(payload)
    # Python/Windows lstat and fstat can expose different ctime semantics.
    # Identity is cross-checked; each interface's full metadata stays stable.
    def fd_view(info):
        return _changed(info, st_ctime_ns=info.st_ctime_ns + 100)
    events = _install(monkeypatch, mod, path, {"fstat1": fd_view, "fstat2": fd_view})
    result = mod.digest_file_once(str(path))
    assert result["input_sha256"] == hashlib.sha256(payload).hexdigest()
    assert events == ["lstat1", "open", "fstat1", "read", "fstat2", "lstat2", "close", "kernel"]


@pytest.mark.skipif(os.name != "nt", reason="Windows reparse metadata availability")
def test_missing_windows_reparse_metadata_is_not_assumed_safe(owned, monkeypatch):
    from types import SimpleNamespace
    mod = _core()
    path = owned / "a.jsonl"
    path.write_bytes(_line())
    def missing_flag(info):
        fields = ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")
        return SimpleNamespace(**{name: getattr(info, name) for name in fields})
    events = _install(monkeypatch, mod, path, {"lstat1": missing_flag})
    with pytest.raises(mod.DigestFileError, match="^metadata$"):
        mod.digest_file_once(str(path))
    assert events == ["lstat1"]


@pytest.mark.parametrize("primary", [None, OSError("primary file error"), MemoryError("primary resource")])
def test_any_nonresource_cleanup_exception_cannot_hide_primary(owned, monkeypatch, primary):
    mod = _core()
    path = owned / "a.jsonl"
    path.write_bytes(_line())
    faults = {"close": RuntimeError("private ordinary cleanup")}
    if primary is not None:
        faults["read"] = primary
    events = _install(monkeypatch, mod, path, faults)
    with pytest.raises(BaseException) as caught:
        mod.digest_file_once(str(path))
    if isinstance(primary, MemoryError):
        assert caught.value is primary
    else:
        assert isinstance(caught.value, mod.DigestFileError)
        assert caught.value.args == ("read" if primary is not None else "close",)
    assert events.count("close") == 1 and "kernel" not in events
