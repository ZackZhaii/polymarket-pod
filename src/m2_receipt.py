"""Persist one already-returned, unverified observation in a new owned file.

No capture, old archive, source, clock, network, or runtime API is called.
The trusted existing root and its parents must remain stable: these checks
are not an OS sandbox or proof against physical aliases/concurrent changes.
"""

import hashlib
import json
import math
import os
from pathlib import Path
import posixpath
import re
import stat


_OBSERVATION = {
    "schema", "status", "declaration_status", "metrics", "denominator",
    "healthy_online_us", "planned_slot", "binding", "attempt",
    "archive_receipt", "phase", "error_type", "not_archived_reason",
}
_NULLS = ("metrics", "denominator", "healthy_online_us")
_PHASES = {"CAPTURE_ERROR", "CAPTURE_NO_REPORT", "ARCHIVE_ERROR", "ARCHIVE_RETURNED"}


def _snapshot(value, ancestors):
    """Copy exact JSON types; shared references are allowed, cycles are not."""
    kind = type(value)
    if value is None or kind in (bool, int):
        return value
    if kind is str:
        value.encode("utf-8")
        return value
    if kind is float and math.isfinite(value):
        return value
    if kind not in (dict, list):
        raise ValueError("not_plain_finite_JSON")
    identity = id(value)
    if identity in ancestors:
        raise ValueError("cyclic_JSON")
    ancestors.add(identity)
    try:
        if kind is list:
            return [_snapshot(item, ancestors) for item in value]
        result = {}
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("nonplain_key")
            key.encode("utf-8")
            result[key] = _snapshot(item, ancestors)
        return result
    finally:
        ancestors.remove(identity)


def _observation_snapshot(value):
    frozen = _snapshot(value, set())
    if type(frozen) is not dict or set(frozen) != _OBSERVATION:
        raise ValueError("observation_shape")
    if (frozen["schema"] != "m2_observe_slot/1"
            or frozen["status"] != "NOT_EVALUATED"
            or frozen["declaration_status"] != "UNVERIFIED"
            or any(frozen[key] is not None for key in _NULLS)):
        raise ValueError("observation_contract")
    for key, fields in (
        ("planned_slot", {"slot_id", "planned_at_utc", "expected_proc"}),
        ("binding", {"scheme", "material_base64", "sha256", "capture_id"}),
        ("attempt", {"slot_id", "attempt_id", "report"}),
    ):
        part = frozen[key]
        if type(part) is not dict or set(part) != fields:
            raise ValueError("observation_part_shape")
        labels = fields - {"report"}
        if any(type(part[label]) is not str or not part[label] for label in labels):
            raise ValueError("observation_label")
    if frozen["phase"] not in _PHASES:
        raise ValueError("observation_phase")
    if any(frozen[key] is not None and type(frozen[key]) is not str
           for key in ("error_type", "not_archived_reason")):
        raise ValueError("observation_error_label")
    return frozen


def _lexical_root(value):
    if type(value) is not str or not value or not value.isascii() or any(ord(char) < 32 for char in value):
        raise ValueError("root_spelling")
    spelling = value.replace("\\", "/")
    if spelling.startswith("//") or "://" in spelling or (os.name != "nt" and "\\" in value):
        raise ValueError("nonordinary_root")
    root = Path(value)
    if not root.is_absolute():
        raise ValueError("relative_root")
    drive = bool(re.match(r"^[A-Za-z]:/", spelling))
    for index, component in enumerate(part for part in spelling.split("/") if part):
        if index == 0 and drive:
            continue
        folded = component.casefold()
        stem = component.split(".")[0].upper()
        if (component in (".", "..") or folded == ".env" or folded.startswith(".env.")
                or component.endswith((" ", ".")) or any(c in component for c in '<>:"|?*')
                or stem in {"CON", "PRN", "AUX", "NUL", "CLOCK$", "CONIN$", "CONOUT$"}
                or re.fullmatch(r"(COM|LPT)[1-9]", stem)):
            raise ValueError("forbidden_root_component")
    return root


def _old_conflict(target, old_label):
    if old_label is None:
        return False
    left = posixpath.normpath(str(target).replace("\\", "/")).casefold()
    right = posixpath.normpath(old_label.replace("\\", "/")).casefold()
    return left == right or posixpath.basename(left) == posixpath.basename(right)


def _trusted_root(root):
    info = root.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise ValueError("reparse_root")
    if not stat.S_ISDIR(info.st_mode):
        raise NotADirectoryError("root_not_directory")
    resolved = root.resolve(strict=True)
    if os.path.normcase(os.path.normpath(str(root))) != os.path.normcase(os.path.normpath(str(resolved))):
        raise ValueError("root_alias_spelling")
    return resolved


def persist_observation_receipt(observation, *, owned_root, file_name):
    """Freeze exact plain EY13 JSON, then create one independent receipt file.

    File7 schema m2_observation_receipt/1 preserves every observation value;
    return18 schema m2_receipt_write/1 describes only this new write point.
    No old binding/raw/producer/phase is authenticated or promoted. A returned
    WRITTEN_READ_BACK proves only selected canonical UTF-8+LF bytes read back
    synchronously, not old archive/execution/health/M2 or power-loss durability.

    The caller must own an existing stable trusted local root and choose a
    unique receipt-[a-z0-9][a-z0-9_-]*.json name. Actual published archive_path
    is only a lexical label: same basename/address refuses without old FS IO.
    Unknown old paths remain unknown; physical aliases/TOCTOU are not proven.
    Only new root metadata and xb/write/flush/fsync/close/rb/read/close occur.
    Definite precreate/EEXIST refuses; other create/postcreate errors UNKNOWN,
    residues retained, primary/cleanup ordinary errors separate. Each acquired
    handle has at most one explicit close attempt. Resource/recursion/Base
    exceptions propagate after cleanup; cleanup Base may supersede primary
    Base. EY not returning, interruption, or this writer failing may leave no
    receipt. No mkdir/delete/truncate/append/repair/rename/retry/newname/lock.
    """
    result = {
        "schema": "m2_receipt_write/1", "status": "NOT_EVALUATED",
        "declaration_status": "UNVERIFIED", "metrics": None,
        "denominator": None, "healthy_online_us": None,
        "write_state": "REFUSED", "phase": "validate", "error_type": None,
        "cleanup_error_type": None, "created": False, "receipt_path": None,
        "expected_bytes": None, "expected_sha256": None,
        "readback_bytes": None, "readback_sha256": None,
        "old_archive_label": None, "old_archive_label_status": "UNKNOWN",
    }
    try:
        frozen = _observation_snapshot(observation)
        previous = frozen["archive_receipt"]
        if type(previous) is dict and type(previous.get("archive_path")) is str and previous["archive_path"]:
            result["old_archive_label"] = previous["archive_path"]
            result["old_archive_label_status"] = "PUBLISHED_LEXICAL"
        envelope = {"schema": "m2_observation_receipt/1", "status": "NOT_EVALUATED",
                    "declaration_status": "UNVERIFIED", "observation": frozen,
                    "metrics": None, "denominator": None, "healthy_online_us": None}
        payload = (json.dumps(envelope, sort_keys=True, ensure_ascii=False,
                              allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")
        result["expected_bytes"] = len(payload)
        result["expected_sha256"] = hashlib.sha256(payload).hexdigest()
        root = _lexical_root(owned_root)
        if type(file_name) is not str or not re.fullmatch(r"receipt-[a-z0-9][a-z0-9_-]*[.]json", file_name):
            raise ValueError("receipt_name")
        target = root / file_name
        result["receipt_path"] = str(target)
        if _old_conflict(target, result["old_archive_label"]):
            raise ValueError("old_target_lexical_conflict")
    except (MemoryError, RecursionError):
        raise
    except Exception as error:
        result["error_type"] = type(error).__name__
        return result

    result["phase"] = "root"
    try:
        resolved = _trusted_root(root)
        target = resolved / file_name
        if _old_conflict(target, result["old_archive_label"]):
            raise ValueError("old_target_lexical_conflict")
        result["receipt_path"] = str(target)
    except (MemoryError, RecursionError):
        raise
    except (ValueError, FileNotFoundError, NotADirectoryError) as error:
        result["error_type"] = type(error).__name__
        return result
    except Exception as error:
        result.update(write_state="UNKNOWN", error_type=type(error).__name__)
        return result

    writer = reader = None
    writer_close_attempted = reader_close_attempted = False
    result["write_state"] = "UNKNOWN"
    try:
        result.update(phase="create", created=None)
        try:
            writer = open(target, "xb")
        except FileExistsError as error:
            result.update(write_state="REFUSED", created=False, error_type=type(error).__name__)
            return result
        result["created"] = True
        result["phase"] = "write"
        count = writer.write(payload)
        if type(count) is not int or count != len(payload):
            raise OSError("short_write")
        result["phase"] = "flush"
        writer.flush()
        result["phase"] = "fsync"
        os.fsync(writer.fileno())
        result["phase"] = "close"
        writer_close_attempted = True
        writer.close()
        result["phase"] = "readback_open"
        reader = open(target, "rb")
        result["phase"] = "readback"
        readback = reader.read()
        if type(readback) is not bytes:
            raise TypeError("nonbytes_readback")
        result["readback_bytes"] = len(readback)
        result["readback_sha256"] = hashlib.sha256(readback).hexdigest()
        result["phase"] = "readback_close"
        reader_close_attempted = True
        reader.close()
        result["phase"] = "compare"
        if readback != payload:
            raise ValueError("readback_mismatch")
        result.update(write_state="WRITTEN_READ_BACK", phase="done")
    except BaseException as error:
        result["error_type"] = type(error).__name__
        if not isinstance(error, Exception) or isinstance(error, (MemoryError, RecursionError)):
            raise
    finally:
        pending = ((reader, reader_close_attempted), (writer, writer_close_attempted))
        reader_close_attempted = writer_close_attempted = True
        for handle, attempted in pending:
            if handle is not None and not attempted:
                try:
                    handle.close()
                except (MemoryError, RecursionError):
                    raise
                except Exception as cleanup:
                    result["cleanup_error_type"] = type(cleanup).__name__
    return result
