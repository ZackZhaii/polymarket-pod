"""EX: pure explicit-slot facts; no capture, archive, clock, IO or health decision.

Only supplied row associations and supplied raw-byte bindings are checked. Producer
identity, declaration validity, execution, files and observation health remain
UNVERIFIED/NOT_EVALUATED. This module imports only Python standard-library code.
"""
import base64
import binascii
from datetime import datetime, timedelta
import hashlib
import math
import re


_PLAN = {"slot_id", "planned_at_utc", "expected_proc"}
_ATTEMPT = {"slot_id", "attempt_id", "report"}
_REPORT = {"status", "completeness", "input", "declaration", "declaration_problems",
           "metrics", "denominator", "healthy_online_us", "capture"}
_INPUT = {"source", "format", "sha256", "bytes", "records", "problems",
          "duplicate_of", "completeness"}
_CAPTURE = {"capture_id", "expected_proc", "started_at_utc", "finished_at_utc", "raw_base64"}
_COMPLETENESS = ("INVENTORIED", "INCOMPLETE", "INVALID")
_SHA = re.compile(r"[0-9a-f]{64}")


def _copy_plain(value, active):
    """Copy exact finite UTF8 JSON types; active ancestry rejects cycles, not aliases."""
    kind = type(value)
    if kind is str:
        value.encode("utf-8", errors="strict")
        return value
    if kind in (type(None), bool, int):
        return value
    if kind is float:
        if not math.isfinite(value):
            raise ValueError("plain_finite_JSON_required")
        return value
    if kind not in (list, dict):
        raise ValueError("plain_finite_JSON_required")
    ident = id(value)
    if ident in active:
        raise ValueError("cyclic_JSON")
    active.add(ident)
    try:
        if kind is list:
            return [_copy_plain(item, active) for item in value]
        copied = {}
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("string_JSON_keys_required")
            key.encode("utf-8", errors="strict")
            copied[key] = _copy_plain(item, active)
        return copied
    finally:
        active.remove(ident)


def _label(value):
    return value if type(value) is str and value else None


def _field(row, key):
    return row.get(key) if type(row) is dict else None


def _indexes(rows, key):
    result = {}
    for index, row in enumerate(rows):
        value = _label(_field(row, key))
        if value is not None:
            result.setdefault(value, []).append(index)
    return result


def _utc(value):
    """Ledger UTC-Z ISO syntax only, never a clock or heartbeat protocol validator."""
    if type(value) is not str or not value.endswith("Z"):
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None and parsed.utcoffset() == timedelta(0) else None


def _report_problems(report):
    """Diagnose bounded report shape/basic invariants without authenticating declarations."""
    if type(report) is not dict:
        return ["report_shape"]
    problems = []
    if set(report) != _REPORT:
        problems.append("report_shape")
    entry, capture = report.get("input"), report.get("capture")
    if type(entry) is not dict or set(entry) != _INPUT:
        problems.append("input_shape")
    if type(capture) is not dict or set(capture) != _CAPTURE:
        problems.append("capture_shape")
    if (report.get("status") != "NOT_EVALUATED"
            or any(report.get(k) is not None for k in ("metrics", "denominator", "healthy_online_us"))
            or report.get("completeness") not in _COMPLETENESS
            or type(entry) is not dict
            or report.get("completeness") != entry.get("completeness")):
        problems.append("report_contract")
    if report.get("declaration") is not None and type(report["declaration"]) is not dict:
        problems.append("declaration_type")
    declared_problems = report.get("declaration_problems")
    if type(declared_problems) is not list or any(type(v) is not str for v in declared_problems):
        problems.append("declaration_problems_type")
    return problems


def _raw_state(report):
    """Bind supplied bytes only: missing/empty/invalid/unextractable remain distinct."""
    if type(report) is not dict:
        return "UNKNOWN"
    entry, capture = report.get("input"), report.get("capture")
    if type(entry) is not dict or type(capture) is not dict or "raw_base64" not in capture:
        return "UNKNOWN"
    encoded = capture["raw_base64"]
    if encoded is None:
        return "MISSING"
    if type(encoded) is not str or not encoded.isascii():
        return "INVALID"
    try:
        raw = base64.b64decode(encoded, validate=True)
    except binascii.Error:
        return "INVALID"
    if base64.b64encode(raw).decode("ascii") != encoded:
        return "INVALID"
    sha, size = entry.get("sha256"), entry.get("bytes")
    if (type(sha) is not str or _SHA.fullmatch(sha) is None
            or hashlib.sha256(raw).hexdigest() != sha
            or type(size) is not int or size < 0 or size != len(raw)):
        return "INVALID"
    return "VERIFIED"


def _time_problems(report):
    problems = []
    capture = _field(report, "capture")
    start, finish = _utc(_field(capture, "started_at_utc")), _utc(_field(capture, "finished_at_utc"))
    if start is None:
        problems.append("invalid_capture_started_at_utc")
    if finish is None:
        problems.append("invalid_capture_finished_at_utc")
    if start is not None and finish is not None and finish < start:
        problems.append("reversed_capture_times")
    declaration = _field(report, "declaration")
    if type(declaration) is dict:
        began, declared = _utc(declaration.get("started_at")), _utc(declaration.get("ts"))
        if began is None:
            problems.append("invalid_producer_started_at")
        if declared is None:
            problems.append("invalid_producer_ts")
        if began is not None and declared is not None and declared < began:
            problems.append("reversed_producer_times")
    return problems


def reconcile_slots(planned_slots, attempt_rows):
    """Return a pure ordered ledger of explicit plans and caller-supplied attempts.

    Both roots must be exact lists. Before any result, fully snapshot both inputs
    as exact finite UTF8 JSON types; custom objects/subclasses/non-string keys,
    cycles/nonfinite/surrogates raise ValueError without coercion. Recursion and
    resource failures and BaseException propagate. All finite JSON bad ROWS remain
    in original with index and diagnostics. Returned originals never alias inputs.

    Plan keys: slot_id/planned_at_utc/expected_proc; attempt keys:
    slot_id/attempt_id/report. Labels are exact nonempty str, whitespace legal.
    Extra/missing keys diagnose *_row_shape but usable slot_id still associates.
    Each slot has index,original,slot_id,candidate_attempt_indexes,state,reason,
    problems. Each attempt has index,original,slot_id,attempt_id,
    candidate_slot_indexes,association,raw_state,problems,report_problems,
    time_problems. Lists retain original order; candidate lists contain ALL input
    indexes, not counted observations. INVALID_SLOT has no usable label; otherwise
    UNOBSERVED/UNKNOWN or ATTEMPTS_SUPPLIED/null. Attempt association is INVALID,
    UNMATCHED, UNIQUE or AMBIGUOUS. Duplicate IDs mark every affected row with
    DUPLICATE_SLOT_ID/DUPLICATE_ATTEMPT_ID; same-slot multiple rows also mark
    MULTIPLE_ATTEMPTS. No rows are overwritten/deduplicated/chosen by time.

    Report diagnostics check only exact root9/input8/capture5, status/null metrics,
    completeness and declaration/problem types. UNKNOWN raw lacks extractable
    dictionaries/key; MISSING is explicit null; VERIFIED is canonical base64 plus
    matching lowercase SHA and exact nonbool int byte count, including empty raw;
    all other raw bindings INVALID. VERIFIED may coexist with bad report/time and
    proves no parsed JSON, declaration, producer, unique sample, execution or file.
    Three original time layers stay in original; pure UTC-Z syntax/reversal issues
    are diagnostics only. Proc mismatch is diagnosed only for a unique association
    with usable literal procs. No clock, capture/archive/report/loader, application
    IO, network, subprocess, sampling, health, duration, auto slot or coverage.

    Exact root: schema m2_slot_ledger/1, status NOT_EVALUATED, declaration_status
    UNVERIFIED, metrics/denominator/healthy_online_us null, slots and attempts.
    Optional archive artifacts/receipts are not verified by this API.
    """
    if type(planned_slots) is not list or type(attempt_rows) is not list:
        raise ValueError("list_roots_required")
    plans = _copy_plain(planned_slots, set())
    supplied = _copy_plain(attempt_rows, set())
    by_slot = _indexes(plans, "slot_id")
    by_attempt_slot = _indexes(supplied, "slot_id")
    by_attempt_id = _indexes(supplied, "attempt_id")
    slots, attempts = [], []
    for index, row in enumerate(plans):
        sid = _label(_field(row, "slot_id"))
        candidates = list(by_attempt_slot.get(sid, []))
        problems = []
        if type(row) is not dict or set(row) != _PLAN:
            problems.append("slot_row_shape")
        if sid is None:
            problems.append("invalid_slot_id")
        if _label(_field(row, "expected_proc")) is None:
            problems.append("invalid_expected_proc")
        if _utc(_field(row, "planned_at_utc")) is None:
            problems.append("invalid_planned_at_utc")
        if sid is not None and len(by_slot[sid]) > 1:
            problems.append("DUPLICATE_SLOT_ID")
        if len(candidates) > 1:
            problems.append("MULTIPLE_ATTEMPTS")
        state = "INVALID_SLOT" if sid is None else "ATTEMPTS_SUPPLIED" if candidates else "UNOBSERVED"
        slots.append({"index": index, "original": row, "slot_id": sid,
                      "candidate_attempt_indexes": candidates, "state": state,
                      "reason": "UNKNOWN" if state == "UNOBSERVED" else None, "problems": problems})
    for index, row in enumerate(supplied):
        sid, aid = _label(_field(row, "slot_id")), _label(_field(row, "attempt_id"))
        candidates = list(by_slot.get(sid, []))
        association = ("INVALID" if sid is None else "UNMATCHED" if not candidates
                       else "UNIQUE" if len(candidates) == 1 else "AMBIGUOUS")
        problems = []
        if type(row) is not dict or set(row) != _ATTEMPT:
            problems.append("attempt_row_shape")
        if sid is None:
            problems.append("invalid_slot_id")
        if aid is None:
            problems.append("invalid_attempt_id")
        if association == "UNMATCHED":
            problems.append("UNMATCHED_SLOT")
        if association == "AMBIGUOUS":
            problems.append("DUPLICATE_SLOT_ID")
        if aid is not None and len(by_attempt_id[aid]) > 1:
            problems.append("DUPLICATE_ATTEMPT_ID")
        if candidates and len(by_attempt_slot[sid]) > 1:
            problems.append("MULTIPLE_ATTEMPTS")
        report = _field(row, "report")
        capture = _field(report, "capture")
        if association == "UNIQUE":
            planned_proc = _label(_field(plans[candidates[0]], "expected_proc"))
            captured_proc = _label(_field(capture, "expected_proc"))
            if planned_proc is not None and captured_proc is not None and planned_proc != captured_proc:
                problems.append("expected_proc_mismatch")
        attempts.append({"index": index, "original": row, "slot_id": sid, "attempt_id": aid,
                         "candidate_slot_indexes": candidates, "association": association,
                         "raw_state": _raw_state(report), "problems": problems,
                         "report_problems": _report_problems(report), "time_problems": _time_problems(report)})
    return {"schema": "m2_slot_ledger/1", "status": "NOT_EVALUATED",
            "declaration_status": "UNVERIFIED", "metrics": None, "denominator": None,
            "healthy_online_us": None, "slots": slots, "attempts": attempts}
