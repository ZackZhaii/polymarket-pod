"""Issue #117 (Task BZ): per-market fee rate from Gamma (Gamma-only slice). Offline: getters are injected and
a socket tripwire fails the test on any network attempt."""
from __future__ import annotations

import json
import math
import socket
from pathlib import Path

import pytest

from src import fee_source as fs
from src import thresholds
from tests.test_intent_builder import build, market

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "fees" / "gamma_fee_markets.json").read_text(encoding="utf-8"))
REAL = {row["slug"]: row for row in FIXTURE["markets"]}
POLITICS = REAL["will-luiz-incio-lula-da-silva-win-the-2026-brazilian-presidential-election"]  # rate 0.04
CRYPTO = REAL["bitcoin-above-84k-on-october-10-2026"]  # rate 0.07
FREE = REAL["us-x-iran-ceasefire-continues-through-october-12"]  # feesEnabled false, no feeSchedule
ZERO = REAL["republican-senate-odds-hit-50-by-october-31"]  # feesEnabled true, rate 0


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("network attempted")
    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def _with(base, **changes):
    row = json.loads(json.dumps(base))
    schedule = changes.pop("schedule", None)
    if schedule:
        row["feeSchedule"] = {**row["feeSchedule"], **schedule}
    for key, value in changes.items():
        if value is fs:  # sentinel: drop the key
            row.pop(key, None)
        else:
            row[key] = value
    return row


def test_real_markets_resolve():
    assert fs.parse_gamma_fee(POLITICS) == (0.04, None)
    assert fs.parse_gamma_fee(CRYPTO) == (0.07, None)
    assert fs.parse_gamma_fee(FREE) == (0.0, None)
    assert fs.parse_gamma_fee(ZERO) == (0.0, None)
    for row in FIXTURE["markets"]:
        rate, problem = fs.parse_gamma_fee(row)
        assert problem is None and rate is not None
        if row["feesEnabled"]:
            assert row["takerBaseFee"] == 1000 and rate == row["feeSchedule"]["rate"]  # tbf is never the rate


@pytest.mark.parametrize(("row", "problem"), [
    (_with(POLITICS, feesEnabled=fs), fs.FEES_ENABLED_UNKNOWN),
    (_with(POLITICS, feesEnabled=None), fs.FEES_ENABLED_UNKNOWN),
    (_with(POLITICS, feesEnabled="true"), fs.FEES_ENABLED_UNKNOWN),
    (_with(POLITICS, feesEnabled=1), fs.FEES_ENABLED_UNKNOWN),
    (_with(POLITICS, feeSchedule=fs), fs.FEE_SCHEDULE_MISSING),
    (_with(POLITICS, feeSchedule=None), fs.FEE_SCHEDULE_MISSING),
    (_with(POLITICS, feeSchedule="0.04"), fs.FEE_SCHEDULE_MISSING),
    (_with(POLITICS, schedule={"rate": math.nan}), fs.FEE_RATE_INVALID),
    (_with(POLITICS, schedule={"rate": math.inf}), fs.FEE_RATE_INVALID),
    (_with(POLITICS, schedule={"rate": "0.04"}), fs.FEE_RATE_INVALID),
    (_with(POLITICS, schedule={"rate": None}), fs.FEE_RATE_INVALID),
    (_with(POLITICS, schedule={"rate": True}), fs.FEE_RATE_INVALID),
    (_with(POLITICS, schedule={"rate": -0.01}), fs.FEE_RATE_OUT_OF_RANGE),
    (_with(POLITICS, schedule={"rate": 0.2500001}), fs.FEE_RATE_OUT_OF_RANGE),
    (_with(POLITICS, schedule={"rate": 10}), fs.FEE_RATE_OUT_OF_RANGE),
    (_with(POLITICS, schedule={"exponent": 0.5}), fs.FEE_SCHEDULE_UNSUPPORTED),
    (_with(POLITICS, schedule={"exponent": None}), fs.FEE_SCHEDULE_UNSUPPORTED),
    (_with(POLITICS, schedule={"exponent": math.nan}), fs.FEE_SCHEDULE_UNSUPPORTED),
    (_with(POLITICS, schedule={"takerOnly": False}), fs.FEE_NOT_TAKER_ONLY),
    (_with(POLITICS, schedule={"takerOnly": "true"}), fs.FEE_NOT_TAKER_ONLY),
    (_with(FREE, feeSchedule={"rate": 0.04, "exponent": 1, "takerOnly": True}), fs.FEE_SCHEDULE_ON_FREE_MARKET),
    (_with(FREE, feeSchedule="x"), fs.FEE_SCHEDULE_ON_FREE_MARKET),
    (_with(FREE, feeSchedule={"rate": "x"}), fs.FEE_RATE_INVALID),
])
def test_unusable_fees_are_rejected(row, problem):
    assert fs.parse_gamma_fee(row) == (None, problem)
    quote = fs.resolve_fee("m", row)
    assert quote.fee_rate is None and quote.problem == problem and quote.cross_checked is False


def test_free_market_with_null_or_zero_schedule_is_zero():
    assert fs.parse_gamma_fee(_with(FREE, feeSchedule=None)) == (0.0, None)
    assert fs.parse_gamma_fee(_with(FREE, feeSchedule={"rate": None, "exponent": None})) == (0.0, None)
    assert fs.parse_gamma_fee(_with(FREE, feeSchedule={"rate": 0})) == (0.0, None)


def test_boundaries_and_basefee_are_ignored():
    assert fs.parse_gamma_fee(_with(POLITICS, schedule={"rate": thresholds.FEE_RATE_MAX})) == (0.25, None)
    assert fs.parse_gamma_fee(_with(POLITICS, schedule={"exponent": 1.0})) == (0.04, None)
    # tbf/mbf 1000 (a signing cap) with rate 0.04 is 0.04; changing them changes nothing
    assert fs.parse_gamma_fee(_with(POLITICS, takerBaseFee=0, makerBaseFee=0)) == (0.04, None)


@pytest.mark.parametrize("raw", [None, "not json", ["list"], 3])
def test_unreadable_gamma_response(raw):
    quote = fs.resolve_fee("m", raw, fetched_at="t")
    assert quote == fs.FeeQuote("m", None, fs.GAMMA_UNREADABLE, fee_gamma_fetched_at="t")


def test_quote_records_source_and_schedule_sha():
    quote = fs.resolve_fee("601819", POLITICS, fetched_at="2026-10-10T13:00:00.000Z")
    assert quote.fee_rate == 0.04 and quote.fee_source == "gamma"
    assert quote.fee_gamma_fetched_at == "2026-10-10T13:00:00.000Z"
    assert quote.fee_schedule_sha == fs.schedule_sha(POLITICS) and len(quote.fee_schedule_sha) == 64
    assert fs.schedule_sha(_with(POLITICS, schedule={"rate": 0.05})) != quote.fee_schedule_sha


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_cache_ttl_negative_ttl_and_invalidate():
    calls = []
    answers = {"good": POLITICS, "bad": _with(POLITICS, feesEnabled=None)}

    def getter(market_id):
        calls.append(market_id)
        return answers[market_id]

    clock = _Clock()
    cache = fs.FeeCache(getter, clock=clock, wall=lambda: "w")
    assert cache.fee_rate("good") == 0.04 and cache.fee_rate("good") == 0.04
    assert cache.fee_rate("bad") is None and cache.fee_rate("bad") is None
    assert calls == ["good", "bad"]
    clock.t = thresholds.FEE_NEG_CACHE_S  # the negative entry expires, the good one does not
    cache.get("good"), cache.get("bad")
    assert calls == ["good", "bad", "bad"]
    clock.t = thresholds.FEE_CACHE_TTL_S
    cache.get("good")
    assert calls[-1] == "good" and len(calls) == 4
    cache.invalidate_all()
    cache.get("good")
    assert len(calls) == 5


def test_expired_quote_is_never_a_fallback():
    state = {"fail": False}

    def getter(market_id):
        if state["fail"]:
            raise TimeoutError("gamma timeout")
        return CRYPTO

    clock = _Clock()
    cache = fs.FeeCache(getter, clock=clock)
    assert cache.fee_rate("x") == 0.07
    state["fail"] = True
    assert cache.fee_rate("x") == 0.07  # still inside the TTL
    clock.t = thresholds.FEE_CACHE_TTL_S + 1
    quote = cache.get("x")
    assert quote.fee_rate is None and quote.problem == fs.GAMMA_UNREADABLE


@pytest.mark.parametrize("row", [_with(POLITICS, feesEnabled=None), _with(POLITICS, schedule={"exponent": 0.5}), None])
def test_rejected_fee_rejects_the_intent(row):
    """The consumer: MarketContext.fee_rate from FeeCache. A rejected fee is None -> missing_fee_data, no intent."""
    cache = fs.FeeCache(lambda market_id: row)
    result = build(mkt=market(fee_rate=cache.fee_rate("m")))
    assert result.intent is None and "missing_fee_data" in result.reasons


def test_usable_fee_feeds_the_intent():
    cache = fs.FeeCache(lambda market_id: FREE)
    result = build(mkt=market(fee_rate=cache.fee_rate("m")))
    assert "missing_fee_data" not in result.reasons
