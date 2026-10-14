"""Task DQ (Poly哨兵 on #309): one public_get call is bounded by PUBLIC_GET_DEADLINE_S, /time never retries 429.

Offline: urlopen is scripted and a fake monotonic clock advances on every socket wait and every sleep.
"""
from __future__ import annotations

import io
import socket
from email.message import Message
from urllib.error import HTTPError

import pytest

from src import podlib, thresholds

GAMMA_URL = podlib.GAMMA_PREFIX + "?closed=false&limit=1"


class _Response:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(url: str, code: int, retry_after: str | None = None) -> HTTPError:
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return HTTPError(url, code, "error", headers, io.BytesIO(b""))


@pytest.fixture
def clock(monkeypatch):
    """A fake monotonic clock. Each scripted step is (seconds the request takes, outcome)."""
    state = {"t": 0.0, "script": [], "timeouts": [], "sleeps": []}

    def fake_urlopen(request, timeout=None):
        state["timeouts"].append(timeout)
        took, outcome = state["script"].pop(0) if state["script"] else state["last"]
        if took is None or took > timeout:  # the socket times out: the attempt uses its whole (clipped) timeout
            state["t"] += float(timeout)
            raise socket.timeout("timed out")
        state["t"] += took
        if isinstance(outcome, BaseException):
            raise outcome
        return _Response(outcome)

    def fake_sleep(seconds):
        state["sleeps"].append(seconds)
        state["t"] += seconds

    monkeypatch.setattr(podlib, "urlopen", fake_urlopen)
    monkeypatch.setattr(podlib, "_sleep", fake_sleep)
    monkeypatch.setattr(podlib, "_monotonic", lambda: state["t"])

    def script(*steps, then=None):
        state["script"] = list(steps)
        state["last"] = then if then is not None else steps[-1]

    state["set"] = script
    return state


def test_deadline_constant_is_a_quarter_of_the_heartbeat_timeout():
    assert podlib.PUBLIC_GET_DEADLINE_S == thresholds.PUBLIC_GET_DEADLINE_S == 30.0
    assert podlib.PUBLIC_GET_DEADLINE_S <= thresholds.HEARTBEAT_RUNNER_TIMEOUT_S / 4


def test_429_storm_with_retry_after_30_ends_within_the_deadline(clock):
    clock["set"]((0.05, _http_error(GAMMA_URL, 429, "30")))
    with pytest.raises(podlib.PublicGetError):
        podlib.public_get(GAMMA_URL)
    assert clock["sleeps"] == []  # a 30 s wait would end past the 30 s deadline: not taken
    assert clock["t"] <= podlib.PUBLIC_GET_DEADLINE_S


def test_429_with_short_retry_after_still_retries_inside_the_deadline(clock):
    clock["set"]((0.1, _http_error(GAMMA_URL, 429, "5")), (0.1, _http_error(GAMMA_URL, 429, "5")), (0.1, b'{"ok": 1}'))
    assert podlib.public_get(GAMMA_URL) == {"ok": 1}
    assert clock["sleeps"] == [5.0, 5.0]
    assert clock["t"] <= podlib.PUBLIC_GET_DEADLINE_S


def test_socket_timeouts_are_clipped_to_the_time_left(clock):
    clock["set"]((None, None))
    with pytest.raises(podlib.PublicGetError):
        podlib.public_get(GAMMA_URL)  # default timeout 20 s
    assert clock["t"] <= podlib.PUBLIC_GET_DEADLINE_S + 1e-9
    assert clock["timeouts"][0] == 20.0
    assert all(t <= podlib.PUBLIC_GET_DEADLINE_S for t in clock["timeouts"])
    assert sum(clock["timeouts"]) + sum(clock["sleeps"]) <= podlib.PUBLIC_GET_DEADLINE_S + 1e-9
    assert len(clock["timeouts"]) == 2  # 20 s, 0.5 s backoff, then the remaining 9.5 s


def test_retry_after_plus_slow_answers_never_pass_the_deadline(clock):
    clock["set"]((12.0, _http_error(GAMMA_URL, 429, "10")), (12.0, _http_error(GAMMA_URL, 429, "10")), (0.1, b"{}"))
    with pytest.raises(podlib.PublicGetError):
        podlib.public_get(GAMMA_URL)
    assert clock["t"] <= podlib.PUBLIC_GET_DEADLINE_S  # 12 + 10 = 22, then 22 + 12 + 10 would pass 30


def test_custom_deadline(clock):
    clock["set"]((0.0, _http_error(GAMMA_URL, 429, "2")), (0.0, b'{"ok": 2}'))
    with pytest.raises(podlib.PublicGetError):
        podlib.public_get(GAMMA_URL, deadline_s=1.0)
    clock["set"]((0.0, _http_error(GAMMA_URL, 429, "2")), (0.0, b'{"ok": 2}'))
    assert podlib.public_get(GAMMA_URL, deadline_s=5.0) == {"ok": 2}


def test_time_url_does_not_retry_429(clock):
    clock["set"]((0.05, _http_error(podlib.TIME_URL, 429, "1")), then=(0.05, b"1760000000"))
    with pytest.raises(podlib.PublicGetError) as info:
        podlib.public_get(podlib.TIME_URL)
    assert len(clock["timeouts"]) == 1
    assert clock["sleeps"] == []
    assert isinstance(info.value.__cause__, HTTPError) and info.value.__cause__.code == 429


def test_time_url_still_retries_5xx(clock):
    clock["set"]((0.05, _http_error(podlib.TIME_URL, 503)), (0.05, b"1760000000"))
    assert podlib.public_get(podlib.TIME_URL) == 1760000000
    assert clock["sleeps"] == [podlib.PUBLIC_GET_BACKOFF_S]


def test_other_4xx_still_raise_at_once(clock):
    clock["set"]((0.0, _http_error(GAMMA_URL, 404)))
    with pytest.raises(HTTPError):
        podlib.public_get(GAMMA_URL)
    assert len(clock["timeouts"]) == 1
