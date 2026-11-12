"""PR #483 (replaces #342): tests for the two post-merge fixes of src/market_close and the ledger overflow path.

Written AFTER the source change (a TDD gap, stated in the PR); dev's own #349 / #353 / #345 tests were the ones that
were red first. They pin what the code does now:

(1) Overflow (#349). A settlement row whose ``settlement_payout`` is an int too large for a float (10**400) makes
    ``ledger_store.verify`` raise ``OverflowError`` (as on dev); ``paper_ledger.load_state`` reports it as
    ``ledger_verify_failed:OverflowError`` and ``apply_writeoffs`` returns it as a problem: nothing is booked,
    the ledger bytes are unchanged and nothing is raised into the runner. The market halt for that payout is
    covered by dev's tests/test_349_settlement_blocking_prefixes.py, not here. A 10**400 in a WRITE-OFF row is a
    plain ``invalid_writeoff`` problem and ``verify`` does not raise.
(2) Keyset reads (#353). ``market_close.gamma_url`` is the allowlisted ``/markets/keyset?id=<id>[&closed=true]``;
    ``gamma_read`` accepts only ``{"markets": [row(s) of that id]}``, reports any other shape as
    ``gamma_bad_response`` (``gamma_duplicate_market`` for two rows of the id), and sends no request for a
    non-numeric id (``gamma_bad_id``). A read problem is an unknown read, not a close.
No network, no order; every file is in the test's folder.
"""
from __future__ import annotations

import json
import socket
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

import pytest

from src import halt_store, ledger_store, m2_runner, market_close, paper_ledger, podlib
from tests.test_writeoff import DAY1, _book, close, fill

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
OVERFLOW = "ledger_verify_failed:OverflowError"
ID = "900001"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("network attempted")
    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


# ------------------------------------------------------------------ (1) overflow (#349)

def _ledger_with(tmp_path, name, value, kind="writeoff"):
    path = tmp_path / "paper.jsonl"
    _book(path, [fill("buy", 20.0, 0.5, market="m", token="t")])
    rows = ledger_store.read_ledger(path)[1].rows
    if kind == "writeoff":
        row = market_close.plan_writeoffs(rows, ["ck-0000"], [close("m")], DAY1).rows[0]
    else:
        row = {**fill("sell", 20.0, 0.0, market="m", token="t"), "kind": "settlement", "strategy": "settlement",
               "risk_decision": "settlement", "fill_id": "settle-x", "condition_id": "0xm",
               "winning_token_id": "other", "closed_ts": "2026-09-01T08:00:00Z", "settlement_payout": 0.0,
               "notional_usd": 0.0}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({**row, name: value}) + "\n")  # raw: book() would refuse it
    return path


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setattr(paper_ledger, "PAPER_PATH", tmp_path / "paper.jsonl")
    monkeypatch.setattr(paper_ledger, "QUOTES_PATH", tmp_path / "quotes.jsonl")
    monkeypatch.setattr(paper_ledger, "DAILY_MARKS_DIR", tmp_path / "daily_marks")
    monkeypatch.setattr(paper_ledger, "MARK_BOOKS_PATH", tmp_path / "books.jsonl")
    monkeypatch.setattr(paper_ledger, "bus", lambda *args, **kwargs: None)
    monkeypatch.setattr(halt_store, "HALT_STATE_PATH", tmp_path / "halt_state.json")
    monkeypatch.setattr(halt_store, "HALT_FALLBACK_PATH", tmp_path / "HALT")
    return tmp_path


HUGE = [10 ** 400, -(10 ** 400)]


@pytest.mark.parametrize("huge", HUGE, ids=["huge", "neg-huge"])
def test_a_huge_settlement_payout_is_never_an_empty_problem_list_in_load_state(paths, huge):
    """verify raises on the huge payout; load_state turns that into ledger_verify_failed:OverflowError and no state."""
    _ledger_with(paths, "settlement_payout", huge, kind="settlement").replace(paper_ledger.PAPER_PATH)
    state = paper_ledger.load_state("x", "t")
    assert OVERFLOW in state["ledger_problems"], state["ledger_problems"]
    assert state["daily_loss_usd"] is None and state["open_positions"] is None


@pytest.mark.parametrize("huge", HUGE, ids=["huge", "neg-huge"])
def test_a_huge_settlement_payout_is_a_problem_of_apply_writeoffs_not_a_crash(paths, huge):
    """apply_writeoffs reads the same ledger: it books nothing, returns ledger_verify_failed as a problem, never raises."""
    _ledger_with(paths, "settlement_payout", huge, kind="settlement").replace(paper_ledger.PAPER_PATH)
    before = paper_ledger.PAPER_PATH.read_bytes()
    out = paper_ledger.apply_writeoffs([close("m")], DAY1)
    assert out["booked"] == [] and out["problems"], out
    assert any(OVERFLOW in problem for problem in out["problems"]), out
    assert paper_ledger.PAPER_PATH.read_bytes() == before


@pytest.mark.parametrize("name", ["writeoff_shares", "writeoff_usd", "writeoff_cost_usd", "writeoff_sod_uplift_usd"])
@pytest.mark.parametrize("huge", HUGE, ids=["huge", "neg-huge"])
def test_a_huge_int_in_a_writeoff_row_is_an_invalid_writeoff_and_verify_does_not_raise(tmp_path, name, huge):
    path = _ledger_with(tmp_path, name, huge)
    problems = ledger_store.verify(ledger_store.read_ledger(path)[1])
    assert any(p.startswith("invalid_writeoff:") for p in problems), problems


def test_an_ordinary_ledger_has_no_overflow_problem(paths):
    _book(paper_ledger.PAPER_PATH, [fill("buy", 20.0, 0.5, market="m", token="t")])
    state = paper_ledger.load_state("x", "t")
    assert not [p for p in state["ledger_problems"] if p.startswith("ledger_verify_failed")]


# ------------------------------------------------------------------ (2) keyset reads

class Keyset:
    """Serves {"markets": [row]} per (id, closed) view and records every URL."""

    def __init__(self, views=None, default=None):
        self.views, self.urls = dict(views or {}), []
        self.default = {"markets": []} if default is None else default

    def __call__(self, url):
        self.urls.append(url)
        q = parse_qs(urlsplit(url).query)
        return self.views.get(((q.get("id") or [""])[0], q.get("closed") == ["true"]), self.default)


def row(market=ID, closed=True, accepting=False, closed_time="2026-09-01 08:00:00+00"):
    return {"id": market, "closed": closed, "acceptingOrders": accepting, "closedTime": closed_time}


@pytest.mark.parametrize("closed,query", [(True, {"id": [ID], "closed": ["true"]}), (False, {"id": [ID]})])
def test_gamma_url_is_the_allowlisted_keyset_path(closed, query):
    url = market_close.gamma_url(ID, closed=closed)
    parts = urlsplit(url)
    assert parts.scheme + "://" + parts.netloc + parts.path == podlib.GAMMA_PREFIX + "/keyset"
    assert parse_qs(parts.query) == query and m2_runner.url_allowed(url)


def test_a_close_is_seen_through_the_closed_true_keyset_view():
    getter = Keyset({(ID, True): {"markets": [row()]}})
    obs = market_close.gamma_read(getter, ID, NOW)
    assert obs.problem is None and obs.closed is True and obs.accepting_orders is False
    assert [parse_qs(urlsplit(u).query).get("closed") for u in getter.urls] == [["true"], None]


def test_an_open_market_is_seen_through_the_default_keyset_view():
    getter = Keyset({(ID, False): {"markets": [row(closed=False, accepting=True, closed_time=None)]}})
    obs = market_close.gamma_read(getter, ID, NOW)
    assert obs.problem is None and obs.closed is False


def test_a_market_listed_in_neither_view_is_unknown():
    assert market_close.gamma_read(Keyset(), ID, NOW).problem == "gamma_market_missing"


def test_a_market_listed_in_both_views_is_unknown():
    getter = Keyset({(ID, True): {"markets": [row()]}, (ID, False): {"markets": [row(closed=False)]}})
    assert market_close.gamma_read(getter, ID, NOW).problem == "gamma_views_disagree"


@pytest.mark.parametrize("answer", [
    [row()],                                        # the legacy bare list is not a keyset answer
    None, "x", {}, {"markets": None}, {"markets": "x"},
    {"markets": [row()], "next_cursor": ""},        # the id filter was ignored (a page of the whole list)
    {"markets": [row(market="900002")]},            # a row of another id
    {"markets": [row(), "junk"]},
])
def test_a_malformed_keyset_answer_is_a_bad_response_never_a_close(answer):
    obs = market_close.gamma_read(Keyset({(ID, True): answer}), ID, NOW)
    assert obs.problem == "gamma_bad_response"


def test_two_rows_of_this_id_are_a_duplicate():
    obs = market_close.gamma_read(Keyset({(ID, True): {"markets": [row(), row()]}}), ID, NOW)
    assert obs.problem == "gamma_duplicate_market"


@pytest.mark.parametrize("bad_id", ["", "m", "held", "12a", "1 2", "٣", "0x1"])
def test_a_non_numeric_market_id_sends_no_request(bad_id):
    getter = Keyset()
    obs = market_close.gamma_read(getter, bad_id, NOW)
    assert obs.problem == "gamma_bad_id" and getter.urls == []


def test_no_legacy_id_query_remains_in_market_close():
    source = open(market_close.__file__, encoding="utf-8").read()
    assert 'GAMMA_PREFIX + "?"' not in source
