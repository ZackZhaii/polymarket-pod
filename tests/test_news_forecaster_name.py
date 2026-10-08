"""Task N: the Forecaster bus name matches ROSTER (NewsForecaster). Offline, tmp bus files only."""
from __future__ import annotations

import json

import pytest

import src.forecaster as forecaster
from src.podlib import ROLE_ALIASES, ROOT, bus, canonical_role, read_jsonl


def _roster_roles() -> set[str]:
    roles = set()
    for line in (ROOT / "docs" / "ROSTER.md").read_text(encoding="utf-8").splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if line.startswith("|") and cells and cells[0] not in {"Role", ""} and not set(cells[0]) <= {"-"}:
            roles.add(cells[0])
    return roles


def test_forecaster_bus_name_is_the_roster_name():
    assert forecaster.BUS_NAME == "NewsForecaster"
    assert forecaster.BUS_NAME in _roster_roles()
    assert "Forecaster" not in _roster_roles()


def test_emit_probability_uses_news_forecaster(tmp_path):
    bus_path = tmp_path / "bus.jsonl"
    message = forecaster.emit_probability(market="m1", model_prob=0.42, bus_path=bus_path)
    assert message["from"] == "NewsForecaster"
    assert message["to"] == "RiskGate"
    assert message["payload"]["order"] is False
    assert [row["from"] for row in read_jsonl(bus_path)] == ["NewsForecaster"]


def test_canonical_role_maps_only_the_legacy_name():
    assert ROLE_ALIASES == {"Forecaster": "NewsForecaster"}
    assert canonical_role("Forecaster") == "NewsForecaster"
    assert canonical_role("NewsForecaster") == "NewsForecaster"
    for name in ("Scanner", "RiskGate", "Executor", "QuantResearcher", "forecaster", ""):
        assert canonical_role(name) == name


def test_legacy_caller_is_written_with_the_canonical_name(tmp_path):
    """An older script still passing "Forecaster" keeps working; the bus line says NewsForecaster."""
    bus_path = tmp_path / "bus.jsonl"
    message = bus("Forecaster", "RiskGate", "signal", "m1", {"model_prob": 0.4, "order": False}, path=bus_path)
    assert message["from"] == "NewsForecaster"
    to_legacy = bus("Reflector", "Forecaster", "proposal", "m1", {"note": "x"}, path=bus_path)
    assert to_legacy["to"] == "NewsForecaster"
    assert {row["from"] for row in read_jsonl(bus_path)} == {"NewsForecaster", "Reflector"}


def test_old_bus_lines_match_through_canonical_role(tmp_path):
    """Existing logs/bus.jsonl lines are not rewritten; readers compare via canonical_role."""
    bus_path = tmp_path / "bus.jsonl"
    old = {"ts": "2026-10-08T00:00:00Z", "from": "Forecaster", "to": "RiskGate", "type": "signal",
           "market": "m0", "payload": {"model_prob": 0.3, "order": False}}
    bus_path.write_text(json.dumps(old) + "\n", encoding="utf-8")
    forecaster.emit_probability(market="m1", model_prob=0.42, bus_path=bus_path)
    rows = read_jsonl(bus_path)
    assert rows[0]["from"] == "Forecaster"
    hits = [row["market"] for row in rows if canonical_role(row["from"]) == forecaster.BUS_NAME]
    assert hits == ["m0", "m1"]


def test_news_forecaster_still_cannot_fill(tmp_path):
    bus_path = tmp_path / "bus.jsonl"
    for name in ("NewsForecaster", "Forecaster"):
        with pytest.raises(RuntimeError, match="only Executor"):
            bus(name, "Sentinel", "fill", "m1", {"risk_decision": "allow"}, path=bus_path)
    assert not bus_path.exists()
