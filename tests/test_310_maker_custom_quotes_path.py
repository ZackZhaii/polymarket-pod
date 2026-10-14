"""PR #310 review (Poly风控): maker.quote must read opposite-leg open buys from the quotes file it actually writes,
not the module-level QUOTES_PATH. Custom file holding an opposite open buy -> refused, nothing written."""
from __future__ import annotations

import json

import pytest

import src.maker as maker
import src.paper_ledger as paper_ledger
from tests.test_237b_maker_interleave import MARKET, NO, YES, _books, _ledger, _markets, _stamp


@pytest.fixture
def no_bus(monkeypatch):
    monkeypatch.setattr(paper_ledger, "bus", lambda *a, **k: None)


def test_custom_quotes_file_with_opposite_open_buy_refuses_buy(tmp_path, monkeypatch, no_bus):
    _ledger(tmp_path, monkeypatch, [])
    default = tmp_path / "default_quotes.jsonl"
    default.write_text("", encoding="utf-8")
    monkeypatch.setattr(paper_ledger, "QUOTES_PATH", default)
    monkeypatch.setattr(maker, "QUOTES_PATH", default)
    custom = tmp_path / "custom" / "quotes.jsonl"
    custom.parent.mkdir()
    row = {"ts": _stamp(-5), "market": MARKET, "token_id": NO, "side": "buy", "price": 0.40, "size": 5,
           "notional_usd": 2.0, "status": "open", "venue": "paper", "exchange_action": "none"}
    custom.write_text(json.dumps(row) + "\n", encoding="utf-8")
    before = custom.read_bytes()
    with pytest.raises(maker.QuoteRefused) as exc:
        maker.quote(market=MARKET, token_id=YES, side="buy", price=0.40, size=5, bus_path=tmp_path / "bus.jsonl",
                    quotes_path=custom, books_path=_books(tmp_path), markets_path=_markets(tmp_path))
    assert "structural_arb_disabled" in str(exc.value) or getattr(exc.value, "reason", None) == "structural_arb_disabled"
    assert custom.read_bytes() == before
    assert default.read_text(encoding="utf-8") == ""
