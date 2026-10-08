"""Shared test isolation: no test may touch the real logs/ Sentinel state or paper quotes (Task AA)."""
from __future__ import annotations

import pytest

import src.paper_cycle as paper_cycle


@pytest.fixture(autouse=True)
def _isolate_sentinel_state(tmp_path_factory, monkeypatch):
    base = tmp_path_factory.mktemp("sentinel_state")
    monkeypatch.setattr(paper_cycle, "SENTINEL_STATE_PATH", base / "sentinel_stale.json")
    monkeypatch.setattr(paper_cycle, "QUOTES_PATH", base / "quotes.jsonl")


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
