"""EW C: one new synthetic evidence file, no shared archive or runtime.

Callers must already own and be authorized for a stable local test root and its
ancestors. Passing a root does not grant permission. No directory creation,
source-path filesystem access, append, lock, sidecar, retry or repair occurs.
Python loading of the existing pure JSON parser is code IO, not runtime startup.
"""
import base64
from datetime import datetime, timedelta
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat

from src.m2_report import _strict_json


_REPORT = {"status", "completeness", "input", "declaration", "declaration_problems",
           "metrics", "denominator", "healthy_online_us", "capture"}
_INPUT = {"source", "format", "sha256", "bytes", "records", "problems", "duplicate_of", "completeness"}
_CAPTURE = {"capture_id", "expected_proc", "started_at_utc", "finished_at_utc", "raw_base64"}
_NULLS = ("metrics", "denominator", "healthy_online_us")


def _plain(value):
    """Reject coercible/custom objects and nonfinite values before any output IO."""
    kind = type(value)
    if kind in (type(None), bool, int, str):
        return
    if kind is float and math.isfinite(value):
        return
    if kind is list:
        for item in value:
            _plain(item)
        return
    if kind is dict and all(type(key) is str for key in value):
        for item in value.values():
            _plain(item)
        return
    raise ValueError("not_plain_finite_JSON")


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _lexical_path(value):
    """Check ordinary host-absolute ASCII spelling without resolving the source."""
    if type(value) is not str or not value or not value.isascii() or "\x00" in value:
        raise ValueError("invalid_path")
    spelling = value.replace("\\", "/")
    if spelling.startswith("//") or "://" in spelling or (os.name != "nt" and "\\" in value):
        raise ValueError("nonordinary_path")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("relative_path")
    drive = bool(re.match(r"^[A-Za-z]:/", spelling))
    parts = [part for part in spelling.split("/") if part]
    for index, part in enumerate(parts):
        if index == 0 and drive:
            continue
        folded = part.casefold()
        stem = part.split(".")[0].upper()
        if (part in (".", "..") or folded == ".env" or folded.startswith(".env.")
                or part.endswith((" ", ".")) or any(c in part for c in '<>:"|?*')
                or stem in {"CON", "PRN", "AUX", "NUL", "CLOCK$", "CONIN$", "CONOUT$"}
                or re.fullmatch(r"(COM|LPT)[1-9]", stem)):
            raise ValueError("forbidden_path_component")
    return path


def _same_spelling(left, right):
    return os.path.normcase(os.path.normpath(str(left))) == os.path.normcase(os.path.normpath(str(right)))


def _snapshot(report):
    """Freeze the report, validate same-raw binding; authenticate no producer."""
    _plain(report)
    # UTF-8 encoding also rejects unencodable surrogate strings before root IO.
    frozen = _strict_json(_canonical(report).encode("utf-8").decode("utf-8"))
    if type(frozen) is not dict or set(frozen) != _REPORT:
        raise ValueError("report_shape")
    entry, cap = frozen["input"], frozen["capture"]
    if type(entry) is not dict or set(entry) != _INPUT or type(cap) is not dict or set(cap) != _CAPTURE:
        raise ValueError("capture_shape")
    if (frozen["status"] != "NOT_EVALUATED" or any(frozen[k] is not None for k in _NULLS)
            or frozen["completeness"] not in ("INVENTORIED", "INCOMPLETE", "INVALID")
            or frozen["completeness"] != entry["completeness"] or entry["format"] != "json"
            or entry["duplicate_of"] is not None):
        raise ValueError("report_contract")
    if entry["records"] is not None and not (type(entry["records"]) is int and entry["records"] == 1):
        raise ValueError("records")
    for problems in (entry["problems"], frozen["declaration_problems"]):
        if type(problems) is not list or any(type(item) is not str for item in problems):
            raise ValueError("problems")
    for key in ("capture_id", "expected_proc"):
        if type(cap[key]) is not str or not cap[key]:
            raise ValueError("capture_label")
    for key in ("started_at_utc", "finished_at_utc"):
        value = cap[key]
        if type(value) is not str or not value.endswith("Z"):
            raise ValueError("attempt_time")
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
        if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
            raise ValueError("attempt_time")
    encoded = cap["raw_base64"]
    if type(encoded) is not str or not encoded.isascii():
        raise ValueError("missing_raw")
    raw = base64.b64decode(encoded, validate=True)
    if base64.b64encode(raw).decode("ascii") != encoded:
        raise ValueError("noncanonical_base64")
    if (type(entry["sha256"]) is not str or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
            or hashlib.sha256(raw).hexdigest() != entry["sha256"]
            or type(entry["bytes"]) is not int or entry["bytes"] < 0 or entry["bytes"] != len(raw)):
        raise ValueError("raw_binding")
    source = _lexical_path(entry["source"])
    declaration = frozen["declaration"]
    if declaration is not None:
        if (type(declaration) is not dict or frozen["completeness"] != "INVENTORIED"
                or entry["records"] != 1 or entry["problems"] or frozen["declaration_problems"]):
            raise ValueError("unavailable_declaration")
        document = _strict_json(raw.decode("utf-8", errors="strict"))
        if (type(document) is not dict or _canonical(document) != _canonical(declaration)
                or declaration.get("proc") != cap["expected_proc"]):
            raise ValueError("declaration_binding")
    envelope = {"schema": "m2_heartbeat_archive/1", "status": "NOT_EVALUATED",
                "declaration_status": "UNVERIFIED", "capture_report": frozen,
                "metrics": None, "denominator": None, "healthy_online_us": None}
    return source, (_canonical(envelope) + "\n").encode("utf-8")


def _open_file(path, mode):
    """Open only the selected new target, in exclusive-write or readback mode."""
    return open(path, mode)


def archive_capture_once(report, *, owned_root, file_name):
    """Archive one already-captured report under an externally authorized test root.

    Both root and filename must be plain strings. The root must already exist,
    be stable, local and trusted (including its ancestors); this is not an OS
    sandbox or a physical-nonalias/TOCTOU guarantee. Only host-absolute ASCII
    root/source spellings and hb-[a-z0-9][a-z0-9_-]*.jsonl names are accepted.
    No source stat/read/resolve, mkdir, shared append, runtime import or lock.
    Capture labels and raw evidence stay unchanged, including empty/malformed
    diagnostic raw. Missing raw is refused; declarations remain UNVERIFIED.

    Receipts always have status NOT_EVALUATED and null metrics/denominator/
    healthy_online_us. write_state is REFUSED only for definite pre-create
    rejection or FileExistsError from the xb call. Other create exceptions and
    every post-create ordinary exception yield UNKNOWN, with phase/error_type,
    cleanup_error_type and created=False/None/True. Possible bytes are retained.
    Each obtained handle receives at most one explicit close attempt; earlier
    errors preserve their primary stage if cleanup close also fails. BaseException
    propagates after the same one-close cleanup. No retry, replacement or repair.

    WRITTEN_READ_BACK requires full write/flush/fsync/write-close, readback/read-
    close and exact expected bytes/hash. It confirms this synchronous call/read,
    not power-loss or directory durability, uniqueness of sampling, producer
    provenance, identity, health, M2 acceptance or future immutability.
    """
    result = {"status": "NOT_EVALUATED", "metrics": None, "denominator": None,
              "healthy_online_us": None, "write_state": "REFUSED", "phase": "validate",
              "error_type": None, "cleanup_error_type": None, "created": False,
              "archive_path": None, "expected_bytes": None, "expected_sha256": None,
              "readback_bytes": None, "readback_sha256": None}
    try:
        source, line = _snapshot(report)
        root = _lexical_path(owned_root)
        if type(file_name) is not str or not re.fullmatch(r"hb-[a-z0-9][a-z0-9_-]*\.jsonl", file_name):
            raise ValueError("file_name")
        result["phase"] = "root"
        info = root.lstat()
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
            raise ValueError("untrusted_root")
        resolved = root.resolve(strict=True)
        _lexical_path(str(resolved))
        if not _same_spelling(root, resolved):
            raise ValueError("root_alias")
        target = resolved / file_name
        if _same_spelling(source, target):
            raise ValueError("source_target_alias")
        result.update(archive_path=str(target), expected_bytes=len(line),
                      expected_sha256=hashlib.sha256(line).hexdigest())
    except Exception as exc:
        result["error_type"] = type(exc).__name__
        return result

    writer = reader = None
    writer_close = reader_close = False
    try:
        result.update(phase="create", created=None)
        try:
            writer = _open_file(target, "xb")
        except FileExistsError as exc:
            result.update(write_state="REFUSED", created=False, error_type=type(exc).__name__)
            return result
        result["created"] = True
        result["phase"] = "write"
        written = writer.write(line)
        if type(written) is not int or written != len(line):
            raise OSError("short_write")
        result["phase"] = "flush"
        writer.flush()
        result["phase"] = "fsync"
        os.fsync(writer.fileno())
        result["phase"] = "close"
        writer_close = True
        writer.close()
        result["phase"] = "readback_open"
        reader = _open_file(target, "rb")
        result["phase"] = "readback_read"
        actual = reader.read()
        result["phase"] = "readback_close"
        reader_close = True
        reader.close()
        result["phase"] = "readback_compare"
        if actual != line:
            raise OSError("readback_mismatch")
        result.update(write_state="WRITTEN_READ_BACK", phase="complete",
                      readback_bytes=len(actual), readback_sha256=hashlib.sha256(actual).hexdigest())
    except Exception as exc:
        result.update(write_state="UNKNOWN", error_type=type(exc).__name__)
    finally:
        for handle, attempted in ((writer, writer_close), (reader, reader_close)):
            if handle is not None and not attempted:
                try:
                    handle.close()
                except Exception as exc:
                    if result["cleanup_error_type"] is None:
                        result["cleanup_error_type"] = type(exc).__name__
    return result
