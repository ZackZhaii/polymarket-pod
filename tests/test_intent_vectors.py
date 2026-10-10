"""Issue #105 (Task CB): Poly预测's golden intent_builder vectors as parametrized tests.

``tests/fixtures/intent_vectors.json`` (schema ``poly_intent_vectors/1``) is a copy of
/workspace/pod-bots/poly-forecast-test-vectors.json, produced by its generator from dev 5440002
(see the issue #105 comments). The file is kept unchanged. Encoding: an absent key is a missing
field; ``{"$float": "nan"|"inf"|"-inf"}`` is a non-finite float.

Acceptance (#105): every control vector and every #105 vector passes. Highlights:
- a missing ``outcome_index`` is never read as outcome 0 / YES (``missing_outcome_index``);
- a NewsForecaster row without a ``token_id`` is rejected (``missing_token_id``);
- half an interval is rejected (``incomplete_interval``).

The same file also has the #106 / #108 vectors. Those that dev still gets wrong are strict xfails
here (``XFAIL_OPEN``); each one should be cleared by the fix for its own issue. A strict xfail that
starts passing fails the suite, so the list cannot go stale.

Offline and pure: no clock read, no file write, no network, no order.
"""
from __future__ import annotations

import json
import math
import pathlib
from datetime import datetime

import pytest

import src.intent_builder as ib
from src.podlib import RISK_PATH, parse_risk_toml

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "intent_vectors.json"
DOC = json.loads(FIXTURE.read_text(encoding="utf-8"))
CFG = parse_risk_toml(RISK_PATH.read_text(encoding="utf-8"))
TOL = DOC["float_tolerance"]
ACCEPT_FIELDS = ("p_side", "exec_price", "limit_price", "shares", "size_usd", "edge", "fee_per_share")

# Vectors of other issues that dev does not meet yet (open issues #106 / #108; not part of #105).
XFAIL_OPEN = {
    "I106-01": "106", "I106-02": "106", "I106-09": "106",
    "I108-02": "108", "I108-03": "108", "I108-05": "108", "I108-06": "108", "I108-20": "108", "I108-21": "108",
}


def dec(x):
    if isinstance(x, dict):
        return float(x["$float"]) if set(x) == {"$float"} else {k: dec(v) for k, v in x.items()}
    return [dec(v) for v in x] if isinstance(x, list) else x


def _params():
    out = []
    for v in DOC["vectors"]:
        marks = ()
        if v["id"] in XFAIL_OPEN:
            marks = (pytest.mark.xfail(strict=True, reason=f"issue #{XFAIL_OPEN[v['id']]} not fixed yet"),)
        out.append(pytest.param(v, id=v["id"], marks=marks))
    return out


def run_vector(inp: dict) -> ib.IntentResult:
    if inp["entry"] == "bus_row":
        sig = ib.signal_from_bus(inp["signal"])
    else:
        sig = ib.ProbabilitySignal(**inp["signal"])
    m = inp["market"]
    # The vectors predate Issue #131 (an untagged market is missing_category); like tests/test_intent_builder.py
    # the harness gives an untagged vector market a neutral tag, so each vector still tests what it says.
    m.setdefault("tags", ["test-market"])
    mk = ib.MarketContext(**{**m, "token_ids": tuple(m["token_ids"]), "outcomes": tuple(m["outcomes"]),
                             "tags": tuple(m["tags"])})
    books = {t: ib.BookSnapshot(**{**b, "asks": tuple(map(tuple, b["asks"])), "bids": tuple(map(tuple, b["bids"]))})
             for t, b in inp["books"].items()}
    return ib.build_order_intent(sig, mk, books, {**CFG, **inp["cfg_override"]},
                                 now=datetime.fromisoformat(inp["now"].replace("Z", "+00:00")),
                                 strategy=inp["strategy"], paper=inp["paper"])


def test_fixture_shape():
    assert DOC["schema"] == "poly_intent_vectors/1"
    ids = [v["id"] for v in DOC["vectors"]]
    assert len(ids) == len(set(ids)) == 70
    assert set(XFAIL_OPEN) <= set(ids)
    # #105 and the controls are never xfailed: they are this issue's acceptance.
    assert not any(v["issue"] in ("105", "control") for v in DOC["vectors"] if v["id"] in XFAIL_OPEN)
    assert sum(v["issue"] == "105" for v in DOC["vectors"]) == 17
    assert sum(v["issue"] == "control" for v in DOC["vectors"]) == 13


def test_proposed_reason_codes_exist_in_group_b():
    proposed = {code for v in DOC["vectors"] for code in v["expected"].get("proposed_reason_codes", [])}
    assert proposed == {"missing_outcome_index", "missing_token_id", "incomplete_interval"}
    order = ib.REASON_ORDER
    anchor = order.index("invalid_outcome_index")
    assert [order.index(code) for code in ("missing_outcome_index", "missing_token_id", "incomplete_interval")] \
        == [anchor + 1, anchor + 2, anchor + 3]
    assert order.index("incomplete_interval") < order.index("market_mismatch")  # still group B


@pytest.mark.parametrize("v", _params())
def test_intent_vector(v):
    inp, exp = dec(v["inputs"]), v["expected"]
    if inp["entry"] == "side_edge":
        e = ib.side_edge(inp["p_side"], inp["exec_price"], inp["fee_rate"])
        assert e.edge == -1.0 and e.fee_per_share is None
        return
    r = run_vector(inp)
    if not exp["intent"]:
        assert r.intent is None, r.detail
        assert set(exp["reasons_include"]) <= set(r.reasons), r.reasons
        if exp.get("reasons_any_of"):
            assert set(exp["reasons_any_of"]) & set(r.reasons), r.reasons
        assert r.first_reason == r.reasons[0]
    else:
        o = r.intent
        assert o is not None, r.reasons
        assert o.outcome_index == exp["outcome_index"]
        for k in ACCEPT_FIELDS:
            assert math.isclose(getattr(o, k), exp[k], rel_tol=0.0, abs_tol=TOL), (k, getattr(o, k), exp[k])


# ---------------------------------------------------------------- #105 beyond the vectors

def _row(**payload):
    base = {"forecast_id": "fc_x", "forecast_ts": "2026-09-01T12:00:00Z", "model_version": "vec/1",
            "model_prob": 0.7, "order": False}
    base.update(payload)
    return {"type": "signal", "from": "NewsForecaster", "to": "RiskGate", "market": "m1",
            "ts": "2026-09-01T12:00:00Z", "payload": base}


def test_signal_from_bus_keeps_a_missing_outcome_index_missing():
    assert ib.signal_from_bus(_row(token_id="t-yes")).outcome_index is None
    assert ib.signal_from_bus(_row(outcome_index=1, token_id="t-no")).outcome_index == 1
    assert ib.signal_from_bus(_row(outcome_index=0)).token_id is None


@pytest.mark.parametrize("payload,codes", [
    ({}, ["missing_outcome_index", "missing_token_id"]),
    ({"token_id": "t-yes"}, ["missing_outcome_index"]),
    ({"outcome_index": None, "token_id": "t-yes"}, ["missing_outcome_index"]),
    ({"outcome_index": 0}, ["missing_token_id"]),
    ({"outcome_index": 0, "token_id": "   "}, ["missing_token_id"]),
    ({"outcome_index": 2, "token_id": "t-yes"}, ["invalid_outcome_index"]),
    ({"outcome_index": 0, "token_id": "t-yes"}, []),
])
def test_outcome_index_problems(payload, codes):
    assert ib.outcome_index_problems(ib.signal_from_bus(_row(**payload))) == codes


@pytest.mark.parametrize("low,high,codes", [
    (0.6, None, ["incomplete_interval"]),
    (None, 0.8, ["incomplete_interval"]),
    (float("nan"), None, ["incomplete_interval"]),
    (None, None, []),
    (0.6, 0.8, []),
])
def test_half_interval_is_incomplete(low, high, codes):
    sig = ib.ProbabilitySignal(signal_id="s", source="news", market_id="m1", outcome_index=0, p=0.7,
                               p_low=low, p_high=high, token_id="t-yes", signal_ts="2026-09-01T12:00:00Z")
    assert ib.probability_problems(sig) == codes


def test_missing_outcome_index_rejects_in_a_blend():
    a = ib.ProbabilitySignal(signal_id="a", source="news", market_id="m1", outcome_index=None, p=0.7,
                             token_id="t-yes", signal_ts="2026-09-01T12:00:00Z")
    b = ib.ProbabilitySignal(signal_id="b", source="sports_devig", market_id="m1", outcome_index=0, p=0.7,
                             token_id="t-yes", signal_ts="2026-09-01T12:00:00Z")
    blended, reasons = ib.combine_probability([a, b])
    assert blended is None and "missing_outcome_index" in reasons


def test_tightening_only_every_issue105_vector_rejects():
    """Every #105 vector must be rejected: none may produce an intent."""
    for v in DOC["vectors"]:
        if v["issue"] == "105":
            assert run_vector(dec(v["inputs"])).intent is None, v["id"]
