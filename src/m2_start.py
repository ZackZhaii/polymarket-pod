"""Issue #133 (Task BR, Poly哨兵 M2 runbook G-P0-6): the NoisePC start orchestration for M2, paper only.

    python -m src.m2_start --reason boot [--m2-end 2026-10-20T00:00:00Z] [--dry-run]
    powershell -File scripts\\m2_start.ps1 -Reason boot [-M2End ...] [-DryRun]   # the same, on NoisePC

Start order (issue #129, Poly指挥: the Sentinel first, nothing else before its heartbeat is fresh):

1. The resident Sentinel ``python -m src.sentinel_tick``. A Sentinel that is already running and fresh
   is kept (never a second one). Otherwise it is launched, and the script waits up to
   SENTINEL_HEARTBEAT_TIMEOUT_S for a fresh heartbeat **from the Sentinel it just launched** (state
   running, ``started_at`` not before the launch, ``ts`` younger than the timeout, stamped
   live_enabled false / mode paper / the pinned risk sha) that every consumer also accepts
   (halt_store.sentinel_heartbeat_problem() is None). An old heartbeat file never counts. If that
   never happens, nothing else is started: exit 4 with the reason.
2. One line appended to ``runs/m2-chain.jsonl``: ts, run_dir, git_sha, reason, m2_end, max_runtime_s,
   host, the Sentinel's pid. A failed append starts nothing else (exit 5).
3. ``python -m src.m1_run --out runs/m2/m1-<UTC stamp> --max-runtime <seconds left until --m2-end>``.
   The run dir is new on every start (m1_run never reuses a non-empty one) and lives under #119's
   fixed ``runs/m2/``. Ready = a fresh m1_run heartbeat from this launch, a valid manifest and at least
   one books file, within m1_run's Sentinel timeout (PROC_SPECS). Not ready: exit 5, nothing after it.
4. ``python -m src.paper_cycle --loop --recording-dir <the same run dir>``. Ready = a fresh
   paper_cycle heartbeat from this launch within its Sentinel timeout.
5. ``python -m src.m2_runner --run-dir runs/m2 --max-runtime <seconds left>``, only once
   src/m2_runner.py exists (#119) and the Sentinel watches it (PROC_SPECS has ``m2_runner``; if the
   module exists without that, the script refuses to start anything: exit 3).

``runs/m2/current.json`` (Current below) is rewritten atomically after every launch and every check:
the run dir (once the chain line is written) and each launched or kept process's name, pid, start ts
and state, plus status starting / started / failed. Monitors read it read-only. A failed write stops
the order like a failed chain append (exit 5).

Then it prints ``halt_store status`` and reminds the Operator: existing halts stay. This script never
resumes, never reads .env or any credential, never calls an order or trading interface, never passes a
live flag, never registers a scheduled task or service (boot start is a manual Operator step,
docs/PHASE0_RUN.md) and refuses to run from the M1 clone (M1_CLONE). Processes it started are never
stopped by it: a launched process that fails its check keeps running for the Operator to inspect, and
the Sentinel watches it like any other.

Refused before anything starts (exit 3): run from the M1 clone; ``runs/m2/KILL`` present; an unreadable
or inconsistent chain; no M2 end (``--m2-end`` on the first start; later starts read it from the chain,
and a different ``--m2-end`` is a usage error); the M2 window already over; m1_run, paper_cycle or
m2_runner already running with a fresh heartbeat; the new run dir already exists. config/risk.toml off
its pins: exit 6 (global halt recorded, as every entry point does; ``--dry-run`` records nothing).

``--dry-run`` prints the plan (commands, waits, run dir, the chain line) after the same refusals and
starts, creates and appends nothing.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import halt_store, heartbeat, paper_cycle, pins  # noqa: E402
from src.sentinel_tick import PROC_SPECS  # noqa: E402
from src.thresholds import SENTINEL_HEARTBEAT_TIMEOUT_S  # noqa: E402

EXIT_OK = 0  # every step started and passed its check (--dry-run: the plan passed the refusals)
EXIT_USAGE = 2
EXIT_REFUSED = 3  # refused before anything started
EXIT_SENTINEL_NOT_FRESH = 4  # the Sentinel never beat fresh: nothing else started
EXIT_NOT_READY = 5  # the chain append failed or a later process failed its check: nothing after it started
EXIT_PIN_DRIFT = 6  # config/risk.toml off its pins: refused, global halt recorded (Task AS)

START_POLL_S = 1.0  # how often a start check looks again while it waits (operational, not a limit)
M1_CLONE = "D:\\AI_Workspace\\pod-m1"  # the running M1 clone: never started from or touched

RUNS_DIR = ROOT / "runs"
M2_DIR = RUNS_DIR / "m2"
CHAIN_PATH = RUNS_DIR / "m2-chain.jsonl"
CURRENT_NAME = "current.json"  # runs/m2/current.json: this start's run dir and processes (read-only for monitors)
CURRENT_SCHEMA = "m2_current/1"
KILL_NAME = "KILL"
CHAIN_FIELDS = ("ts", "run_dir", "git_sha", "reason", "m2_end", "max_runtime_s", "host", "sentinel_pid")
LIVE_STATES = (heartbeat.STATE_STARTING, heartbeat.STATE_RUNNING, heartbeat.STATE_PAUSED)
WATCHED = ("m1_run", "paper_cycle", "m2_runner")  # a second copy of these must never start


class Handle(Protocol):
    pid: int

    def poll(self) -> int | None: ...


Launcher = Callable[[list[str], Path, Path], Handle]


# ---------------------------------------------------------------- small pure helpers

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def parse_utc(value: object) -> datetime | None:
    """An aware ISO 8601 time (``Z`` allowed), else None."""
    moment = heartbeat.parse_stamp(value)
    return moment.astimezone(timezone.utc) if moment is not None else None


def in_m1_clone(root: Path) -> bool:
    """True when ``root`` is the M1 clone or inside it (Windows paths, any case or slash)."""
    here = str(root).replace("/", "\\").rstrip("\\").lower()
    clone = M1_CLONE.lower()
    return here == clone or here.startswith(clone + "\\")


def read_chain(path: Path) -> tuple[list[dict[str, Any]], str | None]:
    """The chain rows, or a problem (unreadable, a line that is not a chain object, mixed M2 ends)."""
    if not path.exists():
        return [], None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        return [], f"chain_unreadable:{type(exc).__name__}"
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            return [], f"chain_corrupt:line {number}"
        if not isinstance(row, dict) or any(field not in row for field in CHAIN_FIELDS) \
                or parse_utc(row["m2_end"]) is None:
            return [], f"chain_malformed:line {number}"
        rows.append(row)
    if len({parse_utc(row["m2_end"]) for row in rows}) > 1:
        return [], "chain_m2_end_inconsistent"
    return rows, None


def fresh_beat(proc: str, *, timeout_s: float, now: datetime,
               launched_at: datetime | None = None) -> tuple[dict[str, Any] | None, str | None]:
    """``(record, None)`` for a live, fresh, paper-stamped heartbeat of ``proc``; else ``(None, why)``.

    With ``launched_at`` the beat must also come from a process started at or after that moment, so a
    heartbeat left over from an earlier run never passes.
    """
    record, problem = heartbeat.read_beat(heartbeat.beat_path(proc), proc)
    if problem is not None:
        return None, problem
    if record is None:
        return None, "no_heartbeat"
    if record["state"] not in LIVE_STATES:
        return None, f"state_{record['state']}"
    age = heartbeat.beat_age_s(record, now)
    if age is None or age > timeout_s:
        return None, "stale"
    if record["live_enabled"] is not False or record["mode"] != "paper" \
            or record["risk_sha256"] != pins.PINNED_RISK_SHA256:
        return None, "not_paper_pinned"
    if launched_at is not None:
        started = parse_utc(record["started_at"])
        floor = launched_at.replace(microsecond=launched_at.microsecond // 1000 * 1000)
        if started is None or started < floor:
            return None, "older_than_launch"
    return record, None


def sentinel_fresh(*, now: datetime, launched_at: datetime | None = None) -> tuple[dict[str, Any] | None, str | None]:
    """A fresh resident Sentinel that the consumers accept too (running, not merely starting)."""
    record, why = fresh_beat("sentinel", timeout_s=SENTINEL_HEARTBEAT_TIMEOUT_S, now=now, launched_at=launched_at)
    if why is not None:
        return None, why
    if record["state"] != heartbeat.STATE_RUNNING:
        return None, f"state_{record['state']}"
    consumer = halt_store.sentinel_heartbeat_problem()
    if consumer is not None:
        return None, consumer
    return record, None


def recording_problem(run_dir: Path) -> str | None:
    """None once m1_run's run dir has a valid manifest and at least one books file."""
    _, problem = paper_cycle.recording_markets(run_dir)
    if problem is not None:
        return problem
    if not paper_cycle.recording_book_files(run_dir):
        return paper_cycle.RECORDING_EMPTY
    return None


def wait_for(check: Callable[[], str | None], *, timeout_s: float, handle: Handle | None,
             sleep: Callable[[float], None], monotonic: Callable[[], float]) -> str | None:
    """Poll ``check`` every START_POLL_S until it passes (None); the last reason on timeout or exit."""
    started = monotonic()
    while True:
        why = check()
        if why is None:
            return None
        if handle is not None:
            code = handle.poll()
            if code is not None:
                return f"exited_{code}:{why}"
        if monotonic() - started >= timeout_s:
            return f"timeout_{timeout_s:g}s:{why}"
        sleep(START_POLL_S)


def popen_launcher(argv: list[str], log_path: Path, cwd: Path) -> Handle:
    """Start ``argv`` detached from this console (it outlives the script), output to ``log_path``."""
    kwargs: dict[str, Any] = {"cwd": str(cwd), "stdin": subprocess.DEVNULL, "stderr": subprocess.STDOUT,
                              "close_fds": True}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    with log_path.open("ab") as log:
        return subprocess.Popen(argv, stdout=log, **kwargs)


def rel(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


# ---------------------------------------------------------------- runs/m2/current.json

class Current:
    """``runs/m2/current.json``: the current start's run dir and every process it launched or kept.

    Written whole on every change through the repo's atomic writer (heartbeat.write_record: temp
    file in the same folder, fsync, os.replace), so a reader sees the old or the new file, never a
    torn one. Fields: schema, status (starting / started / failed), updated_ts, run_dir (null until
    the chain line is written), m2_end, reason, git_sha, host, failure, and processes[]: name, pid
    (the launched or kept process), started_ts, state (starting / ready / not_ready / kept) and
    heartbeat_pid (the pid in its heartbeat once ready; on Windows a venv launcher's child may
    differ). A failed write is reported and stops the start order (fail closed), never skipped.
    """

    def __init__(self, path: Path, *, m2_end: str, reason: str, now: Callable[[], datetime]) -> None:
        self.path = path
        self.now = now
        self.data: dict[str, Any] = {"schema": CURRENT_SCHEMA, "status": "starting", "updated_ts": None,
                                     "run_dir": None, "m2_end": m2_end, "reason": reason,
                                     "git_sha": heartbeat.git_sha(), "host": socket.gethostname(),
                                     "failure": None, "processes": []}

    def process(self, name: str, pid: int, started_ts: str, state: str) -> dict[str, Any]:
        entry = {"name": name, "pid": pid, "started_ts": started_ts, "state": state, "heartbeat_pid": None}
        self.data["processes"].append(entry)
        return entry

    def write(self) -> str | None:
        self.data["updated_ts"] = heartbeat.utc_stamp(self.now())
        return heartbeat.write_record(self.path, self.data)


# ---------------------------------------------------------------- the orchestration

def plan_commands(python: str, run_dir_arg: str, max_runtime_s: int, with_runner: bool) -> list[tuple[str, list[str]]]:
    """(proc, argv) in start order after the Sentinel. Paper only: no live flag exists to pass."""
    steps = [
        ("m1_run", [python, "-m", "src.m1_run", "--out", run_dir_arg, "--max-runtime", str(max_runtime_s)]),
        ("paper_cycle", [python, "-m", "src.paper_cycle", "--loop", "--recording-dir", run_dir_arg]),
    ]
    if with_runner:
        steps.append(("m2_runner", [python, "-m", "src.m2_runner", "--run-dir", "runs/m2",
                                    "--max-runtime", str(max_runtime_s)]))
    return steps


def start(*, reason: str, m2_end: str | None, dry_run: bool, root: Path = ROOT,
          launcher: Launcher = popen_launcher, python: str | None = None,
          now: Callable[[], datetime] = utc_now, sleep: Callable[[float], None] = time.sleep,
          monotonic: Callable[[], float] = time.monotonic,
          runner_available: Callable[[Path], bool] | None = None,
          argv_for: Callable[[str, list[str]], list[str]] | None = None,
          out: Callable[[str], None] = print) -> int:
    """Run the start order (see the module docstring). Returns an exit code; never raises on a refusal."""
    python = python or sys.executable
    tag = "m2_start" + (" [dry-run]" if dry_run else "")
    runs_dir, m2_dir = root / "runs", root / "runs" / "m2"
    chain_path = runs_dir / CHAIN_PATH.name

    def say(line: str) -> None:
        out(f"{tag}: {line}")

    def refuse(code: int, why: str) -> int:
        say(f"REFUSED ({why}); nothing started; orders=none")
        return code

    if not reason.strip():
        return refuse(EXIT_USAGE, "--reason must not be blank")
    if in_m1_clone(root):
        return refuse(EXIT_REFUSED, f"{root} is the M1 clone {M1_CLONE}")
    if (m2_dir / KILL_NAME).exists():
        return refuse(EXIT_REFUSED, f"{rel(m2_dir / KILL_NAME, root)} present (Operator stop)")
    rows, chain_problem = read_chain(chain_path)
    if chain_problem is not None:
        return refuse(EXIT_REFUSED, f"{rel(chain_path, root)}: {chain_problem}")
    chained_end = parse_utc(rows[0]["m2_end"]) if rows else None
    given_end = None
    if m2_end is not None:
        given_end = parse_utc(m2_end)
        if given_end is None:
            return refuse(EXIT_USAGE, f"--m2-end {m2_end!r} is not an ISO 8601 UTC time")
    if chained_end is not None and given_end is not None and given_end != chained_end:
        return refuse(EXIT_USAGE, f"--m2-end {m2_end} differs from the chain's {heartbeat.utc_stamp(chained_end)}")
    end = chained_end or given_end
    if end is None:
        return refuse(EXIT_USAGE, "no M2 end: pass --m2-end on the first start")
    moment = now()
    max_runtime_s = int((end - moment).total_seconds())
    if max_runtime_s < 1:
        return refuse(EXIT_REFUSED, f"the M2 window ended at {heartbeat.utc_stamp(end)}")

    with_runner = (runner_available or (lambda r: (r / "src" / "m2_runner.py").exists()))(root)
    if with_runner and "m2_runner" not in PROC_SPECS:
        return refuse(EXIT_REFUSED, "src/m2_runner.py exists but the Sentinel does not watch m2_runner yet")
    for proc in WATCHED:
        spec = PROC_SPECS.get(proc)
        if spec is not None and fresh_beat(proc, timeout_s=spec.timeout_s, now=moment)[1] is None:
            return refuse(EXIT_REFUSED, f"{proc} is already running with a fresh heartbeat; stop it first")
    run_dir = m2_dir / f"m1-{stamp(moment)}"
    if run_dir.exists():
        return refuse(EXIT_REFUSED, f"{rel(run_dir, root)} already exists (a run dir is never reused)")
    if dry_run:
        drift = pins.risk_file_problems()
    else:
        drift = pins.refuse_start("m2_start", stream=sys.stdout)
    if drift:
        return refuse(EXIT_PIN_DRIFT, f"config/risk.toml off its pins: {', '.join(drift[:5])}")

    run_arg = rel(run_dir, root)
    logs_dir = m2_dir / "start-logs" / stamp(moment)
    steps = plan_commands(python, run_arg, max_runtime_s, with_runner)
    if argv_for is not None:
        steps = [(proc, argv_for(proc, argv)) for proc, argv in steps]
    sentinel_argv = [python, "-m", "src.sentinel_tick"]
    if argv_for is not None:
        sentinel_argv = argv_for("sentinel", sentinel_argv)
    say(f"root={root} m2_end={heartbeat.utc_stamp(end)} max_runtime_s={max_runtime_s} run_dir={run_arg} "
        f"m2_runner={'yes' if with_runner else 'not in this checkout (#119)'} live=none")
    current = Current(m2_dir / CURRENT_NAME, m2_end=heartbeat.utc_stamp(end), reason=reason.strip(), now=now)
    current_rel = rel(current.path, root)

    def publish(failing: str | None = None) -> bool:
        """Write current.json; on a failed write say so. False = stop the order (fail closed)."""
        if failing is not None:
            current.data["status"], current.data["failure"] = "failed", failing
        problem = current.write()
        if problem is not None:
            say(f"FAILED: cannot write {current_rel} ({problem}); nothing after this point started")
            return False
        return True

    # 1. The resident Sentinel, first.
    sentinel_record, _ = sentinel_fresh(now=moment)
    sentinel_handle: Handle | None = None
    sentinel_entry: dict[str, Any] | None = None
    if sentinel_record is not None:
        say(f"step 1 sentinel: already running and fresh (pid {sentinel_record['pid']}); kept, no second one")
        if not dry_run:
            sentinel_entry = current.process("sentinel", sentinel_record["pid"], sentinel_record["started_at"], "kept")
            sentinel_entry["heartbeat_pid"] = sentinel_record["pid"]
    elif dry_run:
        say(f"step 1 sentinel: would start {' '.join(sentinel_argv)} and wait up to "
            f"{SENTINEL_HEARTBEAT_TIMEOUT_S:g} s (SENTINEL_HEARTBEAT_TIMEOUT_S) for its own fresh heartbeat")
    else:
        logs_dir.mkdir(parents=True, exist_ok=True)
        launched = now()
        sentinel_handle = launcher(sentinel_argv, logs_dir / "sentinel.log", root)
        say(f"step 1 sentinel: started pid {sentinel_handle.pid}; waiting up to {SENTINEL_HEARTBEAT_TIMEOUT_S:g} s")
        sentinel_entry = current.process("sentinel", sentinel_handle.pid, heartbeat.utc_stamp(launched), "starting")
        if not publish():
            return EXIT_NOT_READY
        why = wait_for(lambda: sentinel_fresh(now=now(), launched_at=launched)[1],
                       timeout_s=SENTINEL_HEARTBEAT_TIMEOUT_S, handle=sentinel_handle, sleep=sleep,
                       monotonic=monotonic)
        if why is not None:
            say(f"FAILED step 1: the Sentinel heartbeat never became fresh ({why}); nothing else started "
                f"(the launched Sentinel pid {sentinel_handle.pid} is left as is; log {rel(logs_dir, root)})")
            sentinel_entry["state"] = "not_ready"
            publish(f"sentinel: {why}")
            return EXIT_SENTINEL_NOT_FRESH
        sentinel_record, _ = sentinel_fresh(now=now(), launched_at=launched)
        say(f"step 1 sentinel: fresh heartbeat (pid {sentinel_record['pid'] if sentinel_record else '?'})")
        sentinel_entry["state"] = "ready"
        sentinel_entry["heartbeat_pid"] = sentinel_record["pid"] if sentinel_record else None

    # 2. The chain line (before the run dir gets any data).
    chain_row = {"ts": heartbeat.utc_stamp(now()), "run_dir": run_arg, "git_sha": heartbeat.git_sha(),
                 "reason": reason.strip(), "m2_end": heartbeat.utc_stamp(end), "max_runtime_s": max_runtime_s,
                 "host": socket.gethostname(),
                 "sentinel_pid": sentinel_record["pid"] if sentinel_record is not None else None}
    line = json.dumps(chain_row, ensure_ascii=False, sort_keys=True)
    if dry_run:
        say(f"step 2 chain: would append to {rel(chain_path, root)}: {line}")
        say(f"step 2 current: would write {current_rel} (run dir, then each process as it starts)")
    else:
        try:
            runs_dir.mkdir(parents=True, exist_ok=True)
            logs_dir.mkdir(parents=True, exist_ok=True)
            with chain_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            say(f"FAILED step 2: cannot append {rel(chain_path, root)} ({type(exc).__name__}); nothing else started")
            return EXIT_NOT_READY
        say(f"step 2 chain: appended to {rel(chain_path, root)}")
        current.data["run_dir"] = run_arg
        if not publish():
            return EXIT_NOT_READY
        say(f"step 2 current: {current_rel} names {run_arg}")

    # 3.. m1_run, paper_cycle, m2_runner: each only after the one before passed its check.
    for number, (proc, argv) in enumerate(steps, 3):
        timeout_s = PROC_SPECS[proc].timeout_s
        if dry_run:
            extra = " plus a manifest and a books file in the run dir" if proc == "m1_run" else ""
            say(f"step {number} {proc}: would start {' '.join(argv)} and wait up to {timeout_s:g} s "
                f"(its Sentinel timeout) for its own fresh heartbeat{extra}")
            continue
        launched = now()
        handle = launcher(argv, logs_dir / f"{proc}.log", root)
        say(f"step {number} {proc}: started pid {handle.pid}; waiting up to {timeout_s:g} s")
        entry = current.process(proc, handle.pid, heartbeat.utc_stamp(launched), "starting")
        if not publish():
            return EXIT_NOT_READY

        def check(proc: str = proc, launched: datetime = launched) -> str | None:
            why = fresh_beat(proc, timeout_s=timeout_s, now=now(), launched_at=launched)[1]
            if why is None and proc == "m1_run":
                why = recording_problem(run_dir)
            if why is None and (sentinel_fresh(now=now())[1] is not None):
                why = "sentinel_not_fresh"
            return why

        why = wait_for(check, timeout_s=timeout_s, handle=handle, sleep=sleep, monotonic=monotonic)
        if why is not None:
            say(f"FAILED step {number}: {proc} not ready ({why}); nothing after it started "
                f"(pid {handle.pid} is left as is; log {rel(logs_dir, root)})")
            entry["state"] = "not_ready"
            publish(f"{proc}: {why}")
            return EXIT_NOT_READY
        record, _ = fresh_beat(proc, timeout_s=timeout_s, now=now())
        entry["state"], entry["heartbeat_pid"] = "ready", record["pid"] if record else None
        if not publish():
            return EXIT_NOT_READY
        say(f"step {number} {proc}: ready")

    if dry_run:
        say("would print `python -m src.halt_store status` and stop; nothing was started, created or appended")
        return EXIT_OK
    current.data["status"] = "started"
    if not publish():
        return EXIT_NOT_READY
    say("all started. Existing halts are NOT resumed by this script; `python -m src.halt_store status`:")
    try:
        halt_store.main(["status"])
    except Exception as exc:  # noqa: BLE001 - the status print is advice, never a reason to stop anything
        say(f"halt status unavailable ({type(exc).__name__}): run `python -m src.halt_store status`")
    say("Operator (家伟): handle each active halt by the M2 runbook §6.3, one resume at a time, after the "
        "heartbeats have been fresh for 10 min. Bots never resume.")
    return EXIT_OK


# ---------------------------------------------------------------- CLI

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.m2_start",
        description="M2 start order on NoisePC, paper only: Sentinel first (fresh heartbeat verified), then "
                    "m1_run in a new runs/m2 run dir, paper_cycle on it, m2_runner once it exists. Never "
                    "resumes, reads no credential, registers nothing.",
        epilog="Exit codes: 0 started (or --dry-run plan ok); 2 usage; 3 refused, nothing started; 4 the "
               "Sentinel never beat fresh, nothing else started; 5 chain append failed or a later process "
               "not ready, nothing after it started; 6 config/risk.toml off its pins.",
    )
    parser.add_argument("--reason", required=True, help="why this start (boot, reboot, crash, manual ...)")
    parser.add_argument("--m2-end", default=None,
                        help="UTC end of the M2 window, ISO 8601 (first start; later starts read it from the chain)")
    parser.add_argument("--dry-run", action="store_true", help="print the plan only: start, create and append nothing")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return EXIT_OK if exc.code == 0 else EXIT_USAGE
    return start(reason=args.reason, m2_end=args.m2_end, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
