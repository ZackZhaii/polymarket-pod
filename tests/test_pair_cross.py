"""Issue #145 (Task CH): complementary-leg self-trade (MINT_PAIR_CROSS / MERGE_PAIR_CROSS).

On Polymarket a YES buy and a NO buy pair (mint) when their prices sum to >= 1, and a YES sell and a NO sell
pair (merge) when they sum to <= 1. A taker order on one token against our own resting quote on the other
token is therefore a self-trade. Offline; tmp_path files only.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import src.maker as maker
import src.paper_ledger as paper_ledger
from src.podlib import load_risk, read_jsonl
from src.risk_gate import Intent, evaluate
from src.thresholds import PAIR_CROSS_TOLERANCE
from src.wash_detect import (
    MARKET_PAIR_KEY,
    MERGE_PAIR_CROSS,
    MINT_PAIR_CROSS,
    WASH_INPUT_INVALID,
    binary_pair,
    pair_cross_problems,
    wash_problems,
)

NOW = datetime(2026, 10, 9, 0, 0, 0, tzinfo=timezone.utc)
MARKET = "ch-market"
YES, NO = "tok-yes", "tok-no"
CLEAN = "momentum-v2"


def _iso(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def _intent(**overrides: object) -> Intent:
    fields: dict[str, object] = {"strategy": CLEAN, "market": MARKET, "token_id": NO, "side": "buy",
                                 "model_prob": 0.80, "market_price": 0.41, "assumed_slippage": 0.005,
                                 "fee_rate": 0.0, "liquidity_usd": 20000.0, "spread": 0.01, "hypothetical": False,
                                 "book_ts": _iso(NOW), "tags": ("test-market",),
                                 "min_order_size": 5.0}  # issue #226
    fields.update(overrides)
    return Intent(**fields)  # type: ignore[arg-type]


def _quote(side="buy", token=YES, price=0.60, status="open", market=MARKET) -> dict:
    row = {"ts": _iso(NOW), "market": market, "side": side, "price": price, "size": 5, "status": status,
           "venue": "paper"}
    if token is not None:
        row["token_id"] = token
    return row


def _state(quotes=(), fills=(), pair=(YES, NO)) -> dict:
    return {"open_positions": 0, "leg_already_open": False, "exposure_usd": 0.0, "leg_exposure_usd": 0.0,
            "opposite_leg_shares": 0.0, "opposite_pending_buys": 0, "daily_loss_usd": 0.0, "recent_fills": list(fills), "own_quotes": list(quotes), MARKET_PAIR_KEY: pair}


def _eval(intent: Intent, state: dict):
    return evaluate(intent, load_risk(), state, now=NOW)


def _wash(token=NO, side="buy", price=0.41, quotes=(), pair=(YES, NO)):
    return wash_problems(market=MARKET, token_id=token, side=side, strategy=CLEAN, counterparty=None,
                         state=_state(quotes, pair=pair), now=NOW, price=price)


# --- acceptance 1-6 (RiskGate) -------------------------------------------------------------------------

def test_mint_pair_resting_yes_bid_vetoes_taker_no_buy():
    decision = _eval(_intent(market_price=0.41), _state([_quote("buy", YES, 0.60)]))
    assert decision.allow is False and MINT_PAIR_CROSS in decision.reasons
    assert _wash(price=0.41, quotes=[_quote("buy", YES, 0.60)]) == [MINT_PAIR_CROSS]  # the issue's repro


def test_mint_pair_boundary():
    assert _wash(price=0.39, quotes=[_quote("buy", YES, 0.60)]) == []                  # 0.99: allowed
    assert _wash(price=0.40, quotes=[_quote("buy", YES, 0.60)]) == [MINT_PAIR_CROSS]   # 1.00: a pair
    assert _eval(_intent(market_price=0.39), _state([_quote("buy", YES, 0.60)])).allow is True
    assert _wash(price=0.40 - PAIR_CROSS_TOLERANCE / 2, quotes=[_quote("buy", YES, 0.60)]) == [MINT_PAIR_CROSS]


def test_merge_pair_resting_yes_ask_vetoes_taker_no_sell():
    assert _wash(side="sell", price=0.59, quotes=[_quote("sell", YES, 0.40)]) == [MERGE_PAIR_CROSS]
    assert _wash(side="sell", price=0.60, quotes=[_quote("sell", YES, 0.40)]) == [MERGE_PAIR_CROSS]  # 1.00
    assert _wash(side="sell", price=0.61, quotes=[_quote("sell", YES, 0.40)]) == []
    decision = _eval(_intent(side="sell", market_price=0.59, model_prob=0.30), _state([_quote("sell", YES, 0.40)]))
    assert MERGE_PAIR_CROSS in decision.reasons


def test_cancelled_or_other_market_quote_does_not_veto():
    quotes = [_quote("buy", YES, 0.60, status="cancelled"), _quote("buy", YES, 0.60, market="other-market"),
              _quote("sell", YES, 0.60),          # a resting sell does not pair with a buy
              _quote("buy", NO, 0.60)]            # our own same-token bid is not a pair
    assert _wash(quotes=quotes) == []
    assert _wash(quotes=quotes, pair=None) == []  # nothing relevant: the market's tokens are not needed


@pytest.mark.parametrize("quotes, pair, price, token", [
    ([_quote("buy", None, 0.60)], (YES, NO), 0.41, NO),             # the resting quote's token is unknown
    ([_quote("buy", YES, 0.60)], (YES, NO), 0.41, ""),              # the intent's token is unknown
    ([_quote("buy", YES, 0.60)], None, 0.41, NO),                   # the market's tokens are unknown
    ([_quote("buy", YES, 0.60)], ("a", "b"), 0.41, NO),             # not this market's tokens
    ([_quote("buy", YES, 0.60)], (YES, NO, "x"), 0.41, NO),         # not exactly two
    ([_quote("buy", YES, None)], (YES, NO), 0.41, NO),              # missing quote price
    ([_quote("buy", YES, float("nan"))], (YES, NO), 0.41, NO),      # non-finite quote price
    ([_quote("buy", YES, 0.60)], (YES, NO), None, NO),              # missing intent price
    ([_quote("buy", YES, 0.60)], (YES, NO), float("inf"), NO),      # non-finite intent price
    ([_quote("buy", YES, 0.60, status="partial")], (YES, NO), 0.41, NO),  # unknown status
])
def test_unknown_token_or_missing_price_fails_closed(quotes, pair, price, token):
    assert _wash(token=token, price=price, quotes=quotes, pair=pair) == [WASH_INPUT_INVALID]


def test_arb_pair_second_leg_is_not_a_wash():
    fills = [{"ts": _iso(NOW - timedelta(seconds=30)), "strategy": "other", "market": MARKET, "side": "buy",
              "shares": 10.0, "token_id": YES}]
    assert _eval(_intent(), _state(fills=fills)).allow is True  # a filled leg is not a resting quote


@pytest.mark.parametrize("row, expected", [
    ({"token_ids": [YES, NO]}, (YES, NO)),
    ({"token_ids": [YES, NO], "outcomes": ["Yes", "No"]}, (YES, NO)),
    ({"token_ids": [YES]}, None),
    ({"token_ids": [YES, YES]}, None),
    ({"token_ids": [YES, NO, "x"]}, None),
    ({"token_ids": [YES, NO], "neg_risk": True}, None),
    ({"token_ids": [YES, NO], "negRisk": True}, None),
    ({"token_ids": [YES, NO], "token_ids_problem": "mismatch"}, None),
    ({"token_ids": [YES, NO], "outcomes": ["a", "b", "c"]}, None),
    ("garbage", None),
])
def test_binary_pair_from_a_scanner_row(row, expected):
    assert binary_pair(row) == expected


# --- load_state supplies the pair from the scanner rows ---------------------------------------------------

@pytest.fixture
def files(tmp_path, monkeypatch):
    paths = {"paper": tmp_path / "paper.jsonl", "quotes": tmp_path / "quotes.jsonl",
             "markets": tmp_path / "markets.jsonl", "bus": tmp_path / "bus.jsonl"}
    monkeypatch.setattr(paper_ledger, "PAPER_PATH", paths["paper"])
    monkeypatch.setattr(paper_ledger, "QUOTES_PATH", paths["quotes"])
    monkeypatch.setattr(paper_ledger, "PAIR_MARKETS_PATH", paths["markets"])
    monkeypatch.setattr(maker, "MARKETS_PATH", paths["markets"])
    return paths


def _lines(path: Path, rows):
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def test_load_state_reads_the_pair_only_when_needed(files):
    _lines(files["markets"], [{"id": MARKET, "token_ids": [YES, NO]}])
    assert paper_ledger.load_state(MARKET, NO)["market_pair"] is None  # no quotes: not read
    _lines(files["quotes"], [_quote("buy", YES, 0.60)])
    state = paper_ledger.load_state(MARKET, NO)
    assert state["market_pair"] == (YES, NO)
    assert wash_problems(market=MARKET, token_id=NO, side="buy", strategy=CLEAN, counterparty=None,
                         state=state, now=NOW, price=0.41) == [MINT_PAIR_CROSS]


def test_load_state_without_a_scanner_row_fails_closed(files):
    _lines(files["quotes"], [_quote("buy", YES, 0.60)])
    state = paper_ledger.load_state(MARKET, NO)
    assert state["market_pair"] is None
    assert _eval(_intent(), state).reasons.count(WASH_INPUT_INVALID) == 1


# --- acceptance 7: the Maker runs the same check ---------------------------------------------------------

def _maker_quote(files, token, side="buy", price=0.40):
    return maker.quote(market=MARKET, token_id=token, side=side, price=price, size=5, bus_path=files["bus"],
                       quotes_path=files["quotes"], markets_path=files["markets"])


def _fresh_books(*tokens):
    stamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    _lines(maker.BOOKS_PATH, [{"ts": stamp, "book_ts": stamp, "market_id": MARKET, "token_id": t,
                               "best_bid": 0.39, "best_ask": 0.41} for t in tokens])


def test_maker_quote_uses_same_check(files):
    _fresh_books(YES, NO)
    _lines(files["markets"], [{"id": MARKET, "token_ids": [YES, NO]}])
    assert _maker_quote(files, YES, price=0.60)["payload"]["status"] == "open"
    with pytest.raises(maker.QuoteRefused) as info:
        _maker_quote(files, NO, price=0.40)  # 0.60 + 0.40 = 1.00: would mint against our own YES bid
    assert info.value.reason == MINT_PAIR_CROSS
    assert len(read_jsonl(files["quotes"])) == 1  # nothing written
    assert _maker_quote(files, NO, price=0.39)["payload"]["status"] == "open"  # 0.99 does not pair


def test_maker_quote_fails_closed_without_the_market_tokens(files):
    _fresh_books(YES, NO)
    _maker_quote(files, YES, price=0.60)
    with pytest.raises(maker.QuoteRefused) as info:
        _maker_quote(files, NO, price=0.30)
    assert info.value.reason == WASH_INPUT_INVALID


def test_maker_quote_refuses_on_an_unreadable_quotes_file(files):
    _fresh_books(YES)
    files["quotes"].write_text("{torn")
    with pytest.raises(maker.QuoteRefused) as info:
        _maker_quote(files, YES)
    assert info.value.reason == WASH_INPUT_INVALID


def test_risk_gate_and_maker_share_one_function():
    import inspect

    import src.risk_gate as risk_gate
    import src.wash_detect as wash_detect
    assert "pair_cross_problems" in inspect.getsource(wash_detect.wash_problems)
    assert "pair_cross_problems" in inspect.getsource(maker.pair_problem)
    assert "wash_problems" in inspect.getsource(risk_gate.evaluate)
