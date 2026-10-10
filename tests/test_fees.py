"""Task AE (backlog #19): one fee module, src/fees.py, used by RiskGate and the paper ledger.

stress_fee (RiskGate edge stress, USD per share) and ledger_fee (USD booked for a simulated
fill) share fee_per_share = fee_rate * max(market_price, effective_price, 0): the higher price
wins. tests/test_fee_characterisation.py proves no fee went down on a grid; this file pins the
exact new numbers, the fail-closed paths and the wiring. Offline, no credentials.
"""
from __future__ import annotations

import ast
import hashlib
import inspect
import math
from datetime import datetime, timezone

import pytest

import src.fees as fees
import src.paper_ledger as paper_ledger
import src.risk_gate as risk_gate
from src.podlib import RISK_PATH, load_risk, utc_now
from src.risk_gate import Intent, evaluate

NOW = "2026-10-08T08:00:00Z"
NOW_DT = datetime(2026, 10, 8, 8, tzinfo=timezone.utc)
STATE = {"open_positions": 0, "leg_already_open": False, "exposure_usd": 0.0, "leg_exposure_usd": 0.0, "daily_loss_usd": 0.0,
         "recent_fills": [], "own_quotes": []}
RISK_SHA = "0aea23af1e938d330b5b4fab2c6abdec9f475f16807d177a5860a4da32aebc04"
INVALID = [math.nan, math.inf, -math.inf, "0.02", None, True]


def _intent(**overrides: object) -> Intent:
    fields: dict[str, object] = {
        "strategy": "task-ae", "market": "ae", "token_id": "t", "side": "buy",
        "model_prob": 0.70, "market_price": 0.40, "assumed_slippage": 0.005, "fee_rate": 0.02,
        "liquidity_usd": 20000.0, "spread": 0.01, "book_ts": NOW,
    }
    fields.update(overrides)
    return Intent(**fields)  # type: ignore[arg-type]


def _eval(**overrides: object):
    return evaluate(_intent(**overrides), load_risk(), STATE, now=NOW_DT)


# --- the module ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fee_rate", "market", "effective", "expected"),
    [
        (0.02, 0.40, 0.405, 0.0081),  # buy: effective is higher
        (0.02, 0.40, 0.395, 0.008),  # sell: market is higher
        (0.1, 0.95, 0.955, 0.0955),
        (0.02, 0.01, -0.04, 0.0002),  # sell with huge slippage: market wins
        (0.02, -0.1, -0.2, 0.0),  # never a negative fee (a rebate)
        (-0.02, -0.1, -0.2, 0.004),  # old fee_rate * market_price = 0.002; the higher wins
        (0.0, 0.40, 0.405, 0.0),
    ],
)
def test_fee_per_share_charges_the_higher_price(fee_rate, market, effective, expected):
    assert fees.fee_per_share(fee_rate, market, effective) == pytest.approx(expected)
    assert fees.stress_fee(fee_rate, market, effective) == fees.fee_per_share(fee_rate, market, effective)
    assert fees.FEE_PRICE_BASIS == "max(market_price, effective_price)"


@pytest.mark.parametrize("bad", INVALID, ids=lambda v: repr(v))
def test_invalid_fee_rate_fails_closed(bad):
    assert fees.stress_fee(bad, 0.40, 0.405) is None
    assert fees.ledger_fee(bad, 0.40, 0.405, 10.0, 24.69) is None


@pytest.mark.parametrize(("market", "effective"), [(0.40, 0.405), (0.40, 0.395), (-0.1, -0.1), (1.5, 1.55)])
def test_negative_fee_rate_is_never_a_rebate_and_never_below_the_old_fee(market, effective):
    stress = fees.stress_fee(-0.02, market, effective)
    assert stress >= 0.0 and stress >= -0.02 * market
    booked = fees.ledger_fee(-0.02, market, effective, 10.0, 10.0 / abs(effective))
    assert booked >= 0.0 and booked >= 10.0 * -0.02


@pytest.mark.parametrize("bad", INVALID, ids=lambda v: repr(v))
def test_invalid_price_fails_closed_for_the_stress_fee(bad):
    assert fees.stress_fee(0.02, bad, 0.405) is None
    assert fees.stress_fee(0.02, 0.40, bad) is None


def test_ledger_fee_takes_the_higher_of_notional_and_per_share():
    # sell 10 USD at effective 0.395 (market 0.40): 25.3164... shares * 0.02 * 0.40
    shares = 10.0 / 0.395
    assert fees.ledger_fee(0.02, 0.40, 0.395, 10.0, shares) == pytest.approx(0.2025316456)
    # buy: the per-share basis is the effective price, i.e. the old notional * fee_rate
    assert fees.ledger_fee(0.02, 0.40, 0.405, 10.0, 10.0 / 0.405) == pytest.approx(0.2)
    assert fees.ledger_fee(0.02, 0.40, 0.405, 10.0, 10.0 / 0.405) >= 10.0 * 0.02


@pytest.mark.parametrize("shares", [0.0, -1.0, math.nan, math.inf, None, "25"])
def test_ledger_fee_never_below_notional_times_rate_without_usable_shares(shares):
    assert fees.ledger_fee(0.02, 0.40, 0.395, 10.0, shares) == pytest.approx(0.2)


@pytest.mark.parametrize("notional", [0.0, -5.0])
def test_ledger_fee_is_zero_without_size(notional):
    assert fees.ledger_fee(0.02, 0.40, 0.405, notional, 1.0) == 0.0


@pytest.mark.parametrize("notional", [math.nan, math.inf, None, "10"])
def test_ledger_fee_invalid_notional_fails_closed(notional):
    assert fees.ledger_fee(0.02, 0.40, 0.405, notional, 1.0) is None


def test_ledger_fee_with_invalid_prices_keeps_the_notional_fee():
    assert fees.ledger_fee(0.02, math.nan, 0.405, 10.0, 24.0) == pytest.approx(0.2)


def test_booked_fee_rounding_is_unchanged():
    assert fees.booked_fee_usd(0.20253164556962025) == 0.20253165
    assert fees.booked_fee_usd(0.20000000000000004) == 0.2


def test_fee_module_is_pure_and_has_no_threshold():
    tree = ast.parse(inspect.getsource(fees))
    imports = [node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))]
    assert all(not (getattr(node, "module", "") or "").startswith("src") for node in imports)
    assert [alias.name for node in imports for alias in node.names] == ["annotations", "math"]
    source = inspect.getsource(fees)
    assert "open(" not in source and "urlopen" not in source
    numbers = [
        node.targets[0].id for node in tree.body
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, (int, float))
    ]
    assert numbers == []  # no fee constant in code


def test_risk_toml_has_no_fee_key_and_is_unchanged():
    data = RISK_PATH.read_bytes()
    assert "fee" not in data.decode("utf-8").lower()
    assert hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest() == RISK_SHA
    assert load_risk()["live_enabled"] is False


# --- the wiring: only src/fees.py does fee arithmetic --------------------------------------------


@pytest.mark.parametrize("module", [risk_gate, paper_ledger])
def test_no_fee_arithmetic_outside_the_fee_module(module):
    tree = ast.parse(inspect.getsource(module))
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Mult, ast.Div)):
            names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            names |= {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}
            assert "fee_rate" not in names and "fee_usd" not in names, ast.unparse(node)
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "round":
            assert "fee" not in ast.unparse(node), ast.unparse(node)


def test_risk_gate_and_ledger_call_the_named_functions():
    source = inspect.getsource(risk_gate.evaluate)
    assert "fees.stress_fee(" in source and "fees.ledger_fee(" in source
    ledger = inspect.getsource(paper_ledger)
    assert ledger.count("booked_fee_usd(decision.fee_usd)") == 2
    assert "max(market_price, effective_price)" in risk_gate.EDGE_FORMULA_BUY
    assert "max(market_price, effective_price)" in risk_gate.EDGE_FORMULA_SELL


# --- RiskGate: exact new numbers ----------------------------------------------------------------


def test_buy_edge_pays_the_fee_on_the_effective_price():
    decision = _eval(model_prob=0.70)
    assert decision.edge == pytest.approx(0.70 - 0.405 - 0.02 * 0.405)  # 0.2869 (was 0.287)
    # issue #104: 24.20 shares fit 10 USD all-in at 0.405 + 0.0081; the fee per share is unchanged
    assert decision.shares == 24.20
    assert decision.fee_usd == pytest.approx(24.20 * 0.02 * 0.405)  # = notional * fee_rate, as before
    assert decision.notional_usd + decision.fee_usd <= 10.0
    assert decision.allow is True


def test_sell_books_the_fee_on_the_market_price():
    decision = _eval(side="sell", model_prob=0.30)
    assert decision.edge == pytest.approx(0.395 - 0.30 - 0.02 * 0.40)  # 0.087, unchanged
    # issue #104: 24.81 shares fit 10 USD all-in at 0.395 + 0.008; the fee is still charged on market_price
    assert decision.shares == 24.81
    assert decision.fee_usd == pytest.approx(24.81 * 0.02 * 0.40)  # > notional * fee_rate
    assert decision.fee_usd > decision.notional_usd * 0.02
    assert decision.allow is True


def test_more_conservative_edge_can_newly_veto():
    # buy at 0.40 + 0.005, fee 0.02, model 0.433: the old edge 0.433 - 0.405 - 0.02 * 0.40 = 0.020
    # passed (inclusive); the new 0.433 - 0.405 - 0.02 * 0.405 = 0.0199 is edge_below_min.
    decision = _eval(model_prob=0.433)
    assert decision.edge == pytest.approx(0.0199)
    assert decision.reasons == ["edge_below_min"]


def test_negative_fee_rate_is_no_longer_a_rebate():
    decision = _eval(fee_rate=-0.02)
    assert decision.allow is False
    assert decision.reasons == ["negative_cost_assumption"]
    assert decision.edge == pytest.approx(0.295) and decision.fee_usd == 0.0  # was edge 0.303, fee -0.2


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf], ids=["nan", "inf", "-inf"])
def test_non_finite_fee_rate_stays_task_u_fail_closed(bad):
    decision = _eval(fee_rate=bad)
    assert decision.allow is False and decision.edge == -1.0 and decision.fee_usd == 0.0
    assert "non_finite_fee_rate" in decision.reasons


def test_invalid_fee_backstop_vetoes_a_clean_intent(monkeypatch):
    monkeypatch.setattr(fees, "ledger_fee", lambda *args: None)
    decision = _eval()
    assert decision.allow is False and decision.reasons == ["invalid_fee"] and decision.fee_usd == 0.0


def test_stress_fee_none_forces_edge_minus_one(monkeypatch):
    monkeypatch.setattr(fees, "stress_fee", lambda *args: None)
    decision = _eval()
    assert decision.allow is False and decision.edge == -1.0
    assert "non_positive_edge" in decision.reasons


# --- paper ledger -----------------------------------------------------------------------------


def _ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(paper_ledger, "PAPER_PATH", tmp_path / "paper.jsonl")
    monkeypatch.setattr(paper_ledger, "QUOTES_PATH", tmp_path / "quotes.jsonl")
    monkeypatch.setattr(paper_ledger, "bus", lambda *args, **kwargs: None)


def test_ledger_books_the_higher_sell_fee(tmp_path, monkeypatch):
    _ledger(tmp_path, monkeypatch)
    result = paper_ledger.commit_if_allowed(_intent(side="sell", model_prob=0.30, book_ts=utc_now()), load_risk())
    assert result["allow"] is True
    assert result["fill"]["fee_usd"] == 0.19848  # 24.81 shares * 0.02 * 0.40 (issue #104 sizing)
    assert result["fill"]["edge_formula"] == risk_gate.EDGE_FORMULA_SELL
    assert paper_ledger.load_state("ae", "t")["daily_loss_usd"] == 0.19848


def test_ledger_buy_fee_is_unchanged(tmp_path, monkeypatch):
    _ledger(tmp_path, monkeypatch)
    result = paper_ledger.commit_if_allowed(_intent(book_ts=utc_now()), load_risk())
    assert result["fill"]["fee_usd"] == 0.19602  # 24.20 shares * 0.02 * 0.405 (issue #104 sizing)
    assert result["fill"]["fee_usd"] == round(result["fill"]["notional_usd"] * 0.02, 8)  # rate unchanged


def test_ledger_still_fails_closed_on_a_non_finite_fee_row(tmp_path, monkeypatch):
    _ledger(tmp_path, monkeypatch)
    (tmp_path / "paper.jsonl").write_text(
        '{"ts": "%s", "market": "m", "token_id": "t", "side": "buy", "shares": 1.0, '
        '"notional_usd": 0.4, "fee_usd": NaN, "hypothetical": false}\n' % utc_now(), encoding="utf-8")
    state = paper_ledger.load_state("m", "t")
    assert state["daily_loss_usd"] is None and state["non_finite_ledger_rows"] == [0]
    decision = evaluate(_intent(book_ts=utc_now()), load_risk(), state)
    assert decision.allow is False and "invalid_risk_state" in decision.reasons
