"""Shared test isolation: no test may touch the real logs/ Sentinel state, paper quotes (Task AA), halt state (Task AL)
or heartbeats, alerts and the Sentinel tick state (Task AP), or sealed forecasts (Task AX)."""
from __future__ import annotations

import pytest

import src.forecaster as forecaster
import src.halt_store as halt_store
import src.heartbeat as heartbeat
import src.maker as maker
import src.paper_cycle as paper_cycle
import src.sentinel as sentinel
import src.sentinel_tick as sentinel_tick


@pytest.fixture(autouse=True)
def _isolate_sentinel_state(tmp_path_factory, monkeypatch):
    base = tmp_path_factory.mktemp("sentinel_state")
    monkeypatch.setattr(paper_cycle, "SENTINEL_STATE_PATH", base / "sentinel_stale.json")
    monkeypatch.setattr(paper_cycle, "QUOTES_PATH", base / "quotes.jsonl")
    # Issue #125: paper_cycle --recording-dir reads the paper ledger's open positions; never the real one.
    monkeypatch.setattr(paper_cycle, "PAPER_PATH", base / "paper.jsonl")
    # Issue #125: the Maker's book check reads its own books file in the test's folder (empty: no book).
    monkeypatch.setattr(maker, "BOOKS_PATH", base / "maker_books.jsonl")
    # Task AL: the persisted halt state and logs/HALT fallback live in the test's own folder too.
    monkeypatch.setattr(halt_store, "HALT_STATE_PATH", base / "halt_state.json")
    monkeypatch.setattr(halt_store, "HALT_FALLBACK_PATH", base / "HALT")
    monkeypatch.setattr(sentinel, "QUOTES_PATH", base / "sentinel_quotes.jsonl")
    # Task AP: heartbeats, Sentinel alerts and the tick state stay in the test's folder too.
    monkeypatch.setattr(heartbeat, "HEARTBEAT_DIR", base / "heartbeat")
    monkeypatch.setattr(sentinel_tick, "ALERTS_PATH", base / "alerts.jsonl")
    monkeypatch.setattr(sentinel_tick, "TICK_STATE_PATH", base / "sentinel_tick.json")
    # Task AX: sealed forecasts (logs/forecasts.jsonl) stay in the test's folder too.
    monkeypatch.setattr(forecaster, "FORECASTS_PATH", base / "forecasts.jsonl")
    # Issue #129: no Sentinel heartbeat at all is a global halt (sentinel_heartbeat_never_seen), so every
    # test starts with a resident Sentinel that has just beaten (a fixture, not a switch in the code).
    # A test about the Sentinel's absence removes it with the ``no_sentinel`` fixture.
    _sentinel_beat()


def _sentinel_beat() -> None:
    """Write one fresh, running Sentinel heartbeat (and its .seen marker) into heartbeat.HEARTBEAT_DIR."""
    beat = heartbeat.Heartbeat("sentinel", every_s=30.0)
    beat.beat(heartbeat.STATE_RUNNING, force=True)
    if beat.last_problem is not None:
        raise RuntimeError(f"test fixture could not write the Sentinel heartbeat: {beat.last_problem}")


@pytest.fixture
def no_sentinel():
    """Issue #129: remove the fixture's Sentinel heartbeat and its .seen marker: a Sentinel never run here."""
    path = heartbeat.beat_path("sentinel")
    path.unlink(missing_ok=True)
    heartbeat.state_marker(path).unlink(missing_ok=True)
    return path


@pytest.fixture
def sentinel_beat():
    """Issue #129: ``sentinel_beat()`` writes a fresh Sentinel heartbeat into the current HEARTBEAT_DIR
    (for a test that points the heartbeat folder somewhere else)."""
    return _sentinel_beat


@pytest.fixture
def fresh_book():
    """Issue #125: ``fresh_book(market, token)`` records a book stamped now for that leg in the Maker's
    books file, so a test that is about something else can still quote it."""
    import json

    from src.podlib import utc_now_ms

    def add(market: str, token: str) -> None:
        row = {"ts": utc_now_ms(), "book_ts": utc_now_ms(), "market_id": market, "token_id": token,
               "best_bid": 0.39, "best_ask": 0.41}
        with maker.BOOKS_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row) + "\n")

    return add


def pytest_addoption(parser):
    """Task AJ: the only way to rewrite tests/fixtures/e2e/golden.json. Off by default."""
    parser.addoption(
        "--update-golden",
        action="store_true",
        default=False,
        help="rewrite tests/fixtures/e2e/golden.json from this run of tests/test_e2e_offline.py; "
             "that run then fails on purpose so an update never passes silently (review the diff, rerun "
             "without the flag)",
    )
