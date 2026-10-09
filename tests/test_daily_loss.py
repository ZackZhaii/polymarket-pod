"""Task AR (backlog #4): max_daily_loss counts today's realized losses as well as today's fees.

Synthetic rows only. daily_loss() is pure; the load_state / commit_if_allowed tests use a temp
ledger, a temp halt state and a stubbed bus, never the real logs/.
"""
from __future__ import annotations

import math
from pathlib import Path

import pytest

from src import halt_store, ledger_store, paper_ledger, podlib
from src.daily_loss import DailyLoss, counts_today, daily_loss
from src.fees import booked_fee_usd, ledger_fee
from src.podlib import load_risk
from src.thresholds import PAPER_EQUITY_USD

TODAY = "2026-09-01"
NOW = "2026-09-01T12:00:00Z"
YESTERDAY = "2026-08-31T20:00:00Z"


def fill(side="buy", shares=100.0, price=0.5, ts=NOW, market="m", token="t", rate=0.0, **extra):
    notional = round(shares * price, 8)
    row = dict(ts=ts, market=market, token_id=token, side=side, shares=shares, notional_usd=notional,
               fee_rate=rate, market_price=price, effective_price=price,
               fee_usd=booked_fee_usd(ledger_fee(rate, price, price, notional, shares)),
               hypothetical=False, counts_toward_risk=True, risk_decision="allow")
    row.update(extra)
    return row


def run(rows, today=TODAY) -> DailyLoss:
    return daily_loss(rows, today)


# --- realized P&L ---------------------------------------------------------------------------------

def test_long_closed_at_a_loss_today():
    day = run([fill("buy", 100, 0.6, YESTERDAY), fill("sell", 100, 0.29)])
    assert day.realized_loss_usd == pytest.approx(31.0)
    assert (day.realized_gain_usd, day.fees_usd, day.closing_fills_today, day.problems) == (0.0, 0.0, 1, [])
    assert day.total_usd == pytest.approx(31.0)


def test_partial_closes_use_the_average_cost():
    rows = [fill("buy", 100, 0.6, YESTERDAY), fill("buy", 100, 0.4, YESTERDAY),  # avg 0.50
            fill("sell", 50, 0.3), fill("sell", 50, 0.7)]
    day = run(rows)
    assert day.realized_loss_usd == pytest.approx(10.0)
    assert day.realized_gain_usd == pytest.approx(10.0)
    assert day.total_usd == pytest.approx(10.0)  # the gain does not offset


def test_gains_never_offset_losses_or_fees():
    rows = [fill("buy", 100, 0.6, YESTERDAY, market="a"), fill("sell", 100, 0.29, market="a"),
            fill("buy", 100, 0.2, YESTERDAY, market="b"), fill("sell", 100, 0.6, market="b"),
            fill("buy", 20, 0.5, rate=0.02, market="c")]
    day = run(rows)
    assert day.realized_gain_usd == pytest.approx(40.0)
    assert day.fees_usd == pytest.approx(0.2)
    assert day.total_usd == pytest.approx(31.2)
    only_gain = run([fill("buy", 100, 0.2, YESTERDAY), fill("sell", 100, 0.6), fill("buy", 20, 0.5, rate=0.02,
                                                                                    market="c")])
    assert only_gain.total_usd == pytest.approx(0.2)  # never below the fees the old rule counted


def test_short_closed_by_a_buy_and_a_flip():
    rows = [fill("sell", 100, 0.5, YESTERDAY),          # short 100, received 50
            fill("buy", 150, 0.7)]                       # closes 100 at 70 (loss 20), opens long 50 at 0.7
    day = run(rows)
    assert day.realized_loss_usd == pytest.approx(20.0)
    day = run(rows + [fill("sell", 50, 0.6)])            # the flipped long closes at a 5 loss
    assert day.realized_loss_usd == pytest.approx(25.0)
    assert day.closing_fills_today == 2


def test_legs_are_separate_by_token():
    rows = [fill("buy", 100, 0.6, YESTERDAY, token="yes"), fill("sell", 100, 0.3, token="no")]
    day = run(rows)  # the sell opens a short on another token: nothing closed
    assert (day.realized_loss_usd, day.closing_fills_today) == (0.0, 0)


def test_opening_fills_realize_nothing():
    assert run([fill("buy", 100, 0.9), fill("buy", 10, 0.1)]).total_usd == 0.0


def test_hypothetical_and_non_counting_rows_are_ignored():
    rows = [fill("buy", 100, 0.6, YESTERDAY), fill("sell", 100, 0.1, hypothetical=True),
            fill("sell", 100, 0.1, counts_toward_risk=False)]
    day = run(rows)
    assert (day.total_usd, day.closing_fills_today) == (0.0, 0)


def test_missing_notional_is_valued_at_the_worst_price():
    buy = fill("buy", 100, 0.6, YESTERDAY)
    buy.pop("notional_usd")                              # valued at 1 USD/share: cost 100
    sell = fill("sell", 100, 0.6)
    assert run([buy, sell]).realized_loss_usd == pytest.approx(40.0)
    sell.pop("notional_usd")                             # valued at 0: proceeds 0
    assert run([fill("buy", 100, 0.6, YESTERDAY), sell]).realized_loss_usd == pytest.approx(60.0)


def test_numeric_strings_are_read_as_the_ledger_reads_them():
    buy = fill("buy", 100, 0.6, YESTERDAY)
    buy.update(shares="100", notional_usd="60")
    rows = [buy, fill("sell", 100, 0.29, notional_usd="29")]
    assert run(rows).realized_loss_usd == pytest.approx(31.0)


# --- day boundary (UTC) ---------------------------------------------------------------------------

@pytest.mark.parametrize("ts,counted", [
    ("2026-09-01T00:00:00Z", True),
    ("2026-08-31T23:59:59Z", False),
    ("2026-09-01T23:59:59Z", True),
    ("2026-09-02T01:00:00+05:00", True),   # 2026-09-01T20:00Z
    ("2026-09-01T23:00:00-05:00", True),   # starts with today (the old rule) and is 2026-09-02Z
    ("2026-09-01T02:00:00+05:00", True),   # starts with today: kept, even though it is 08-31 21:00Z
    ("2026-09-03T00:00:00Z", True),        # a future-dated row counts toward today
])
def test_day_boundary(ts, counted):
    assert counts_today(ts, TODAY) is counted
    day = run([fill("buy", 100, 0.6, "2026-08-30T00:00:00Z"), fill("sell", 100, 0.29, ts)])
    assert day.realized_loss_usd == (pytest.approx(31.0) if counted else 0.0)


def test_loss_belongs_to_the_day_of_the_close_not_the_open():
    rows = [fill("buy", 100, 0.6, "2026-08-01T00:00:00Z"), fill("sell", 100, 0.29, "2026-09-01T00:00:00Z")]
    assert run(rows).realized_loss_usd == pytest.approx(31.0)
    assert run(rows, today="2026-09-02").realized_loss_usd == 0.0


def test_fees_are_still_counted_by_the_old_rule():
    rows = [fill("buy", 20, 0.5, rate=0.02), fill("buy", 20, 0.5, YESTERDAY, rate=0.02, market="y")]
    assert run(rows).fees_usd == pytest.approx(0.2)


# --- fail closed ----------------------------------------------------------------------------------

@pytest.mark.parametrize("field,value", [
    ("ts", None), ("ts", "not-a-timestamp"), ("ts", "2026-09-01"), ("ts", "2026-09-01T10:00:00"),
    ("side", "short"), ("side", None),
    ("shares", -1.0), ("shares", "abc"), ("shares", math.nan),
    ("notional_usd", -5.0), ("notional_usd", math.inf), ("notional_usd", "x"),
    ("fee_usd", -0.1), ("fee_usd", math.nan), ("fee_usd", "fee"),
])
def test_corrupt_counted_rows_fail_closed(field, value):
    row = fill("buy", 10, 0.5)
    row[field] = value
    day = daily_loss([fill("buy", 10, 0.5, YESTERDAY), row], TODAY, ids=["r1", "r2"])
    assert day.problems == [f"daily_loss_invalid_{field}:r2"]
    assert day.total_usd is None


def test_corrupt_rows_that_do_not_count_are_ignored_as_before():
    assert run([fill("buy", 10, 0.5, ts="garbage", hypothetical=True)]).problems == []


def test_an_invalid_today_fails_closed():
    assert daily_loss([], "2026-13-40").problems == ["invalid_today"]
    assert daily_loss([], "").total_usd is None


# --- load_state / RiskGate / commit_if_allowed -----------------------------------------------------

@pytest.fixture
def ledger(tmp_path, monkeypatch):
    path = tmp_path / "paper.jsonl"
    bus: list[tuple] = []
    monkeypatch.setattr(paper_ledger, "PAPER_PATH", path)
    monkeypatch.setattr(paper_ledger, "QUOTES_PATH", tmp_path / "quotes.jsonl")
    monkeypatch.setattr(paper_ledger, "bus", lambda *args, **kwargs: bus.append(args))
    monkeypatch.setattr(paper_ledger, "utc_now", lambda: NOW)
    monkeypatch.setattr(halt_store, "HALT_STATE_PATH", tmp_path / "halt_state.json")
    monkeypatch.setattr(halt_store, "HALT_FALLBACK_PATH", tmp_path / "HALT")
    return path


def _book(path: Path, rows):
    for index, row in enumerate(rows):
        ledger_store.book(path, {"fill_id": f"ar-{index:04d}", **row})


def test_load_state_reports_the_parts(ledger):
    _book(ledger, [fill("buy", 100, 0.6, YESTERDAY, market="a"), fill("sell", 100, 0.29, market="a"),
                   fill("buy", 20, 0.5, rate=0.02, market="c")])
    state = paper_ledger.load_state("x", "t")
    assert state["daily_loss_usd"] == pytest.approx(31.2)
    assert state["daily_fees_usd"] == pytest.approx(0.2)
    assert state["daily_realized_loss_usd"] == pytest.approx(31.0)
    assert state["daily_realized_gain_usd"] == 0.0
    assert state["ledger_problems"] == []


def test_realized_loss_today_vetoes_max_daily_loss(ledger):
    cap_usd = load_risk()["max_daily_loss_pct"] * PAPER_EQUITY_USD
    _book(ledger, [fill("buy", 100, 0.6, YESTERDAY, market="a"), fill("sell", 100, 0.6 - (cap_usd + 1) / 100,
                                                                       market="a")])
    state = paper_ledger.load_state("x", "t")
    assert state["daily_loss_usd"] >= cap_usd
    decision = paper_ledger.evaluate(_intent(), load_risk(), state)
    assert decision.allow is False and "max_daily_loss" in decision.reasons


def test_the_same_loss_closed_yesterday_does_not_count(ledger):
    _book(ledger, [fill("buy", 100, 0.6, "2026-08-31T00:00:00Z", market="a"),
                   fill("sell", 100, 0.2, "2026-08-31T23:59:59Z", market="a")])
    state = paper_ledger.load_state("x", "t")
    assert state["daily_loss_usd"] == 0.0
    assert "max_daily_loss" not in paper_ledger.evaluate(_intent(), load_risk(), state).reasons


def test_corrupt_ts_is_invalid_risk_state_and_a_global_halt(ledger):
    _book(ledger, [fill("buy", 10, 0.5, ts="not-a-timestamp", market="a")])
    before = ledger.read_bytes()
    state = paper_ledger.load_state("x", "t")
    assert state["daily_loss_usd"] is None and state["open_positions"] is None
    assert state["ledger_problems"] == ["daily_loss_invalid_ts:ar-0000"]
    result = paper_ledger.commit_if_allowed(_intent(), load_risk())
    assert result["allow"] is False and result["halted"] is True
    assert result["halt_reason"] == "invalid_risk_state"
    assert result["ledger_problems"] == ["daily_loss_invalid_ts:ar-0000"]
    assert any(h["scope"] == "global" for h in halt_store.active_halts())
    assert ledger.read_bytes() == before


def _intent():
    return paper_ledger._intent(market="ar", token_id="t", model_prob=0.7, market_price=0.4, fee_rate=0.02,
                                hypothetical=False, book_ts=podlib.utc_now())  # fresh on RiskGate's clock


def test_the_cap_value_is_unchanged():
    assert load_risk()["max_daily_loss_pct"] == 0.03
