"""Issue #119 (Task CE, M2-C1): the M2 paper runner skeleton, offline.

Getters are fakes (tests/m2_runner_rig.py), every path is in tmp_path, and the halt-first cases arm getters that
fail the test if called. Covers: the --once end-to-end path (scan -> inbox -> seal -> book -> intent -> RiskGate
allow -> fill with forecast_id / decision_id / fee_source / fill_context), every preflight stop (KILL, global halt,
logs/HALT, a corrupt halt state, a never-seen Sentinel, pin drift, each live flag, the daily loss breaker and its
UTC-day reset, a damaged ledger, disk), market halts, missing fee, the single-instance lock, the heartbeat and the
Sentinel's watch (stale = global halt, paused too long = alert), and the exit codes.
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import src.halt_store as halt_store
import src.heartbeat as heartbeat
import src.ledger_store as ledger_store
import src.sentinel_tick as sentinel_tick
from src import forecast_inbox, m2_runner, paper_ledger, podlib
from src.thresholds import HEARTBEAT_RUNNER_TIMEOUT_S, RUNNER_PAUSED_ALERT_S
from tests import m2_runner_rig as rig

RISK_TEXT = Path(podlib.RISK_PATH).read_text(encoding="utf-8")


@pytest.fixture
def env(tmp_path, monkeypatch):
    paths = rig.isolate(monkeypatch, tmp_path)
    now = datetime.now(timezone.utc)
    return {**paths, "now": now, "tmp": tmp_path}


def _journal(env) -> list[dict]:
    return rig.read_jsonl(env["run"] / "journal.jsonl")


def _beat() -> dict:
    record, problem = heartbeat.read_beat(heartbeat.beat_path(m2_runner.PROC), m2_runner.PROC)
    assert problem is None and record is not None
    return record


# ---------------------------------------------------------------- --once end to end


def test_once_scans_seals_reads_books_builds_and_books_one_allowed_fill(env):
    fake = rig.FakeGetters(env["now"])
    pending = rig.deliver(env["run"], env["now"])
    assert rig.run_once(env["run"], fake) == m2_runner.EXIT_OK
    fills = rig.read_jsonl(env["paper"])
    assert len(fills) == 1
    fill = fills[0]
    assert fill["risk_decision"] == "allow" and fill["fill_context"] == "intent_builder"
    assert fill["forecast_id"].startswith("fc_") and fill["decision_id"].startswith("dc_")
    assert (fill["fee_source"], fill["fee_rate"]) == ("gamma", 0.0) and fill["fee_schedule_sha"]
    assert fill["token_id"] == rig.TOKENS[0] and fill["strategy"] == m2_runner.STRATEGY
    assert fill["calibration_problems"] == []
    steps = [row["step"] for row in _journal(env)]
    assert steps == ["sealed", "built", "committed"]
    built = _journal(env)[1]
    assert built["decision_id"] == fill["decision_id"] and _journal(env)[2]["fill_id"] == fill["fill_id"]
    # the seal came first: the archived file and its sha are in the sealed forecast row
    assert not pending.exists()
    sealed = rig.read_jsonl(Path(m2_runner.forecaster.FORECASTS_PATH))
    assert len(sealed) == 1 and sealed[0]["forecast_id"] == fill["forecast_id"]
    # reads: one keyset page, one Gamma fee read, the two books; all allowlisted GETs
    assert len(fake.gamma_urls) == 2 and len(fake.book_urls) == 2 and fake.time_calls == [podlib.TIME_URL]
    assert all(m2_runner.url_allowed(url) for url in fake.gamma_urls + fake.book_urls + fake.time_calls)
    _, read = ledger_store.read_ledger(env["paper"])
    assert ledger_store.verify(read) == []
    # Task CF (#124): RiskGate's start-of-day leg marks were written under the run dir, and the default restored
    marks = list((env["run"] / "daily_marks").glob("*.json"))
    assert len(marks) == 1 and json.loads(marks[0].read_text())["day"] == marks[0].stem
    assert paper_ledger.DAILY_MARKS_DIR != env["run"] / "daily_marks"
    candidates = rig.read_jsonl(env["run"] / "candidates.jsonl")
    assert [row["market_id"] for row in candidates] == [rig.MARKET_ID]
    assert not set(candidates[0]) & set(forecast_inbox.PRICE_FIELDS)
    beat = _beat()
    assert beat["state"] == heartbeat.STATE_FINISHED and beat["exit_code"] == 0
    progress = beat["progress"]
    assert (progress["forecasts_sealed_total"], progress["intents_built_total"], progress["allows_total"],
            progress["vetoes_total"]) == (1, 1, 1, 0)
    assert progress["executor_enabled"] is False and progress["resume_count"] == 0
    for key in ("iteration", "last_scan_ts", "scan_age_s", "forecasts_rejected_total", "missing_fee_total",
                "last_book_age_s_max", "paused_reason", "disk_free_gb", "ledger_rows"):
        assert key in progress


def test_a_rejected_inbox_file_is_journaled_and_never_reaches_a_book(env):
    fake = rig.FakeGetters(env["now"])
    rig.deliver(env["run"], env["now"], market_price_seen=True)
    assert rig.run_once(env["run"], fake) == m2_runner.EXIT_OK
    assert [row["step"] for row in _journal(env)] == ["rejected"]
    assert "price_seen" in _journal(env)[0]["reasons"]
    assert fake.book_urls == [] and rig.read_jsonl(env["paper"]) == []


def test_a_market_halt_skips_its_signal_before_any_fee_or_book_read(env):
    fake = rig.FakeGetters(env["now"])
    rig.deliver(env["run"], env["now"])
    halt_store.record_halt(scope=halt_store.SCOPE_MARKET, market=rig.MARKET_ID, reason="test_market_halt",
                           source="Sentinel")
    assert rig.run_once(env["run"], fake) == m2_runner.EXIT_OK
    steps = _journal(env)
    assert [row["step"] for row in steps] == ["sealed", "skipped"]
    assert steps[1]["reasons"][0] == "halted_market"
    assert fake.book_urls == [] and len(fake.gamma_urls) == 1  # the scan only; no fee read
    assert rig.read_jsonl(env["paper"]) == []


def test_a_missing_fee_skips_the_signal_without_reading_books(env):
    fake = rig.FakeGetters(env["now"], gamma_row_changes={"feesEnabled": None})
    rig.deliver(env["run"], env["now"])
    assert rig.run_once(env["run"], fake) == m2_runner.EXIT_OK
    steps = _journal(env)
    assert steps[-1]["step"] == "skipped" and steps[-1]["reasons"][0] == "missing_fee_data"
    assert fake.book_urls == [] and rig.read_jsonl(env["paper"]) == []
    assert _beat()["progress"]["missing_fee_total"] == 1


def test_a_failed_scan_builds_nothing(env):
    fake = rig.FakeGetters(env["now"])

    def broken(url):
        raise OSError("down")
    pending = rig.deliver(env["run"], env["now"])
    assert m2_runner.main(["--run-dir", str(env["run"]), "--once"],
                          getters=m2_runner.Getters(gamma=broken, book=fake.book)) == m2_runner.EXIT_OK
    assert pending.exists() and _journal(env) == [] and fake.book_urls == []
    events = rig.read_jsonl(env["run"] / "events.jsonl")
    assert events[0]["event"] == "scan_failed"


# ---------------------------------------------------------------- the checks come first


def _halt_global(env):
    halt_store.record_halt(scope=halt_store.SCOPE_GLOBAL, reason="test_global", source="Sentinel")


def _halt_file(env):
    halt_store.write_fallback("test logs/HALT")


def _corrupt_halt_state(env):
    path = Path(halt_store.HALT_STATE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")


def _no_sentinel(env):
    path = heartbeat.beat_path("sentinel")
    path.unlink(missing_ok=True)
    heartbeat.state_marker(path).unlink(missing_ok=True)


def _damaged_ledger(env):
    env["paper"].parent.mkdir(parents=True, exist_ok=True)
    env["paper"].write_text('{"torn": \n', encoding="utf-8")


@pytest.mark.parametrize("setup,first_reason", [
    (_halt_global, "global_halt"),
    (_halt_file, "global_halt"),
    (_corrupt_halt_state, "global_halt"),
    (_no_sentinel, "global_halt"),
    (_damaged_ledger, m2_runner.LEDGER_HALT_REASON),
])
def test_a_halt_pauses_before_any_getter_seal_or_commit(env, setup, first_reason):
    fake = rig.FakeGetters(env["now"], fail=True)
    pending = rig.deliver(env["run"], env["now"])
    setup(env)
    assert rig.run_once(env["run"], fake) == m2_runner.EXIT_OK
    assert fake.calls == 0 and pending.exists() and _journal(env) == []
    assert not Path(m2_runner.forecaster.FORECASTS_PATH).exists()
    beat = _beat()
    assert beat["progress"]["paused_reason"] == first_reason
    assert beat["progress"]["paused_since"] is not None


def test_a_damaged_ledger_records_the_invalid_risk_state_halt(env):
    _damaged_ledger(env)
    rig.run_once(env["run"], rig.FakeGetters(env["now"], fail=True))
    assert any(h["reason"] == m2_runner.LEDGER_HALT_REASON and h["scope"] == "global"
               for h in halt_store.active_halts())


def test_kill_finishes_with_exit_0_and_does_nothing(env):
    fake = rig.FakeGetters(env["now"], fail=True)
    pending = rig.deliver(env["run"], env["now"])
    (env["run"] / "KILL").write_text("stop", encoding="utf-8")
    assert m2_runner.main(["--run-dir", str(env["run"])], getters=fake.getters()) == m2_runner.EXIT_OK
    assert fake.calls == 0 and pending.exists()
    beat = _beat()
    assert beat["state"] == heartbeat.STATE_FINISHED and beat["exit_code"] == 0


def _risk_file(env, old: str, new: str) -> Path:
    assert old in RISK_TEXT
    path = env["tmp"] / "risk.toml"
    path.write_text(RISK_TEXT.replace(old, new), encoding="utf-8")
    return path


@pytest.mark.parametrize("old,new,reason", [
    ("live_enabled = false", "live_enabled = true", m2_runner.LIVE_HALT_REASON),
    ('mode = "paper"', 'mode = "live"', m2_runner.LIVE_HALT_REASON),
    ("require_human_gate = true", "require_human_gate = false", m2_runner.LIVE_HALT_REASON),
    ("max_spread = 0.04", "max_spread = 0.05", "risk_toml_pin_drift"),
])
def test_a_live_flag_or_pin_drift_exits_6_with_a_global_halt_and_no_getter(env, old, new, reason):
    fake = rig.FakeGetters(env["now"], fail=True)
    pending = rig.deliver(env["run"], env["now"])
    risk = _risk_file(env, old, new)
    assert rig.run_once(env["run"], fake, risk_path=risk) == m2_runner.EXIT_PIN_OR_LIVE
    assert fake.calls == 0 and pending.exists() and rig.read_jsonl(env["paper"]) == []
    assert any(h["reason"] == reason and h["scope"] == "global" for h in halt_store.active_halts())
    beat = _beat()
    assert beat["state"] == heartbeat.STATE_FINISHED and beat["exit_code"] == m2_runner.EXIT_PIN_OR_LIVE


def test_pins_are_checked_on_every_iteration_not_throttled(env, monkeypatch):
    calls = []
    real = m2_runner.pins.risk_file_problems
    monkeypatch.setattr(m2_runner.pins, "risk_file_problems", lambda path=None: calls.append(path) or real(path))
    state = m2_runner.RunnerState(paths=m2_runner.RunnerPaths.under(env["run"]))
    fake = rig.FakeGetters(env["now"])
    for _ in range(3):
        m2_runner.iteration(state, now=env["now"], getters=fake.getters())
    assert len(calls) == 3


def test_the_daily_loss_breaker_pauses_today_and_resumes_the_next_utc_day(env, monkeypatch):
    fake = rig.FakeGetters(env["now"])
    pending = rig.deliver(env["run"], env["now"])
    today = env["now"].strftime("%Y-%m-%d")

    def loss(rows, ids, now):
        return {"loss_usd": 30.0 if now[:10] == today else 0.0, "problems": []}  # the cap exactly
    monkeypatch.setattr(paper_ledger, "daily_risk_loss", loss)
    state = m2_runner.RunnerState(paths=m2_runner.RunnerPaths.under(env["run"]))
    report = m2_runner.iteration(state, now=env["now"], getters=fake.getters())
    assert report.action == m2_runner.ACT_PAUSE and report.reasons[0] == "daily_loss_breaker"
    assert fake.time_calls == [podlib.TIME_URL] and fake.gamma_urls == fake.book_urls == [] and pending.exists()
    tomorrow = (env["now"] + timedelta(days=1)).replace(hour=0, minute=0, second=1)
    assert m2_runner.preflight(state.paths, now=tomorrow).action == m2_runner.ACT_RUN


def test_disk_low_and_disk_budget_pause(env, monkeypatch):
    paths = m2_runner.RunnerPaths.under(env["run"])
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(paths.run_dir)
    monkeypatch.setattr(m2_runner.shutil, "disk_usage", lambda p: usage._replace(free=int(1.5e9)))
    assert m2_runner.preflight(paths, now=env["now"]).reasons[0] == "disk_low"
    monkeypatch.setattr(m2_runner.shutil, "disk_usage", lambda p: usage._replace(free=int(50e9)))
    monkeypatch.setattr(m2_runner, "_dir_bytes", lambda root: int(5.1e9))
    assert m2_runner.preflight(paths, now=env["now"]).reasons[0] == "disk_budget"


def test_paused_since_is_kept_while_the_same_reason_holds(env):
    _halt_global(env)
    state = m2_runner.RunnerState(paths=m2_runner.RunnerPaths.under(env["run"]))
    fake = rig.FakeGetters(env["now"], fail=True)
    m2_runner.iteration(state, now=env["now"], getters=fake.getters())
    first = state.paused_since
    m2_runner.iteration(state, now=env["now"] + timedelta(minutes=5), getters=fake.getters())
    assert state.paused_since == first and state.paused_reason == "global_halt"


# ---------------------------------------------------------------- single instance, exit codes


def test_a_second_runner_exits_3_and_writes_no_heartbeat(env):
    paths = m2_runner.RunnerPaths.under(env["run"])
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    holder = m2_runner.RunnerLock(paths.lock_file)
    assert holder.acquire("2026-10-11T00:00:00.000Z")
    try:
        fake = rig.FakeGetters(env["now"], fail=True)
        assert rig.run_once(env["run"], fake) == m2_runner.EXIT_ALREADY_RUNNING
        assert fake.calls == 0
        assert not heartbeat.beat_path(m2_runner.PROC).exists()
        with paths.lock_file.open("rb", buffering=0) as handle:
            handle.seek(1)  # Windows locks byte 0: skip it before any read, not after reading it.
            info = json.loads(handle.read().decode("utf-8"))
        assert set(info) == {"pid", "host", "started_at"}
    finally:
        holder.release()
    assert rig.run_once(env["run"], rig.FakeGetters(env["now"])) == m2_runner.EXIT_OK


def test_a_journal_write_failure_exits_4(env):
    fake = rig.FakeGetters(env["now"])
    rig.deliver(env["run"], env["now"])
    (env["run"] / "journal.jsonl").mkdir(parents=True)
    assert rig.run_once(env["run"], fake) == m2_runner.EXIT_WRITE_FAILED
    assert _beat()["exit_code"] == m2_runner.EXIT_WRITE_FAILED


def test_an_unwritable_run_dir_exits_7(env):
    env["run"].parent.mkdir(parents=True, exist_ok=True)
    env["run"].write_text("a file, not a dir", encoding="utf-8")
    assert rig.run_once(env["run"], rig.FakeGetters(env["now"], fail=True)) == m2_runner.EXIT_SELF_CHECK


@pytest.mark.parametrize("extra", [["--live"], ["--executor"], ["--enable-executor"], ["--max-runtime", "0"]])
def test_usage_errors_exit_2(env, extra):
    fake = rig.FakeGetters(env["now"], fail=True)
    assert m2_runner.main(["--run-dir", str(env["run"]), *extra], getters=fake.getters()) == m2_runner.EXIT_USAGE
    assert m2_runner.main([], getters=fake.getters()) == m2_runner.EXIT_USAGE  # --run-dir is required


def test_the_loop_beats_while_it_waits_and_stops_at_max_runtime(env):
    fake = rig.FakeGetters(env["now"])
    clock = {"t": 0.0}
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        clock["t"] += seconds
    code = m2_runner.main(["--run-dir", str(env["run"]), "--max-runtime", "150"], getters=fake.getters(),
                          sleep=sleep, monotonic=lambda: clock["t"])
    assert code == m2_runner.EXIT_OK
    assert sleeps and max(sleeps) <= m2_runner.HEARTBEAT_RUNNER_EVERY_S
    assert _beat()["progress"]["iteration"] == 3  # t = 0, 60, 120


# ---------------------------------------------------------------- book parsing


def test_book_snapshot_from_clob_keeps_full_depth_in_order():
    snap = m2_runner.book_snapshot_from_clob(rig.clob_book(rig.TOKENS[0], datetime.now(timezone.utc)),
                                             rig.MARKET_ID, rig.TOKENS[0])
    assert snap.asks == ((0.41, 400.0), (0.42, 800.0)) and snap.bids == ((0.39, 500.0), (0.38, 900.0))
    assert (snap.best_ask, snap.best_bid, snap.spread, snap.tick_size, snap.min_order_size) == (0.41, 0.39, 0.02,
                                                                                                0.01, 5.0)
    assert snap.book_ts is not None and snap.token_id == rig.TOKENS[0]


@pytest.mark.parametrize("raw", [
    None, [], "x", {"asset_id": "other", "asks": [], "bids": []},
    {"asks": [{"price": "x", "size": "1"}], "bids": []}, {"asks": "bad", "bids": []},
])
def test_an_unusable_book_gives_no_levels(raw):
    snap = m2_runner.book_snapshot_from_clob(raw, rig.MARKET_ID, rig.TOKENS[0])
    assert snap.asks == () and snap.bids == ()


# ---------------------------------------------------------------- the Sentinel watches m2_runner


def _runner_beat(now: datetime, *, state: str, progress: dict) -> None:
    hb = heartbeat.Heartbeat(m2_runner.PROC, every_s=30.0, now=lambda: now)
    hb.beat(state, progress, force=True)


def test_the_sentinel_halts_on_a_stale_runner_heartbeat(tmp_path):
    now = datetime.now(timezone.utc)
    _runner_beat(now - timedelta(seconds=HEARTBEAT_RUNNER_TIMEOUT_S + 5), state=heartbeat.STATE_RUNNING,
                 progress={})
    result = sentinel_tick.tick(now=now, bus_path=tmp_path / "bus.jsonl", ledger_path=tmp_path / "paper.jsonl")
    assert any("heartbeat_stale:m2_runner" in h for h in result["halts"])
    assert sentinel_tick.PROC_SPECS["m2_runner"].on_stale == sentinel_tick.GLOBAL


@pytest.mark.parametrize("since_s,alerted", [(RUNNER_PAUSED_ALERT_S - 60, False), (RUNNER_PAUSED_ALERT_S + 60, True),
                                             (None, True)])
def test_the_sentinel_alerts_when_the_runner_is_paused_too_long(tmp_path, since_s, alerted):
    now = datetime.now(timezone.utc)
    since = None if since_s is None else heartbeat.utc_stamp(now - timedelta(seconds=since_s))
    _runner_beat(now, state=heartbeat.STATE_PAUSED, progress={"paused_since": since, "paused_reason": "global_halt"})
    result = sentinel_tick.tick(now=now, bus_path=tmp_path / "bus.jsonl", ledger_path=tmp_path / "paper.jsonl")
    assert ("runner_paused_too_long" in result["alerts"]) is alerted


@pytest.mark.parametrize("code,halted", [(4, True), (6, True), (3, False), (7, False)])
def test_runner_exit_codes_in_the_sentinel_matrix(tmp_path, code, halted):
    now = datetime.now(timezone.utc)
    hb = heartbeat.Heartbeat(m2_runner.PROC, every_s=30.0, now=lambda: now)
    hb.finish(code, {})
    result = sentinel_tick.tick(now=now, bus_path=tmp_path / "bus.jsonl", ledger_path=tmp_path / "paper.jsonl")
    assert any(f"exit_{code}:m2_runner" in h for h in result["halts"]) is halted


def test_a_runner_crash_is_a_global_halt():
    assert "m2_runner" in sentinel_tick.CRASH_IS_GLOBAL
