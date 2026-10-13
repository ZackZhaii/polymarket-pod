"""Issue #277 (Task DF): consecutive ``input_pending`` rounds of paper_cycle are counted and escalate.

A round skipped because a monitored input is still changing after the one bounded re-read (#75
criterion 2) used to leave no trace: in ``--loop`` the heartbeat stayed alive while stale and
all_markets_stale could never fire. ``input_pending_streak`` now lives in the StaleWatch state next to
``all_stale_streak``; at ALL_STALE_CONSECUTIVE_CYCLES it takes the existing all_markets_stale global halt
path. A normal completed cycle clears it; a single transient change still only skips one round.
Offline only: no network, no orders, no risk-file edits.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import src.paper_cycle as paper_cycle
from src import halt_store, heartbeat, sentinel
from src.podlib import RISK_PATH
from src.thresholds import ALL_STALE_CONSECUTIVE_CYCLES

NOW = "2026-10-08T00:10:00Z"
_FRESH = "2026-10-08T00:09:30Z"
_OLD = "2026-10-08T00:00:00Z"
_MARKET = {"ts": _FRESH, "id": "binary-gap", "token_ids": ["y", "n"]}
_BOOKS = [
    {"ts": _FRESH, "book_ts": _FRESH, "market_id": "binary-gap", "token_id": "y",
     "best_ask": 0.40, "best_ask_size": 50},
    {"ts": _FRESH, "book_ts": _FRESH, "market_id": "binary-gap", "token_id": "n",
     "best_ask": 0.40, "best_ask_size": 50},
]


def _write(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


class _Appends:
    """Deterministic stand-in for a writer appending to ``target`` while the cycle reads it.

    While ``on`` every signature look differs (the bounded re-read still sees a change); off, the real
    signature is returned. No sleeps, no threads.
    """

    def __init__(self, monkeypatch, target: Path) -> None:
        self.target = Path(target)
        self.on = True
        self.looks = 0
        self._real = paper_cycle._file_signature
        monkeypatch.setattr(paper_cycle, "_file_signature", self._signature)

    def _signature(self, path):
        base = self._real(path)
        if not self.on or Path(path) != self.target or base is None:
            return base
        self.looks += 1
        return (base[0] + self.looks, base[1] + self.looks)


@pytest.fixture
def files(tmp_path):
    markets, books = tmp_path / "markets.jsonl", tmp_path / "books.jsonl"
    _write(markets, [_MARKET])
    _write(books, [dict(row, book_ts=_OLD) for row in _BOOKS])  # one stale market: a market streak to keep
    paths = {"markets": markets, "books": books, "bus": tmp_path / "bus.jsonl",
             "state": tmp_path / "stale.json", "quotes": tmp_path / "quotes.jsonl"}

    def run():
        return paper_cycle.run(markets_path=paths["markets"], books_path=paths["books"], bus_path=paths["bus"],
                               now=NOW, sentinel_state_path=paths["state"], quotes_path=paths["quotes"])
    paths["run"] = run
    return paths


def _saved(files) -> dict:
    return json.loads(files["state"].read_text(encoding="utf-8"))


def _all_stale_globals() -> list[dict]:
    return [h for h in halt_store.active_halts()
            if h["scope"] == "global" and h["reason"].startswith(paper_cycle.ALL_STALE_ALARM)]


def test_the_threshold_is_the_existing_all_stale_constant():
    assert ALL_STALE_CONSECUTIVE_CYCLES == 2  # reused, never a new threshold
    assert paper_cycle.ALL_STALE_CONSECUTIVE_CYCLES is ALL_STALE_CONSECUTIVE_CYCLES


def test_sustained_changing_input_escalates_via_all_markets_stale(files, monkeypatch):
    before = RISK_PATH.read_bytes()
    first = files["run"]()  # a normal cycle: the stale market's streak is 1
    assert first["ok"] is True and first["sentinel"]["input_pending_streak"] == 0
    market_streaks = _saved(files)["streaks"]
    assert market_streaks == {"binary-gap": 1}
    appends = _Appends(monkeypatch, files["books"])

    results = [files["run"]() for _ in range(ALL_STALE_CONSECUTIVE_CYCLES)]

    for n, result in enumerate(results, start=1):
        assert result["ok"] is False and result["reason"] == paper_cycle.INPUT_PENDING
        assert (result["gaps"], result["stale"], result["signals"], result["fills"]) == (0, 0, 0, 0)
        assert result["sentinel"]["input_pending_streak"] == n
    assert [r["sentinel"]["input_pending_halt"] for r in results] == [False] * (ALL_STALE_CONSECUTIVE_CYCLES - 1) + [True]
    saved = _saved(files)
    assert saved["input_pending_streak"] == ALL_STALE_CONSECUTIVE_CYCLES
    assert saved["streaks"] == market_streaks  # #75: the per-market streaks are neither advanced nor reset
    halts = _all_stale_globals()
    assert len(halts) == 1
    assert "input_pending" in halts[0]["reason"] and str(ALL_STALE_CONSECUTIVE_CYCLES) in halts[0]["reason"]
    assert appends.looks > 0

    again = files["run"]()  # still changing: the streak grows, the active halt is not duplicated
    assert again["sentinel"]["input_pending_streak"] == ALL_STALE_CONSECUTIVE_CYCLES + 1
    assert again["sentinel"]["input_pending_halt"] is False
    assert len(_all_stale_globals()) == 1
    assert RISK_PATH.read_bytes() == before


def test_a_resumed_halt_fires_again_while_the_input_is_still_pending(files, monkeypatch):
    _Appends(monkeypatch, files["books"])
    for _ in range(ALL_STALE_CONSECUTIVE_CYCLES):
        files["run"]()
    (halt,) = _all_stale_globals()
    halt_store.resume(from_role="Operator", scope="global", halt_id=halt["id"], approved_by="op",
                      root_cause="x", postmortem="PR #0", bus_path=files["bus"])
    assert _all_stale_globals() == []
    assert files["run"]()["sentinel"]["input_pending_halt"] is True  # like all_markets_stale after a resume
    assert len(_all_stale_globals()) == 1


def test_one_pending_round_then_a_normal_cycle_clears_the_streak(files, monkeypatch):
    appends = _Appends(monkeypatch, files["books"])
    pending = files["run"]()
    assert pending["reason"] == paper_cycle.INPUT_PENDING
    assert pending["sentinel"]["input_pending_streak"] == 1 and pending["sentinel"]["input_pending_halt"] is False

    appends.on = False
    normal = files["run"]()
    assert normal["ok"] is True and normal["reason"] == "signals_only"
    assert normal["sentinel"]["input_pending_streak"] == 0
    assert _saved(files)["input_pending_streak"] == 0

    appends.on = True
    later = files["run"]()  # a later single change counts from 1 again: never a halt
    assert later["sentinel"]["input_pending_streak"] == 1 and later["sentinel"]["input_pending_halt"] is False
    assert _all_stale_globals() == []
    assert [h for h in halt_store.active_halts() if h["scope"] == "global"] == []


def test_a_single_transient_change_only_skips_one_round(files, monkeypatch):
    halts_before = halt_store.active_halts()
    _Appends(monkeypatch, files["books"])

    result = files["run"]()

    assert result["ok"] is False and result["reason"] == paper_cycle.INPUT_PENDING
    assert result["skipped"] == 1 and result["input_problems"][0]["problem"] == paper_cycle.INPUT_CHANGING
    assert result["sentinel"]["input_pending_streak"] == 1
    assert result["sentinel"]["input_pending_halt"] is False
    assert halt_store.active_halts() == halts_before
    saved = _saved(files)
    assert saved["input_pending_streak"] == 1 and saved["streaks"] == {} and saved["all_stale_streak"] == 0
    assert not files["bus"].exists()  # no gap, no stale and no halt signal


def test_no_duplicate_when_an_all_markets_stale_halt_is_already_active(files, monkeypatch):
    sentinel.halt(reason=f"{paper_cycle.ALL_STALE_ALARM}: all 1 checked markets stale for 2 consecutive cycles",
                  scope=halt_store.SCOPE_GLOBAL, bus_path=files["bus"], quotes_path=files["quotes"])
    _Appends(monkeypatch, files["books"])
    results = [files["run"]() for _ in range(ALL_STALE_CONSECUTIVE_CYCLES)]
    assert results[-1]["sentinel"]["input_pending_streak"] == ALL_STALE_CONSECUTIVE_CYCLES
    assert not any(r["sentinel"]["input_pending_halt"] for r in results)
    assert len(_all_stale_globals()) == 1


def test_no_halt_written_while_the_fallback_halt_file_is_active(files, monkeypatch):
    halt_store.HALT_FALLBACK_PATH.parent.mkdir(parents=True, exist_ok=True)
    halt_store.HALT_FALLBACK_PATH.write_text("halt\n", encoding="utf-8")
    _Appends(monkeypatch, files["books"])
    results = [files["run"]() for _ in range(ALL_STALE_CONSECUTIVE_CYCLES)]
    assert results[-1]["sentinel"]["input_pending_streak"] == ALL_STALE_CONSECUTIVE_CYCLES
    assert not any(r["sentinel"]["input_pending_halt"] for r in results)


def test_a_bad_state_file_is_not_overwritten_and_counts_one_more_pending_round(files, monkeypatch):
    files["state"].write_text("{not json", encoding="utf-8")
    damaged = files["state"].read_bytes()
    _Appends(monkeypatch, files["books"])

    result = files["run"]()

    assert result["sentinel"]["state_problem"] == sentinel.STATE_CORRUPT
    # Fail closed like a stale check: the unreadable streak counts as one more pending round.
    assert result["sentinel"]["input_pending_streak"] == 2 and result["sentinel"]["input_pending_halt"] is True
    assert files["state"].read_bytes() == damaged  # kept for the next full cycle to count per market
    assert len(_all_stale_globals()) == 1


def test_a_failed_state_write_counts_one_more_pending_round(files, monkeypatch):
    monkeypatch.setattr(sentinel, "save_watch", lambda watch, path: sentinel.STATE_WRITE_FAILED)
    _Appends(monkeypatch, files["books"])

    result = files["run"]()

    assert result["sentinel"]["write_problem"] == sentinel.STATE_WRITE_FAILED
    assert result["sentinel"]["input_pending_streak"] == 2 and result["sentinel"]["input_pending_halt"] is True
    assert len(_all_stale_globals()) == 1


def test_input_invalid_leaves_the_input_pending_streak_untouched(files, monkeypatch):
    appends = _Appends(monkeypatch, files["books"])
    files["run"]()
    appends.on = False
    recorded = files["state"].read_bytes()
    assert json.loads(recorded)["input_pending_streak"] == 1
    files["markets"].write_text("{not json\n", encoding="utf-8")

    result = files["run"]()

    assert result["reason"] == paper_cycle.INPUT_INVALID
    assert files["state"].read_bytes() == recorded


def test_state_without_input_pending_streak_loads_as_zero_and_a_bad_one_is_malformed():
    watch = sentinel.StaleWatch()
    watch.input_pending_streak = 3
    data = watch.to_dict()
    assert data["input_pending_streak"] == 3
    assert sentinel.StaleWatch.from_dict(data).input_pending_streak == 3
    del data["input_pending_streak"]  # a state file written before Task DF
    assert sentinel.StaleWatch.from_dict(data).input_pending_streak == 0
    for bad in (-1, True, False, "1", 1.5, None):
        data["input_pending_streak"] = bad
        with pytest.raises((ValueError, TypeError)):
            sentinel.StaleWatch.from_dict(data)


def test_a_malformed_input_pending_streak_is_a_state_problem(tmp_path):
    path = tmp_path / "stale.json"
    data = sentinel.StaleWatch().to_dict()
    data["input_pending_streak"] = True
    path.write_text(json.dumps(data), encoding="utf-8")
    watch, problem = sentinel.load_watch(path)
    assert problem == sentinel.STATE_MALFORMED and watch.input_pending_streak == 0


# ---------------------------------------------------------------- heartbeat progress

def _read_beat() -> dict:
    return json.loads(heartbeat.beat_path("paper_cycle").read_text(encoding="utf-8"))


def _cli(files, monkeypatch):
    real_run = paper_cycle.run
    beats: list[dict] = []

    def run(**kw):
        beats.append(_read_beat()["progress"])
        kw.pop("books_path", None)
        return real_run(markets_path=files["markets"], books_path=files["books"], bus_path=files["bus"], now=NOW,
                        sentinel_state_path=files["state"], quotes_path=files["quotes"], **kw)
    monkeypatch.setattr(paper_cycle, "run", run)
    return beats


def test_the_loop_heartbeat_carries_the_input_pending_streak(files, monkeypatch, capsys):
    beats = _cli(files, monkeypatch)
    _Appends(monkeypatch, files["books"])
    cycles = ALL_STALE_CONSECUTIVE_CYCLES + 1
    code = paper_cycle.main(["--books", str(files["books"]), "--loop", "--max-cycles", str(cycles)],
                            sleep=lambda s: None, monotonic=lambda: 0.0)
    assert code == paper_cycle.EXIT_OK  # --loop keeps going through pending rounds
    assert [b.get("input_pending_streak") for b in beats[1:]] == list(range(1, cycles))
    final = _read_beat()
    assert final["state"] == "finished" and final["exit_code"] == 0
    assert final["progress"]["input_pending_streak"] == cycles
    assert len(_all_stale_globals()) == 1


def test_a_one_shot_pending_round_finishes_with_the_streak(files, monkeypatch, capsys):
    _cli(files, monkeypatch)
    _Appends(monkeypatch, files["books"])
    assert paper_cycle.main([]) == paper_cycle.EXIT_OK
    final = _read_beat()
    assert final["exit_code"] == 0 and final["progress"]["input_pending_streak"] == 1


def test_a_normal_cycle_reports_a_cleared_streak_in_the_heartbeat(files, monkeypatch, capsys):
    _cli(files, monkeypatch)
    assert paper_cycle.main([]) == paper_cycle.EXIT_OK
    assert _read_beat()["progress"]["input_pending_streak"] == 0
