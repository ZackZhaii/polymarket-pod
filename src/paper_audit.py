"""Issue #69 (acceptance 1): offline cross-check of the paper ledger against Executor bus fills.

    python -m src.paper_audit --ledger PATH --bus PATH [--json]

The paper pipeline books a fill into the ledger (``logs/paper.jsonl``) first and only then writes
the same fill as an Executor bus message (``logs/bus.jsonl``, ``type`` "fill" whose payload is the
booked row). Both records carry the fill's explicit ``fill_id``, so the two disk logs can be
joined offline and compared without touching the pipeline.

This first entry point reads both files read-only (standard library only, no network, no halt,
nothing written, no risk decision changed) and cross-checks them by ``fill_id``: how many fills
appear on both sides, which ids are only on the ledger side, which only on the bus side. A fill
that cannot be joined is reported, never guessed. Deeper checks (ledger_store.verify, full payload
equality, duplicate and conflict handling, input SHA256, the UNKNOWN markers) come with the later
acceptance items of issue #69; this tool never claims more than it checked.

Exit codes: 0 at least one fill joined and neither side has an orphan; 1 no fill joined, or an
orphan or join problem on either side (the logs are not validated as agreeing); 2 usage error, or
an input that cannot be read in full (bad flags, a missing or unreadable file, undecodable bytes,
a malformed or non-object line, a torn last line). Registry: docs/EXIT_CODES.md.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

EXIT_OK = 0
EXIT_MISMATCH = 1
EXIT_USAGE = 2

BUS_FILL_TYPE = "fill"
FILL_ID_FIELD = "fill_id"


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
    """Read both files read-only and cross-check ledger fills against Executor bus fills."""
    report: dict[str, Any] = {"ledger": {"path": str(ledger_path)}, "bus": {"path": str(bus_path)},
                              "matched": 0, "ledger_only": [], "bus_only": [], "problems": [],
                              "input_problems": [], "pass": False}
    parsed: dict[str, list[dict[str, Any]]] = {}
    for path, label in ((ledger_path, "ledger"), (bus_path, "bus")):
        try:
            data = path.read_bytes()
        except OSError as exc:
            report["input_problems"].append(f"{label}:unreadable:{type(exc).__name__}")
            report["problems"] = list(report["input_problems"])
            return report
        rows, problems = parse_jsonl(data, label)
        parsed[label] = rows
        report[label]["rows"] = len(rows)
        report["input_problems"].extend(problems)
    if report["input_problems"]:
        report["problems"] = list(report["input_problems"])
        return report  # a file that cannot be read in full is not a partial pass

    ledger_ids: list[str] = []
    for row in parsed["ledger"]:
        fill_id = _fill_id(row.get(FILL_ID_FIELD))
        if fill_id is None:
            report["problems"].append("ledger:row_without_fill_id")  # legacy history: not guessed
        else:
            ledger_ids.append(fill_id)
    bus_ids: list[str] = []
    bus_fills = 0
    for row in parsed["bus"]:
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
    report["bus"]["duplicate_fills"] = len(bus_ids) - len(set(bus_ids))  # counted, judged later (#69)
    report["ledger_only"] = sorted(set(ledger_ids) - set(bus_ids))
    report["bus_only"] = sorted(set(bus_ids) - set(ledger_ids))
    report["matched"] = len(set(ledger_ids) & set(bus_ids))
    report["pass"] = (not report["problems"] and report["matched"] > 0
                      and not report["ledger_only"] and not report["bus_only"])
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.paper_audit",
        description="Offline, read-only cross-check of the paper ledger against Executor bus fills "
                    "by fill_id (issue #69).",
        epilog="Exit codes: 0 every joined fill agrees and neither side has an orphan; 1 no fill "
               "joined or an orphan / parse problem; 2 usage error. See docs/EXIT_CODES.md.")
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
              f"pass={'yes' if report['pass'] else 'no'}")
        for problem in report["problems"]:
            print(f"  problem {problem}")
    if report["input_problems"]:
        print("paper_audit: usage error: an input could not be read in full", file=sys.stderr)
        return EXIT_USAGE
    return EXIT_OK if report["pass"] else EXIT_MISMATCH


if __name__ == "__main__":
    raise SystemExit(main())
