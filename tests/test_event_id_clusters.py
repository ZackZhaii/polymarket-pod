"""Task BF (M3): the Polymarket event id on forecasts and fills, and event_id as the calibration cluster key.

- calibration_ids.scanner_event reads Gamma's ``events`` list (include_tag=true market rows) into
  (event_id, event_slug). Missing, malformed or conflicting event data gives None; nothing is guessed.
- The scanner row, MarketContext, OrderIntent, risk_gate.Intent and the paper fill carry event_id /
  event_slug (calibration_ids.FILL_EVENT_FIELDS); logs/forecasts.jsonl rows carry them too
  (FORECAST_EVENT_FIELDS, the last keys of FORECAST_ROW_FIELDS). A missing event id is never a
  calibration problem. Recorded only: RiskGate never reads them; no decision, threshold or size moves.
- src/calibration.py clusters ``event_cluster`` by event_id where a sample has one and falls back to
  condition_id per sample; cluster_key says "event_id", "condition_id" or "mixed" and the counts say how
  many samples fell back. Rows written before Task BF (no event keys) stay valid.
- src/reflector_proposals.py carries the new key through unchanged.
Offline, synthetic rows only.
"""
from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timezone

import pytest

from src import calibration as cal
from src import calibration_ids as ci
from src import forecaster, intent_builder, paper_ledger, scanner
from src.podlib import load_risk, read_jsonl
from src.risk_gate import evaluate
from tests.test_calibration import build, fill, forecast, resolution
from tests.test_calibration_ids import ROOT, STATE, _ok_intent, paths  # noqa: F401 - fixture
from tests.test_intent_builder import build as build_intent, market as ib_market

LEGACY_FORECAST_FIELDS = tuple(k for k in ci.FORECAST_ROW_FIELDS if k not in ci.FORECAST_EVENT_FIELDS)


def with_event(row: dict, event_id, event_slug=None) -> dict:
    """A forecast row (tests.test_calibration.forecast) with its event fields set."""
    return {**row, "event_id": event_id, "event_slug": event_slug}


def legacy(row: dict) -> dict:
    """The same forecast row as written before Task BF (no event keys)."""
    return {k: row[k] for k in LEGACY_FORECAST_FIELDS}


# ------------------------------------------------------------------ Gamma events -> (event_id, event_slug)

def test_field_lists():
    assert ci.FILL_EVENT_FIELDS == ("event_id", "event_slug")
    assert ci.FORECAST_EVENT_FIELDS == ("event_id", "event_slug")
    assert ci.FORECAST_ROW_FIELDS[-2:] == ci.FORECAST_EVENT_FIELDS
    assert not set(ci.FILL_EVENT_FIELDS) & set(ci.FILL_JOIN_FIELDS)  # never needed for the join


@pytest.mark.parametrize("events, want", [
    ([{"id": "9001", "slug": "us-election", "title": "US election"}], ("9001", "us-election")),
    ([{"id": 9001, "slug": "us-election"}], ("9001", "us-election")),
    ([{"id": " 9001 ", "slug": " "}], ("9001", None)),
    ([{"id": "9001"}], ("9001", None)),
    ([{"id": "9001", "slug": "a"}, {"id": "9001", "slug": "a"}], ("9001", "a")),
    ([{"id": "9001", "slug": "a"}, {"id": "9002", "slug": "b"}], (None, None)),  # conflicting: never guess
    ([], (None, None)),
    (None, (None, None)),
    ("9001", (None, None)),
    ({"id": "9001"}, (None, None)),
    ([{"slug": "no-id"}], (None, None)),
    ([{"id": ""}], (None, None)),
    ([{"id": True}], (None, None)),
    ([{"id": 1.5}], (None, None)),
    ([{"id": None, "slug": "x"}], (None, None)),
    (["9001", 7, None], (None, None)),
    ([{"id": "9001", "slug": 5}], ("9001", None)),
    ([{"id": "9001"}, "junk", {"slug": "x"}], ("9001", None)),
])
def test_scanner_event_reads_gamma_events_and_never_guesses(events, want):
    market = {"id": "1"} if events is None else {"id": "1", "events": events}
    assert ci.scanner_event(market) == want


@pytest.mark.parametrize("value, want", [("9001", "9001"), (" e1 ", "e1"), (12, "12"), ("", None), ("  ", None),
                                         (None, None), (True, None), (1.0, None), ([1], None), ({"id": 1}, None)])
def test_event_value_normalises_or_gives_none(value, want):
    assert ci.event_value(value) == want


def test_scanner_rows_carry_event_id_and_slug():
    market = {"id": "1", "conditionId": "0xc", "clobTokenIds": '["a","b"]', "outcomes": '["Yes","No"]',
              "outcomePrices": '["0.4","0.6"]', "endDate": "2026-12-01T00:00:00Z",
              "events": [{"id": "9001", "slug": "us-election"}]}
    row = scanner.normalize(market, "2026-10-09T00:00:00Z")
    assert (row["event_id"], row["event_slug"]) == ("9001", "us-election")
    bare = scanner.normalize({k: v for k, v in market.items() if k != "events"}, "2026-10-09T00:00:00Z")
    assert bare["event_id"] is None and bare["event_slug"] is None
    junk = scanner.normalize({**market, "events": "garbage"}, "2026-10-09T00:00:00Z")
    assert junk["event_id"] is None and junk["event_slug"] is None


def test_events_never_filter_a_market():
    src = (ROOT / "src" / "scanner.py").read_text(encoding="utf-8")
    body = src[src.index("def rejection_reasons"):src.index("def ", src.index("def rejection_reasons") + 10)]
    assert "event" not in body


# ------------------------------------------------------------------ forecasts.jsonl

def test_a_forecast_row_carries_the_event(tmp_path):
    path = tmp_path / "forecasts.jsonl"
    forecaster.emit_probability(market="m1", model_prob=0.6, condition_id="0xc1", token_id="t1", outcome_index=0,
                                end_date="2026-12-01T00:00:00Z", event_id="9001", event_slug="us-election",
                                forecasts_path=path, bus_path=tmp_path / "bus.jsonl")
    (row,) = read_jsonl(path)
    assert tuple(row) == ci.FORECAST_ROW_FIELDS
    assert (row["event_id"], row["event_slug"]) == ("9001", "us-election")
    assert row["calibration_problems"] == []


@pytest.mark.parametrize("event_id, event_slug", [(None, None), ("", " "), (True, 5), ([1], {"a": 1}), (12, None)])
def test_missing_or_malformed_event_data_never_breaks_a_forecast(tmp_path, event_id, event_slug):
    path = tmp_path / "forecasts.jsonl"
    signal = forecaster.emit_probability(market="m1", model_prob=0.6, condition_id="0xc1", token_id="t1",
                                         outcome_index=0, end_date="2026-12-01T00:00:00Z", event_id=event_id,
                                         event_slug=event_slug, forecasts_path=path, bus_path=tmp_path / "bus.jsonl")
    (row,) = read_jsonl(path)
    assert row["event_id"] == ci.event_value(event_id) and row["event_slug"] == ci.event_value(event_slug)
    assert row["calibration_problems"] == []  # a missing event id is never a calibration problem
    assert "event_id" not in signal["payload"]  # RiskGate's signal is unchanged


def test_the_event_does_not_change_the_forecast_id(tmp_path):
    a = forecaster.emit_probability(market="m1", model_prob=0.6, forecasts_path=tmp_path / "a.jsonl",
                                    bus_path=tmp_path / "bus.jsonl")
    row = read_jsonl(tmp_path / "a.jsonl")[0]
    assert row["event_id"] is None
    assert a["payload"]["forecast_id"] == ci.make_forecast_id(
        source="news", ts=row["ts"], market_id="m1", condition_id=None, outcome_index=None, token_id=None,
        model_prob=0.6, p_low=None, p_high=None, model_version=forecaster.MODEL_VERSION)


def test_pre_bf_and_bf_forecast_rows_both_read():
    old = legacy(forecast(1, 0.6))
    new = with_event(forecast(2, 0.6), "9001", "e")
    report, _ = build([old, new], [resolution(1, 0), resolution(2, 0)])
    assert report["forecasts"]["eligible"] == 2


@pytest.mark.parametrize("mutate", [
    lambda r: r.pop("event_slug"),  # one event key without the other is still read (missing = None)
    lambda r: r.update(event_id=17),
    lambda r: r.update(event_id=["x"]),
    lambda r: r.update(event_slug={"a": 1}),
    lambda r: r.update(event_id=""),
])
def test_malformed_event_fields_fall_back_and_never_corrupt(mutate):
    row = with_event(forecast(1, 0.6), "9001", "e")
    mutate(row)
    report, _ = build([row], [resolution(1, 0)])
    assert report["forecasts"]["eligible"] == 1


def test_a_forecast_row_with_another_extra_key_is_still_corrupt():
    with pytest.raises(cal.CorruptInput):
        build([{**forecast(1, 0.6), "extra": 1}], [])


# ------------------------------------------------------------------ fills

def test_a_fill_carries_the_event(paths):
    result = paper_ledger.commit_if_allowed(_ok_intent(event_id="9001", event_slug="us-election"), load_risk())
    assert result["allow"] is True
    fill_row = read_jsonl(paths["tmp"] / "paper.jsonl")[0]
    assert (fill_row["event_id"], fill_row["event_slug"]) == ("9001", "us-election")
    assert fill_row["calibration_problems"] == []


@pytest.mark.parametrize("event_id, event_slug", [(None, None), ("", ""), (True, 3), (7, None)])
def test_a_fill_without_a_usable_event_is_booked_without_a_problem(paths, event_id, event_slug):
    result = paper_ledger.commit_if_allowed(_ok_intent(event_id=event_id, event_slug=event_slug), load_risk())
    assert result["allow"] is True
    fill_row = read_jsonl(paths["tmp"] / "paper.jsonl")[0]
    assert fill_row["event_id"] == ci.event_value(event_id) and fill_row["event_slug"] == ci.event_value(event_slug)
    assert fill_row["calibration_problems"] == []


def test_the_intent_builder_path_carries_the_event_from_the_scanner_row():
    row = {"id": "m1", "condition_id": "0xc1", "token_ids": ["a", "b"], "outcomes": ["Yes", "No"],
           "end_date": "2026-12-01T00:00:00Z", "event_id": "9001", "event_slug": "us-election"}
    ctx = intent_builder.market_context_from_row(row, fee_rate=0.0)
    assert (ctx.event_id, ctx.event_slug) == ("9001", "us-election")
    bare = intent_builder.market_context_from_row({k: v for k, v in row.items() if not k.startswith("event")},
                                                  fee_rate=0.0)
    assert (bare.event_id, bare.event_slug) == (None, None)
    junk = intent_builder.market_context_from_row({**row, "event_id": ["x"], "event_slug": 4.5}, fee_rate=0.0)
    assert (junk.event_id, junk.event_slug) == (None, None)
    oi = build_intent(mkt=ib_market(event_id="9001", event_slug="us-election")).intent
    assert (oi.event_id, oi.event_slug) == ("9001", "us-election")
    risk = intent_builder.to_risk_intent(oi)
    assert (risk.event_id, risk.event_slug) == ("9001", "us-election")


def test_the_event_never_changes_the_intent():
    plain = build_intent().intent
    tagged = build_intent(mkt=ib_market(event_id="9001", event_slug="x")).intent
    assert dataclasses.replace(tagged, event_id=None, event_slug=None) == plain


def test_risk_gate_never_reads_the_event():
    src = (ROOT / "src" / "risk_gate.py").read_text(encoding="utf-8")
    assert "event_id" not in src[src.index("def evaluate"):] and "event_slug" not in src[src.index("def evaluate"):]
    # Issue #262: one shared book_ts with the fixed now (utc_now truncates to whole seconds).
    from tests.test_calibration_ids import _stable_book_and_now
    book_ts, now = _stable_book_and_now()
    cfg = load_risk()
    base = dataclasses.asdict(evaluate(_ok_intent(book_ts=book_ts), cfg, dict(STATE), now=now))
    for extra in (dict(event_id="9001", event_slug="x"), dict(event_id=None), dict(event_id=["junk"])):
        assert dataclasses.asdict(evaluate(_ok_intent(book_ts=book_ts, **extra), cfg, dict(STATE), now=now)) == base


# ------------------------------------------------------------------ calibration cluster key

def _events_world(n: int, event_of):
    fs, rs = [], []
    for i in range(n):
        f = forecast(i, 0.6)
        e = event_of(i)
        fs.append(f if e is False else with_event(f, e, None if e is None else f"slug-{e}"))
        rs.append(resolution(i, 0 if i % 3 else 1))
    return fs, rs


def test_no_event_ids_keep_the_condition_id_cluster():
    fs, rs = _events_world(10, lambda i: False)  # rows from before Task BF
    ec = build(fs, rs)[0]["forecasts"]["metrics"]["event_cluster"]
    assert ec["cluster_key"] == "condition_id" and ec["n_clusters"] == 10
    assert ec["event_id_samples"] == 0 and ec["condition_id_fallback_samples"] == 10


def test_event_ids_group_markets_of_one_event():
    fs, rs = _events_world(10, lambda i: "E1" if i < 4 else f"E{i}")
    ec = build(fs, rs)[0]["forecasts"]["metrics"]["event_cluster"]
    assert ec["cluster_key"] == "event_id"
    assert ec["n_clusters"] == 7 and ec["largest_cluster"] == "E1" and ec["largest_cluster_n"] == 4
    assert ec["largest_share"] == pytest.approx(0.4)
    assert ec["event_id_samples"] == 10 and ec["condition_id_fallback_samples"] == 0


def test_rows_without_an_event_fall_back_per_row_and_say_mixed():
    fs, rs = _events_world(10, lambda i: "E1" if i < 3 else (None if i < 6 else False))
    ec = build(fs, rs)[0]["forecasts"]["metrics"]["event_cluster"]
    assert ec["cluster_key"] == "mixed"
    assert ec["event_id_samples"] == 3 and ec["condition_id_fallback_samples"] == 7
    assert ec["n_clusters"] == 8 and ec["largest_cluster"] == "E1"


def test_a_malformed_event_id_falls_back_to_the_condition_id():
    fs = [with_event(forecast(0, 0.6), ["junk"]), with_event(forecast(1, 0.6), "E1")]
    ec = build(fs, [resolution(0, 0), resolution(1, 0)])[0]["forecasts"]["metrics"]["event_cluster"]
    assert ec["cluster_key"] == "mixed" and ec["condition_id_fallback_samples"] == 1


def test_an_empty_section_reports_the_condition_id_key():
    ec = build([], [])[0]["forecasts"]["metrics"]["event_cluster"]
    assert ec["cluster_key"] == "condition_id" and ec["n"] == 0
    assert ec["event_id_samples"] == 0 and ec["condition_id_fallback_samples"] == 0


def test_trades_take_the_fill_event_else_the_forecast_event():
    f0 = with_event(forecast(0, 0.6), "E1")
    f1 = with_event(forecast(1, 0.6), "E1")
    f2 = legacy(forecast(2, 0.6))
    f3 = with_event(forecast(3, 0.6), "E9")
    fills = [fill(f0, event_id="E1", event_slug="s"),  # the fill's own event
             fill(f1),  # pre-BF fill: the joined forecast's event
             fill(f2),  # neither: condition_id
             fill(f3, event_id=None, event_slug=None)]  # recorded null on the fill: the forecast's event
    report, rows = build([f0, f1, f2, f3], [resolution(i, 0) for i in range(4)], fills)
    samples = {s["condition_id"]: s for s in rows if s["kind"] == "trade" and s["eligible"]}
    assert [samples[f"0xc{i}"]["event_id"] for i in range(4)] == ["E1", "E1", None, "E9"]
    ec = report["trades"]["metrics"]["event_cluster"]
    assert ec["cluster_key"] == "mixed" and ec["event_id_samples"] == 3 and ec["condition_id_fallback_samples"] == 1
    assert ec["largest_cluster"] == "E1" and ec["largest_cluster_n"] == 2


def test_a_fill_event_that_disagrees_with_its_forecast_falls_back():
    f0 = with_event(forecast(0, 0.6), "E1")
    report, rows = build([f0], [resolution(0, 0)], [fill(f0, event_id="E2")])
    (sample,) = [s for s in rows if s["kind"] == "trade" and s["eligible"]]
    assert sample["event_id"] is None
    assert report["trades"]["metrics"]["event_cluster"]["cluster_key"] == "condition_id"


def test_the_concentration_flag_uses_the_event_key():
    """40 markets: one market each would be ok; 13 of them in one event is concentrated (> 0.30)."""
    from tests.test_calibration import world
    fs, rs = world(80)
    fs = [with_event(f, "BIG" if i < 30 else f"E{i}") for i, f in enumerate(fs)]
    m = build(fs, rs)[0]["forecasts"]["metrics"]
    assert m["status"] == "sufficient"
    ec = m["event_cluster"]
    assert ec["cluster_key"] == "event_id" and ec["largest_cluster"] == "BIG"
    assert ec["largest_share"] == pytest.approx(30 / 80) and ec["flag"] == "concentrated"
    assert m["verdict"] != "insufficient"  # report only: the flag gates nothing


def test_the_report_says_which_key_it_used():
    fs, rs = _events_world(4, lambda i: "E1")
    report, _ = build(fs, rs)
    markdown = cal.render_markdown(report)
    assert any("event_id" in note and "condition_id" in note for note in report["notes"])
    assert "event cluster (event_id)" in markdown and "fell back" in markdown


def test_bootstrap_intervals_follow_the_event_cluster():
    """Task BG: the bootstrap intervals resample the event_cluster clusters (tests/test_event_id_bootstrap.py);
    one event per market resamples exactly like the market bootstrap, one event for all gives no interval."""
    from tests.test_calibration import world
    fs, rs = world(80)
    plain = build(fs, rs)[0]["forecasts"]["metrics"]
    one_each = build([with_event(f, f"E{i}") for i, f in enumerate(fs)], rs)[0]["forecasts"]["metrics"]
    assert one_each["brier_ci"] == plain["brier_ci"] and one_each["calibration"] == plain["calibration"]
    tagged = build([with_event(f, "E1") for f in fs], rs)[0]["forecasts"]["metrics"]
    assert tagged["brier_ci"] is None and tagged["bootstrap_cluster_key"] == "event_id"


# ------------------------------------------------------------------ reflector

def test_the_reflector_carries_the_event_key(tmp_path):
    from tests.test_reflector_proposals import base, overconfident, rows, run
    report = overconfident(base())
    report["forecasts"]["metrics"]["event_cluster"].update(cluster_key="event_id", event_id_samples=40,
                                                            condition_id_fallback_samples=0)
    run(report, tmp_path)
    (row,) = rows(tmp_path)
    assert row["evidence"]["event_cluster"]["cluster_key"] == "event_id"
    report["forecasts"]["metrics"]["event_cluster"].update(cluster_key="mixed", flag="concentrated", largest_share=0.5)
    run(report, tmp_path / "b")
    (row,) = rows(tmp_path / "b")
    assert "cluster (mixed)" in row["risk"]


def test_offline_report_round_trip_with_events(tmp_path):
    fs, rs = _events_world(6, lambda i: "E1" if i % 2 else False)
    report, _ = build(fs, rs)
    json.dumps(report, allow_nan=False)  # still exact JSON
