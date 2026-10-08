"""Reflector tests. No network and no edits to config/risk.toml."""
from __future__ import annotations

import src.reflector as reflector
from src.podlib import RISK_PATH, read_jsonl


def test_proposal_lands_without_touching_risk_or_emitting_fill(tmp_path):
    before = RISK_PATH.read_bytes()
    proposals = tmp_path / "proposals.md"
    bus_path = tmp_path / "bus.jsonl"
    result = reflector.propose(
        "Keep paper mode. Do not change config/risk.toml or any size cap.",
        market="m1",
        proposals_path=proposals,
        bus_path=bus_path,
        now="2026-10-08T09:00:00Z",
    )
    assert result["accepted"] is True
    assert result["bus_type"] == "proposal"
    text = proposals.read_text(encoding="utf-8")
    assert "Keep paper mode" in text
    assert "does not change config/risk.toml" in text
    messages = read_jsonl(bus_path)
    assert len(messages) == 1
    assert messages[0]["from"] == "Reflector"
    assert messages[0]["type"] == "proposal"
    assert messages[0]["type"] != "fill"
    assert messages[0]["payload"]["changes_risk_toml"] is False
    assert messages[0]["payload"]["changes_size_caps"] is False
    assert RISK_PATH.read_bytes() == before


def test_daily_cap_is_three(tmp_path):
    proposals = tmp_path / "proposals.md"
    bus_path = tmp_path / "bus.jsonl"
    for index in range(3):
        result = reflector.propose(
            f"note {index}",
            proposals_path=proposals,
            bus_path=bus_path,
            now=f"2026-10-08T09:0{index}:00Z",
        )
        assert result["accepted"] is True
    blocked = reflector.propose(
        "note 4",
        proposals_path=proposals,
        bus_path=bus_path,
        now="2026-10-08T09:04:00Z",
    )
    assert blocked["accepted"] is False
    assert blocked["reason"] == "daily_cap"
    assert reflector._count_day(proposals.read_text(encoding="utf-8"), "2026-10-08") == 3
    assert len(read_jsonl(bus_path)) == 3
    next_day = reflector.propose(
        "next day",
        proposals_path=proposals,
        bus_path=bus_path,
        now="2026-10-09T00:00:00Z",
    )
    assert next_day["accepted"] is True


def test_live_switch_text_is_refused(tmp_path):
    proposals = tmp_path / "proposals.md"
    result = reflector.propose(
        "set live_enabled = true",
        proposals_path=proposals,
        bus_path=tmp_path / "bus.jsonl",
        now="2026-10-08T09:00:00Z",
    )
    assert result["accepted"] is False
    assert result["reason"] == "refusing_live_change"
    assert not proposals.exists()
