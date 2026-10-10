"""Issue #141 (Task BX): multi-leg / complete-set signals are rejected (structural_arb_disabled) and expired or
resolving markets give no signal (expired / market_expired_or_resolving). Offline; no network, no orders."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import src.paper_cycle as paper_cycle
from src import arb_binary, neg_risk, scanner, structural_gate
from src.podlib import RISK_PATH, read_jsonl
from src.structural_gate import (
    MARKET_EXPIRED_OR_RESOLVING,
    OUTCOME_SET_INCOMPLETE,
    STRUCTURAL_ARB_DISABLED,
    resolution_problems,
    structural_reasons,
    uma_resolving,
)

FIXTURES = Path(__file__).parent / "fixtures"
VECTORS = json.loads((FIXTURES / "issue141_vectors.json").read_text(encoding="utf-8"))
DISPUTED = json.loads((FIXTURES / "issue141_disputed_markets.json").read_text(encoding="utf-8"))["markets"]
NOW = datetime(2026, 10, 10, 12, 58, 19, tzinfo=timezone.utc)  # the vectors' snapshot start
NOW_TEXT = "2026-10-10T12:58:19Z"


def test_vector_file_is_the_reviewed_set():
    assert VECTORS["schema"] == "issue141_vectors/1"
    assert VECTORS["now"] == NOW_TEXT
    ids = [vector["id"] for vector in VECTORS["vectors"]]
    assert ids == [f"{group}0{n}" for group in "ABCDE" for n in range(1, 6)]


@pytest.mark.parametrize("vector", VECTORS["vectors"], ids=lambda vector: vector["id"])
def test_real_vectors(vector):
    reasons = structural_reasons(legs=vector["legs"], now=NOW, markets=vector["markets"],
                                 event=vector.get("event"), event_markets=vector.get("event_markets"))
    assert reasons == vector["expected"]
    group = vector["id"][0]
    if group in "ABD":
        assert STRUCTURAL_ARB_DISABLED in reasons  # every complete set rejects, whatever its edge
    if group == "D":
        assert OUTCOME_SET_INCOMPLETE in reasons
    if group == "E":
        assert reasons == []  # a normal single leg still passes


def test_outcome_set_detail_d04():
    vector = next(item for item in VECTORS["vectors"] if item["id"] == "D04")
    detail = structural_gate.outcome_set_detail(vector["event"], vector["event_markets"])
    assert detail["inactive_outcomes"] == ["Other", "A", "B", "C", "D", "E"]
    assert detail["augmented_without_live_other"] is True
    assert detail["live_leg_count"] == 2
    assert detail["incomplete"] is True


@pytest.mark.parametrize("legs", [2, 3, 13, 0, -1, None, "2", 2.0, True])
def test_any_multi_leg_or_unknown_leg_count_rejects(legs):
    assert structural_reasons(legs=legs, now=NOW) == [STRUCTURAL_ARB_DISABLED]  # type: ignore[arg-type]


def test_single_leg_without_market_passes():
    assert structural_reasons(legs=1, now=NOW) == []


@pytest.mark.parametrize(("status", "statuses", "resolving"), [
    (None, None, False),
    ("", None, False),
    ("   ", None, False),
    (None, "[]", False),  # the ruling: "[]" with a null scalar is not rejected
    (None, "", False),
    (None, [], False),
    ("proposed", None, True),
    ("disputed", "[]", True),
    ("resolved", None, True),
    (None, '["proposed"]', True),
    (None, '["proposed", "disputed"]', True),
    (None, "not json", True),  # unparseable: fail closed
    (None, "{}", True),
    (None, '"proposed"', True),
    (None, ["proposed"], True),
    (None, 7, True),
    (7, None, True),
])
def test_uma_resolving(status, statuses, resolving):
    assert uma_resolving(status, statuses) is resolving


def test_resolution_problems_both_and_boundary():
    both = {"endDate": "2026-10-10T03:59:00Z", "umaResolutionStatus": "proposed"}
    assert resolution_problems(both, NOW) == ["expired", MARKET_EXPIRED_OR_RESOLVING]
    assert resolution_problems({"end_date": NOW_TEXT}, NOW) == ["expired"]  # end == now is expired
    assert resolution_problems({"end_date": "2026-10-10T12:58:20Z"}, NOW) == []
    assert resolution_problems({"end_date": "garbage"}, NOW) == ["invalid_end_date"]
    assert resolution_problems({"end_date": "2026-10-10T12:58:20Z"}, None) == ["invalid_now"]  # type: ignore[arg-type]
    assert resolution_problems({}, NOW) == []
    assert resolution_problems({"uma_resolution_statuses": "[oops"}, NOW) == [MARKET_EXPIRED_OR_RESOLVING]


@pytest.mark.parametrize("market", DISPUTED, ids=lambda market: market["id"])
def test_scanner_rejects_disputed_markets_that_used_to_pass(market):
    assert sorted(item["id"] for item in DISPUTED) == ["1796138", "2412362", "2412367", "2747434"]
    limits = scanner.load_thresholds()
    row = scanner.normalize(market, NOW_TEXT)
    assert row["uma_resolution_status"] == "disputed"
    assert scanner.rejection_reasons(row, limits, NOW) == [MARKET_EXPIRED_OR_RESOLVING]
    bare = scanner.normalize({k: v for k, v in market.items() if not k.startswith("uma")}, NOW_TEXT)
    assert scanner.rejection_reasons(bare, limits, NOW) == []  # the UMA status is the only reason
    result = scanner.filter_markets([market], limits, NOW_TEXT, now=NOW)
    assert result["passed"] == 0
    assert result["first_reason"] == {MARKET_EXPIRED_OR_RESOLVING: 1}


def test_scanner_reason_order_and_statuses_only():
    assert scanner.REASONS.index("invalid_now") + 1 == scanner.REASONS.index(MARKET_EXPIRED_OR_RESOLVING)
    assert scanner.REASONS.index(MARKET_EXPIRED_OR_RESOLVING) + 1 == scanner.REASONS.index("not_accepting_orders")
    market = dict(DISPUTED[0])
    market["umaResolutionStatus"] = None
    market["umaResolutionStatuses"] = "[]"
    assert scanner.rejection_reasons(scanner.normalize(market, NOW_TEXT), scanner.load_thresholds(), NOW) == []
    market["umaResolutionStatuses"] = "{bad"
    assert scanner.rejection_reasons(scanner.normalize(market, NOW_TEXT), scanner.load_thresholds(),
                                     NOW) == [MARKET_EXPIRED_OR_RESOLVING]
    market["umaResolutionStatuses"] = "[]"
    market["endDate"] = "2026-10-01T00:00:00Z"
    market["umaResolutionStatus"] = "proposed"
    reasons = scanner.rejection_reasons(scanner.normalize(market, NOW_TEXT), scanner.load_thresholds(), NOW)
    assert reasons[:2] == ["expired", MARKET_EXPIRED_OR_RESOLVING]


def test_stub_gap_records_are_rejects(tmp_path):
    bus_path = tmp_path / "bus.jsonl"
    for message in (arb_binary.emit_gap(0.40, 0.40, "m-bin", bus_path=bus_path),
                    neg_risk.emit_gap([0.3, 0.3, 0.3], "m-neg", bus_path=bus_path)):
        assert message is not None
        payload = message["payload"]
        assert payload["rejected"] is True
        assert payload["reject_reason"] == STRUCTURAL_ARB_DISABLED
        assert payload["gap_signal"] == "rejected"
        assert payload["exchange_action"] == "none"
        assert payload["locked_profit"] is False
    assert len(read_jsonl(bus_path)) == 2


def _write(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def test_paper_cycle_rejects_gaps_and_skips_resolving_markets(tmp_path):
    before = RISK_PATH.read_bytes()
    markets, books, bus_path = tmp_path / "markets.jsonl", tmp_path / "books.jsonl", tmp_path / "bus.jsonl"
    now = "2026-10-10T12:58:19.000Z"
    fresh = "2026-10-10T12:58:00.000Z"
    future = "2027-01-01T04:59:00Z"
    _write(markets, [
        {"ts": fresh, "id": "bin", "token_ids": ["y", "n"], "end_date": future},
        {"ts": fresh, "id": "neg", "token_ids": ["a", "b", "c"], "end_date": future},
        {"ts": fresh, "id": "disputed", "token_ids": ["d1", "d2"], "end_date": future,
         "uma_resolution_status": "disputed", "uma_resolution_statuses": '["proposed", "disputed"]'},
        {"ts": fresh, "id": "past", "token_ids": ["p1", "p2"], "end_date": "2026-10-10T03:59:00Z",
         "uma_resolution_status": "proposed"},
        {"ts": fresh, "id": "badjson", "token_ids": ["j1", "j2"], "end_date": future,
         "uma_resolution_status": None, "uma_resolution_statuses": "[oops"},
        {"ts": fresh, "id": "emptylist", "token_ids": ["e1", "e2"], "end_date": future,
         "uma_resolution_status": None, "uma_resolution_statuses": "[]"},
    ])
    rows = []
    for market, tokens in (("bin", "yn"), ("neg", "abc"), ("disputed", ["d1", "d2"]), ("past", ["p1", "p2"]),
                           ("badjson", ["j1", "j2"]), ("emptylist", ["e1", "e2"])):
        for token in tokens:
            rows.append({"ts": fresh, "book_ts": fresh, "market_id": market, "token_id": token,
                         "best_ask": 0.30, "best_ask_size": 10})
    _write(books, rows)
    result = paper_cycle.run(markets_path=markets, books_path=books, bus_path=bus_path, now=now,
                             sentinel_state_path=tmp_path / "sentinel.json", quotes_path=tmp_path / "quotes.jsonl")
    assert result["ok"] is True and result["fills"] == 0
    # bin, neg and emptylist have a complete-set gap: all rejected, none passed on.
    assert result["gaps"] == 3 and result["structural_rejected"] == 3
    assert result["resolving_markets"] == 3
    by_kind = {}
    for skip in result["skips"]:
        by_kind.setdefault(skip["kind"], []).append(skip)
    assert sorted(item["market"] for item in by_kind[STRUCTURAL_ARB_DISABLED]) == ["bin", "emptylist", "neg"]
    assert {item["market"]: item["reasons"] for item in by_kind["resolving_market"]} == {
        "disputed": [MARKET_EXPIRED_OR_RESOLVING],
        "past": ["expired", MARKET_EXPIRED_OR_RESOLVING],
        "badjson": [MARKET_EXPIRED_OR_RESOLVING],
    }
    messages = read_jsonl(bus_path)
    assert "fill" not in {row["type"] for row in messages}
    arb = [row for row in messages if row["from"] in ("ArbBinary", "NegRiskArb")]
    assert sorted(row["market"] for row in arb) == ["bin", "emptylist", "neg"]
    assert all(row["payload"]["reject_reason"] == STRUCTURAL_ARB_DISABLED for row in arb)
    skipped = {row["market"]: row["payload"] for row in messages if row["from"] == "BookWatcher"}
    assert skipped["past"]["reasons"] == ["expired", MARKET_EXPIRED_OR_RESOLVING]
    assert skipped["disputed"]["gap_signal"] == "skipped"
    assert RISK_PATH.read_bytes() == before
