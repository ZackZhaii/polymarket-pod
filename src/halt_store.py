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
* Issue #52: ``record_halt`` and a normal-record ``resume`` share one cross-process mutex
  (``StateLock``) and hold it across the halt-state read, the validation and the write-back, so
  two processes can never interleave those steps and overwrite each other's records. A resume
  re-reads and re-validates inside the mutex and flips only its own ``halt_ref`` id: a halt
  another process added meanwhile (market or global) is never dropped. If the mutex cannot be
  taken or the write fails, the resume is refused (``ResumeRejected``) and every halt stays in
  force (the bus line is already logged: nothing is released after it).

No network, no orders, no exchange action, no edits to config/risk.toml. The only new numbers are
the halt-state mutex timings (HALT_STATE_LOCK_TIMEOUT_S, HALT_STATE_LOCK_POLL_S in
src/thresholds.py, M2 pending).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.podlib import LOG_DIR, bus, canonical_role, utc_now  # noqa: E402
from src.thresholds import HALT_STATE_LOCK_POLL_S, HALT_STATE_LOCK_TIMEOUT_S  # noqa: E402

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
SENTINEL_LOST_ID = "sentinel-lost"  # Task AP: only while the lost-Sentinel halt could not be stored

STATE_MISSING = "halt_state_missing"
STATE_UNREADABLE = "halt_state_unreadable"
STATE_CORRUPT = "halt_state_corrupt"
STATE_MALFORMED = "halt_state_malformed"
CHECK_FAILED = "halt_check_failed"
FALLBACK_ACTIVE = "halt_file"

PERSISTED_STATE = "state"
PERSISTED_FALLBACK = "fallback"
PERSISTED_NONE = "none"

# Issue #52: refused release / unguarded write when the halt-state mutex cannot be taken.
LOCK_FAILED = "halt_lock_failed"


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
    if not isinstance(rid, str) or not rid or rid in (FALLBACK_ID, STATE_PROBLEM_ID, SENTINEL_LOST_ID):
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


SENTINEL_LOST_PREFIX = "sentinel_heartbeat_"
# Issue #129: no Sentinel heartbeat file and no ``.seen`` marker: the resident Sentinel has never run here.
SENTINEL_NEVER_SEEN = SENTINEL_LOST_PREFIX + "never_seen"


def sentinel_heartbeat_problem() -> str | None:
    """Task AP (design: "Sentinel 失联时 consumer 按 halt"): the halt reason when the resident Sentinel
    is lost or absent, else None (only a fresh heartbeat of a Sentinel that has not stopped).

    Issue #129: a Sentinel that has never run here (no heartbeat file and no ``.seen`` marker) is
    ``sentinel_heartbeat_never_seen``, not "nothing to lose": start the Sentinel first. After it has
    beaten once, a missing (marker but no file), unreadable, corrupt, malformed, future-stamped or
    too-old (SENTINEL_HEARTBEAT_TIMEOUT_S) heartbeat, or a Sentinel that finished or crashed, is a
    lost Sentinel. Any error while checking is too. There is no switch that turns this off.
    """
    try:
        from src import heartbeat  # local: heartbeat imports podlib only
        from src.thresholds import SENTINEL_HEARTBEAT_TIMEOUT_S

        record, problem = heartbeat.read_beat(heartbeat.beat_path("sentinel"), "sentinel")
        if problem is not None:
            return SENTINEL_LOST_PREFIX + problem.replace("heartbeat_", "")
        if record is None:
            return SENTINEL_NEVER_SEEN  # issue #129: never started here is fail closed, not "no halt"
        if record["state"] in (heartbeat.STATE_FINISHED, heartbeat.STATE_CRASHED):
            return SENTINEL_LOST_PREFIX + "stopped"
        age = heartbeat.beat_age_s(record, heartbeat.datetime.now(heartbeat.timezone.utc))
        if age is None or age > SENTINEL_HEARTBEAT_TIMEOUT_S:
            return SENTINEL_LOST_PREFIX + "stale"
        return None
    except Exception:  # noqa: BLE001 - a broken check is a lost Sentinel (fail closed)
        return SENTINEL_LOST_PREFIX + "check_failed"


def active_halts(path: Path | None = None, fallback: Path | None = None) -> list[dict[str, Any]]:
    """Every halt now in force, the synthetic global ones (bad state file, logs/HALT) included.

    Task AP: a lost resident Sentinel (sentinel_heartbeat_problem) is persisted here as a global
    halt from source halt_store, so it too stays until the Operator resumes it (and is recorded
    again at once if the Sentinel is still lost after the resume).

    Issue #129: a Sentinel that has never run here (sentinel_heartbeat_never_seen) is a global halt
    too, but a synthetic one that is not stored: nothing has been lost yet, so it needs no Operator
    resume and ends with the Sentinel's first fresh heartbeat. From then on a lost Sentinel is
    stored as above.
    """
    records, problem = read_state(path)
    lost = sentinel_heartbeat_problem()
    if lost is not None and lost != SENTINEL_NEVER_SEEN and problem is None:
        record_halt(scope=SCOPE_GLOBAL, reason=lost, source="halt_store", path=path, fallback=fallback)
        records, problem = read_state(path)
    active = [record for record in records if record["active"]]
    if lost is not None and not any(r["scope"] == SCOPE_GLOBAL and r["reason"] == lost for r in active):
        # the state file could not take it (already a global halt), the write went to logs/HALT, or
        # (issue #129) the Sentinel has never run here, which is never stored
        active.append({"id": SENTINEL_LOST_ID, "scope": SCOPE_GLOBAL, "market": "", "reason": lost,
                       "source": "halt_store", "active": True})
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
            elif record["id"] == SENTINEL_LOST_ID:
                reasons.append(record["reason"])
            elif record["scope"] == SCOPE_GLOBAL:
                reasons.append(f"halt_global:{record['id']}")
            elif record["market"] == key and key:
                reasons.append(f"halt_market:{record['id']}")
        return reasons
    except Exception:  # noqa: BLE001 - any failure to check is a halt (fail closed)
        return [CHECK_FAILED]


def has_active_halt(*, scope: str, market: str = "", reason_prefix: str = "", path: Path | None = None) -> bool | None:
    """Task AP: is a stored, active halt with this scope/market and reason prefix in force?

    None when the state file cannot be read (that is already a global halt; the caller must not
    treat None as "no halt"). Used to re-alarm at once after an Operator resume when the cause is
    still there (a stale book, a lost process), without writing a duplicate halt every check.
    """
    try:
        records, problem = read_state(path)
        if problem is not None:
            return None
        key = str(market or "") if scope == SCOPE_MARKET else ""
        return any(r["active"] and r["scope"] == scope and r["market"] == key and r["reason"].startswith(reason_prefix)
                   for r in records)
    except Exception:  # noqa: BLE001
        return None


def global_halt_active(path: Path | None = None, fallback: Path | None = None) -> list[str]:
    """Reasons that block every market (no market halts). Never raises."""
    try:
        return [reason for reason in halt_problems("", path, fallback) if not reason.startswith("halt_market:")]
    except Exception:  # noqa: BLE001
        return [CHECK_FAILED]


# ---------------------------------------------------------------- cross-process mutex (issue #52)

if os.name == "nt":  # the Windows dev machines
    import msvcrt

    def _lock_fd(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock_fd(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:  # CI runs Linux
    import fcntl

    def _lock_fd(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock_fd(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


def state_lock_path(path: Path | None = None) -> Path:
    """The mutex file beside a halt state file. It is never deleted, only locked, so the mutex
    always lives on a path no writer replaces."""
    target = state_path(path)
    return target.with_name(target.name + ".lock")


class StateLock:
    """One cross-process mutex over a halt-state read-modify-write (issue #52).

    ``record_halt`` and a normal-record ``resume`` take this mutex and hold it across the read,
    the validation and the write-back of the state file, so two writers can never interleave
    those steps and overwrite each other's records. The lock is the platform byte lock
    (``msvcrt.locking`` on Windows, ``fcntl.flock`` elsewhere) on ``<state file>.lock``, waited
    for at most HALT_STATE_LOCK_TIMEOUT_S in HALT_STATE_LOCK_POLL_S steps.

    ``acquire()`` is False when the mutex is held elsewhere or the lock file cannot be opened:
    the caller must fail closed then and never run an unguarded read-modify-write.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = state_lock_path(path)
        self._fd: int | None = None

    def acquire(self) -> bool:
        deadline = time.monotonic() + HALT_STATE_LOCK_TIMEOUT_S
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        except OSError:
            return False
        while True:
            try:
                _lock_fd(fd)
                self._fd = fd
                return True
            except OSError:
                if time.monotonic() >= deadline:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    return False
                time.sleep(HALT_STATE_LOCK_POLL_S)

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            _unlock_fd(fd)
        except OSError:
            pass  # closing the fd drops the OS lock either way
        finally:
            try:
                os.close(fd)
            except OSError:
                pass


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
    halt goes to logs/HALT; so does a failed write, and so does a halt-state mutex that cannot be
    taken (issue #52: the read, the duplicate check and the write-back run under the shared
    cross-process mutex, never as an unguarded read-modify-write). ``persisted="none"`` means
    even logs/HALT failed: the caller must still treat the halt as active.
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
    record: dict[str, Any] = {"scope": scope, "market": market, "reason": reason, "source": source, "ts": stamp,
                              "active": True}
    mutex = StateLock(target)
    if mutex.acquire():
        try:
            records, problem = read_state(target)
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
        finally:
            mutex.release()
    else:
        record["id"] = _halt_id(0, stamp, scope, market, source, reason)
        record["state_problem"] = LOCK_FAILED
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
    write releases nothing. Issue #52: the release is a halt-state read-modify-write, so it runs
    under the shared cross-process mutex (StateLock) and re-reads and re-validates inside it,
    flipping only ``halt_ref``'s id — a halt a concurrent process added is never overwritten.
    When the mutex cannot be taken or the write fails, ResumeRejected is raised and every halt
    stays in force. Special halt ids: ``halt-file`` (logs/HALT, renamed aside so the
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

    # Issue #52: release under the shared halt-state mutex, as one read-validate-write transaction.
    mutex = StateLock(target)
    if not mutex.acquire():
        raise ResumeRejected(LOCK_FAILED,
                             f"{state_lock_path(target).name} busy after {HALT_STATE_LOCK_TIMEOUT_S}s")
    try:
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
                records, problem = read_state(target)  # the mutex covers read, validation and write-back
                if problem is not None:
                    raise ResumeRejected("halt_state_problem", f"{problem}: resume {STATE_PROBLEM_ID} first")
                current = next((r for r in records if r["id"] == halt_id), None)
                if current is None or not current["active"]:
                    raise ResumeRejected("halt_ref_not_active", halt_id)
                if current["scope"] == SCOPE_GLOBAL and scope != SCOPE_GLOBAL:
                    raise ResumeRejected("market_resume_cannot_lift_global", halt_id)
                if current["scope"] != scope:
                    raise ResumeRejected("scope_mismatch", f"halt is {current['scope']}, resume is {scope}")
                if scope == SCOPE_MARKET and market and market != current["market"]:
                    raise ResumeRejected("market_mismatch", f"halt is {current['market']!r}")
                updated = [dict(r, active=False, resolved=resolved) if r["id"] == halt_id else r for r in records]
                _atomic_write(target, updated)
        except ResumeRejected:
            raise  # a doubt about the halt itself, not a write failure
        except (OSError, ValueError, TypeError) as exc:
            raise ResumeRejected("release_write_failed", f"{type(exc).__name__}: {exc}") from exc
    finally:
        mutex.release()
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


EXIT_OK = 0
EXIT_REJECTED = 1  # resume rejected (bad checks JSON, unknown / wrong-scope halt, missing approval, ...)
EXIT_USAGE = 2
EXIT_ACTIVE = 3  # status: at least one halt is active


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return EXIT_OK if exc.code == 0 else EXIT_USAGE
    if args.command == "status":
        active = active_halts()
        for record in active:
            print(f"ACTIVE {record['id']} scope={record['scope']} market={record['market'] or '-'} "
                  f"reason={record['reason']}")
        print(f"halts_active={len(active)} exchange_action=none")
        return EXIT_ACTIVE if active else EXIT_OK
    try:
        checks = json.loads(args.checks)
    except ValueError:
        print("resume rejected: bad_checks (not JSON)", file=sys.stderr)
        return EXIT_REJECTED
    try:
        result = resume(from_role=OPERATOR, scope=args.scope, halt_id=args.halt_id, market=args.market,
                        approved_by=args.approved_by, root_cause=args.root_cause, postmortem=args.postmortem,
                        checks=checks)
    except ResumeRejected as exc:
        print(f"resume rejected: {exc}", file=sys.stderr)
        return EXIT_REJECTED
    print(f"resumed {result['released']} scope={result['scope']} logged=bus exchange_action=none")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
