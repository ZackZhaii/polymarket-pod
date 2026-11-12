"""ER / #345: only logical settlement getter exceptions gain a counter.

Real readers/collectors/runner, owned synthetic ledgers and fake network only.
"""
from __future__ import annotations

import copy
import socket
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError

import pytest

from src import halt_store, heartbeat, m2_runner, paper_ledger, podlib, resolutions, resolution_watch, sentinel_tick
from tests import m2_runner_rig as rig
from tests import test_runner_settlement as co
from tests.test_m2_runner_heartbeat_clock import NEW_KEYS, OLD_KEYS
from tests.test_public_get_retry import fake_net  # noqa: F401 - actual offline opener fixture

env = co.env  # reuse the original owned settlement paths and real ledger_store.book fixture
KEY = "settlement_get_failures_total"
URL = resolution_watch.gamma_url(rig.MARKET_ID, closed=True)


@pytest.fixture(autouse=True)
def writeoff_hook_is_not_under_test(monkeypatch):
    """#345 counts the COLLECTOR's reads and failures only (every count below is that of the settlement step). The
    close-then-write-off hook (#342) reads Gamma too, on its own, and is covered by tests/test_writeoff.py: here it
    is the dev stub (no read, no problem), exactly the body #345 was written against. A test may patch it again."""
    monkeypatch.setattr(m2_runner, "_writeoff_hook", lambda *args, **kwargs: [])


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("external network attempted")
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)


class Views(co.Gamma):
    def __init__(self, responses=None, rows=None):
        super().__init__()
        self.responses = responses or {}
        self.rows = rows or {rig.MARKET_ID: co.resolved_row()}

    def __call__(self, url):
        query = rig.parse_qs(rig.urlsplit(url).query)
        if "id" not in query:
            return super().__call__(url)  # original successful empty scan
        self.urls.append(url)
        market = query["id"][0]
        closed = query.get("closed") == ["true"]
        row = self.rows.get(market)
        default = {"markets": [row] if row is not None and closed else []}
        value = self.responses.get((market, closed), default)
        if isinstance(value, BaseException):
            raise value
        return value


def test_progress_adds_one_process_counter_and_remains_read_only(env):
    state = env["state"]
    before = copy.deepcopy(state.counters)
    progress = m2_runner.progress(state, co.NOW)
    assert set(progress) == OLD_KEYS | NEW_KEYS | {KEY}
    assert type(progress[KEY]) is int and progress[KEY] == 0
    assert state.counters == before and m2_runner.progress(state, co.NOW) == progress
    progress[KEY] = 999
    assert state.counters[KEY] == 0


@pytest.mark.parametrize("closed", [True, False], ids=["first-view", "second-view"])
@pytest.mark.parametrize("make", [
    lambda: HTTPError(URL, 503, "offline", {}, None),
    lambda: TimeoutError("offline"),
    lambda: URLError(TimeoutError("offline")),
], ids=["http", "timeout", "wrapped"])
def test_actual_iteration_read_failure_preserves_skip_without_snapshot_or_apply(env, monkeypatch, closed, make):
    co._hold_loser(env)
    applied = []
    def apply(*args, **kwargs):
        applied.append(True)
        raise AssertionError("failed collection must not apply")
    monkeypatch.setattr(paper_ledger, "apply_settlements", apply)
    error = make()
    getter = Views({(rig.MARKET_ID, closed): error})
    before = halt_store.active_halts()
    report = co._tick(env, getter)
    assert report.action == m2_runner.ACT_RUN and co._settlements(env) == []
    assert not env["res"].exists() and applied == [] and halt_store.active_halts() == before
    assert len(getter.id_reads) == (1 if closed else 2)
    events = co._events(env, "settlement_skipped")
    assert len(events) == 1 and events[0]["reasons"] == [rig.MARKET_ID + ":gamma_read_failed:" + type(error).__name__]
    assert env["state"].counters["time_check_failures_total"] == 0
    assert env["state"].counters["book_get_failures_total"] == 0
    assert m2_runner.progress(env["state"], co.NOW)[KEY] == 1


def observed(env, getter):
    return lambda url: m2_runner._settlement_get_observed(env["state"], getter, url)


def two_held(env):
    co._hold_loser(env)
    co._book(env, [co.fill("buy", 5.0, 0.5, rig.stamp(co.NOW), market="900004", token="o2")], prefix="sibling")
    return {rig.MARKET_ID: co.resolved_row(),
            "900004": co.resolved_row(id="900004", conditionId="0xother", clobTokenIds='["o1","o2"]')}


@pytest.mark.parametrize("error", [HTTPError(URL, 503, "offline", {}, None), TimeoutError("offline"),
                                   URLError(TimeoutError("offline")), ValueError("getter decode")])
def test_observer_counts_once_and_reraises_original_object(env, error):
    calls = []
    before = copy.deepcopy(env["state"].counters)
    def getter(url):
        calls.append(url)
        raise error
    with pytest.raises(type(error)) as info:
        observed(env, getter)(URL)
    assert info.value is error and calls == [URL]
    before[KEY] += 1
    assert env["state"].counters == before


@pytest.mark.parametrize("value", [None, [], False, "bad body", {}, {"markets": []}])
def test_normal_getter_returns_are_exact_and_do_not_count(env, value):
    assert observed(env, lambda url: value)(URL) is value
    assert env["state"].counters[KEY] == 0


@pytest.mark.parametrize("outcome", ["success", "timeout", "refused"])
@pytest.mark.parametrize("hook_error", [False, True])
def test_guard_after_order_and_ordinary_hook_errors_remain_original(env, outcome, hook_error):
    trace, value = [], {"markets": []}
    error = TimeoutError("offline") if outcome == "timeout" else m2_runner.RefusedURL("offline")
    def getter(url):
        trace.append("getter")
        if outcome != "success":
            raise error
        return value
    def after():
        trace.append("after")
        if hook_error:
            raise ValueError("hook remains isolated")
    guard = m2_runner.GuardedGetter(getter, after)
    if outcome == "success":
        assert observed(env, guard)(URL) is value
    else:
        with pytest.raises(type(error)) as info:
            observed(env, guard)(URL)
        assert info.value is error
    assert trace == ["getter", "after"] and guard.calls == [URL]
    assert env["state"].counters[KEY] == int(outcome == "timeout")


@pytest.mark.parametrize("error", [KeyboardInterrupt("offline"), SystemExit(19)])
def test_baseexception_crosses_real_reader_and_settlement_unchanged(env, error):
    co._hold_loser(env)
    trace = []
    def getter(url):
        trace.append("getter")
        raise error
    guard = m2_runner.GuardedGetter(getter, lambda: trace.append("after"))
    with pytest.raises(type(error)) as info:
        m2_runner._settle_tick(env["state"], co.NOW, guard)
    assert info.value is error and trace == ["getter", "after"]
    assert env["state"].counters[KEY] == 0 and not env["res"].exists()


def test_refused_url_guard_reader_and_outer_entrypoints_keep_original_behavior(env, monkeypatch):
    trace = []
    guard = m2_runner.GuardedGetter(lambda url: trace.append("inner"), lambda: trace.append("after"))
    with pytest.raises(m2_runner.RefusedURL):
        observed(env, guard)("https://not-allowlisted.invalid/markets")
    assert trace == [] and guard.calls == [] and len(guard.refused) == 1
    error = m2_runner.RefusedURL("offline refusal")
    def refused(url):
        trace.append("inner")
        raise error
    co._hold_loser(env)
    guard = m2_runner.GuardedGetter(refused, lambda: trace.append("after"))
    assert m2_runner._settle_tick(env["state"], co.NOW, guard) == []
    assert trace == ["inner", "after"] and guard.calls == [URL]
    assert co._events(env, "settlement_skipped")[0]["reasons"] == [rig.MARKET_ID + ":gamma_read_failed:RefusedURL"]
    def outer(*args, **kwargs):
        raise error
    monkeypatch.setattr(resolution_watch, "collect", outer)
    with pytest.raises(m2_runner.RefusedURL) as info:
        m2_runner._settle_tick(env["state"], co.NOW, guard)
    assert info.value is error and env["state"].counters[KEY] == 0


def test_two_market_failures_accumulate_once_each_and_success_never_resets(env):
    rows = two_held(env)
    failed = Views({(market, True): TimeoutError("offline") for market in rows}, rows)
    assert m2_runner._settle_tick(env["state"], co.NOW, m2_runner.GuardedGetter(failed)) == []
    assert len(failed.id_reads) == 2 and env["state"].counters[KEY] == 2
    assert co._settlements(env) == [] and not env["res"].exists()
    healthy = Views(rows=rows)
    assert m2_runner._settle_tick(env["state"], co.NOW, m2_runner.GuardedGetter(healthy)) == []
    assert len(healthy.id_reads) == 4 and len(co._settlements(env)) == 2
    assert env["state"].counters[KEY] == 2 and not co._global_halt()
    assert env["state"].counters["book_get_failures_total"] == 0
    assert env["state"].counters["time_check_failures_total"] == 0
    assert env["state"].time_check_failures_by_class == {c: 0 for c in m2_runner.TIME_FAILURE_CLASSES}
    assert env["state"].last_time_check is None
    assert m2_runner.RunnerState(paths=env["state"].paths).counters[KEY] == 0


@pytest.mark.parametrize("case", ["empty", "blocked", "bad-id"])
def test_no_settlement_request_means_no_getter_failure(env, case):
    if case == "blocked":
        co._hold_loser(env)
        halt_store.record_halt(scope=halt_store.SCOPE_MARKET, market=rig.MARKET_ID,
                               reason="invalid_settlement_payout:owned", source="RiskGate")
    elif case == "bad-id":
        co._book(env, [co.fill("buy", 2.0, 0.5, rig.stamp(co.NOW), market="bad-id", token="owned")])
    calls = []
    def getter(url):
        calls.append(url)
        raise AssertionError("must not request")
    assert m2_runner._settle_tick(env["state"], co.NOW, m2_runner.GuardedGetter(getter)) == []
    assert calls == [] and env["state"].counters[KEY] == 0


@pytest.mark.parametrize("response", [None, [], {}, {"markets": None}, {"markets": []},
                                      {"markets": [{"id": rig.MARKET_ID, "closed": "yes"}]}])
def test_collection_schema_or_missing_failures_are_not_getter_exceptions(env, response):
    co._hold_loser(env)
    getter = Views({(rig.MARKET_ID, True): response})
    assert m2_runner._settle_tick(env["state"], co.NOW, m2_runner.GuardedGetter(getter)) == []
    assert co._events(env, "settlement_skipped") and not env["res"].exists()
    assert co._settlements(env) == [] and env["state"].counters[KEY] == 0


def test_classification_hold_is_not_a_getter_failure(env):
    co._hold_loser(env)
    getter = Views(rows={rig.MARKET_ID: co.resolved_row(closed_hours_ago=1.0)})
    assert m2_runner._settle_tick(env["state"], co.NOW, m2_runner.GuardedGetter(getter)) == []
    assert len(getter.id_reads) == 2 and co._events(env, "settlement_skipped") == []
    assert co._settlements(env) == [] and not env["res"].exists() and env["state"].counters[KEY] == 0


@pytest.mark.parametrize("case", ["history", "append"])
def test_history_or_append_failure_is_not_a_getter_exception(env, monkeypatch, case):
    co._hold_loser(env)
    original = b'{"condition_id":"torn-owned"}'
    if case == "history":
        env["res"].parent.mkdir(parents=True)
        env["res"].write_bytes(original)
    else:
        def fail(*args, **kwargs):
            raise OSError("owned write failure")
        monkeypatch.setattr(resolutions, "persist_resolutions", fail)
    assert m2_runner._settle_tick(env["state"], co.NOW, m2_runner.GuardedGetter(Views())) == []
    assert co._settlements(env) == [] and co._events(env, "settlement_skipped")
    assert env["state"].counters[KEY] == 0
    if case == "history":
        assert env["res"].read_bytes() == original
    else:
        assert not env["res"].exists()


def test_partial_append_prefix_is_retained_without_apply_or_counter(env, monkeypatch):
    rows = two_held(env)
    persist, collect = resolutions.persist_resolutions, resolution_watch.collect
    calls, outputs, applies = [], [], []
    def fail_second(path, batch):
        calls.append(copy.deepcopy(batch))
        if len(calls) == 2:
            raise OSError("owned second append failure")
        return persist(path, batch)
    def capture(*args, **kwargs):
        result = collect(*args, **kwargs)
        outputs.append(result)
        return result
    monkeypatch.setattr(resolutions, "persist_resolutions", fail_second)
    monkeypatch.setattr(resolution_watch, "collect", capture)
    monkeypatch.setattr(paper_ledger, "apply_settlements", lambda *a, **k: applies.append(True))
    assert m2_runner._settle_tick(env["state"], co.NOW, m2_runner.GuardedGetter(Views(rows=rows))) == []
    assert len(calls) == 2 and len(outputs) == 1 and not outputs[0].ok
    assert outputs[0].written == calls[0] and resolutions.read_history(env["res"]) == calls[0]
    assert len(env["res"].read_text().splitlines()) == 1 and applies == [] and co._settlements(env) == []
    assert co._events(env, "settlement_skipped") and env["state"].counters[KEY] == 0


@pytest.mark.parametrize("route", ["scan", "fee", "writeoff", "time", "book"])
def test_shared_getter_routes_do_not_increment_settlement_counter(env, monkeypatch, route):
    error = TimeoutError("owned route failure")
    def fail(url):
        raise error
    guard = m2_runner.GuardedGetter(fail)
    state = env["state"]
    if route == "scan":
        m2_runner._scan(state, co.NOW, guard)
        assert state.counters["scan_failures_total"] == 1
    elif route == "fee":
        with pytest.raises(TimeoutError) as info:
            m2_runner._gamma_market(guard, rig.MARKET_ID)
        assert info.value is error
    elif route == "writeoff":
        seen = []
        def hook(current, now, original):
            seen.append(original)
            with pytest.raises(TimeoutError) as info:
                original(URL)
            assert info.value is error
            return []
        monkeypatch.setattr(m2_runner, "_writeoff_hook", hook)
        assert m2_runner._settle_tick(state, co.NOW, guard) == [] and seen == [guard]
    elif route == "time":
        assert m2_runner._clock_check(state, guard, co.NOW) == "clock_unverified"
        assert state.counters["time_check_failures_total"] == 1
    else:
        with pytest.raises(TimeoutError):
            m2_runner._book_get_observed(state, guard, podlib.BOOK_PREFIX + "token_id=" + rig.TOKENS[0])
        assert state.counters["book_get_failures_total"] == 1
    assert state.counters[KEY] == 0


@pytest.mark.parametrize("where", ["ledger", "blocked-state"])
def test_pre_get_local_failure_keeps_original_global_pause_without_count(env, monkeypatch, where):
    co._hold_loser(env)
    def fail(*args, **kwargs):
        raise OSError("owned local failure")
    if where == "ledger":
        monkeypatch.setattr(m2_runner, "_held_markets", fail)
    else:
        monkeypatch.setattr(paper_ledger, "settlement_blocked_markets", fail)
    getter = Views()
    report = co._tick(env, getter)
    assert report.action == m2_runner.ACT_PAUSE and report.reasons[0] == m2_runner.LEDGER_HALT_REASON
    assert co._global_halt() and getter.urls == [] and env["state"].counters[KEY] == 0


def test_event_writefailed_retains_iteration_exit_after_real_getter_failure(env, monkeypatch):
    co._hold_loser(env)
    event = m2_runner.event
    def fail(current, kind, now, **fields):
        if kind == "settlement_skipped":
            raise m2_runner.WriteFailed("owned event failure")
        return event(current, kind, now, **fields)
    monkeypatch.setattr(m2_runner, "event", fail)
    report = co._tick(env, Views({(rig.MARKET_ID, True): TimeoutError("offline")}))
    assert report.action == m2_runner.ACT_WRITE_FAILED and co._settlements(env) == []
    assert not co._global_halt() and env["state"].counters[KEY] == 1


@pytest.mark.parametrize("make,attempts", [
    (lambda: HTTPError(URL, 403, "offline", {}, None), 1),
    (lambda: HTTPError(URL, 429, "offline", {}, None), 3),
    (lambda: HTTPError(URL, 503, "offline", {}, None), 3),
    (lambda: TimeoutError("offline"), 3),
    (lambda: URLError(TimeoutError("offline")), 3),
    (lambda: podlib._DeadlineExceeded("offline"), 1),
    (lambda: ValueError("getter decode"), 1),
])
def test_real_public_get_attempts_produce_one_logical_settlement_failure(env, monkeypatch, fake_net, make, attempts):
    co._hold_loser(env)
    fake_net["set"](make())
    monkeypatch.setattr(podlib, "_monotonic", lambda: 0.0)
    errors, after = [], []
    def real(url):
        try:
            return podlib.public_get(url, attempts=3, backoff_s=0, deadline_s=2)
        except Exception as error:
            errors.append(error)
            raise
    guard = m2_runner.GuardedGetter(real, lambda: after.append(True))
    assert m2_runner._settle_tick(env["state"], co.NOW, guard) == []
    assert fake_net["calls"] == [URL] * attempts and guard.calls == [URL] and after == [True]
    assert len(errors) == 1 and env["state"].counters[KEY] == 1
    assert co._events(env, "settlement_skipped") and not env["res"].exists()
    if attempts > 1:
        assert isinstance(errors[0], podlib.PublicGetError) and errors[0].attempts == attempts
        assert isinstance(errors[0].__cause__, type(make()))


@pytest.mark.parametrize("error", [HTTPError(URL, 503, "offline", {}, None), TimeoutError("offline")])
def test_public_get_retry_then_success_does_not_count_and_really_settles(env, monkeypatch, fake_net, error):
    co._hold_loser(env)
    fake_net["set"](error, {"markets": [co.resolved_row()]}, {"markets": []})
    monkeypatch.setattr(podlib, "_monotonic", lambda: 0.0)
    after = []
    guard = m2_runner.GuardedGetter(lambda url: podlib.public_get(url, attempts=3, backoff_s=0, deadline_s=2),
                                    lambda: after.append(True))
    assert m2_runner._settle_tick(env["state"], co.NOW, guard) == []
    other = resolution_watch.gamma_url(rig.MARKET_ID, closed=False)
    assert fake_net["calls"] == [URL, URL, other] and guard.calls == [URL, other] and after == [True, True]
    assert len(co._settlements(env)) == 1 and env["state"].counters[KEY] == 0


def test_real_legacy_new_heartbeat_validator_reader_sentinel_keep_missing_unknown(env):
    resident, problem = heartbeat.read_beat(heartbeat.beat_path("sentinel"), "sentinel")
    assert problem is None and resident is not None
    legacy = m2_runner.progress(env["state"], co.NOW)
    legacy.pop(KEY)
    beat = heartbeat.Heartbeat(m2_runner.PROC, every_s=1.0)
    ledger = env["run"] / "owned-empty-ledger.jsonl"
    ledger.write_text("", encoding="utf-8")
    before = halt_store.active_halts()
    for progress in (legacy, {**legacy, KEY: 2}):
        assert beat.beat(heartbeat.STATE_RUNNING, progress=progress, force=True) is None
        record, problem = heartbeat.read_beat(beat.path, m2_runner.PROC)
        assert problem is None and heartbeat.validate_record(record, m2_runner.PROC) == record
        assert record["progress"] == progress
        assert record["progress"].get(KEY, "UNKNOWN") == progress.get(KEY, "UNKNOWN")
        tick = sentinel_tick.tick(now=datetime.now(timezone.utc), bus_path=env["run"] / "owned-bus.jsonl",
                                  quotes_path=env["run"] / "owned-quotes.jsonl", ledger_path=ledger)
        assert tick["checked"][m2_runner.PROC] == heartbeat.STATE_RUNNING and halt_store.active_halts() == before
