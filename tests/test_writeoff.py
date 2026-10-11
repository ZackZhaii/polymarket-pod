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
    assert state["daily_realized_gain_usd"] == pytest.approx(10.0)  # reported, never netted
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
    assert row["fill_id"] == market_close.writeoff_fill_id("m", "t", "2026-09-01T08:00:00+00:00")
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


def test_a_second_close_time_for_a_written_off_leg_is_a_contradiction(env):
    _book(env["ledger"], [fill("buy", 20.0, 0.5, market="m", token="t")])
    paper_ledger.apply_writeoffs([close("m")])
    out = paper_ledger.apply_writeoffs([close("m", "2026-09-01T09:00:00Z")])
    assert out["booked"] == [] and out["problems"] == ["writeoff_conflict:m|t:closed_time_changed"]


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


def test_a_read_that_does_not_qualify_starts_over_and_a_new_close_time_restarts():
    tracker = market_close.CloseTracker()
    tracker.update(_obs(gamma()), T0)
    t1 = T0 + timedelta(seconds=40)
    assert tracker.update(_obs(gamma(accepting=True), t1), t1) is None
    t2 = T0 + timedelta(seconds=80)
    assert tracker.update(_obs(gamma(), t2), t2) is None  # the first qualifying read again
    t3 = T0 + timedelta(seconds=100)
    assert tracker.update(_obs(gamma(closed_time="2026-09-01T08:05:00Z"), t3), t3) is None
    t4 = T0 + timedelta(seconds=140)
    assert tracker.update(_obs(gamma(closed_time="2026-09-01T08:05:00Z"), t4), t4) is not None


def test_a_second_source_confirms_only_after_a_qualifying_gamma_read():
    tracker = market_close.CloseTracker()
    assert tracker.confirm_by_second_source("m", market_close.BY_CLOB, {"accepting_orders": False}, T0) is None
    tracker.update(_obs(gamma()), T0)
    assert tracker.confirm_by_second_source("m", market_close.BY_CLOB, {"accepting_orders": None}, T0) is None
    assert tracker.confirm_by_second_source("m", market_close.BY_CLOB, {"accepting_orders": False}, T0) is not None
    data = {"tokens": [{"token_id": "a", "closed": True}, {"token_id": "b", "closed": False}]}
    assert tracker.confirm_by_second_source("m", market_close.BY_DATA, data, T0, ["a"]) is not None
    assert tracker.confirm_by_second_source("m", market_close.BY_DATA, data, T0, ["a", "b"]) is None
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


def test_gamma_read_asks_with_and_without_closed_true_and_an_empty_answer_is_a_read_failure():
    getter = Gamma(closed=[gamma()])
    obs = market_close.gamma_read(getter, "m", T0)
    assert market_close.qualifies(obs, T0)
    assert sorted(parse_qs(urlsplit(u).query).get("closed", [""])[0] for u in getter.urls) == ["", "true"]
    assert all(m2_runner.url_allowed(u) for u in getter.urls)
    assert market_close.gamma_read(Gamma(), "m", T0).problem == "gamma_market_missing"
    assert market_close.gamma_read(Gamma(error=TimeoutError()), "m", T0).problem == "gamma_read_failed:TimeoutError"
    assert market_close.gamma_read(Gamma(default={"x": 1}), "m", T0).problem == "gamma_bad_response"
    split = market_close.gamma_read(Gamma(default=[gamma(closed=False, accepting=True)], closed=[gamma()]), "m", T0)
    assert split.problem is None and not market_close.qualifies(split, T0)


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


def test_a_failed_close_read_of_a_held_market_is_a_global_invalid_risk_state_halt(runner):
    getter = RunnerGamma(gamma("held"))
    getter.error = TimeoutError()
    report = _iterate(runner, getter, RUN_NOW)
    assert "market_close_halt" in report.reasons
    assert any(r.startswith("market_close_unreadable:held:gamma_read_failed") for r in report.reasons)
    assert halt_store.has_active_halt(scope=halt_store.SCOPE_GLOBAL, reason_prefix="invalid_risk_state") is True
    assert _writeoffs(runner["paper"]) == []


def test_an_empty_answer_for_a_held_market_is_a_read_failure_not_a_close(runner):
    getter = RunnerGamma({"id": "someone-else", "closed": True})
    report = _iterate(runner, getter, RUN_NOW)
    assert any(r.endswith("gamma_market_missing") for r in report.reasons)
    assert _writeoffs(runner["paper"]) == []


def test_a_written_off_market_that_reads_open_again_is_a_contradiction_halt(runner):
    getter = RunnerGamma(gamma("held", closed_time=runner["closed_at"]))
    _iterate(runner, getter, RUN_NOW)
    _iterate(runner, getter, RUN_NOW + timedelta(seconds=60))
    assert len(_writeoffs(runner["paper"])) == 1
    getter.row = gamma("held", closed=False, accepting=True, closed_time=None)
    report = _iterate(runner, getter, RUN_NOW + timedelta(seconds=120))
    assert "market_close_contradiction:held:reads_open_after_writeoff" in report.reasons
    assert halt_store.has_active_halt(scope=halt_store.SCOPE_GLOBAL, reason_prefix="invalid_risk_state") is True
