"""FA author tests: actual unchanged EY/EZ calls on owned synthetic paths."""
import base64
import builtins
import copy
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path

import pytest


def _api():
    assert importlib.util.find_spec("src.m2_observe_persisted") is not None, "FA real-chain API absent on actual base"
    return importlib.import_module("src.m2_observe_persisted").observe_persisted_once


def _beat():
    return {"proc": "m2_runner", "pid": 1, "host": "owned-fa-fixture",
            "started_at": "2026-11-03T00:00:00Z", "ts": "2026-11-03T00:00:01Z", "seq": 0,
            "git_sha": None, "risk_sha256": None, "live_enabled": None, "mode": None,
            "state": "paused", "exit_code": None, "progress": {"synthetic": "原文"}}


def _case(tmp_path, *, missing=False, archive_existing=None, receipt_existing=None):
    # The source controller alone sets this nonsecret variable to the explicitly
    # granted Source owned base. CI defaults wholly inside pytest's own temp.
    base = Path(os.environ["POD_FA_OWNED_BASE"]) if "POD_FA_OWNED_BASE" in os.environ else tmp_path
    case = hashlib.sha256(str(tmp_path).encode()).hexdigest()[:12]
    inputs, archives, receipts = (base / part / case for part in ("inputs", "archives", "receipts"))
    for root in (inputs, archives, receipts): root.mkdir(parents=True)
    source = inputs / "heartbeat.json"
    raw = json.dumps(_beat(), ensure_ascii=False).encode("utf-8")
    if not missing: source.write_bytes(raw)
    if archive_existing is not None: (archives / "hb-one.jsonl").write_bytes(archive_existing)
    if receipt_existing is not None: (receipts / "receipt-one.json").write_bytes(receipt_existing)
    planned = {"slot_id": "slot-FA-汉字", "planned_at_utc": "opaque supplied plan time", "expected_proc": "m2_runner"}
    kwargs = {"attempt_id": "attempt-FA", "spec": {"source": str(source), "format": "json"},
              "archive_owned_root": str(archives), "archive_file_name": "hb-one.jsonl",
              "receipt_owned_root": str(receipts), "receipt_file_name": "receipt-one.json"}
    return planned, kwargs, raw


def _archive_path(kwargs): return Path(kwargs["archive_owned_root"]) / kwargs["archive_file_name"]


def _receipt_path(kwargs): return Path(kwargs["receipt_owned_root"]) / kwargs["receipt_file_name"]


def test_real_chain_creates_file_for_its_actual_return(tmp_path):
    api = _api()
    planned, kwargs, raw = _case(tmp_path)
    result = api(planned, **kwargs)
    assert set(result) == {"schema", "status", "declaration_status", "metrics", "denominator", "healthy_online_us", "observation", "receipt_write"}
    assert result["schema"] == "m2_observe_persisted/1"
    assert result["observation"]["archive_receipt"]["write_state"] == "WRITTEN_READ_BACK"
    assert result["receipt_write"]["write_state"] == "WRITTEN_READ_BACK"
    assert json.loads(_receipt_path(kwargs).read_bytes())["observation"] == result["observation"]
    assert json.loads(_archive_path(kwargs).read_bytes())["capture_report"] == result["observation"]["attempt"]["report"]
    assert base64.b64decode(result["observation"]["attempt"]["report"]["capture"]["raw_base64"]) == raw


def test_real_missing_source_failure_still_persists(tmp_path):
    api = _api()
    planned, kwargs, _ = _case(tmp_path, missing=True)
    result = api(planned, **kwargs)
    assert result["observation"]["attempt"]["report"]["capture"]["raw_base64"] is None
    assert result["observation"]["archive_receipt"]["write_state"] == "REFUSED"
    assert result["receipt_write"]["write_state"] == "WRITTEN_READ_BACK"
    assert not _archive_path(kwargs).exists()
    assert json.loads(_receipt_path(kwargs).read_bytes())["observation"] == result["observation"]


class _OSBinding:
    """Override one callee's os binding; never mutate the shared os module."""
    def __init__(self, original, **overrides):
        self.original, self.overrides = original, overrides

    def __getattr__(self, name):
        return self.overrides[name] if name in self.overrides else getattr(self.original, name)


def _trace(api, monkeypatch):
    # Bind from the function actually called: cold-import tests can leave
    # package parent attributes pointing at a different module namespace.
    real_ey = api.__globals__["observe_slot_once"]
    real_ez = api.__globals__["persist_observation_receipt"]
    real_ev = real_ey.__globals__["capture_heartbeat_once"]
    real_ew = real_ey.__globals__["archive_capture_once"]
    ev_globals, ew_globals, ez_globals = real_ev.__globals__, real_ew.__globals__, real_ez.__globals__
    ev_os, ew_os, ez_os = ev_globals["os"], ew_globals["os"], ez_globals["os"]
    real_clock, real_fdopen = ev_globals["_capture_stamp"], ev_os.fdopen
    ew_open, ez_open = ew_globals.get("open", builtins.open), ez_globals.get("open", builtins.open)
    record = {"calls": {key: 0 for key in ("EY", "EV", "EW", "EZ", "clock", "source_body", "EW_fsync", "EZ_fsync")},
              "returned": {key: [] for key in ("EY", "EV", "EW", "EZ")},
              "clock_values": [], "opens": {"EW": [], "EZ": []}, "fault": {}}
    def ey(*args, **kwargs):
        record["calls"]["EY"] += 1
        value = real_ey(*args, **kwargs)
        record["returned"]["EY"].append(value)
        return value
    def ev(*args, **kwargs):
        record["calls"]["EV"] += 1
        value = real_ev(*args, **kwargs)
        record["returned"]["EV"].append(value)
        return value
    def ew(report, **kwargs):
        record["calls"]["EW"] += 1
        assert report is record["returned"]["EV"][0]
        value = real_ew(report, **kwargs)
        record["returned"]["EW"].append(value)
        return value
    def ez(observation, **kwargs):
        record["calls"]["EZ"] += 1
        assert observation is record["returned"]["EY"][0]
        value = real_ez(observation, **kwargs)
        record["returned"]["EZ"].append(value)
        return value
    def clock():
        record["calls"]["clock"] += 1
        if record["calls"]["clock"] == record["fault"].get("clock_at"):
            raise record["fault"]["clock_error"]
        value = real_clock()
        record["clock_values"].append(value)
        return value
    class BodyReader:
        def __init__(self, handle): self.handle = handle
        def __enter__(self): self.handle.__enter__(); return self
        def __exit__(self, *args): return self.handle.__exit__(*args)
        def __getattr__(self, name): return getattr(self.handle, name)
        def read(self, *args, **kwargs):
            record["calls"]["source_body"] += 1
            return self.handle.read(*args, **kwargs)
    def fdopen(*args, **kwargs): return BodyReader(real_fdopen(*args, **kwargs))
    def ew_fsync(fd):
        record["calls"]["EW_fsync"] += 1
        if "EW_fsync" in record["fault"]: raise record["fault"]["EW_fsync"]
        return ew_os.fsync(fd)
    def ez_fsync(fd):
        record["calls"]["EZ_fsync"] += 1
        if "EZ_fsync" in record["fault"]: raise record["fault"]["EZ_fsync"]
        return ez_os.fsync(fd)
    def open_ew(path, mode):
        record["opens"]["EW"].append((Path(path), mode))
        return ew_open(path, mode)
    def open_ez(path, mode):
        record["opens"]["EZ"].append((Path(path), mode))
        return ez_open(path, mode)
    monkeypatch.setitem(api.__globals__, "observe_slot_once", ey)
    monkeypatch.setitem(api.__globals__, "persist_observation_receipt", ez)
    monkeypatch.setitem(real_ey.__globals__, "capture_heartbeat_once", ev)
    monkeypatch.setitem(real_ey.__globals__, "archive_capture_once", ew)
    monkeypatch.setitem(ev_globals, "_capture_stamp", clock)
    monkeypatch.setitem(ev_globals, "os", _OSBinding(ev_os, fdopen=fdopen))
    monkeypatch.setitem(ew_globals, "os", _OSBinding(ew_os, fsync=ew_fsync))
    monkeypatch.setitem(ez_globals, "os", _OSBinding(ez_os, fsync=ez_fsync))
    monkeypatch.setitem(ew_globals, "open", open_ew)
    monkeypatch.setitem(ez_globals, "open", open_ez)
    return record


def test_actual_single_calls_binding_times_and_inputs_are_preserved(tmp_path, monkeypatch):
    api = _api()
    planned, kwargs, raw = _case(tmp_path)
    before = copy.deepcopy((planned, kwargs))
    trace = _trace(api, monkeypatch)
    result = api(planned, **kwargs)
    assert trace["calls"] == {"EY": 1, "EV": 1, "EW": 1, "EZ": 1, "clock": 2, "source_body": 1, "EW_fsync": 1, "EZ_fsync": 1}
    assert result["observation"] is trace["returned"]["EY"][0]
    assert result["receipt_write"] is trace["returned"]["EZ"][0]
    assert (planned, kwargs) == before
    observation = result["observation"]
    assert observation["attempt"]["report"] is trace["returned"]["EV"][0]
    assert observation["archive_receipt"] is trace["returned"]["EW"][0]
    material = b"polymarket-pod/m2-observe-slot/1\x00" + json.dumps({"planned_slot": planned, "attempt_id": kwargs["attempt_id"]}, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
    digest = hashlib.sha256(material).hexdigest()
    assert observation["binding"] == {"scheme": "m2_slot_capture/1", "material_base64": base64.b64encode(material).decode(), "sha256": digest, "capture_id": "m2-slot-v1:" + digest}
    report = observation["attempt"]["report"]
    assert report["capture"]["capture_id"] == "m2-slot-v1:" + digest
    assert report["capture"]["started_at_utc"] == trace["clock_values"][0]
    assert report["capture"]["finished_at_utc"] == trace["clock_values"][1]
    assert observation["planned_slot"]["planned_at_utc"] == "opaque supplied plan time"
    assert report["declaration"]["ts"] == "2026-11-03T00:00:01Z"
    assert base64.b64decode(report["capture"]["raw_base64"]) == raw
    receipt_bytes = _receipt_path(kwargs).read_bytes()
    assert json.loads(receipt_bytes)["observation"] == trace["returned"]["EY"][0]
    assert result["receipt_write"]["expected_sha256"] == hashlib.sha256(receipt_bytes).hexdigest()
    assert trace["opens"] == {"EW": [(_archive_path(kwargs), "xb"), (_archive_path(kwargs), "rb")], "EZ": [(_receipt_path(kwargs), "xb"), (_receipt_path(kwargs), "rb")]}
    assert result["status"] == "NOT_EVALUATED" and result["declaration_status"] == "UNVERIFIED"
    assert all(result[key] is None for key in ("metrics", "denominator", "healthy_online_us"))


def test_second_clock_error_after_source_read_is_saved_without_fabrication(tmp_path, monkeypatch):
    api = _api()
    planned, kwargs, _ = _case(tmp_path)
    # Deliberately stale parent attribute: fault injection must still target
    # the actual EV globals held by the actual EY function.
    monkeypatch.setattr(importlib.import_module("src"), "m2_report", object(), raising=False)
    trace = _trace(api, monkeypatch)
    trace["fault"].update(clock_at=2, clock_error=RuntimeError("synthetic second endpoint"))
    result = api(planned, **kwargs)
    observation = result["observation"]
    assert trace["calls"]["source_body"] == 1 and trace["calls"]["clock"] == 2
    assert trace["calls"]["EW"] == 0 and trace["calls"]["EZ"] == 1
    assert trace["returned"]["EV"] == [] and len(trace["clock_values"]) == 1
    assert observation["phase"] == "CAPTURE_ERROR" and observation["error_type"] == "RuntimeError"
    assert observation["attempt"]["report"] is None and observation["archive_receipt"] is None
    assert result["receipt_write"]["write_state"] == "WRITTEN_READ_BACK"
    assert json.loads(_receipt_path(kwargs).read_bytes())["observation"] == observation
    assert not _archive_path(kwargs).exists()


def test_existing_raw_refusal_is_persisted_without_changing_existing_raw(tmp_path, monkeypatch):
    api = _api()
    planned, kwargs, _ = _case(tmp_path, archive_existing=b"old raw partial")
    trace = _trace(api, monkeypatch)
    result = api(planned, **kwargs)
    assert result["observation"]["archive_receipt"]["write_state"] == "REFUSED"
    assert result["receipt_write"]["write_state"] == "WRITTEN_READ_BACK"
    assert _archive_path(kwargs).read_bytes() == b"old raw partial"
    assert trace["opens"]["EW"] == [(_archive_path(kwargs), "xb")]
    assert json.loads(_receipt_path(kwargs).read_bytes())["observation"] == result["observation"]


def test_raw_fsync_unknown_does_not_poison_independent_receipt_writer(tmp_path, monkeypatch):
    api = _api()
    planned, kwargs, _ = _case(tmp_path)
    trace = _trace(api, monkeypatch)
    trace["fault"]["EW_fsync"] = OSError("only raw writer")
    result = api(planned, **kwargs)
    old = result["observation"]["archive_receipt"]
    assert old["write_state"] == "UNKNOWN" and old["created"] is True
    assert result["receipt_write"]["write_state"] == "WRITTEN_READ_BACK"
    assert trace["calls"]["EW_fsync"] == trace["calls"]["EZ_fsync"] == 1
    assert _archive_path(kwargs).exists()
    assert json.loads(_receipt_path(kwargs).read_bytes())["observation"]["archive_receipt"] == old


def test_existing_result_refusal_keeps_prior_raw_effect_and_old_result(tmp_path, monkeypatch):
    api = _api()
    planned, kwargs, _ = _case(tmp_path, receipt_existing=b"old result partial")
    trace = _trace(api, monkeypatch)
    result = api(planned, **kwargs)
    assert result["observation"]["archive_receipt"]["write_state"] == "WRITTEN_READ_BACK"
    assert result["receipt_write"]["write_state"] == "REFUSED" and result["receipt_write"]["created"] is False
    assert _receipt_path(kwargs).read_bytes() == b"old result partial"
    assert _archive_path(kwargs).exists()
    assert trace["opens"]["EZ"] == [(_receipt_path(kwargs), "xb")]


def test_result_fsync_unknown_keeps_successful_raw_and_new_residue(tmp_path, monkeypatch):
    api = _api()
    planned, kwargs, _ = _case(tmp_path)
    trace = _trace(api, monkeypatch)
    trace["fault"]["EZ_fsync"] = OSError("only result writer")
    result = api(planned, **kwargs)
    assert result["observation"]["archive_receipt"]["write_state"] == "WRITTEN_READ_BACK"
    assert result["receipt_write"]["write_state"] == "UNKNOWN" and result["receipt_write"]["created"] is True
    assert _archive_path(kwargs).exists() and _receipt_path(kwargs).exists()
    assert trace["calls"]["EW_fsync"] == trace["calls"]["EZ_fsync"] == 1
    assert json.loads(_archive_path(kwargs).read_bytes())["capture_report"] == result["observation"]["attempt"]["report"]


def test_receipt_parameter_rejection_is_after_actual_ey_effects(tmp_path, monkeypatch):
    api = _api()
    planned, kwargs, _ = _case(tmp_path)
    kwargs["receipt_file_name"] = "../bad.json"
    trace = _trace(api, monkeypatch)
    result = api(planned, **kwargs)
    assert result["receipt_write"]["write_state"] == "REFUSED" and result["receipt_write"]["phase"] == "validate"
    assert result["observation"]["archive_receipt"]["write_state"] == "WRITTEN_READ_BACK"
    assert _archive_path(kwargs).exists() and trace["opens"]["EZ"] == []
    assert trace["calls"]["EY"] == trace["calls"]["EZ"] == trace["calls"]["source_body"] == 1


def test_escaping_ey_validation_error_calls_no_capture_or_result_writer(tmp_path, monkeypatch):
    api = _api()
    planned, kwargs, _ = _case(tmp_path)
    kwargs["attempt_id"] = ""
    trace = _trace(api, monkeypatch)
    with pytest.raises(ValueError): api(planned, **kwargs)
    assert trace["calls"]["EY"] == 1
    assert all(trace["calls"][key] == 0 for key in ("EV", "EW", "EZ", "clock", "source_body"))
    assert not _archive_path(kwargs).exists() and not _receipt_path(kwargs).exists()


def test_ey_base_after_actual_source_read_never_calls_result_writer(tmp_path, monkeypatch):
    api = _api()
    planned, kwargs, _ = _case(tmp_path)
    trace = _trace(api, monkeypatch)
    trace["fault"].update(clock_at=2, clock_error=KeyboardInterrupt("synthetic interruption"))
    with pytest.raises(KeyboardInterrupt): api(planned, **kwargs)
    assert trace["calls"]["source_body"] == 1 and trace["calls"]["clock"] == 2
    assert trace["calls"]["EZ"] == trace["calls"]["EW"] == 0
    assert trace["returned"]["EY"] == []
    assert not _archive_path(kwargs).exists() and not _receipt_path(kwargs).exists()


@pytest.mark.parametrize("error", [SystemExit("synthetic interruption"), MemoryError("synthetic resource fault")])
def test_escaping_result_base_or_resource_error_preserves_previous_effects(tmp_path, monkeypatch, error):
    api = _api()
    planned, kwargs, _ = _case(tmp_path)
    trace = _trace(api, monkeypatch)
    trace["fault"]["EZ_fsync"] = error
    with pytest.raises(type(error)): api(planned, **kwargs)
    assert trace["calls"]["EY"] == trace["calls"]["EZ"] == 1 and trace["returned"]["EZ"] == []
    assert trace["returned"]["EY"][0]["archive_receipt"]["write_state"] == "WRITTEN_READ_BACK"
    assert _archive_path(kwargs).exists() and _receipt_path(kwargs).exists()


def test_cold_module_execution_has_zero_application_io(monkeypatch):
    importlib.import_module("src.m2_observe_slot")
    importlib.import_module("src.m2_receipt")
    source = Path(__file__).resolve().parents[1] / "src" / "m2_observe_persisted.py"
    code = compile(source.read_bytes(), str(source), "exec")
    calls = []
    def forbidden(*args, **kwargs): calls.append(args); raise AssertionError("cold module application IO")
    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", forbidden)
        patch.setattr(os, "open", forbidden)
        for name in ("open", "stat", "lstat", "resolve"): patch.setattr(Path, name, forbidden)
        namespace = {"__name__": "src.fa_cold_probe", "__package__": "src"}
        exec(code, namespace)
    assert calls == [] and callable(namespace["observe_persisted_once"])
