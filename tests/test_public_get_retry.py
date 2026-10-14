"""Offline tests for public_get retry/backoff (Task K; HTTP 429 Retry-After: Task DP, issue #199).

No network: urlopen is mocked, sleep is recorded."""
from __future__ import annotations

import io
import json
import socket
import ssl
from email.message import Message
from urllib.error import HTTPError, URLError

import pytest

import src.podlib as podlib
import src.scanner as scanner
from src.podlib import RISK_PATH

GAMMA_URL = podlib.GAMMA_PREFIX + "/keyset?closed=false&limit=1"


class _Response:
    def __init__(self, payload: object) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def _ssl_eof() -> ssl.SSLEOFError:
    return ssl.SSLEOFError(8, "[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol (_ssl.c:1081)")


@pytest.fixture
def fake_net(monkeypatch):
    """Replace urlopen with a scripted sequence and record backoff sleeps instead of sleeping."""
    state = {"script": [], "calls": [], "sleeps": []}

    def fake_urlopen(request, timeout=None):
        state["calls"].append(request.full_url)
        step = state["script"].pop(0) if state["script"] else state["last"]
        if isinstance(step, BaseException):
            raise step
        return _Response(step)

    monkeypatch.setattr(podlib, "urlopen", fake_urlopen)
    monkeypatch.setattr(podlib, "_sleep", state["sleeps"].append)

    def script(*steps: object, then: object | None = None) -> None:
        state["script"] = list(steps)
        state["last"] = then if then is not None else steps[-1]

    state["set"] = script
    return state


def _http_error(code: int, retry_after: str | None = None) -> HTTPError:
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return HTTPError(GAMMA_URL, code, "error", headers, io.BytesIO(b""))


def test_retry_constants_are_small():
    assert 2 <= podlib.PUBLIC_GET_ATTEMPTS <= 5
    assert 0 < podlib.PUBLIC_GET_BACKOFF_S <= 2
    assert 0 < podlib.PUBLIC_GET_RETRY_AFTER_MAX_S <= 60


def test_ssl_eof_then_success(fake_net):
    fake_net["set"](_ssl_eof(), {"ok": 1})
    assert podlib.public_get(GAMMA_URL) == {"ok": 1}
    assert len(fake_net["calls"]) == 2
    assert fake_net["sleeps"] == [podlib.PUBLIC_GET_BACKOFF_S]


def test_ssl_eof_wrapped_in_urlerror_then_success(fake_net):
    # This is the shape seen on NoisePC: urlopen wraps the handshake SSLEOFError in URLError.
    fake_net["set"](URLError(_ssl_eof()), URLError(_ssl_eof()), {"ok": 2})
    assert podlib.public_get(GAMMA_URL) == {"ok": 2}
    assert len(fake_net["calls"]) == 3
    assert fake_net["sleeps"] == [podlib.PUBLIC_GET_BACKOFF_S, podlib.PUBLIC_GET_BACKOFF_S * 2]


def test_urlerror_then_success(fake_net):
    fake_net["set"](URLError("temporary failure in name resolution"), {"ok": 3})
    assert podlib.public_get(GAMMA_URL) == {"ok": 3}
    assert len(fake_net["calls"]) == 2


@pytest.mark.parametrize("error", [ConnectionResetError(10054, "reset"), socket.timeout("timed out"), TimeoutError("timed out")])
def test_reset_and_timeout_are_retried(fake_net, error):
    fake_net["set"](error, {"ok": 4})
    assert podlib.public_get(GAMMA_URL) == {"ok": 4}
    assert len(fake_net["calls"]) == 2


def test_http_5xx_is_retried(fake_net):
    fake_net["set"](HTTPError(GAMMA_URL, 503, "Service Unavailable", {}, io.BytesIO(b"")), {"ok": 5})
    assert podlib.public_get(GAMMA_URL) == {"ok": 5}
    assert len(fake_net["calls"]) == 2


@pytest.mark.parametrize("code", [400, 401, 403, 404, 422])
def test_http_4xx_is_not_retried(fake_net, code):
    fake_net["set"](HTTPError(GAMMA_URL, code, "client error", {}, io.BytesIO(b"")), then={"ok": 0})
    with pytest.raises(HTTPError) as info:
        podlib.public_get(GAMMA_URL)
    assert info.value.code == code
    assert len(fake_net["calls"]) == 1
    assert fake_net["sleeps"] == []


@pytest.mark.parametrize("code", [400, 401, 403])
def test_http_4xx_with_retry_after_is_still_not_retried(fake_net, code):
    # Only 429 reads Retry-After; any other 4xx is raised at once even when the header is set.
    fake_net["set"](_http_error(code, "1"), then={"ok": 0})
    with pytest.raises(HTTPError) as info:
        podlib.public_get(GAMMA_URL)
    assert info.value.code == code
    assert len(fake_net["calls"]) == 1
    assert fake_net["sleeps"] == []


# --- Task DP (issue #199): HTTP 429 is retried, waiting Retry-After seconds -----------------------------


def test_http_429_with_retry_after_waits_then_succeeds(fake_net):
    fake_net["set"](_http_error(429, "1"), {"ok": 6})
    assert podlib.public_get(GAMMA_URL) == {"ok": 6}
    assert len(fake_net["calls"]) == 2
    assert fake_net["sleeps"] == [1.0]


def test_http_429_with_retry_after_on_data_api_status(fake_net):
    fake_net["set"](_http_error(429, " 2 "), {"status": "ok"})
    assert podlib.public_get(podlib.DATA_API_STATUS_URL) == {"status": "ok"}
    assert fake_net["calls"] == [podlib.DATA_API_STATUS_URL] * 2
    assert fake_net["sleeps"] == [2.0]


def test_http_429_retry_after_zero_retries_without_waiting(fake_net):
    fake_net["set"](_http_error(429, "0"), {"ok": 7})
    assert podlib.public_get(GAMMA_URL) == {"ok": 7}
    assert fake_net["sleeps"] == [0.0]


def test_http_429_without_retry_after_backs_off_and_exhausts(fake_net):
    fake_net["set"](_http_error(429))
    with pytest.raises(podlib.PublicGetError) as info:
        podlib.public_get(GAMMA_URL)
    assert len(fake_net["calls"]) == podlib.PUBLIC_GET_ATTEMPTS
    base = podlib.PUBLIC_GET_BACKOFF_S
    assert fake_net["sleeps"] == [base * (2 ** n) for n in range(podlib.PUBLIC_GET_ATTEMPTS - 1)]
    assert isinstance(info.value.__cause__, HTTPError) and info.value.__cause__.code == 429
    assert "429" in str(info.value)


def test_http_429_without_retry_after_then_success(fake_net):
    fake_net["set"](_http_error(429), {"ok": 8})
    assert podlib.public_get(GAMMA_URL) == {"ok": 8}
    assert fake_net["sleeps"] == [podlib.PUBLIC_GET_BACKOFF_S]


@pytest.mark.parametrize(
    "value",
    ["", "soon", "-1", "1.5", "+3", "1e2", "\u00b2", "Wed, 21 Oct 2026 07:28:00 GMT"],
)
def test_http_429_invalid_retry_after_falls_back_to_backoff(fake_net, value):
    fake_net["set"](_http_error(429, value), {"ok": 9})
    assert podlib.public_get(GAMMA_URL) == {"ok": 9}
    assert fake_net["sleeps"] == [podlib.PUBLIC_GET_BACKOFF_S]


def test_http_429_retry_after_is_capped(fake_net):
    # Task DQ: a wider deadline than the default so the 30 s cap itself is visible (the default 30 s deadline
    # refuses a 30 s wait; see tests/test_public_get_deadline.py).
    fake_net["set"](_http_error(429, "3600"), {"ok": 10})
    assert podlib.public_get(GAMMA_URL, deadline_s=120.0) == {"ok": 10}
    assert fake_net["sleeps"] == [podlib.PUBLIC_GET_RETRY_AFTER_MAX_S]


def test_http_429_retry_after_and_backoff_respect_custom_backoff(fake_net):
    fake_net["set"](_http_error(429, "4"), _http_error(429), {"ok": 11})
    assert podlib.public_get(GAMMA_URL, backoff_s=0.1) == {"ok": 11}
    assert fake_net["sleeps"] == [4.0, 0.2]


def test_http_429_counts_against_the_shared_attempt_budget(fake_net):
    # 429, 503 and an SSL EOF share PUBLIC_GET_ATTEMPTS: the third failure ends the call.
    fake_net["set"](_http_error(429, "2"), HTTPError(GAMMA_URL, 503, "busy", {}, io.BytesIO(b"")), _ssl_eof(), then={"ok": 0})
    with pytest.raises(podlib.PublicGetError) as info:
        podlib.public_get(GAMMA_URL)
    assert len(fake_net["calls"]) == podlib.PUBLIC_GET_ATTEMPTS == 3
    assert fake_net["sleeps"] == [2.0, podlib.PUBLIC_GET_BACKOFF_S * 2]
    assert isinstance(info.value.__cause__, ssl.SSLEOFError)


def test_http_429_single_attempt_is_not_retried(fake_net):
    fake_net["set"](_http_error(429, "1"), then={"ok": 0})
    with pytest.raises(podlib.PublicGetError):
        podlib.public_get(GAMMA_URL, attempts=1)
    assert len(fake_net["calls"]) == 1
    assert fake_net["sleeps"] == []


def test_http_429_then_other_4xx_raises_at_once(fake_net):
    fake_net["set"](_http_error(429, "1"), _http_error(403), then={"ok": 0})
    with pytest.raises(HTTPError) as info:
        podlib.public_get(GAMMA_URL)
    assert info.value.code == 403
    assert len(fake_net["calls"]) == 2
    assert fake_net["sleeps"] == [1.0]


def test_http_5xx_ignores_retry_after(fake_net):
    # 5xx keeps Task K's exponential backoff; only 429 reads Retry-After.
    fake_net["set"](_http_error(503, "7"), {"ok": 12})
    assert podlib.public_get(GAMMA_URL) == {"ok": 12}
    assert fake_net["sleeps"] == [podlib.PUBLIC_GET_BACKOFF_S]


def test_all_attempts_fail_raises_clear_error(fake_net):
    fake_net["set"](_ssl_eof())
    with pytest.raises(podlib.PublicGetError) as info:
        podlib.public_get(GAMMA_URL)
    assert len(fake_net["calls"]) == podlib.PUBLIC_GET_ATTEMPTS
    assert len(fake_net["sleeps"]) == podlib.PUBLIC_GET_ATTEMPTS - 1
    message = str(info.value)
    assert str(podlib.PUBLIC_GET_ATTEMPTS) in message and "SSLEOFError" in message
    assert isinstance(info.value.__cause__, ssl.SSLEOFError)


def test_refused_urls_are_not_retried(fake_net):
    fake_net["set"]({"ok": 1})
    with pytest.raises(RuntimeError):
        podlib.public_get("https://clob.polymarket.com/order")
    assert fake_net["calls"] == []


def _scanner_exit(tmp_path, fake_net, error):
    before = RISK_PATH.read_bytes()
    fake_net["set"](error)
    markets = tmp_path / "markets.jsonl"
    bus_path = tmp_path / "bus.jsonl"
    code = scanner.main(markets_path=markets, bus_path=bus_path, ts="2026-10-08T10:00:00Z")
    assert RISK_PATH.read_bytes() == before
    return code, markets, bus_path


def test_scanner_exits_3_when_all_attempts_hit_ssl_eof(tmp_path, fake_net, capsys):
    code, markets, bus_path = _scanner_exit(tmp_path, fake_net, URLError(_ssl_eof()))
    assert code == scanner.EXIT_INCOMPLETE == 3
    assert len(fake_net["calls"]) == podlib.PUBLIC_GET_ATTEMPTS
    assert not markets.exists() and not bus_path.exists()
    captured = capsys.readouterr()
    assert "stop=network_error" in captured.out
    assert "pagination incomplete (network_error)" in captured.err


def test_scanner_exits_3_when_all_attempts_hit_urlerror(tmp_path, fake_net):
    code, markets, bus_path = _scanner_exit(tmp_path, fake_net, URLError("connection refused"))
    assert code == scanner.EXIT_INCOMPLETE
    assert not markets.exists() and not bus_path.exists()


def test_scanner_exits_3_when_all_attempts_hit_429(tmp_path, fake_net):
    code, markets, bus_path = _scanner_exit(tmp_path, fake_net, _http_error(429, "1"))
    assert code == scanner.EXIT_INCOMPLETE
    assert len(fake_net["calls"]) == podlib.PUBLIC_GET_ATTEMPTS
    assert fake_net["sleeps"] == [1.0] * (podlib.PUBLIC_GET_ATTEMPTS - 1)
    assert not markets.exists() and not bus_path.exists()


def test_scanner_network_error_mid_walk_is_incomplete():
    pages = {"": ([{"id": str(i)} for i in range(3)], "c1")}

    def getter(url: str):
        if "after_cursor" in url:
            raise podlib.PublicGetError(url, podlib.PUBLIC_GET_ATTEMPTS, ConnectionResetError("reset"))
        return {"markets": pages[""][0], "next_cursor": pages[""][1]}

    rows, meta = scanner.fetch_all_markets(getter)
    assert len(rows) == 3
    assert meta["stop_reason"] == "network_error"
    assert meta["complete"] is False
    assert meta["pages"] == 1
    assert "ConnectionResetError" in meta["error"]