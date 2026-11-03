"""EK: read-only evidence inventory, not an M2 evaluation or run launcher.

inventory_inputs accepts a list/tuple of {"source": str-or-PathLike,
"format": "json"|"jsonl"}. There are no implicit/default input paths.
Each source must be an explicitly authorized local evidence file, never a real
account/credential file. Environment-file paths are rejected, including resolved
aliases. Explicit Windows UNC/network namespace paths are rejected before any
filesystem access. This is not an OS sandbox or proof that mapped drives or local
reparse targets are offline. This function does not authenticate account, host or
evidence provenance.

JSON records counts whole documents (any JSON root), not business samples.
JSONL records counts complete object lines, with LF or CRLF termination. A bad,
empty, missing, duplicate or otherwise incomplete source has records=None, never
a partial denominator. Readable malformed input retains its raw SHA and size.
Distinct files with identical content remain distinct; path/physical aliases do
not. Unavailable physical identity is incomplete evidence, not independence.

status is always NOT_EVALUATED; completeness only describes supplied inputs.
metrics and denominator stay None. No output files, scans, runtime imports,
network, subprocesses, ledger access or module-body filesystem operations.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import stat


_RANK = {"INVENTORIED": 0, "INCOMPLETE": 1, "INVALID": 2}


class _BadJSON(ValueError):
    pass


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _BadJSON("duplicate_key")
        result[key] = value
    return result


def _constant(_value):
    raise _BadJSON("non_finite")


def _strict_json(text):
    value = json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant)
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, float) and not math.isfinite(item):
            raise _BadJSON("non_finite")
        if isinstance(item, dict):
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return value


def _parse(raw, fmt):
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None, ["invalid_utf8"]

    if fmt == "json":
        try:
            _strict_json(text)
        except _BadJSON as exc:
            return None, [str(exc)]
        except (ValueError, RecursionError):
            return None, ["invalid_json"]
        return 1, []

    problems = []
    if not text.endswith("\n"):
        problems.append("missing_final_newline")
    # Only LF separates JSONL records. str.splitlines would split valid U+2028
    # characters inside JSON strings and manufacture extra records.
    lines = text.split("\n")
    if text.endswith("\n"):
        lines.pop()
    for number, line in enumerate(lines, 1):
        if line.endswith("\r"):
            line = line[:-1]
        reason = None
        if not line.strip():
            reason = "blank_line"
        else:
            try:
                value = _strict_json(line)
                if not isinstance(value, dict):
                    reason = "non_object"
            except _BadJSON as exc:
                reason = str(exc)
            except (ValueError, RecursionError):
                reason = "invalid_json"
        if reason:
            problems.append(f"line_{number}:{reason}")
    return (None, problems) if problems else (len(lines), [])


def _problem(entry, reason, completeness="INVALID"):
    if reason not in entry["problems"]:
        entry["problems"].append(reason)
    if _RANK[completeness] > _RANK[entry["completeness"]]:
        entry["completeness"] = completeness


def _forbidden(path):
    return any(part.casefold() == ".env" or part.casefold().startswith(".env.")
               for part in path.parts)


def _signature(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _duplicate(entry, index):
    if entry["duplicate_of"] is None:
        entry["duplicate_of"] = index
    _problem(entry, "duplicate_source")


def _one(spec, index, paths, identities):
    entry = {"source": None, "format": None, "sha256": None, "bytes": None,
             "records": None, "problems": [], "duplicate_of": None,
             "completeness": "INVENTORIED"}
    if not isinstance(spec, dict) or set(spec) != {"source", "format"}:
        _problem(entry, "invalid_input_spec")
        return entry
    if isinstance(spec["format"], str):
        entry["format"] = spec["format"]
    try:
        source = os.fspath(spec["source"])
    except (TypeError, ValueError, OSError):
        source = None
    if isinstance(source, str):
        entry["source"] = source
    if not isinstance(source, str) or not source or "\x00" in source:
        _problem(entry, "invalid_source")
        return entry
    if entry["format"] not in {"json", "jsonl"}:
        _problem(entry, "invalid_format")
        return entry
    # Classify Windows network namespaces lexically: resolving them can perform
    # OS file-sharing IO even when Python sockets are blocked. Only the explicit
    # extended local drive namespace is exempt; preserve original source spelling.
    windows_source = source.replace("\\", "/")
    extended_local = (windows_source.startswith("//?/")
                      and len(windows_source) >= 7
                      and windows_source[4] in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
                      and windows_source[5:7] == ":/")
    if windows_source.startswith("//") and not extended_local:
        _problem(entry, "nonlocal_source")
        return entry
    path = Path(source)
    if _forbidden(path):
        _problem(entry, "forbidden_source")
        return entry

    # The request key also makes repeated missing requests traceable. It is a
    # comparison of exact request spelling, never physical-identity proof. Do not
    # case-fold or collapse dot segments before resolving the actual filesystem.
    request_key = ("request", source)
    if request_key in paths:
        _duplicate(entry, paths[request_key])
    paths.setdefault(request_key, index)
    raw = before = after = None
    try:
        resolved = path.resolve(strict=True)
        if _forbidden(resolved):
            _problem(entry, "forbidden_source")
            return entry
        canonical_key = ("resolved", str(resolved))
        if canonical_key in paths and paths[canonical_key] != index:
            _duplicate(entry, paths[canonical_key])
        paths.setdefault(canonical_key, index)
        before_path = resolved.stat()
        if not stat.S_ISREG(before_path.st_mode):
            _problem(entry, "not_regular_file")
            return entry
        # Nonblocking where supported prevents a file->FIFO race from hanging a
        # reader; both pre-open and descriptor checks still require a regular file.
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(resolved, flags)
        try:
            with os.fdopen(fd, "rb") as handle:
                fd = None  # handle owns closure even if reading/fstat fails
                before = os.fstat(handle.fileno())
                if not stat.S_ISREG(before.st_mode):
                    _problem(entry, "not_regular_file")
                    return entry
                raw = handle.read()
                after = os.fstat(handle.fileno())
        finally:
            if fd is not None:
                os.close(fd)
    except FileNotFoundError:
        if raw is None:
            _problem(entry, "missing_source", "INCOMPLETE")
            return entry
        _problem(entry, "metadata_unavailable", "INCOMPLETE")
    except (OSError, ValueError, RuntimeError):
        if raw is None:
            _problem(entry, "unreadable_source", "INCOMPLETE")
            return entry
        _problem(entry, "metadata_unavailable", "INCOMPLETE")

    # Hash and parser consume this single captured byte string, never reopen it.
    entry["sha256"] = hashlib.sha256(raw).hexdigest()
    entry["bytes"] = len(raw)
    if before is None or after is None:
        _problem(entry, "metadata_unavailable", "INCOMPLETE")
    else:
        if (_signature(before_path) != _signature(before)
                or _signature(before) != _signature(after) or len(raw) != after.st_size):
            _problem(entry, "source_changed_during_read")
        try:
            if _signature(path.stat()) != _signature(after):
                _problem(entry, "source_changed_during_read")
        except OSError:
            _problem(entry, "source_changed_during_read")
    if before is not None and before.st_ino:
        identity = before.st_dev, before.st_ino
        if identity in identities:
            _duplicate(entry, identities[identity])
        identities.setdefault(identity, index)
    else:
        _problem(entry, "identity_unavailable", "INCOMPLETE")
    if not raw:
        _problem(entry, "empty_input", "INCOMPLETE")
        return entry
    records, problems = _parse(raw, entry["format"])
    for problem in problems:
        _problem(entry, problem)
    if not entry["problems"]:
        entry["records"] = records
    return entry


def inventory_inputs(inputs):
    """Return evidence for explicit sources without evaluating any M2 criterion.

    See the module contract for descriptor shape, counts, completeness, aliases
    and side-effect boundaries. Malformed descriptors return INVALID; missing
    files/empty inputs return INCOMPLETE. Parsing/alias failures return INVALID.
    No caller-provided completeness/status override is accepted.
    """
    report = {"status": "NOT_EVALUATED", "completeness": "INVENTORIED",
              "inputs": [], "problems": [], "metrics": None, "denominator": None}
    if not isinstance(inputs, (list, tuple)):
        report.update(completeness="INVALID", problems=["invalid_inputs"])
        return report
    if not inputs:
        report.update(completeness="INCOMPLETE", problems=["no_inputs"])
        return report
    paths, identities = {}, {}
    for index, spec in enumerate(inputs):
        entry = _one(spec, index, paths, identities)
        report["inputs"].append(entry)
        if _RANK[entry["completeness"]] > _RANK[report["completeness"]]:
            report["completeness"] = entry["completeness"]
    return report
