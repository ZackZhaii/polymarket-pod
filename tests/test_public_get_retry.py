"""Offline tests for public_get retry/backoff (Task K). No network: urlopen is mocked, sleep is recorded."""
from __future__ import annotations

import io
import json
import socket
import ssl
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


def test_retry_constants_are_small():
    assert 2 <= podlib.PUBLIC_GET_ATTEMPTS <= 5
    assert 0 < podlib.PUBLIC_GET_BACKOFF_S <= 2


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


@pytest.mark.parametrize("code", [400, 404, 422, 429])
def test_http_4xx_is_not_retried(fake_net, code):
    fake_net["set"](HTTPError(GAMMA_URL, code, "client error", {}, io.BytesIO(b"")), then={"ok": 0})
    with pytest.raises(HTTPError) as info:
        podlib.public_get(GAMMA_URL)
    assert info.value.code == code
    assert len(fake_net["calls"]) == 1
    assert fake_net["sleeps"] == []


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