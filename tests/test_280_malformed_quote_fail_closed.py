"""PR #280 (Task DD) Codex P2 @ 2026-10-13T12:30Z on head 2a354f9: a malformed paper quote row makes
load_state raise (TypeError in _opposite_pending_buys, src/paper_ledger.py:411) instead of a veto.
Expected everywhere: fail closed -> no uncaught exception, the buy is refused with an explicit reason
(wash_input_invalid or structural_arb_disabled), nothing is booked.
Also: opposite_pending_buys read failures (file / content / field type) refuse the buy.
Offline paper ledger only: no network, no credentials, no orders. Style follows tests/test_dd_opposite_leg_fail_closed.py.
"""
from __future__ import annotations

import json
import math
import os

import pytest

import src.paper_ledger as paper_ledger
from src.podlib import load_risk
from src.risk_gate import evaluate
from tests.test_single_leg_exposure import MARKET, NO, YES, _intent, _ledger

STRUCTURAL = "structural_arb_disabled"
WASH_INVALID = "wash_input_invalid"
FAIL_CLOSED = {STRUCTURAL, WASH_INVALID}
GOOD = {"market": MARKET, "token_id": YES, "side": "buy", "price": 0.45, "status": "open",
        "cancelled_by": None, "cancelled_ts": None}


def _buy(token: str = NO, market: str = MARKET):
    return _intent(market=market, token_id=token, side="buy", model_prob=0.70, market_price=0.40, min_order_size=5.0)


@pytest.fixture
def no_bus(monkeypatch):
    monkeypatch.setattr(paper_ledger, "bus", lambda *a, **k: None)


def _write_raw(tmp_path, text: str | bytes) -> None:
    path = tmp_path / "quotes.jsonl"
    path.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))


def _row(**overrides) -> str:
    row = dict(GOOD)
    for key, value in overrides.items():
        if value == "<drop>":
            row.pop(key, None)
        else:
            row[key] = value
    return json.dumps(row) + "\n"  # json.dumps writes NaN / Infinity tokens, which json.loads reads back


def _assert_refused(tmp_path, monkeypatch, *, market: str = MARKET):
    """load_state + evaluate and commit_if_allowed: no exception, refused, explicit reason, nothing booked."""
    state = paper_ledger.load_state(market, NO)  # must not raise
    decision = evaluate(_buy(market=market), load_risk(), state)
    assert decision.allow is False, decision.reasons
    assert FAIL_CLOSED & set(decision.reasons), decision.reasons
    result = paper_ledger.commit_if_allowed(_buy(market=market), load_risk())  # must not raise
    assert result["allow"] is False
    assert FAIL_CLOSED & set(result["decision"]["reasons"]), result["decision"]["reasons"]
    assert paper_ledger.load_state(market, NO).get("leg_net_shares") in (0.0, None)


# ---------------------------------------------------------------- Codex P2 exact repro

def test_codex_p2_exact_quote_market_int_status_list(tmp_path, monkeypatch, no_bus):
    _ledger(tmp_path, monkeypatch, [])
    _write_raw(tmp_path, json.dumps({"market": 123, "status": [], "side": "buy", "token_id": "tok-yes"}) + "\n")
    _assert_refused(tmp_path, monkeypatch, market="123")


# ---------------------------------------------------------------- malformed fields on a same-market quote

MALFORMED = [
    ("status_list", dict(status=[])),
    ("status_dict", dict(status={"s": "open"})),
    ("status_int", dict(status=1)),
    ("status_none", dict(status=None)),
    ("status_missing", dict(status="<drop>")),
    ("status_unknown_str", dict(status="weird")),
    ("side_list", dict(side=["buy"])),
    ("side_none", dict(side=None)),
    ("side_missing", dict(side="<drop>")),
    ("token_list", dict(token_id=["tok-yes"])),
    ("token_dict", dict(token_id={"t": 1})),
    ("token_none", dict(token_id=None)),
    ("token_missing", dict(token_id="<drop>")),
    ("market_list", dict(market=[MARKET])),
    ("market_dict", dict(market={"id": MARKET})),
    ("market_none", dict(market=None)),
    ("market_missing", dict(market="<drop>")),
    ("price_str", dict(price="0.45")),
    ("price_none", dict(price=None)),
    ("price_nan", dict(price=math.nan)),
    ("price_inf", dict(price=math.inf)),
    ("price_neg", dict(price=-0.1)),
    ("price_gt1", dict(price=1.5)),
    ("price_bool", dict(price=True)),
    ("price_missing", dict(price="<drop>")),
]


@pytest.mark.parametrize("overrides", [m[1] for m in MALFORMED], ids=[m[0] for m in MALFORMED])
def test_malformed_quote_field_refuses_without_exception(tmp_path, monkeypatch, no_bus, overrides):
    _ledger(tmp_path, monkeypatch, [])
    _write_raw(tmp_path, _row(**overrides))
    _assert_refused(tmp_path, monkeypatch)


# ---------------------------------------------------------------- broken quote file content

BROKEN = [
    ("bad_json", '{"market": "task-p-pair", "side": \n'),
    ("truncated_last_row", json.dumps(GOOD)[:-5]),
    ("no_trailing_newline", json.dumps(GOOD)),
    ("non_object_array", "[1, 2]\n"),
    ("non_object_null", "null\n"),
    ("non_utf8", b"\xff\xfe\x00bad\n"),
    ("good_then_bad", json.dumps(GOOD) + "\n{oops\n"),
]


@pytest.mark.parametrize("text", [b[1] for b in BROKEN], ids=[b[0] for b in BROKEN])
def test_broken_quote_file_refuses_without_exception(tmp_path, monkeypatch, no_bus, text):
    _ledger(tmp_path, monkeypatch, [])
    _write_raw(tmp_path, text)
    _assert_refused(tmp_path, monkeypatch)


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs POSIX non-root for chmod 000")
def test_unreadable_quote_file_refuses_without_exception(tmp_path, monkeypatch, no_bus):
    _ledger(tmp_path, monkeypatch, [])
    _write_raw(tmp_path, _row())
    path = tmp_path / "quotes.jsonl"
    path.chmod(0)
    try:
        _assert_refused(tmp_path, monkeypatch)
    finally:
        path.chmod(0o600)


def test_quote_path_is_a_directory_refuses_without_exception(tmp_path, monkeypatch, no_bus):
    _ledger(tmp_path, monkeypatch, [])
    (tmp_path / "quotes.jsonl").mkdir()
    _assert_refused(tmp_path, monkeypatch)


def test_control_missing_quote_file_is_empty_history_and_allows(tmp_path, monkeypatch):
    """Design (read_quotes, issue #56): a missing file is an empty history, not a read failure."""
    _ledger(tmp_path, monkeypatch, [])
    state = paper_ledger.load_state(MARKET, NO)
    assert state["opposite_pending_buys"] == 0
    assert evaluate(_buy(), load_risk(), state).allow is True


# ---------------------------------------------------------------- opposite_pending_buys field type in the state

BAD_PENDING = [("list", [1]), ("dict", {"n": 0}), ("str_zero", "0"), ("bool_true", True), ("bool_false", False),
               ("huge_float", 1e308), ("huge_int", 10 ** 30), ("inf", math.inf), ("neg_inf", -math.inf),
               ("fraction", 0.5), ("negative", -3)]


@pytest.mark.parametrize("bad", [b[1] for b in BAD_PENDING], ids=[b[0] for b in BAD_PENDING])
def test_bad_opposite_pending_buys_value_refuses(tmp_path, monkeypatch, bad):
    _ledger(tmp_path, monkeypatch, [])
    state = paper_ledger.load_state(MARKET, NO)
    state["opposite_pending_buys"] = bad
    decision = evaluate(_buy(), load_risk(), state)
    assert decision.allow is False and STRUCTURAL in decision.reasons, decision.reasons


def test_missing_opposite_pending_buys_key_refuses(tmp_path, monkeypatch):
    _ledger(tmp_path, monkeypatch, [])
    state = paper_ledger.load_state(MARKET, NO)
    state.pop("opposite_pending_buys")
    decision = evaluate(_buy(), load_risk(), state)
    assert decision.allow is False and STRUCTURAL in decision.reasons, decision.reasons


def test_control_well_formed_open_quote_still_refuses_and_cancelled_allows(tmp_path, monkeypatch):
    _ledger(tmp_path, monkeypatch, [])
    _write_raw(tmp_path, _row())
    assert STRUCTURAL in evaluate(_buy(), load_risk(), paper_ledger.load_state(MARKET, NO)).reasons
    _write_raw(tmp_path, _row(status="cancelled", cancelled_by="Sentinel", cancelled_ts="2026-01-01T00:00:00Z"))
    assert evaluate(_buy(), load_risk(), paper_ledger.load_state(MARKET, NO)).allow is True
