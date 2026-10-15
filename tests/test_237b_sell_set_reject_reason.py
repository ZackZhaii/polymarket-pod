"""#237 remaining item 2 (dev 043e9ce): a sell-the-full-set signal (best bids summing to > 1: split 1 USDC, sell
every leg) is a multi-leg structural arb like the buy side, but paper_cycle only looks at ask sums < 1, so it emits
nothing and leaves no reject record. Expected: an explicit structural_arb_disabled reject (bus + skip), never a fill.
Offline: tmp_path files only. Style follows tests/test_structural_arb_disabled.py.
"""
from __future__ import annotations

import json

import src.paper_cycle as paper_cycle
from src.podlib import RISK_PATH, read_jsonl
from src.structural_gate import STRUCTURAL_ARB_DISABLED

NOW = "2026-10-10T12:58:19.000Z"
FRESH = "2026-10-10T12:58:00.000Z"
FUTURE = "2027-01-01T04:59:00Z"


def _write(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _run(tmp_path, books_by_market):
    markets, books, bus_path = tmp_path / "markets.jsonl", tmp_path / "books.jsonl", tmp_path / "bus.jsonl"
    _write(markets, [{"ts": FRESH, "id": m, "token_ids": [t for t, _, _ in legs], "end_date": FUTURE}
                     for m, legs in books_by_market.items()])
    _write(books, [{"ts": FRESH, "book_ts": FRESH, "market_id": m, "token_id": t, "best_bid": bid, "best_bid_size": 10,
                    "best_ask": ask, "best_ask_size": 10} for m, legs in books_by_market.items() for t, bid, ask in legs])
    before = RISK_PATH.read_bytes()
    result = paper_cycle.run(markets_path=markets, books_path=books, bus_path=bus_path, now=NOW,
                             sentinel_state_path=tmp_path / "sentinel.json", quotes_path=tmp_path / "quotes.jsonl")
    assert RISK_PATH.read_bytes() == before
    return result, (read_jsonl(bus_path) if bus_path.exists() else [])


def _structural(result, market):
    return [s for s in result["skips"] if s["market"] == market and s["kind"] == STRUCTURAL_ARB_DISABLED]


def test_binary_bid_sum_above_one_leaves_structural_reject(tmp_path):
    # A03 from the #141 vectors: YES bid 0.35 / ask 0.40, NO bid 0.70 / ask 0.80 -> bid sum 1.05, ask sum 1.20
    result, messages = _run(tmp_path, {"sellbin": [("y", 0.35, 0.40), ("n", 0.70, 0.80)]})
    assert result["ok"] is True and result["fills"] == 0
    skips = _structural(result, "sellbin")
    assert skips and skips[0]["reason"] == STRUCTURAL_ARB_DISABLED, result["skips"]
    arb = [m for m in messages if m["market"] == "sellbin" and m["payload"].get("reject_reason") == STRUCTURAL_ARB_DISABLED]
    assert arb and all(m["payload"]["rejected"] is True for m in arb)


def test_negrisk_bid_sum_above_one_leaves_structural_reject(tmp_path):
    # 3 legs, bid sum 0.36*3 = 1.08, ask sum 1.20
    result, messages = _run(tmp_path, {"sellneg": [("a", 0.36, 0.40), ("b", 0.36, 0.40), ("c", 0.36, 0.40)]})
    assert result["fills"] == 0
    skips = _structural(result, "sellneg")
    assert skips and skips[0]["reason"] == STRUCTURAL_ARB_DISABLED, result["skips"]


def test_control_bid_sum_below_one_no_signal(tmp_path):
    result, _ = _run(tmp_path, {"plain": [("y", 0.40, 0.42), ("n", 0.55, 0.60)]})  # bid 0.95, ask 1.02
    assert _structural(result, "plain") == [] and result["gaps"] == 0


def test_control_ask_sum_below_one_still_rejected(tmp_path):
    result, _ = _run(tmp_path, {"buybin": [("y", 0.40, 0.45), ("n", 0.45, 0.50)]})  # ask 0.95
    assert _structural(result, "buybin") and result["structural_rejected"] == 1
