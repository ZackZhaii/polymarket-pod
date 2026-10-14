"""C222: independent clock evidence and per-signal book freshness, entirely offline."""
from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone, tzinfo

import pytest

from scripts import guard
from src import book_staleness, heartbeat, m2_runner, paper_ledger, podlib, wash_detect
from tests import m2_runner_rig as rig

TIME_URL = "https://clob.polymarket.com/time"
# commit_if_allowed -> RiskGate.evaluate(now=None) reads podlib/book_staleness/wash_detect datetime.now.
# Under full-suite load a wall-only fixture now drifts past INTENT_TTL_S (or past a synthetic
# future_book_ts), so the second signal flake-fails. Freeze those three modules (e2e pattern) to
# the fixture clock; do not freeze heartbeat — the autouse Sentinel beat is stamped on the real
# clock just before this fixture runs.
_REAL_DATETIME = datetime


class _FrozenMeta(type):
    def __instancecheck__(cls, instance):
        return isinstance(instance, _REAL_DATETIME)

    def __subclasscheck__(cls, sub):
        return issubclass(sub, _REAL_DATETIME)


def _freeze_wall(monkeypatch, moment: datetime) -> None:
    """Freeze RiskGate / book_age_problem / wash clocks; leave heartbeat on the wall for Sentinel."""

    class Frozen(_REAL_DATETIME, metaclass=_FrozenMeta):
        @classmethod
        def now(cls, tz=None):
            return moment.astimezone(tz) if tz is not None else moment.replace(tzinfo=None)

        @classmethod
        def utcnow(cls):
            return moment.astimezone(timezone.utc).replace(tzinfo=None)

    monkeypatch.setattr(podlib, "datetime", Frozen)
    monkeypatch.setattr(book_staleness, "datetime", Frozen)
    monkeypatch.setattr(wash_detect, "datetime", Frozen)


@pytest.fixture
def env(tmp_path, monkeypatch):
    paths = rig.isolate(monkeypatch, tmp_path)
    # Second precision matches book stamps built from this same now; freeze so RiskGate cannot drift.
    now = datetime.now(timezone.utc).replace(microsecond=0)
    _freeze_wall(monkeypatch, now)
    paths["run"].mkdir(parents=True)
    state = m2_runner.RunnerState(paths=m2_runner.RunnerPaths.under(paths["run"]), clock=lambda: now,
                                  monotonic=lambda: now.timestamp())
    return {**paths, "now": now, "state": state}


class ClockGetters(rig.FakeGetters):
    def __init__(self, now, *, server=None, book_age=0):
        super().__init__(now)
        self.server = now.timestamp() if server is None else server
        self.book_age = book_age

    def book(self, url):
        if url == TIME_URL:
            self.time_calls.append(url)
            if isinstance(self.server, Exception):
                raise self.server
            return self.server
        self.book_urls.append(url)
        token = rig.parse_qs(rig.urlsplit(url).query)["token_id"][0]
        return rig.clob_book(token, self.now - timedelta(seconds=self.book_age))


def tick(env, fake):
    return m2_runner.iteration(env["state"], now=env["now"], getters=fake.getters())


@pytest.mark.parametrize("offset,reason", [
    (0, None), (4, None), (-4, None), (4.000001, "clock_unverified"), (-4.000001, "clock_unverified"),
    (5, "clock_unverified"), (-5, "clock_unverified"), (6, "clock_unverified"), (-6, "clock_unverified"),
    (7, "clock_skew"), (-7, "clock_skew"),
])
def test_independent_clock_quantization_boundaries_before_any_work(env, monkeypatch, offset, reason):
    fake = ClockGetters(env["now"], server=env["now"].timestamp() - offset)
    actions = []
    monkeypatch.setattr(m2_runner, "_settle_tick", lambda *a: actions.append("settle") or [])
    commit = paper_ledger.commit_if_allowed
    monkeypatch.setattr(paper_ledger, "commit_if_allowed", lambda *a, **k: actions.append("commit") or commit(*a, **k))
    pending = rig.deliver(env["run"], env["now"])
    report = tick(env, fake)
    assert fake.time_calls == [TIME_URL]
    if reason is None:
        assert report.action == m2_runner.ACT_RUN and actions[0] == "settle"
    else:
        assert report.action == m2_runner.ACT_PAUSE and report.reasons == [reason]
        assert report.paused_reason == reason and actions == ["settle"]
        assert fake.gamma_urls == fake.book_urls == [] and pending.exists()


@pytest.mark.parametrize("server", [
    None, True, False, "1234567890", "", [], {}, float("nan"), float("inf"), -float("inf"),
    10**400, 253402300800, -62135596801, TimeoutError("offline"), ValueError("bad JSON"),
])
def test_unusable_server_time_settles_then_pauses_without_intent(env, monkeypatch, server):
    fake = ClockGetters(env["now"])
    fake.server = server
    actions = []
    monkeypatch.setattr(m2_runner, "_settle_tick", lambda *a: actions.append("settle") or [])
    monkeypatch.setattr(paper_ledger, "commit_if_allowed", lambda *a, **k: actions.append("commit"))
    report = tick(env, fake)
    assert report.action == m2_runner.ACT_PAUSE and report.reasons == ["clock_unverified"]
    assert actions == ["settle"] and fake.time_calls == [TIME_URL] and fake.gamma_urls == fake.book_urls == []


@pytest.mark.parametrize("elapsed,reason", [(2, None), (4, None), (4.000001, "clock_unverified"),
                                             (8, "clock_unverified"), (-0.001, "clock_unverified")])
def test_request_interval_must_be_fully_certified(env, monkeypatch, elapsed, reason):
    moments = iter([env["now"], env["now"] + timedelta(seconds=elapsed)])
    env["state"].clock = lambda: next(moments)
    actions = []
    monkeypatch.setattr(m2_runner, "_settle_tick", lambda *a: actions.append("settle") or [])
    fake = ClockGetters(env["now"])
    report = tick(env, fake)  # no signal, so no later book-clock reads
    assert fake.time_calls == [TIME_URL]
    if reason is None:
        assert report.action == m2_runner.ACT_RUN and actions == ["settle"]
    else:
        assert report.action == m2_runner.ACT_PAUSE and report.reasons == [reason] and actions == ["settle"]


class FoldZone(tzinfo):
    def utcoffset(self, value):
        return timedelta(hours=0 if value.fold else 1)

    def dst(self, value):
        return timedelta(0)


@pytest.mark.parametrize("forward", [False, True], ids=["real_backward", "wall_backward_real_forward"])
def test_fold_clock_request_uses_utc_order(env, monkeypatch, forward):
    zone = FoldZone()
    if forward:
        sent = datetime(2026, 10, 25, 2, 59, 59, tzinfo=zone, fold=0)
        received = datetime(2026, 10, 25, 2, 0, 1, tzinfo=zone, fold=1)
    else:
        sent = datetime(2026, 10, 25, 2, 30, tzinfo=zone, fold=1)
        received = sent.replace(fold=0)
    moments = iter([sent, received])
    env["state"].clock = lambda: next(moments)
    fake = ClockGetters(env["now"], server=(sent.timestamp() + received.timestamp()) / 2)
    actions = []
    monkeypatch.setattr(m2_runner, "_settle_tick", lambda *a: actions.append("settle") or [])
    report = tick(env, fake)
    assert fake.time_calls == [TIME_URL]
    if forward:
        assert report.action == m2_runner.ACT_RUN and actions == ["settle"]
    else:
        assert report.action == m2_runner.ACT_PAUSE and report.reasons == ["clock_unverified"]
        assert actions == ["settle"] and fake.gamma_urls == fake.book_urls == []


@pytest.mark.parametrize("offset_hours", [8, -3.5])
def test_fixed_offset_local_clock_remains_valid(env, offset_hours):
    local = env["now"].astimezone(timezone(timedelta(hours=offset_hours)))
    env["state"].clock = lambda: local
    assert tick(env, ClockGetters(env["now"])).action == m2_runner.ACT_RUN


@pytest.mark.parametrize("local", [None, True, "2026-10-12T00:00:00Z", datetime(2026, 10, 12)])
def test_unusable_local_clock_cannot_certify_server_time(env, monkeypatch, local):
    env["state"].clock = lambda: local
    actions = []
    monkeypatch.setattr(m2_runner, "_settle_tick", lambda *a: actions.append("settle") or [])
    report = tick(env, ClockGetters(env["now"]))
    assert report.action == m2_runner.ACT_PAUSE and report.reasons == ["clock_unverified"] and actions == ["settle"]


@pytest.mark.parametrize("age", [6, 35, 120])
def test_old_book_with_good_clock_does_not_pause_the_runner(env, age):
    rig.deliver(env["run"], env["now"])
    fake = ClockGetters(env["now"], book_age=age)
    report = tick(env, fake)
    assert report.action == m2_runner.ACT_RUN and report.paused_reason is None
    assert env["state"].paused_reason is None and fake.time_calls == [TIME_URL]
    assert fake.book_urls and not any("clock" in r for r in report.reasons)


@pytest.mark.parametrize("age,reason", [(121, "stale_book"), (-6, "future_book_ts")])
def test_bad_book_skips_one_signal_and_the_next_signal_can_commit(env, age, reason):
    class OneBad(ClockGetters):
        def book(self, url):
            self.book_age = age if not self.book_urls else 0
            return super().book(url)

    rig.deliver(env["run"], env["now"], "0001.json")
    rig.deliver(env["run"], env["now"], "0002.json", p=0.63)
    fake = OneBad(env["now"])
    report = tick(env, fake)
    assert report.action == m2_runner.ACT_RUN and report.skipped == 1 and report.allows == 1
    skipped = [row for row in rig.read_jsonl(env["run"] / "journal.jsonl") if row["step"] == "skipped"]
    assert skipped[0]["reasons"] == [reason] and fake.time_calls == [TIME_URL]
    assert len(rig.read_jsonl(env["paper"])) == 1


def test_failed_clock_pauses_and_recovers_without_any_signal(env):
    fake = ClockGetters(env["now"])
    assert tick(env, fake).action == m2_runner.ACT_RUN
    fake.server = env["now"].timestamp() - 7
    failed = tick(env, fake)
    assert failed.action == m2_runner.ACT_PAUSE and env["state"].paused_reason == "clock_skew"
    fake.server = env["now"].timestamp()
    recovered = tick(env, fake)
    assert recovered.action == m2_runner.ACT_RUN
    assert env["state"].paused_reason is env["state"].paused_since is None
    assert fake.time_calls == [TIME_URL] * 3 and fake.book_urls == []


@pytest.mark.parametrize("bad_timestamp", [None, "bad", True, float("nan")])
def test_malformed_book_skips_one_signal_without_blocking_the_next(env, bad_timestamp):
    class OneMalformed(ClockGetters):
        def book(self, url):
            first_book = url != TIME_URL and not self.book_urls
            body = super().book(url)
            if first_book:
                body["timestamp"] = bad_timestamp
            return body

    rig.deliver(env["run"], env["now"], "0001.json")
    rig.deliver(env["run"], env["now"], "0002.json", p=0.63)
    report = tick(env, OneMalformed(env["now"]))
    assert report.action == m2_runner.ACT_RUN and report.skipped == 1 and report.allows == 1
    assert env["state"].paused_reason is None


def test_unverified_clock_emits_a_paused_heartbeat(env, monkeypatch):
    fake = ClockGetters(env["now"], server=True)
    beats = []
    beat = heartbeat.Heartbeat.beat
    monkeypatch.setattr(heartbeat.Heartbeat, "beat",
                        lambda self, state, *a, **k: beats.append(state) or beat(self, state, *a, **k))
    elapsed = {"s": 0}
    code = m2_runner.main(["--run-dir", str(env["run"]), "--max-runtime", "1"], getters=fake.getters(),
                          clock=lambda: env["now"], monotonic=lambda: elapsed["s"],
                          sleep=lambda seconds: elapsed.update(s=elapsed["s"] + seconds))
    assert code == m2_runner.EXIT_OK and heartbeat.STATE_PAUSED in beats
    assert fake.time_calls == [TIME_URL] and fake.gamma_urls == fake.book_urls == []


BAD_TIME_URLS = [
    TIME_URL + "?", TIME_URL + "?x=1", TIME_URL + "#", TIME_URL + "#x", TIME_URL + "/", TIME_URL + ";",
    TIME_URL + ".", TIME_URL + ",", TIME_URL + ":", TIME_URL + "/extra", TIME_URL.replace("https:", "http:"),
    TIME_URL.replace("clob.", "u:p@clob."), TIME_URL.replace(".com/", ".com:443/"),
    TIME_URL.replace(".com/", ".com:8443/"), TIME_URL.replace("clob.", "CLOB."),
    TIME_URL.replace(".com/", ".com.evil.example/"),
]


@pytest.mark.parametrize("url", BAD_TIME_URLS)
def test_near_time_urls_are_refused_by_every_get_boundary(url, monkeypatch):
    touched = []
    monkeypatch.setattr(podlib, "urlopen", lambda *a, **k: touched.append(a))
    assert not m2_runner.url_allowed(url)
    guarded = m2_runner.GuardedGetter(touched.append)
    with pytest.raises(m2_runner.RefusedURL):
        guarded(url)
    with pytest.raises(RuntimeError):
        podlib.public_get(url)
    assert guard.url_problem(url, code=True) is not None
    assert guard.url_problems_in_source(f"URL = {url!r}\n", "probe.py")
    assert touched == []


def test_exact_time_url_uses_the_existing_read_only_json_transport(monkeypatch):
    requests = []

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def read(request, **kwargs):
        requests.append(request)
        return Response(json.dumps(1234567890).encode())

    monkeypatch.setattr(podlib, "urlopen", read)
    assert podlib.TIME_URL == TIME_URL and m2_runner.url_allowed(TIME_URL)
    assert podlib.public_get(TIME_URL) == 1234567890
    assert [(r.full_url, r.get_method(), r.data) for r in requests] == [(TIME_URL, "GET", None)]
    assert guard.url_problem(TIME_URL, code=True) is None
    assert guard.url_problems_in_source(f"URL = {TIME_URL!r}\n", "probe.py") == []


def test_repeated_clock_pause_keeps_the_first_paused_since(env):
    """#238 check (Codex回迁 intake): the same clock pause reason on later ticks keeps the original paused_since."""
    first = env["now"]
    fake = ClockGetters(first, server=RuntimeError("time down"))
    assert tick(env, fake).paused_reason == "clock_unverified"
    since = env["state"].paused_since
    assert since is not None
    for minutes in (1, 30, 120):
        later = first + timedelta(minutes=minutes)
        env["now"] = later
        env["state"].clock = lambda later=later: later
        env["state"].monotonic = lambda later=later: later.timestamp()
        fake = ClockGetters(later, server=RuntimeError("time down"))
        assert m2_runner.iteration(env["state"], now=later, getters=fake.getters()).paused_reason == "clock_unverified"
        assert env["state"].paused_since == since


@pytest.mark.parametrize("server,reason", [(RuntimeError("time down"), "clock_unverified"), ("skew", "clock_skew")])
def test_clock_pause_still_settles_before_pausing(env, monkeypatch, server, reason):
    """PR #253 review (Poly哨兵 b) / issue #206: an unverifiable or skewed clock must not defer settlement.
    Tick order: halt check -> settlement -> (write-off) -> clock -> daily loss -> RiskGate; no scan, no intent."""
    fake = ClockGetters(env["now"], server=env["now"].timestamp() - 7 if server == "skew" else server)
    actions = []
    monkeypatch.setattr(m2_runner, "_settle_tick", lambda *a: actions.append("settle") or [])
    monkeypatch.setattr(paper_ledger, "commit_if_allowed", lambda *a, **k: actions.append("commit"))
    pending = rig.deliver(env["run"], env["now"])
    report = tick(env, fake)
    assert report.action == m2_runner.ACT_PAUSE and report.reasons == [reason]
    assert actions == ["settle"] and fake.book_urls == [] and pending.exists()

