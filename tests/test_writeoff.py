"""Issue #197 (Task CK, KEY-approved "close-then-write-off"): a confirmed closed market's legs are written off once.

家伟 approved the rule on 2026-10-11 09:47 Beijing (issue #197, comment 6104331631). Synthetic Gamma rows and ledger
rows; every file lives in the test's folder. Paper only: no network, no order.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from src import daily_loss, halt_store, ledger_store, m2_runner, market_close, paper_ledger, reconcile
from src.fees import booked_fee_usd, ledger_fee
from src.podlib import load_risk
from src.risk_gate import Intent
from src.thresholds import MARKET_CLOSE_RECHECK_S, PAPER_EQUITY_USD

DAY1 = "2026-09-01T12:00:00Z"
DAY2 = "2026-09-02T12:00:00Z"
DAY3 = "2026-09-03T12:00:00Z"
YESTERDAY = "2026-08-31T20:00:00Z"
CLOSED_TS = "2026-09-01 08:00:00+00"
T0 = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
CAP_USD = load_risk()["max_daily_loss_pct"] * PAPER_EQUITY_USD


def fill(side="buy", shares=100.0, price=0.5, ts=YESTERDAY, market="m", token="yes"):
    notional = round(shares * price, 8)
    return dict(ts=ts, market=market, token_id=token, side=side, shares=shares, notional_usd=notional,
                fee_rate=0.0, market_price=price, effective_price=price,
                fee_usd=booked_fee_usd(ledger_fee(0.0, price, price, notional, shares)),
                hypothetical=False, counts_toward_risk=True, risk_decision="allow")


def settle(market, token, shares, payout, ts, side="sell"):
    return dict(ts=ts, kind="settlement", strategy="settlement", market=market, token_id=token, side=side,
                shares=shares, market_price=payout, effective_price=payout, notional_usd=shares * payout,
                fee_rate=0.0, fee_usd=0.0, hypothetical=False, counts_toward_risk=True, risk_decision="settlement",
                condition_id=f"0x{market}", winning_token_id=token if payout == 1.0 else "other",
                closed_ts="2026-09-01T08:00:00Z", settlement_payout=payout)


def gamma(market="m", closed=True, accepting=False, closed_time=CLOSED_TS, **extra):
    row = {"id": market, "closed": closed, "acceptingOrders": accepting, "closedTime": closed_time}
    row.update(extra)
    return row


def close(market="m", closed_time=CLOSED_TS):
    when = market_close.parse_end_date(closed_time)
    return market_close.Close(market, when, T0, T0 + timedelta(seconds=60), market_close.BY_REREAD)


@pytest.fixture
def env(tmp_path, monkeypatch):
    clock = {"now": DAY1}
    paths = {"ledger": tmp_path / "paper.jsonl"}
    monkeypatch.setattr(paper_ledger, "PAPER_PATH", paths["ledger"])
    monkeypatch.setattr(paper_ledger, "QUOTES_PATH", tmp_path / "quotes.jsonl")
    monkeypatch.setattr(paper_ledger, "MARK_BOOKS_PATH", tmp_path / "books.jsonl")
    monkeypatch.setattr(paper_ledger, "DAILY_MARKS_DIR", tmp_path / "daily_marks")
    monkeypatch.setattr(paper_ledger, "SETTLEMENT_RESOLUTIONS_PATH", tmp_path / "resolutions.jsonl")
    monkeypatch.setattr(paper_ledger, "bus", lambda *args, **kwargs: None)
    monkeypatch.setattr(paper_ledger, "utc_now", lambda: clock["now"])
    monkeypatch.setattr(halt_store, "HALT_STATE_PATH", tmp_path / "halt_state.json")
    monkeypatch.setattr(halt_store, "HALT_FALLBACK_PATH", tmp_path / "HALT")
    return {**paths, "clock": clock}


def _book(path: Path, rows, prefix="ck"):
    for index, row in enumerate(rows):
        ledger_store.book(path, {"fill_id": f"{prefix}-{index:04d}", **row})


def _rows(path: Path) -> list[dict]:
    return ledger_store.read_ledger(path)[1].rows


def _intent(market="x", token="t"):
    return Intent(strategy="test", market=market, side="buy", model_prob=0.6, market_price=0.4,
                  assumed_slippage=0.005, fee_rate=0.0, liquidity_usd=50_000.0, spread=0.01, token_id=token,
                  book_ts=DAY1)


# --- required regression ------------------------------------------------------------------------------

def test_three_legs_awaiting_settlement_count_once_on_the_close_day_and_never_trip_the_breaker_later(env):
    """Required (#197): 3 legs closed at once are written off once on the close day; later days count 0."""
    _book(env["ledger"], [fill("buy", 24.0, 0.5, market=f"m{i}", token=f"t{i}") for i in range(3)])  # 12 USD each
    out = paper_ledger.apply_writeoffs([close(f"m{i}") for i in range(3)])
    assert out["problems"] == [] and len(out["booked"]) == 3
    assert [row["writeoff_usd"] for row in out["booked"]] == [12.0, 12.0, 12.0]
    day1 = paper_ledger.load_state("x", "t")
    assert day1["daily_writeoff_usd"] == pytest.approx(36.0)
    assert day1["daily_realized_loss_usd"] == pytest.approx(36.0)
    assert day1["daily_unrealized_loss_usd"] == 0.0  # written off: not also an unrealized loss
    assert day1["daily_loss_usd"] == pytest.approx(36.0)  # once, not 72
    assert {leg["ref_source"] for leg in day1["daily_marks"]} == {"written_off"}
    # the legs still hold every limit until they settle
    assert day1["open_positions"] == 3 and day1["exposure_usd"] == pytest.approx(36.0)
    for now in (DAY2, DAY3):
        env["clock"]["now"] = now
        assert paper_ledger.apply_writeoffs([close(f"m{i}") for i in range(3)])["booked"] == []  # restart-safe
        later = paper_ledger.load_state("x", "t")
        assert later["daily_loss_usd"] == 0.0 and later["daily_unrealized_loss_usd"] == 0.0
        assert later["open_positions"] == 3
        assert "max_daily_loss" not in paper_ledger.evaluate(_intent(), load_risk(), later).reasons
    assert sum(1 for row in _rows(env["ledger"]) if ledger_store.is_writeoff(row)) == 3


def test_without_the_writeoff_the_closed_legs_counted_again_every_day(env):
    """The bug (#197): a closed, unsettled leg with no book is valued at 0 against its cost every day."""
    _book(env["ledger"], [fill("buy", 24.0, 0.5, market=f"m{i}", token=f"t{i}") for i in range(3)])
    for now in (DAY1, DAY2, DAY3):
        env["clock"]["now"] = now
        assert paper_ledger.load_state("x", "t")["daily_loss_usd"] == pytest.approx(36.0)


def test_settlement_after_writeoff_loser_books_nothing_winner_gain_never_offsets_and_releases_capacity(env):
    _book(env["ledger"], [fill("buy", 20.0, 0.5, market="lose", token="a"),
                          fill("buy", 20.0, 0.5, market="win", token="b")])
    paper_ledger.apply_writeoffs([close("lose"), close("win")])
    assert paper_ledger.load_state("x", "t")["daily_loss_usd"] == pytest.approx(20.0)
    env["clock"]["now"] = DAY2
    _book(env["ledger"], [settle("lose", "a", 20.0, 0.0, DAY2), settle("win", "b", 20.0, 1.0, DAY2)], prefix="st")
    state = paper_ledger.load_state("x", "t")
    assert state["daily_realized_loss_usd"] == 0.0  # the loser's 10 USD was written off on day 1
    # INV-3 (Poly预测 v3): the winner's whole payout is its gain, measured from the write-off; reported, never netted
    assert state["daily_realized_gain_usd"] == pytest.approx(20.0)
    assert state["daily_loss_usd"] == 0.0
    assert state["open_positions"] == 0 and state["exposure_usd"] == 0.0  # capacity released at settlement
    day = daily_loss.daily_loss(_rows(env["ledger"]), "2026-09-02")
    assert day.writeoff_settled_loss_usd == pytest.approx(10.0) and day.problems == []


def test_settlement_on_the_close_day_counts_the_loss_once(env):
    _book(env["ledger"], [fill("buy", 20.0, 0.5, market="lose", token="a")])
    paper_ledger.apply_writeoffs([close("lose")])
    _book(env["ledger"], [settle("lose", "a", 20.0, 0.0, DAY1)], prefix="st")
    assert paper_ledger.load_state("x", "t")["daily_loss_usd"] == pytest.approx(10.0)


def test_a_short_leg_is_written_off_at_its_buy_back_cost(env):
    _book(env["ledger"], [fill("sell", 10.0, 0.3, market="s", token="t")])  # proceeds 3, buy-back worst 10
    out = paper_ledger.apply_writeoffs([close("s")])
    assert out["booked"][0]["writeoff_usd"] == pytest.approx(7.0) and out["booked"][0]["side"] == "buy"
    assert paper_ledger.load_state("x", "t")["daily_loss_usd"] == pytest.approx(7.0)


# --- the ledger row -------------------------------------------------------------------------------------

def test_the_writeoff_row_moves_no_cash_or_shares_and_verifies(env):
    _book(env["ledger"], [fill("buy", 20.0, 0.5, market="m", token="t")])
    before = _rows(env["ledger"])[-1]
    row = paper_ledger.apply_writeoffs([close("m")])["booked"][0]
    assert (row["kind"], row["risk_decision"], row["shares"], row["notional_usd"]) == ("writeoff", "writeoff", 0.0, 0.0)
    assert row["fill_id"] == market_close.writeoff_fill_id("m", "t")  # the key is the leg (Poly风控 #197)
    assert row["close_first_seen_ts"] and row["closed_ts"] == "2026-09-01T08:00:00+00:00"
    for name in (ledger_store.CASH_FIELD, ledger_store.LEG_SHARES_FIELD, ledger_store.LEG_COST_FIELD):
        assert row[name] == before[name]
    assert ledger_store.verify(ledger_store.read_ledger(env["ledger"])[1]) == []
    view = reconcile.ledger_view(env["ledger"])  # not a trade: no Data API row expected, no shares problem
    assert view.problems == [] and view.flows == {"t": {"BUY": 20.0, "SELL": 0.0}}


@pytest.mark.parametrize("change", [
    {"shares": 1.0}, {"notional_usd": 0.5}, {"fee_usd": 0.01}, {"writeoff_usd": -1.0}, {"writeoff_usd": "10"},
    {"writeoff_shares": 0.0}, {"closed_ts": "2026-09-01"}, {"closed_ts": None}, {"risk_decision": "allow"},
    {"hypothetical": True, "counts_toward_risk": False}, {"kind": "write_off"},
])
def test_a_damaged_writeoff_row_is_refused(env, change):
    _book(env["ledger"], [fill("buy", 20.0, 0.5, market="m", token="t")])
    row = market_close.plan_writeoffs(_rows(env["ledger"]), ["ck-0000"], [close("m")], DAY1).rows[0]
    row.update(change)
    with pytest.raises(ledger_store.LedgerIntegrityError):
        ledger_store.book(env["ledger"], row)


def test_a_writeoff_on_a_flat_leg_or_twice_or_with_other_shares_is_refused(env):
    _book(env["ledger"], [fill("buy", 20.0, 0.5, market="m", token="t")])
    row = market_close.plan_writeoffs(_rows(env["ledger"]), ["ck-0000"], [close("m")], DAY1).rows[0]
    with pytest.raises(ledger_store.LedgerIntegrityError):
        ledger_store.book(env["ledger"], {**row, "writeoff_shares": 19.0})
    with pytest.raises(ledger_store.LedgerIntegrityError):
        ledger_store.book(env["ledger"], {**row, "market": "other"})  # no open leg there
    with pytest.raises(ledger_store.LedgerIntegrityError):
        ledger_store.book(env["ledger"], {**row, "side": "buy"})  # not the closing side
    ledger_store.book(env["ledger"], row)
    with pytest.raises(ledger_store.LedgerIntegrityError):
        ledger_store.book(env["ledger"], {**row, "fill_id": "writeoff-again"})


def test_a_writeoff_amount_that_is_not_the_full_cost_is_invalid_risk_state(env):
    _book(env["ledger"], [fill("buy", 20.0, 0.5, market="m", token="t")])
    row = market_close.plan_writeoffs(_rows(env["ledger"]), ["ck-0000"], [close("m")], DAY1).rows[0]
    ledger_store.book(env["ledger"], {**row, "writeoff_usd": 4.0})  # well formed, but not the cost (10)
    state = paper_ledger.load_state("x", "t")
    assert state["daily_loss_usd"] is None
    assert any(p.startswith("daily_loss_invalid_writeoff") for p in state["daily_loss_problems"])
    decision = paper_ledger.evaluate(_intent(), load_risk(), state)
    assert "invalid_risk_state" in decision.reasons


def test_a_fill_that_grows_a_written_off_leg_is_invalid(env):
    _book(env["ledger"], [fill("buy", 20.0, 0.5, market="m", token="t")])
    paper_ledger.apply_writeoffs([close("m")])
    _book(env["ledger"], [fill("buy", 5.0, 0.5, ts=DAY1, market="m", token="t")], prefix="grow")
    day = daily_loss.daily_loss(_rows(env["ledger"]), "2026-09-01")
    assert any(p.startswith("daily_loss_writeoff_leg_grew") for p in day.problems)


def test_writeoff_is_idempotent_and_restart_safe(env):
    _book(env["ledger"], [fill("buy", 20.0, 0.5, market="m", token="t")])
    assert len(paper_ledger.apply_writeoffs([close("m")])["booked"]) == 1
    again = paper_ledger.apply_writeoffs([close("m"), close("m")])
    assert again == {"booked": [], "problems": []}
    assert sum(1 for row in _rows(env["ledger"]) if ledger_store.is_writeoff(row)) == 1


def test_a_second_close_time_for_a_written_off_leg_writes_nothing_and_is_not_a_halt(env):
    """Poly风控 (#197 ruling): another closedTime after the write-off is ignored; the leg is never written off twice."""
    _book(env["ledger"], [fill("buy", 20.0, 0.5, market="m", token="t")])
    paper_ledger.apply_writeoffs([close("m")])
    assert paper_ledger.apply_writeoffs([close("m", "2026-09-01T09:00:00Z")]) == {"booked": [], "problems": []}
    rows = _rows(env["ledger"])
    obs = _obs(gamma(closed_time="2026-09-01T09:00:00Z"), T0)
    step = market_close.detect(market_close.CloseTracker(), rows, {"m": obs}, T0)
    assert step.halts == [] and step.reasons == {"m": "already_written_off_closed_time_changed"}


def test_two_different_writeoff_rows_for_one_leg_key_are_invalid_risk_state(env):
    _book(env["ledger"], [fill("buy", 20.0, 0.5, market="m", token="t")])
    row = paper_ledger.apply_writeoffs([close("m")])["booked"][0]
    with env["ledger"].open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({**row, "writeoff_usd": 9.0}) + "\n")
    problems = ledger_store.verify(ledger_store.read_ledger(env["ledger"])[1])
    assert any("conflicting_fill_id" in problem for problem in problems)


def test_an_identical_writeoff_row_written_twice_counts_once(env):
    _book(env["ledger"], [fill("buy", 20.0, 0.5, market="m", token="t")])
    row = paper_ledger.apply_writeoffs([close("m")])["booked"][0]
    with env["ledger"].open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")
    _, read = ledger_store.read_ledger(env["ledger"])
    assert ledger_store.verify(read) == []
    assert sum(1 for r in read.rows if ledger_store.is_writeoff(r)) == 1


def test_an_unverifiable_ledger_writes_nothing_off(env):
    _book(env["ledger"], [fill("buy", 20.0, 0.5, market="m", token="t")])
    with env["ledger"].open("a", encoding="utf-8") as handle:
        handle.write("{not json\n")
    out = paper_ledger.apply_writeoffs([close("m")])
    assert out["booked"] == [] and out["problems"]


def test_a_writeoff_for_a_market_not_held_books_nothing(env):
    _book(env["ledger"], [fill("buy", 20.0, 0.5, market="m", token="t")])
    assert paper_ledger.apply_writeoffs([close("elsewhere")]) == {"booked": [], "problems": []}


# --- the trigger -------------------------------------------------------------------------------------

def _obs(row, at=T0):
    return market_close.observation_from_row(row["id"], row, at)


@pytest.mark.parametrize("row,ok", [
    (gamma(), True),
    (gamma(closed_time="2026-09-01T08:00:00.123456Z"), True),
    (gamma(closed="true"), False),               # a string is not a boolean
    (gamma(closed=None), False),
    (gamma(accepting=True), False),
    (gamma(accepting=None), False),               # unknown is not "not accepting"
    (gamma(closed_time=None), False),
    (gamma(closed_time="2026-09-01"), False),     # no time, no zone
    (gamma(closed_time="2026-09-01T08:00:00"), False),  # no zone
    (gamma(closed_time="2026-09-02T08:00:00Z"), False),  # in the future
    # never on its own: endDate past, active false, UMA resolved, while closed is false
    (gamma(closed=False, accepting=False, endDate="2026-08-01T00:00:00Z", active=False,
           umaResolutionStatus="resolved"), False),
])
def test_the_trigger_needs_closed_true_not_accepting_and_a_past_close_time(row, ok):
    assert market_close.qualifies(_obs(row), T0) is ok


def test_one_read_never_confirms_a_reread_after_the_recheck_window_does():
    tracker = market_close.CloseTracker()
    assert tracker.update(_obs(gamma()), T0) is None
    soon = T0 + timedelta(seconds=MARKET_CLOSE_RECHECK_S - 1)
    assert tracker.update(_obs(gamma(), soon), soon) is None
    later = T0 + timedelta(seconds=MARKET_CLOSE_RECHECK_S)
    found = tracker.update(_obs(gamma(), later), later)
    assert found is not None and found.first_seen == T0 and found.confirmed_by == market_close.BY_REREAD
    assert found.closed_ts == "2026-09-01T08:00:00+00:00"


def test_a_read_that_does_not_qualify_drops_the_pending_read_and_a_new_close_time_does_not_restart():
    tracker = market_close.CloseTracker()
    assert tracker.step(_obs(gamma()), T0) == (None, "awaiting_confirmation")
    t1 = T0 + timedelta(seconds=40)
    assert tracker.step(_obs(gamma(accepting=True), t1), t1) == (None, "confirmation_failed:accepting_orders_not_false")
    t2 = T0 + timedelta(seconds=80)
    assert tracker.step(_obs(gamma(), t2), t2) == (None, "awaiting_confirmation")  # the first qualifying read again
    t3 = T0 + timedelta(seconds=120)
    other = gamma(closed_time="2026-09-01T08:05:00Z")
    assert tracker.step(_obs(other, t3), t3) == (None, "confirmation_failed_closed_time_changed")
    t4 = T0 + timedelta(seconds=160)
    assert tracker.step(_obs(other, t4), t4) == (None, "awaiting_confirmation")
    t5 = T0 + timedelta(seconds=200)
    assert tracker.step(_obs(other, t5), t5)[1] == "writeoff_confirmed_reread"


def test_a_failed_read_drops_the_pending_read_without_a_halt():
    tracker = market_close.CloseTracker()
    tracker.step(_obs(gamma()), T0)
    t1 = T0 + timedelta(seconds=40)
    assert tracker.step(market_close.failed("m", t1, "gamma_market_missing"), t1) == (
        None, "close_read_failed_pending_dropped")
    assert tracker.first == {}


@pytest.mark.parametrize("after_s,halts", [(60, False), (299, False), (300, True), (3600, True)])
def test_reading_open_after_the_writeoff_is_a_halt_only_after_the_gamma_lag_grace(env, after_s, halts):
    """Gamma by-id needs 33-309 s to show closed=true: an open read within MARKET_CLOSE_REOPEN_GRACE_S of the first
    closed read is lag, later it is the OQ-8 contradiction."""
    _book(env["ledger"], [fill("buy", 20.0, 0.5, market="m", token="t")])
    paper_ledger.apply_writeoffs([close("m")])
    first_seen = datetime.fromisoformat(_rows(env["ledger"])[-1]["close_first_seen_ts"])
    at = first_seen + timedelta(seconds=after_s)
    for row in (gamma(closed=False, accepting=True, closed_time=None), gamma(accepting=True)):
        step = market_close.detect(market_close.CloseTracker(), _rows(env["ledger"]), {"m": _obs(row, at)}, at)
        assert step.halts == (["closed_flag_contradiction_after_writeoff:m"] if halts else [])


@pytest.mark.parametrize("obs", [
    market_close.failed("m", T0, "gamma_read_failed:HTTPError"),
    market_close.failed("m", T0, "gamma_market_missing"),  # an empty [] is unknown
    market_close.Observation("m", T0, closed="true", accepting_orders=False),
    market_close.Observation("m", T0, closed=None, accepting_orders=None),
])
def test_a_written_off_market_ignores_failed_empty_and_non_boolean_reads(env, obs):
    _book(env["ledger"], [fill("buy", 20.0, 0.5, market="m", token="t")])
    paper_ledger.apply_writeoffs([close("m")])
    later = T0 + timedelta(hours=2)
    step = market_close.detect(market_close.CloseTracker(), _rows(env["ledger"]), {"m": obs}, later)
    assert step.halts == [] and step.closes == [] and step.reasons == {"m": "already_written_off_read_failed"}


def test_a_second_source_confirms_only_after_a_qualifying_gamma_read():
    tracker = market_close.CloseTracker()
    assert tracker.confirm_by_second_source("m", market_close.BY_CLOB, {"accepting_orders": False}, T0) is None
    tracker.update(_obs(gamma()), T0)
    assert tracker.confirm_by_second_source("m", market_close.BY_CLOB, {"accepting_orders": None}, T0) is None
    assert tracker.confirm_by_second_source("m", market_close.BY_CLOB, {"accepting_orders": False}, T0) is not None
    assert tracker.first == {}  # a confirmed close is no longer pending
    data = {"tokens": [{"token_id": "a", "closed": True}, {"token_id": "b", "closed": False}]}
    tracker.update(_obs(gamma()), T0)
    assert tracker.confirm_by_second_source("m", market_close.BY_DATA, data, T0, ["a", "b"]) is None
    assert tracker.confirm_by_second_source("m", market_close.BY_DATA, data, T0, ["a"]) is not None
    assert tracker.confirm_by_second_source("m", "ws_market_resolved", {"closed": True}, T0) is None


class Gamma:
    def __init__(self, default=None, closed=None, error=None):
        self.default, self.closed, self.error, self.urls = default, closed, error, []

    def __call__(self, url):
        self.urls.append(url)
        if self.error:
            raise self.error
        is_closed = parse_qs(urlsplit(url).query).get("closed") == ["true"]
        return (self.closed if is_closed else self.default) or []


def test_gamma_read_sees_a_close_only_through_closed_true_and_an_empty_answer_is_unknown():
    getter = Gamma(closed=[gamma()])
    obs = market_close.gamma_read(getter, "m", T0)
    assert market_close.qualifies(obs, T0)
    assert sorted(parse_qs(urlsplit(u).query).get("closed", [""])[0] for u in getter.urls) == ["", "true"]
    assert all(m2_runner.url_allowed(u) for u in getter.urls)
    assert all(u != m2_runner.gamma_market_url("m") for u in getter.urls if "closed=true" in u)
    assert market_close.gamma_read(Gamma(), "m", T0).problem == "gamma_market_missing"
    assert market_close.gamma_read(Gamma(error=TimeoutError()), "m", T0).problem == "gamma_read_failed:TimeoutError"
    assert market_close.gamma_read(Gamma(default={"x": 1}), "m", T0).problem == "gamma_bad_response"
    # Gamma's two views split around a close: unknown, not open and not closed
    split = market_close.gamma_read(Gamma(default=[gamma(closed=False, accepting=True)], closed=[gamma()]), "m", T0)
    assert split.problem == "gamma_views_disagree"
    # the default view alone never shows a close
    assert market_close.gamma_read(Gamma(default=[gamma()]), "m", T0).problem == "gamma_default_view_closed"
    open_now = market_close.gamma_read(Gamma(default=[gamma(closed=False, accepting=True)]), "m", T0)
    assert open_now.problem is None and open_now.closed is False


# --- the runner: active query of held legs, write-off once, halt on failure or contradiction ------------

RUN_NOW = datetime.now(timezone.utc).replace(microsecond=0)


class RunnerGamma:
    """Gamma for the runner: the keyset page (no candidates) and /markets?id= reads of the held market."""

    def __init__(self, row):
        self.row, self.urls, self.error = row, [], None

    def __call__(self, url):
        self.urls.append(url)
        parts = urlsplit(url)
        if parts.path.endswith("/keyset"):
            return {"markets": [], "next_cursor": ""}
        if self.error:
            raise self.error
        query = parse_qs(parts.query)
        want_closed = query.get("closed") == ["true"]
        return [self.row] if (self.row.get("closed") is True) == want_closed else []


@pytest.fixture
def runner(tmp_path, monkeypatch):
    paths = {"paper": tmp_path / "logs" / "paper.jsonl", "run": tmp_path / "runs" / "m2"}
    monkeypatch.setattr(paper_ledger, "PAPER_PATH", paths["paper"])
    monkeypatch.setattr(paper_ledger, "QUOTES_PATH", tmp_path / "logs" / "quotes.jsonl")
    monkeypatch.setattr(paper_ledger, "DAILY_MARKS_DIR", tmp_path / "daily_marks")
    monkeypatch.setattr(m2_runner.podlib, "BUS_PATH", tmp_path / "logs" / "bus.jsonl")
    paths["paper"].parent.mkdir(parents=True, exist_ok=True)
    paths["run"].mkdir(parents=True, exist_ok=True)
    stamp = (RUN_NOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _book(paths["paper"], [fill("buy", 20.0, 0.5, ts=stamp, market="held", token="t1")])
    state = m2_runner.RunnerState(paths=m2_runner.RunnerPaths.under(paths["run"]))
    closed_at = (RUN_NOW - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S+00")
    return {**paths, "state": state, "closed_at": closed_at}


def _iterate(runner, getter, now):
    runner["state"].monotonic = lambda: now.timestamp()  # wall and monotonic move together (#120: no "resume")
    return m2_runner.iteration(runner["state"], now=now, getters=m2_runner.Getters(gamma=getter, book=_no_book))


def _no_book(url):
    raise AssertionError(f"no book read expected: {url}")


def _writeoffs(path):
    return [row for row in _rows(path) if ledger_store.is_writeoff(row)]


def test_the_runner_reads_held_markets_and_writes_off_on_the_confirming_reread_once(runner):
    getter = RunnerGamma(gamma("held", closed_time=runner["closed_at"]))
    first = _iterate(runner, getter, RUN_NOW)
    assert "market_close_halt" not in first.reasons and _writeoffs(runner["paper"]) == []
    assert any("id=held" in url and "closed=true" in url for url in getter.urls)
    second = _iterate(runner, getter, RUN_NOW + timedelta(seconds=60))
    assert "market_close_halt" not in second.reasons
    booked = _writeoffs(runner["paper"])
    assert len(booked) == 1 and booked[0]["writeoff_usd"] == pytest.approx(10.0)
    # restart: a new tracker confirms again, but nothing is written off twice
    runner["state"] = m2_runner.RunnerState(paths=m2_runner.RunnerPaths.under(runner["run"]))
    for seconds in (120, 180):
        report = _iterate(runner, getter, RUN_NOW + timedelta(seconds=seconds))
        assert "market_close_halt" not in report.reasons
    assert len(_writeoffs(runner["paper"])) == 1
    events = [json.loads(line) for line in (runner["run"] / "events.jsonl").read_text().splitlines()]
    assert [e for e in events if e.get("kind") == "writeoff" or e.get("event") == "writeoff"]


def test_the_runner_does_not_write_off_an_open_market(runner):
    getter = RunnerGamma(gamma("held", closed=False, accepting=True, closed_time=None))
    for seconds in (0, 60, 120):
        assert "market_close_halt" not in _iterate(runner, getter, RUN_NOW + timedelta(seconds=seconds)).reasons
    assert _writeoffs(runner["paper"]) == []


def _halted():
    return halt_store.has_active_halt(scope=halt_store.SCOPE_GLOBAL, reason_prefix="invalid_risk_state") is True


def test_a_failed_close_read_of_a_held_market_is_not_a_halt_and_writes_nothing_off(runner):
    getter = RunnerGamma(gamma("held"))
    getter.error = TimeoutError()
    for seconds in (0, 60):
        _iterate(runner, getter, RUN_NOW + timedelta(seconds=seconds))
    assert not _halted() and _writeoffs(runner["paper"]) == []
    events = [json.loads(line) for line in (runner["run"] / "events.jsonl").read_text().splitlines()]
    assert any(e.get("event") == "close_read_skipped" for e in events)


def test_an_empty_answer_for_a_held_market_is_unknown_not_a_close(runner):
    getter = RunnerGamma({"id": "someone-else", "closed": True})
    for seconds in (0, 60):
        _iterate(runner, getter, RUN_NOW + timedelta(seconds=seconds))
    assert not _halted() and _writeoffs(runner["paper"]) == []


def test_a_written_off_market_that_reads_open_again_after_the_grace_is_a_contradiction_halt(runner):
    getter = RunnerGamma(gamma("held", closed_time=runner["closed_at"]))
    _iterate(runner, getter, RUN_NOW)
    _iterate(runner, getter, RUN_NOW + timedelta(seconds=60))
    assert len(_writeoffs(runner["paper"])) == 1
    getter.row = gamma("held", closed=False, accepting=True, closed_time=None)
    _iterate(runner, getter, RUN_NOW + timedelta(seconds=120))  # Gamma lag: not a contradiction yet
    assert not _halted()
    report = _iterate(runner, getter, RUN_NOW + timedelta(seconds=600))
    assert "closed_flag_contradiction_after_writeoff:held" in report.reasons and _halted()
    assert len(_writeoffs(runner["paper"])) == 1  # no rollback
    assert halt_store.has_active_halt(scope=halt_store.SCOPE_MARKET, market="held",
                                      reason_prefix="closed_flag_contradiction_after_writeoff") is True


def test_writeoff_events_follow_the_ledger_and_are_sent_again_after_a_restart(runner):
    getter = RunnerGamma(gamma("held", closed_time=runner["closed_at"]))
    _iterate(runner, getter, RUN_NOW)
    _iterate(runner, getter, RUN_NOW + timedelta(seconds=60))
    _iterate(runner, getter, RUN_NOW + timedelta(seconds=120))
    runner["state"] = m2_runner.RunnerState(paths=m2_runner.RunnerPaths.under(runner["run"]))
    _iterate(runner, getter, RUN_NOW + timedelta(seconds=180))
    events = [json.loads(line) for line in (runner["run"] / "events.jsonl").read_text().splitlines()]
    sent = [(e["resent"], [x["key"] for x in e["events"]]) for e in events if e.get("event") == "writeoff"]
    key = _writeoffs(runner["paper"])[0]["fill_id"]
    assert sent == [(False, [key]), (True, [key])]  # once on booking, again after the restart; consumers dedupe


# --- RiskGate correction (#197, issuecomment-6109537257): the write-off amount ----------------------------
# Shares held at the start of the UTC day are written off at max(cost, start-of-day mark); shares bought today
# at cost. So the write-off day's loss is never below the #124 loss just before the write-off (tightening only).

SOD_NOW = "2026-09-01T00:00:10Z"


def _mark_books(env, *rows):
    with (env["ledger"].parent / "books.jsonl").open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def _top(bid, ts, market="m", token="t"):
    return {"ts": ts, "book_ts": ts, "market_id": market, "token_id": token, "best_bid": bid,
            "best_bid_size": 1000.0, "best_ask": round(bid + 0.02, 4), "best_ask_size": 1000.0}


def _start_of_day(env, monkeypatch, bid, on_time=True):
    """The day's snapshot written at the day roll with a book bid of ``bid`` (on time unless told otherwise)."""
    monkeypatch.setattr(paper_ledger, "_LAST_EVAL_DAY", "2026-08-31" if on_time else None)
    # Task CX/DA: lateness is src/day_boundary.day_start_late (time since 00:00 UTC), not _LAST_EVAL_DAY
    env["clock"]["now"] = SOD_NOW if on_time else "2026-09-01T03:00:00Z"
    _mark_books(env, _top(bid, "2026-09-01T00:00:05Z"))
    snap_state = paper_ledger.load_state("x", "t", clock_ok=True)  # verified clock (Task DC)
    assert snap_state["daily_marks_late"] is (not on_time)
    env["clock"]["now"] = DAY1


def test_a_start_of_day_mark_above_cost_is_the_writeoff_amount_and_the_day_loss_never_drops(env, monkeypatch):
    _book(env["ledger"], [fill("buy", 20.0, 0.5, market="m", token="t")])  # cost 10, held since yesterday
    _start_of_day(env, monkeypatch, 0.8)  # start-of-day mark 16 > cost 10
    _mark_books(env, _top(0.1, "2026-09-01T11:59:55Z"))
    before = paper_ledger.load_state("x", "t")
    assert before["daily_loss_usd"] == pytest.approx(14.0)  # #124: 16 - 2
    out = paper_ledger.apply_writeoffs([close("m")])
    assert out["problems"] == []
    row = out["booked"][0]
    assert row["writeoff_usd"] == pytest.approx(16.0)
    assert row["writeoff_cost_usd"] == pytest.approx(10.0) and row["writeoff_sod_uplift_usd"] == pytest.approx(6.0)
    after = paper_ledger.load_state("x", "t")
    assert after["daily_loss_usd"] == pytest.approx(16.0)
    assert after["daily_loss_usd"] >= before["daily_loss_usd"]
    assert after["daily_writeoff_usd"] == pytest.approx(16.0)


def test_shares_bought_today_are_written_off_at_cost_next_to_the_start_of_day_mark(env, monkeypatch):
    _book(env["ledger"], [fill("buy", 20.0, 0.5, market="m", token="t")])  # carried: cost 10
    _start_of_day(env, monkeypatch, 0.8)  # carried mark 16
    _book(env["ledger"], [fill("buy", 10.0, 0.6, ts="2026-09-01T06:00:00Z", market="m", token="t")], prefix="today")
    _mark_books(env, _top(0.1, "2026-09-01T11:59:55Z"))
    before = paper_ledger.load_state("x", "t")["daily_loss_usd"]
    row = paper_ledger.apply_writeoffs([close("m")])["booked"][0]
    assert row["writeoff_usd"] == pytest.approx(22.0)  # 16 (start mark) + 6 (today's cost)
    assert row["writeoff_cost_usd"] == pytest.approx(16.0) and row["writeoff_sod_uplift_usd"] == pytest.approx(6.0)
    assert paper_ledger.load_state("x", "t")["daily_loss_usd"] >= before


@pytest.mark.parametrize("bid, on_time", [(0.3, True), (0.8, False)])
def test_a_start_of_day_mark_below_cost_or_a_late_snapshot_keeps_the_cost(env, monkeypatch, bid, on_time):
    _book(env["ledger"], [fill("buy", 20.0, 0.5, market="m", token="t")])
    _start_of_day(env, monkeypatch, bid, on_time=on_time)
    _mark_books(env, _top(0.1, "2026-09-01T11:59:55Z"))
    before = paper_ledger.load_state("x", "t")["daily_loss_usd"]
    row = paper_ledger.apply_writeoffs([close("m")])["booked"][0]
    assert row["writeoff_usd"] == pytest.approx(10.0) and row["writeoff_sod_uplift_usd"] == 0.0
    assert paper_ledger.load_state("x", "t")["daily_loss_usd"] >= before


def test_no_snapshot_yet_today_writes_off_at_cost(env):
    _book(env["ledger"], [fill("buy", 20.0, 0.5, market="m", token="t")])
    row = paper_ledger.apply_writeoffs([close("m")])["booked"][0]
    assert row["writeoff_usd"] == pytest.approx(10.0) and row["writeoff_sod_uplift_usd"] == 0.0


def test_a_short_carried_leg_uses_the_lower_start_of_day_buy_back(env, monkeypatch):
    _book(env["ledger"], [fill("sell", 10.0, 0.6, market="m", token="t")])  # proceeds 6: cost write-off 4
    _start_of_day(env, monkeypatch, 0.28)  # buy-back at the ask 0.30: 3 -> write-off 10 - 3 = 7
    row = paper_ledger.apply_writeoffs([close("m")])["booked"][0]
    assert row["writeoff_usd"] == pytest.approx(7.0) and row["writeoff_cost_usd"] == pytest.approx(4.0)


@pytest.mark.parametrize("change, ok", [
    ({}, True),
    ({"writeoff_usd": 12.0, "writeoff_sod_uplift_usd": 2.0}, True),    # cost + uplift, within shares x 1
    ({"writeoff_usd": 12.0}, False),                                  # does not add up
    ({"writeoff_cost_usd": 9.0, "writeoff_usd": 9.0}, False),         # not the replayed cost
    ({"writeoff_usd": 21.0, "writeoff_sod_uplift_usd": 11.0}, False),  # above the leg's worst case (20 x 1)
])
def test_daily_loss_checks_the_writeoff_breakdown(env, change, ok):
    _book(env["ledger"], [fill("buy", 20.0, 0.5, market="m", token="t")])
    row = market_close.plan_writeoffs(_rows(env["ledger"]), ["ck-0000"], [close("m")], DAY1).rows[0]
    row.update(change)
    ledger_store.book(env["ledger"], row)
    day = daily_loss.daily_loss(_rows(env["ledger"]), "2026-09-01")
    assert (day.problems == []) is ok, day.problems
    if ok:
        assert day.writeoff_usd == pytest.approx(row["writeoff_usd"])
        assert day.total_usd == pytest.approx(row["writeoff_usd"])


@pytest.mark.parametrize("change", [{"writeoff_sod_uplift_usd": -1.0}, {"writeoff_cost_usd": "10"},
                                    {"writeoff_cost_usd": float("nan")}])
def test_a_damaged_writeoff_breakdown_is_refused(env, change):
    _book(env["ledger"], [fill("buy", 20.0, 0.5, market="m", token="t")])
    row = market_close.plan_writeoffs(_rows(env["ledger"]), ["ck-0000"], [close("m")], DAY1).rows[0]
    row.update(change)
    with pytest.raises(ledger_store.LedgerIntegrityError):
        ledger_store.book(env["ledger"], row)


# --- RiskGate ruling OQ-N7 (#197, issuecomment-6109526792): a reopen after the write-off is a GLOBAL halt ----

def test_a_reopen_after_writeoff_halts_every_market_and_its_settlement_stays_blocked_after_the_global_resume(runner):
    getter = RunnerGamma(gamma("held", closed_time=runner["closed_at"]))
    _iterate(runner, getter, RUN_NOW)
    _iterate(runner, getter, RUN_NOW + timedelta(seconds=60))
    getter.row = gamma("held", closed=False, accepting=True, closed_time=None)
    _iterate(runner, getter, RUN_NOW + timedelta(seconds=600))
    # global, not market-only: every market's new intent is vetoed (src/risk_gate halts first)
    assert _halted()
    assert any(r.startswith("halt_global:") for r in halt_store.halt_problems("another-market"))
    later = _iterate(runner, getter, RUN_NOW + timedelta(seconds=660))
    assert later.action == m2_runner.ACT_PAUSE and later.reasons[0] == "global_halt"
    assert len(_writeoffs(runner["paper"])) == 1  # not rolled back, not written off twice
    # #214 stays as an extra guard: once the Operator lifts the global halt, this market's settlement is still blocked
    for record in halt_store.active_halts():
        if record["scope"] == halt_store.SCOPE_GLOBAL:
            halt_store.resume(from_role="Operator", scope=halt_store.SCOPE_GLOBAL, halt_id=record["id"],
                              approved_by="test", root_cause="test", postmortem="test",
                              bus_path=runner["run"] / "resume_bus.jsonl")
    assert not _halted()
    assert "held" in paper_ledger.settlement_blocked_markets()
    assert len(_writeoffs(runner["paper"])) == 1


def test_e2e_real_settle_hook_with_a_paused_clock_writes_off_and_the_daily_loss_counts_it(runner):
    """#197 option A (PR #342 review, non-blocking): through the real _settle_tick/_writeoff_hook. The runner's
    clock is never verified here (no /time answer), so runner_clock_ok is False on every tick: the write-off is
    still booked, at the late reference, and the daily loss counts it."""
    getter = RunnerGamma(gamma("held", closed_time=runner["closed_at"]))
    _iterate(runner, getter, RUN_NOW)
    assert m2_runner.runner_clock_ok(runner["state"]) is False
    report = _iterate(runner, getter, RUN_NOW + timedelta(seconds=60))
    assert report.action == m2_runner.ACT_PAUSE  # the clock pause; the write-off ran before it
    booked = _writeoffs(runner["paper"])
    assert len(booked) == 1 and booked[0]["writeoff_sod_late"] is True
    assert booked[0]["writeoff_usd"] == pytest.approx(10.0)
    read = ledger_store.read_ledger(runner["paper"])[1]
    stamp = (RUN_NOW + timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
    risk = paper_ledger.daily_risk_loss(read.rows, list(read.ids), stamp, clock_ok=False)
    assert risk["problems"] == [] and risk["loss_usd"] == pytest.approx(10.0)


class _Crash(BaseException):
    """A process crash between two leg writes (not an Exception: nothing in the runner catches it)."""


def test_a_writeoff_interrupted_between_two_legs_writes_off_the_remaining_leg_after_a_restart(runner, monkeypatch):
    """PR #342 review (Codex 6183035627): completion is per leg. The market's YES leg is written off, the process
    dies before the NO leg's row; after a restart the NO leg is written off exactly once."""
    stamp = (RUN_NOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _book(runner["paper"], [fill("buy", 10.0, 0.4, ts=stamp, market="held", token="t2")], prefix="ck2")
    getter = RunnerGamma(gamma("held", closed_time=runner["closed_at"]))
    _iterate(runner, getter, RUN_NOW)
    real_book = ledger_store.book

    def crash_on_t2(path, row, **kwargs):
        if ledger_store.is_writeoff(row) and row.get("token_id") == "t2":
            raise _Crash()
        return real_book(path, row, **kwargs)

    monkeypatch.setattr(ledger_store, "book", crash_on_t2)
    with pytest.raises(_Crash):
        _iterate(runner, getter, RUN_NOW + timedelta(seconds=60))
    monkeypatch.setattr(ledger_store, "book", real_book)
    assert [r["token_id"] for r in _writeoffs(runner["paper"])] == ["t1"]  # interrupted after one leg
    runner["state"] = m2_runner.RunnerState(paths=m2_runner.RunnerPaths.under(runner["run"]))  # restart
    report = _iterate(runner, getter, RUN_NOW + timedelta(seconds=120))
    assert "market_close_halt" not in report.reasons and not _halted()
    booked = _writeoffs(runner["paper"])
    assert sorted(r["token_id"] for r in booked) == ["t1", "t2"]
    t2 = next(r for r in booked if r["token_id"] == "t2")
    assert t2["writeoff_usd"] == pytest.approx(4.0) and t2["close_first_seen_ts"] == booked[0]["close_first_seen_ts"]
    for seconds in (180, 240):  # later ticks: nothing written off twice
        _iterate(runner, getter, RUN_NOW + timedelta(seconds=seconds))
    assert len(_writeoffs(runner["paper"])) == 2
    assert ledger_store.verify(ledger_store.read_ledger(runner["paper"])[1]) == []
