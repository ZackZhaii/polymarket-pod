"""Task AD: endgame_scan also selects a market when its SECOND outcome is in the Endgame band.

Same band (src/thresholds.py ENDGAME_PRICE_MIN..MAX, no new numbers), same scanner rules,
same window, same minimum market count, same exit codes. Outcome 0 is scanner.yes_price,
outcome 1 is Gamma's outcomePrices[1]. Both prices are needed for the decision (fail closed);
both in the band is an inconsistent book. Signals name the outcome and its token and go to
RiskGate only. Offline fixtures only.
"""
from __future__ import annotations

import ast
import copy
import inspect
from datetime import datetime, timezone

import pytest

import src.endgame as endgame
import src.endgame_scan as endgame_scan
import src.scanner as scanner
from src.podlib import MARKETS_PATH, RISK_PATH, read_jsonl
from src.thresholds import ENDGAME_PRICE_MAX, ENDGAME_PRICE_MIN, MIN_SCANNED_MARKETS

TS = "2026-10-08T08:00:00Z"
NOW = datetime(2026, 10, 8, 8, 0, 0, tzinfo=timezone.utc)
LIMITS = {"min_liquidity_usd": 10000.0, "max_spread": 0.04, "min_price": 0.03, "max_price": 0.97}
IN_36H = "2026-10-09T20:00:00Z"
IN_60H = "2026-10-10T20:00:00Z"
IN_12H = "2026-10-08T20:00:00Z"
BANNED = ("/order", "/auth", "api-key", "secret", "signature", "withdraw", "private_key", "commit_if_allowed")


def _market(index: int, bid: object, ask: object, prices: object, **extra: object) -> dict:
    row = {
        "id": f"m{index}", "slug": f"m{index}", "question": f"Market {index}",
        "liquidityNum": 20000, "volume24hr": 100 + index, "bestBid": bid, "bestAsk": ask,
        "spread": 0.01, "endDate": IN_36H, "closed": False, "acceptingOrders": True, "version": "v1",
        "outcomes": '["Yes", "No"]', "clobTokenIds": f'["{index}01", "{index}02"]',
    }
    if prices is not None:
        row["outcomePrices"] = prices
    row.update(extra)
    return row


def _second(index: int = 1, **extra: object) -> dict:
    """First outcome at 0.015 (bid/ask mid), second outcome at 0.985: second in the band."""
    return _market(index, 0.01, 0.02, '["0.015", "0.985"]', **extra)


def _first(index: int = 1, **extra: object) -> dict:
    """First outcome at 0.985, second at 0.015: the pre-AD case."""
    return _market(index, 0.98, 0.99, '["0.985", "0.015"]', **extra)


def _normal(index: int) -> dict:
    return _market(1000 + index, 0.49, 0.51, '["0.5", "0.5"]', endDate="2026-12-31T00:00:00Z")


def _run(*raw: dict) -> dict:
    return endgame_scan.endgame_candidates(list(raw), LIMITS, TS, now=NOW)


def _pager(pages):
    def getter(url: str) -> dict:
        assert url.startswith("https://gamma-api.polymarket.com/markets/keyset?")
        assert not any(token in url.lower() for token in BANNED)
        cursor = url.split("after_cursor=", 1)[1] if "after_cursor=" in url else ""
        batch, nxt = pages[cursor]
        return {"markets": batch, "next_cursor": nxt}
    return getter


# --- selection ----------------------------------------------------------------------------


def test_second_outcome_in_band_is_selected_and_named():
    result = _run(_second(), _normal(1))
    assert result["candidates"] == 1 and result["scanned"] == 2
    row = result["rows"][0]
    assert (row["endgame_outcome_index"], row["endgame_outcome"], row["endgame_token_id"]) == (1, "No", "102")
    assert row["endgame_price"] == pytest.approx(0.985)
    assert row["price"] == pytest.approx(0.015)  # the scanner's first-outcome price is untouched
    assert result["selected_outcomes"] == {"1": 1}
    assert result["first_reason"] == {"outside_endgame_band": 1}


def test_first_outcome_selection_still_works_and_is_named():
    result = _run(_first())
    row = result["rows"][0]
    assert (row["endgame_outcome_index"], row["endgame_outcome"], row["endgame_token_id"]) == (0, "Yes", "101")
    assert row["endgame_price"] == pytest.approx(row["price"]) == pytest.approx(0.985)
    assert result["selected_outcomes"] == {"0": 1}


def test_mixed_selection_counts_each_outcome():
    result = _run(_first(1), _second(2), _second(3), _normal(1))
    assert result["selected_outcomes"] == {"0": 1, "1": 2}
    assert {row["id"]: row["endgame_outcome_index"] for row in result["rows"]} == {"m1": 0, "m2": 1, "m3": 1}


def test_v2_position_ids_name_the_second_token():
    raw = _second(version="v2", positionIds=["111", "222"])
    raw.pop("clobTokenIds")
    row = _run(raw)["rows"][0]
    assert (row["endgame_outcome_index"], row["endgame_token_id"]) == (1, "222")


def test_missing_outcome_labels_still_name_index_and_token():
    raw = _second()
    raw.pop("outcomes")
    row = _run(raw)["rows"][0]
    assert (row["endgame_outcome_index"], row["endgame_outcome"], row["endgame_token_id"]) == (1, None, "102")


@pytest.mark.parametrize(
    ("second", "inside"),
    [("0.98", True), ("0.985", True), ("0.99", True), ("0.979", False), ("0.991", False), ("0.97", False)],
)
def test_second_outcome_band_edges_are_the_same_band(second, inside):
    result = _run(_market(1, 0.01, 0.02, f'["0.015", "{second}"]'))
    assert (result["candidates"] == 1) is inside, result
    if not inside:
        assert result["first_reason"] == {"outside_endgame_band": 1}


def test_band_is_the_thresholds_band():
    assert (endgame.PRICE_MIN, endgame.PRICE_MAX) == (ENDGAME_PRICE_MIN, ENDGAME_PRICE_MAX) == (0.98, 0.99)
    assert _run(_second())["band"] == [ENDGAME_PRICE_MIN, ENDGAME_PRICE_MAX]


def test_no_new_numbers_in_the_decision():
    """The decision only uses ENDGAME_PRICE_MIN/MAX: no numeric literal other than the indices 0, 1, 2."""
    for func in (endgame_scan.band_decision, endgame_scan._in_band, endgame_scan._price):
        tree = ast.parse(inspect.getsource(func).lstrip())
        numbers = {
            node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool)
        }
        assert numbers <= {0, 1, 2}, (func.__name__, numbers)


# --- inconsistent books --------------------------------------------------------------------


def test_both_outcomes_in_band_is_rejected_not_picked(tmp_path):
    raw = _market(1, 0.98, 0.99, '["0.985", "0.985"]')
    result = _run(raw)
    assert result["candidates"] == 0
    assert result["first_reason"] == {"both_outcomes_in_band": 1}
    assert endgame_scan.BOTH_OUTCOMES_IN_BAND == "both_outcomes_in_band"
    assert endgame_scan.emit_signals(result["rows"], TS, bus_path=tmp_path / "bus.jsonl") == []


@pytest.mark.parametrize(
    "raw",
    [
        _market(1, 0.98, 0.99, '["0.985", "0.995"]'),  # first in band, second above it
        _market(1, 0.99, 0.999, '["0.9945", "0.985"]'),  # second in band, first above it
    ],
)
def test_other_outcome_above_the_band_is_rejected(raw):
    result = _run(raw)
    assert result["candidates"] == 0
    assert result["first_reason"] == {"other_outcome_above_band": 1}


# --- fail closed on prices -----------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        _market(1, 0.98, 0.99, None),  # first in band, no outcomePrices at all
        _market(1, 0.98, 0.99, '["0.985"]'),  # only one outcome price
        _market(1, 0.98, 0.99, '["0.985", "abc"]'),  # unparseable second price
        _market(1, 0.98, 0.99, '["0.985", null]'),
        _market(1, 0.49, 0.51, '["0.5"]'),  # first outside the band, second missing
        _market(1, 0.01, 0.02, "not json"),
    ],
)
def test_missing_second_price_is_missing_price(raw):
    result = _run(raw)
    assert result["candidates"] == 0
    assert result["first_reason"] == {"missing_price": 1}, result
    assert endgame_scan.MISSING_PRICE in scanner.REASONS


def test_missing_first_price_is_missing_price():
    raw = _market(1, None, None, '["", "0.985"]')
    result = _run(raw)
    assert result["candidates"] == 0
    assert result["any_reason"]["missing_price"] == 1, result


@pytest.mark.parametrize("bad", ["nan", "NaN", "inf", "-Infinity"])
def test_non_finite_second_price_fails_closed_once(bad):
    result = _run(_market(1, 0.98, 0.99, f'["0.985", "{bad}"]'))
    assert result["candidates"] == 0
    assert result["first_reason"] == {"non_finite_outcome_prices": 1}, result
    assert result["any_reason"]["non_finite_outcome_prices"] == 1
    assert endgame_scan.NON_FINITE_OUTCOME_PRICES in scanner.NON_FINITE_REASONS


def test_band_decision_rechecks_a_non_finite_second_price_itself():
    row = scanner.normalize(_second(), TS)
    row["outcome_prices"] = [0.015, float("nan")]
    assert endgame_scan.band_decision(row) == (["non_finite_outcome_prices"], None)


def test_non_finite_first_price_blocks_a_second_outcome_selection():
    raw = _second(bestBid=None, bestAsk=None, lastTradePrice="inf")
    result = _run(raw)
    assert result["candidates"] == 0
    assert result["any_reason"]["non_finite_price"] == 1, result


# --- tokens ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("extra", "drop", "reason"),
    [
        ({}, "clobTokenIds", "missing_token_ids"),
        ({"clobTokenIds": "not json"}, None, "invalid_token_ids"),
        ({"clobTokenIds": '["101", "102", "103"]'}, None, "token_count_mismatch"),
        ({"outcomes": None, "clobTokenIds": '["101"]'}, None, "missing_token_ids"),  # no token for outcome 1
    ],
)
def test_selected_outcome_without_its_token_is_rejected(extra, drop, reason):
    raw = _second(**extra)
    if drop:
        raw.pop(drop)
    result = _run(raw)
    assert result["candidates"] == 0
    assert result["first_reason"] == {reason: 1}, result


def test_first_outcome_without_a_token_is_rejected_too():
    raw = _first()
    raw.pop("clobTokenIds")
    assert _run(raw)["first_reason"] == {"missing_token_ids": 1}


# --- nothing else is loosened --------------------------------------------------------------


@pytest.mark.parametrize(
    ("extra", "reason"),
    [
        ({"closed": True}, "closed"),
        ({"version": None}, "missing_version"),
        ({"version": "v9"}, "unsupported_version"),
        ({"endDate": IN_12H}, "near_expiry"),
        ({"endDate": ""}, "missing_end_date"),
        ({"acceptingOrders": False}, "not_accepting_orders"),
        ({"bestBid": 0}, "missing_bid_ask"),
        ({"liquidityNum": 50}, "low_liquidity"),
        ({"spread": 0.2}, "wide_spread"),
        ({"spread": "nan"}, "non_finite_spread"),
    ],
)
def test_every_scanner_rule_applies_to_second_outcome_rows(extra, reason):
    result = _run(_second(**extra))
    assert result["candidates"] == 0
    assert reason in result["any_reason"], result


def test_scanner_output_is_unchanged_for_second_outcome_rows():
    raw = [_second(1), _first(2), _normal(1)]
    snapshot = copy.deepcopy(raw)
    result = scanner.filter_markets(copy.deepcopy(raw), LIMITS, TS, now=NOW)
    assert result["passed"] == 1 and result["first_reason"] == {"long_tail_price": 2}
    _run(*raw)
    assert raw == snapshot


def test_window_still_applies_to_second_outcome_signals(tmp_path):
    bus_path = tmp_path / "bus.jsonl"
    rows = _run(_second(1), _second(2, endDate=IN_60H))["rows"]
    assert len(rows) == 2
    signals = endgame_scan.emit_signals(rows, TS, bus_path=bus_path)
    assert [s["market"] for s in signals] == ["m1"]  # 60 h is outside the 48 h window


# --- signals -----------------------------------------------------------------------------------


def test_signals_name_outcome_and_token_and_go_to_riskgate_only(tmp_path):
    bus_path = tmp_path / "bus.jsonl"
    rows = _run(_first(1), _second(2))["rows"]
    endgame_scan.emit_signals(rows, TS, bus_path=bus_path)
    messages = {m["market"]: m for m in read_jsonl(bus_path)}
    assert set(messages) == {"m1", "m2"}
    for market, index, label, token in (("m1", 0, "Yes", "101"), ("m2", 1, "No", "202")):
        message = messages[market]
        assert (message["from"], message["to"], message["type"]) == ("Endgame", "RiskGate", "signal")
        payload = message["payload"]
        assert (payload["outcome_index"], payload["outcome"], payload["token_id"]) == (index, label, token)
        assert payload["price"] == pytest.approx(0.985)
        assert payload["locked_profit"] is False and payload["exchange_action"] == "none"


def test_emit_signals_skips_rows_without_a_selection(tmp_path):
    bus_path = tmp_path / "bus.jsonl"
    row = scanner.normalize(_first(), TS)
    assert endgame_scan.emit_signals([row], TS, bus_path=bus_path) == []
    assert not bus_path.exists()


def test_emit_without_outcome_keeps_the_old_payload(tmp_path):
    message = endgame.emit(0.985, "2026-10-09T00:00:00Z", "x", now=TS, bus_path=tmp_path / "bus.jsonl")
    assert set(message["payload"]) == {
        "price", "end_date", "hours_to_end", "band_low", "band_high",
        "locked_profit", "one_leg_meaning", "exchange_action",
    }


@pytest.mark.parametrize(
    ("index", "token"),
    [(1, None), (1, ""), (1, 102), (True, "102"), (-1, "102"), ("1", "102")],
)
def test_emit_with_a_bad_selection_emits_nothing(tmp_path, index, token):
    bus_path = tmp_path / "bus.jsonl"
    message = endgame.emit(
        0.985, "2026-10-09T00:00:00Z", "x", now=TS, bus_path=bus_path,
        outcome_index=index, outcome="No", token_id=token,
    )
    assert message is None and not bus_path.exists()


def test_emit_still_checks_band_and_window_for_a_selection(tmp_path):
    bus_path = tmp_path / "bus.jsonl"
    kwargs = {"now": TS, "bus_path": bus_path, "outcome_index": 1, "outcome": "No", "token_id": "102"}
    assert endgame.emit(0.979, "2026-10-09T00:00:00Z", "cheap", **kwargs) is None
    assert endgame.emit(0.991, "2026-10-09T00:00:00Z", "rich", **kwargs) is None
    assert endgame.emit(0.985, IN_60H, "late", **kwargs) is None
    assert not bus_path.exists()


# --- main: same exit codes, same minimum market count --------------------------------------------


def test_main_emits_a_second_outcome_signal(tmp_path, capsys):
    before_risk = RISK_PATH.read_bytes()
    before_markets = MARKETS_PATH.read_bytes() if MARKETS_PATH.exists() else None
    raw = [_second(1)] + [_normal(i) for i in range(5)]
    bus_path = tmp_path / "bus.jsonl"
    code = endgame_scan.main(getter=_pager({"": (raw[:3], "c1"), "c1": (raw[3:], "")}), bus_path=bus_path, ts=TS)
    assert code == endgame_scan.EXIT_OK == 0
    messages = read_jsonl(bus_path)
    assert [(m["type"], m["market"], m["payload"]["outcome_index"], m["payload"]["token_id"]) for m in messages] == [
        ("signal", "m1", 1, "102")
    ]
    assert "outcome=1:No token=102" in capsys.readouterr().out
    assert RISK_PATH.read_bytes() == before_risk
    assert (MARKETS_PATH.read_bytes() if MARKETS_PATH.exists() else None) == before_markets


def test_main_minimum_market_count_is_unchanged(tmp_path):
    bus_path = tmp_path / "bus.jsonl"
    raw = [_second(i) for i in range(MIN_SCANNED_MARKETS - 1)]
    code = endgame_scan.main(getter=_pager({"": (raw, "")}), bus_path=bus_path, ts=TS)
    assert MIN_SCANNED_MARKETS == 5
    assert code == endgame_scan.EXIT_TOO_FEW_SCANNED == scanner.EXIT_TOO_FEW_SCANNED
    assert not bus_path.exists()


def test_main_incomplete_fetch_still_writes_nothing(tmp_path):
    bus_path = tmp_path / "bus.jsonl"
    raw = [_second(i) for i in range(10)]
    code = endgame_scan.main(getter=_pager({"": (raw[:5], "c1"), "c1": (raw[5:], "c1")}), bus_path=bus_path, ts=TS)
    assert code == endgame_scan.EXIT_INCOMPLETE == scanner.EXIT_INCOMPLETE
    assert not bus_path.exists()


def test_exit_codes_are_unchanged():
    assert (endgame_scan.EXIT_OK, endgame_scan.EXIT_TOO_FEW_SCANNED, endgame_scan.EXIT_INCOMPLETE) == (0, 1, 3)


def test_source_stays_read_only():
    for func in (endgame_scan.band_decision, endgame_scan.emit_signals, endgame.emit):
        source = inspect.getsource(func).lower()
        assert "fill" not in source.replace("no fill", "").replace("never a fill", "")
        assert "urlopen" not in source
        assert not any(token in source for token in BANNED)
