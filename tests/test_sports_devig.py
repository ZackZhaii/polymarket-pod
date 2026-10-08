"""Offline SportsDevig tests. No network, no fills, no risk-file edits."""
from __future__ import annotations

import inspect

from src.podlib import RISK_PATH, read_jsonl
from src.sports_devig import emit, fair_two_way


def test_equal_odds_devig_to_one_half_and_signal_only_on_edge(tmp_path):
    before = RISK_PATH.read_bytes()
    fair = fair_two_way(1.91, 1.91)
    assert fair is not None
    assert abs(fair[0] - 0.5) < 1e-9
    assert abs(fair[1] - 0.5) < 1e-9
    bus_path = tmp_path / "bus.jsonl"
    hit = emit(1.91, 1.91, 0.42, "match-a", bus_path=bus_path)
    assert hit is not None
    assert hit["from"] == "SportsDevig"
    assert hit["type"] == "signal"
    assert hit["payload"]["edge"] > 0
    assert hit["payload"]["locked_profit"] is False
    assert hit["payload"]["exchange_action"] == "none"
    assert emit(1.91, 1.91, 0.50, "match-b", bus_path=bus_path) is None
    assert emit(1.0, 1.91, 0.20, "bad-odds", bus_path=bus_path) is None
    assert fair_two_way(1.5, 1.5) == (0.5, 0.5)
    kinds = [row["type"] for row in read_jsonl(bus_path)]
    assert kinds == ["signal"]
    source = inspect.getsource(emit)
    assert "urlopen" not in source
    assert "fill" not in source
    assert RISK_PATH.read_bytes() == before
