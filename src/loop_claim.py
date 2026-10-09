"""Claim one paper-loop task on docs/loop/state.json.

Does not read secrets, place orders, or edit config/risk.toml.
The caller commits and pushes. This file only updates the queue.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.podlib import load_risk, pin_problems  # noqa: E402

STATE_PATH = ROOT / "docs" / "loop" / "state.json"
SURFACES = frozenset({"grok-build-local", "grok-web", "grok-bot"})
EXIT_PIN = 2
EXIT_BUSY = 3
EXIT_CLOSED = 4
EXIT_OK = 0
# Task AS (gap 4): a usage error was argparse's 2, the same code as EXIT_PIN (a global halt for the
# resident Sentinel). It is now its own code.
EXIT_USAGE = 5


def utc_stamp(moment: datetime | None = None) -> str:
    current = moment or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_stamp(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def load_state(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state, ensure_ascii=False, indent=2) + "\n"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(path)


def next_todo(state: dict) -> dict | None:
    for task in state.get("tasks") or []:
        if task.get("status") == "todo":
            return task
    return None


def _task(state: dict, task_id: str) -> dict:
    for task in state.get("tasks") or []:
        if task.get("id") == task_id:
            return task
    raise KeyError(task_id)


def claim_state(state: dict, surface: str, task_id: str, now: datetime) -> dict:
    if surface not in SURFACES:
        raise ValueError("surface")
    task = _task(state, task_id)
    if task.get("status") == "done":
        raise PermissionError("closed")
    if task.get("status") not in {"todo", "doing"}:
        raise PermissionError("closed")
    nxt = next_todo(state)
    claim = state.get("claim")
    if task.get("status") == "todo" and (nxt is None or nxt.get("id") != task_id):
        raise PermissionError("closed")
    if claim and not _expired(claim, now):
        same = claim.get("surface") == surface and claim.get("task") == task_id
        if not same:
            raise RuntimeError("busy")
    hours = int(state.get("claim_hours") or 3)
    task["status"] = "doing"
    state["claim"] = {
        "surface": surface,
        "task": task_id,
        "since": utc_stamp(now),
        "until": utc_stamp(now + timedelta(hours=hours)),
    }
    return state


def release_state(state: dict, surface: str, done: bool, now: datetime) -> dict:
    claim = state.get("claim")
    if not claim or claim.get("surface") != surface:
        raise RuntimeError("busy")
    task = _task(state, str(claim.get("task")))
    task["status"] = "done" if done else "todo"
    if done:
        task["done_at"] = utc_stamp(now)
    state["claim"] = None
    return state


def _expired(claim: dict, now: datetime) -> bool:
    try:
        return parse_stamp(str(claim.get("until"))) <= now
    except (TypeError, ValueError):
        return True


def pins_ok() -> bool:
    """podlib.PINS on the parsed config and (Task AS) the risk file pins: sha256 and every key."""
    from src.pins import record_drift, risk_file_problems

    drift = risk_file_problems()
    if drift:
        record_drift("loop_claim", drift, phase="start")  # one global halt, deduped (src/pins.py)
        return False
    try:
        return not pin_problems(load_risk())
    except Exception:  # noqa: BLE001 - unreadable config: refuse (fail closed)
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Claim or release one paper-loop task")
    parser.add_argument("action", choices=("status", "claim", "release"))
    parser.add_argument("--surface", choices=sorted(SURFACES))
    parser.add_argument("--task")
    parser.add_argument("--done", action="store_true")
    parser.add_argument("--todo", action="store_true")
    parser.add_argument("--state", type=Path, default=STATE_PATH)
    parser.add_argument("--now")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return EXIT_OK if exc.code == 0 else EXIT_USAGE
    if not pins_ok():
        print("live_enabled must stay false; queue not changed")
        return EXIT_PIN
    state = load_state(args.state)
    now = parse_stamp(args.now) if args.now else datetime.now(timezone.utc)
    try:
        if args.action == "status":
            waiting = next_todo(state)
            print(json.dumps({"claim": state.get("claim"), "next": None if waiting is None else waiting.get("id")}, ensure_ascii=False))
            return EXIT_OK
        if args.action == "claim":
            if not args.surface or not args.task:
                print("usage error: claim needs --surface and --task", file=sys.stderr)
                return EXIT_USAGE
            claim_state(state, args.surface, args.task, now)
        else:
            if not args.surface or args.done == args.todo:
                print("usage error: release needs --surface and exactly one of --done or --todo", file=sys.stderr)
                return EXIT_USAGE
            release_state(state, args.surface, args.done, now)
    except RuntimeError:
        print("claim busy")
        return EXIT_BUSY
    except (KeyError, PermissionError):
        print("task is not open")
        return EXIT_CLOSED
    save_state(args.state, state)
    print(json.dumps(state["claim"], ensure_ascii=False))
    return EXIT_OK


if __name__ == "__main__":
    # Task AP: start/finish heartbeat (logs/heartbeat/loop_claim.json) for the resident Sentinel.
    from src.heartbeat import run_main

    raise SystemExit(run_main("loop_claim", main))
