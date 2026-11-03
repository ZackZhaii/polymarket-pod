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
