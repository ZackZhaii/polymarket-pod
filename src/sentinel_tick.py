"""Task AP (Poly哨兵 design gaps 2, 4, 12, 13): the resident Sentinel tick and its event matrix.

    python -m src.sentinel_tick            # resident: one tick every SENTINEL_TICK_S (30 s)
    python -m src.sentinel_tick --once     # one check-only tick, no Sentinel heartbeat (exit 3 if it raised)

Each tick, in this order (paper only: no network, no orders, no exchange action):

1. The Sentinel's own heartbeat (logs/heartbeat/sentinel.json). Once it has beaten once, every
   consumer (RiskGate, Maker, paper_cycle via src/halt_store.py) treats a Sentinel heartbeat older
   than SENTINEL_HEARTBEAT_TIMEOUT_S (or missing, corrupt, or finished) as a global halt.
2. config/risk.toml, read-only: live_enabled not false, mode not paper, require_human_gate not true,
   or a sha256 other than the pin is a GLOBAL halt (SEV-1).
3. Every known process heartbeat (PROC_SPECS): liveness, pin fields, exit code, escalation counts.
   A heartbeat that is unreadable, corrupt, malformed, future-stamped or lost after it was written
   once is stale = a GLOBAL halt (fail closed), whatever the process.
4. The paper ledger, read-only: ledger_store.verify problems (or an unreadable ledger) are a
   GLOBAL halt.

Event matrix (design table; ``alert`` = one line in logs/alerts.jsonl plus a Sentinel->Operator
``signal`` on the bus with ``alert``; a failed bus write writes logs/HALT, a global halt):

==================  =========================================================================
global halt         ws_recorder / m1_run exit 4 or crash; endgame_scan exit 4; loop_claim exit 2;
                    m2_runner exit 4 / 6 or crash, m2_runner heartbeat stale (HEARTBEAT_RUNNER_TIMEOUT_S);
                    scanner / ws_recorder / m1_run exit 6 and paper_cycle exit 1 (pin drift, Task AS);
                    resolutions exit 6 (pin drift) and 5 (bad snapshot clock) (Task AV);
                    ws_recorder / m1_run heartbeat stale (HEARTBEAT_RECORDER_TIMEOUT_S);
                    halt_consumer heartbeat stale (HEARTBEAT_CONSUMER_TIMEOUT_S);
                    paper_cycle heartbeat stale for HALT_FACTOR x its timeout;
                    any heartbeat unreadable / corrupt / malformed / lost; a running heartbeat
                    stamped with live_enabled true, mode not paper or another risk sha;
                    risk.toml pins (step 2); ledger invalid (step 4)
market halt         one asset with RECORDER_GAP_RESYNC_HALT_COUNT gap resyncs in
                    RECORDER_ESCALATION_WINDOW_S (its market; also an alert)
alert               scanner exit 1 / 2; scanner exit 3 SCANNER_INCOMPLETE_CONSECUTIVE times in a
                    row; scanner / endgame_scan / loop_claim / resolutions start with no finish after
                    SCANNER_HANG_S; no successful scan for SCANNER_MAX_SUCCESS_AGE_S; m1_run exit
                    1 / 2 / 3; m1_rest heartbeat stale (HEARTBEAT_REST_TIMEOUT_S) or non-zero exit;
                    paper_cycle heartbeat stale (HEARTBEAT_PAPER_CYCLE_TIMEOUT_S) or crash;
                    RECORDER_RECONNECT_ALERT_COUNT reconnects in the window; any other non-zero
                    exit; a crash of a process not listed above; m2_runner paused longer than
                    RUNNER_PAUSED_ALERT_S (or paused with no readable paused_since)
log only            m1_run exit 5 (the Operator stopped the run)
==================  =========================================================================

Halts go through sentinel.halt (persisted in the halt store first; released only by an Operator
resume). The Sentinel never resumes anything. A continuing condition (a lost heartbeat, a pin drift,
an invalid ledger, a gap-resync storm) that is still there after the Operator resumed its halt is
halted again on the next tick; a one-off exit event is halted once per process run (the tick state
logs/sentinel_tick.json remembers it; if that file is unreadable the tick alerts and handles the
events again, fail closed). To retire a process for good, let it finish cleanly (a finished
heartbeat is not checked for liveness) or, as the Operator, remove its heartbeat file and marker.

Exit codes, by (module, code) (design gap 4: they collide across modules, so never read alone).
The registry is docs/EXIT_CODES.md (Task AS); tests/test_exit_codes.py and the guard check
``exit_codes`` keep it, the code and EXIT_EVENTS below in agreement.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import halt_store, heartbeat, ledger_store, sentinel  # noqa: E402
from src.podlib import LOG_DIR, PAPER_PATH, append_jsonl, bus  # noqa: E402
from src.thresholds import (  # noqa: E402
    HEARTBEAT_CONSUMER_TIMEOUT_S,
    HEARTBEAT_PAPER_CYCLE_HALT_FACTOR,
    HEARTBEAT_PAPER_CYCLE_TIMEOUT_S,
    HEARTBEAT_RECORDER_TIMEOUT_S,
    HEARTBEAT_REST_TIMEOUT_S,
    HEARTBEAT_RUNNER_TIMEOUT_S,
    RECORDER_GAP_RESYNC_HALT_COUNT,
    RECORDER_RECONNECT_ALERT_COUNT,
    RUNNER_PAUSED_ALERT_S,
    SCANNER_HANG_S,
    SCANNER_INCOMPLETE_CONSECUTIVE,
    SCANNER_MAX_SUCCESS_AGE_S,
    SENTINEL_TICK_S,
)

PINNED_RISK_SHA256 = "0aea23af1e938d330b5b4fab2c6abdec9f475f16807d177a5860a4da32aebc04"
ALERTS_PATH = LOG_DIR / "alerts.jsonl"
TICK_STATE_PATH = LOG_DIR / "sentinel_tick.json"
TICK_STATE_VERSION = 1

ALERT = "alert"
GLOBAL = "global_halt"
MARKET = "market_halt"
LOG = "log"

EXIT_OK = 0
EXIT_RAISED = 3  # --once: something was halted or alerted this tick
EXIT_USAGE = 2
HANDLED_KEEP = 50  # one-off exit events remembered per process (bookkeeping, not a threshold)


@dataclass(frozen=True)
class ProcSpec:
    timeout_s: float
    on_stale: str
    clock: str = "ts"  # "started_at" for start/finish CLIs: a start with no finish is hung
    halt_after_s: float | None = None  # escalate a stale ALERT to a GLOBAL halt after this


PROC_SPECS: dict[str, ProcSpec] = {
    "ws_recorder": ProcSpec(HEARTBEAT_RECORDER_TIMEOUT_S, GLOBAL),
    "m1_run": ProcSpec(HEARTBEAT_RECORDER_TIMEOUT_S, GLOBAL),
    "m1_rest": ProcSpec(HEARTBEAT_REST_TIMEOUT_S, ALERT),
    "scanner": ProcSpec(SCANNER_HANG_S, ALERT, clock="started_at"),
    "endgame_scan": ProcSpec(SCANNER_HANG_S, ALERT, clock="started_at"),
    "loop_claim": ProcSpec(SCANNER_HANG_S, ALERT, clock="started_at"),
    "resolutions": ProcSpec(SCANNER_HANG_S, ALERT, clock="started_at"),  # Task AV
    "paper_cycle": ProcSpec(HEARTBEAT_PAPER_CYCLE_TIMEOUT_S, ALERT,
                            halt_after_s=HEARTBEAT_PAPER_CYCLE_TIMEOUT_S * HEARTBEAT_PAPER_CYCLE_HALT_FACTOR),
    "halt_consumer": ProcSpec(HEARTBEAT_CONSUMER_TIMEOUT_S, GLOBAL),
    # Issue #119 (Task CE): the M2 paper runner. Stale = global halt; paused too long = alert (check_proc).
    "m2_runner": ProcSpec(HEARTBEAT_RUNNER_TIMEOUT_S, GLOBAL),
}

# (proc, exit code) -> action. Any other non-zero code is an ALERT; scanner 3 is handled by streak.
EXIT_EVENTS: dict[tuple[str, int], str] = {
    ("scanner", 1): ALERT, ("scanner", 2): ALERT,
    ("ws_recorder", 1): ALERT, ("ws_recorder", 2): ALERT, ("ws_recorder", 3): ALERT, ("ws_recorder", 4): GLOBAL,
    ("m1_run", 1): ALERT, ("m1_run", 2): ALERT, ("m1_run", 3): ALERT, ("m1_run", 4): GLOBAL, ("m1_run", 5): LOG,
    ("endgame_scan", 4): GLOBAL,
    ("loop_claim", 2): GLOBAL,
    # Task AS (gaps 4, 5): refused to start / stopped on risk.toml pin drift (docs/EXIT_CODES.md).
    ("scanner", 6): GLOBAL, ("ws_recorder", 6): GLOBAL, ("m1_run", 6): GLOBAL, ("paper_cycle", 1): GLOBAL,
    # Task AV: resolutions' old exit 4 split. 6 = pin drift (as scanner); 5 = the snapshot clock is not a
    # zoned timestamp. No design row for the clock; the stricter action (a broken clock, like a heartbeat
    # with a bad time, is a global halt).
    ("resolutions", 5): GLOBAL, ("resolutions", 6): GLOBAL,
    # Issue #119 (Task CE): m2_runner 6 = risk.toml pin drift or a live flag (it recorded the global halt too);
    # 4 = its journal / events / candidates could not be written (as m1_run 4).
    ("m2_runner", 4): GLOBAL, ("m2_runner", 6): GLOBAL,
}
CRASH_IS_GLOBAL = frozenset({"ws_recorder", "m1_run", "halt_consumer", "m2_runner"})


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- tick state

def load_state(path: Path) -> tuple[dict[str, Any], str | None]:
    """``(state, problem)``; a bad file gives a fresh state and its problem (the tick alerts on it)."""
    fresh: dict[str, Any] = {"version": TICK_STATE_VERSION, "raised": [], "handled": {}, "ticks": 0}
    try:
        if not path.exists():
            return fresh, ("tick_state_missing" if heartbeat.state_marker(path).exists() else None)
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return fresh, "tick_state_unreadable"
    except ValueError:
        return fresh, "tick_state_corrupt"
    ok = (isinstance(data, dict) and data.get("version") == TICK_STATE_VERSION
          and isinstance(data.get("raised"), list) and all(isinstance(k, str) for k in data["raised"])
          and isinstance(data.get("handled"), dict)
          and all(isinstance(k, str) and isinstance(v, list) and all(isinstance(x, str) for x in v)
                  for k, v in data["handled"].items())
          and isinstance(data.get("ticks"), int) and not isinstance(data.get("ticks"), bool))
    return (data, None) if ok else (fresh, "tick_state_malformed")


def save_state(path: Path, state: dict[str, Any]) -> str | None:
    return heartbeat.write_record(path, state)


# ---------------------------------------------------------------- one tick

class Tick:
    def __init__(self, now: datetime, *, bus_path: Path | None, quotes_path: Path | None, alerts_path: Path,
                 state: dict[str, Any]) -> None:
        self.now = now
        self.bus_path = bus_path
        self.quotes_path = quotes_path
        self.alerts_path = alerts_path
        self.state = state
        self.raised_before = set(state.get("raised") or [])
        self.raised_now: set[str] = set()
        self.handled: dict[str, list[str]] = {k: list(v) for k, v in (state.get("handled") or {}).items()}
        self.halts: list[str] = []
        self.alerts: list[str] = []
        self.logs: list[str] = []
        self.checked: dict[str, str] = {}

    # -- actions
    def halt(self, reason: str, *, market: str = "", extra: dict | None = None) -> None:
        """Halt unless an identical active halt (scope, market, reason prefix) is already stored."""
        scope = halt_store.SCOPE_MARKET if market else halt_store.SCOPE_GLOBAL
        active = halt_store.has_active_halt(scope=scope, market=market, reason_prefix=reason)
        if active is None and scope == halt_store.SCOPE_GLOBAL:
            return  # the halt state is unreadable: that is already a global halt
        if active is True:
            return
        if halt_store.fallback_active() and scope == halt_store.SCOPE_GLOBAL:
            return  # logs/HALT already halts everything; re-halted on the next tick after a resume
        sentinel.halt(reason=reason, market=market, scope=scope, source="Sentinel", to="Executor",
                      bus_path=self.bus_path, quotes_path=self.quotes_path, extra=extra)
        self.halts.append(f"{scope}:{market or '*'}:{reason}")

    def alert(self, key: str | None, code: str, detail: str = "", *, proc: str = "", severity: str = "warn") -> None:
        """Alert once while the condition ``key`` holds (re-armed when it clears); key None: always."""
        if key is not None:
            self.raised_now.add(key)
            if key in self.raised_before:
                return
        row = {"ts": heartbeat.utc_stamp(self.now), "from": "Sentinel", "alert": code, "severity": severity,
               "proc": proc, "detail": detail, "exchange_action": "none"}
        try:
            append_jsonl(self.alerts_path, row)
        except OSError:
            pass
        try:
            bus("Sentinel", "Operator", "signal", "", {"alert": code, "severity": severity, "proc": proc,
                                                        "detail": detail, "exchange_action": "none"},
                path=self.bus_path)
        except Exception as exc:  # noqa: BLE001 - design: a bus write failure is a global halt (logs/HALT)
            halt_store.write_fallback(f"{row['ts']} Sentinel alert {code} proc={proc or '-'} bus_write_failed "
                                      f"{type(exc).__name__}")
        self.alerts.append(code)

    def once(self, proc: str, run_key: str, action: str, code: str, detail: str, *,
             severity: str = "warn") -> None:
        """A one-off event of one process run (an exit code): handled once per run."""
        key = f"{run_key}:{code}"
        done = self.handled.setdefault(proc, [])
        if key in done:
            return
        done.append(key)
        if action == GLOBAL:
            self.halt(f"{code}:{proc}", extra={"proc": proc, "detail": detail})
            self.alert(None, code, detail, proc=proc, severity="sev1")
        elif action == ALERT:
            self.alert(None, code, detail, proc=proc, severity=severity)
        else:
            self.logs.append(f"{proc}:{code}")

    def finish_state(self) -> dict[str, Any]:
        # keep only the alerts whose condition still holds; one-off events stay in ``handled`` (last 50 runs)
        handled = {proc: keys[-HANDLED_KEEP:] for proc, keys in self.handled.items()}
        return {"version": TICK_STATE_VERSION, "raised": sorted(self.raised_now), "handled": handled,
                "ticks": int(self.state.get("ticks") or 0) + 1}

    # -- checks
    def check_risk(self, risk_path: Path | None) -> None:
        ident = heartbeat.risk_identity(risk_path)
        problems = []
        if ident["live_enabled"] is not False:
            problems.append(f"live_enabled={ident['live_enabled']!r}")
        if ident["mode"] != "paper":
            problems.append(f"mode={ident['mode']!r}")
        if ident["require_human_gate"] is not True:
            problems.append(f"require_human_gate={ident['require_human_gate']!r}")
        if ident["risk_sha256"] != PINNED_RISK_SHA256:
            problems.append("risk_sha256_not_pinned")
        # Task AS (gap 6): every pinned key of the file, its types and no unknown key (src/pins.py).
        from src.pins import risk_file_problems

        problems += [p for p in risk_file_problems(risk_path) if p not in problems]
        if problems:
            detail = ", ".join(problems)
            self.halt("risk_toml_pin_drift", extra={"detail": detail, "severity": "sev1"})
            self.alert("risk_toml_pin_drift", "risk_toml_pin_drift", detail, severity="sev1")

    def check_ledger(self, ledger_path: Path) -> None:
        try:
            _, read = ledger_store.read_ledger(ledger_path)
            problems = ledger_store.verify(read)
        except Exception as exc:  # noqa: BLE001 - an unreadable ledger is a halt (fail closed)
            problems = [f"ledger_unreadable:{type(exc).__name__}"]
        if problems:
            detail = ", ".join(str(p) for p in problems[:10])
            self.halt("ledger_invalid", extra={"detail": detail})
            self.alert("ledger_invalid", "ledger_invalid", detail, severity="sev1")

    def check_proc(self, proc: str, spec: ProcSpec, directory: Path | None) -> None:
        record, problem = heartbeat.read_beat(heartbeat.beat_path(proc, directory), proc)
        if problem is not None:
            self.checked[proc] = problem
            self.halt(f"{problem}:{proc}")
            self.alert(f"{problem}:{proc}", problem, proc=proc, severity="sev1")
            return
        if record is None:
            self.checked[proc] = "never_ran"
            return
        run_key = f"{record['started_at']}:{record['pid']}"
        state = record["state"]
        self.checked[proc] = state
        # pin fields of a live process
        if state != heartbeat.STATE_FINISHED and (
                record["live_enabled"] is not False or record["mode"] != "paper"
                or record["risk_sha256"] != PINNED_RISK_SHA256):
            detail = f"live_enabled={record['live_enabled']!r} mode={record['mode']!r}"
            self.halt(f"heartbeat_pin_drift:{proc}", extra={"detail": detail})
            self.alert(f"heartbeat_pin_drift:{proc}", "heartbeat_pin_drift", detail, proc=proc, severity="sev1")
        if state == heartbeat.STATE_FINISHED:
            self._exit_event(proc, record, run_key)
        else:
            if state == heartbeat.STATE_CRASHED:
                action = GLOBAL if proc in CRASH_IS_GLOBAL else ALERT
                self.once(proc, run_key, action, "process_crashed", f"seq={record['seq']}", severity="sev1")
            self._liveness(proc, spec, record)
        if proc in ("ws_recorder", "m1_run") and state != heartbeat.STATE_FINISHED:
            self._escalation(proc, record)
        if proc == "scanner":
            self._scanner_success(record)
        if proc == "m2_runner" and state == heartbeat.STATE_PAUSED:
            self._runner_paused(record)

    def _liveness(self, proc: str, spec: ProcSpec, record: dict[str, Any]) -> None:
        age = heartbeat.beat_age_s(record, self.now, spec.clock)
        if age is None:  # unparseable or in the future: fail closed
            self.halt(f"heartbeat_bad_time:{proc}")
            self.alert(f"heartbeat_bad_time:{proc}", "heartbeat_bad_time", proc=proc, severity="sev1")
            return
        code = "process_hung" if spec.clock == "started_at" else "heartbeat_stale"
        if age > spec.timeout_s:
            detail = f"age_s={age:.0f} timeout_s={spec.timeout_s:g}"
            if spec.on_stale == GLOBAL:
                self.halt(f"{code}:{proc}", extra={"detail": detail})
                self.alert(f"{code}:{proc}", code, detail, proc=proc, severity="sev1")
            else:
                self.alert(f"{code}:{proc}", code, detail, proc=proc)
            if spec.halt_after_s is not None and age > spec.halt_after_s:
                self.halt(f"{code}_escalated:{proc}", extra={"detail": detail})
                self.alert(f"{code}_escalated:{proc}", f"{code}_escalated", detail, proc=proc, severity="sev1")

    def _exit_event(self, proc: str, record: dict[str, Any], run_key: str) -> None:
        code = record["exit_code"]
        if code == 0:
            return
        if proc == "scanner" and code == 3:
            streak = record["progress"].get("exit_streak")
            valid = isinstance(streak, int) and not isinstance(streak, bool) and streak >= 1
            if not valid or streak >= SCANNER_INCOMPLETE_CONSECUTIVE:
                self.once(proc, run_key, ALERT, "scanner_incomplete_repeated", f"exit 3 streak={streak!r}")
            else:
                self.once(proc, run_key, LOG, "scanner_incomplete", "exit 3 once")
            return
        action = EXIT_EVENTS.get((proc, code), ALERT)
        self.once(proc, run_key, action, f"exit_{code}", f"{proc} exit {code}",
                  severity="sev1" if action == GLOBAL else "warn")

    def _escalation(self, proc: str, record: dict[str, Any]) -> None:
        progress = record["progress"]
        reconnects = progress.get("reconnects_in_window", 0)
        gaps = progress.get("gap_resyncs_in_window", {})
        markets = progress.get("asset_markets", {})
        if (isinstance(reconnects, bool) or not isinstance(reconnects, int) or not isinstance(gaps, dict)
                or not isinstance(markets, dict)):
            self.halt(f"heartbeat_malformed:{proc}")
            self.alert(f"heartbeat_malformed:{proc}", "heartbeat_malformed", "progress", proc=proc, severity="sev1")
            return
        if reconnects >= RECORDER_RECONNECT_ALERT_COUNT:
            self.alert(f"reconnect_storm:{proc}", "reconnect_storm", f"reconnects_in_window={reconnects}", proc=proc)
        for asset, count in sorted(gaps.items()):
            usable = isinstance(count, int) and not isinstance(count, bool)
            if usable and count < RECORDER_GAP_RESYNC_HALT_COUNT:
                continue  # a malformed count falls through: treated as a storm (fail closed)
            market = markets.get(asset)
            detail = f"asset={asset} gap_resyncs_in_window={count!r}"
            if isinstance(market, str) and market:
                self.halt(f"recorder_gap_resyncs:{asset}", market=market, extra={"detail": detail})
            self.alert(f"gap_resync_storm:{proc}:{asset}", "gap_resync_storm", detail, proc=proc)

    def _runner_paused(self, record: dict[str, Any]) -> None:
        """Issue #119: an m2_runner paused (a halt, the daily loss breaker, disk) longer than RUNNER_PAUSED_ALERT_S
        alerts the Operator. A paused heartbeat without a readable ``paused_since`` alerts too (fail closed)."""
        since = record["progress"].get("paused_since")
        age = heartbeat.beat_age_s({"t": since}, self.now, "t") if isinstance(since, str) else None
        if age is None or age > RUNNER_PAUSED_ALERT_S:
            self.alert("runner_paused_too_long:m2_runner", "runner_paused_too_long",
                       f"paused_since={since!r} reason={record['progress'].get('paused_reason')!r}", proc="m2_runner")

    def _scanner_success(self, record: dict[str, Any]) -> None:
        last = record["progress"].get("last_success_ts")
        if last is None:
            if record["state"] == heartbeat.STATE_FINISHED:
                self.alert("scanner_no_success", "scanner_no_success", "no successful scan recorded", proc="scanner")
            return
        age = heartbeat.beat_age_s({"t": last}, self.now, "t")
        if age is None or age > SCANNER_MAX_SUCCESS_AGE_S:
            self.alert("scanner_success_too_old", "scanner_success_too_old", f"last_success_ts={last!r}",
                       proc="scanner")


def tick(*, now: datetime | None = None, heartbeat_dir: Path | None = None, bus_path: Path | None = None,
         quotes_path: Path | None = None, alerts_path: Path | None = None, state_path: Path | None = None,
         ledger_path: Path | None = None, risk_path: Path | None = None, own: Any = None) -> dict[str, Any]:
    """Run one Sentinel tick. Never raises for I/O; returns what it did."""
    moment = now or _now()
    if own is not None:
        own.beat(heartbeat.STATE_RUNNING, {"phase": "tick"}, force=True)
    st_path = Path(state_path) if state_path is not None else Path(TICK_STATE_PATH)
    state, state_problem = load_state(st_path)
    t = Tick(moment, bus_path=bus_path, quotes_path=quotes_path,
             alerts_path=Path(alerts_path) if alerts_path is not None else Path(ALERTS_PATH), state=state)
    if state_problem is not None:
        t.alert(state_problem, state_problem, "exit events are handled again", severity="warn")
    t.check_risk(risk_path)
    for proc, spec in PROC_SPECS.items():
        try:
            t.check_proc(proc, spec, heartbeat_dir)
        except Exception as exc:  # noqa: BLE001 - a broken check is a halt (fail closed)
            t.halt(f"heartbeat_check_failed:{proc}")
            t.alert(f"heartbeat_check_failed:{proc}", "heartbeat_check_failed", type(exc).__name__, proc=proc,
                    severity="sev1")
    t.check_ledger(Path(ledger_path) if ledger_path is not None else Path(PAPER_PATH))
    write_problem = save_state(st_path, t.finish_state())
    if write_problem is not None:
        t.alert("tick_state_write_failed", "tick_state_write_failed", severity="warn")
    return {"halts": t.halts, "alerts": t.alerts, "logs": t.logs, "checked": t.checked,
            "state_problem": state_problem, "write_problem": write_problem}


# ---------------------------------------------------------------- CLI (resident loop)

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.sentinel_tick",
        description="Resident Sentinel: heartbeat, risk.toml pins, process heartbeats, ledger; halts and alerts "
                    "only (never resumes, no exchange action).",
        epilog="Exit codes: 0 ran (--once: nothing raised); 2 usage; 3 --once raised a halt or an alert.",
    )
    parser.add_argument("--once", action="store_true", help="one tick, then exit")
    parser.add_argument("--max-ticks", type=int, default=0, help="stop after N ticks (0 = run until stopped)")
    return parser


def main(argv: list[str] | None = None, *, sleep: Callable[[float], None] = time.sleep,
         monotonic: Callable[[], float] = time.monotonic) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return EXIT_OK if exc.code == 0 else EXIT_USAGE
    if args.max_ticks < 0:
        return EXIT_USAGE
    # --once is a check-only run: it writes no Sentinel heartbeat, so it never registers as the
    # resident Sentinel (whose later absence would be a global halt for every consumer).
    own = None if args.once else heartbeat.Heartbeat("sentinel", every_s=SENTINEL_TICK_S, monotonic=monotonic)
    ticks = 0
    raised = False
    try:
        while True:
            started = monotonic()
            result = tick(own=own)
            ticks += 1
            raised = raised or bool(result["halts"] or result["alerts"])
            print(f"sentinel tick={ticks} halts={len(result['halts'])} alerts={len(result['alerts'])} "
                  f"exchange_action=none")
            if args.once or (args.max_ticks and ticks >= args.max_ticks):
                break
            sleep(max(0.0, SENTINEL_TICK_S - (monotonic() - started)))
    except BaseException:
        if own is not None:
            own.finish(None, {"ticks": ticks}, crashed=True)
        raise
    # A planned stop is still a lost Sentinel for the consumers (fail closed) until it runs again.
    if own is not None:
        own.finish(0, {"ticks": ticks})
    return EXIT_RAISED if (args.once and raised) else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
