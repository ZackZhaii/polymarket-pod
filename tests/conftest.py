"""Shared test isolation: no test may touch the real logs/ Sentinel state, paper quotes (Task AA), halt state (Task AL)
or heartbeats, alerts and the Sentinel tick state (Task AP)."""
from __future__ import annotations

import pytest

import src.halt_store as halt_store
import src.heartbeat as heartbeat
import src.paper_cycle as paper_cycle
import src.sentinel as sentinel
import src.sentinel_tick as sentinel_tick


@pytest.fixture(autouse=True)
def _isolate_sentinel_state(tmp_path_factory, monkeypatch):
    base = tmp_path_factory.mktemp("sentinel_state")
    monkeypatch.setattr(paper_cycle, "SENTINEL_STATE_PATH", base / "sentinel_stale.json")
    monkeypatch.setattr(paper_cycle, "QUOTES_PATH", base / "quotes.jsonl")
    # Task AL: the persisted halt state and logs/HALT fallback live in the test's own folder too.
    monkeypatch.setattr(halt_store, "HALT_STATE_PATH", base / "halt_state.json")
    monkeypatch.setattr(halt_store, "HALT_FALLBACK_PATH", base / "HALT")
    monkeypatch.setattr(sentinel, "QUOTES_PATH", base / "sentinel_quotes.jsonl")
    # Task AP: heartbeats, Sentinel alerts and the tick state stay in the test's folder too.
    monkeypatch.setattr(heartbeat, "HEARTBEAT_DIR", base / "heartbeat")
    monkeypatch.setattr(sentinel_tick, "ALERTS_PATH", base / "alerts.jsonl")
    monkeypatch.setattr(sentinel_tick, "TICK_STATE_PATH", base / "sentinel_tick.json")


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
