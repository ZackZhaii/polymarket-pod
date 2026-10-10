"""Task AR (backlog #4): max_daily_loss counts today's realized losses as well as today's fees.

Synthetic rows only. daily_loss() is pure; the load_state / commit_if_allowed tests use a temp
ledger, a temp halt state and a stubbed bus, never the real logs/.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
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


# --- boundary values and fail-closed shapes ---------------------------------------------------------

@pytest.mark.parametrize("ts,counted", [
    (datetime(2026, 9, 1, 12, tzinfo=timezone.utc), True),        # aware datetime: parsed, not a string
    (datetime(2026, 8, 31, 12, tzinfo=timezone.utc), False),
    (datetime(2026, 9, 1, 12, tzinfo=timezone.utc).timestamp(), True),   # epoch seconds, no date prefix
    (datetime(2026, 8, 31, 12, tzinfo=timezone.utc).timestamp(), False),
    (datetime(2026, 9, 1, 12), None),                             # naive: rejected, never guessed as UTC
    (True, None),                                                 # bool is not an epoch (parse_book_ts refuses)
    (math.nan, None), (math.inf, None),
    (" 2026-09-01T12:00:00Z ", True),                             # surrounding space is stripped, not fatal
])
def test_usable_non_string_timestamps_and_the_ones_that_are_not(ts, counted):
    assert counts_today(ts, TODAY) is counted


def test_an_epoch_timestamp_books_the_close_on_its_own_utc_day():
    rows = [fill("buy", 100, 0.6, "2026-08-30T00:00:00Z"),
            fill("sell", 100, 0.29, datetime(2026, 9, 1, 12, tzinfo=timezone.utc).timestamp())]
    day = run(rows)
    assert (day.realized_loss_usd, day.closing_fills_today, day.problems) == (pytest.approx(31.0), 1, [])


def test_a_row_bad_in_several_fields_names_them_all_and_injects_nothing():
    good_buy = fill("buy", 20, 0.5, rate=0.02)
    corrupt = fill("buy", 10, 0.5)
    corrupt.update(side="hold", shares=math.nan, fee_usd=-1.0, notional_usd=math.inf, ts=None)
    good_sell = fill("sell", 20, 0.3)
    day = daily_loss([good_buy, corrupt, good_sell], TODAY, ids=["r1", "r2", "r3"])
    assert day.problems == [f"daily_loss_invalid_{field}:r2"
                            for field in ("side", "shares", "fee_usd", "notional_usd", "ts")]
    assert day.total_usd is None
    # the corrupt row is skipped whole: it neither pays its -1 fee, opens its leg, nor blocks row 3
    assert day.fees_usd == pytest.approx(0.2)
    assert day.realized_loss_usd == pytest.approx(4.0)  # 20 shares bought at 0.5, sold at 0.3
    assert day.closing_fills_today == 1


def test_a_fill_without_shares_or_with_zero_shares_is_valid_and_opens_nothing():
    missing = fill("buy", 10, 0.5)
    missing.pop("shares")
    missing["fee_usd"] = 0.05
    zero = fill("buy", 0.0, 0.5)
    day = daily_loss([missing, zero, fill("sell", 100, 0.4), fill("buy", 100, 0.5)], TODAY)
    assert day.problems == []
    assert day.fees_usd == pytest.approx(0.05)  # a fee still counts, the absent position does not
    assert day.closing_fills_today == 1         # only the short opened by the sell and closed by the buy
    assert day.realized_loss_usd == pytest.approx(10.0)  # short 100 at 0.4, covered at 0.5


def test_a_null_notional_is_the_legacy_row_not_a_corrupt_one():
    buy = fill("buy", 100, 0.6, YESTERDAY)
    buy["notional_usd"] = None  # JSON null, the same meaning the ledger gives a missing key
    day = run([buy, fill("sell", 100, 0.6)])
    assert day.problems == []
    assert day.realized_loss_usd == pytest.approx(40.0)  # the buy is valued at the worst price, 1.0


def test_a_close_within_the_open_epsilon_leaves_the_leg_flat():
    rows = [fill("buy", 100, 0.6, YESTERDAY),
            fill("sell", 100.0000000001, 0.7),   # rest 1e-10 <= LEDGER_OPEN_EPS: no dust position left
            fill("buy", 100, 0.5)]                # opens a new long, it does not close a 1e-10 short
    day = run(rows)
    assert (day.realized_loss_usd, day.closing_fills_today, day.problems) == (0.0, 1, [])
    assert day.realized_gain_usd == pytest.approx(10.0)
    assert (day.fees_usd, day.total_usd) == (0.0, 0.0)


def test_a_position_below_the_open_epsilon_is_flat_and_never_closes_anything():
    rows = [fill("buy", 1e-10, 1.0, YESTERDAY),   # dust: held is reset to flat before the next fill is booked
            fill("sell", 100, 0.5),               # so this opens a short instead of closing 1e-10 shares
            fill("buy", 100, 0.6)]                # and only this buy-back closes (10 USD loss)
    day = run(rows)
    assert (day.realized_loss_usd, day.closing_fills_today, day.problems) == (pytest.approx(10.0), 1, [])


def test_a_flip_opens_the_remainder_at_the_fill_price():
    rows = [fill("buy", 100, 0.6, YESTERDAY),
            fill("sell", 150, 0.7),    # closes 100 (gain 10), opens a short 50 whose basis is 35
            fill("buy", 50, 0.9)]      # covering that short at 0.9 loses 10
    day = run(rows)
    assert (day.realized_loss_usd, day.realized_gain_usd) == (pytest.approx(10.0), pytest.approx(10.0))
    assert day.total_usd == pytest.approx(10.0)
    assert day.closing_fills_today == 2


def test_an_unknown_leg_never_nets_with_a_named_token():
    sell = fill("sell", 100, 0.29)
    sell.pop("token_id")
    day = run([fill("buy", 100, 0.6, YESTERDAY, token="t"), sell])
    assert day.problems == []
    assert (day.realized_loss_usd, day.closing_fills_today) == (0.0, 0)


@pytest.mark.parametrize("today", ["2026-9-01", "2026-09-1", "20260901", "2026-09-01 ", "not-a-day", None, 20260901,
                                   "2026-02-30"])  # ten characters, but not a day that exists
def test_only_a_ten_character_utc_date_is_a_usable_today(today):
    assert daily_loss([fill("buy", 10, 0.5)], today).problems == ["invalid_today"]


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
