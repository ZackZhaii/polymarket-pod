"""Task AS (Poly哨兵 design gaps 5 and 6): runtime pin checks of config/risk.toml.

Gap 6: ``podlib.PINS`` holds five keys and is checked against an in-memory config. The file pin
set here is every key of config/risk.toml (``RISK_FILE_PINS``) plus its sha256
(``PINNED_RISK_SHA256``, CRLF normalised like scripts/guard.py). An unknown key, a missing key, a
different value or type, an unreadable or unparseable file, or another sha is drift.

Gap 5: long-running and data-writing entry points check the file at process start
(``refuse_start``: on drift they record a global halt and exit with their pin code, see
docs/EXIT_CODES.md) and, while running, at least every ``PIN_RECHECK_S`` (``PinWatch``: on drift the
run stops the same way). Fail closed: a check that cannot run is drift. Read only: this module never
edits risk.toml and never lifts a halt (only the Operator resumes).
"""
from __future__ import annotations

import hashlib
import sys
import time
from pathlib import Path
from typing import Any, Callable, TextIO

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import podlib  # noqa: E402
from src.thresholds import PIN_RECHECK_S  # noqa: E402

PINNED_RISK_SHA256 = "0aea23af1e938d330b5b4fab2c6abdec9f475f16807d177a5860a4da32aebc04"
# Every key of config/risk.toml with its pinned value (the five podlib.PINS plus the rest).
RISK_FILE_PINS: dict[str, Any] = {
    **podlib.PINS,
    "max_open_positions": 5,
    "min_liquidity_usd": 10000,
    "max_spread": 0.04,
    "kelly_fraction": 0.15,
}
# Halt reason shared with src/sentinel_tick.py, so one drift is one stored halt whoever sees it first.
PIN_HALT_REASON = "risk_toml_pin_drift"


def _same(value: Any, expected: Any) -> bool:
    if isinstance(expected, bool) or isinstance(value, bool):
        return type(value) is type(expected) and value == expected
    if isinstance(expected, (int, float)):
        return isinstance(value, (int, float)) and value == expected
    return type(value) is type(expected) and value == expected


def risk_file_problems(risk_path: Path | None = None) -> list[str]:
    """Drift problems of the risk file (empty = pinned). Never raises."""
    path = Path(risk_path) if risk_path is not None else Path(podlib.RISK_PATH)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return [f"risk_toml_unreadable:{type(exc).__name__}"]
    problems: list[str] = []
    if hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest() != PINNED_RISK_SHA256:
        problems.append("risk_sha256_not_pinned")
    try:
        cfg = podlib.parse_risk_toml(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - an unparseable file is drift (fail closed)
        return problems + [f"risk_toml_unparseable:{type(exc).__name__}"]
    for key, expected in RISK_FILE_PINS.items():
        if key not in cfg:
            problems.append(f"{key}_missing")
        elif not _same(cfg[key], expected):
            problems.append(f"{key}={cfg[key]!r} expected {expected!r}")
    problems += [f"unknown_key:{key}" for key in sorted(set(cfg) - set(RISK_FILE_PINS))]
    return problems


def _checked(risk_path: Path | None) -> list[str]:
    try:
        return risk_file_problems(risk_path)
    except Exception as exc:  # noqa: BLE001 - a broken check is drift
        return [f"pin_check_failed:{type(exc).__name__}"]


def record_drift(proc: str, problems: list[str], *, phase: str, bus_path: Path | None = None) -> None:
    """Record one global halt for a drift (deduped by an active halt with the same reason). Never raises."""
    try:
        from src import halt_store, sentinel  # local: keeps importers light, no import cycle

        active = halt_store.has_active_halt(scope=halt_store.SCOPE_GLOBAL, reason_prefix=PIN_HALT_REASON)
        if active is not False or halt_store.fallback_active():
            return  # already halted (or the halt state is unreadable: that is a global halt already)
        sentinel.halt(reason=PIN_HALT_REASON, scope=halt_store.SCOPE_GLOBAL, source="Sentinel", to="Executor",
                      bus_path=bus_path,
                      extra={"detected_by": proc, "phase": phase, "problems": problems[:10], "severity": "sev1",
                             "live_enabled": False})
    except Exception as exc:  # noqa: BLE001 - last resort: logs/HALT (a global halt)
        try:
            from src import halt_store

            halt_store.write_fallback(f"{proc} {PIN_HALT_REASON} phase={phase} record_failed {type(exc).__name__}")
        except Exception:  # noqa: BLE001
            pass


def refuse_start(proc: str, *, risk_path: Path | None = None, bus_path: Path | None = None,
                 stream: TextIO | None = None) -> list[str]:
    """The start check: [] when pinned; else the problems, after recording the global halt."""
    problems = _checked(risk_path)
    if problems:
        record_drift(proc, problems, phase="start", bus_path=bus_path)
        print(f"{proc}: refused to start: config/risk.toml off its pins ({', '.join(problems[:5])}); "
              f"global halt recorded; orders=none", file=stream or sys.stderr)
    return problems


class PinWatch:
    """Runtime re-check, at most every ``every_s`` (first call checks). Once drifted it stays drifted."""

    def __init__(self, proc: str, *, every_s: float = PIN_RECHECK_S, monotonic: Callable[[], float] | None = None,
                 risk_path: Path | None = None, bus_path: Path | None = None) -> None:
        self.proc = proc
        self.every_s = float(every_s)
        self.monotonic = monotonic or time.monotonic
        self.risk_path = risk_path
        self.bus_path = bus_path
        self.next_at: float | None = None
        self.problems: list[str] = []
        self.checks = 0

    def drifted(self) -> bool:
        if self.problems:
            return True
        now = self.monotonic()
        if self.next_at is not None and now < self.next_at:
            return False
        self.next_at = now + self.every_s
        self.checks += 1
        problems = _checked(self.risk_path)
        if problems:
            self.problems = problems
            record_drift(self.proc, problems, phase="runtime", bus_path=self.bus_path)
            print(f"{self.proc}: stopping: config/risk.toml drifted while running ({', '.join(problems[:5])}); "
                  f"global halt recorded; orders=none", file=sys.stderr)
            return True
        return False
