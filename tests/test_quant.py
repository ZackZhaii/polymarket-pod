"""Offline Brier tests. No network, no credentials, no risk-file edits."""
from __future__ import annotations

import inspect

from src.podlib import RISK_PATH, read_jsonl
from src.quant import FIXTURE, brier, propose


def test_brier_is_mean_squared_error():
    score = brier(({"p": 0.70, "y": 1}, {"p": 0.20, "y": 0}))
    assert score == ((0.70 - 1) ** 2 + (0.20 - 0) ** 2) / 2


def test_fixture_emits_one_proposal_and_blocks_a_second(tmp_path):
    before = RISK_PATH.read_bytes()
    bus_path = tmp_path / "bus.jsonl"
    first = propose(FIXTURE, bus_path=bus_path)
    second = propose(FIXTURE, bus_path=bus_path)
    assert first["accepted"] is True
    assert first["bus_type"] == "proposal"
    assert first["brier"] == brier(FIXTURE)
    assert second["accepted"] is False
    assert second["reason"] == "already_proposed"
    messages = read_jsonl(bus_path)
    assert len(messages) == 1
    assert messages[0]["from"] == "QuantResearcher"
    assert messages[0]["type"] == "proposal"
    assert messages[0]["payload"]["changes_risk_toml"] is False
    assert messages[0]["payload"]["changes_size_caps"] is False
    assert messages[0]["payload"]["exchange_action"] == "none"
    assert "fill" not in [row["type"] for row in messages]
    source = inspect.getsource(propose)
    assert "urlopen" not in source
    assert "risk.toml" not in source or "does not change config/risk.toml" in source
    assert RISK_PATH.read_bytes() == before


def test_bad_rows_do_not_emit(tmp_path):
    bus_path = tmp_path / "bus.jsonl"
    for rows in (
        (),
        ({"p": 1.2, "y": 1},),
        ({"p": 0.4, "y": 2},),
        ({"p": True, "y": 1},),
    ):
        try:
            propose(rows, bus_path=bus_path)
        except ValueError:
            continue
        raise AssertionError(rows)
    assert not bus_path.exists()
