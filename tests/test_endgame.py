"""Offline Endgame tests. No network, no fills, no risk-file edits."""
from __future__ import annotations

import inspect

from src.endgame import emit
from src.podlib import RISK_PATH, read_jsonl

NOW = "2026-10-08T00:00:00Z"


def test_band_and_window_emit_one_signal(tmp_path):
    before = RISK_PATH.read_bytes()
    bus_path = tmp_path / "bus.jsonl"
    hit = emit(0.985, "2026-10-09T00:00:00Z", "near", now=NOW, bus_path=bus_path)
    assert hit is not None
    assert hit["type"] == "signal"
    assert hit["from"] == "Endgame"
    assert hit["payload"]["locked_profit"] is False
    assert hit["payload"]["exchange_action"] == "none"
    assert emit(0.98, "2026-10-10T00:00:00Z", "low-edge", now=NOW, bus_path=bus_path) is not None
    assert emit(0.99, "2026-10-08T12:00:00Z", "high-edge", now=NOW, bus_path=bus_path) is not None
    assert emit(0.979, "2026-10-09T00:00:00Z", "cheap", now=NOW, bus_path=bus_path) is None
    assert emit(0.991, "2026-10-09T00:00:00Z", "rich", now=NOW, bus_path=bus_path) is None
    assert emit(0.985, "2026-10-10T00:00:01Z", "late", now=NOW, bus_path=bus_path) is None
    assert emit(0.985, "2026-10-07T23:00:00Z", "past", now=NOW, bus_path=bus_path) is None
    kinds = [row["type"] for row in read_jsonl(bus_path)]
    assert kinds == ["signal", "signal", "signal"]
    assert "fill" not in kinds
    source = inspect.getsource(emit)
    assert "fill" not in source
    assert "urlopen" not in source
    assert RISK_PATH.read_bytes() == before
