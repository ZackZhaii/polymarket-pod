"""Ledger-side fail-closed branches and two boundaries of src/reconcile.py the main suite never pins.

test_reconcile.py covers the API side (envelope, rows, pagination, /v2/status) and the ledger
cases that make the whole ledger unusable (torn file, fill without a token_id, one token in two
markets). What was asserted nowhere:

* ``ledger_fill_invalid_shares:<fill_id>`` (reconcile.py:377-380): a counted fill whose ``shares``
  is not a finite number > 0 (0, negative, or the numeric string ledger_store's looser float()
  reader accepts). The reconciler cannot verify such a fill against the wallet's trades, so it is
  a ledger problem (exit 4) before any request is made.
* ``ledger_short_leg:<token>`` (reconcile.py:398-400): a negative open leg. A paper ledger should
  never be short, and the API comparison for that token would otherwise be meaningless, so the
  token short-circuits the position and the flow checks and stays a mismatch (exit 1) — through
  the CLI too, where it is the usual one global Reconciler halt.
* ``status_invalid_age_seconds`` for a boolean ``age_seconds`` (reconcile.py:177-181): a bool is not
  a measured age (True == 1 in Python), and /v2/status is read first and alone.

Every case is a rejection: none of them may come back as exit 0. Offline: the network is patched
out, the ledger lives in tmp_path.
"""
from __future__ import annotations

import http.client
import json
import socket
import urllib.request
from pathlib import Path

import pytest

from src import ledger_store, podlib, reconcile
from tests.test_reconcile import FakeApi, TA, W, _fill

EMPTY_PAGE = {"data": [], "pagination": {"limit": 500, "offset": 0, "has_more": False, "next_cursor": None}}


def _no_network(*args, **kwargs):
    raise AssertionError("network used in an offline test")


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", _no_network)
    monkeypatch.setattr(socket, "create_connection", _no_network)
    monkeypatch.setattr(urllib.request, "urlopen", _no_network)
    monkeypatch.setattr(podlib, "urlopen", _no_network)
    monkeypatch.setattr(http.client.HTTPConnection, "connect", _no_network)
    monkeypatch.setattr(podlib, "_sleep", lambda s: None)


def empty_api() -> FakeApi:
    """A wallet the API serves nothing for: every local leg is unverifiable."""
    return FakeApi({("positions", None): dict(EMPTY_PAGE), ("trades", None): dict(EMPTY_PAGE)})


@pytest.fixture
def short_ledger(tmp_path) -> Path:
    """One sell with nothing bought before it: the token's leg is short (-10 shares)."""
    path = tmp_path / "paper.jsonl"
    ledger_store.book(path, _fill(TA, "sell", 10.0))
    return path


# ---------------------------------------------------------------- a short ledger leg (reconcile.py:398)


def test_a_short_ledger_leg_is_a_mismatch_never_a_pass(short_ledger):
    report = reconcile.reconcile(W, short_ledger, empty_api())
    assert report.exit_code == reconcile.EXIT_MISMATCH == 1
    assert report.problems == [f"ledger_short_leg:{TA}"]
    assert not report.ok and report.tokens_checked == 1


def test_a_short_leg_is_reported_alone_and_skips_the_api_comparison(short_ledger):
    # The fixture API does serve TA (a 6-share position and its trades): the negative leg must
    # short-circuit both checks instead of being read as a position or a flow mismatch.
    api = FakeApi()
    report = reconcile.reconcile(W, short_ledger, api)
    assert report.exit_code == reconcile.EXIT_MISMATCH
    # Only what the fixture API serves for TA is short-circuited; the other fixture token (TB) is
    # absent from this ledger, so its own problems stay (they are not this branch's business).
    assert [p for p in report.problems if p.endswith(":" + TA)] == [f"ledger_short_leg:{TA}"]
    assert not any(p.startswith(("position_mismatch:" + TA, "trade_flow_mismatch:" + TA))
                   for p in report.problems)
    assert report.tokens_checked == 2


def test_selling_everything_is_flat_not_short(tmp_path):
    path = tmp_path / "paper.jsonl"
    ledger_store.book(path, _fill(TA, "buy", 10.0))
    ledger_store.book(path, _fill(TA, "sell", 10.0))
    _, read = ledger_store.read_ledger(path)
    _, legs, problems = ledger_store.replay(read.rows, read.ids)
    assert problems == [] and legs[ledger_store.leg_key("m-1", TA)]["shares"] == 0.0
    report = reconcile.reconcile(W, path, empty_api())
    assert not any(p.startswith("ledger_short_leg:") for p in report.problems)  # flat is not short
    assert report.exit_code == reconcile.EXIT_MISMATCH  # the wallet's trades are unverifiable: no pass


# ---------------------------------------------------------------- a counted fill without usable shares (reconcile.py:377)


def _legacy_ledger(tmp_path, shares) -> Path:
    """One hand-written legacy fill row (no fill_id: the pre-Task-AF shape the ledger still reads)."""
    row = {"ts": "2026-10-09T00:00:00Z", "market": "m1", "token_id": TA, "side": "buy", "shares": shares,
           "notional_usd": 0.0, "fee_usd": 0.0, "hypothetical": False, "counts_toward_risk": True,
           "risk_decision": "allow"}
    path = tmp_path / "legacy.jsonl"
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    return path


@pytest.mark.parametrize("shares", [0.0, -5.0])
def test_a_counted_fill_without_positive_shares_is_exit_4_and_reads_nothing(tmp_path, shares):
    path = _legacy_ledger(tmp_path, shares)
    api = empty_api()
    report = reconcile.reconcile(W, path, api)
    assert report.exit_code == reconcile.EXIT_LEDGER == 4
    assert len(report.problems) == 1 and report.problems[0].startswith("ledger_fill_invalid_shares:")
    assert api.urls == []  # the ledger is refused before /v2/status or any wallet read
    assert not report.ok


def test_a_numeric_string_share_count_is_not_coerced_by_the_reconciler(tmp_path):
    # ledger_store reads numbers with float(), so "10" is a valid 10-share row there; reconcile's
    # stricter reader refuses it, and an unverifiable fill is a problem, never a pass.
    path = _legacy_ledger(tmp_path, "10")
    _, read = ledger_store.read_ledger(path)
    assert ledger_store.verify(read) == []
    api = empty_api()
    report = reconcile.reconcile(W, path, api)
    assert report.exit_code == reconcile.EXIT_LEDGER
    assert len(report.problems) == 1 and report.problems[0].startswith("ledger_fill_invalid_shares:")
    assert api.urls == []


# ---------------------------------------------------------------- the halt at the process boundary


def test_the_cli_halts_once_on_a_short_leg(short_ledger, tmp_path, capsys):
    bus_path = tmp_path / "bus.jsonl"
    code = reconcile.main(["--user", W, "--ledger", str(short_ledger), "--bus", str(bus_path)],
                          getter=empty_api())
    assert code == reconcile.EXIT_MISMATCH == 1
    (line,) = [json.loads(text) for text in bus_path.read_text(encoding="utf-8").splitlines()]
    assert (line["from"], line["to"], line["type"], line["market"]) == ("Reconciler", "Sentinel", "halt", "*")
    assert line["payload"]["scope"] == "global" and line["payload"]["exit_code"] == 1
    assert line["payload"]["exchange_action"] == "none" and line["payload"]["live_enabled"] is False
    assert f"ledger_short_leg:{TA}" in line["payload"]["problems"]
    assert "reconcile FAIL" in capsys.readouterr().out


def test_a_boolean_status_age_is_not_an_integer(tmp_path):
    # /v2/status is read first and every round: a bool is not a measured age (True == 1 in Python).
    api = FakeApi()
    api.pages[("status", None)]["data"].update(age_seconds=True)
    report = reconcile.reconcile(W, tmp_path / "none.jsonl", api)
    assert report.exit_code == reconcile.EXIT_REMOTE
    assert report.problems == ["status_invalid_age_seconds"]
    assert len(api.urls) == 1  # nothing else was read
