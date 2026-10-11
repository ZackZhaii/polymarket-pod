"""Issue #120 (Task CJ, M2-C2): restart / suspend safety for the M2 paper runner.

Crash injection (built → commit → committed), unique_decision_id booking, suspend/resume detection,
clock skew, book_before_resume / stale_book, and recover(). Offline: injected clocks, FakeGetters.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import src.ledger_store as ledger_store
from src import book, intent_builder, m2_runner
from src.calibration_ids import make_decision_id
from src.thresholds import CLOCK_SKEW_MAX_S, MAX_BOOK_AGE_S
from tests import m2_runner_rig as rig


@pytest.fixture
def env(tmp_path, monkeypatch):
    paths = rig.isolate(monkeypatch, tmp_path)
    now = datetime.now(timezone.utc)
    return {**paths, "now": now, "tmp": tmp_path}


def _journal(env) -> list[dict]:
    return rig.read_jsonl(env["run"] / "journal.jsonl")


def _events(env) -> list[dict]:
    path = env["run"] / "events.jsonl"
    return rig.read_jsonl(path) if path.exists() else []


class _Clock:
    def __init__(self, start: datetime):
        self.wall = start
        self.mono = 1000.0

    def now(self) -> datetime:
        return self.wall

    def monotonic(self) -> float:
        return self.mono

    def advance_wall(self, seconds: float) -> None:
        self.wall = self.wall + timedelta(seconds=seconds)


# ---------------------------------------------------------------- unique_decision_id


def test_unique_decision_id_returns_already_booked_without_a_second_row(env):
    decision_id = make_decision_id("news-forecaster", "m", "t", "fc_x")
    row = {
        "fill_id": "fill-a", "ts": "2026-10-11T12:00:00Z", "strategy": "news-forecaster", "market": "m",
        "token_id": "t", "side": "buy", "shares": 10.0, "notional_usd": 5.0, "fee_usd": 0.0, "fee_rate": 0.0,
        "market_price": 0.5, "effective_price": 0.5, "hypothetical": False, "counts_toward_risk": True,
        "risk_decision": "allow", "decision_id": decision_id, "forecast_id": "fc_x",
    }
    first = ledger_store.book(env["paper"], row, unique_decision_id=True)
    assert first.get("already_booked") is not True and first["fill_id"] == "fill-a"
    second = ledger_store.book(env["paper"], dict(row, fill_id="fill-b"), unique_decision_id=True)
    assert second == {"already_booked": True, "fill": first}
    _, read = ledger_store.read_ledger(env["paper"])
    assert len(read.rows) == 1 and ledger_store.verify(read) == []


def test_unique_decision_id_refuses_a_missing_decision_id(env):
    row = {
        "fill_id": "fill-a", "ts": "2026-10-11T12:00:00Z", "strategy": "s", "market": "m", "token_id": "t",
        "side": "buy", "shares": 1.0, "notional_usd": 0.5, "fee_usd": 0.0, "fee_rate": 0.0,
        "market_price": 0.5, "effective_price": 0.5, "hypothetical": False, "counts_toward_risk": True,
        "risk_decision": "allow",
    }
    with pytest.raises(ledger_store.LedgerIntegrityError) as caught:
        ledger_store.book(env["paper"], row, unique_decision_id=True)
    assert any("missing_decision_id" in p for p in caught.value.problems)


def test_unique_decision_id_false_keeps_legacy_booking(env):
    row = {
        "ts": "2026-10-11T12:00:00Z", "strategy": "s", "market": "m", "token_id": "t", "side": "buy",
        "shares": 1.0, "notional_usd": 0.5, "fee_usd": 0.0, "fee_rate": 0.0, "market_price": 0.5,
        "effective_price": 0.5, "hypothetical": False, "counts_toward_risk": True, "risk_decision": "allow",
    }
    a = ledger_store.book(env["paper"], dict(row, fill_id="a"))
    b = ledger_store.book(env["paper"], dict(row, fill_id="b"))
    assert a["fill_id"] != b["fill_id"]
    _, read = ledger_store.read_ledger(env["paper"])
    assert len(read.rows) == 2


# ---------------------------------------------------------------- book.fetch_depth + book_snapshot_from_clob


def test_fetch_depth_returns_full_book_and_normalised_book_ts(env):
    now = env["now"]
    millis = int(now.timestamp() * 1000)

    def getter(url: str):
        assert "token_id=81001" in url
        return {"asset_id": "81001", "timestamp": str(millis), "tick_size": "0.01", "min_order_size": "5",
                "asks": [{"price": "0.55", "size": "100"}, {"price": "0.56", "size": "50"}],
                "bids": [{"price": "0.54", "size": "80"}, {"price": "0.53", "size": "40"}]}

    raw = book.fetch_depth("81001", getter=getter)
    assert raw is not None and raw["book_ts"].endswith("Z") and raw["tick_size"] == "0.01"
    snap = intent_builder.book_snapshot_from_clob(raw, "m", "81001")
    assert len(snap.asks) == 2 and len(snap.bids) == 2
    assert snap.asks[0][0] == 0.55 and snap.bids[0][0] == 0.54
    assert snap.book_ts == raw["book_ts"]


def test_book_snapshot_from_clob_rejects_another_asset():
    snap = intent_builder.book_snapshot_from_clob(
        {"asset_id": "other", "asks": [{"price": "0.5", "size": "1"}], "bids": [{"price": "0.4", "size": "1"}]},
        "m", "81001")
    assert snap.token_id == "" and snap.asks == () and snap.bids == ()


def test_m2_runner_reexports_book_snapshot_from_clob():
    raw = {"asks": [{"price": "0.5", "size": "1"}], "bids": [{"price": "0.4", "size": "1"}],
           "timestamp": "2026-10-11T12:00:00.000Z"}
    assert m2_runner.book_snapshot_from_clob(raw, "m", "t") == intent_builder.book_snapshot_from_clob(raw, "m", "t")


# ---------------------------------------------------------------- recover / crash


def test_recover_patches_built_when_ledger_already_has_the_decision(env):
    fake = rig.FakeGetters(env["now"])
    rig.deliver(env["run"], env["now"])
    assert rig.run_once(env["run"], fake, clock=lambda: env["now"]) == m2_runner.EXIT_OK
    fills = rig.read_jsonl(env["paper"])
    assert len(fills) == 1
    decision_id = fills[0]["decision_id"]
    built = next(r for r in _journal(env) if r["step"] == "built")
    env["run"].joinpath("journal.jsonl").write_text(json.dumps(built) + "\n", encoding="utf-8")
    state = m2_runner.recover(m2_runner.RunnerPaths.under(env["run"]), env["paper"], now=env["now"])
    patched = _journal(env)[-1]
    assert patched["step"] == "committed" and patched["decision_id"] == decision_id and patched.get("recovered")
    assert built.get("idempotency_key") in state.seen_keys
    _, read = ledger_store.read_ledger(env["paper"])
    assert len(read.rows) == 1 and ledger_store.verify(read) == []


def test_recover_leaves_built_alone_when_ledger_has_no_fill(env):
    paths = m2_runner.RunnerPaths.under(env["run"])
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    decision_id = make_decision_id("news-forecaster", "m2-alpha", rig.TOKENS[0], "fc_orphan")
    built = {"ts": "2026-10-11T12:00:00Z", "step": "built", "forecast_id": "fc_orphan",
             "intent_id": "in_x", "decision_id": decision_id, "idempotency_key": "ik_orphan"}
    paths.journal.write_text(json.dumps(built) + "\n", encoding="utf-8")
    state = m2_runner.recover(paths, env["paper"], now=env["now"])
    assert [r["step"] for r in _journal(env)] == ["built"]
    assert "ik_orphan" in state.seen_keys
    assert ledger_store.read_ledger(env["paper"])[1].rows == []


def test_crash_after_built_then_rerun_books_the_decision_only_once(env):
    fake = rig.FakeGetters(env["now"])
    rig.deliver(env["run"], env["now"])
    assert rig.run_once(env["run"], fake, clock=lambda: env["now"]) == m2_runner.EXIT_OK
    fills = rig.read_jsonl(env["paper"])
    decision_id = fills[0]["decision_id"]
    built = next(r for r in _journal(env) if r["step"] == "built")
    env["run"].joinpath("journal.jsonl").write_text(json.dumps(built) + "\n", encoding="utf-8")
    state = m2_runner.recover(m2_runner.RunnerPaths.under(env["run"]), env["paper"], now=env["now"])
    assert any(r.get("recovered") for r in _journal(env) if r["step"] == "committed")
    again = ledger_store.book(env["paper"], dict(fills[0], fill_id="retry-fill"), unique_decision_id=True)
    assert again["already_booked"] is True and again["fill"]["decision_id"] == decision_id
    _, read = ledger_store.read_ledger(env["paper"])
    assert len([r for r in read.rows if r.get("decision_id") == decision_id]) == 1
    assert ledger_store.verify(read) == []
    assert built["idempotency_key"] in state.seen_keys


# ---------------------------------------------------------------- suspend / resume / skew / stale


def test_wall_gap_triggers_resume_detected_and_skips_orders(env):
    clock = _Clock(env["now"])
    fake = rig.FakeGetters(env["now"])
    rig.deliver(env["run"], env["now"])
    paths = m2_runner.RunnerPaths.under(env["run"])
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    state = m2_runner.RunnerState(paths=paths, clock=clock.now, monotonic=clock.monotonic)
    report1 = m2_runner.iteration(state, now=clock.now(), getters=fake.getters())
    assert report1.action == m2_runner.ACT_RUN and state.last_iter_wall is not None
    clock.advance_wall(3600)
    before_books = len(fake.book_urls)
    report2 = m2_runner.iteration(state, now=clock.now(), getters=fake.getters())
    assert "resume_detected" in report2.reasons
    assert report2.allows == 0 and report2.built == 0
    assert state.resume_count == 1 and state.last_resume_at is not None
    assert any(e.get("event") == "resume_detected" for e in _events(env))
    assert len(fake.book_urls) == before_books


def test_book_fetched_before_resume_is_refused(env):
    clock = _Clock(env["now"])
    fake = rig.FakeGetters(env["now"])
    rig.deliver(env["run"], env["now"])
    paths = m2_runner.RunnerPaths.under(env["run"])
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    state = m2_runner.RunnerState(paths=paths, clock=clock.now, monotonic=clock.monotonic)
    state.last_resume_at = clock.now() + timedelta(seconds=60)
    before = len(rig.read_jsonl(env["paper"]))
    report = m2_runner.iteration(state, now=clock.now(), getters=fake.getters())
    assert report.allows == 0
    assert len(rig.read_jsonl(env["paper"])) == before
    assert any(r.get("step") == "skipped" and "book_before_resume" in (r.get("reasons") or [])
               for r in _journal(env)), _journal(env)


def test_clock_skew_pauses_the_runner(env):
    clock = _Clock(env["now"])
    skewed = env["now"] - timedelta(seconds=CLOCK_SKEW_MAX_S + 30)

    class Skewed(rig.FakeGetters):
        def book(self, url: str):
            body = dict(super().book(url))
            body["timestamp"] = str(int(skewed.timestamp() * 1000))
            return body

    fake = Skewed(env["now"])
    rig.deliver(env["run"], env["now"])
    paths = m2_runner.RunnerPaths.under(env["run"])
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    state = m2_runner.RunnerState(paths=paths, clock=clock.now, monotonic=clock.monotonic)
    report = m2_runner.iteration(state, now=clock.now(), getters=fake.getters())
    assert report.action == m2_runner.ACT_PAUSE and report.paused_reason == "clock_skew"
    assert any(e.get("event") == "clock_skew" for e in _events(env))


def test_stale_book_is_skipped(env):
    clock = _Clock(env["now"])
    old = env["now"] - timedelta(seconds=MAX_BOOK_AGE_S + 10)

    class Stale(rig.FakeGetters):
        def book(self, url: str):
            body = dict(super().book(url))
            body["timestamp"] = str(int(old.timestamp() * 1000))
            return body

    fake = Stale(env["now"])
    rig.deliver(env["run"], env["now"])
    paths = m2_runner.RunnerPaths.under(env["run"])
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    state = m2_runner.RunnerState(paths=paths, clock=clock.now, monotonic=clock.monotonic)
    report = m2_runner.iteration(state, now=clock.now(), getters=fake.getters())
    assert report.allows == 0
    assert any(r.get("step") == "skipped" and "stale_book" in (r.get("reasons") or []) for r in _journal(env))


def test_progress_reports_resume_count(env):
    paths = m2_runner.RunnerPaths.under(env["run"])
    state = m2_runner.RunnerState(paths=paths, resume_count=3)
    assert m2_runner.progress(state, env["now"])["resume_count"] == 3
