"""Task AY: recompute the WS book snapshot hash over saved raw frames (offline, read-only).

    python -m src.ws_hash_check <run dir | raw_books dir | raw_books-NNNNNN.jsonl> [--json]

Reads every ``raw_books-*.jsonl`` under the path (``src.m1_run --raw-books`` writes them as
``{"ts", "asset_id", "frame"}``), recomputes each frame's hash with src.ws_recorder.book_hash_check
(the Task AW algorithm: ``hash`` set to "", compact JSON of market, asset_id, timestamp, hash, bids,
asks, min_order_size, tick_size, neg_risk, last_trade_price in that order, SHA-1 hex) and reports
match / mismatch / incomplete / absent / uncomputable counts, overall and per asset, plus lines that
are not a saved frame (``unreadable``). It opens files for reading only, writes nothing, touches no
network and never changes src.ws_recorder.BOOK_HASH_ENFORCED: enforcing stays a decision for later.

Exit codes: 0 at least one frame checked and every one matched; 1 no frame, or not every frame
matched (the algorithm is not validated by this data); 2 usage error (bad arguments, no such path,
a file that cannot be read). Registry: docs/EXIT_CODES.md.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.ws_recorder import book_hash_check  # noqa: E402

EXIT_ALL_MATCH = 0
EXIT_NOT_VALIDATED = 1
EXIT_USAGE = 2

PATTERN = "raw_books-*.jsonl"
RESULTS = ("match", "mismatch", "incomplete", "absent", "uncomputable", "unreadable")


def raw_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    return sorted(p for p in path.rglob(PATTERN) if p.is_file())


def check_files(files: list[Path]) -> dict[str, Any]:
    totals: Counter = Counter({name: 0 for name in RESULTS})
    per_asset: dict[str, Counter] = defaultdict(Counter)
    per_file: dict[str, dict[str, int]] = {}
    for path in files:
        counts: Counter = Counter()
        with open(path, "rb") as handle:
            for raw in handle:
                if not raw.strip():
                    continue
                try:
                    row = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    row = None
                frame = row.get("frame") if isinstance(row, dict) else None
                if not isinstance(frame, dict) or frame.get("event_type") != "book":
                    result, asset = "unreadable", ""
                else:
                    result, asset = book_hash_check(frame), str(frame.get("asset_id") or "")
                counts[result] += 1
                totals[result] += 1
                per_asset[asset][result] += 1
        per_file[str(path)] = dict(counts)
    checked = sum(totals[name] for name in RESULTS if name != "unreadable")
    return {"files": len(files), "frames": checked, "counts": dict(totals),
            "all_match": checked > 0 and totals["match"] == checked and totals["unreadable"] == 0,
            "per_asset": {asset: dict(counts) for asset, counts in sorted(per_asset.items())},
            "per_file": per_file}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.ws_hash_check",
        description="Recompute the WS book snapshot hash over saved raw frames (read-only, offline).",
        epilog="Exit codes: 0 every frame matched; 1 no frame or not every frame matched; 2 usage error. "
               "See docs/EXIT_CODES.md.")
    parser.add_argument("path", type=Path, help="M1 run directory, its raw_books/ directory, or one raw_books file")
    parser.add_argument("--json", action="store_true", help="print the full report as JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return EXIT_USAGE if exc.code else EXIT_ALL_MATCH
    if not args.path.exists():
        print(f"ws_hash_check: usage error: {args.path} does not exist", file=sys.stderr)
        return EXIT_USAGE
    files = raw_files(args.path)
    try:
        report = check_files(files)
    except OSError as exc:
        print(f"ws_hash_check: usage error: cannot read {exc.filename}: {type(exc).__name__}", file=sys.stderr)
        return EXIT_USAGE
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        counts = " ".join(f"{name}={report['counts'][name]}" for name in RESULTS)
        print(f"ws_hash_check files={report['files']} frames={report['frames']} {counts} "
              f"all_match={'yes' if report['all_match'] else 'no'}")
        for asset, per in report["per_asset"].items():
            print(f"  asset {asset or '-'}: " + " ".join(f"{k}={v}" for k, v in sorted(per.items())))
    return EXIT_ALL_MATCH if report["all_match"] else EXIT_NOT_VALIDATED


if __name__ == "__main__":
    raise SystemExit(main())
