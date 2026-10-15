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


# ---------------------------------------------------------------- Task DU: Poly风控 6168109778 / Codex 6167868817


def _wait_no_workers(limit_s: float = 3.0) -> None:
    end = time.monotonic() + limit_s
    while any(t.name == "public_get" for t in threading.enumerate()) and time.monotonic() < end:
        time.sleep(0.01)


def test_slot_wait_counts_toward_the_deadline(monkeypatch):
    """Codex 6167868817 P1: 4 slots busy for 0.6 s, then our request hangs: total stays <= deadline 1.0 s."""
    _wait_no_workers()
    release = threading.Event()

    def fake_urlopen(request, timeout=None):
        if "hog" in request.full_url:
            time.sleep(0.6)  # uncancellable: holds its slot 0.6 s
            raise socket.timeout("timed out")
        release.wait(10)
        raise socket.timeout("timed out")

    monkeypatch.setattr(podlib, "urlopen", fake_urlopen)
    hog_url = GAMMA_URL + "&hog=1"
    hogs = [threading.Thread(target=lambda: _swallow(hog_url)) for _ in range(podlib._MAX_WORKERS)]
    try:
        for h in hogs:
            h.start()
        time.sleep(0.05)
        result, elapsed = _timed_public_get(timeout=10.0, deadline_s=1.0, attempts=1)
        assert isinstance(result, podlib.PublicGetError), result
        assert elapsed <= 1.0 + 0.3, elapsed
    finally:
        release.set()
        for h in hogs:
            h.join(5)
        _wait_no_workers()


def _swallow(url):
    try:
        podlib.public_get(url, timeout=10.0, deadline_s=0.3, attempts=1)
    except podlib.PublicGetError:
        pass


def test_slow_headers_free_their_slots_and_healthy_requests_recover(monkeypatch):
    """Codex 6167868817 P2: slow-drip headers on every slot are cancelled at the deadline (socket shut down),
    so the slots free promptly and a healthy request right after succeeds."""
    _wait_no_workers()
    opener = podlib._make_opener({})  # podlib's tracked handlers, no proxy for 127.0.0.1
    with _DripServer(every=0.05, phase="headers") as slow, _DripServer(every=0.002, step=8) as fast:
        target = {"port": slow.port}
        monkeypatch.setattr(podlib, "urlopen", lambda request, timeout=None: opener.open(
            f"http://127.0.0.1:{target['port']}/markets", timeout=timeout))
        results = []
        callers = [threading.Thread(target=lambda: results.append(_timed_public_get(timeout=5.0, deadline_s=0.5, attempts=1)))
                   for _ in range(podlib._MAX_WORKERS)]
        for c in callers:
            c.start()
        for c in callers:
            c.join(5)
        assert len(results) == podlib._MAX_WORKERS
        assert all(isinstance(r, podlib.PublicGetError) for r, _ in results), results
        target["port"] = fast.port
        result, elapsed = _timed_public_get(timeout=2.0, deadline_s=2.0, attempts=1)
        assert result == {"ok": "x" * 40}, result
        assert elapsed < 1.0, elapsed


def test_max_workers_is_bounded_at_import():
    assert podlib._valid_max_workers(podlib._MAX_WORKERS)
    assert 1 <= podlib._MAX_WORKERS <= 16
    for bad in (0, 17, -1, True, 4.0, "4", None):
        assert not podlib._valid_max_workers(bad), bad
    for good in (1, 4, 16):
        assert podlib._valid_max_workers(good)
    import inspect
    assert "assert _valid_max_workers(_MAX_WORKERS)" in inspect.getsource(podlib)


# ---------------------------------------------------------------- Task DU: Codex 6168938541 / Poly风控 ruling


class _BigResponse:
    """read1 hands out ``total`` bytes in 1 MiB pieces; ``length`` mimics http.client's Content-Length left."""

    def __init__(self, total: int, length=None) -> None:
        self.left, self.length, self.reads = total, length, 0

    def read1(self, n=-1):
        self.reads += 1
        if self.length is not None and self.length > podlib._MAX_BODY_BYTES:
            raise AssertionError("read a body whose Content-Length is over the cap")
        piece = min(self.left, 1024 * 1024)
        self.left -= piece
        return b" " * piece

    def close(self):
        pass


def test_max_body_bytes_is_8_mib():
    assert podlib._MAX_BODY_BYTES == 8 * 1024 * 1024


def test_body_over_the_cap_while_reading_fails_closed_without_retry(monkeypatch):
    calls = []

    def fake_urlopen(request, timeout=None):
        calls.append(1)
        return _BigResponse(podlib._MAX_BODY_BYTES + 2 * 1024 * 1024)  # no Content-Length (chunked)

    monkeypatch.setattr(podlib, "urlopen", fake_urlopen)
    with pytest.raises(podlib.PublicGetError) as info:
        podlib.public_get(GAMMA_URL, timeout=5.0, deadline_s=10.0, attempts=3)
    assert isinstance(info.value.__cause__, podlib._BodyTooLarge)
    assert len(calls) == 1  # never retried


def test_content_length_over_the_cap_is_rejected_before_reading(monkeypatch):
    responses = []

    def fake_urlopen(request, timeout=None):
        responses.append(_BigResponse(podlib._MAX_BODY_BYTES + 1, length=podlib._MAX_BODY_BYTES + 1))
        return responses[-1]

    monkeypatch.setattr(podlib, "urlopen", fake_urlopen)
    with pytest.raises(podlib.PublicGetError) as info:
        podlib.public_get(GAMMA_URL, timeout=5.0, deadline_s=10.0, attempts=3)
    assert isinstance(info.value.__cause__, podlib._BodyTooLarge)
    assert len(responses) == 1 and responses[0].reads == 0


class _SlowConnectProxy:
    """127.0.0.1 HTTP proxy that answers CONNECT one byte every 0.05 s (each recv beats the socket timeout)."""

    def __init__(self) -> None:
        self.stop = threading.Event()
        self.sock = socket.create_server(("127.0.0.1", 0))
        self.sock.settimeout(0.1)
        self.port = self.sock.getsockname()[1]
        self.connects = 0
        self.thread = threading.Thread(target=self._serve, daemon=True)

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

    def _answer(self, conn) -> None:
        try:
            conn.settimeout(5)
            got = b""
            while b"\r\n\r\n" not in got:
                piece = conn.recv(4096)
                if not piece:
                    return
                got += piece
            if got.startswith(b"CONNECT"):
                self.connects += 1
            for byte in b"HTTP/1.1 200 Connection established\r\nX-Pad: " + b"p" * 400 + b"\r\n\r\n":
                if self.stop.is_set():
                    return
                conn.sendall(bytes([byte]))
                time.sleep(0.05)
        except OSError:
            pass  # the client shut the socket down at its deadline: that is the point
        finally:
            conn.close()


def test_slow_proxy_connect_frees_every_slot_and_healthy_requests_recover(monkeypatch):
    """Codex 6168938541 P2: a slow proxy CONNECT on all 4 slots is cancelled at the deadline (socket registered
    at creation, before the tunnel and TLS), every slot is freed, and a healthy request then succeeds in < 1 s."""
    _wait_no_workers()
    with _SlowConnectProxy() as proxy, _DripServer(every=0.002, step=8) as fast:
        proxied = podlib._make_opener({"https": f"http://127.0.0.1:{proxy.port}"})
        direct = podlib._make_opener({})
        mode = {"slow": True}

        def routed_urlopen(request, timeout=None):
            if mode["slow"]:
                return proxied.open("https://gamma-api.polymarket.com/markets", timeout=timeout)
            return direct.open(f"http://127.0.0.1:{fast.port}/markets", timeout=timeout)

        monkeypatch.setattr(podlib, "urlopen", routed_urlopen)
        results = []
        callers = [threading.Thread(target=lambda: results.append(_timed_public_get(timeout=5.0, deadline_s=0.5, attempts=1)))
                   for _ in range(podlib._MAX_WORKERS)]
        for c in callers:
            c.start()
        for c in callers:
            c.join(5)
        assert len(results) == podlib._MAX_WORKERS
        assert all(isinstance(r, podlib.PublicGetError) for r, _ in results), results
        assert proxy.connects >= 1
        mode["slow"] = False
        result, elapsed = _timed_public_get(timeout=2.0, deadline_s=2.0, attempts=1)
        assert result == {"ok": "x" * 40}, result
        assert elapsed < 1.0, elapsed
        _wait_no_workers(1.0)
        assert not any(t.name == "public_get" for t in threading.enumerate())


# ---------------------------------------------------------------- Task DU: Codex 6169540943 / Poly风控 6169773911

import ssl  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402


@pytest.fixture
def tls_cert(tmp_path):
    # Task EC: no openssl CLI (or one too old for -addext) skips this TLS case cleanly instead of erroring.
    if shutil.which("openssl") is None:
        pytest.skip("openssl CLI not available to make a throwaway TLS cert")
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    try:
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(cert),
                        "-days", "1", "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1"],
                       check=True, capture_output=True, timeout=60)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        pytest.skip(f"openssl could not make a throwaway TLS cert: {type(exc).__name__}")
    return cert, key


class _SlowTlsHeadersServer:
    """127.0.0.1 HTTPS server: the TLS handshake completes at once, then the response headers drip 1 byte / 0.05 s."""

    def __init__(self, cert, key) -> None:
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.load_cert_chain(str(cert), str(key))
        self.stop = threading.Event()
        self.sock = socket.create_server(("127.0.0.1", 0))
        self.sock.settimeout(0.1)
        self.port = self.sock.getsockname()[1]
        self.handshakes = 0
        self.thread = threading.Thread(target=self._serve, daemon=True)

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

    def _answer(self, raw) -> None:
        try:
            raw.settimeout(5)
            conn = self.ctx.wrap_socket(raw, server_side=True)
            self.handshakes += 1
            got = b""
            while b"\r\n\r\n" not in got:
                piece = conn.recv(4096)
                if not piece:
                    return
                got += piece
            for byte in b"HTTP/1.1 200 OK\r\nX-Pad: " + b"p" * 400 + b"\r\n\r\n":
                if self.stop.is_set():
                    return
                conn.sendall(bytes([byte]))
                time.sleep(0.05)
        except (OSError, ssl.SSLError):
            pass  # the client shut the TLS socket down at its deadline: that is the point
        finally:
            raw.close()


def test_slow_https_headers_free_every_slot_and_healthy_requests_recover(monkeypatch, tls_cert):
    """Codex 6169540943 P1: after the TLS wrap the raw socket's fd is detached, so the SSLSocket itself must be
    shut down at the deadline. 4 slow-header HTTPS requests fill all slots; all are freed, then a healthy request
    succeeds in < 1 s and no worker thread is left."""
    _wait_no_workers()
    cert, key = tls_cert
    client_ctx = ssl.create_default_context(cafile=str(cert))
    tls_opener = podlib.build_opener(podlib.ProxyHandler({}), podlib._TrackedHTTPSHandler(context=client_ctx))
    direct = podlib._make_opener({})
    with _SlowTlsHeadersServer(cert, key) as slow, _DripServer(every=0.002, step=8) as fast:
        mode = {"slow": True}

        def routed_urlopen(request, timeout=None):
            if mode["slow"]:
                return tls_opener.open(f"https://127.0.0.1:{slow.port}/markets", timeout=timeout)
            return direct.open(f"http://127.0.0.1:{fast.port}/markets", timeout=timeout)

        monkeypatch.setattr(podlib, "urlopen", routed_urlopen)
        results = []
        callers = [threading.Thread(target=lambda: results.append(_timed_public_get(timeout=5.0, deadline_s=0.5, attempts=1)))
                   for _ in range(podlib._MAX_WORKERS)]
        for c in callers:
            c.start()
        for c in callers:
            c.join(5)
        assert len(results) == podlib._MAX_WORKERS
        assert all(isinstance(r, podlib.PublicGetError) for r, _ in results), results
        assert slow.handshakes >= 1
        _wait_no_workers(0.5)
        assert not any(t.name == "public_get" for t in threading.enumerate())
        mode["slow"] = False
        result, elapsed = _timed_public_get(timeout=2.0, deadline_s=2.0, attempts=1)
        assert result == {"ok": "x" * 40}, result
        assert elapsed < 1.0, elapsed
        _wait_no_workers(1.0)
        assert not any(t.name == "public_get" for t in threading.enumerate())


# ---------------------------------------------------------------- Task DY (#330): bound JSON-parse overrun


def _near_cap_json_bytes() -> bytes:
    """Valid JSON array just under _MAX_BODY_BYTES (ints: dense, deterministic size)."""
    budget = podlib._MAX_BODY_BYTES - 64
    # "[" + "0,"*(n-1) + "0]" has length 2*n + 1
    n = max(1, (budget - 1) // 2)
    while 2 * n + 1 > budget:
        n -= 1
    return ("[" + "0," * (n - 1) + "0]").encode("ascii")


def _gamma_like_cap_json_bytes() -> bytes:
    """Near-cap Gamma-like object array (realistic parse cost for the wall-clock bound)."""
    item = (
        '{"id":"' + ("x" * 20) + '","question":"' + ("q" * 80)
        + '","outcomePrices":"[0.5,0.5]","volume":"12345.67","liquidity":"999.99"}'
    )
    item_b = len(item.encode("ascii"))
    n = max(1, (podlib._MAX_BODY_BYTES - 64) // (item_b + 1))
    raw = ("[" + ",".join(item for _ in range(n)) + "]").encode("ascii")
    assert len(raw) <= podlib._MAX_BODY_BYTES, len(raw)
    return raw


def _measure_decode_loads_s(body: bytes) -> float:
    text = body.decode("utf-8")
    t0 = time.perf_counter()
    podlib.json.loads(text)
    return time.perf_counter() - t0


def test_parse_budget_scales_with_body_size():
    assert podlib._PARSE_BUDGET_S_AT_CAP == 1.0
    assert podlib._parse_budget_s(0) == 0.0
    assert podlib._parse_budget_s(podlib._MAX_BODY_BYTES) == pytest.approx(1.0)
    assert podlib._parse_budget_s(podlib._MAX_BODY_BYTES // 2) == pytest.approx(0.5)


def test_ensure_parse_budget_refuses_when_too_little_time_left():
    now = podlib._monotonic()
    with pytest.raises(podlib._DeadlineExceeded):
        podlib._ensure_parse_budget(podlib._MAX_BODY_BYTES, started=now, budget=0.4)
    podlib._ensure_parse_budget(podlib._MAX_BODY_BYTES, started=now, budget=1.5)  # enough
    podlib._ensure_parse_budget(100, started=now, budget=0.05)  # tiny body: almost free


def test_precheck_refuses_near_cap_parse_when_little_deadline_left(clock, monkeypatch):
    """Fake clock: body arrives at 29.5 s of a 30 s deadline; left 0.5 s < 1.0 s budget → fail closed, no loads."""
    body = _near_cap_json_bytes()
    assert podlib._parse_budget_s(len(body)) > 0.5
    clock["set"]((29.5, body))
    calls = {"n": 0}
    real_loads = podlib.json.loads

    def counting_loads(text, *args, **kwargs):
        calls["n"] += 1
        return real_loads(text, *args, **kwargs)

    monkeypatch.setattr(podlib.json, "loads", counting_loads)
    with pytest.raises(podlib.PublicGetError) as info:
        podlib.public_get(GAMMA_URL, timeout=60.0, deadline_s=30.0, attempts=1)
    assert isinstance(info.value.__cause__, podlib._DeadlineExceeded)
    assert calls["n"] == 0  # never started the GIL-holding parse


def test_near_cap_body_with_enough_deadline_still_parses(clock):
    body = _near_cap_json_bytes()
    clock["set"]((1.0, body))
    assert isinstance(podlib.public_get(GAMMA_URL, deadline_s=30.0, attempts=1), list)


class _FixedBodyServer:
    """127.0.0.1 HTTP/1.1 server that sends ``body`` in one shot (real sockets, offline)."""

    def __init__(self, body: bytes) -> None:
        self.body = body
        self.stop = threading.Event()
        self.sock = socket.create_server(("127.0.0.1", 0))
        self.sock.settimeout(0.1)
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, name="fixed-body", daemon=True)

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

    def _answer(self, conn) -> None:
        try:
            conn.settimeout(5)
            request = b""
            while b"\r\n\r\n" not in request:
                got = conn.recv(4096)
                if not got:
                    return
                request += got
            head = (
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                + b"Content-Length: %d\r\nConnection: close\r\n\r\n" % len(self.body)
            )
            conn.sendall(head + self.body)
        except OSError:
            pass
        finally:
            conn.close()


def test_wall_clock_overrun_bounded_by_one_cap_parse(local):
    """#330: real wall clock, real local HTTP server, no fake clock.

    A near-cap body with a short deadline must fail closed (never a late OK), and the caller's elapsed
    time stays <= deadline + time to parse one cap-size body (+ margin for a loaded machine).
    """
    body = _gamma_like_cap_json_bytes()
    parse_s = _measure_decode_loads_s(body)
    # Slack for thread start-up, scheduler jitter and a loaded CI/dev box.
    margin_s = max(0.75, 3.0 * parse_s)
    deadline_s = 0.05  # short: connect+read of ~8 MiB locally is fast; parse may start near/past deadline
    with _FixedBodyServer(body) as server:
        local(server)
        result, elapsed = _timed_public_get(timeout=5.0, deadline_s=deadline_s, attempts=1)
    assert isinstance(result, podlib.PublicGetError), result
    assert elapsed <= deadline_s + parse_s + margin_s, (
        f"elapsed={elapsed:.4f} deadline={deadline_s} parse_s={parse_s:.4f} margin={margin_s:.4f}"
    )


def test_wall_clock_near_cap_inside_deadline_still_returns(local):
    """A near-cap body with a generous deadline still parses successfully (no behaviour change)."""
    body = _gamma_like_cap_json_bytes()
    with _FixedBodyServer(body) as server:
        local(server)
        result, elapsed = _timed_public_get(timeout=10.0, deadline_s=30.0, attempts=1)
    assert isinstance(result, list) and len(result) > 0, type(result)
    assert elapsed < 10.0, elapsed
