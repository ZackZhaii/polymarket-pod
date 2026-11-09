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

EL summarize_recording_manifest accepts exactly one {source, format="json"}
descriptor. It reuses the same raw read and strict parsed document as inventory.
It reports only m1_run/1 declarations and a declared UTC window, never actual
recording success, healthy coverage, scoring, or M2 acceptance. The imported
book timestamp parser is a pure utility; no clock-dependent staleness check runs.

EM summarize_heartbeat_snapshot reuses the original heartbeat protocol function
through fixed-source code loading, without executing heartbeat/podlib module
initializers. Code-source IO is distinct from the one explicit input capture;
no healthy/online assertion or historical sampling follows from a declaration.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import stat

from src.book_staleness import parse_book_ts


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
        return None, ["invalid_utf8"], None

    if fmt == "json":
        try:
            document = _strict_json(text)
        except _BadJSON as exc:
            return None, [str(exc)], None
        except (ValueError, RecursionError):
            return None, ["invalid_json"], None
        return 1, [], document

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
    return (None, problems, None) if problems else (len(lines), [], None)


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


def _capture_one(spec, index, paths, identities, formats=("json", "jsonl")):
    """Return original evidence, trusted document and the same captured bytes."""
    raw = None
    entry = {"source": None, "format": None, "sha256": None, "bytes": None,
             "records": None, "problems": [], "duplicate_of": None,
             "completeness": "INVENTORIED"}
    if not isinstance(spec, dict) or set(spec) != {"source", "format"}:
        _problem(entry, "invalid_input_spec")
        return entry, None, raw
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
        return entry, None, raw
    if entry["format"] not in formats:
        _problem(entry, "invalid_format")
        return entry, None, raw
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
        return entry, None, raw
    path = Path(source)
    if _forbidden(path):
        _problem(entry, "forbidden_source")
        return entry, None, raw

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
            return entry, None, raw
        canonical_key = ("resolved", str(resolved))
        if canonical_key in paths and paths[canonical_key] != index:
            _duplicate(entry, paths[canonical_key])
        paths.setdefault(canonical_key, index)
        before_path = resolved.stat()
        if not stat.S_ISREG(before_path.st_mode):
            _problem(entry, "not_regular_file")
            return entry, None, raw
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
                    return entry, None, raw
                raw = handle.read()
                after = os.fstat(handle.fileno())
        finally:
            if fd is not None:
                os.close(fd)
    except FileNotFoundError:
        if raw is None:
            _problem(entry, "missing_source", "INCOMPLETE")
            return entry, None, raw
        _problem(entry, "metadata_unavailable", "INCOMPLETE")
    except (OSError, ValueError, RuntimeError):
        if raw is None:
            _problem(entry, "unreadable_source", "INCOMPLETE")
            return entry, None, raw
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
        return entry, None, raw
    records, problems, document = _parse(raw, entry["format"])
    for problem in problems:
        _problem(entry, problem)
    if not entry["problems"]:
        entry["records"] = records
    return entry, document if not entry["problems"] else None, raw

def _one(spec, index, paths, identities, formats=("json", "jsonl")):
    """Preserve the original two-result reader API while sharing one raw capture."""
    entry, document, _ = _capture_one(spec, index, paths, identities, formats=formats)
    return entry, document


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
        entry, _ = _one(spec, index, paths, identities)
        report["inputs"].append(entry)
        if _RANK[entry["completeness"]] > _RANK[report["completeness"]]:
            report["completeness"] = entry["completeness"]
    return report



def _declared_endpoint(value, field, problems):
    if value is None:
        problems.append("missing_" + field)
        return None
    if not isinstance(value, str):
        problems.append("invalid_" + field)
        return None
    try:
        stamp = parse_book_ts(value)
    except (ValueError, OverflowError, OSError):
        stamp = None
    if stamp is None:
        problems.append("invalid_" + field)
    return stamp


def summarize_recording_manifest(spec):
    """Summarize one explicit local m1_run/1 JSON declaration, never a run verdict.

    spec is exactly {"source": str-or-PathLike, "format": "json"}; no defaults,
    list of runs, directories, outputs or CLI. input preserves inventory's entry
    contract. completeness describes raw input integrity, separately from
    declaration_problems. A JSON document can be INVENTORIED while its schema or
    declaration is unusable. Wrong schema/root or incomplete raw evidence leaves
    all declaration/time facts null; it never impersonates a valid recording.

    declaration retains original schema/status/started_at/ended_at values only
    for the correct schema object. status (including completed) and any exit_code
    do not authenticate execution success. Endpoints must be timezone-aware
    strings; *_utc is canonical ISO UTC with Z, microseconds retained where set.
    declared_interval_us is an exact int only when both endpoints are valid and
    end >= start; zero is legal. Reversed endpoints retain individually valid UTC
    declarations but have a null interval and a problem. No other field, mtime or
    current time supplies a missing endpoint. Healthy coverage is never inferred.

    SHA, strict parse and all declaration facts share one captured byte string.
    The source is not reopened after inventory. Existing local-only/UNC/env and
    read-only boundaries apply; this is not an OS sandbox/provenance guarantee.
    status is always NOT_EVALUATED; metrics/denominator/healthy_online_us are None.
    """
    entry, document = _one(spec, 0, {}, {}, formats=("json",))
    report = {"status": "NOT_EVALUATED", "completeness": entry["completeness"],
              "input": entry,
              "declaration": {"schema": None, "status": None,
                              "started_at": None, "ended_at": None},
              "declaration_problems": [], "started_at_utc": None,
              "ended_at_utc": None, "declared_interval_us": None,
              "metrics": None, "denominator": None, "healthy_online_us": None}
    problems = report["declaration_problems"]
    if entry["completeness"] != "INVENTORIED":
        problems.append("input_unavailable")
        return report
    if not isinstance(document, dict):
        problems.append("manifest_not_object")
        return report
    if document.get("schema") != "m1_run/1":
        problems.append("missing_schema" if document.get("schema") is None else "invalid_schema")
        return report
    declaration = {field: document.get(field) for field in report["declaration"]}
    report["declaration"] = declaration
    status = declaration["status"]
    if status is None:
        problems.append("missing_status")
    elif not isinstance(status, str) or not status.strip():
        problems.append("invalid_status")
    start = _declared_endpoint(declaration["started_at"], "started_at", problems)
    end = _declared_endpoint(declaration["ended_at"], "ended_at", problems)
    if start is not None:
        report["started_at_utc"] = start.isoformat().replace("+00:00", "Z")
    if end is not None:
        report["ended_at_utc"] = end.isoformat().replace("+00:00", "Z")
    if start is not None and end is not None:
        if end < start:
            problems.append("reversed_interval")
        else:
            delta = end - start
            report["declared_interval_us"] = (delta.days * 86400000000
                                              + delta.seconds * 1000000 + delta.microseconds)
    return report



def _heartbeat_protocol_validator():
    """Load the original pure protocol closure, not heartbeat's runtime module.

    This reads fixed sibling heartbeat.py SOURCE via Python's source loader.
    That code-loading IO is explicit; no zero-total-IO claim is made for the API.
    Executing the full heartbeat module would resolve runtime paths and initialize
    podlib's network opener/SSL context. Only the unchanged original function ASTs
    and their seven pure constants are compiled here, in source order. Caller
    data/path never supplies code. No sys.modules stubs or validator substitutes.
    """
    import __future__
    import ast
    from datetime import datetime
    from importlib.machinery import SourceFileLoader

    code_path = os.path.join(os.path.dirname(__file__), "heartbeat.py")
    source = SourceFileLoader("src.heartbeat", code_path).get_source("src.heartbeat")
    if source is None:
        raise ImportError("heartbeat protocol source unavailable")
    functions = {"parse_stamp", "_int", "validate_record"}
    constants = {"STATE_STARTING", "STATE_RUNNING", "STATE_PAUSED", "STATE_FINISHED",
                 "STATE_CRASHED", "STATES", "FIELDS"}
    selected, names = [], []
    for node in ast.parse(source, filename=code_path).body:
        if isinstance(node, ast.FunctionDef) and node.name in functions:
            if node.decorator_list:
                raise ValueError("unexpected protocol decorator")
            selected.append(node)
            names.append(node.name)
        elif (isinstance(node, ast.Assign) and len(node.targets) == 1
              and isinstance(node.targets[0], ast.Name) and node.targets[0].id in constants):
            selected.append(node)
            names.append(node.targets[0].id)
    if len(names) != 10 or set(names) != functions | constants:
        raise ValueError("heartbeat protocol closure changed")
    namespace = {"__name__": "src.heartbeat_protocol", "datetime": datetime}
    module = ast.Module(body=selected, type_ignores=[])
    exec(compile(module, code_path, "exec", flags=__future__.annotations.compiler_flag), namespace)
    return namespace["validate_record"]


def _heartbeat_snapshot_capture(spec, *, expected_proc):
    """Capture once and build the original protocol report, without clocks or outputs."""
    raw = None
    invalid_proc = not isinstance(expected_proc, str) or not expected_proc
    if invalid_proc:
        entry = {"source": None, "format": None, "sha256": None, "bytes": None,
                 "records": None, "problems": ["invalid_expected_proc"],
                 "duplicate_of": None, "completeness": "INVALID"}
        if isinstance(spec, dict):
            for field in ("source", "format"):
                if isinstance(spec.get(field), str):
                    entry[field] = spec[field]
        document = None
    else:
        entry, document, raw = _capture_one(spec, 0, {}, {}, formats=("json",))
    report = {"status": "NOT_EVALUATED", "completeness": entry["completeness"],
              "input": entry, "declaration": None, "declaration_problems": [],
              "metrics": None, "denominator": None, "healthy_online_us": None}
    if invalid_proc:
        report["declaration_problems"].append("invalid_expected_proc")
        return report, raw
    if entry["completeness"] != "INVENTORIED":
        report["declaration_problems"].append("input_unavailable")
        return report, raw
    try:
        validator = _heartbeat_protocol_validator()
    except (OSError, ImportError, SyntaxError, UnicodeError, ValueError, TypeError, NameError) as exc:
        report["declaration_problems"].append("validator_unavailable:" + type(exc).__name__)
        return report, raw
    try:
        report["declaration"] = validator(document, expected_proc)
    except (ValueError, TypeError, OverflowError) as exc:
        report["declaration_problems"].append("invalid_heartbeat:" + str(exc))
    return report, raw


def summarize_heartbeat_snapshot(spec, *, expected_proc):
    """Return protocol declarations from one explicit local heartbeat JSON file.

    spec is exactly {"source": str-or-PathLike, "format": "json"}. expected_proc
    is a required keyword, explicitly a nonempty string (no proc whitelist or
    identity authentication). Invalid expected_proc rejects before even coercing
    a path or loading protocol source; only literal source/format strings can be
    retained in that unread input diagnostic. Other descriptors use _one's entry.

    input/completeness describe raw integrity separately from declaration and
    declaration_problems. SHA, strict parse and declaration share one captured
    input byte string, never a reopen or reserialized hash. Complete protocol-
    valid objects retain ALL original values/extras, including legal nullable,
    UNKNOWN, paused, finished and exit_code values. Protocol validation proves no
    host/process identity, freshness, health or execution success. Bad/incomplete
    raw, malformed protocol or unavailable validator yields declaration=None.

    The original validate_record pure closure is loaded from fixed sibling source
    without importing heartbeat/podlib runtime bodies; this distinct code-source
    IO may occur at call time. No marker/risk/account read, runtime class/read_beat,
    git/network/subprocess/sleep, output/lock/heartbeat write or directory scan.
    Module import retains its existing no-application-IO contract. No raw archive,
    sampling/history or default paths are created. NOT_EVALUATED is unconditional;
    metrics/denominator/healthy_online_us remain None. Existing APIs are unchanged.
    """
    report, _ = _heartbeat_snapshot_capture(spec, expected_proc=expected_proc)
    return report


def _capture_stamp():
    """Return a local UTC attempt endpoint, never heartbeat health or duration."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _invalid_capture_id_report(spec):
    """Reject an invalid label without coercing or accessing the input."""
    entry = {"source": None, "format": None, "sha256": None, "bytes": None,
             "records": None, "problems": ["invalid_capture_id"], "duplicate_of": None,
             "completeness": "INVALID"}
    if isinstance(spec, dict):
        for field in ("source", "format"):
            if isinstance(spec.get(field), str):
                entry[field] = spec[field]
    return {"status": "NOT_EVALUATED", "completeness": "INVALID", "input": entry,
            "declaration": None, "declaration_problems": ["invalid_capture_id"],
            "metrics": None, "denominator": None, "healthy_online_us": None}


def capture_heartbeat_once(spec, *, expected_proc, capture_id):
    """Return one same-read heartbeat raw capture in memory; never write an archive.

    spec and expected_proc follow summarize_heartbeat_snapshot. capture_id is an
    explicit nonempty string label, not an authenticated or unique file identity.
    Existing report fields are unchanged; capture adds only literal argument
    strings or None, two local UTC attempt endpoints, and exact raw base64.
    Missing raw is None, captured empty raw is "". Raw/SHA/strict parse/declaration
    share one read, and incomplete evidence cannot supply a trusted declaration.

    Invalid expected_proc takes precedence over invalid capture_id. Invalid
    required arguments reject before path coercion, protocol source IO or clocks;
    diagnostic metadata retains literal strings only. Valid whitespace strings
    are not tightened. Only this new API calls the clock twice around capture and
    protocol summary, even for rejected/unavailable sources. Endpoints can reverse;
    no duration, freshness, health or successful read is inferred. Clock errors
    propagate. Old APIs acquire no clocks and their original report stays intact.

    Original local-only/env/UNC, integrity and fixed protocol-code loading limits
    apply. NOT_EVALUATED/null metrics, denominator and healthy_online_us remain.
    No file output, archive, sidecar, lock, runtime imports, fallback, retry,
    network, process or directory scan is added.
    """
    started = finished = None
    if not isinstance(expected_proc, str) or not expected_proc:
        report, raw = _heartbeat_snapshot_capture(spec, expected_proc=expected_proc)
    elif not isinstance(capture_id, str) or not capture_id:
        report, raw = _invalid_capture_id_report(spec), None
    else:
        started = _capture_stamp()
        report, raw = _heartbeat_snapshot_capture(spec, expected_proc=expected_proc)
        finished = _capture_stamp()
    report["capture"] = {
        "capture_id": capture_id if isinstance(capture_id, str) else None,
        "expected_proc": expected_proc if isinstance(expected_proc, str) else None,
        "started_at_utc": started, "finished_at_utc": finished,
        "raw_base64": None if raw is None else base64.b64encode(raw).decode("ascii"),
    }
    return report
