"""Issue #128 (Task BM, the precision part only): paper_cycle's default clock has millisecond precision.

Before: podlib.utc_now() is cut to whole seconds, so with now=12:59:04Z a book stamped 12:59:04.634Z was
634 ms "in the future" and read as future_book_ts (stale); two cycles in a row would halt a live market.
Now paper_cycle reads podlib.utc_now_ms(). The future check itself is unchanged: no tolerance is added
here (FUTURE_BOOK_TS_TOLERANCE_S stays pending M2 approval), and the 120 s boundary is unchanged.
Offline: a frozen podlib.datetime, local files under tmp_path.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

import pytest

import src.paper_cycle as paper_cycle
from src import podlib
from src.book_staleness import FUTURE_BOOK_TS, STALE_BOOK, book_age_problem
from src.podlib import RISK_PATH, read_jsonl
from src.thresholds import MAX_BOOK_AGE_S

REAL_NOW = datetime(2026, 10, 10, 12, 59, 4, 634_500, tzinfo=timezone.utc)  # the issue's repro second
MARKET = "m-128"


def _freeze(monkeypatch, moment: datetime) -> None:
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: D102 - same signature as datetime.now
            return moment.astimezone(tz) if tz is not None else moment.replace(tzinfo=None)

    monkeypatch.setattr(podlib, "datetime", Frozen)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _write(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _cycle(tmp_path, book_ts: str) -> dict:
    """One paper_cycle run with now=None (the default clock) over a single market whose two legs carry
    ``book_ts``; returns the BookWatcher stale payload reasons (empty when fresh) and the result."""
    markets, books, bus = tmp_path / "markets.jsonl", tmp_path / "books.jsonl", tmp_path / "bus.jsonl"
    _write(markets, [{"ts": "2026-10-10T12:58:00Z", "id": MARKET, "token_ids": ["y", "n"]}])
    _write(books, [{"ts": book_ts, "book_ts": book_ts, "market_id": MARKET, "token_id": token, "best_ask": 0.6,
                    "best_ask_size": 10} for token in ("y", "n")])
    before = RISK_PATH.read_bytes()
    result = paper_cycle.run(markets_path=markets, books_path=books, bus_path=bus,
                             sentinel_state_path=tmp_path / "sentinel_stale.json",
                             quotes_path=tmp_path / "quotes.jsonl")
    assert RISK_PATH.read_bytes() == before
    stale = [row["payload"] for row in read_jsonl(bus) if row.get("from") == "BookWatcher"]
    return {"result": result, "stale": stale}


# --- the clock ----------------------------------------------------------------------------------


def test_utc_now_ms_format_and_truncation(monkeypatch):
    _freeze(monkeypatch, REAL_NOW)
    stamp = podlib.utc_now_ms()
    assert stamp == "2026-10-10T12:59:04.634Z"  # truncated, never rounded up past the real clock
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", stamp)
    assert podlib.utc_now() == "2026-10-10T12:59:04Z"  # the seconds stamp for rows and logs is unchanged


def test_utc_now_ms_on_the_whole_second(monkeypatch):
    _freeze(monkeypatch, datetime(2026, 10, 10, 0, 0, 0, tzinfo=timezone.utc))
    assert podlib.utc_now_ms() == "2026-10-10T00:00:00.000Z"


def test_the_real_clock_is_never_behind_utc_now_ms():
    before = datetime.now(timezone.utc).replace(microsecond=datetime.now(timezone.utc).microsecond // 1000 * 1000)
    stamp = datetime.fromisoformat(podlib.utc_now_ms().replace("Z", "+00:00"))
    after = datetime.now(timezone.utc)
    assert stamp <= after and stamp >= before - timedelta(seconds=1)


# --- paper_cycle's default clock --------------------------------------------------------------------


def test_the_issue_repro_a_book_from_this_second_is_fresh(tmp_path, monkeypatch):
    _freeze(monkeypatch, REAL_NOW)
    out = _cycle(tmp_path, "2026-10-10T12:59:04.634Z")
    assert out["result"]["ok"] is True and out["result"]["stale"] == 0 and out["stale"] == []
    # the old seconds clock reads the same book as from the future (the bug)
    old_now = datetime.fromisoformat(podlib.utc_now().replace("Z", "+00:00"))
    assert book_age_problem("2026-10-10T12:59:04.634Z", old_now, MAX_BOOK_AGE_S) == FUTURE_BOOK_TS


@pytest.mark.parametrize("age_s", [0.05, 0.0, 1.0, float(MAX_BOOK_AGE_S)], ids=["50ms", "same-ms", "1s", "120s"])
def test_fresh_ages_with_the_default_clock(tmp_path, monkeypatch, age_s):
    _freeze(monkeypatch, REAL_NOW)
    clock = REAL_NOW.replace(microsecond=REAL_NOW.microsecond // 1000 * 1000)
    out = _cycle(tmp_path, _iso(clock - timedelta(seconds=age_s)))
    assert out["result"]["stale"] == 0 and out["stale"] == []


def test_one_millisecond_past_the_boundary_is_stale(tmp_path, monkeypatch):
    _freeze(monkeypatch, REAL_NOW)
    clock = REAL_NOW.replace(microsecond=REAL_NOW.microsecond // 1000 * 1000)
    out = _cycle(tmp_path, _iso(clock - timedelta(seconds=MAX_BOOK_AGE_S + 0.001)))
    assert out["result"]["stale"] == 1 and out["stale"][0]["stale_reasons"] == [STALE_BOOK]


@pytest.mark.parametrize("ahead_s", [0.001, 0.4, 0.6])
def test_a_book_ahead_of_the_clock_is_still_future_no_tolerance(tmp_path, monkeypatch, ahead_s):
    """No FUTURE_BOOK_TS_TOLERANCE_S here (pending M2 approval): any book_ts later than the ms clock stays stale."""
    _freeze(monkeypatch, REAL_NOW)
    clock = REAL_NOW.replace(microsecond=REAL_NOW.microsecond // 1000 * 1000)
    out = _cycle(tmp_path, _iso(clock + timedelta(seconds=ahead_s)))
    assert out["result"]["stale"] == 1 and out["stale"][0]["stale_reasons"] == [FUTURE_BOOK_TS]


def test_an_explicit_now_is_still_used_as_given(tmp_path, monkeypatch):
    _freeze(monkeypatch, REAL_NOW)
    markets, books, bus = tmp_path / "markets.jsonl", tmp_path / "books.jsonl", tmp_path / "bus.jsonl"
    _write(markets, [{"ts": "2026-10-10T12:58:00Z", "id": MARKET, "token_ids": ["y", "n"]}])
    _write(books, [{"ts": "x", "book_ts": "2026-10-10T12:59:04.634Z", "market_id": MARKET, "token_id": t,
                    "best_ask": 0.6, "best_ask_size": 10} for t in ("y", "n")])
    result = paper_cycle.run(markets_path=markets, books_path=books, bus_path=bus, now="2026-10-10T12:59:04Z",
                             sentinel_state_path=tmp_path / "s.json", quotes_path=tmp_path / "q.jsonl")
    assert result["stale"] == 1  # a caller's own seconds clock is not rewritten


def test_paper_cycle_no_longer_reads_the_seconds_clock():
    assert not hasattr(paper_cycle, "utc_now") and paper_cycle.utc_now_ms is podlib.utc_now_ms
