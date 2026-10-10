"""Task AL: the halt consumer (Sentinel design gaps 1, 3, 7, 8, 10). Offline, paper only.

Every halt is persisted (src/halt_store.py) and read by RiskGate, the Maker and paper_cycle. A bad
halt state file or logs/HALT is a global halt. Only an Operator resume (approved_by + halt_ref)
lifts a halt; a market resume never lifts a global one. conftest.py points every halt path at the
test's own folder.
"""
from __future__ import annotations

import json
import os
import threading

import pytest

import src.halt_store as halt_store
import src.maker as maker
import src.paper_cycle as paper_cycle
import src.paper_ledger as paper_ledger
import src.podlib as podlib
import src.sentinel as sentinel
from src.podlib import RISK_PATH, load_risk, read_jsonl, utc_now
from src.risk_gate import evaluate

GOOD_STATE = {"open_positions": 0, "exposure_usd": 0.0, "leg_exposure_usd": 0.0, "daily_loss_usd": 0.0, "recent_fills": [], "own_quotes": []}


def _intent(market="m1", **kw):
    return paper_ledger._intent(market=market, model_prob=0.70, market_price=0.40, book_ts=utc_now(), **kw)


def _veto(market="m1"):
    return evaluate(_intent(market), load_risk(), dict(GOOD_STATE)).reasons


def _resume(halt_id, scope, tmp_path, **kw):
    args = {"from_role": "Operator", "scope": scope, "halt_id": halt_id, "approved_by": "家伟",
            "root_cause": "test", "postmortem": "PR #test", "bus_path": tmp_path / "bus.jsonl"}
    args.update(kw)
    return halt_store.resume(**args)


# ---------------------------------------------------------------- isolation and defaults

def test_default_paths_are_under_logs_and_tests_are_isolated():
    assert halt_store.HALT_STATE_PATH != podlib.LOG_DIR / "halt_state.json"  # conftest
    assert halt_store.HALT_FALLBACK_PATH != podlib.LOG_DIR / "HALT"
    assert halt_store.state_path() == halt_store.HALT_STATE_PATH


def test_no_halt_means_no_veto_reason():
    assert halt_store.active_halts() == []
    assert halt_store.halt_problems("m1") == []
    assert _veto() == []


# ---------------------------------------------------------------- gap 1: consumers read the halt

def test_market_halt_vetoes_that_market_only(tmp_path):
    msg = sentinel.halt(reason="drill", market="m1", bus_path=tmp_path / "bus.jsonl",
                        quotes_path=tmp_path / "q.jsonl")
    hid = msg["payload"]["halt_id"]
    assert msg["payload"]["scope"] == "market" and hid.startswith("H0001-")
    assert _veto("m1") == [f"halt_market:{hid}"]
    assert _veto("m2") == []


def test_global_halt_vetoes_every_market(tmp_path):
    msg = sentinel.halt(reason="drill", bus_path=tmp_path / "bus.jsonl", quotes_path=tmp_path / "q.jsonl")
    assert msg["payload"]["scope"] == "global"
    for market in ("m1", "m2"):
        assert _veto(market) == [f"halt_global:{msg['payload']['halt_id']}"]


def test_a_halt_survives_a_fresh_book_and_a_new_process(tmp_path):
    sentinel.halt(reason="drill", market="m1", bus_path=tmp_path / "bus.jsonl", quotes_path=tmp_path / "q.jsonl")
    data = json.loads(halt_store.HALT_STATE_PATH.read_text(encoding="utf-8"))  # what a new process reads
    assert data["version"] == 1 and data["halts"][0]["active"] is True
    assert _veto("m1")  # book_ts is fresh (now); the halt alone vetoes


def test_maker_refuses_a_halted_market(tmp_path, fresh_book):
    fresh_book("m1", "t")
    fresh_book("m2", "t")
    sentinel.halt(reason="drill", market="m1", bus_path=tmp_path / "bus.jsonl", quotes_path=tmp_path / "q.jsonl")
    with pytest.raises(maker.HaltActive):
        maker.quote(market="m1", token_id="t", side="buy", price=0.4, size=5,
                    bus_path=tmp_path / "bus.jsonl", quotes_path=tmp_path / "q.jsonl")
    assert read_jsonl(tmp_path / "q.jsonl") == []
    maker.quote(market="m2", token_id="t", side="buy", price=0.4, size=5,
                bus_path=tmp_path / "bus.jsonl", quotes_path=tmp_path / "q.jsonl")
    assert [q["market"] for q in read_jsonl(tmp_path / "q.jsonl")] == ["m2"]


def test_maker_cancels_its_own_quote_when_a_halt_lands_during_the_append(tmp_path, monkeypatch, fresh_book):
    fresh_book("m1", "t")
    quotes = tmp_path / "q.jsonl"
    real_append = maker.append_jsonl

    def append_then_halt(path, row):
        real_append(path, row)
        halt_store.record_halt(scope="market", market="m1", reason="raced", source="Sentinel")

    monkeypatch.setattr(maker, "append_jsonl", append_then_halt)
    with pytest.raises(maker.HaltActive):
        maker.quote(market="m1", token_id="t", side="buy", price=0.4, size=5, bus_path=tmp_path / "bus.jsonl",
                    quotes_path=quotes)
    assert [q["status"] for q in read_jsonl(quotes)] == ["cancelled"]
    assert not (tmp_path / "bus.jsonl").exists()  # no quote signal was announced


def _cycle_files(tmp_path):
    markets, books = tmp_path / "markets.jsonl", tmp_path / "books.jsonl"
    rows = [{"ts": "2026-10-09T00:00:00Z", "id": m, "token_ids": [f"{m}a", f"{m}b"]} for m in ("m1", "m2")]
    markets.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    brows = [{"ts": "2026-10-09T00:09:00Z", "book_ts": "2026-10-09T00:09:55Z", "market_id": m, "token_id": t,
              "best_ask": 0.40, "best_ask_size": 10} for m in ("m1", "m2") for t in (f"{m}a", f"{m}b")]
    books.write_text("".join(json.dumps(r) + "\n" for r in brows), encoding="utf-8")
    return markets, books


def _cycle(tmp_path):
    markets, books = _cycle_files(tmp_path)
    return paper_cycle.run(markets_path=markets, books_path=books, bus_path=tmp_path / "bus.jsonl",
                           now="2026-10-09T00:10:00Z", sentinel_state_path=tmp_path / "s.json",
                           quotes_path=tmp_path / "q.jsonl")


def test_paper_cycle_skips_gap_signals_of_a_halted_market(tmp_path):
    assert _cycle(tmp_path)["gaps"] == 2
    halt_store.record_halt(scope="market", market="m1", reason="drill", source="Sentinel")
    result = _cycle(tmp_path)
    assert result["gaps"] == 1 and result["halted_markets"] == ["m1"]
    assert [s["kind"] for s in result["skips"]] == ["halted_market"]


def test_paper_cycle_under_a_corrupt_halt_file_emits_no_gap(tmp_path):
    halt_store.HALT_STATE_PATH.write_text("{oops", encoding="utf-8")
    result = _cycle(tmp_path)
    assert result["gaps"] == 0 and result["halted_markets"] == ["m1", "m2"]
    assert result["skips"][0]["halt_reasons"] == [halt_store.STATE_CORRUPT]


# ---------------------------------------------------------------- fail closed: bad file = global halt

@pytest.mark.parametrize(
    ("content", "problem"),
    [
        (b"{not json", halt_store.STATE_CORRUPT),
        (b"", halt_store.STATE_CORRUPT),
        (b"\xff\xfe garbage", halt_store.STATE_UNREADABLE),
        (b"[]", halt_store.STATE_MALFORMED),
        (b'{"version": 2, "halts": []}', halt_store.STATE_MALFORMED),
        (b'{"version": 1, "halts": [{"id": "x"}]}', halt_store.STATE_MALFORMED),
        (b'{"version": 1, "halts": [{"id": "x", "scope": "market", "market": "", "active": true, '
         b'"reason": "r", "source": "s", "ts": "t"}]}', halt_store.STATE_MALFORMED),
        (b'{"version": 1, "halts": [{"id": "x", "scope": "global", "market": "", "active": false, '
         b'"reason": "r", "source": "s", "ts": "t"}]}', halt_store.STATE_MALFORMED),  # released without record
    ],
)
def test_a_bad_halt_file_is_a_global_halt(content, problem):
    halt_store.HALT_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    halt_store.HALT_STATE_PATH.write_bytes(content)
    assert halt_store.halt_problems("any") == [problem]
    assert _veto("any") == [problem]


def test_a_lost_halt_file_after_a_save_is_a_global_halt():
    halt_store.record_halt(scope="market", market="m1", reason="r", source="Sentinel")
    halt_store.HALT_STATE_PATH.unlink()
    assert _veto("m2") == [halt_store.STATE_MISSING]


def test_an_unreadable_halt_path_is_a_global_halt():
    halt_store.HALT_STATE_PATH.mkdir(parents=True)
    assert _veto("m1") == [halt_store.STATE_UNREADABLE]


def test_the_fallback_file_is_a_global_halt():
    assert halt_store.write_fallback("bus down")
    assert _veto("m9") == [halt_store.FALLBACK_ACTIVE]


def test_a_broken_check_is_a_halt(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("bug")

    monkeypatch.setattr(halt_store, "active_halts", boom)
    assert halt_store.halt_problems("m1") == [halt_store.CHECK_FAILED]
    assert _veto("m1") == [halt_store.CHECK_FAILED]


def test_record_halt_keeps_a_corrupt_file_as_evidence_and_writes_the_fallback():
    halt_store.HALT_STATE_PATH.write_text("{bad", encoding="utf-8")
    record = halt_store.record_halt(scope="market", market="m1", reason="r", source="Sentinel")
    assert record["persisted"] == halt_store.PERSISTED_FALLBACK
    assert halt_store.HALT_STATE_PATH.read_text(encoding="utf-8") == "{bad"
    assert "scope=market market=m1" in halt_store.HALT_FALLBACK_PATH.read_text(encoding="utf-8")


def test_record_halt_dedupes_an_identical_active_halt():
    first = halt_store.record_halt(scope="market", market="m1", reason="r", source="Sentinel")
    again = halt_store.record_halt(scope="market", market="m1", reason="r", source="Sentinel")
    assert again["id"] == first["id"] and again["duplicate"] is True
    assert len(json.loads(halt_store.HALT_STATE_PATH.read_text(encoding="utf-8"))["halts"]) == 1


def test_record_halt_rejects_bad_input():
    with pytest.raises(ValueError):
        halt_store.record_halt(scope="planet", reason="r", source="Sentinel")
    with pytest.raises(ValueError):
        halt_store.record_halt(scope="market", market="", reason="r", source="Sentinel")
    with pytest.raises(ValueError):
        halt_store.record_halt(scope="global", reason=" ", source="Sentinel")


# ---------------------------------------------------------------- gap 8: halt write order

def test_halt_writes_the_record_before_cancelling_and_the_bus(tmp_path, monkeypatch):
    order = []
    real_record, real_cancel = halt_store.record_halt, sentinel.cancel_open_quotes
    monkeypatch.setattr(halt_store, "record_halt", lambda **k: order.append("record") or real_record(**k))
    monkeypatch.setattr(sentinel, "cancel_open_quotes", lambda *a: order.append("cancel") or real_cancel(*a))
    monkeypatch.setattr(sentinel, "bus", lambda *a, **k: order.append("bus") or podlib.bus(*a, **k))
    sentinel.halt(reason="drill", market="m1", bus_path=tmp_path / "bus.jsonl", quotes_path=tmp_path / "q.jsonl")
    assert order == ["record", "cancel", "bus"]


def test_a_failed_bus_write_writes_logs_halt_and_does_not_raise(tmp_path, monkeypatch):
    def broken_bus(*a, **k):
        raise OSError("bus disk full")

    monkeypatch.setattr(sentinel, "bus", broken_bus)
    msg = sentinel.halt(reason="drill", market="m1", bus_path=tmp_path / "bus.jsonl",
                        quotes_path=tmp_path / "q.jsonl")
    assert msg["failures"] == ["bus_write_failed:OSError"]
    assert halt_store.HALT_FALLBACK_PATH.exists()
    assert _veto("m2") == [halt_store.FALLBACK_ACTIVE]  # bus failure escalates to a global halt
    assert any(r.startswith("halt_market:") for r in _veto("m1"))  # and the record was written first


def test_a_failed_cancel_writes_logs_halt(tmp_path, monkeypatch):
    quotes = tmp_path / "q.jsonl"
    quotes.write_text(json.dumps({"market": "m1", "status": "open"}) + "\n", encoding="utf-8")
    real_replace = os.replace

    def broken(src, dst):
        if os.fspath(dst) == os.fspath(quotes):
            raise OSError("locked")
        return real_replace(src, dst)

    monkeypatch.setattr(sentinel.os, "replace", broken)
    msg = sentinel.halt(reason="drill", market="m1", bus_path=tmp_path / "bus.jsonl", quotes_path=quotes)
    assert msg["failures"] == ["cancel_failed:OSError"]
    assert halt_store.HALT_FALLBACK_PATH.exists()
    assert read_jsonl(quotes)[0]["status"] == "open"  # never a torn file
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]


def test_cancel_is_an_atomic_rewrite(tmp_path, monkeypatch):
    quotes = tmp_path / "q.jsonl"
    quotes.write_text("".join(json.dumps({"market": m, "status": "open"}) + "\n" for m in ("m1", "m2")),
                      encoding="utf-8")
    calls = []
    real_replace = os.replace
    monkeypatch.setattr(sentinel.os, "replace", lambda s, d: calls.append(os.fspath(d)) or real_replace(s, d))
    assert sentinel.cancel_open_quotes(quotes, "2026-10-09T00:00:00Z", "m1") == 1
    assert os.fspath(quotes) in calls
    assert [q["status"] for q in read_jsonl(quotes)] == ["cancelled", "open"]


def test_a_global_halt_cancels_every_open_quote(tmp_path):
    quotes = tmp_path / "q.jsonl"
    quotes.write_text("".join(json.dumps({"market": m, "status": "open"}) + "\n" for m in ("m1", "m2")),
                      encoding="utf-8")
    msg = sentinel.halt(reason="drill", bus_path=tmp_path / "bus.jsonl", quotes_path=quotes)
    assert msg["payload"]["paper_quotes_cancelled"] == 2


# ---------------------------------------------------------------- gap 7: escalation

def _ledger_paths(tmp_path, monkeypatch):
    paper = tmp_path / "paper.jsonl"
    monkeypatch.setattr(paper_ledger, "PAPER_PATH", paper)
    monkeypatch.setattr(paper_ledger, "QUOTES_PATH", tmp_path / "quotes.jsonl")
    sent = []
    monkeypatch.setattr(paper_ledger, "bus", lambda *a, **k: sent.append(a) or {"type": a[2], "payload": a[4]})
    return paper, sent


def test_invalid_risk_state_is_a_global_halt(tmp_path, monkeypatch):
    paper, sent = _ledger_paths(tmp_path, monkeypatch)
    paper.write_text('{"fill_id": "torn", "ts": "2026-10-0', encoding="utf-8")
    result = paper_ledger.commit_if_allowed(_intent("m1"), load_risk())
    assert result["allow"] is False and result["halted"] is True and result["halt_reason"] == "invalid_risk_state"
    assert [a[2] for a in sent] == ["veto", "halt"]
    assert sent[-1][4]["scope"] == "global" and sent[-1][0] == "Sentinel"
    paper.unlink()  # the ledger is "fixed", but the halt stays until the Operator resumes it
    later = paper_ledger.commit_if_allowed(_intent("m2"), load_risk())
    assert later["allow"] is False and any(r.startswith("halt_global:") for r in later["decision"]["reasons"])
    _resume(halt_store.active_halts()[0]["id"], "global", tmp_path)
    assert paper_ledger.commit_if_allowed(_intent("m2"), load_risk())["allow"] is True


def test_executor_ledger_halt_is_a_global_halt(tmp_path, monkeypatch):
    paper, sent = _ledger_paths(tmp_path, monkeypatch)

    def boom(*a, **k):
        raise OSError("disk")

    monkeypatch.setattr(paper_ledger.ledger_store, "book", boom)
    result = paper_ledger.commit_if_allowed(_intent("m1"), load_risk())
    assert result["halted"] and result["halt_reason"] == "ledger_write_failed"
    assert sent[-1][0] == "Executor" and sent[-1][2] == "halt" and sent[-1][4]["scope"] == "global"
    assert [h["scope"] for h in halt_store.active_halts()] == ["global"]


# ---------------------------------------------------------------- gap 3: resume

def test_only_the_operator_may_resume(tmp_path):
    rec = halt_store.record_halt(scope="market", market="m1", reason="r", source="Sentinel")
    for role in ("Sentinel", "Executor", "RiskGate", "Poly指挥", ""):
        with pytest.raises(halt_store.ResumeRejected) as info:
            _resume(rec["id"], "market", tmp_path, from_role=role)
        assert info.value.reason == "resume_not_operator"
    assert len(halt_store.active_halts()) == 1
    assert not (tmp_path / "bus.jsonl").exists()


@pytest.mark.parametrize("field", ["approved_by", "root_cause", "postmortem"])
def test_resume_needs_approval_cause_and_postmortem(tmp_path, field):
    rec = halt_store.record_halt(scope="market", market="m1", reason="r", source="Sentinel")
    with pytest.raises(halt_store.ResumeRejected) as info:
        _resume(rec["id"], "market", tmp_path, **{field: "  "})
    assert info.value.reason == f"missing_{field}"
    assert len(halt_store.active_halts()) == 1


def test_a_market_resume_cannot_lift_a_global_halt(tmp_path):
    glob = halt_store.record_halt(scope="global", reason="g", source="Sentinel")
    with pytest.raises(halt_store.ResumeRejected) as info:
        _resume(glob["id"], "market", tmp_path, market="m1")
    assert info.value.reason == "market_resume_cannot_lift_global"
    for special in (halt_store.FALLBACK_ID, halt_store.STATE_PROBLEM_ID):
        with pytest.raises(halt_store.ResumeRejected):
            _resume(special, "market", tmp_path)
    local = halt_store.record_halt(scope="market", market="m1", reason="m", source="Sentinel")
    _resume(local["id"], "market", tmp_path, market="m1")
    assert _veto("m1") == [f"halt_global:{glob['id']}"]  # the global halt still blocks m1


def test_a_global_resume_does_not_lift_a_market_halt(tmp_path):
    local = halt_store.record_halt(scope="market", market="m1", reason="m", source="Sentinel")
    with pytest.raises(halt_store.ResumeRejected) as info:
        _resume(local["id"], "global", tmp_path)
    assert info.value.reason == "scope_mismatch"


def test_resume_needs_an_active_halt_ref(tmp_path):
    with pytest.raises(halt_store.ResumeRejected) as info:
        _resume("H9999-nope", "global", tmp_path)
    assert info.value.reason == "halt_ref_not_active"
    rec = halt_store.record_halt(scope="market", market="m1", reason="r", source="Sentinel")
    _resume(rec["id"], "market", tmp_path)
    with pytest.raises(halt_store.ResumeRejected):
        _resume(rec["id"], "market", tmp_path)  # already released
    with pytest.raises(halt_store.ResumeRejected) as info:
        rec2 = halt_store.record_halt(scope="market", market="m2", reason="r", source="Sentinel")
        _resume(rec2["id"], "market", tmp_path, market="m3")
    assert info.value.reason == "market_mismatch"


def test_a_resume_is_logged_on_the_bus_before_release(tmp_path, monkeypatch):
    rec = halt_store.record_halt(scope="market", market="m1", reason="r", source="Sentinel")

    def broken_bus(*a, **k):
        raise OSError("bus down")

    with monkeypatch.context() as patch:
        patch.setattr(halt_store, "bus", broken_bus)
        with pytest.raises(OSError):
            _resume(rec["id"], "market", tmp_path)
    assert halt_store.halt_problems("m1") == [f"halt_market:{rec['id']}"]  # nothing released
    result = _resume(rec["id"], "market", tmp_path, checks={"guard": "exit 0"})
    line = read_jsonl(tmp_path / "bus.jsonl")[-1]
    assert line["from"] == "Operator" and line["to"] == "Sentinel" and line["type"] == "resume"
    assert line["payload"]["approved_by"] == "家伟" and line["payload"]["halt_ref"]["id"] == rec["id"]
    assert line["payload"]["checks"] == {"guard": "exit 0"} and line["payload"]["exchange_action"] == "none"
    assert result["released"] == rec["id"] and halt_store.halt_problems("m1") == []
    saved = json.loads(halt_store.HALT_STATE_PATH.read_text(encoding="utf-8"))["halts"][0]
    assert saved["active"] is False and saved["resolved"]["approved_by"] == "家伟"


def test_resume_of_the_fallback_file_keeps_it_as_evidence(tmp_path):
    halt_store.write_fallback("bus down")
    _resume(halt_store.FALLBACK_ID, "global", tmp_path)
    assert not halt_store.HALT_FALLBACK_PATH.exists()
    assert [p.name.startswith("HALT.resolved-") for p in halt_store.HALT_FALLBACK_PATH.parent.iterdir()
            if p.name.startswith("HALT")] == [True]
    assert _veto("m1") == []


def test_resume_of_a_corrupt_state_moves_it_aside(tmp_path):
    halt_store.HALT_STATE_PATH.write_text("{bad", encoding="utf-8")
    with pytest.raises(halt_store.ResumeRejected) as info:
        _resume("H0001-x", "global", tmp_path)
    assert info.value.reason == "halt_state_problem"
    _resume(halt_store.STATE_PROBLEM_ID, "global", tmp_path)
    assert _veto("m1") == []
    bad = [p for p in halt_store.HALT_STATE_PATH.parent.iterdir() if ".bad-" in p.name]
    assert len(bad) == 1 and bad[0].read_text(encoding="utf-8") == "{bad"


# ---------------------------------------------------------------- issue #52: concurrent read-modify-write

def test_a_global_halt_added_during_a_market_resume_is_not_overwritten(tmp_path, monkeypatch):
    """The issue #52 regression, deterministic: another writer's record_halt lands at the resume's
    bus step (between the resume's read and its write-back). The resume re-reads under the shared
    mutex and releases only its own id: the new global halt survives and still blocks other markets."""
    old = halt_store.record_halt(scope="market", market="m1", reason="stale_book", source="Sentinel")
    added = {}
    real_bus = podlib.bus

    def bus_then_halt(*args, **kwargs):
        added["record"] = halt_store.record_halt(scope="global", reason="ledger_integrity", source="Executor")
        return real_bus(*args, **kwargs)

    monkeypatch.setattr(halt_store, "bus", bus_then_halt)
    result = _resume(old["id"], "market", tmp_path, market="m1")
    assert result["released"] == old["id"]
    assert added["record"]["persisted"] == halt_store.PERSISTED_STATE
    rows = json.loads(halt_store.HALT_STATE_PATH.read_text(encoding="utf-8"))["halts"]
    assert [(r["scope"], r["reason"], r["active"]) for r in rows] == [
        ("market", "stale_book", False), ("global", "ledger_integrity", True)]
    assert halt_store.halt_problems("m1") == [f"halt_global:{added['record']['id']}"]  # only its own id released
    assert halt_store.halt_problems("m2") == [f"halt_global:{added['record']['id']}"]  # other market still blocked


def test_two_concurrent_record_halt_calls_keep_both_records(monkeypatch):
    """Two writers racing (issue #52): the second record_halt starts while the first is between
    its read and its write-back. The shared mutex serialises them and both records survive."""
    real_write, real_lock = halt_store._atomic_write, halt_store.StateLock
    holder: dict[str, threading.Thread] = {}
    second_started, second = threading.Event(), {}

    class SignallingLock(real_lock):
        def acquire(self):
            if threading.current_thread() is holder.get("second"):
                second_started.set()
            return super().acquire()

    def second_record():
        second["record"] = halt_store.record_halt(scope="global", reason="raced_global", source="Executor")

    def write_after_race(target, records):
        if "second" not in holder:
            holder["second"] = threading.Thread(target=second_record, name="second-record")
            holder["second"].start()
            assert second_started.wait(10)  # the other writer is at the mutex (or past it, unlocked)
        return real_write(target, records)

    monkeypatch.setattr(halt_store, "StateLock", SignallingLock)
    monkeypatch.setattr(halt_store, "_atomic_write", write_after_race)
    first = halt_store.record_halt(scope="market", market="m1", reason="raced_market", source="Sentinel")
    holder["second"].join(10)
    assert first["persisted"] == halt_store.PERSISTED_STATE and second["record"]["persisted"] == halt_store.PERSISTED_STATE
    rows = json.loads(halt_store.HALT_STATE_PATH.read_text(encoding="utf-8"))["halts"]
    assert [(r["scope"], r["reason"], r["active"]) for r in rows] == [
        ("market", "raced_market", True), ("global", "raced_global", True)]  # final records are complete
    assert halt_store.halt_problems("m2") == [f"halt_global:{second['record']['id']}"]  # other market still blocked


def test_a_resume_refuses_to_release_when_the_state_mutex_is_held(tmp_path, monkeypatch):
    """issue #52: when the mutex cannot be taken the resume is refused and the halt stays in force."""
    rec = halt_store.record_halt(scope="market", market="m1", reason="r", source="Sentinel")
    monkeypatch.setattr(halt_store, "HALT_STATE_LOCK_TIMEOUT_S", 0.1)
    held = halt_store.StateLock(halt_store.state_path())
    assert held.acquire()
    try:
        with pytest.raises(halt_store.ResumeRejected) as info:
            _resume(rec["id"], "market", tmp_path, market="m1")
        assert info.value.reason == halt_store.LOCK_FAILED
    finally:
        held.release()
    assert halt_store.halt_problems("m1") == [f"halt_market:{rec['id']}"]  # still halted, nothing released


def test_record_halt_falls_back_to_logs_halt_when_the_state_mutex_is_held(monkeypatch):
    """issue #52: a writer that cannot take the mutex never writes unguarded; the halt still blocks."""
    monkeypatch.setattr(halt_store, "HALT_STATE_LOCK_TIMEOUT_S", 0.1)
    held = halt_store.StateLock(halt_store.state_path())
    assert held.acquire()
    try:
        record = halt_store.record_halt(scope="global", reason="raced", source="Executor")
    finally:
        held.release()
    assert record["persisted"] == halt_store.PERSISTED_FALLBACK
    assert record["state_problem"] == halt_store.LOCK_FAILED
    assert not halt_store.HALT_STATE_PATH.exists()  # nothing was written outside the mutex
    assert _veto("m2") == [halt_store.FALLBACK_ACTIVE]  # fail closed: every market is still blocked


def test_a_resume_write_failure_releases_nothing(tmp_path, monkeypatch):
    """issue #52: a failed write-back refuses the resume and keeps the halt in force."""
    rec = halt_store.record_halt(scope="market", market="m1", reason="r", source="Sentinel")

    def broken_write(path, rows):
        raise OSError("disk full")

    monkeypatch.setattr(halt_store, "_atomic_write", broken_write)
    with pytest.raises(halt_store.ResumeRejected) as info:
        _resume(rec["id"], "market", tmp_path, market="m1")
    assert info.value.reason == "release_write_failed"
    assert halt_store.halt_problems("m1") == [f"halt_market:{rec['id']}"]


# ---------------------------------------------------------------- bus rules

def test_bus_accepts_resume_only_from_operator(tmp_path):
    assert "resume" in podlib.BUS_TYPES
    ok = {"scope": "market", "halt_ref": {"id": "H1", "scope": "market"}, "approved_by": "家伟"}
    podlib.bus("Operator", "Sentinel", "resume", "m1", ok, path=tmp_path / "bus.jsonl")
    bad_cases = [
        ("Sentinel", "Sentinel", "resume", ok),
        ("Operator", "Executor", "resume", ok),
        ("Operator", "Sentinel", "resume", dict(ok, approved_by="")),
        ("Operator", "Sentinel", "resume", dict(ok, halt_ref={})),
        ("Operator", "Sentinel", "resume", dict(ok, scope="planet")),
        ("Operator", "Sentinel", "resume", dict(ok, halt_ref={"id": "H1", "scope": "global"})),
        ("Operator", "Sentinel", "resume", dict(ok, exchange_action="cancel_all")),
        ("Operator", "RiskGate", "signal", {}),  # Operator is not a bus role otherwise
        ("Operator", "Executor", "halt", {"reason": "x"}),
    ]
    for frm, to, kind, payload in bad_cases:
        with pytest.raises(RuntimeError):
            podlib.bus(frm, to, kind, "m1", payload, path=tmp_path / "bus.jsonl")
    assert len(read_jsonl(tmp_path / "bus.jsonl")) == 1


# ---------------------------------------------------------------- CLI

def test_cli_status_and_resume(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(podlib, "BUS_PATH", tmp_path / "bus.jsonl")
    assert halt_store.main(["status"]) == 0
    rec = halt_store.record_halt(scope="market", market="m1", reason="r", source="Sentinel")
    assert halt_store.main(["status"]) == 3
    assert rec["id"] in capsys.readouterr().out
    base = ["resume", "--halt-id", rec["id"], "--approved-by", "家伟", "--root-cause", "c", "--postmortem", "PR #1"]
    assert halt_store.main(base + ["--scope", "global"]) == 1  # scope mismatch
    assert halt_store.main(base + ["--scope", "market", "--checks", "{bad"]) == 1
    assert halt_store.main(base + ["--scope", "market", "--checks", '{"pytest": "pass"}']) == 0
    assert halt_store.main(["status"]) == 0
    assert read_jsonl(tmp_path / "bus.jsonl")[-1]["type"] == "resume"


def test_risk_toml_untouched():
    assert halt_store.__doc__ and "config/risk.toml" in halt_store.__doc__
    before = RISK_PATH.read_bytes()
    halt_store.record_halt(scope="global", reason="r", source="Sentinel")
    assert RISK_PATH.read_bytes() == before
