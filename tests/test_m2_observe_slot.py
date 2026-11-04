"""EY author tests; only owned synthetic fixtures, not independent QC."""
import importlib
import importlib.util


def module():
    assert importlib.util.find_spec("src.m2_observe_slot") is not None, "EY API absent on actual base"
    mod = importlib.import_module("src.m2_observe_slot")
    assert callable(getattr(mod, "observe_slot_once", None)), "EY API missing"
    return mod


def plan():
    return {"slot_id": " slot-汉字 ", "planned_at_utc": "opaque planned label", "expected_proc": "m2_runner"}


def args(tmp_path):
    return {"attempt_id": " attempt-一 ", "spec": {"source": str(tmp_path / "input.json"), "format": "json"},
            "owned_root": str(tmp_path), "file_name": "hb-once.jsonl"}


def test_capture_error_keeps_binding_and_exact_ex_attempt_without_archive(monkeypatch, tmp_path):
    mod = module(); calls = []
    def failed(*a, **k):
        calls.append("capture"); raise RuntimeError("synthetic")
    def forbidden(*a, **k):
        raise AssertionError("archive forbidden when capture failed")
    monkeypatch.setattr(mod, "capture_heartbeat_once", failed)
    monkeypatch.setattr(mod, "archive_capture_once", forbidden)
    result = mod.observe_slot_once(plan(), **args(tmp_path))
    assert calls == ["capture"] and result["phase"] == "CAPTURE_ERROR"
    assert result["attempt"] == {"slot_id": " slot-汉字 ", "attempt_id": " attempt-一 ", "report": None}
    assert result["archive_receipt"] is None and result["error_type"] == "RuntimeError"
    assert result["not_archived_reason"] == "capture_exception_no_report"
    assert result["binding"]["capture_id"].startswith("m2-slot-v1:")


def test_returned_report_and_receipt_are_unmodified_single_calls(monkeypatch, tmp_path):
    mod = module(); report = {"diagnostic": "retained"}; receipt = {"write_state": "UNKNOWN", "created": True}
    calls = []
    def captured(spec, *, expected_proc, capture_id):
        calls.append(("capture", expected_proc, capture_id)); return report
    def archived(given, *, owned_root, file_name):
        assert given is report; calls.append(("archive", owned_root, file_name)); return receipt
    monkeypatch.setattr(mod, "capture_heartbeat_once", captured)
    monkeypatch.setattr(mod, "archive_capture_once", archived)
    result = mod.observe_slot_once(plan(), **args(tmp_path))
    assert len(calls) == 2 and calls[0][0] == "capture" and calls[1][0] == "archive"
    assert result["attempt"]["report"] is report and result["archive_receipt"] is receipt
    assert result["phase"] == "ARCHIVE_RETURNED" and result["not_archived_reason"] == "archive_unknown"
    assert result["status"] == "NOT_EVALUATED" and result["declaration_status"] == "UNVERIFIED"
    assert all(result[k] is None for k in ("metrics", "denominator", "healthy_online_us"))


import base64
import copy
import hashlib
import json
from pathlib import Path
import pytest


def beat():
    return {"proc": "m2_runner", "pid": 1, "host": "owned-fixture",
            "started_at": "2026-11-03T00:00:00Z", "ts": "2026-11-03T00:00:01Z", "seq": 0,
            "git_sha": None, "risk_sha256": None, "live_enabled": None, "mode": None,
            "state": "paused", "exit_code": None, "progress": {"synthetic": True}}


def fixture_args(tmp_path, raw=None):
    data = json.dumps(beat(), ensure_ascii=False).encode() if raw is None else raw
    source = tmp_path / "input.json"; source.write_bytes(data)
    root = tmp_path / "archive"; root.mkdir()
    return {"attempt_id": " attempt-一 ", "spec": {"source": str(source), "format": "json"},
            "owned_root": str(root), "file_name": "hb-once.jsonl"}


def independent_binding(planned, ident):
    raw = b"polymarket-pod/m2-observe-slot/1\x00" + json.dumps(
        {"planned_slot": planned, "attempt_id": ident}, sort_keys=True,
        ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    return {"scheme": "m2_slot_capture/1", "material_base64": base64.b64encode(raw).decode(),
            "sha256": digest, "capture_id": "m2-slot-v1:" + digest}


def test_real_single_capture_archive_persists_independently_verifiable_slot_reference(monkeypatch, tmp_path):
    mod = module(); values = fixture_args(tmp_path); planned = plan(); before = copy.deepcopy((planned, values))
    import src.m2_report as ev
    real_cap, real_archive = mod.capture_heartbeat_once, mod.archive_capture_once
    real_clock, real_fdopen = ev._capture_stamp, ev.os.fdopen
    calls = {"capture": 0, "archive": 0, "clock": 0, "source_body": 0}; captured = []; receipts = []
    def capture(*a, **k):
        calls["capture"] += 1; rep = real_cap(*a, **k); captured.append(rep); return rep
    def archive(rep, **k):
        calls["archive"] += 1; assert rep is captured[0]
        receipt = real_archive(rep, **k); receipts.append(receipt); return receipt
    def clock():
        calls["clock"] += 1; return real_clock()
    class SourceReader:
        def __init__(self, handle): self.handle = handle
        def __enter__(self): self.handle.__enter__(); return self
        def __exit__(self, *a): return self.handle.__exit__(*a)
        def __getattr__(self, key): return getattr(self.handle, key)
        def read(self, *a, **k):
            calls["source_body"] += 1; return self.handle.read(*a, **k)
    def fdopen(*a, **k): return SourceReader(real_fdopen(*a, **k))
    monkeypatch.setattr(mod, "capture_heartbeat_once", capture)
    monkeypatch.setattr(mod, "archive_capture_once", archive)
    monkeypatch.setattr(ev, "_capture_stamp", clock)
    monkeypatch.setattr(ev.os, "fdopen", fdopen)
    result = mod.observe_slot_once(planned, **values)
    assert calls == {"capture": 1, "archive": 1, "clock": 2, "source_body": 1}
    assert result["attempt"]["report"] is captured[0] and result["archive_receipt"] is receipts[0]
    assert (planned, values) == before and result["planned_slot"] == planned
    assert result["planned_slot"] is not planned
    expected = independent_binding(planned, values["attempt_id"])
    assert result["binding"] == expected
    target = Path(values["owned_root"]) / values["file_name"]
    envelope = json.loads(target.read_bytes())
    assert envelope["capture_report"]["capture"]["capture_id"] == expected["capture_id"]
    assert envelope["capture_report"]["declaration"]["ts"] == beat()["ts"]
    assert envelope["capture_report"]["capture"]["started_at_utc"] != planned["planned_at_utc"]
    assert result["archive_receipt"]["write_state"] == "WRITTEN_READ_BACK"
    assert result["not_archived_reason"] is None
    assert set(result["attempt"]) == {"slot_id", "attempt_id", "report"}
    assert set(result) == {"schema", "status", "declaration_status", "metrics", "denominator",
                           "healthy_online_us", "planned_slot", "binding", "attempt", "archive_receipt",
                           "phase", "error_type", "not_archived_reason"}
    assert result["status"] == "NOT_EVALUATED" and result["declaration_status"] == "UNVERIFIED"
    assert all(result[k] is None for k in ("metrics", "denominator", "healthy_online_us"))
    assert list(Path(values["owned_root"]).iterdir()) == [target]


def test_canonical_binding_keeps_unicode_whitespace_and_is_not_a_unique_sample_id(monkeypatch, tmp_path):
    mod = module()
    monkeypatch.setattr(mod, "capture_heartbeat_once", lambda *a, **k: None)
    values = args(tmp_path); original = plan()
    first = mod.observe_slot_once(original, **values)
    reordered = dict(reversed(list(original.items())))
    second = mod.observe_slot_once(reordered, **values)
    assert first["binding"] == second["binding"] == independent_binding(original, values["attempt_id"])
    assert first["planned_slot"] == original and first["attempt"]["attempt_id"] == " attempt-一 "
    changed = dict(original); changed["planned_at_utc"] += " "
    assert mod.observe_slot_once(changed, **values)["binding"]["capture_id"] != first["binding"]["capture_id"]
    values["attempt_id"] += " "
    assert mod.observe_slot_once(original, **values)["binding"]["capture_id"] != first["binding"]["capture_id"]
    assert first["phase"] == "CAPTURE_NO_REPORT" and first["attempt"]["report"] is None


@pytest.mark.parametrize("raw,completeness", [(b"", "INCOMPLETE"), (b"bad json", "INVALID")])
def test_empty_or_bad_raw_diagnostic_report_is_still_archived_once(tmp_path, raw, completeness):
    values = fixture_args(tmp_path, raw); result = module().observe_slot_once(plan(), **values)
    rep = result["attempt"]["report"]
    assert rep["completeness"] == completeness and rep["declaration"] is None
    assert rep["capture"]["raw_base64"] == base64.b64encode(raw).decode()
    assert result["archive_receipt"]["write_state"] == "WRITTEN_READ_BACK"
    assert result["phase"] == "ARCHIVE_RETURNED" and result["not_archived_reason"] is None


def test_missing_source_keeps_attempt_and_original_refused_receipt(tmp_path):
    values = args(tmp_path); result = module().observe_slot_once(plan(), **values)
    rep = result["attempt"]["report"]
    assert rep is not None and rep["capture"]["raw_base64"] is None
    assert result["archive_receipt"]["write_state"] == "REFUSED"
    assert result["not_archived_reason"] == "archive_refused" and result["attempt"]["attempt_id"] == values["attempt_id"]
    assert not (tmp_path / values["file_name"]).exists()


@pytest.mark.parametrize("failed_clock", [1, 2])
def test_first_or_second_old_clock_exception_has_no_fabricated_report_or_archive(monkeypatch, tmp_path, failed_clock):
    mod = module(); values = fixture_args(tmp_path); import src.m2_report as ev
    count = [0]; capcount = [0]; actual_capture = mod.capture_heartbeat_once
    def stamp():
        count[0] += 1
        if count[0] == failed_clock: raise OSError("synthetic clock")
        return "2026-11-04T18:00:00Z"
    def capture(*a, **k): capcount[0] += 1; return actual_capture(*a, **k)
    def forbidden(*a, **k): raise AssertionError("no archive without EV report")
    monkeypatch.setattr(ev, "_capture_stamp", stamp)
    monkeypatch.setattr(mod, "capture_heartbeat_once", capture)
    monkeypatch.setattr(mod, "archive_capture_once", forbidden)
    result = mod.observe_slot_once(plan(), **values)
    assert count == [failed_clock] and capcount == [1]
    assert result["phase"] == "CAPTURE_ERROR" and result["error_type"] == "OSError"
    assert result["attempt"]["report"] is None and result["archive_receipt"] is None
    assert result["binding"] == independent_binding(plan(), values["attempt_id"])
    assert not list(Path(values["owned_root"]).iterdir())


def test_existing_target_refused_preserves_existing_bytes_without_retry(tmp_path):
    values = fixture_args(tmp_path); target = Path(values["owned_root"]) / values["file_name"]
    prior = b"old-owned-evidence\n"; target.write_bytes(prior)
    result = module().observe_slot_once(plan(), **values)
    assert result["archive_receipt"]["write_state"] == "REFUSED"
    assert result["archive_receipt"]["created"] is False and result["archive_receipt"]["phase"] == "create"
    assert result["not_archived_reason"] == "archive_refused" and target.read_bytes() == prior
    assert list(Path(values["owned_root"]).iterdir()) == [target]


@pytest.mark.parametrize("postcreate", [False, True])
def test_representative_create_and_postcreate_unknown_keep_receipt_and_partial(monkeypatch, tmp_path, postcreate):
    mod = module(); values = fixture_args(tmp_path); import src.m2_archive as ew
    real = ew._open_file; opens = []; closes = []
    class Partial:
        def __init__(self, handle): self.handle = handle
        def write(self, data): self.handle.write(data[:7]); self.handle.flush(); raise OSError("synthetic partial")
        def close(self): closes.append(1); return self.handle.close()
    def failed(path, mode):
        opens.append(mode)
        if not postcreate: raise OSError("synthetic create")
        return Partial(real(path, mode))
    monkeypatch.setattr(ew, "_open_file", failed)
    result = mod.observe_slot_once(plan(), **values); receipt = result["archive_receipt"]
    assert opens == ["xb"] and result["attempt"]["report"] is not None
    assert receipt["write_state"] == "UNKNOWN" and receipt["created"] is (True if postcreate else None)
    assert result["not_archived_reason"] == "archive_unknown" and receipt["error_type"] == "OSError"
    target = Path(values["owned_root"]) / values["file_name"]
    if postcreate:
        assert receipt["phase"] == "write" and len(target.read_bytes()) == 7 and closes == [1]
    else:
        assert receipt["phase"] == "create" and not target.exists() and closes == []


def test_unexpected_archive_exception_keeps_original_report_no_invented_receipt(monkeypatch, tmp_path):
    mod = module(); report = {"raw": "retained"}; calls = []
    def capture(*a, **k): calls.append("capture"); return report
    class SilentMessageError(RuntimeError):
        def __str__(self): raise AssertionError("must not stringify exceptions")
    def archive(*a, **k): calls.append("archive"); raise SilentMessageError()
    monkeypatch.setattr(mod, "capture_heartbeat_once", capture); monkeypatch.setattr(mod, "archive_capture_once", archive)
    result = mod.observe_slot_once(plan(), **args(tmp_path))
    assert calls == ["capture", "archive"] and result["attempt"]["report"] is report
    assert result["archive_receipt"] is None and result["phase"] == "ARCHIVE_ERROR"
    assert result["error_type"] == "SilentMessageError" and result["not_archived_reason"] == "archive_exception"


@pytest.mark.parametrize("receipt,reason", [({"write_state": "REFUSED", "phase": "validate", "error_type": "ValueError"}, "archive_refused"),
    ({"write_state": "UNKNOWN", "cleanup_error_type": "OSError"}, "archive_unknown"),
    ({"write_state": "WRITTEN_READ_BACK"}, None), ({"write_state": []}, "archive_result_unclassified"),
    (None, "archive_result_unclassified")])
def test_receipts_are_not_rebuilt_or_promoted(monkeypatch, tmp_path, receipt, reason):
    mod = module(); monkeypatch.setattr(mod, "capture_heartbeat_once", lambda *a, **k: {})
    monkeypatch.setattr(mod, "archive_capture_once", lambda *a, **k: receipt)
    result = mod.observe_slot_once(plan(), **args(tmp_path))
    assert result["archive_receipt"] is receipt and result["not_archived_reason"] == reason
    assert result["phase"] == "ARCHIVE_RETURNED" and result["error_type"] is None


@pytest.mark.parametrize("where", ["capture", "archive"])
def test_baseexception_propagates_without_business_receipt_or_retry(monkeypatch, tmp_path, where):
    mod = module(); original = KeyboardInterrupt("synthetic"); calls = []
    def capture(*a, **k):
        calls.append("capture")
        if where == "capture": raise original
        return {}
    def archive(*a, **k): calls.append("archive"); raise original
    monkeypatch.setattr(mod, "capture_heartbeat_once", capture); monkeypatch.setattr(mod, "archive_capture_once", archive)
    with pytest.raises(KeyboardInterrupt) as caught: mod.observe_slot_once(plan(), **args(tmp_path))
    assert caught.value is original and calls == (["capture"] if where == "capture" else ["capture", "archive"])


@pytest.mark.parametrize("field,value", [("attempt_id", ""), ("attempt_id", None), ("attempt_id", True),
    ("attempt_id", "\ud800"), ("file_name", "bad.jsonl"), ("file_name", "hb-x.jsonl/next"),
    ("owned_root", "relative"), ("owned_root", "//server/share/root"),
    ("owned_root", "https://example.test/root"), ("owned_root", "C:/.ENV/child")])
def test_required_or_path_arguments_reject_before_capture_and_archive(monkeypatch, tmp_path, field, value):
    mod = module(); calls = []
    def forbidden(*a, **k): calls.append(1); raise AssertionError("preIO reject")
    monkeypatch.setattr(mod, "capture_heartbeat_once", forbidden); monkeypatch.setattr(mod, "archive_capture_once", forbidden)
    values = args(tmp_path); values[field] = value
    with pytest.raises(ValueError): mod.observe_slot_once(plan(), **values)
    assert calls == []


@pytest.mark.parametrize("source", ["relative.json", "//server/share/hb.json", "https://example.test/hb.json",
                                    "C:/owned/.env", "C:/owned/.ENV.secret", "\\\\?\\C:\\owned\\hb.json"])
def test_forbidden_source_spellings_reject_before_old_ev_clocks(monkeypatch, tmp_path, source):
    mod = module(); calls = []
    def forbidden(*a, **k): calls.append(1); raise AssertionError("must reject first")
    monkeypatch.setattr(mod, "capture_heartbeat_once", forbidden)
    values = args(tmp_path); values["spec"]["source"] = source
    with pytest.raises(ValueError): mod.observe_slot_once(plan(), **values)
    assert calls == []


@pytest.mark.parametrize("bad", [None, [], {}, {"slot_id": "s"}, {"slot_id": "s", "planned_at_utc": float("nan"), "expected_proc": "m2_runner"},
    {"slot_id": "s", "planned_at_utc": "time", "expected_proc": ""},
    {"slot_id": "s", "planned_at_utc": "time", "expected_proc": "p", "extra": True}])
def test_plan_bad_plain_schema_rejects_pre_io(monkeypatch, tmp_path, bad):
    mod = module(); calls = []
    def forbidden(*a, **k): calls.append(1); raise AssertionError("preIO")
    monkeypatch.setattr(mod, "capture_heartbeat_once", forbidden)
    with pytest.raises(ValueError): mod.observe_slot_once(bad, **args(tmp_path))
    assert calls == []


@pytest.mark.parametrize("bad", [None, [], {}, {"source": "x"}, {"source": "x", "format": "jsonl"},
                                 {"source": "x", "format": "json", "extra": 1}])
def test_spec_schema_is_exact_before_io(monkeypatch, tmp_path, bad):
    mod = module(); calls = []
    monkeypatch.setattr(mod, "capture_heartbeat_once", lambda *a, **k: calls.append(1))
    values = args(tmp_path); values["spec"] = bad
    with pytest.raises(ValueError): mod.observe_slot_once(plan(), **values)
    assert calls == []


def test_custom_and_subclass_conversion_hooks_never_run(tmp_path):
    mod = module(); hits = []
    class Custom:
        def __str__(self): hits.append("str"); raise AssertionError()
        def __fspath__(self): hits.append("fspath"); raise AssertionError()
    class DictSubclass(dict):
        def __iter__(self): hits.append("iter"); raise AssertionError()
    class StringSubclass(str):
        def encode(self, *a, **k): hits.append("encode"); raise AssertionError()
    for field in ("owned_root", "attempt_id", "file_name"):
        values = args(tmp_path); values[field] = Custom()
        with pytest.raises(ValueError): mod.observe_slot_once(plan(), **values)
    values = args(tmp_path); values["spec"]["source"] = Custom()
    with pytest.raises(ValueError): mod.observe_slot_once(plan(), **values)
    with pytest.raises(ValueError): mod.observe_slot_once(DictSubclass(plan()), **args(tmp_path))
    bad = plan(); bad["slot_id"] = StringSubclass("s")
    with pytest.raises(ValueError): mod.observe_slot_once(bad, **args(tmp_path))
    assert hits == []


def test_unicode_source_is_not_relabelled_to_bypass_existing_ew_ascii_refusal(tmp_path):
    values = fixture_args(tmp_path); old = Path(values["spec"]["source"]); target = old.with_name("证据.json")
    target.write_bytes(old.read_bytes()); values["spec"]["source"] = str(target)
    result = module().observe_slot_once(plan(), **values)
    assert result["attempt"]["report"]["capture"]["raw_base64"] is not None
    assert result["archive_receipt"]["write_state"] == "REFUSED"
    assert result["not_archived_reason"] == "archive_refused" and not list(Path(values["owned_root"]).iterdir())


def test_cold_new_module_import_no_application_io_or_runtime_start(monkeypatch):
    import builtins, io, socket, subprocess, time, sys, ast
    # Old public modules are sealed and warm; their old cold matrix is not rerun.
    import src.m2_report, src.m2_archive
    saved = sys.modules.pop("src.m2_observe_slot", None); before = set(sys.modules); hits = []
    def forbidden(*a, **k): hits.append(1); raise AssertionError("appIO/runtime at import")
    try:
        with monkeypatch.context() as m:
            for obj, names in ((builtins, ["open"]), (io, ["open"]),
                (Path, ["resolve", "stat", "lstat", "open", "read_bytes", "read_text", "mkdir"]),
                (socket, ["socket", "create_connection"]), (subprocess, ["Popen"]),
                (time, ["time", "monotonic", "sleep"])):
                for name in names: m.setattr(obj, name, forbidden)
            importlib.import_module("src.m2_observe_slot")
        assert hits == [] and not any(x.startswith("src.") and x != "src.m2_observe_slot" for x in set(sys.modules) - before)
    finally:
        if saved is not None:
            sys.modules["src.m2_observe_slot"] = saved; setattr(sys.modules["src"], "m2_observe_slot", saved)
    tree = ast.parse(Path(__file__).parents[1].joinpath("src/m2_observe_slot.py").read_text())
    assert not any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in
                   {"open", "read", "read_bytes", "read_text", "resolve", "stat", "lstat", "mkdir", "sleep", "now"}
                   for n in ast.walk(tree))
