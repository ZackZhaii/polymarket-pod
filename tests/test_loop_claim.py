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


def test_repo_queue_has_i_done_and_no_open_task():
    """After I, J, K, L, M, N, O, P, Q, R, S, T, U, V, W, X, Y, Z, AA, AB, AC, AD, AE, AF, AG, AH, AI, AJ, AK, AL, AN, AQ, AR, AS, AU, AW, AY, BA, BC and BD close, the committed queue has no claim and no todo. The file is not written."""
    path = loop_claim.STATE_PATH
    before = path.read_bytes()
    state = json.loads(before.decode("utf-8"))
    assert state.get("claim") is None
    task_i = next(task for task in state["tasks"] if task["id"] == "I")
    assert task_i["status"] == "done"
    task_j = next(task for task in state["tasks"] if task["id"] == "J")
    assert task_j["status"] == "done"
    task_k = next(task for task in state["tasks"] if task["id"] == "K")
    assert task_k["status"] == "done"
    task_l = next(task for task in state["tasks"] if task["id"] == "L")
    assert task_l["status"] == "done"
    task_m = next(task for task in state["tasks"] if task["id"] == "M")
    assert task_m["status"] == "done"
    task_n = next(task for task in state["tasks"] if task["id"] == "N")
    assert task_n["status"] == "done"
    task_o = next(task for task in state["tasks"] if task["id"] == "O")
    assert task_o["status"] == "done"
    task_p = next(task for task in state["tasks"] if task["id"] == "P")
    assert task_p["status"] == "done"
    task_q = next(task for task in state["tasks"] if task["id"] == "Q")
    assert task_q["status"] == "done"
    task_r = next(task for task in state["tasks"] if task["id"] == "R")
    assert task_r["status"] == "done"
    task_s = next(task for task in state["tasks"] if task["id"] == "S")
    assert task_s["status"] == "done"
    task_t = next(task for task in state["tasks"] if task["id"] == "T")
    assert task_t["status"] == "done"
    task_u = next(task for task in state["tasks"] if task["id"] == "U")
    assert task_u["status"] == "done"
    task_v = next(task for task in state["tasks"] if task["id"] == "V")
    assert task_v["status"] == "done"
    task_w = next(task for task in state["tasks"] if task["id"] == "W")
    assert task_w["status"] == "done"
    task_x = next(task for task in state["tasks"] if task["id"] == "X")
    assert task_x["status"] == "done"
    task_y = next(task for task in state["tasks"] if task["id"] == "Y")
    assert task_y["status"] == "done"
    task_z = next(task for task in state["tasks"] if task["id"] == "Z")
    assert task_z["status"] == "done"
    task_aa = next(task for task in state["tasks"] if task["id"] == "AA")
    assert task_aa["status"] == "done"
    task_ab = next(task for task in state["tasks"] if task["id"] == "AB")
    assert task_ab["status"] == "done"
    task_ac = next(task for task in state["tasks"] if task["id"] == "AC")
    assert task_ac["status"] == "done"
    task_ad = next(task for task in state["tasks"] if task["id"] == "AD")
    assert task_ad["status"] == "done"
    task_ae = next(task for task in state["tasks"] if task["id"] == "AE")
    assert task_ae["status"] == "done"
    task_af = next(task for task in state["tasks"] if task["id"] == "AF")
    assert task_af["status"] == "done"
    task_ag = next(task for task in state["tasks"] if task["id"] == "AG")
    assert task_ag["status"] == "done"
    task_ah = next(task for task in state["tasks"] if task["id"] == "AH")
    assert task_ah["status"] == "done"
    task_ai = next(task for task in state["tasks"] if task["id"] == "AI")
    assert task_ai["status"] == "done"
    task_aj = next(task for task in state["tasks"] if task["id"] == "AJ")
    assert task_aj["status"] == "done"
    task_ak = next(task for task in state["tasks"] if task["id"] == "AK")
    assert task_ak["status"] == "done"
    task_al = next(task for task in state["tasks"] if task["id"] == "AL")
    assert task_al["status"] == "done"
    task_an = next(task for task in state["tasks"] if task["id"] == "AN")
    assert task_an["status"] == "done"
    task_aq = next(task for task in state["tasks"] if task["id"] == "AQ")
    assert task_aq["status"] == "done"
    task_ar = next(task for task in state["tasks"] if task["id"] == "AR")
    assert task_ar["status"] == "done"
    task_as = next(task for task in state["tasks"] if task["id"] == "AS")
    assert task_as["status"] == "done" and task_as["surface"] == "grok-bot"
    task_au = next(task for task in state["tasks"] if task["id"] == "AU")
    assert task_au["status"] == "done"
    task_aw = next(task for task in state["tasks"] if task["id"] == "AW")
    assert task_aw["status"] == "done" and task_aw["surface"] == "grok-bot"
    task_ay = next(task for task in state["tasks"] if task["id"] == "AY")
    assert task_ay["status"] == "done" and task_ay["surface"] == "grok-bot"
    task_ba = next(task for task in state["tasks"] if task["id"] == "BA")
    assert task_ba["status"] == "done" and task_ba["surface"] == "grok-bot"
    task_bc = next(task for task in state["tasks"] if task["id"] == "BC")
    assert task_bc["status"] == "done" and task_bc["surface"] == "grok-bot"
    task_bd = next(task for task in state["tasks"] if task["id"] == "BD")
    assert task_bd["status"] == "done" and task_bd["surface"] == "grok-bot"
    task_be = next(task for task in state["tasks"] if task["id"] == "BE")
    assert task_be["status"] == "done" and task_be["surface"] == "grok-bot"
    task_bf = next(task for task in state["tasks"] if task["id"] == "BF")
    assert task_bf["status"] == "done" and task_bf["surface"] == "grok-bot"
    task_bg = next(task for task in state["tasks"] if task["id"] == "BG")
    assert task_bg["status"] == "done" and task_bg["surface"] == "grok-bot"
    task_bh = next(task for task in state["tasks"] if task["id"] == "BH")
    assert task_bh["status"] == "done" and task_bh["surface"] == "grok-bot"
    task_bi = next(task for task in state["tasks"] if task["id"] == "BI")
    assert task_bi["status"] == "done" and task_bi["surface"] == "grok-bot"
    task_bk = next(task for task in state["tasks"] if task["id"] == "BK")
    assert task_bk["status"] == "done" and task_bk["surface"] == "grok-bot"
    task_bj = next(task for task in state["tasks"] if task["id"] == "BJ")
    assert task_bj["status"] == "done" and task_bj["surface"] == "grok-bot"
    assert loop_claim.next_todo(state) is None
    assert path.read_bytes() == before
