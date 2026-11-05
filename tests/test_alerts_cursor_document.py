"""#244-C: observable document boundaries, real cursor continuation, and no app I/O.

Mutations caught: permissive byte coercion/JSON/shape, fake source binding,
swallowed escaping failures, reused results, and application side effects.
All alert/cursor payloads are synthetic in-memory bytes; no host-root fixture.
"""
from __future__ import annotations

import builtins
import hashlib
import importlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import time
import types

import pytest


def api():
    # Missing API is an assertion failure inside a test, never collection/setup.
    assert importlib.util.find_spec("src.alerts_cursor_document") is not None, (
        "missing public strict cursor document API"
    )
    return importlib.import_module("src.alerts_cursor_document")


def document(**changes):
    value = {"schema": "pod_alert_cursor/1", "offset": 0,
             "prefix_sha256": "0" * 64}
    value.update(changes)
    return json.dumps(value, ensure_ascii=True).encode("utf-8")


def alert(name):
    return (json.dumps(
        {"ts": "2026-11-05T00:00:00+00:00", "from": "synthetic",
         "alert": name, "severity": "warn", "proc": "unit",
         "detail": "中文😀", "exchange_action": "none"},
        ensure_ascii=False, separators=(",", ":")
    ) + "\r\n").encode("utf-8")


def test_real_kernel_cursor_json_roundtrip_consumes_only_appended_multibyte_crlf():
    decoder = api()
    from src.alerts_digest import digest_snapshot

    first = alert("reconnect_storm")
    appended = alert("scan_too_old")
    old = digest_snapshot(first)["next_cursor"]
    encoded = json.dumps(old).encode("utf-8")
    candidate = decoder.decode_cursor_document(encoded)
    assert candidate == old and candidate is not old
    result = digest_snapshot(first + appended, cursor=candidate)
    assert result["new_rows"] == 1 and result["rows_total"] == 2
    assert result["groups"] == [
        {"category": "scan_too_old", "severity": "warn", "count": 1}
    ]
    assert candidate == old and encoded == json.dumps(old).encode("utf-8")
    assert result["delivery"] == "NOT_DELIVERED"
    assert result["runtime_health"] == "NOT_EVALUATED"


@pytest.mark.parametrize("payload", [b"", alert("other")], ids=["empty", "EOF"])
def test_real_kernel_empty_and_eof_cursor_returns_no_new_alerts(payload):
    decoder = api()
    from src.alerts_digest import digest_snapshot

    original = digest_snapshot(payload)["next_cursor"]
    candidate = decoder.decode_cursor_document(json.dumps(original).encode())
    assert candidate == original and candidate is not original
    result = digest_snapshot(payload, cursor=candidate)
    assert result["new_rows"] == 0 and result["status"] == "NO_NEW_ALERTS"


@pytest.mark.parametrize("kind", ["zero_wrong_hash", "beyond_end", "not_lf"])
def test_document_candidate_does_not_certify_snapshot_binding(kind):
    decoder = api()
    from src.alerts_digest import DigestInputError, digest_snapshot

    payload = alert("other")
    offset = {"zero_wrong_hash": 0, "beyond_end": len(payload) + 1,
              "not_lf": 1}[kind]
    fingerprint = hashlib.sha256(payload[:offset]).hexdigest()
    if kind == "zero_wrong_hash":
        fingerprint = "0" * 64
    candidate = decoder.decode_cursor_document(
        document(offset=offset, prefix_sha256=fingerprint)
    )
    assert candidate["offset"] == offset
    with pytest.raises(DigestInputError, match="^cursor$"):
        digest_snapshot(payload, cursor=candidate)


class BytesSubclass(bytes):
    pass


@pytest.mark.parametrize("payload", [
    "", None, 1, True, bytearray(b"{}"), memoryview(b"{}"),
    BytesSubclass(b"{}"),
], ids=["str", "none", "int", "bool", "bytearray", "memoryview", "subclass"])
def test_non_exact_bytes_refuse_without_coercion(payload):
    decoder = api()
    with pytest.raises(decoder.CursorDocumentError) as got:
        decoder.decode_cursor_document(payload)
    assert type(got.value) is decoder.CursorDocumentError
    assert got.value.args == ("payload_type",)


@pytest.mark.parametrize("payload", [
    b"", b"\xff", b"\xef\xbb\xbf{}", b"{}{}", b"{} trailing",
    b'{"a":1,"a":2}', b'{"x":{"a":1,"a":2}}',
    b'{"x":[NaN]}', b'{"x":Infinity}', b'{"x":-Infinity}',
    b'{"x":1e999}', b'{"x":"\\ud800"}',
    b'{"\\udfff":0}', b'{"x":[{"y":"\\ud800"}]}',
    b'{"x":', b'{"x":01}', b"\x00{}", b"{} \xc2\xa0",
], ids=["empty", "utf8", "bom", "second_doc", "garbage", "duplicate",
        "nested_duplicate", "nan", "inf", "minus_inf", "overflow_float",
        "surrogate_value", "surrogate_key", "nested_surrogate",
        "unfinished", "leading_zero", "nul", "non_json_space"])
def test_invalid_json_anywhere_has_fixed_json_reason(payload):
    decoder = api()
    with pytest.raises(decoder.CursorDocumentError) as got:
        decoder.decode_cursor_document(payload)
    assert got.value.args == ("json",)
    assert got.value.__cause__ is None and got.value.__suppress_context__


@pytest.mark.parametrize("value", [
    None, True, 0, [], "text", {},
    {"schema": "pod_alert_cursor/1", "offset": 0},
    {"schema": "wrong", "offset": 0, "prefix_sha256": "0" * 64},
    {"schema": 1, "offset": 0, "prefix_sha256": "0" * 64},
    {"schema": "pod_alert_cursor/1", "offset": True, "prefix_sha256": "0" * 64},
    {"schema": "pod_alert_cursor/1", "offset": 0.0, "prefix_sha256": "0" * 64},
    {"schema": "pod_alert_cursor/1", "offset": -1, "prefix_sha256": "0" * 64},
    {"schema": "pod_alert_cursor/1", "offset": "0", "prefix_sha256": "0" * 64},
    {"schema": "pod_alert_cursor/1", "offset": 0, "prefix_sha256": None},
    {"schema": "pod_alert_cursor/1", "offset": 0, "prefix_sha256": "A" * 64},
    {"schema": "pod_alert_cursor/1", "offset": 0, "prefix_sha256": "g" * 64},
    {"schema": "pod_alert_cursor/1", "offset": 0, "prefix_sha256": "0" * 63},
    {"schema": "pod_alert_cursor/1", "offset": 0, "prefix_sha256": "0" * 65},
    {"schema": "pod_alert_cursor/1", "offset": 0, "prefix_sha256": "0" * 64,
     "unknown": {"valid": [1, "中文"]}},
], ids=["null", "bool", "int", "array", "string", "empty",
        "missing", "schema_value", "schema_type", "offset_bool",
        "offset_float", "offset_negative", "offset_string", "hash_type",
        "hash_upper", "hash_nonhex", "hash_short", "hash_long", "extra"])
def test_valid_json_wrong_cursor_shape_has_fixed_cursor_reason(value):
    decoder = api()
    with pytest.raises(decoder.CursorDocumentError) as got:
        decoder.decode_cursor_document(json.dumps(value).encode())
    assert got.value.args == ("cursor",)


@pytest.mark.parametrize("surrounding", [b"", b" ", b"\t\r\n", b"\n \t\r"])
def test_json_whitespace_and_large_unbound_offset_are_accepted(surrounding):
    decoder = api()
    payload = surrounding + document(offset=2**200) + surrounding
    candidate = decoder.decode_cursor_document(payload)
    assert type(candidate) is dict and set(candidate) == {
        "schema", "offset", "prefix_sha256"
    }
    assert type(candidate["offset"]) is int and candidate["offset"] == 2**200


def test_fresh_plain_result_and_immutable_payload():
    decoder = api()
    payload = document(prefix_sha256="0123456789abcdef" * 4)
    before = bytes(payload)
    first = decoder.decode_cursor_document(payload)
    second = decoder.decode_cursor_document(payload)
    assert type(first) is dict and first == second and first is not second
    first["offset"] = 99
    assert second["offset"] == 0 and payload == before


@pytest.mark.parametrize("failure", [
    MemoryError("private"), RecursionError("private"), SystemExit(0),
    SystemExit(5), KeyboardInterrupt("private"), RuntimeError("programming"),
], ids=["memory", "recursion", "exit0", "exit5", "interrupt", "programming"])
def test_real_json_callee_escaping_failure_retains_identity_and_no_result(
        monkeypatch, failure):
    decoder = api()
    payload = document()
    sentinel = object()
    adopted = sentinel

    def fail(*args, **kwargs):
        raise failure

    with monkeypatch.context() as patch:
        patch.setattr(decoder.json, "loads", fail)
        with pytest.raises(type(failure)) as got:
            adopted = decoder.decode_cursor_document(payload)
    assert got.value is failure and adopted is sentinel


@pytest.mark.parametrize("failure", [
    ValueError("private cursor bytes"), UnicodeError("private cursor bytes"),
])
def test_ordinary_json_callee_decode_failure_is_sanitized(monkeypatch, failure):
    decoder = api()
    payload = document()

    def fail(*args, **kwargs):
        raise failure

    with monkeypatch.context() as patch:
        patch.setattr(decoder.json, "loads", fail)
        with pytest.raises(decoder.CursorDocumentError) as got:
            decoder.decode_cursor_document(payload)
    assert got.value.args == ("json",)
    assert got.value.__cause__ is None and got.value.__suppress_context__


@pytest.mark.parametrize("failure", [
    MemoryError("private"), RecursionError("private"), SystemExit(0),
    SystemExit(5), KeyboardInterrupt("private"),
])
def test_real_finite_check_failure_retains_identity(monkeypatch, failure):
    decoder = api()

    def fail(value):
        raise failure

    sentinel = object()
    adopted = sentinel
    with monkeypatch.context() as patch:
        patch.setattr(decoder.math, "isfinite", fail)
        with pytest.raises(type(failure)) as got:
            adopted = decoder.decode_cursor_document(b'{"unknown":[1.25]}')
    assert got.value is failure and adopted is sentinel


def test_cold_namespace_import_and_decode_have_zero_application_io(monkeypatch):
    decoder = api()
    # Explicit code I/O occurs BEFORE the application-I/O fence.
    source = Path(decoder.__file__).read_bytes()
    compiled = compile(source, "<cursor-document-code-read-before-fence>", "exec")
    cold = types.ModuleType("cursor_document_cold")
    payload = document()
    calls = []

    def forbidden(*args, **kwargs):
        calls.append("application_io")
        raise AssertionError("application I/O")

    with monkeypatch.context() as patch:
        for target, name in [
            (builtins, "open"), (builtins, "print"), (io, "open"),
            (os, "open"), (os, "system"), (Path, "open"),
            (socket, "socket"), (socket, "create_connection"),
            (subprocess, "Popen"), (time, "time"), (time, "monotonic"),
        ]:
            patch.setattr(target, name, forbidden)
        exec(compiled, cold.__dict__)
        result = cold.decode_cursor_document(payload)
        with pytest.raises(cold.CursorDocumentError) as got:
            cold.decode_cursor_document(b"{}")
    assert calls == [] and got.value.args == ("cursor",)
    assert result == {"schema": "pod_alert_cursor/1", "offset": 0,
                      "prefix_sha256": "0" * 64}
