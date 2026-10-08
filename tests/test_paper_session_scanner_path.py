"""Task AB: paper_session uses the current scanner path and the L/R/T book-time rules. Offline."""
from __future__ import annotations

import inspect
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest

import src.paper_session as paper_session
import src.scanner as scanner
from src.podlib import RISK_PATH, read_jsonl
from tests.test_paper_session import RAW, REJECTED

SESSION = "2026-10-08T06:00:00Z"
BOOK_MS = "1791439200000"  # 2026-10-08T06:00:00.000Z


def _paths(tmp_path):
    return {
        "markets_path": tmp_path / "markets.jsonl",
        "books_path": tmp_path / "books.jsonl",
        "bus_path": tmp_path / "bus.jsonl",
        "sessions_path": tmp_path / "sessions.jsonl",
    }


class Books:
    """Fake public /book. timestamps maps token_id -> /book timestamp (absent key = default)."""

    def __init__(self, timestamps=None, default=BOOK_MS):
        self.timestamps = timestamps or {}
        self.default = default
        self.tokens: list[str] = []

    def __call__(self, url: str) -> dict:
        assert url.startswith("https://clob.polymarket.com/book?")
        token = parse_qs(urlparse(url).query)["token_id"][0]
        self.tokens.append(token)
        book = {"bids": [{"price": "0.30", "size": "10"}], "asks": [{"price": "0.40", "size": "12"}],
                "tick_size": "0.01", "min_order_size": "5", "market": "cond"}
        stamp = self.timestamps.get(token, self.default)
        if stamp is not None:
            book["timestamp"] = stamp
        return book


def _run(tmp_path, *, raw=None, books=None, clock=lambda: "2026-10-08T06:00:00.500Z", **kwargs):
    return paper_session.run(raw_markets=[RAW, *REJECTED] if raw is None else raw,
                             book_getter=books or Books(), now=SESSION, clock=clock, **_paths(tmp_path), **kwargs)


def _kinds(tmp_path):
    return [row["type"] for row in read_jsonl(tmp_path / "bus.jsonl")]


def test_session_no_longer_uses_the_legacy_unfiltered_path():
    source = inspect.getsource(paper_session)
    assert "fetch_markets" not in source
    assert "visible_rows" not in source
    assert "scanner.scan(" in source
    assert "scanner.thresholds_from(" in source


def test_only_markets_that_pass_the_scanner_are_persisted_and_booked(tmp_path):
    books = Books()
    result = _run(tmp_path, books=books)
    assert (result["ok"], result["markets"], result["books"], result["scanned"]) == (True, 1, 2, 5)
    assert [row["id"] for row in read_jsonl(tmp_path / "markets.jsonl")] == ["m1"]
    assert books.tokens == ["yes-token", "no-token"]
    scanner_signals = [row for row in read_jsonl(tmp_path / "bus.jsonl") if row["from"] == "Scanner"]
    assert [row["market"] for row in scanner_signals] == ["m1"]


@pytest.mark.parametrize(
    "bad",
    [
        {"spread": "NaN"},
        {"liquidityNum": "inf"},
        {"bestAsk": "-Infinity"},
        {"outcomePrices": json.dumps(["NaN", "0.6"])},
    ],
)
def test_non_finite_markets_are_rejected_and_never_booked(tmp_path, bad):
    books = Books()
    raw = [dict(RAW, **bad), *REJECTED]
    result = _run(tmp_path, raw=raw, books=books)
    assert (result["ok"], result["reason"]) == (False, "no_markets")
    assert books.tokens == []
    assert not (tmp_path / "markets.jsonl").exists()
    assert not (tmp_path / "bus.jsonl").exists()


def test_v2_markets_book_their_position_ids(tmp_path):
    books = Books()
    v2 = dict(RAW, id="m2", slug="m2", version="v2", positionIds=json.dumps(["111", "222"]),
              clobTokenIds=json.dumps(["wrong-a", "wrong-b"]))
    result = _run(tmp_path, raw=[v2, *REJECTED], books=books)
    assert result["ok"] is True and result["books"] == 2
    assert books.tokens == ["111", "222"]


def test_unknown_version_is_rejected(tmp_path):
    books = Books()
    result = _run(tmp_path, raw=[dict(RAW, version="v3"), *REJECTED], books=books)
    assert result["reason"] == "no_markets" and books.tokens == []


def test_too_few_scanned_markets_write_nothing(tmp_path):
    books = Books()
    result = _run(tmp_path, raw=[RAW, *REJECTED[:3]], books=books)
    assert (result["ok"], result["reason"], result["markets"]) == (False, "too_few_markets", 0)
    assert books.tokens == []
    assert not (tmp_path / "markets.jsonl").exists()


def test_incomplete_paging_writes_nothing(tmp_path):
    books = Books()

    def pages(url: str):
        # Every page returns the same cursor and the same rows: the walk stalls (incomplete).
        return {"markets": [RAW, *REJECTED], "next_cursor": "again"}

    result = paper_session.run(market_getter=pages, book_getter=books, now=SESSION,
                               clock=lambda: "2026-10-08T06:00:00.500Z", **_paths(tmp_path))
    assert (result["ok"], result["reason"]) == (False, "markets_incomplete")
    assert books.tokens == []
    assert not (tmp_path / "markets.jsonl").exists()
    assert not (tmp_path / "bus.jsonl").exists()


def test_complete_keyset_fetch_uses_the_risk_liquidity_prefilter(tmp_path):
    urls = []

    def pages(url: str):
        urls.append(url)
        return {"markets": [RAW, *REJECTED], "next_cursor": ""}

    result = paper_session.run(market_getter=pages, book_getter=Books(), now=SESSION,
                               clock=lambda: "2026-10-08T06:00:00.500Z", **_paths(tmp_path))
    assert result["ok"] is True
    assert len(urls) == 1 and urls[0].startswith(scanner.GAMMA_KEYSET)
    assert "liquidity_num_min=10000" in urls[0]


def test_a_failing_market_read_writes_nothing(tmp_path):
    def broken(url: str):
        raise RuntimeError("boom")

    result = paper_session.run(market_getter=broken, book_getter=Books(), now=SESSION, **_paths(tmp_path))
    assert result["reason"] in ("public_read_failed", "markets_incomplete")
    assert not (tmp_path / "markets.jsonl").exists()


@pytest.mark.parametrize("patch", [{"max_spread": float("nan")}, {"min_liquidity_usd": True}, {"max_spread": None}])
def test_bad_scanner_threshold_fails_closed_before_any_read(tmp_path, patch):
    cfg = paper_session.load_risk()
    cfg.update(patch)
    called = []
    result = paper_session.run(market_getter=lambda url: called.append(url), book_getter=Books(), cfg=cfg,
                               now=SESSION, **_paths(tmp_path))
    assert (result["ok"], result["reason"]) == (False, "invalid_scanner_threshold")
    assert called == []


def test_missing_threshold_key_fails_closed(tmp_path):
    cfg = paper_session.load_risk()
    del cfg["max_spread"]
    result = _run(tmp_path, cfg=cfg)
    assert result["reason"] == "invalid_scanner_threshold"


def test_book_age_is_judged_after_the_books_are_read(tmp_path):
    """The session stamp is taken before the reads; the book is newer than it. Old code judged at the
    session stamp (age about 0 s, or a future book); the evaluation clock is read after recording."""
    books = Books()
    seen = []

    def clock():
        seen.append(len(books.tokens))
        return "2026-10-08T06:00:01.250Z"

    result = paper_session.run(raw_markets=[RAW, *REJECTED], book_getter=books, now="2026-10-08T05:59:59Z",
                               clock=clock, **_paths(tmp_path))
    assert seen == [2]  # read once, after both books
    assert (result["gaps"], result["stale"]) == (1, 0)
    assert result["evaluated_at"] == "2026-10-08T06:00:01.250Z"
    session = read_jsonl(tmp_path / "sessions.jsonl")[0]
    assert session["ts"] == "2026-10-08T05:59:59Z" and session["evaluated_at"] == "2026-10-08T06:00:01.250Z"
    rows = read_jsonl(tmp_path / "books.jsonl")
    assert {row["ts"] for row in rows} == {"2026-10-08T05:59:59Z"}
    assert {row["book_ts"] for row in rows} == {"2026-10-08T06:00:00.000Z"}


def test_an_old_book_is_stale_even_when_just_recorded(tmp_path):
    result = _run(tmp_path, clock=lambda: "2026-10-08T06:02:00.001Z")  # 120.001 s after the CLOB stamp
    assert (result["gaps"], result["stale"]) == (0, 1)  # stale counts markets
    assert "fill" not in _kinds(tmp_path)


def test_exactly_120_s_is_fresh(tmp_path):
    result = _run(tmp_path, clock=lambda: "2026-10-08T06:02:00.000Z")
    assert (result["gaps"], result["stale"]) == (1, 0)


def test_the_recorder_ts_is_never_used_for_age(tmp_path):
    """No CLOB timestamp: the row's recorder ts equals the session stamp, but the book is still stale."""
    result = _run(tmp_path, books=Books(default=None), clock=lambda: SESSION)
    assert (result["gaps"], result["stale"]) == (0, 1)  # stale counts markets
    assert {row["book_ts_problem"] for row in read_jsonl(tmp_path / "books.jsonl")} == {"missing_book_ts"}


def test_a_future_book_is_stale(tmp_path):
    result = _run(tmp_path, clock=lambda: "2026-10-08T05:59:59.999Z")  # 1 ms before the CLOB stamp
    assert (result["gaps"], result["stale"]) == (0, 1)  # stale counts markets


@pytest.mark.parametrize("stamp", ["not-a-time", "1791439200000.5", "-5", "2026-10-08T06:00:00"])
def test_an_unparseable_book_timestamp_is_stale(tmp_path, stamp):
    result = _run(tmp_path, books=Books({"yes-token": stamp}))
    assert result["gaps"] == 0 and result["stale"] >= 1


@pytest.mark.parametrize("bad_clock", ["", "garbage", "2026-10-08T06:00:01", "nan"])
def test_a_broken_clock_makes_every_book_stale(tmp_path, bad_clock):
    result = _run(tmp_path, clock=lambda: bad_clock)
    assert result["gaps"] == 0
    assert "fill" not in _kinds(tmp_path)


def test_default_clock_is_utc_with_milliseconds():
    stamp = paper_session.eval_clock()
    parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    assert stamp.endswith("Z") and len(stamp) == len("2026-10-08T06:00:00.000Z")
    assert abs(datetime.now(timezone.utc) - parsed) < timedelta(seconds=5)


def test_risk_file_unchanged_and_no_fills(tmp_path):
    before = RISK_PATH.read_bytes()
    _run(tmp_path)
    assert RISK_PATH.read_bytes() == before
    assert "fill" not in _kinds(tmp_path)
