"""Gap fill: RiskGate's fail-closed side check (src/risk_gate.py:421-422, 481-505).

``evaluate`` only ever sizes a single leg, so ``intent.side`` must be exactly the literal "buy" or
"sell". Every other value (a typo, an arb-pair marker, another case, a missing side) has to veto --
neither the hypothetical / phase0-gate-test exemption nor the open-leg cap exemption may let it
through, an unknown side must never be priced as a buy (no slippage added), and a vetoed decision is
never booked (paper_ledger.commit_if_allowed returns before it reads decision.shares).

The rest of the gate is covered elsewhere: test_risk_gate.py (vetoes, clean pass), test_non_finite_params.py
(NaN / inf), test_riskgate_intent_size.py (issue #104 size), test_slippage_floor.py, test_hypothetical_caps.py,
test_single_leg_exposure.py and test_add_to_open_leg.py. Offline: pure function, temp halt state via conftest.
"""
from __future__ import annotations

import pytest

import src.risk_gate as risk_gate
from src.podlib import load_risk, utc_now
from src.risk_gate import Intent, evaluate

STATE = {"open_positions": 0, "exposure_usd": 0.0, "leg_exposure_usd": 0.0, "opposite_leg_shares": 0.0, "daily_loss_usd": 0.0,
         "recent_fills": [], "own_quotes": []}

BAD_SIDES = ["hold", "both", "BUY", "", None]


def _intent(**overrides: object) -> Intent:
    fields: dict[str, object] = {
        "strategy": "paper-gap-check",
        "market": "test-market",
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


def _state(**overrides: object) -> dict:
    state = dict(STATE)
    state.update(overrides)
    return state


# --- what "a usable side" means ----------------------------------------------------------------

def test_buy_and_sell_are_the_only_sides_that_can_pass():
    """The two documented sides still pass, each with its own formula and effective price."""
    cfg = load_risk()
    buy = evaluate(_intent(side="buy"), cfg, STATE)
    assert (buy.allow, buy.reasons, buy.formula) == (True, [], risk_gate.EDGE_FORMULA_BUY)
    assert buy.effective_price == pytest.approx(0.405) and buy.shares > 0

    # Issue #226 (D4): a sell needs an open long leg (no naked short); the net is large so it never caps the size.
    held = _state(open_positions=1, leg_already_open=True, leg_net_shares=1_000_000.0)
    sell = evaluate(_intent(side="sell", model_prob=0.40, market_price=0.55), cfg, held)
    assert (sell.allow, sell.reasons, sell.formula) == (True, [], risk_gate.EDGE_FORMULA_SELL)
    assert sell.effective_price == pytest.approx(0.545) and sell.shares > 0


# --- no other side is ever allowed -------------------------------------------------------------

@pytest.mark.parametrize("side", BAD_SIDES)
def test_an_unusable_side_is_vetoed_and_never_priced_as_a_buy(side):
    decision = evaluate(_intent(side=side), load_risk(), STATE)
    assert decision.allow is False
    assert "side_must_be_single_leg" in decision.reasons
    assert "wash_input_invalid" in decision.reasons  # wash_detect refuses the same unknown side
    # an unknown side is not priced as a buy: no slippage is added to the effective price
    assert decision.effective_price == pytest.approx(0.40)
    assert decision.formula == "" and decision.edge == -1.0
    # the echo size stays inside the 1% clip (10 / 0.40 = 25 shares) but is informational only:
    # paper_ledger.commit_if_allowed returns before reading decision.shares when allow is False.
    assert (decision.shares, decision.notional_usd, decision.fee_usd) == (25.0, pytest.approx(10.0), 0.0)


def test_the_unusable_side_collapses_the_decision_to_the_fail_closed_sentinel():
    """The exact veto stack and the -1 edge: the side guard is never the only thing standing."""
    decision = evaluate(_intent(side="hold"), load_risk(), STATE)
    assert decision.reasons == ["wash_input_invalid", "side_must_be_single_leg", "non_positive_edge"]
    assert decision.edge == -1.0 and decision.formula == ""
    assert decision.assumed_slippage == 0.005 and decision.fee_rate == 0.0  # echoed, not used
    clean = evaluate(_intent(side="buy"), load_risk(), STATE)
    assert clean.allow is True and clean.shares == 24.69  # the same inputs do book size on a real side


def test_the_hypothetical_gate_test_exemption_does_not_cover_a_bad_side():
    """hypothetical + the gate-test strategy skips one check only; the side guard still fires."""
    decision = evaluate(_intent(side="BUY", hypothetical=True, strategy="phase0-gate-test"), load_risk(), STATE)
    assert decision.allow is False
    assert "hypothetical_strategy_not_allowed" not in decision.reasons
    assert "side_must_be_single_leg" in decision.reasons and decision.edge == -1.0


def test_the_position_cap_exemption_does_not_cover_a_bad_side():
    """At the position cap the open-leg exemption is the only way through; a bad side still vetoes."""
    capped = _state(open_positions=5)
    vetoed = evaluate(_intent(side="hold"), load_risk(), capped)
    assert vetoed.allow is False and "max_open_positions" in vetoed.reasons
    assert "side_must_be_single_leg" in vetoed.reasons
    exempt = evaluate(_intent(side="sell", model_prob=0.40, market_price=0.55), load_risk(),
                      _state(open_positions=5, leg_already_open=True, leg_net_shares=10.0))
    assert exempt.allow is True and "max_open_positions" not in exempt.reasons
