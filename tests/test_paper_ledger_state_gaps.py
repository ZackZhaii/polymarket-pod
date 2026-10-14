"""Fail-closed branches of src/paper_ledger.py's risk state that no test in the repo reached.

Walking the module's problem codes against the suite left these branches, all of which turn a usable
risk state into an unusable one, and none of them was asserted anywhere: the rejection is what makes
``load_state`` report ``daily_loss_usd = None`` / invalid_risk_state, so RiskGate vetoes and (Task AL)
halts globally.

* ``leg_marks_invalid_now`` (src/paper_ledger.py:246-248): ``daily_unrealized`` must refuse to value
  anything when its ``now`` is not a zoned timestamp -- and it must do so before reading a marks
  snapshot, a book or the ledger.
* ``leg_marks_error:<type>`` (src/paper_ledger.py:295-298): the catch-all is the last line of
  defence of the valuation; a ``now`` that is not even a string must come back as a problem, never
  as a zero loss.
* ``daily_marks_missing_after_create`` (src/paper_ledger.py:272-276): another process won the
  snapshot race (``SnapshotExists``) and then the snapshot could not be read back -- the loss is
  unavailable, not 0.
* ``quote_type_error`` (src/paper_ledger.py:450-455): a quote history the complementary-leg pair
  check cannot read (``TypeError`` out of ``wash_detect.pair_quotes``) must leave ``own_quotes``
  None and name the problem, never a short or empty history.
* ``settlement_problems`` (src/paper_ledger.py:560-571): a settlement pass that reports problems
  halts the whole commit (global halt, ``allow`` False) and books nothing.

Offline: tmp_path ledger, quotes, books and marks only; the bus and ``sentinel.halt`` are stubbed, so
no real logs/ is read or written. No network, no orders, paper mode only. Nothing in src/ changes.
"""
from __future__ import annotations

import pytest

from src import paper_ledger, podlib
from src.podlib import load_risk

NOW = "2026-10-14T12:00:00Z"


@pytest.fixture
def bus(monkeypatch, tmp_path):
    """The real bus file must never be reached through podlib's own name."""
    path = tmp_path / "bus.jsonl"
    monkeypatch.setattr(podlib, "BUS_PATH", path)
    return path


@pytest.fixture
def offline(monkeypatch, tmp_path, bus):
    """Every path paper_ledger reads or writes points into tmp_path; halts are recorded, not written."""
    for name, relative in (("PAPER_PATH", "paper.jsonl"), ("QUOTES_PATH", "quotes.jsonl"),
                           ("MARK_BOOKS_PATH", "books.jsonl"), ("PAIR_MARKETS_PATH", "markets.jsonl"),
                           ("SETTLEMENT_RESOLUTIONS_PATH", "resolutions.jsonl")):
        monkeypatch.setattr(paper_ledger, name, tmp_path / relative)
    monkeypatch.setattr(paper_ledger, "DAILY_MARKS_DIR", tmp_path / "daily_marks")
    halts: list[dict] = []
    monkeypatch.setattr(paper_ledger.sentinel, "halt",
                        lambda **kwargs: halts.append(kwargs) or {"halted": True})
    return {"dir": tmp_path, "halts": halts, "bus": bus}


def _intent():
    return paper_ledger._intent(market="gap-market", token_id="gap-yes", model_prob=0.7,
                                market_price=0.4, fee_rate=0.02, hypothetical=False,
                                book_ts=podlib.utc_now())


# --- the valuation clock (leg_marks_invalid_now) ---------------------------------------------------

@pytest.mark.parametrize("now", ["", "   ", "garbage", "2026-10-14", "2026-10-14T12:00:00",
                                 "2026-13-40T00:00:00Z"], ids=["empty", "blank", "text", "date-only",
                                                               "naive", "impossible-day"])
def test_daily_unrealized_refuses_a_now_that_is_not_a_zoned_timestamp(now):
    result = paper_ledger.daily_unrealized([], [], now)
    assert result["problems"] == ["leg_marks_invalid_now"]
    assert result["loss_usd"] is None and result["realized_topup_usd"] is None and result["legs"] == []


@pytest.mark.parametrize("now", [None, 20261014], ids=["none", "int"])
def test_daily_unrealized_reports_a_broken_now_as_an_error_and_never_a_zero_loss(now):
    # the catch-all, not "no open legs": a valuation that cannot run is an invalid risk state
    result = paper_ledger.daily_unrealized([], [], now)
    assert result["problems"] == ["leg_marks_error:TypeError"]
    assert result["loss_usd"] is None and result["realized_topup_usd"] is None and result["late"] is None


def test_the_now_check_runs_before_any_snapshot_book_or_ledger_is_read(offline):
    good = paper_ledger.daily_unrealized([], [], NOW)  # a usable day: an empty book is worth nothing
    assert good["problems"] == [] and good["loss_usd"] == 0.0
    assert paper_ledger.daily_unrealized([], [], "garbage")["problems"] == ["leg_marks_invalid_now"]
    assert not (offline["dir"] / "paper.jsonl").exists()  # neither day read or wrote the ledger


# --- the snapshot race (daily_marks_missing_after_create) ------------------------------------------

def test_a_snapshot_created_by_another_process_and_then_unreadable_fails_closed(offline, monkeypatch):
    def lost_the_race(*args, **kwargs):
        # the real exception type: the branch under test catches daily_marks.SnapshotExists only
        raise paper_ledger.daily_marks.SnapshotExists("another process wrote it first")

    monkeypatch.setattr(paper_ledger.daily_marks, "create_snapshot", lost_the_race)
    monkeypatch.setattr(paper_ledger.daily_marks, "read_snapshot", lambda *args, **kwargs: (None, []))
    result = paper_ledger.daily_unrealized([], [], NOW)
    assert result["problems"] == ["daily_marks_missing_after_create"]
    assert result["loss_usd"] is None and result["legs"] == []


# --- the quote history (quote_type_error) ----------------------------------------------------------

def test_a_quote_history_the_pair_check_cannot_read_is_quote_type_error(offline, monkeypatch):
    def unreadable(*args, **kwargs):
        raise TypeError("'<' not supported between instances of 'dict' and 'dict'")

    monkeypatch.setattr(paper_ledger.wash_detect, "pair_quotes", unreadable)
    state = paper_ledger.load_state("gap-market", "gap-yes")
    assert state["own_quotes"] is None  # never a short or empty history
    assert "quote_type_error" in state["quote_problems"]
    assert state["market_pair"] is None


# --- a damaged settlement pass halts the commit (settlement_problems) ------------------------------

def test_settlement_problems_halt_the_commit_and_book_nothing(offline, monkeypatch):
    problems = ["settlement_not_closing:fill-1", "invalid_settlement:fill-2"]
    monkeypatch.setattr(paper_ledger, "apply_settlements", lambda *a, **k: {"problems": problems})
    result = paper_ledger._commit_locked(_intent(), load_risk())
    assert result["allow"] is False and result["halted"] is True
    assert result["halt_reason"] == "invalid_risk_state"
    assert result["ledger_problems"] == problems
    assert result["decision"]["decision"] == "halt" and result["decision"]["risk_decision"] == "veto"
    assert result["decision"]["settlement_problems"] == problems
    halt = offline["halts"][-1]
    assert halt["reason"] == "invalid_risk_state" and halt["scope"] == "global"
    assert halt["extra"]["ledger_problems"] == problems
    assert not (offline["dir"] / "paper.jsonl").exists()  # nothing was booked
