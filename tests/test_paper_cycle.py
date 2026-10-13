"""Offline paper-cycle tests. No network, no fills, no risk-file edits."""
from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

import src.paper_cycle as paper_cycle
from src import halt_store
from src.podlib import RISK_PATH, read_jsonl


def _write(path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def test_cycle_emits_gap_and_stale_signals_but_no_fills(tmp_path):
    before = RISK_PATH.read_bytes()
    markets = tmp_path / "markets.jsonl"
    books = tmp_path / "books.jsonl"
    bus_path = tmp_path / "bus.jsonl"
    now = "2026-10-08T00:10:00Z"
    fresh = "2026-10-08T00:09:30Z"
    old = "2026-10-08T00:00:00Z"
    _write(
        markets,
        [
            {"ts": fresh, "id": "binary-gap", "token_ids": ["y", "n"]},
            {"ts": fresh, "id": "multi-gap", "token_ids": ["a", "b", "c"]},
            {"ts": fresh, "id": "no-gap", "token_ids": ["p", "q"]},
            {"ts": old, "id": "stale", "token_ids": ["s1", "s2"]},
        ],
    )
    _write(
        books,
        [
            {"ts": fresh, "book_ts": fresh, "market_id": "binary-gap", "token_id": "y", "best_ask": 0.40, "best_ask_size": 50},
            {"ts": fresh, "book_ts": fresh, "market_id": "binary-gap", "token_id": "n", "best_ask": 0.40, "best_ask_size": 20},
            {"ts": fresh, "book_ts": fresh, "market_id": "multi-gap", "token_id": "a", "best_ask": 0.30, "best_ask_size": 10},
            {"ts": fresh, "book_ts": fresh, "market_id": "multi-gap", "token_id": "b", "best_ask": 0.30, "best_ask_size": 8},
            {"ts": fresh, "book_ts": fresh, "market_id": "multi-gap", "token_id": "c", "best_ask": 0.25, "best_ask_size": 12},
            {"ts": fresh, "book_ts": fresh, "market_id": "no-gap", "token_id": "p", "best_ask": 0.60, "best_ask_size": 5},
            {"ts": fresh, "book_ts": fresh, "market_id": "no-gap", "token_id": "q", "best_ask": 0.50, "best_ask_size": 5},
            {"ts": old, "book_ts": old, "market_id": "stale", "token_id": "s1", "best_ask": 0.60, "best_ask_size": 5},
            {"ts": old, "book_ts": old, "market_id": "stale", "token_id": "s2", "best_ask": 0.50, "best_ask_size": 5},
        ],
    )
    result = paper_cycle.run(
        markets_path=markets,
        books_path=books,
        bus_path=bus_path,
        now=now,
    )
    assert result["ok"] is True
    assert result["fills"] == 0
    assert result["gaps"] == 2
    assert result["stale"] == 1
    messages = read_jsonl(bus_path)
    kinds = [row["type"] for row in messages]
    assert "fill" not in kinds
    assert kinds.count("signal") == 3
    by_from = {row["from"] for row in messages}
    assert by_from == {"ArbBinary", "NegRiskArb", "BookWatcher"}
    binary = next(row for row in messages if row["from"] == "ArbBinary")
    assert binary["payload"]["locked_profit"] is False
    assert binary["payload"]["displayed_depth"] == 20
    assert binary["payload"]["depth_is_not_an_order"] is True
    assert RISK_PATH.read_bytes() == before
    assert "urlopen" not in inspect.getsource(paper_cycle)


def test_pin_drift_emits_nothing(tmp_path):
    before = RISK_PATH.read_bytes()
    cfg = paper_cycle.load_risk()
    cfg["live_enabled"] = True
    bus_path = tmp_path / "bus.jsonl"
    result = paper_cycle.run(markets_path=tmp_path / "markets.jsonl", books_path=tmp_path / "books.jsonl", bus_path=bus_path, cfg=cfg)
    assert result["ok"] is False
    assert result["reason"] == "phase0_pin"
    assert result["fills"] == 0
    assert not bus_path.exists()
    assert RISK_PATH.read_bytes() == before


# ------------------------------------------------------------------ Issue #75: strict input reading

_FRESH = "2026-10-08T00:09:30Z"
_MARKET = {"ts": _FRESH, "id": "binary-gap", "token_ids": ["y", "n"]}
# Two fresh 0.40 asks: with partial rows these would already be a gap.
_BOOKS = [
    {"ts": _FRESH, "book_ts": _FRESH, "market_id": "binary-gap", "token_id": "y",
     "best_ask": 0.40, "best_ask_size": 50},
    {"ts": _FRESH, "book_ts": _FRESH, "market_id": "binary-gap", "token_id": "n",
     "best_ask": 0.40, "best_ask_size": 50},
]
_BROKEN_LINES = {
    "bad_json": (b"{not json}\n", "bad_json", 3),
    "not_object": (b"[1, 2]\n", "not_object", 3),
    "blank_line": (b"   \n", "blank_line", 3),
    "not_utf8": (b"\xff\xfe\n", "not_utf8", None),
    "torn_tail": (b'{"ts": "2026-10-08T00:09:30Z"', "torn_tail", None),
}


def _broken_books(tmp_path, tail: bytes) -> Path:
    books = tmp_path / "books.jsonl"
    _write(books, _BOOKS)
    books.write_bytes(books.read_bytes() + tail)
    return books


@pytest.mark.parametrize("name", sorted(_BROKEN_LINES))
def test_corrupt_book_input_refuses_the_cycle_without_a_gap(tmp_path, name):
    before = RISK_PATH.read_bytes()
    tail, problem, line = _BROKEN_LINES[name]
    markets, bus_path = tmp_path / "markets.jsonl", tmp_path / "bus.jsonl"
    state = tmp_path / "stale.json"
    _write(markets, [_MARKET])
    books = _broken_books(tmp_path, tail)
    damaged = books.read_bytes()

    result = paper_cycle.run(markets_path=markets, books_path=books, bus_path=bus_path,
                             now="2026-10-08T00:10:00Z", sentinel_state_path=state)

    assert result["ok"] is False
    assert result["reason"] == "input_invalid"
    assert (result["gaps"], result["stale"], result["signals"], result["fills"]) == (0, 0, 0, 0)
    assert result["input_problems"] == [{"path": str(books), "line": line, "problem": problem}]
    assert result["sentinel"]["checked"] == 0
    assert not bus_path.exists()  # no gap, no stale and no skip signal
    assert not state.exists()  # StaleWatch untouched: no advance and no reset
    assert books.read_bytes() == damaged  # the source file is reported, never repaired or truncated
    assert RISK_PATH.read_bytes() == before


@pytest.mark.parametrize("name", ["bad_json", "not_utf8"])
def test_corrupt_markets_input_refuses_the_cycle(tmp_path, name):
    tail, problem, line = _BROKEN_LINES[name]
    markets, bus_path = tmp_path / "markets.jsonl", tmp_path / "bus.jsonl"
    _write(markets, [_MARKET])
    markets.write_bytes(markets.read_bytes() + tail)
    books = tmp_path / "books.jsonl"
    _write(books, _BOOKS)

    result = paper_cycle.run(markets_path=markets, books_path=books, bus_path=bus_path,
                             now="2026-10-08T00:10:00Z", sentinel_state_path=tmp_path / "stale.json")

    assert result["ok"] is False
    assert result["reason"] == "input_invalid"
    assert result["gaps"] == 0 and result["fills"] == 0
    # one good market row, so a line-level problem is line 2 of markets.jsonl
    expected_line = None if line is None else 2
    assert result["input_problems"] == [{"path": str(markets), "line": expected_line, "problem": problem}]
    assert not bus_path.exists()


def test_corrupt_rotated_book_file_refuses_the_cycle(tmp_path):
    run_dir, bus_path = tmp_path / "run", tmp_path / "bus.jsonl"
    (run_dir / "books").mkdir(parents=True)
    markets = tmp_path / "markets.jsonl"
    _write(markets, [_MARKET])
    first = _broken_books(run_dir / "books", b"")
    first.rename(run_dir / "books" / "books-000001.jsonl")
    bad = run_dir / "books" / "books-000002.jsonl"
    bad.write_bytes(b'{"market_id": "binary-gap", "token_id": "y", "best_ask": 0.4}\nnot json\n')

    result = paper_cycle.run(markets_path=markets, bus_path=bus_path, recording_dir=run_dir,
                             now="2026-10-08T00:10:00Z", sentinel_state_path=tmp_path / "stale.json")

    assert result["ok"] is False
    assert result["reason"] == "input_invalid"
    assert result["gaps"] == 0
    assert result["input_problems"] == [{"path": str(bad), "line": 2, "problem": "bad_json"}]
    assert not bus_path.exists()


def test_unreadable_books_are_refused_not_treated_as_empty(tmp_path):
    markets, bus_path = tmp_path / "markets.jsonl", tmp_path / "bus.jsonl"
    _write(markets, [_MARKET])
    not_a_file = tmp_path / "books.jsonl"
    not_a_file.mkdir()

    result = paper_cycle.run(markets_path=markets, books_path=not_a_file, bus_path=bus_path,
                             now="2026-10-08T00:10:00Z", sentinel_state_path=tmp_path / "stale.json")

    assert result["ok"] is False
    assert result["input_problems"] == [{"path": str(not_a_file), "line": None, "problem": "unreadable"}]
    assert result["stale"] == 0 and result["fills"] == 0


def test_missing_and_empty_books_keep_the_missing_book_rule(tmp_path):
    before = RISK_PATH.read_bytes()
    markets = tmp_path / "markets.jsonl"
    _write(markets, [_MARKET])
    empty = tmp_path / "books.jsonl"
    empty.write_text("", encoding="utf-8")

    for books in (tmp_path / "absent.jsonl", empty):
        result = paper_cycle.run(markets_path=markets, books_path=books, now="2026-10-08T00:10:00Z",
                                 sentinel_state_path=tmp_path / f"stale-{books.name}.json")

        assert result["ok"] is True and result["reason"] == "signals_only"
        assert result["books_problem"] is None
        assert (result["gaps"], result["stale"], result["fills"]) == (0, 1, 0)  # missing_book stays stale
        assert result["skips"][0]["stale_reasons"] == ["missing_book"]
    assert RISK_PATH.read_bytes() == before


# ------------------------------------- Issue #75 criterion 2: one bounded re-read of a changing input

_OLD = "2026-10-08T00:00:00Z"


def _stale_books() -> list[dict]:
    """The same two legs with an old book_ts: one stale market, one StaleWatch check."""
    return [dict(row, book_ts=_OLD) for row in _BOOKS]


def _changing(monkeypatch, target, *, stabilise: bool = False) -> dict:
    """Make ``target``'s signature change on every look, or quiet down after its first read pair.

    A deterministic stand-in for a writer appending while the cycle reads: no sleeps, no threads.
    """
    real = paper_cycle._file_signature
    state = {"looks": 0}

    def signature(path):
        base = real(path)
        if Path(path) != Path(target) or base is None:
            return base
        state["looks"] += 1
        if stabilise and state["looks"] > 2:
            return base
        return (base[0] + state["looks"], base[1] + state["looks"])

    monkeypatch.setattr(paper_cycle, "_file_signature", signature)
    return state


def _counted_reads(monkeypatch) -> list[str]:
    reads: list[str] = []
    real = paper_cycle.read_strict_jsonl

    def counted(path):
        reads.append(str(path))
        return real(path)

    monkeypatch.setattr(paper_cycle, "read_strict_jsonl", counted)
    return reads


def test_a_book_appended_while_it_is_read_is_reread_once_and_the_cycle_runs(tmp_path, monkeypatch):
    before = RISK_PATH.read_bytes()
    markets, books, bus_path = tmp_path / "markets.jsonl", tmp_path / "books.jsonl", tmp_path / "bus.jsonl"
    _write(markets, [_MARKET])
    _write(books, _BOOKS)
    _changing(monkeypatch, books, stabilise=True)  # one append during the first read, then quiet
    reads = _counted_reads(monkeypatch)

    result = paper_cycle.run(markets_path=markets, books_path=books, bus_path=bus_path,
                             now="2026-10-08T00:10:00Z", sentinel_state_path=tmp_path / "stale.json")

    assert result["ok"] is True and result["reason"] == "signals_only"
    assert result["gaps"] == 1  # the gap of the re-read rows, not of a partial view
    assert reads.count(str(books)) == 2  # the initial read plus exactly the one bounded re-read
    assert reads.count(str(markets)) == 1  # a file that never changed is read exactly once
    assert RISK_PATH.read_bytes() == before


@pytest.mark.parametrize("which", ["books", "markets"])
def test_an_input_still_changing_after_the_one_re_read_skips_the_round(tmp_path, monkeypatch, which):
    before = RISK_PATH.read_bytes()
    markets, books, bus_path = tmp_path / "markets.jsonl", tmp_path / "books.jsonl", tmp_path / "bus.jsonl"
    state = tmp_path / "stale.json"
    _write(markets, [_MARKET])
    _write(books, _BOOKS)
    target = books if which == "books" else markets
    damaged = target.read_bytes()
    _changing(monkeypatch, target)  # every look sees a new append
    reads = _counted_reads(monkeypatch)
    halts_before = halt_store.active_halts()

    result = paper_cycle.run(markets_path=markets, books_path=books, bus_path=bus_path,
                             now="2026-10-08T00:10:00Z", sentinel_state_path=state)

    assert result["ok"] is False and result["reason"] == paper_cycle.INPUT_PENDING
    assert (result["gaps"], result["stale"], result["signals"], result["fills"]) == (0, 0, 0, 0)
    assert result["input_problems"] == [{"path": str(target), "line": None, "problem": "changing"}]
    assert result["skipped"] == 1
    assert result["skips"] == [{"kind": "input_pending", "reason": "input_pending", "path": str(target)}]
    assert reads.count(str(target)) == 2  # one bounded re-read, never an unbounded retry
    assert not bus_path.exists()  # no gap, no stale and no skip signal
    assert not state.exists()  # StaleWatch untouched: no advance and no reset
    assert halt_store.active_halts() == halts_before  # a transient append is never a permanent halt
    assert target.read_bytes() == damaged  # the source file is reported, never repaired or truncated
    assert RISK_PATH.read_bytes() == before


def test_input_pending_leaves_the_stale_watch_streak_untouched(tmp_path, monkeypatch):
    markets, books, bus_path = tmp_path / "markets.jsonl", tmp_path / "books.jsonl", tmp_path / "bus.jsonl"
    state = tmp_path / "stale.json"
    _write(markets, [_MARKET])
    _write(books, _stale_books())

    first = paper_cycle.run(markets_path=markets, books_path=books, bus_path=bus_path,
                            now="2026-10-08T00:10:00Z", sentinel_state_path=state)
    assert first["stale"] == 1 and state.exists()
    recorded = state.read_bytes()

    _changing(monkeypatch, books)  # still appended to
    skipped = paper_cycle.run(markets_path=markets, books_path=books, bus_path=bus_path,
                              now="2026-10-08T00:10:00Z", sentinel_state_path=state)
    assert skipped["reason"] == paper_cycle.INPUT_PENDING
    assert state.read_bytes() == recorded  # the streak is neither advanced nor cleared
    assert skipped["sentinel"] == {"checked": 0, "state_problem": None, "write_problem": None, "halts": [],
                                   "state_path": str(state), "all_stale_streak": 0, "all_stale_halt": False}


def test_only_the_active_rotated_book_file_is_monitored_for_append_changes(tmp_path, monkeypatch):
    run_dir, bus_path = tmp_path / "run", tmp_path / "bus.jsonl"
    (run_dir / "books").mkdir(parents=True)
    markets = tmp_path / "markets.jsonl"
    _write(markets, [_MARKET])
    closed = run_dir / "books" / "books-000001.jsonl"
    active = run_dir / "books" / "books-000002.jsonl"
    _write(closed, _BOOKS)
    _write(active, _BOOKS)
    _changing(monkeypatch, closed)  # a rotated-away file keeps "changing": it is not monitored
    reads = _counted_reads(monkeypatch)

    result = paper_cycle.run(markets_path=markets, bus_path=bus_path, recording_dir=run_dir,
                             now="2026-10-08T00:10:00Z", sentinel_state_path=tmp_path / "stale.json")

    assert result["ok"] is True
    assert reads.count(str(closed)) == 1  # read strictly, once: only the active file may be re-read
    assert reads.count(str(active)) == 1  # a quiet active file is read exactly once too


def test_a_still_changing_active_rotated_book_file_skips_the_round(tmp_path, monkeypatch):
    run_dir, bus_path = tmp_path / "run", tmp_path / "bus.jsonl"
    (run_dir / "books").mkdir(parents=True)
    markets = tmp_path / "markets.jsonl"
    _write(markets, [_MARKET])
    active = run_dir / "books" / "books-000002.jsonl"
    _write(run_dir / "books" / "books-000001.jsonl", _BOOKS)
    _write(active, _BOOKS)
    _changing(monkeypatch, active)

    result = paper_cycle.run(markets_path=markets, bus_path=bus_path, recording_dir=run_dir,
                             now="2026-10-08T00:10:00Z", sentinel_state_path=tmp_path / "stale.json")

    assert result["ok"] is False and result["reason"] == paper_cycle.INPUT_PENDING
    assert (result["gaps"], result["stale"], result["fills"]) == (0, 0, 0)
    assert result["input_problems"] == [{"path": str(active), "line": None, "problem": "changing"}]
    assert not bus_path.exists()
