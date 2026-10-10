"""Issue #80 step 1: src/replay_intent.py — the single-shot offline replay bundle and its entry point."""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from src import replay_intent

NOW = "2026-09-01T12:00:00Z"
BOOK_TS = "2026-09-01T11:59:55.000Z"
SIGNAL = {"type": "signal", "ts": NOW, "from": "NewsForecaster", "market": "m1",
          "payload": {"forecast_id": "f-1", "model_prob": 0.47, "p_low": 0.45, "p_high": 0.49,
                      "outcome_index": 0, "token_id": "t-yes", "note": "replay fixture"}}
MARKET = {"id": "m1", "condition_id": "0xm1", "token_ids": ["t-yes", "t-no"], "outcomes": ["Yes", "No"],
          "end_date": "2026-10-15T00:00:00Z", "liquidity_usd": 52000.0, "accepting_orders": True,
          "closed": False, "tags": ["test-market"]}


def book_row(token: str, ask: float, bid: float) -> dict:
    return {"market_id": "m1", "token_id": token, "best_bid": bid, "best_ask": ask, "best_bid_size": 400.0,
            "best_ask_size": 400.0, "spread": round(ask - bid, 6), "book_ts": BOOK_TS, "tick_size": 0.01,
            "min_order_size": 5.0}


def bundle(**overrides) -> dict:
    base = {"schema_version": 1, "signal": dict(SIGNAL), "market": dict(MARKET),
            "books": [book_row("t-yes", 0.41, 0.39), book_row("t-no", 0.61, 0.59)],
            "strategy": "news-forecaster", "fee_rate": 0.0, "now": NOW}
    base.update(overrides)
    return base


def write(tmp_path: Path, payload, name: str = "bundle.json") -> Path:
    path = tmp_path / name
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    return path


def run(tmp_path: Path, capsys, payload) -> tuple[int, dict | None, str]:
    code = replay_intent.main(["--bundle", str(write(tmp_path, payload))])
    out, err = capsys.readouterr()
    return code, (json.loads(out) if out.strip() else None), err


# ---------------------------------------------------------------- the single shot

def test_one_valid_bundle_builds_one_shadow_intent(tmp_path, capsys):
    code, out, err = run(tmp_path, capsys, bundle())
    assert code == replay_intent.EXIT_OK and err == ""
    assert out["shadow"] is True and out["reasons"] == [] and out["first_reason"] is None
    assert out["intent"] is not None and out["intent"]["token_id"] == "t-yes"
    assert out["intent"]["side"] == "buy" and out["kelly_shadow"] == out["intent"]["kelly_frac"]


def test_the_same_bundle_replays_to_the_same_result(tmp_path, capsys):
    path = write(tmp_path, bundle())
    first = replay_intent.main(["--bundle", str(path)])
    out_first = capsys.readouterr().out
    assert replay_intent.main(["--bundle", str(path)]) == first == replay_intent.EXIT_OK
    assert capsys.readouterr().out == out_first


def test_a_stale_book_row_keeps_the_builders_own_reason(tmp_path, capsys):
    rows = [dict(book_row("t-yes", 0.41, 0.39), book_ts="2026-09-01T10:00:00.000Z"),
            book_row("t-no", 0.61, 0.59)]
    code, out, _ = run(tmp_path, capsys, bundle(books=rows))
    assert code == replay_intent.EXIT_OK and out["intent"] is None and "stale_book" in out["reasons"]


def test_no_book_rows_is_missing_book_not_a_default(tmp_path, capsys):
    code, out, _ = run(tmp_path, capsys, bundle(books=[]))
    assert code == replay_intent.EXIT_OK and out["intent"] is None and "missing_book" in out["reasons"]


def test_a_null_fee_rate_keeps_missing_fee_data(tmp_path, capsys):
    code, out, _ = run(tmp_path, capsys, bundle(fee_rate=None))
    assert code == replay_intent.EXIT_OK and out["intent"] is None and "missing_fee_data" in out["reasons"]


# ---------------------------------------------------------------- refusals

@pytest.mark.parametrize("payload", ["{not json", json.dumps({"schema_version": 1}),
                                     json.dumps(bundle(schema_version=2)), json.dumps(bundle(now="2026-09-01T12:00:00"))],
                         ids=["not-json", "missing-keys", "wrong-schema", "naive-now"])
def test_a_corrupt_bundle_is_refused_with_its_own_code(tmp_path, capsys, payload):
    code, out, err = run(tmp_path, capsys, payload)
    assert code == replay_intent.EXIT_CORRUPT_BUNDLE and out is None and "replay_intent:" in err


@pytest.mark.parametrize("payload",
                         [bundle(signal=[SIGNAL]), bundle(history=[{"ts": NOW}]), bundle(books=[book_row("t-x", 0.5, 0.4)])],
                         ids=["two-signals", "history", "another-market"])
def test_anything_but_one_signal_and_one_market_is_unsupported(tmp_path, capsys, payload):
    if payload["books"] and payload["books"][0]["token_id"] == "t-x":
        payload["books"][0]["market_id"] = "m2"
    code, out, err = run(tmp_path, capsys, payload)
    assert code == replay_intent.EXIT_UNSUPPORTED_BUNDLE and out is None and "replay_intent:" in err


def test_a_usage_error_is_the_entrys_own_code(tmp_path, capsys):
    assert replay_intent.main(["--bundle"]) == replay_intent.EXIT_USAGE
    assert replay_intent.main([]) == replay_intent.EXIT_USAGE
    capsys.readouterr()


# ---------------------------------------------------------------- read-only surface

def test_the_entry_has_no_execution_or_network_surface():
    tree = ast.parse(Path(replay_intent.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    called: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported |= {node.module.split(".")[0]} | {alias.name for alias in node.names}
        elif isinstance(node, ast.Call):
            called.add(ast.unparse(node.func))
    assert imported.isdisjoint({"socket", "urllib", "http", "requests", "subprocess", "risk_gate", "executor",
                                "halt_store", "sentinel", "bus", "ledger_store", "paper_ledger"})
    # No write of any kind: the replay prints one JSON object and touches no file but its bundle.
    assert called.isdisjoint({"open", "evaluate", "halt", "commit", "record_drift", "refuse_start"})
    assert not [name for name in called if name.endswith(("write_text", "write_bytes", "mkdir", "unlink", "touch"))]
