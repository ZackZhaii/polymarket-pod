"""Task AK: read-only Reconciler on Data API v2. Offline: every network path raises.

Fixtures in tests/fixtures/data_api_v2/ follow a live public read of /v2/positions and /v2/trades
(2026-10-09), sanitised. Every ledger lives in tmp_path.
"""
from __future__ import annotations

import copy
import http.client
import json
import math
import socket
import sys
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from src import ledger_store, podlib, reconcile, thresholds
from src.fees import booked_fee_usd, ledger_fee

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "data_api_v2"
sys.path.insert(0, str(ROOT / "scripts"))
import guard  # noqa: E402

W = "0x1111111111111111111111111111111111111111"
TA = "1" + "0" * 75
TB = "2" + "0" * 75


def _no_network(*args, **kwargs):
    raise AssertionError("network used in an offline test")


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", _no_network)
    monkeypatch.setattr(socket, "create_connection", _no_network)
    monkeypatch.setattr(urllib.request, "urlopen", _no_network)
    monkeypatch.setattr(podlib, "urlopen", _no_network)
    monkeypatch.setattr(http.client.HTTPConnection, "connect", _no_network)
    monkeypatch.setattr(podlib, "_sleep", lambda s: None)


def page(name: str) -> dict:
    return json.loads((FIX / name).read_text(encoding="utf-8"))


class FakeApi:
    """Serves the fixture pages by route and cursor; records every URL."""

    def __init__(self, pages: dict | None = None):
        self.pages = pages or {}
        self.pages.setdefault(("status", None), page("status.json"))
        if not pages:
            self.pages.update({
            ("positions", None): page("positions_p1.json"),
            ("positions", "cG9zLTI"): page("positions_p2.json"),
            ("trades", None): page("trades_p1.json"),
            ("trades", "dHJhZGVzLTI"): page("trades_p2.json"),
            })
        self.urls: list[str] = []

    def __call__(self, url: str):
        self.urls.append(url)
        parts = urlsplit(url)
        route = parts.path.rsplit("/", 1)[-1]
        cursor = parse_qs(parts.query).get("cursor", [None])[0]
        return copy.deepcopy(self.pages[(route, cursor)])


def _fill(token, side, shares, market=None):
    price = 0.4
    notional = shares * price
    return dict(ts="2026-10-09T00:00:00Z", market=market or f"m-{token[0]}", token_id=token, side=side,
                shares=shares, notional_usd=notional, fee_rate=0.0, market_price=price, effective_price=price,
                fee_usd=booked_fee_usd(ledger_fee(0.0, price, price, notional, shares)),
                hypothetical=False, counts_toward_risk=True, risk_decision="allow")


@pytest.fixture
def ledger(tmp_path):
    path = tmp_path / "paper.jsonl"
    for fill in (_fill(TA, "buy", 10.0), _fill(TA, "sell", 4.0), _fill(TB, "buy", 20.0), _fill(TB, "buy", 5.0)):
        ledger_store.book(path, fill)
    return path


def run(ledger, api=None):
    return reconcile.reconcile(W, ledger, api or FakeApi())


# ---------------------------------------------------------------- happy path and URLs

def test_fixture_ledger_reconciles(ledger):
    api = FakeApi()
    before = ledger.read_bytes()
    report = run(ledger, api)
    assert report.ok and report.exit_code == 0 and report.problems == []
    assert (report.positions_remote, report.trades_remote, report.tokens_checked) == (2, 4, 2)
    assert report.lag_s == 7 and report.trades_deduped == 0
    assert report.pages == {"positions": 2, "trades": 2}
    assert ledger.read_bytes() == before  # read-only


def test_urls_are_exact_v2_routes_and_feed_filters_are_resent(ledger):
    api = FakeApi()
    run(ledger, api)
    assert api.urls[0] == "https://data-api.polymarket.com/v2/status"  # freshness first, every round
    assert guard.url_problem(api.urls.pop(0), code=True) is None
    parsed = [(urlsplit(u).scheme, urlsplit(u).hostname, urlsplit(u).path, parse_qs(urlsplit(u).query)) for u in api.urls]
    assert [p[:3] for p in parsed] == [("https", "data-api.polymarket.com", "/v2/positions")] * 2 + \
        [("https", "data-api.polymarket.com", "/v2/trades")] * 2
    assert parsed[0][3] == {"user": [W], "status": ["OPEN"], "filter_type": ["TOKENS"], "filter_amount": ["0.1"],
                            "limit": ["500"]}
    assert parsed[1][3]["cursor"] == ["cG9zLTI"] and "limit" not in parsed[1][3]
    assert parsed[2][3] == {"user": [W], "taker_only": ["false"], "start": ["1"], "limit": ["500"]}
    assert parsed[3][3] == {"user": [W], "taker_only": ["false"], "start": ["1"], "cursor": ["dHJhZGVzLTI"]}
    for url in api.urls:
        assert guard.url_problem(url, code=True) is None


@pytest.mark.parametrize("url", [
    "https://data-api.polymarket.com/positions?user=" + W,
    "https://data-api.polymarket.com/trades?user=" + W,
    "https://data-api.polymarket.com/closed-positions?user=" + W,
    "https://data-api.polymarket.com/v1/market-positions?user=" + W,
    "https://data-api.polymarket.com/v2/positions/combos?user=" + W,
    "https://data-api.polymarket.com/v2/activity?user=" + W,
    "https://data-api.polymarket.com/v2/value?user=" + W,
    "https://data-api.polymarket.com/v2/status/extra",
])
def test_v1_and_other_routes_are_refused_by_public_get_and_guard(url):
    with pytest.raises(RuntimeError, match="refusing URL"):
        podlib.public_get(url)
    assert guard.url_problem(url, code=True) is not None


def test_real_public_get_with_network_down_fails_closed(ledger):
    report = reconcile.reconcile(W, ledger)  # default getter: podlib.public_get, urlopen patched to raise
    assert report.exit_code == reconcile.EXIT_REMOTE
    assert report.problems == ["status_fetch_failed:AssertionError"]


def test_empty_ledger_and_empty_wallet_reconcile(tmp_path):
    empty = {"data": [], "pagination": {"limit": 500, "offset": 0, "has_more": False, "next_cursor": None}}
    api = FakeApi({("positions", None): empty, ("trades", None): empty})
    report = reconcile.reconcile(W, tmp_path / "none.jsonl", api)
    assert report.ok and report.tokens_checked == 0


# ---------------------------------------------------------------- envelope / pagination fail closed

def _api_with(route_page, mutate):
    api = FakeApi()
    body = api.pages[route_page]
    mutate(body)
    return api


ENVELOPE_CASES = [
    (("positions", None), lambda b: b.pop("data"), "positions_missing_data"),
    (("positions", None), lambda b: b.update(data=None), "positions_data_null"),
    (("positions", None), lambda b: b.update(data={}), "positions_data_not_list"),
    (("positions", None), lambda b: b.pop("pagination"), "positions_missing_pagination"),
    (("positions", None), lambda b: b["pagination"].pop("has_more"), "positions_invalid_has_more"),
    (("positions", None), lambda b: b["pagination"].update(has_more="true"), "positions_invalid_has_more"),
    (("positions", None), lambda b: b["pagination"].pop("next_cursor"), "positions_missing_next_cursor"),
    (("positions", None), lambda b: b["pagination"].update(next_cursor=""), "positions_invalid_next_cursor"),
    (("positions", None), lambda b: b["pagination"].update(next_cursor=7), "positions_invalid_next_cursor"),
    (("positions", None), lambda b: b["pagination"].update(has_more=False), "positions_pagination_inconsistent"),
    (("positions", "cG9zLTI"), lambda b: b["pagination"].update(has_more=True), "positions_pagination_inconsistent"),
    (("trades", None), lambda b: b.update(data=None), "trades_data_null"),
    (("trades", "dHJhZGVzLTI"), lambda b: b["pagination"].update(has_more=True, next_cursor="dHJhZGVzLTI"),
     "trades_cursor_loop"),
]


@pytest.mark.parametrize("where,mutate,problem", ENVELOPE_CASES)
def test_bad_envelope_fails_closed(ledger, where, mutate, problem):
    report = run(ledger, _api_with(where, mutate))
    assert report.exit_code == reconcile.EXIT_REMOTE and report.problems == [problem]
    assert not report.ok


def test_non_object_body_fails_closed(ledger):
    api = FakeApi()
    api.pages[("positions", None)] = [page("positions_p1.json")["data"]]
    assert run(ledger, api).problems == ["positions_envelope_not_object"]


def test_walk_cap_is_incomplete_not_partial(ledger, monkeypatch):
    monkeypatch.setattr(reconcile, "MAX_PAGES", 1)
    report = run(ledger)
    assert report.exit_code == reconcile.EXIT_REMOTE and report.problems == ["positions_incomplete_walk:1"]


@pytest.mark.parametrize("error", [OSError("reset"), ValueError("bad json"), RuntimeError("HTTP 400")])
def test_fetch_error_fails_closed(ledger, error):
    def boom(url):
        raise error
    report = run(ledger, boom)
    assert report.exit_code == reconcile.EXIT_REMOTE
    assert report.problems == [f"status_fetch_failed:{type(error).__name__}"]


# ---------------------------------------------------------------- row fields fail closed

BAD_VALUES = [None, "6.0", True, float("nan"), float("inf"), -1.0, [], {}]
_MISSING = object()


def _mutate_row(route_page, index, name, value):
    def mutate(body):
        if value is _MISSING:
            body["data"][index].pop(name)
        else:
            body["data"][index][name] = value
    return mutate


POSITION_FIELDS = {
    "proxy_wallet": ([_MISSING, None, "", "0x12", 5, "0x" + "2" * 40], ("position_invalid_proxy_wallet", "position_foreign_wallet")),
    "token_id": ([_MISSING, None, "", "0xabc", 12, "12a"], ("position_invalid_token_id",)),
    "condition_id": ([_MISSING, None, "", "0x" + "a" * 63, "a" * 66], ("position_invalid_condition_id",)),
    "current_size": ([_MISSING, *BAD_VALUES], ("position_invalid_current_size",)),
    "status": ([_MISSING, None, "CLOSED", "open", "MERGEABLE", 1], ("position_invalid_status",)),
}
POSITION_CASES = [(name, value, kinds) for name, (values, kinds) in POSITION_FIELDS.items() for value in values]


@pytest.mark.parametrize("name,value,kinds", POSITION_CASES, ids=[f"{n}-{i}" for i, (n, _, _) in enumerate(POSITION_CASES)])
def test_bad_position_field_fails_closed(ledger, name, value, kinds):
    report = run(ledger, _api_with(("positions", None), _mutate_row(("positions", None), 0, name, value)))
    assert report.exit_code == reconcile.EXIT_REMOTE, report.problems
    assert report.problems and all(p.split(":")[0] in kinds for p in report.problems), report.problems


TRADE_FIELDS = {
    "proxy_wallet": ([_MISSING, None, "x", "0x" + "2" * 40], ("trade_invalid_proxy_wallet", "trade_foreign_wallet")),
    "side": ([_MISSING, None, "buy", "HOLD", 1], ("trade_invalid_side",)),
    "token_id": ([_MISSING, None, "", "t", 3], ("trade_invalid_token_id",)),
    "condition_id": ([_MISSING, None, "0x1"], ("trade_invalid_condition_id",)),
    "size": ([_MISSING, 0.0, *BAD_VALUES], ("trade_invalid_size",)),
    "price": ([_MISSING, 1.5, *BAD_VALUES], ("trade_invalid_price",)),
    "timestamp": ([_MISSING, None, 0, -5, 1.5, "1791000300", True], ("trade_invalid_timestamp",)),
    "transaction_hash": ([_MISSING, None, "", "0x" + "1" * 63], ("trade_invalid_transaction_hash",)),
}
TRADE_CASES = [(name, value, kinds) for name, (values, kinds) in TRADE_FIELDS.items() for value in values]


@pytest.mark.parametrize("name,value,kinds", TRADE_CASES, ids=[f"{n}-{i}" for i, (n, _, _) in enumerate(TRADE_CASES)])
def test_bad_trade_field_fails_closed(ledger, name, value, kinds):
    report = run(ledger, _api_with(("trades", None), _mutate_row(("trades", None), 0, name, value)))
    assert report.exit_code == reconcile.EXIT_REMOTE, report.problems
    assert report.problems and all(p.split(":")[0] in kinds for p in report.problems), report.problems


def test_non_object_rows_and_duplicate_tokens_fail_closed(ledger):
    api = _api_with(("positions", None), lambda b: b["data"].append("row"))
    assert run(ledger, api).problems == ["position_not_object:1"]
    api = _api_with(("trades", None), lambda b: b["data"].append(None))
    assert run(ledger, api).problems == ["trade_not_object:2"]
    api = _api_with(("positions", "cG9zLTI"), lambda b: b["data"].append(copy.deepcopy(b["data"][0])))
    assert run(ledger, api).problems == [f"duplicate_remote_token:{TA}"]


def test_redeemable_rows_on_the_open_request_are_accepted(ledger):
    api = _api_with(("positions", None), lambda b: b["data"][0].update(status="REDEEMABLE", redeemable=True))
    assert run(ledger, api).ok


def test_unused_fields_are_not_checked(ledger):
    api = _api_with(("positions", None), lambda b: [b["data"][0].pop(k) for k in ("title", "icon", "slug", "name")])
    assert run(ledger, api).ok


# ---------------------------------------------------------------- comparison

def test_size_within_tolerance_passes_and_beyond_fails(ledger):
    tol = thresholds.RECONCILE_SHARES_TOL
    ok = _api_with(("positions", "cG9zLTI"), lambda b: b["data"][0].update(current_size=6.0 + tol / 2))
    assert run(ledger, ok).ok
    bad = _api_with(("positions", "cG9zLTI"), lambda b: b["data"][0].update(current_size=6.0 + tol * 2))
    report = run(ledger, bad)
    assert report.exit_code == reconcile.EXIT_MISMATCH and report.problems == [f"position_mismatch:{TA}"]


def test_missing_and_unexpected_positions(ledger):
    api = _api_with(("positions", "cG9zLTI"), lambda b: b["data"].clear())
    assert run(ledger, api).problems == [f"missing_remote_position:{TA}"]
    extra = "9" * 70
    api = _api_with(("positions", "cG9zLTI"), lambda b: b["data"].append(dict(b["data"][0], token_id=extra)))
    assert run(ledger, api).problems == [f"unexpected_remote_position:{extra}"]


def test_trade_flow_mismatch_both_sides(ledger):
    api = _api_with(("trades", "dHJhZGVzLTI"), lambda b: b["data"].pop(0))  # drop TB BUY 20
    assert run(ledger, api).problems == [f"trade_flow_mismatch:{TB}:BUY"]
    api = _api_with(("trades", None), lambda b: b["data"].pop(1))  # drop TA SELL 4
    assert run(ledger, api).problems == [f"trade_flow_mismatch:{TA}:SELL"]


def test_leg_below_the_positions_dust_floor_is_unverifiable(tmp_path):
    path = tmp_path / "paper.jsonl"
    ledger_store.book(path, _fill(TA, "buy", 0.05))
    trade = page("trades_p2.json")["data"][1] | {"size": 0.05}
    api = FakeApi({("positions", None): {"data": [], "pagination": {"limit": 1, "offset": 0, "has_more": False,
                                                                    "next_cursor": None}},
                   ("trades", None): {"data": [trade], "pagination": {"limit": 1, "offset": 0, "has_more": False,
                                                                       "next_cursor": None}}})
    report = reconcile.reconcile(W, path, api)
    assert report.exit_code == reconcile.EXIT_MISMATCH and report.problems == [f"position_below_data_api_floor:{TA}"]


def test_fill_below_the_trades_floor_fails_as_ledger(tmp_path):
    path = tmp_path / "paper.jsonl"
    ledger_store.book(path, _fill(TA, "buy", 0.005))
    report = reconcile.reconcile(W, path, FakeApi())
    assert report.exit_code == reconcile.EXIT_LEDGER
    assert report.problems[0].startswith("ledger_fill_below_data_api_trade_floor:")


# ---------------------------------------------------------------- ledger fails closed

def test_torn_ledger_is_exit_4_and_no_fetch(ledger):
    ledger.write_bytes(ledger.read_bytes() + b'{"side":"buy"')
    api = FakeApi()
    report = run(ledger, api)
    assert report.exit_code == reconcile.EXIT_LEDGER and "ledger_invalid:unterminated_last_row" in report.problems
    assert api.urls == []


def test_ledger_fill_without_token_is_exit_4(tmp_path):
    path = tmp_path / "paper.jsonl"
    ledger_store.book(path, _fill("", "buy", 10.0, market="m"))
    report = reconcile.reconcile(W, path, FakeApi())
    assert report.exit_code == reconcile.EXIT_LEDGER
    assert "ledger_leg_without_token_id:m" in report.problems


def test_token_in_two_markets_is_exit_4(tmp_path):
    path = tmp_path / "paper.jsonl"
    ledger_store.book(path, _fill(TA, "buy", 10.0, market="m1"))
    ledger_store.book(path, _fill(TA, "buy", 10.0, market="m2"))
    report = reconcile.reconcile(W, path, FakeApi())
    assert report.exit_code == reconcile.EXIT_LEDGER and report.problems == [f"ledger_token_in_two_markets:{TA}"]


def test_hypothetical_rows_are_ignored(ledger):
    ledger_store.book(ledger, _fill(TA, "buy", 99.0) | {"hypothetical": True, "counts_toward_risk": False})
    assert run(ledger).ok


# ---------------------------------------------------------------- CLI

def test_cli_pass_writes_no_bus(ledger, tmp_path, capsys):
    bus = tmp_path / "bus.jsonl"
    code = reconcile.main(["--user", W, "--ledger", str(ledger), "--bus", str(bus), "--json"], getter=FakeApi())
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["ok"] and out["live_enabled"] is False and out["exchange_action"] == "none"
    assert not bus.exists()


@pytest.mark.parametrize("api,code", [
    (lambda: _api_with(("positions", "cG9zLTI"), lambda b: b["data"].clear()), 1),
    (lambda: _api_with(("trades", None), lambda b: b.update(data=None)), 3),
])
def test_cli_failure_writes_one_halt(ledger, tmp_path, api, code, capsys):
    bus = tmp_path / "bus.jsonl"
    assert reconcile.main(["--user", W, "--ledger", str(ledger), "--bus", str(bus)], getter=api()) == code
    lines = [json.loads(line) for line in bus.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 1
    msg = lines[0]
    assert (msg["from"], msg["to"], msg["type"]) == ("Reconciler", "Sentinel", "halt")
    assert msg["payload"]["exchange_action"] == "none" and msg["payload"]["exit_code"] == code
    assert "reconcile FAIL" in capsys.readouterr().out


@pytest.mark.parametrize("argv", [["--user", "0x12"], ["--user", "1111111111111111111111111111111111111111"], []])
def test_cli_usage_is_exit_2_without_fetch_or_bus(tmp_path, argv, capsys):
    api = FakeApi()
    bus = tmp_path / "bus.jsonl"
    assert reconcile.main(argv + ["--bus", str(bus), "--ledger", str(tmp_path / "l.jsonl")], getter=api) == 2
    assert api.urls == [] and not bus.exists()


def test_no_order_or_key_surface_in_the_module():
    text = (ROOT / "src" / "reconcile.py").read_text(encoding="utf-8")
    for word in ("private_key", "api_key", "secret", "passphrase", "/order", "cancel", ".env"):
        assert word not in text.lower().replace("never places, signs or cancels", "")


def test_thresholds_relation():
    assert thresholds.LEDGER_OPEN_EPS <= thresholds.RECONCILE_SHARES_TOL < thresholds.DATA_API_TRADES_MIN_SHARES
    assert thresholds.DATA_API_TRADES_MIN_SHARES <= thresholds.DATA_API_POSITIONS_DUST_SHARES
    assert math.isclose(reconcile.RECONCILE_SHARES_TOL, 1e-6)


# ---------------------------------------------------------------- /v2/status first (research_dataapi_ws_m2.md)

def _status(mutate):
    api = FakeApi()
    mutate(api.pages[("status", None)]["data"])
    return api


STATUS_CASES = [
    (lambda d: d.update(age_seconds=None), ["status_invalid_age_seconds"]),
    (lambda d: d.pop("age_seconds"), ["status_invalid_age_seconds"]),
    (lambda d: d.update(age_seconds="6"), ["status_invalid_age_seconds"]),
    (lambda d: d.update(age_seconds=-1), ["status_invalid_age_seconds"]),
    (lambda d: d["serving"].update(lag_seconds=None), ["status_invalid_lag_seconds"]),
    (lambda d: d["serving"].update(lag_seconds=1.5), ["status_invalid_lag_seconds"]),
    (lambda d: d["serving"].update(lag_seconds=True), ["status_invalid_lag_seconds"]),
    (lambda d: d.pop("serving"), ["status_invalid_lag_seconds"]),
    (lambda d: d["ingestion"].update(chain_id=80002), ["status_wrong_chain"]),
    (lambda d: d["ingestion"].update(network=None), ["status_wrong_chain"]),
    (lambda d: d.pop("ingestion"), ["status_wrong_chain"]),
    (lambda d: d["serving"].update(lag_seconds=int(thresholds.DATA_API_MAX_LAG_S)),
     [f"status_lag_too_large:{int(thresholds.DATA_API_MAX_LAG_S) + 6}"]),
]


@pytest.mark.parametrize("mutate,problems", STATUS_CASES)
def test_status_unprovable_or_stale_fails_closed_before_any_wallet_read(ledger, mutate, problems):
    api = _status(mutate)
    report = run(ledger, api)
    assert report.exit_code == reconcile.EXIT_REMOTE and report.problems == problems
    assert len(api.urls) == 1  # nothing else is read


def test_status_lag_at_the_limit_passes(ledger):
    limit = int(thresholds.DATA_API_MAX_LAG_S)
    api = _status(lambda d: (d.update(age_seconds=0), d["serving"].update(lag_seconds=limit)))
    assert run(ledger, api).ok


def test_status_url_is_exact_in_public_get():
    for url in ("https://data-api.polymarket.com/v2/status?x=1", "https://data-api.polymarket.com/v2/statusx"):
        with pytest.raises(RuntimeError, match="refusing URL"):
            podlib.public_get(url)


def test_status_body_without_data_fails(ledger):
    api = FakeApi()
    api.pages[("status", None)] = {"data": None}
    assert run(ledger, api).problems == ["status_missing_data"]


# ---------------------------------------------------------------- position identities

@pytest.mark.parametrize("name", reconcile.IDENTITY_FIELDS)
@pytest.mark.parametrize("value", [_MISSING, None, "1.0", float("nan")])
def test_identity_field_missing_or_null_fails_closed(ledger, name, value):
    report = run(ledger, _api_with(("positions", None), _mutate_row(("positions", None), 0, name, value)))
    assert report.exit_code == reconcile.EXIT_REMOTE
    assert report.problems == [f"position_invalid_{name}:{TB}"]


@pytest.mark.parametrize("name,expected", [
    ("current_value", ["current_value", "unrealized_pnl"]),
    ("current_price", ["current_value"]),
    ("unrealized_pnl", ["unrealized_pnl", "total_pnl"]),
    ("realized_pnl", ["total_pnl"]),
    ("total_pnl", ["total_pnl"]),
    ("total_cost_usdc", ["total_cost_usdc"]),
    ("entry_fees_usdc", ["total_cost_usdc"]),
    ("entry_cost_usdc", ["unrealized_pnl", "total_cost_usdc"]),
])
def test_broken_identity_fails_closed(ledger, name, expected):
    bump = 10 * thresholds.DATA_API_IDENTITY_TOL_USDC
    api = _api_with(("positions", None), lambda b: b["data"][0].update({name: b["data"][0][name] + bump}))
    report = run(ledger, api)
    assert report.exit_code == reconcile.EXIT_REMOTE
    assert report.problems == [f"position_identity_broken_{n}:{TB}" for n in expected]


def test_identity_within_tolerance_passes(ledger):
    half = thresholds.DATA_API_IDENTITY_TOL_USDC / 2
    api = _api_with(("positions", None), lambda b: b["data"][0].update(total_pnl=b["data"][0]["total_pnl"] + half))
    assert run(ledger, api).ok


def test_live_shaped_redeemable_row_satisfies_the_identities():
    row = page("positions_p1.json")["data"][0] | {
        "current_size": 12000.0, "current_price": 0.0, "current_value": 0.0, "entry_cost_usdc": 4560.0,
        "entry_fees_usdc": 141.36, "total_cost_usdc": 4701.36, "realized_pnl": -141.36, "unrealized_pnl": -4560.0,
        "total_pnl": -4701.36, "status": "REDEEMABLE"}
    sizes, problems = reconcile.parse_positions([row], W)
    assert problems == [] and sizes == {TB: 12000.0}


# ---------------------------------------------------------------- trade dedupe (transaction_hash + token_id + side)

def test_exact_repeat_trade_row_counts_once(ledger):
    api = _api_with(("trades", "dHJhZGVzLTI"), lambda b: b["data"].append(copy.deepcopy(b["data"][0])))
    report = run(ledger, api)
    assert report.ok and report.trades_deduped == 1


def test_same_key_different_fill_counts_both_and_then_mismatches(ledger):
    # One transaction can fill several orders (seen live: 102.73 and 27.27 at 0.22, same tx/token/side).
    api = _api_with(("trades", "dHJhZGVzLTI"), lambda b: b["data"].append(dict(b["data"][0], size=1.0)))
    report = run(ledger, api)
    assert report.exit_code == reconcile.EXIT_MISMATCH and report.problems == [f"trade_flow_mismatch:{TB}:BUY"]
    assert report.trades_deduped == 0


def test_split_fill_matching_the_ledger_passes(ledger):
    def split(b):
        row = b["data"][0]  # TB BUY 20 -> 15 + 5 in the same transaction
        b["data"][0] = dict(row, size=15.0)
        b["data"].append(dict(row, size=5.0))
    assert run(ledger, _api_with(("trades", "dHJhZGVzLTI"), split)).ok
