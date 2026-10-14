"""Task DQ (Poly哨兵 on #309): rate-limit-storm regressions. Offline.

A 429 storm (every public GET answers 429, Retry-After 30) over the real ``podlib.public_get``: urlopen is
scripted and one fake clock drives podlib's monotonic/sleep, the runner's clock/monotonic/sleep and the
heartbeat timestamps. One m2_runner tick and the m1_run start-up clock check must each end within 120 s of
fake wall time, and no heartbeat gap may reach the Sentinel's 120 s timeout.
"""
from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone
from email.message import Message
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import pytest

import tests.m2_runner_rig as rig
from src import heartbeat, m1_run, m2_runner, podlib, thresholds

STORM_LIMIT_S = 120.0
BASE = datetime(2026, 10, 14, 4, 0, tzinfo=timezone.utc)


class FakeTime:
    def __init__(self) -> None:
        self.t = 0.0

    def monotonic(self) -> float:
        return self.t

    def now(self) -> datetime:
        return BASE + timedelta(seconds=self.t)

    def sleep(self, seconds: float) -> None:
        self.t += max(0.0, float(seconds))


class _Response:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _429(url: str, retry_after: str = "30") -> HTTPError:
    headers = Message()
    headers["Retry-After"] = retry_after
    return HTTPError(url, 429, "Too Many Requests", headers, io.BytesIO(b""))


@pytest.fixture
def storm(monkeypatch):
    """Patch podlib's network and clock. ``answer(url) -> (seconds, body | exception)``; default: a 429 storm."""
    ft = FakeTime()
    state = {"time": ft, "urls": [], "beats": [], "answer": lambda url: (0.2, _429(url))}

    def fake_urlopen(request, timeout=None):
        state["urls"].append(request.full_url)
        took, outcome = state["answer"](request.full_url)
        if took > timeout:
            ft.t += timeout
            raise TimeoutError("timed out")
        ft.t += took
        if isinstance(outcome, BaseException):
            raise outcome
        return _Response(json.dumps(outcome).encode("utf-8"))

    monkeypatch.setattr(podlib, "urlopen", fake_urlopen)
    monkeypatch.setattr(podlib, "_sleep", ft.sleep)
    monkeypatch.setattr(podlib, "_monotonic", ft.monotonic)
    real_write = heartbeat.write_record

    def recording_write(path, record):
        state["beats"].append((ft.t, record.get("proc"), record.get("state")))
        return real_write(path, record)

    monkeypatch.setattr(heartbeat, "write_record", recording_write)
    return state


def _max_gap(times: list[float], start: float, end: float) -> float:
    points = [start, *sorted(times), end]
    return max(b - a for a, b in zip(points, points[1:]))


# ---------------------------------------------------------------- m2_runner: one tick


def _run_tick(tmp_path, monkeypatch, storm) -> int:
    paths = rig.isolate(monkeypatch, tmp_path)
    rig.deliver(paths["run"], BASE)
    ft = storm["time"]
    getters = m2_runner.Getters(gamma=podlib.public_get, book=podlib.public_get)
    return m2_runner.main(["--run-dir", str(paths["run"]), "--once"], getters=getters, clock=ft.now,
                          sleep=ft.sleep, monotonic=ft.monotonic)


def test_m2_runner_one_tick_in_a_429_storm_ends_within_120s(tmp_path, monkeypatch, storm):
    assert _run_tick(tmp_path, monkeypatch, storm) == m2_runner.EXIT_OK
    assert storm["urls"], "the tick reached public_get"
    assert storm["time"].t < STORM_LIMIT_S
    # /time is not retried on 429 (one request), so the clock check fails closed at once
    assert storm["urls"].count(podlib.TIME_URL) == 1


def test_m2_runner_one_tick_with_time_ok_and_gamma_book_storm_ends_within_120s(tmp_path, monkeypatch, storm):
    def answer(url):
        if url == podlib.TIME_URL:
            return 0.2, storm["time"].now().timestamp()
        return 0.2, _429(url)

    storm["answer"] = answer
    assert _run_tick(tmp_path, monkeypatch, storm) == m2_runner.EXIT_OK
    assert len(storm["urls"]) > 1
    assert storm["time"].t < STORM_LIMIT_S
    beats = [t for t, proc, _ in storm["beats"] if proc == m2_runner.PROC]
    assert _max_gap(beats, 0.0, storm["time"].t) < thresholds.HEARTBEAT_RUNNER_TIMEOUT_S


def test_m2_runner_beats_between_slow_gets_inside_one_tick(tmp_path, monkeypatch, storm):
    """Every Gamma / book GET answers after a 429 + 25 s wait: the tick is long, but the heartbeat is beaten between GETs."""
    seen: set[str] = set()

    def answer(url):
        now = storm["time"].now()
        if url == podlib.TIME_URL:  # /time never retries a 429; answer it so the tick goes on to scan and decide
            return 0.2, now.timestamp()
        if url not in seen:
            seen.add(url)
            return 0.5, _429(url, "25")
        seen.discard(url)
        parts = urlsplit(url)
        if parts.path.endswith("/book"):
            return 0.5, rig.clob_book((parse_qs(parts.query).get("token_id") or [""])[0], now)
        row = rig.gamma_row(BASE)
        if parts.path.endswith("/keyset"):
            return 0.5, {"markets": [row], "next_cursor": ""}
        return 0.5, [row]

    storm["answer"] = answer
    assert _run_tick(tmp_path, monkeypatch, storm) == m2_runner.EXIT_OK
    beats = [t for t, proc, _ in storm["beats"] if proc == m2_runner.PROC]
    gets = len(storm["urls"])
    assert gets >= 4  # /time, the scan page, at least one more read, each answered after a wait
    assert len(beats) >= 3  # starting, at least one mid-tick beat, finished
    assert _max_gap(beats, 0.0, storm["time"].t) < thresholds.HEARTBEAT_RUNNER_TIMEOUT_S


def test_guarded_getter_hook_runs_after_answers_and_failures_and_never_raises():
    calls: list[str] = []

    def hook():
        calls.append("beat")
        raise RuntimeError("heartbeat broke")

    ok = m2_runner.GuardedGetter(lambda url: 7, hook)
    assert ok(podlib.TIME_URL) == 7

    def failing(url):
        raise podlib.PublicGetError(url, 1, TimeoutError("x"))

    bad = m2_runner.GuardedGetter(failing, hook)
    with pytest.raises(podlib.PublicGetError):
        bad(podlib.TIME_URL)
    with pytest.raises(m2_runner.RefusedURL):
        bad("https://evil.example/order")
    assert calls == ["beat", "beat"]  # a refused URL fetches nothing and beats nothing


# ---------------------------------------------------------------- m1_run: start-up clock check


def _meta(assets):
    return {token: {"market_id": f"m{i}", "question": "Q?", "condition_id": f"0x{i}", "outcome": "Yes",
                    "liquidity_usd": 5000.0} for i, token in enumerate(assets)}


def test_m1_clock_check_in_a_429_storm_ends_within_120s_and_beats_after_each_asset(storm):
    assets = [str(90000 + i) for i in range(10)]  # r2 has 10 assets
    done: list[tuple[int, float]] = []
    result = m1_run.clock_check(assets, _meta(assets), getter=podlib.public_get,
                                beat=lambda n: done.append((n, storm["time"].t)))
    assert result["ok"] is False and result["reason"] == "no_usable_sample"  # fail closed, as before
    assert [n for n, _ in done] == list(range(1, 11))
    assert storm["time"].t < STORM_LIMIT_S
    assert _max_gap([t for _, t in done], 0.0, storm["time"].t) < thresholds.HEARTBEAT_RECORDER_TIMEOUT_S


def test_m1_clock_check_with_slow_timeouts_beats_after_each_asset(storm):
    """Worst case per asset: every attempt hits the socket timeout. Each GET still ends within the deadline."""
    storm["answer"] = lambda url: (10_000.0, None)
    assets = [str(91000 + i) for i in range(10)]
    done: list[float] = []
    m1_run.clock_check(assets, _meta(assets), getter=podlib.public_get, beat=lambda n: done.append(storm["time"].t))
    assert len(done) == 10
    assert _max_gap(done, 0.0, storm["time"].t) <= podlib.PUBLIC_GET_DEADLINE_S + 1e-9
    assert podlib.PUBLIC_GET_DEADLINE_S < thresholds.HEARTBEAT_RECORDER_TIMEOUT_S


def test_m1_clock_check_beat_failure_is_ignored(storm):
    assets = ["92001", "92002"]

    def boom(n):
        raise OSError("disk")

    result = m1_run.clock_check(assets, _meta(assets), getter=podlib.public_get, beat=boom)
    assert result["requested"] == 2 and result["unusable"] == 2


def test_m1_run_startup_in_a_429_storm_ends_within_120s_and_beats_per_asset(tmp_path, storm):
    assets = [str(93000 + i) for i in range(10)]
    rows = [{"ts": "2026-10-14T04:00:00Z", "id": f"m{i}", "question": "Q?", "condition_id": f"0x{i}",
             "token_ids": [assets[2 * i], assets[2 * i + 1]], "version": "v2", "token_id_source": "clobTokenIds",
             "token_ids_problem": "", "outcomes": ["Yes", "No"], "liquidity_usd": 5000.0, "volume_24h_usd": 100.0}
            for i in range(5)]
    markets = tmp_path / "markets.jsonl"
    markets.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    code = m1_run.main(["--out", str(tmp_path / "run"), "--markets", str(markets), "--max-runtime", "60",
                        "--top", "10"], transport=object(), rest_getter=podlib.public_get,
                       clock_getter=podlib.public_get, install_signals=False, git=lambda: {"sha": "f" * 40, "dirty": False})
    assert code == m1_run.EXIT_CLOCK_BEHIND  # nothing usable in a storm: refuse to start, as before
    assert not (tmp_path / "run").exists()
    assert storm["time"].t < STORM_LIMIT_S
    beats = [t for t, proc, state in storm["beats"] if proc == "m1_run" and state == "starting"]
    assert len(beats) >= 1 + 10  # the start beat plus one per asset read
