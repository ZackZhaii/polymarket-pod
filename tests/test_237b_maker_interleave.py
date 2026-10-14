"""#237 remaining item 1 (dev 043e9ce): maker.quote writes a paper quote without the ledger commit lock and without
looking at ledger holdings, so an own buy quote on the other token can land while commit_if_allowed sits between
its RiskGate allow and its book. Result: the ledger holds one leg and an open buy quote rests on the other leg ->
the complete set is assembled, around the #237 opposite-leg veto. Expected (tightening): fail closed, the pair
never ends up "held leg + open opposite buy". Offline: tmp_path files only, no network, no orders.
Interleaving is deterministic (wrap paper_ledger.evaluate, like tests/test_dd_opposite_leg_fail_closed.py).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import src.maker as maker
import src.paper_ledger as paper_ledger
from src.podlib import load_risk, read_jsonl
from src.risk_gate import evaluate
from tests.test_single_leg_exposure import MARKET, NO, YES, _intent, _ledger, _row

STRUCTURAL = "structural_arb_disabled"
OLD_TS = "2026-01-01T00:00:00Z"


def _stamp(delta_s: float = 0.0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=delta_s)).isoformat(timespec="milliseconds") \
        .replace("+00:00", "Z")


def _books(tmp_path):
    path = tmp_path / "books.jsonl"
    rows = [{"ts": _stamp(-1), "book_ts": _stamp(-1), "market_id": MARKET, "token_id": t, "best_ask": 0.41,
             "best_bid": 0.39} for t in (YES, NO)]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


def _markets(tmp_path):
    path = tmp_path / "markets.jsonl"
    path.write_text(json.dumps({"id": MARKET, "token_ids": [YES, NO], "outcomes": ["Yes", "No"]}) + "\n",
                    encoding="utf-8")
    return path


def _maker_buy(tmp_path, token):
    return maker.quote(market=MARKET, token_id=token, side="buy", price=0.40, size=5,
                       bus_path=tmp_path / "bus.jsonl", quotes_path=tmp_path / "quotes.jsonl",
                       books_path=_books(tmp_path), markets_path=_markets(tmp_path))


def _buy(token=NO):
    return _intent(token_id=token, side="buy", model_prob=0.70, market_price=0.40, min_order_size=5.0)


def _open_buys(tmp_path, token):
    return [q for q in read_jsonl(tmp_path / "quotes.jsonl")
            if q.get("token_id") == token and q.get("side") == "buy" and q.get("status") == "open"]


@pytest.fixture
def no_bus(monkeypatch):
    monkeypatch.setattr(paper_ledger, "bus", lambda *a, **k: None)


def test_maker_quote_between_riskgate_allow_and_book_cannot_assemble_the_set(tmp_path, monkeypatch, no_bus):
    """Timeline: T1 commit(buy NO): load_state (no YES quote) -> RiskGate allow ; T2 maker.quote(buy YES) ;
    T1 book NO. Expected: either the maker quote is refused (QuoteRefused / no row) or the NO commit is refused."""
    _ledger(tmp_path, monkeypatch, [])
    real_evaluate = paper_ledger.evaluate
    seen: dict = {}

    def interleaving_evaluate(intent, cfg, state):
        decision = real_evaluate(intent, cfg, state)
        if intent.token_id == NO and "maker" not in seen:
            seen["maker"] = None
            try:
                seen["maker"] = _maker_buy(tmp_path, YES)
            except (maker.QuoteRefused, maker.HaltActive) as exc:
                seen["maker"] = exc
        return decision

    monkeypatch.setattr(paper_ledger, "evaluate", interleaving_evaluate)
    result = paper_ledger.commit_if_allowed(_buy(NO), load_risk())
    no_held = paper_ledger.load_state(MARKET, NO)["leg_net_shares"] > 0
    yes_quoted = bool(_open_buys(tmp_path, YES))
    assert not (no_held and yes_quoted), (
        f"set assembled: NO booked={result['allow']} and YES open buy quote={yes_quoted} (maker={seen['maker']!r})")


def test_maker_buy_quote_on_other_token_while_leg_is_held_is_refused(tmp_path, monkeypatch):
    """No interleaving needed: NO already held in the ledger, the Maker still rests a YES buy (the RiskGate veto
    only covers taker intents)."""
    _ledger(tmp_path, monkeypatch, [_row(MARKET, NO, "buy", 10.0, 4.0, ts=OLD_TS)])
    monkeypatch.setattr(paper_ledger, "PAPER_PATH", tmp_path / "paper.jsonl")
    with pytest.raises(maker.QuoteRefused) as info:
        _maker_buy(tmp_path, YES)
    assert info.value.reason == STRUCTURAL
    assert _open_buys(tmp_path, YES) == []


# --- controls (expected GREEN today) ---------------------------------------------------------------------

def test_control_maker_quote_first_then_commit_is_refused(tmp_path, monkeypatch, no_bus):
    _ledger(tmp_path, monkeypatch, [])
    _maker_buy(tmp_path, YES)
    result = paper_ledger.commit_if_allowed(_buy(NO), load_risk())
    assert result["allow"] is False and STRUCTURAL in result["decision"]["reasons"]


def test_control_maker_quote_on_empty_market_is_written(tmp_path, monkeypatch):
    _ledger(tmp_path, monkeypatch, [])
    assert _maker_buy(tmp_path, YES)["payload"]["status"] == "open"
