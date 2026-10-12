"""Issue #125 (Task BN): maker.quote fails closed without a usable book for the quoted leg.

BN stops halting unrecorded, unheld markets for their missing book under --recording-dir; those halts were
the only thing that stopped the Maker quoting them. Now the Maker checks the leg's latest recorded book itself
(book_staleness.book_age_problem at MAX_BOOK_AGE_S, the RiskGate / Sentinel rule; no new threshold) and, with
``recording_dir``, the run's manifest scope (read from the manifest the run already writes; nothing new is
persisted). Every refusal raises QuoteRefused and writes nothing. Offline: tmp_path files only.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import src.halt_store as halt_store
import src.maker as maker
import src.paper_cycle as paper_cycle
from src import sentinel
from src.book_staleness import FUTURE_BOOK_TS, INVALID_BOOK_TS, MISSING_BOOK_TS, STALE_BOOK
from src.podlib import read_jsonl
from src.thresholds import MAX_BOOK_AGE_S


def _stamp(delta_s: float = 0.0) -> str:
    moment = datetime.now(timezone.utc) + timedelta(seconds=delta_s)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _rows(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def _book(market="m1", token="t", age_s=1.0, ts=None, **extra):
    return {"ts": ts or _stamp(-age_s), "book_ts": _stamp(-age_s), "market_id": market, "token_id": token,
            "best_ask": 0.41, **extra}


def _quote(tmp_path, market="m1", token="t", **kw):
    return maker.quote(market=market, token_id=token, side="buy", price=0.40, size=5,
                       bus_path=tmp_path / "bus.jsonl", quotes_path=tmp_path / "quotes.jsonl", **kw)


def _refused(tmp_path, reason, **kw):
    with pytest.raises(maker.QuoteRefused) as info:
        _quote(tmp_path, **kw)
    assert info.value.reason == reason and reason in str(info.value)
    assert read_jsonl(tmp_path / "quotes.jsonl") == [] and not (tmp_path / "bus.jsonl").exists()


# --- the book check (default books file) ---------------------------------------------------------------


def test_a_fresh_book_quotes(tmp_path):
    _rows(maker.BOOKS_PATH, [_book()])
    assert _quote(tmp_path)["payload"]["status"] == "open"


def test_no_book_refuses(tmp_path):
    _refused(tmp_path, maker.MISSING_BOOK)


def test_another_legs_book_does_not_count(tmp_path):
    _rows(maker.BOOKS_PATH, [_book(token="other"), _book(market="m2")])
    _refused(tmp_path, maker.MISSING_BOOK)


def test_a_stale_book_refuses(tmp_path):
    _rows(maker.BOOKS_PATH, [_book(age_s=MAX_BOOK_AGE_S + 5)])
    _refused(tmp_path, STALE_BOOK)


def test_a_future_book_refuses(tmp_path):
    _rows(maker.BOOKS_PATH, [_book(age_s=-5.0)])
    _refused(tmp_path, FUTURE_BOOK_TS)


@pytest.mark.parametrize("recorded, reason", [(MISSING_BOOK_TS, MISSING_BOOK_TS), (INVALID_BOOK_TS, INVALID_BOOK_TS),
                                              ("something_else", INVALID_BOOK_TS)])
def test_a_recorded_book_ts_problem_refuses(tmp_path, recorded, reason):
    _rows(maker.BOOKS_PATH, [_book(book_ts_problem=recorded)])
    _refused(tmp_path, reason)


@pytest.mark.parametrize("book_ts", [None, "", "not a time", "2026-10-10"])
def test_an_unusable_book_ts_refuses(tmp_path, book_ts):
    row = _book()
    row["book_ts"] = book_ts
    _rows(maker.BOOKS_PATH, [row])
    with pytest.raises(maker.QuoteRefused):
        _quote(tmp_path)


def test_the_latest_recording_wins(tmp_path):
    # an older recording of a fresh book, then a newer recording whose book is stale: refused
    _rows(maker.BOOKS_PATH, [_book(ts=_stamp(-10)), {**_book(age_s=MAX_BOOK_AGE_S + 5), "ts": _stamp(-1)}])
    _refused(tmp_path, STALE_BOOK)


def test_an_explicit_books_path(tmp_path):
    books = tmp_path / "books.jsonl"
    _rows(books, [_book()])
    assert _quote(tmp_path, books_path=books)["payload"]["status"] == "open"


def test_a_halt_is_still_reported_first(tmp_path):
    sentinel.halt(reason="drill", market="m1", bus_path=tmp_path / "hb.jsonl", quotes_path=tmp_path / "q.jsonl")
    with pytest.raises(maker.HaltActive):
        _quote(tmp_path)  # no book either: the halt is the reason given


# --- the recording scope --------------------------------------------------------------------------------


def _recording(tmp_path, manifest_markets=("m1",), books=()):
    run = tmp_path / "run"
    _rows(run / "books" / "books-000001.jsonl", list(books))
    (run / "manifest.json").write_text(json.dumps({"schema": "m1_run/1", "assets": [
        {"token_id": "t", "market_id": m} for m in manifest_markets]}), encoding="utf-8")
    return run


def test_a_recorded_market_with_a_fresh_recorded_book_quotes(tmp_path):
    run = _recording(tmp_path, books=[_book()])
    assert _quote(tmp_path, recording_dir=run)["payload"]["status"] == "open"


def test_a_market_outside_the_manifest_is_not_recorded_even_with_a_fresh_book(tmp_path):
    run = _recording(tmp_path, books=[_book(market="x1")])
    _rows(maker.BOOKS_PATH, [_book(market="x1")])  # the default books file is not consulted with a recording
    _refused(tmp_path, paper_cycle.NOT_RECORDED, market="x1", recording_dir=run)


def test_a_recorded_market_without_a_recorded_book_refuses(tmp_path):
    run = _recording(tmp_path, books=[_book(market="m1", token="other")])
    _refused(tmp_path, maker.MISSING_BOOK, recording_dir=run)


def test_a_recording_with_no_book_file_refuses(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "manifest.json").write_text(json.dumps({"schema": "m1_run/1", "assets": [
        {"token_id": "t", "market_id": "m1"}]}), encoding="utf-8")
    _refused(tmp_path, paper_cycle.RECORDING_EMPTY, recording_dir=run)


@pytest.mark.parametrize("manifest, reason", [(None, paper_cycle.MANIFEST_MISSING), ("{bad", paper_cycle.MANIFEST_INVALID),
                                              ({"schema": "m1_run/1", "assets": []}, paper_cycle.MANIFEST_NO_MARKETS)])
def test_a_bad_manifest_refuses_every_quote(tmp_path, manifest, reason):
    run = _recording(tmp_path, books=[_book()])
    if manifest is None:
        (run / "manifest.json").unlink()
    else:
        (run / "manifest.json").write_text(manifest if isinstance(manifest, str) else json.dumps(manifest),
                                           encoding="utf-8")
    _refused(tmp_path, reason, recording_dir=run)


# --- the issue's case end to end -------------------------------------------------------------------------


def test_an_unrecorded_unheld_market_is_not_halted_and_the_maker_refuses_it(tmp_path):
    now = _stamp()
    markets = tmp_path / "markets.jsonl"
    _rows(markets, [{"ts": now, "id": m, "token_ids": [f"{m}-y", f"{m}-n"]} for m in ("m1", "x1")])
    run = tmp_path / "run"
    _rows(run / "books" / "books-000001.jsonl", [_book(market="m1", token=f"m1-{s}", age_s=0.5) for s in "yn"])
    (run / "manifest.json").write_text(json.dumps({"schema": "m1_run/1", "assets": [
        {"token_id": f"m1-{s}", "market_id": "m1"} for s in "yn"]}), encoding="utf-8")
    for _ in range(3):
        result = paper_cycle.run(markets_path=markets, books_path=tmp_path / "unused.jsonl",
                                 bus_path=tmp_path / "cycle-bus.jsonl", sentinel_state_path=tmp_path / "s.json",
                                 quotes_path=tmp_path / "quotes.jsonl", recording_dir=run,
                                 paper_path=tmp_path / "paper.jsonl")
    assert result["not_recorded"] == 1
    assert halt_store.halt_problems("x1") == []  # not halted ...
    _refused(tmp_path, paper_cycle.NOT_RECORDED, market="x1", token="x1-y", recording_dir=run)  # ... yet never quoted
    _refused(tmp_path, maker.MISSING_BOOK, market="x1", token="x1-y")  # nor through the default books file
    assert _quote(tmp_path, market="m1", token="m1-y", recording_dir=run)["payload"]["status"] == "open"
