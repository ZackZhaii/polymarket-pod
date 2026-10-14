"""Task DD (#280, Poly风控 review 2a354f9). Offline only: no network, no credentials, no orders."""
from __future__ import annotations

import math
from datetime import datetime, timezone

import pytest

import src.halt_store as halt_store
import src.paper_ledger as paper_ledger
from src import wash_detect

MARKET, NO = "m-dd", "t-no"
GOOD = {"market": MARKET, "token_id": "t-yes", "side": "buy", "status": "open", "price": 0.4}
BAD_VALUES = [["open"], {"s": 1}, 123, None]


def _bad(field, value):
    row = dict(GOOD)
    row[field] = value
    return row


# (1) str validation in wash_detect: pair_quotes, pair_cross_problems, wash_problems

@pytest.mark.parametrize("field", ["market", "status", "side"])
@pytest.mark.parametrize("value", BAD_VALUES)
def test_pair_quotes_malformed_field_fails_closed(field, value):
    assert wash_detect.pair_quotes(MARKET, NO, [_bad(field, value)]) is True


@pytest.mark.parametrize("field", ["market", "status", "side"])
@pytest.mark.parametrize("value", BAD_VALUES)
def test_pair_cross_problems_malformed_field_fails_closed(field, value):
    out = wash_detect.pair_cross_problems(market=MARKET, token_id=NO, side="buy", price=0.4,
                                          quotes=[_bad(field, value)], market_pair=("t-yes", NO))
    assert out == [wash_detect.WASH_INPUT_INVALID]


@pytest.mark.parametrize("field", ["status", "side", "token_id"])
@pytest.mark.parametrize("value", [["x"], {"s": 1}, 7])
def test_wash_problems_malformed_field_fails_closed(field, value):
    state = {wash_detect.FILLS_KEY: [], wash_detect.QUOTES_KEY: [_bad(field, value)]}
    out = wash_detect.wash_problems(market=MARKET, token_id=NO, side="buy", strategy="s", counterparty=None,
                                    state=state, now=datetime.now(timezone.utc), price=0.4)
    assert wash_detect.WASH_INPUT_INVALID in out


# (2) _opposite_pending_buys: a record missing a field -> None, not skipped

@pytest.mark.parametrize("field", ["market", "status", "side", "token_id"])
def test_pending_count_record_missing_a_field_is_none(field):
    row = dict(GOOD)
    row.pop(field)
    assert paper_ledger._opposite_pending_buys(MARKET, NO, [row]) is None


def test_pending_count_other_market_malformed_record_is_none():
    assert paper_ledger._opposite_pending_buys(MARKET, NO, [_bad("status", ["x"]) | {"market": "other"}]) is None


def test_pending_count_well_formed():
    assert paper_ledger._opposite_pending_buys(MARKET, NO, [dict(GOOD)]) == 1
    assert paper_ledger._opposite_pending_buys(MARKET, NO, [GOOD | {"status": "cancelled"}]) == 0


# (3) StateLock.acquire timeout_s: finite > 0 only

def test_acquire_nan_timeout_returns_false(tmp_path):
    assert halt_store.StateLock(tmp_path / "s.json").acquire(timeout_s=math.nan) is False


def test_acquire_inf_timeout_returns_false(tmp_path):
    assert halt_store.StateLock(tmp_path / "s.json").acquire(timeout_s=math.inf) is False


@pytest.mark.parametrize("bad", [0, -1.0, -math.inf, True, "1"])
def test_acquire_non_positive_or_non_number_timeout_returns_false(tmp_path, bad):
    assert halt_store.StateLock(tmp_path / "s.json").acquire(timeout_s=bad) is False


def test_acquire_finite_timeout_and_default_still_lock(tmp_path):
    lock = halt_store.StateLock(tmp_path / "s.json")
    assert lock.acquire(timeout_s=0.5) is True
    lock.release()
    assert lock.acquire() is True
    lock.release()


# (4) reason constant + threshold registration

def test_reason_code_is_the_structural_constant():
    from src.structural_gate import STRUCTURAL_ARB_DISABLED
    from src import risk_gate

    assert risk_gate.STRUCTURAL_ARB_DISABLED is STRUCTURAL_ARB_DISABLED
    assert paper_ledger.STRUCTURAL_ARB_DISABLED is STRUCTURAL_ARB_DISABLED


def test_commit_lock_timeout_is_registered():
    from pathlib import Path

    doc = (Path(__file__).resolve().parents[1] / "docs" / "THRESHOLDS.md").read_text(encoding="utf-8")
    assert "`COMMIT_LOCK_TIMEOUT_S`" in doc and paper_ledger.COMMIT_LOCK_TIMEOUT_S == 5.0
