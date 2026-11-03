"""EQ / #345: book getter failures are observations, with original control behavior.

Only owned temporary paths, fake getters and opener scripts; no external network.
"""
from __future__ import annotations

import copy
import socket
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError

import pytest

from src import book, halt_store, heartbeat, m2_runner, podlib, sentinel_tick
from tests import m2_runner_rig as rig
from tests.test_public_get_retry import fake_net  # noqa: F401 - real offline opener fixture

KEY = "book_get_failures_total"
BOOK_URL = podlib.BOOK_PREFIX + "token_id=" + rig.TOKENS[0]
BASE_PROGRESS_KEYS = {
    "iteration", "last_scan_ts", "scan_age_s", "forecasts_sealed_total", "forecasts_rejected_total",
    "intents_built_total", "allows_total", "vetoes_total", "missing_fee_total", "scans_total",
    "scan_failures_total", "last_book_age_s_max", "resume_count", "paused_reason", "paused_since",
    "disk_free_gb", "ledger_rows", "pending_signals", "executor_enabled", "clock_anomaly",
    "clock_anomaly_since", "last_time_check", "time_check_failures_total", "time_check_failures_by_class",
    "settlement_get_failures_total",
}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("external network attempted")
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)


@pytest.fixture
def env(tmp_path, monkeypatch):
    paths = rig.isolate(monkeypatch, tmp_path)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    paths["run"].mkdir(parents=True)
    monkeypatch.setattr(m2_runner.forecaster, "utc_now", lambda: rig.stamp(now))
    state = m2_runner.RunnerState(paths=m2_runner.RunnerPaths.under(paths["run"]), clock=lambda: now,
                                  monotonic=lambda: 1000.0)
    return {**paths, "now": now, "state": state}


class BookAnswers(rig.FakeGetters):
    def __init__(self, now, answers=None):
        super().__init__(now)
        self.answers = answers or {}

    def book(self, url):
        if url == podlib.TIME_URL:
            return super().book(url)
        self.book_urls.append(url)
        token = rig.parse_qs(rig.urlsplit(url).query)["token_id"][0]
        value = self.answers.get(token, rig.clob_book(token, self.now))
        if isinstance(value, BaseException):
            raise value
        return value


def tick(env, answers=None):
    fake = BookAnswers(env["now"], answers)
    report = m2_runner.iteration(env["state"], now=env["now"], getters=fake.getters())
    return report, fake


def test_progress_adds_only_one_process_counter_and_is_read_only(env):
    before = copy.deepcopy(env["state"].counters)
    first = m2_runner.progress(env["state"], env["now"])
    assert set(first) == BASE_PROGRESS_KEYS | {KEY}
    assert type(first[KEY]) is int and first[KEY] == 0
    assert env["state"].counters == before
    assert m2_runner.progress(env["state"], env["now"]) == first
    first[KEY] = 999
    assert env["state"].counters[KEY] == 0


@pytest.mark.parametrize("make", [
    lambda: HTTPError(BOOK_URL, 503, "offline", {}, None),
    lambda: TimeoutError("offline"),
    lambda: URLError(TimeoutError("offline")),
    lambda: ValueError("getter decoding failed"),
], ids=["503", "timeout", "wrapped-timeout", "getter-decode"])
def test_real_iteration_counts_failures_hidden_by_fetch_depth(env, make):
    rig.deliver(env["run"], env["now"])
    before = halt_store.active_halts()
    report, fake = tick(env, {token: make() for token in rig.TOKENS})
    assert report.action == m2_runner.ACT_RUN and report.allows == report.built == 0
    assert report.skipped == 1 and len(fake.book_urls) == 2 and fake.time_calls == [podlib.TIME_URL]
    assert rig.read_jsonl(env["paper"]) == [] and halt_store.active_halts() == before
    state = env["state"]
    assert state.counters["time_check_failures_total"] == 0
    assert state.time_check_failures_by_class == {c: 0 for c in m2_runner.TIME_FAILURE_CLASSES}
    assert state.last_time_check["result"] == "ok"
    assert state.last_time_check["http_status"] is None and state.last_time_check["exc_type"] is None
    assert m2_runner.progress(state, env["now"])[KEY] == 2


@pytest.mark.parametrize("error", [HTTPError(BOOK_URL, 503, "offline", {}, None), TimeoutError("offline"),
                                   URLError(TimeoutError("offline")), ValueError("decode")])
def test_observer_reraises_exact_getter_exception_once(env, error):
    calls = []
    before = copy.deepcopy(env["state"].counters)
    def getter(url):
        calls.append(url)
        raise error
    with pytest.raises(type(error)) as info:
        m2_runner._book_get_observed(env["state"], getter, BOOK_URL)
    assert info.value is error and calls == [BOOK_URL]
    before[KEY] += 1
    assert env["state"].counters == before


@pytest.mark.parametrize("value", [None, [], False, 0, "not an object", {}, {"timestamp": "bad"}])
def test_normal_returns_are_not_getter_failures(env, value):
    calls = []
    def getter(url):
        calls.append(url)
        return value
    observed = lambda url: m2_runner._book_get_observed(env["state"], getter, url)
    assert observed(BOOK_URL) is value
    raw = book.fetch_depth(rig.TOKENS[0], getter=observed)
    assert (raw is not None) == isinstance(value, dict)
    assert calls == [BOOK_URL, BOOK_URL] and env["state"].counters[KEY] == 0


@pytest.mark.parametrize("token", ["", "  ", None, False, 0])
def test_fetch_depth_without_a_request_never_counts(env, token):
    calls = []
    def getter(url):
        calls.append(url)
        raise TimeoutError("must not run")
    assert book.fetch_depth(token, getter=lambda url: m2_runner._book_get_observed(env["state"], getter, url)) is None
    assert calls == [] and env["state"].counters[KEY] == 0


@pytest.mark.parametrize("outcome", ["success", "timeout", "refused"])
@pytest.mark.parametrize("hook_error", [False, True])
def test_original_getter_after_order_and_hook_outcome(env, outcome, hook_error):
    trace = []
    value = {"owned": True}
    error = TimeoutError("offline") if outcome == "timeout" else m2_runner.RefusedURL("offline")
    def getter(url):
        trace.append("getter")
        if outcome != "success":
            raise error
        return value
    def after():
        trace.append("after")
        if hook_error:
            raise ValueError("ordinary hook errors remain swallowed")
    guard = m2_runner.GuardedGetter(getter, after)
    if outcome == "success":
        assert m2_runner._book_get_observed(env["state"], guard, BOOK_URL) is value
    else:
        with pytest.raises(type(error)) as info:
            m2_runner._book_get_observed(env["state"], guard, BOOK_URL)
        assert info.value is error
    assert trace == ["getter", "after"] and guard.calls == [BOOK_URL]
    assert env["state"].counters[KEY] == int(outcome == "timeout")


@pytest.mark.parametrize("error", [KeyboardInterrupt("offline"), SystemExit(17)])
def test_baseexception_keeps_original_propagation_after_hook_and_no_count(env, error):
    trace = []
    def getter(url):
        trace.append("getter")
        raise error
    guard = m2_runner.GuardedGetter(getter, lambda: trace.append("after"))
    with pytest.raises(type(error)) as info:
        book.fetch_depth(rig.TOKENS[0], getter=lambda url: m2_runner._book_get_observed(env["state"], guard, url))
    assert info.value is error and trace == ["getter", "after"]
    assert env["state"].counters[KEY] == 0


def test_refused_url_three_original_entrypoints(env, monkeypatch):
    called = []
    guard = m2_runner.GuardedGetter(lambda url: called.append("inner"), lambda: called.append("after"))
    with pytest.raises(m2_runner.RefusedURL):
        m2_runner._book_get_observed(env["state"], guard, "https://not-allowlisted.invalid/book")
    assert called == [] and guard.calls == [] and len(guard.refused) == 1
    error = m2_runner.RefusedURL("offline sentinel")
    def refused(url):
        called.append("inner")
        raise error
    guard = m2_runner.GuardedGetter(refused, lambda: called.append("after"))
    assert book.fetch_depth(rig.TOKENS[0], getter=lambda url: m2_runner._book_get_observed(env["state"], guard, url)) is None
    assert called == ["inner", "after"] and guard.calls == [BOOK_URL]
    # A refusal outside fetch_depth's get try-block still reaches _decide's original re-raise.
    def depth(*args, **kwargs):
        raise error
    monkeypatch.setattr(book, "fetch_depth", depth)
    rig.deliver(env["run"], env["now"])
    with pytest.raises(m2_runner.RefusedURL) as info:
        tick(env)
    assert info.value is error and env["state"].counters[KEY] == 0


@pytest.mark.parametrize("stamp", [None, "bad", True])
def test_bad_book_timestamp_preserves_veto_but_is_not_getter_failure(env, stamp):
    rig.deliver(env["run"], env["now"])
    body = rig.clob_book(rig.TOKENS[0], env["now"])
    body["timestamp"] = stamp
    report, fake = tick(env, {rig.TOKENS[0]: body})
    assert report.action == m2_runner.ACT_RUN and report.skipped == 1 and report.allows == 0
    assert env["state"].tick_clock_vetoed is True and len(fake.book_urls) == 1
    assert env["state"].counters[KEY] == 0 and env["state"].counters["time_check_failures_total"] == 0


@pytest.mark.parametrize("where", ["parse", "snapshot", "local-clock"])
def test_post_get_errors_are_not_getter_failures(env, monkeypatch, where):
    rig.deliver(env["run"], env["now"])
    error = ValueError("owned post-get error")
    def broken(*args, **kwargs):
        raise error
    if where == "parse":
        monkeypatch.setattr(book, "book_timestamp", broken)
        report, fake = tick(env)
        assert report.action == m2_runner.ACT_RUN and report.skipped == 1 and len(fake.book_urls) == 2
    else:
        fake = BookAnswers(env["now"])
        if where == "snapshot":
            monkeypatch.setattr(m2_runner, "book_snapshot_from_clob", broken)
        else:
            original = fake.book
            def change_clock_after_success(url):
                value = original(url)
                if url != podlib.TIME_URL:
                    env["state"].clock = broken
                return value
            fake.book = change_clock_after_success
        with pytest.raises(ValueError) as info:
            m2_runner.iteration(env["state"], now=env["now"], getters=fake.getters())
        assert info.value is error and len(fake.book_urls) == 1
    assert env["state"].counters[KEY] == 0


@pytest.mark.parametrize("make,attempts", [
    (lambda: HTTPError(BOOK_URL, 425, "offline", {}, None), 1),
    (lambda: HTTPError(BOOK_URL, 403, "offline", {}, None), 1),
    (lambda: HTTPError(BOOK_URL, 429, "offline", {}, None), 3),
    (lambda: HTTPError(BOOK_URL, 503, "offline", {}, None), 3),
    (lambda: TimeoutError("offline"), 3),
    (lambda: podlib._DeadlineExceeded("offline"), 1),
    (lambda: ValueError("getter JSON decode"), 1),
])
def test_real_public_get_attempts_are_one_logical_failure(env, monkeypatch, fake_net, make, attempts):
    fake_net["set"](make())
    monkeypatch.setattr(podlib, "_monotonic", lambda: 0.0)
    captured, after = [], []
    def real(url):
        try:
            return podlib.public_get(url, attempts=3, backoff_s=0, deadline_s=2)
        except Exception as error:
            captured.append(error)
            raise
    guard = m2_runner.GuardedGetter(real, lambda: after.append("after"))
    assert book.fetch_depth(rig.TOKENS[0], getter=lambda url: m2_runner._book_get_observed(env["state"], guard, url)) is None
    assert fake_net["calls"] == [BOOK_URL] * attempts and after == ["after"] and guard.calls == [BOOK_URL]
    assert len(captured) == 1 and env["state"].counters[KEY] == 1
    if attempts > 1:
        assert isinstance(captured[0], podlib.PublicGetError) and captured[0].attempts == attempts
        assert isinstance(captured[0].__cause__, type(make()))


@pytest.mark.parametrize("error", [HTTPError(BOOK_URL, 503, "offline", {}, None),
                                   HTTPError(BOOK_URL, 429, "offline", {}, None), TimeoutError("offline")])
def test_real_public_get_failed_attempt_then_success_does_not_count(env, monkeypatch, fake_net, error):
    fake_net["set"](error, rig.clob_book(rig.TOKENS[0], env["now"]))
    monkeypatch.setattr(podlib, "_monotonic", lambda: 0.0)
    guard = m2_runner.GuardedGetter(lambda url: podlib.public_get(url, attempts=3, backoff_s=0, deadline_s=2))
    raw = book.fetch_depth(rig.TOKENS[0], getter=lambda url: m2_runner._book_get_observed(env["state"], guard, url))
    assert raw["book_ts"] is not None and fake_net["calls"] == [BOOK_URL] * 2 and guard.calls == [BOOK_URL]
    assert env["state"].counters[KEY] == 0


@pytest.mark.parametrize("failed_tokens", [rig.TOKENS, rig.TOKENS[:1]])
def test_iteration_mixed_tokens_recovers_without_reset_or_new_controls(env, failed_tokens):
    rig.deliver(env["run"], env["now"])
    before = halt_store.active_halts()
    first, fake = tick(env, {token: TimeoutError("offline") for token in failed_tokens})
    assert first.action == m2_runner.ACT_RUN and first.skipped == 1 and first.allows == 0
    assert len(fake.book_urls) == 2 and env["state"].counters[KEY] == len(failed_tokens)
    assert env["state"].clock_anomaly == ("clock_unverified" if len(failed_tokens) == 2 else None)
    second, healthy = tick(env)
    assert second.action == m2_runner.ACT_RUN and second.allows == 1 and second.skipped == second.vetoes == 0
    assert healthy.time_calls == [podlib.TIME_URL] and len(healthy.book_urls) == 2
    assert len(rig.read_jsonl(env["paper"])) == 1 and halt_store.active_halts() == before
    state = env["state"]
    assert state.clock_anomaly is None and state.paused_reason is None and state.paused_since is None
    assert state.counters[KEY] == len(failed_tokens) and state.counters["time_check_failures_total"] == 0
    assert state.time_check_failures_by_class == {c: 0 for c in m2_runner.TIME_FAILURE_CLASSES}
    assert state.last_time_check == {"ts": m2_runner._stamp(env["now"]), "result": "ok", "low_s": -1.0,
                                     "high_s": 1.0, "rtt_s": 0.0, "http_status": None, "exc_type": None}
    fresh = m2_runner.RunnerState(paths=state.paths)
    assert fresh.counters[KEY] == 0  # no claim of persistence across a restart


def test_old_and_new_heartbeats_use_real_read_validator_and_sentinel(env):
    resident, problem = heartbeat.read_beat(heartbeat.beat_path("sentinel"), "sentinel")
    assert problem is None and resident is not None  # own conftest resident, not a new empty heartbeat root
    legacy = m2_runner.progress(env["state"], env["now"])
    legacy.pop(KEY)
    beat = heartbeat.Heartbeat(m2_runner.PROC, every_s=1.0)
    ledger = env["run"] / "owned-empty-ledger.jsonl"
    ledger.write_text("", encoding="utf-8")
    before = halt_store.active_halts()
    for progress in (legacy, {**legacy, KEY: 3}):
        assert beat.beat(heartbeat.STATE_RUNNING, progress=progress, force=True) is None
        record, problem = heartbeat.read_beat(beat.path, m2_runner.PROC)
        assert problem is None and heartbeat.validate_record(record, m2_runner.PROC) == record
        assert record["progress"] == progress
        assert record["progress"].get(KEY, "UNKNOWN") == progress.get(KEY, "UNKNOWN")
        result = sentinel_tick.tick(now=datetime.now(timezone.utc), bus_path=env["run"] / "owned-bus.jsonl",
                                    quotes_path=env["run"] / "owned-quotes.jsonl", ledger_path=ledger)
        assert result["checked"][m2_runner.PROC] == heartbeat.STATE_RUNNING
        assert halt_store.active_halts() == before
