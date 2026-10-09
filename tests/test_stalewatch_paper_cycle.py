"""Task AA: Sentinel StaleWatch wired into paper_cycle with a persisted streak file. Offline, paper only."""
from __future__ import annotations

import json
import os

import pytest

import src.maker as maker
import src.paper_cycle as paper_cycle
import src.podlib as podlib
import src.sentinel as sentinel
from src.podlib import RISK_PATH, read_jsonl

NOW1 = "2026-10-09T00:10:00Z"
NOW2 = "2026-10-09T00:11:00Z"
NOW3 = "2026-10-09T00:12:00Z"


def _write(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


@pytest.fixture()
def paths(tmp_path):
    paths = {name: tmp_path / f"{name}.jsonl" for name in ("markets", "books", "bus", "quotes")}
    paths["state"] = tmp_path / "state" / "sentinel_stale.json"
    _write(paths["markets"], [
        {"ts": "2026-10-09T00:00:00Z", "id": "fresh", "token_ids": ["f1", "f2"]},
        {"ts": "2026-10-09T00:00:00Z", "id": "stale", "token_ids": ["s1", "s2"]},
    ])
    # Task AL gap 10: a market with no book row is now a stale check (test_markets_without_any_book_*
    # adds "unbooked" to markets.jsonl itself); its paper quote stays here as a bystander.
    for market, token in (("fresh", "f1"), ("stale", "s1"), ("unbooked", "u1")):
        maker.quote(market=market, token_id=token, side="buy", price=0.40, size=5,
                    bus_path=paths["bus"], quotes_path=paths["quotes"])
    return paths


def _books(paths, now_fresh: str, stale_ts: str = "2026-10-09T00:00:00Z", recorded: str = "2026-10-09T00:09:00Z"):
    rows = []
    for token, ask in (("f1", 0.40), ("f2", 0.40)):
        rows.append({"ts": recorded, "book_ts": now_fresh, "market_id": "fresh", "token_id": token,
                     "best_ask": ask, "best_ask_size": 10})
    for token in ("s1", "s2"):
        rows.append({"ts": recorded, "book_ts": stale_ts, "market_id": "stale", "token_id": token,
                     "best_ask": 0.60, "best_ask_size": 10})
    with paths["books"].open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def _run(paths, now, **kwargs):
    return paper_cycle.run(markets_path=paths["markets"], books_path=paths["books"], bus_path=paths["bus"],
                           now=now, sentinel_state_path=paths["state"], quotes_path=paths["quotes"], **kwargs)


def _quotes(paths):
    return {row["market"]: row["status"] for row in read_jsonl(paths["quotes"])}


def _halts(paths):
    return [row for row in read_jsonl(paths["bus"]) if row["type"] == "halt"]


def _stale_signals(paths):
    return [row for row in read_jsonl(paths["bus"]) if row["type"] == "signal" and row["payload"].get("stale")]


def test_default_state_path_is_under_logs_and_tests_are_isolated():
    assert podlib.SENTINEL_STATE_PATH == podlib.LOG_DIR / "sentinel_stale.json"
    assert paper_cycle.SENTINEL_STATE_PATH != podlib.SENTINEL_STATE_PATH  # conftest isolation
    assert paper_cycle.QUOTES_PATH != podlib.QUOTES_PATH


def test_first_run_counts_one_and_does_not_halt(paths):
    _books(paths, "2026-10-09T00:09:50Z")
    result = _run(paths, NOW1)
    assert (result["gaps"], result["stale"], result["fills"]) == (1, 1, 0)
    assert result["sentinel"] == {"checked": 2, "state_problem": None, "write_problem": None, "halts": [],
                                  "state_path": str(paths["state"]), "all_stale_streak": 0, "all_stale_halt": False}
    saved = json.loads(paths["state"].read_text(encoding="utf-8"))
    assert saved["streaks"] == {"stale": 1} and saved["alarmed"] == []
    assert sentinel.state_marker(paths["state"]).exists()
    assert _halts(paths) == []
    assert _quotes(paths) == {"fresh": "open", "stale": "open", "unbooked": "open"}


def test_second_stale_run_halts_that_market_only_and_per_check_signals_stay(paths):
    _books(paths, "2026-10-09T00:09:50Z")
    first = _run(paths, NOW1)
    _books(paths, "2026-10-09T00:10:50Z", recorded="2026-10-09T00:10:55Z")
    second = _run(paths, NOW2)
    assert second["sentinel"]["halts"] == ["stale"]
    assert (first["gaps"], first["stale"]) == (second["gaps"], second["stale"]) == (1, 1)
    assert len(_stale_signals(paths)) == 2  # one per stale check, as before
    halts = _halts(paths)
    assert len(halts) == 1
    assert halts[0]["from"] == "Sentinel" and halts[0]["market"] == "stale"
    assert halts[0]["payload"]["exchange_action"] == "none"
    assert halts[0]["payload"]["reason"].startswith("stale_book_consecutive: 2 consecutive")
    assert _quotes(paths) == {"fresh": "open", "stale": "cancelled", "unbooked": "open"}
    assert "fill" not in {row["type"] for row in read_jsonl(paths["bus"])}
    third = _run(paths, NOW3)
    assert third["sentinel"]["halts"] == []  # one halt per streak
    assert json.loads(paths["state"].read_text(encoding="utf-8"))["streaks"] == {"stale": 3}


def test_a_fresh_book_resets_the_streak(paths):
    _books(paths, "2026-10-09T00:09:50Z")
    _run(paths, NOW1)
    _books(paths, "2026-10-09T00:10:50Z", stale_ts="2026-10-09T00:10:40Z", recorded="2026-10-09T00:10:55Z")
    assert _run(paths, NOW2)["sentinel"]["halts"] == []
    _books(paths, "2026-10-09T00:11:50Z", recorded="2026-10-09T00:11:55Z")
    assert _run(paths, NOW3)["sentinel"]["halts"] == []
    assert json.loads(paths["state"].read_text(encoding="utf-8"))["streaks"] == {"stale": 1}


def test_markets_without_any_book_are_stale_and_halt(paths):
    """Task AL gap 10 (was: not checked). No book row = a stale check; two in a row halt that market."""
    with paths["markets"].open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"ts": "2026-10-09T00:00:00Z", "id": "unbooked", "token_ids": ["u1", "u2"]}) + "\n")
    _books(paths, "2026-10-09T00:09:50Z")
    first = _run(paths, NOW1)
    assert first["sentinel"]["checked"] == 3 and first["sentinel"]["halts"] == []
    assert "unbooked" in {s["market"] for s in first["skips"] if s.get("kind") == "stale_market"}
    assert json.loads(paths["state"].read_text(encoding="utf-8"))["streaks"]["unbooked"] == 1
    _books(paths, "2026-10-09T00:10:50Z", recorded="2026-10-09T00:10:55Z")
    second = _run(paths, NOW2)
    assert second["sentinel"]["halts"] == ["stale", "unbooked"]
    assert _quotes(paths)["unbooked"] == "cancelled"


def test_a_market_with_one_unbooked_leg_is_stale(paths):
    """Task AL gap 10: a missing leg no longer drops out quietly; the market gets no gap signal."""
    rows = [{"ts": "2026-10-09T00:09:00Z", "book_ts": "2026-10-09T00:09:50Z", "market_id": "fresh",
             "token_id": "f1", "best_ask": 0.40, "best_ask_size": 10}]
    _write(paths["books"], rows)
    result = _run(paths, NOW1)
    fresh = [s for s in result["skips"] if s.get("market") == "fresh" and s.get("kind") == "stale_market"]
    assert fresh and fresh[0]["stale_reasons"] == [paper_cycle.MISSING_BOOK]
    assert result["gaps"] == 0


@pytest.mark.parametrize(
    ("content", "problem"),
    [
        ("{not json", sentinel.STATE_CORRUPT),
        ("", sentinel.STATE_CORRUPT),
        ("[]", sentinel.STATE_MALFORMED),
        ('{"max_age_s": 120.0, "consecutive": 2, "streaks": {"stale": -1}, "alarmed": []}', sentinel.STATE_MALFORMED),
        ('{"max_age_s": NaN, "consecutive": 2, "streaks": {}, "alarmed": []}', sentinel.STATE_MALFORMED),
        ('{"max_age_s": 600.0, "consecutive": 2, "streaks": {}, "alarmed": []}', sentinel.STATE_MALFORMED),
        ('{"max_age_s": 120.0, "consecutive": 9, "streaks": {}, "alarmed": []}', sentinel.STATE_MALFORMED),
        ('{"max_age_s": 120.0, "consecutive": 2, "streaks": {}, "alarmed": ["ghost"]}', sentinel.STATE_MALFORMED),
    ],
)
def test_corrupt_or_malformed_state_counts_as_stale(paths, content, problem):
    paths["state"].parent.mkdir(parents=True, exist_ok=True)
    paths["state"].write_text(content, encoding="utf-8")
    _books(paths, "2026-10-09T00:09:50Z")
    result = _run(paths, NOW1)
    assert result["sentinel"]["state_problem"] == problem
    # Bad state (1) + a stale book (2): halt in this run. The fresh market resets after its bad-state count.
    assert result["sentinel"]["halts"] == ["stale"]
    assert _quotes(paths) == {"fresh": "open", "stale": "cancelled", "unbooked": "open"}
    saved = json.loads(paths["state"].read_text(encoding="utf-8"))  # rewritten valid
    assert saved["streaks"] == {"stale": 2} and saved["alarmed"] == ["stale"]
    assert (result["gaps"], result["stale"]) == (1, 1)


def test_state_with_invalid_utf8_counts_as_stale(paths):
    paths["state"].parent.mkdir(parents=True, exist_ok=True)
    paths["state"].write_bytes(b"\xff\xfe\x00garbage")
    _books(paths, "2026-10-09T00:09:50Z")
    result = _run(paths, NOW1)
    assert result["sentinel"]["state_problem"] == sentinel.STATE_UNREADABLE
    assert result["sentinel"]["halts"] == ["stale"]


def test_state_file_missing_after_a_prior_run_counts_as_stale(paths):
    _books(paths, "2026-10-09T00:09:50Z", stale_ts="2026-10-09T00:09:40Z")
    first = _run(paths, NOW1)
    assert first["sentinel"]["state_problem"] is None and first["sentinel"]["halts"] == []
    paths["state"].unlink()
    _books(paths, "2026-10-09T00:10:50Z", recorded="2026-10-09T00:10:55Z")
    second = _run(paths, NOW2)
    assert second["sentinel"]["state_problem"] == sentinel.STATE_MISSING
    assert second["sentinel"]["halts"] == ["stale"]


def test_first_run_without_file_or_marker_starts_at_zero(paths):
    assert not paths["state"].exists() and not sentinel.state_marker(paths["state"]).exists()
    _books(paths, "2026-10-09T00:09:50Z")
    result = _run(paths, NOW1)
    assert result["sentinel"]["state_problem"] is None
    assert result["sentinel"]["halts"] == []


def test_unreadable_state_path_counts_as_stale(paths):
    paths["state"].mkdir(parents=True)  # a directory where the file should be: read and write both fail
    _books(paths, "2026-10-09T00:09:50Z")
    result = _run(paths, NOW1)
    assert result["sentinel"]["state_problem"] == sentinel.STATE_UNREADABLE
    assert result["sentinel"]["write_problem"] == sentinel.STATE_WRITE_FAILED
    assert result["sentinel"]["halts"] == ["stale"]
    assert _quotes(paths)["fresh"] == "open"


def test_write_failure_counts_as_stale_and_leaves_the_old_file(paths, monkeypatch):
    _books(paths, "2026-10-09T00:09:50Z", stale_ts="2026-10-09T00:09:40Z")
    _run(paths, NOW1)
    before = paths["state"].read_bytes()

    real_replace = os.replace

    def broken_replace(src, dst):
        # Only the StaleWatch state write fails (Task AL: halt records and quote cancels use os.replace too).
        if os.fspath(dst) == os.fspath(paths["state"]):
            raise OSError("disk full")
        return real_replace(src, dst)

    monkeypatch.setattr(sentinel.os, "replace", broken_replace)
    _books(paths, "2026-10-09T00:10:50Z", recorded="2026-10-09T00:10:55Z")
    result = _run(paths, NOW2)
    assert result["sentinel"]["write_problem"] == sentinel.STATE_WRITE_FAILED
    # Stale book (1) + lost write (2): halt now; the fresh market only reaches 1.
    assert result["sentinel"]["halts"] == ["stale"]
    assert _quotes(paths) == {"fresh": "open", "stale": "cancelled", "unbooked": "open"}
    assert paths["state"].read_bytes() == before  # never half-written
    assert not [item for item in paths["state"].parent.iterdir() if item.name.endswith(".tmp")]


def test_save_is_atomic_and_leaves_no_temp_files(paths, monkeypatch):
    calls = []
    real_replace = os.replace

    def spy(src, dst):
        if os.fspath(dst) != os.fspath(paths["state"]):
            return real_replace(src, dst)  # Task AL: halt state / quote rewrites are not this test's subject
        calls.append((os.path.dirname(src), os.fspath(dst)))
        assert json.loads(open(src, encoding="utf-8").read())["consecutive"] == 2
        return real_replace(src, dst)

    monkeypatch.setattr(sentinel.os, "replace", spy)
    _books(paths, "2026-10-09T00:09:50Z")
    _run(paths, NOW1)
    assert calls == [(str(paths["state"].parent), str(paths["state"]))]
    assert sorted(item.name for item in paths["state"].parent.iterdir()) == [
        "sentinel_stale.json", "sentinel_stale.json.seen"]


def test_a_looser_stale_after_s_never_resets_the_sentinel(paths):
    """stale_after_s=300 keeps its old per-check meaning, but Sentinel still judges at 120 s."""
    _books(paths, "2026-10-09T00:09:50Z", stale_ts="2026-10-09T00:06:30Z")  # 210 s old at NOW1
    first = _run(paths, NOW1, stale_after_s=300)
    assert first["stale"] == 0  # per-check behaviour unchanged for this caller
    _books(paths, "2026-10-09T00:10:50Z", stale_ts="2026-10-09T00:07:30Z", recorded="2026-10-09T00:10:55Z")
    second = _run(paths, NOW2, stale_after_s=300)
    assert second["stale"] == 0
    assert second["sentinel"]["halts"] == ["stale"]


def test_missing_or_future_book_ts_count_as_stale(paths):
    rows = [
        {"ts": "2026-10-09T00:09:00Z", "book_ts": None, "book_ts_problem": "missing_book_ts",
         "market_id": "stale", "token_id": "s1", "best_ask": 0.6},
        {"ts": "2026-10-09T00:09:00Z", "book_ts": "2026-10-09T00:30:00Z", "market_id": "stale", "token_id": "s2",
         "best_ask": 0.5},
    ]
    _write(paths["books"], rows)
    assert _run(paths, NOW1)["sentinel"]["halts"] == []
    # "fresh" has no book rows in this test: Task AL gap 10 makes it stale (missing_book) too.
    assert _run(paths, NOW2)["sentinel"]["halts"] == ["fresh", "stale"]


def test_pin_drift_returns_before_touching_state(paths):
    cfg = paper_cycle.load_risk()
    cfg["live_enabled"] = True
    before = RISK_PATH.read_bytes()
    result = _run(paths, NOW1, cfg=cfg)
    assert result["reason"] == "phase0_pin"
    assert not paths["state"].exists()
    assert RISK_PATH.read_bytes() == before


def test_record_treats_odd_problems_as_failed(tmp_path):
    watch = sentinel.StaleWatch(bus_path=tmp_path / "bus.jsonl", quotes_path=tmp_path / "quotes.jsonl")
    assert watch.record("m", "")["problem"] == sentinel.CHECK_FAILED
    assert watch.record("m", 0)["halt"] is not None  # type: ignore[arg-type]
