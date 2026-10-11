"""Issue #126 (Task CG, F2b): resolved markets' open legs are settled once, and a settled loss is not counted again.

Synthetic ledger rows and settlement snapshots; the ledger, snapshots, marks, books, halt state and bus all live in
the test's folder. Paper only: no network, no order.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src import halt_store, ledger_store, paper_ledger, reconcile, settlement
from src.fees import booked_fee_usd, ledger_fee
from src.podlib import load_risk
from src.risk_gate import Intent
from src.thresholds import PAPER_EQUITY_USD

DAY1 = "2026-09-01T12:00:00Z"
DAY2 = "2026-09-02T12:00:00Z"
DAY3 = "2026-09-03T12:00:00Z"
YESTERDAY = "2026-08-31T20:00:00Z"
CAP_USD = load_risk()["max_daily_loss_pct"] * PAPER_EQUITY_USD


def fill(side="buy", shares=100.0, price=0.5, ts=YESTERDAY, market="m", token="yes"):
    notional = round(shares * price, 8)
    return dict(ts=ts, market=market, token_id=token, side=side, shares=shares, notional_usd=notional,
                fee_rate=0.0, market_price=price, effective_price=price,
                fee_usd=booked_fee_usd(ledger_fee(0.0, price, price, notional, shares)),
                hypothetical=False, counts_toward_risk=True, risk_decision="allow")


def snap(condition="0xc", tokens=("yes", "no"), winner="yes", confirmed=True, legacy=False):
    row = {"condition_id": condition, "winning_token_id": winner,
           "outcome_prices_final": [1.0 if t == winner else 0.0 for t in tokens],
           "closed_ts": "2026-08-29T00:00:00Z", "snapshot_ts": "2026-08-31T01:00:00Z",
           "status": "resolved" if confirmed else "closed", "uma_checked": confirmed}
    if not legacy:
        row["outcome_token_ids"] = list(tokens)
    return row


@pytest.fixture
def env(tmp_path, monkeypatch):
    clock = {"now": DAY1}
    bus: list[tuple] = []
    paths = {"ledger": tmp_path / "paper.jsonl", "res": tmp_path / "resolutions.jsonl"}
    monkeypatch.setattr(paper_ledger, "PAPER_PATH", paths["ledger"])
    monkeypatch.setattr(paper_ledger, "QUOTES_PATH", tmp_path / "quotes.jsonl")
    monkeypatch.setattr(paper_ledger, "MARK_BOOKS_PATH", tmp_path / "books.jsonl")
    monkeypatch.setattr(paper_ledger, "DAILY_MARKS_DIR", tmp_path / "daily_marks")
    monkeypatch.setattr(paper_ledger, "SETTLEMENT_RESOLUTIONS_PATH", paths["res"])
    monkeypatch.setattr(paper_ledger, "bus", lambda *args, **kwargs: bus.append(args))
    monkeypatch.setattr(paper_ledger, "utc_now", lambda: clock["now"])
    monkeypatch.setattr(halt_store, "HALT_STATE_PATH", tmp_path / "halt_state.json")
    monkeypatch.setattr(halt_store, "HALT_FALLBACK_PATH", tmp_path / "HALT")
    return {**paths, "clock": clock, "bus": bus}


def _book(path: Path, rows):
    for index, row in enumerate(rows):
        ledger_store.book(path, {"fill_id": f"cg-{index:04d}", **row})


def _snaps(path: Path, *rows):
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def _intent(market="x", token="t"):
    return Intent(strategy="test", market=market, side="buy", model_prob=0.6, market_price=0.4,
                  assumed_slippage=0.005, fee_rate=0.0, liquidity_usd=50_000.0, spread=0.01, token_id=token,
                  book_ts=DAY1)


def _rows(path: Path) -> list[dict]:
    return ledger_store.read_ledger(path)[1].rows


# --- acceptance -------------------------------------------------------------------------------------

def test_a_losing_long_settles_once_and_is_not_counted_again_on_later_utc_days(env):
    """Required regression: a settled leg's loss counts on its settlement day only."""
    _book(env["ledger"], [fill("buy", 20.0, 0.5, token="no")])  # cost 10, the losing token
    _snaps(env["res"], snap(winner="yes"))
    # Day 1 (UTC): settlement is booked, the 10 USD loss is realized today, the leg is closed.
    out = paper_ledger.apply_settlements()
    assert [row["settlement_payout"] for row in out["booked"]] == [0.0] and out["problems"] == []
    day1 = paper_ledger.load_state("x", "t")
    assert day1["daily_realized_loss_usd"] == pytest.approx(10.0)
    assert day1["daily_loss_usd"] == pytest.approx(10.0)  # not 20: no unrealized part on a closed leg
    assert day1["open_positions"] == 0 and day1["daily_marks"] == []
    # Days 2 and 3: nothing settles again and nothing counts again.
    for now in (DAY2, DAY3):
        env["clock"]["now"] = now
        assert paper_ledger.apply_settlements()["booked"] == []
        later = paper_ledger.load_state("x", "t")
        assert (later["daily_loss_usd"], later["daily_unrealized_loss_usd"], later["open_positions"]) == (0.0, 0.0, 0)
    assert sum(1 for row in _rows(env["ledger"]) if ledger_store.is_settlement(row)) == 1


def test_without_settlement_a_closed_leg_counted_again_every_day(env):
    """The bug this fixes (Task CF valued a book-less closed leg at 0 against its cost every day)."""
    _book(env["ledger"], [fill("buy", 20.0, 0.5, token="no")])
    for now in (DAY1, DAY2, DAY3):
        env["clock"]["now"] = now
        assert paper_ledger.load_state("x", "t")["daily_loss_usd"] == pytest.approx(10.0)


def test_three_settled_losers_do_not_keep_the_breaker_tripped(env):
    _book(env["ledger"], [fill("buy", 24.0, 0.5, market=f"m{i}", token=f"no{i}") for i in range(3)])
    _snaps(env["res"], *[snap(condition=f"0x{i}", tokens=(f"yes{i}", f"no{i}"), winner=f"yes{i}") for i in range(3)])
    paper_ledger.apply_settlements()
    assert paper_ledger.load_state("x", "t")["daily_loss_usd"] == pytest.approx(36.0)  # day 1: >= cap
    env["clock"]["now"] = DAY2
    state = paper_ledger.load_state("x", "t")
    assert state["daily_loss_usd"] == 0.0
    assert "max_daily_loss" not in paper_ledger.evaluate(_intent(), load_risk(), state).reasons


def test_a_winning_leg_settles_at_one_and_its_gain_offsets_nothing(env):
    _book(env["ledger"], [fill("buy", 20.0, 0.5, token="yes"), fill("buy", 20.0, 0.5, market="b", token="nb")])
    _snaps(env["res"], snap(winner="yes"), snap(condition="0xb", tokens=("yb", "nb"), winner="yb"))
    out = paper_ledger.apply_settlements()
    assert sorted(row["settlement_payout"] for row in out["booked"]) == [0.0, 1.0]
    state = paper_ledger.load_state("x", "t")
    assert state["daily_realized_gain_usd"] == pytest.approx(10.0)
    assert state["daily_loss_usd"] == pytest.approx(10.0)  # the loser only
    assert state["open_positions"] == 0
    _, read = ledger_store.read_ledger(env["ledger"])
    assert ledger_store.verify(read) == []  # cash, shares and cost conservation hold with settlement rows
    assert read.rows[-1]["cash_after_usd"] == pytest.approx(PAPER_EQUITY_USD - 20.0 + 20.0)


def test_a_short_is_bought_back_at_the_payout(env):
    _book(env["ledger"], [fill("sell", 10.0, 0.3, token="yes")])
    _snaps(env["res"], snap(winner="yes"))
    row = paper_ledger.apply_settlements()["booked"][0]
    assert (row["side"], row["shares"], row["notional_usd"]) == ("buy", 10.0, 10.0)
    assert paper_ledger.load_state("x", "t")["daily_realized_loss_usd"] == pytest.approx(7.0)


def test_settlement_is_idempotent_and_a_conflicting_copy_is_an_integrity_problem(env):
    _book(env["ledger"], [fill("buy", 20.0, 0.5, token="no")])
    _snaps(env["res"], snap())
    first = paper_ledger.apply_settlements()["booked"][0]
    before = env["ledger"].read_bytes()
    assert paper_ledger.apply_settlements() == {"booked": [], "unsettled_closed_legs": [], "problems": []}
    assert env["ledger"].read_bytes() == before
    with env["ledger"].open("ab") as handle:  # the same row again: deduped, counted once
        handle.write(before.splitlines(keepends=True)[-1])
    _, read = ledger_store.read_ledger(env["ledger"])
    assert read.duplicates_ignored == 1 and ledger_store.verify(read) == []
    changed = dict(first, settlement_payout=1.0)
    with env["ledger"].open("ab") as handle:
        handle.write((json.dumps(changed) + "\n").encode())
    _, read = ledger_store.read_ledger(env["ledger"])
    assert f"conflicting_fill_id:{first['fill_id']}" in ledger_store.verify(read)


def test_an_unconfirmed_snapshot_settles_nothing_and_is_reported(env):
    _book(env["ledger"], [fill("buy", 20.0, 0.5, token="no")])
    _snaps(env["res"], snap(confirmed=False))
    out = paper_ledger.apply_settlements()
    assert out["booked"] == [] and out["unsettled_closed_legs"] == ["m|no"]
    state = paper_ledger.load_state("x", "t")
    assert state["unsettled_closed_legs"] == ["m|no"] and state["open_positions"] == 1


def test_a_legacy_snapshot_settles_the_winner_only(env):
    _book(env["ledger"], [fill("buy", 20.0, 0.5, token="yes"), fill("buy", 20.0, 0.5, token="no")])
    _snaps(env["res"], snap(legacy=True))
    out = paper_ledger.apply_settlements()
    assert [(r["token_id"], r["settlement_payout"]) for r in out["booked"]] == [("yes", 1.0)]


@pytest.mark.parametrize("change, problem", [
    ({"settlement_payout": None, "market_price": 0.5, "effective_price": 0.5}, "invalid_settlement"),
    ({"fee_usd": 0.01}, "invalid_settlement"),
    ({"notional_usd": 3.0}, "invalid_settlement"),
    ({"risk_decision": "allow"}, "invalid_risk_decision"),
    ({"kind": "redeem"}, "invalid_kind"),
    ({"hypothetical": True, "counts_toward_risk": False}, "invalid_settlement"),
    ({"winning_token_id": ""}, "invalid_settlement"),
    ({"shares": 10.0, "notional_usd": 0.0}, "settlement_not_closing"),
])
def test_a_bad_settlement_row_is_an_integrity_problem(env, change, problem):
    _book(env["ledger"], [fill("buy", 20.0, 0.5, token="no")])
    _snaps(env["res"], snap())
    row = settlement.plan(_rows(env["ledger"]), [], [snap()], DAY1).rows[0]
    with pytest.raises(ledger_store.LedgerIntegrityError) as caught:
        ledger_store.book(env["ledger"], dict(row, **change))
    assert any(problem in p for p in caught.value.problems)


def test_a_damaged_settlement_row_in_the_ledger_is_invalid_risk_state_and_a_global_halt(env):
    _book(env["ledger"], [fill("buy", 20.0, 0.5, token="no")])
    row = settlement.plan(_rows(env["ledger"]), [], [snap()], DAY1).rows[0]
    with env["ledger"].open("ab") as handle:
        handle.write((json.dumps(dict(row, fee_usd="0")) + "\n").encode())
    result = paper_ledger.commit_if_allowed(_intent(), load_risk())
    assert result["halted"] is True and result["halt_reason"] == "invalid_risk_state"
    assert any(h["scope"] == "global" for h in halt_store.active_halts())


@pytest.mark.parametrize("bad", ["garbage", "conflict"])
def test_an_unusable_settlement_history_halts_before_any_booking(env, bad):
    _book(env["ledger"], [fill("buy", 20.0, 0.5, token="no")])
    if bad == "garbage":
        env["res"].write_text("{not json\n")
    else:
        _snaps(env["res"], snap(), snap(condition="0xother", winner="no"))
    before = env["ledger"].read_bytes()
    result = paper_ledger.commit_if_allowed(_intent(), load_risk())
    assert result["allow"] is False and result["halted"] is True and result["halt_reason"] == "invalid_risk_state"
    assert result["ledger_problems"]
    assert env["ledger"].read_bytes() == before


def test_commit_if_allowed_settles_first(env):
    _book(env["ledger"], [fill("buy", 20.0, 0.5, token="no")])
    _snaps(env["res"], snap())
    paper_ledger.commit_if_allowed(_intent(), load_risk())
    assert any(ledger_store.is_settlement(row) for row in _rows(env["ledger"]))


def test_no_snapshots_reads_nothing_and_books_nothing(env):
    _book(env["ledger"], [fill("buy", 20.0, 0.5, token="no")])
    before = env["ledger"].read_bytes()
    assert paper_ledger.apply_settlements() == {"booked": [], "unsettled_closed_legs": [], "problems": []}
    assert env["ledger"].read_bytes() == before


def test_the_reconciler_does_not_count_a_settlement_as_a_trade(env):
    _book(env["ledger"], [fill("buy", 20.0, 0.5, token="no")])
    _snaps(env["res"], snap())
    paper_ledger.apply_settlements()
    view = reconcile.ledger_view(env["ledger"])
    assert view.problems == []
    assert view.flows["no"] == {"BUY": 20.0, "SELL": 0.0} and view.shares["no"] == 0.0
