"""Offline scanner tests. Fixture JSON only. No network, no orders, no risk-file edits."""
from __future__ import annotations

import inspect
import json
from datetime import datetime, timezone
from pathlib import Path

import src.scanner as scanner
from src.podlib import RISK_PATH, read_jsonl

FIXTURE = Path(__file__).parent / "fixtures" / "gamma_markets.json"
TS = "2026-10-08T08:00:00Z"
NOW = datetime(2026, 10, 8, 8, 0, 0, tzinfo=timezone.utc)
LIMITS = {"min_liquidity_usd": 10000.0, "max_spread": 0.04, "min_price": 0.03, "max_price": 0.97}
BANNED_URL_TOKENS = ("/order", "/auth", "api-key", "secret", "signature", "withdraw")


def _raw() -> list[dict]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _passing(index: int, volume: float) -> dict:
    return {
        "id": f"p{index}",
        "slug": f"p{index}",
        "question": f"Synthetic passing market {index}",
        "liquidityNum": 20000,
        "volume24hr": volume,
        "bestBid": 0.49,
        "bestAsk": 0.51,
        "spread": 0.02,
        "endDate": "2026-12-31T00:00:00Z",
        "closed": False,
        "acceptingOrders": True,
    }


def _pager(pages: dict[str, tuple[list[dict], str]], seen_urls: list[str] | None = None):
    def getter(url: str) -> dict:
        assert url.startswith("https://gamma-api.polymarket.com/markets/keyset?")
        assert not any(token in url.lower() for token in BANNED_URL_TOKENS)
        if seen_urls is not None:
            seen_urls.append(url)
        cursor = url.split("after_cursor=", 1)[1] if "after_cursor=" in url else ""
        batch, nxt = pages[cursor]
        return {"markets": batch, "next_cursor": nxt}

    return getter


def _write_risk(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "risk.toml"
    path.write_text('mode = "paper"\nlive_enabled = false\n' + body, encoding="utf-8")
    return path


def _expect_runtime_error(path: Path, needle: str) -> None:
    try:
        scanner.load_thresholds(path)
    except RuntimeError as exc:
        assert needle in str(exc), str(exc)
    else:
        raise AssertionError(f"expected RuntimeError mentioning {needle!r}")


# ---- thresholds -------------------------------------------------------------

def test_thresholds_come_from_risk_toml_and_file_is_untouched():
    before = RISK_PATH.read_bytes()
    limits = scanner.load_thresholds()
    assert limits["min_liquidity_usd"] == 10000.0
    assert limits["max_spread"] == 0.04
    assert limits["min_price"] == scanner.MIN_PRICE == 0.03
    assert limits["max_price"] == scanner.MAX_PRICE == 0.97
    assert scanner.thresholds_from(scanner.load_risk()) == limits  # one parser, same answer
    assert RISK_PATH.read_bytes() == before
    assert b"live_enabled = false" in before


def test_optional_price_band_overrides(tmp_path):
    path = _write_risk(
        tmp_path,
        "min_liquidity_usd = 5000\nmax_spread = 0.02\nscanner_min_price = 0.05\nscanner_max_price = 0.95\n",
    )
    assert scanner.load_thresholds(path) == {
        "min_liquidity_usd": 5000.0,
        "max_spread": 0.02,
        "min_price": 0.05,
        "max_price": 0.95,
    }


def test_missing_keys_fail_closed(tmp_path):
    _expect_runtime_error(_write_risk(tmp_path, "max_spread = 0.02\n"), "min_liquidity_usd")
    _expect_runtime_error(_write_risk(tmp_path, "min_liquidity_usd = 10000\n"), "max_spread")


def test_reversed_or_equal_price_band_fails_closed(tmp_path):
    base = "min_liquidity_usd = 10000\nmax_spread = 0.04\n"
    _expect_runtime_error(_write_risk(tmp_path, base + "scanner_min_price = 0.9\nscanner_max_price = 0.1\n"), "price band")
    _expect_runtime_error(_write_risk(tmp_path, base + "scanner_min_price = 0.5\nscanner_max_price = 0.5\n"), "price band")
    _expect_runtime_error(_write_risk(tmp_path, base + "scanner_max_price = 1.5\n"), "price band")


def test_non_positive_max_spread_fails_closed(tmp_path):
    _expect_runtime_error(_write_risk(tmp_path, "min_liquidity_usd = 10000\nmax_spread = 0\n"), "max_spread")
    _expect_runtime_error(_write_risk(tmp_path, "min_liquidity_usd = 10000\nmax_spread = -0.01\n"), "max_spread")
    _expect_runtime_error(_write_risk(tmp_path, 'min_liquidity_usd = 10000\nmax_spread = "wide"\n'), "numbers")


def test_scanner_uses_one_risk_parser():
    source = inspect.getsource(scanner)
    assert "tomllib" not in source
    assert "parse_risk_toml" in source


# ---- filtering ----------------------------------------------------------------

def test_filter_passes_sorts_and_counts_reasons():
    result = scanner.filter_markets(_raw(), LIMITS, TS, now=NOW)
    assert result["scanned"] == 13
    assert result["passed"] == 3
    assert [row["id"] for row in result["rows"]] == ["ok-high", "edge-spread", "ok-low"]
    assert result["first_reason"] == {
        "low_liquidity": 1,
        "wide_spread": 1,
        "long_tail_price": 2,
        "expired": 1,
        "closed": 1,
        "not_accepting_orders": 1,
        "missing_bid_ask": 2,
        "missing_end_date": 1,
    }
    # "expired" is also thin, so it shows up twice in any_reason but once in first_reason.
    assert result["any_reason"]["low_liquidity"] == 2
    # "wide", the one-sided book, and "no-book" (unknown spread fails closed).
    assert result["any_reason"]["wide_spread"] == 3
    assert sum(result["first_reason"].values()) == result["scanned"] - result["passed"]
    top = result["rows"][0]
    assert top["price_source"] == "mid"
    assert abs(top["price"] - 0.55) < 1e-9
    assert abs(top["days_to_expiry"] - (datetime(2026, 12, 31, tzinfo=timezone.utc) - NOW).total_seconds() / 86400) < 1e-9


def test_spread_and_price_fallbacks():
    raw = {row["id"]: row for row in _raw()}
    ok_low = scanner.normalize(raw["ok-low"], TS)
    assert ok_low["liquidity_usd"] == 12000.5
    assert abs(ok_low["spread"] - 0.03) < 1e-9  # bestAsk - bestBid
    no_book = scanner.normalize(raw["no-book"], TS)
    assert no_book["price_source"] == "last_trade" and no_book["price"] == 0.5
    only_prices = {"outcomePrices": "[\"0.25\", \"0.75\"]"}
    assert scanner.yes_price(only_prices) == (0.25, "outcome_prices_0")
    assert scanner.yes_price({}) == (None, "none")


# ---- pagination ---------------------------------------------------------------

def test_keyset_pagination_uses_cursor_and_respects_cap():
    raw = _raw()
    pages = {"": (raw[:5], "c1"), "c1": (raw[5:10], "c2"), "c2": (raw[10:], "")}
    seen_urls: list[str] = []
    rows, meta = scanner.fetch_all_markets(_pager(pages, seen_urls), page_limit=5)
    assert len(rows) == 13 and meta["pages"] == 3
    assert meta["cap_hit"] is False and meta["complete"] is True and meta["stop_reason"] == "exhausted"
    assert "active=true" in seen_urls[0] and "closed=false" in seen_urls[0]
    assert "liquidity_num_min" not in seen_urls[0]
    seen_urls.clear()
    rows, meta = scanner.fetch_all_markets(_pager(pages, seen_urls), max_markets=7, page_limit=5, server_min_liquidity=10000)
    assert len(rows) == 7 and meta["pages"] == 2
    assert meta["cap_hit"] is True and meta["complete"] is False and meta["stop_reason"] == "max_markets"
    assert all("liquidity_num_min=10000" in url for url in seen_urls)
    assert "after_cursor=c1" in seen_urls[1]


def test_max_pages_is_a_cap_hit():
    pages = {"": ([_passing(0, 1)], "c1"), "c1": ([_passing(1, 1)], "c2"), "c2": ([_passing(2, 1)], "")}
    rows, meta = scanner.fetch_all_markets(_pager(pages), max_pages=2)
    assert len(rows) == 2 and meta["pages"] == 2
    assert meta["cap_hit"] is True and meta["stop_reason"] == "max_pages"


def test_repeated_cursor_stops_instead_of_spinning():
    calls = {"n": 0}

    def getter(url: str) -> dict:
        calls["n"] += 1
        # New rows every time, but the server keeps handing back the same cursor.
        return {"markets": [_passing(calls["n"], 1)], "next_cursor": "stuck"}

    rows, meta = scanner.fetch_all_markets(getter)
    assert calls["n"] == 2
    assert len(rows) == 2
    assert meta["stop_reason"] == "cursor_repeat"
    assert meta["complete"] is False and meta["cap_hit"] is False


def test_cursor_cycle_is_detected():
    pages = {"": ([_passing(0, 1)], "a"), "a": ([_passing(1, 1)], "b"), "b": ([_passing(2, 1)], "a")}
    rows, meta = scanner.fetch_all_markets(_pager(pages))
    assert meta["pages"] == 3 and len(rows) == 3
    assert meta["stop_reason"] == "cursor_repeat"


def test_page_with_no_new_markets_stops():
    same = [_passing(0, 1), _passing(1, 1)]
    calls = {"n": 0}

    def getter(url: str) -> dict:
        calls["n"] += 1
        return {"markets": same, "next_cursor": f"c{calls['n']}"}  # fresh cursor, stale rows

    rows, meta = scanner.fetch_all_markets(getter)
    assert calls["n"] == 2
    assert len(rows) == 2
    assert meta["stop_reason"] == "no_new_rows"
    assert meta["complete"] is False


# ---- persistence / bus cap ------------------------------------------------------

def test_bus_gets_top_50_and_markets_file_gets_everything(tmp_path):
    raw = [_passing(index, volume=float(index)) for index in range(80)]
    result = scanner.scan(raw_markets=raw, limits=LIMITS, ts=TS)
    assert result["passed"] == 80
    markets = tmp_path / "markets.jsonl"
    bus_path = tmp_path / "bus.jsonl"
    written = scanner.persist_markets(result["rows"], markets, bus_path, bus_limit=scanner.BUS_TOP_N)
    assert written == 80
    assert len(read_jsonl(markets)) == 80
    messages = read_jsonl(bus_path)
    assert len(messages) == scanner.BUS_TOP_N == 50
    assert [m["market"] for m in messages] == [f"p{index}" for index in range(79, 29, -1)]
    assert {m["from"] for m in messages} == {"Scanner"}
    assert {m["type"] for m in messages} == {"signal"}


def test_persist_without_limit_keeps_legacy_one_signal_per_row(tmp_path):
    rows = scanner.visible_rows([_passing(index, 1) for index in range(3)], TS)
    bus_path = tmp_path / "bus.jsonl"
    assert scanner.persist_markets(rows, tmp_path / "markets.jsonl", bus_path) == 3
    assert len(read_jsonl(bus_path)) == 3


# ---- main() exit codes ----------------------------------------------------------

def _run_main(tmp_path: Path, pages: dict, **kwargs) -> tuple[int, Path, Path]:
    markets = tmp_path / "markets.jsonl"
    bus_path = tmp_path / "bus.jsonl"
    code = scanner.main(getter=_pager(pages), markets_path=markets, bus_path=bus_path, ts=TS, **kwargs)
    return code, markets, bus_path


def test_main_ok_writes_full_file_and_capped_bus(tmp_path):
    before = RISK_PATH.read_bytes()
    raw = [_passing(index, volume=float(index)) for index in range(60)] + _raw()
    pages = {"": (raw[:40], "c1"), "c1": (raw[40:], "")}
    code, markets, bus_path = _run_main(tmp_path, pages)
    assert code == scanner.EXIT_OK == 0
    assert len(read_jsonl(markets)) == 63  # 60 synthetic + 3 fixture passes
    assert len(read_jsonl(bus_path)) == 50
    assert RISK_PATH.read_bytes() == before


def test_main_returns_nonzero_when_nothing_passes(tmp_path):
    raw = [row for row in _raw() if row["id"] not in {"ok-high", "ok-low", "edge-spread"}]
    code, markets, bus_path = _run_main(tmp_path, {"": (raw, "")})
    assert code == scanner.EXIT_NONE_PASSED != 0
    assert not markets.exists() and not bus_path.exists()


def test_main_returns_nonzero_when_cap_is_hit(tmp_path):
    raw = [_passing(index, 1) for index in range(10)]
    pages = {"": (raw[:5], "c1"), "c1": (raw[5:], "")}
    code, markets, bus_path = _run_main(tmp_path, pages, max_markets=5)
    assert code == scanner.EXIT_INCOMPLETE != 0
    assert not markets.exists() and not bus_path.exists()


def test_main_returns_nonzero_when_paging_stalls(tmp_path):
    raw = [_passing(index, 1) for index in range(10)]
    pages = {"": (raw[:5], "c1"), "c1": (raw[5:], "c1")}
    code, markets, _ = _run_main(tmp_path, pages)
    assert code == scanner.EXIT_INCOMPLETE
    assert not markets.exists()


def test_main_returns_nonzero_when_too_few_scanned(tmp_path):
    code, markets, _ = _run_main(tmp_path, {"": ([_passing(0, 1)], "")})
    assert code == scanner.EXIT_TOO_FEW_SCANNED != 0
    assert not markets.exists()


# ---- end to end / safety --------------------------------------------------------

def test_scan_end_to_end_offline_and_signals_only(tmp_path):
    before = RISK_PATH.read_bytes()
    result = scanner.scan(raw_markets=_raw(), ts=TS)
    assert result["passed"] == 3
    markets = tmp_path / "markets.jsonl"
    bus_path = tmp_path / "bus.jsonl"
    assert scanner.persist_markets(result["rows"], markets, bus_path, bus_limit=scanner.BUS_TOP_N) == 3
    messages = read_jsonl(bus_path)
    assert {m["from"] for m in messages} == {"Scanner"}
    assert {m["type"] for m in messages} == {"signal"}
    assert RISK_PATH.read_bytes() == before


def test_scanner_source_has_no_keys_or_order_paths():
    source = inspect.getsource(scanner)
    assert source.startswith('"""Phase 0 read-only Gamma scanner. No keys. No orders.')
    for token in ("py_clob_client", "POLY_PRIVATE_KEY", "private_key", "os.environ", "load_dotenv", "\"fill\""):
        assert token not in source
    assert "write_text" not in source and "write_bytes" not in source
