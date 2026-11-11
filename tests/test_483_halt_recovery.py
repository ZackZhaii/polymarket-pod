"""Real file-backed #483 recovery after an injected market-halt persistence failure.
No production files or network; the Operator resume call is only against synthetic test state.
"""
import json
import socket
from datetime import timedelta
import pytest
from src import halt_store, paper_ledger, m2_runner, resolution_watch
from tests import test_runner_settlement as co
from tests import test_342_writeoff_settlement_get_combinations as existing

env = co.env

@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("network prohibited in isolated regression")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)

@pytest.mark.parametrize("persistence", ["state", "fallback", "none", "pending_fallback"])
@pytest.mark.parametrize("entry", ["runner", "direct", "direct_explicit_empty"])
def test_failed_market_halt_then_global_only_resume_still_blocks_settlement(env, monkeypatch, persistence, entry):
    getter = existing._written_off(env)
    original_write = halt_store._atomic_write
    original_fallback = halt_store.write_fallback
    original_record = halt_store.record_halt
    observed = []
    def fail_market_write(path, records):
        if persistence == "pending_fallback" or (persistence != "state" and records and records[-1].get("scope") == halt_store.SCOPE_MARKET):
            raise OSError("injected synthetic market halt write failure")
        return original_write(path, records)
    def selective_fallback(line, fallback=None):
        if "scope=market" in line and persistence in ("none", "pending_fallback"):
            return False
        return original_fallback(line, fallback)
    def track_record(**kwargs):
        result = original_record(**kwargs)
        if kwargs.get("scope") == halt_store.SCOPE_MARKET:
            observed.append(result["persisted"])
        return result
    monkeypatch.setattr(halt_store, "_atomic_write", fail_market_write)
    monkeypatch.setattr(halt_store, "write_fallback", selective_fallback)
    monkeypatch.setattr(halt_store, "record_halt", track_record)
    report = co._tick(env, getter, co.NOW + timedelta(seconds=460))
    expected = "none" if persistence == "pending_fallback" else persistence
    assert observed == [expected], observed
    if expected != "state":
        assert f"writeoff_market_halt_failed:{existing.ID}:{expected}" in report.reasons
    assert halt_store.global_halt_active()
    assert co._settlements(env) == []
    monkeypatch.setattr(halt_store, "_atomic_write", original_write)
    monkeypatch.setattr(halt_store, "write_fallback", original_fallback)
    monkeypatch.setattr(halt_store, "record_halt", original_record)
    for record in halt_store.active_halts():
        if record["scope"] == halt_store.SCOPE_GLOBAL:
            halt_store.resume(from_role="Operator",scope=halt_store.SCOPE_GLOBAL,halt_id=record["id"],
                              approved_by="synthetic fixture",root_cause="injected I/O recovered",postmortem="offline regression",
                              bus_path=env["run"] / "synthetic_resume_bus.jsonl")
    assert not halt_store.global_halt_active()
    getter.row = co.resolved_row()
    now = co.NOW + timedelta(seconds=520)
    env["state"] = m2_runner.RunnerState(paths=m2_runner.RunnerPaths.under(env["run"]))
    if entry == "runner":
        co._tick(env, getter, now)
    else:
        assert resolution_watch.collect(getter, [existing.ID], now, now.isoformat(), env["res"]).ok
        kwargs = {"skip_markets": frozenset()} if entry == "direct_explicit_empty" else {}
        paper_ledger.apply_settlements(now.isoformat(), **kwargs)
    booked = co._settlements(env)
    print("RECOVERY_OBSERVATION", json.dumps({"persistence":persistence,"booked":len(booked),"blocked_markets":sorted(paper_ledger.settlement_blocked_markets())}))
    assert booked == [], "Global-only resume must not discard the previously detected market settlement block"
    # Another market still settles, with no dependence on the runner's in-memory tracker.
    other = "900099"
    co._book(env, [co.fill("buy", 5.0, 0.5, co.NOW.isoformat(), market=other, token="other-no")], prefix="other")
    other_getter = co.Gamma(co.resolved_row(id=other, conditionId="0xother", clobTokenIds='["other-yes", "other-no"]'))
    assert resolution_watch.collect(other_getter, [other], now, now.isoformat(), env["res"]).ok
    out = paper_ledger.apply_settlements(now.isoformat(), skip_markets=frozenset())
    assert [row["market"] for row in out["booked"]] == [other]
    # Only the explicit market resume releases the original event; settlement stays idempotent.
    for record in halt_store.active_halts():
        if record["scope"] == "market" and record["market"] == existing.ID:
            _resume(env, record)
    assert resolution_watch.collect(getter, [existing.ID], now, now.isoformat(), env["res"]).ok
    out = paper_ledger.apply_settlements(now.isoformat(), skip_markets=frozenset())
    assert [row["market"] for row in out["booked"]] == [existing.ID]
    assert paper_ledger.apply_settlements(now.isoformat())["booked"] == []


def _resume(env, record):
    return halt_store.resume(from_role="Operator", scope=record["scope"], halt_id=record["id"],
                             market=record.get("market", ""), approved_by="synthetic fixture",
                             root_cause="injected I/O recovered", postmortem="offline regression",
                             bus_path=env["run"] / "synthetic_resume_bus.jsonl")


def _fallback(env, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(halt_store, "_atomic_write", lambda *a: (_ for _ in ()).throw(OSError("injected")))
        record = halt_store.record_halt(scope="market", market=existing.ID,
                                       reason="closed_flag_contradiction_after_writeoff", source="m2_runner")
    assert record["persisted"] == "fallback"
    return record


def test_fallback_resume_cannot_erase_market_during_shared_read(env, monkeypatch):
    _fallback(env, monkeypatch)
    read_state = halt_store.read_state
    resumed = False
    def resume_between_reads(*args, **kwargs):
        nonlocal resumed
        before = read_state(*args, **kwargs)
        if not resumed:
            resumed = True
            _resume(env, {"id": halt_store.FALLBACK_ID, "scope": "global"})
        return before
    monkeypatch.setattr(halt_store, "read_state", resume_between_reads)
    assert paper_ledger.settlement_blocked_markets() == frozenset({existing.ID})


def test_fallback_archive_cannot_swallow_concurrent_append(env, monkeypatch):
    from threading import Event, Thread
    _fallback(env, monkeypatch)
    appended, started = Event(), Event()
    original = halt_store._atomic_write
    line = f"2026-11-11T01:00:00Z halt id=other-market-event scope=market market=999999 source=m2_runner reason=closed_flag_contradiction_after_writeoff"
    outcomes = []
    threads = []
    def append():
        started.set()
        outcomes.append(halt_store.write_fallback(line))
        appended.set()
    def write_and_race(path, records):
        original(path, records)
        thread = Thread(target=append)
        threads.append(thread)
        thread.start()
        assert started.wait(1)
        appended.wait(0.1)  # old code appends immediately; synchronized code waits for archival
    monkeypatch.setattr(halt_store, "_atomic_write", write_and_race)
    _resume(env, {"id": halt_store.FALLBACK_ID, "scope": "global"})
    for thread in threads:
        thread.join(3)
        assert not thread.is_alive()
    assert outcomes == [True]
    assert paper_ledger.settlement_blocked_markets() == frozenset({existing.ID, "999999"})


def test_failed_recovery_preserves_fallback_and_shared_block(env, monkeypatch):
    _fallback(env, monkeypatch)
    before = halt_store.fallback_path().read_bytes()
    monkeypatch.setattr(halt_store, "_atomic_write", lambda *a: (_ for _ in ()).throw(OSError("still failed")))
    with pytest.raises(halt_store.ResumeRejected):
        _resume(env, {"id": halt_store.FALLBACK_ID, "scope": "global"})
    assert halt_store.fallback_path().read_bytes() == before
    assert paper_ledger.settlement_blocked_markets() == frozenset({existing.ID})


@pytest.mark.parametrize("line", ["broken halt id=x scope=market", "2026-11-11T01:00:00Z halt id=x scope=global market=- source=m2_runner reason=pending_market_halt:{broken"])
def test_malformed_market_evidence_is_not_archived(env, line):
    co._hold_loser(env)
    assert resolution_watch.collect(co.Gamma(co.resolved_row()), [existing.ID], co.NOW,
                                    co.NOW.isoformat(), env["res"]).ok
    assert halt_store.write_fallback(line)
    before = halt_store.fallback_path().read_bytes()
    with pytest.raises(halt_store.ResumeRejected):
        _resume(env, {"id": halt_store.FALLBACK_ID, "scope": "global"})
    assert halt_store.fallback_path().read_bytes() == before
    assert paper_ledger.apply_settlements(co.NOW.isoformat(), skip_markets=frozenset())["problems"]


def test_total_persistence_failure_stops_runner_without_booking(env, monkeypatch):
    getter = existing._written_off(env)
    before = env["paper"].read_bytes()
    monkeypatch.setattr(halt_store, "_atomic_write", lambda *a: (_ for _ in ()).throw(OSError("disk unavailable")))
    monkeypatch.setattr(halt_store, "write_fallback", lambda *a: False)
    report = co._tick(env, getter, co.NOW + timedelta(seconds=460))
    assert report.action == m2_runner.ACT_WRITE_FAILED
    assert f"market_halt_recovery_failed:{existing.ID}" in report.reasons
    assert env["paper"].read_bytes() == before


def test_unreadable_state_cannot_drop_retained_market_at_direct_entry(env, monkeypatch):
    co._hold_loser(env)
    now = co.NOW
    assert resolution_watch.collect(co.Gamma(co.resolved_row()), [existing.ID], now, now.isoformat(), env["res"]).ok
    halt_store.state_path().parent.mkdir(parents=True, exist_ok=True)
    halt_store.state_path().write_text("{corrupt", encoding="utf-8")
    before = env["paper"].read_bytes()
    out = paper_ledger.apply_settlements(now.isoformat(), skip_markets=frozenset())
    assert out["booked"] == [] and out["problems"]
    assert env["paper"].read_bytes() == before


def test_released_same_event_is_not_reactivated_by_unarchived_fallback(env, monkeypatch):
    original = _fallback(env, monkeypatch)
    real_replace = halt_store.os.replace
    def fail_archive(src, dst):
        if str(src) == str(halt_store.fallback_path()):
            raise OSError("injected archive failure")
        return real_replace(src, dst)
    with monkeypatch.context() as patch:
        patch.setattr(halt_store.os, "replace", fail_archive)
        with pytest.raises(halt_store.ResumeRejected):
            _resume(env, {"id": halt_store.FALLBACK_ID, "scope": "global"})
    _resume(env, original)
    assert paper_ledger.settlement_blocked_markets() == frozenset()
    _resume(env, {"id": halt_store.FALLBACK_ID, "scope": "global"})
    assert paper_ledger.settlement_blocked_markets() == frozenset()


def test_unrelated_fallback_retains_previous_global_resume_semantics(env, monkeypatch):
    line = "2026-11-11T01:00:00Z halt id=old-stale-event scope=market market=900099 source=Sentinel reason=stale_book"
    assert halt_store.write_fallback(line)
    _resume(env, {"id": halt_store.FALLBACK_ID, "scope": "global"})
    assert not halt_store.has_active_halt(scope="market", market="900099")


def test_shared_read_waits_for_fallback_line_completion(env, monkeypatch):
    from threading import Event, Thread
    finished = Event()
    results = []
    path = halt_store.fallback_path()
    with halt_store.append_lock(path):
        path.write_text("2026-11-11T01:00:00Z ha", encoding="utf-8")
        def read():
            try:
                results.append(paper_ledger.settlement_blocked_markets())
            finally:
                finished.set()
        thread = Thread(target=read)
        thread.start()
        try:
            assert not finished.wait(0.1), "reader observed a partial fallback append"
        finally:
            path.write_text("2026-11-11T01:00:00Z halt id=pending-write scope=market market=900001 source=m2_runner reason=closed_flag_contradiction_after_writeoff\n", encoding="utf-8")
    thread.join(3)
    assert not thread.is_alive()
    assert results == [frozenset({existing.ID})]


@pytest.mark.parametrize("partial", ["2", "2026-11-11T01:00:00Z ha", "2026-11-11T01:00:00Z halt id=interrupted scope=market market=900001 source=m2_runner reason=closed_flag_contra"])
def test_interrupted_fallback_record_is_not_completed_or_archived(env, partial):
    co._hold_loser(env)
    assert resolution_watch.collect(co.Gamma(co.resolved_row()), [existing.ID], co.NOW,
                                    co.NOW.isoformat(), env["res"]).ok
    path = halt_store.fallback_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(partial, encoding="utf-8")
    before = path.read_bytes()
    assert paper_ledger.apply_settlements(co.NOW.isoformat(), skip_markets=frozenset())["problems"]
    with pytest.raises(halt_store.ResumeRejected):
        _resume(env, {"id": halt_store.FALLBACK_ID, "scope": "global"})
    assert not halt_store.write_fallback("another global halt")
    assert path.read_bytes() == before
