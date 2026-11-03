"""EP / #345: only /time getter exception observations; old control paths remain unchanged.

Derived from the archived 25-case draft; no skip/xfail or permanent-absence baseline.
All getters/paths are offline owned fixtures. Counters describe one RunnerState lifetime.
"""
from __future__ import annotations

import copy
import socket
from datetime import datetime, timedelta, timezone
from email.message import Message
from urllib.error import HTTPError, URLError

import pytest

from src import book_staleness, halt_store, heartbeat, m2_runner, podlib, sentinel_tick, wash_detect
from tests import m2_runner_rig as rig
from tests.test_public_get_retry import fake_net  # noqa: F401 - reuse the actual offline opener fixture

TIME_URL = "https://clob.polymarket.com/time"
CLASSES = ("425", "timeout", "5xx", "4xx_other", "other")
_REAL_DATETIME = datetime


class _FrozenMeta(type):
    def __instancecheck__(cls, instance):
        return isinstance(instance, _REAL_DATETIME)

    def __subclasscheck__(cls, sub):
        return issubclass(sub, _REAL_DATETIME)


def _freeze_wall(monkeypatch, moment):  # same pattern as tests/test_m2_runner_clock.py
    class Frozen(_REAL_DATETIME, metaclass=_FrozenMeta):
        @classmethod
        def now(cls, tz=None):
            return moment.astimezone(tz) if tz is not None else moment.replace(tzinfo=None)

    for module in (podlib, book_staleness, wash_detect):
        monkeypatch.setattr(module, "datetime", Frozen)


def _http(code, msg):
    headers = Message()
    if code == 425:
        headers["Retry-After"] = "1"
    return HTTPError(TIME_URL, code, msg, headers, None)


# (exception raised by the /time getter, expected class, expected http_status, expected exc_type)
FAILURES = [
    pytest.param(lambda: _http(425, "Too Early"), "425", 425, "HTTPError", id="425"),
    pytest.param(lambda: TimeoutError("The read operation timed out"), "timeout", None, "TimeoutError", id="timeout"),
    pytest.param(lambda: _http(503, "Service Unavailable"), "5xx", 503, "HTTPError", id="503"),
    pytest.param(lambda: _http(403, "Forbidden"), "4xx_other", 403, "HTTPError", id="403"),
    pytest.param(lambda: ValueError("bad json"), "other", None, "ValueError", id="other"),
]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("network attempted")
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)


@pytest.fixture
def env(tmp_path, monkeypatch):
    paths = rig.isolate(monkeypatch, tmp_path)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    _freeze_wall(monkeypatch, now)
    paths["run"].mkdir(parents=True)
    state = m2_runner.RunnerState(paths=m2_runner.RunnerPaths.under(paths["run"]), clock=lambda: now,
                                  monotonic=lambda: now.timestamp())
    return {**paths, "now": now, "state": state}


class TimeFails(rig.FakeGetters):
    """/time raises ``make()``; /book never reached (the tick pauses first)."""

    def __init__(self, now, make):
        super().__init__(now)
        self.make = make

    def book(self, url):
        if url == TIME_URL:
            self.time_calls.append(url)
            raise self.make()
        self.book_urls.append(url)
        token = rig.parse_qs(rig.urlsplit(url).query)["token_id"][0]
        return rig.clob_book(token, self.now)


def _tick(env, make, minutes=0):
    now = env["now"] + timedelta(minutes=minutes)
    env["state"].clock = lambda: now
    env["state"].monotonic = lambda: now.timestamp()
    fake = TimeFails(now, make)
    return m2_runner.iteration(env["state"], now=now, getters=fake.getters()), fake


# ---------------------------------------------------------------- baseline (today's behavior): must PASS

@pytest.mark.parametrize("make,klass,status,exc", FAILURES)
def test_baseline_any_time_failure_is_one_clock_unverified_pause(env, make, klass, status, exc):
    halts_before = halt_store.active_halts()
    counters_before = {k: v for k, v in env["state"].counters.items() if not k.startswith("time_check_failures")}
    report, fake = _tick(env, make)
    assert report.action == m2_runner.ACT_PAUSE and report.reasons == ["clock_unverified"]
    assert fake.time_calls == [TIME_URL] and fake.book_urls == []  # one /time GET, no book, no intent
    check = env["state"].last_time_check
    assert check["result"] == "clock_unverified"
    assert check["low_s"] is None and check["high_s"] is None and check["rtt_s"] is None
    assert env["state"].clock_anomaly == "clock_unverified" and env["state"].clock_anomaly_since is not None
    assert halt_store.active_halts() == halts_before  # no halt (global or market)
    assert {k: v for k, v in env["state"].counters.items() if k in counters_before} == counters_before  # all old counters unchanged


def test_progress_adds_exact_observation_fields_and_retains_old_five_values(env):
    _tick(env, FAILURES[0].values[0])
    prog = m2_runner.progress(env["state"], env["now"])
    assert prog["time_check_failures_total"] == 1
    assert prog["time_check_failures_by_class"] == {c: int(c == "425") for c in CLASSES}
    assert prog["last_time_check"] == {"ts": m2_runner._stamp(env["now"]), "result": "clock_unverified",
                                       "low_s": None, "high_s": None, "rtt_s": None,
                                       "http_status": 425, "exc_type": "HTTPError"}


def patrol_time_class(progress):
    """Draft patrol-side reader (box side, not repo code): an old heartbeat without the #345 fields is 'unknown'."""
    check = progress.get("last_time_check") or {}
    if check.get("result") in (None, "ok", "clock_skew"):
        return None
    if "http_status" not in check and "exc_type" not in check:
        return "unknown"
    code, exc = check.get("http_status"), check.get("exc_type") or ""
    if code == 425:
        return "425"
    if isinstance(code, int) and 500 <= code < 600:
        return "5xx"
    if isinstance(code, int) and 400 <= code < 500:
        return "4xx_other"
    if "Timeout" in exc or exc in {"timeout", "_DeadlineExceeded"}:
        return "timeout"
    return "other"


def test_baseline_old_heartbeat_without_fields_reads_as_unknown(env):
    legacy = {"last_time_check": {"ts": m2_runner._stamp(env["now"]), "result": "clock_unverified",
                                  "low_s": None, "high_s": None, "rtt_s": None}}
    beat = heartbeat.Heartbeat(m2_runner.PROC, every_s=1.0)
    assert beat.beat(heartbeat.STATE_PAUSED, progress=legacy, force=True) is None
    record, problem = heartbeat.read_beat(beat.path, m2_runner.PROC)
    assert problem is None and record["progress"] == legacy
    assert patrol_time_class(record["progress"]) == "unknown"
    assert record["progress"].get("time_check_failures_total") is None  # missing is not zero
    ledger = env["run"] / "legacy-empty-ledger.jsonl"
    ledger.write_text("", encoding="utf-8")
    tick = sentinel_tick.tick(now=env["now"], bus_path=env["run"] / "legacy-bus.jsonl",
                              quotes_path=env["run"] / "legacy-quotes.jsonl", ledger_path=ledger)
    assert tick["checked"][m2_runner.PROC] == heartbeat.STATE_PAUSED


def test_baseline_recovery_clears_pause(env):
    _tick(env, FAILURES[0].values[0])
    now = env["now"] + timedelta(minutes=1)
    env["state"].clock = lambda: now
    env["state"].monotonic = lambda: now.timestamp()

    class Ok(rig.FakeGetters):
        def book(self, url):
            if url == TIME_URL:
                self.time_calls.append(url)
                return now.timestamp()
            self.book_urls.append(url)
            token = rig.parse_qs(rig.urlsplit(url).query)["token_id"][0]
            return rig.clob_book(token, now)

    report = m2_runner.iteration(env["state"], now=now, getters=Ok(now).getters())
    assert report.action != m2_runner.ACT_PAUSE and env["state"].paused_reason is None


# ---------------------------------------------------------------- #345 current contract

@pytest.mark.parametrize("make,klass,status,exc", FAILURES)
def test_345_last_time_check_records_http_status_and_exc_type(env, make, klass, status, exc):
    _tick(env, make)
    check = env["state"].last_time_check
    assert check["http_status"] == status and check["exc_type"] == exc
    assert check["result"] == "clock_unverified"  # unchanged decision


@pytest.mark.parametrize("make,klass,status,exc", FAILURES)
def test_345_failure_counters_accumulate_by_class(env, make, klass, status, exc):
    for minutes in (0, 1, 2):
        _tick(env, make, minutes)
    prog = m2_runner.progress(env["state"], env["now"])
    assert prog["time_check_failures_total"] == 3
    by = prog["time_check_failures_by_class"]
    assert set(by) == set(CLASSES) and by[klass] == 3 and sum(by.values()) == 3
    assert patrol_time_class(prog) == klass


def test_345_mixed_classes_count_separately(env):
    for minutes, make in enumerate([FAILURES[0].values[0], FAILURES[1].values[0], FAILURES[0].values[0]]):
        _tick(env, make, minutes)
    by = m2_runner.progress(env["state"], env["now"])["time_check_failures_by_class"]
    assert by["425"] == 2 and by["timeout"] == 1 and by["5xx"] == by["4xx_other"] == by["other"] == 0


def test_345_fresh_state_starts_at_zero_and_ok_does_not_count(env):
    prog = m2_runner.progress(env["state"], env["now"])
    assert prog["time_check_failures_total"] == 0
    assert prog["time_check_failures_by_class"] == {c: 0 for c in CLASSES}


@pytest.mark.parametrize("make,klass,status,exc", FAILURES)
def test_345_counting_adds_no_halt_pause_or_veto(env, make, klass, status, exc):
    halts_before = halt_store.active_halts()
    report, _ = _tick(env, make)
    assert report.action == m2_runner.ACT_PAUSE and report.reasons == ["clock_unverified"]  # same as baseline
    assert halt_store.active_halts() == halts_before
    others = {k: v for k, v in env["state"].counters.items() if not k.startswith("time_check_failures")}
    assert others["vetoes_total"] == 0 and others["allows_total"] == 0
    assert env["state"].counters["time_check_failures_total"] == 1  # needs #345 (KeyError today)


# The expected table is independent of the production classifier.
def _wrapped(error, *, cause=True):
    outer = podlib.PublicGetError(TIME_URL, 3, error)
    if cause:
        outer.__cause__ = error
    return outer


def _chain(hops):
    error = _http(503, "offline")
    for _ in range(hops):
        error = URLError(error)
    return error


def _cycle():
    error = URLError("offline")
    error.reason = error
    return error


def _cause_only():
    error = URLError("offline")
    error.__cause__ = TimeoutError("offline")
    return error


def _unrelated_cause():
    error = ValueError("bad JSON")
    error.__cause__ = TimeoutError("not a supported wrapper")
    return error


def _competing_links():
    error = _wrapped(_http(425, "last_error"), cause=False)
    error.__cause__ = _http(503, "explicit cause")
    return error


@pytest.mark.parametrize("make,klass,status,exc", [
    (lambda: _wrapped(_http(503, "offline")), "5xx", 503, "HTTPError"),
    (lambda: _wrapped(TimeoutError("offline")), "timeout", None, "TimeoutError"),
    (lambda: _wrapped(_http(425, "offline"), cause=False), "425", 425, "HTTPError"),
    (lambda: URLError(TimeoutError("offline")), "timeout", None, "TimeoutError"),
    (_cause_only, "timeout", None, "TimeoutError"),
    (lambda: _wrapped(URLError(TimeoutError("offline"))), "timeout", None, "TimeoutError"),
    (lambda: _wrapped(podlib._DeadlineExceeded("offline")), "timeout", None, "_DeadlineExceeded"),
    (lambda: URLError("timed out"), "other", None, "URLError"),
    (_unrelated_cause, "other", None, "ValueError"),
    (lambda: _chain(8), "5xx", 503, "HTTPError"),
    (lambda: _chain(9), "other", None, "URLError"),
    (_cycle, "other", None, "URLError"),
    (_competing_links, "5xx", 503, "HTTPError"),
    (lambda: _http(429, "offline"), "4xx_other", 429, "HTTPError"),
    (lambda: _http(True, "invalid status"), "other", None, "HTTPError"),
    (lambda: _http(301, "offline"), "other", 301, "HTTPError"),
])
def test_effective_exception_details_and_bounded_known_wrappers(env, make, klass, status, exc):
    before = halt_store.active_halts()
    report, fake = _tick(env, make)
    assert report.reasons == ["clock_unverified"] and fake.time_calls == [TIME_URL]
    progress = m2_runner.progress(env["state"], env["now"])
    assert progress["time_check_failures_total"] == 1
    assert progress["time_check_failures_by_class"] == {c: int(c == klass) for c in CLASSES}
    check = progress["last_time_check"]
    assert check["http_status"] == status and check["exc_type"] == exc
    assert set(check) == {"ts", "result", "low_s", "high_s", "rtt_s", "http_status", "exc_type"}
    assert halt_store.active_halts() == before and fake.book_urls == []


@pytest.mark.parametrize("scenario", ["sent_raises", "received_raises", "sent_invalid", "received_invalid",
                                        "parse_raises", "bad_return", "bad_bool", "bad_huge", "bad_nan",
                                        "skew", "rtt"])
def test_non_getter_clock_failures_are_not_counted(env, monkeypatch, scenario):
    clocks, gets = [], []
    now = env["now"]
    def clock():
        clocks.append(1)
        if (scenario == "sent_raises" and len(clocks) == 1) or (scenario == "received_raises" and len(clocks) == 2):
            raise ValueError("local clock")
        if (scenario == "sent_invalid" and len(clocks) == 1) or (scenario == "received_invalid" and len(clocks) == 2):
            return None
        return now + timedelta(seconds=4.5 if scenario == "rtt" and len(clocks) == 2 else 0)
    def getter(url):
        gets.append(url)
        if scenario == "bad_return":
            return "not a timestamp"
        if scenario == "bad_bool":
            return True
        if scenario == "bad_huge":
            return 10 ** 400
        if scenario == "bad_nan":
            return float("nan")
        return now.timestamp() - (7 if scenario == "skew" else 0)
    if scenario == "parse_raises":
        def bad_parse(value):
            raise ValueError("local parser")
        monkeypatch.setattr(m2_runner, "parse_book_ts", bad_parse)
    env["state"].clock = clock
    result = m2_runner._clock_check(env["state"], m2_runner.GuardedGetter(getter), now)
    assert result == ("clock_skew" if scenario == "skew" else "clock_unverified")
    prog = m2_runner.progress(env["state"], now)
    assert prog["time_check_failures_total"] == 0 and sum(prog["time_check_failures_by_class"].values()) == 0
    assert prog["last_time_check"]["http_status"] is None and prog["last_time_check"]["exc_type"] is None
    assert gets == ([] if scenario == "sent_raises" else [TIME_URL])


@pytest.mark.parametrize("getter_fails", [False, True])
@pytest.mark.parametrize("after_fails", [False, True])
def test_getter_and_after_boundaries_remain_unchanged(env, getter_fails, after_fails):
    trace = []
    def clock():
        trace.append("clock")
        return env["now"]
    def getter(url):
        trace.append("getter")
        if getter_fails:
            raise _http(425, "offline")
        return env["now"].timestamp()
    def after():
        trace.append("after")
        if after_fails:
            raise ValueError("after hook")
    env["state"].clock = clock
    guard = m2_runner.GuardedGetter(getter, after=after)
    result = m2_runner._clock_check(env["state"], guard, env["now"])
    assert trace == ["clock", "getter", "after"] + ([] if getter_fails else ["clock"])
    assert result == ("clock_unverified" if getter_fails else None) and guard.calls == [TIME_URL]
    assert env["state"].counters["time_check_failures_total"] == int(getter_fails)
    assert env["state"].last_time_check["exc_type"] == ("HTTPError" if getter_fails else None)


@pytest.mark.parametrize("error", [KeyboardInterrupt, SystemExit])
def test_baseexceptions_keep_their_existing_propagation(env, error):
    def getter(url):
        raise error("offline control")
    with pytest.raises(error):
        m2_runner._clock_check(env["state"], m2_runner.GuardedGetter(getter), env["now"])
    assert env["state"].counters["time_check_failures_total"] == 0
    assert env["state"].last_time_check is None


def test_refused_url_preserves_each_original_entry_behavior(env):
    called = []
    direct = m2_runner.GuardedGetter(lambda url: called.append(url), after=lambda: called.append("after"))
    with pytest.raises(m2_runner.RefusedURL):
        direct("https://not-allowlisted.invalid/time")
    assert direct.calls == [] and called == [] and len(direct.refused) == 1
    def refused(url):
        raise m2_runner.RefusedURL("offline control")
    assert m2_runner._clock_check(env["state"], m2_runner.GuardedGetter(refused), env["now"]) == "clock_unverified"
    assert env["state"].last_time_check["exc_type"] == "RefusedURL"
    assert env["state"].time_check_failures_by_class["other"] == 1


def test_no_timestamp_still_counts_getter_failure_but_keeps_last_check_none(env):
    def getter(url):
        raise TimeoutError("offline")
    assert m2_runner._clock_check(env["state"], m2_runner.GuardedGetter(getter)) == "clock_unverified"
    assert env["state"].counters["time_check_failures_total"] == 1 and env["state"].last_time_check is None


def test_early_kill_does_not_make_or_count_a_time_read(env):
    (env["run"] / "KILL").write_text("offline fixture", encoding="utf-8")
    fake = rig.FakeGetters(env["now"])
    report = m2_runner.iteration(env["state"], now=env["now"], getters=fake.getters())
    assert report.action == m2_runner.ACT_FINISH and fake.time_calls == []
    assert env["state"].counters["time_check_failures_total"] == 0 and env["state"].last_time_check is None


def test_success_does_not_reset_and_progress_nested_dicts_are_readonly_copies(env):
    _tick(env, FAILURES[0].values[0])
    guard = m2_runner.GuardedGetter(lambda url: env["now"].timestamp())
    assert m2_runner._clock_check(env["state"], guard, env["now"]) is None
    first = m2_runner.progress(env["state"], env["now"])
    before = copy.deepcopy(first)
    assert first["time_check_failures_total"] == 1 and first["time_check_failures_by_class"]["425"] == 1
    assert first["last_time_check"]["http_status"] is None and first["last_time_check"]["exc_type"] is None
    assert m2_runner.progress(env["state"], env["now"]) == before
    first["time_check_failures_by_class"]["425"] = 999
    first["last_time_check"]["result"] = "tampered"
    assert m2_runner.progress(env["state"], env["now"]) == before


@pytest.mark.parametrize("make,klass,status,exc,attempts", [
    (lambda: _http(425, "offline"), "425", 425, "HTTPError", 1),
    (lambda: _http(429, "offline"), "4xx_other", 429, "HTTPError", 1),
    (lambda: _http(503, "offline"), "5xx", 503, "HTTPError", 3),
    (lambda: TimeoutError("offline"), "timeout", None, "TimeoutError", 3),
    (lambda: podlib._DeadlineExceeded("offline"), "timeout", None, "_DeadlineExceeded", 1),
    (lambda: ValueError("bad JSON"), "other", None, "ValueError", 1),
])
def test_real_public_get_wrapping_attempts_are_observed_once(env, monkeypatch, fake_net, make, klass, status, exc, attempts):
    fake_net["set"](make())
    monkeypatch.setattr(podlib, "_monotonic", lambda: 0.0)
    captured = []
    def getter(url):
        try:
            return podlib.public_get(url, attempts=3, backoff_s=0, deadline_s=2)
        except Exception as error:
            captured.append(error)
            raise
    guard = m2_runner.GuardedGetter(getter)
    assert m2_runner._clock_check(env["state"], guard, env["now"]) == "clock_unverified"
    assert guard.calls == [TIME_URL] and fake_net["calls"] == [TIME_URL] * attempts
    assert env["state"].counters["time_check_failures_total"] == 1
    assert env["state"].time_check_failures_by_class == {c: int(c == klass) for c in CLASSES}
    check = env["state"].last_time_check
    assert check["http_status"] == status and check["exc_type"] == exc
    assert len(captured) == 1
    if isinstance(captured[0], podlib.PublicGetError):
        assert captured[0].attempts == attempts and captured[0].__cause__ is not None
