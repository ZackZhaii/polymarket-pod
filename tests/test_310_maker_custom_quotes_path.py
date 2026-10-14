"""PR #310 review (Poly风控, reverse bypass): paper_ledger.commit_if_allowed and load_state's default read only
paper_ledger.QUOTES_PATH, so a maker buy written to any other quotes file could pair unseen with the opposite leg
in the default file. maker.quote now refuses (structural_arb_disabled) any buy whose quotes_path is not
paper_ledger.QUOTES_PATH, before writing anything. Offline: tmp_path only, no network, no orders."""
from __future__ import annotations

import json

import pytest

import src.maker as maker
import src.paper_ledger as paper_ledger
from src.podlib import read_jsonl
from tests.test_237b_maker_interleave import MARKET, NO, YES, _books, _ledger, _markets


@pytest.fixture
def files(tmp_path, monkeypatch):
    _ledger(tmp_path, monkeypatch, [])
    default = tmp_path / "default_quotes.jsonl"
    default.write_text("", encoding="utf-8")
    monkeypatch.setattr(paper_ledger, "QUOTES_PATH", default)
    monkeypatch.setattr(maker, "QUOTES_PATH", default)
    custom = tmp_path / "custom" / "quotes.jsonl"
    custom.parent.mkdir()
    custom.write_text("", encoding="utf-8")
    return {"default": default, "custom": custom}


def _buy(tmp_path, token, quotes_path):
    return maker.quote(market=MARKET, token_id=token, side="buy", price=0.40, size=5, bus_path=tmp_path / "bus.jsonl",
                       quotes_path=quotes_path, books_path=_books(tmp_path), markets_path=_markets(tmp_path))


def _buys(path):
    return [r for r in read_jsonl(path) if r.get("side") == "buy"] if path.exists() else []


def test_custom_file_first_then_opposite_leg_via_default(tmp_path, files):
    """(a) custom NO buy refused before any write; the default YES buy is then the only buy line."""
    with pytest.raises(maker.QuoteRefused) as exc:
        _buy(tmp_path, NO, files["custom"])
    assert exc.value.reason == "structural_arb_disabled"
    assert _buys(files["custom"]) == [] and _buys(files["default"]) == []
    _buy(tmp_path, YES, files["default"])
    assert [r["token_id"] for r in _buys(files["default"])] == [YES]
    assert _buys(files["custom"]) == []


def test_default_first_then_opposite_leg_via_custom_file(tmp_path, files):
    """(b) reverse order: default YES buy written, then the custom NO buy is refused; no new buy line anywhere."""
    _buy(tmp_path, YES, files["default"])
    before = files["default"].read_bytes()
    with pytest.raises(maker.QuoteRefused) as exc:
        _buy(tmp_path, NO, files["custom"])
    assert exc.value.reason == "structural_arb_disabled"
    assert files["default"].read_bytes() == before
    assert _buys(files["custom"]) == []


def test_custom_file_with_opposite_open_buy_refuses_and_writes_nothing(tmp_path, files):
    row = {"ts": "2026-10-14T00:00:00.000Z", "market": MARKET, "token_id": NO, "side": "buy", "price": 0.40,
           "size": 5, "notional_usd": 2.0, "status": "open", "venue": "paper", "exchange_action": "none"}
    files["custom"].write_text(json.dumps(row) + "\n", encoding="utf-8")
    before = files["custom"].read_bytes()
    with pytest.raises(maker.QuoteRefused) as exc:
        _buy(tmp_path, YES, files["custom"])
    assert exc.value.reason == "structural_arb_disabled"
    assert files["custom"].read_bytes() == before
    assert files["default"].read_text(encoding="utf-8") == ""
