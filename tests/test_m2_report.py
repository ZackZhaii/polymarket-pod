"""EK: inventory evidence, never infer M2 acceptance from readable input."""
import builtins
import hashlib
import importlib
import json  # Warm stdlib before the module-body IO probe; loader IO is excluded.
import math
import os
from pathlib import Path
import socket
import stat
import subprocess

import pytest


FIXTURES = Path(__file__).parent / "fixtures" / "m2_report"


def inventory(inputs):
    # Missing feature is a test failure, not a collection/import error in RED.
    assert importlib.util.find_spec("src.m2_report") is not None, "inventory module not implemented"
    module = importlib.import_module("src.m2_report")
    assert callable(getattr(module, "inventory_inputs", None)), "inventory callable not implemented"
    return module.inventory_inputs(inputs)


def spec(path, fmt="json"):
    return {"source": str(path), "format": fmt}


def assert_not_evaluated(report, completeness):
    assert report["status"] == "NOT_EVALUATED"
    assert report["completeness"] == completeness
    assert "PASS" not in report.values()


@pytest.mark.parametrize("fmt,name,records", [("json", "valid.json", 1), ("jsonl", "valid.jsonl", 2)])
def test_valid_fixture_is_only_inventoried(fmt, name, records):
    path = FIXTURES / name
    raw = path.read_bytes()
    before = (raw, path.stat().st_mtime_ns)
    report = inventory([spec(path, fmt)])
    assert_not_evaluated(report, "INVENTORIED")
    entry = report["inputs"][0]
    assert entry["source"] == str(path) and entry["format"] == fmt
    assert entry["sha256"] == hashlib.sha256(raw).hexdigest()
    assert entry["bytes"] == len(raw) and entry["records"] == records
    assert entry["problems"] == []
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


@pytest.mark.parametrize("raw", [b"null", b"true", b"7", b"0.5", b'"text"', b"[]", b"{}"])
def test_json_counts_documents_not_business_samples(raw, tmp_path):
    path = tmp_path / "document.json"
    path.write_bytes(raw)
    report = inventory([spec(path)])
    assert_not_evaluated(report, "INVENTORIED")
    assert report["inputs"][0]["records"] == 1


@pytest.mark.parametrize("raw,problem", [
    (b'{"x":1,"x":2}', "duplicate_key"),
    (b'{"nested":[{"x":1,"x":2}]}', "duplicate_key"),
    (b'{"a":1,"\\u0061":2}', "duplicate_key"),
    (b'{"x":NaN}', "non_finite"),
    (b'[Infinity]', "non_finite"),
    (b'{"x":-Infinity}', "non_finite"),
    (b'{"x":[1e999]}', "non_finite"),
    (b'{"x":', "invalid_json"),
    (b'{} {}', "invalid_json"),
    (b'\xff', "invalid_utf8"),
])
def test_bad_json_retains_raw_fingerprint(raw, problem, tmp_path):
    path = tmp_path / "bad.json"
    path.write_bytes(raw)
    before = (raw, path.stat().st_mtime_ns)
    report = inventory([spec(path)])
    assert_not_evaluated(report, "INVALID")
    entry = report["inputs"][0]
    assert any(problem in p for p in entry["problems"])
    assert entry["sha256"] == hashlib.sha256(raw).hexdigest()
    assert entry["bytes"] == len(raw) and entry["records"] is None
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


@pytest.mark.parametrize("raw,problem", [
    (b'{}', "missing_final_newline"),
    (b'{}\n\n', "blank_line"),
    (b' \t\n', "blank_line"),
    (b'{}\n{"x":\n', "invalid_json"),
    (b'{}\n{"x":', "invalid_json"),
    (b'{}\n[]\n', "non_object"),
    (b'{}\n{"x":1,"x":2}\n', "duplicate_key"),
    (b'{}\n{"x":1e999}\n', "non_finite"),
    (b'{}\n\xff\n', "invalid_utf8"),
])
def test_bad_jsonl_never_certifies_a_partial_denominator(raw, problem, tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_bytes(raw)
    report = inventory([spec(path, "jsonl")])
    assert_not_evaluated(report, "INVALID")
    entry = report["inputs"][0]
    assert entry["sha256"] == hashlib.sha256(raw).hexdigest() and entry["bytes"] == len(raw)
    assert entry["records"] is None and any(problem in p for p in entry["problems"])


def test_jsonl_crlf_and_unicode_separators_are_not_extra_records(tmp_path):
    path = tmp_path / "unicode.jsonl"
    raw = '{"s":"甲\u2028乙"}\r\n{"s":"ok"}\r\n'.encode("utf-8")
    path.write_bytes(raw)
    report = inventory([spec(path, "jsonl")])
    assert_not_evaluated(report, "INVENTORIED")
    assert report["inputs"][0]["records"] == 2
    assert report["inputs"][0]["sha256"] == hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize("fmt", ["json", "jsonl"])
def test_missing_and_empty_are_distinct_incomplete_evidence(fmt, tmp_path):
    empty = tmp_path / "empty"
    empty.write_bytes(b"")
    missing = tmp_path / "missing"
    report = inventory([spec(missing, fmt), spec(empty, fmt)])
    assert_not_evaluated(report, "INCOMPLETE")
    a, b = report["inputs"]
    assert a["sha256"] is None and a["bytes"] is None and a["records"] is None
    assert "missing_source" in a["problems"]
    assert b["sha256"] == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert b["bytes"] == 0 and b["records"] is None and "empty_input" in b["problems"]


def test_no_sources_cannot_be_a_complete_inventory():
    report = inventory([])
    assert_not_evaluated(report, "INCOMPLETE")
    assert report["inputs"] == [] and report["problems"]


@pytest.mark.parametrize("kind", ["same", "dot", "hardlink"])
def test_aliases_are_not_independent_inputs(kind, tmp_path):
    first = tmp_path / "first.json"
    first.write_bytes(b"{}")
    if kind == "same":
        second = first
    elif kind == "dot":
        (tmp_path / "sub").mkdir()
        second = tmp_path / "sub" / ".." / "first.json"
    else:
        second = tmp_path / "linked.json"
        os.link(first, second)
    report = inventory([spec(first), spec(second)])
    assert_not_evaluated(report, "INVALID")
    assert report["inputs"][1]["duplicate_of"] == 0
    assert any("duplicate" in p for p in report["inputs"][1]["problems"])


def test_equal_bytes_in_distinct_files_are_not_aliases(tmp_path):
    paths = [tmp_path / "a.json", tmp_path / "b.json"]
    for path in paths:
        path.write_bytes(b"{}")
    report = inventory([spec(p) for p in paths])
    assert_not_evaluated(report, "INVENTORIED")
    assert all(e["duplicate_of"] is None for e in report["inputs"])
    assert report["inputs"][0]["sha256"] == report["inputs"][1]["sha256"]


@pytest.mark.parametrize("inputs", [None, "file.json", [{}], [{"source": "x", "format": "yaml"}]])
def test_invalid_descriptors_do_not_guess_defaults(inputs):
    report = inventory(inputs)
    assert_not_evaluated(report, "INVALID")
    assert report["problems"] or any(e["problems"] for e in report["inputs"])


def test_directory_input_is_not_scanned(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("directory scan attempted")
    monkeypatch.setattr(os, "scandir", forbidden)
    report = inventory([spec(tmp_path)])
    assert_not_evaluated(report, "INVALID")
    assert "not_regular_file" in report["inputs"][0]["problems"]


def test_environment_file_is_rejected_without_reading(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_bytes(b"owned-test-fixture-not-a-secret")
    def forbidden(*args, **kwargs):
        raise AssertionError("environment input opened")
    monkeypatch.setattr(os, "open", forbidden)
    report = inventory([spec(path)])
    assert_not_evaluated(report, "INVALID")
    assert report["inputs"][0]["sha256"] is None
    assert "forbidden_source" in report["inputs"][0]["problems"]


def test_import_body_has_zero_io(monkeypatch):
    path = FIXTURES.parents[2] / "src" / "m2_report.py"
    assert path.is_file(), "inventory module not implemented"
    code = compile(path.read_text(encoding="utf-8"), str(path), "exec")
    def forbidden(*args, **kwargs):
        raise AssertionError("module import attempted IO")
    for attr in ("open", "read_bytes", "read_text", "resolve", "stat", "glob", "rglob"):
        monkeypatch.setattr(Path, attr, forbidden)
    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(os, "open", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    namespace = {"__name__": "m2_inventory_import_probe", "__file__": str(path)}
    exec(code, namespace)
    assert callable(namespace["inventory_inputs"])


def test_call_writes_nothing_and_does_not_touch_business_runtime(tmp_path, monkeypatch):
    path = tmp_path / "input.json"
    path.write_bytes(b'{"v":1}')
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    real_open = os.open
    def readonly_open(name, flags, *args, **kwargs):
        assert not flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
        assert Path(name) == path
        return real_open(name, flags, *args, **kwargs)
    def forbidden(*args, **kwargs):
        raise AssertionError("write, directory scan or business runtime attempted")
    monkeypatch.setattr(os, "open", readonly_open)
    monkeypatch.setattr(os, "scandir", forbidden)
    monkeypatch.setattr(Path, "write_bytes", forbidden)
    monkeypatch.setattr(Path, "write_text", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    report = inventory([spec(path)])
    assert_not_evaluated(report, "INVENTORIED")
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_captured_bytes_survive_a_later_metadata_error(tmp_path, monkeypatch):
    path = tmp_path / "captured.json"
    raw = b'{"v":1}'
    path.write_bytes(raw)
    real_fstat = os.fstat
    calls = 0
    def fail_after_read(fd):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("owned injected post-read metadata error")
        return real_fstat(fd)
    monkeypatch.setattr(os, "fstat", fail_after_read)
    report = inventory([spec(path)])
    assert_not_evaluated(report, "INCOMPLETE")
    entry = report["inputs"][0]
    assert entry["sha256"] == hashlib.sha256(raw).hexdigest()
    assert entry["bytes"] == len(raw) and entry["records"] is None
    assert "metadata_unavailable" in entry["problems"]


def test_invalid_nul_path_is_a_diagnostic_not_a_reader_crash():
    report = inventory([{"source": "bad\x00path.json", "format": "json"}])
    assert_not_evaluated(report, "INVALID")
    assert "invalid_source" in report["inputs"][0]["problems"]
    assert report["inputs"][0]["sha256"] is None
    assert report["inputs"][0]["source"] == "bad\x00path.json"


@pytest.mark.parametrize("same_physical", [False, True])
def test_physical_identity_wins_over_a_colliding_lexical_normalizer(same_physical, tmp_path, monkeypatch):
    # Fault injection represents the suspect lexical collision without claiming
    # this Windows directory has POSIX symlink/.. or case-sensitive semantics.
    # File reads, stat and identities remain real; only normalization collides.
    first, second = tmp_path / "one.json", tmp_path / "two.json"
    first.write_bytes(b"{}")
    if same_physical:
        os.link(first, second)
    else:
        second.write_bytes(b'{"v":2}')
    assert os.path.samefile(first, second) is same_physical
    real_os = os
    class PathView:
        def normcase(self, value):
            return "controlled-lexical-collision"
        def __getattr__(self, name):
            return getattr(real_os.path, name)
    class OSView:
        path = PathView()
        def __getattr__(self, name):
            return getattr(real_os, name)
    module = importlib.import_module("src.m2_report")
    monkeypatch.setattr(module, "os", OSView())
    report = inventory([spec(first), spec(second)])
    assert_not_evaluated(report, "INVALID" if same_physical else "INVENTORIED")
    assert report["inputs"][1]["duplicate_of"] == (0 if same_physical else None)


def test_native_directory_link_dotdot_uses_actual_file_identity(tmp_path):
    local, other = tmp_path / "local", tmp_path / "other"
    local.mkdir()
    (other / "deep").mkdir(parents=True)
    (local / "x.json").write_bytes(b"{}")
    (other / "x.json").write_bytes(b'{"v":2}')
    link = local / "link"
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(other / "deep"), str(link))
    else:
        os.symlink(other / "deep", link, target_is_directory=True)
    first, second = link / ".." / "x.json", local / "x.json"
    same = os.path.samefile(first, second)
    # The Windows Win32 path is normalized before its junction traversal; POSIX
    # traverses the directory symlink first. Do not falsely report a Windows RED.
    assert same is (os.name == "nt")
    report = inventory([spec(first), spec(second)])
    assert_not_evaluated(report, "INVALID" if same else "INVENTORIED")
    assert report["inputs"][1]["duplicate_of"] == (0 if same else None)
