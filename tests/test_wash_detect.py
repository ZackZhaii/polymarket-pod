"""Task S (backlog item 1): structured wash / self-trade detection in RiskGate.

Issue #56 adds the strict quotes boundary of paper_ledger.load_state to this file: a quotes file
that cannot be read in full is an unusable history (wash_input_invalid), never a short one.

The strategy-name keyword ban stays; these checks look at structure, so a clean name
does not get through. Offline. No network, no credentials. config/risk.toml is read,
never written.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import src.halt_store as halt_store

import src.maker as maker
import src.paper_ledger as paper_ledger
import src.sentinel as sentinel
from src.podlib import RISK_PATH, load_risk, read_jsonl
from src.risk_gate import BANNED_STRATEGY_TOKENS, Intent, evaluate
from src.wash_detect import (
    CROSS_SIDE_SAME_LEG,
    SELF_TRADE_STRUCTURE,
    WASH_INPUT_INVALID,
    WASH_INPUT_MISSING,
    WASH_SAME_MARKET,
    WASH_WINDOW_S,
    wash_problems,
)

NOW = datetime(2026, 10, 9, 0, 0, 0, tzinfo=timezone.utc)
MARKET = "task-s-market"
YES = "tok-yes"
NO = "tok-no"
CLEAN = "momentum-v2"  # innocuous name: no banned keyword
WASH_REASONS = {CROSS_SIDE_SAME_LEG, SELF_TRADE_STRUCTURE, WASH_SAME_MARKET, WASH_INPUT_MISSING, WASH_INPUT_INVALID}


def _iso(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def _intent(**overrides: object) -> Intent:
    fields: dict[str, object] = {
        "strategy": CLEAN,
        "market": MARKET,
        "token_id": YES,
        "side": "buy",
        "model_prob": 0.70,
        "market_price": 0.40,
        "assumed_slippage": 0.005,
        "fee_rate": 0.0,
        "liquidity_usd": 20000.0,
        "spread": 0.01,
        "hypothetical": False,
        "book_ts": _iso(NOW),
    }
    fields.update(overrides)
    return Intent(**fields)  # type: ignore[arg-type]


def _state(fills: list | None = None, quotes: list | None = None) -> dict:
    return {
        "open_positions": 0,
        "leg_already_open": False,
        "exposure_usd": 0.0, "leg_exposure_usd": 0.0, "daily_loss_usd": 0.0,
        "recent_fills": [] if fills is None else fills,
        "own_quotes": [] if quotes is None else quotes,
    }


def _fill(side: str, token: str | None = YES, ago_s: float = 30.0, market: str = MARKET, **extra: object) -> dict:
    row: dict[str, object] = {"ts": _iso(NOW - timedelta(seconds=ago_s)), "strategy": "other-strat",
                              "market": market, "side": side, "shares": 10.0}
    if token is not None:
        row["token_id"] = token
    row.update(extra)
    return row


def _quote(side: str, token: str | None = YES, status: object = "open", market: str = MARKET) -> dict:
    row: dict[str, object] = {"ts": _iso(NOW), "market": market, "side": side, "price": 0.6,
                              "size": 5, "status": status, "venue": "paper"}
    if token is not None:
        row["token_id"] = token
    return row


def _eval(intent: Intent, state: dict):
    return evaluate(intent, load_risk(), state, now=NOW)


# --- legitimate one-sided intents still pass ---------------------------------------


def test_clean_one_sided_intent_with_no_history_is_allowed():
    decision = _eval(_intent(), _state())
    assert decision.allow is True, decision.reasons


def test_one_sided_history_does_not_veto():
    fills = [_fill("buy"), _fill("buy", ago_s=1)]  # adding to the same side of the same leg
    quotes = [_quote("buy")]                        # our own bid on the same side
    decision = _eval(_intent(), _state(fills, quotes))
    assert decision.allow is True, decision.reasons


def test_other_token_other_market_and_cancelled_quotes_do_not_veto():
    fills = [_fill("sell", token=NO), _fill("sell", market="other-market")]
    quotes = [_quote("sell", token=NO), _quote("sell", market="other-market"), _quote("sell", status="cancelled")]
    decision = _eval(_intent(), _state(fills, quotes))
    assert decision.allow is True, decision.reasons


def test_arb_pair_second_leg_is_not_a_wash():
    # Buy YES then buy NO is directional per leg (Task P), not trading with ourselves.
    decision = _eval(_intent(token_id=NO), _state([_fill("buy", token=YES)]))
    assert decision.allow is True, decision.reasons


def test_external_counterparty_is_allowed():
    decision = _eval(_intent(counterparty="0xabc-external-maker"), _state())
    assert decision.allow is True, decision.reasons


# --- required: uncancelled opposite resting order on the same leg -----------------


@pytest.mark.parametrize("side,quote_side", [("buy", "sell"), ("sell", "buy")])
def test_opposite_resting_quote_on_same_leg_vetoes_a_clean_name(side, quote_side):
    decision = _eval(_intent(side=side), _state(quotes=[_quote(quote_side)]))
    assert decision.allow is False
    assert CROSS_SIDE_SAME_LEG in decision.reasons
    assert "banned_behavior" not in decision.reasons


def test_old_but_uncancelled_quote_still_vetoes():
    quote = _quote("sell")
    quote["ts"] = _iso(NOW - timedelta(days=3))
    assert CROSS_SIDE_SAME_LEG in _eval(_intent(), _state(quotes=[quote])).reasons


def test_unknown_leg_quote_is_treated_as_same_leg():
    assert CROSS_SIDE_SAME_LEG in _eval(_intent(), _state(quotes=[_quote("sell", token=None)])).reasons
    assert CROSS_SIDE_SAME_LEG in _eval(_intent(token_id=""), _state(quotes=[_quote("sell")])).reasons


# --- hardening: opposite paper fill on the same leg within the window ------------


@pytest.mark.parametrize("side,fill_side", [("buy", "sell"), ("sell", "buy")])
def test_opposite_fill_within_window_vetoes_a_clean_name(side, fill_side):
    decision = _eval(_intent(side=side), _state([_fill(fill_side, strategy=CLEAN)]))
    assert decision.allow is False
    assert WASH_SAME_MARKET in decision.reasons
    assert "banned_behavior" not in decision.reasons


def test_window_boundary_and_outside():
    at_edge = _eval(_intent(), _state([_fill("sell", ago_s=WASH_WINDOW_S)]))
    assert WASH_SAME_MARKET in at_edge.reasons
    outside = _eval(_intent(), _state([_fill("sell", ago_s=WASH_WINDOW_S + 1)]))
    assert outside.allow is True, outside.reasons


def test_future_stamped_opposite_fill_counts_as_inside_window():
    assert WASH_SAME_MARKET in _eval(_intent(), _state([_fill("sell", ago_s=-60)])).reasons


def test_hypothetical_opposite_fill_still_counts():
    fill = _fill("sell", hypothetical=True, counts_toward_risk=False)
    assert WASH_SAME_MARKET in _eval(_intent(), _state([fill])).reasons


# --- counterparty / maker is ourselves ---------------------------------------------


@pytest.mark.parametrize("counterparty", [CLEAN, " Momentum-V2 ", "Maker", "self", "other-strat"])
def test_own_counterparty_vetoes(counterparty):
    decision = _eval(_intent(counterparty=counterparty), _state([_fill("buy", market="elsewhere")]))
    assert decision.allow is False
    assert SELF_TRADE_STRUCTURE in decision.reasons


# --- keyword ban still works, and both checks stack ---------------------------------


@pytest.mark.parametrize("token", BANNED_STRATEGY_TOKENS)
def test_keyword_ban_still_vetoes_with_clean_structure(token):
    decision = _eval(_intent(strategy=f"{token}-probe"), _state())
    assert decision.allow is False
    assert "banned_behavior" in decision.reasons
    assert not WASH_REASONS & set(decision.reasons)


def test_keyword_and_structure_both_reported():
    decision = _eval(_intent(strategy="wash-probe"), _state(quotes=[_quote("sell")]))
    assert {"banned_behavior", CROSS_SIDE_SAME_LEG} <= set(decision.reasons)


# --- fail closed --------------------------------------------------------------------


@pytest.mark.parametrize("drop", ["recent_fills", "own_quotes"])
def test_missing_history_vetoes(drop):
    state = _state()
    del state[drop]
    decision = _eval(_intent(), state)
    assert decision.allow is False
    assert WASH_INPUT_MISSING in decision.reasons


@pytest.mark.parametrize(
    "fills,quotes",
    [
        (None, []),
        ([], "[]"),
        (["not a row"], []),
        ([{"ts": _iso(NOW), "side": "sell"}], []),  # no market
        ([_fill("hold")], []),
        ([_fill("sell", ts="2026-10-09 00:00:00")], []),  # naive
        ([_fill("sell", ts=None)], []),
        ([_fill("sell", token_id=7)], []),
        ([], [_quote("sell", status=None)]),
        ([], [_quote("sell", status="filled")]),  # unknown status: not assumed closed
        ([], [_quote("both")]),
        ([], [{"side": "sell", "status": "open"}]),
    ],
)
def test_malformed_history_vetoes(fills, quotes):
    state = _state()
    state["recent_fills"], state["own_quotes"] = fills, quotes
    decision = _eval(_intent(), state)
    assert decision.allow is False
    assert WASH_INPUT_INVALID in decision.reasons


@pytest.mark.parametrize("counterparty", ["", "   ", 5, True])
def test_unusable_counterparty_vetoes(counterparty):
    decision = _eval(_intent(counterparty=counterparty), _state())
    assert WASH_INPUT_INVALID in decision.reasons


def test_unusable_now_or_market_vetoes():
    base = dict(token_id=YES, side="buy", strategy=CLEAN, counterparty=None, state=_state())
    assert wash_problems(market=MARKET, now="2026-10-09T00:00:00Z", **base) == [WASH_INPUT_INVALID]
    assert wash_problems(market="", now=NOW, **base) == [WASH_INPUT_INVALID]
    assert wash_problems(market=MARKET, now=NOW, **base) == []


def test_other_market_rows_are_not_parsed_beyond_market():
    # Scope: only rows on the intent's market are needed; their ts/side must be valid.
    fills = [_fill("sell", market="other-market", ts="garbage")]
    assert _eval(_intent(), _state(fills)).allow is True


# --- end to end through the paper ledger --------------------------------------------


@pytest.fixture
def paper(tmp_path, monkeypatch):
    paths = {"paper": tmp_path / "paper.jsonl", "quotes": tmp_path / "quotes.jsonl", "bus": tmp_path / "bus.jsonl"}
    monkeypatch.setattr(paper_ledger, "PAPER_PATH", paths["paper"])
    monkeypatch.setattr(paper_ledger, "QUOTES_PATH", paths["quotes"])
    monkeypatch.setattr(paper_ledger, "bus", lambda *args, **kwargs: None)
    return paths


def _live_intent(**overrides: object) -> Intent:
    from src.podlib import utc_now
    return _intent(book_ts=utc_now(), **overrides)


def test_ledger_vetoes_buy_against_own_resting_sell_until_cancelled(paper):
    before = RISK_PATH.read_bytes()
    cfg = load_risk()
    maker.quote(market=MARKET, token_id=YES, side="sell", price=0.6, size=5,
                bus_path=paper["bus"], quotes_path=paper["quotes"])
    vetoed = paper_ledger.commit_if_allowed(_live_intent(), cfg)
    assert vetoed["allow"] is False
    assert CROSS_SIDE_SAME_LEG in vetoed["decision"]["reasons"]
    assert read_jsonl(paper["paper"]) == []
    halt = sentinel.halt(reason="task-s test", market=MARKET, bus_path=paper["bus"], quotes_path=paper["quotes"])
    # Task AL: the halt also blocks the market until the Operator resumes it.
    still = paper_ledger.commit_if_allowed(_live_intent(), cfg)
    assert still["allow"] is False and f"halt_market:{halt['payload']['halt_id']}" in still["decision"]["reasons"]
    halt_store.resume(from_role="Operator", scope="market", halt_id=halt["payload"]["halt_id"],
                      approved_by="test", root_cause="test", postmortem="test", bus_path=paper["bus"])
    allowed = paper_ledger.commit_if_allowed(_live_intent(), cfg)
    assert allowed["allow"] is True, allowed
    assert RISK_PATH.read_bytes() == before


def test_ledger_vetoes_immediate_reverse_fill_on_same_leg(paper):
    cfg = load_risk()
    first = paper_ledger.commit_if_allowed(_live_intent(), cfg)
    assert first["allow"] is True, first
    reverse = paper_ledger.commit_if_allowed(_live_intent(side="sell", model_prob=0.30, market_price=0.40), cfg)
    assert reverse["allow"] is False
    assert WASH_SAME_MARKET in reverse["decision"]["reasons"]
    assert len(read_jsonl(paper["paper"])) == 1


def test_load_state_carries_the_history(paper):
    paper["paper"].write_text(json.dumps(_fill("buy")) + "\n", encoding="utf-8")
    paper["quotes"].write_text(json.dumps(_quote("sell")) + "\n", encoding="utf-8")
    state = paper_ledger.load_state(MARKET, YES)
    assert len(state["recent_fills"]) == 1
    assert len(state["own_quotes"]) == 1


# --- issue #56: strict quotes reading at the load_state boundary ---------------------
#
# A loose read (podlib.read_jsonl) skips bad lines, so a damaged uncancelled opposite quote
# could leave an empty history and let the structural check allow. Every case below must end in
# the existing wash_input_invalid veto, never in a partial history.

GOOD_QUOTE = json.dumps(_quote("sell"))  # an uncancelled opposite open quote on the intent's leg


def _write_quotes(paper, text: str) -> None:
    paper["quotes"].write_bytes(text.encode("utf-8"))


def _quote_state(paper) -> dict:
    return paper_ledger.load_state(MARKET, YES)


def test_a_clean_quotes_file_reads_as_before_and_still_vetoes(paper):
    _write_quotes(paper, GOOD_QUOTE + "\n")
    state = _quote_state(paper)
    assert state["quote_problems"] == []
    assert len(state["own_quotes"]) == 1
    result = paper_ledger.commit_if_allowed(_live_intent(), load_risk())
    assert result["allow"] is False
    assert CROSS_SIDE_SAME_LEG in result["decision"]["reasons"]
    assert read_jsonl(paper["paper"]) == []


def test_one_sided_and_cancelled_quotes_keep_their_result(paper):
    _write_quotes(paper, json.dumps(_quote("buy")) + "\n" + json.dumps(_quote("sell", status="cancelled")) + "\n")
    state = _quote_state(paper)
    assert state["quote_problems"] == []
    assert len(state["own_quotes"]) == 2
    result = paper_ledger.commit_if_allowed(_live_intent(), load_risk())
    assert result["allow"] is True, result
    assert len(read_jsonl(paper["paper"])) == 1


def test_a_missing_quotes_file_is_still_an_empty_history(paper):
    state = _quote_state(paper)
    assert state["own_quotes"] == [] and state["quote_problems"] == []
    assert paper_ledger.commit_if_allowed(_live_intent(), load_risk())["allow"] is True


def test_a_blank_quotes_file_is_still_an_empty_history(paper):
    _write_quotes(paper, "\n   \n")
    state = _quote_state(paper)
    assert state["own_quotes"] == [] and state["quote_problems"] == []
    assert paper_ledger.commit_if_allowed(_live_intent(), load_risk())["allow"] is True


@pytest.mark.parametrize(
    "lines,problems",
    [
        (["{not json", GOOD_QUOTE], ["malformed_row:1"]),                       # bad first line
        ([GOOD_QUOTE, '{"market": "task-s-market", "side":', GOOD_QUOTE],
         ["malformed_row:2"]),                                                 # bad middle line, truncated
        ([GOOD_QUOTE, "not json either"], ["malformed_row:2"]),                 # bad last line
        (["[]", GOOD_QUOTE], ["non_object_row:1"]),                             # array line
        ([GOOD_QUOTE, "null"], ["non_object_row:2"]),                           # null line
        (["7", GOOD_QUOTE], ["non_object_row:1"]),                              # number line
        (['"a quote"', GOOD_QUOTE], ["non_object_row:1"]),                      # string line
    ],
)
def test_a_broken_quote_line_makes_the_whole_history_unusable(paper, lines, problems):
    _write_quotes(paper, "".join(line + "\n" for line in lines))
    state = _quote_state(paper)
    assert state["own_quotes"] is None  # never a partial history
    assert state["quote_problems"] == problems
    result = paper_ledger.commit_if_allowed(_live_intent(), load_risk())
    assert result["allow"] is False
    assert WASH_INPUT_INVALID in result["decision"]["reasons"]
    assert result.get("halted") is not True  # no new halt policy: the existing veto only
    assert read_jsonl(paper["paper"]) == []


def test_a_quote_file_without_the_final_newline_is_unusable(paper):
    _write_quotes(paper, GOOD_QUOTE)  # a torn append: podlib.append_jsonl always writes "\n"
    state = _quote_state(paper)
    assert state["own_quotes"] is None
    assert state["quote_problems"] == ["unterminated_last_row:1"]
    assert WASH_INPUT_INVALID in paper_ledger.commit_if_allowed(_live_intent(), load_risk())["decision"]["reasons"]


def test_undecodable_quote_bytes_are_unusable(paper):
    paper["quotes"].write_bytes(GOOD_QUOTE.encode("utf-8") + b"\n\xff\xfe\n")
    state = _quote_state(paper)
    assert state["own_quotes"] is None
    assert state["quote_problems"] == ["undecodable_quotes"]
    assert WASH_INPUT_INVALID in paper_ledger.commit_if_allowed(_live_intent(), load_risk())["decision"]["reasons"]


def test_an_unreadable_quotes_file_is_unusable(paper):
    paper["quotes"].mkdir()  # a directory where the file belongs: read_bytes raises OSError
    state = _quote_state(paper)
    assert state["own_quotes"] is None
    assert len(state["quote_problems"]) == 1
    assert state["quote_problems"][0].startswith("unreadable_quotes:")


def test_a_broken_quotes_file_vetoes_and_writes_nothing(paper):
    _write_quotes(paper, GOOD_QUOTE + "\n" + "{oops\n")
    before = paper["quotes"].read_bytes()
    result = paper_ledger.commit_if_allowed(_live_intent(), load_risk())
    assert result["allow"] is False
    assert WASH_INPUT_INVALID in result["decision"]["reasons"]  # the veto RiskGate already had
    assert result["quote_problems"] == ["malformed_row:2"]      # the diagnosis is visible
    assert result.get("halted") is not True                     # and it is not a halt
    assert paper["quotes"].read_bytes() == before               # the source file is untouched
    assert not paper["paper"].exists() or read_jsonl(paper["paper"]) == []  # no fill booked
    assert halt_store.active_halts() == []
