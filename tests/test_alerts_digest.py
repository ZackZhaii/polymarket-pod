"""#244-A: real byte consumption; no CLI, runtime inputs or receipt-writer mocks."""
from __future__ import annotations

import copy
import hashlib
import importlib
import importlib.util
import json

import pytest


def _core():
    assert importlib.util.find_spec("src.alerts_digest") is not None, "missing actual #244-A pure digest kernel"
    return importlib.import_module("src.alerts_digest")


def _row(**changes):
    row = {"ts": "2026-11-03T00:00:01+09:00", "from": "Sentinel", "alert": "runner_paused_too_long",
           "severity": "sev1", "proc": "m2_runner", "detail": "\u6c49\U0001f600", "exchange_action": "none"}
    row.update(changes)
    return row


def _line(row, ending=b"\n"):
    return json.dumps(row, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8") + ending


def _cursor(payload, offset=None):
    offset = len(payload) if offset is None else offset
    return {"schema": "pod_alert_cursor/1", "offset": offset,
            "prefix_sha256": hashlib.sha256(payload[:offset]).hexdigest()}


def test_real_byte_cursor_resume_counts_only_new_rows():
    # Catches character offsets, dropped duplicate/unknown rows and replayed consumed rows.
    core = _core()
    one = _line(_row())
    payload = one + one + _line(_row(alert="future_alert", severity="future_severity"), b"\r\n")
    first = core.digest_snapshot(payload)
    assert first == {
        "schema": "pod_alert_digest/1", "status": "ALERTS_PRESENT", "input_bytes": len(payload),
        "input_sha256": hashlib.sha256(payload).hexdigest(), "rows_total": 3, "new_rows": 3,
        "groups": [{"category": "other", "severity": "other", "count": 1},
                   {"category": "runner_paused_too_long", "severity": "sev1", "count": 2}],
        "cursor_used": _cursor(b""), "next_cursor": _cursor(payload),
        "delivery": "NOT_DELIVERED", "runtime_health": "NOT_EVALUATED",
    }
    supplied = first["next_cursor"]
    before = copy.deepcopy(supplied)
    appended = payload + _line(_row(ts="2026-10-01T00:00:00Z", alert="settlement_conflict", severity="warn"))
    second = core.digest_snapshot(appended, cursor=supplied)
    assert (second["rows_total"], second["new_rows"], second["groups"]) == (
        4, 1, [{"category": "settlement", "severity": "warn", "count": 1}])
    assert second["cursor_used"] == before and supplied == before
    assert second["cursor_used"] is not supplied and second["next_cursor"] is not supplied
    assert second["next_cursor"] == _cursor(appended)
    eof = core.digest_snapshot(appended, cursor=second["next_cursor"])
    assert (eof["status"], eof["new_rows"], eof["groups"]) == ("NO_NEW_ALERTS", 0, [])
    assert len(appended.decode("utf-8")) < len(appended)


def test_bad_row_in_consumed_prefix_still_refuses():
    # Catches validation that silently skips corrupt already-consumed bytes.
    core = _core()
    bad_prefix = b"not-json\n"
    payload = bad_prefix + _line(_row())
    with pytest.raises(core.DigestInputError):
        core.digest_snapshot(payload, cursor=_cursor(payload, len(bad_prefix)))


@pytest.mark.parametrize("detail", ["\u6c49\U0001f600", "\u2028", "\u2029", "\ufeff"])
def test_legal_unicode_is_one_record(detail):
    # Catches splitlines framing and blanket rejection of valid Unicode.
    result = _core().digest_snapshot(_line(_row(detail=detail)))
    assert result["rows_total"] == result["new_rows"] == 1
    assert result["groups"] == [{"category": "runner_paused_too_long", "severity": "sev1", "count": 1}]


def test_escaped_pair_and_finite_extra_and_zoned_non_utc_time_are_valid():
    row = _row(ts="2026-10-01T01:02:03-04:00", detail="\U0001f600", proc="", extra={"int": 10**100, "f": 1.25, "x": [True, None, "\ufeff"]})
    payload = json.dumps(row, ensure_ascii=True, allow_nan=False).encode() + b"\n"
    assert b"\\ud83d\\ude00" in payload
    result = _core().digest_snapshot(payload)
    assert result["rows_total"] == result["new_rows"] == 1


def test_unknown_severity_is_counted_without_case_or_space_normalization():
    rows = [_row(severity=s) for s in ["sev1", "SEV1", " sev1 "]]
    rows.append(_row(alert="unrecognized", severity="warn"))
    result = _core().digest_snapshot(b"".join(_line(r) for r in rows))
    assert result["groups"] == [
        {"category": "other", "severity": "warn", "count": 1},
        {"category": "runner_paused_too_long", "severity": "other", "count": 2},
        {"category": "runner_paused_too_long", "severity": "sev1", "count": 1},
    ]
    assert result["new_rows"] == 4


def test_empty_and_initial_cursor_are_valid_and_do_not_alias():
    core = _core()
    empty = core.digest_snapshot(b"", cursor=_cursor(b""))
    assert empty == {"schema": "pod_alert_digest/1", "status": "NO_NEW_ALERTS", "input_bytes": 0,
                     "input_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
                     "rows_total": 0, "new_rows": 0, "groups": [], "cursor_used": _cursor(b""),
                     "next_cursor": _cursor(b""), "delivery": "NOT_DELIVERED", "runtime_health": "NOT_EVALUATED"}
    assert empty["cursor_used"] is not empty["next_cursor"]
    payload = _line(_row())
    result = core.digest_snapshot(payload)
    cursor = result["next_cursor"]
    same_content_new_snapshot = bytes(bytearray(payload)) + _line(_row(alert="scan_too_old", severity="info"))
    resumed = core.digest_snapshot(same_content_new_snapshot, cursor=cursor)
    resumed["cursor_used"]["offset"] = -1
    assert cursor == _cursor(payload)
    assert resumed["new_rows"] == 1 and resumed["groups"] == [{"category": "scan_too_old", "severity": "info", "count": 1}]


_BASE = _line(_row())
@pytest.mark.parametrize("bad", [
    b"\xff\n", b"not-json\n", b"[]\n", b" \r\n", _BASE[:-1], b"\xef\xbb\xbf" + _BASE,
    _BASE[:-2] + b',"extra":{"a":1,"\\u0061":2}}\n',
    _BASE[:-2] + b',"extra":{"number":1e999}}\n',
    _BASE[:-2] + b',"extra":[NaN]}\n',
    _BASE[:-2] + b',"extra":{"x":Infinity}}\n',
    _BASE[:-2] + b',"extra":"\\ud800"}\n',
    _BASE[:-2] + b',"extra":{"\\udc00":1}}\n',
    _line(_row(detail="marker")).replace(b'"marker"', b'"\\udc00"'),
], ids=["badutf8", "badjson", "array", "blank", "torn", "bom", "nested-decoded-duplicate", "exponent-overflow", "nan-extra", "inf-extra", "surrogate-value", "surrogate-key", "surrogate-known-field"])
def test_invalid_json_anywhere_is_refused_without_partial_digest(bad):
    # A valid sev1 first must never mask a later invalid record or extra field.
    core = _core()
    with pytest.raises(core.DigestInputError):
        core.digest_snapshot(_BASE + bad)


@pytest.mark.parametrize("missing", ["ts", "from", "alert", "severity", "proc", "detail", "exchange_action"])
def test_every_producer_field_is_required(missing):
    row = _row()
    del row[missing]
    core = _core()
    with pytest.raises(core.DigestInputError):
        core.digest_snapshot(_line(row))


@pytest.mark.parametrize("change", [
    {"ts": "2026-11-03T00:00:00"}, {"ts": "invalid"}, {"from": " "}, {"alert": ""},
    {"severity": "\t"}, {"proc": 1}, {"detail": None}, {"exchange_action": "buy"},
])
def test_bad_field_types_and_naive_time_refuse(change):
    core = _core()
    with pytest.raises(core.DigestInputError):
        core.digest_snapshot(_line(_row(**change)))


@pytest.mark.parametrize("kind", ["negative", "bool", "outside", "midline", "multibyte", "hash", "uppercase", "schema", "extra", "offset-string", "schema-subclass", "dict-subclass"])
def test_cursor_rejects_wrong_types_boundaries_and_fingerprint(kind):
    core = _core()
    payload = _BASE
    cursor = _cursor(payload)
    if kind == "negative": cursor = _cursor(payload, -1)
    elif kind == "bool": cursor["offset"] = True
    elif kind == "outside": cursor["offset"] = len(payload) + 1
    elif kind == "midline": cursor = _cursor(payload, 1)
    elif kind == "multibyte": cursor = _cursor(payload, payload.index("\u6c49".encode()) + 1)
    elif kind == "hash": cursor["prefix_sha256"] = "0" * 64
    elif kind == "uppercase": cursor["prefix_sha256"] = cursor["prefix_sha256"].upper()
    elif kind == "schema": cursor["schema"] = "other"
    elif kind == "extra": cursor["extra"] = 0
    elif kind == "offset-string": cursor["offset"] = "0"
    elif kind == "schema-subclass":
        class S(str): pass
        cursor["schema"] = S("pod_alert_cursor/1")
    elif kind == "dict-subclass":
        class D(dict):
            def keys(self): raise AssertionError("must reject before custom methods")
        cursor = D(cursor)
    before = dict(cursor)
    with pytest.raises(core.DigestInputError):
        core.digest_snapshot(payload, cursor=cursor)
    assert dict(cursor) == before


def test_truncated_or_same_length_modified_prefix_never_resets():
    core = _core()
    payload = _BASE + _line(_row(alert="reconnect_storm"))
    cursor = core.digest_snapshot(payload)["next_cursor"]
    changed = payload.replace(b"Sentinel", b"Observer", 1)
    assert len(changed) == len(payload)
    for raw in [_BASE, changed]:
        with pytest.raises(core.DigestInputError):
            core.digest_snapshot(raw, cursor=cursor)
    # A valid prefix with an independently malformed suffix must fail too.
    with pytest.raises(core.DigestInputError):
        core.digest_snapshot(payload + b"broken\n", cursor=cursor)


@pytest.mark.parametrize("kind", ["str", "bytearray", "memoryview", "bytes-subclass"])
def test_payload_requires_exact_bytes(kind):
    core = _core()
    if kind == "str": payload = _BASE.decode()
    elif kind == "bytearray": payload = bytearray(_BASE)
    elif kind == "memoryview": payload = memoryview(_BASE)
    else:
        class B(bytes): pass
        payload = B(_BASE)
    with pytest.raises(core.DigestInputError):
        core.digest_snapshot(payload)


@pytest.mark.parametrize("failure", [MemoryError("owned"), RecursionError("owned"), KeyboardInterrupt(), SystemExit(7)])
def test_resource_and_base_interruptions_propagate_by_identity(monkeypatch, failure):
    core = _core()
    # Patch the helper in the actually called function's globals, not a stale module alias.
    def interrupted(_): raise failure
    with monkeypatch.context() as patch:
        patch.setitem(core.digest_snapshot.__globals__, "_decode_json", interrupted)
        with pytest.raises(type(failure)) as caught:
            core.digest_snapshot(_BASE)
    assert caught.value is failure


def test_real_kernel_and_fresh_namespace_have_no_application_io(monkeypatch):
    import ast
    import builtins
    import os
    import pathlib
    import socket
    import time
    from datetime import datetime

    core = _core()
    source = pathlib.Path(core.__file__).read_text(encoding="utf-8")
    compiled = compile(source, core.__file__, "exec")  # code IO occurs before tripwires
    tree = ast.parse(source)
    imports = {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    imports |= {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert imports <= {"__future__", "datetime", "hashlib", "json", "math"}
    assert not any(isinstance(n, ast.FunctionDef) and n.name == "main" for n in tree.body)
    assert not any(isinstance(n, ast.If) and "__name__" in ast.unparse(n.test) for n in tree.body)
    namespace = {"__name__": "owned_fb_cold_namespace"}
    calls = []
    def forbidden(*args, **kwargs):
        calls.append("application IO or clock")
        raise AssertionError("unexpected application IO or clock")
    class NoClock:
        fromisoformat = staticmethod(datetime.fromisoformat)
        now = utcnow = staticmethod(forbidden)
    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", forbidden)
        patch.setattr(pathlib.Path, "open", forbidden)
        patch.setattr(os, "open", forbidden)
        patch.setattr(socket, "socket", forbidden)
        patch.setattr(time, "time", forbidden)
        patch.setattr(time, "monotonic", forbidden)
        exec(compiled, namespace)
        namespace["datetime"] = NoClock
        patch.setitem(core.digest_snapshot.__globals__, "datetime", NoClock)
        cold = namespace["digest_snapshot"](_BASE)
        real = core.digest_snapshot(_BASE)
    assert calls == [] and cold == real
    assert real["new_rows"] == 1 and real["groups"] == [{"category": "runner_paused_too_long", "severity": "sev1", "count": 1}]
