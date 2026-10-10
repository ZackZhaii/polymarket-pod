"""Task AP (Poly哨兵 design gap 2): per-process heartbeat files that the resident Sentinel reads.

Every long-running loop (and every scan-style CLI at start and finish) writes one JSON file,
``logs/heartbeat/<proc>.json`` (HEARTBEAT_DIR, read at call time so tests and the e2e gate can
move it). Written atomically: temp file in the same folder, fsync, os.replace; then a ``.seen``
marker next to it after the first write, so a later missing file is a lost heartbeat, not a first
run. Not written through the bus.

Fields: proc, pid, host, started_at, ts, seq, git_sha, risk_sha256, live_enabled, mode, state,
exit_code, progress{}. ``state`` is one of STATES. ``exit_code`` is null until the process ends.

Reading is fail closed: read_beat returns a problem for a file that is unreadable, not JSON,
malformed (any field of the wrong type, a naive or unparseable time, an unknown state, another
proc's name) or missing after its marker exists. src/sentinel_tick.py treats every such problem
as a stale heartbeat (a global halt). A failed write is never raised into the caller's loop
(issue #79): Heartbeat.beat records it at once as the global halt ``heartbeat_write_failed:<proc>``
through src/halt_store.py (deduplicated, logs/HALT fallback, no bus, independent of the ``.seen``
marker), and only an Operator resume lifts that halt. write_record stays generic: the Sentinel
tick state (src/sentinel_tick.py save_state) is written with it and is never a heartbeat event.

Cadences and timeouts are code constants in src/thresholds.py (M2 pending). No network, no orders,
no edits to config/risk.toml (it is only read, to stamp risk_sha256 / live_enabled / mode).
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.podlib import LOG_DIR, RISK_PATH, parse_risk_toml  # noqa: E402

HEARTBEAT_DIR = LOG_DIR / "heartbeat"
_DATETIME = datetime  # the real class, for isinstance checks (``datetime`` itself may be frozen by the e2e gate)

STATE_STARTING = "starting"
STATE_RUNNING = "running"
STATE_PAUSED = "paused"  # backing off between reconnects (still beating)
STATE_FINISHED = "finished"
STATE_CRASHED = "crashed"
STATES = (STATE_STARTING, STATE_RUNNING, STATE_PAUSED, STATE_FINISHED, STATE_CRASHED)

HB_MISSING = "heartbeat_missing"
HB_UNREADABLE = "heartbeat_unreadable"
HB_CORRUPT = "heartbeat_corrupt"
HB_MALFORMED = "heartbeat_malformed"
HB_WRITE_FAILED = "heartbeat_write_failed"
HB_PROBLEMS = (HB_MISSING, HB_UNREADABLE, HB_CORRUPT, HB_MALFORMED)

FIELDS = ("proc", "pid", "host", "started_at", "ts", "seq", "git_sha", "risk_sha256", "live_enabled", "mode",
          "state", "exit_code", "progress")

_GIT_SHA: list[str | None] = []  # cached once per process


def heartbeat_dir(directory: Path | None = None) -> Path:
    return Path(directory) if directory is not None else Path(HEARTBEAT_DIR)


def beat_path(proc: str, directory: Path | None = None) -> Path:
    if not isinstance(proc, str) or not proc or not proc.replace("_", "").isalnum():
        raise ValueError(f"bad heartbeat proc name {proc!r}")
    return heartbeat_dir(directory) / f"{proc}.json"


def state_marker(path: Path) -> Path:
    return path.with_name(path.name + ".seen")


def utc_stamp(moment: datetime | None = None) -> str:
    """ISO 8601 UTC with milliseconds and Z (``datetime`` is module-level so the e2e gate can freeze it)."""
    moment = moment or datetime.now(timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def parse_stamp(value: object) -> datetime | None:
    """An aware ISO 8601 time, else None (naive, date-only, numeric, bool and non-strings fail)."""
    if not isinstance(value, str) or "T" not in value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None or moment.utcoffset() is None:
        return None
    return moment


def git_sha() -> str | None:
    if not _GIT_SHA:
        try:
            done = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, timeout=10,
                                  check=False)
            sha = done.stdout.decode("utf-8", "replace").strip() if done.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            sha = ""
        _GIT_SHA.append(sha or None)
    return _GIT_SHA[0]


def risk_identity(path: Path | None = None) -> dict[str, Any]:
    """sha256 of config/risk.toml (CRLF normalised, as guard reads it), live_enabled and mode.

    Any read or parse failure gives None values, which the Sentinel treats as a pin problem.
    """
    target = Path(path) if path is not None else Path(RISK_PATH)
    try:
        raw = target.read_bytes()
    except OSError:
        return {"risk_sha256": None, "live_enabled": None, "mode": None, "require_human_gate": None}
    sha = hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()
    try:
        cfg = parse_risk_toml(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        cfg = {}
    live = cfg.get("live_enabled")
    mode = cfg.get("mode")
    gate = cfg.get("require_human_gate")
    return {"risk_sha256": sha, "live_enabled": live if isinstance(live, bool) else None,
            "mode": mode if isinstance(mode, str) else None,
            "require_human_gate": gate if isinstance(gate, bool) else None}


def write_record(path: Path, record: dict[str, Any]) -> str | None:
    """Atomic write (temp, fsync, os.replace) then the marker. None on success, else HB_WRITE_FAILED."""
    tmp_name = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps(record, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=path.name + ".",
                                         suffix=".tmp", delete=False) as handle:
            tmp_name = handle.name
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        tmp_name = None
        marker = state_marker(path)
        if not marker.exists():
            marker.write_text("1\n", encoding="utf-8")
    except (OSError, ValueError, TypeError):
        return HB_WRITE_FAILED
    finally:
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
    return None


def _int(value: object, minimum: int = 0) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def validate_record(data: object, proc: str | None = None) -> dict[str, Any]:
    """Return the record or raise ValueError on any malformed field (fail closed)."""
    if not isinstance(data, dict):
        raise ValueError("heartbeat must be an object")
    missing = [name for name in FIELDS if name not in data]
    if missing:
        raise ValueError(f"missing fields {missing}")
    if not isinstance(data["proc"], str) or not data["proc"] or (proc is not None and data["proc"] != proc):
        raise ValueError("proc")
    if not _int(data["pid"], 1) or not _int(data["seq"], 0):
        raise ValueError("pid/seq")
    if not isinstance(data["host"], str):
        raise ValueError("host")
    if parse_stamp(data["started_at"]) is None or parse_stamp(data["ts"]) is None:
        raise ValueError("started_at/ts")
    if data["git_sha"] is not None and not isinstance(data["git_sha"], str):
        raise ValueError("git_sha")
    if data["risk_sha256"] is not None and not isinstance(data["risk_sha256"], str):
        raise ValueError("risk_sha256")
    if data["live_enabled"] is not None and not isinstance(data["live_enabled"], bool):
        raise ValueError("live_enabled")
    if data["mode"] is not None and not isinstance(data["mode"], str):
        raise ValueError("mode")
    if data["state"] not in STATES:
        raise ValueError("state")
    code = data["exit_code"]
    if code is not None and (not isinstance(code, int) or isinstance(code, bool)):
        raise ValueError("exit_code")
    if data["state"] == STATE_FINISHED and code is None:
        raise ValueError("a finished heartbeat needs an exit_code")
    if not isinstance(data["progress"], dict):
        raise ValueError("progress")
    return data


def read_beat(path: Path, proc: str | None = None) -> tuple[dict[str, Any] | None, str | None]:
    """``(record, problem)``. ``(None, None)`` only for a true first run (no file and no marker)."""
    try:
        if not path.exists():
            return None, (HB_MISSING if state_marker(path).exists() else None)
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None, HB_UNREADABLE
    try:
        data = json.loads(text)
    except ValueError:
        return None, HB_CORRUPT
    try:
        return validate_record(data, proc), None
    except (ValueError, TypeError, KeyError):
        return None, HB_MALFORMED


def beat_age_s(record: dict[str, Any], now: datetime, field: str = "ts") -> float | None:
    """Seconds since ``record[field]``; None when unusable or in the future (the caller fails closed)."""
    moment = parse_stamp(record.get(field))
    if moment is None or not isinstance(now, _DATETIME) or now.tzinfo is None:
        return None
    age = (now - moment).total_seconds()
    if not math.isfinite(age) or age < 0:
        return None
    return age


class Heartbeat:
    """One process's heartbeat writer. ``beat`` is throttled to ``every_s`` (monotonic); never raises."""

    def __init__(self, proc: str, *, every_s: float, directory: Path | None = None,
                 monotonic: Callable[[], float] | None = None, now: Callable[[], datetime] | None = None,
                 risk_path: Path | None = None) -> None:
        if isinstance(every_s, bool) or not isinstance(every_s, (int, float)) or not every_s > 0:
            raise ValueError("every_s must be > 0")
        self.proc = proc
        self.path = beat_path(proc, directory)
        self.every_s = float(every_s)
        self.monotonic = monotonic or time.monotonic
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.risk_path = risk_path
        self.started_at = utc_stamp(self.now())
        self.seq = 0
        self.last_mono: float | None = None
        self.last_problem: str | None = None
        self.last_halt: dict[str, Any] | None = None  # the heartbeat_write_failed halt of the last beat
        self.carry: dict[str, Any] = {}

    def record(self, state: str, progress: dict[str, Any] | None, exit_code: int | None) -> dict[str, Any]:
        identity = risk_identity(self.risk_path)
        return {"proc": self.proc, "pid": os.getpid(), "host": socket.gethostname(), "started_at": self.started_at,
                "ts": utc_stamp(self.now()), "seq": self.seq, "git_sha": git_sha(),
                "risk_sha256": identity["risk_sha256"], "live_enabled": identity["live_enabled"],
                "mode": identity["mode"], "state": state, "exit_code": exit_code,
                "progress": {**self.carry, **(progress or {})}}

    def beat(self, state: str = STATE_RUNNING, progress: dict[str, Any] | None = None, *,
             exit_code: int | None = None, force: bool = False) -> str | None:
        """Write now if forced or ``every_s`` has passed. Returns the write problem (None when fine).

        The shared failure branch (the first write and every later one alike): a failed write is
        recorded at once as the global halt ``heartbeat_write_failed:<proc>`` through
        src/halt_store.py (deduplicated, with the logs/HALT fallback, no bus, no ``.seen``
        dependency). ``last_halt`` is that halt record -- its ``persisted`` says where it landed
        (state, fallback or ``none``: never claim a persisted halt on ``none``) -- and None on a
        good write or when even recording the halt broke. A later good write never resumes the
        halt: only the Operator does.
        """
        try:
            current = self.monotonic()
            if not force and self.last_mono is not None and current - self.last_mono < self.every_s:
                return None
            self.seq += 1
            problem = write_record(self.path, self.record(state, progress, exit_code))
            self.last_mono = current
        except Exception:  # noqa: BLE001 - a heartbeat never breaks the loop; the failure halts below
            problem = HB_WRITE_FAILED
        self.last_problem = problem
        self.last_halt = self._write_failed_halt() if problem is not None else None
        return problem

    def _write_failed_halt(self) -> dict[str, Any] | None:
        """The failed write's global halt (issue #79), through the existing halt store. Never raises.

        record_halt keeps the halt deduplicated and falls back to logs/HALT when the halt state
        cannot take it; its ``persisted`` result is handed to the caller unchanged (``none`` means
        even logs/HALT failed: the halt is in force but nothing persisted it).
        """
        try:
            from src import halt_store  # local: heartbeat imports podlib only at module level
            return halt_store.record_halt(scope=halt_store.SCOPE_GLOBAL, reason=f"{HB_WRITE_FAILED}:{self.proc}",
                                          source="heartbeat")
        except Exception:  # noqa: BLE001 - a broken halt store must not break the caller's loop
            return None

    def finish(self, exit_code: int | None, progress: dict[str, Any] | None = None, *,
               crashed: bool = False) -> str | None:
        state = STATE_CRASHED if crashed or exit_code is None else STATE_FINISHED
        return self.beat(state, progress, exit_code=exit_code, force=True)


def previous_progress(proc: str, directory: Path | None = None) -> dict[str, Any]:
    """The last readable heartbeat's progress (for counters carried across runs); {} if none."""
    record, _ = read_beat(beat_path(proc, directory), proc)
    return dict(record["progress"]) if record else {}


def run_main(proc: str, main: Callable[[], int], *, directory: Path | None = None,
             now: Callable[[], datetime] | None = None) -> int:
    """Start/finish heartbeat around a scan-style CLI (scanner, endgame_scan, loop_claim).

    The start beat is ``running`` (the Sentinel's hang check counts from started_at); the finish
    beat carries the exit code, ``exit_streak`` (consecutive runs ending with this same code) and
    ``last_success_ts`` (the latest exit 0), both carried over from the previous heartbeat. An
    exception is written as ``crashed`` and re-raised.
    """
    before = previous_progress(proc, directory)
    hb = Heartbeat(proc, every_s=1.0, directory=directory, now=now)
    keep = {k: before[k] for k in ("last_success_ts", "last_exit_code", "exit_streak") if k in before}
    hb.carry = keep
    hb.beat(STATE_RUNNING, force=True)
    try:
        code = main()
    except BaseException:
        hb.finish(None, crashed=True)
        raise
    code = code if isinstance(code, int) and not isinstance(code, bool) else 1
    streak = before.get("exit_streak") if before.get("last_exit_code") == code else 0
    streak = (streak if _int(streak, 0) else 0) + 1
    finish = {"last_exit_code": code, "exit_streak": streak}
    if code == 0:
        finish["last_success_ts"] = utc_stamp(hb.now())
    hb.finish(code, finish)
    return code
