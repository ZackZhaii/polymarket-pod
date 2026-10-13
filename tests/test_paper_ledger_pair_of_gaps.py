"""Issue #145 gap: ``paper_ledger.pair_of``, the scanner-row reader behind the complementary-leg veto.

``pair_of`` is the only boundary between the scanner's ``markets.jsonl`` (podlib.MARKETS_PATH shape) and
``wash_detect.pair_cross_problems``: ``load_state`` puts its result into the state key ``market_pair``, and
RiskGate's mint / merge pair check reads that key. A pair it cannot prove must come back as ``None`` (fail
closed: ``wash_input_invalid``), and a stale pair must never be reused once the newest row for the market
says the market is no longer a clean binary pair.

tests/test_pair_cross.py pins ``binary_pair`` (the row shape) and one ``load_state`` happy path; the reader
itself -- which row wins, and what an unreadable file does -- was untested. Offline, tmp_path files only;
no network, no credentials, no orders, no behaviour change.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src import paper_ledger
from src.podlib import load_risk
from src.risk_gate import Intent, evaluate
from src.wash_detect import WASH_INPUT_INVALID, wash_problems

NOW = datetime(2026, 10, 9, 0, 0, 0, tzinfo=timezone.utc)
MARKET = "gap-market"
YES, NO = "gap-yes", "gap-no"
CLEAN = "clean-strategy"


def _iso(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def _append(path: Path, rows) -> Path:
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")
    return path


def _quote(side: str = "buy", token: str = YES, price: float = 0.60) -> dict:
    """One own resting (uncancelled) paper quote on the other token of MARKET (wash_detect.pair_quotes)."""
    return {"ts": _iso(NOW), "market": MARKET, "side": side, "price": price, "size": 5, "status": "open",
            "venue": "paper", "token_id": token}


def _intent(**overrides) -> Intent:
    fields: dict[str, object] = {"strategy": CLEAN, "market": MARKET, "token_id": NO, "side": "buy",
                                "model_prob": 0.80, "market_price": 0.41, "assumed_slippage": 0.005,
                                "fee_rate": 0.0, "liquidity_usd": 20_000.0, "spread": 0.01,
                                "hypothetical": False, "book_ts": _iso(NOW), "tags": ("gap",),
                                "min_order_size": 5.0}
    fields.update(overrides)
    return Intent(**fields)  # type: ignore[arg-type]


@pytest.fixture
def files(tmp_path, monkeypatch) -> dict[str, Path]:
    paths = {"paper": tmp_path / "paper.jsonl", "quotes": tmp_path / "quotes.jsonl",
             "markets": tmp_path / "markets.jsonl"}
    monkeypatch.setattr(paper_ledger, "PAPER_PATH", paths["paper"])
    monkeypatch.setattr(paper_ledger, "QUOTES_PATH", paths["quotes"])
    monkeypatch.setattr(paper_ledger, "PAIR_MARKETS_PATH", paths["markets"])
    return paths


def _wash(state: dict, price: float = 0.41):
    return wash_problems(market=MARKET, token_id=NO, side="buy", strategy=CLEAN, counterparty=None,
                         state=state, now=NOW, price=price)


# --- the reader: which row is the market's pair ---------------------------------------------------------

def test_a_clean_row_gives_the_two_tokens_stripped(tmp_path, monkeypatch):
    markets = tmp_path / "markets.jsonl"
    monkeypatch.setattr(paper_ledger, "PAIR_MARKETS_PATH", markets)
    _append(markets, [{"id": MARKET, "token_ids": [f"  {YES} ", NO]}, {"id": "other", "token_ids": ["a", "b"]}])
    assert paper_ledger.pair_of(MARKET) == (YES, NO)  # binary_pair strips; another market's row is ignored


def test_the_newest_row_for_the_market_wins(tmp_path, monkeypatch):
    markets = tmp_path / "markets.jsonl"
    monkeypatch.setattr(paper_ledger, "PAIR_MARKETS_PATH", markets)
    _append(markets, [{"id": MARKET, "token_ids": [YES, NO]}, {"id": "other", "token_ids": ["a", "b"]},
                      {"id": MARKET, "token_ids": ["new-yes", "new-no"]}])
    assert paper_ledger.pair_of(MARKET) == ("new-yes", "new-no")


@pytest.mark.parametrize("newest", [
    {"id": MARKET, "token_ids": [YES, NO], "neg_risk": True},          # a neg-risk market has no clean pair
    {"id": MARKET, "token_ids": [YES, NO], "negRisk": True},
    {"id": MARKET, "token_ids": [YES, NO], "token_ids_problem": "mismatch"},
    {"id": MARKET, "token_ids": ["yes", "no", "maybe"]},               # not exactly two tokens
    {"id": MARKET, "token_ids": [YES, YES]},                           # not distinct
    {"id": MARKET},                                                    # no token_ids at all
])
def test_a_newest_unusable_row_is_unknown_never_the_older_pair(tmp_path, monkeypatch, newest):
    """The stale pair of an older row must not be handed to RiskGate once the newest row is unusable."""
    markets = tmp_path / "markets.jsonl"
    monkeypatch.setattr(paper_ledger, "PAIR_MARKETS_PATH", markets)
    _append(markets, [{"id": MARKET, "token_ids": [YES, NO]}, newest])
    assert paper_ledger.pair_of(MARKET) is None


def test_the_market_id_is_compared_as_text_and_a_row_without_an_id_is_ignored(tmp_path, monkeypatch):
    markets = tmp_path / "markets.jsonl"
    monkeypatch.setattr(paper_ledger, "PAIR_MARKETS_PATH", markets)
    _append(markets, [{"token_ids": [YES, NO]}, {"id": "gap-market-2", "token_ids": ["a", "b"]},
                      {"id": 42, "token_ids": ["int-yes", "int-no"]}])
    assert paper_ledger.pair_of("42") == ("int-yes", "int-no")  # str(row["id"]) == str(market)
    assert paper_ledger.pair_of(MARKET) is None                 # no row carries this id


# --- the reader: an unusable file is unknown, never a guess ---------------------------------------------

def test_a_missing_or_unreadable_scanner_file_is_unknown(tmp_path, monkeypatch):
    missing = tmp_path / "no-markets.jsonl"
    monkeypatch.setattr(paper_ledger, "PAIR_MARKETS_PATH", missing)
    assert paper_ledger.pair_of(MARKET) is None
    directory = tmp_path / "markets-dir"
    directory.mkdir()
    monkeypatch.setattr(paper_ledger, "PAIR_MARKETS_PATH", directory)
    assert paper_ledger.pair_of(MARKET) is None  # OSError: unknown, not a crash


def test_non_utf8_scanner_bytes_are_unknown(tmp_path, monkeypatch):
    markets = tmp_path / "markets.jsonl"
    markets.write_bytes(b"\xff\xfe\x00{")
    monkeypatch.setattr(paper_ledger, "PAIR_MARKETS_PATH", markets)
    assert paper_ledger.pair_of(MARKET) is None  # UnicodeDecodeError: unknown, not a crash


def test_unreadable_lines_are_skipped_and_a_garbage_only_file_is_unknown(tmp_path, monkeypatch):
    markets = tmp_path / "markets.jsonl"
    monkeypatch.setattr(paper_ledger, "PAIR_MARKETS_PATH", markets)
    markets.write_text("{not json\n[]\nnull\n" + json.dumps({"id": MARKET, "token_ids": [YES, NO]}) + "\n",
                       encoding="utf-8")
    assert paper_ledger.pair_of(MARKET) == (YES, NO)  # read_jsonl skips the bad lines, the row is still found
    markets.write_text("{not json\n[]\nnull\n", encoding="utf-8")
    assert paper_ledger.pair_of(MARKET) is None


# --- through load_state: the state key RiskGate reads ---------------------------------------------------

def test_load_state_vetoes_when_the_newest_row_makes_the_pair_unknown(files):
    _append(files["quotes"], [_quote()])
    _append(files["markets"], [{"id": MARKET, "token_ids": [YES, NO]},
                               {"id": MARKET, "token_ids": [YES, NO], "neg_risk": True}])
    state = paper_ledger.load_state(MARKET, NO)
    assert state["market_pair"] is None
    assert _wash(state) == [WASH_INPUT_INVALID]
    decision = evaluate(_intent(), load_risk(), state, now=NOW)
    assert decision.allow is False and WASH_INPUT_INVALID in decision.reasons


def test_load_state_vetoes_when_the_scanner_file_cannot_be_read(files):
    _append(files["quotes"], [_quote()])
    files["markets"].mkdir()  # exists but unreadable: the pair is unknown, not absent
    state = paper_ledger.load_state(MARKET, NO)
    assert state["market_pair"] is None
    assert _wash(state) == [WASH_INPUT_INVALID]
    assert evaluate(_intent(), load_risk(), state, now=NOW).allow is False
