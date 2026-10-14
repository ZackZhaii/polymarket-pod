"""Issue #119 (Task CE, M2-C1) + issue #120 (Task CJ, M2-C2): the M2 paper runner. Paper only; there is no executor.

    python -m src.m2_runner --run-dir runs/m2 [--max-runtime 604800] [--once]

One process, one loop (Poly预测's M2 runner spec, /workspace/pod-bots/poly-forecast-m2-runner-spec.md, C.1, C.2,
C.4, C.6, C.8; Poly执行's no-live audit F2 / T1-T13 on the issue):

    scanner (Gamma)  ->  forecast inbox (src/forecast_inbox: seal first)  ->  fee (src/fee_source, Gamma)
    ->  CLOB /book for both tokens (read only after the seal)  ->  intent_builder.build_order_intent(paper=True)
    ->  paper_ledger.commit_if_allowed (RiskGate decides in that same call; nothing else writes the ledger)

**Every iteration starts with the checks, before any getter, seal or commit** (``preflight``). In this order:

1. ``<run dir>/KILL`` (the Operator's file): stop. One alert line ``kill_file`` in logs/alerts.jsonl (issue #247),
   heartbeat ``finished``, exit 0.
2. config/risk.toml: ``live_enabled is False``, ``mode == "paper"`` and ``require_human_gate is True``, else a
   global halt ``m2_live_flag`` is recorded and the runner exits 6.
3. ``pins.risk_file_problems()`` on every iteration (not PinWatch's 60 s throttle): drift records the global halt
   ``risk_toml_pin_drift`` (``pins.record_drift``) and the runner exits 6.
4. Halts: any global halt (``halt_store.global_halt_active``: a stored global halt, a corrupt or unreadable halt
   state file, ``logs/HALT``, a lost or never-seen Sentinel) pauses the runner. It keeps beating ``paused`` and
   waits; it never resumes anything (only the Operator does, with ``halt_store resume``). A market halt skips
   that market's signals before any getter.
5. The paper ledger: ``ledger_store.read_ledger`` + ``verify``. A problem records the global halt
   ``invalid_risk_state`` (as paper_ledger does) and pauses.
6. Independent CLOB /time: the complete local-minus-server uncertainty interval must fit inside ±5 s. An
   unverifiable clock (``clock_unverified``) or a confirmed excessive skew (``clock_skew``) pauses after
   settlement (#206) and before any scan or intent, even with no pending signal.
7. The daily loss breaker: today's (UTC) fees, gross cost losses, closed reference topups and open-leg losses
   at or above ``max_daily_loss_pct x PAPER_EQUITY_USD``: paused (``daily_loss_breaker``) for the rest of the UTC
   day; the next day counts from zero again, so it resumes by itself. RiskGate vetoes these anyway; this only
   stops earlier.
8. Disk: less than DISK_MIN_FREE_GB free on the run dir's volume, or the run dir above RUNNER_DISK_BUDGET_GB:
   paused (``disk_low`` / ``disk_budget``), one event line.

KILL comes first and the live / pin checks before the halts, so a drifted file exits 6 even while a halt is in
force (stricter than the order listed in the issue; each case still does nothing else that iteration).

Network: the runner reaches the network only through its getters (``Getters``; production: ``podlib.public_get``
for both). Every URL first passes ``url_allowed``: GET only, https, Gamma ``/markets`` or ``/markets/keyset``, or
CLOB ``/book`` or exact ``/time``; no user info, port or order / auth word. ``main`` also installs ``network_guard``,
a socket-level tripwire for the process: name resolution for any host but those two raises. ``/clob-markets`` is not used (its
allowlisting is a KEY issue) and no WebSocket is opened.

There is no executor. ``EXECUTOR_ENABLED`` is the literal ``False``, assigned once; no argument, environment
variable or config key reaches it, and the CLI has only ``--run-dir``, ``--max-runtime`` and ``--once``. Every
intent is built with ``paper=True``. Nothing here signs, sends or cancels anything, and no credential is read.

Heartbeat ``logs/heartbeat/m2_runner.json`` (src/heartbeat.py fields; a failed write is the global halt
``heartbeat_write_failed:m2_runner``, issue #79) every HEARTBEAT_RUNNER_EVERY_S, ``paused`` while paused. The
Sentinel watches it (src/sentinel_tick.PROC_SPECS): stale is a global halt, ``paused`` longer than
RUNNER_PAUSED_ALERT_S an alert. Task DQ: inside a tick the heartbeat is also beaten (throttled) after every GET,
and every ``podlib.public_get`` is bounded by PUBLIC_GET_DEADLINE_S, so a 429 storm cannot stale it. KILL is
checked at the start of each tick, so a KILL file stops the runner at most one tick late. Single instance: ``<run dir>/runner.lock`` (pid, host, started_at) is held with an
OS byte lock for the process's life; a second runner exits 3. ``<run dir>/journal.jsonl`` (one fsynced line per
step: sealed, rejected, built, committed, vetoed, skipped) and ``events.jsonl`` record what happened.

Issue #120 (Task CJ, M2-C2): restart recovery (``recover``: journal + ledger, ``seen_keys`` restored, built-but-
not-committed reconciled to the ledger), ``unique_decision_id=True`` on every commit (one allow fill per
``decision_id``), suspend/resume detection (wall gap or wall-vs-monotonic skew), clock-skew pause, and
``book_before_resume`` / stale-book refusal on the decision-time REST /book read (``book.fetch_depth`` +
``intent_builder.book_snapshot_from_clob``). Brier backfill is #121.

Exit codes (docs/EXIT_CODES.md): 0 KILL or --max-runtime reached or --once done; 2 usage; 3 another runner holds
the lock; 4 a journal / event / candidates / KILL alert write failed; 6 risk.toml off its pins or a live flag (global halt
recorded); 7 the start self-check failed (run dir not writable).
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import math
import os
import platform
import shutil
import socket
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import (  # noqa: E402
    book,
    fee_source,
    forecast_inbox,
    forecaster,
    halt_store,
    heartbeat,
    intent_builder,
    ledger_store,
    paper_ledger,
    pins,
    podlib,
    resolution_watch,
    scanner,
    sentinel,
    sentinel_tick,
)
from src.daily_loss import daily_loss  # noqa: E402
from src.book_staleness import INVALID_BOOK_TS, MISSING_BOOK_TS, book_age_problem, parse_book_ts  # noqa: E402
from src.thresholds import (  # noqa: E402
    CLOCK_SKEW_MAX_S,
    DISK_MIN_FREE_GB,
    HEARTBEAT_RUNNER_EVERY_S,
    MAX_BOOK_AGE_S,
    RUNNER_DISK_BUDGET_GB,
    RUNNER_LOOP_S,
    RUNNER_MAX_EVALS_PER_SIGNAL,
    RUNNER_MAX_INGEST_PER_ITER,
    RUNNER_SCAN_EVERY_S,
    RUNNER_SCAN_MAX_AGE_S,
    RUNNER_SUSPEND_GAP_S,
    RUNNER_WALL_MONO_SKEW_S,
)

PROC = "m2_runner"
EXECUTOR_ENABLED = False  # no switch, argument, environment variable or config key changes this (issue #119 F2)

STRATEGY = forecaster.STRATEGY
DEFAULT_MAX_RUNTIME_S = 604800  # spec C.6: one week (the M2 window); the CLI value, not a threshold

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_ALREADY_RUNNING = 3
EXIT_WRITE_FAILED = 4
EXIT_PIN_OR_LIVE = 6
EXIT_SELF_CHECK = 7

LIVE_HALT_REASON = "m2_live_flag"
KILL_ALERT = "kill_file"  # issue #247: the alert code written to logs/alerts.jsonl when KILL stops the runner
LEDGER_HALT_REASON = "invalid_risk_state"

ACT_RUN = "run"
ACT_PAUSE = "pause"
ACT_FINISH = "finish"
ACT_EXIT_PIN = "exit_pin_or_live"
ACT_WRITE_FAILED = "write_failed"

# Read-only endpoints derived from podlib's pins, so this module spells no host.
_GAMMA = urlsplit(podlib.GAMMA_PREFIX)
_BOOK = urlsplit(podlib.BOOK_PREFIX)
_TIME = urlsplit(podlib.TIME_URL)
ALLOWED_GETS = frozenset({
    ("https", _GAMMA.hostname, _GAMMA.path),
    ("https", _GAMMA.hostname, _GAMMA.path + "/keyset"),
    ("https", _BOOK.hostname, _BOOK.path),
    ("https", _TIME.hostname, _TIME.path),
})
ALLOWED_HOSTS = frozenset(host for _, host, _ in ALLOWED_GETS)
BANNED_URL_WORDS = ("/order", "/cancel", "/auth", "api-key", "api_key", "apikey", "balance-allowance", "/ws/user",
                    "relayer", "secret", "passphrase", "signature", "withdraw", "/clob-markets")
DROP_REASONS = frozenset({"stale_signal", "duplicate_intent", "closed", "expired", "near_expiry",
                          "not_accepting_orders", "market_mismatch", "token_mismatch"})


class RefusedURL(RuntimeError):
    """A getter was asked for a URL outside ALLOWED_GETS; nothing was fetched."""


class WriteFailed(RuntimeError):
    """A journal / event / candidates / KILL alert write failed (exit 4)."""


def url_allowed(url: object) -> bool:
    """GET-able by the runner: https, an allowed (host, path), no user info / port / order or auth word."""
    if not isinstance(url, str):
        return False
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    if parts.scheme != "https" or parts.username or parts.password or port is not None or parts.fragment:
        return False
    lowered = url.lower()
    if any(word in lowered for word in BANNED_URL_WORDS):
        return False
    if parts.path == _TIME.path:
        return url == podlib.TIME_URL
    return (parts.scheme, (parts.hostname or "").lower(), parts.path) in ALLOWED_GETS


@dataclass
class Getters:
    """The runner's only way out. Each is ``get(url) -> parsed JSON``; production: podlib.public_get (GET only)."""

    gamma: Callable[[str], Any] = podlib.public_get
    book: Callable[[str], Any] = podlib.public_get


class GuardedGetter:
    """Calls ``inner`` only for an allowed URL; counts calls and refusals.

    Task DQ (Poly哨兵 on #309): ``after`` (optional, never raises into the caller) runs after every fetched URL,
    answered or failed: the runner beats its heartbeat there, so a tick of many GETs (the /time check, Gamma
    settlement, every scan page, every signal's fee and /book reads) never goes HEARTBEAT_RUNNER_TIMEOUT_S
    without a beat while each GET is bounded by PUBLIC_GET_DEADLINE_S."""

    def __init__(self, inner: Callable[[str], Any], after: Callable[[], None] | None = None) -> None:
        self.inner = inner
        self.after = after
        self.calls: list[str] = []
        self.refused: list[str] = []

    def __call__(self, url: str) -> Any:
        if not url_allowed(url):
            self.refused.append(str(url))
            raise RefusedURL(f"m2_runner refuses {url!r}")
        self.calls.append(url)
        try:
            return self.inner(url)
        finally:
            if self.after is not None:
                try:
                    self.after()
                except Exception:  # noqa: BLE001 - a heartbeat hook never changes a GET's outcome
                    pass


@dataclass
class RunnerPaths:
    """Everything the runner writes, under one run dir (tests use a temp dir)."""

    run_dir: Path
    inbox: Path
    archive: Path
    journal: Path
    events: Path
    kill_file: Path
    lock_file: Path
    candidates: Path

    @classmethod
    def under(cls, run_dir: Path) -> RunnerPaths:
        run_dir = Path(run_dir)
        return cls(run_dir=run_dir, inbox=run_dir / "inbox" / "pending", archive=run_dir / "inbox" / "archive",
                   journal=run_dir / "journal.jsonl", events=run_dir / "events.jsonl", kill_file=run_dir / "KILL",
                   lock_file=run_dir / "runner.lock", candidates=run_dir / "candidates.jsonl")


@dataclass
class Pending:
    signal: intent_builder.ProbabilitySignal
    evals: int = 0


@dataclass
class RunnerState:
    paths: RunnerPaths
    risk_path: Path | None = None
    forecasts_path: Path | None = None  # None: forecaster.FORECASTS_PATH at the time of use
    bus_path: Path | None = None  # None: podlib.BUS_PATH at the time of use
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(timezone.utc))
    monotonic: Callable[[], float] = time.monotonic
    iteration: int = 0
    markets: dict[str, dict[str, Any]] = field(default_factory=dict)
    scan_at: datetime | None = None
    pending: dict[str, Pending] = field(default_factory=dict)
    seen_keys: set[str] = field(default_factory=set)
    rejected_files: dict[str, tuple[int, int]] = field(default_factory=dict)
    fee_cache: fee_source.FeeCache | None = None
    fee_getter: Callable[[str], Any] | None = None
    paused_reason: str | None = None
    paused_since: str | None = None
    # Task DA (#225 follow-up): the clock anomaly the runner currently holds ("clock_skew"; #253 can set
    # "clock_unverified"). Kept apart from paused_reason so a later preflight pause does not forget it.
    # Task DC (Poly风控 on #274): fail closed from startup / restart: "clock_unverified" until a tick fetched at
    # least one book within CLOCK_SKEW_MAX_S of the local clock (and did not re-pause).
    clock_anomaly: str | None = "clock_unverified"
    # set during a tick when a fetched book passed the skew check; only such a tick may clear clock_anomaly
    tick_clock_verified: bool = False
    tick_clock_vetoed: bool = False  # Poly风控 P1 on #275: any book with an invalid book_ts this tick
    tick_time_ok: bool = False  # #253: this tick's CLOB /time check passed (|skew| <= CLOCK_SKEW_MAX_S)
    # Issue #289 (Task DJ): heartbeat observation only, never read by a decision. When the current clock anomaly
    # episode began (kept across a clock_unverified <-> clock_skew switch, None once clock_anomaly clears), and
    # the last CLOB /time check (ts, result, low_s / high_s interval, rtt_s), None before the first check.
    clock_anomaly_since: str | None = None
    last_time_check: dict[str, Any] | None = None
    counters: dict[str, int] = field(default_factory=lambda: {
        "forecasts_sealed_total": 0, "forecasts_rejected_total": 0, "intents_built_total": 0, "allows_total": 0,
        "vetoes_total": 0, "missing_fee_total": 0, "scans_total": 0, "scan_failures_total": 0})
    last_book_age_s_max: float | None = None
    disk_free_gb: float | None = None
    ledger_rows: int | None = None
    # Issue #120 (Task CJ): suspend / resume tracking
    last_iter_wall: datetime | None = None
    last_iter_mono: float | None = None
    last_resume_at: datetime | None = None  # books fetched before this are refused
    resume_count: int = 0
    force_rescan: bool = False
    kill_alerted: bool = False  # issue #247: the KILL alert line was written for this stop (at most once)
    # Task DQ: called after every GET of a tick (GuardedGetter.after); _run sets it to a throttled heartbeat beat.
    on_get: Callable[[], None] | None = None


@dataclass
class Preflight:
    action: str
    reasons: list[str]
    disk_free_gb: float | None = None
    ledger_rows: int | None = None


@dataclass
class IterationReport:
    action: str
    reasons: list[str] = field(default_factory=list)
    sealed: int = 0
    rejected: int = 0
    built: int = 0
    allows: int = 0
    vetoes: int = 0
    skipped: int = 0
    fills: list[dict[str, Any]] = field(default_factory=list)
    paused_reason: str | None = None


# ---------------------------------------------------------------- small I/O helpers

def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def append_synced(path: Path, row: dict[str, Any]) -> None:
    """One JSON line, flushed and fsynced. Raises WriteFailed."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = (json.dumps(row, ensure_ascii=False, separators=(",", ":"), default=str) + "\n").encode("utf-8")
        with open(path, "ab") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise WriteFailed(f"{path.name}: {type(exc).__name__}") from exc


def journal(state: RunnerState, step: str, now: datetime, **fields: Any) -> None:
    append_synced(state.paths.journal, {"ts": _stamp(now), "step": step, **fields})


def event(state: RunnerState, kind: str, now: datetime, **fields: Any) -> None:
    append_synced(state.paths.events, {"ts": _stamp(now), "event": kind, **fields})


def kill_alert(state: RunnerState, now: datetime) -> None:
    """Issue #247 (Task DK): one line in the Sentinel's logs/alerts.jsonl (``sentinel_tick.ALERTS_PATH``, the same
    row shape as ``sentinel_tick.Tick.alert``) when KILL stops the runner, at most once per stop. Only a record:
    the exit code stays 0, nothing is resumed or restarted (auto-restart is KEY #243). Raises WriteFailed.

    Issue #305 (Task DQ): any ``Exception`` while building or writing the row (not only OSError) is a write
    failure (exit 4); KeyboardInterrupt / SystemExit are not caught."""
    if state.kill_alerted:
        return
    try:
        row = {"ts": _stamp(now), "from": PROC, "alert": KILL_ALERT, "severity": "info", "proc": PROC,
               "detail": f"{state.paths.kill_file} found; runner stopped, exit_code={EXIT_OK}",
               "exchange_action": "none"}
        podlib.append_jsonl(Path(sentinel_tick.ALERTS_PATH), row)
    except Exception as exc:  # noqa: BLE001 - issue #305: every failure here is a write failure (exit 4)
        raise WriteFailed(f"alerts.jsonl: {type(exc).__name__}") from exc
    state.kill_alerted = True


def _risk_cfg(risk_path: Path | None) -> dict[str, Any]:
    target = Path(risk_path) if risk_path is not None else Path(podlib.RISK_PATH)
    return podlib.parse_risk_toml(target.read_text(encoding="utf-8"))


def _dir_bytes(root: Path) -> int:
    total = 0
    for folder, _, files in os.walk(root):
        for name in files:
            try:
                total += os.stat(os.path.join(folder, name)).st_size
            except OSError:
                continue
    return total


def _record_global_halt(reason: str, extra: dict[str, Any]) -> None:
    """One global halt for ``reason`` unless one is already in force (or the state is unreadable). Never raises."""
    try:
        active = halt_store.has_active_halt(scope=halt_store.SCOPE_GLOBAL, reason_prefix=reason)
        if active is not False or halt_store.fallback_active():
            return
        sentinel.halt(reason=reason, scope=halt_store.SCOPE_GLOBAL, source="Sentinel", to="Executor",
                      extra={"detected_by": PROC, "live_enabled": False, **extra})
    except Exception as exc:  # noqa: BLE001 - last resort: logs/HALT (a global halt)
        try:
            halt_store.write_fallback(f"{PROC} {reason} record_failed {type(exc).__name__}")
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------- 1. the checks (before any action)

def live_flag_problems(risk_path: Path | None = None) -> list[str]:
    ident = heartbeat.risk_identity(risk_path)
    problems = []
    if ident["live_enabled"] is not False:
        problems.append(f"live_enabled={ident['live_enabled']!r}")
    if ident["mode"] != "paper":
        problems.append(f"mode={ident['mode']!r}")
    if ident["require_human_gate"] is not True:
        problems.append(f"require_human_gate={ident['require_human_gate']!r}")
    return problems


def runner_clock_ok(state: "RunnerState") -> bool:
    """Task DA: True only after a clock check has passed and while the runner holds no clock anomaly (generic:
    any non-None reason is not ok). Before the first check (right after startup) it is False: fail closed."""
    return state.clock_anomaly is None


def preflight(paths: RunnerPaths, *, now: datetime, risk_path: Path | None = None, clock_ok: bool = False,
              ledger_path: Path | None = None, settle: Callable[[], list[str]] | None = None,
              clock_check: Callable[[], str | None] | None = None) -> Preflight:
    """The C.2 checks, in order; the first failing one decides. Writes no fill itself.

    Issue #206 (Task CO): ``settle`` (the runner's settlement step, ``_settle_tick``) runs after the KILL / live /
    pin / global-halt checks and a verified ledger (so never under a hard halt, a KILL file or
    invalid_risk_state), and before the independent clock certification (CLOB /time, C222) and the daily-loss
    breaker, which then reads the ledger again. A clock_unverified / clock_skew pause therefore still settles. It runs whether or not the breaker or a later check pauses this tick. Its problems
    are a contradiction: invalid_risk_state
    and a global halt. It never clears a pause or a halt; the checks after it decide this tick.
    """
    if paths.kill_file.exists():
        return Preflight(ACT_FINISH, ["kill_file"])
    live = live_flag_problems(risk_path)
    if live:
        _record_global_halt(LIVE_HALT_REASON, {"problems": live, "severity": "sev1"})
        return Preflight(ACT_EXIT_PIN, [LIVE_HALT_REASON, *live])
    try:
        drift = pins.risk_file_problems(risk_path)
    except Exception as exc:  # noqa: BLE001 - a broken check is drift
        drift = [f"pin_check_failed:{type(exc).__name__}"]
    if drift:
        pins.record_drift(PROC, drift, phase="runtime")
        return Preflight(ACT_EXIT_PIN, [pins.PIN_HALT_REASON, *drift])
    halts = halt_store.global_halt_active()
    if halts:
        return Preflight(ACT_PAUSE, ["global_halt", *halts])
    ledger = Path(ledger_path) if ledger_path is not None else Path(paper_ledger.PAPER_PATH)
    try:
        _, read = ledger_store.read_ledger(ledger)
        problems = ledger_store.verify(read)
    except Exception as exc:  # noqa: BLE001 - an unreadable ledger is invalid (fail closed)
        read, problems = None, [f"ledger_unreadable:{type(exc).__name__}"]
    if not problems and read is not None:
        day = daily_loss(read.rows, now.astimezone(timezone.utc).strftime("%Y-%m-%d"), read.ids)
        problems = list(day.problems)
    if problems or read is None:
        _record_global_halt(LEDGER_HALT_REASON, {"ledger_problems": [str(p) for p in problems[:10]]})
        return Preflight(ACT_PAUSE, [LEDGER_HALT_REASON, *[str(p) for p in problems[:10]]])
    if settle is not None:  # issue #206: settlement before the daily-loss breaker (see the docstring)
        problems = [str(p) for p in settle()]
        if not problems:
            try:
                _, read = ledger_store.read_ledger(ledger)
                problems = ledger_store.verify(read)
            except Exception as exc:  # noqa: BLE001 - fail closed
                read, problems = None, [f"ledger_unreadable:{type(exc).__name__}"]
        if problems or read is None:
            _record_global_halt(LEDGER_HALT_REASON, {"ledger_problems": [str(p) for p in problems[:10]],
                                                     "source": "settlement"})
            return Preflight(ACT_PAUSE, [LEDGER_HALT_REASON, *[str(p) for p in problems[:10]]])
    if clock_check is not None:  # PR #253 review: after settlement, so a clock pause never defers it (#206)
        problem = clock_check()
        if problem is not None:
            return Preflight(ACT_PAUSE, [problem], ledger_rows=len(read.rows))
    rows = len(read.rows)
    risk = paper_ledger.daily_risk_loss(read.rows, list(read.ids), _stamp(now), clock_ok=clock_ok)
    if risk["problems"]:
        problems = [str(p) for p in risk["problems"][:10]]
        _record_global_halt(LEDGER_HALT_REASON, {"ledger_problems": problems})
        return Preflight(ACT_PAUSE, [LEDGER_HALT_REASON, *problems], ledger_rows=rows)
    try:
        cap = float(_risk_cfg(risk_path)["max_daily_loss_pct"]) * podlib.PAPER_EQUITY_USD
    except Exception:  # noqa: BLE001 - the pin check above makes this unreachable; fail closed anyway
        return Preflight(ACT_PAUSE, ["daily_loss_cap_unreadable"], ledger_rows=rows)
    total = risk["loss_usd"]
    if total is None or total >= cap:
        return Preflight(ACT_PAUSE, ["daily_loss_breaker", f"daily_loss_usd={total!r} cap_usd={cap:g}"],
                         ledger_rows=rows)
    try:
        free_gb = shutil.disk_usage(paths.run_dir).free / 1e9
        used_gb = _dir_bytes(paths.run_dir) / 1e9
    except OSError as exc:
        return Preflight(ACT_PAUSE, [f"disk_unreadable:{type(exc).__name__}"], ledger_rows=rows)
    if free_gb < DISK_MIN_FREE_GB:
        return Preflight(ACT_PAUSE, ["disk_low", f"free_gb={free_gb:.2f}"], free_gb, rows)
    if used_gb > RUNNER_DISK_BUDGET_GB:
        return Preflight(ACT_PAUSE, ["disk_budget", f"run_dir_gb={used_gb:.2f}"], free_gb, rows)
    return Preflight(ACT_RUN, [], free_gb, rows)


CLOCK_PAUSE_REASONS = frozenset({"clock_unverified", "clock_skew"})


def _clock_check(state: "RunnerState", getter: GuardedGetter, now: datetime | None = None) -> str | None:
    """#253 + Task DA: a clock_unverified / clock_skew result also marks ``state.clock_anomaly``, so later
    ticks' daily-loss evaluation treats the day-start reference as late (fail closed)."""
    measured: dict[str, float | None] = {}
    problem = _clock_problem(state.clock, getter, measured)
    if problem is not None:
        state.clock_anomaly = problem
    else:
        state.tick_time_ok = True  # #253: a clean CLOB /time check (the skew source) this tick
    if now is not None:  # issue #289: observation only (heartbeat), after the decision above
        state.last_time_check = {"ts": _stamp(now), "result": problem or "ok", **{
            key: _rounded(measured.get(key)) for key in ("low_s", "high_s", "rtt_s")}}
        _note_clock_anomaly(state, now)
    return problem


def _rounded(value: float | None) -> float | None:
    return round(value, 3) if isinstance(value, float) and math.isfinite(value) else None


def _note_clock_anomaly(state: "RunnerState", now: datetime) -> None:
    """Issue #289: heartbeat bookkeeping only. Stamp ``clock_anomaly_since`` when an anomaly episode is first
    seen (a clock_unverified <-> clock_skew switch keeps it), clear it with the anomaly. Never changes a decision."""
    if state.clock_anomaly is None:
        state.clock_anomaly_since = None
    elif state.clock_anomaly_since is None:
        state.clock_anomaly_since = _stamp(now)


def _clock_problem(clock: Callable[[], datetime], getter: GuardedGetter,
                   measured: dict[str, float | None] | None = None) -> str | None:
    """Certify the whole local-minus-server interval, including request time and ±1 s server quantization.
    Issue #289: ``measured`` (optional, observation only) receives low_s / high_s / rtt_s once computed."""
    try:
        sent = clock()
        server = getter(podlib.TIME_URL)
        received = clock()
        if not isinstance(sent, datetime) or not isinstance(received, datetime):
            return "clock_unverified"
        sent, received = parse_book_ts(sent), parse_book_ts(received)
        if (sent is None or received is None or received < sent
                or isinstance(server, bool) or not isinstance(server, (int, float))
                or parse_book_ts(server) is None):
            return "clock_unverified"
        low = sent.timestamp() - server - 1.0
        high = received.timestamp() - server + 1.0
        if measured is not None:
            measured.update(low_s=low, high_s=high, rtt_s=received.timestamp() - sent.timestamp())
    except Exception:  # noqa: BLE001 - failed reads, invalid clocks and numeric overflow cannot certify time
        return "clock_unverified"
    if low >= -CLOCK_SKEW_MAX_S and high <= CLOCK_SKEW_MAX_S:
        return None
    if low > CLOCK_SKEW_MAX_S or high < -CLOCK_SKEW_MAX_S:
        return "clock_skew"
    return "clock_unverified"


# ---------------------------------------------------------------- books

def book_snapshot_from_clob(raw: object, market_id: str, token_id: str) -> intent_builder.BookSnapshot:
    """Issue #120: thin re-export of ``intent_builder.book_snapshot_from_clob`` (full-depth CLOB /book)."""
    return intent_builder.book_snapshot_from_clob(raw, market_id, token_id)


def book_url(token_id: str) -> str:
    return podlib.BOOK_PREFIX + urlencode({"token_id": token_id})


def gamma_market_url(market_id: str) -> str:
    return podlib.GAMMA_PREFIX + "?" + urlencode({"id": market_id})


def _gamma_market(getter: Callable[[str], Any], market_id: str) -> object:
    """Gamma's /markets?id=<id> row for the fee (src/fee_source reads feesEnabled / feeSchedule)."""
    data = getter(gamma_market_url(market_id))
    rows = data if isinstance(data, list) else []
    matches = [row for row in rows if isinstance(row, dict) and str(row.get("id")) == str(market_id)]
    return matches[0] if len(matches) == 1 else None


# ---------------------------------------------------------------- one iteration

def _signal_from_forecast(row: dict[str, Any]) -> intent_builder.ProbabilitySignal:
    return intent_builder.ProbabilitySignal(
        signal_id=str(row["forecast_id"]), source="news", market_id=str(row.get("market_id") or ""),
        outcome_index=row.get("outcome_index"), p=row.get("model_prob"), p_low=row.get("p_low"),  # type: ignore[arg-type]
        p_high=row.get("p_high"), token_id=row.get("token_id"), signal_ts=row.get("ts"),
        note=str(row.get("note") or ""), forecast_id=row.get("forecast_id"), forecast_ts=row.get("ts"),
        model_version=row.get("model_version"))


def _forecast_row(path: Path, forecast_id: str) -> dict[str, Any] | None:
    for row in podlib.read_jsonl(path) if path.exists() else []:
        if row.get("forecast_id") == forecast_id:
            return row
    return None


def _pending_files(inbox: Path) -> list[Path]:
    if not inbox.exists():
        return []
    return sorted(p for p in inbox.glob("*/*.json") if p.is_file())


def _scan(state: RunnerState, now: datetime, gamma: GuardedGetter) -> None:
    try:
        result = scanner.scan(getter=gamma, risk_path=Path(state.risk_path or podlib.RISK_PATH), now=now)
    except Exception as exc:  # noqa: BLE001 - a failed scan keeps the last one (until it is too old)
        state.counters["scan_failures_total"] += 1
        event(state, "scan_failed", now, error=type(exc).__name__)
        return
    if not result.get("fetch", {}).get("complete"):
        state.counters["scan_failures_total"] += 1
        event(state, "scan_incomplete", now, stop_reason=result.get("fetch", {}).get("stop_reason"))
        return
    rows = result["rows"]
    state.markets = {str(row["id"]): row for row in rows if row.get("id")}
    state.scan_at = now
    state.counters["scans_total"] += 1
    try:
        forecast_inbox.write_candidates(rows, state.paths.candidates)
    except OSError as exc:
        raise WriteFailed(f"candidates: {type(exc).__name__}") from exc
    event(state, "scanned", now, scanned=result.get("scanned"), passed=result.get("passed"))


def _ingest(state: RunnerState, now: datetime, report: IterationReport) -> None:
    forecasts = Path(state.forecasts_path or forecaster.FORECASTS_PATH)
    bus_path = Path(state.bus_path or podlib.BUS_PATH)
    checked = 0
    for path in _pending_files(state.paths.inbox):
        if checked >= RUNNER_MAX_INGEST_PER_ITER:
            break
        try:
            info = path.stat()
        except OSError:
            continue
        key = (info.st_size, info.st_mtime_ns)
        if state.rejected_files.get(str(path)) == key:
            continue  # the same rejected bytes: not checked again until the file changes
        checked += 1
        result = forecast_inbox.ingest_one(path, markets=state.markets, now=now, forecasts_path=forecasts,
                                           bus_path=bus_path, archive_dir=state.paths.archive)
        if result.status == forecast_inbox.SEALED and result.forecast_id:
            row = _forecast_row(forecasts, result.forecast_id)
            journal(state, "sealed", now, file=path.name, sha256=result.sha256, forecast_id=result.forecast_id)
            state.counters["forecasts_sealed_total"] += 1
            report.sealed += 1
            if row is not None:
                state.pending[result.forecast_id] = Pending(_signal_from_forecast(row))
        elif result.status == forecast_inbox.REJECTED:
            state.rejected_files[str(path)] = key
            journal(state, "rejected", now, file=path.name, sha256=result.sha256, reasons=list(result.reasons))
            state.counters["forecasts_rejected_total"] += 1
            report.rejected += 1
        else:
            journal(state, "duplicate", now, file=path.name, sha256=result.sha256)


def _decide(state: RunnerState, now: datetime, cfg: dict[str, Any], gamma: GuardedGetter, book_get: GuardedGetter,
            report: IterationReport) -> None:
    if state.fee_cache is None:
        state.fee_cache = fee_source.FeeCache(lambda market_id: _gamma_market(state.fee_getter, market_id),
                                              clock=state.monotonic)
    state.fee_getter = gamma  # this iteration's guarded getter (the cache outlives it)
    for forecast_id in sorted(state.pending, key=lambda k: str(state.pending[k].signal.forecast_ts)):
        item = state.pending[forecast_id]
        signal = item.signal
        market_id = signal.market_id
        row = state.markets.get(market_id)
        if row is None:
            _skip(state, now, report, item, forecast_id, ["market_not_in_scan"])
            continue
        market_halts = [r for r in halt_store.halt_problems(market_id) if r.startswith("halt_market:")]
        if market_halts:
            _skip(state, now, report, item, forecast_id, ["halted_market", *market_halts])
            continue
        quote = state.fee_cache.get(market_id)
        if quote.fee_rate is None:
            state.counters["missing_fee_total"] += 1
            _skip(state, now, report, item, forecast_id, ["missing_fee_data", str(quote.problem)])
            continue
        books: dict[str, intent_builder.BookSnapshot] = {}
        book_refused: list[str] | None = None
        for token in [str(t) for t in row.get("token_ids") or []]:
            try:
                # Issue #120: full-depth REST /book via book.fetch_depth (getter injected / guarded).
                raw = book.fetch_depth(token, getter=book_get)
            except RefusedURL:
                raise
            except Exception:  # noqa: BLE001 - an unreadable book is a missing book
                continue
            if raw is None:
                continue
            fetched_at = state.clock()
            # book_before_resume: a fetch that started before the last resume is refused
            if state.last_resume_at is not None and fetched_at < state.last_resume_at:
                book_refused = ["book_before_resume"]
                break
            snap = book_snapshot_from_clob(raw, market_id, token)
            problem = book_age_problem(snap.book_ts, fetched_at, MAX_BOOK_AGE_S)
            if problem is not None:
                if problem in (MISSING_BOOK_TS, INVALID_BOOK_TS):
                    # Poly风控 P1 on #275: a missing / unparseable / non-finite book_ts vetoes this tick's
                    # clock verification (even after a clean /time check)
                    state.tick_clock_vetoed = True
                book_refused = [problem]
                break
            books[token] = snap
            # Task DC + #253: verified only by a clean /time check plus at least one fresh, valid book this tick
            # (an empty tick proves nothing and never clears the anomaly)
            if state.tick_time_ok:
                state.tick_clock_verified = True
        if book_refused is not None:
            _skip(state, now, report, item, forecast_id, book_refused)
            continue
        decided_at = max(now, state.clock())
        ages = [age for age in (_book_age_s(b.book_ts, decided_at) for b in books.values()) if age is not None]
        if ages:
            state.last_book_age_s_max = max(ages)
        context = intent_builder.market_context_from_row(row, fee_rate=quote.fee_rate)
        result = intent_builder.build_order_intent(signal, context, books, cfg, now=decided_at, strategy=STRATEGY,
                                                   paper=True, seen_keys=frozenset(state.seen_keys),
                                                   banned_categories=frozenset())
        oi = result.intent
        if oi is None:
            _skip(state, now, report, item, forecast_id, list(result.reasons))
            continue
        journal(state, "built", now, forecast_id=forecast_id, intent_id=oi.intent_id, decision_id=oi.decision_id,
                idempotency_key=oi.idempotency_key)
        state.counters["intents_built_total"] += 1
        report.built += 1
        risk_intent = dataclasses.replace(intent_builder.to_risk_intent(oi), fee_source=quote.fee_source,
                                          fee_gamma_fetched_at=quote.fee_gamma_fetched_at,
                                          fee_schedule_sha=quote.fee_schedule_sha)
        outcome = paper_ledger.commit_if_allowed(risk_intent, cfg, unique_decision_id=True)
        fill = outcome.get("fill") if isinstance(outcome, dict) else None
        if outcome.get("allow") is True and isinstance(fill, dict) and fill.get("risk_decision") == "allow":
            journal(state, "committed", now, forecast_id=forecast_id, decision_id=oi.decision_id,
                    fill_id=fill.get("fill_id"), already_booked=bool(outcome.get("already_booked")))
            state.counters["allows_total"] += 1
            report.allows += 1
            report.fills.append(fill)
            state.seen_keys.add(oi.idempotency_key)
            del state.pending[forecast_id]
            continue
        reasons = list((outcome.get("decision") or {}).get("reasons") or []) if isinstance(outcome, dict) else []
        journal(state, "vetoed", now, forecast_id=forecast_id, decision_id=oi.decision_id, reasons=reasons,
                halted=bool(outcome.get("halted")))
        state.counters["vetoes_total"] += 1
        report.vetoes += 1
        item.evals += 1
        if item.evals >= RUNNER_MAX_EVALS_PER_SIGNAL:
            del state.pending[forecast_id]
        if outcome.get("halted"):
            break  # a ledger halt: nothing more this iteration; the next one pauses on it


def _skip(state: RunnerState, now: datetime, report: IterationReport, item: Pending, forecast_id: str,
          reasons: list[str]) -> None:
    journal(state, "skipped", now, forecast_id=forecast_id, reasons=reasons)
    report.skipped += 1
    item.evals += 1
    if item.evals >= RUNNER_MAX_EVALS_PER_SIGNAL or DROP_REASONS.intersection(reasons):
        del state.pending[forecast_id]


def _book_age_s(book_ts: object, now: datetime) -> float | None:
    parsed = heartbeat.parse_stamp(book_ts) if isinstance(book_ts, str) else None
    if parsed is None:
        return None
    age = round((now - parsed).total_seconds(), 3)
    return age if math.isfinite(age) else None


def _read_journal(path: Path) -> list[dict[str, Any]]:
    if not path.exists() or not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def recover(paths: RunnerPaths, ledger_path: Path, *, now: datetime | None = None,
            clock: Callable[[], datetime] | None = None,
            monotonic: Callable[[], float] | None = None,
            risk_path: Path | None = None) -> RunnerState:
    """Issue #120 (Task CJ): rebuild runner state from the journal and the ledger after a restart.

    * ``seen_keys`` come from journal ``built`` / ``committed`` idempotency keys and from ledger fills.
    * A journal ``built`` with no matching ``committed`` is reconciled to the ledger: if the
      ``decision_id`` is already on the ledger, a ``committed`` line is appended; otherwise the build is
      treated as never having happened (the next iteration may rebuild with a fresh book; the same
      ``decision_id`` can still only book once via ``unique_decision_id``).
    * Pending sealed signals are not re-hydrated here (already archived); the inbox is empty of them.
    """
    now = now or (clock or (lambda: datetime.now(timezone.utc)))()
    state = RunnerState(paths=paths, risk_path=risk_path, clock=clock or (lambda: datetime.now(timezone.utc)),
                        monotonic=monotonic or time.monotonic)
    _note_clock_anomaly(state, now)  # issue #289: the startup clock_unverified is stamped at startup (heartbeat)
    _, ledger = ledger_store.read_ledger(Path(ledger_path))
    ledger_by_decision: dict[str, dict[str, Any]] = {}
    for row in ledger.rows:
        if not ledger_store.counts(row):
            continue
        decision_id = row.get("decision_id")
        if isinstance(decision_id, str) and decision_id.strip():
            ledger_by_decision.setdefault(decision_id, row)
            # idempotency_key is not always on the fill; decision_id is the durable key
        key = row.get("idempotency_key")
        if isinstance(key, str) and key:
            state.seen_keys.add(key)
    journal_rows = _read_journal(paths.journal)
    committed_ids = {row.get("decision_id") for row in journal_rows if row.get("step") == "committed"}
    built_open: list[dict[str, Any]] = []
    for row in journal_rows:
        step = row.get("step")
        if step == "built":
            key = row.get("idempotency_key")
            if isinstance(key, str) and key:
                state.seen_keys.add(key)
            decision_id = row.get("decision_id")
            if decision_id not in committed_ids:
                built_open.append(row)
        elif step == "committed":
            key = row.get("idempotency_key")
            if isinstance(key, str) and key:
                state.seen_keys.add(key)
    for row in built_open:
        decision_id = row.get("decision_id")
        if not isinstance(decision_id, str) or not decision_id.strip():
            continue
        existing = ledger_by_decision.get(decision_id)
        if existing is None:
            continue  # never booked: leave it; next eval can rebuild (same decision_id, unique booking)
        # ledger won: patch the journal so recover is idempotent next time
        try:
            journal(state, "committed", now, forecast_id=row.get("forecast_id"), decision_id=decision_id,
                    fill_id=existing.get("fill_id"), recovered=True)
        except WriteFailed:
            pass
        committed_ids.add(decision_id)
    # resume_count from prior events (informational continuity)
    if paths.events.exists():
        for row in _read_journal(paths.events):
            if row.get("event") == "resume_detected":
                state.resume_count = max(state.resume_count, int(row.get("resume_count") or 0))
    state.ledger_rows = len(ledger.rows)
    return state


def _suspend_detected(state: RunnerState, now: datetime) -> tuple[bool, float]:
    """Issue #120: True when the wall gap or wall-vs-monotonic skew says the host suspended."""
    if state.last_iter_wall is None or state.last_iter_mono is None:
        return False, 0.0
    wall_gap = (now - state.last_iter_wall).total_seconds()
    mono_gap = state.monotonic() - state.last_iter_mono
    skew = abs(wall_gap - mono_gap)
    if wall_gap > RUNNER_SUSPEND_GAP_S or skew > RUNNER_WALL_MONO_SKEW_S:
        return True, wall_gap
    return False, wall_gap


def _held_markets() -> list[str]:
    """Markets with an open leg on the paper ledger (src/settlement.open_legs), sorted."""
    from src import settlement

    _, read = ledger_store.read_ledger(Path(paper_ledger.PAPER_PATH))
    return sorted({market for market, _token in settlement.open_legs(read.rows) if market})


def _writeoff_hook(state: RunnerState, now: datetime, gamma: GuardedGetter) -> list[str]:
    """Issue #197 (Task CK) hook: close-then-write-off runs here, AFTER settlement and BEFORE the daily loss.

    Poly风控's tick order (#197, issuecomment-6106669720): halted -> end; settle; write off the legs that are
    neither settled nor written off; #124 daily loss; RiskGate. CK is not merged (it waits for 家伟), so this is
    a no-op that returns no problems; CK replaces the body. Problems it returns are invalid_risk_state.
    """
    return []


def _settle_tick(state: RunnerState, now: datetime, gamma: GuardedGetter) -> list[str]:
    """Issue #206 (Task CO): this tick's settlement step (called by ``preflight``, see its docstring).

    1. Collect settlement snapshots from Gamma for the markets with an open leg only (src/resolution_watch).
       A collection failure books nothing this tick and is not a halt (#197 ruling ②): an event, retry next tick.
    2. ``paper_ledger.apply_settlements``: one settlement row per resolved open leg, keyed by leg + condition
       (src/settlement.settlement_fill_id), written to the ledger and fsynced before any event. Booking the same
       key again is a no-op; the same key with other content is refused (conflicting_fill_id): a problem.
    3. ``_writeoff_hook`` (CK).
    Returns problems (invalid_risk_state); WriteFailed (events) propagates to the iteration.
    """
    try:
        held = _held_markets()
    except Exception as exc:  # noqa: BLE001 - preflight verified the ledger; a re-read failure is a problem
        return [f"settlement_ledger_unreadable:{type(exc).__name__}"]
    try:
        blocked = paper_ledger.settlement_blocked_markets()
    except Exception as exc:  # noqa: BLE001 - an unreadable halt state is a global halt at the next check
        return [f"settlement_halt_state_unreadable:{type(exc).__name__}"]
    if blocked:
        event(state, "settlement_blocked", now, markets=sorted(blocked))
    held = [market for market in held if market not in blocked]
    if held:
        collected = resolution_watch.collect(gamma, held, now, _stamp(now),
                                             Path(paper_ledger.SETTLEMENT_RESOLUTIONS_PATH))
        if not collected.ok:
            event(state, "settlement_skipped", now, reasons=collected.failures[:10])
        else:
            if collected.written:
                event(state, "resolutions_collected", now,
                      condition_ids=[row["condition_id"] for row in collected.written])
            out = paper_ledger.apply_settlements(_stamp(now), skip_markets=blocked)
            if out["problems"]:
                return [str(p) for p in out["problems"]]
            if out["booked"]:  # the rows are on disk (ledger_store.book fsyncs) before this event
                event(state, "settled", now, rows=[{"fill_id": row.get("fill_id"), "market": row.get("market"),
                                                   "token_id": row.get("token_id"),
                                                   "settlement_payout": row.get("settlement_payout")}
                                                  for row in out["booked"]])
    return _writeoff_hook(state, now, gamma)


def iteration(state: RunnerState, *, now: datetime, getters: Getters) -> IterationReport:
    """One loop step: the checks first (settlement inside them, issue #206); only when they all pass, scan /
    inbox / decisions."""
    state.iteration += 1
    state.tick_clock_verified = False  # Task DC: per tick
    state.tick_clock_vetoed = False
    state.tick_time_ok = False
    _note_clock_anomaly(state, now)  # issue #289: heartbeat stamp for an anomaly held from startup
    gamma = GuardedGetter(getters.gamma, state.on_get)  # one guarded Gamma getter per tick: settlement, scan, fee
    book_get = GuardedGetter(getters.book, state.on_get)  # shared read-only transport for /time and later /book
    try:
        check = preflight(state.paths, now=now, risk_path=state.risk_path, clock_ok=runner_clock_ok(state),
                          settle=lambda: _settle_tick(state, now, gamma),
                          clock_check=lambda: _clock_check(state, book_get, now))
    except WriteFailed as exc:
        state.last_iter_wall, state.last_iter_mono = now, state.monotonic()
        return IterationReport(ACT_WRITE_FAILED, [str(exc)])
    state.disk_free_gb = check.disk_free_gb if check.disk_free_gb is not None else state.disk_free_gb
    state.ledger_rows = check.ledger_rows if check.ledger_rows is not None else state.ledger_rows
    if check.action != ACT_RUN:
        report = IterationReport(check.action, check.reasons)
        if check.action == ACT_FINISH and check.reasons[:1] == ["kill_file"]:
            try:
                kill_alert(state, now)  # issue #247: a record only; the exit code stays 0
            except WriteFailed as exc:
                state.last_iter_wall, state.last_iter_mono = now, state.monotonic()
                return IterationReport(ACT_WRITE_FAILED, [str(exc)])
        if check.action == ACT_PAUSE:
            if state.paused_reason != check.reasons[0]:
                # PR #253 review: clock_unverified <-> clock_skew is one clock pause episode (keep paused_since)
                if not (state.paused_reason in CLOCK_PAUSE_REASONS and check.reasons[0] in CLOCK_PAUSE_REASONS
                        and state.paused_since is not None):
                    state.paused_since = _stamp(now)
                try:
                    event(state, "paused", now, reasons=check.reasons)
                except WriteFailed:
                    return IterationReport(ACT_WRITE_FAILED, ["events_write_failed"])
            state.paused_reason = check.reasons[0]
            report.paused_reason = state.paused_reason
        # still advance the suspend clocks so a long pause is not itself a false resume
        state.last_iter_wall, state.last_iter_mono = now, state.monotonic()
        return report
    if state.paused_reason is not None:
        state.paused_reason = state.paused_since = None
    # Issue #120: suspend / resume detection before any scan or decision
    suspended, gap_s = _suspend_detected(state, now)
    if suspended:
        state.resume_count += 1
        state.last_resume_at = now
        state.force_rescan = True
        if state.fee_cache is not None:
            state.fee_cache.invalidate_all()
        state.fee_cache = None
        try:
            event(state, "resume_detected", now, gap_s=round(gap_s, 3), resume_count=state.resume_count)
        except WriteFailed as exc:
            state.last_iter_wall, state.last_iter_mono = now, state.monotonic()
            return IterationReport(ACT_WRITE_FAILED, [str(exc)])
        state.last_iter_wall, state.last_iter_mono = now, state.monotonic()
        report = IterationReport(ACT_RUN, ["resume_detected"])
        report.reasons = ["resume_detected"]
        # force rescan this iteration but build no intents (C.3: 本轮不出单)
        try:
            _scan(state, now, gamma)
            state.force_rescan = False
            _ingest(state, now, report)
        except WriteFailed as exc:
            return IterationReport(ACT_WRITE_FAILED, [str(exc)])
        return report
    report = IterationReport(ACT_RUN)
    try:
        cfg = _risk_cfg(state.risk_path)
        if (state.force_rescan or state.scan_at is None
                or (now - state.scan_at).total_seconds() >= RUNNER_SCAN_EVERY_S):
            _scan(state, now, gamma)
            state.force_rescan = False
        if state.scan_at is None or (now - state.scan_at).total_seconds() > RUNNER_SCAN_MAX_AGE_S:
            report.reasons.append("scan_too_old")
            state.last_iter_wall, state.last_iter_mono = now, state.monotonic()
            return report
        _ingest(state, now, report)
        _decide(state, now, cfg, gamma, book_get, report)
    except WriteFailed as exc:
        state.last_iter_wall, state.last_iter_mono = now, state.monotonic()
        return IterationReport(ACT_WRITE_FAILED, [str(exc)])
    state.last_iter_wall, state.last_iter_mono = now, state.monotonic()
    if state.tick_clock_vetoed:  # one invalid book_ts vetoes the whole tick's clock verification
        state.tick_clock_verified = False
    # Task DC + #253: a clock anomaly (clock_skew / clock_unverified) clears only after a decide that did not
    # re-pause, a clean CLOB /time check this tick, and no book with a missing / invalid book_ts
    if report.action == ACT_RUN and state.tick_clock_verified:
        state.clock_anomaly = None
        _note_clock_anomaly(state, now)  # issue #289: heartbeat only
    return report


def progress(state: RunnerState, now: datetime) -> dict[str, Any]:
    scan_age = None if state.scan_at is None else round((now - state.scan_at).total_seconds(), 3)
    return {"iteration": state.iteration, "last_scan_ts": None if state.scan_at is None else _stamp(state.scan_at),
            "scan_age_s": scan_age, **state.counters, "last_book_age_s_max": state.last_book_age_s_max,
            "resume_count": state.resume_count, "paused_reason": state.paused_reason, "paused_since": state.paused_since,
            "disk_free_gb": None if state.disk_free_gb is None else round(state.disk_free_gb, 3),
            "ledger_rows": state.ledger_rows, "pending_signals": len(state.pending),
            "executor_enabled": EXECUTOR_ENABLED,
            # issue #289 (Task DJ): read-only clock observation; see docs/EXIT_CODES.md
            "clock_anomaly": state.clock_anomaly, "clock_anomaly_since": state.clock_anomaly_since,
            "last_time_check": None if state.last_time_check is None else dict(state.last_time_check)}


# ---------------------------------------------------------------- single instance, network guard, CLI

class RunnerLock:
    """``runner.lock``: an OS byte lock held for the process's life (released by the OS if it dies, so it is
    never stale) plus pid / host / started_at for the Operator. ``acquire`` never waits."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._fd: int | None = None

    def acquire(self, started_at: str) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
        except OSError:
            return False
        try:
            _lock_byte(fd)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        info = json.dumps({"pid": os.getpid(), "host": platform.node(), "started_at": started_at}).encode("utf-8")
        try:
            os.lseek(fd, 1, os.SEEK_SET)  # byte 0 is the lock byte; the info follows it
            os.ftruncate(fd, 1)
            os.write(fd, info + b"\n")
        except OSError:
            pass  # the lock itself is what counts
        return True

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            _unlock_byte(fd)
        except OSError:
            pass
        finally:
            os.close(fd)


if os.name == "nt":  # NoisePC
    import msvcrt

    def _lock_byte(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock_byte(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock_byte(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock_byte(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def network_guard() -> Iterator[list[str]]:
    """Socket-level tripwire for a real paper run (issue #119 F2): resolving any host but ALLOWED_HOSTS raises
    (and is recorded), so even a code path that bypassed the getters could reach nothing else."""
    refused: list[str] = []
    real = socket.getaddrinfo

    def guarded(host, *args, **kwargs):  # noqa: ANN001 - socket.getaddrinfo's signature
        name = host.decode("ascii", "replace") if isinstance(host, bytes) else str(host)
        if name.lower().rstrip(".") not in ALLOWED_HOSTS:
            refused.append(name)
            raise OSError(f"m2_runner network guard: {name!r} is not an allowed host")
        return real(host, *args, **kwargs)

    socket.getaddrinfo = guarded
    try:
        yield refused
    finally:
        socket.getaddrinfo = real


def self_check(paths: RunnerPaths) -> str | None:
    """The start self-check: the run dir and inbox exist and are writable. None when fine."""
    try:
        for folder in (paths.run_dir, paths.inbox, paths.archive):
            folder.mkdir(parents=True, exist_ok=True)
        probe = paths.run_dir / f".m2_runner_probe_{os.getpid()}"
        probe.write_bytes(b"ok")
        probe.unlink()
    except OSError as exc:
        return f"run_dir_not_writable:{type(exc).__name__}"
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.m2_runner",
        description="M2 paper runner: scanner -> forecast inbox -> intent_builder -> RiskGate -> paper ledger. "
                    "Paper only; no executor, no orders, no credentials.",
        epilog="Exit codes: 0 KILL / max-runtime / --once; 2 usage; 3 another runner holds the lock; 4 a write "
               "failed; 6 risk.toml off its pins or a live flag (global halt); 7 start self-check failed.")
    parser.add_argument("--run-dir", required=True, type=Path, help="the run dir (runs/m2)")
    parser.add_argument("--max-runtime", type=float, default=float(DEFAULT_MAX_RUNTIME_S),
                        help="stop after this many seconds (default one week)")
    parser.add_argument("--once", action="store_true", help="one iteration, then exit")
    return parser


def main(argv: list[str] | None = None, *, getters: Getters | None = None,
         clock: Callable[[], datetime] | None = None, sleep: Callable[[float], None] = time.sleep,
         monotonic: Callable[[], float] = time.monotonic, risk_path: Path | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return EXIT_OK if exc.code == 0 else EXIT_USAGE
    if not args.max_runtime > 0:
        return EXIT_USAGE
    now_fn = clock or (lambda: datetime.now(timezone.utc))
    paths = RunnerPaths.under(args.run_dir)
    problem = self_check(paths)
    if problem is not None:
        print(f"{PROC}: start self-check failed: {problem}; orders=none", file=sys.stderr)
        return EXIT_SELF_CHECK
    lock = RunnerLock(paths.lock_file)
    if not lock.acquire(_stamp(now_fn())):
        print(f"{PROC}: another m2_runner holds {paths.lock_file}; exiting", file=sys.stderr)
        return EXIT_ALREADY_RUNNING
    # Task CF (#124): RiskGate's start-of-day leg marks live under this run dir (runs/m2/daily_marks by default).
    marks_dir_before = paper_ledger.DAILY_MARKS_DIR
    paper_ledger.DAILY_MARKS_DIR = paths.run_dir / "daily_marks"
    try:
        with network_guard():
            return _run(args, paths, getters or Getters(), now_fn, sleep, monotonic, risk_path)
    finally:
        paper_ledger.DAILY_MARKS_DIR = marks_dir_before
        lock.release()


def _run(args: argparse.Namespace, paths: RunnerPaths, getters: Getters, now_fn: Callable[[], datetime],
         sleep: Callable[[float], None], monotonic: Callable[[], float], risk_path: Path | None) -> int:
    hb = heartbeat.Heartbeat(PROC, every_s=HEARTBEAT_RUNNER_EVERY_S, monotonic=monotonic, now=now_fn,
                             risk_path=risk_path)
    # Issue #120: recover seen_keys / journal before the first iteration
    state = recover(paths, Path(paper_ledger.PAPER_PATH), now=now_fn(), clock=now_fn, monotonic=monotonic,
                    risk_path=risk_path)
    started = monotonic()
    hb.beat(heartbeat.STATE_STARTING, progress(state, now_fn()), force=True)

    def beat_between_gets() -> None:
        # Task DQ (Poly哨兵 on #309): a mid-tick beat after each GET, throttled to HEARTBEAT_RUNNER_EVERY_S; with
        # every GET bounded by PUBLIC_GET_DEADLINE_S, a 429 storm never stales the heartbeat inside one tick.
        beat_state = heartbeat.STATE_PAUSED if state.paused_reason is not None else heartbeat.STATE_RUNNING
        hb.beat(beat_state, progress(state, now_fn()))

    state.on_get = beat_between_gets
    try:
        while True:
            began = monotonic()
            now = now_fn()
            report = iteration(state, now=now, getters=getters)
            line = (f"{PROC} iteration={state.iteration} action={report.action} sealed={report.sealed} "
                    f"built={report.built} allows={report.allows} vetoes={report.vetoes} orders=none")
            print(line + (f" reasons={','.join(report.reasons[:3])}" if report.reasons else ""))
            if report.action == ACT_FINISH:
                hb.finish(EXIT_OK, progress(state, now_fn()))
                return EXIT_OK
            if report.action == ACT_EXIT_PIN:
                hb.finish(EXIT_PIN_OR_LIVE, progress(state, now_fn()))
                return EXIT_PIN_OR_LIVE
            if report.action == ACT_WRITE_FAILED:
                hb.finish(EXIT_WRITE_FAILED, progress(state, now_fn()))
                return EXIT_WRITE_FAILED
            beat_state = heartbeat.STATE_PAUSED if report.action == ACT_PAUSE else heartbeat.STATE_RUNNING
            hb.beat(beat_state, progress(state, now_fn()), force=True)
            if args.once or monotonic() - started >= args.max_runtime:
                hb.finish(EXIT_OK, progress(state, now_fn()))
                return EXIT_OK
            while True:  # wait for the next iteration, beating meanwhile
                left = RUNNER_LOOP_S - (monotonic() - began)
                if left <= 0 or monotonic() - started >= args.max_runtime:
                    break
                sleep(min(left, HEARTBEAT_RUNNER_EVERY_S, max(0.0, args.max_runtime - (monotonic() - started))))
                hb.beat(beat_state, progress(state, now_fn()))
            if monotonic() - started >= args.max_runtime:
                hb.finish(EXIT_OK, progress(state, now_fn()))
                return EXIT_OK
    except BaseException:
        hb.finish(None, progress(state, now_fn()), crashed=True)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
