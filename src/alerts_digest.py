"""#244-A: pure, strict byte-snapshot alert consumption; no CLI or runtime I/O.

The caller supplies a snapshot and owns cursor storage. A returned cursor is
only a proposal. No alert delivery, runtime health, or exactly-once claim is
made. The later file/stdout CLI and its exit protocol are deliberately absent.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import math


_CATEGORIES = frozenset({"runner_paused_too_long", "reconnect_storm", "tick_state_write_failed", "scan_too_old"})
_SEVERITIES = frozenset({"sev1", "warn", "info"})
_FIELDS = ("ts", "from", "alert", "severity", "proc", "detail", "exchange_action")
_CURSOR_KEYS = frozenset({"schema", "offset", "prefix_sha256"})


class DigestInputError(ValueError):
    """Invalid snapshot/cursor; contains a fixed reason, never raw log data."""


def _pairs(pairs):
    record = {}
    for key, value in pairs:
        if key in record:
            raise DigestInputError("json")
        record[key] = value
    return record


def _nonfinite(_):
    raise DigestInputError("json")


def _check_json(value):
    if type(value) is str:
        try:
            value.encode("utf-8", "strict")
        except UnicodeError:
            raise DigestInputError("json") from None
    elif type(value) is float:
        if not math.isfinite(value):
            raise DigestInputError("json")
    elif type(value) is dict:
        for key, child in value.items():
            _check_json(key)
            _check_json(child)
    elif type(value) is list:
        for child in value:
            _check_json(child)


def _decode_json(raw):
    try:
        value = json.loads(raw.decode("utf-8", "strict"), object_pairs_hook=_pairs,
                           parse_constant=_nonfinite)
    except (UnicodeError, ValueError):
        raise DigestInputError("json") from None
    _check_json(value)
    return value


def _record(raw):
    value = _decode_json(raw)
    if type(value) is not dict or any(type(value.get(key)) is not str for key in _FIELDS):
        raise DigestInputError("record")
    if any(not value[key].strip() for key in ("from", "alert", "severity")) or value["exchange_action"] != "none":
        raise DigestInputError("record")
    try:
        stamp = datetime.fromisoformat(value["ts"])
    except ValueError:
        raise DigestInputError("record") from None
    if stamp.utcoffset() is None:
        raise DigestInputError("record")
    return value


def _content_cursor(payload, cursor):
    if cursor is None:
        return {"schema": "pod_alert_cursor/1", "offset": 0,
                "prefix_sha256": hashlib.sha256(b"").hexdigest()}
    if type(cursor) is not dict or any(type(key) is not str for key in cursor) or set(cursor) != _CURSOR_KEYS:
        raise DigestInputError("cursor")
    schema, offset, fingerprint = cursor["schema"], cursor["offset"], cursor["prefix_sha256"]
    if type(schema) is not str or type(offset) is not int or type(fingerprint) is not str:
        raise DigestInputError("cursor")
    if schema != "pod_alert_cursor/1" or not 0 <= offset <= len(payload):
        raise DigestInputError("cursor")
    if len(fingerprint) != 64 or any(char not in "0123456789abcdef" for char in fingerprint):
        raise DigestInputError("cursor")
    if offset and payload[offset - 1] != 10:
        raise DigestInputError("cursor")
    if hashlib.sha256(payload[:offset]).hexdigest() != fingerprint:
        raise DigestInputError("cursor")
    return {"schema": schema, "offset": offset, "prefix_sha256": fingerprint}


def digest_snapshot(payload: bytes, *, cursor: dict[str, object] | None = None) -> dict[str, object]:
    """Strictly validate one byte snapshot and summarize rows after a content cursor.

    Accepts exact bytes and an exact plain dict cursor (or None). Validates
    every row, including the consumed prefix. The cursor is an LF-boundary
    byte offset plus SHA256 of those exact prefix bytes. Invalid input raises
    DigestInputError, with no partial digest or next cursor. Resource and
    BaseException interruptions propagate unchanged. No file I/O, clock,
    network, stdout, project/runtime imports, or caller mutation occurs.
    A returned next_cursor is a proposal: it proves neither saved state nor
    delivery, exactly-once consumption, runtime health, or alert resolution.
    """
    if type(payload) is not bytes:
        raise DigestInputError("payload_type")
    used = _content_cursor(payload, cursor)
    if payload and not payload.endswith(b"\n"):
        raise DigestInputError("json")
    groups = {}
    offset = rows_total = new_rows = 0
    for raw in payload.split(b"\n")[:-1]:
        row = _record(raw)
        rows_total += 1
        if offset >= used["offset"]:
            alert = row["alert"]
            category = alert if alert in _CATEGORIES else ("settlement" if alert.startswith("settlement_") else "other")
            severity = row["severity"] if row["severity"] in _SEVERITIES else "other"
            key = (category, severity)
            groups[key] = groups.get(key, 0) + 1
            new_rows += 1
        offset += len(raw) + 1
    fingerprint = hashlib.sha256(payload).hexdigest()
    return {
        "schema": "pod_alert_digest/1",
        "status": "ALERTS_PRESENT" if new_rows else "NO_NEW_ALERTS",
        "input_bytes": len(payload), "input_sha256": fingerprint,
        "rows_total": rows_total, "new_rows": new_rows,
        "groups": [{"category": category, "severity": severity, "count": count}
                   for (category, severity), count in sorted(groups.items())],
        "cursor_used": used,
        "next_cursor": {"schema": "pod_alert_cursor/1", "offset": len(payload), "prefix_sha256": fingerprint},
        "delivery": "NOT_DELIVERED", "runtime_health": "NOT_EVALUATED",
    }
