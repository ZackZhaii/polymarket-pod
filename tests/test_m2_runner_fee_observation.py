"""ET: returned FeeCache quote facts, real caches and owned offline journal I/O."""
from __future__ import annotations

import dataclasses
import socket
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src import fee_source, heartbeat, ledger_store, m2_runner, paper_ledger, podlib
from tests import m2_runner_rig as rig


STEP = "fee_quote_evaluated"
TOP_KEYS = {"ts", "step", "forecast_id", "market_id", "iteration", "iteration_now", "fee_available",
            "cache_hit", "quote"}
QUOTE_KEYS = {"market_id", "fee_rate", "problem", "fee_source", "fee_gamma_fetched_at", "fee_schedule_sha",
              "cross_checked"}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refused(*args, **kwargs):
        pytest.fail("ET offline fixture attempted network access")

    monkeypatch.setattr(socket.socket, "connect", refused)
    monkeypatch.setattr(socket, "create_connection", refused)
    monkeypatch.setattr(socket, "getaddrinfo", refused)
    monkeypatch.setattr(podlib, "urlopen", refused)


@pytest.fixture
def case(tmp_path, monkeypatch):
    def make(count=2):
        paths = rig.isolate(monkeypatch, tmp_path)
        now = datetime.now(timezone.utc) - timedelta(seconds=1)
        monkeypatch.setattr(m2_runner.forecaster, "utc_now", lambda: rig.stamp(now - timedelta(seconds=1)))
        state = m2_runner.RunnerState(paths=m2_runner.RunnerPaths.under(paths["run"]),
                                      clock=lambda: now, monotonic=lambda: 0.0)
        fake = rig.FakeGetters(now)
        m2_runner._scan(state, now, m2_runner.GuardedGetter(fake.gamma))
        for i in range(count):
            rig.deliver(paths["run"], now, name=f"{i:04d}.json", p=0.62 + i * 0.01,
                        p_low=0.55 + i * 0.01, p_high=0.68 + i * 0.01)
        sealed = m2_runner.IterationReport(m2_runner.ACT_RUN)
        m2_runner._ingest(state, now, sealed)
        assert sealed.sealed == count and len(state.pending) == count
        fake.gamma_urls.clear()
        return SimpleNamespace(paths=paths, now=now, state=state, fake=fake,
                               source_stamp=(now - timedelta(seconds=7)).isoformat(timespec="microseconds"))

    return make


def observe(case, monkeypatch, *, kind="free", book_mode="good", no_return=None, real_commit=False):
    """Keep real FeeCache/getter/snapshot/builder behavior and trace its public boundaries."""
    trace, returned, commits = [], [], []
    real_build, real_commit_fn = m2_runner.intent_builder.build_order_intent, paper_ledger.commit_if_allowed
    real_journal = m2_runner.journal
    failure = BaseException("owned getter base failure") if no_return == "getter" else RuntimeError("owned clock")

    def cache_clock():
        trace.append(("fee_clock",))
        if no_return == "clock":
            raise failure
        return 0.0

    def source_wall():
        trace.append(("fee_wall",))
        if no_return == "wall":
            raise failure
        return case.source_stamp

    def gamma(url):
        trace.append(("get", "gamma"))
        case.fake.gamma_urls.append(url)
        if no_return == "getter":
            raise failure
        if kind == "getter_error":
            raise TimeoutError("owned transport failure")
        row = rig.gamma_row(case.now)
        if kind == "paid":
            row.update(feesEnabled=True, feeSchedule={"rate": 0.04, "exponent": 1, "takerOnly": True})
        elif kind == "missing":
            row["feesEnabled"] = None
        return {"markets": [row]}

    def books(url):
        is_time = url == podlib.TIME_URL
        trace.append(("get", "time" if is_time else "book"))
        if not is_time and book_mode == "error":
            case.fake.book_urls.append(url)
            raise OSError("owned book error")
        raw = case.fake.book(url)
        if not is_time and book_mode == "none":
            return None
        if not is_time and book_mode == "stale":
            raw["timestamp"] = str(int((case.now - timedelta(seconds=m2_runner.MAX_BOOK_AGE_S + 2)).timestamp() * 1000))
        return raw

    def after():
        trace.append(("after",))

    def clock():
        trace.append(("clock",))
        return case.now

    def build(*args, **kwargs):
        trace.append(("build",))
        return real_build(*args, **kwargs)

    def commit(*args, **kwargs):
        trace.append(("commit",))
        commits.append((args, kwargs))
        if real_commit:
            return real_commit_fn(*args, **kwargs)
        return {"allow": False, "decision": {"reasons": ["owned-test-veto"]}, "halted": False}

    def journal(state, step, now, **fields):
        trace.append(("journal", step))
        return real_journal(state, step, now, **fields)

    cache = fee_source.FeeCache(lambda mid: m2_runner._gamma_market(case.state.fee_getter, mid),
                               clock=cache_clock, wall=source_wall)
    original_get = cache.get

    def get(mid):
        quote = original_get(mid)
        returned.append(quote)
        return quote

    monkeypatch.setattr(cache, "get", get)
    monkeypatch.setattr(m2_runner.intent_builder, "build_order_intent", build)
    monkeypatch.setattr(paper_ledger, "commit_if_allowed", commit)
    monkeypatch.setattr(m2_runner, "journal", journal)
    case.state.fee_cache, case.state.clock, case.state.on_get = cache, clock, after
    report = m2_runner.IterationReport(m2_runner.ACT_RUN)
    result = SimpleNamespace(trace=trace, returned=returned, commits=commits, report=report, failure=failure,
                             getters=m2_runner.Getters(gamma=gamma, book=books))

    def run(now=None):
        m2_runner._decide(case.state, now or case.now, podlib.load_risk(), m2_runner.GuardedGetter(gamma, after),
                          m2_runner.GuardedGetter(books, after), report)
        return result

    result.run = run
    return result


def facts(case):
    return [row for row in rig.read_jsonl(case.state.paths.journal) if row["step"] == STEP]


def fail_observation_fsync(monkeypatch, journal_path):
    """Fail the real append after its write/flush, not the observation or journal facade."""
    active = [False]
    real_append, real_fsync = m2_runner.append_synced, m2_runner.os.fsync
    error = OSError("owned observation fsync failure")

    def fsync(fd):
        if active[0]:
            raise error
        return real_fsync(fd)

    def append(path, row):
        active[0] = path == journal_path and row.get("step") == STEP
        try:
            return real_append(path, row)
        finally:
            active[0] = False

    monkeypatch.setattr(m2_runner.os, "fsync", fsync)
    monkeypatch.setattr(m2_runner, "append_synced", append)
    return error


@pytest.mark.parametrize("kind", ["free", "paid"])
def test_real_positive_cache_two_return_facts_are_not_two_gets(case, monkeypatch, kind):
    c = case()
    r = observe(c, monkeypatch, kind=kind)
    batch = c.now.astimezone(timezone(timedelta(hours=5, minutes=30)))
    r.run(batch)
    rows = facts(c)
    assert len(rows) == 2
    assert len(r.returned) == 2 and r.returned[0] is r.returned[1]
    assert len(c.fake.gamma_urls) == 1 and len(c.fake.book_urls) == 4
    assert r.trace.count(("fee_clock",)) == 2 and r.trace.count(("fee_wall",)) == 1
    assert r.trace.count(("clock",)) == 6 and r.trace.count(("after",)) == 5
    for row, quote in zip(rows, r.returned):
        assert set(row) == TOP_KEYS and set(row["quote"]) == QUOTE_KEYS
        assert row["quote"] == dataclasses.asdict(quote)
        assert row["fee_available"] is True and row["cache_hit"] == "UNKNOWN"
        assert row["market_id"] == quote.market_id == rig.MARKET_ID
        assert row["iteration"] == 0 and row["iteration_now"] == batch.isoformat(timespec="microseconds")
        assert row["ts"] == m2_runner._stamp(batch)
        assert row["quote"]["fee_gamma_fetched_at"] == c.source_stamp != row["iteration_now"]
        assert row["quote"]["fee_rate"] == (0.0 if kind == "free" else 0.04)
    old_rows = [row for row in rig.read_jsonl(c.state.paths.journal) if row["step"] != STEP]
    assert [row["step"] for row in old_rows] == ["sealed", "sealed", "built", "vetoed", "built", "vetoed"]
    assert [row["forecast_id"] for row in rows] == [old_rows[2]["forecast_id"], old_rows[4]["forecast_id"]]
    assert rows[0]["forecast_id"] != rows[1]["forecast_id"]
    tail = [("get", "book"), ("after",), ("clock",), ("get", "book"), ("after",), ("clock",),
            ("clock",), ("build",), ("journal", "built"), ("commit",), ("journal", "vetoed")]
    assert r.trace == [("fee_clock",), ("fee_wall",), ("get", "gamma"), ("after",), ("journal", STEP),
                       *tail, ("fee_clock",), ("journal", STEP), *tail]
    assert (r.report.built, r.report.vetoes, r.report.allows) == (2, 2, 0)
    assert c.state.counters["missing_fee_total"] == 0


@pytest.mark.parametrize("kind,problem", [("missing", "fees_enabled_unknown"), ("getter_error", "gamma_unreadable")])
def test_real_negative_cache_repeats_facts_and_original_missing_count(case, monkeypatch, kind, problem):
    c = case()
    r = observe(c, monkeypatch, kind=kind).run()
    rows = facts(c)
    assert len(rows) == len(r.returned) == 2 and r.returned[0] is r.returned[1]
    assert len(c.fake.gamma_urls) == 1 and c.fake.book_urls == []
    for row, quote in zip(rows, r.returned):
        assert set(row) == TOP_KEYS and row["quote"] == dataclasses.asdict(quote)
        assert row["fee_available"] is False and row["cache_hit"] == "UNKNOWN"
        assert row["quote"]["fee_rate"] is None and row["quote"]["problem"] == problem
        assert row["quote"]["fee_gamma_fetched_at"] == c.source_stamp
    assert [row["step"] for row in rig.read_jsonl(c.state.paths.journal)] == ["sealed", "sealed", STEP,
                                                                           "skipped", STEP, "skipped"]
    assert c.state.counters["missing_fee_total"] == 2 and r.report.skipped == 2
    assert [pending.evals for pending in c.state.pending.values()] == [1, 1]
    assert not r.commits and ("clock",) not in r.trace
    assert r.trace == [("fee_clock",), ("fee_wall",), ("get", "gamma"), ("after",), ("journal", STEP),
                       ("journal", "skipped"), ("fee_clock",), ("journal", STEP), ("journal", "skipped")]


@pytest.mark.parametrize("exit_kind", ["missing_market", "halt"])
def test_pre_get_early_exits_do_not_fabricate_a_quote_fact(case, monkeypatch, exit_kind):
    c = case(1)
    r = observe(c, monkeypatch)
    if exit_kind == "missing_market":
        c.state.markets.clear()
    else:
        monkeypatch.setattr(m2_runner.halt_store, "halt_problems", lambda market: ["halt_market:owned"])
    r.run()
    assert facts(c) == [] and r.returned == [] and c.fake.gamma_urls == [] and c.fake.book_urls == []
    assert r.trace == [("journal", "skipped")] and r.report.skipped == 1


@pytest.mark.parametrize("boundary", ["clock", "wall", "getter"])
def test_get_itself_not_returning_propagates_without_a_fact(case, monkeypatch, boundary):
    c = case(1)
    r = observe(c, monkeypatch, no_return=boundary)
    with pytest.raises(BaseException) as caught:
        r.run()
    assert caught.value is r.failure
    assert facts(c) == [] and r.returned == [] and r.commits == [] and c.fake.book_urls == []
    assert c.state.counters["missing_fee_total"] == 0
    assert len(c.fake.gamma_urls) == int(boundary == "getter")
    assert r.trace.count(("after",)) == int(boundary == "getter")


@pytest.mark.parametrize("outcome", ["none", "error", "stale", "nonbuilt", "veto"])
def test_returned_quote_fact_survives_original_later_outcomes(case, monkeypatch, outcome):
    c = case(1)
    if outcome == "nonbuilt":
        c.state.markets[rig.MARKET_ID]["liquidity_usd"] = 0
    r = observe(c, monkeypatch, book_mode=outcome if outcome in {"none", "error", "stale"} else "good").run()
    assert len(facts(c)) == 1 and facts(c)[0]["fee_available"] is True
    rows = rig.read_jsonl(c.state.paths.journal)
    assert [row["step"] for row in rows[:2]] == ["sealed", STEP]
    assert rows[-1]["step"] == ("vetoed" if outcome == "veto" else "skipped")
    assert bool(r.commits) is (outcome == "veto")
    assert c.state.counters["missing_fee_total"] == 0


@pytest.mark.parametrize("kind", ["free", "missing"])
def test_observation_fsync_failure_stops_before_current_counter_books_build_commit(case, monkeypatch, kind):
    c = case(1)
    r = observe(c, monkeypatch, kind=kind)
    error = fail_observation_fsync(monkeypatch, c.state.paths.journal)
    with pytest.raises(m2_runner.WriteFailed) as caught:
        r.run()
    assert caught.value.__cause__ is error
    assert len(r.returned) == len(c.fake.gamma_urls) == 1 and c.fake.book_urls == []
    assert r.trace[-1] == ("journal", STEP) and not r.commits and ("build",) not in r.trace
    assert c.state.counters["missing_fee_total"] == c.state.counters["intents_built_total"] == 0
    assert (r.report.skipped, r.report.built, r.report.allows, r.report.vetoes) == (0, 0, 0, 0)
    assert [item.evals for item in c.state.pending.values()] == [0]
    # Real flush preceded the injected fsync error: bytes can remain despite WriteFailed.
    assert [row["step"] for row in rig.read_jsonl(c.state.paths.journal)] == ["sealed", STEP]


@pytest.mark.parametrize("kind", ["free", "missing"])
def test_iteration_preserves_existing_write_failed_action_at_new_position(case, monkeypatch, kind):
    c = case(1)
    r = observe(c, monkeypatch, kind=kind)
    fail_observation_fsync(monkeypatch, c.state.paths.journal)
    report = m2_runner.iteration(c.state, now=c.now, getters=r.getters)
    assert report.action == m2_runner.ACT_WRITE_FAILED and report.reasons == ["journal.jsonl: OSError"]
    assert len(r.returned) == 1 and c.fake.book_urls == [] and len(c.fake.time_calls) == 1
    assert c.state.counters["missing_fee_total"] == 0 and r.commits == []


def test_actual_cli_observation_fsync_failure_uses_original_exit_4(tmp_path, monkeypatch):
    paths = rig.isolate(monkeypatch, tmp_path)
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(m2_runner.forecaster, "utc_now", lambda: rig.stamp(now))
    rig.deliver(paths["run"], now)
    fake = rig.FakeGetters(now)
    fail_observation_fsync(monkeypatch, m2_runner.RunnerPaths.under(paths["run"]).journal)
    assert rig.run_once(paths["run"], fake) == m2_runner.EXIT_WRITE_FAILED == 4
    assert fake.book_urls == [] and len(fake.time_calls) == 1 and len(fake.gamma_urls) == 2
    assert [row["step"] for row in rig.read_jsonl(paths["run"] / "journal.jsonl")] == ["sealed", STEP]
    record, problem = heartbeat.read_beat(heartbeat.beat_path(m2_runner.PROC), m2_runner.PROC)
    assert problem is None and record["exit_code"] == 4


def test_real_iteration_retains_allow_ledger_and_original_es_book_evidence(case, monkeypatch):
    c = case(1)
    r = observe(c, monkeypatch, real_commit=True)
    report = m2_runner.iteration(c.state, now=c.now, getters=r.getters)
    assert report.action == m2_runner.ACT_RUN and (report.built, report.allows, report.vetoes) == (1, 1, 0)
    rows = rig.read_jsonl(c.state.paths.journal)
    assert [row["step"] for row in rows] == ["sealed", STEP, "built", "committed"]
    fact, built = rows[1:3]
    assert set(fact) == TOP_KEYS and fact["quote"] == dataclasses.asdict(r.returned[0])
    assert fact["iteration"] == 1 and fact["iteration_now"] == c.now.isoformat(timespec="microseconds")
    assert fact["forecast_id"] == built["forecast_id"]
    assert built["book_fetch_evidence"] == {
        token: {"fetched_at": c.now.isoformat(timespec="microseconds"), "source": "clob_public_book"}
        for token in rig.TOKENS}
    fills = rig.read_jsonl(c.paths["paper"])
    assert len(fills) == 1 and fills[0]["risk_decision"] == "allow"
    assert fills[0]["decision_id"] == built["decision_id"] and fills[0]["forecast_id"] == fact["forecast_id"]
    assert (fills[0]["fee_source"], fills[0]["fee_rate"]) == ("gamma", 0.0)
    _, ledger = ledger_store.read_ledger(c.paths["paper"])
    assert ledger_store.verify(ledger) == []
