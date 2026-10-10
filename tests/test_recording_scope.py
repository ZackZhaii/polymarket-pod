"""Issue #125 (Task BN): paper_cycle --recording-dir signals only for the M1 run's manifest markets.

Poly指挥's ruling on the #88 conflict: (a) staleness of every market is still recorded and alarmed; (b) only
markets in RUN_DIR/manifest.json may produce a gap signal (fail closed on a missing / bad / empty manifest);
(c) a market outside it is rejected as ``not_recorded`` and no longer halted market by market for a book it
was never going to have; (d) a market holding a paper position still halts when outside the manifest or stale.
all_markets_stale counts the manifest's markets plus the held ones. Without --recording-dir nothing changes.
Offline: tmp_path files only; the conftest isolates halt state, Sentinel state, quotes and the ledger.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import src.halt_store as halt_store
import src.paper_cycle as paper_cycle
from src import m1_report, m1_run
from src.podlib import RISK_PATH, read_jsonl
from src.thresholds import ALL_STALE_CONSECUTIVE_CYCLES

NOW = "2026-10-10T12:00:00.000Z"
FRESH = "2026-10-10T11:59:58.000Z"
OLD = "2026-10-10T11:00:00.000Z"
MARKETS = ["m0", "m3", "x0", "x1", "x2"]  # m0 recorded; m3 held in the legacy ledger fixture (m1 m2 m3 m4 m5)
LEDGER_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "ledger" / "legacy_mixed.jsonl"


def _write(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _book(market, token, ts=FRESH, ask=0.4):
    return {"ts": ts, "book_ts": ts, "market_id": market, "token_id": token, "best_ask": ask, "best_ask_size": 10}


class Setup:
    """markets.jsonl with MARKETS (two legs each), a run dir with ``books`` rows and a manifest."""

    def __init__(self, tmp: Path, *, manifest=("m0",), books=None, ledger: Path | None = None):
        self.tmp = tmp
        self.markets = tmp / "markets.jsonl"
        _write(self.markets, [{"ts": OLD, "id": m, "token_ids": [f"{m}-y", f"{m}-n"]} for m in MARKETS])
        self.run_dir = tmp / "run"
        _write(self.run_dir / "books" / "books-000001.jsonl",
               books if books is not None else [_book("m0", "m0-y"), _book("m0", "m0-n")])
        if manifest is not None:
            self.write_manifest({"schema": "m1_run/1", "assets": [
                {"token_id": f"{m}-{side}", "market_id": m} for m in manifest for side in ("y", "n")]})
        self.bus = tmp / "bus.jsonl"
        self.ledger = ledger if ledger is not None else tmp / "paper.jsonl"

    def write_manifest(self, payload):
        text = payload if isinstance(payload, str) else json.dumps(payload)
        (self.run_dir / "manifest.json").write_text(text, encoding="utf-8")

    def cycle(self, **kw):
        kw.setdefault("recording_dir", self.run_dir)
        before = RISK_PATH.read_bytes()
        result = paper_cycle.run(markets_path=self.markets, books_path=self.tmp / "unused.jsonl", bus_path=self.bus,
                                 now=NOW, sentinel_state_path=self.tmp / "state.json",
                                 quotes_path=self.tmp / "quotes.jsonl", paper_path=self.ledger, **kw)
        assert RISK_PATH.read_bytes() == before
        return result

    def signals(self, sender):
        return [row for row in read_jsonl(self.bus) if row.get("from") == sender]

    def gap_markets(self):
        return sorted({row["market"] for row in read_jsonl(self.bus) if row.get("from") in ("ArbBinary", "NegRiskArb")})


def _market_halts():
    return sorted(h["market"] for h in halt_store.active_halts() if h["scope"] == "market")


def _held_ledger(tmp: Path) -> Path:
    path = tmp / "held.jsonl"
    shutil.copyfile(LEDGER_FIXTURE, path)
    return path


# --- (b) + (c): one recorded market of five ---------------------------------------------------------


def test_unrecorded_markets_are_not_recorded_rejects_not_halts_and_their_staleness_is_still_recorded(tmp_path):
    s = Setup(tmp_path)
    for _ in range(ALL_STALE_CONSECUTIVE_CYCLES + 1):
        result = s.cycle()
    assert result["not_recorded"] == 4 and result["sentinel"]["checked"] == 1
    assert result["sentinel"]["halts"] == [] and _market_halts() == []  # no market halt for m3 x0 x1 x2
    assert result["gaps"] == 1 and s.gap_markets() == ["m0"]
    assert result["recording"] == {"manifest_problem": None, "recorded_markets": 1, "held_markets": 0,
                                   "ledger_problem": None}
    rejects = [row for row in s.signals("BookWatcher") if row["payload"].get("skip_reason") == "not_recorded"]
    assert sorted({row["market"] for row in rejects}) == ["m3", "x0", "x1", "x2"]
    # (a) staleness still recorded and alarmed: every reject says why its book is unusable
    assert all(row["payload"]["stale"] is True and row["payload"]["stale_reasons"] == ["missing_book"]
               and row["payload"]["gap_signal"] == "skipped" and row["payload"]["exchange_action"] == "none"
               for row in rejects)
    kinds = {(skip["market"], skip["kind"], skip["reason"]) for skip in result["skips"]}
    assert ("x1", "not_recorded_market", "not_recorded") in kinds
    assert not result["sentinel"]["all_stale_halt"]


def test_a_not_recorded_market_with_a_perfect_gap_in_the_recording_still_gets_no_signal(tmp_path):
    books = [_book(m, f"{m}-{side}", ask=0.30) for m in ("m0", "x1") for side in ("y", "n")]  # 0.60 total: gaps
    s = Setup(tmp_path, books=books)
    result = s.cycle()
    assert s.gap_markets() == ["m0"] and result["gaps"] == 1  # x1 is fresh and gapped, but not recorded
    x1 = [row for row in s.signals("BookWatcher") if row["market"] == "x1"]
    assert len(x1) == 1 and x1[0]["payload"]["skip_reason"] == "not_recorded" and x1[0]["payload"]["stale"] is False


# --- (b) fail closed on the manifest -------------------------------------------------------------------


BAD_MANIFESTS = {
    "missing": None,
    "not_json": "{not json",
    "wrong_schema": {"schema": "other/1", "assets": [{"token_id": "m0-y", "market_id": "m0"}]},
    "not_an_object": [{"market_id": "m0"}],
    "assets_not_a_list": {"schema": "m1_run/1", "assets": {"market_id": "m0"}},
    "no_assets": {"schema": "m1_run/1", "assets": []},
    "blank_market_ids": {"schema": "m1_run/1", "assets": [{"token_id": "m0-y", "market_id": ""},
                                                          {"token_id": "m0-n", "market_id": None}]},
}
EXPECTED_PROBLEM = {"missing": paper_cycle.MANIFEST_MISSING, "no_assets": paper_cycle.MANIFEST_NO_MARKETS,
                    "blank_market_ids": paper_cycle.MANIFEST_NO_MARKETS}


@pytest.mark.parametrize("case", sorted(BAD_MANIFESTS))
def test_a_bad_manifest_lets_no_market_signal_and_keeps_every_market_watched(tmp_path, case):
    books = [_book(m, f"{m}-{side}", ask=0.30) for m in MARKETS for side in ("y", "n")]  # every market gapped
    s = Setup(tmp_path, manifest=None, books=books)
    if BAD_MANIFESTS[case] is not None:
        s.write_manifest(BAD_MANIFESTS[case])
    result = s.cycle()
    problem = EXPECTED_PROBLEM.get(case, paper_cycle.MANIFEST_INVALID)
    assert result["gaps"] == 0 and s.gap_markets() == []  # never "all markets"
    assert result["recording"]["manifest_problem"] == problem and result["not_recorded"] == len(MARKETS)
    assert result["sentinel"]["checked"] == len(MARKETS)  # StaleWatch as before #125
    alarm = [row for row in s.signals("BookWatcher") if row["payload"].get("recording_manifest_problem") == problem
             and row["market"] == ""]
    assert len(alarm) == 1


def test_a_bad_manifest_with_missing_books_still_halts_as_before(tmp_path):
    s = Setup(tmp_path, manifest=None, books=[])
    for _ in range(2):
        result = s.cycle()
    assert sorted(result["sentinel"]["halts"] + _market_halts()) != [] and result["gaps"] == 0
    assert set(_market_halts()) == set(MARKETS)


# --- (d) open positions ----------------------------------------------------------------------------------


def test_a_held_market_outside_the_manifest_still_halts_and_never_signals(tmp_path):
    s = Setup(tmp_path, ledger=_held_ledger(tmp_path))
    for _ in range(2):
        result = s.cycle()
    assert result["recording"]["held_markets"] == 5  # m1 m2 m3 m4 m5 in the fixture
    assert _market_halts() == ["m3"]  # held + not recorded: missing_book -> StaleWatch -> market halt
    assert result["sentinel"]["checked"] == 2 and result["not_recorded"] == 3  # x0 x1 x2
    assert "m3" not in s.gap_markets()


def test_a_held_market_outside_the_manifest_with_a_fresh_book_gets_no_signal(tmp_path):
    books = [_book(m, f"{m}-{side}", ask=0.30) for m in ("m0", "m3") for side in ("y", "n")]
    s = Setup(tmp_path, books=books, ledger=_held_ledger(tmp_path))
    result = s.cycle()
    assert s.gap_markets() == ["m0"] and _market_halts() == []
    m3 = [row for row in s.signals("BookWatcher") if row["market"] == "m3"]
    assert len(m3) == 1 and m3[0]["payload"]["skip_reason"] == "not_recorded" and m3[0]["payload"]["stale"] is False
    assert result["sentinel"]["checked"] == 2


def test_a_held_market_in_the_manifest_with_a_stale_book_halts(tmp_path):
    books = [_book("m3", f"m3-{side}", ts=OLD) for side in ("y", "n")] + [_book("m0", f"m0-{s}") for s in "yn"]
    s = Setup(tmp_path, manifest=("m0", "m3"), books=books, ledger=_held_ledger(tmp_path))
    for _ in range(2):
        s.cycle()
    assert _market_halts() == ["m3"]


def test_an_unreadable_ledger_keeps_every_market_watched(tmp_path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text("{not json\n", encoding="utf-8")
    s = Setup(tmp_path, ledger=bad)
    result = s.cycle()
    assert result["recording"]["held_markets"] is None
    assert result["recording"]["ledger_problem"] == paper_cycle.LEDGER_UNREADABLE
    assert result["sentinel"]["checked"] == len(MARKETS) and s.gap_markets() == ["m0"]


def test_held_markets_reads_open_legs_only(tmp_path):
    assert paper_cycle.held_markets(_held_ledger(tmp_path)) == frozenset({"m1", "m2", "m3", "m4", "m5"})
    assert paper_cycle.held_markets(tmp_path / "none.jsonl") == frozenset()


# --- all_markets_stale -------------------------------------------------------------------------------------


def test_all_markets_stale_counts_the_manifest_markets_only(tmp_path):
    s = Setup(tmp_path, books=[_book("m0", f"m0-{side}", ts=OLD) for side in ("y", "n")])
    for _ in range(ALL_STALE_CONSECUTIVE_CYCLES):
        result = s.cycle()
    assert result["sentinel"]["checked"] == 1 and result["sentinel"]["all_stale_halt"]


def test_all_markets_stale_counts_held_markets_too(tmp_path):
    # m0 fresh (recorded), m3 held and unrecorded (missing book): not all stale
    s = Setup(tmp_path, ledger=_held_ledger(tmp_path))
    for _ in range(ALL_STALE_CONSECUTIVE_CYCLES):
        result = s.cycle()
    assert result["sentinel"]["checked"] == 2 and result["sentinel"]["all_stale_streak"] == 0
    # m0 stale too: both watched markets stale -> the global all-stale halt
    _write(s.run_dir / "books" / "books-000001.jsonl", [_book("m0", f"m0-{side}", ts=OLD) for side in ("y", "n")])
    for _ in range(ALL_STALE_CONSECUTIVE_CYCLES):
        result = s.cycle()
    assert result["sentinel"]["all_stale_halt"]


# --- without --recording-dir: unchanged (#88) ----------------------------------------------------------------


def test_without_a_recording_dir_every_market_is_watched_as_before(tmp_path):
    s = Setup(tmp_path)
    _write(tmp_path / "books.jsonl", [_book("m0", "m0-y"), _book("m0", "m0-n")])
    for _ in range(2):
        result = paper_cycle.run(markets_path=s.markets, books_path=tmp_path / "books.jsonl", bus_path=s.bus, now=NOW,
                                 sentinel_state_path=tmp_path / "state.json", quotes_path=tmp_path / "quotes.jsonl",
                                 paper_path=s.ledger)
    assert result["not_recorded"] == 0 and result["recording"] is None
    assert result["sentinel"]["checked"] == len(MARKETS)
    assert _market_halts() == ["m3", "x0", "x1", "x2"]  # missing books halt, exactly as before


def test_the_manifest_schema_is_m1_runs():
    assert paper_cycle.RECORDING_MANIFEST_SCHEMA == m1_run.SCHEMA == m1_report.RUN_SCHEMA
