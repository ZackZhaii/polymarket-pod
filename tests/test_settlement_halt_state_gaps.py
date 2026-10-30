"""PR #211 gap: ``paper_ledger.settlement_blocked_markets`` (the halt state -> blocked markets) is never
exercised through ``apply_settlements`` without an explicit ``skip_markets``.

tests/test_runner_settlement.py books the runner path and always passes ``skip_markets=...`` itself, so the
wiring that reads logs/halt_state.json (scope=market, a settlement-data reason) and the fail-closed branch
("halt state unreadable -> settle nothing, the caller halts") had no direct coverage. Offline: the ledger,
snapshots and halt state all live in the test's tmp folder. Paper only, no network, no order.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src import halt_store, ledger_store, paper_ledger, podlib, settlement
from src.fees import booked_fee_usd, ledger_fee

NOW = "2026-09-01T12:00:00Z"
YESTERDAY = "2026-08-31T20:00:00Z"
M1, M2 = "m-one", "m-two"


def fill(side="buy", shares=100.0, price=0.5, ts=YESTERDAY, market=M1, token="yes"):
    notional = round(shares * price, 8)
    return dict(ts=ts, market=market, token_id=token, side=side, shares=shares, notional_usd=notional,
                fee_rate=0.0, market_price=price, effective_price=price,
                fee_usd=booked_fee_usd(ledger_fee(0.0, price, price, notional, shares)),
                hypothetical=False, counts_toward_risk=True, risk_decision="allow")


def snap(condition="0xc", tokens=("yes", "no"), winner="no"):
    """A confirmed (UMA checked, resolved) snapshot: only those settle anything (src/resolutions.py)."""
    return {"condition_id": condition, "winning_token_id": winner,
            "outcome_prices_final": [1.0 if token == winner else 0.0 for token in tokens],
            "outcome_token_ids": list(tokens),
            "closed_ts": "2026-08-29T00:00:00Z", "snapshot_ts": "2026-08-31T01:00:00Z",
            "status": "resolved", "uma_checked": True}


@pytest.fixture
def env(tmp_path, monkeypatch):
    paths = {"ledger": tmp_path / "paper.jsonl", "res": tmp_path / "resolutions.jsonl",
             "halts": tmp_path / "halt_state.json", "halt_marker": tmp_path / "HALT",
             "bus": tmp_path / "bus.jsonl"}
    monkeypatch.setattr(paper_ledger, "PAPER_PATH", paths["ledger"])
    monkeypatch.setattr(paper_ledger, "SETTLEMENT_RESOLUTIONS_PATH", paths["res"])
    monkeypatch.setattr(paper_ledger, "bus", lambda *args, **kwargs: None)
    monkeypatch.setattr(halt_store, "HALT_STATE_PATH", paths["halts"])
    monkeypatch.setattr(halt_store, "HALT_FALLBACK_PATH", paths["halt_marker"])
    monkeypatch.setattr(podlib, "BUS_PATH", paths["bus"])  # never the real logs/bus.jsonl
    return paths


def _book(path: Path, rows):
    for index, row in enumerate(rows):
        ledger_store.book(path, {"fill_id": f"hs-{index:04d}", **row})


def _snaps(path: Path, *rows):
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def _open_legs(path: Path):
    rows = ledger_store.read_ledger(path)[1].rows
    return settlement.open_legs(rows), [row for row in rows if ledger_store.is_settlement(row)]


# ---------------------------------------------------------------- settlement_blocked_markets()


def test_only_a_blocking_market_halt_is_in_the_blocked_set(env):
    halt_store.record_halt(scope=halt_store.SCOPE_MARKET, market=M1, reason="settlement_conflict:0xc",
                           source="RiskGate")
    halt_store.record_halt(scope=halt_store.SCOPE_MARKET, market=M2, reason="stale_book", source="Sentinel")
    assert paper_ledger.settlement_blocked_markets() == frozenset({M1})


def test_a_global_halt_with_a_blocking_reason_blocks_no_market(env):
    """Scope decides: a global halt stops the tick at the caller, it never skips one market's settlement."""
    halt_store.record_halt(scope=halt_store.SCOPE_GLOBAL, reason="settlement_conflict:0xc", source="RiskGate")
    assert paper_ledger.settlement_blocked_markets() == frozenset()


@pytest.mark.parametrize("reason", [" settlement_conflict:0xc", "invalid_settlement_payout", "settlement_result_changed"])
def test_a_padded_or_bare_blocking_reason_blocks(env, reason):
    """record_halt normalizes the reason (" ".join(split())) and blocks_settlement matches by prefix."""
    halt_store.record_halt(scope=halt_store.SCOPE_MARKET, market=M1, reason=reason, source="RiskGate")
    assert paper_ledger.settlement_blocked_markets() == frozenset({M1})


def test_a_reason_that_only_contains_the_prefix_does_not_block(env):
    halt_store.record_halt(scope=halt_store.SCOPE_MARKET, market=M1, reason="x_settlement_conflict:0xc",
                           source="RiskGate")
    assert paper_ledger.settlement_blocked_markets() == frozenset()


# ---------------------------------------------------------------- apply_settlements(): the wiring


def test_apply_settlements_reads_the_halt_state_when_skip_markets_is_not_given(env):
    """The commit path (skip_markets=None) must skip the blocked market and leave its leg open."""
    _book(env["ledger"], [fill(market=M1, token="yes")])
    _snaps(env["res"], snap())
    halt_store.record_halt(scope=halt_store.SCOPE_MARKET, market=M1, reason="closed_flag_contradiction_after_writeoff:m1",
                           source="RiskGate")

    out = paper_ledger.apply_settlements(NOW)
    assert out["booked"] == [] and out["problems"] == []
    assert out["skipped_markets"] == [M1]
    legs, settlements = _open_legs(env["ledger"])
    assert legs == {(M1, "yes"): 100.0} and settlements == []  # nothing booked, the leg is still open


def test_a_blocking_halt_for_another_market_skips_only_that_market(env):
    _book(env["ledger"], [fill(market=M1, token="yes"), fill(market=M2, token="yes2")])
    _snaps(env["res"], snap(condition="0xc", tokens=("yes", "no")),
           snap(condition="0xd", tokens=("yes2", "no2"), winner="no2"))
    halt_store.record_halt(scope=halt_store.SCOPE_MARKET, market=M2, reason="invalid_settlement_payout",
                           source="RiskGate")

    out = paper_ledger.apply_settlements(NOW)
    assert [row["market"] for row in out["booked"]] == [M1]
    assert out["skipped_markets"] == [M2] and out["problems"] == []
    legs, settlements = _open_legs(env["ledger"])
    assert legs == {(M2, "yes2"): 100.0} and len(settlements) == 1


def test_a_non_blocking_market_halt_still_settles(env):
    _book(env["ledger"], [fill(market=M1, token="yes")])
    _snaps(env["res"], snap())
    halt_store.record_halt(scope=halt_store.SCOPE_MARKET, market=M1, reason="stale_book", source="Sentinel")

    out = paper_ledger.apply_settlements(NOW)
    assert [row["market"] for row in out["booked"]] == [M1]
    assert out["problems"] == [] and out.get("skipped_markets") is None


def test_an_unreadable_halt_state_settles_nothing_fail_closed(env, monkeypatch):
    """apply_settlements line 351: any error while reading the halts means settle nothing (the caller halts)."""
    _book(env["ledger"], [fill(market=M1, token="yes")])
    _snaps(env["res"], snap())

    def boom():
        raise RuntimeError("halt state gone")

    monkeypatch.setattr(paper_ledger, "settlement_blocked_markets", boom)
    out = paper_ledger.apply_settlements(NOW)
    assert out["problems"] == ["settlement_halt_state_unreadable:RuntimeError"]
    assert out["booked"] == [] and out.get("skipped_markets") is None
    legs, settlements = _open_legs(env["ledger"])
    assert legs == {(M1, "yes"): 100.0} and settlements == []


def test_a_corrupt_halt_state_file_is_a_global_halt_not_a_per_market_block(env):
    """A corrupt state file is a *global* synthetic halt (m2_runner preflight pauses the tick), so the
    per-market block set is empty: an empty set here is not a fail-open, the global halt stops the caller."""
    _book(env["ledger"], [fill(market=M1, token="yes")])
    _snaps(env["res"], snap())
    env["halts"].write_text("{not json", encoding="utf-8")

    assert paper_ledger.settlement_blocked_markets() == frozenset()
    assert halt_store.global_halt_active(), "a corrupt state file must halt globally"
    assert [row["market"] for row in paper_ledger.apply_settlements(NOW)["booked"]] == [M1]


def test_an_operator_resume_releases_the_market_for_settlement(env):
    """The only way out of a settlement block: the Operator resumes that market's halt (PR #211)."""
    _book(env["ledger"], [fill(market=M1, token="yes")])
    _snaps(env["res"], snap())
    record = halt_store.record_halt(scope=halt_store.SCOPE_MARKET, market=M1, reason="settlement_result_changed",
                                    source="RiskGate")
    assert paper_ledger.settlement_blocked_markets() == frozenset({M1})
    assert paper_ledger.apply_settlements(NOW)["booked"] == []

    halt_store.resume(from_role="Operator", scope=halt_store.SCOPE_MARKET, halt_id=record["id"], market=M1,
                      approved_by="Operator", root_cause="test", postmortem="test",
                      path=env["halts"], fallback=env["halt_marker"], bus_path=env["bus"])
    assert paper_ledger.settlement_blocked_markets() == frozenset()
    assert [row["market"] for row in paper_ledger.apply_settlements(NOW)["booked"]] == [M1]
