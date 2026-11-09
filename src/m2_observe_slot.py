"""EY: one explicitly authorized synthetic slot attempt, not a sampling runtime.

Only existing public EV capture and EW archive perform application IO. Callers
must already own/authorize the explicit local fixture source and stable archive
root/ancestors under the task's fixed Source/QC/CI bases. Arguments do not grant
IO permission or an OS sandbox/physical-nonalias/TOCTOU guarantee. Import code IO
is distinct from application IO; importing this module starts no runtime.
"""
import base64
import hashlib
import json
from pathlib import Path
import re

from src.m2_report import capture_heartbeat_once
from src.m2_archive import archive_capture_once


_DOMAIN = b"polymarket-pod/m2-observe-slot/1\x00"
_PLAN = {"slot_id", "planned_at_utc", "expected_proc"}
_SPEC = {"source", "format"}
_NAME = re.compile(r"hb-[a-z0-9][a-z0-9_-]*\.jsonl")


def _string(value):
    """Require an original nonempty plain UTF8 string; no conversion or trimming."""
    if type(value) is not str or not value:
        raise ValueError("nonempty_plain_UTF8_string_required")
    value.encode("utf-8", errors="strict")
    return value


def _labels(value, fields):
    """Snapshot exact plain dictionaries of exact string labels, no custom hooks."""
    if type(value) is not dict or any(type(k) is not str for k in value):
        raise ValueError("plain_label_object_required")
    if set(value) != fields:
        raise ValueError("label_object_fields")
    return {key: _string(item) for key, item in value.items()}


def _local(value):
    """Pure lexical local-path guard; neither resolve nor stat nor authorization."""
    value = _string(value)
    spelling = value.replace("\\", "/")
    if ("\x00" in value or spelling.startswith("//") or "://" in spelling
            or spelling.startswith(("/??/", "/?/", "/./"))):
        raise ValueError("nonordinary_local_path")
    if any(part.casefold() == ".env" or part.casefold().startswith(".env.")
           for part in spelling.split("/")):
        raise ValueError("environment_file_path")
    if not Path(value).is_absolute():
        raise ValueError("host_absolute_path_required")
    return value


def observe_slot_once(planned_slot, *, attempt_id, spec, owned_root, file_name):
    r"""Observe one supplied plan/attempt via at most one public EV then one EW.

    Required inputs are exact dict plan={slot_id,planned_at_utc,expected_proc},
    spec={source,format}, and exact nonempty plain UTF8 string labels/attempt/root/
    name. format is exactly json. Wrong keys/types/nonJSON/subclasses/surrogates
    raise ValueError before EV clocks/application IO; no PathLike/default/str
    coercion. Source/root must be hostabsolute and pass lexical URL/UNC/device/
    .env rejection; filename matches hb-[a-z0-9][a-z0-9_-]*\.jsonl. Existing EV
    resolved-alias guards and EW stricter ASCII/trusted-root guards still apply;
    no source/root ASCII tightening here. Input label dicts are snapshotted; raw
    strings stay unchanged, including whitespace/Unicode. planned_at_utc is an
    opaque label, never parsed, normalized or used to schedule/associate/score.

    Binding material is b"polymarket-pod/m2-observe-slot/1\x00" plus UTF8 of JSON
    {planned_slot:snapshot,attempt_id:original} with sort_keys=True,
    ensure_ascii=False,allow_nan=False,separators=(",",":"). Lowercase SHA256 of
    material yields capture_id="m2-slot-v1:"+hex. Return binding exact keys scheme
    m2_slot_capture/1, material_base64, sha256, capture_id. Given plan+attempt,
    recompute independently against successful EW file's capture_report.capture.
    capture_id; file alone cannot recover/authenticate plan/attempt. Identical
    material repeats its ID; this is no authenticated or unique sample identity.

    Exact root keys: schema=m2_observe_slot/1,status=NOT_EVALUATED,
    declaration_status=UNVERIFIED,metrics/denominator/healthy_online_us=None,
    planned_slot,binding,attempt,archive_receipt,phase,error_type,
    not_archived_reason. attempt has exactly slot_id,attempt_id,report (EX shape).
    EV report and EW receipt are kept unchanged, not repaired or revalidated.
    Capture ordinary Exception -> CAPTURE_ERROR/class-name/no report or receipt/
    capture_exception_no_report; defensive None -> CAPTURE_NO_REPORT/null-error/
    capture_returned_no_report. Both archive0, no fabricated endpoints/read-stage.
    Otherwise EW called once: ordinary exception -> ARCHIVE_ERROR/class-name/null
    receipt/archive_exception with report retained. Returned receipt ->
    ARCHIVE_RETURNED/null-error and reason archive_refused/archive_unknown/null
    for REFUSED/UNKNOWN/WRITTEN_READ_BACK, else archive_result_unclassified.
    No exception string messages, retry, second source read/new clock/new path/
    second output file/sidecar/private protocol parser. BaseException propagates;
    old EW cleanupBase can supersede originalBase. Resource/recursion failures in
    required-input processing propagate. Failure result is memory only and may
    have no archive; interrupts may return nothing. Not durable30s failure audit.

    Only explicit authorized synthetic source/fixed sibling heartbeat protocol
    CODE/old EV clocks and EW root-metadata+one xb/readback target are permitted
    by this task. No wrapper IO/clock/network/process/loop/sleep/CLI/runtime/
    metrics/health/cadence/M2/profit inference. Successful readback is synchronous
    evidence, not powerloss durability, provenance, real sampling or online time.
    """
    plan = _labels(planned_slot, _PLAN)
    given_spec = _labels(spec, _SPEC)
    attempt_id = _string(attempt_id)
    if given_spec["format"] != "json":
        raise ValueError("json_format_required")
    _local(given_spec["source"])
    owned_root = _local(owned_root)
    file_name = _string(file_name)
    if _NAME.fullmatch(file_name) is None:
        raise ValueError("archive_file_name")
    material = _DOMAIN + json.dumps({"planned_slot": plan, "attempt_id": attempt_id},
                                   sort_keys=True, ensure_ascii=False, allow_nan=False,
                                   separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(material).hexdigest()
    capture_id = "m2-slot-v1:" + digest
    result = {"schema": "m2_observe_slot/1", "status": "NOT_EVALUATED",
              "declaration_status": "UNVERIFIED", "metrics": None, "denominator": None,
              "healthy_online_us": None, "planned_slot": plan,
              "binding": {"scheme": "m2_slot_capture/1",
                          "material_base64": base64.b64encode(material).decode("ascii"),
                          "sha256": digest, "capture_id": capture_id},
              "attempt": {"slot_id": plan["slot_id"], "attempt_id": attempt_id, "report": None},
              "archive_receipt": None, "phase": "CAPTURE_ERROR", "error_type": None,
              "not_archived_reason": "capture_exception_no_report"}
    try:
        report = capture_heartbeat_once(given_spec, expected_proc=plan["expected_proc"],
                                        capture_id=capture_id)
    except Exception as exc:
        result["error_type"] = type(exc).__name__
        return result
    if report is None:
        result.update(phase="CAPTURE_NO_REPORT", not_archived_reason="capture_returned_no_report")
        return result
    result["attempt"]["report"] = report
    try:
        receipt = archive_capture_once(report, owned_root=owned_root, file_name=file_name)
    except Exception as exc:
        result.update(phase="ARCHIVE_ERROR", error_type=type(exc).__name__,
                      not_archived_reason="archive_exception")
        return result
    state = receipt.get("write_state") if type(receipt) is dict else None
    reason = ("archive_refused" if state == "REFUSED" else "archive_unknown" if state == "UNKNOWN"
              else None if state == "WRITTEN_READ_BACK" else "archive_result_unclassified")
    result.update(phase="ARCHIVE_RETURNED", archive_receipt=receipt,
                  not_archived_reason=reason)
    return result
