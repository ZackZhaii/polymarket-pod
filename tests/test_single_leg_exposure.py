"""Task P (backlog item 5): a single-leg arb fill is directional exposure.

The ledger nets shares per leg (market + token_id). Only a buy and a sell of the
same token offset each other (that is closing a position). The two tokens of an
"arb pair" never offset each other: each open leg is its own position toward
max_open_positions, and its exposure is counted in full.

Offline. No network, no credentials. config/risk.toml is read, never written.
"""
from __future__ import annotations

import json

import pytest

import src.paper_ledger as paper_ledger
from src.podlib import RISK_PATH, load_risk, utc_now
from src.risk_gate import Intent, evaluate

MARKET = "task-p-pair"
YES = "tok-yes"
NO = "tok-no"


def _row(market: str, token: str | None, side: str, shares: float, notional: float | None, **extra: object) -> dict:
    row: dict[str, object] = {
        "ts": utc_now(),
        "market": market,
        "side": side,
        "shares": shares,
        "fee_usd": 0.0,
        "hypothetical": False,
        "counts_toward_risk": True,
    }
    if token is not None:
        row["token_id"] = token
    if notional is not None:
        row["notional_usd"] = notional
    row.update(extra)
    return row


def _ledger(tmp_path, monkeypatch, rows: list[dict]) -> None:
    path = tmp_path / "paper.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    monkeypatch.setattr(paper_ledger, "PAPER_PATH", path)
    monkeypatch.setattr(paper_ledger, "QUOTES_PATH", tmp_path / "quotes.jsonl")


def _intent(**overrides: object) -> Intent:
    fields: dict[str, object] = {
        "strategy": "task-p-real",
        "market": MARKET,
        "token_id": NO,
        "side": "buy",
        "model_prob": 0.70,
        "market_price": 0.40,
        "assumed_slippage": 0.005,
        "fee_rate": 0.0,
        "liquidity_usd": 20000.0,
        "spread": 0.01,
        "hypothetical": False,
        "book_ts": utc_now(),
    }
    fields.update(overrides)
    fields.setdefault("tags", ("test-market",))  # issue #131: a tagged market (missing tags veto)
    fields.setdefault("min_order_size", 5.0)  # issue #226: a book minimum (missing vetoes)
    return Intent(**fields)  # type: ignore[arg-type]


# --- ledger: per-leg netting, no pair offset ---------------------------------


def test_only_one_leg_filled_is_one_position_with_that_legs_notional(tmp_path, monkeypatch):
    _ledger(tmp_path, monkeypatch, [_row(MARKET, YES, "buy", 10.0, 4.5)])
    state = paper_ledger.load_state(MARKET, YES)
    assert state["open_positions"] == 1
    assert state["leg_already_open"] is True
    assert state["exposure_usd"] == pytest.approx(4.5)
    assert state["leg_exposure_usd"] == pytest.approx(4.5)
    # The other token of the pair is not open: filling it would be a new position.
    other = paper_ledger.load_state(MARKET, NO)
    assert other["leg_already_open"] is False
    assert other["leg_exposure_usd"] == 0.0


def test_both_legs_filled_count_as_two_positions_without_offset(tmp_path, monkeypatch):
    _ledger(tmp_path, monkeypatch, [
        _row(MARKET, YES, "buy", 10.0, 4.5),
        _row(MARKET, NO, "buy", 10.0, 5.0),
    ])
    state = paper_ledger.load_state(MARKET, YES)
    assert state["open_positions"] == 2
    assert state["exposure_usd"] == pytest.approx(9.5)
    assert state["leg_exposure_usd"] == pytest.approx(4.5)


def test_sell_on_the_opposite_token_does_not_cancel_the_other_leg(tmp_path, monkeypatch):
    # Old per-market netting summed +10 and -10 to zero and reported no open position.
    _ledger(tmp_path, monkeypatch, [
        _row(MARKET, YES, "buy", 10.0, 4.5),
        _row(MARKET, NO, "sell", 10.0, 5.0),
    ])
    state = paper_ledger.load_state(MARKET, YES)
    assert state["open_positions"] == 2
    assert state["exposure_usd"] == pytest.approx(9.5)


def test_partially_filled_opposite_leg_does_not_shrink_the_filled_leg(tmp_path, monkeypatch):
    _ledger(tmp_path, monkeypatch, [
        _row(MARKET, YES, "buy", 10.0, 4.5),
        _row(MARKET, NO, "buy", 4.0, 2.0),
    ])
    yes = paper_ledger.load_state(MARKET, YES)
    no = paper_ledger.load_state(MARKET, NO)
    assert yes["open_positions"] == 2
    assert yes["leg_exposure_usd"] == pytest.approx(4.5)  # full 10 shares, not 10 - 4
    assert no["leg_exposure_usd"] == pytest.approx(2.0)
    assert yes["exposure_usd"] == pytest.approx(6.5)


def test_partial_opposite_sell_does_not_shrink_the_filled_leg(tmp_path, monkeypatch):
    _ledger(tmp_path, monkeypatch, [
        _row(MARKET, YES, "buy", 10.0, 4.5),
        _row(MARKET, NO, "sell", 4.0, 2.0),
    ])
    state = paper_ledger.load_state(MARKET, YES)
    assert state["open_positions"] == 2
    assert state["leg_exposure_usd"] == pytest.approx(4.5)
    assert state["exposure_usd"] == pytest.approx(6.5)


def test_same_token_sell_closes_the_position(tmp_path, monkeypatch):
    _ledger(tmp_path, monkeypatch, [
        _row(MARKET, YES, "buy", 10.0, 4.5),
        _row(MARKET, YES, "sell", 10.0, 5.0),
    ])
    state = paper_ledger.load_state(MARKET, YES)
    assert state["open_positions"] == 0
    assert state["leg_already_open"] is False
    assert state["exposure_usd"] == 0.0


def test_same_token_partial_close_leaves_the_rest_at_average_fill_price(tmp_path, monkeypatch):
    _ledger(tmp_path, monkeypatch, [
        _row(MARKET, YES, "buy", 10.0, 4.0),
        _row(MARKET, YES, "sell", 4.0, 2.0),
    ])
    state = paper_ledger.load_state(MARKET, YES)
    assert state["open_positions"] == 1
    # 6 shares left; average fill price over the leg is (4 + 2) / (10 + 4).
    assert state["leg_exposure_usd"] == pytest.approx(6.0 * 6.0 / 14.0)


def test_row_without_token_id_never_offsets_a_named_leg(tmp_path, monkeypatch):
    _ledger(tmp_path, monkeypatch, [
        _row(MARKET, YES, "buy", 10.0, 4.5),
        _row(MARKET, None, "sell", 10.0, 5.0),
    ])
    state = paper_ledger.load_state(MARKET, YES)
    assert state["open_positions"] == 2


def test_row_without_notional_is_valued_at_one_dollar_per_share(tmp_path, monkeypatch):
    _ledger(tmp_path, monkeypatch, [_row(MARKET, YES, "buy", 10.0, None)])
    assert paper_ledger.load_state(MARKET, YES)["exposure_usd"] == pytest.approx(10.0)


def test_hypothetical_rows_still_do_not_count(tmp_path, monkeypatch):
    _ledger(tmp_path, monkeypatch, [
        _row(MARKET, YES, "buy", 10.0, 4.5, hypothetical=True, counts_toward_risk=False),
    ])
    state = paper_ledger.load_state(MARKET, YES)
    assert state["open_positions"] == 0
    assert state["exposure_usd"] == 0.0


def test_leg_positions_is_keyed_by_market_and_token():
    legs = paper_ledger.leg_positions([
        _row(MARKET, YES, "buy", 10.0, 4.5),
        _row(MARKET, NO, "buy", 4.0, 2.0),
        _row("other", YES, "buy", 1.0, 0.5),
    ])
    assert set(legs) == {(MARKET, YES), (MARKET, NO), ("other", YES)}
    assert legs[(MARKET, NO)]["shares"] == pytest.approx(4.0)


# --- risk side: max_open_positions ------------------------------------------


def _cap_rows(cfg: dict, pair_legs: list[tuple[str, str]]) -> list[dict]:
    """Fill the book to max_open_positions with single legs, ending with pair_legs."""
    cap = int(cfg["max_open_positions"])
    rows = [_row(MARKET, token, "buy", 10.0, 4.5) for _, token in pair_legs]
    rows += [_row(f"single-{n}", "t", "buy", 1.0, 0.5) for n in range(cap - len(rows))]
    return rows


def test_second_leg_of_a_pair_is_a_new_position_against_the_cap(tmp_path, monkeypatch):
    cfg = load_risk()
    _ledger(tmp_path, monkeypatch, _cap_rows(cfg, [(MARKET, YES)]))
    state = paper_ledger.load_state(MARKET, NO)
    assert state["open_positions"] == int(cfg["max_open_positions"])
    decision = evaluate(_intent(token_id=NO), cfg, state)
    assert decision.allow is False
    assert "max_open_positions" in decision.reasons


def test_adding_to_the_same_open_leg_is_vetoed_not_exempt(tmp_path, monkeypatch):
    # Issue #123: the open leg's position-cap exemption no longer lets it grow.
    cfg = load_risk()
    _ledger(tmp_path, monkeypatch, _cap_rows(cfg, [(MARKET, YES)]))
    state = paper_ledger.load_state(MARKET, YES)
    decision = evaluate(_intent(token_id=YES), cfg, state)
    assert decision.allow is False
    assert decision.reasons == ["add_to_open_leg"]


def test_a_filled_pair_uses_two_slots(tmp_path, monkeypatch):
    cfg = load_risk()
    cap = int(cfg["max_open_positions"])
    rows = [_row(MARKET, YES, "buy", 10.0, 4.5), _row(MARKET, NO, "buy", 10.0, 5.0)]
    rows += [_row(f"single-{n}", "t", "buy", 1.0, 0.5) for n in range(cap - 2)]
    _ledger(tmp_path, monkeypatch, rows)
    state = paper_ledger.load_state("fresh-market", "fresh-token")
    assert state["open_positions"] == cap  # old per-market count was cap - 1
    decision = evaluate(_intent(market="fresh-market", token_id="fresh-token"), cfg, state)
    assert "max_open_positions" in decision.reasons


def test_one_leg_under_the_cap_still_allows(tmp_path, monkeypatch):
    cfg = load_risk()
    _ledger(tmp_path, monkeypatch, [_row(MARKET, YES, "buy", 10.0, 4.5)])
    state = paper_ledger.load_state(MARKET, NO)
    assert evaluate(_intent(token_id=NO), cfg, state).allow is True


def test_market_level_flag_alone_no_longer_exempts():
    cfg = load_risk()
    state = {"open_positions": int(cfg["max_open_positions"]), "market_already_open": True,
             "daily_loss_usd": 0.0}
    decision = evaluate(_intent(), cfg, state)
    assert "max_open_positions" in decision.reasons


@pytest.mark.parametrize("flag", [1, "true", "yes", None])
def test_leg_flag_must_be_exactly_true(flag):
    cfg = load_risk()
    state = {"open_positions": int(cfg["max_open_positions"]), "leg_already_open": flag,
             "daily_loss_usd": 0.0}
    assert "max_open_positions" in evaluate(_intent(), cfg, state).reasons


def test_commit_if_allowed_reads_the_intents_own_leg(tmp_path, monkeypatch):
    before = RISK_PATH.read_bytes()
    cfg = load_risk()
    _ledger(tmp_path, monkeypatch, _cap_rows(cfg, [(MARKET, YES)]))
    monkeypatch.setattr(paper_ledger, "bus", lambda *args, **kwargs: None)
    vetoed = paper_ledger.commit_if_allowed(_intent(token_id=NO), cfg)
    assert vetoed["allow"] is False
    assert "max_open_positions" in vetoed["decision"]["reasons"]
    ledger_before = paper_ledger.PAPER_PATH.read_bytes()
    # Issue #123: the own (open) leg is not counted as a new position, but a buy adds to it.
    added = paper_ledger.commit_if_allowed(_intent(token_id=YES), cfg)
    assert added["allow"] is False
    assert added["decision"]["reasons"] == ["add_to_open_leg"]
    assert paper_ledger.PAPER_PATH.read_bytes() == ledger_before
    assert RISK_PATH.read_bytes() == before
