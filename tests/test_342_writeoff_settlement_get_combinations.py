"""PR #342 r2: #342's REAL write-off hook against every combination of a settlement GET failure and a write-off
read failure, with an exact read count per tick.

Why this file exists: #345's tests (tests/test_m2_runner_settlement_get_failures.py) count the settlement collector's
reads and failures only, so they run with the write-off hook stubbed (an autouse fixture there). This file restores
the coverage that stub removes: nothing here stubs ``m2_runner._writeoff_hook``.

One tick of a held market that Gamma lists closed makes these id reads, in this order (src/resolution_watch.read_market
reads closed=true first and stops at a failure, then the default view; src/market_close.gamma_read does the same):
    settlement: closed=true, default view   (1 read when the closed=true read fails)
    write-off:  closed=true, default view   (1 read when the closed=true read fails)
so the total is 4 reads, minus one for each phase whose first (closed=true) read fails.

What "fail closed" means for a read failure (#197 rulings, src/market_close): nothing is booked off a read that
failed (no write-off, no settlement), a pending close confirmation is dropped (the next qualifying read starts it
again), the failure is reported as an event, and it is NOT a halt by itself (the leg keeps its #124 worst-case mark).
A contradiction read (the written-off market reads open again after the grace) IS a halt of that market, whether or
not the settlement GET of the same tick failed; a failed write-off read hides it for one tick only, never for good.
No network; every file is in the test's folder.
"""
from __future__ import annotations

import socket
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest

from src import halt_store, ledger_store, settlement
from tests import m2_runner_rig as rig
from tests import test_runner_settlement as co

env = co.env  # the owned settlement paths, the real ledger and the real runner state
ID = rig.MARKET_ID
FAILURES = [None, "closed", "default"]  # which read of the phase fails: none, its closed=true read, its default view
FAILED = f"gamma_read_failed:{TimeoutError.__name__}"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("network attempted")
    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


class Scripted(co.Gamma):
    """co.Gamma whose id reads fail by position within one tick (``plan``), recording every id read in order."""

    def __init__(self, row):
        super().__init__(row)
        self.fails: set[int] = set()
        self.tick_reads: list[str] = []

    def plan(self, settle: str | None, writeoff: str | None) -> int:
        """Arm the next tick; returns the exact number of id reads it must make."""
        s_len = 1 if settle == "closed" else 2
        w_len = 1 if writeoff == "closed" else 2
        self.fails = ({0} if settle == "closed" else {1} if settle == "default" else set())
        if writeoff == "closed":
            self.fails.add(s_len)
        elif writeoff == "default":
            self.fails.add(s_len + 1)
        self.tick_reads = []
        return s_len + w_len

    def __call__(self, url):
        query = parse_qs(urlsplit(url).query)
        if "id" in query:
            index = len(self.tick_reads)
            self.tick_reads.append("closed" if query.get("closed") == ["true"] else "default")
            if index in self.fails:
                self.urls.append(url)
                raise TimeoutError("offline")
        return super().__call__(url)


def _setup(env):
    co._hold_loser(env)
    return Scripted(co.resolved_row(closed_hours_ago=1.0))  # closed 1 h ago: settlement is on classification hold


def _tick(env, getter, seconds, settle=None, writeoff=None):
    expected = getter.plan(settle, writeoff)
    report = co._tick(env, getter, co.NOW + timedelta(seconds=seconds))
    assert len(getter.tick_reads) == expected, (getter.tick_reads, expected)  # the exact read count of this tick
    return report


def _writeoffs(env):
    return [row for row in ledger_store.read_ledger(env["paper"])[1].rows if ledger_store.is_writeoff(row)]


def _no_halt():
    return halt_store.active_halts() == []


def _skipped(env, kind):
    return co._events(env, kind)


# ------------------------------------------------------------------ (A) the confirming tick

@pytest.mark.parametrize("writeoff", FAILURES)
@pytest.mark.parametrize("settle", FAILURES)
def test_a_confirming_tick_with_every_combination_of_read_failures(env, settle, writeoff):
    getter = _setup(env)
    _tick(env, getter, 0)  # first closed read: pending, nothing booked, 4 reads
    assert len(getter.tick_reads) == 4 and _writeoffs(env) == [] and _no_halt()
    before = env["paper"].read_bytes()
    skipped_before = len(_skipped(env, "settlement_skipped"))
    close_skipped_before = len(_skipped(env, "close_read_skipped"))

    _tick(env, getter, 60, settle, writeoff)  # the tick that would confirm and write off

    assert _no_halt() and not co._global_halt()  # a read failure is never a halt by itself
    assert co._settlements(env) == []  # the market is on classification hold: nothing settles in any combination
    skipped = _skipped(env, "settlement_skipped")[skipped_before:]
    if settle is None:
        assert skipped == []
    else:  # the settlement GET failure is reported once, with the market and the original exception name
        assert len(skipped) == 1 and skipped[0]["reasons"] == [f"{ID}:{FAILED}"]
        assert not env["res"].exists()
    close_skipped = _skipped(env, "close_read_skipped")[close_skipped_before:]
    if writeoff is None:  # the write-off read worked: confirmed and written off once, whatever the settlement GET did
        assert close_skipped == [] and len(_writeoffs(env)) == 1
        assert env["paper"].read_bytes() != before
    else:  # fail closed: nothing booked off a failed read, ledger byte for byte unchanged, failure reported
        assert len(close_skipped) == 1 and close_skipped[0]["markets"] == {ID: FAILED}
        assert _writeoffs(env) == [] and env["paper"].read_bytes() == before
        assert ID not in env["state"].close_tracker.first  # the pending confirmation was dropped


@pytest.mark.parametrize("writeoff", ["closed", "default"])
@pytest.mark.parametrize("settle", FAILURES)
def test_a_failed_writeoff_read_restarts_the_confirmation_it_never_confirms_later(env, settle, writeoff):
    """After a failed write-off read the next good tick only starts a new pending read (4 reads, nothing booked);
    the write-off comes one more good tick later."""
    getter = _setup(env)
    _tick(env, getter, 0)
    _tick(env, getter, 60, settle, writeoff)
    before = env["paper"].read_bytes()
    _tick(env, getter, 120)  # good reads: starts a new pending, books nothing
    assert len(getter.tick_reads) == 4 and _writeoffs(env) == [] and env["paper"].read_bytes() == before
    _tick(env, getter, 180)  # >= 30 s later and good: confirmed
    assert len(getter.tick_reads) == 4 and len(_writeoffs(env)) == 1 and _no_halt()


# ------------------------------------------------------------------ (B) a written-off market reads open again

def _written_off(env):
    getter = _setup(env)
    _tick(env, getter, 0)
    _tick(env, getter, 60)
    assert len(_writeoffs(env)) == 1 and _no_halt()
    getter.row = co.resolved_row(closed_hours_ago=1.0, closed=False, acceptingOrders=True)  # Gamma reopens it
    return getter


def _market_halts():
    return [h for h in halt_store.active_halts() if h.get("scope") == halt_store.SCOPE_MARKET]


@pytest.mark.parametrize("writeoff", FAILURES)
@pytest.mark.parametrize("settle", FAILURES)
def test_b_a_reopen_after_the_grace_with_every_combination_of_read_failures(env, settle, writeoff):
    getter = _written_off(env)
    before = env["paper"].read_bytes()
    skipped_before = len(_skipped(env, "close_read_skipped"))

    _tick(env, getter, 460, settle, writeoff)  # 400 s after the confirming read: past the 300 s grace

    assert env["paper"].read_bytes() == before and len(_writeoffs(env)) == 1  # never rolled back, never repeated
    if writeoff is None:  # the contradiction is seen: that market halts (even when the settlement GET failed)
        halts = _market_halts()
        assert [h["market"] for h in halts] == [ID], halts
        assert halts[0]["reason"].startswith("closed_flag_contradiction_after_writeoff")
        assert settlement.blocks_settlement(halts[0]["reason"])  # its settlement stays blocked
        assert co._settlements(env) == []
    else:  # the failed read hides it for this tick only: reported, no halt, no change
        assert _no_halt() and co._settlements(env) == []
        new = _skipped(env, "close_read_skipped")[skipped_before:]
        assert len(new) == 1 and new[0]["markets"] == {ID: FAILED}


@pytest.mark.parametrize("writeoff", ["closed", "default"])
def test_b_a_contradiction_hidden_by_a_failed_read_is_halted_on_the_next_good_read(env, writeoff):
    getter = _written_off(env)
    _tick(env, getter, 460, None, writeoff)
    assert _no_halt()
    _tick(env, getter, 520)  # good reads again: 4 reads, the contradiction is still there
    assert len(getter.tick_reads) == 4
    assert [h["market"] for h in _market_halts()] == [ID]
