"""M3 step 4 (Task BB): structured Reflector proposals from the calibration report. Proposals only.

Reads one ``reports/calibration/<UTC date>/report.json`` (src/calibration.py, Task AZ) read-only, applies the
tightening rules of Poly反思's M3 calibration design section 4, and writes each proposal three ways:

- ``playbook/proposals.jsonl``: one structured row per proposal (schema below, ``PROPOSAL_FIELDS``);
- ``playbook/proposals.md``: a ``## <UTC ts>`` block through ``reflector.propose`` (so the daily cap
  ``REFLECTOR_MAX_PER_UTC_DAY`` counts every Reflector proposal, structured or not);
- the bus: ``Reflector -> Operator``, type ``proposal``, ``exchange_action: none``, the row under ``proposal``.

Hard limits (tested):
- Nothing here applies a proposal. No write to config/ (risk.toml, any strategy switch; config/strategies.toml
  stays nonexistent), to any .toml or to src/thresholds.py; no order, no exchange call, no network.
- A section whose status is ``insufficient`` yields no proposal. Proposals need intervals: a rule only fires
  when the whole interval is past its threshold.
- At most ``REFLECTOR_MAX_PER_UTC_DAY`` per UTC day (disable before param_change; the rest are deferred).
- A proposal that would loosen a risk check is marked ``loosens_risk_check: true`` with
  ``approval.required_from: KEY``; the rules here only tighten, and one that loosens without that marking is
  refused. Every proposal is ``scope.env: paper``, ``live_requires_human_approval: true``.
- A target that is already open (proposed/approved) is not proposed again; a rejected one waits
  ``REFLECTOR_REJECT_COOLDOWN_DAYS`` unless the sample grew by ``REFLECTOR_REJECT_NEW_SAMPLES``.
- Fail closed: a missing report (exit 3), a corrupt or incomplete one (exit 4) or a corrupt proposals.jsonl
  (exit 5) writes no proposal. Exit codes: docs/EXIT_CODES.md.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import reflector  # noqa: E402
from src.podlib import RISK_PATH  # noqa: E402
from src.thresholds import (  # noqa: E402
    MIN_EDGE,
    REFLECTOR_DISABLE_REALIZATION_MAX,
    REFLECTOR_DISABLE_SLOPE_CI_HIGH_MAX,
    REFLECTOR_DISABLE_SLOPE_MAX,
    REFLECTOR_ECE_MAX,
    REFLECTOR_MAX_PER_UTC_DAY,
    REFLECTOR_MIN_EDGE_STEP,
    REFLECTOR_REJECT_COOLDOWN_DAYS,
    REFLECTOR_REJECT_NEW_SAMPLES,
    REFLECTOR_TIGHTEN_SLOPE_CI_HIGH_MAX,
    REFLECTOR_TIGHTEN_SLOPE_MAX,
)

REPORTS_DIR = ROOT / "reports" / "calibration"
JSONL_PATH = ROOT / "playbook" / "proposals.jsonl"
MD_PATH = reflector.PROPOSALS_PATH
SCHEMA_VERSION = 1
REPORT_SCHEMA_VERSION = 1  # src/calibration.py SCHEMA_VERSION this reader understands

EXIT_OK = 0  # ran; wrote zero or more proposals (zero = observations only)
EXIT_USAGE = 2  # usage error, including an output path in config/, a .toml or src/thresholds.py
EXIT_REPORT_MISSING = 3  # no report.json found; nothing written
EXIT_REPORT_CORRUPT = 4  # report.json unreadable, not JSON, incomplete or off its schema; nothing written
EXIT_PROPOSALS_CORRUPT = 5  # playbook/proposals.jsonl torn or off its schema; nothing written
EXIT_WRITE_FAILED = 6  # writing a proposal failed, or risk.toml changed during the run

PROPOSAL_FIELDS = ("schema_version", "proposal_id", "created_at", "type", "target", "current_value",
                   "proposed_value", "direction", "loosens_risk_check", "approval", "evidence", "confidence",
                   "rationale", "expected_impact", "risk", "rollback", "scope", "status", "exchange_action",
                   "applied_by_code")
TYPES = ("disable", "param_change")
STATES = ("proposed", "approved", "rejected", "applied")
# Which way is tighter for each key a rule may target. Unknown keys count as loosening (fail closed).
# Files a proposal may *describe* a change to (a human applies it in a separate task). Never config/.
TARGET_FILES = (None, "src/thresholds.py")
TIGHTER_WHEN = {"MIN_EDGE": "higher", "enabled": "disabled"}


class ReportError(Exception):
    """report.json cannot be trusted (unreadable, incomplete, or off its schema)."""


class ProposalsCorrupt(Exception):
    """playbook/proposals.jsonl cannot be read back exactly."""


# ------------------------------------------------------------------ reading the report (strict)

def _reject_constant(name: str) -> float:
    raise ValueError(f"non-finite JSON constant {name}")


def load_report(path: Path) -> tuple[dict[str, Any], str]:
    """(report, sha256). Raises FileNotFoundError when absent, ReportError when it cannot be trusted."""
    data = Path(path).read_bytes()
    try:
        report = json.loads(data.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ReportError(f"not JSON: {exc}") from exc
    check_report(report)
    return report, hashlib.sha256(data).hexdigest()


def _get(node: object, *keys: str) -> Any:
    for key in keys:
        if not isinstance(node, Mapping) or key not in node:
            raise ReportError(f"missing {'.'.join(keys)}")
        node = node[key]
    return node


def _num(node: object, *keys: str) -> float | None:
    value = _get(node, *keys)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ReportError(f"{'.'.join(keys)} is not a finite number")
    return float(value)


def _interval(node: Mapping[str, Any], key: str) -> list[float] | None:
    """An optional [low, high] interval; absent or None = no interval."""
    if key not in node or node[key] is None:
        return None
    value = node[key]
    if (not isinstance(value, list) or len(value) != 2
            or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in value)
            or value[0] > value[1]):
        raise ReportError(f"{key} is not a [low, high] interval")
    return [float(value[0]), float(value[1])]


def check_report(report: object) -> None:
    if not isinstance(report, dict):
        raise ReportError("not a JSON object")
    if report.get("schema_version") != REPORT_SCHEMA_VERSION:
        raise ReportError(f"schema_version {report.get('schema_version')!r} is not {REPORT_SCHEMA_VERSION}")
    if report.get("complete") is not True:
        raise ReportError("report is not marked complete")
    if not isinstance(report.get("generated_at"), str) or _parse_ts(report["generated_at"]) is None:
        raise ReportError("generated_at is not a zoned timestamp")
    for section in ("forecasts", "trades"):
        metrics = _get(report, section, "metrics")
        status = _get(metrics, "status")
        verdict = _get(metrics, "verdict")
        if status not in ("sufficient", "insufficient"):
            raise ReportError(f"{section}.metrics.status {status!r}")
        if (status == "insufficient") != (verdict == "insufficient"):
            raise ReportError(f"{section}: status and verdict disagree")
        if status == "sufficient" and not isinstance(verdict, dict):
            raise ReportError(f"{section}: a sufficient section needs a verdict object")
        for key in ("n", "markets", "resolution_days"):
            value = _get(metrics, key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ReportError(f"{section}.metrics.{key} is not a count")
        groups = _get(report, section, "by_strategy")
        if not isinstance(groups, dict):
            raise ReportError(f"{section}.by_strategy is not an object")
    if not isinstance(_get(report, "inputs"), dict) or not isinstance(_get(report, "thresholds"), dict):
        raise ReportError("inputs / thresholds missing")


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str) or "T" not in value:
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def latest_report(reports_dir: Path = REPORTS_DIR) -> Path | None:
    """The newest reports/calibration/<YYYY-MM-DD>/report.json (by folder name), or None."""
    if not reports_dir.is_dir():
        return None
    days = sorted(p for p in reports_dir.iterdir() if p.is_dir() and len(p.name) == 10 and p.name[4] == "-")
    for day in reversed(days):
        if (day / "report.json").is_file():
            return day / "report.json"
    return None


# ------------------------------------------------------------------ rules (pure)

def _single_strategy(report: Mapping[str, Any], section: str) -> str | None:
    groups = report[section]["by_strategy"]
    return next(iter(groups)) if len(groups) == 1 and next(iter(groups)) != "(unknown)" else None


def _evidence(report: Mapping[str, Any], report_ref: Mapping[str, Any], section: str,
              refs: Sequence[str], metrics: Mapping[str, Any]) -> dict[str, Any]:
    m = report[section]["metrics"]
    inputs = report["inputs"]
    return {
        "report": dict(report_ref),
        "sections": list(refs),
        "sample": {"section": section, "n": m["n"], "n_markets": m["markets"],
                   "resolution_days": m["resolution_days"]},
        "metrics": dict(metrics),
        "data_versions": {name: (inputs.get(name) or {}).get("sha256") if isinstance(inputs.get(name), dict)
                          else None for name in ("forecasts", "resolutions", "ledger")}
        | {"bootstrap_seed": report["thresholds"].get("bootstrap_seed")},
    }


def candidates(report: Mapping[str, Any], report_ref: Mapping[str, Any]) -> tuple[list[dict], list[str]]:
    """(draft proposals in priority order, observations). Drafts lack id/created_at/status."""
    drafts: list[dict[str, Any]] = []
    notes: list[str] = []
    a = report["trades"]["metrics"]
    b = report["forecasts"]["metrics"]
    level = report["thresholds"].get("ci_level")

    # (a) disable a strategy: trade sample (A) triggers, then forecast sample (B) slope.
    disable: dict[str, dict[str, Any]] = {}
    if a["status"] != "sufficient":
        notes.append("trades: insufficient, no proposal")
    else:
        strategy = _single_strategy(report, "trades")
        skill_ci = _interval(a, "brier_skill_ci")
        realized_ci = _interval(a, "realized_usd_ci")
        realization = _num(a, "realization")
        reasons, refs, intervals = [], [], {}
        if strategy is None:
            notes.append("trades: several strategies and no per-strategy interval in the report; no disable")
        else:
            if skill_ci is not None and skill_ci[1] < 0:
                reasons.append("Brier skill below 0 with its whole interval below 0: steadily worse than the market")
                refs.append("trades.metrics.brier_skill_ci")
                intervals["brier_skill"] = skill_ci
            if (realized_ci is not None and realized_ci[1] < 0 and realization is not None
                    and realization < REFLECTOR_DISABLE_REALIZATION_MAX):
                reasons.append(f"realized paper P&L interval below 0 and realization {realization:.3f} < "
                               f"{REFLECTOR_DISABLE_REALIZATION_MAX}")
                refs.append("trades.metrics.realized_usd_ci")
                intervals["realized_usd"] = realized_ci
            if reasons:
                disable[strategy] = {"section": "trades", "reasons": reasons, "refs": refs, "intervals": intervals,
                                     "metrics": {"brier_model": _num(a, "brier_model"),
                                                 "brier_market": _num(a, "brier_market"),
                                                 "delta_brier": _num(a, "delta_brier"),
                                                 "brier_skill": _num(a, "brier_skill"),
                                                 "expected_usd": _num(a, "expected_usd"),
                                                 "realized_usd": _num(a, "realized_usd"),
                                                 "realization": realization}}
    b_strategy = None
    slope = slope_ci = None
    if b["status"] != "sufficient":
        notes.append("forecasts: insufficient, no proposal")
    else:
        b_strategy = _single_strategy(report, "forecasts")
        calibration = _get(b, "calibration")
        slope = _num(calibration, "slope")
        slope_ci = _interval(calibration, "slope_ci")
        if (b_strategy is not None and slope is not None and slope_ci is not None
                and slope < REFLECTOR_DISABLE_SLOPE_MAX and slope_ci[1] < REFLECTOR_DISABLE_SLOPE_CI_HIGH_MAX):
            entry = disable.setdefault(b_strategy, {"section": "forecasts", "reasons": [], "refs": [],
                                                    "intervals": {}, "metrics": {}})
            entry["reasons"].append(f"calibration slope {slope:.3f} < {REFLECTOR_DISABLE_SLOPE_MAX} with interval "
                                    f"upper bound < {REFLECTOR_DISABLE_SLOPE_CI_HIGH_MAX}: severely overconfident")
            entry["refs"].append("forecasts.metrics.calibration.slope_ci")
            entry["intervals"]["calibration_slope"] = slope_ci
            entry["metrics"]["calibration_slope"] = slope
    for strategy, entry in disable.items():
        drafts.append({
            "type": "disable",
            "target": {"strategy": strategy, "key": "enabled", "file": None},
            "current_value": "enabled", "proposed_value": "disabled",
            "evidence": _evidence(report, report_ref, entry["section"], entry["refs"], entry["metrics"]),
            "confidence": {"ci_level": level, "intervals": entry["intervals"]},
            "rationale": "; ".join(entry["reasons"]) + ".",
            "expected_impact": f"No new paper entries from {strategy}; its open paper positions run to settlement.",
            "risk": "Small samples can be noise; a later report with the interval no longer past the threshold "
                    "says this was wrong.",
            "rollback": f"Re-enable {strategy} (no strategy switch exists in code; the Operator decides how).",
        })

    # (b) tighten parameters (forecast sample B). Not for a strategy that is being disabled.
    if b["status"] == "sufficient" and b_strategy not in disable:
        scope_strategy = b_strategy or "all"
        proposed = round(MIN_EDGE + REFLECTOR_MIN_EDGE_STEP, 10)
        if (slope is not None and slope_ci is not None and REFLECTOR_DISABLE_SLOPE_MAX <= slope
                < REFLECTOR_TIGHTEN_SLOPE_MAX and slope_ci[1] < REFLECTOR_TIGHTEN_SLOPE_CI_HIGH_MAX):
            drafts.append({
                "type": "param_change",
                "target": {"strategy": scope_strategy, "key": "MIN_EDGE", "file": "src/thresholds.py"},
                "current_value": MIN_EDGE, "proposed_value": proposed,
                "evidence": _evidence(report, report_ref, "forecasts", ["forecasts.metrics.calibration.slope_ci"],
                                      {"calibration_slope": slope, "brier": _num(b, "brier"),
                                       "ece": _num(_get(b, "reliability"), "ece")}),
                "confidence": {"ci_level": level, "intervals": {"calibration_slope": slope_ci}},
                "rationale": f"Calibration slope {slope:.3f} in [{REFLECTOR_DISABLE_SLOPE_MAX}, "
                             f"{REFLECTOR_TIGHTEN_SLOPE_MAX}) with interval upper bound < "
                             f"{REFLECTOR_TIGHTEN_SLOPE_CI_HIGH_MAX}: the forecaster is overconfident.",
                "expected_impact": "Fewer low-edge paper entries; slower settled-sample growth.",
                "risk": "If the next sample shows a slope interval reaching 1, this was noise.",
                "rollback": f"Restore MIN_EDGE = {MIN_EDGE} (revert the PR that applies it).",
            })
        reliability = _get(b, "reliability")
        ece = _num(reliability, "ece")
        bins = _get(reliability, "bins")
        if not isinstance(bins, list):
            raise ReportError("forecasts.metrics.reliability.bins is not a list")
        flagged = []
        for item in bins:
            if not isinstance(item, Mapping):
                raise ReportError("a reliability bin is not an object")
            if item.get("mean_p_outside_wilson") is True:
                gap = abs(_num(item, "mean_p") - _num(item, "hit_rate"))  # type: ignore[operator]
                flagged.append((gap, item))
        if ece is not None and ece > REFLECTOR_ECE_MAX and flagged:
            gap, worst = max(flagged, key=lambda pair: pair[0])
            band = worst.get("range")
            drafts.append({
                "type": "param_change",
                "target": {"strategy": scope_strategy, "key": "MIN_EDGE", "file": "src/thresholds.py",
                           "price_band": band},
                "current_value": MIN_EDGE, "proposed_value": proposed,
                "evidence": _evidence(report, report_ref, "forecasts", ["forecasts.metrics.reliability"],
                                      {"ece": ece, "worst_bin": {"range": band, "n": worst.get("n"),
                                                                 "mean_p": worst.get("mean_p"),
                                                                 "hit_rate": worst.get("hit_rate"),
                                                                 "wilson": worst.get("wilson")}}),
                "confidence": {"ci_level": level, "intervals": {"worst_bin_wilson": worst.get("wilson")}},
                "rationale": f"ECE {ece:.3f} > {REFLECTOR_ECE_MAX} and the bin {band} has its mean p outside its "
                             f"Wilson interval (gap {gap:.3f}).",
                "expected_impact": "Fewer paper entries in that price band only.",
                "risk": "One bin of a small sample; a band-specific MIN_EDGE needs a new parameter.",
                "rollback": f"Drop the band rule; MIN_EDGE stays {MIN_EDGE} elsewhere.",
            })
    if not drafts and not notes:
        notes.append("all intervals cross their thresholds; observations only")
    return drafts, notes


def direction(target: Mapping[str, Any], current: object, proposed: object) -> str:
    """tighten | loosen. Unknown keys and non-comparable values count as loosen (fail closed)."""
    rule = TIGHTER_WHEN.get(str(target.get("key")))
    if rule == "higher" and isinstance(current, (int, float)) and isinstance(proposed, (int, float)):
        return "tighten" if proposed > current else "loosen"
    if rule == "disabled":
        return "tighten" if proposed == "disabled" else "loosen"
    return "loosen"


def finish(draft: Mapping[str, Any], proposal_id: str, created_at: str) -> dict[str, Any]:
    way = direction(draft["target"], draft["current_value"], draft["proposed_value"])
    loosens = way == "loosen"
    row = {
        "schema_version": SCHEMA_VERSION, "proposal_id": proposal_id, "created_at": created_at,
        "type": draft["type"], "target": draft["target"], "current_value": draft["current_value"],
        "proposed_value": draft["proposed_value"], "direction": way, "loosens_risk_check": loosens,
        "approval": {"required": True, "required_from": "KEY" if loosens else "Operator",
                     "note": ("Loosens a risk check: only KEY (the user) can approve it." if loosens else
                              "Tightening; still a human decision. Never applied by code.")},
        "evidence": draft["evidence"], "confidence": draft["confidence"], "rationale": draft["rationale"],
        "expected_impact": draft["expected_impact"], "risk": draft["risk"], "rollback": draft["rollback"],
        "scope": {"env": "paper", "live_requires_human_approval": True},
        "status": {"state": "proposed", "approver": None, "decided_at": None, "applied_at": None},
        "exchange_action": "none", "applied_by_code": False,
    }
    return row


def validate(row: Mapping[str, Any]) -> list[str]:
    """Problems that make a proposal unwritable ([] = ok)."""
    problems = []
    if tuple(row) != PROPOSAL_FIELDS:
        problems.append("keys differ from PROPOSAL_FIELDS")
        return problems
    if row["type"] not in TYPES:
        problems.append(f"type {row['type']!r}")
    if row["exchange_action"] != "none" or row["applied_by_code"] is not False:
        problems.append("a proposal never acts on an exchange and is never applied by code")
    if row["scope"] != {"env": "paper", "live_requires_human_approval": True}:
        problems.append("scope must be paper with live_requires_human_approval")
    target = row["target"]
    if not isinstance(target, Mapping) or target.get("file") not in TARGET_FILES:
        problems.append("target file is not one a proposal may describe (no config/, no .toml)")
    way = direction(target, row["current_value"], row["proposed_value"])
    if way != row["direction"] or (way == "loosen") != (row["loosens_risk_check"] is True):
        problems.append("direction / loosens_risk_check do not match the values")
    if row["loosens_risk_check"] and (row["approval"].get("required_from") != "KEY"
                                      or row["approval"].get("required") is not True):
        problems.append("a loosening proposal must require KEY's approval")
    if not row["evidence"].get("sections") or row["evidence"]["sample"]["n"] <= 0:
        problems.append("evidence needs report sections and a sample count")
    return problems


# ------------------------------------------------------------------ proposals ledger

def read_proposals(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    data = path.read_bytes()
    if data and not data.endswith(b"\n"):
        raise ProposalsCorrupt(f"{path.name}: torn last line")
    rows = []
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProposalsCorrupt(f"{path.name}: not UTF-8") from exc
    for number, line in enumerate(text.split("\n")[:-1] if text else [], 1):
        try:
            row = json.loads(line, parse_constant=_reject_constant)
        except ValueError as exc:
            raise ProposalsCorrupt(f"{path.name}:{number}: not JSON") from exc
        if (not isinstance(row, dict) or not isinstance(row.get("target"), dict)
                or not isinstance(row.get("status"), dict) or row["status"].get("state") not in STATES
                or not isinstance(row.get("proposal_id"), str)):
            raise ProposalsCorrupt(f"{path.name}:{number}: off the proposal schema")
        rows.append(row)
    return rows


def _target_key(target: Mapping[str, Any]) -> str:
    return json.dumps(target, sort_keys=True, ensure_ascii=False)


def blocked(draft: Mapping[str, Any], existing: Sequence[Mapping[str, Any]], now: datetime) -> str | None:
    key = _target_key(draft["target"])
    n_now = draft["evidence"]["sample"]["n"]
    for row in existing:
        if _target_key(row["target"]) != key:
            continue
        state = row["status"]["state"]
        if state in ("proposed", "approved"):
            return "already_open"
        if state == "rejected":
            decided = _parse_ts(row["status"].get("decided_at")) or _parse_ts(row.get("created_at"))
            n_then = ((row.get("evidence") or {}).get("sample") or {}).get("n") or 0
            if (decided is not None and now - decided < timedelta(days=REFLECTOR_REJECT_COOLDOWN_DAYS)
                    and n_now - n_then < REFLECTOR_REJECT_NEW_SAMPLES):
                return "rejected_recently"
    return None


def _append_line(path: Path, row: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(row, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)
    try:
        if os.write(fd, payload) != len(payload):
            raise OSError("short write")
        os.fsync(fd)
    finally:
        os.close(fd)


def summary(row: Mapping[str, Any]) -> str:
    t = row["target"]
    band = f" band {t['price_band']}" if t.get("price_band") else ""
    return (f"proposal {row['proposal_id']} {row['type']} {t.get('strategy')}:{t.get('key')}{band} "
            f"{row['current_value']} -> {row['proposed_value']} ({row['direction']}; approval "
            f"{row['approval']['required_from']}). Proposal only; exchange_action none; never applied by code. "
            f"Evidence {', '.join(row['evidence']['sections'])} n={row['evidence']['sample']['n']}.")


def run(report: Mapping[str, Any], report_ref: Mapping[str, Any], *, now: str, jsonl_path: Path = JSONL_PATH,
        md_path: Path = MD_PATH, bus_path: Path | None = None) -> dict[str, Any]:
    """Write the day's proposals. Raises ProposalsCorrupt before writing anything if the ledger is bad."""
    for path in (jsonl_path, md_path):
        if reflector.refused_path(path):
            raise ValueError(f"refusing to write {path}")
    existing = read_proposals(jsonl_path)
    drafts, notes = candidates(report, report_ref)
    now_dt = _parse_ts(now)
    if now_dt is None:
        raise ValueError("now must be a zoned timestamp")
    day = now_dt.strftime("%Y-%m-%d")
    stamp = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    used = reflector._count_day(md_path.read_text(encoding="utf-8"), day) if md_path.exists() else 0
    written, skipped, deferred = [], [], []
    for draft in drafts:
        reason = blocked(draft, existing, now_dt)
        if reason:
            skipped.append({"target": draft["target"], "reason": reason})
            continue
        if used >= REFLECTOR_MAX_PER_UTC_DAY:
            deferred.append({"target": draft["target"], "reason": "daily_cap"})
            continue
        row = finish(draft, f"{day}-{used + 1:02d}", stamp)
        problems = validate(row)
        if problems:
            skipped.append({"target": draft["target"], "reason": "invalid: " + "; ".join(problems)})
            continue
        _append_line(jsonl_path, row)
        result = reflector.propose(summary(row), "", proposals_path=md_path, bus_path=bus_path, now=stamp,
                                   payload=row)
        if not result.get("accepted"):
            raise OSError(f"reflector refused {row['proposal_id']}: {result.get('reason')}")
        existing.append(row)
        written.append(row["proposal_id"])
        used += 1
    return {"written": written, "skipped": skipped, "deferred": deferred, "observations": notes}


# ------------------------------------------------------------------ CLI

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.reflector_proposals",
        description="Structured Reflector proposals from a calibration report.json. Proposals only; never applied.",
        epilog="Exit codes: 0 ran; 2 usage; 3 report missing; 4 report corrupt; 5 proposals.jsonl corrupt; "
               "6 write failed or risk.toml changed. See docs/EXIT_CODES.md.")
    parser.add_argument("--report", type=Path, default=None, help="default: newest reports/calibration/*/report.json")
    parser.add_argument("--jsonl", type=Path, default=None, help="default playbook/proposals.jsonl")
    parser.add_argument("--md", type=Path, default=None, help="default playbook/proposals.md")
    parser.add_argument("--bus", type=Path, default=None, help="default logs/bus.jsonl")
    parser.add_argument("--now", default=None, help="zoned ISO time (default: now, UTC)")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return EXIT_OK if exc.code == 0 else EXIT_USAGE
    now = args.now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if _parse_ts(now) is None:
        print("reflector_proposals: usage error: --now must be a zoned ISO timestamp", file=sys.stderr)
        return EXIT_USAGE
    jsonl_path, md_path = args.jsonl or JSONL_PATH, args.md or MD_PATH
    if any(reflector.refused_path(p) for p in (jsonl_path, md_path) + ((args.bus,) if args.bus else ())):
        print("reflector_proposals: usage error: refusing to write config/, a .toml or src/thresholds.py",
              file=sys.stderr)
        return EXIT_USAGE
    report_path = args.report or latest_report()
    if report_path is None or not Path(report_path).is_file():
        print("reflector_proposals: no calibration report.json; no proposal written", file=sys.stderr)
        return EXIT_REPORT_MISSING
    try:
        report, sha = load_report(Path(report_path))
        ref = {"path": str(report_path), "sha256": sha, "generated_at": report["generated_at"]}
        candidates(report, ref)  # every field the rules read is type-checked before anything is written
    except (OSError, ReportError) as exc:
        print(f"reflector_proposals: report cannot be trusted, no proposal written: {exc}", file=sys.stderr)
        return EXIT_REPORT_CORRUPT
    before = RISK_PATH.read_bytes()
    try:
        result = run(report, ref, now=now, jsonl_path=jsonl_path, md_path=md_path, bus_path=args.bus)
    except (ProposalsCorrupt, UnicodeDecodeError) as exc:
        print(f"reflector_proposals: proposals.jsonl corrupt, no proposal written: {exc}", file=sys.stderr)
        return EXIT_PROPOSALS_CORRUPT
    except (OSError, ValueError) as exc:
        print(f"reflector_proposals: write failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_WRITE_FAILED
    if RISK_PATH.read_bytes() != before:
        print("reflector_proposals: risk.toml changed during the run", file=sys.stderr)
        return EXIT_WRITE_FAILED
    print(f"reflector_proposals written={len(result['written'])} skipped={len(result['skipped'])} "
          f"deferred={len(result['deferred'])} observations={result['observations']} ids={result['written']}")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
