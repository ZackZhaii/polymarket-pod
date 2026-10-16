"""Issue #349 (Task EG): the three SETTLEMENT_BLOCKING_HALT_PREFIXES settlement_result_changed, settlement_conflict and
invalid_settlement_payout were never recorded as a market halt, so blocks_settlement never applied to them: only the
global invalid_risk_state halted, and once the Operator resumed that global halt the market settled again.

Now each of the three problems is also a scope=market halt whose reason starts with its prefix (the global halt is
kept, no prefix is removed): a global resume alone leaves the market blocked, other markets settle, and only the
Operator's resume of that market halt lets it settle again. Draft (Poly exec, issue #349 comments 6206470263 /
6206545843; correction 6206705965 / 6206857474) with the strict xfail markers removed once the halts are written.
Offline; no network, no keys, no orders.
"""
from __future__ import annotations

import json
import re
from datetime import timedelta
from pathlib import Path

import pytest

from src import halt_store, ledger_store, m2_runner, paper_ledger, settlement
from tests import m2_runner_rig as rig
from tests.test_runner_settlement import (NOW, LOSER, WINNER, Gamma, _book, _events, _global_halt,  # noqa: F401
                                          _hold_loser, _settlements, _tick, env, fill, resolved_row)

ROOT = Path(__file__).resolve().parents[1]
DEAD = ["settlement_result_changed", "settlement_conflict", "invalid_settlement_payout"]
OTHER_ROW = dict(id="m-other", conditionId="0xother", clobTokenIds='["o1", "o2"]')


@pytest.mark.parametrize("prefix", DEAD)
def test_a_some_src_code_records_a_market_halt_with_the_prefix(prefix):
    """Static: outside settlement.py's tuple, the prefix must appear as a halt reason (sentinel.halt / halt_store)."""
    hits = []
    for path in (ROOT / "src").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"halt\([^)]*" + re.escape(prefix), text, flags=re.S):
            hits.append(f"{path.name}:{match.start()}")
    assert hits, f"{prefix}: never passed to a halt() call in src/"


def test_b_a_settlement_conflict_also_records_a_blocking_market_halt(env):
    """Same scenario as tests/test_runner_settlement::test_a_contradiction_in_the_resolutions_is_a_global_halt."""
    _hold_loser(env)
    _tick(env, Gamma(resolved_row()))
    rows = [json.loads(line) for line in env["res"].read_text().splitlines()]
    other = dict(rows[0], winning_token_id=LOSER, outcome_prices_final=[0.0, 1.0])
    with env["res"].open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(other) + "\n")
    _book(env, [fill("buy", 5.0, 0.5, NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), market="m-other", token="o1")], prefix="o")
    _tick(env, Gamma(resolved_row(id="m-other", conditionId="0xother", clobTokenIds='["o1", "o2"]')),
          NOW + timedelta(seconds=60))
    assert _global_halt()  # the global halt is kept (control inside the repro)
    market_blocks = [h for h in halt_store.active_halts()
                     if h.get("scope") == halt_store.SCOPE_MARKET and settlement.blocks_settlement(h.get("reason"))]
    assert market_blocks, "only the global halt exists; clearing it would let the market settle again"


# control (must stay green): blocks_settlement itself accepts all four prefixes
def test_c_control_blocks_settlement_accepts_the_prefixes():
    for prefix in DEAD + ["closed_flag_contradiction_after_writeoff"]:
        assert settlement.blocks_settlement(f"{prefix}:m1")


def test_c2_the_three_prefixes_stay_in_the_blocking_tuple():
    """Correction 6206705965: no prefix is removed or weakened (only market halts are added)."""
    for prefix in DEAD:
        assert prefix in settlement.SETTLEMENT_BLOCKING_HALT_PREFIXES
        assert settlement.blocks_settlement(prefix) and settlement.blocks_settlement(f"{prefix}:m1|t1")


# ---------------------------------------------------------------- each prefix, end to end (M2 runner tick)

def _stamp(when):
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _history(env):
    return [json.loads(line) for line in env["res"].read_text().splitlines()]


def _write_history(env, rows):
    env["res"].write_bytes("".join(json.dumps(row) + "\n" for row in rows).encode("utf-8"))


def _blocking_market_halts():
    return [h for h in halt_store.active_halts()
            if h.get("scope") == halt_store.SCOPE_MARKET and settlement.blocks_settlement(h.get("reason"))]


def _resume_every_global_halt():
    """The Operator resumes the global halt(s) only (as after a postmortem of invalid_risk_state)."""
    found = [h for h in halt_store.active_halts() if h["scope"] == halt_store.SCOPE_GLOBAL]
    assert found
    for record in found:
        halt_store.resume(from_role="Operator", scope=halt_store.SCOPE_GLOBAL, halt_id=record["id"],
                          approved_by="test", root_cause="settlement data repaired", postmortem="#349")
    assert not _global_halt()


def _conflict(env, settled):
    """Two confirmed snapshots of the market's condition disagree (another winner)."""
    with env["res"].open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(dict(settled, winning_token_id=LOSER, outcome_prices_final=[0.0, 1.0])) + "\n")


def _result_changed(env, settled):
    """The booked settlement is read again with another result (the stored snapshot was replaced)."""
    _write_history(env, [dict(settled, winning_token_id=LOSER, outcome_prices_final=[0.0, 1.0])])


def _invalid_payout(env, settled):
    """A settlement row paid outside {0, 1} (0.5) is in the ledger (book() refuses it, so it is appended raw)."""
    row = dict(_settlements(env)[0], fill_id="settle-bad-payout", market_price=0.5, effective_price=0.5,
               settlement_payout=0.5, notional_usd=10.0)
    with env["paper"].open("ab") as handle:
        handle.write((json.dumps(row) + "\n").encode("utf-8"))


SCENARIOS = {"settlement_conflict": _conflict, "settlement_result_changed": _result_changed,
             "invalid_settlement_payout": _invalid_payout}


@pytest.mark.parametrize("prefix", DEAD)
def test_d_each_problem_is_a_global_and_a_blocking_market_halt_a_global_resume_does_not_lift(env, prefix):
    """#352 report section 3 (A): for each prefix, the global halt AND a market halt with that prefix; after the
    Operator resumes only the global halt (data repaired) the market still does not settle, the other market does;
    the market settles only after its own market halt is resumed."""
    _hold_loser(env)
    _tick(env, Gamma(resolved_row()))  # stores the market's snapshot and settles its losing leg
    assert len(_settlements(env)) == 1
    settled = _history(env)[0]
    _book(env, [fill("buy", 5.0, 0.5, _stamp(NOW), market="m-other", token="o1")], prefix="o")
    SCENARIOS[prefix](env, settled)

    report = _tick(env, Gamma(resolved_row(**OTHER_ROW)), NOW + timedelta(seconds=60))
    assert report.reasons[0] == m2_runner.LEDGER_HALT_REASON and _global_halt()  # global halt kept
    blocks = _blocking_market_halts()
    assert [h["market"] for h in blocks] == [rig.MARKET_ID]
    assert all(h["reason"].startswith(prefix + ":") for h in blocks)
    assert {s["market"] for s in _settlements(env)} == {rig.MARKET_ID}  # nothing else was booked (m-other open)

    # the Operator repairs the data and resumes the global halt only
    if prefix == "invalid_settlement_payout":
        lines = env["paper"].read_bytes().splitlines(keepends=True)
        env["paper"].write_bytes(b"".join(lines[:-1]))
    _write_history(env, [settled] + [row for row in _history(env) if row["condition_id"] != settled["condition_id"]])
    _resume_every_global_halt()
    assert _blocking_market_halts() == blocks  # a global resume never lifts a market halt
    _book(env, [fill("buy", 5.0, 0.5, _stamp(NOW), token=WINNER)], prefix="w")  # a new leg in the halted market

    _tick(env, Gamma(resolved_row(**OTHER_ROW)), NOW + timedelta(seconds=120))
    booked = _settlements(env)
    assert sorted(s["market"] for s in booked) == sorted([rig.MARKET_ID, "m-other"])  # m-other settled now
    assert not any(s["market"] == rig.MARKET_ID and s["token_id"] == WINNER for s in booked)  # still blocked
    assert _events(env, "settlement_blocked")[-1]["markets"] == [rig.MARKET_ID] and not _global_halt()
    assert paper_ledger.apply_settlements(_stamp(NOW + timedelta(seconds=130)))["skipped_markets"] == [rig.MARKET_ID]

    # only the Operator's resume of the market halt itself lets that market settle again
    for record in blocks:
        halt_store.resume(from_role="Operator", scope=halt_store.SCOPE_MARKET, halt_id=record["id"],
                          market=rig.MARKET_ID, approved_by="test", root_cause="repaired", postmortem="#349")
    _tick(env, Gamma(resolved_row()), NOW + timedelta(seconds=180))
    assert any(s["market"] == rig.MARKET_ID and s["token_id"] == WINNER for s in _settlements(env))
    assert not _global_halt() and _blocking_market_halts() == []


@pytest.mark.parametrize("prefix", DEAD)
def test_e_the_commit_path_records_the_same_market_halt(env, prefix):
    """commit_if_allowed's apply_settlements() (no runner) writes the market halt too; nothing is booked."""
    _hold_loser(env)
    _tick(env, Gamma(resolved_row()))
    settled = _history(env)[0]
    SCENARIOS[prefix](env, settled)
    out = paper_ledger.apply_settlements(_stamp(NOW + timedelta(seconds=60)))
    assert out["booked"] == []
    if prefix == "invalid_settlement_payout":
        # the ledger does not verify: as before nothing is reported here and load_state is invalid_risk_state
        assert out["problems"] == [] and ledger_store.verify(ledger_store.read_ledger(env["paper"])[1])
    else:
        assert any(str(p).startswith(prefix + ":") for p in out["problems"])  # the caller's global halt
    blocks = _blocking_market_halts()
    assert [h["market"] for h in blocks] == [rig.MARKET_ID] and blocks[0]["reason"].startswith(prefix + ":")
    again = paper_ledger.apply_settlements(_stamp(NOW + timedelta(seconds=120)))  # an identical halt is not added
    assert again["booked"] == [] and _blocking_market_halts() == blocks


def test_f_a_token_in_two_conditions_halts_every_market_holding_it():
    rows = [fill("buy", 5.0, 0.5, _stamp(NOW), market="mA", token="t1"),
            fill("buy", 5.0, 0.5, _stamp(NOW), market="mB", token="t1"),
            fill("buy", 5.0, 0.5, _stamp(NOW), market="mC", token="t9")]

    def snap(condition, tokens):
        return {"condition_id": condition, "winning_token_id": tokens[0], "outcome_prices_final": [1.0, 0.0],
                "closed_ts": "2026-01-01T00:00:00Z", "snapshot_ts": "2026-01-03T00:00:00Z", "status": "resolved",
                "uma_checked": True, "outcome_token_ids": tokens}

    planned = settlement.plan(rows, [], [snap("0xa", ["t1", "t2"]), snap("0xb", ["t3", "t1"])], _stamp(NOW))
    assert planned.rows == [] and planned.problems == ["settlement_conflict:t1:token_in_two_conditions"]
    assert planned.market_halts == [("mA", "settlement_conflict:t1:token_in_two_conditions"),
                                    ("mB", "settlement_conflict:t1:token_in_two_conditions")]
    assert all(settlement.blocks_settlement(reason) for _market, reason in planned.market_halts)


def test_g_no_problem_no_market_halt(env):
    """Control: a clean settlement writes no market halt (and the plan carries none)."""
    _hold_loser(env)
    _tick(env, Gamma(resolved_row()))
    _tick(env, Gamma(resolved_row()), NOW + timedelta(seconds=60))
    assert len(_settlements(env)) == 1 and not _global_halt()
    assert [h for h in halt_store.active_halts() if h["scope"] == halt_store.SCOPE_MARKET] == []
    read = ledger_store.read_ledger(env["paper"])[1]
    planned = settlement.plan(read.rows, list(read.ids), _history(env), _stamp(NOW))
    assert planned.problems == [] and planned.market_halts == []
    assert settlement.invalid_payout_halts(read.rows) == []


# ---------------------------------------------------------------- PR #360 review (Codex 6213482409, Poly风控 6213662139)

def _snap(condition, tokens, winner):
    return {"condition_id": condition, "winning_token_id": winner, "outcome_prices_final": [1.0, 0.0],
            "closed_ts": "2026-01-01T00:00:00Z", "snapshot_ts": "2026-01-03T00:00:00Z", "status": "resolved",
            "uma_checked": True, "outcome_token_ids": tokens}


def _settled_row(market, token, condition, winner, payout):
    return {"fill_id": settlement.settlement_fill_id(condition, market, token), "kind": "settlement",
            "market": market, "token_id": token, "condition_id": condition, "winning_token_id": winner,
            "settlement_payout": payout, "market_price": payout, "effective_price": payout}


@pytest.mark.parametrize("huge", [10**400, -10**400])
def test_h_a_huge_int_payout_is_invalid_not_an_overflow(huge):
    """P1-1: 10**400 in a settlement row is invalid (None) and halts its market, never an OverflowError."""
    assert settlement._number(huge) is None
    row = _settled_row("m1", "t1", "0xc", "t1", 1.0)
    row["settlement_payout"] = huge
    assert settlement.invalid_payout_halts([row]) == [("m1", "invalid_settlement_payout:m1|t1")]
    planned = settlement.plan([row], [row["fill_id"]], [_snap("0xc", ["t1", "t2"], "t1")], _stamp(NOW))
    assert planned.rows == [] and planned.problems == ["settlement_result_changed:m1|t1"]


def test_h2_the_commit_path_with_a_huge_int_payout_records_the_market_halt(env):
    """P1-1 end to end: apply_settlements on a ledger with settlement_payout=10**400 does not crash and writes the
    invalid_settlement_payout market halt (the ledger does not verify: load_state's global halt as before)."""
    _hold_loser(env)
    _tick(env, Gamma(resolved_row()))
    row = dict(_settlements(env)[0], fill_id="settle-huge-payout", market_price=0.5, effective_price=0.5,
               settlement_payout=10**400)
    with env["paper"].open("ab") as handle:
        handle.write((json.dumps(row) + "\n").encode("utf-8"))
    out = paper_ledger.apply_settlements(_stamp(NOW + timedelta(seconds=60)))
    assert out["booked"] == [] and out["problems"] == []
    blocks = _blocking_market_halts()
    assert [h["market"] for h in blocks] == [rig.MARKET_ID]
    assert blocks[0]["reason"].startswith("invalid_settlement_payout:")


def test_h3_the_runner_with_a_huge_int_payout_halts_global_and_market(env):
    _hold_loser(env)
    _tick(env, Gamma(resolved_row()))
    row = dict(_settlements(env)[0], fill_id="settle-huge-payout", settlement_payout=10**400)
    with env["paper"].open("ab") as handle:
        handle.write((json.dumps(row) + "\n").encode("utf-8"))
    report = _tick(env, Gamma(resolved_row()), NOW + timedelta(seconds=60))
    assert report.reasons[0] == m2_runner.LEDGER_HALT_REASON and _global_halt()
    assert [h["market"] for h in _blocking_market_halts()] == [rig.MARKET_ID]


def test_i_a_settled_token_dropped_from_its_confirmed_mapping_is_a_result_change():
    """P1-2: winner a -> x and mapping [a,b] -> [a,x]: the booked settlement of b no longer maps to any snapshot,
    but its condition is still confirmed. settlement_result_changed + market halt; nothing settles (not even x)."""
    rows = [fill("buy", 5.0, 0.5, _stamp(NOW), market="mB", token="b"),
            _settled_row("mB", "b", "0xc", "a", 0.0),
            fill("buy", 5.0, 0.5, _stamp(NOW), market="mX", token="x")]
    ids = [row.get("fill_id", f"id{i}") for i, row in enumerate(rows)]
    planned = settlement.plan(rows, ids, [_snap("0xc", ["a", "x"], "x")], _stamp(NOW))
    assert planned.rows == []
    assert planned.problems == ["settlement_result_changed:mB|b"]
    assert planned.market_halts == [("mB", "settlement_result_changed:mB|b")]
    assert all(settlement.blocks_settlement(reason) for _m, reason in planned.market_halts)


def test_i2_control_no_confirmed_snapshot_for_the_token_or_its_condition_is_nothing_to_compare():
    rows = [_settled_row("mB", "b", "0xc", "a", 0.0)]
    planned = settlement.plan(rows, [rows[0]["fill_id"]], [_snap("0xother", ["o1", "o2"], "o1")], _stamp(NOW))
    assert planned.problems == [] and planned.market_halts == []


def test_i3_control_an_unchanged_settled_token_is_no_problem():
    rows = [_settled_row("mB", "b", "0xc", "a", 0.0)]
    planned = settlement.plan(rows, [rows[0]["fill_id"]], [_snap("0xc", ["a", "b"], "a")], _stamp(NOW))
    assert planned.problems == [] and planned.market_halts == []


def _failing_halt(**kwargs):
    return {"type": "halt", "failures": ["halt_record_fallback"]}


def test_j_record_market_halts_reports_sentinel_failures_and_exceptions(monkeypatch):
    """P2: a failure sentinel.halt returns (not raises) is reported to the caller, as is a raised one."""
    from src import sentinel

    monkeypatch.setattr(halt_store, "has_active_halt", lambda **kw: False)
    monkeypatch.setattr(sentinel, "halt", _failing_halt)
    assert settlement.record_market_halts([("m1", "settlement_conflict:x")]) == [
        "settlement_market_halt_failed:m1:halt_record_fallback"]

    def boom(**kwargs):
        raise OSError("disk")
    monkeypatch.setattr(sentinel, "halt", boom)
    assert settlement.record_market_halts([("m1", "settlement_conflict:x")]) == [
        "settlement_market_halt_failed:m1:OSError"]
    monkeypatch.setattr(sentinel, "halt", lambda **kw: {"type": "halt"})
    assert settlement.record_market_halts([("m1", "settlement_conflict:x")]) == []  # control


@pytest.mark.parametrize("prefix", DEAD)
def test_j2_the_commit_path_reports_an_unwritten_market_halt(env, monkeypatch, prefix):
    from src import sentinel

    _hold_loser(env)
    _tick(env, Gamma(resolved_row()))
    SCENARIOS[prefix](env, _history(env)[0])
    monkeypatch.setattr(sentinel, "halt", _failing_halt)
    out = paper_ledger.apply_settlements(_stamp(NOW + timedelta(seconds=60)))
    assert out["booked"] == []
    assert f"settlement_market_halt_failed:{rig.MARKET_ID}:halt_record_fallback" in out["problems"]


def test_j3_the_runner_preflight_reports_an_unwritten_market_halt(env, monkeypatch):
    _hold_loser(env)
    _tick(env, Gamma(resolved_row()))
    _invalid_payout(env, _history(env)[0])
    monkeypatch.setattr(settlement, "record_market_halts", lambda halts, **kw: ["settlement_market_halt_failed:m:x"])
    report = _tick(env, Gamma(resolved_row()), NOW + timedelta(seconds=60))
    assert report.reasons[0] == m2_runner.LEDGER_HALT_REASON and _global_halt()
    assert "settlement_market_halt_failed:m:x" in report.reasons
