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

Honesty rule (review P1 on PR #202): the deep equality checks — full payload comparison, outer
market and sender roles, duplicate / conflict copy policy, the UNKNOWN markers and input SHA256 —
are the later acceptance items of issue #69 and are not implemented yet, so this tool never
reports PASS: a run without problems still ends with verdict ``unknown`` and a non-zero exit, and
the report names every check that remains unverified.

Exit codes: 0 reserved for the fully checked pass (not reachable until those acceptance items
land); 1 a problem — the ledger failed ``ledger_store.verify``, an orphan or join problem on
either side — or verdict ``unknown`` (the logs are not validated as agreeing); 2 usage error, or
an input that cannot be read in full (bad flags, a missing or unreadable file, undecodable bytes,
a malformed or non-object line, a torn last line). Registry: docs/EXIT_CODES.md.
"""
from __future__ import annotations

import argparse
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

# Review P1 on PR #202: until these later acceptance items of issue #69 land, a clean fill_id join
# is not proof that the two logs agree, so the verdict stays "unknown" and the exit stays non-zero.
UNVERIFIED_CHECKS = (
    "payload_equality_by_fill_id",        # acceptance 3: full payload + outer market comparison
    "bus_outer_market_and_sender_roles",  # acceptance 3/4: outer market, from/to sender roles
    "duplicate_and_conflict_copies",      # acceptance 4: bus-side duplicate / conflict policy
    "legacy_and_hypothetical_unknown",    # acceptance 5: UNKNOWN markers for unjoinable rows
    "input_sha256_and_counts",            # acceptance 6: input hashes, verified / unverified counts
)


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


def audit(ledger_path: Path, bus_path: Path) -> dict[str, Any]:
    """Read both files read-only; the ledger must pass ledger_store.verify before any join."""
    report: dict[str, Any] = {"ledger": {"path": str(ledger_path)}, "bus": {"path": str(bus_path)},
                              "matched": 0, "ledger_only": [], "bus_only": [], "problems": [],
                              "input_problems": [], "ledger_verified": False, "verdict": "fail",
                              "unverified": list(UNVERIFIED_CHECKS)}
    try:
        ledger_data = ledger_path.read_bytes()
    except OSError as exc:
        report["input_problems"].append(f"ledger:unreadable:{type(exc).__name__}")
        report["problems"] = list(report["input_problems"])
        return report
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
    bus_rows, bus_problems = parse_jsonl(bus_data, "bus")
    report["bus"]["rows"] = len(bus_rows)
    report["input_problems"].extend(bus_problems)
    if report["input_problems"]:
        report["problems"] = list(report["input_problems"])
        return report  # a file that cannot be read in full is not a partial pass

    ledger_ids: list[str] = []
    for row in ledger_read.rows:
        fill_id = _fill_id(row.get(FILL_ID_FIELD))
        if fill_id is None:
            report["problems"].append("ledger:row_without_fill_id")  # legacy history: not guessed
        else:
            ledger_ids.append(fill_id)
    bus_ids: list[str] = []
    bus_fills = 0
    for row in bus_rows:
        if row.get("type") != BUS_FILL_TYPE:
            continue  # the bus holds other message types; only fills are this audit's evidence
        bus_fills += 1
        payload = row.get("payload")
        fill_id = _fill_id(payload.get(FILL_ID_FIELD)) if isinstance(payload, dict) else None
        if fill_id is None:
            report["problems"].append("bus:fill_without_fill_id")
        else:
            bus_ids.append(fill_id)
    report["bus"]["fills"] = bus_fills
    report["ledger"]["fills"] = len(ledger_ids)
    report["bus"]["duplicate_fills"] = len(bus_ids) - len(set(bus_ids))  # judged in acceptance 4
    report["ledger_only"] = sorted(set(ledger_ids) - set(bus_ids))
    report["bus_only"] = sorted(set(bus_ids) - set(ledger_ids))
    report["matched"] = len(set(ledger_ids) & set(bus_ids))
    # P1: "pass" is deliberately not assigned here; without the unverified checks above it would
    # claim more than this tool proved. It appears only with the later acceptance items of #69.
    report["verdict"] = ("fail" if report["problems"] or report["ledger_only"] or report["bus_only"]
                         else "unknown")
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.paper_audit",
        description="Offline, read-only cross-check of the paper ledger against Executor bus fills "
                    "by fill_id (issue #69). The ledger must pass ledger_store.verify first.",
        epilog="Exit codes: 0 reserved for the fully checked pass (not reachable yet); 1 a problem "
               "or verdict unknown (the logs are not validated as agreeing); 2 usage error or an "
               "input that cannot be read in full. See docs/EXIT_CODES.md.")
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
        for name in report["unverified"]:
            print(f"  unverified {name}")
        for problem in report["problems"]:
            print(f"  problem {problem}")
    if report["input_problems"]:
        print("paper_audit: usage error: an input could not be read in full", file=sys.stderr)
        return EXIT_USAGE
    return EXIT_OK if report["verdict"] == "pass" else EXIT_MISMATCH


if __name__ == "__main__":
    raise SystemExit(main())
