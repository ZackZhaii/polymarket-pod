"""Task CF (#124, F2a): the daily loss counts each open leg's loss at the conservative liquidation value.

Poly风控's ruling (issuecomment-6102130981). Synthetic rows and books only; the ledger, the snapshots, the
books file, the halt state and the bus are all in the test's folder.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src import daily_marks, halt_store, leg_marks, ledger_store, paper_ledger
from src.fees import booked_fee_usd, ledger_fee
from src.podlib import load_risk
from src.risk_gate import Intent
from src.thresholds import MAX_BOOK_AGE_S, PAPER_EQUITY_USD

TODAY = "2026-09-01"
NOW = "2026-09-01T12:00:00Z"
NOW_DT = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
YESTERDAY = "2026-08-31T20:00:00Z"
FRESH = "2026-09-01T11:59:30Z"
CAP_USD = load_risk()["max_daily_loss_pct"] * PAPER_EQUITY_USD


def fill(side="buy", shares=100.0, price=0.5, ts=NOW, market="m", token="t", rate=0.0):
    notional = round(shares * price, 8)
    return dict(ts=ts, market=market, token_id=token, side=side, shares=shares, notional_usd=notional,
                fee_rate=rate, market_price=price, effective_price=price,
                fee_usd=booked_fee_usd(ledger_fee(rate, price, price, notional, shares)),
                hypothetical=False, counts_toward_risk=True, risk_decision="allow")


def book(bid=None, bid_size=1000.0, ask=None, ask_size=1000.0, ts=FRESH, market="m", token="t", **extra):
    row = {"ts": ts, "book_ts": ts, "market_id": market, "token_id": token, "best_bid": bid,
           "best_bid_size": bid_size, "best_ask": ask, "best_ask_size": ask_size}
    row.update(extra)
    return row


# --- liquidation_mark (pure) -------------------------------------------------------------------------

def test_a_long_walks_the_bid_ladder_and_the_rest_is_worth_zero():
    ladder = {"book_ts": FRESH, "bids": [{"price": 0.3, "size": 20}, {"price": 0.4, "size": 60}],
              "asks": [[0.45, 10]]}
    mark = leg_marks.liquidation_mark(100.0, ladder, NOW_DT)
    assert mark.value_usd == pytest.approx(60 * 0.4 + 20 * 0.3)  # 20 shares unabsorbed are worth 0
    assert (mark.source, mark.mark_invalid, mark.book_ts) == ("bid_ladder", False, FRESH)


def test_a_top_of_book_row_is_a_one_level_ladder():
    assert leg_marks.liquidation_mark(100.0, book(0.4, 40.0, 0.42), NOW_DT).value_usd == pytest.approx(16.0)
    assert leg_marks.liquidation_mark(100.0, book(0.4, 400.0, 0.42), NOW_DT).value_usd == pytest.approx(40.0)
    no_size = leg_marks.liquidation_mark(100.0, book(0.4, None, 0.42), NOW_DT)
    assert no_size.value_usd == 0.0 and no_size.mark_invalid is True  # nothing to sell into


def test_never_the_mid():
    mark = leg_marks.liquidation_mark(10.0, book(0.30, 1000.0, 0.70), NOW_DT)
    assert mark.value_usd == pytest.approx(3.0)  # not 5.0


def test_a_short_buys_back_up_the_ask_ladder_and_the_rest_costs_one():
    ladder = {"book_ts": FRESH, "bids": [[0.5, 10]], "asks": [[0.7, 30], [0.6, 50]]}
    mark = leg_marks.liquidation_mark(-100.0, ladder, NOW_DT)
    assert mark.value_usd == pytest.approx(-(50 * 0.6 + 30 * 0.7 + 20 * 1.0))
    assert mark.source == "ask_ladder"


@pytest.mark.parametrize("bad, reason", [
    (None, "no_book"),
    ({"book_ts": None, "best_bid": 0.4, "best_bid_size": 100}, "missing_book_ts"),
    (book(0.4, ts="2026-09-01T11:57:59Z"), "stale_book"),
    (book(0.4, ts="2026-09-01T12:00:05Z"), "future_book_ts"),
    (book(0.4, ts="yesterday-ish"), "invalid_book_ts"),
    (book(0.45, 100.0, 0.44), "crossed_book"),
    (book(0.45, 100.0, 0.45), "crossed_book"),
    (book(1.2, 100.0, 1.3), "bad_level"),
    (book(0.0, 100.0, 0.1), "bad_level"),
    ({"book_ts": FRESH, "bids": [[0.4, -1]], "asks": []}, "bad_level"),
    ({"book_ts": FRESH, "bids": [[float("nan"), 5]], "asks": []}, "bad_level"),
    ({"book_ts": FRESH, "bids": "0.4", "asks": []}, "bad_level"),
    ({"book_ts": FRESH, "bids": [], "asks": [[0.5, 10]]}, "no_levels"),
])
def test_a_missing_stale_crossed_or_out_of_range_book_is_the_worst_case_and_marked_invalid(bad, reason):
    long = leg_marks.liquidation_mark(100.0, bad, NOW_DT)
    assert (long.value_usd, long.mark_invalid, long.source, long.reason) == (0.0, True, "invalid_book", reason)
    if reason != "no_levels" and not (isinstance(bad, dict) and bad.get("best_ask") == 0.1):
        short = leg_marks.liquidation_mark(-100.0, bad, NOW_DT)
        assert (short.value_usd, short.mark_invalid) == (-100.0, True)


@pytest.mark.parametrize("asks", [[], None, "garbage", [[1.5, 10]], [[0.5, float("nan")]]])
def test_a_long_needs_only_the_bid_side(asks):
    """Task CI (KEY): an empty or unusable ask side never marks a long invalid."""
    row = {"book_ts": FRESH, "bids": [[0.4, 1000]]}
    if asks is not None:
        row["asks"] = asks
    mark = leg_marks.liquidation_mark(100.0, row, NOW_DT)
    assert (mark.value_usd, mark.mark_invalid) == (pytest.approx(40.0), False)
    top = leg_marks.liquidation_mark(100.0, book(0.4, 1000.0, None), NOW_DT)  # recorder row, no ask
    assert (top.value_usd, top.mark_invalid) == (pytest.approx(40.0), False)


@pytest.mark.parametrize("bids", [[], None, [[0.0, 10]], [[0.5, -1]]])
def test_a_short_needs_only_the_ask_side(bids):
    row = {"book_ts": FRESH, "asks": [[0.6, 1000]]}
    if bids is not None:
        row["bids"] = bids
    mark = leg_marks.liquidation_mark(-100.0, row, NOW_DT)
    assert (mark.value_usd, mark.mark_invalid) == (pytest.approx(-60.0), False)


def test_a_long_with_an_empty_bid_side_is_still_invalid():
    mark = leg_marks.liquidation_mark(100.0, {"book_ts": FRESH, "bids": [], "asks": [[0.5, 10]]}, NOW_DT)
    assert (mark.value_usd, mark.mark_invalid, mark.reason) == (0.0, True, "no_levels")


def test_the_age_limit_is_max_book_age_s_inclusive():
    edge = datetime.fromtimestamp(NOW_DT.timestamp() - MAX_BOOK_AGE_S, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert leg_marks.liquidation_mark(10.0, book(0.4, ts=edge), NOW_DT).mark_invalid is False


def test_settled_and_closed_unresolved_legs():
    assert leg_marks.liquidation_mark(10.0, None, NOW_DT, settled_payout=1.0).value_usd == 10.0
    assert leg_marks.liquidation_mark(10.0, None, NOW_DT, settled_payout=0.0).value_usd == 0.0
    closed = leg_marks.liquidation_mark(10.0, book(0.9), NOW_DT, closed=True)
    assert (closed.value_usd, closed.source) == (0.0, "closed_unresolved")
    assert leg_marks.liquidation_mark(-10.0, None, NOW_DT, closed=True).value_usd == -10.0
    assert leg_marks.liquidation_mark(10.0, None, NOW_DT, settled_payout=0.5).mark_invalid is True


# --- positions / unrealized_loss (pure) ----------------------------------------------------------------

def _snap(legs, late=False):
    return {"day": TODAY, "created_at": NOW, "late": late, "legs": legs, "sha256": "x"}


def _snap_leg(shares, cost, mark, invalid=False, key="m|t"):
    return {"leg_key": key, "shares": shares, "cost_basis_usd": cost, "mark_usd": mark, "mark_source": "bid_ladder",
            "book_ts": FRESH, "mark_invalid": invalid}


def _loss(rows, now_books, snap):
    pos = leg_marks.positions(rows, TODAY)
    marks = {key: leg_marks.liquidation_mark(leg.shares, now_books.get(key), NOW_DT) for key, leg in pos.now.items()}
    return leg_marks.unrealized_loss(pos, marks, snap)


def test_a_long_whose_bid_falls_from_050_to_040_adds_ten_dollars():
    rows = [fill("buy", 100, 0.45, YESTERDAY)]
    result = _loss(rows, {("m", "t"): book(0.40, 1000.0, 0.42)}, _snap([_snap_leg(100.0, 45.0, 50.0)]))
    assert result.loss_usd == pytest.approx(10.0)
    assert result.legs[0]["ref_source"] == "start_mark"


def test_a_gaining_leg_never_offsets_a_losing_one():
    rows = [fill("buy", 100, 0.5, YESTERDAY, market="a"), fill("buy", 100, 0.5, YESTERDAY, market="b")]
    snap = _snap([_snap_leg(100.0, 50.0, 50.0, key="a|t"), _snap_leg(100.0, 50.0, 50.0, key="b|t")])
    result = _loss(rows, {("a", "t"): book(0.40, market="a", ask=0.41), ("b", "t"): book(0.70, market="b", ask=0.71)},
                   snap)
    assert result.loss_usd == pytest.approx(10.0)
    assert sorted(leg["loss_usd"] for leg in result.legs) == pytest.approx([0.0, 10.0])


def test_an_invalid_or_zero_start_mark_or_a_late_snapshot_uses_the_cost():
    rows = [fill("buy", 100, 0.6, YESTERDAY)]
    now = {("m", "t"): book(0.40, ask=0.41)}
    for snap in (_snap([_snap_leg(100.0, 60.0, 0.0, invalid=True)]), _snap([_snap_leg(100.0, 60.0, 0.0)]),
                 _snap([_snap_leg(100.0, 60.0, 45.0)], late=True)):
        result = _loss(rows, now, snap)
        assert result.loss_usd == pytest.approx(20.0)
        assert result.legs[0]["ref_source"] == "cost"


def test_a_leg_opened_today_uses_todays_cost_without_fees():
    rows = [fill("buy", 100, 0.5, NOW, rate=0.02)]
    result = _loss(rows, {("m", "t"): book(0.44, ask=0.46)}, _snap([]))
    assert result.loss_usd == pytest.approx(6.0)


def test_a_leg_held_since_yesterday_and_added_today():
    rows = [fill("buy", 100, 0.6, YESTERDAY), fill("buy", 100, 0.5, NOW)]
    result = _loss(rows, {("m", "t"): book(0.40, ask=0.41)}, _snap([_snap_leg(100.0, 60.0, 45.0)]))
    assert result.loss_usd == pytest.approx((45.0 + 50.0) - 80.0)


def test_a_snapshot_that_does_not_match_the_start_of_day_legs_is_a_problem():
    rows = [fill("buy", 100, 0.5, YESTERDAY)]
    assert _loss(rows, {}, _snap([])).problems
    assert _loss(rows, {}, _snap([_snap_leg(90.0, 45.0, 45.0)])).problems
    assert _loss([], {}, _snap([_snap_leg(90.0, 45.0, 45.0)])).problems


def test_a_closed_position_has_no_unrealized_loss():
    rows = [fill("buy", 100, 0.5, YESTERDAY), fill("sell", 100, 0.2, NOW)]
    result = _loss(rows, {}, _snap([_snap_leg(100.0, 50.0, 50.0)]))
    assert (result.loss_usd, result.legs) == (0.0, [])


# --- the snapshot file ----------------------------------------------------------------------------------

def test_the_snapshot_is_written_once_and_read_back(tmp_path):
    snap = daily_marks.create_snapshot(tmp_path, TODAY, NOW, False, [_snap_leg(1.0, 0.5, 0.4)])
    assert daily_marks.read_snapshot(tmp_path, TODAY) == (snap, [])
    before = (tmp_path / f"{TODAY}.json").read_bytes()
    with pytest.raises(daily_marks.SnapshotExists):
        daily_marks.create_snapshot(tmp_path, TODAY, NOW, True, [])
    assert (tmp_path / f"{TODAY}.json").read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == [f"{TODAY}.json"]  # no temp file left


def test_a_missing_snapshot_is_none_without_problems(tmp_path):
    assert daily_marks.read_snapshot(tmp_path, TODAY) == (None, [])


@pytest.mark.parametrize("damage", ["sha", "day", "garbage", "fields", "leg"])
def test_a_damaged_snapshot_is_a_problem(tmp_path, damage):
    daily_marks.create_snapshot(tmp_path, TODAY, NOW, False, [_snap_leg(1.0, 0.5, 0.4)])
    path = tmp_path / f"{TODAY}.json"
    data = json.loads(path.read_text())
    if damage == "sha":
        data["legs"][0]["mark_usd"] = 0.9
    elif damage == "day":
        data["day"] = "2026-08-31"
        data["sha256"] = daily_marks.digest(data)
    elif damage == "fields":
        data["extra"] = 1
    elif damage == "leg":
        data["legs"][0]["mark_invalid"] = "no"
        data["sha256"] = daily_marks.digest(data)
    path.write_text("{not json" if damage == "garbage" else json.dumps(data))
    snap, problems = daily_marks.read_snapshot(tmp_path, TODAY)
    assert snap is None and problems


# --- load_state / commit_if_allowed ---------------------------------------------------------------------

@pytest.fixture
def env(tmp_path, monkeypatch):
    path = tmp_path / "paper.jsonl"
    bus: list[tuple] = []
    clock = {"now": NOW}
    monkeypatch.setattr(paper_ledger, "PAPER_PATH", path)
    monkeypatch.setattr(paper_ledger, "QUOTES_PATH", tmp_path / "quotes.jsonl")
    monkeypatch.setattr(paper_ledger, "MARK_BOOKS_PATH", tmp_path / "books.jsonl")
    monkeypatch.setattr(paper_ledger, "DAILY_MARKS_DIR", tmp_path / "daily_marks")
    monkeypatch.setattr(paper_ledger, "bus", lambda *args, **kwargs: bus.append(args))
    monkeypatch.setattr(paper_ledger, "utc_now", lambda: clock["now"])
    monkeypatch.setattr(halt_store, "HALT_STATE_PATH", tmp_path / "halt_state.json")
    monkeypatch.setattr(halt_store, "HALT_FALLBACK_PATH", tmp_path / "HALT")
    return {"ledger": path, "books": tmp_path / "books.jsonl", "marks": tmp_path / "daily_marks", "bus": bus,
            "clock": clock}


def _book_fills(path: Path, rows):
    for index, row in enumerate(rows):
        ledger_store.book(path, {"fill_id": f"cf-{index:04d}", **row})


def _books(path: Path, *rows):
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def _intent(market="x", token="t"):
    return Intent(strategy="test", market=market, side="buy", model_prob=0.6, market_price=0.4,
                  assumed_slippage=0.005, fee_rate=0.0, liquidity_usd=50_000.0, spread=0.01, token_id=token,
                  book_ts=NOW)


def test_load_state_on_time_snapshot_then_a_ten_dollar_drop(env, monkeypatch):
    _book_fills(env["ledger"], [fill("buy", 100, 0.45, YESTERDAY)])
    monkeypatch.setattr(paper_ledger, "_LAST_EVAL_DAY", "2026-08-31")  # running at the day roll
    env["clock"]["now"] = "2026-09-01T00:00:10Z"
    _books(env["books"], book(0.50, ask=0.52, ts="2026-09-01T00:00:05Z"))
    first = paper_ledger.load_state("x", "t", clock_ok=True)
    assert first["daily_marks_late"] is False and first["daily_unrealized_loss_usd"] == 0.0
    snap = json.loads((env["marks"] / f"{TODAY}.json").read_text())
    assert snap["legs"][0]["mark_usd"] == pytest.approx(50.0) and snap["late"] is False
    env["clock"]["now"] = NOW
    _books(env["books"], book(0.40, ask=0.42, ts=FRESH))
    state = paper_ledger.load_state("x", "t", clock_ok=True)
    assert state["daily_unrealized_loss_usd"] == pytest.approx(10.0)
    assert state["daily_loss_usd"] == pytest.approx(10.0)
    assert state["daily_marks"][0]["ref_source"] == "start_mark"


def test_a_restart_without_a_snapshot_writes_a_late_one_and_uses_the_cost(env):
    _book_fills(env["ledger"], [fill("buy", 100, 0.45, YESTERDAY)])
    _books(env["books"], book(0.40, ask=0.42))
    state = paper_ledger.load_state("x", "t")  # this process never evaluated yesterday
    assert state["daily_marks_late"] is True
    assert state["daily_unrealized_loss_usd"] == pytest.approx(5.0)  # cost 45, not the 40 seen at restart
    assert json.loads((env["marks"] / f"{TODAY}.json").read_text())["late"] is True


def test_a_missing_or_stale_book_counts_at_zero_and_trips_max_daily_loss(env):
    shares = 100.0
    price = round((CAP_USD + 5) / shares, 4)
    _book_fills(env["ledger"], [fill("buy", shares, price, YESTERDAY)])
    state = paper_ledger.load_state("x", "t")  # no book at all
    assert state["daily_unrealized_loss_usd"] == pytest.approx(shares * price)
    assert state["daily_marks"][0]["mark_invalid"] is True
    assert "max_daily_loss" in paper_ledger.evaluate(_intent(), load_risk(), state).reasons
    _books(env["books"], book(price, ts="2026-09-01T11:00:00Z", ask=price + 0.01))  # stale
    state = paper_ledger.load_state("x", "t")
    assert state["daily_marks"][0]["reason"] == "stale_book"
    result = paper_ledger.commit_if_allowed(_intent(), load_risk())
    assert result["allow"] is False and "max_daily_loss" in result["decision"]["reasons"]
    assert not result.get("halted")  # a bad book is not a halt: the breaker stops trading
    veto = [args for args in env["bus"] if args[2] == "veto"][-1]
    assert veto[4]["daily_marks"][0]["mark_invalid"] is True  # the marks are on the bus line


def test_realized_gains_do_not_reduce_the_unrealized_loss(env):
    _book_fills(env["ledger"], [fill("buy", 100, 0.2, YESTERDAY, market="g"), fill("sell", 100, 0.6, NOW, market="g"),
                                fill("buy", 100, 0.5, NOW, market="m")])
    _books(env["books"], book(0.45, ask=0.47))
    state = paper_ledger.load_state("x", "t")
    assert state["daily_realized_gain_usd"] == pytest.approx(40.0)
    assert state["daily_loss_usd"] == pytest.approx(5.0)


@pytest.mark.parametrize("damage", ["sha", "garbage"])
def test_a_corrupt_snapshot_is_invalid_risk_state_and_a_global_halt(env, damage):
    _book_fills(env["ledger"], [fill("buy", 10, 0.5, YESTERDAY)])
    paper_ledger.load_state("x", "t")
    path = env["marks"] / f"{TODAY}.json"
    if damage == "sha":
        data = json.loads(path.read_text())
        data["late"] = False
        path.write_text(json.dumps(data))
    else:
        path.write_text("{")
    before = path.read_bytes()
    state = paper_ledger.load_state("x", "t")
    assert state["daily_loss_usd"] is None and state["ledger_problems"]
    result = paper_ledger.commit_if_allowed(_intent(), load_risk())
    assert result["halted"] is True and result["halt_reason"] == "invalid_risk_state"
    assert any(h["scope"] == "global" for h in halt_store.active_halts())
    assert path.read_bytes() == before  # never deleted or rebuilt


@pytest.mark.skipif(not hasattr(__import__("os"), "geteuid") or __import__("os").geteuid() == 0,
                    reason="POSIX permissions, not as root")
def test_a_snapshot_that_cannot_be_written_is_invalid_risk_state(env):
    _book_fills(env["ledger"], [fill("buy", 10, 0.5, YESTERDAY)])
    env["marks"].mkdir()
    env["marks"].chmod(0o500)  # readable, not writable
    try:
        state = paper_ledger.load_state("x", "t")
    finally:
        env["marks"].chmod(0o700)
    assert state["daily_loss_usd"] is None
    assert state["ledger_problems"] == ["daily_marks_write_failed:PermissionError"]
    env["marks"].rmdir()
    env["marks"].write_text("a file where the folder should be")
    state = paper_ledger.load_state("x", "t")
    assert state["daily_loss_usd"] is None and state["ledger_problems"][0].startswith("daily_marks_")


def test_a_valuation_exception_is_invalid_risk_state(env, monkeypatch):
    _book_fills(env["ledger"], [fill("buy", 10, 0.5, YESTERDAY)])

    def boom(*args, **kwargs):
        raise ZeroDivisionError

    monkeypatch.setattr(leg_marks, "unrealized_loss", boom)
    state = paper_ledger.load_state("x", "t")
    assert state["daily_loss_usd"] is None and state["ledger_problems"] == ["leg_marks_error:ZeroDivisionError"]


def test_crossing_utc_midnight_starts_a_new_snapshot_without_counting_yesterday_twice(env, monkeypatch):
    _book_fills(env["ledger"], [fill("buy", 100, 0.5, "2026-08-31T10:00:00Z")])
    env["clock"]["now"] = "2026-08-31T23:59:50Z"
    _books(env["books"], book(0.40, ask=0.42, ts="2026-08-31T23:59:45Z"))
    yesterday = paper_ledger.load_state("x", "t", clock_ok=True)
    assert yesterday["daily_unrealized_loss_usd"] == pytest.approx(10.0)  # opened that day: ref = cost
    env["clock"]["now"] = "2026-09-01T00:00:10Z"
    _books(env["books"], book(0.40, ask=0.42, ts="2026-09-01T00:00:05Z"))
    today = paper_ledger.load_state("x", "t", clock_ok=True)
    assert today["daily_marks_late"] is False  # this process evaluated on 08-31
    assert today["daily_unrealized_loss_usd"] == 0.0  # yesterday's 10 is not counted again
    assert sorted(p.name for p in env["marks"].glob("*.json")) == ["2026-08-31.json", "2026-09-01.json"]


def test_load_state_with_no_open_legs_is_unchanged(env):
    state = paper_ledger.load_state("x", "t")
    assert (state["daily_loss_usd"], state["daily_unrealized_loss_usd"], state["daily_marks"]) == (0.0, 0.0, [])
