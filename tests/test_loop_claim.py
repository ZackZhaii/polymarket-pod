"""Offline claim tests. No network. The real risk file must stay paper."""
from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone

import src.loop_claim as loop_claim
from src.podlib import RISK_PATH

NOW = datetime(2026, 10, 8, 8, 0, tzinfo=timezone.utc)


def _state() -> dict:
    return {
        "schema": 1,
        "claim_hours": 3,
        "claim": None,
        "tasks": [
            {"id": "F", "status": "todo", "title": "Endgame signal only"},
            {"id": "G", "status": "todo", "title": "Offline Brier proposal"},
        ],
    }


def test_claim_is_exclusive_until_it_expires():
    before = RISK_PATH.read_bytes()
    state = _state()
    loop_claim.claim_state(state, "grok-web", "F", NOW)
    assert state["tasks"][0]["status"] == "doing"
    assert state["claim"]["surface"] == "grok-web"
    try:
        loop_claim.claim_state(state, "grok-bot", "F", NOW + timedelta(hours=1))
    except RuntimeError as exc:
        assert str(exc) == "busy"
    else:
        raise AssertionError("second claim should be busy")
    later = NOW + timedelta(hours=3)
    loop_claim.claim_state(state, "grok-build-local", "F", later)
    assert state["claim"]["surface"] == "grok-build-local"
    assert RISK_PATH.read_bytes() == before


def test_release_done_opens_the_next_task_only():
    state = _state()
    loop_claim.claim_state(state, "grok-bot", "F", NOW)
    loop_claim.release_state(state, "grok-bot", True, NOW)
    assert state["claim"] is None
    assert state["tasks"][0]["status"] == "done"
    assert loop_claim.next_todo(state)["id"] == "G"
    try:
        loop_claim.claim_state(state, "grok-web", "F", NOW)
    except PermissionError:
        pass
    else:
        raise AssertionError("done task must stay closed")


def test_cli_never_writes_the_repo_queue_on_status(tmp_path):
    before = RISK_PATH.read_bytes()
    path = tmp_path / "state.json"
    original = _state()
    path.write_text(json.dumps(original), encoding="utf-8")
    assert loop_claim.main(["status", "--state", str(path), "--now", "2026-10-08T08:00:00Z"]) == 0
    assert json.loads(path.read_text(encoding="utf-8")) == original
    assert loop_claim.main(["claim", "--surface", "grok-web", "--task", "G", "--state", str(path), "--now", "2026-10-08T08:00:00Z"]) == loop_claim.EXIT_CLOSED
    assert json.loads(path.read_text(encoding="utf-8"))["claim"] is None
    assert loop_claim.main(["claim", "--surface", "grok-web", "--task", "F", "--state", str(path), "--now", "2026-10-08T08:00:00Z"]) == 0
    claimed = json.loads(path.read_text(encoding="utf-8"))
    assert claimed["claim"]["task"] == "F"
    assert loop_claim.main(["release", "--surface", "grok-bot", "--done", "--state", str(path)]) == loop_claim.EXIT_BUSY
    assert RISK_PATH.read_bytes() == before
    assert "dotenv" not in open(loop_claim.__file__, encoding="utf-8").read()


def test_pin_guard_rejects_live_without_editing_the_file():
    before = RISK_PATH.read_bytes()
    cfg = copy.deepcopy(loop_claim.load_risk())
    cfg["live_enabled"] = True
    assert loop_claim.pin_problems(cfg)
    assert not loop_claim.pin_problems(loop_claim.load_risk())
    assert RISK_PATH.read_bytes() == before


def test_repo_queue_has_no_todo_after_task_i():
    """Task I is closed. The committed queue has no todo and no claim. The file is not written."""
    path = loop_claim.STATE_PATH
    before = path.read_bytes()
    state = json.loads(before.decode("utf-8"))
    assert state.get("claim") is None
    assert loop_claim.next_todo(state) is None
    task_i = next(task for task in state["tasks"] if task.get("id") == "I")
    assert task_i["status"] == "done"
    assert task_i.get("surface") == "grok-web"
    assert path.read_bytes() == before
