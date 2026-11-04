"""Focused tests for independent returned-observation receipt persistence."""
import importlib
import importlib.util
import json
import ast
import builtins
import hashlib
import math
import os
from pathlib import Path
import posixpath
import re
import stat
import pytest


def _api():
    assert importlib.util.find_spec("src.m2_receipt") is not None, "new receipt module is required"
    return importlib.import_module("src.m2_receipt").persist_observation_receipt


def _observation():
    return {
        "schema": "m2_observe_slot/1", "status": "NOT_EVALUATED",
        "declaration_status": "UNVERIFIED", "metrics": None,
        "denominator": None, "healthy_online_us": None,
        "planned_slot": {"slot_id": "slot-1", "planned_at_utc": "opaque planned time", "expected_proc": "synthetic"},
        "binding": {"scheme": "m2_slot_capture/1", "material_base64": "unverified", "sha256": "not authenticated", "capture_id": "label"},
        "attempt": {"slot_id": "slot-1", "attempt_id": "attempt-1", "report": {"input": {"source": "never-read-source"}, "capture": {"raw_base64": None}}},
        "archive_receipt": {"write_state": "UNKNOWN", "archive_path": "unopened-old-partial.jsonl", "created": True, "error_type": "OSError", "cleanup_error_type": "CloseError"},
        "phase": "ARCHIVE_RETURNED", "error_type": None, "not_archived_reason": "archive_unknown",
    }


def test_success_file_contains_unchanged_observation(tmp_path):
    observation = _observation()
    result = _api()(observation, owned_root=str(tmp_path), file_name="receipt-success.json")
    assert result["write_state"] == "WRITTEN_READ_BACK"
    content = json.loads((tmp_path / "receipt-success.json").read_bytes())
    assert content["observation"] == observation
    assert content["observation"]["archive_receipt"]["write_state"] == "UNKNOWN"
    assert content["status"] == "NOT_EVALUATED" and content["metrics"] is None


def test_capture_error_without_report_can_be_saved(tmp_path):
    observation = _observation()
    observation.update(phase="CAPTURE_ERROR", error_type="ClockError", not_archived_reason="capture_exception_no_report", archive_receipt=None)
    observation["attempt"]["report"] = None
    result = _api()(observation, owned_root=str(tmp_path), file_name="receipt-failure.json")
    assert result["write_state"] == "WRITTEN_READ_BACK"
    assert json.loads((tmp_path / "receipt-failure.json").read_bytes())["observation"] == observation


@pytest.mark.parametrize("phase,receipt", [
    ("CAPTURE_ERROR", None), ("CAPTURE_NO_REPORT", None),
    ("ARCHIVE_ERROR", None),
    ("ARCHIVE_RETURNED", {"write_state": "REFUSED"}),
    ("ARCHIVE_RETURNED", {"write_state": "UNKNOWN", "created": None}),
    ("ARCHIVE_RETURNED", {"write_state": "WRITTEN_READ_BACK"}),
    ("ARCHIVE_RETURNED", ["unclassified", None, False]),
])
def test_original_stages_and_receipt_values_never_promoted(tmp_path, phase, receipt):
    observation = _observation()
    observation.update(phase=phase, archive_receipt=receipt, error_type="OriginalError", not_archived_reason="原原因")
    if phase.startswith("CAPTURE_"):
        observation["attempt"]["report"] = None
    result = _api()(observation, owned_root=str(tmp_path), file_name="receipt-stages.json")
    assert result["write_state"] == "WRITTEN_READ_BACK"
    assert json.loads((tmp_path / "receipt-stages.json").read_bytes())["observation"] == observation
    assert all(result[key] is None for key in ("metrics", "denominator", "healthy_online_us"))


@pytest.mark.parametrize("receipt", [None, [], 0, {}, {"archive_path": None}, {"archive_path": 23}, {"archive_path": ""}])
def test_unknown_old_path_not_invented_or_rejected(tmp_path, receipt):
    observation = _observation()
    observation["archive_receipt"] = receipt
    result = _api()(observation, owned_root=str(tmp_path), file_name="receipt-unknown.json")
    assert result["write_state"] == "WRITTEN_READ_BACK"
    assert result["old_archive_label"] is None and result["old_archive_label_status"] == "UNKNOWN"
    assert json.loads((tmp_path / "receipt-unknown.json").read_bytes())["observation"]["archive_receipt"] == receipt


def test_exact_canonical_file_and_return_schemas_and_real_hashes(tmp_path):
    observation = _observation()
    observation["attempt"]["report"]["unicode"] = "原文"
    result = _api()(observation, owned_root=str(tmp_path), file_name="receipt-schema.json")
    raw = (tmp_path / "receipt-schema.json").read_bytes()
    envelope = json.loads(raw)
    assert set(envelope) == {"schema", "status", "declaration_status", "observation", "metrics", "denominator", "healthy_online_us"}
    assert envelope["schema"] == "m2_observation_receipt/1" and envelope["declaration_status"] == "UNVERIFIED"
    assert raw == (json.dumps(envelope, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode()
    assert set(result) == {"schema", "status", "declaration_status", "metrics", "denominator", "healthy_online_us", "write_state", "phase", "error_type", "cleanup_error_type", "created", "receipt_path", "expected_bytes", "expected_sha256", "readback_bytes", "readback_sha256", "old_archive_label", "old_archive_label_status"}
    assert result["schema"] == "m2_receipt_write/1" and result["phase"] == "done" and result["created"] is True
    assert result["error_type"] is None and result["cleanup_error_type"] is None
    assert result["expected_bytes"] == result["readback_bytes"] == len(raw)
    assert result["expected_sha256"] == result["readback_sha256"] == hashlib.sha256(raw).hexdigest()
    assert result["old_archive_label"] == observation["archive_receipt"]["archive_path"]
    assert result["old_archive_label_status"] == "PUBLISHED_LEXICAL"


def test_snapshot_bytes_not_late_mutated_input(tmp_path, monkeypatch):
    api = _api()
    observation = _observation()
    before = json.loads(json.dumps(observation))
    def change_original(path, mode):
        if mode == "xb":
            observation["planned_slot"]["slot_id"] = "mutated"
            observation["archive_receipt"]["write_state"] = "mutated"
        return builtins.open(path, mode)
    monkeypatch.setitem(api.__globals__, "open", change_original)
    result = api(observation, owned_root=str(tmp_path), file_name="receipt-snapshot.json")
    assert result["write_state"] == "WRITTEN_READ_BACK"
    assert json.loads((tmp_path / "receipt-snapshot.json").read_bytes())["observation"] == before
    assert observation != before


def test_shared_json_reference_is_not_a_cycle(tmp_path):
    observation = _observation()
    shared = {"ok": [1, True, None, 2.5]}
    observation["attempt"]["report"] = [shared, shared]
    result = _api()(observation, owned_root=str(tmp_path), file_name="receipt-shared.json")
    assert result["write_state"] == "WRITTEN_READ_BACK"
    assert json.loads((tmp_path / "receipt-shared.json").read_bytes())["observation"] == observation


def _preio_refused(api, monkeypatch, observation, root, name):
    calls = []
    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("application IO before validation")
    with monkeypatch.context() as patch:
        patch.setattr(Path, "lstat", forbidden)
        patch.setattr(Path, "resolve", forbidden)
        patch.setitem(api.__globals__, "open", forbidden)
        result = api(observation, owned_root=root, file_name=name)
    assert calls == []
    assert result["write_state"] == "REFUSED" and result["created"] is False
    assert result["phase"] == "validate" and result["error_type"] is not None


class _StringSubclass(str):
    pass


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), b"raw", (1,), {1}, object(), _StringSubclass("x"), "\ud800"])
def test_nonplain_or_nonfinite_report_rejected_before_io(tmp_path, monkeypatch, bad):
    observation = _observation()
    observation["attempt"]["report"] = bad
    _preio_refused(_api(), monkeypatch, observation, str(tmp_path), "receipt-bad.json")


def test_cycle_rejected_before_io(tmp_path, monkeypatch):
    observation = _observation()
    observation["attempt"]["report"] = observation
    _preio_refused(_api(), monkeypatch, observation, str(tmp_path), "receipt-cycle.json")


@pytest.mark.parametrize("change", ["extra", "missing", "part", "empty", "binding", "metric", "phase", "error"])
def test_observation_shape_rejected_before_io(tmp_path, monkeypatch, change):
    observation = _observation()
    if change == "extra": observation["extra"] = 1
    elif change == "missing": del observation["schema"]
    elif change == "part": observation["planned_slot"]["extra"] = 1
    elif change == "empty": observation["attempt"]["attempt_id"] = ""
    elif change == "binding": observation["binding"] = []
    elif change == "metric": observation["metrics"] = 0
    elif change == "phase": observation["phase"] = "HEALTHY"
    elif change == "error": observation["error_type"] = []
    _preio_refused(_api(), monkeypatch, observation, str(tmp_path), "receipt-shape.json")


@pytest.mark.parametrize("name", [None, 1, "../receipt-x.json", "receipt-X.json", "receipt-x.jsonl", ".env", "hb-x.jsonl", _StringSubclass("receipt-x.json")])
def test_bad_file_names_rejected_before_io(tmp_path, monkeypatch, name):
    _preio_refused(_api(), monkeypatch, _observation(), str(tmp_path), name)


@pytest.mark.parametrize("root", [None, 42, "relative", "//server/share", "\\\\?\\C:\\fake", "suffix:.json", ".env", ".ENV.secret", "CON.txt", "COM1", "LPT9.json", "trailing.", "..", "."])
def test_bad_roots_rejected_before_io(tmp_path, monkeypatch, root):
    if type(root) is str and root not in ("relative", "//server/share", "\\\\?\\C:\\fake"):
        root = str(tmp_path) + "/" + root
    _preio_refused(_api(), monkeypatch, _observation(), root, "receipt-root.json")


@pytest.mark.parametrize("style", ["same", "basename", "backslash"])
def test_old_target_lexical_address_or_basename_refused_preio(tmp_path, monkeypatch, style):
    observation = _observation()
    old = str(tmp_path / "receipt-same.json")
    if style == "basename": old = str(tmp_path / "different" / "RECEIPT-SAME.JSON")
    if style == "backslash": old = old.replace("/", "\\").replace("receipt-same", ".\\RECEIPT-SAME")
    observation["archive_receipt"]["archive_path"] = old
    _preio_refused(_api(), monkeypatch, observation, str(tmp_path), "receipt-same.json")


def test_only_new_target_opened_old_source_and_partial_untouched(tmp_path, monkeypatch):
    api = _api()
    source, partial = tmp_path / "old-source.json", tmp_path / "hb-partial.jsonl"
    source.write_bytes(b"synthetic source")
    partial.write_bytes(b"partial bytes without newline")
    observation = _observation()
    observation["attempt"]["report"]["input"]["source"] = str(source)
    observation["archive_receipt"]["archive_path"] = str(partial)
    calls = []
    def own_open(path, mode):
        calls.append((Path(path), mode))
        assert Path(path) == tmp_path / "receipt-isolated.json"
        return builtins.open(path, mode)
    monkeypatch.setitem(api.__globals__, "open", own_open)
    result = api(observation, owned_root=str(tmp_path), file_name="receipt-isolated.json")
    assert result["write_state"] == "WRITTEN_READ_BACK"
    assert calls == [(tmp_path / "receipt-isolated.json", "xb"), (tmp_path / "receipt-isolated.json", "rb")]
    assert source.read_bytes() == b"synthetic source" and partial.read_bytes() == b"partial bytes without newline"


def test_existing_new_target_unchanged_and_not_read(tmp_path, monkeypatch):
    api = _api()
    target = tmp_path / "receipt-existing.json"
    target.write_bytes(b"existing must survive")
    calls = []
    def own_open(path, mode):
        calls.append(mode)
        return builtins.open(path, mode)
    monkeypatch.setitem(api.__globals__, "open", own_open)
    result = api(_observation(), owned_root=str(tmp_path), file_name=target.name)
    assert result["write_state"] == "REFUSED" and result["created"] is False and result["phase"] == "create"
    assert calls == ["xb"] and target.read_bytes() == b"existing must survive"


@pytest.mark.parametrize("exists", [False, True])
def test_missing_or_nondirectory_root_definitely_refused(tmp_path, exists):
    root = tmp_path / "root"
    if exists: root.write_bytes(b"not a directory")
    result = _api()(_observation(), owned_root=str(root), file_name="receipt-root.json")
    assert result["write_state"] == "REFUSED" and result["created"] is False and result["phase"] == "root"


def test_root_metadata_uncertainty_is_not_definite_refusal(tmp_path, monkeypatch):
    api = _api()
    def denied(*args, **kwargs): raise PermissionError("synthetic")
    with monkeypatch.context() as patch:
        patch.setattr(Path, "lstat", denied)
        result = api(_observation(), owned_root=str(tmp_path), file_name="receipt-root.json")
    assert result["write_state"] == "UNKNOWN" and result["created"] is False and result["phase"] == "root"


def test_other_create_error_preserves_uncertain_creation(tmp_path, monkeypatch):
    api = _api()
    def denied(*args, **kwargs): raise PermissionError("synthetic")
    monkeypatch.setitem(api.__globals__, "open", denied)
    result = api(_observation(), owned_root=str(tmp_path), file_name="receipt-create.json")
    assert result["write_state"] == "UNKNOWN" and result["created"] is None and result["phase"] == "create"


class _FaultHandle:
    def __init__(self, actual, mode, fault, closes, primary=None, cleanup=None):
        self.actual, self.mode, self.fault, self.closes = actual, mode, fault, closes
        self.primary, self.cleanup = primary, cleanup

    def write(self, data):
        if self.primary is not None:
            self.actual.write(data[:3])
            raise self.primary
        if self.fault == "short": return self.actual.write(data[:3])
        if self.fault == "write": raise OSError("synthetic write")
        return self.actual.write(data)

    def flush(self):
        if self.fault == "flush": raise OSError("synthetic flush")
        return self.actual.flush()

    def fileno(self): return self.actual.fileno()

    def read(self):
        if self.fault == "readback": raise OSError("synthetic read")
        raw = self.actual.read()
        return raw + b"!" if self.fault == "compare" else raw

    def close(self):
        self.closes.append(self.mode)
        self.actual.close()
        if self.cleanup is not None: raise self.cleanup
        if (self.mode == "xb" and self.fault == "close") or (self.mode == "rb" and self.fault == "readback_close"):
            raise OSError("synthetic close")


@pytest.mark.parametrize("fault,phase", [("short", "write"), ("write", "write"), ("flush", "flush"), ("fsync", "fsync"), ("close", "close"), ("readback_open", "readback_open"), ("readback", "readback"), ("readback_close", "readback_close"), ("compare", "compare")])
def test_new_writer_faults_keep_unknown_residue_and_close_once(tmp_path, monkeypatch, fault, phase):
    api = _api()
    closes, modes = [], []
    def own_open(path, mode):
        modes.append(mode)
        if mode == "rb" and fault == "readback_open": raise FileExistsError("postcreate EEXIST is UNKNOWN")
        return _FaultHandle(builtins.open(path, mode), mode, fault, closes)
    monkeypatch.setitem(api.__globals__, "open", own_open)
    if fault == "fsync":
        def denied(*args): raise OSError("synthetic fsync")
        monkeypatch.setattr(api.__globals__["os"], "fsync", denied)
    result = api(_observation(), owned_root=str(tmp_path), file_name="receipt-fault.json")
    assert result["write_state"] == "UNKNOWN" and result["created"] is True and result["phase"] == phase
    assert result["error_type"] is not None and result["cleanup_error_type"] is None
    assert closes.count("xb") == 1 and closes.count("rb") <= 1
    assert modes.count("xb") == 1 and modes.count("rb") <= 1
    assert (tmp_path / "receipt-fault.json").exists()
    if fault == "short": assert len((tmp_path / "receipt-fault.json").read_bytes()) == 3
    if fault == "compare": assert result["readback_bytes"] == result["expected_bytes"] + 1


def test_primary_and_cleanup_errors_separate_and_partial_retained(tmp_path, monkeypatch):
    api = _api()
    closes = []
    def own_open(path, mode):
        return _FaultHandle(builtins.open(path, mode), mode, "", closes, OSError("primary"), RuntimeError("cleanup"))
    monkeypatch.setitem(api.__globals__, "open", own_open)
    result = api(_observation(), owned_root=str(tmp_path), file_name="receipt-cleanup.json")
    assert result["error_type"] == "OSError" and result["cleanup_error_type"] == "RuntimeError"
    assert result["write_state"] == "UNKNOWN" and result["created"] is True and result["phase"] == "write"
    assert closes == ["xb"] and len((tmp_path / "receipt-cleanup.json").read_bytes()) == 3


@pytest.mark.parametrize("error", [KeyboardInterrupt(), SystemExit(), MemoryError(), RecursionError()])
def test_base_and_resource_errors_propagate_after_one_cleanup(tmp_path, monkeypatch, error):
    api = _api()
    closes = []
    def own_open(path, mode): return _FaultHandle(builtins.open(path, mode), mode, "", closes, error)
    monkeypatch.setitem(api.__globals__, "open", own_open)
    with pytest.raises(type(error)):
        api(_observation(), owned_root=str(tmp_path), file_name="receipt-base.json")
    assert closes == ["xb"] and (tmp_path / "receipt-base.json").read_bytes()


def test_cleanup_base_may_supersede_primary_base(tmp_path, monkeypatch):
    api = _api()
    closes = []
    def own_open(path, mode):
        return _FaultHandle(builtins.open(path, mode), mode, "", closes, KeyboardInterrupt("primary"), SystemExit("cleanup"))
    monkeypatch.setitem(api.__globals__, "open", own_open)
    with pytest.raises(SystemExit):
        api(_observation(), owned_root=str(tmp_path), file_name="receipt-base-cleanup.json")
    assert closes == ["xb"] and (tmp_path / "receipt-base-cleanup.json").exists()


def test_cold_import_only_stdlib_and_zero_application_io(monkeypatch):
    source = (Path(__file__).resolve().parents[1] / "src" / "m2_receipt.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {n.module if isinstance(n, ast.ImportFrom) else a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in (n.names if isinstance(n, ast.Import) else [None])}
    assert imports <= {"hashlib", "json", "math", "os", "pathlib", "posixpath", "re", "stat"}
    code = compile(tree, "m2_receipt_cold_import", "exec")
    calls = []
    def forbidden(*args, **kwargs): calls.append(args); raise AssertionError("import application IO")
    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", forbidden)
        for name in ("open", "stat", "lstat", "resolve"):
            patch.setattr(Path, name, forbidden)
        namespace = {"__name__": "m2_receipt_cold_probe"}
        exec(code, namespace)
    assert calls == [] and callable(namespace["persist_observation_receipt"])
