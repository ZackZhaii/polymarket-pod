"""Issue #123: a same-direction add to an open leg vetoes add_to_open_leg, and RiskGate fails closed
on missing / non-finite / negative exposure inputs.

Before: leg_already_open=True exempted the order from max_open_positions, and RiskGate never read
exposure_usd / leg_exposure_usd, so one leg could be bought again and again ($10 at a time). Now a
buy of a long leg (or a sell of a short leg) vetoes add_to_open_leg whatever the position count;
an order that reduces the leg keeps the exemption. The other token of the same market is still a
new leg (Task P), unchanged.

Offline. No network, no credentials. config/risk.toml is read, never written.
"""
from __future__ import annotations

import math

import pytest

import src.paper_ledger as paper_ledger
from src.podlib import RISK_PATH, load_risk
from src.risk_gate import evaluate, exposure_state_problems, open_leg_problems
from tests.test_single_leg_exposure import MARKET, NO, YES, _intent, _ledger, _row

CLEAN = {"open_positions": 0, "leg_already_open": False, "exposure_usd": 0.0, "leg_exposure_usd": 0.0,
         "leg_net_shares": 0.0, "opposite_leg_shares": 0.0, "daily_loss_usd": 0.0, "recent_fills": [], "own_quotes": []}


def _open(net: float) -> dict:
    return dict(CLEAN, open_positions=1, leg_already_open=True, leg_net_shares=net,
                exposure_usd=abs(net) * 0.4, leg_exposure_usd=abs(net) * 0.4)


@pytest.fixture
def no_bus(monkeypatch):
    monkeypatch.setattr(paper_ledger, "bus", lambda *args, **kwargs: None)


# --- acceptance: a same-direction buy on an open leg vetoes add_to_open_leg -------------------

def test_a_buy_on_an_open_leg_from_the_ledger_vetoes_add_to_open_leg(tmp_path, monkeypatch):
    cfg = load_risk()
    _ledger(tmp_path, monkeypatch, [_row(MARKET, YES, "buy", 10.0, 4.5)])
    state = paper_ledger.load_state(MARKET, YES)
    assert state["leg_already_open"] is True
    assert state["leg_net_shares"] == pytest.approx(10.0)
    decision = evaluate(_intent(token_id=YES), cfg, state)
    assert decision.allow is False
    assert decision.reasons == ["add_to_open_leg"]


def test_three_buys_on_the_same_leg_only_the_first_is_allowed(tmp_path, monkeypatch, no_bus):
    cfg = load_risk()
    _ledger(tmp_path, monkeypatch, [])
    results = [paper_ledger.commit_if_allowed(_intent(token_id=YES), cfg) for _ in range(3)]
    assert [r["allow"] for r in results] == [True, False, False]
    for result in results[1:]:
        assert result["decision"]["reasons"] == ["add_to_open_leg"]
        assert not result.get("halted")
    rows = [line for line in paper_ledger.PAPER_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) == 1


def test_the_other_token_of_the_same_market_is_still_a_new_leg(tmp_path, monkeypatch, no_bus):
    cfg = load_risk()
    _ledger(tmp_path, monkeypatch, [_row(MARKET, YES, "buy", 10.0, 4.5)])
    state = paper_ledger.load_state(MARKET, NO)
    assert state["leg_already_open"] is False
    assert state["leg_net_shares"] == 0.0
    assert state["open_positions"] == 1
    # Issue #237: a new leg, but buying it while YES is held builds the complete set -> structural_arb_disabled.
    assert "add_to_open_leg" not in evaluate(_intent(token_id=NO), cfg, state).reasons
    assert "structural_arb_disabled" in evaluate(_intent(token_id=NO), cfg, state).reasons
    assert paper_ledger.commit_if_allowed(_intent(token_id=NO), cfg)["allow"] is False


def test_the_other_token_still_counts_toward_the_cap(tmp_path, monkeypatch):
    cfg = load_risk()
    cap = int(cfg["max_open_positions"])
    rows = [_row(MARKET, YES, "buy", 10.0, 4.5)] + [_row(f"other-{n}", "t", "buy", 1.0, 0.5) for n in range(cap - 1)]
    _ledger(tmp_path, monkeypatch, rows)
    reasons = evaluate(_intent(token_id=NO), cfg, paper_ledger.load_state(MARKET, NO)).reasons
    assert "max_open_positions" in reasons
    assert "add_to_open_leg" not in reasons


def test_a_closed_leg_can_be_opened_again(tmp_path, monkeypatch):
    cfg = load_risk()
    _ledger(tmp_path, monkeypatch, [_row(MARKET, YES, "buy", 10.0, 4.5), _row(MARKET, YES, "sell", 10.0, 4.0)])
    state = paper_ledger.load_state(MARKET, YES)
    assert state["leg_already_open"] is False
    reasons = evaluate(_intent(token_id=YES), cfg, state).reasons
    assert "add_to_open_leg" not in reasons
    assert "invalid_leg_net_shares" not in reasons  # (the fresh opposite fill is a separate wash veto)


# --- direction: only an add is vetoed ----------------------------------------------------------

@pytest.mark.parametrize("side,net,expected", [
    ("buy", 10.0, ["add_to_open_leg"]),
    ("sell", -10.0, ["add_to_open_leg"]),
    ("sell", 10.0, []),
    ("buy", -10.0, []),
])
def test_open_leg_problems_by_direction(side, net, expected):
    assert open_leg_problems(side, _open(net)) == expected


def test_reducing_an_open_leg_keeps_the_cap_exemption():
    cfg = load_risk()
    state = dict(_open(10.0), open_positions=int(cfg["max_open_positions"]))
    reasons = evaluate(_intent(side="sell", model_prob=0.30, market_price=0.40), cfg, state).reasons
    assert "add_to_open_leg" not in reasons
    assert "max_open_positions" not in reasons


@pytest.mark.parametrize("net", [None, 0.0, -0.0, math.nan, math.inf, "10", True])
def test_an_open_leg_without_usable_net_shares_vetoes(net):
    state = dict(_open(10.0), leg_net_shares=net)
    if net is None:
        del state["leg_net_shares"]
    for side in ("buy", "sell"):
        assert open_leg_problems(side, state) == ["invalid_leg_net_shares"]
        assert "invalid_leg_net_shares" in evaluate(_intent(side=side), load_risk(), state).reasons


@pytest.mark.parametrize("flag", [False, None, 1, "true"])
def test_only_an_exactly_true_open_flag_counts(flag):
    assert open_leg_problems("buy", dict(_open(10.0), leg_already_open=flag)) == []


def test_the_veto_does_not_depend_on_the_position_count():
    cfg = load_risk()
    for count in (1, int(cfg["max_open_positions"]) - 1, int(cfg["max_open_positions"])):
        reasons = evaluate(_intent(token_id=YES), cfg, dict(_open(10.0), open_positions=count)).reasons
        assert reasons == ["add_to_open_leg"]


# --- exposure inputs fail closed ---------------------------------------------------------------

@pytest.mark.parametrize("field", ["exposure_usd", "leg_exposure_usd"])
@pytest.mark.parametrize("value", ["missing", None, math.nan, math.inf, -math.inf, -0.01, "1.0", True])
def test_bad_exposure_vetoes(field, value):
    state = dict(CLEAN)
    if value == "missing":
        del state[field]
    else:
        state[field] = value
    decision = evaluate(_intent(), load_risk(), state)
    assert decision.allow is False
    assert decision.reasons == [f"invalid_{field}"]


def test_zero_and_positive_exposure_pass():
    assert exposure_state_problems(dict(CLEAN)) == []
    assert exposure_state_problems(dict(CLEAN, exposure_usd=12.5, leg_exposure_usd=4.5)) == []
    assert evaluate(_intent(), load_risk(), dict(CLEAN, exposure_usd=4.5, open_positions=1)).allow is True


def test_a_corrupt_ledger_state_has_no_exposure_or_net_shares(tmp_path, monkeypatch):
    path = tmp_path / "paper.jsonl"
    path.write_text('{"fill_id": "x", "ts": "not-a-time"\n', encoding="utf-8")
    monkeypatch.setattr(paper_ledger, "PAPER_PATH", path)
    monkeypatch.setattr(paper_ledger, "QUOTES_PATH", tmp_path / "quotes.jsonl")
    state = paper_ledger.load_state(MARKET, YES)
    assert state["exposure_usd"] is None and state["leg_exposure_usd"] is None
    assert state["leg_net_shares"] is None
    reasons = evaluate(_intent(token_id=YES), load_risk(), state).reasons
    assert "invalid_risk_state" in reasons  # vetoed once; the missing exposure adds no second reason


def test_risk_file_is_untouched(tmp_path, monkeypatch, no_bus):
    before = RISK_PATH.read_bytes()
    _ledger(tmp_path, monkeypatch, [])
    for _ in range(2):
        paper_ledger.commit_if_allowed(_intent(token_id=YES), load_risk())
    assert RISK_PATH.read_bytes() == before
