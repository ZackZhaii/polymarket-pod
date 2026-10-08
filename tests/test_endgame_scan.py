"""Task W (COORDINATION #10): a second, read-only path that hands Endgame the 0.98-0.99 band.

The normal scanner drops prices above 0.97 as long_tail_price, so Endgame never saw its band.
endgame_scan reuses the scanner's public fetch and every other scanner rule, keeps only the
Endgame band, and hands rows to endgame.emit (a signal to RiskGate, never a fill or order).
Scanner output, filters and exit codes stay unchanged. Offline fixtures only.
"""
from __future__ import annotations

import copy
import inspect
from datetime import datetime, timezone

import pytest

import src.endgame as endgame
import src.endgame_scan as endgame_scan
import src.scanner as scanner
from src.podlib import MARKETS_PATH, RISK_PATH, read_jsonl

TS = "2026-10-08T08:00:00Z"
NOW = datetime(2026, 10, 8, 8, 0, 0, tzinfo=timezone.utc)
LIMITS = {"min_liquidity_usd": 10000.0, "max_spread": 0.04, "min_price": 0.03, "max_price": 0.97}
IN_36H = "2026-10-09T20:00:00Z"
IN_60H = "2026-10-10T20:00:00Z"
IN_12H = "2026-10-08T20:00:00Z"
BANNED = ("/order", "/auth", "api-key", "secret", "signature", "withdraw", "private_key", "commit_if_allowed")


def _band(index: int, bid: object = 0.98, ask: object = 0.99, **extra: object) -> dict:
    # Task AD: realistic Gamma rows carry both outcome prices and both token ids. The second
    # outcome's price mirrors the first-outcome mid, so it sits far below the band.
    numeric = all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in (bid, ask))
    first = round((bid + ask) / 2, 6) if numeric else 0.985
    row = {
        "id": f"eg{index}", "slug": f"eg{index}", "question": f"Endgame market {index}",
        "liquidityNum": 20000, "volume24hr": 100 + index, "bestBid": bid, "bestAsk": ask,
        "spread": 0.01, "endDate": IN_36H, "closed": False, "acceptingOrders": True, "version": "v1",
        "outcomes": '["Yes", "No"]',
        "outcomePrices": f'["{first}", "{round(1 - first, 6)}"]',
        "clobTokenIds": f'["{index}01", "{index}02"]',
    }
    row.update(extra)
    return row


def _normal(index: int) -> dict:
    return _band(1000 + index, bid=0.49, ask=0.51, endDate="2026-12-31T00:00:00Z")


def _pager(pages):
    def getter(url: str) -> dict:
        assert url.startswith("https://gamma-api.polymarket.com/markets/keyset?")
        assert not any(token in url.lower() for token in BANNED)
        cursor = url.split("after_cursor=", 1)[1] if "after_cursor=" in url else ""
        batch, nxt = pages[cursor]
        return {"markets": batch, "next_cursor": nxt}
    return getter


# --- the scanner is unchanged -----------------------------------------------------------


def test_scanner_still_excludes_the_band_and_keeps_its_constants():
    result = scanner.filter_markets([_band(1)], LIMITS, TS, now=NOW)
    assert result["passed"] == 0 and result["first_reason"] == {"long_tail_price": 1}
    assert scanner.MAX_PRICE == 0.97 and scanner.MIN_PRICE == 0.03
    assert (scanner.EXIT_OK, scanner.EXIT_TOO_FEW_SCANNED, scanner.EXIT_NONE_PASSED, scanner.EXIT_INCOMPLETE) == (0, 1, 2, 3)
    assert scanner.REASONS[-1] == "long_tail_price"


def test_endgame_path_does_not_mutate_inputs_or_scanner_output():
    raw = [_band(1), _normal(1), _normal(2)]
    snapshot, limits = copy.deepcopy(raw), dict(LIMITS)
    before = scanner.filter_markets(copy.deepcopy(raw), LIMITS, TS, now=NOW)
    endgame_scan.endgame_candidates(raw, LIMITS, TS, now=NOW)
    after = scanner.filter_markets(copy.deepcopy(raw), LIMITS, TS, now=NOW)
    assert raw == snapshot and LIMITS == limits
    assert before == after and after["passed"] == 2


# --- band selection ----------------------------------------------------------------------


def test_band_market_reaches_endgame():
    result = endgame_scan.endgame_candidates([_band(1), _normal(1)], LIMITS, TS, now=NOW)
    assert [row["id"] for row in result["rows"]] == ["eg1"]
    assert result["scanned"] == 2 and result["candidates"] == 1
    assert result["first_reason"] == {"outside_endgame_band": 1}
    assert result["band"] == [endgame.PRICE_MIN, endgame.PRICE_MAX]


@pytest.mark.parametrize(
    ("bid", "ask", "inside"),
    [(0.975, 0.985, True), (0.985, 0.995, True), (0.97, 0.98, False), (0.99, 0.995, False), (0.96, 0.97, False)],
)
def test_band_edges(bid, ask, inside):
    result = endgame_scan.endgame_candidates([_band(1, bid=bid, ask=ask)], LIMITS, TS, now=NOW)
    assert (result["candidates"] == 1) is inside, result


@pytest.mark.parametrize(
    ("extra", "reason"),
    [
        ({"closed": True}, "closed"),
        ({"version": None}, "missing_version"),
        ({"version": "v9"}, "unsupported_version"),
        ({"endDate": IN_12H}, "near_expiry"),
        ({"endDate": ""}, "missing_end_date"),
        ({"endDate": "2026-10-09"}, "invalid_end_date"),
        ({"acceptingOrders": False}, "not_accepting_orders"),
        ({"bestAsk": 1}, "missing_bid_ask"),
        ({"liquidityNum": 50}, "low_liquidity"),
        ({"spread": 0.2}, "wide_spread"),
    ],
)
def test_every_other_scanner_rule_still_applies(extra, reason):
    result = endgame_scan.endgame_candidates([_band(1, **extra)], LIMITS, TS, now=NOW)
    assert result["candidates"] == 0
    assert reason in result["any_reason"], result


@pytest.mark.parametrize(
    ("extra", "reason"),
    [
        ({"spread": "nan"}, "non_finite_spread"),
        ({"liquidityNum": "nan"}, "non_finite_liquidity_usd"),
        ({"liquidityNum": "inf"}, "non_finite_liquidity_usd"),
        ({"bestBid": "nan", "lastTradePrice": 0.985}, "non_finite_best_bid"),
        ({"bestAsk": "-inf"}, "non_finite_best_ask"),
    ],
)
def test_non_finite_numbers_fail_closed(extra, reason):
    result = endgame_scan.endgame_candidates([_band(1, **extra)], LIMITS, TS, now=NOW)
    assert result["candidates"] == 0
    assert reason in result["any_reason"], result


@pytest.mark.parametrize(
    ("extra", "reason"),
    [
        ({"spread": "NaN"}, "non_finite_spread"),
        ({"liquidityNum": "-Infinity"}, "non_finite_liquidity_usd"),
        ({"bestBid": float("nan"), "lastTradePrice": 0.985}, "non_finite_best_bid"),
        ({"bestAsk": "inf"}, "non_finite_best_ask"),
        ({"bestBid": None, "lastTradePrice": "inf"}, "non_finite_price"),
        ({"volume24hr": "nan"}, "non_finite_volume_24h_usd"),
        ({"outcomePrices": '["nan", "0.015"]'}, "non_finite_outcome_prices"),
    ],
)
def test_shared_scanner_non_finite_reasons_first_and_counted_once(extra, reason):
    """Task Y: the scanner's non_finite_<field> names come first; band_problems never doubles them."""
    result = endgame_scan.endgame_candidates([_band(1, **extra)], LIMITS, TS, now=NOW)
    assert result["candidates"] == 0
    assert result["first_reason"] == {reason: 1}, result
    assert result["any_reason"][reason] == 1, result
    assert endgame_scan.OUTSIDE_BAND not in result["any_reason"], result


def test_invalid_thresholds_fail_closed():
    with pytest.raises(RuntimeError):
        endgame_scan.endgame_candidates([_band(1)], {"min_liquidity_usd": 1.0}, TS, now=NOW)


# --- signals, never fills ------------------------------------------------------------------


def test_signals_go_to_riskgate_only_and_respect_the_window(tmp_path):
    bus_path = tmp_path / "bus.jsonl"
    rows = endgame_scan.endgame_candidates(
        [_band(1), _band(2, endDate=IN_60H)], LIMITS, TS, now=NOW
    )["rows"]
    assert len(rows) == 2
    signals = endgame_scan.emit_signals(rows, TS, bus_path=bus_path)
    assert [s["market"] for s in signals] == ["eg1"]  # 60 h is outside Endgame's 48 h window
    messages = read_jsonl(bus_path)
    assert len(messages) == 1
    message = messages[0]
    assert (message["from"], message["to"], message["type"]) == ("Endgame", "RiskGate", "signal")
    assert message["payload"]["locked_profit"] is False
    assert message["payload"]["exchange_action"] == "none"
    assert message["payload"]["price"] == pytest.approx(0.985)


def test_main_ok_writes_only_endgame_signals(tmp_path):
    before_risk = RISK_PATH.read_bytes()
    before_markets = MARKETS_PATH.read_bytes() if MARKETS_PATH.exists() else None
    raw = [_band(1), _band(2, endDate=IN_60H)] + [_normal(i) for i in range(5)]
    bus_path = tmp_path / "bus.jsonl"
    code = endgame_scan.main(getter=_pager({"": (raw[:3], "c1"), "c1": (raw[3:], "")}), bus_path=bus_path, ts=TS)
    assert code == endgame_scan.EXIT_OK == 0
    messages = read_jsonl(bus_path)
    assert [(m["from"], m["type"], m["market"]) for m in messages] == [("Endgame", "signal", "eg1")]
    assert RISK_PATH.read_bytes() == before_risk
    assert (MARKETS_PATH.read_bytes() if MARKETS_PATH.exists() else None) == before_markets


def test_main_with_no_band_market_is_ok_and_silent(tmp_path):
    bus_path = tmp_path / "bus.jsonl"
    code = endgame_scan.main(getter=_pager({"": ([_normal(i) for i in range(6)], "")}), bus_path=bus_path, ts=TS)
    assert code == endgame_scan.EXIT_OK
    assert not bus_path.exists()


def test_main_incomplete_fetch_writes_nothing(tmp_path):
    bus_path = tmp_path / "bus.jsonl"
    raw = [_band(i) for i in range(10)]
    code = endgame_scan.main(getter=_pager({"": (raw[:5], "c1"), "c1": (raw[5:], "c1")}), bus_path=bus_path, ts=TS)
    assert code == endgame_scan.EXIT_INCOMPLETE == scanner.EXIT_INCOMPLETE
    assert not bus_path.exists()


def test_main_too_few_scanned_writes_nothing(tmp_path):
    bus_path = tmp_path / "bus.jsonl"
    code = endgame_scan.main(getter=_pager({"": ([_band(1)], "")}), bus_path=bus_path, ts=TS)
    assert code == endgame_scan.EXIT_TOO_FEW_SCANNED == scanner.EXIT_TOO_FEW_SCANNED
    assert not bus_path.exists()


@pytest.mark.parametrize(("key", "value"), [("live_enabled", True), ("mode", "live"), ("max_order_pct", 0.05)])
def test_main_refuses_unless_paper_pins_hold(tmp_path, monkeypatch, key, value):
    import src.podlib as podlib

    cfg = dict(podlib.load_risk())
    cfg[key] = value
    monkeypatch.setattr(endgame_scan, "load_risk", lambda: cfg)
    calls: list[str] = []
    bus_path = tmp_path / "bus.jsonl"
    code = endgame_scan.main(getter=lambda url: calls.append(url) or {}, bus_path=bus_path, ts=TS)
    assert code == endgame_scan.EXIT_NOT_PAPER != 0
    assert calls == [] and not bus_path.exists()


def test_source_has_no_order_fill_or_key_paths():
    source = inspect.getsource(endgame_scan).lower()
    assert not any(token in source for token in BANNED)
    assert '"fill"' not in source and "persist_markets" not in source and "markets_path" not in source
