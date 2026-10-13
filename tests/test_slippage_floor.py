"""Task AU: RiskGate vetoes an intent that assumes less slippage than ASSUMED_SLIPPAGE.

The floor is Task AT's one slippage (src/thresholds.py, 0.005), inclusive. Missing, non-number or
non-finite slippage vetoes too. paper_ledger's CLI refuses --slippage below the floor before any
read or write. Offline, temp paths only.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

import pytest

from src import halt_store, paper_ledger, risk_gate
from src.podlib import load_risk
from src.risk_gate import Intent, evaluate
from src.thresholds import ASSUMED_SLIPPAGE, INTENT_ASSUMED_SLIPPAGE, SPORTS_DEVIG_ASSUMED_SLIPPAGE

NOW = "2026-10-08T08:00:00Z"
NOW_DT = datetime(2026, 10, 8, 8, tzinfo=timezone.utc)
STATE = {"open_positions": 0, "leg_already_open": False, "exposure_usd": 0.0, "leg_exposure_usd": 0.0, "opposite_leg_shares": 0.0, "daily_loss_usd": 0.0,
         "recent_fills": [], "own_quotes": []}
# Issue #226 (D4): a sell needs an open long leg (no naked short); the net is large so it never caps the size.
HELD = dict(STATE, open_positions=1, leg_already_open=True, leg_net_shares=1_000_000.0)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(halt_store, "HALT_STATE_PATH", tmp_path / "halt_state.json")
    monkeypatch.setattr(halt_store, "HALT_FALLBACK_PATH", tmp_path / "HALT")
    monkeypatch.setattr(paper_ledger, "PAPER_PATH", tmp_path / "paper.jsonl")
    monkeypatch.setattr(paper_ledger, "QUOTES_PATH", tmp_path / "quotes.jsonl")
    return tmp_path


def intent(slippage, side="buy", **kw):
    base = dict(strategy="task-au", market="au", side=side, model_prob=0.7 if side == "buy" else 0.3,
                market_price=0.4, assumed_slippage=slippage, fee_rate=0.02, liquidity_usd=20000.0,
                spread=0.01, token_id="t", book_ts=NOW)
    base.update(kw)
    base.setdefault("tags", ("test-market",))  # issue #131: a tagged market (missing tags veto)
    base.setdefault("min_order_size", 5.0)  # issue #226: a book minimum (missing vetoes)
    return Intent(**base)


def decide(slippage, side="buy", **kw):
    return evaluate(intent(slippage, side, **kw), load_risk(), dict(HELD if side == "sell" else STATE), now=NOW_DT)


def test_the_floor_is_the_one_slippage():
    assert risk_gate.ASSUMED_SLIPPAGE is ASSUMED_SLIPPAGE == 0.005
    assert INTENT_ASSUMED_SLIPPAGE == SPORTS_DEVIG_ASSUMED_SLIPPAGE == ASSUMED_SLIPPAGE


@pytest.mark.parametrize("side", ["buy", "sell"])
@pytest.mark.parametrize("slippage", [ASSUMED_SLIPPAGE, 0.0050000001, 0.01, 0.05])
def test_at_or_above_the_floor_allows(side, slippage):
    decision = decide(slippage, side)
    assert decision.allow is True, decision.reasons


@pytest.mark.parametrize("side", ["buy", "sell"])
@pytest.mark.parametrize("slippage", [0.0, 0, 0.004, 0.0049999999, 1e-12, -0.0])
def test_below_the_floor_vetoes(side, slippage):
    decision = decide(slippage, side)
    assert decision.allow is False
    assert decision.reasons == ["slippage_below_floor"]


def test_negative_slippage_vetoes_with_both_reasons():
    reasons = decide(-0.01).reasons
    assert "slippage_below_floor" in reasons and "negative_cost_assumption" in reasons


@pytest.mark.parametrize("value,reason", [
    (math.nan, "non_finite_assumed_slippage"), (math.inf, "non_finite_assumed_slippage"),
    (-math.inf, "non_finite_assumed_slippage"), (None, "invalid_assumed_slippage"),
    ("0.005", "invalid_assumed_slippage"), (True, "invalid_assumed_slippage"),
])
def test_missing_or_non_finite_slippage_vetoes(value, reason):
    decision = decide(value)
    assert decision.allow is False and reason in decision.reasons
    assert decision.edge == -1.0


def test_hypothetical_gate_test_intents_get_the_floor_too():
    decision = decide(0.0, strategy="phase0-gate-test", hypothetical=True)
    assert "slippage_below_floor" in decision.reasons


def test_commit_if_allowed_books_nothing_below_the_floor(monkeypatch):
    bus = []
    monkeypatch.setattr(paper_ledger, "bus", lambda *a, **k: bus.append(a))
    live = Intent(**{**intent(0.0).__dict__, "book_ts": paper_ledger.utc_now()})
    result = paper_ledger.commit_if_allowed(live, load_risk())
    assert result["allow"] is False and result["decision"]["reasons"] == ["slippage_below_floor"]
    assert not paper_ledger.PAPER_PATH.exists()
    assert [row[2] for row in bus] == ["veto"]


# ---------------------------------------------------------------- the CLI

@pytest.mark.parametrize("text", ["0", "0.0", "0.004", "0.0049999", "-0.01", "nan", "inf", "-inf", "abc", ""])
def test_cli_refuses_slippage_below_the_floor(text, capsys):
    with pytest.raises(SystemExit) as exc:
        paper_ledger.build_parser().parse_args(["--slippage", text])
    assert exc.value.code == 2
    assert "--slippage" in capsys.readouterr().err


@pytest.mark.parametrize("text,value", [("0.005", 0.005), ("0.01", 0.01), ("5e-3", 0.005)])
def test_cli_accepts_the_floor_and_above(text, value):
    assert paper_ledger.build_parser().parse_args(["--slippage", text]).slippage == value


def test_cli_default_is_the_floor():
    assert paper_ledger.build_parser().parse_args([]).slippage == ASSUMED_SLIPPAGE


def test_cli_main_with_slippage_zero_exits_before_any_read_or_write(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not run")
    monkeypatch.setattr(paper_ledger, "load_risk", boom)
    monkeypatch.setattr(paper_ledger, "commit_if_allowed", boom)
    code = paper_ledger.main(["--strategy", "au", "--market", "au", "--model-prob", "0.7", "--market-price", "0.4",
                              "--slippage", "0", "--liquidity", "20000", "--spread", "0.01", "--book-ts", NOW])
    assert code == paper_ledger.EXIT_USAGE == 2  # Task AS: usage errors exit through EXIT_USAGE
    assert not paper_ledger.PAPER_PATH.exists()


def test_slippage_arg_is_the_parser_type():
    assert paper_ledger.slippage_arg("0.005") == ASSUMED_SLIPPAGE
    with pytest.raises(Exception):
        paper_ledger.slippage_arg("0")
