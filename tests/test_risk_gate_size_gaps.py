"""Offline gaps in RiskGate's size grid and ledger-state read (src/risk_gate.py).

Two helpers decide how much a paper order may book, and neither has a direct test today:

* ``_floor_to_step`` (src/risk_gate.py:384-391) turns a raw cap into the number of shares that is
  actually booked. It must floor -- never round up -- because the booked shares are what the 1% clip
  check and ``worst_case_loss`` are computed from: a helper that rounds up hands back a size whose
  cost is above the cap (issue #104). The epsilon at :388 pushes a value a hair below a step edge onto
  that edge, and the float guard at :389-390 has to step it back down; both directions are asserted
  here, plus the non-finite / non-positive sentinel that keeps NaN out of the size math.
* ``_risk_state`` (src/risk_gate.py:309-317) is the only reader of ``open_positions`` and
  ``daily_loss_usd``. A fractional count, a negative loss or a non-finite value means the ledger view
  is unusable and must veto (``invalid_risk_state`` -> a persisted global halt, Task AL), never be
  read as 0.

The rest of the gate is covered elsewhere: tests/test_risk_gate.py, tests/test_risk_gate_side_gaps.py,
tests/test_riskgate_intent_size.py (issue #104 size / limit price), tests/test_hypothetical_caps.py
(caps), tests/test_non_finite_params.py, tests/test_daily_loss_projected.py (worst_case_loss),
tests/test_add_to_open_leg.py (open-leg direction and the reduce cap). Offline: pure functions and
tmp_path-free state dicts, no network, no real ledger. The one ``evaluate`` call passes halt paths
under tmp_path so no halt state outside the test folder is read or written.
"""
from __future__ import annotations

import math

import pytest

import src.risk_gate as risk_gate
from src.podlib import load_risk
from src.risk_gate import evaluate
from src.thresholds import INTENT_SHARE_STEP
from tests.test_risk_gate import _intent

STATE = {"open_positions": 0, "exposure_usd": 0.0, "leg_exposure_usd": 0.0, "daily_loss_usd": 0.0,
         "recent_fills": [], "own_quotes": []}


# --- src/risk_gate.py:384-391: the share grid floors, and never past the raw cap -----------------

def test_the_share_grid_floors_and_never_hands_back_more_than_it_was_given():
    """The property the check at :389-390 exists for: booked shares <= the raw cap, always."""
    for raw in (0.005, 0.0099999, 0.0149999, 0.999999999, 1e-12, 24.69, 24.699999,
                INTENT_SHARE_STEP + 1e-12, 5.0 - 1e-12):
        floored = risk_gate._floor_to_step(raw)
        assert floored <= raw, raw
        assert 0.0 <= floored < raw + INTENT_SHARE_STEP, raw
        # ... and it lands on the intent_builder grid (the step is 0.01 shares).
        assert abs(floored / INTENT_SHARE_STEP - round(floored / INTENT_SHARE_STEP)) < 1e-9, raw


def test_a_value_just_below_a_step_edge_is_not_pushed_up_to_it():
    """0.01 - 1e-12 hits the epsilon at :388; the guard has to take the step back off."""
    assert risk_gate._floor_to_step(INTENT_SHARE_STEP - 1e-12) == 0.0
    assert risk_gate._floor_to_step(2 * INTENT_SHARE_STEP - 1e-12) == INTENT_SHARE_STEP


def test_the_grid_returns_the_step_itself_on_an_exact_edge():
    """No off-by-one at the edge: an exact multiple stays where it is (never stepped down)."""
    for steps in (1, 2, 99, 2469):
        assert risk_gate._floor_to_step(steps * INTENT_SHARE_STEP) == pytest.approx(steps * INTENT_SHARE_STEP)


@pytest.mark.parametrize("raw", [0.0, -0.0, -1.0, -0.005, -math.inf, math.nan, math.inf])
def test_a_non_positive_or_non_finite_cap_books_zero_shares(raw):
    """The sentinel at :386-387: NaN must not reach the floor/round math (every compare is False)."""
    assert risk_gate._floor_to_step(raw) == 0.0


def test_the_floored_size_is_exactly_what_the_gate_books():
    """The helper is not decorative: 10 / 0.405 = 24.691... floors to the 24.69 the decision carries."""
    raw = 10.0 / 0.405
    assert raw == pytest.approx(24.691358, abs=1e-6)
    assert risk_gate._floor_to_step(raw) == 24.69
    decision = evaluate(_intent(), load_risk(), dict(STATE))
    assert decision.allow is True and decision.shares == 24.69
    assert decision.shares == risk_gate._floor_to_step(10.0 / decision.effective_price)


# --- src/risk_gate.py:309-317: an unusable ledger view fails closed, it is never read as 0 --------

@pytest.mark.parametrize("state", [
    {"open_positions": 5, "daily_loss_usd": 0.0},
    {"open_positions": 0, "daily_loss_usd": 0.0},
    {"open_positions": 5.0, "daily_loss_usd": 12.5},          # an integral float is a whole number
    {"open_positions": 0.0, "daily_loss_usd": 0.0},
])
def test_a_whole_non_negative_state_is_readable(state):
    assert risk_gate._risk_state(state) == (int(state["open_positions"]), state["daily_loss_usd"])


@pytest.mark.parametrize("state", [
    {},                                                       # both fields missing
    {"open_positions": 1},                                    # daily_loss_usd missing
    {"daily_loss_usd": 0.0},                                  # open_positions missing
    {"open_positions": 5.5, "daily_loss_usd": 0.0},           # a fractional position count
    {"open_positions": 0.9999999999, "daily_loss_usd": 0.0},
    {"open_positions": -1, "daily_loss_usd": 0.0},
    {"open_positions": 0, "daily_loss_usd": -0.01},           # a negative realized loss
    {"open_positions": 0, "daily_loss_usd": math.nan},
    {"open_positions": 0, "daily_loss_usd": math.inf},
    {"open_positions": math.nan, "daily_loss_usd": 0.0},
    {"open_positions": math.inf, "daily_loss_usd": 0.0},
    {"open_positions": True, "daily_loss_usd": 0.0},          # a bool is not a count (_finite_number)
    {"open_positions": "3", "daily_loss_usd": 0.0},
    {"open_positions": None, "daily_loss_usd": None},
])
def test_an_unusable_state_reads_as_none(state):
    assert risk_gate._risk_state(state) is None


@pytest.mark.parametrize("bad", [
    {"open_positions": 5.5, "daily_loss_usd": 0.0},
    {"open_positions": 0, "daily_loss_usd": math.inf},
    {"open_positions": 0, "daily_loss_usd": -1.0},
])
def test_an_unusable_state_vetoes_instead_of_being_read_as_zero(bad, tmp_path):
    """A torn ledger must veto (Task AL turns this into a global halt), never allow a fresh order.

    The decision still echoes a size/an edge (paper_ledger.commit_if_allowed books nothing when allow
    is False: it records the veto and, for this reason, the global halt); the veto is the point.
    """
    state = dict(STATE, **bad)
    decision = evaluate(_intent(), load_risk(), state, halt_path=tmp_path / "halt.json",
                        halt_fallback_path=tmp_path / "HALT")
    assert decision.allow is False
    assert decision.reasons == ["invalid_risk_state"]
    # the daily-cap check never ran off the unreadable loss: no projected-loss veto was invented
    assert "max_daily_loss_projected" not in decision.reasons
    # a clean state with the same numbers does pass, so the veto really comes from the torn read
    clean = evaluate(_intent(), load_risk(), dict(STATE), halt_path=tmp_path / "halt.json",
                     halt_fallback_path=tmp_path / "HALT")
    assert clean.allow is True and clean.reasons == []


# --- src/risk_gate.py:281-284: the book minimum is a finite number >= 0 ---------------------------

def test_a_zero_minimum_order_size_is_usable_and_a_missing_one_is_not():
    """0 shares is a real book value (no minimum); anything unusable still has to veto."""
    assert risk_gate.min_order_size_value(0) == 0.0
    assert risk_gate.min_order_size_value(0.0) == 0.0
    assert risk_gate.min_order_size_value(5) == 5.0
    missing = evaluate(_intent(min_order_size=None), load_risk(), dict(STATE))
    assert missing.allow is False and "missing_min_order_size" in missing.reasons
