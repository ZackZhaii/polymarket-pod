"""Offline paper-session tests. No network, no fills, no risk-file edits.

Since Task AB the session uses scanner.scan, which needs at least 5 scanned markets, so the
fixture adds four markets the scanner rejects; book age is judged at an injected clock.
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path

import src.paper_session as paper_session
from src import pins
import src.podlib as podlib
from src.podlib import RISK_PATH, read_jsonl


RAW = {
    "id": "m1",
    "question": "Paper session fixture",
    "slug": "paper-session-fixture",
    "conditionId": "cond-1",
    "clobTokenIds": json.dumps(["yes-token", "no-token"]),
    "outcomes": json.dumps(["Yes", "No"]),
    "outcomePrices": json.dumps(["0.40", "0.40"]),
    "liquidityNum": 25000,
    "volumeNum": 1000,
    "endDate": "2026-12-01T00:00:00Z",
    "bestBid": 0.39,
    "bestAsk": 0.41,
    "spread": 0.02,
    "feesEnabled": False,
    "closed": False,
    "acceptingOrders": True,  # Task CR (#232): the scanner rejects a missing / non-bool flag
    "version": "v1",
}

# Rejected by the current scanner path (closed, wide spread, missing version, near expiry):
# they only make up the 5-market floor and must never be persisted or booked.
REJECTED = [
    dict(RAW, id="r-closed", slug="r-closed", closed=True),
    dict(RAW, id="r-wide", slug="r-wide", spread=0.30, bestBid=0.20, bestAsk=0.50),
    {key: value for key, value in dict(RAW, id="r-nover", slug="r-nover").items() if key != "version"},
    dict(RAW, id="r-soon", slug="r-soon", endDate="2026-10-08T12:00:00Z"),
]
CLOCK = "2026-10-08T06:00:00.500Z"


def _book(url: str) -> dict:
    assert url.startswith("https://clob.polymarket.com/book?")
    banned = ("/order", "api-key", "secret", "withdraw")
    assert not any(token in url.lower() for token in banned)
    return {
        "bids": [{"price": "0.30", "size": "10"}],
        "asks": [{"price": "0.40", "size": "12"}],
        "tick_size": "0.01",
        "min_order_size": "5",
        "market": "cond-1",
        # The exchange's own /book time (Unix ms) = the session's now; age is read from this (Task T).
        "timestamp": "1791439200000",
    }


def test_session_records_signals_without_fills(tmp_path):
    before = RISK_PATH.read_bytes()
    markets = tmp_path / "markets.jsonl"
    books = tmp_path / "books.jsonl"
    bus_path = tmp_path / "bus.jsonl"
    sessions = tmp_path / "sessions.jsonl"
    result = paper_session.run(
        raw_markets=[RAW, *REJECTED],
        book_getter=_book,
        markets_path=markets,
        books_path=books,
        bus_path=bus_path,
        sessions_path=sessions,
        now="2026-10-08T06:00:00Z",
        clock=lambda: CLOCK,
    )
    assert result["ok"] is True
    assert result["reason"] == "signals_only"
    assert result["markets"] == 1
    assert result["books"] == 2
    assert result["gaps"] == 1
    assert result["fills"] == 0
    kinds = [row["type"] for row in read_jsonl(bus_path)]
    assert "fill" not in kinds
    assert "signal" in kinds
    session = read_jsonl(sessions)[0]
    assert session["fills"] == 0
    assert session["orders"] == "none"
    assert session["keys"] == "none"
    assert session["live_enabled"] is False
    assert RISK_PATH.read_bytes() == before
    source = inspect.getsource(paper_session)
    assert "load_dotenv" not in source
    assert "os.environ" not in source
    assert "POLY_PRIVATE_KEY" not in source
    assert "polymarket-client" not in source
    assert "withdraw" not in source


def test_pin_drift_does_not_read_the_book(tmp_path):
    before = RISK_PATH.read_bytes()
    cfg = paper_session.load_risk()
    cfg["live_enabled"] = True
    called = {"n": 0}

    def getter(url: str) -> dict:
        called["n"] += 1
        raise AssertionError(url)

    result = paper_session.run(
        raw_markets=[RAW, *REJECTED],
        book_getter=getter,
        markets_path=tmp_path / "markets.jsonl",
        books_path=tmp_path / "books.jsonl",
        bus_path=tmp_path / "bus.jsonl",
        sessions_path=tmp_path / "sessions.jsonl",
        cfg=cfg,
    )
    assert result["ok"] is False
    assert result["reason"] == "phase0_pin"
    assert result["fills"] == 0
    assert called["n"] == 0
    assert not (tmp_path / "sessions.jsonl").exists()
    assert RISK_PATH.read_bytes() == before


# --- Task DH (#233): full risk.toml SHA + RISK_FILE_PINS before start (reuse src.pins) ---

_REAL_RISK = (Path(__file__).resolve().parents[1] / "config" / "risk.toml").read_bytes().replace(b"\r\n", b"\n")


def _risk_file(tmp_path, data: bytes | str, name="risk.toml") -> Path:
    path = tmp_path / name
    path.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
    return path


def _drifted(tmp_path, old: str, new: str, name="drift.toml") -> Path:
    text = _REAL_RISK.decode("utf-8")
    assert text.count(old) == 1
    return _risk_file(tmp_path, text.replace(old, new), name)


def _assert_wrote_nothing(tmp_path) -> None:
    assert not (tmp_path / "markets.jsonl").exists()
    assert not (tmp_path / "books.jsonl").exists()
    assert not (tmp_path / "sessions.jsonl").exists()


def test_pre_start_sha_drift_refuses_and_writes_nothing(tmp_path):
    """A comment-only change is still sha drift: refuse before any public read or write."""
    before = RISK_PATH.read_bytes()
    drifted = _risk_file(tmp_path, _REAL_RISK + b"# note\n", "sha-drift.toml")
    called = {"n": 0}

    def getter(url: str) -> dict:
        called["n"] += 1
        raise AssertionError(url)

    result = paper_session.run(
        raw_markets=[RAW, *REJECTED],
        book_getter=getter,
        markets_path=tmp_path / "markets.jsonl",
        books_path=tmp_path / "books.jsonl",
        bus_path=tmp_path / "bus.jsonl",
        sessions_path=tmp_path / "sessions.jsonl",
        risk_path=drifted,
    )
    assert result["ok"] is False
    assert result["reason"] == "risk_toml_pin_drift"
    assert result["markets"] == 0 and result["books"] == 0 and result["fills"] == 0
    assert called["n"] == 0
    _assert_wrote_nothing(tmp_path)
    assert RISK_PATH.read_bytes() == before


def test_pre_start_changed_pin_refuses_and_writes_nothing(tmp_path):
    before = RISK_PATH.read_bytes()
    drifted = _drifted(tmp_path, "live_enabled = false", "live_enabled = true")
    called = {"n": 0}

    def getter(url: str) -> dict:
        called["n"] += 1
        raise AssertionError(url)

    result = paper_session.run(
        raw_markets=[RAW, *REJECTED],
        book_getter=getter,
        markets_path=tmp_path / "markets.jsonl",
        books_path=tmp_path / "books.jsonl",
        bus_path=tmp_path / "bus.jsonl",
        sessions_path=tmp_path / "sessions.jsonl",
        risk_path=drifted,
    )
    assert result["ok"] is False
    assert result["reason"] == "risk_toml_pin_drift"
    assert called["n"] == 0
    _assert_wrote_nothing(tmp_path)
    assert RISK_PATH.read_bytes() == before


def test_pre_start_missing_pin_refuses_and_writes_nothing(tmp_path):
    before = RISK_PATH.read_bytes()
    drifted = _drifted(tmp_path, "kelly_fraction = 0.15\n", "")
    called = {"n": 0}

    def getter(url: str) -> dict:
        called["n"] += 1
        raise AssertionError(url)

    result = paper_session.run(
        raw_markets=[RAW, *REJECTED],
        book_getter=getter,
        markets_path=tmp_path / "markets.jsonl",
        books_path=tmp_path / "books.jsonl",
        bus_path=tmp_path / "bus.jsonl",
        sessions_path=tmp_path / "sessions.jsonl",
        risk_path=drifted,
    )
    assert result["ok"] is False
    assert result["reason"] == "risk_toml_pin_drift"
    assert called["n"] == 0
    _assert_wrote_nothing(tmp_path)
    assert RISK_PATH.read_bytes() == before


def test_clean_pinned_config_still_starts(tmp_path):
    """Explicit risk_path at the real pinned file still runs the happy path."""
    before = RISK_PATH.read_bytes()
    assert pins.risk_file_problems(RISK_PATH) == []
    result = paper_session.run(
        raw_markets=[RAW, *REJECTED],
        book_getter=_book,
        markets_path=tmp_path / "markets.jsonl",
        books_path=tmp_path / "books.jsonl",
        bus_path=tmp_path / "bus.jsonl",
        sessions_path=tmp_path / "sessions.jsonl",
        now="2026-10-08T06:00:00Z",
        clock=lambda: CLOCK,
        risk_path=RISK_PATH,
    )
    assert result["ok"] is True
    assert result["reason"] == "signals_only"
    assert result["fills"] == 0
    assert (tmp_path / "sessions.jsonl").exists()
    assert RISK_PATH.read_bytes() == before


def test_cfg_none_with_risk_path_does_not_call_default_load_risk(tmp_path, monkeypatch):
    """Pin check and load must share risk_path; default load_risk() must not run."""
    before = RISK_PATH.read_bytes()
    clean = _risk_file(tmp_path, _REAL_RISK, "clean.toml")

    def boom():
        raise AssertionError("load_risk() must not run when risk_path is set and cfg is None")

    monkeypatch.setattr(paper_session, "load_risk", boom)
    result = paper_session.run(
        raw_markets=[RAW, *REJECTED],
        book_getter=_book,
        markets_path=tmp_path / "markets.jsonl",
        books_path=tmp_path / "books.jsonl",
        bus_path=tmp_path / "bus.jsonl",
        sessions_path=tmp_path / "sessions.jsonl",
        now="2026-10-08T06:00:00Z",
        clock=lambda: CLOCK,
        risk_path=clean,
    )
    assert result["ok"] is True
    assert result["reason"] == "signals_only"
    assert RISK_PATH.read_bytes() == before


def test_pin_check_and_load_use_same_path_when_default_drifted(tmp_path, monkeypatch):
    """If default RISK_PATH is drifted but risk_path is clean, load the clean file (not default)."""
    real_before = (Path(__file__).resolve().parents[1] / "config" / "risk.toml").read_bytes()
    clean = _risk_file(tmp_path, _REAL_RISK, "clean.toml")
    drifted = _drifted(tmp_path, "max_spread = 0.04", "max_spread = 0.05", "default-drift.toml")
    monkeypatch.setattr(paper_session, "RISK_PATH", drifted)
    monkeypatch.setattr(podlib, "RISK_PATH", drifted)

    seen: dict = {}
    real_thresholds = paper_session.scanner.thresholds_from

    def spy(cfg):
        seen["max_spread"] = cfg.get("max_spread")
        return real_thresholds(cfg)

    monkeypatch.setattr(paper_session.scanner, "thresholds_from", spy)
    result = paper_session.run(
        raw_markets=[RAW, *REJECTED],
        book_getter=_book,
        markets_path=tmp_path / "markets.jsonl",
        books_path=tmp_path / "books.jsonl",
        bus_path=tmp_path / "bus.jsonl",
        sessions_path=tmp_path / "sessions.jsonl",
        now="2026-10-08T06:00:00Z",
        clock=lambda: CLOCK,
        risk_path=clean,
    )
    assert result["ok"] is True
    assert seen.get("max_spread") == 0.04
    # Committed tree file untouched (monkeypatch only redirects the Path object).
    assert (Path(__file__).resolve().parents[1] / "config" / "risk.toml").read_bytes() == real_before


def test_unreadable_risk_path_refuses_as_drift_not_throw(tmp_path):
    """Missing/unreadable risk_path → structured risk_toml_pin_drift before any scan."""
    before = RISK_PATH.read_bytes()
    missing = tmp_path / "no-such-risk.toml"
    called = {"n": 0}

    def getter(url: str) -> dict:
        called["n"] += 1
        raise AssertionError(url)

    result = paper_session.run(
        raw_markets=[RAW, *REJECTED],
        book_getter=getter,
        markets_path=tmp_path / "markets.jsonl",
        books_path=tmp_path / "books.jsonl",
        bus_path=tmp_path / "bus.jsonl",
        sessions_path=tmp_path / "sessions.jsonl",
        risk_path=missing,
    )
    assert result["ok"] is False
    assert result["reason"] == "risk_toml_pin_drift"
    assert result["markets"] == 0 and result["books"] == 0 and result["fills"] == 0
    assert called["n"] == 0
    _assert_wrote_nothing(tmp_path)
    assert RISK_PATH.read_bytes() == before


def test_unparseable_risk_path_refuses_as_drift_not_throw(tmp_path):
    """Garbage bytes → risk_toml_pin_drift (+ record_drift), not a raw parse exception."""
    before = RISK_PATH.read_bytes()
    bad = _risk_file(tmp_path, b"live_enabled = [[[\n", "bad.toml")
    called = {"n": 0}

    def getter(url: str) -> dict:
        called["n"] += 1
        raise AssertionError(url)

    result = paper_session.run(
        raw_markets=[RAW, *REJECTED],
        book_getter=getter,
        markets_path=tmp_path / "markets.jsonl",
        books_path=tmp_path / "books.jsonl",
        bus_path=tmp_path / "bus.jsonl",
        sessions_path=tmp_path / "sessions.jsonl",
        risk_path=bad,
    )
    assert result["ok"] is False
    assert result["reason"] == "risk_toml_pin_drift"
    assert called["n"] == 0
    _assert_wrote_nothing(tmp_path)
    assert RISK_PATH.read_bytes() == before
