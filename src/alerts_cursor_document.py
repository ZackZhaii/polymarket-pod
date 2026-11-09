"""#244-C: pure cursor byte-document decoding, without file or CLI integration."""
from __future__ import annotations

import json
import math


class CursorDocumentError(ValueError):
    """Fixed reason only; never raw cursor bytes or decoder exception text."""


def _pairs(pairs):
    record = {}
    for key, value in pairs:
        if key in record:
            raise CursorDocumentError("json")
        record[key] = value
    return record


def _nonfinite(_):
    raise CursorDocumentError("json")


def _check_json(value):
    if type(value) is str:
        try:
            value.encode("utf-8", "strict")
        except UnicodeError:
            raise CursorDocumentError("json") from None
    elif type(value) is float:
        if not math.isfinite(value):
            raise CursorDocumentError("json") from None
    elif type(value) is dict:
        for key, child in value.items():
            _check_json(key)
            _check_json(child)
    elif type(value) is list:
        for child in value:
            _check_json(child)


def decode_cursor_document(payload: bytes) -> dict[str, object]:
    """Decode one strict UTF-8 JSON cursor document into a fresh plain dict.

    Accepts exact bytes only. Rejects duplicate keys, non-finite JSON values,
    invalid Unicode and any shape other than the three-field pod_alert_cursor/1
    candidate: exact string schema, exact nonnegative int offset, and a
    lowercase 64-hex prefix_sha256 string. A single document with JSON
    surrounding whitespace is allowed; BOM, trailing documents or garbage
    are rejected. Returns only schema/offset/prefix_sha256 in a new plain dict.

    This is document/shape validation, not binding to an alert snapshot.
    Offset upper bound, LF boundary and prefix content hash still require the
    real digest_snapshot when actual alert bytes are supplied. No fake empty
    snapshot, private validator, file I/O, clock, stdout, status, cursor saving,
    retry, network, process or runtime integration occurs.
    Only an observed direct same-process normal return provides this candidate.
    Any escaping failure yields no result; resource/Recursion/Base exceptions
    propagate unchanged. No delivery, health or exactly-once assurance.
    """
    if type(payload) is not bytes:
        raise CursorDocumentError("payload_type")
    try:
        value = json.loads(payload.decode("utf-8", "strict"),
                           object_pairs_hook=_pairs, parse_constant=_nonfinite)
    except (UnicodeError, ValueError):
        raise CursorDocumentError("json") from None
    # Validate the whole document before its shape, including unknown fields.
    _check_json(value)
    if type(value) is not dict or set(value) != {"schema", "offset", "prefix_sha256"}:
        raise CursorDocumentError("cursor")
    schema, offset, fingerprint = value["schema"], value["offset"], value["prefix_sha256"]
    if (type(schema) is not str or schema != "pod_alert_cursor/1"
            or type(offset) is not int or offset < 0
            or type(fingerprint) is not str or len(fingerprint) != 64
            or any(char not in "0123456789abcdef" for char in fingerprint)):
        raise CursorDocumentError("cursor")
    return {"schema": schema, "offset": offset, "prefix_sha256": fingerprint}
