"""Issue #350: JSON integers too large for float must fail closed, offline."""
from __future__ import annotations

import json

import pytest

from src import fees, ledger_store, paper_ledger

HUGE = 10**400
FEE_FIELDS = ("fee_rate", "market_price", "effective_price")


def _fill():
    # Hand-derived complete snapshot: 1000 cash - 50 notional - 5 fee = 945.
    return {
        "ts": "2026-10-16T08:00:00Z", "fill_id": "f-350",
        "market": "issue-350", "token_id": "yes", "side": "buy",
        "shares": 100.0, "notional_usd": 50.0, "fee_usd": 5.0,
        "fee_rate": 0.1, "market_price": 0.5, "effective_price": 0.5,
        "hypothetical": False, "counts_toward_risk": True, "risk_decision": "allow",
        "cash_after_usd": 945.0, "leg_shares_after": 100.0,
        "leg_cost_basis_after_usd": 50.0,
    }


def _bytes(row):
    return (json.dumps(row) + "\n").encode("utf-8")


@pytest.mark.parametrize("value", [HUGE, -HUGE], ids=["positive", "negative"])
def test_finite_rejects_integer_conversion_overflow(value):
    assert fees._finite(value) is None


@pytest.mark.parametrize("field", FEE_FIELDS)
@pytest.mark.parametrize("value", [HUGE, -HUGE], ids=["positive", "negative"])
def test_fee_per_share_rejects_integer_conversion_overflow(field, value):
    inputs = {"fee_rate": 0.1, "market_price": 0.5, "effective_price": 0.5}
    inputs[field] = value
    assert fees.fee_per_share(**inputs) is None


@pytest.mark.parametrize("field", FEE_FIELDS)
@pytest.mark.parametrize("value", [HUGE, -HUGE], ids=["positive", "negative"])
def test_verify_reports_the_overflow_as_an_invalid_fee(field, value):
    row = _fill()
    row[field] = value
    assert ledger_store.verify(ledger_store.parse_ledger(_bytes(row))) == ["invalid_fee:f-350"]


@pytest.mark.parametrize("field", FEE_FIELDS)
@pytest.mark.parametrize("value", [HUGE, -HUGE], ids=["positive", "negative"])
def test_load_state_fails_closed_on_fee_input_overflow(field, value, tmp_path, monkeypatch):
    row = _fill()
    row[field] = value
    path = tmp_path / "paper.jsonl"
    path.write_bytes(_bytes(row))
    monkeypatch.setattr(paper_ledger, "PAPER_PATH", path)
    monkeypatch.setattr(paper_ledger, "QUOTES_PATH", tmp_path / "quotes.jsonl")

    state = paper_ledger.load_state("issue-350", "yes")

    assert state["daily_loss_usd"] is None
    assert state["ledger_problems"] == ["invalid_fee:f-350"]
    assert state["quote_problems"] == []


def test_ordinary_values_and_complete_conservation_snapshot_remain_valid():
    assert fees._finite(0.1) == 0.1
    assert fees.fee_per_share(0.1, 0.5, 0.5) == pytest.approx(0.05)
    assert ledger_store.verify(ledger_store.parse_ledger(_bytes(_fill()))) == []
