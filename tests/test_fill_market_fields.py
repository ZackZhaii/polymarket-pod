"""Task BA (M3 step 2, rest of design 1.5): market context on paper fills.

market_mid, best_bid, best_ask, hours_to_expiry_at_entry, kelly_fraction_used are recorded at commit from
data the intent already carries. Not computable = None + named in calibration_problems, never invented.
RiskGate never reads them; no decision, threshold or size moves. Offline, temp paths only.
"""
from __future__ import annotations

import dataclasses
import math
from datetime import datetime, timezone

import pytest

from src import calibration_ids as ci
from src import intent_builder, paper_ledger, podlib
from src.podlib import load_risk, read_jsonl
from src.risk_gate import NUMERIC_INTENT_FIELDS, evaluate
from tests.test_calibration_ids import ROOT, STATE, _ok_intent, paths  # noqa: F401 - fixture
from tests.test_intent_builder import build

FIELDS = ci.FILL_MARKET_FIELDS
TS = "2026-10-09T12:00:00Z"


def _fields(**kw):
    return paper_ledger.market_fields(_ok_intent(**kw), TS)


def test_the_field_list():
    assert FIELDS == ("market_mid", "best_bid", "best_ask", "hours_to_expiry_at_entry", "kelly_fraction_used",
                      "kelly_frac_raw")  # Task BC added kelly_frac_raw


def test_all_fields_from_the_intent():
    fields, problems = _fields(best_bid=0.38, best_ask=0.4, end_date="2026-10-10T18:30:00Z", kelly_fraction_used=0.15)
    assert problems == []
    assert fields == {"market_mid": 0.39, "best_bid": 0.38, "best_ask": 0.4, "hours_to_expiry_at_entry": 30.5,
                      "kelly_fraction_used": 0.15, "kelly_frac_raw": 0.1}


def test_hours_can_be_negative_when_already_past():
    fields, problems = _fields(end_date="2026-10-09T10:00:00Z")
    assert fields["hours_to_expiry_at_entry"] == -2.0 and problems == []


@pytest.mark.parametrize("bad", [None, math.nan, math.inf, 0, 0.0, 1, 1.2, -0.1, "0.4", True])
@pytest.mark.parametrize("side", ["best_bid", "best_ask"])
def test_an_unusable_side_is_null_and_named(side, bad):
    fields, problems = _fields(**{side: bad})
    assert fields[side] is None and fields["market_mid"] is None
    kind = "missing" if bad is None else "invalid"
    assert f"{kind}_{side}" in problems and "missing_market_mid" in problems
    other = "best_ask" if side == "best_bid" else "best_bid"
    assert fields[other] is not None  # the usable side is still recorded


def test_a_crossed_book_records_both_sides_but_no_mid():
    fields, problems = _fields(best_bid=0.45, best_ask=0.4)
    assert (fields["best_bid"], fields["best_ask"], fields["market_mid"]) == (0.45, 0.4, None)
    assert problems == ["invalid_market_mid"]


def test_a_locked_book_has_a_mid():
    fields, problems = _fields(best_bid=0.4, best_ask=0.4)
    assert fields["market_mid"] == 0.4 and problems == []


@pytest.mark.parametrize("end_date, problem", [
    (None, "missing_hours_to_expiry_at_entry"), ("2026-12-01", "invalid_hours_to_expiry_at_entry"),
    ("2026-12-01T00:00:00", "invalid_hours_to_expiry_at_entry"), ("soon", "invalid_hours_to_expiry_at_entry"),
])
def test_a_bad_end_date_leaves_hours_null(end_date, problem):
    fields, problems = _fields(end_date=end_date)
    assert fields["hours_to_expiry_at_entry"] is None and problems == [problem]


@pytest.mark.parametrize("bad, problem", [
    (None, "missing_kelly_fraction_used"), (0, "invalid_kelly_fraction_used"), (1.5, "invalid_kelly_fraction_used"),
    (math.nan, "invalid_kelly_fraction_used"), ("0.15", "invalid_kelly_fraction_used"),
    (True, "invalid_kelly_fraction_used"),
])
def test_a_bad_kelly_fraction_is_null(bad, problem):
    fields, problems = _fields(kelly_fraction_used=bad)
    assert fields["kelly_fraction_used"] is None and problems == [problem]


def test_a_fill_carries_the_fields(paths):
    result = paper_ledger.commit_if_allowed(_ok_intent(), load_risk())
    assert result["allow"] is True
    fill = read_jsonl(paths["tmp"] / "paper.jsonl")[0]
    assert all(k in fill for k in FIELDS) and fill["calibration_problems"] == []
    assert fill["market_mid"] == 0.39 and fill["kelly_fraction_used"] == 0.15
    end = datetime(2026, 12, 1, tzinfo=timezone.utc)
    ts = datetime.fromisoformat(fill["ts"].replace("Z", "+00:00"))
    assert fill["hours_to_expiry_at_entry"] == pytest.approx((end - ts).total_seconds() / 3600, abs=1e-5)
    assert fill["market_price"] == 0.4  # unchanged: the price RiskGate priced the fill at


def test_a_fill_without_market_context_is_booked_and_flagged(paths):
    result = paper_ledger.commit_if_allowed(_ok_intent(best_bid=None, best_ask=float("nan"), kelly_fraction_used=None,
                                                       kelly_frac_raw=None, end_date="tomorrow"), load_risk())
    assert result["allow"] is True  # never a veto
    fill = read_jsonl(paths["tmp"] / "paper.jsonl")[0]
    assert all(fill[k] is None for k in FIELDS)
    assert {"missing_best_bid", "invalid_best_ask", "missing_market_mid", "invalid_hours_to_expiry_at_entry",
            "missing_kelly_fraction_used", "missing_kelly_frac_raw"} <= set(fill["calibration_problems"])


def test_the_intent_builder_path_records_the_books_top_and_shadow_kelly(paths):
    oi = build().intent
    intent = dataclasses.replace(intent_builder.to_risk_intent(oi), book_ts=podlib.utc_now(),
                                 end_date="2027-01-01T00:00:00Z")
    result = paper_ledger.commit_if_allowed(intent, load_risk())
    assert result["allow"] is True
    fill = result["fill"]
    assert (fill["best_bid"], fill["best_ask"], fill["market_mid"]) == (oi.best_bid, oi.best_ask, 0.4)
    assert fill["kelly_fraction_used"] == oi.kelly_fraction == load_risk()["kelly_fraction"]
    assert fill["shares"] != oi.kelly_size_usd  # sized by RiskGate's clip, not by Kelly


# ------------------------------------------------------------------ RiskGate never reads them

def test_risk_gate_never_reads_the_market_fields():
    src = (ROOT / "src" / "risk_gate.py").read_text(encoding="utf-8")
    body = src[src.index("def evaluate"):]
    for name in ("best_bid", "best_ask", "kelly_fraction_used", "market_mid", "hours_to_expiry"):
        assert name not in body, name
    assert not set(FIELDS) & set(NUMERIC_INTENT_FIELDS)


def test_market_fields_never_change_a_decision_or_a_size():
    now = datetime.now(timezone.utc)
    cfg = load_risk()
    for kw in [dict(), dict(model_prob=0.41), dict(spread=0.2), dict(liquidity_usd=10.0), dict(side="sell")]:
        base = dataclasses.asdict(evaluate(_ok_intent(**kw), cfg, dict(STATE), now=now))
        for extra in [dict(best_bid=None, best_ask=None, kelly_fraction_used=None),
                      dict(best_bid=0.99, best_ask=0.01, kelly_fraction_used=1.0),
                      dict(best_bid=math.nan, best_ask=math.inf, kelly_fraction_used=-5.0)]:
            assert dataclasses.asdict(evaluate(_ok_intent(**kw, **extra), cfg, dict(STATE), now=now)) == base, (kw, extra)
