"""Shared test isolation: no test may touch the real logs/ Sentinel state or paper quotes (Task AA)."""
from __future__ import annotations

import pytest

import src.paper_cycle as paper_cycle


@pytest.fixture(autouse=True)
def _isolate_sentinel_state(tmp_path_factory, monkeypatch):
    base = tmp_path_factory.mktemp("sentinel_state")
    monkeypatch.setattr(paper_cycle, "SENTINEL_STATE_PATH", base / "sentinel_stale.json")
    monkeypatch.setattr(paper_cycle, "QUOTES_PATH", base / "quotes.jsonl")
