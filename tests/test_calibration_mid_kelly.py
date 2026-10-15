"""Task BC (M3): the calibration report scores the market at market_mid, both Kelly values are recorded,
and fills without book context get their own drop reason.

- q = the first fill's market_mid. Only a fill booked before Task BA (no market_mid key at all) uses its
  market_price (design 3, price_source noted); a recorded but unusable mid is dropped, never replaced.
- kelly_fraction_used stays the shadow multiplier; kelly_frac_raw is intent_builder's per-trade raw Kelly.
  Recorded only: RiskGate and the order size never read either (source scan + identical decisions).
- fill_context is stamped at fill time (intent_builder / operator_cli / gate_check); operator_cli and
  gate_check fills are dropped as no_book_context, never inferred from missing fields.
Offline, synthetic rows and temp paths only.
"""
from __future__ import annotations

import argparse
import dataclasses
import math
from datetime import datetime, timezone

import pytest

from src import calibration as cal
from src import calibration_ids as ci
from src import paper_ledger
from src.podlib import load_risk, read_jsonl
from src.risk_gate import NUMERIC_INTENT_FIELDS, evaluate
from tests.test_calibration import build, fill, forecast, resolution, world
from tests.test_calibration_ids import ROOT, STATE, _ok_intent, paths  # noqa: F401 - fixture
from tests.test_intent_builder import build as build_oi
from tests.test_intent_builder import fresh_intent

TS = "2026-10-09T12:00:00Z"
NEW = ("kelly_frac_raw", "fill_context")


# ------------------------------------------------------------------ fill_context at fill time

def test_the_contexts():
    assert ci.FILL_CONTEXTS == ("intent_builder", "operator_cli", "gate_check")
    assert ci.NO_BOOK_CONTEXTS == {"operator_cli", "gate_check"}
    assert ci.FILL_CONTEXT_INTENT_BUILDER not in ci.NO_BOOK_CONTEXTS


def test_the_intent_builder_path_is_stamped_and_records_both_kelly_values(paths):
    oi = build_oi().intent
    # Issue #76: one consistent clock stamps book_ts and the intent window (fresh_intent), so the
    # commit at the real clock sees a fresh, unexpired intent instead of a 2026-09 book with one.
    intent = fresh_intent(oi, end_date="2027-01-01T00:00:00Z")
    assert intent.fill_context == ci.FILL_CONTEXT_INTENT_BUILDER
    result = paper_ledger.commit_if_allowed(intent, load_risk())
    fill_row = result["fill"]
    assert fill_row["fill_context"] == "intent_builder"
    assert fill_row["kelly_fraction_used"] == oi.kelly_fraction == load_risk()["kelly_fraction"]
    assert fill_row["kelly_frac_raw"] == oi.kelly_raw and 0 < oi.kelly_raw <= 1
    assert fill_row["kelly_frac_raw"] * fill_row["kelly_fraction_used"] == pytest.approx(oi.kelly_frac)
    assert not any("fill_context" in p or "kelly" in p for p in fill_row["calibration_problems"])


def test_the_operator_cli_is_stamped_operator_cli(monkeypatch):
    # Issue #210: the operator CLI always refuses before commit (no book levels).
    seen = []
    monkeypatch.setattr(paper_ledger, "commit_if_allowed", lambda intent, cfg: seen.append(intent) or {"allow": False})
    args = argparse.Namespace(strategy="manual", market="m1", side="buy", model_prob=0.7, market_price=0.4,
                              slippage=0.005, fee_rate=0.0, liquidity=20000.0, spread=0.01, token_id="t",
                              book_ts=None, note=None, tags="us", min_order_size=5.0)
    refused = paper_ledger.run_cli_intent(args, load_risk())
    assert refused["allow"] is False and "missing_book_levels" in refused["decision"]["reasons"]
    assert seen == []
    # Poly风控 6178561328 / Codex P1 6178323003: even with asks supplied, the CLI never reaches commit.
    args_with_depth = argparse.Namespace(**{**vars(args), "asks": [(0.40, 100.0)]})
    refused = paper_ledger.run_cli_intent(args_with_depth, load_risk())
    assert refused["allow"] is False and "missing_book_levels" in refused["decision"]["reasons"]
    assert seen == []


def test_the_gate_checks_are_stamped_gate_check(monkeypatch):
    assert paper_ledger._intent().fill_context == "gate_check"
    seen = []
    monkeypatch.setattr(paper_ledger, "commit_if_allowed", lambda intent, cfg: seen.append(intent) or {})
    book = {"market_id": "m1", "token_id": "t1", "ts": "2026-10-09T00:00:00Z", "book_ts": "2026-10-09T00:00:00Z",
            "best_ask": 0.4, "best_bid": 0.38, "spread": 0.02}
    rows = {str(paper_ledger.BOOKS_PATH): [book], str(paper_ledger.MARKETS_PATH): [{"id": "m1", "liquidity_usd": 2e4}]}
    monkeypatch.setattr(paper_ledger, "read_jsonl", lambda path: rows.get(str(path), []))
    paper_ledger.realistic_no_edge(load_risk())
    assert seen and seen[0].fill_context == "gate_check"


@pytest.mark.parametrize("value, problem", [(None, "missing_fill_context"), ("cli", "invalid_fill_context"),
                                            ("", "invalid_fill_context")])
def test_an_unknown_or_missing_context_is_null_and_named(paths, value, problem):
    result = paper_ledger.commit_if_allowed(_ok_intent(fill_context=value), load_risk())
    assert result["allow"] is True  # never a veto
    row = read_jsonl(paths["tmp"] / "paper.jsonl")[0]
    assert row["fill_context"] is None and problem in row["calibration_problems"]


@pytest.mark.parametrize("bad, problem", [
    (None, "missing_kelly_frac_raw"), (0, "invalid_kelly_frac_raw"), (-0.1, "invalid_kelly_frac_raw"),
    (1.01, "invalid_kelly_frac_raw"), (math.nan, "invalid_kelly_frac_raw"), (math.inf, "invalid_kelly_frac_raw"),
    ("0.1", "invalid_kelly_frac_raw"), (True, "invalid_kelly_frac_raw"),
])
def test_a_bad_raw_kelly_is_null_and_named(bad, problem):
    fields, problems = paper_ledger.market_fields(_ok_intent(kelly_frac_raw=bad), TS)
    assert fields["kelly_frac_raw"] is None and problems == [problem]
    assert fields["kelly_fraction_used"] == 0.15  # the multiplier is untouched


# ------------------------------------------------------------------ recorded only

def test_risk_gate_and_sizing_never_read_them():
    src = (ROOT / "src" / "risk_gate.py").read_text(encoding="utf-8")
    body = src[src.index("def evaluate"):]
    for name in NEW:
        assert name not in body, name
    assert not set(NEW) & set(NUMERIC_INTENT_FIELDS)
    builder = (ROOT / "src" / "intent_builder.py").read_text(encoding="utf-8")
    before_adapter = builder[:builder.index("def to_risk_intent")]
    assert "kelly_frac_raw" not in before_adapter and "fill_context" not in before_adapter
    ledger = (ROOT / "src" / "paper_ledger.py").read_text(encoding="utf-8")
    sizing = ledger[ledger.index("def commit_if_allowed"):ledger.index("    fill = {")]
    assert "kelly" not in sizing and "fill_context" not in sizing


def test_they_never_change_a_decision_or_a_size():
    # Issue #262: one shared book_ts with the fixed now (utc_now truncates to whole seconds).
    from tests.test_calibration_ids import _stable_book_and_now
    book_ts, now = _stable_book_and_now()
    cfg = load_risk()
    for kw in [dict(), dict(model_prob=0.41), dict(spread=0.2), dict(liquidity_usd=10.0), dict(side="sell")]:
        base = dataclasses.asdict(evaluate(_ok_intent(book_ts=book_ts, **kw), cfg, dict(STATE), now=now))
        for extra in [dict(kelly_frac_raw=None, fill_context=None), dict(kelly_frac_raw=1.0, fill_context="gate_check"),
                      dict(kelly_frac_raw=math.nan, fill_context="operator_cli"), dict(kelly_frac_raw=-3.0)]:
            got = dataclasses.asdict(evaluate(_ok_intent(book_ts=book_ts, **kw, **extra), cfg, dict(STATE), now=now))
            assert got == base, (kw, extra)


def test_a_booked_fill_has_the_same_size_with_or_without_them(paths):
    first = paper_ledger.commit_if_allowed(_ok_intent(market="m1"), load_risk())["fill"]
    second = paper_ledger.commit_if_allowed(_ok_intent(market="m2", token_id="t2", kelly_frac_raw=None,
                                                       fill_context="operator_cli"), load_risk())["fill"]
    for key in ("shares", "notional_usd", "effective_price", "fee_usd", "edge"):
        assert first[key] == second[key], key


# ------------------------------------------------------------------ report: q = market_mid

def _mid_fill(f, mid=0.45, **over):
    return fill(f, price=0.4, **{"market_mid": mid, "fill_context": "intent_builder", **over})


def test_q_is_the_market_mid():
    fs, rs = world(3)
    report, samples = build(fs, rs, [_mid_fill(fs[0], 0.45), _mid_fill(fs[1], 0.3)])
    eligible = {s["forecast_id"]: s for s in samples if s.get("kind") == "trade" and s["eligible"]}
    assert eligible[fs[0]["forecast_id"]]["q"] == 0.45 and eligible[fs[1]["forecast_id"]]["q"] == 0.3
    assert {s["price_source"] for s in eligible.values()} == {"market_mid"}
    assert report["trades"]["price_sources"] == {"market_mid": 2}
    y = [1 if r["winning_token_id"] == f["token_id"] else 0 for f, r in zip(fs[:2], rs[:2])]
    expected = ((0.45 - y[0]) ** 2 + (0.3 - y[1]) ** 2) / 2
    assert report["trades"]["metrics"]["brier_market"] == pytest.approx(expected)


def test_a_fill_from_before_task_ba_falls_back_to_market_price_and_says_so():
    fs, rs = world(2)
    report, samples = build(fs, rs, [fill(fs[0], price=0.4), _mid_fill(fs[1], 0.5)])
    trade = {s["forecast_id"]: s for s in samples if s.get("kind") == "trade"}
    assert (trade[fs[0]["forecast_id"]]["q"], trade[fs[0]["forecast_id"]]["price_source"]) == (0.4, "market_price")
    assert report["trades"]["price_sources"] == {"market_mid": 1, "market_price": 1}
    assert any("market_mid" in note and "fill_market_mid_missing" in note for note in report["notes"])


@pytest.mark.parametrize("mid", [None, math.nan, 0, 1, 1.5, "0.4", True])
def test_a_recorded_but_unusable_mid_is_dropped_never_replaced(mid):
    fs, rs = world(2)
    t = build(fs, rs, [_mid_fill(fs[0], mid), _mid_fill(fs[1], 0.5)])[0]["trades"]
    assert t["eligible"] == 1 and t["dropped_by_reason"] == {"fill_market_mid_missing": 1}
    assert t["price_sources"] == {"market_mid": 1}


def test_a_null_mid_with_its_stored_problem_is_a_counted_problem_drop():
    fs, rs = world(1)
    row = _mid_fill(fs[0], None)
    row["calibration_problems"] = ["missing_market_mid"]  # as paper_ledger stores it
    t = build(fs, rs, [row])[0]["trades"]
    assert t["dropped_by_reason"] == {"fill_calibration_problems": 1}
    assert t["calibration_problems"] == {"missing_market_mid": 1}


def test_an_unusable_mid_does_not_use_up_a_re_entry_slot():
    f0 = forecast(0, 0.6)
    fs = [f0, forecast(0, 0.6, ts="2026-10-02T00:00:00Z"), forecast(0, 0.6, ts="2026-10-03T00:00:00Z")]
    fills = [_mid_fill(fs[0], None, ts=fs[0]["ts"])] + [_mid_fill(f, 0.5, ts=f["ts"]) for f in fs[1:]]
    t = build(fs, [resolution(0, 0, day=5)], fills)[0]["trades"]  # close 2026-10-05: all fills before it
    assert t["eligible"] == 2 and t["dropped_by_reason"] == {"fill_market_mid_missing": 1}


# ------------------------------------------------------------------ report: no_book_context

@pytest.mark.parametrize("context", ["operator_cli", "gate_check"])
def test_no_book_context_is_its_own_drop_reason(context):
    fs, rs = world(3)
    no_book = fill(fs[0], fill_context=context, market_mid=None)
    no_book["calibration_problems"] = ["missing_best_ask", "missing_best_bid", "missing_market_mid"]
    genuine = _mid_fill(fs[1], None)
    genuine["calibration_problems"] = ["missing_market_mid"]
    t = build(fs, rs, [no_book, genuine, _mid_fill(fs[2], 0.5)])[0]["trades"]
    assert t["eligible"] == 1
    assert t["dropped_by_reason"] == {"fill_calibration_problems": 1, "no_book_context": 1}
    assert t["calibration_problems"] == {"missing_market_mid": 1}  # only the genuine one is a data problem


def test_no_book_context_wins_even_for_an_excluded_strategy_or_a_clean_row():
    fs, rs = world(2)
    rows = [_mid_fill(fs[0], 0.5, strategy="wash-probe", fill_context="gate_check"),
            _mid_fill(fs[1], 0.5, fill_context="operator_cli")]
    t = build(fs, rs, rows)[0]["trades"]
    assert t["eligible"] == 0 and t["dropped_by_reason"] == {"no_book_context": 2}


def test_context_is_never_inferred_from_missing_fields():
    fs, rs = world(2)
    legacy = fill(fs[0], price=0.4)  # no fill_context key, no market fields: booked before Task BA/BC
    assert "fill_context" not in legacy
    t = build(fs, rs, [legacy])[0]["trades"]
    assert t["eligible"] == 1 and "no_book_context" not in t["dropped_by_reason"]


def test_a_missing_or_unknown_stored_context_is_a_data_problem_not_no_book_context():
    fs, rs = world(2)
    a = _mid_fill(fs[0], 0.5, fill_context=None)
    a["calibration_problems"] = ["missing_fill_context"]
    b = _mid_fill(fs[1], 0.5, fill_context="somewhere")
    assert b["calibration_problems"] == []  # a stored clean list does not hide the recomputed problem
    t = build(fs, rs, [a, b])[0]["trades"]
    assert t["eligible"] == 0 and t["dropped_by_reason"] == {"fill_calibration_problems": 2}
    assert t["calibration_problems"] == {"invalid_fill_context": 1, "missing_fill_context": 1}


# ------------------------------------------------------------------ report compatibility (BB reads report.json)

def test_the_report_only_adds_keys():
    report, samples = build(*world(2))
    assert report["schema_version"] == cal.SCHEMA_VERSION == 1
    assert {"eligible", "dropped", "dropped_by_reason", "calibration_problems", "metrics", "by_category",
            "by_strategy", "by_model_version", "by_side", "by_edge_tier", "by_expiry_tier",
            "by_price_band"} <= set(report["trades"])
    assert report["trades"]["price_sources"] == {}
    assert "market probability q by source" in cal.render_markdown(report)


def test_calibration_never_imports_trading_or_sizing():
    src = (ROOT / "src" / "calibration.py").read_text(encoding="utf-8")
    assert "kelly" not in src and "risk_gate" not in src and "intent_builder" not in src
