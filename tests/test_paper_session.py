"""Offline paper-session tests. No network, no fills, no risk-file edits.

Since Task AB the session uses scanner.scan, which needs at least 5 scanned markets, so the
fixture adds four markets the scanner rejects; book age is judged at an injected clock.
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

import src.paper_cycle as paper_cycle
import src.paper_session as paper_session
from src import pins
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


# --- Issue #75 criterion 6: the cycle's input refusal propagates to the session ---

_BROKEN = b"{not json}\n"


@pytest.mark.parametrize("which", ["markets", "books"])
def test_a_corrupt_cycle_input_propagates_and_writes_no_sessions_record(tmp_path, which):
    """A bad line in the session's own markets/books file: ok=False reason=input_invalid, no sessions row.

    The caller propagation of the cycle's refusal (path, line and problem code only); the damaged
    source file is reported, never repaired or truncated.
    """
    before = RISK_PATH.read_bytes()
    markets = tmp_path / "markets.jsonl"
    books = tmp_path / "books.jsonl"
    sessions = tmp_path / "sessions.jsonl"
    target = books if which == "books" else markets
    target.write_bytes(_BROKEN)

    result = paper_session.run(
        raw_markets=[RAW, *REJECTED],
        book_getter=_book,
        markets_path=markets,
        books_path=books,
        bus_path=tmp_path / "bus.jsonl",
        sessions_path=sessions,
        now="2026-10-08T06:00:00Z",
        clock=lambda: CLOCK,
    )

    assert result["ok"] is False
    assert result["reason"] == paper_cycle.INPUT_INVALID
    assert (result["gaps"], result["stale"], result["fills"]) == (0, 0, 0)
    assert result["input_problems"] == [{"path": str(target), "line": 1, "problem": "bad_json"}]
    assert not sessions.exists()  # a refused round never writes a success sessions record
    assert _BROKEN in target.read_bytes()
    assert RISK_PATH.read_bytes() == before


def test_a_still_changing_cycle_input_propagates_as_pending_and_writes_no_sessions_record(tmp_path, monkeypatch):
    """A markets/books file still changing after the bounded re-read: ok=False reason=input_pending.

    A transient append is a skip, not a halt: zero gaps and fills, no sessions record. The one
    pending-round record is stubbed so the test never touches the real sentinel state.
    """
    before = RISK_PATH.read_bytes()
    books = tmp_path / "books.jsonl"
    sessions = tmp_path / "sessions.jsonl"
    real = paper_cycle._file_signature
    state = {"looks": 0}

    def signature(path):
        base = real(path)
        if Path(path) != books or base is None:
            return base
        state["looks"] += 1
        return (base[0] + state["looks"], base[1] + state["looks"])  # every look sees a new append

    monkeypatch.setattr(paper_cycle, "_file_signature", signature)
    monkeypatch.setattr(
        paper_cycle, "_record_input_pending",
        lambda *args, **kwargs: {"state_problem": None, "write_problem": None,
                                 "input_pending_streak": 1, "input_pending_halt": False})

    result = paper_session.run(
        raw_markets=[RAW, *REJECTED],
        book_getter=_book,
        markets_path=tmp_path / "markets.jsonl",
        books_path=books,
        bus_path=tmp_path / "bus.jsonl",
        sessions_path=sessions,
        now="2026-10-08T06:00:00Z",
        clock=lambda: CLOCK,
    )

    assert result["ok"] is False
    assert result["reason"] == paper_cycle.INPUT_PENDING
    assert (result["gaps"], result["stale"], result["fills"]) == (0, 0, 0)
    assert result["input_problems"] == [{"path": str(books), "line": None, "problem": "changing"}]
    assert not sessions.exists()
    assert RISK_PATH.read_bytes() == before
