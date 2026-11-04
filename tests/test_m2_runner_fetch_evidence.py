"""ES/#121: original accepted per-token fetch facts on built rows, offline only."""
from __future__ import annotations

import json
import socket
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from src import ledger_store, m2_runner, paper_ledger, podlib
from tests import m2_runner_rig as rig


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refused(*args, **kwargs):
        pytest.fail("ES offline fixture attempted network access")

    monkeypatch.setattr(socket.socket, "connect", refused)
    monkeypatch.setattr(socket, "create_connection", refused)
    monkeypatch.setattr(socket, "getaddrinfo", refused)
    monkeypatch.setattr(podlib, "urlopen", refused)


@pytest.fixture
def case(tmp_path, monkeypatch):
    paths = rig.isolate(monkeypatch, tmp_path)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    monkeypatch.setattr(m2_runner.forecaster, "utc_now", lambda: rig.stamp(now - timedelta(seconds=1)))
    state = m2_runner.RunnerState(paths=m2_runner.RunnerPaths.under(paths["run"]), monotonic=lambda: 1000.0)
    fake = rig.FakeGetters(now)
    m2_runner._scan(state, now, m2_runner.GuardedGetter(fake.gamma))
    rig.deliver(paths["run"], now)
    sealed = m2_runner.IterationReport(m2_runner.ACT_RUN)
    m2_runner._ingest(state, now, sealed)
    assert sealed.sealed == 1 and len(state.pending) == 1
    return SimpleNamespace(paths=paths, now=now, state=state, fake=fake)


def decide(case, monkeypatch, moments, *, responses=None, allow=False, fail_built=None):
    """Use real fetch_depth, snapshots, FeeCache and builder; trace only the commit boundary."""
    trace, intents, builds = [], [], []
    clock_values = iter(moments)
    replies = iter(responses) if responses is not None else None
    real_build = m2_runner.intent_builder.build_order_intent
    real_journal = m2_runner.journal
    counters = dict(case.state.counters)

    def clock():
        value = next(clock_values)
        trace.append(("clock", value))
        return value

    def gamma(url):
        trace.append(("get", "gamma"))
        return case.fake.gamma(url)

    def book(url):
        token = parse_qs(urlsplit(url).query)["token_id"][0]
        trace.append(("get", token))
        if replies is not None:
            response = next(replies)
            if isinstance(response, BaseException):
                raise response
            return response
        age = 2 if token == rig.TOKENS[0] else 1
        return rig.clob_book(token, case.now - timedelta(seconds=age))

    def after():
        trace.append(("after",))

    def build(*args, **kwargs):
        trace.append(("build",))
        result = real_build(*args, **kwargs)
        builds.append((dict(args[2]), kwargs, result))
        return result

    def commit(intent, cfg, **kwargs):
        trace.append(("commit",))
        intents.append((intent, cfg, kwargs))
        if allow:
            return {"allow": True, "fill": {"fill_id": "owned-synthetic-fill", "risk_decision": "allow"}}
        return {"allow": False, "decision": {"reasons": ["owned-test-veto"]}, "halted": False}

    def journal(state, step, now, **fields):
        trace.append(("journal", step))
        if step == "built" and fail_built is not None:
            raise fail_built
        return real_journal(state, step, now, **fields)

    case.state.clock = clock
    monkeypatch.setattr(m2_runner.intent_builder, "build_order_intent", build)
    monkeypatch.setattr(paper_ledger, "commit_if_allowed", commit)
    monkeypatch.setattr(m2_runner, "journal", journal)
    report = m2_runner.IterationReport(m2_runner.ACT_RUN)
    result = SimpleNamespace(trace=trace, intents=intents, builds=builds, report=report, counters_before=counters)
    case.result = result
    m2_runner._decide(case.state, case.now, podlib.load_risk(), m2_runner.GuardedGetter(gamma, after),
                      m2_runner.GuardedGetter(book, after), report)
    with pytest.raises(StopIteration):
        next(clock_values)
    return result


def built_row(case):
    return next(row for row in rig.read_jsonl(case.state.paths.journal) if row["step"] == "built")


@pytest.mark.parametrize("timing", ["distinct", "same_millisecond", "offsets"])
def test_built_preserves_each_original_fetch_time_and_parent_linkage(case, monkeypatch, timing):
    a = case.now + timedelta(seconds=1, microseconds=123)
    b = case.now + timedelta(seconds=2, microseconds=456)
    if timing == "same_millisecond":
        b = case.now + timedelta(seconds=1, microseconds=456)
    if timing == "offsets":
        a = a.astimezone(timezone(timedelta(hours=9)))
        b = b.astimezone(timezone(timedelta(hours=-4, minutes=-30)))
    decided = case.now + timedelta(seconds=3, microseconds=789)
    r = decide(case, monkeypatch, [a, b, decided])
    row = built_row(case)
    assert row["book_fetch_evidence"] == {
        rig.TOKENS[0]: {"fetched_at": a.isoformat(timespec="microseconds"), "source": "clob_public_book"},
        rig.TOKENS[1]: {"fetched_at": b.isoformat(timespec="microseconds"), "source": "clob_public_book"},
    }
    assert set(row) == {"ts", "step", "forecast_id", "intent_id", "decision_id", "idempotency_key",
                        "book_fetch_evidence"}
    intent, cfg, kwargs = r.intents[0]
    original_intent = r.builds[0][2].intent
    for key in ("forecast_id", "intent_id", "decision_id", "idempotency_key"):
        assert row[key] == getattr(original_intent, key)
    assert (intent.forecast_id, intent.decision_id) == (row["forecast_id"], row["decision_id"])
    assert row["ts"] == m2_runner._stamp(case.now)
    assert kwargs == {"unique_decision_id": True} and cfg == podlib.load_risk()
    assert intent.fee_rate == 0.0 and intent.fee_source == "gamma" and intent.fee_schedule_sha
    assert r.builds[0][1]["now"] is decided
    assert [value for kind, *value in r.trace if kind == "clock"] == [[a], [b], [decided]]
    raw_rows = rig.read_jsonl(case.state.paths.journal)
    fee_rows = [row for row in raw_rows if row["step"] == "fee_quote_evaluated"]
    assert len(fee_rows) == 1 and raw_rows[1] is fee_rows[0]
    assert set(fee_rows[0]) == {"ts", "step", "forecast_id", "market_id", "iteration", "iteration_now", "fee_available", "cache_hit", "quote"}
    assert set(fee_rows[0]["quote"]) == {"market_id", "fee_rate", "problem", "fee_source", "fee_gamma_fetched_at", "fee_schedule_sha", "cross_checked"}
    assert fee_rows[0]["fee_available"] is True and fee_rows[0]["cache_hit"] == "UNKNOWN"
    assert (fee_rows[0]["market_id"], fee_rows[0]["quote"]["fee_rate"],
            fee_rows[0]["quote"]["fee_source"]) == (rig.MARKET_ID, 0.0, "gamma")
    old_rows = [row for row in raw_rows if row["step"] != "fee_quote_evaluated"]
    assert fee_rows[0]["forecast_id"] == row["forecast_id"]
    assert r.trace.count(("journal", "fee_quote_evaluated")) == 1
    assert r.trace[2] == ("journal", "fee_quote_evaluated")
    old_trace = [event for event in r.trace if event != ("journal", "fee_quote_evaluated")]
    assert old_trace == [("get", "gamma"), ("after",), ("get", rig.TOKENS[0]), ("after",),
                       ("clock", a), ("get", rig.TOKENS[1]), ("after",), ("clock", b),
                       ("clock", decided), ("build",), ("journal", "built"), ("commit",),
                       ("journal", "vetoed")]
    assert [row["step"] for row in old_rows] == ["sealed", "built", "vetoed"]
    assert set(case.state.counters) == set(r.counters_before)
    expected = dict(r.counters_before, intents_built_total=1, vetoes_total=1)
    assert case.state.counters == expected


@pytest.mark.parametrize("allow", [False, True])
def test_original_commit_outcome_and_journal_order_are_retained(case, monkeypatch, allow):
    r = decide(case, monkeypatch, [case.now] * 3, allow=allow)
    raw_rows = rig.read_jsonl(case.state.paths.journal)
    fee_rows = [row for row in raw_rows if row["step"] == "fee_quote_evaluated"]
    assert len(fee_rows) == 1 and raw_rows[1] is fee_rows[0]
    assert set(fee_rows[0]) == {"ts", "step", "forecast_id", "market_id", "iteration", "iteration_now", "fee_available", "cache_hit", "quote"}
    assert set(fee_rows[0]["quote"]) == {"market_id", "fee_rate", "problem", "fee_source", "fee_gamma_fetched_at", "fee_schedule_sha", "cross_checked"}
    assert fee_rows[0]["fee_available"] is True and fee_rows[0]["cache_hit"] == "UNKNOWN"
    assert (fee_rows[0]["market_id"], fee_rows[0]["quote"]["fee_rate"],
            fee_rows[0]["quote"]["fee_source"]) == (rig.MARKET_ID, 0.0, "gamma")
    rows = [row for row in raw_rows if row["step"] != "fee_quote_evaluated"]

    assert [row["step"] for row in rows] == ["sealed", "built", "committed" if allow else "vetoed"]
    assert "book_fetch_evidence" in rows[1]
    assert all("book_fetch_evidence" not in row for row in (rows[0], rows[2]))
    assert (r.report.built, r.report.allows, r.report.vetoes) == (1, int(allow), int(not allow))
    assert len(r.intents) == 1
    assert bool(case.state.seen_keys) is allow
    assert len(case.state.pending) == int(not allow)
    if allow:
        assert rows[2]["fill_id"] == "owned-synthetic-fill" and len(r.report.fills) == 1
    else:
        assert rows[2]["reasons"] == ["owned-test-veto"] and r.report.fills == []


@pytest.mark.parametrize("missing", [None, [], OSError("owned failure")])
def test_partial_success_records_only_retained_successful_token(case, monkeypatch, missing):
    a = case.now + timedelta(seconds=1, microseconds=123)
    good = rig.clob_book(rig.TOKENS[0], case.now - timedelta(seconds=2))
    r = decide(case, monkeypatch, [a, case.now + timedelta(seconds=3)], responses=[good, missing])
    assert r.report.built == 1 and len(r.intents) == 1
    assert built_row(case)["book_fetch_evidence"] == {
        rig.TOKENS[0]: {"fetched_at": a.isoformat(timespec="microseconds"), "source": "clob_public_book"}}
    assert list(r.builds[0][0]) == [rig.TOKENS[0]]
    assert case.state.counters["book_get_failures_total"] == int(isinstance(missing, Exception))
    assert sum(event[0] == "clock" for event in r.trace) == 2
    assert sum(event[0] == "after" for event in r.trace) == 3


@pytest.mark.parametrize("second_missing", [False, True])
def test_duplicate_token_evidence_matches_final_retained_snapshot(case, monkeypatch, second_missing):
    token = rig.TOKENS[0]
    case.state.markets[rig.MARKET_ID]["token_ids"] = [token, token]
    first = rig.clob_book(token, case.now - timedelta(seconds=2))
    second = rig.clob_book(token, case.now - timedelta(seconds=1))
    a = case.now + timedelta(seconds=1, microseconds=123)
    b = case.now + timedelta(seconds=2, microseconds=456)
    moments = [a, case.now + timedelta(seconds=3)] if second_missing else [a, b, case.now + timedelta(seconds=3)]
    r = decide(case, monkeypatch, moments, responses=[first, None if second_missing else second])
    assert r.report.built == 1
    expected = a if second_missing else b
    assert built_row(case)["book_fetch_evidence"] == {
        token: {"fetched_at": expected.isoformat(timespec="microseconds"), "source": "clob_public_book"}}
    retained = r.builds[0][0][token]
    stamp = first["timestamp"] if second_missing else second["timestamp"]
    assert datetime.fromisoformat(retained.book_ts.replace("Z", "+00:00")).timestamp() == int(stamp) / 1000


@pytest.mark.parametrize("blocked", ["stale", "missing_timestamp", "before_resume"])
def test_rejected_book_keeps_original_skip_without_built_or_evidence(case, monkeypatch, blocked):
    first = rig.clob_book(rig.TOKENS[0], case.now)
    if blocked == "stale":
        first = rig.clob_book(rig.TOKENS[0], case.now - timedelta(seconds=m2_runner.MAX_BOOK_AGE_S + 2))
    elif blocked == "missing_timestamp":
        first.pop("timestamp")
    else:
        case.state.last_resume_at = case.now + timedelta(seconds=2)
    r = decide(case, monkeypatch, [case.now + timedelta(seconds=1)], responses=[first])
    raw_rows = rig.read_jsonl(case.state.paths.journal)
    fee_rows = [row for row in raw_rows if row["step"] == "fee_quote_evaluated"]
    assert len(fee_rows) == 1 and raw_rows[1] is fee_rows[0]
    assert set(fee_rows[0]) == {"ts", "step", "forecast_id", "market_id", "iteration", "iteration_now", "fee_available", "cache_hit", "quote"}
    assert set(fee_rows[0]["quote"]) == {"market_id", "fee_rate", "problem", "fee_source", "fee_gamma_fetched_at", "fee_schedule_sha", "cross_checked"}
    assert fee_rows[0]["fee_available"] is True and fee_rows[0]["cache_hit"] == "UNKNOWN"
    assert (fee_rows[0]["market_id"], fee_rows[0]["quote"]["fee_rate"],
            fee_rows[0]["quote"]["fee_source"]) == (rig.MARKET_ID, 0.0, "gamma")
    rows = [row for row in raw_rows if row["step"] != "fee_quote_evaluated"]

    assert [row["step"] for row in rows] == ["sealed", "skipped"]
    assert all("book_fetch_evidence" not in row for row in rows)
    assert (r.report.built, r.report.skipped) == (0, 1) and not r.builds and not r.intents
    assert sum(event[0] == "clock" for event in r.trace) == 1
    if blocked == "before_resume":
        assert rows[-1]["reasons"] == ["book_before_resume"]


def test_original_nonbuilt_decision_emits_no_fetch_evidence(case, monkeypatch):
    case.state.markets[rig.MARKET_ID]["liquidity_usd"] = 0
    r = decide(case, monkeypatch, [case.now] * 3)
    raw_rows = rig.read_jsonl(case.state.paths.journal)
    fee_rows = [row for row in raw_rows if row["step"] == "fee_quote_evaluated"]
    assert len(fee_rows) == 1 and raw_rows[1] is fee_rows[0]
    assert set(fee_rows[0]) == {"ts", "step", "forecast_id", "market_id", "iteration", "iteration_now", "fee_available", "cache_hit", "quote"}
    assert set(fee_rows[0]["quote"]) == {"market_id", "fee_rate", "problem", "fee_source", "fee_gamma_fetched_at", "fee_schedule_sha", "cross_checked"}
    assert fee_rows[0]["fee_available"] is True and fee_rows[0]["cache_hit"] == "UNKNOWN"
    assert (fee_rows[0]["market_id"], fee_rows[0]["quote"]["fee_rate"],
            fee_rows[0]["quote"]["fee_source"]) == (rig.MARKET_ID, 0.0, "gamma")
    rows = [row for row in raw_rows if row["step"] != "fee_quote_evaluated"]

    assert [row["step"] for row in rows] == ["sealed", "skipped"]
    assert "liquidity_below_min" in rows[-1]["reasons"]
    assert all("book_fetch_evidence" not in row for row in rows)
    assert r.report.built == 0 and not r.intents and len(r.builds) == 1


def test_all_missing_books_keep_original_nonbuilt_result(case, monkeypatch):
    r = decide(case, monkeypatch, [case.now], responses=[None, None])
    assert r.report.built == 0 and not r.intents
    assert "missing_book" in rig.read_jsonl(case.state.paths.journal)[-1]["reasons"]
    assert all("book_fetch_evidence" not in row for row in rig.read_jsonl(case.state.paths.journal))


def test_original_built_write_failure_propagates_before_commit(case, monkeypatch):
    failure = m2_runner.WriteFailed("owned built append failure")
    with pytest.raises(m2_runner.WriteFailed) as caught:
        decide(case, monkeypatch, [case.now] * 3, fail_built=failure)
    assert caught.value is failure
    assert case.result.intents == [] and case.result.report.built == 0
    assert case.state.counters["intents_built_total"] == 0
    raw_rows = rig.read_jsonl(case.state.paths.journal)
    fee_rows = [row for row in raw_rows if row["step"] == "fee_quote_evaluated"]
    assert len(fee_rows) == 1 and raw_rows[1] is fee_rows[0]
    assert set(fee_rows[0]) == {"ts", "step", "forecast_id", "market_id", "iteration", "iteration_now", "fee_available", "cache_hit", "quote"}
    assert set(fee_rows[0]["quote"]) == {"market_id", "fee_rate", "problem", "fee_source", "fee_gamma_fetched_at", "fee_schedule_sha", "cross_checked"}
    assert fee_rows[0]["fee_available"] is True and fee_rows[0]["cache_hit"] == "UNKNOWN"
    assert (fee_rows[0]["market_id"], fee_rows[0]["quote"]["fee_rate"],
            fee_rows[0]["quote"]["fee_source"]) == (rig.MARKET_ID, 0.0, "gamma")
    old_rows = [row for row in raw_rows if row["step"] != "fee_quote_evaluated"]
    assert fee_rows[0]["forecast_id"] == next(iter(case.state.pending))
    assert [row["step"] for row in old_rows] == ["sealed"]


def test_actual_iteration_books_one_allowed_fill_with_original_ids_and_fee(tmp_path, monkeypatch):
    paths = rig.isolate(monkeypatch, tmp_path)
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(m2_runner.forecaster, "utc_now", lambda: rig.stamp(now))
    rig.deliver(paths["run"], now)
    state = m2_runner.RunnerState(paths=m2_runner.RunnerPaths.under(paths["run"]),
                                  clock=lambda: now, monotonic=lambda: 1000.0)
    state.paths.run_dir.mkdir(parents=True, exist_ok=True)
    fake = rig.FakeGetters(now)
    report = m2_runner.iteration(state, now=now, getters=fake.getters())
    assert report.action == m2_runner.ACT_RUN and (report.built, report.allows, report.vetoes) == (1, 1, 0)
    fills = rig.read_jsonl(paths["paper"])
    assert len(fills) == 1
    fill = fills[0]
    raw_rows = rig.read_jsonl(state.paths.journal)
    fee_rows = [row for row in raw_rows if row["step"] == "fee_quote_evaluated"]
    assert len(fee_rows) == 1 and raw_rows[1] is fee_rows[0]
    assert set(fee_rows[0]) == {"ts", "step", "forecast_id", "market_id", "iteration", "iteration_now", "fee_available", "cache_hit", "quote"}
    assert set(fee_rows[0]["quote"]) == {"market_id", "fee_rate", "problem", "fee_source", "fee_gamma_fetched_at", "fee_schedule_sha", "cross_checked"}
    assert fee_rows[0]["fee_available"] is True and fee_rows[0]["cache_hit"] == "UNKNOWN"
    assert (fee_rows[0]["market_id"], fee_rows[0]["quote"]["fee_rate"],
            fee_rows[0]["quote"]["fee_source"]) == (rig.MARKET_ID, 0.0, "gamma")
    rows = [row for row in raw_rows if row["step"] != "fee_quote_evaluated"]
    assert fee_rows[0]["forecast_id"] == fill["forecast_id"]
    assert fee_rows[0]["iteration"] == 1
    assert fee_rows[0]["iteration_now"] == now.isoformat(timespec="microseconds")
    assert [row["step"] for row in rows] == ["sealed", "built", "committed"]
    assert rows[1]["book_fetch_evidence"] == {
        token: {"fetched_at": now.isoformat(timespec="microseconds"), "source": "clob_public_book"}
        for token in rig.TOKENS}
    assert rows[1]["forecast_id"] == fill["forecast_id"] and rows[1]["decision_id"] == fill["decision_id"]
    assert rows[2]["fill_id"] == fill["fill_id"]
    assert (fill["risk_decision"], fill["fee_source"], fill["fee_rate"]) == ("allow", "gamma", 0.0)
    assert fill["fee_schedule_sha"] and fill["calibration_problems"] == []
    _, ledger = ledger_store.read_ledger(paths["paper"])
    assert ledger_store.verify(ledger) == []
    assert (len(fake.gamma_urls), len(fake.book_urls), len(fake.time_calls)) == (2, 2, 1)


def test_historical_built_missing_evidence_stays_unknown_and_bytes_unchanged(tmp_path):
    paths = m2_runner.RunnerPaths.under(tmp_path / "owned-run")
    paths.run_dir.mkdir(parents=True)
    old = {"ts": "2026-10-01T00:00:00.000Z", "step": "built", "forecast_id": "fc_old",
           "intent_id": "old-intent", "decision_id": "dc_old", "idempotency_key": "old-key"}
    raw = (json.dumps(old) + "\n").encode()
    paths.journal.write_bytes(raw)
    state = m2_runner.recover(paths, tmp_path / "owned-missing-ledger.jsonl", now=datetime.now(timezone.utc))
    assert paths.journal.read_bytes() == raw
    rows = m2_runner._read_journal(paths.journal)
    assert rows == [old] and "book_fetch_evidence" not in rows[0]
    assert "old-key" in state.seen_keys
