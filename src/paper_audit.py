"""Issue #69: offline cross-check of the paper ledger against Executor bus fills.

    python -m src.paper_audit --ledger PATH --bus PATH [--json]

The paper pipeline books a fill into the ledger (``logs/paper.jsonl``) first and only then writes
the same fill as an Executor bus message (``logs/bus.jsonl``, ``type`` "fill" whose payload is the
booked row). Both records carry the fill's explicit ``fill_id``, so the two disk logs can be
joined offline and compared without touching the pipeline.

This tool reads both files read-only (standard library and first-party ``src`` only, no network,
no halt, nothing written, no risk decision changed). Acceptance 2 of issue #69: the ledger side
must first pass ``ledger_store.verify`` (the same strict parse, fill-id, non-finite, conservation
and risk-control-field checks the pipeline itself runs), and the bus side is parsed strictly as
UTF-8 JSONL — a bad JSON line, a non-object line, a torn last line or an unreadable file is a
named failure; no lenient reader that skips bad lines is used. Only a ledger that verifies is
joined, by explicit ``fill_id``.

Honesty rule (review P1 on PR #202, closed by acceptance 6 of issue #69): every check of the issue
now runs, and the report carries each input's SHA256 (``ledger`` / ``bus`` ``sha256``, of the exact
bytes read), the verified / unverified counts (``counts``: fills whose two copies were cross-checked
by the join vs legacy rows that can never be joined) and the difference reasons (the named
``problems``, ``ledger_only`` / ``bus_only`` and ``input_problems`` entries, each naming the fill
and the drifted field). Two aspects this audit cannot verify on its own evidence are explicitly
UNKNOWN in every run (``unknown_checks``): the independent RiskGate allow attribution (the decision
summary carries no fill_id / token_id / decision_id to join on, src/paper_ledger.py) and
mark-to-market (part of neither log). They are honest non-claims, not differences, and do not gate
the verdict — the verdict speaks only to the checks this tool runs. A problem-free run that
cross-checked at least one fill and has no unverified legacy row is verdict ``pass`` / exit 0; the
acceptance 5 coverage gate keeps an all-empty or legacy-only run at ``unknown`` / exit 1.

Acceptance 5 of issue #69 (this change): a modern hypothetical fill (``hypothetical`` true,
``counts_toward_risk`` false) carries a ``fill_id`` like every modern fill and is written to both
logs, so it joins and its duplicate copies are checked exactly like a real fill's (counted in
``ledger.hypothetical_fills``). A ledger row without ``fill_id`` is pre-Task-AF history —
ledger_store accepts such rows only before the first id'd one — and has no honest join, so none is
guessed: the row is reported UNKNOWN (``unknowns``, its derived ``legacy:`` id used as a stable
name, never a join key) and counted (``ledger.legacy_rows``), not named a problem. Coverage gate:
a run that cross-checked no fill at all (all-empty inputs, or nothing joinable) or that still has
unverified legacy rows proved nothing about the logs as a whole, so it can never report the full
pass — verdict ``unknown``.

Acceptance 3 of issue #69: every joined fill is compared in full. The ledger row and
the Executor fill's ``payload`` must be equal under a type-sensitive normalized JSON comparison —
key order and JSON formatting are normalized away, but ``10``, ``10.0``, ``true`` and ``"10.0"``
stay distinct (Python ``==`` would call the first three equal) — and the message's outer ``market``
must name the fill's market. The outer ``bus.ts`` is the bus write time and is deliberately
ignored. A drifted copy, a missing field, a non-finite number or a wrong outer market is a named
problem and a non-zero exit.

Acceptance 4 of issue #69 (this change): the sender roles of every bus fill must be exactly the
ones the pipeline writes — ``from`` Executor ``to`` Sentinel (paper_ledger is the only fill emitter
and podlib refuses a fill from anyone else); bus copies of one fill that are fully identical
(payload, outer market, roles — the outer ``bus.ts`` write time is ignored) count once and the
extra-copy count is reported, while copies that contradict each other are a named conflict; an
orphan on either side is a named failure. NEW-3b (ruled into #69): settlement rows (``kind``
"settlement") are booked to the ledger only and never have a bus fill, so they are counted
separately (``ledger.settlements``) and are not orphans; a settlement row without a fill_id, or a
bus fill carrying a settlement's fill_id, is a named problem.

Exit codes: 0 the fully checked pass — every check ran, nothing differs, at least one fill was
cross-checked and no unverified legacy row remains (the coverage gate keeps an all-empty or
legacy-only run at ``unknown``); 1 a
problem — the ledger failed ``ledger_store.verify``, an orphan, a content mismatch or a join
problem on either side — or verdict ``unknown`` (the logs are not validated as agreeing); 2 usage
error, or an input that cannot be read in full (bad flags, a missing or unreadable file,
undecodable bytes, a malformed or non-object line, a torn last line). Registry: docs/EXIT_CODES.md.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from src import ledger_store

EXIT_OK = 0
EXIT_MISMATCH = 1
EXIT_USAGE = 2

BUS_FILL_TYPE = "fill"
FILL_ID_FIELD = "fill_id"
# Acceptance 4: the only fill emitter is paper_ledger (src/paper_ledger.py:671), always
# bus("Executor", "Sentinel", "fill", ...); podlib.bus itself refuses a fill from anyone else.
BUS_FILL_FROM = "Executor"
BUS_FILL_TO = "Sentinel"

# Acceptance 6 of issue #69: aspects this audit cannot verify on its own evidence and therefore
# reports as UNKNOWN in every run — an honest non-claim, never a pass claim and never a difference.
# The RiskGate decision summary carries no fill_id / token_id / decision_id (src/paper_ledger.py),
# so an "allow" cannot be attributed to a fill independently of the two copies agreeing, and
# mark-to-market revaluation is part of neither log. Neither gates the verdict: the verdict speaks
# only to the checks this tool actually runs.
UNKNOWN_CHECKS = ("riskgate_allow_attribution", "mark_to_market")


def parse_jsonl(data: bytes, label: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Strictly parse one UTF-8 JSONL file: every non-blank line must be one JSON object.

    Never raises; every defect (undecodable bytes, a malformed line, a non-object line, a last
    line without its newline) is returned as a named problem. No line is ever skipped silently.
    """
    rows: list[dict[str, Any]] = []
    problems: list[str] = []
    if not data:
        return rows, problems
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return [], [f"{label}:undecodable"]
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    else:
        problems.append(f"{label}:torn_last_row")
    for number, line in enumerate(lines, start=1):
        line = line.rstrip("\r")
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            problems.append(f"{label}:malformed_row:{number}")
            continue
        if isinstance(row, dict):
            rows.append(row)
        else:
            problems.append(f"{label}:non_object_row:{number}")
    return rows, problems


def _fill_id(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _canonical(value: Any) -> str | None:
    """Type-sensitive normalized JSON text of one value (acceptance 3).

    Key order and JSON formatting are normalized away, but the JSON type is not: ``10`` ("10"),
    ``10.0`` ("10.0"), ``true`` ("true") and ``"10.0"`` stay distinct, where Python ``==`` would
    call the first three equal. None means the value is not finite JSON (NaN / inf): it can never
    compare equal to anything, and is named by ``_non_finite_fields``.
    """
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":"))
    except (TypeError, ValueError):
        return None


def _non_finite_fields(row: dict[str, Any]) -> list[str]:
    """Field names whose value is not finite JSON (NaN / inf / unserializable), sorted."""
    return [name for name in sorted(row) if _canonical(row[name]) is None]


def _changed_fields(ledger_row: dict[str, Any], payload: dict[str, Any]) -> list[str]:
    """Top-level field names where the booked row and the bus payload differ, sorted.

    A key present on one side only is a difference too: the payload is the booked row verbatim
    (paper_ledger: ``fill = booked``), so the key sets must match exactly.
    """
    changed = []
    for name in sorted(set(ledger_row) | set(payload)):
        if name not in ledger_row or name not in payload:
            changed.append(name)
        elif _canonical(ledger_row[name]) != _canonical(payload[name]):
            changed.append(name)
    return changed


def audit(ledger_path: Path, bus_path: Path) -> dict[str, Any]:
    """Read both files read-only; the ledger must pass ledger_store.verify before any join."""
    report: dict[str, Any] = {"ledger": {"path": str(ledger_path), "sha256": None},
                              "bus": {"path": str(bus_path), "sha256": None},
                              "matched": 0, "ledger_only": [], "bus_only": [], "problems": [],
                              "input_problems": [], "ledger_verified": False, "verdict": "fail",
                              "unknowns": [], "unknown_checks": list(UNKNOWN_CHECKS),
                              "counts": {"verified_fills": 0, "unverified_rows": 0}}
    try:
        ledger_data = ledger_path.read_bytes()
    except OSError as exc:
        report["input_problems"].append(f"ledger:unreadable:{type(exc).__name__}")
        report["problems"] = list(report["input_problems"])
        return report
    report["ledger"]["sha256"] = hashlib.sha256(ledger_data).hexdigest()
    ledger_read = ledger_store.parse_ledger(ledger_data)
    report["ledger"]["rows"] = len(ledger_read.rows)
    report["ledger"]["duplicates_ignored"] = ledger_read.duplicates_ignored
    if ledger_read.problems:
        # A parse-level defect (undecodable bytes, a torn tail, a malformed / non-object line, a
        # bad or conflicting fill_id): the file cannot be read in full, exactly as ledger_store
        # itself reports it. Not a partial pass.
        report["input_problems"].extend(f"ledger:{problem}" for problem in ledger_read.problems)
        report["problems"] = list(report["input_problems"])
        return report
    verify_problems = ledger_store.verify(ledger_read)
    if verify_problems:
        # Acceptance 2: the audit refuses to join a ledger that does not verify.
        report["problems"].extend(f"ledger:verify:{problem}" for problem in verify_problems)
        return report
    report["ledger_verified"] = True

    try:
        bus_data = bus_path.read_bytes()
    except OSError as exc:
        report["input_problems"].append(f"bus:unreadable:{type(exc).__name__}")
        report["problems"] = list(report["input_problems"])
        return report
    report["bus"]["sha256"] = hashlib.sha256(bus_data).hexdigest()
    bus_rows, bus_problems = parse_jsonl(bus_data, "bus")
    report["bus"]["rows"] = len(bus_rows)
    report["input_problems"].extend(bus_problems)
    if report["input_problems"]:
        report["problems"] = list(report["input_problems"])
        return report  # a file that cannot be read in full is not a partial pass

    ledger_ids: list[str] = []
    ledger_by_id: dict[str, dict[str, Any]] = {}
    settlement_ids: set[str] = set()
    settlements = 0
    hypotheticals = 0
    legacy_rows = 0
    for row, derived_id in zip(ledger_read.rows, ledger_read.ids):
        fill_id = _fill_id(row.get(FILL_ID_FIELD))
        if fill_id is None and ledger_store.is_settlement(row):
            # NEW-3b: a settlement row without a fill_id gets its own name (ledger_store itself
            # only reports a generic missing fill_id, and a legacy settlement row passes verify).
            report["problems"].append("ledger:settlement_without_fill_id")
        elif fill_id is None:
            # Acceptance 5: a row without fill_id is pre-Task-AF history (ledger_store accepts
            # such rows only before the first id'd one); there is no honest join for it, so none
            # is guessed. It is UNKNOWN and counted, its derived legacy id used as a stable name,
            # never a join key, and the row is never read as a ledger_only orphan either.
            legacy_rows += 1
            report["unknowns"].append(derived_id)
        elif ledger_store.is_settlement(row):
            # NEW-3b: settlement rows are booked to the ledger only and never have a bus fill, so
            # they are counted separately and never read as ledger-only orphans.
            settlements += 1
            settlement_ids.add(fill_id)
        else:
            ledger_ids.append(fill_id)
            ledger_by_id[fill_id] = row
            if row.get("hypothetical") is True:
                # Acceptance 5: a modern hypothetical fill joins and takes part in the copy checks
                # exactly like a real fill (verify already proved the field is a bool).
                hypotheticals += 1
    bus_ids: list[str] = []
    bus_by_id: dict[str, list[dict[str, Any]]] = {}
    bus_fills = 0
    for row in bus_rows:
        if row.get("type") != BUS_FILL_TYPE:
            continue  # the bus holds other message types; only fills are this audit's evidence
        bus_fills += 1
        payload = row.get("payload")
        fill_id = _fill_id(payload.get(FILL_ID_FIELD)) if isinstance(payload, dict) else None
        if fill_id is None:
            report["problems"].append("bus:fill_without_fill_id")
            continue
        # Acceptance 4: a fill is always the Executor → Sentinel message paper_ledger writes.
        if row.get("from") != BUS_FILL_FROM or row.get("to") != BUS_FILL_TO:
            report["problems"].append(f"sender_role_mismatch:{fill_id}")
        # NEW-3b: a settlement is ledger-only; a bus fill with its id contradicts that.
        if fill_id in settlement_ids:
            report["problems"].append(f"settlement_with_bus_fill:{fill_id}")
        bus_ids.append(fill_id)
        bus_by_id.setdefault(fill_id, []).append(row)
    report["bus"]["fills"] = bus_fills
    report["ledger"]["fills"] = len(ledger_ids)
    report["ledger"]["settlements"] = settlements
    report["ledger"]["hypothetical_fills"] = hypotheticals
    report["ledger"]["legacy_rows"] = legacy_rows  # acceptance 5: the unverified count
    report["bus"]["duplicate_fills"] = len(bus_ids) - len(set(bus_ids))  # extra copies, reported
    # Acceptance 4: copies of one fill that contradict each other are a named conflict, even when
    # one of them equals the booked row. The outer bus.ts is the write time and is ignored, so
    # copies written at different times are still identical when nothing else drifted.
    for fill_id, messages in sorted(bus_by_id.items()):
        if len(messages) < 2:
            continue
        identities = {_canonical({name: message[name] for name in message if name != "ts"})
                      for message in messages}
        if len(identities) > 1:
            report["problems"].append(f"duplicate_conflict:{fill_id}")
    report["ledger_only"] = sorted(set(ledger_ids) - set(bus_ids))
    report["bus_only"] = sorted(set(bus_ids) - set(ledger_ids))
    report["matched"] = len(set(ledger_ids) & set(bus_ids))

    # Acceptance 3 of issue #69: every joined fill's full payload and the message's outer market.
    # The comparison is type-sensitive normalized JSON (see _canonical); the outer bus.ts is the
    # bus write time and is deliberately ignored. Every copy of a fill is compared, so a drifted
    # second copy fails too (acceptance 4 names the pair a duplicate_conflict as well).
    for fill_id in sorted(set(ledger_ids) & set(bus_ids)):
        ledger_row = ledger_by_id[fill_id]
        for message in bus_by_id[fill_id]:
            payload = message.get("payload")
            if _non_finite_fields(ledger_row) or _non_finite_fields(payload):
                # NaN / inf can never be evidence of equality (even to a same-valued NaN copy).
                report["problems"].append(f"non_finite_payload:{fill_id}")
                continue
            fields = _changed_fields(ledger_row, payload)
            if fields:
                report["problems"].append(f"payload_mismatch:{fill_id}:{'+'.join(fields)}")
            outer_market = _canonical(message.get("market"))
            if outer_market is None or outer_market != _canonical(ledger_row.get("market")):
                report["problems"].append(f"outer_market_mismatch:{fill_id}")
    # Acceptance 6: the verified / unverified counts. Verified = fills whose two copies were read
    # and fully cross-checked by the join (a drifted copy is verified too — the difference is what
    # the named reasons report); unverified = legacy rows, which can never be joined.
    report["counts"] = {"verified_fills": report["matched"], "unverified_rows": legacy_rows}
    # Acceptance 5, coverage gate: a run that cross-checked no fill at all (all-empty inputs, or
    # nothing joinable) or that still has unverified legacy rows proved nothing about the logs as a
    # whole, so it can never report the full pass.
    if report["problems"] or report["ledger_only"] or report["bus_only"]:
        report["verdict"] = "fail"
    elif report["unknowns"] or report["matched"] == 0:
        report["verdict"] = "unknown"
    else:
        report["verdict"] = "pass"
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.paper_audit",
        description="Offline, read-only cross-check of the paper ledger against Executor bus fills "
                    "by fill_id (issue #69). The ledger must pass ledger_store.verify first.",
        epilog="Exit codes: 0 the fully checked pass (every check ran and agrees; the coverage gate "
               "keeps an all-empty or legacy-only run unknown); 1 a problem or verdict unknown (the "
               "logs are not validated as agreeing); 2 usage error or an input that cannot be read "
               "in full. See docs/EXIT_CODES.md.")
    parser.add_argument("--ledger", type=Path, required=True, help="the paper ledger JSONL file")
    parser.add_argument("--bus", type=Path, required=True, help="the bus JSONL file")
    parser.add_argument("--json", action="store_true", help="print the full report as JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return EXIT_USAGE if exc.code else EXIT_OK
    report = audit(args.ledger, args.bus)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"paper_audit matched={report['matched']} "
              f"ledger_only={','.join(report['ledger_only']) or '-'} "
              f"bus_only={','.join(report['bus_only']) or '-'} "
              f"verdict={report['verdict']}")
        counts = report["counts"]
        print(f"  verified_fills={counts['verified_fills']} "
              f"unverified_rows={counts['unverified_rows']}")
        for name in report["unknown_checks"]:
            print(f"  unknown check {name}")  # acceptance 6: honest non-claims, never a pass claim
        for name in report["unknowns"]:
            print(f"  unknown {name}")
        for problem in report["problems"]:
            print(f"  problem {problem}")
    if report["input_problems"]:
        print("paper_audit: usage error: an input could not be read in full", file=sys.stderr)
        return EXIT_USAGE
    return EXIT_OK if report["verdict"] == "pass" else EXIT_MISMATCH


if __name__ == "__main__":
    raise SystemExit(main())
