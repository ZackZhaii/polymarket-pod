"""Offline paper-cycle tests. No network, no fills, no risk-file edits."""
from __future__ import annotations

import inspect
import json

import src.paper_cycle as paper_cycle
from src.podlib import RISK_PATH, read_jsonl


def _write(path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def test_cycle_emits_gap_and_stale_signals_but_no_fills(tmp_path):
    before = RISK_PATH.read_bytes()
    markets = tmp_path / "markets.jsonl"
    books = tmp_path / "books.jsonl"
    bus_path = tmp_path / "bus.jsonl"
    now = "2026-10-08T00:10:00Z"
    fresh = "2026-10-08T00:09:30Z"
    old = "2026-10-08T00:00:00Z"
    _write(
        markets,
        [
            {"ts": fresh, "id": "binary-gap", "token_ids": ["y", "n"]},
            {"ts": fresh, "id": "multi-gap", "token_ids": ["a", "b", "c"]},
            {"ts": fresh, "id": "no-gap", "token_ids": ["p", "q"]},
            {"ts": old, "id": "stale", "token_ids": ["s1", "s2"]},
        ],
    )
    _write(
        books,
        [
            {"ts": fresh, "market_id": "binary-gap", "token_id": "y", "best_ask": 0.40, "best_ask_size": 50},
            {"ts": fresh, "market_id": "binary-gap", "token_id": "n", "best_ask": 0.40, "best_ask_size": 20},
            {"ts": fresh, "market_id": "multi-gap", "token_id": "a", "best_ask": 0.30, "best_ask_size": 10},
            {"ts": fresh, "market_id": "multi-gap", "token_id": "b", "best_ask": 0.30, "best_ask_size": 8},
            {"ts": fresh, "market_id": "multi-gap", "token_id": "c", "best_ask": 0.25, "best_ask_size": 12},
            {"ts": fresh, "market_id": "no-gap", "token_id": "p", "best_ask": 0.60, "best_ask_size": 5},
            {"ts": fresh, "market_id": "no-gap", "token_id": "q", "best_ask": 0.50, "best_ask_size": 5},
            {"ts": old, "market_id": "stale", "token_id": "s1", "best_ask": 0.60, "best_ask_size": 5},
            {"ts": old, "market_id": "stale", "token_id": "s2", "best_ask": 0.50, "best_ask_size": 5},
        ],
    )
    result = paper_cycle.run(
        markets_path=markets,
        books_path=books,
        bus_path=bus_path,
        now=now,
    )
    assert result["ok"] is True
    assert result["fills"] == 0
    assert result["gaps"] == 2
    assert result["stale"] == 1
    messages = read_jsonl(bus_path)
    kinds = [row["type"] for row in messages]
    assert "fill" not in kinds
    assert kinds.count("signal") == 3
    by_from = {row["from"] for row in messages}
    assert by_from == {"ArbBinary", "NegRiskArb", "BookWatcher"}
    binary = next(row for row in messages if row["from"] == "ArbBinary")
    assert binary["payload"]["locked_profit"] is False
    assert binary["payload"]["displayed_depth"] == 20
    assert binary["payload"]["depth_is_not_an_order"] is True
    assert RISK_PATH.read_bytes() == before
    assert "urlopen" not in inspect.getsource(paper_cycle)


def test_pin_drift_emits_nothing(tmp_path):
    before = RISK_PATH.read_bytes()
    cfg = paper_cycle.load_risk()
    cfg["live_enabled"] = True
    bus_path = tmp_path / "bus.jsonl"
    result = paper_cycle.run(markets_path=tmp_path / "markets.jsonl", books_path=tmp_path / "books.jsonl", bus_path=bus_path, cfg=cfg)
    assert result["ok"] is False
    assert result["reason"] == "phase0_pin"
    assert result["fills"] == 0
    assert not bus_path.exists()
    assert RISK_PATH.read_bytes() == before
