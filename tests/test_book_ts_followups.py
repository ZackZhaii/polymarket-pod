"""Task T: Task R follow-ups (all tightening). Offline. No network, no .env, no order SDK.

1) paper_cycle and paper_ledger.realistic_no_edge judge book age by the exchange's book_ts
   (book.book_timestamp; book_ts / book_ts_problem on rows), never the recorder's ts.
   Missing or invalid book_ts is stale. 120 s unchanged; exactly 120 s is fresh.
2) paper_cycle emits no gap signal for a stale market; it writes a skip record instead.
3) "Latest row" compares parsed times, not strings; unparseable rows are skipped with a reason.
4) Historical "Forecaster" mentions in docs stay as they are.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import src.paper_cycle as paper_cycle
import src.paper_ledger as paper_ledger
from src.book_staleness import MAX_BOOK_AGE_S, latest_by_time
from src.podlib import RISK_PATH, ROOT, load_risk, read_jsonl

NOW = datetime(2026, 10, 8, 0, 10, 0, tzinfo=timezone.utc)
NOW_S = "2026-10-08T00:10:00Z"


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _ago(seconds: float) -> str:
    return _iso(NOW - timedelta(seconds=seconds))


def _write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _run(tmp_path: Path, markets: list[dict], books: list[dict]) -> tuple[dict, list[dict]]:
    _write(tmp_path / "markets.jsonl", markets)
    _write(tmp_path / "books.jsonl", books)
    bus_path = tmp_path / "bus.jsonl"
    result = paper_cycle.run(markets_path=tmp_path / "markets.jsonl", books_path=tmp_path / "books.jsonl",
                             bus_path=bus_path, now=NOW_S)
    return result, read_jsonl(bus_path) if bus_path.exists() else []


def _pair(market: str, *, ts: object = NOW_S, asks=(0.60, 0.50), **book_fields: object) -> list[dict]:
    rows = []
    for token, ask in zip(("y", "n"), asks):
        row = {"ts": ts, "market_id": market, "token_id": token, "best_ask": ask, "best_ask_size": 5}
        row.update(book_fields)
        rows.append(row)
    return rows


def _market(market: str, tokens=("y", "n"), ts: object = NOW_S) -> dict:
    return {"ts": ts, "id": market, "token_ids": list(tokens)}


def _stale(messages: list[dict]) -> list[dict]:
    return [row for row in messages if row["from"] == "BookWatcher" and row["payload"].get("stale")]


# --- 1) book age comes from book_ts, never the recorder ts ------------------------------------


def test_fresh_recorder_ts_does_not_hide_a_stale_book_ts(tmp_path):
    result, messages = _run(tmp_path, [_market("m1")], _pair("m1", ts=NOW_S, book_ts=_ago(121)))
    assert result["stale"] == 1
    assert _stale(messages)[0]["payload"]["stale_reasons"] == ["stale_book"]


def test_old_recorder_ts_with_fresh_book_ts_is_fresh(tmp_path):
    result, messages = _run(tmp_path, [_market("m1")], _pair("m1", ts=_ago(3600), book_ts=_ago(30)))
    assert result["stale"] == 0 and _stale(messages) == []


def test_boundary_is_unchanged_on_book_ts(tmp_path):
    assert paper_cycle.STALE_AFTER_S == 120 == MAX_BOOK_AGE_S
    fresh, _ = _run(tmp_path / "a", [_market("m1")], _pair("m1", book_ts=_ago(120)))
    stale, _ = _run(tmp_path / "b", [_market("m1")], _pair("m1", book_ts=_ago(121)))
    assert fresh["stale"] == 0 and stale["stale"] == 1


def test_row_without_book_ts_is_stale_even_with_a_fresh_recorder_ts(tmp_path):
    result, messages = _run(tmp_path, [_market("m1")], _pair("m1", ts=NOW_S))
    assert result["stale"] == 1
    assert _stale(messages)[0]["payload"]["stale_reasons"] == ["missing_book_ts"]


@pytest.mark.parametrize("problem", ["missing_book_ts", "invalid_book_ts"])
def test_null_book_ts_carries_its_recorded_problem(tmp_path, problem):
    result, messages = _run(tmp_path, [_market("m1")], _pair("m1", book_ts=None, book_ts_problem=problem))
    assert result["stale"] == 1
    assert _stale(messages)[0]["payload"]["stale_reasons"] == [problem]


@pytest.mark.parametrize("problem", ["invalid_book_ts", "something_else", ""])
def test_a_recorded_problem_wins_over_a_fresh_looking_book_ts(tmp_path, problem):
    result, messages = _run(tmp_path, [_market("m1")], _pair("m1", book_ts=_ago(5), book_ts_problem=problem))
    assert result["stale"] == 1
    assert _stale(messages)[0]["payload"]["stale_reasons"] == ["invalid_book_ts"]


@pytest.mark.parametrize("value", ["garbage", "2026-10-08T00:09:30", "1791488993", True])
def test_unparseable_book_ts_is_stale(tmp_path, value):
    result, messages = _run(tmp_path, [_market("m1")], _pair("m1", book_ts=value))
    assert _stale(messages)[0]["payload"]["stale_reasons"] == ["invalid_book_ts"]


def _ledger_env(tmp_path, monkeypatch, books: list[dict], markets: list[dict]) -> list:
    _write(tmp_path / "books.jsonl", books)
    _write(tmp_path / "markets.jsonl", markets)
    monkeypatch.setattr(paper_ledger, "BOOKS_PATH", tmp_path / "books.jsonl")
    monkeypatch.setattr(paper_ledger, "MARKETS_PATH", tmp_path / "markets.jsonl")
    seen: list = []

    def fake_commit(intent, cfg):
        seen.append(intent)
        return {"allow": False, "decision": {"reasons": ["captured"]}}

    monkeypatch.setattr(paper_ledger, "commit_if_allowed", fake_commit)
    return seen


def _ledger_book(market: str, token: str, ts: object, **extra: object) -> dict:
    row = {"ts": ts, "market_id": market, "token_id": token, "best_ask": 0.40, "spread": 0.01}
    row.update(extra)
    return row


def test_realistic_no_edge_passes_book_ts_not_the_recorder_ts(tmp_path, monkeypatch):
    seen = _ledger_env(tmp_path, monkeypatch, [_ledger_book("m1", "y", NOW_S, book_ts=_ago(30))],
                       [{"id": "m1", "liquidity_usd": 20000.0}])
    paper_ledger.realistic_no_edge(load_risk())
    assert seen[0].book_ts == _ago(30)


def test_realistic_no_edge_without_book_ts_vetoes_missing_book_ts(tmp_path, monkeypatch):
    _write(tmp_path / "books.jsonl", [_ledger_book("m1", "y", paper_ledger.utc_now())])
    _write(tmp_path / "markets.jsonl", [{"id": "m1", "liquidity_usd": 20000.0}])
    for name, path in (("BOOKS_PATH", "books.jsonl"), ("MARKETS_PATH", "markets.jsonl"),
                       ("PAPER_PATH", "paper.jsonl"), ("QUOTES_PATH", "quotes.jsonl")):
        monkeypatch.setattr(paper_ledger, name, tmp_path / path)
    monkeypatch.setattr(paper_ledger, "bus", lambda *args, **kwargs: None)
    before = RISK_PATH.read_bytes()
    result = paper_ledger.realistic_no_edge(load_risk())
    assert result["allow"] is False
    assert "missing_book_ts" in result["decision"]["reasons"]
    assert read_jsonl(tmp_path / "paper.jsonl") == []
    assert RISK_PATH.read_bytes() == before


# --- 2) no gap signal for a stale market; a skip record instead -------------------------------


def test_stale_binary_market_with_a_gap_emits_no_gap_and_a_skip_record(tmp_path):
    result, messages = _run(tmp_path, [_market("m1")], _pair("m1", asks=(0.40, 0.40), book_ts=_ago(121)))
    assert result["gaps"] == 0 and result["stale"] == 1 and result["fills"] == 0
    assert not [row for row in messages if row["from"] in {"ArbBinary", "NegRiskArb"}]
    payload = _stale(messages)[0]["payload"]
    assert payload["gap_signal"] == "skipped"
    assert payload["skip_reason"] == "stale_book"
    assert {"market": "m1", "kind": "stale_market", "reason": "stale_book",
            "stale_reasons": ["stale_book"]} in result["skips"]


def test_one_stale_leg_blocks_the_gap_for_the_whole_market(tmp_path):
    books = _pair("m1", asks=(0.40, 0.40), book_ts=_ago(10))
    books[1]["book_ts"] = None
    books[1]["book_ts_problem"] = "missing_book_ts"
    result, messages = _run(tmp_path, [_market("m1")], books)
    assert result["gaps"] == 0 and result["stale"] == 1
    assert _stale(messages)[0]["payload"]["stale_reasons"] == ["missing_book_ts"]


def test_stale_multi_outcome_market_emits_no_gap(tmp_path):
    books = [{"ts": NOW_S, "market_id": "mm", "token_id": t, "best_ask": 0.30, "best_ask_size": 5,
              "book_ts": _ago(500)} for t in ("a", "b", "c")]
    result, messages = _run(tmp_path, [_market("mm", ("a", "b", "c"))], books)
    assert result["gaps"] == 0
    assert not [row for row in messages if row["from"] == "NegRiskArb"]


def test_fresh_gap_market_still_signals_next_to_a_stale_one(tmp_path):
    books = _pair("fresh", asks=(0.40, 0.40), book_ts=_ago(10)) + _pair("old", asks=(0.40, 0.40), book_ts=_ago(999))
    result, messages = _run(tmp_path, [_market("fresh"), _market("old")], books)
    assert result["gaps"] == 1 and result["stale"] == 1
    gaps = [row for row in messages if row["from"] == "ArbBinary"]
    assert [row["market"] for row in gaps] == ["fresh"]


# --- 3) latest row by parsed time, unparseable rows skipped with a reason ---------------------


def test_latest_by_time_compares_times_not_strings():
    rows = [
        {"k": "a", "ts": "2026-10-08T00:30:00Z", "n": 1},
        {"k": "a", "ts": "2026-10-08T07:00:00+08:00", "n": 2},  # 2026-10-07T23:00Z: string-larger, time-older
        {"k": "b", "ts": "2026-10-08T00:09:00.5Z", "n": 3},
        {"k": "b", "ts": "2026-10-08T00:09:00Z", "n": 4},  # 'Z' > '.' as text, but 0.5 s older
    ]
    chosen, skipped = latest_by_time(rows, lambda row: row["k"])
    assert chosen["a"]["n"] == 1 and chosen["b"]["n"] == 3 and skipped == []


def test_latest_by_time_tie_keeps_the_later_row():
    rows = [{"k": "a", "ts": NOW_S, "n": 1}, {"k": "a", "ts": "2026-10-08T08:10:00+08:00", "n": 2}]
    chosen, _ = latest_by_time(rows, lambda row: row["k"])
    assert chosen["a"]["n"] == 2


@pytest.mark.parametrize("value, reason", [
    ("garbage", "invalid_row_ts"), ("2026-10-08T00:09:30", "invalid_row_ts"), ("1791488993", "invalid_row_ts"),
    (True, "invalid_row_ts"), (None, "missing_row_ts"), ("", "missing_row_ts"), ("   ", "missing_row_ts"),
])
def test_latest_by_time_skips_unparseable_rows_with_a_reason(value, reason):
    rows = [{"k": "a", "ts": NOW_S, "n": 1}, {"k": "a", "ts": value, "n": 2}]
    chosen, skipped = latest_by_time(rows, lambda row: row["k"])
    assert chosen["a"]["n"] == 1
    assert skipped == [{"index": 1, "key": "a", "reason": reason}]


def test_latest_by_time_missing_field_and_ignored_keys():
    rows = [{"k": "a", "n": 1}, {"k": None, "ts": NOW_S}]
    chosen, skipped = latest_by_time(rows, lambda row: row["k"])
    assert chosen == {} and skipped == [{"index": 0, "key": "a", "reason": "missing_row_ts"}]


def test_cycle_picks_the_latest_book_by_time(tmp_path):
    # Text order says the +08:00 row is newer; in time it is 70 minutes older and stale.
    newer = _pair("m1", ts="2026-10-08T00:09:50Z", book_ts=_ago(10))
    older = _pair("m1", ts="2026-10-08T07:00:00+08:00", book_ts="2026-10-08T07:00:00+08:00")
    result, messages = _run(tmp_path, [_market("m1")], newer + older)
    assert result["stale"] == 0 and _stale(messages) == []


def test_cycle_skips_and_records_unparseable_book_rows(tmp_path):
    good = _pair("m1", ts=NOW_S, book_ts=_ago(10))
    bad = _pair("m1", ts="garbage", book_ts=_ago(9999))  # string-larger; old code picked it
    result, messages = _run(tmp_path, [_market("m1")], good + bad)
    assert result["stale"] == 0
    skipped = [s for s in result["skips"] if s["kind"] == "skipped_row"]
    assert {(s["source"], s["token_id"], s["reason"]) for s in skipped} == {
        ("books", "y", "invalid_row_ts"), ("books", "n", "invalid_row_ts")}
    recorded = [row for row in messages if row["payload"].get("skipped_row")]
    assert len(recorded) == 2
    assert all(row["payload"]["reason"] == "invalid_row_ts" for row in recorded)
    assert all(row["payload"]["exchange_action"] == "none" for row in recorded)


def test_cycle_picks_the_latest_market_row_by_time_and_skips_bad_ones(tmp_path):
    markets = [
        {"ts": "2026-10-08T00:09:00Z", "id": "m1", "token_ids": ["y", "n"]},
        {"ts": "2026-10-08T07:00:00+08:00", "id": "m1", "token_ids": ["x1", "x2"]},  # text-newer, time-older
        {"ts": None, "id": "m1", "token_ids": ["z1", "z2"]},
    ]
    result, _ = _run(tmp_path, markets, _pair("m1", asks=(0.40, 0.40), book_ts=_ago(10)))
    assert result["gaps"] == 1  # read tokens y/n from the time-latest row
    assert {"kind": "skipped_row", "source": "markets", "market": "m1", "token_id": "",
            "reason": "missing_row_ts"} in result["skips"]


def test_realistic_no_edge_picks_the_latest_book_by_time_and_reports_skips(tmp_path, monkeypatch):
    books = [
        _ledger_book("m1", "y", "2026-10-08T00:09:00Z", book_ts="2026-10-08T00:09:00Z"),
        _ledger_book("m2", "y", "2026-10-08T07:00:00+08:00", book_ts="2026-10-07T23:00:00Z"),
        _ledger_book("m3", "y", "garbage", book_ts=_ago(1)),
    ]
    seen = _ledger_env(tmp_path, monkeypatch, books,
                       [{"id": m, "liquidity_usd": 20000.0} for m in ("m1", "m2", "m3")])
    result = paper_ledger.realistic_no_edge(load_risk())
    assert seen[0].market == "m1"
    assert result["skipped_rows"] == [{"index": 2, "key": ["m3", "y"], "reason": "invalid_row_ts"}]


# --- 4) historical Forecaster mentions stay -----------------------------------------------------


def test_historical_forecaster_mentions_are_kept():
    loop = (ROOT / "docs" / "LOOP.md").read_text(encoding="utf-8")
    task_n = loop.split("## Task N", 1)[1].split("\n## ", 1)[0]
    assert "Forecaster" in task_n
