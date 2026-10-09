"""Task AL (Sentinel design gaps 1, 3, 7, 8): persisted halt state and the halt consumer.

A halt used to be a bus line nobody read. Now every halt is also a record in
logs/halt_state.json (HALT_STATE_PATH, written atomically: temp file, fsync, os.replace), and
RiskGate, paper_cycle and the Maker read it before they act:

* ``scope="market"`` blocks that market; ``scope="global"`` blocks every market.
* A halt stays active until the Operator resumes it (``python -m src.halt_store resume ...``).
  Nothing releases a halt automatically: a fresh book, a reset StaleWatch streak or a clean
  ledger never lift it.
* Fail closed: a halt state file that is unreadable, not JSON, malformed, or missing after it was
  written once (the ``.seen`` marker, as for StaleWatch) is a global halt. So is the fallback
  file logs/HALT (HALT_FALLBACK_PATH), written when a halt could not be recorded in the state
  file or on the bus. Any error while checking is a global halt too.
* ``resume`` is the only way out. Only ``from="Operator"`` may resume, with a non-empty
  ``approved_by``, root cause and postmortem, and a ``halt_ref`` naming one active halt. A
  market-level resume can never lift a global halt (and a global resume does not lift a
  market halt: the scope must match the halt). The resume is logged on the bus (type
  ``resume``) before the halt is released; if that bus write fails nothing is released.

No network, no orders, no exchange action, no edits to config/risk.toml. No new thresholds.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.podlib import LOG_DIR, bus, canonical_role, utc_now  # noqa: E402

HALT_STATE_PATH = LOG_DIR / "halt_state.json"
HALT_FALLBACK_PATH = LOG_DIR / "HALT"
STATE_VERSION = 1

SCOPE_GLOBAL = "global"
SCOPE_MARKET = "market"
SCOPES = (SCOPE_GLOBAL, SCOPE_MARKET)
OPERATOR = "Operator"

# Synthetic global halts (never stored as records): the fallback file and a bad state file.
FALLBACK_ID = "halt-file"
STATE_PROBLEM_ID = "halt-state-problem"

STATE_MISSING = "halt_state_missing"
STATE_UNREADABLE = "halt_state_unreadable"
STATE_CORRUPT = "halt_state_corrupt"
STATE_MALFORMED = "halt_state_malformed"
CHECK_FAILED = "halt_check_failed"
FALLBACK_ACTIVE = "halt_file"

PERSISTED_STATE = "state"
PERSISTED_FALLBACK = "fallback"
PERSISTED_NONE = "none"


class ResumeRejected(ValueError):
    """A resume that must not release anything. ``reason`` is a short code."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        super().__init__(f"{reason}: {detail}" if detail else reason)


def state_path(path: Path | None = None) -> Path:
    """The halt state path; the module default is read at call time (tests and the e2e gate patch it)."""
    return Path(path) if path is not None else Path(HALT_STATE_PATH)


def fallback_path(path: Path | None = None) -> Path:
    return Path(path) if path is not None else Path(HALT_FALLBACK_PATH)


def state_marker(path: Path) -> Path:
    """Written after the first successful save: a later missing file is a lost file, not a first run."""
    return path.with_name(path.name + ".seen")


# ---------------------------------------------------------------- reading

def _valid_record(record: object) -> bool:
    if not isinstance(record, dict):
        return False
    rid, scope, market = record.get("id"), record.get("scope"), record.get("market")
    if not isinstance(rid, str) or not rid or rid in (FALLBACK_ID, STATE_PROBLEM_ID):
        return False
    if scope not in SCOPES or not isinstance(market, str):
        return False
    if (scope == SCOPE_MARKET) != bool(market):
        return False
    if not isinstance(record.get("active"), bool):
        return False
    for key in ("reason", "source", "ts"):
        if not isinstance(record.get(key), str) or not record.get(key):
            return False
    if record["active"] is False and not isinstance(record.get("resolved"), dict):
        return False
    return True


def validate_state(data: object) -> list[dict[str, Any]]:
    """Return the halt records, or raise ValueError on any malformed field (fail closed)."""
    if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
        raise ValueError("halt state must be a dict with version 1")
    halts = data.get("halts")
    if not isinstance(halts, list):
        raise ValueError("halts must be a list")
    seen: set[str] = set()
    for record in halts:
        if not _valid_record(record):
            raise ValueError("bad halt record")
        if record["id"] in seen:
            raise ValueError("repeated halt id")
        seen.add(record["id"])
    return halts


def read_state(path: Path | None = None) -> tuple[list[dict[str, Any]], str | None]:
    """Return ``(records, problem)``. problem is None on a clean read or a true first run."""
    target = state_path(path)
    try:
        if not target.exists():
            return [], (STATE_MISSING if state_marker(target).exists() else None)
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return [], STATE_UNREADABLE
    try:
        data = json.loads(text)
    except ValueError:
        return [], STATE_CORRUPT
    try:
        return validate_state(data), None
    except (ValueError, TypeError, KeyError):
        return [], STATE_MALFORMED


def fallback_active(fallback: Path | None = None) -> bool:
    """True when logs/HALT exists, or when its presence cannot be checked (fail closed)."""
    try:
        os.stat(fallback_path(fallback))
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def active_halts(path: Path | None = None, fallback: Path | None = None) -> list[dict[str, Any]]:
    """Every halt now in force, the synthetic global ones (bad state file, logs/HALT) included."""
    records, problem = read_state(path)
    active = [record for record in records if record["active"]]
    if problem is not None:
        active.append({"id": STATE_PROBLEM_ID, "scope": SCOPE_GLOBAL, "market": "", "reason": problem,
                       "source": "halt_store", "active": True})
    if fallback_active(fallback):
        active.append({"id": FALLBACK_ID, "scope": SCOPE_GLOBAL, "market": "", "reason": FALLBACK_ACTIVE,
                       "source": "halt_store", "active": True})
    return active


def halt_problems(market: object, path: Path | None = None, fallback: Path | None = None) -> list[str]:
    """Veto reasons from active halts for ``market`` (empty list: no halt applies). Never raises.

    ``halt_global:<id>`` for a global halt, ``halt_market:<id>`` for this market's halt, the state
    problem name (halt_state_corrupt, ...) or ``halt_file`` for the synthetic global halts, and
    ``halt_check_failed`` when the check itself broke.
    """
    try:
        key = str(market or "")
        reasons: list[str] = []
        for record in active_halts(path, fallback):
            if record["id"] == STATE_PROBLEM_ID:
                reasons.append(record["reason"])
            elif record["id"] == FALLBACK_ID:
                reasons.append(FALLBACK_ACTIVE)
            elif record["scope"] == SCOPE_GLOBAL:
                reasons.append(f"halt_global:{record['id']}")
            elif record["market"] == key and key:
                reasons.append(f"halt_market:{record['id']}")
        return reasons
    except Exception:  # noqa: BLE001 - any failure to check is a halt (fail closed)
        return [CHECK_FAILED]


def global_halt_active(path: Path | None = None, fallback: Path | None = None) -> list[str]:
    """Reasons that block every market (no market halts). Never raises."""
    try:
        return [reason for reason in halt_problems("", path, fallback) if not reason.startswith("halt_market:")]
    except Exception:  # noqa: BLE001
        return [CHECK_FAILED]


# ---------------------------------------------------------------- writing

def _atomic_write(path: Path, records: list[dict[str, Any]]) -> None:
    """Temp file in the same folder, fsync, os.replace, then the .seen marker. Raises OSError/ValueError."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps({"version": STATE_VERSION, "halts": records}, ensure_ascii=False, sort_keys=True,
                      allow_nan=False, indent=1) + "\n"
    tmp_name = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=path.name + ".",
                                         suffix=".tmp", delete=False) as handle:
            tmp_name = handle.name
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        tmp_name = None
    finally:
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
    marker = state_marker(path)
    if not marker.exists():
        marker.write_text("1\n", encoding="utf-8")


def write_fallback(line: str, fallback: Path | None = None) -> bool:
    """Append one line to logs/HALT (its presence alone is a global halt). Never raises."""
    try:
        target = fallback_path(fallback)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(" ".join(str(line).split()) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return True
    except Exception:  # noqa: BLE001
        return False


def _halt_id(seq: int, ts: str, scope: str, market: str, source: str, reason: str) -> str:
    digest = hashlib.sha256("\x1f".join((str(seq), ts, scope, market, source, reason)).encode("utf-8"))
    return f"H{seq:04d}-{digest.hexdigest()[:10]}"


def record_halt(
    *,
    scope: str,
    market: str = "",
    reason: str,
    source: str,
    path: Path | None = None,
    fallback: Path | None = None,
    ts: str | None = None,
) -> dict[str, Any]:
    """Persist one halt and return its record (``persisted`` says where). Never raises for I/O.

    An identical active halt (same scope, market, source and reason) is returned as is. If the
    state file is bad it is left untouched as evidence (that is already a global halt) and the
    halt goes to logs/HALT; so does a failed write. ``persisted="none"`` means even logs/HALT
    failed: the caller must still treat the halt as active.
    """
    if scope not in SCOPES:
        raise ValueError("halt scope must be global or market")
    market = str(market or "")
    if scope == SCOPE_GLOBAL:
        market = ""
    elif not market:
        raise ValueError("a market halt needs a market")
    reason = " ".join(str(reason or "").split())
    source = canonical_role(str(source or "").strip())
    if not reason or not source:
        raise ValueError("halt reason and source required")
    stamp = ts or utc_now()
    target = state_path(path)
    records, problem = read_state(target)
    record: dict[str, Any] = {"scope": scope, "market": market, "reason": reason, "source": source, "ts": stamp,
                              "active": True}
    if problem is None:
        for existing in records:
            if existing["active"] and all(existing[k] == record[k] for k in ("scope", "market", "reason", "source")):
                return dict(existing, persisted=PERSISTED_STATE, duplicate=True)
        record["id"] = _halt_id(len(records) + 1, stamp, scope, market, source, reason)
        try:
            _atomic_write(target, records + [record])
            return dict(record, persisted=PERSISTED_STATE)
        except (OSError, ValueError, TypeError):
            pass
    else:
        record["id"] = _halt_id(0, stamp, scope, market, source, reason)
        record["state_problem"] = problem
    line = f"{stamp} halt id={record['id']} scope={scope} market={market or '-'} source={source} reason={reason}"
    persisted = PERSISTED_FALLBACK if write_fallback(line, fallback) else PERSISTED_NONE
    return dict(record, persisted=persisted)


# ---------------------------------------------------------------- resume (Operator only)

def _require_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ResumeRejected(f"missing_{name}")
    return " ".join(value.split())


def resume(
    *,
    from_role: str,
    scope: str,
    halt_id: str,
    approved_by: str,
    root_cause: str,
    postmortem: str,
    market: str = "",
    checks: dict[str, Any] | None = None,
    path: Path | None = None,
    fallback: Path | None = None,
    bus_path: Path | None = None,
) -> dict[str, Any]:
    """Release exactly one active halt. Raises ResumeRejected (nothing released) on any doubt.

    Order: validate, write the ``resume`` bus line (from=Operator), then release. A failed bus
    write releases nothing. Special halt ids: ``halt-file`` (logs/HALT, renamed aside so the
    evidence stays) and ``halt-state-problem`` (the bad state file is moved aside and a fresh
    state written); both are global and need ``scope="global"``.
    """
    if canonical_role(str(from_role or "")) != OPERATOR:
        raise ResumeRejected("resume_not_operator", f"from={from_role!r}")
    if scope not in SCOPES:
        raise ResumeRejected("bad_scope", repr(scope))
    halt_id = _require_text(halt_id, "halt_ref")
    approved = _require_text(approved_by, "approved_by")
    cause = _require_text(root_cause, "root_cause")
    post = _require_text(postmortem, "postmortem")
    if checks is not None and not isinstance(checks, dict):
        raise ResumeRejected("bad_checks")
    market = str(market or "")
    target = state_path(path)
    records, problem = read_state(target)

    if halt_id in (FALLBACK_ID, STATE_PROBLEM_ID):
        if scope != SCOPE_GLOBAL:
            raise ResumeRejected("market_resume_cannot_lift_global", halt_id)
        if halt_id == FALLBACK_ID and not fallback_active(fallback):
            raise ResumeRejected("halt_ref_not_active", halt_id)
        if halt_id == STATE_PROBLEM_ID and problem is None:
            raise ResumeRejected("halt_ref_not_active", halt_id)
        halt_ref = {"id": halt_id, "scope": SCOPE_GLOBAL, "market": "",
                    "reason": FALLBACK_ACTIVE if halt_id == FALLBACK_ID else problem}
    else:
        if problem is not None:
            raise ResumeRejected("halt_state_problem", f"{problem}: resume {STATE_PROBLEM_ID} first")
        found = next((r for r in records if r["id"] == halt_id), None)
        if found is None or not found["active"]:
            raise ResumeRejected("halt_ref_not_active", halt_id)
        if found["scope"] == SCOPE_GLOBAL and scope != SCOPE_GLOBAL:
            raise ResumeRejected("market_resume_cannot_lift_global", halt_id)
        if found["scope"] != scope:
            raise ResumeRejected("scope_mismatch", f"halt is {found['scope']}, resume is {scope}")
        if scope == SCOPE_MARKET and market and market != found["market"]:
            raise ResumeRejected("market_mismatch", f"halt is {found['market']!r}")
        halt_ref = {k: found[k] for k in ("id", "scope", "market", "reason", "source", "ts")}

    payload = {"scope": scope, "halt_ref": halt_ref, "root_cause": cause, "postmortem": post,
               "checks": dict(checks or {}), "approved_by": approved, "exchange_action": "none"}
    message = bus(OPERATOR, "Sentinel", "resume", halt_ref["market"], payload, path=bus_path)
    stamp = message["ts"]
    resolved = {"ts": stamp, "approved_by": approved, "root_cause": cause, "postmortem": post}

    try:
        if halt_id == FALLBACK_ID:
            src = fallback_path(fallback)
            os.replace(src, src.with_name(f"{src.name}.resolved-{stamp.replace(':', '')}"))
        elif halt_id == STATE_PROBLEM_ID:
            if target.exists():
                os.replace(target, target.with_name(f"{target.name}.bad-{stamp.replace(':', '')}"))
            note = {"id": _halt_id(1, stamp, SCOPE_GLOBAL, "", "halt_store", str(problem)), "scope": SCOPE_GLOBAL,
                    "market": "", "reason": str(problem), "source": "halt_store", "ts": stamp, "active": False,
                    "resolved": resolved}
            _atomic_write(target, [note])
        else:
            updated = [dict(r, active=False, resolved=resolved) if r["id"] == halt_id else r for r in records]
            _atomic_write(target, updated)
    except (OSError, ValueError, TypeError) as exc:
        raise ResumeRejected("release_write_failed", f"{type(exc).__name__}: {exc}") from exc
    return {"released": halt_id, "scope": scope, "message": message}


# ---------------------------------------------------------------- CLI (the Operator's only way out)

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.halt_store",
        description="Halt state: list active halts, or (Operator only) resume one. No exchange action.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="print active halts (exit 3 when any halt is active)")
    res = sub.add_parser("resume", help="Operator resume of one halt; logged on the bus as type resume")
    res.add_argument("--scope", choices=SCOPES, required=True)
    res.add_argument("--halt-id", required=True, help=f"id from status, or {FALLBACK_ID} / {STATE_PROBLEM_ID}")
    res.add_argument("--market", default="")
    res.add_argument("--approved-by", required=True)
    res.add_argument("--root-cause", required=True)
    res.add_argument("--postmortem", required=True, help="e.g. PR #NN")
    res.add_argument("--checks", default="{}", help="JSON object of the pre-resume checks")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "status":
        active = active_halts()
        for record in active:
            print(f"ACTIVE {record['id']} scope={record['scope']} market={record['market'] or '-'} "
                  f"reason={record['reason']}")
        print(f"halts_active={len(active)} exchange_action=none")
        return 3 if active else 0
    try:
        checks = json.loads(args.checks)
    except ValueError:
        print("resume rejected: bad_checks (not JSON)", file=sys.stderr)
        return 1
    try:
        result = resume(from_role=OPERATOR, scope=args.scope, halt_id=args.halt_id, market=args.market,
                        approved_by=args.approved_by, root_cause=args.root_cause, postmortem=args.postmortem,
                        checks=checks)
    except ResumeRejected as exc:
        print(f"resume rejected: {exc}", file=sys.stderr)
        return 1
    print(f"resumed {result['released']} scope={result['scope']} logged=bus exchange_action=none")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
