"""Offline book timestamp consistency tests (Task R). No network, no .env, no order SDK.

(a) paper_cycle judges book age with book_staleness, the same parser and boundary as RiskGate.
(b) book.record puts the book's own timestamp (CLOB /book ``timestamp``) on the bus, never a
    stand-in; a missing or unusable one is carried as null plus a reason, which RiskGate vetoes.
"""
from __future__ import annotations

import inspect
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import src.book as book
import src.paper_cycle as paper_cycle
from src.book_staleness import MAX_BOOK_AGE_S, book_age_problem, parse_book_ts
from src.podlib import RISK_PATH, load_risk, read_jsonl
from src.risk_gate import Intent, evaluate

NOW = datetime(2026, 10, 8, 0, 10, 0, tzinfo=timezone.utc)
NOW_S = "2026-10-08T00:10:00Z"
STATE = {"open_positions": 0, "market_already_open": False, "daily_loss_usd": 0.0, "recent_fills": [], "own_quotes": []}


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _ago(seconds: float) -> str:
    return _iso(NOW - timedelta(seconds=seconds))


def _write(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _cycle(tmp_path: Path, book_ts: object, *, now: str = NOW_S, missing: bool = False, **kwargs: object) -> tuple[dict, list[dict]]:
    """One binary market with no gap (asks sum to 1.10); only the staleness signal can fire."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    markets = tmp_path / "markets.jsonl"
    books = tmp_path / "books.jsonl"
    bus_path = tmp_path / "bus.jsonl"
    _write(markets, [{"ts": NOW_S, "id": "m1", "token_ids": ["y", "n"]}])
    rows = []
    for token, ask in (("y", 0.60), ("n", 0.50)):
        # Recorder clock in ts; the book's own exchange time in book_ts, which is what is judged (Task T).
        row = {"ts": NOW_S, "market_id": "m1", "token_id": token, "best_ask": ask, "best_ask_size": 5}
        if not missing:
            row["book_ts"] = book_ts
        rows.append(row)
    _write(books, rows)
    result = paper_cycle.run(markets_path=markets, books_path=books, bus_path=bus_path, now=now, **kwargs)
    messages = read_jsonl(bus_path) if bus_path.exists() else []
    return result, [row for row in messages if row["from"] == "BookWatcher"]


def _stale_reasons(tmp_path: Path, book_ts: object, **kwargs: object) -> list[str] | None:
    result, stale = _cycle(tmp_path, book_ts, **kwargs)
    assert result["ok"] is True and result["fills"] == 0 and result["gaps"] == 0
    if result["stale"] == 0:
        assert stale == []
        return None
    assert result["stale"] == 1 and len(stale) == 1
    payload = stale[0]["payload"]
    assert payload["stale"] is True and payload["exchange_action"] == "none"
    return payload["stale_reasons"]


# --- (a) paper_cycle uses book_staleness ------------------------------------------------------


def test_threshold_is_unchanged_and_matches_riskgate():
    assert paper_cycle.STALE_AFTER_S == 120
    assert paper_cycle.STALE_AFTER_S == MAX_BOOK_AGE_S
    assert "book_age" not in RISK_PATH.read_text(encoding="utf-8")


def test_own_parser_is_gone_and_book_staleness_is_used():
    assert not hasattr(paper_cycle, "_age_seconds")
    source = inspect.getsource(paper_cycle)
    assert "book_age_problem" in source and "parse_book_ts" in source
    assert "strptime" not in source
    assert "urlopen" not in source


def test_exactly_120_seconds_is_fresh(tmp_path):
    assert _stale_reasons(tmp_path, _ago(120)) is None


def test_121_seconds_is_stale(tmp_path):
    assert _stale_reasons(tmp_path, _ago(121)) == ["stale_book"]


def test_fractional_seconds_follow_the_same_boundary(tmp_path):
    assert _stale_reasons(tmp_path / "a", "2026-10-08T00:08:00.500Z") is None  # 119.5 s
    assert _stale_reasons(tmp_path / "b", "2026-10-08T00:07:59.500Z") == ["stale_book"]  # 120.5 s


def test_future_timestamp_is_stale_not_fresh(tmp_path):
    assert _stale_reasons(tmp_path / "a", _iso(NOW + timedelta(seconds=1))) == ["future_book_ts"]
    assert _stale_reasons(tmp_path / "b", _iso(NOW + timedelta(hours=1))) == ["future_book_ts"]


def test_offset_timestamps_are_read_like_riskgate(tmp_path):
    assert _stale_reasons(tmp_path / "a", "2026-10-08T08:09:30+08:00") is None  # 30 s old
    assert _stale_reasons(tmp_path / "b", "2026-10-08T08:08:00+08:00") is None  # exactly 120 s
    assert _stale_reasons(tmp_path / "c", "2026-10-08T08:07:59+08:00") == ["stale_book"]
    assert _stale_reasons(tmp_path / "d", "2026-10-08T08:10:01+08:00") == ["future_book_ts"]
    assert _stale_reasons(tmp_path / "e", "2026-10-07T19:09:30-05:00") is None


def test_epoch_seconds_are_read_like_riskgate(tmp_path):
    epoch = NOW.timestamp()
    assert _stale_reasons(tmp_path / "a", epoch - 30) is None
    assert _stale_reasons(tmp_path / "b", int(epoch) - 120) is None
    assert _stale_reasons(tmp_path / "c", int(epoch) - 121) == ["stale_book"]
    assert _stale_reasons(tmp_path / "d", epoch + 5) == ["future_book_ts"]


def test_now_may_carry_an_offset(tmp_path):
    assert _stale_reasons(tmp_path / "a", _ago(30), now="2026-10-08T08:10:00+08:00") is None
    assert _stale_reasons(tmp_path / "b", _ago(121), now="2026-10-08T08:10:00+08:00") == ["stale_book"]


@pytest.mark.parametrize(
    "value, reason",
    [
        ("garbage", "invalid_book_ts"),
        ("2026-10-08T00:09:30", "invalid_book_ts"),  # naive
        ("2026-10-08", "invalid_book_ts"),  # date only
        ("1791488993", "invalid_book_ts"),  # numeric string
        (True, "invalid_book_ts"),
        ("", "missing_book_ts"),
        (None, "missing_book_ts"),
    ],
)
def test_unparseable_book_timestamp_fails_closed(tmp_path, value, reason):
    assert _stale_reasons(tmp_path, value) == [reason]


def test_missing_book_ts_key_fails_closed(tmp_path):
    assert _stale_reasons(tmp_path, None, missing=True) == ["missing_book_ts"]


def test_unparseable_now_marks_every_book_stale_without_a_wall_clock_fallback(tmp_path):
    assert _stale_reasons(tmp_path / "a", _ago(5), now="not-a-time") == ["invalid_now"]
    assert _stale_reasons(tmp_path / "b", _ago(5), now="2026-10-08T00:10:00") == ["invalid_now"]


@pytest.mark.parametrize("limit", [0, -1, float("nan"), float("inf"), True])
def test_broken_stale_after_s_cannot_make_a_book_fresh(tmp_path, limit):
    assert _stale_reasons(tmp_path, _ago(5), stale_after_s=limit) == ["invalid_max_book_age"]


@pytest.mark.parametrize(
    "value",
    [_ago(0), _ago(120), _ago(121), _iso(NOW + timedelta(seconds=1)), "2026-10-08T08:09:30+08:00",
     NOW.timestamp() - 60, NOW.timestamp() + 60, "2026-10-08T00:09:59.999Z", "garbage", "", None],
)
def test_paper_cycle_and_riskgate_agree(tmp_path, value):
    expected = book_age_problem(value, NOW)
    reasons = _stale_reasons(tmp_path, value)
    assert reasons == (None if expected is None else [expected])


def test_stale_market_still_emits_no_fill(tmp_path):
    result, stale = _cycle(tmp_path, _iso(NOW + timedelta(seconds=30)))
    assert result["fills"] == 0 and stale[0]["type"] == "signal"


# --- (b) book.record carries the book's own timestamp -----------------------------------------

BOOK_MS = "1791488993552"  # live /book shape: Unix milliseconds in a string
BOOK_ISO = "2026-10-08T19:49:53.552Z"
LOCAL_TS = "2026-10-09T00:00:00Z"  # the recorder's own clock; must never become book_ts


def _book(**extra: object) -> dict:
    payload = {"market": "0xcond", "bids": [{"price": "0.49", "size": "10"}], "asks": [{"price": "0.51", "size": "10"}]}
    payload.update(extra)
    return payload


def _record(tmp_path: Path, payload: dict) -> tuple[list[dict], list[dict]]:
    market = {"id": "m1", "question": "Q?", "token_ids": ["t1"], "outcomes": ["Yes"]}
    tmp_path.mkdir(parents=True, exist_ok=True)
    written = book.record([market], LOCAL_TS, books_path=tmp_path / "books.jsonl", bus_path=tmp_path / "bus.jsonl",
                          getter=lambda url: payload)
    assert written == 1
    return read_jsonl(tmp_path / "books.jsonl"), read_jsonl(tmp_path / "bus.jsonl")


def test_book_timestamp_reads_clob_epoch_milliseconds():
    assert book.book_timestamp(BOOK_MS) == (BOOK_ISO, None)
    assert book.book_timestamp(int(BOOK_MS)) == (BOOK_ISO, None)
    assert book.book_timestamp(" 1791488993552 ") == (BOOK_ISO, None)
    expected = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=int(BOOK_MS))
    assert parse_book_ts(BOOK_ISO) == expected


def test_book_timestamp_accepts_an_aware_iso_string():
    assert book.book_timestamp("2026-10-09T03:49:53.552+08:00") == (BOOK_ISO, None)
    assert book.book_timestamp("2026-10-08T19:49:53Z") == ("2026-10-08T19:49:53.000Z", None)


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_book_timestamp_missing(raw):
    assert book.book_timestamp(raw) == (None, "missing_book_ts")


@pytest.mark.parametrize(
    "raw",
    ["abc", "-5", "1.7e12", "1791488993552.0", "１７９１４８８９９３５５２", "9" * 30, 1.791488993552e12,
     -1, True, False, "2026-10-08T19:49:53", "2026-10-08", [], {}],
)
def test_book_timestamp_rejects_anything_else(raw):
    assert book.book_timestamp(raw) == (None, "invalid_book_ts")


def test_record_puts_the_books_own_timestamp_on_the_bus(tmp_path):
    rows, messages = _record(tmp_path, _book(timestamp=BOOK_MS))
    payload = messages[0]["payload"]
    assert payload["book_ts"] == BOOK_ISO
    assert payload["book_ts_problem"] is None
    assert payload["book_ts"] != LOCAL_TS
    assert rows[0]["book_ts"] == BOOK_ISO and rows[0]["book_ts_problem"] is None
    assert rows[0]["ts"] == LOCAL_TS  # the recorder clock stays in ts, separately
    fresh_now = parse_book_ts(BOOK_ISO) + timedelta(seconds=30)
    assert book_age_problem(payload["book_ts"], fresh_now) is None
    assert book_age_problem(payload["book_ts"], parse_book_ts(BOOK_ISO) + timedelta(seconds=121)) == "stale_book"


def test_record_without_a_book_timestamp_never_invents_one(tmp_path, capsys):
    rows, messages = _record(tmp_path, _book())
    payload = messages[0]["payload"]
    assert payload["book_ts"] is None
    assert payload["book_ts_problem"] == "missing_book_ts"
    assert rows[0]["book_ts"] is None and rows[0]["book_ts_problem"] == "missing_book_ts"
    assert "book_ts_problem token=t1 reason=missing_book_ts" in capsys.readouterr().err


def test_record_with_an_unusable_book_timestamp_carries_the_reason(tmp_path):
    _, messages = _record(tmp_path, _book(timestamp="soon"))
    assert messages[0]["payload"]["book_ts"] is None
    assert messages[0]["payload"]["book_ts_problem"] == "invalid_book_ts"


def _intent_from(payload: dict) -> Intent:
    return Intent(strategy="paper-gap-check", market="m1", side="buy", model_prob=0.70, market_price=0.40,
                  assumed_slippage=0.005, fee_rate=0.0, liquidity_usd=20000.0, spread=0.01, hypothetical=False,
                  book_ts=payload["book_ts"])


def test_riskgate_vetoes_a_bus_book_without_its_own_timestamp(tmp_path):
    _, missing = _record(tmp_path / "a", _book())
    decision = evaluate(_intent_from(missing[0]["payload"]), load_risk(), STATE, now=NOW)
    assert decision.allow is False and "missing_book_ts" in decision.reasons
    _, bad = _record(tmp_path / "b", _book(timestamp="soon"))
    decision = evaluate(_intent_from(bad[0]["payload"]), load_risk(), STATE, now=NOW)
    assert decision.allow is False and "missing_book_ts" in decision.reasons


def test_riskgate_reads_the_bus_book_ts_with_the_same_boundary(tmp_path):
    _, messages = _record(tmp_path, _book(timestamp=BOOK_MS))
    stamp = parse_book_ts(BOOK_ISO)
    intent = _intent_from(messages[0]["payload"])
    assert "stale_book" not in evaluate(intent, load_risk(), STATE, now=stamp + timedelta(seconds=120)).reasons
    assert "stale_book" in evaluate(intent, load_risk(), STATE, now=stamp + timedelta(seconds=121)).reasons
    assert "future_book_ts" in evaluate(intent, load_risk(), STATE, now=stamp - timedelta(seconds=1)).reasons


def test_book_module_stays_public_and_read_only():
    source = inspect.getsource(book)
    for word in ("private_key", "api_key", "post_order", "create_order", "signature"):
        assert word not in source
