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


# ---------------------------------------------------------------- Task DU (#316): hard wall-clock read deadline
# The fake network above returns a body in one go, so it hides how long the body read takes. These tests use a
# real local HTTP server (127.0.0.1, ephemeral port, its own thread) that drips the answer slowly. Offline only:
# podlib.urlopen is pointed at 127.0.0.1, with no proxy, so nothing leaves the machine.

import http.client  # noqa: E402
import math  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import urllib.request  # noqa: E402

_DRIP_BODY = b'{"ok": "' + b"x" * 40 + b'"}'


class _DripServer:
    """A raw-socket HTTP/1.1 server on 127.0.0.1 that sends its answer ``step`` bytes every ``every`` seconds.

    ``phase`` picks what drips: "body" sends the status line and headers at once and drips the body; "headers"
    drips the headers too. ``chunked`` uses Transfer-Encoding: chunked (one HTTP chunk per drip) instead of
    Content-Length.
    """

    def __init__(self, *, every: float, phase: str = "body", chunked: bool = False, step: int = 1) -> None:
        self.every, self.phase, self.chunked, self.step = every, phase, chunked, step
        self.stop = threading.Event()
        self.sock = socket.create_server(("127.0.0.1", 0))
        self.sock.settimeout(0.1)
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, name="drip-server", daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.thread.join(5)
        self.sock.close()
        return False

    def _serve(self) -> None:
        while not self.stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except (socket.timeout, OSError):
                continue
            threading.Thread(target=self._answer, args=(conn,), daemon=True).start()

    def _drip(self, conn, data: bytes, *, chunk_frame: bool = False) -> None:
        for i in range(0, len(data), self.step):
            if self.stop.is_set():
                return
            piece = data[i:i + self.step]
            conn.sendall(b"%x\r\n%s\r\n" % (len(piece), piece) if chunk_frame else piece)
            time.sleep(self.every)

    def _answer(self, conn) -> None:
        try:
            conn.settimeout(5)
            request = b""
            while b"\r\n\r\n" not in request:
                got = conn.recv(4096)
                if not got:
                    return
                request += got
            framing = b"Transfer-Encoding: chunked\r\n" if self.chunked else b"Content-Length: %d\r\n" % len(_DRIP_BODY)
            head = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n" + framing + b"Connection: close\r\n\r\n"
            if self.phase == "headers":
                self._drip(conn, head)
            else:
                conn.sendall(head)
            self._drip(conn, _DRIP_BODY, chunk_frame=self.chunked)
            if self.chunked and not self.stop.is_set():
                conn.sendall(b"0\r\n\r\n")
        except OSError:
            pass  # the client gave up (deadline): that is the point
        finally:
            conn.close()


@pytest.fixture
def local(monkeypatch):
    """Point podlib.urlopen at a local server: the real urllib stack (socket, http.client, body read), no proxy."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    calls = []

    def use(server: _DripServer) -> list:
        def local_urlopen(request, timeout=None):
            calls.append(timeout)
            return opener.open(f"http://127.0.0.1:{server.port}/markets", timeout=timeout)

        monkeypatch.setattr(podlib, "urlopen", local_urlopen)
        return calls

    return use


def _timed_public_get(**kwargs):
    started = time.monotonic()
    try:
        return podlib.public_get(GAMMA_URL, **kwargs), time.monotonic() - started
    except podlib.PublicGetError as exc:
        return exc, time.monotonic() - started


# Slack for thread start-up, interpreter jitter and a slow CI box. The drip itself would take ~4 s.
_MARGIN_S = 0.75


@pytest.mark.parametrize("chunked", [False, True], ids=["content-length", "chunked"])
def test_slow_drip_body_fails_closed_at_the_deadline(local, chunked):
    """#316: each byte arrives well inside the socket timeout, but the whole body would take ~4 s > deadline 1.5 s."""
    with _DripServer(every=0.1, chunked=chunked) as server:
        local(server)
        result, elapsed = _timed_public_get(timeout=1.0, deadline_s=1.5)
    assert isinstance(result, podlib.PublicGetError), result  # never partial data, never a late OK
    assert elapsed <= 1.5 + _MARGIN_S, elapsed


def test_slow_drip_headers_fail_closed_at_the_deadline(local):
    """The hard cap covers the status line and headers too, not only the body."""
    with _DripServer(every=0.05, phase="headers") as server:
        local(server)
        result, elapsed = _timed_public_get(timeout=1.0, deadline_s=1.0)
    assert isinstance(result, podlib.PublicGetError), result
    assert elapsed <= 1.0 + _MARGIN_S, elapsed


@pytest.mark.parametrize("chunked", [False, True], ids=["content-length", "chunked"])
def test_answer_inside_the_deadline_still_returns(local, chunked):
    """A real answer that arrives in pieces but well inside the deadline still parses (no behaviour change)."""
    with _DripServer(every=0.002, chunked=chunked, step=8) as server:
        calls = local(server)
        result, elapsed = _timed_public_get(timeout=2.0, deadline_s=5.0)
    assert result == {"ok": "x" * 40}
    assert len(calls) == 1 and elapsed < 5.0


class _ChunkedFakeResponse:
    """A response whose read1() advances the fake clock by ``per_chunk`` seconds for each ``size``-byte chunk."""

    def __init__(self, state, body: bytes, per_chunk: float, size: int = 4) -> None:
        self.state, self.body, self.per_chunk, self.size = state, body, per_chunk, size

    def read1(self, n=-1):
        self.state["t"] += self.per_chunk
        piece, self.body = self.body[: self.size], self.body[self.size:]
        return piece

    def read(self, n=-1):  # pragma: no cover - public_get must read in chunks when read1 exists
        raise AssertionError("public_get read the whole body in one call")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_chunked_body_read_checks_the_deadline_on_the_fake_clock(clock, monkeypatch):
    """Deterministic: 3 s per 4-byte chunk, deadline 10 s: the read stops after the deadline, never returns."""
    monkeypatch.setattr(podlib, "urlopen", lambda request, timeout=None: _ChunkedFakeResponse(clock, b'{"ok": 123456789}', 3.0))
    with pytest.raises(podlib.PublicGetError):
        podlib.public_get(GAMMA_URL, timeout=5.0, deadline_s=10.0)
    assert clock["sleeps"] == []  # the deadline is final: no retry after it


def test_chunked_body_inside_the_deadline_returns_on_the_fake_clock(clock, monkeypatch):
    monkeypatch.setattr(podlib, "urlopen", lambda request, timeout=None: _ChunkedFakeResponse(clock, b'{"ok": 1}', 1.0))
    assert podlib.public_get(GAMMA_URL, timeout=5.0, deadline_s=10.0) == {"ok": 1}


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf, 0.0, -1.0, 0, True, False, "30"])
def test_deadline_must_be_a_finite_positive_number(monkeypatch, bad):
    """#316 (Codex P2 on #314): NaN / inf skipped the budget check. Anything but a finite number > 0 fails closed
    before any network call."""
    def no_network(request, timeout=None):
        raise AssertionError("public_get reached the network with a bad deadline_s")

    monkeypatch.setattr(podlib, "urlopen", no_network)
    with pytest.raises(ValueError):
        podlib.public_get(GAMMA_URL, deadline_s=bad)


def test_default_and_valid_custom_deadlines_are_accepted(clock):
    clock["set"]((0.0, b'{"ok": 3}'))
    assert podlib.public_get(GAMMA_URL) == {"ok": 3}
    assert podlib.public_get(GAMMA_URL, deadline_s=2) == {"ok": 3}  # an int is a number
    assert podlib.public_get(GAMMA_URL, deadline_s=0.5) == {"ok": 3}


# ---------------------------------------------------------------- Task DU: Codex REQUEST_CHANGES 6167337740 on #328


class _TruncatingServer:
    """127.0.0.1 server that announces Content-Length: 100 but sends only a short body, then closes."""

    def __init__(self) -> None:
        self.sock = socket.create_server(("127.0.0.1", 0))
        self.sock.settimeout(5)
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.thread.join(5)
        self.sock.close()
        return False

    def _serve(self) -> None:
        for _ in range(3):  # public_get may retry: answer every attempt the same short way
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                conn.settimeout(5)
                request = b""
                while b"\r\n\r\n" not in request:
                    got = conn.recv(4096)
                    if not got:
                        break
                    request += got
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 100\r\n"
                             b"Connection: close\r\n\r\n{}")


def test_truncated_content_length_body_fails_closed(local):
    """Codex P1: '{}' is valid JSON, but 98 of the 100 announced bytes are missing: never a success."""
    with _TruncatingServer() as server:
        local(server)
        result, _ = _timed_public_get(timeout=2.0, deadline_s=5.0, attempts=1)
    assert isinstance(result, podlib.PublicGetError), result
    assert isinstance(result.__cause__, http.client.IncompleteRead), repr(result.__cause__)


class _BlockingResponse:
    """read1 blocks until close() is called (like a recv on a socket that gets shut down)."""

    def __init__(self) -> None:
        self.closed = threading.Event()

    def read1(self, n=-1):
        self.closed.wait(10)
        raise OSError("closed")

    def close(self):
        self.closed.set()


def test_deadline_closes_the_response_of_the_abandoned_worker(monkeypatch):
    """Codex P2: after the deadline the worker's response is closed at once, so the thread ends now."""
    response = _BlockingResponse()
    monkeypatch.setattr(podlib, "urlopen", lambda request, timeout=None: response)
    result, elapsed = _timed_public_get(timeout=5.0, deadline_s=0.3)
    assert isinstance(result, podlib.PublicGetError), result
    assert response.closed.wait(1.0), "the abandoned response was never closed"
    deadline = time.monotonic() + 2.0
    while any(t.name == "public_get" for t in threading.enumerate()) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not any(t.name == "public_get" for t in threading.enumerate())


def test_worker_threads_stuck_in_urlopen_are_bounded(monkeypatch):
    """Codex P2: workers stuck in urlopen (connect/headers) cannot pile up past podlib._MAX_WORKERS."""
    release = threading.Event()

    def stuck_urlopen(request, timeout=None):
        release.wait(10)
        raise socket.timeout("timed out")

    monkeypatch.setattr(podlib, "urlopen", stuck_urlopen)
    try:
        for _ in range(podlib._MAX_WORKERS * 2 + 2):
            result, _ = _timed_public_get(timeout=10.0, deadline_s=0.05, attempts=1)
            assert isinstance(result, podlib.PublicGetError), result
        alive = sum(1 for t in threading.enumerate() if t.name == "public_get")
        assert alive <= podlib._MAX_WORKERS, alive
    finally:
        release.set()
        end = time.monotonic() + 3.0
        while any(t.name == "public_get" for t in threading.enumerate()) and time.monotonic() < end:
            time.sleep(0.01)


def test_decode_and_parse_count_toward_the_deadline(clock, monkeypatch):
    """Codex P2: the body arrives at 1 s, but parsing ends at 31 s > deadline 30 s: raise, not success."""
    clock["set"]((1.0, b'{"ok": 1}'))
    real_loads = podlib.json.loads

    def slow_loads(text, *args, **kwargs):
        clock["t"] += 30.0
        return real_loads(text, *args, **kwargs)

    monkeypatch.setattr(podlib.json, "loads", slow_loads)
    with pytest.raises(podlib.PublicGetError):
        podlib.public_get(GAMMA_URL, deadline_s=30.0)


def test_fast_parse_inside_the_deadline_still_returns(clock):
    clock["set"]((1.0, b'{"ok": 1}'))
    assert podlib.public_get(GAMMA_URL, deadline_s=30.0) == {"ok": 1}
