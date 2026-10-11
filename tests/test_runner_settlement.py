"""Issue #206 (Task CO): the M2 runner settles resolved legs on every tick, before the daily-loss breaker.

Before this, ``apply_settlements`` ran only inside ``commit_if_allowed``, so a daily-loss pause (no intent) never
settled a closed leg, and the same loss counted again the next day: a deadlock. Tick order (Poly风控, #197
issuecomment-6106669720): halted -> end; settle; write-off hook (CK); #124 daily loss; RiskGate. A resolutions
collection failure books nothing and is not a halt. Offline: fake Gamma, every file in the test's folder.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest

from src import halt_store, ledger_store, m2_runner, paper_ledger, resolution_watch, settlement
from src.fees import booked_fee_usd, ledger_fee
from tests import m2_runner_rig as rig

NOW = datetime.now(timezone.utc).replace(microsecond=0)
WINNER, LOSER = rig.TOKENS


def fill(side, shares, price, ts, market=rig.MARKET_ID, token=LOSER):
    notional = round(shares * price, 8)
    return dict(ts=ts, market=market, token_id=token, side=side, shares=shares, notional_usd=notional,
                fee_rate=0.0, market_price=price, effective_price=price,
                fee_usd=booked_fee_usd(ledger_fee(0.0, price, price, notional, shares)),
                hypothetical=False, counts_toward_risk=True, risk_decision="allow")


def resolved_row(closed_hours_ago=72.0, **changes):
    closed = NOW - timedelta(hours=closed_hours_ago)
    base = dict(closed=True, acceptingOrders=False, active=True, closedTime=closed.strftime("%Y-%m-%d %H:%M:%S+00"),
                umaResolutionStatus="resolved", outcomePrices='["1", "0"]')
    base.update(changes)
    return rig.gamma_row(NOW, **base)


class Gamma:
    """Gamma for the tick: the keyset page (no candidates) and the /markets?id= reads of held markets."""

    def __init__(self, row=None, *, error=None, empty=False):
        self.row, self.error, self.empty, self.urls = row, error, empty, []

    def __call__(self, url):
        self.urls.append(url)
        parts = urlsplit(url)
        if parts.path.endswith("/keyset"):
            return {"markets": [], "next_cursor": ""}
        if self.error is not None:
            raise self.error
        if self.empty or self.row is None:
            return []
        query = parse_qs(parts.query)
        if query.get("id") != [str(self.row.get("id"))]:
            return []
        want_closed = query.get("closed") == ["true"]
        return [self.row] if (self.row.get("closed") is True) == want_closed else []

    @property
    def id_reads(self):
        return [u for u in self.urls if "id=" in u]


def _no_book(url, now):
    if url == m2_runner.podlib.TIME_URL:
        return now.timestamp()
    raise AssertionError(f"no book read expected: {url}")


@pytest.fixture
def env(tmp_path, monkeypatch):
    paths = rig.isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(paper_ledger, "SETTLEMENT_RESOLUTIONS_PATH", tmp_path / "data" / "resolutions.jsonl")
    paths["res"] = tmp_path / "data" / "resolutions.jsonl"
    paths["paper"].parent.mkdir(parents=True, exist_ok=True)
    paths["run"].mkdir(parents=True, exist_ok=True)
    return {**paths, "state": m2_runner.RunnerState(paths=m2_runner.RunnerPaths.under(paths["run"]))}


def _book(env, rows, prefix="co"):
    for index, row in enumerate(rows):
        ledger_store.book(env["paper"], {"fill_id": f"{prefix}-{index:04d}", **row})


def _hold_loser(env):
    _book(env, [fill("buy", 20.0, 0.5, (NOW - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ"))])


def _tick(env, getter, now=NOW):
    env["state"].monotonic = lambda: now.timestamp()  # wall and monotonic move together (no #120 resume)
    env["state"].clock = lambda: now
    return m2_runner.iteration(env["state"], now=now,
                               getters=m2_runner.Getters(gamma=getter, book=lambda url: _no_book(url, now)))


def _settlements(env):
    return [row for row in ledger_store.read_ledger(env["paper"])[1].rows if ledger_store.is_settlement(row)]


def _events(env, kind):
    path = env["run"] / "events.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    return [row for row in rows if row.get("event") == kind]


def _global_halt():
    return halt_store.has_active_halt(scope=halt_store.SCOPE_GLOBAL, reason_prefix="") is True


# ---------------------------------------------------------------- the deadlock (#206)

def test_settlement_books_during_a_daily_loss_pause(env):
    """Required: the breaker pauses the tick (no intent), and the resolved leg is still settled."""
    earlier = (NOW - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    today = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
    _book(env, [fill("buy", 100.0, 0.5, earlier, market="lossy", token="lx"),
                fill("sell", 100.0, 0.1, today, market="lossy", token="lx")], prefix="loss")  # -40 today >= cap
    _hold_loser(env)
    report = _tick(env, Gamma(resolved_row()))
    assert report.action == m2_runner.ACT_PAUSE and report.reasons[0] == "daily_loss_breaker"
    booked = _settlements(env)
    assert len(booked) == 1 and booked[0]["token_id"] == LOSER and booked[0]["settlement_payout"] == 0.0
    assert settlement.open_legs(ledger_store.read_ledger(env["paper"])[1].rows) == {}
    assert len(_events(env, "settled")) == 1 and not _global_halt()
    # settlement never clears the pause: the next tick is still paused by the breaker, nothing settles twice
    again = _tick(env, Gamma(resolved_row()), NOW + timedelta(seconds=60))
    assert again.reasons[0] == "daily_loss_breaker" and len(_settlements(env)) == 1


def test_without_a_pause_the_tick_settles_before_scanning(env):
    _hold_loser(env)
    getter = Gamma(resolved_row())
    report = _tick(env, getter)
    assert report.action == m2_runner.ACT_RUN and len(_settlements(env)) == 1
    first_scan = next(i for i, u in enumerate(getter.urls) if "/keyset" in u)
    assert all(i < first_scan for i, u in enumerate(getter.urls) if "id=" in u)  # settle reads come first
    assert all(m2_runner.url_allowed(u) for u in getter.urls)


def test_repeated_ticks_do_not_double_book(env):
    _hold_loser(env)
    for seconds in (0, 60, 120, 180):
        _tick(env, Gamma(resolved_row()), NOW + timedelta(seconds=seconds))
    assert len(_settlements(env)) == 1
    history = [json.loads(line) for line in env["res"].read_text().splitlines()]
    assert len(history) == 1 and history[0]["uma_checked"] is True


def test_no_open_leg_reads_nothing(env):
    getter = Gamma(resolved_row())
    _tick(env, getter)
    assert getter.id_reads == [] and _settlements(env) == []


# ---------------------------------------------------------------- never under a halt / KILL / invalid_risk_state

def test_a_hard_halt_runs_nothing(env):
    _hold_loser(env)
    halt_store.record_halt(scope=halt_store.SCOPE_GLOBAL, reason="test_global_halt", source="Sentinel")
    getter = Gamma(resolved_row())
    report = _tick(env, getter)
    assert report.action == m2_runner.ACT_PAUSE and report.reasons[0] == "global_halt"
    assert getter.urls == [] and _settlements(env) == [] and not env["res"].exists()


def test_a_kill_file_runs_nothing(env):
    _hold_loser(env)
    env["state"].paths.kill_file.write_text("stop")
    getter = Gamma(resolved_row())
    assert _tick(env, getter).action == m2_runner.ACT_FINISH
    assert getter.urls == [] and _settlements(env) == []


def test_invalid_risk_state_runs_nothing(env):
    _hold_loser(env)
    with env["paper"].open("a", encoding="utf-8") as handle:
        handle.write("{broken\n")
    getter = Gamma(resolved_row())
    report = _tick(env, getter)
    assert report.reasons[0] == m2_runner.LEDGER_HALT_REASON
    assert getter.urls == [] and not env["res"].exists()


# ---------------------------------------------------------------- collection failure: no booking, no halt

@pytest.mark.parametrize("getter", [
    Gamma(error=TimeoutError()),                       # 503 / timeout
    Gamma(empty=True),                                 # empty [] in both views
    Gamma(resolved_row(closed="true")),                # closed not a JSON boolean
    Gamma(resolved_row(acceptingOrders=None)),         # acceptingOrders not a JSON boolean
], ids=["read_error", "empty_list", "closed_not_bool", "accepting_not_bool"])
def test_a_collection_failure_books_nothing_and_does_not_halt(env, getter):
    _hold_loser(env)
    report = _tick(env, getter)
    assert _settlements(env) == [] and not _global_halt()
    assert report.action == m2_runner.ACT_RUN  # the tick goes on (#124 accounting keeps the worst case)
    assert len(_events(env, "settlement_skipped")) == 1 and not env["res"].exists()
    # next tick retries and books
    _tick(env, Gamma(resolved_row()), NOW + timedelta(seconds=60))
    assert len(_settlements(env)) == 1


def test_an_open_or_hold_pending_market_books_nothing_and_is_not_a_failure(env):
    _hold_loser(env)
    _tick(env, Gamma(rig.gamma_row(NOW)))  # open: only in the default list
    _tick(env, Gamma(resolved_row(closed_hours_ago=1.0)), NOW + timedelta(seconds=60))  # inside the 48 h hold
    assert _settlements(env) == [] and _events(env, "settlement_skipped") == [] and not _global_halt()


def test_a_contradiction_in_the_resolutions_is_a_global_halt(env):
    _hold_loser(env)
    _tick(env, Gamma(resolved_row()))
    assert len(_settlements(env)) == 1
    # a second confirmed snapshot of the same condition with another winner: settlement_conflict
    rows = [json.loads(line) for line in env["res"].read_text().splitlines()]
    other = dict(rows[0], winning_token_id=LOSER, outcome_prices_final=[0.0, 1.0])
    with env["res"].open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(other) + "\n")
    _book(env, [fill("buy", 5.0, 0.5, NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), market="m-other", token="o1")], prefix="o")
    report = _tick(env, Gamma(resolved_row(id="m-other", conditionId="0xother", clobTokenIds='["o1", "o2"]')),
                   NOW + timedelta(seconds=60))
    assert report.reasons[0] == m2_runner.LEDGER_HALT_REASON
    assert any("settlement_conflict" in r for r in report.reasons) and _global_halt()


# ---------------------------------------------------------------- restart in the middle of a tick

def test_a_crash_after_the_ledger_write_before_the_event_neither_double_books_nor_misses(env, monkeypatch):
    _hold_loser(env)
    real_event = m2_runner.event

    def crash_on_settled(state, kind, now, **fields):
        if kind == "settled":
            raise m2_runner.WriteFailed("events.jsonl: crash before the bus line")
        return real_event(state, kind, now, **fields)

    monkeypatch.setattr(m2_runner, "event", crash_on_settled)
    assert _tick(env, Gamma(resolved_row())).action == m2_runner.ACT_WRITE_FAILED
    assert len(_settlements(env)) == 1  # ledger first (fsynced), then the event
    monkeypatch.setattr(m2_runner, "event", real_event)
    env["state"] = m2_runner.RunnerState(paths=m2_runner.RunnerPaths.under(env["run"]))  # restart
    for seconds in (60, 120):
        _tick(env, Gamma(resolved_row()), NOW + timedelta(seconds=seconds))
    assert len(_settlements(env)) == 1 and not _global_halt()


class Crash(Exception):
    pass


def test_a_crash_after_collecting_before_settling_books_on_the_next_tick(env, monkeypatch):
    _hold_loser(env)
    real = paper_ledger.apply_settlements
    monkeypatch.setattr(paper_ledger, "apply_settlements", lambda *a, **k: (_ for _ in ()).throw(Crash()))
    with pytest.raises(Crash):
        _tick(env, Gamma(resolved_row()))
    assert _settlements(env) == [] and env["res"].exists()  # the snapshot is stored, nothing booked yet
    monkeypatch.setattr(paper_ledger, "apply_settlements", real)
    env["state"] = m2_runner.RunnerState(paths=m2_runner.RunnerPaths.under(env["run"]))
    _tick(env, Gamma(resolved_row()), NOW + timedelta(seconds=60))
    assert len(_settlements(env)) == 1
    assert len(env["res"].read_text().splitlines()) == 1  # the history is not appended twice


def test_the_same_settlement_key_with_other_content_is_refused(env):
    _hold_loser(env)
    rows = ledger_store.read_ledger(env["paper"])[1].rows
    snap = {"condition_id": "0xm2alpha", "winning_token_id": WINNER, "outcome_prices_final": [1.0, 0.0],
            "closed_ts": "2026-01-01T00:00:00Z", "snapshot_ts": "2026-01-03T00:00:00Z", "status": "resolved",
            "uma_checked": True, "outcome_token_ids": list(rig.TOKENS)}
    planned = settlement.plan(rows, ["co-0000"], [snap], NOW.strftime("%Y-%m-%dT%H:%M:%SZ")).rows[0]
    ledger_store.book(env["paper"], planned)
    read = ledger_store.read_ledger(env["paper"])[1]
    # same key again: the leg is closed, so the plan books nothing (a no-op)
    assert settlement.plan(read.rows, list(read.ids), [snap], NOW.strftime("%Y-%m-%dT%H:%M:%SZ")).rows == []
    # the same line appended twice is read once (Task AF dedupe): still one settlement, ledger verifies
    with env["paper"].open("ab") as handle:
        handle.write(env["paper"].read_bytes().splitlines(keepends=True)[-1])
    read = ledger_store.read_ledger(env["paper"])[1]
    assert ledger_store.verify(read) == [] and len(_settlements(env)) == 1
    with pytest.raises(ledger_store.LedgerIntegrityError):
        ledger_store.book(env["paper"], dict(planned, note="other content"))


# ---------------------------------------------------------------- end to end: the runner loop calls apply_settlements

def test_the_runner_loop_calls_apply_settlements_without_any_intent(env, monkeypatch):
    _hold_loser(env)
    calls = []
    real = paper_ledger.apply_settlements
    monkeypatch.setattr(paper_ledger, "apply_settlements", lambda *a, **k: calls.append(a) or real(*a, **k))
    fake = rig.FakeGetters(NOW, gamma_row_changes=dict(
        closed=True, acceptingOrders=False, umaResolutionStatus="resolved", outcomePrices='["1", "0"]',
        closedTime=(NOW - timedelta(hours=72)).strftime("%Y-%m-%d %H:%M:%S+00")))

    def gamma(url):  # the rig's Gamma, with the closed=true view of the held market
        fake.gamma_urls.append(url)
        parts = urlsplit(url)
        if parts.path.endswith("/keyset"):
            return {"markets": [], "next_cursor": ""}
        want_closed = parse_qs(parts.query).get("closed") == ["true"]
        return [fake.row] if want_closed else []

    fake.gamma = gamma
    assert rig.run_once(env["run"], fake, clock=lambda: NOW) == m2_runner.EXIT_OK
    assert len(calls) == 1 and len(_settlements(env)) == 1


def test_resolution_watch_reads_only_the_allowlisted_gamma_markets_path():
    getter = Gamma(resolved_row())
    row, failure = resolution_watch.read_market(getter, rig.MARKET_ID)
    assert failure is None and row["closed"] is True
    assert len(getter.urls) == 2 and all(m2_runner.url_allowed(u) for u in getter.urls)
    assert {urlsplit(u).hostname for u in getter.urls} == {urlsplit(m2_runner.podlib.GAMMA_PREFIX).hostname}


# ---------------------------------------------------------------- PR #211 review (Poly风控 issuecomment-6106764972)

def _spy_bus(monkeypatch):
    sent = []
    real_podlib_bus, real_ledger_bus = m2_runner.podlib.bus, paper_ledger.bus
    monkeypatch.setattr(m2_runner.podlib, "bus", lambda *a, **k: sent.append(a) or real_podlib_bus(*a, **k))
    monkeypatch.setattr(paper_ledger, "bus", lambda *a, **k: sent.append(a) or real_ledger_bus(*a, **k))
    return sent


def test_a_market_halt_unrelated_to_settlement_data_still_settles(env):
    """A stale-book market halt does not block that market's settlement."""
    _hold_loser(env)
    halt_store.record_halt(scope=halt_store.SCOPE_MARKET, market=rig.MARKET_ID, reason="stale_book", source="Sentinel")
    _tick(env, Gamma(resolved_row()))
    assert len(_settlements(env)) == 1 and _events(env, "settlement_blocked") == []


@pytest.mark.parametrize("reason", sorted(settlement.SETTLEMENT_BLOCKING_HALT_PREFIXES))
def test_a_market_halt_for_its_own_settlement_data_blocks_that_market_only(env, reason):
    """The market's own close / settlement data contradicts itself: that market does not settle; others do."""
    _hold_loser(env)
    _book(env, [fill("buy", 5.0, 0.5, (NOW - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                     market="m-other", token="o2")], prefix="o")
    halt_store.record_halt(scope=halt_store.SCOPE_MARKET, market=rig.MARKET_ID, reason=f"{reason}:detail",
                           source="RiskGate")
    rows = {rig.MARKET_ID: resolved_row(),
            "m-other": resolved_row(id="m-other", conditionId="0xother", clobTokenIds='["o1", "o2"]')}

    def getter(url):
        parts = urlsplit(url)
        if parts.path.endswith("/keyset"):
            return {"markets": [], "next_cursor": ""}
        query = parse_qs(parts.query)
        assert query.get("id") != [rig.MARKET_ID], "a blocked market is not even collected"
        row = rows.get((query.get("id") or [""])[0])
        return [row] if row is not None and query.get("closed") == ["true"] else []

    _tick(env, getter)
    booked = _settlements(env)
    assert [row["market"] for row in booked] == ["m-other"] and not _global_halt()
    assert _events(env, "settlement_blocked")[0]["markets"] == [rig.MARKET_ID]


def test_blocking_is_decided_by_the_halt_reason():
    assert settlement.blocks_settlement("closed_flag_contradiction_after_writeoff:m1")
    assert settlement.blocks_settlement("invalid_settlement_payout")
    for reason in ("stale_book", "quote_cancelled", "", None, "global_halt", "x_settlement_conflict"):
        assert not settlement.blocks_settlement(reason)


def test_a_blocked_market_with_a_stored_resolution_is_not_booked_by_apply_settlements(env):
    _hold_loser(env)
    _tick(env, Gamma(resolved_row(closed_hours_ago=72.0)), NOW)  # stores the snapshot and settles
    assert len(_settlements(env)) == 1
    # a second leg of the same market under a blocking halt: the stored snapshot is not used for it
    _book(env, [fill("buy", 5.0, 0.5, NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), token=WINNER)], prefix="w")
    out = paper_ledger.apply_settlements(NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), skip_markets=frozenset({rig.MARKET_ID}))
    assert out["booked"] == [] and out["skipped_markets"] == [rig.MARKET_ID] and out["problems"] == []


def test_the_settlement_step_puts_no_intent_order_or_quote_on_the_bus(env, monkeypatch):
    """Settlement only books: no signal (intent), fill (order) or veto on the bus, no quote, no journal step."""
    earlier = (NOW - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    today = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
    _book(env, [fill("buy", 100.0, 0.5, earlier, market="lossy", token="lx"),
                fill("sell", 100.0, 0.1, today, market="lossy", token="lx")], prefix="loss")
    _hold_loser(env)
    rig.deliver(env["run"], NOW)  # a pending forecast exists, but the breaker pauses before any decision
    sent = _spy_bus(monkeypatch)
    report = _tick(env, Gamma(resolved_row()))
    assert report.reasons[0] == "daily_loss_breaker" and len(_settlements(env)) == 1
    assert sent == []
    assert rig.read_jsonl(env["bus"]) == [] and not env["quotes"].exists()
    assert not (env["run"] / "journal.jsonl").exists()


def test_a_settle_only_running_tick_adds_no_bus_message_for_the_settlement(env, monkeypatch):
    _hold_loser(env)
    sent = _spy_bus(monkeypatch)
    _tick(env, Gamma(resolved_row()))  # no forecast in the inbox: only settlement can act
    assert len(_settlements(env)) == 1 and sent == [] and rig.read_jsonl(env["bus"]) == []


def test_the_commit_path_also_skips_a_blocked_market(env):
    """commit_if_allowed's own apply_settlements() (no explicit skip) honours the same blocking halts."""
    _hold_loser(env)
    halt_store.record_halt(scope=halt_store.SCOPE_MARKET, market=rig.MARKET_ID,
                           reason="settlement_result_changed:test", source="RiskGate")
    env["res"].parent.mkdir(parents=True, exist_ok=True)
    snap = {"condition_id": "0xm2alpha", "winning_token_id": WINNER, "outcome_prices_final": [1.0, 0.0],
            "closed_ts": "2026-01-01T00:00:00Z", "snapshot_ts": "2026-01-03T00:00:00Z", "status": "resolved",
            "uma_checked": True, "outcome_token_ids": list(rig.TOKENS)}
    env["res"].write_text(json.dumps(snap) + "\n")
    out = paper_ledger.apply_settlements()
    assert out["booked"] == [] and out["skipped_markets"] == [rig.MARKET_ID]
