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


@pytest.mark.parametrize("source", [
    r"\\server\share\input.json",
    r"\\?\UNC\server\share\input.json",
    "//server/share/input.json",
    "//?/UNC/server/share/input.json",
    r"\/server\share\input.json",
    r"/\server/share/input.json",
    r"\\?\unc\server\share\input.json",
    r"\\.\UNC\server\share\input.json",
])
@pytest.mark.parametrize("fmt", ["json", "jsonl"])
def test_unc_rejected_before_any_filesystem_access(source, fmt, monkeypatch):
    # Removing the pre-resolution local-path gate would expose OS SMB access.
    # Sentinels replace the FS boundary; no actual UNC path is ever touched.
    module = importlib.import_module("src.m2_report")
    def forbidden(*args, **kwargs):
        raise AssertionError("nonlocal input reached a filesystem boundary")
    with monkeypatch.context() as patch:
        for attr in ("resolve", "stat", "lstat", "open", "read_bytes"):
            patch.setattr(Path, attr, forbidden)
        for attr in ("stat", "lstat", "open", "fstat"):
            patch.setattr(os, attr, forbidden)
        patch.setattr(builtins, "open", forbidden)
        report = module.inventory_inputs([{"source": source, "format": fmt}])
    assert_not_evaluated(report, "INVALID")
    entry = report["inputs"][0]
    assert entry["source"] == source and entry["format"] == fmt
    assert entry["sha256"] is None and entry["bytes"] is None and entry["records"] is None
    assert entry["duplicate_of"] is None and "nonlocal_source" in entry["problems"]
    assert report["metrics"] is None and report["denominator"] is None


@pytest.mark.parametrize("kind", ["absolute", "relative", "extended_local"])
def test_local_paths_remain_readable_after_unc_rejection(kind, tmp_path):
    path = tmp_path / "local.json"
    path.write_bytes(b"{}")
    source = str(path)
    if kind == "relative":
        source = os.path.relpath(path)
    elif kind == "extended_local" and os.name == "nt":
        source = "\\\\?\\" + str(path)
    report = inventory([{"source": source, "format": "json"}])
    assert_not_evaluated(report, "INVENTORIED")
    entry = report["inputs"][0]
    assert entry["source"] == source and entry["records"] == 1
    assert entry["sha256"] == "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"


# EL: a declared recording window is not continuous healthy coverage.
MANIFEST_FIXTURES = FIXTURES / "manifest-window"


def summarize_manifest(descriptor):
    module = importlib.import_module("src.m2_report")
    fn = getattr(module, "summarize_recording_manifest", None)
    assert callable(fn), "single-manifest summary not implemented"
    return fn(descriptor)


def declaration_payload(**changes):
    value = {"schema": "m1_run/1", "status": "completed",
             "started_at": "2026-11-03T00:00:00Z", "ended_at": "2026-11-03T00:02:00Z",
             "exit_code": 0}
    value.update(changes)
    return value


def write_manifest(tmp_path, payload):
    path = tmp_path / "manifest.json"
    path.write_bytes(json.dumps(payload).encode("utf-8"))
    return path


def assert_summary_not_evaluated(report):
    assert report["status"] == "NOT_EVALUATED"
    assert report["metrics"] is None and report["denominator"] is None
    assert report["healthy_online_us"] is None


def test_manifest_literal_offset_window_and_raw_sha_are_only_declarations():
    path = MANIFEST_FIXTURES / "valid.json"
    before = path.read_bytes(), path.stat().st_mtime_ns
    report = summarize_manifest(spec(path))
    assert_summary_not_evaluated(report)
    assert report["completeness"] == "INVENTORIED"
    assert report["input"]["sha256"] == "756723996aac4fb7071c47832a7b0035d46f06ead400e0a786416df5fcb784a9"
    assert report["input"]["bytes"] == 132 and report["input"]["records"] == 1
    assert report["input"]["source"] == str(path) and report["input"]["format"] == "json"
    assert report["declaration"] == {"schema": "m1_run/1", "status": "completed",
        "started_at": "2026-11-03T09:00:00+09:00", "ended_at": "2026-11-03T00:02:00Z"}
    assert report["started_at_utc"] == "2026-11-03T00:00:00Z"
    assert report["ended_at_utc"] == "2026-11-03T00:02:00Z"
    assert report["declared_interval_us"] == 120000000
    assert type(report["declared_interval_us"]) is int
    assert report["declaration_problems"] == []
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


@pytest.mark.parametrize("start,end,want", [
    ("2026-11-03T00:00:00Z", "2026-11-03T00:00:00Z", 0),
    ("2026-11-03T00:00:00.000001Z", "2026-11-03T00:00:00.000002Z", 1),
    ("0001-01-01T00:00:00Z", "9999-12-31T23:59:59.999999Z", 315537897599999999),
])
def test_manifest_interval_is_exact_integer_microseconds(start, end, want, tmp_path):
    path = write_manifest(tmp_path, declaration_payload(started_at=start, ended_at=end))
    report = summarize_manifest(spec(path))
    assert_summary_not_evaluated(report)
    assert report["declared_interval_us"] == want
    assert type(report["declared_interval_us"]) is int


def test_running_manifest_never_fills_missing_end_from_other_evidence():
    path = MANIFEST_FIXTURES / "running.json"
    report = summarize_manifest(spec(path))
    assert_summary_not_evaluated(report)
    assert report["input"]["records"] == 1 and report["input"]["problems"] == []
    assert report["declaration"]["status"] == "running"
    assert report["declaration"]["ended_at"] is None
    assert report["ended_at_utc"] is None and report["declared_interval_us"] is None
    assert "missing_ended_at" in report["declaration_problems"]


@pytest.mark.parametrize("field", ["started_at", "ended_at"])
@pytest.mark.parametrize("value", [None, "", "2026-11-03", "2026-11-03T00:00:00", 0, True, [], {},
    "bad", "0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00"])
def test_manifest_invalid_endpoints_keep_raw_values_and_null_interval(field, value, tmp_path):
    path = write_manifest(tmp_path, declaration_payload(**{field: value}))
    report = summarize_manifest(spec(path))
    assert_summary_not_evaluated(report)
    assert report["declaration"][field] == value
    assert report[field + "_utc"] is None and report["declared_interval_us"] is None
    reason = ("missing_" if value is None else "invalid_") + field
    assert reason in report["declaration_problems"]
    assert report["completeness"] == "INVENTORIED" and report["input"]["records"] == 1


def test_manifest_reversed_endpoints_remain_facts_but_have_no_interval(tmp_path):
    path = write_manifest(tmp_path, declaration_payload(ended_at="2026-11-02T23:59:59Z"))
    report = summarize_manifest(spec(path))
    assert_summary_not_evaluated(report)
    assert report["started_at_utc"] == "2026-11-03T00:00:00Z"
    assert report["ended_at_utc"] == "2026-11-02T23:59:59Z"
    assert report["declared_interval_us"] is None
    assert "reversed_interval" in report["declaration_problems"]


@pytest.mark.parametrize("payload,reason", [
    ([], "manifest_not_object"), (None, "manifest_not_object"),
    ({"started_at": "2026-11-03T00:00:00Z"}, "missing_schema"),
    ({"schema": "other/1", "status": "completed", "started_at": "2026-11-03T00:00:00Z",
      "ended_at": "2026-11-03T00:02:00Z"}, "invalid_schema"),
])
def test_other_json_cannot_impersonate_a_recording(payload, reason, tmp_path):
    path = write_manifest(tmp_path, payload)
    report = summarize_manifest(spec(path))
    assert_summary_not_evaluated(report)
    assert report["input"]["records"] == 1 and report["completeness"] == "INVENTORIED"
    assert all(v is None for v in report["declaration"].values())
    assert report["started_at_utc"] is None and report["ended_at_utc"] is None
    assert report["declared_interval_us"] is None and reason in report["declaration_problems"]


@pytest.mark.parametrize("key", ["status", "started_at", "ended_at"])
def test_missing_declaration_fields_have_separate_diagnostics(key, tmp_path):
    payload = declaration_payload()
    del payload[key]
    report = summarize_manifest(spec(write_manifest(tmp_path, payload)))
    assert report["input"]["problems"] == []
    assert "missing_" + key in report["declaration_problems"]
    assert report["declaration"][key] is None
    if key != "status":
        assert report["declared_interval_us"] is None


@pytest.mark.parametrize("raw", [b"", b"\xff", b'{"schema":"m1_run/1","schema":"m1_run/1"}',
    b'{"schema":"m1_run/1","x":NaN}', b'{"schema":"m1_run/1","x":1e999}', b'{"schema":'])
def test_bad_raw_manifest_keeps_fingerprint_but_no_time_facts(raw, tmp_path):
    path = tmp_path / "bad.json"
    path.write_bytes(raw)
    report = summarize_manifest(spec(path))
    assert_summary_not_evaluated(report)
    assert report["input"]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert report["input"]["bytes"] == len(raw) and report["input"]["records"] is None
    assert report["started_at_utc"] is None and report["ended_at_utc"] is None
    assert report["declared_interval_us"] is None
    assert all(v is None for v in report["declaration"].values())


def test_missing_manifest_cannot_publish_time_facts(tmp_path):
    path = tmp_path / "missing.json"
    report = summarize_manifest(spec(path))
    assert_summary_not_evaluated(report)
    assert report["completeness"] == "INCOMPLETE" and report["input"]["sha256"] is None
    assert report["declared_interval_us"] is None and "missing_source" in report["input"]["problems"]


@pytest.mark.parametrize("descriptor", [None, [], {}, {"source": "x", "format": "jsonl"}])
def test_manifest_requires_one_explicit_json_descriptor(descriptor):
    report = summarize_manifest(descriptor)
    assert_summary_not_evaluated(report)
    assert report["completeness"] == "INVALID" and report["input"]["sha256"] is None
    assert report["declared_interval_us"] is None


@pytest.mark.parametrize("source", [r"\\server\share\manifest.json", r"\\?\UNC\server\share\manifest.json"])
def test_manifest_unc_is_rejected_before_any_filesystem_access(source, monkeypatch):
    module = importlib.import_module("src.m2_report")
    fn = getattr(module, "summarize_recording_manifest", None)
    assert callable(fn), "single-manifest summary not implemented"
    def forbidden(*args, **kwargs):
        raise AssertionError("manifest UNC reached filesystem")
    with monkeypatch.context() as patch:
        for attr in ("resolve", "stat", "lstat", "open", "read_bytes"):
            patch.setattr(Path, attr, forbidden)
        for attr in ("stat", "lstat", "open", "fstat"):
            patch.setattr(os, attr, forbidden)
        patch.setattr(builtins, "open", forbidden)
        report = fn({"source": source, "format": "json"})
    assert report["completeness"] == "INVALID"
    assert report["input"]["source"] == source and report["input"]["format"] == "json"
    assert "nonlocal_source" in report["input"]["problems"] and report["input"]["sha256"] is None
    assert report["declared_interval_us"] is None


def test_manifest_hash_and_time_facts_use_one_read_and_leave_source_unchanged(tmp_path, monkeypatch):
    path = write_manifest(tmp_path, declaration_payload())
    raw, mtime = path.read_bytes(), path.stat().st_mtime_ns
    real_open = os.open
    opens = []
    def one_readonly_open(name, flags, *args, **kwargs):
        assert not opens, "summary reopened a source instead of reusing captured bytes"
        assert not flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
        opens.append(str(name))
        return real_open(name, flags, *args, **kwargs)
    def forbidden(*args, **kwargs):
        raise AssertionError("summary attempted extra file read, write or runtime")
    with monkeypatch.context() as patch:
        patch.setattr(os, "open", one_readonly_open)
        for attr in ("read_bytes", "read_text", "write_bytes", "write_text", "glob", "rglob"):
            patch.setattr(Path, attr, forbidden)
        patch.setattr(builtins, "open", forbidden)
        patch.setattr(os, "scandir", forbidden)
        patch.setattr(socket, "socket", forbidden)
        patch.setattr(subprocess, "Popen", forbidden)
        report = summarize_manifest(spec(path))
    assert len(opens) == 1
    assert report["input"]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert report["declared_interval_us"] == 120000000
    assert (path.read_bytes(), path.stat().st_mtime_ns) == (raw, mtime)


def test_manifest_incomplete_capture_has_no_trusted_time_facts(tmp_path, monkeypatch):
    path = write_manifest(tmp_path, declaration_payload())
    raw = path.read_bytes()
    real_fstat = os.fstat
    calls = 0
    def fail_after_read(fd):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("owned injected metadata failure")
        return real_fstat(fd)
    monkeypatch.setattr(os, "fstat", fail_after_read)
    report = summarize_manifest(spec(path))
    assert report["completeness"] == "INCOMPLETE"
    assert report["input"]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert report["input"]["records"] is None
    assert all(v is None for v in report["declaration"].values())
    assert report["started_at_utc"] is None and report["ended_at_utc"] is None
    assert report["declared_interval_us"] is None


# EM: protocol declarations from one raw snapshot, never online/healthy status.
def heartbeat_payload(**changes):
    value = {"proc": "m2_runner", "pid": 7, "host": "owned-fixture",
        "started_at": "2026-11-03T00:00:00Z", "ts": "2026-11-03T00:00:01Z", "seq": 0,
        "git_sha": None, "risk_sha256": None, "live_enabled": None, "mode": None,
        "state": "paused", "exit_code": None, "progress": {"status": "UNKNOWN"},
        "extra": {"keep": True}}
    value.update(changes)
    return value


def summarize_heartbeat(descriptor, expected_proc="m2_runner"):
    module = importlib.import_module("src.m2_report")
    fn = getattr(module, "summarize_heartbeat_snapshot", None)
    assert callable(fn), "heartbeat declaration summary not implemented"
    return fn(descriptor, expected_proc=expected_proc)


def test_heartbeat_literal_raw_sha_nulls_and_extra_fields_are_preserved(tmp_path):
    raw = b' {"proc":"m2_runner","pid":7,"host":"owned-fixture","started_at":"2026-11-03T00:00:00Z","ts":"2026-11-03T00:00:01Z","seq":0,"git_sha":null,"risk_sha256":null,"live_enabled":null,"mode":null,"state":"paused","exit_code":null,"progress":{"status":"UNKNOWN"},"extra":{"keep":true}}\n'
    path = tmp_path / "owned.json"
    path.write_bytes(raw)
    before = raw, path.stat().st_mtime_ns
    report = summarize_heartbeat(spec(path))
    assert_summary_not_evaluated(report)
    assert report["completeness"] == "INVENTORIED" and report["input"]["records"] == 1
    assert report["input"]["sha256"] == "6fadc556d289e46500a5f4a2e7d8791a85d572dc38493b9bfc36646bffb69c55" and report["input"]["bytes"] == 279
    assert report["declaration"] == heartbeat_payload()
    assert report["declaration_problems"] == []
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


@pytest.mark.parametrize("state,code", [("starting", None), ("running", None), ("paused", None),
    ("finished", 0), ("finished", 7), ("crashed", None)])
def test_heartbeat_valid_states_are_not_health_or_success_verdicts(state, code, tmp_path):
    payload = heartbeat_payload(state=state, exit_code=code, git_sha="UNKNOWN", mode="UNKNOWN",
        ts="9999-12-31T23:59:59Z")
    report = summarize_heartbeat(spec(write_manifest(tmp_path, payload)))
    assert_summary_not_evaluated(report)
    assert report["declaration"] == payload and report["declaration_problems"] == []


@pytest.mark.parametrize("field,value", [("pid", True), ("pid", 0), ("seq", -1), ("seq", True),
    ("host", 123), ("started_at", "2026-11-03T00:00:00"), ("ts", None), ("ts", "2026-11-03"),
    ("git_sha", 3), ("risk_sha256", False), ("live_enabled", "false"), ("mode", False),
    ("state", "UNKNOWN"), ("state", []), ("state", {}), ("exit_code", True), ("progress", []),
    ("proc", "other_proc")])
def test_heartbeat_protocol_failures_are_separate_from_readable_input(field, value, tmp_path):
    payload = heartbeat_payload(**{field: value})
    report = summarize_heartbeat(spec(write_manifest(tmp_path, payload)))
    assert_summary_not_evaluated(report)
    assert report["input"]["records"] == 1 and report["input"]["problems"] == []
    assert report["completeness"] == "INVENTORIED" and report["declaration"] is None
    assert report["declaration_problems"]


@pytest.mark.parametrize("field", ["proc", "state", "progress"])
def test_heartbeat_missing_required_fields_do_not_get_defaults(field, tmp_path):
    payload = heartbeat_payload()
    del payload[field]
    report = summarize_heartbeat(spec(write_manifest(tmp_path, payload)))
    assert report["declaration"] is None and report["declaration_problems"]
    assert report["input"]["records"] == 1


def test_heartbeat_finished_without_exit_code_is_invalid(tmp_path):
    report = summarize_heartbeat(spec(write_manifest(tmp_path, heartbeat_payload(state="finished"))))
    assert report["declaration"] is None and report["declaration_problems"]
    assert report["input"]["records"] == 1


@pytest.mark.parametrize("expected_proc", [None, "", 0, True, [], {}])
def test_heartbeat_invalid_expected_proc_rejects_before_source_or_code_io(expected_proc, monkeypatch):
    class ForbiddenSource:
        def __fspath__(self):
            raise AssertionError("invalid expected_proc touched source descriptor")
    def forbidden(*args, **kwargs):
        raise AssertionError("invalid expected_proc touched filesystem")
    with monkeypatch.context() as patch:
        for attr in ("resolve", "stat", "open", "read_bytes", "read_text"):
            patch.setattr(Path, attr, forbidden)
        patch.setattr(os, "open", forbidden)
        patch.setattr(builtins, "open", forbidden)
        report = summarize_heartbeat({"source": ForbiddenSource(), "format": "json"}, expected_proc)
    assert_summary_not_evaluated(report)
    assert report["completeness"] == "INVALID" and report["declaration"] is None
    assert report["input"]["sha256"] is None and "invalid_expected_proc" in report["declaration_problems"]


@pytest.mark.parametrize("descriptor", [None, [], {}, {"source": "unused", "format": "jsonl"},
    {"source": "unused", "format": "json", "sha256": "override"}])
def test_heartbeat_requires_explicit_single_json_descriptor(descriptor, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("invalid heartbeat descriptor reached filesystem")
    with monkeypatch.context() as patch:
        patch.setattr(Path, "resolve", forbidden)
        patch.setattr(os, "open", forbidden)
        patch.setattr(builtins, "open", forbidden)
        report = summarize_heartbeat(descriptor)
    assert report["completeness"] == "INVALID" and report["declaration"] is None
    assert report["input"]["sha256"] is None


@pytest.mark.parametrize("raw", [b"", b"\xff", b'{"proc":"m2_runner","proc":"other"}',
    b'{"x":NaN}', b'{"x":Infinity}', b'{"x":1e999}', b'{"proc":', b"[]", b"null"])
def test_heartbeat_bad_raw_or_root_cannot_publish_partial_declaration(raw, tmp_path):
    path = tmp_path / "bad.json"
    path.write_bytes(raw)
    report = summarize_heartbeat(spec(path))
    assert_summary_not_evaluated(report)
    assert report["input"]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert report["input"]["bytes"] == len(raw) and report["declaration"] is None
    assert report["declaration_problems"]
    assert report["input"]["records"] == (1 if raw in (b"[]", b"null") else None)


def test_heartbeat_missing_source_is_not_first_run_or_missing_live_beat(tmp_path):
    report = summarize_heartbeat(spec(tmp_path / "missing.json"))
    assert_summary_not_evaluated(report)
    assert report["completeness"] == "INCOMPLETE" and report["declaration"] is None
    assert report["input"]["sha256"] is None and "missing_source" in report["input"]["problems"]
    assert "heartbeat_missing" not in report["input"]["problems"] + report["declaration_problems"]


@pytest.mark.parametrize("source", [r"\\server\share\beat.json", r"\\?\UNC\server\share\beat.json"])
def test_heartbeat_unc_rejects_before_filesystem(source, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("UNC heartbeat reached filesystem")
    with monkeypatch.context() as patch:
        for attr in ("resolve", "stat", "open", "read_bytes"):
            patch.setattr(Path, attr, forbidden)
        patch.setattr(os, "open", forbidden)
        report = summarize_heartbeat({"source": source, "format": "json"})
    assert report["completeness"] == "INVALID" and report["declaration"] is None
    assert report["input"]["source"] == source and "nonlocal_source" in report["input"]["problems"]


def test_heartbeat_env_source_is_rejected_without_reading(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_bytes(b"owned-test-fixture")
    def forbidden(*args, **kwargs):
        raise AssertionError("environment fixture opened")
    monkeypatch.setattr(os, "open", forbidden)
    report = summarize_heartbeat(spec(path))
    assert report["declaration"] is None and "forbidden_source" in report["input"]["problems"]


def test_heartbeat_incomplete_capture_keeps_sha_but_no_declaration(tmp_path, monkeypatch):
    path = write_manifest(tmp_path, heartbeat_payload())
    raw = path.read_bytes()
    real_fstat = os.fstat
    calls = 0
    def fail_after_read(fd):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("owned injected incomplete metadata")
        return real_fstat(fd)
    monkeypatch.setattr(os, "fstat", fail_after_read)
    report = summarize_heartbeat(spec(path))
    assert report["completeness"] == "INCOMPLETE" and report["declaration"] is None
    assert report["input"]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert report["input"]["records"] is None


def test_heartbeat_cold_protocol_loading_avoids_runtime_and_reads_snapshot_once(tmp_path, monkeypatch):
    # Real original protocol source executes; no validator stub or full heartbeat import.
    import importlib.machinery
    import ssl
    import sys
    import time
    import tokenize  # Warm standard code-loader dependencies before IO boundary probe.
    import urllib.request
    path = write_manifest(tmp_path, heartbeat_payload())
    raw, mtime = path.read_bytes(), path.stat().st_mtime_ns
    module = importlib.import_module("src.m2_report")
    fn = getattr(module, "summarize_heartbeat_snapshot", None)
    assert callable(fn), "heartbeat declaration summary not implemented"
    code_path = str(Path(module.__file__).with_name("heartbeat.py"))
    real_open, real_resolve = os.open, Path.resolve
    real_data = importlib.machinery.SourceFileLoader.get_data
    real_import = builtins.__import__
    snapshot_opens, code_reads = [], []
    def source_only_open(name, flags, *args, **kwargs):
        assert not snapshot_opens, "snapshot reopened"
        assert Path(name) == path
        assert not flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
        snapshot_opens.append(str(name))
        return real_open(name, flags, *args, **kwargs)
    def source_only_resolve(target, *args, **kwargs):
        assert target == path, "runtime/module ROOT.resolve attempted"
        return real_resolve(target, *args, **kwargs)
    def actual_code_read(loader, name):
        assert Path(name) == Path(code_path), "unexpected code loader input"
        code_reads.append(str(name))
        return real_data(loader, name)
    def no_runtime_import(name, globals=None, locals=None, fromlist=(), level=0):
        assert name not in ("src.heartbeat", "src.podlib", "src.secret_scan")
        assert not (name == "src" and any(x in ("heartbeat", "podlib", "secret_scan") for x in fromlist))
        return real_import(name, globals, locals, fromlist, level)
    def forbidden(*args, **kwargs):
        raise AssertionError("runtime/marker/risk/write/network/scan/sleep attempted")
    with monkeypatch.context() as patch:
        for name in ("src.heartbeat", "src.podlib", "src.secret_scan"):
            patch.delitem(sys.modules, name, raising=False)
        patch.setattr(os, "open", source_only_open)
        patch.setattr(Path, "resolve", source_only_resolve)
        patch.setattr(importlib.machinery.SourceFileLoader, "get_data", actual_code_read)
        patch.setattr(builtins, "__import__", no_runtime_import)
        patch.setattr(builtins, "open", forbidden)
        for attr in ("read_bytes", "read_text", "write_bytes", "write_text", "glob", "rglob"):
            patch.setattr(Path, attr, forbidden)
        patch.setattr(os, "scandir", forbidden)
        patch.setattr(urllib.request, "build_opener", forbidden)
        patch.setattr(ssl, "create_default_context", forbidden)
        patch.setattr(socket, "socket", forbidden)
        patch.setattr(subprocess, "Popen", forbidden)
        patch.setattr(time, "sleep", forbidden)
        report = fn(spec(path), expected_proc="m2_runner")
        assert all(name not in sys.modules for name in ("src.heartbeat", "src.podlib", "src.secret_scan"))
    assert len(snapshot_opens) == 1 and len(code_reads) == 1
    assert report["input"]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert report["declaration"] == heartbeat_payload() and report["declaration_problems"] == []
    assert (path.read_bytes(), path.stat().st_mtime_ns) == (raw, mtime)



def test_heartbeat_unavailable_protocol_source_has_no_runtime_fallback(tmp_path, monkeypatch):
    import importlib.machinery
    path = write_manifest(tmp_path, heartbeat_payload())
    raw = path.read_bytes()
    original = importlib.machinery.SourceFileLoader.get_source
    def missing_code(loader, fullname):
        if fullname == "src.heartbeat":
            raise OSError("owned injected protocol source unavailable")
        return original(loader, fullname)
    monkeypatch.setattr(importlib.machinery.SourceFileLoader, "get_source", missing_code)
    report = summarize_heartbeat(spec(path))
    assert_summary_not_evaluated(report)
    assert report["completeness"] == "INVENTORIED" and report["input"]["records"] == 1
    assert report["input"]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert report["declaration"] is None
    assert report["declaration_problems"] == ["validator_unavailable:OSError"]


# EV: same-read memory capture. These cases never create an archive.
def _ev_capture_api():
    module = importlib.import_module("src.m2_report")
    fn = getattr(module, "capture_heartbeat_once", None)
    assert callable(fn), "same-read raw heartbeat capture not implemented"
    return module, fn


def test_ev_capture_same_raw_binding_and_attempt_times(tmp_path, monkeypatch):
    import base64
    module, fn = _ev_capture_api()
    payload = heartbeat_payload(extra={"keep": True, "label": "雪"})
    raw = (" \r\n" + json.dumps(payload, ensure_ascii=False, indent=1) + "\r\n").encode()
    path = tmp_path / "capture.json"
    path.write_bytes(raw)
    before = path.stat().st_mtime_ns
    stamps = iter(["2026-11-04T02:00:02.000001Z", "2026-11-04T02:00:01.000002Z"])
    monkeypatch.setattr(module, "_capture_stamp", lambda: next(stamps))
    real_open = os.open
    opens = []
    def single_open(name, flags, *args, **kwargs):
        assert not opens
        assert Path(name) == path
        assert not flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
        opens.append(name)
        return real_open(name, flags, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(os, "open", single_open)
        result = fn(spec(path), expected_proc="m2_runner", capture_id="explicit-id")
    assert_summary_not_evaluated(result)
    assert result["declaration"] == payload and result["declaration_problems"] == []
    assert result["capture"] == {"capture_id": "explicit-id", "expected_proc": "m2_runner",
        "started_at_utc": "2026-11-04T02:00:02.000001Z", "finished_at_utc": "2026-11-04T02:00:01.000002Z",
        "raw_base64": base64.b64encode(raw).decode("ascii")}
    assert len(opens) == 1
    assert result["input"]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert result["input"]["bytes"] == len(raw)
    assert (path.read_bytes(), path.stat().st_mtime_ns) == (raw, before)


def test_ev_capture_bad_id_rejects_without_coercion_or_clock(monkeypatch):
    module, fn = _ev_capture_api()
    class Explosive:
        def __str__(self):
            raise AssertionError("invalid object coerced")
        def __fspath__(self):
            raise AssertionError("invalid object path coerced")
    def forbidden(*args, **kwargs):
        raise AssertionError("invalid id performed IO or clock")
    with monkeypatch.context() as patch:
        patch.setattr(module, "_capture_stamp", forbidden)
        patch.setattr(module, "_heartbeat_protocol_validator", forbidden)
        patch.setattr(os, "open", forbidden)
        patch.setattr(Path, "resolve", forbidden)
        result = fn({"source": Explosive(), "format": "json"}, expected_proc="m2_runner", capture_id=Explosive())
    assert_summary_not_evaluated(result)
    assert result["completeness"] == "INVALID" and result["declaration"] is None
    assert result["input"]["problems"] == ["invalid_capture_id"]
    assert result["declaration_problems"] == ["invalid_capture_id"]
    assert result["capture"] == {"capture_id": None, "expected_proc": "m2_runner",
        "started_at_utc": None, "finished_at_utc": None, "raw_base64": None}


@pytest.mark.parametrize("field,bad", [(f, v) for f in ("expected_proc", "capture_id")
    for v in (None, "", 0, True, [], {})])
def test_ev_capture_invalid_required_args_are_literal_only(field, bad, monkeypatch):
    module, fn = _ev_capture_api()
    class Source:
        def __str__(self):
            raise AssertionError("coerced")
        def __fspath__(self):
            raise AssertionError("path coerced")
    def forbidden(*args, **kwargs):
        raise AssertionError("invalid arguments touched IO/clock")
    args = {"expected_proc": "m2_runner", "capture_id": "id"}
    args[field] = bad
    with monkeypatch.context() as patch:
        patch.setattr(module, "_capture_stamp", forbidden)
        patch.setattr(module, "_heartbeat_protocol_validator", forbidden)
        patch.setattr(Path, "resolve", forbidden)
        patch.setattr(os, "open", forbidden)
        result = fn({"source": Source(), "format": "json"}, **args)
    reason = "invalid_" + field
    assert_summary_not_evaluated(result)
    assert result["completeness"] == result["input"]["completeness"] == "INVALID"
    assert result["declaration_problems"] == result["input"]["problems"] == [reason]
    assert result["input"]["source"] is None
    assert result["capture"][field] == (bad if isinstance(bad, str) else None)
    assert result["capture"]["raw_base64"] is None
    assert result["capture"]["started_at_utc"] is result["capture"]["finished_at_utc"] is None


def test_ev_capture_expected_proc_error_has_priority(monkeypatch):
    module, fn = _ev_capture_api()
    def forbidden(*args, **kwargs):
        raise AssertionError("invalid arguments touched capture")
    monkeypatch.setattr(module, "_capture_stamp", forbidden)
    result = fn({"source": "literal.json", "format": "json"}, expected_proc="", capture_id="")
    assert result["status"] == "NOT_EVALUATED" and result["completeness"] == "INVALID"
    assert result["input"]["source"] == "literal.json"
    assert result["input"]["problems"] == ["invalid_expected_proc"]
    assert result["capture"]["expected_proc"] == result["capture"]["capture_id"] == ""


@pytest.mark.parametrize("raw", [b"", b"\xff", b'{"proc":"a","proc":"b"}', b'{"x":NaN}',
    b'{"x":1e999}', b"[]", b"null", b'{"proc":'])
def test_ev_capture_readable_bad_raw_retains_exact_bytes(raw, tmp_path):
    import base64
    _, fn = _ev_capture_api()
    path = tmp_path / "bad.json"
    path.write_bytes(raw)
    result = fn(spec(path), expected_proc="m2_runner", capture_id="bad")
    assert_summary_not_evaluated(result)
    assert base64.b64decode(result["capture"]["raw_base64"], validate=True) == raw
    assert result["input"]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert result["input"]["bytes"] == len(raw) and result["declaration"] is None
    assert result["input"]["records"] == (1 if raw in (b"[]", b"null") else None)
    assert result["declaration_problems"]
    if not raw:
        assert result["capture"]["raw_base64"] == "" and result["completeness"] == "INCOMPLETE"


def test_ev_capture_unreadable_is_not_empty_and_records_attempt_only(tmp_path, monkeypatch):
    module, fn = _ev_capture_api()
    calls = []
    def stamp():
        calls.append(1)
        return "2026-11-04T02:00:00.000001Z"
    monkeypatch.setattr(module, "_capture_stamp", stamp)
    result = fn(spec(tmp_path / "missing.json"), expected_proc="m2_runner", capture_id="missing")
    assert_summary_not_evaluated(result)
    assert len(calls) == 2 and result["capture"]["raw_base64"] is None
    assert result["input"]["sha256"] is None and result["input"]["bytes"] is None
    assert result["input"]["problems"] == ["missing_source"]
    assert result["declaration"] is None and result["completeness"] == "INCOMPLETE"


@pytest.mark.parametrize("failure,reason", [("fstat-after", "metadata_unavailable"),
    ("source-change", "source_changed_during_read"), ("last-stat", "source_changed_during_read"),
    ("identity", "identity_unavailable")])
def test_ev_capture_integrity_failure_retains_raw_not_declaration(failure, reason, tmp_path, monkeypatch):
    import base64
    from types import SimpleNamespace
    module, fn = _ev_capture_api()
    path = write_manifest(tmp_path, heartbeat_payload())
    raw, mtime = path.read_bytes(), path.stat().st_mtime_ns
    real_fstat, real_stat = os.fstat, Path.stat
    calls = 0
    read_done = False
    def changed(info, **changes):
        fields = {k: getattr(info, k) for k in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_mode")}
        return SimpleNamespace(**{**fields, **changes})
    def fstat(fd):
        nonlocal calls, read_done
        calls += 1
        info = real_fstat(fd)
        if calls == 2:
            read_done = True
            if failure == "fstat-after":
                raise OSError("owned metadata failure")
            if failure == "source-change":
                return changed(info, st_mtime_ns=info.st_mtime_ns + 1)
        return changed(info, st_ino=0) if failure == "identity" else info
    def path_stat(target, *args, **kwargs):
        if target == path and read_done and failure == "last-stat":
            raise OSError("owned path disappeared after read")
        info = real_stat(target, *args, **kwargs)
        return changed(info, st_ino=0) if target == path and failure == "identity" else info
    def no_validator():
        raise AssertionError("incomplete input reached protocol")
    with monkeypatch.context() as patch:
        patch.setattr(os, "fstat", fstat)
        patch.setattr(Path, "stat", path_stat)
        patch.setattr(module, "_heartbeat_protocol_validator", no_validator)
        result = fn(spec(path), expected_proc="m2_runner", capture_id="integrity")
    assert_summary_not_evaluated(result)
    assert reason in result["input"]["problems"]
    assert result["input"]["records"] is None and result["declaration"] is None
    assert result["input"]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert base64.b64decode(result["capture"]["raw_base64"], validate=True) == raw
    assert result["declaration_problems"] == ["input_unavailable"]
    assert (path.read_bytes(), path.stat().st_mtime_ns) == (raw, mtime)


@pytest.mark.parametrize("kind", ["unc", "extended-unc", "env", "resolved-env"])
def test_ev_capture_forbidden_paths_before_source_or_protocol_io(kind, tmp_path, monkeypatch):
    module, fn = _ev_capture_api()
    source = {"unc": r"\\server\share\beat.json", "extended-unc": r"\\?\UNC\server\beat.json",
        "env": str(tmp_path / ".env.fixture"), "resolved-env": str(tmp_path / "alias.json")}[kind]
    def forbidden(*args, **kwargs):
        raise AssertionError("forbidden source reached IO/protocol")
    with monkeypatch.context() as patch:
        patch.setattr(os, "open", forbidden)
        patch.setattr(Path, "stat", forbidden)
        patch.setattr(module, "_heartbeat_protocol_validator", forbidden)
        patch.setattr(Path, "resolve", (lambda *a, **k: tmp_path / ".env.fixture") if kind == "resolved-env" else forbidden)
        result = fn({"source": source, "format": "json"}, expected_proc="m2_runner", capture_id="path")
    assert result["status"] == "NOT_EVALUATED" and result["completeness"] == "INVALID"
    assert result["input"]["problems"] == ["nonlocal_source" if "unc" in kind else "forbidden_source"]
    assert result["capture"]["raw_base64"] is None and result["declaration"] is None


def test_ev_capture_unavailable_original_protocol_has_no_fallback(tmp_path, monkeypatch):
    import base64
    import importlib.machinery
    _, fn = _ev_capture_api()
    path = write_manifest(tmp_path, heartbeat_payload())
    raw = path.read_bytes()
    def unavailable(*args, **kwargs):
        raise OSError("owned unavailable code")
    monkeypatch.setattr(importlib.machinery.SourceFileLoader, "get_source", unavailable)
    result = fn(spec(path), expected_proc="m2_runner", capture_id="protocol")
    assert result["declaration_problems"] == ["validator_unavailable:OSError"]
    assert result["declaration"] is None and result["input"]["records"] == 1
    assert base64.b64decode(result["capture"]["raw_base64"], validate=True) == raw


def test_ev_capture_old_api_projection_and_no_new_clock(tmp_path, monkeypatch):
    module, fn = _ev_capture_api()
    path = write_manifest(tmp_path, heartbeat_payload())
    result = fn(spec(path), expected_proc="m2_runner", capture_id="projection")
    def forbidden():
        raise AssertionError("old API acquired a clock")
    monkeypatch.setattr(module, "_capture_stamp", forbidden)
    summary = module.summarize_heartbeat_snapshot(spec(path), expected_proc="m2_runner")
    assert {k: v for k, v in result.items() if k != "capture"} == summary
    assert module.inventory_inputs([spec(path)])["inputs"][0] == summary["input"]
    assert module.summarize_recording_manifest(spec(path))["input"] == summary["input"]
    assert set(result["capture"]) == {"capture_id", "expected_proc", "started_at_utc", "finished_at_utc", "raw_base64"}


def test_ev_capture_valid_whitespace_and_real_utc_attempts_are_not_tightened(tmp_path):
    from datetime import datetime, timezone
    _, fn = _ev_capture_api()
    payload = heartbeat_payload(proc=" ")
    result = fn(spec(write_manifest(tmp_path, payload)), expected_proc=" ", capture_id=" ")
    assert result["declaration"] == payload and result["declaration_problems"] == []
    assert result["capture"]["expected_proc"] == result["capture"]["capture_id"] == " "
    for key in ("started_at_utc", "finished_at_utc"):
        value = result["capture"][key]
        assert value.endswith("Z") and datetime.fromisoformat(value.replace("Z", "+00:00")).utcoffset() == timezone.utc.utcoffset(None)


@pytest.mark.parametrize("fail_on", [1, 2])
def test_ev_capture_clock_failure_propagates_without_retry_or_write(fail_on, tmp_path, monkeypatch):
    module, fn = _ev_capture_api()
    path = write_manifest(tmp_path, heartbeat_payload())
    raw, mtime = path.read_bytes(), path.stat().st_mtime_ns
    calls, opens = [], []
    real_open = os.open
    def stamp():
        calls.append(1)
        if len(calls) == fail_on:
            raise OSError("owned clock failure")
        return "2026-11-04T02:00:00Z"
    def read_open(name, flags, *args, **kwargs):
        assert Path(name) == path and not flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
        opens.append(name)
        return real_open(name, flags, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(module, "_capture_stamp", stamp)
        patch.setattr(os, "open", read_open)
        with pytest.raises(OSError, match="owned clock failure"):
            fn(spec(path), expected_proc="m2_runner", capture_id="clock")
    assert len(calls) == fail_on and len(opens) == fail_on - 1
    assert (path.read_bytes(), path.stat().st_mtime_ns) == (raw, mtime)
    assert list(tmp_path.iterdir()) == [path]
