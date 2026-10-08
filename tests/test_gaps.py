"""Offline ask-sum tests. No network, no .env, no order SDK."""
from __future__ import annotations

import src.gaps as gaps


def test_yes_no_sum_below_one_is_a_gap_not_locked_profit():
    signal = gaps.binary_ask_sum(0.48, 0.47)
    assert signal["shape"] == "yes_no"
    assert signal["outcome_count"] == 2
    assert signal["ask_sum"] == 0.95
    assert signal["gap"] == 0.05
    assert signal["is_gap"] is True
    assert signal["locked_profit"] is False
    assert signal["single_leg_is_directional"] is True
    assert signal["one_leg_meaning"] == "directional"


def test_yes_no_sum_at_or_above_one_is_not_a_gap():
    at_one = gaps.binary_ask_sum(0.50, 0.50)
    assert at_one["ask_sum"] == 1.0
    assert at_one["is_gap"] is False
    assert at_one["gap"] == 0.0
    assert at_one["locked_profit"] is False
    above = gaps.binary_ask_sum(0.52, 0.50)
    assert above["is_gap"] is False
    assert above["ask_sum"] == 1.02


def test_multi_outcome_sum_below_one_is_a_gap_not_locked_profit():
    signal = gaps.multi_ask_sum([0.30, 0.30, 0.25])
    assert signal["shape"] == "multi"
    assert signal["outcome_count"] == 3
    assert signal["ask_sum"] == 0.85
    assert signal["gap"] == 0.15
    assert signal["is_gap"] is True
    assert signal["locked_profit"] is False
    assert gaps.fill_meaning(3, 1) == "directional"
    assert gaps.fill_meaning(3, 2) == "directional"


def test_multi_outcome_sum_not_below_one():
    signal = gaps.multi_ask_sum([0.40, 0.40, 0.20])
    assert signal["ask_sum"] == 1.0
    assert signal["is_gap"] is False
    full = gaps.multi_ask_sum([0.34, 0.33, 0.33])
    assert full["ask_sum"] == 1.0
    assert full["is_gap"] is False


def test_one_filled_leg_stays_directional_even_when_gap_exists():
    signal = gaps.binary_ask_sum(0.40, 0.40)
    assert signal["is_gap"] is True
    assert gaps.fill_meaning(signal["outcome_count"], 1) == "directional"
    assert gaps.fill_meaning(signal["outcome_count"], 2) == "all_legs_shown_still_not_a_profit_promise"
    assert signal["locked_profit"] is False


def test_incomplete_books_are_rejected():
    try:
        gaps.binary_ask_sum(0.40, 0.0)
    except ValueError as exc:
        assert "inside" in str(exc)
    else:
        raise AssertionError("zero ask should be rejected")
    try:
        gaps.multi_ask_sum([0.40, 0.40])
    except ValueError as exc:
        assert "three" in str(exc)
    else:
        raise AssertionError("two asks are not multi-outcome")
