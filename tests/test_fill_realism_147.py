"""Issue #210 / #147 leftover: operator CLI must not default --fee-rate to 0.0, and CLI
intents without book depth/levels must not book fills (tighten-only, non-KEY).

Offline. No network, no credentials. config/risk.toml is read, never written.
"""
from __future__ import annotations

import argparse

import pytest

import src.paper_ledger as paper_ledger
from src.podlib import load_risk


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(paper_ledger, "PAPER_PATH", tmp_path / "paper.jsonl")
    monkeypatch.setattr(paper_ledger, "QUOTES_PATH", tmp_path / "quotes.jsonl")
    monkeypatch.setattr(paper_ledger, "bus", lambda *args, **kwargs: None)
    return tmp_path / "paper.jsonl"


def _cli(**kw) -> argparse.Namespace:
    """Same shape as tests/test_category.py::_cli (the operator CLI's parsed args)."""
    base = dict(
        strategy="manual",
        market="m1",
        side="buy",
        model_prob=0.7,
        market_price=0.4,
        slippage=0.005,
        fee_rate=0.04,
        liquidity=20000.0,
        spread=0.01,
        token_id="t",
        book_ts=paper_ledger.utc_now(),
        note="",
        tags="us",
        min_order_size=5.0,
    )
    base.update(kw)
    return argparse.Namespace(**base)


def test_cli_fee_rate_has_no_zero_default():
    """#147 item 6: --fee-rate must be required (or None); a silent 0.0 books free trades."""
    args = ["--strategy", "manual", "--market", "m1", "--model-prob", "0.7", "--market-price", "0.4"]
    try:
        parsed = paper_ledger.build_parser().parse_args(args)
    except SystemExit:
        return  # required argument: the stricter outcome
    assert parsed.fee_rate is None, f"--fee-rate defaults to {parsed.fee_rate!r}"


def test_operator_cli_without_book_levels_books_nothing(ledger):
    """#147 item 1: operator CLI has no ask levels / depth evidence, so it must not book."""
    result = paper_ledger.run_cli_intent(_cli(), load_risk())
    assert result["allow"] is False, "operator CLI booked a fill with no depth evidence"
    reasons = set(result["decision"]["reasons"])
    assert {"missing_book_levels", "insufficient_depth"} & reasons
    assert not ledger.exists() or ledger.read_text() == ""


def test_cli_without_a_fee_rate_never_books_a_zero_fee(ledger):
    """Explicit fee_rate=None must refuse; never book as a free (0) fee."""
    result = paper_ledger.run_cli_intent(_cli(fee_rate=None), load_risk())
    assert result["allow"] is False
    assert {"missing_fee_data", "invalid_fee_rate"} & set(result["decision"]["reasons"])
    assert not ledger.exists() or ledger.read_text() == ""
