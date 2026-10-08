"""Offline M1 acceptance report (Task AI) for a run directory written by src.m1_run.

    python -m src.m1_report RUN_DIR [--out-md PATH] [--out-json PATH]

Reads only RUN_DIR (manifest.json, books/, events/, rest/); no network. Writes report.md and
report.json into RUN_DIR (or the given paths) and prints PASS or FAIL.

Measured over the run window (manifest started_at .. ended_at, recorder clock):

- coverage per asset: the share of timeline points (every M1_REPORT_STEP_S) at which the latest
  WS row written for the asset is a live book whose exchange ``book_ts`` satisfies
  ``0 <= t - book_ts <= MAX_BOOK_AGE_S`` (the book_staleness.book_age_problem rule; a future
  book_ts, an invalidation row or no row yet are not fresh);
- book age p50/p90/p99/max per asset over the points that have a live book, plus the share of
  points with no live book;
- resyncs by reason (``disconnect_*`` versus gaps and invalid data), reconnects, connect
  failures, disconnect reasons, from events/;
- REST vs WS: each REST /book sample is compared with the live WS row for the same token whose
  exchange book_ts is nearest (within M1_REST_MATCH_WINDOW_S); best bid and best ask must be equal.
  Samples without such a row (or with an unusable REST book_ts) are unmatched; mismatches are listed.

Acceptance criteria (src/thresholds.py, M2 pending): see CRITERIA below and docs/M1_ACCEPTANCE.md.
Exit codes: 0 PASS; 1 FAIL (including an unfinished or unreadable run); 2 usage error.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.book_staleness import parse_book_ts  # noqa: E402
from src.thresholds import (  # noqa: E402
    M1_MAX_GAP_RESYNCS_PER_ASSET,
    M1_MAX_RECONNECTS,
    M1_MAX_REST_UNMATCHED_RATE,
    M1_MAX_TOP_MISMATCH_RATE,
    M1_MIN_COVERAGE,
    M1_MIN_REST_MATCHED,
    M1_MIN_RUN_S,
    M1_REPORT_STEP_S,
    M1_REST_MATCH_WINDOW_S,
    MAX_BOOK_AGE_S,
)

SCHEMA = "m1_report/1"
RUN_SCHEMA = "m1_run/1"
from src.m1_run import PINNED_RISK_SHA256  # noqa: E402  (one copy of the pin in M1 code)
STREAMS = ("books", "events", "rest")
EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_US = timedelta(microseconds=1)
MISMATCH_LIST_LIMIT = 200


def to_us(value: object) -> int | None:
    """Exact integer microseconds since the epoch (no float rounding at the freshness boundary)."""
    stamp = parse_book_ts(value)
    return None if stamp is None else (stamp - _EPOCH) // _US


def percentile(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile (q in (0, 100]); None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100.0 * len(ordered)))
    return ordered[rank - 1]


# --- reading -----------------------------------------------------------------------------------


def read_stream(run_dir: Path, name: str, listed: list[str] | None) -> tuple[list[dict], dict[str, Any]]:
    """All rows of one stream in file order; unreadable lines, missing and unlisted files are counted."""
    folder = run_dir / name
    on_disk = sorted(path.relative_to(run_dir).as_posix() for path in folder.glob(f"{name}-*.jsonl")) if folder.is_dir() else []
    listed = list(listed or [])
    files = sorted(set(on_disk) | set(listed))
    rows: list[dict] = []
    bad_lines = 0
    for rel in files:
        path = run_dir / rel
        if not path.is_file():
            continue
        with open(path, "rb") as handle:
            for raw in handle:
                if not raw.strip():
                    continue
                try:
                    row = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    bad_lines += 1
                    continue
                if isinstance(row, dict):
                    rows.append(row)
                else:
                    bad_lines += 1
    info = {"files": files, "rows": len(rows), "unreadable_lines": bad_lines,
            "missing_files": sorted(set(listed) - set(on_disk)), "unlisted_files": sorted(set(on_disk) - set(listed))}
    return rows, info


# --- WS timeline -------------------------------------------------------------------------------


def _live_book_us(row: dict) -> int | None:
    """book_ts in microseconds for a live WS row; None for invalidation rows or unusable stamps."""
    if row.get("book_ts_problem") is not None or row.get("ws_state") == "resync":
        return None
    return to_us(row.get("book_ts"))


def timeline(rows: list[dict], start_us: int, end_us: int, step_us: int, max_age_us: int) -> dict[str, Any]:
    """Coverage and age for one asset. ``rows`` are that asset's WS rows in any order."""
    stamped = []
    bad_ts = 0
    for index, row in enumerate(rows):
        written = to_us(row.get("ts"))
        if written is None:
            bad_ts += 1
            continue
        stamped.append((written, index, _live_book_us(row)))
    stamped.sort()
    points = fresh = no_book = 0
    ages: list[float] = []
    current: int | None = None
    have_row = False
    cursor = 0
    t = start_us
    while t <= end_us:
        while cursor < len(stamped) and stamped[cursor][0] <= t:
            current = stamped[cursor][2]
            have_row = True
            cursor += 1
        points += 1
        if not have_row or current is None:
            no_book += 1
        else:
            age = t - current
            ages.append(age / 1e6)
            if 0 <= age <= max_age_us:
                fresh += 1
        t += step_us
    return {
        "points": points,
        "coverage": fresh / points if points else 0.0,
        "no_book_share": no_book / points if points else 1.0,
        "age_s": {"p50": percentile(ages, 50), "p90": percentile(ages, 90), "p99": percentile(ages, 99),
                  "max": max(ages) if ages else None},
        "rows": len(rows),
        "rows_bad_ts": bad_ts,
    }


# --- REST vs WS --------------------------------------------------------------------------------


def _price(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return round(float(value), 6)


def compare_rest(rest_rows: list[dict], ws_by_asset: dict[str, list[dict]], window_us: int) -> dict[str, Any]:
    live: dict[str, list[tuple[int, dict]]] = {}
    for asset, rows in ws_by_asset.items():
        items = [(stamp, row) for row in rows if (stamp := _live_book_us(row)) is not None]
        items.sort(key=lambda item: item[0])
        live[asset] = items
    matched = mismatched = 0
    unmatched: Counter = Counter()
    mismatches: list[dict] = []
    per_asset: dict[str, Counter] = defaultdict(Counter)
    for rest in rest_rows:
        asset = str(rest.get("token_id") or "")
        per_asset[asset]["samples"] += 1
        rest_us = None if rest.get("book_ts_problem") is not None else to_us(rest.get("book_ts"))
        if rest_us is None:
            unmatched["rest_book_ts_unusable"] += 1
            per_asset[asset]["unmatched"] += 1
            continue
        best = None
        for stamp, row in live.get(asset, []):  # lists are short (rows only on top changes); linear is fine
            distance = abs(stamp - rest_us)
            if best is None or distance < best[0]:
                best = (distance, stamp, row)
        if best is None or best[0] > window_us:
            unmatched["no_ws_row_in_window" if best is not None else "no_ws_row"] += 1
            per_asset[asset]["unmatched"] += 1
            continue
        matched += 1
        per_asset[asset]["matched"] += 1
        ws_row = best[2]
        bid_ok = _price(rest.get("best_bid")) == _price(ws_row.get("best_bid"))
        ask_ok = _price(rest.get("best_ask")) == _price(ws_row.get("best_ask"))
        if not (bid_ok and ask_ok):
            mismatched += 1
            per_asset[asset]["mismatched"] += 1
            if len(mismatches) < MISMATCH_LIST_LIMIT:
                mismatches.append({
                    "token_id": asset, "rest_book_ts": rest.get("book_ts"), "ws_book_ts": ws_row.get("book_ts"),
                    "dt_s": round((best[1] - rest_us) / 1e6, 3),
                    "rest_best_bid": rest.get("best_bid"), "ws_best_bid": ws_row.get("best_bid"),
                    "rest_best_ask": rest.get("best_ask"), "ws_best_ask": ws_row.get("best_ask"),
                    "sides": [side for side, ok in (("bid", bid_ok), ("ask", ask_ok)) if not ok],
                })
    samples = len(rest_rows)
    unmatched_total = sum(unmatched.values())
    return {
        "samples": samples,
        "matched": matched,
        "mismatched": mismatched,
        "match_rate": (matched - mismatched) / matched if matched else None,
        "mismatch_rate": mismatched / matched if matched else None,
        "unmatched": unmatched_total,
        "unmatched_rate": unmatched_total / samples if samples else None,
        "unmatched_reasons": dict(unmatched),
        "per_asset": {asset: dict(counts) for asset, counts in sorted(per_asset.items())},
        "mismatches": mismatches,
        "mismatches_listed": len(mismatches),
    }


# --- events ------------------------------------------------------------------------------------


def summarize_events(events: list[dict], assets: list[str]) -> dict[str, Any]:
    kinds = Counter(str(event.get("event")) for event in events)
    disconnects = Counter(str(event.get("reason")) for event in events if event.get("event") == "disconnected")
    failures = Counter(str(event.get("error")) for event in events if event.get("event") == "connect_failed")
    resync_reasons: Counter = Counter()
    gap_by_asset: Counter = Counter({asset: 0 for asset in assets})
    resync_by_asset: dict[str, Counter] = defaultdict(Counter)
    for event in events:
        if event.get("event") != "resync":
            continue
        reason = str(event.get("reason"))
        asset = str(event.get("asset"))
        resync_reasons[reason] += 1
        resync_by_asset[asset][reason] += 1
        if not reason.startswith("disconnect_"):
            gap_by_asset[asset] += 1
    connects = kinds["connected"]
    return {
        "connects": connects,
        "connect_failures": kinds["connect_failed"],
        "connect_failure_errors": dict(failures),
        "reconnects": max(0, connects - 1),
        "reconnect_attempts": max(0, connects + kinds["connect_failed"] - 1),
        "disconnects": dict(disconnects),
        "resyncs_by_reason": dict(sorted(resync_reasons.items())),
        "gap_resyncs_total": sum(gap_by_asset.values()),
        "gap_resyncs_by_asset": dict(gap_by_asset),
        "resyncs_by_asset": {asset: dict(counts) for asset, counts in sorted(resync_by_asset.items())},
        "resubscribes": kinds["resubscribe"],
        "rest_errors": kinds["rest_error"],
        "end": next((event.get("ended_by") for event in reversed(events) if event.get("event") == "end"), None),
        "event_counts": dict(sorted(kinds.items())),
    }


# --- report ------------------------------------------------------------------------------------


CRITERIA = {
    "run_completed": "manifest status completed, exit 0, ended by its deadline (not a signal)",
    "run_duration": f"elapsed >= M1_MIN_RUN_S ({M1_MIN_RUN_S:g} s)",
    "risk_pinned": "config/risk.toml sha256 equals the pin at start and end, live_enabled false at both",
    "integrity": "every listed file present, no unreadable line, no unparseable row time",
    "coverage": f"every asset fresh (age <= MAX_BOOK_AGE_S {MAX_BOOK_AGE_S:g} s) for >= M1_MIN_COVERAGE ({M1_MIN_COVERAGE:g}) of the run",
    "book_age_p99": f"every asset's p99 book age <= MAX_BOOK_AGE_S ({MAX_BOOK_AGE_S:g} s)",
    "reconnects": f"connect attempts after the first <= M1_MAX_RECONNECTS ({M1_MAX_RECONNECTS})",
    "gap_resyncs": f"non-disconnect resyncs per asset <= M1_MAX_GAP_RESYNCS_PER_ASSET ({M1_MAX_GAP_RESYNCS_PER_ASSET})",
    "rest_matched": f"REST samples compared >= M1_MIN_REST_MATCHED ({M1_MIN_REST_MATCHED})",
    "rest_top_mismatch": f"best bid/ask mismatch rate <= M1_MAX_TOP_MISMATCH_RATE ({M1_MAX_TOP_MISMATCH_RATE:g})",
    "rest_unmatched": f"REST samples without a WS row within M1_REST_MATCH_WINDOW_S ({M1_REST_MATCH_WINDOW_S:g} s) "
                      f"<= M1_MAX_REST_UNMATCHED_RATE ({M1_MAX_REST_UNMATCHED_RATE:g})",
}


def build_report(run_dir: Path) -> dict[str, Any]:
    manifest_path = run_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise FileNotFoundError(f"{manifest_path}: {type(exc).__name__}") from exc
    if not isinstance(manifest, dict) or manifest.get("schema") != RUN_SCHEMA:
        raise ValueError(f"{manifest_path}: not an {RUN_SCHEMA} manifest")
    assets = [str(item.get("token_id")) for item in manifest.get("assets") or [] if isinstance(item, dict)]
    files = manifest.get("files") if isinstance(manifest.get("files"), dict) else {}
    streams = {}
    rows_by_stream = {}
    for name in STREAMS:
        rows_by_stream[name], streams[name] = read_stream(run_dir, name, files.get(name))
    start_us, end_us = to_us(manifest.get("started_at")), to_us(manifest.get("ended_at"))
    window_ok = start_us is not None and end_us is not None and end_us >= start_us
    ws_by_asset: dict[str, list[dict]] = defaultdict(list)
    for row in rows_by_stream["books"]:
        ws_by_asset[str(row.get("token_id"))].append(row)
    per_asset = {}
    step_us = int(M1_REPORT_STEP_S * 1e6)
    for asset in assets:
        if window_ok:
            per_asset[asset] = timeline(ws_by_asset.get(asset, []), start_us, end_us, step_us, int(MAX_BOOK_AGE_S * 1e6))
        else:
            per_asset[asset] = {"points": 0, "coverage": 0.0, "no_book_share": 1.0,
                                "age_s": {"p50": None, "p90": None, "p99": None, "max": None},
                                "rows": len(ws_by_asset.get(asset, [])), "rows_bad_ts": 0}
        meta = next((item for item in manifest["assets"] if isinstance(item, dict) and str(item.get("token_id")) == asset), {})
        per_asset[asset].update({"market_id": meta.get("market_id"), "outcome": meta.get("outcome"),
                                 "question": meta.get("question")})
    events = summarize_events(rows_by_stream["events"], assets)
    rest = compare_rest(rows_by_stream["rest"], ws_by_asset, int(M1_REST_MATCH_WINDOW_S * 1e6))
    risk = manifest.get("risk") if isinstance(manifest.get("risk"), dict) else {}
    counts = manifest.get("counts") if isinstance(manifest.get("counts"), dict) else {}
    elapsed = manifest.get("elapsed_s")
    elapsed = float(elapsed) if isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool) else None

    def risk_ok(side: str) -> bool:
        state = risk.get(side)
        return isinstance(state, dict) and state.get("sha256") == PINNED_RISK_SHA256 and state.get("live_enabled") is False

    rows_bad_ts = sum(item["rows_bad_ts"] for item in per_asset.values())
    unknown_assets = sorted(set(ws_by_asset) - set(assets))
    integrity_problems = []
    for name, info in streams.items():
        if info["unreadable_lines"]:
            integrity_problems.append(f"{name}: {info['unreadable_lines']} unreadable lines")
        if info["missing_files"]:
            integrity_problems.append(f"{name}: missing {info['missing_files']}")
    if rows_bad_ts:
        integrity_problems.append(f"books: {rows_bad_ts} rows without a usable ts")
    if unknown_assets:
        integrity_problems.append(f"books: rows for assets not in the manifest {unknown_assets}")
    for name, key in (("books", "book_rows"), ("events", "events"), ("rest", "rest_rows")):
        if isinstance(counts.get(key), int) and counts[key] != streams[name]["rows"]:
            integrity_problems.append(f"{name}: manifest says {counts[key]} rows, read {streams[name]['rows']}")
    if not window_ok:
        integrity_problems.append("manifest has no usable started_at/ended_at (run unfinished?)")

    coverages = {asset: item["coverage"] for asset, item in per_asset.items()}
    p99s = {asset: item["age_s"]["p99"] for asset, item in per_asset.items()}
    results = {
        "run_completed": manifest.get("status") == "completed" and manifest.get("exit_code") == 0
        and counts.get("ended_by") == "deadline" and events["end"] == "deadline",
        "run_duration": elapsed is not None and elapsed >= M1_MIN_RUN_S,
        "risk_pinned": risk.get("pinned_sha256") == PINNED_RISK_SHA256 and risk_ok("start") and risk_ok("end"),
        "integrity": not integrity_problems,
        "coverage": bool(assets) and all(value >= M1_MIN_COVERAGE for value in coverages.values()),
        "book_age_p99": bool(assets) and all(value is not None and value <= MAX_BOOK_AGE_S for value in p99s.values()),
        "reconnects": events["reconnect_attempts"] <= M1_MAX_RECONNECTS,
        "gap_resyncs": all(value <= M1_MAX_GAP_RESYNCS_PER_ASSET for value in events["gap_resyncs_by_asset"].values()),
        "rest_matched": rest["matched"] >= M1_MIN_REST_MATCHED,
        "rest_top_mismatch": rest["mismatch_rate"] is not None and rest["mismatch_rate"] <= M1_MAX_TOP_MISMATCH_RATE,
        "rest_unmatched": rest["unmatched_rate"] is not None and rest["unmatched_rate"] <= M1_MAX_REST_UNMATCHED_RATE,
    }
    git = manifest.get("git") if isinstance(manifest.get("git"), dict) else {}
    warnings = []
    if git.get("dirty"):
        warnings.append("the git tree was dirty at start (tracked files modified)")
    if not git.get("sha"):
        warnings.append("git sha unknown")
    for name, info in streams.items():
        if info["unlisted_files"]:
            warnings.append(f"{name}: files on disk not in the manifest {info['unlisted_files']}")
    return {
        "schema": SCHEMA,
        "run_dir": str(run_dir),
        "verdict": "PASS" if all(results.values()) else "FAIL",
        "criteria": {name: {"pass": ok, "rule": CRITERIA[name]} for name, ok in results.items()},
        "failed": [name for name, ok in results.items() if not ok],
        "integrity_problems": integrity_problems,
        "warnings": warnings,
        "run": {key: manifest.get(key) for key in ("status", "started_at", "ended_at", "elapsed_s", "max_runtime_s",
                                                  "exit_code", "stop_signal")},
        "version": manifest.get("version"),
        "git": git,
        "risk": risk,
        "thresholds": {"MAX_BOOK_AGE_S": MAX_BOOK_AGE_S, "M1_MIN_RUN_S": M1_MIN_RUN_S, "M1_MIN_COVERAGE": M1_MIN_COVERAGE,
                       "M1_MAX_RECONNECTS": M1_MAX_RECONNECTS, "M1_MAX_GAP_RESYNCS_PER_ASSET": M1_MAX_GAP_RESYNCS_PER_ASSET,
                       "M1_REST_MATCH_WINDOW_S": M1_REST_MATCH_WINDOW_S, "M1_MIN_REST_MATCHED": M1_MIN_REST_MATCHED,
                       "M1_MAX_TOP_MISMATCH_RATE": M1_MAX_TOP_MISMATCH_RATE,
                       "M1_MAX_REST_UNMATCHED_RATE": M1_MAX_REST_UNMATCHED_RATE, "M1_REPORT_STEP_S": M1_REPORT_STEP_S},
        "streams": streams,
        "assets": per_asset,
        "events": events,
        "rest_vs_ws": rest,
    }


def _fmt(value: object, digits: int = 1) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def render_markdown(report: dict[str, Any]) -> str:
    run = report["run"]
    lines = [
        f"# M1 acceptance report: {report['verdict']}",
        "",
        f"Run dir `{report['run_dir']}`; status {run.get('status')}, exit {run.get('exit_code')}, "
        f"{run.get('started_at')} .. {run.get('ended_at')} ({_fmt(run.get('elapsed_s'))} s).",
        f"git {report['git'].get('sha')} (dirty: {report['git'].get('dirty')}); "
        f"risk sha start {((report['risk'].get('start') or {}).get('sha256') or '-')[:12]}, "
        f"end {((report['risk'].get('end') or {}).get('sha256') or '-')[:12]}; versions {report.get('version')}.",
        "",
        "## Criteria (M2 pending values from src/thresholds.py)",
        "",
        "| Criterion | Result | Rule |",
        "| --- | --- | --- |",
    ]
    lines += [f"| {name} | {'PASS' if item['pass'] else 'FAIL'} | {item['rule']} |" for name, item in report["criteria"].items()]
    lines += ["", "## Coverage and book age per asset", "",
              "| Asset | Market | Outcome | Coverage | No book | p50 s | p90 s | p99 s | max s | Rows | Gap resyncs |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    gaps = report["events"]["gap_resyncs_by_asset"]
    for asset, item in report["assets"].items():
        age = item["age_s"]
        lines.append(f"| `{asset[:16]}...` | {item.get('market_id')} | {item.get('outcome')} | {item['coverage']:.4f} | "
                     f"{item['no_book_share']:.4f} | {_fmt(age['p50'])} | {_fmt(age['p90'])} | {_fmt(age['p99'])} | "
                     f"{_fmt(age['max'])} | {item['rows']} | {gaps.get(asset, 0)} |")
    events = report["events"]
    lines += ["", "## Connection and resyncs", "",
              f"- connects {events['connects']}, reconnects {events['reconnects']}, connect failures "
              f"{events['connect_failures']} {events['connect_failure_errors']}, reconnect attempts {events['reconnect_attempts']}",
              f"- disconnects by reason: {events['disconnects']}",
              f"- resyncs by reason: {events['resyncs_by_reason']} (gap/invalid total {events['gap_resyncs_total']})",
              f"- resubscribes {events['resubscribes']}, REST errors {events['rest_errors']}, run end: {events['end']}"]
    rest = report["rest_vs_ws"]
    lines += ["", "## REST /book vs WS", "",
              f"- samples {rest['samples']}, compared {rest['matched']}, mismatched {rest['mismatched']} "
              f"(rate {_fmt(rest['mismatch_rate'], 4)}), unmatched {rest['unmatched']} "
              f"(rate {_fmt(rest['unmatched_rate'], 4)}) {rest['unmatched_reasons']}"]
    if rest["mismatches"]:
        lines += ["", "| Token | REST book_ts | WS book_ts | dt s | REST bid | WS bid | REST ask | WS ask |",
                  "| --- | --- | --- | --- | --- | --- | --- | --- |"]
        lines += [f"| `{item['token_id'][:16]}...` | {item['rest_book_ts']} | {item['ws_book_ts']} | {item['dt_s']} | "
                  f"{item['rest_best_bid']} | {item['ws_best_bid']} | {item['rest_best_ask']} | {item['ws_best_ask']} |"
                  for item in rest["mismatches"]]
        if rest["mismatched"] > rest["mismatches_listed"]:
            lines.append(f"\n({rest['mismatched'] - rest['mismatches_listed']} more not listed)")
    lines += ["", "## Integrity", ""]
    lines += [f"- {name}: {info['rows']} rows in {len(info['files'])} files, {info['unreadable_lines']} unreadable lines"
              for name, info in report["streams"].items()]
    lines += [f"- PROBLEM: {item}" for item in report["integrity_problems"]]
    lines += [f"- warning: {item}" for item in report["warnings"]]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.m1_report", description="Offline M1 acceptance report for an m1_run directory.",
                                     epilog="Exit codes: 0 PASS; 1 FAIL; 2 usage error (no run dir or no m1_run manifest).")
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--out-md", type=Path, default=None, help="default RUN_DIR/report.md")
    parser.add_argument("--out-json", type=Path, default=None, help="default RUN_DIR/report.json")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return EXIT_USAGE if exc.code else EXIT_PASS
    if not args.run_dir.is_dir():
        print(f"m1_report: usage error: {args.run_dir} is not a directory", file=sys.stderr)
        return EXIT_USAGE
    try:
        report = build_report(args.run_dir)
    except (FileNotFoundError, ValueError) as exc:
        print(f"m1_report: usage error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    out_md = args.out_md or args.run_dir / "report.md"
    out_json = args.out_json or args.run_dir / "report.json"
    out_json.write_text(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + "\n", encoding="utf-8")
    out_md.write_text(render_markdown(report), encoding="utf-8")
    print(f"m1_report {report['verdict']} failed={','.join(report['failed']) or '-'} md={out_md} json={out_json}")
    return EXIT_PASS if report["verdict"] == "PASS" else EXIT_FAIL


if __name__ == "__main__":
    raise SystemExit(main())
