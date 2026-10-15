"""Task AZ (M3 step 3): offline calibration report. Synthetic data only; no network."""
from __future__ import annotations

import ast
import json
import random
from pathlib import Path

import pytest

from src import calibration as cal
from src import calibration_ids as ci
from src import resolutions as res
from src import thresholds as th

ROOT = Path(__file__).resolve().parents[1]
NOW = "2026-11-20T00:00:00Z"


def forecast(i: int, p: float, *, outcome_index: int = 0, token: str | None = None, condition: str | None = None,
             ts: str | None = None, problems: list[str] | None = None, strategy: str = "news-forecaster",
             market: str | None = None, category: str | None = "Politics", version: str = "v1") -> dict:
    condition = condition or f"0xc{i}"
    market = market or f"m{i}"
    token = token if token is not None else f"t{i}-{outcome_index}"
    ts = ts or f"2026-10-01T00:{i // 60:02d}:{i % 60:02d}Z"
    row = {"schema_version": 1, "forecast_id": "", "ts": ts, "source": "news", "strategy": strategy,
           "market_id": market, "condition_id": condition, "outcome_index": outcome_index,
           "outcome": "Yes" if outcome_index == 0 else "No", "token_id": token, "model_prob": p, "p_low": None,
           "p_high": None, "category": category, "tags": ["x"], "end_date": "2026-10-20T00:00:00Z",
           "model_version": version, "note": "", "sealed_before_book_read": True, "calibration_problems": [],
           "event_id": None, "event_slug": None}  # Task BF event fields (None: falls back to condition_id)
    row["forecast_id"] = ci.make_forecast_id(source="news", ts=ts, market_id=market, condition_id=condition,
                                             outcome_index=outcome_index, token_id=token, model_prob=p, p_low=None,
                                             p_high=None, model_version=version)
    if problems is not None:
        row["calibration_problems"] = problems
    return {k: row[k] for k in ci.FORECAST_ROW_FIELDS}


def resolution(i: int, winner: int, *, day: int | None = None, status: str = "resolved", uma: bool = True,
               prices: list[float] | None = None, condition: str | None = None) -> dict:
    day = (i % 7) + 1 if day is None else day
    prices = prices if prices is not None else [1.0, 0.0] if winner == 0 else [0.0, 1.0]
    return {"condition_id": condition or f"0xc{i}", "winning_token_id": f"t{i}-{winner}",
            "outcome_prices_final": prices, "closed_ts": f"2026-10-{day:02d}T12:00:00Z",
            "snapshot_ts": "2026-10-15T00:00:00Z", "status": status, "uma_checked": uma}


def fill(f: dict, *, side: str = "buy", shares: float = 10.0, price: float = 0.4, edge: float = 0.05,
         fee: float = 0.01, fill_id: str | None = None, ts: str = "2026-10-01T06:00:00Z", **over) -> dict:
    # issue #70: the default fill sits between the default forecast ts (00:0x) and the earliest default
    # close (2026-10-01T12:00:00Z), so a plain fixture fill is provably in the window.
    row = {"fill_id": fill_id or f"f-{f['forecast_id'][:10]}-{side}-{shares}", "ts": ts, "strategy": f["strategy"],
           "market": f["market_id"], "token_id": f["token_id"], "side": side, "model_prob": f["model_prob"],
           "market_price": price, "edge": edge, "fee_usd": fee, "notional_usd": round(shares * price, 8),
           "shares": shares, "hypothetical": False, "counts_toward_risk": True, "risk_decision": "allow",
           "decision_id": ci.make_decision_id(f["strategy"], f["market_id"], f["token_id"], f["forecast_id"]),
           "forecast_id": f["forecast_id"], "forecast_ts": f["ts"], "condition_id": f["condition_id"],
           "outcome_index": f["outcome_index"], "outcome": f["outcome"], "category": f["category"],
           "tags": f["tags"], "end_date": f["end_date"], "model_version": f["model_version"]}
    row.update(over)
    row["calibration_problems"] = ci.fill_problems(row)
    return row


def world(n: int, seed: int = 7, *, days: int = 7):
    """n markets, one forecast each, outcomes drawn from the forecast p (well calibrated)."""
    rng = random.Random(seed)
    fs, rs = [], []
    for i in range(n):
        p = round(rng.uniform(0.05, 0.95), 3)
        fs.append(forecast(i, p))
        rs.append(resolution(i, 0 if rng.random() < p else 1, day=(i % days) + 1))
    return fs, rs


def build(fs, rs, fills=()):
    return cal.build_report(list(fs), list(rs), list(fills), generated_at=NOW)


# ------------------------------------------------------------------ joins and drops


def test_schema_matches_the_resolutions_writer():
    assert cal.RESOLUTION_FIELDS == res.ROW_FIELDS
    assert cal.LEGACY_RESOLUTION_FIELDS == tuple(k for k in res.ROW_FIELDS if k != "outcome_token_ids")


def test_the_eight_field_schema_is_accepted_and_a_bad_mapping_is_corrupt():
    # issue #81: the consumer names two schemas only; a mapping that cannot identify the outcomes
    # is corrupt input, exactly like any other off-schema row.
    row = resolution(1, 0)
    row["outcome_token_ids"] = ["t1-0", "t1-1"]
    by_condition, disputed = cal.check_resolution_rows([row])
    assert by_condition["0xc1"]["outcome_token_ids"] == ["t1-0", "t1-1"] and disputed == set()
    for broken in (["t1-0", "t1-0"], ["t1-0"], "t1-0", ["t1-1", "t1-0"], ["t1-0", ""]):
        with pytest.raises(cal.CorruptInput):
            cal.check_resolution_rows([{**row, "outcome_token_ids": broken}])
    with pytest.raises(cal.CorruptInput):  # a key that is in neither schema
        cal.check_resolution_rows([{**row, "note": "x"}])


def test_a_clean_join_scores_every_market():
    fs, rs = world(40)
    report, samples = build(fs, rs)
    f = report["forecasts"]
    assert f["eligible"] == 40 and f["dropped"] == 0 and f["dropped_by_reason"] == {}
    expected = sum((x["model_prob"] - (1 if r["winning_token_id"].endswith("-0") else 0)) ** 2
                   for x, r in zip(fs, rs)) / 40
    assert f["metrics"]["brier"] == pytest.approx(expected)
    assert sum(1 for s in samples if s["eligible"]) == 40
    assert report["complete"] is True and report["thresholds"]["status"] == "M3 pending"


def test_every_drop_is_counted_and_nothing_is_imputed():
    fs, rs = world(3)
    fs += [
        forecast(10, 0.5, problems=["missing_token_id"]),       # flagged at seal time
        forecast(11, 0.5),                                       # no resolution yet
        forecast(12, 0.5),                                       # closed, UMA not resolved
        forecast(13, 0.5),                                       # unknown status
        forecast(14, 0.5),                                       # void prices
        forecast(15, 0.5, outcome_index=0, token="t15-1"),       # token says outcome 1, index says 0
        forecast(16, 0.5),                                       # disputed snapshots
        forecast(0, 0.9, ts="2026-10-01T06:00:00Z"),             # second forecast on market 0 (before its close)
        forecast(17, 0.5, strategy="phase0-gate-test"),
        forecast(18, 0.5, market="e2e-alpha"),
        forecast(19, 0.5, outcome_index=5),                      # out of range for a binary market
    ]
    rs += [resolution(12, 0, status="closed", uma=False), resolution(13, 0, status="weird"),
           resolution(14, 0, prices=[0.5, 0.5]), resolution(15, 1), resolution(16, 0),
           {**resolution(16, 1)}, resolution(19, 0)]
    report, samples = build(fs, rs)
    f = report["forecasts"]
    assert f["eligible"] == 3
    assert f["dropped_by_reason"] == {
        "duplicate_market_forecast": 1, "excluded_market": 1, "excluded_strategy": 1,
        "forecast_calibration_problems": 1, "outcome_index_out_of_range": 1, "resolution_ambiguous_status": 1,
        "resolution_disputed": 1, "resolution_not_uma_resolved": 1, "resolution_pending": 1,
        "resolution_void": 1, "token_outcome_mismatch": 1}
    assert f["dropped"] == 11 and f["calibration_problems"] == {"missing_token_id": 1}
    dropped = [s for s in samples if not s["eligible"]]
    assert len(dropped) == 11 and all(s["reason"] for s in dropped)
    assert report["resolutions"]["disputed"] == 1


def test_a_stored_clean_list_does_not_hide_a_recomputed_problem():
    row = forecast(1, 0.5)
    row["token_id"] = None  # problems list still says [] but the row cannot join
    report, _ = build([row], [resolution(1, 0)])
    assert report["forecasts"]["dropped_by_reason"] == {"forecast_calibration_problems": 1}
    assert report["forecasts"]["calibration_problems"] == {"missing_token_id": 1}


def test_the_first_sealed_forecast_per_market_wins():
    late = forecast(0, 0.9, ts="2026-10-01T08:00:00Z")  # both sealed before the 2026-10-01T12:00:00Z close
    early = forecast(0, 0.2, ts="2026-10-01T00:00:00Z")
    report, samples = build([late, early], [resolution(0, 0)])
    kept = [s for s in samples if s["eligible"]]
    assert [s["p"] for s in kept] == [0.2]


# ------------------------------------------------------------------ causal time (issue #70)


def test_a_forecast_strictly_before_the_close_is_kept():
    f = forecast(0, 0.7, ts="2026-10-01T11:59:59Z")  # one second before the close
    report, samples = build([f], [resolution(0, 0, day=1)])
    assert report["forecasts"]["eligible"] == 1 and report["forecasts"]["dropped"] == 0
    assert samples[0]["eligible"] is True


def test_a_forecast_at_the_close_moment_is_dropped():
    f = forecast(0, 0.99, ts="2026-10-01T12:00:00Z")  # exactly resolution(0).closed_ts
    report, samples = build([f], [resolution(0, 0, day=1)])
    assert report["forecasts"]["eligible"] == 0
    assert report["forecasts"]["dropped_by_reason"] == {"forecast_not_before_close": 1}
    assert [s["reason"] for s in samples if not s["eligible"]] == ["forecast_not_before_close"]


def test_a_forecast_after_the_settlement_is_never_a_sample():
    # issue #70 repro: closed 2026-10-10, forecast 2026-10-20, winner prob .99: no sample, no Brier
    f = forecast(0, 0.99, ts="2026-10-20T00:00:00Z")
    report, samples = build([f], [resolution(0, 0, day=10)])
    assert report["forecasts"]["eligible"] == 0
    assert report["forecasts"]["metrics"]["brier"] is None
    assert report["forecasts"]["dropped_by_reason"] == {"forecast_not_before_close": 1}


@pytest.mark.parametrize("ts", ["yesterday", "2026-10-01", "2026-10-01T00:00:00", "2026-10-01T25:00:00Z"])
def test_a_forecast_whose_time_cannot_be_parsed_or_has_no_zone_is_dropped(ts):
    f = forecast(0, 0.5, ts=ts)
    report, samples = build([f], [resolution(0, 0, day=1)])
    assert report["forecasts"]["eligible"] == 0
    assert report["forecasts"]["dropped_by_reason"] == {"forecast_ts_invalid": 1}
    assert [s["reason"] for s in samples if not s["eligible"]] == ["forecast_ts_invalid"]


def test_a_missing_forecast_time_is_dropped_and_recorded():
    row = forecast(0, 0.5)
    row["ts"] = ""  # a blank time cannot prove when the forecast was sealed
    report, samples = build([row], [resolution(0, 0)])
    assert report["forecasts"]["eligible"] == 0
    assert report["forecasts"]["dropped_by_reason"] == {"forecast_calibration_problems": 1}
    assert report["forecasts"]["calibration_problems"] == {"missing_ts": 1}


def test_the_same_moment_in_another_offset_is_not_before_the_close():
    # closed 2026-10-01T12:00:00Z: 14:00+02:00 and 07:00-05:00 are that same moment; 18:30+08:00 is 10:30Z
    fs = [forecast(0, 0.99, ts="2026-10-01T14:00:00+02:00"),
          forecast(1, 0.99, ts="2026-10-01T07:00:00-05:00"),
          forecast(2, 0.5, ts="2026-10-01T18:30:00+08:00")]
    report, samples = build(fs, [resolution(i, 0, day=1) for i in range(3)])
    assert report["forecasts"]["eligible"] == 1
    assert report["forecasts"]["dropped_by_reason"] == {"forecast_not_before_close": 2}
    kept = [s for s in samples if s["eligible"]]
    assert [s["forecast_id"] for s in kept] == [fs[2]["forecast_id"]]


def test_an_unprovable_time_never_takes_the_first_forecast_slot():
    bad = forecast(0, 0.99, ts="2026-09-30T00:00")  # sorts first but has no timezone
    good = forecast(0, 0.2, ts="2026-10-01T11:00:00Z")
    report, samples = build([bad, good], [resolution(0, 0, day=1)])
    kept = [s for s in samples if s["eligible"]]
    assert [s["p"] for s in kept] == [0.2]
    assert report["forecasts"]["dropped_by_reason"] == {"forecast_ts_invalid": 1}


def test_the_first_forecast_slot_follows_utc_not_the_written_form():
    # 2026-10-01T02:00:00+05:00 is 2026-09-30T21:00:00Z: sealed before 2026-10-01T00:30:00Z,
    # although its written form sorts after that text
    early = forecast(0, 0.2, ts="2026-10-01T02:00:00+05:00")
    late = forecast(0, 0.9, ts="2026-10-01T00:30:00Z")
    report, samples = build([late, early], [resolution(0, 0, day=1)])
    kept = [s for s in samples if s.get("kind") == "forecast" and s["eligible"]]
    assert [s["forecast_id"] for s in kept] == [early["forecast_id"]]
    assert report["forecasts"]["dropped_by_reason"] == {"duplicate_market_forecast": 1}


def test_mixed_times_drop_exactly_and_match_samples_jsonl(tmp_path):
    fs = [forecast(0, 0.7, ts="2026-10-01T11:00:00Z"),          # before the close: kept
          forecast(1, 0.99, ts="2026-10-01T12:00:00Z"),          # at the close
          forecast(2, 0.99, ts="2026-10-20T00:00:00Z"),          # after the close
          forecast(3, 0.5, ts="yesterday"),                      # unparseable
          forecast(4, 0.5, ts="2026-10-01T00:00:00"),            # no timezone
          forecast(5, 0.5, ts="2026-10-01T13:00:00+02:00"),      # 11:00Z: before the close
          forecast(6, 0.5)]                                      # no time at all
    fs[6]["ts"] = ""
    rs = [resolution(i, 0, day=1) for i in range(7)]
    report, samples = build(fs, rs)
    f = report["forecasts"]
    assert f["eligible"] == 2 and f["dropped"] == 5
    assert f["dropped_by_reason"] == {"forecast_calibration_problems": 1, "forecast_not_before_close": 2,
                                      "forecast_ts_invalid": 2}
    assert f["calibration_problems"] == {"missing_ts": 1}
    assert sum(1 for s in samples if s["eligible"]) == 2
    assert sum(1 for s in samples if not s["eligible"]) == 5
    out = tmp_path / "out"
    code = cal.main(["--forecasts", str(_write(tmp_path / "f.jsonl", fs)), "--resolutions",
                     str(_write(tmp_path / "r.jsonl", rs)), "--ledger", str(tmp_path / "absent.jsonl"),
                     "--out-dir", str(out), "--now", NOW])
    assert code == cal.EXIT_OK
    written = [json.loads(line) for line
               in (out / "2026-11-20" / "samples.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(written) == 7
    assert sum(1 for s in written if s["eligible"]) == 2
    reasons = {}
    for s in written:
        if not s["eligible"]:
            reasons[s["reason"]] = reasons.get(s["reason"], 0) + 1
    assert reasons == f["dropped_by_reason"]


# ------------------------------------------------------------------ corrupt input


@pytest.mark.parametrize("mutate", [
    lambda r: r.pop("note"),
    lambda r: r.update(extra=1),
    lambda r: r.update(model_prob="0.5"),
    lambda r: r.update(calibration_problems="none"),
    lambda r: r.update(tags=[1]),
])
def test_a_forecast_row_off_its_schema_is_corrupt(mutate):
    row = forecast(1, 0.5)
    mutate(row)
    with pytest.raises(cal.CorruptInput):
        build([row], [])


def test_a_conflicting_forecast_id_is_corrupt():
    a = forecast(1, 0.5)
    with pytest.raises(cal.CorruptInput):
        build([a, {**a, "note": "changed"}], [])
    report, _ = build([a, dict(a)], [resolution(1, 0)])  # an exact repeat is the same forecast
    assert report["forecasts"]["distinct"] == 1


@pytest.mark.parametrize("mutate", [
    lambda r: r.pop("status"), lambda r: r.update(uma_checked="true"), lambda r: r.update(closed_ts="yesterday"),
    lambda r: r.update(outcome_prices_final=["1", "0"]), lambda r: r.update(condition_id=""),
])
def test_a_resolution_row_off_its_schema_is_corrupt(mutate):
    row = resolution(1, 0)
    mutate(row)
    with pytest.raises(cal.CorruptInput):
        build([forecast(1, 0.5)], [row])


@pytest.mark.parametrize("data", [b'{"a": 1}', b'{"a": 1}\n\n', b'not json\n', b'[1]\n', b'{"a": NaN}\n',
                                  b'\xff\xfe\n'])
def test_strict_reader_rejects_torn_or_bad_files(tmp_path, data):
    path = tmp_path / "x.jsonl"
    path.write_bytes(data)
    with pytest.raises(cal.CorruptInput):
        cal.read_strict_jsonl(path, "x")


def test_an_absent_file_is_empty_not_corrupt(tmp_path):
    rows, meta = cal.read_strict_jsonl(tmp_path / "none.jsonl", "x")
    assert rows == [] and meta["present"] is False


# ------------------------------------------------------------------ minimums and verdicts


def test_below_the_minimums_the_report_says_insufficient():
    fs, rs = world(th.CALIBRATION_MIN_SAMPLES - 1)
    m = build(fs, rs)[0]["forecasts"]["metrics"]
    assert m["status"] == "insufficient" and m["verdict"] == "insufficient"
    assert any("samples" in s for s in m["short_of"]) and "brier_ci" not in m


def test_too_few_resolution_days_is_insufficient_even_with_enough_samples():
    fs, rs = world(40, days=th.CALIBRATION_MIN_RESOLUTION_DAYS - 1)
    m = build(fs, rs)[0]["forecasts"]["metrics"]
    assert m["status"] == "insufficient" and m["short_of"] == [
        f"resolution days {th.CALIBRATION_MIN_RESOLUTION_DAYS - 1} < {th.CALIBRATION_MIN_RESOLUTION_DAYS}"]


def test_enough_samples_give_a_verdict_with_intervals():
    fs, rs = world(60)
    m = build(fs, rs)[0]["forecasts"]["metrics"]
    assert m["status"] == "sufficient" and isinstance(m["verdict"], dict)
    low, high = m["brier_ci"]
    assert low <= m["brier"] <= high
    assert m["reliability"]["kind"] == "equal_frequency" and len(m["reliability"]["bins"]) == th.CALIBRATION_EQUAL_FREQ_BINS


def test_an_overconfident_model_is_called_overconfident():
    rng = random.Random(3)
    fs, rs = [], []
    for i in range(400):
        truth = rng.uniform(0.3, 0.7)
        p = min(0.99, max(0.01, 0.5 + 2.5 * (truth - 0.5)))  # stretched away from 0.5
        fs.append(forecast(i, round(p, 4)))
        rs.append(resolution(i, 0 if rng.random() < truth else 1))
    m = build(fs, rs)[0]["forecasts"]["metrics"]
    assert m["reliability"]["kind"] == "fixed"
    assert m["verdict"]["calibration_slope"] == "overconfident"  # Task BD added calibration_intercept beside it
    assert m["calibration"]["slope"] < 1


def test_bootstrap_is_deterministic():
    fs, rs = world(50)
    assert build(fs, rs)[0] == build(fs, rs)[0]


def test_groups_below_their_minimum_have_no_brier():
    fs, rs = world(40)
    fs[0] = forecast(0, fs[0]["model_prob"], category="Rare")
    groups = build(fs, rs)[0]["forecasts"]["by_category"]
    assert groups["Rare"] == {"n": 1, "markets": 1, "status": "insufficient", "brier": None,
                              "intervals": None}  # Task BD: by_category carries intervals (None when not judged)
    assert groups["Politics"]["status"] == "sufficient" and groups["Politics"]["brier"] is not None


# ------------------------------------------------------------------ metrics


def test_metric_helpers():
    assert cal.brier([(0.7, 1), (0.2, 0)]) == pytest.approx((0.09 + 0.04) / 2)
    assert cal.wilson(0, 10)[0] == 0.0 and cal.wilson(10, 10)[1] == 1.0
    low, high = cal.wilson(5, 10)
    assert low == pytest.approx(0.2366, abs=1e-3) and high == pytest.approx(0.7634, abs=1e-3)
    assert cal.logistic_fit([(0.2, 0), (0.8, 1), (0.3, 0)]) is None  # separable: not estimable
    assert cal.logistic_fit([(0.5, 1)] * 5) is None
    clip = th.CALIBRATION_LOGLOSS_CLIP
    assert cal.log_loss([(0.0, 1)]) == pytest.approx(-__import__("math").log(clip))
    rel = cal.reliability([(0.05, 0), (0.95, 1)] * 60)
    assert rel["kind"] == "fixed" and rel["ece"] == pytest.approx(0.05)


# ------------------------------------------------------------------ trade sample


def test_trades_join_by_decision_and_score_against_the_market():
    fs, rs = world(4)
    fills = [fill(fs[0], shares=10, price=0.4), fill(fs[0], shares=5, price=0.42, fill_id="second"),
             fill(fs[1], price=0.6)]
    t = build(fs, rs, fills)[0]["trades"]
    assert t["eligible"] == 2 and t["dropped"] == 0
    first = fs[0]
    y = 1 if rs[0]["winning_token_id"] == first["token_id"] else 0
    cost = 10 * 0.4 + 0.01 + 5 * 0.42 + 0.01
    sample = next(s for s in build(fs, rs, fills)[1] if s.get("decision_id") and s["eligible"]
                  and s["forecast_id"] == first["forecast_id"])
    assert sample["fills"] == 2 and sample["q"] == 0.4
    assert sample["realized_usd"] == pytest.approx(15 * y - cost)
    assert t["metrics"]["status"] == "insufficient" and t["metrics"]["verdict"] == "insufficient"


def test_trade_drops_are_counted():
    fs, rs = world(5)
    legacy = fill(fs[0])
    legacy.pop("calibration_problems")
    unknown = fill(forecast(99, 0.5))
    flagged = fill(fs[1], forecast_id=None)
    hypothetical = fill(fs[2], hypothetical=True)
    excluded = fill(fs[3], strategy="wash-probe")
    t = build(fs, rs, [legacy, unknown, flagged, hypothetical, excluded])[0]["trades"]
    assert t["eligible"] == 0
    assert t["dropped_by_reason"] == {"excluded_strategy": 1, "fill_calibration_problems": 1,
                                      "fill_forecast_not_found": 1, "fill_missing_identity": 1}


def test_a_third_entry_on_one_market_is_capped():
    f0 = forecast(0, 0.6)
    fs = [f0, forecast(0, 0.6, ts="2026-10-02T00:00:00Z"), forecast(0, 0.6, ts="2026-10-03T00:00:00Z")]
    fills = [fill(f, ts=f["ts"]) for f in fs]
    t = build(fs, [resolution(0, 0, day=5)], fills)[0]["trades"]  # close 2026-10-05: all fills provably before it
    assert t["eligible"] == 2 and t["dropped_by_reason"] == {"re_entry_cap": 1}
    assert t["metrics"]["re_entries"] == 1


def test_trade_verdict_needs_the_minimums():
    rng = random.Random(11)
    fs, rs, fills = [], [], []
    for i in range(40):
        p = round(rng.uniform(0.2, 0.8), 3)
        f = forecast(i, p)
        fs.append(f)
        rs.append(resolution(i, 0 if rng.random() < p else 1))
        fills.append(fill(f, price=0.5))
    m = build(fs, rs, fills)[0]["trades"]["metrics"]
    assert m["status"] == "sufficient" and m["verdict"]["model_vs_market"] in {
        "better_than_market", "worse_than_market", "inconclusive"}
    low, high = m["delta_brier_ci"]
    assert low <= m["delta_brier"] <= high


# ------------------------------------------------------------------ causal fill time (issue #70)


def test_a_fill_between_its_forecast_and_the_close_is_kept():
    f = forecast(0, 0.7, ts="2026-10-01T00:00:00Z")  # close 2026-10-01T12:00:00Z
    t = build([f], [resolution(0, 0, day=1)], [fill(f, ts="2026-10-01T06:00:00Z")])[0]["trades"]
    assert t["eligible"] == 1 and t["dropped"] == 0 and t["dropped_by_reason"] == {}


def test_a_fill_at_its_forecast_moment_is_kept_but_at_the_close_is_dropped():
    f0 = forecast(0, 0.7, ts="2026-10-01T10:00:00Z")
    f1 = forecast(1, 0.99, ts="2026-10-01T00:00:00Z")
    fills = [fill(f0, ts="2026-10-01T10:00:00Z"),  # sealed and filled at the same instant: still causal
             fill(f1, ts="2026-10-01T12:00:00Z")]  # exactly resolution(1).closed_ts: not before it
    t = build([f0, f1], [resolution(i, 0, day=1) for i in range(2)], fills)[0]["trades"]
    assert t["eligible"] == 1 and t["dropped_by_reason"] == {"fill_not_before_close": 1}


def test_a_fill_after_the_settlement_is_never_a_sample():
    f = forecast(0, 0.99, ts="2026-10-01T00:00:00Z")
    t = build([f], [resolution(0, 0, day=10)], [fill(f, ts="2026-10-20T00:00:00Z")])[0]["trades"]
    assert t["eligible"] == 0 and t["dropped_by_reason"] == {"fill_not_before_close": 1}


def test_a_fill_before_its_forecast_is_dropped():
    f = forecast(0, 0.7, ts="2026-10-01T06:00:00Z")
    t = build([f], [resolution(0, 0, day=1)], [fill(f, ts="2026-10-01T05:59:59Z")])[0]["trades"]
    assert t["eligible"] == 0 and t["dropped_by_reason"] == {"fill_before_forecast": 1}


@pytest.mark.parametrize("ts", ["yesterday", "2026-10-01", "2026-10-01T00:00:00", "2026-10-01T25:00:00Z", ""])
def test_a_fill_whose_time_cannot_be_proved_is_dropped(ts):
    f = forecast(0, 0.7)
    t = build([f], [resolution(0, 0)], [fill(f, ts=ts)])[0]["trades"]
    assert t["eligible"] == 0 and t["dropped_by_reason"] == {"fill_ts_invalid": 1}


def test_an_unprovable_forecast_time_rejects_its_trades():
    f = forecast(0, 0.7, ts="2026-10-01T00:00:00")  # no timezone: the fills' order cannot be proved
    t = build([f], [resolution(0, 0)], [fill(f)])[0]["trades"]
    assert t["eligible"] == 0 and t["dropped_by_reason"] == {"fill_forecast_ts_invalid": 1}


def test_the_same_moment_in_another_offset_is_counted_once():
    # close 2026-10-01T12:00:00Z: 14:00+02:00 and 07:00-05:00 are that moment, 18:30+08:00 is 10:30Z
    fs = [forecast(i, 0.99, ts="2026-10-01T00:00:00Z") for i in range(3)]
    fills = [fill(fs[0], ts="2026-10-01T14:00:00+02:00"), fill(fs[1], ts="2026-10-01T07:00:00-05:00"),
             fill(fs[2], ts="2026-10-01T18:30:00+08:00")]
    t = build(fs, [resolution(i, 0, day=1) for i in range(3)], fills)[0]["trades"]
    assert t["eligible"] == 1 and t["dropped_by_reason"] == {"fill_not_before_close": 2}


def test_one_unprovable_fill_rejects_the_whole_decision_and_counts_every_fill():
    f = forecast(0, 0.7, ts="2026-10-01T06:00:00Z")
    fills = [fill(f, fill_id="good", ts="2026-10-01T08:00:00Z"),
             fill(f, fill_id="early", ts="2026-10-01T05:00:00Z")]  # before its own forecast
    report, samples = build([f], [resolution(0, 0, day=1)], fills)
    t = report["trades"]
    assert t["eligible"] == 0 and t["dropped_by_reason"] == {"fill_before_forecast": 2}
    dropped = [s for s in samples if s.get("kind") == "trade" and not s["eligible"]]
    assert len(dropped) == 1 and dropped[0]["fills"] == 2 and dropped[0]["reason"] == "fill_before_forecast"


def test_mixed_fill_times_drop_exactly_and_match_samples_jsonl(tmp_path):
    fs = [forecast(i, 0.7, ts="2026-10-01T00:00:00Z") for i in range(6)]
    rs = [resolution(i, 0, day=1) for i in range(6)]  # every close 2026-10-01T12:00:00Z
    fills = [fill(fs[0], fill_id="ok", ts="2026-10-01T06:00:00Z"),           # in the window: kept
             fill(fs[1], fill_id="late", ts="2026-10-01T12:00:00Z"),         # at the close
             fill(fs[2], fill_id="early", ts="2026-09-30T23:00:00Z"),        # before its forecast
             fill(fs[3], fill_id="bad", ts="yesterday"),                     # unparseable
             fill(fs[4], fill_id="unzoned", ts="2026-10-01T06:00:00"),       # no timezone
             fill(fs[5], fill_id="two-ok", ts="2026-10-01T07:00:00Z"),       # one decision, two fills:
             fill(fs[5], fill_id="two-late", ts="2026-10-01T14:00:00+02:00")]  # the close in another offset
    report, samples = build(fs, rs, fills)
    t = report["trades"]
    assert t["eligible"] == 1 and t["dropped"] == 6
    assert t["dropped_by_reason"] == {"fill_before_forecast": 1, "fill_not_before_close": 3,
                                      "fill_ts_invalid": 2}
    folder = cal.write_report(tmp_path / "out", report, samples)  # the report writer, minus the CLI readers
    written = [json.loads(line) for line
               in (folder / "samples.jsonl").read_text(encoding="utf-8").splitlines()]
    trade_rows = [s for s in written if s.get("kind") == "trade"]
    assert sum(1 for s in trade_rows if s["eligible"]) == t["eligible"]
    assert sum(s.get("fills", 1) for s in trade_rows if not s["eligible"]) == t["dropped"]
    reasons = {}
    for s in trade_rows:
        if not s["eligible"]:
            reasons[s["reason"]] = reasons.get(s["reason"], 0) + s.get("fills", 1)
    assert reasons == t["dropped_by_reason"]


def test_an_unprovable_time_never_takes_a_trade_re_entry_slot():
    fs = [forecast(0, 0.6, ts="2026-10-01T00:00:00Z"),
          forecast(0, 0.6, ts="2026-10-02T00:00:00Z"),
          forecast(0, 0.6, ts="2026-10-03T00:00:00Z")]
    fills = [fill(fs[0], fill_id="first", ts="2026-10-01T01:00:00Z"),
             fill(fs[1], fill_id="bad", ts="yesterday"),  # unprovable: no slot and no cap budget
             fill(fs[2], fill_id="third", ts="2026-10-03T01:00:00Z")]
    report, _ = build(fs, [resolution(0, 0, day=5)], fills)  # close 2026-10-05: the two provable fills fit
    t = report["trades"]
    assert t["eligible"] == 2 and t["dropped_by_reason"] == {"fill_ts_invalid": 1}
    assert t["metrics"]["re_entries"] == 1


def test_a_trade_re_entry_slot_follows_utc_not_the_written_form():
    fs = [forecast(0, 0.6, ts="2026-09-30T00:00:00Z"),
          forecast(0, 0.6, ts="2026-09-30T12:00:00Z"),
          forecast(0, 0.6, ts="2026-09-30T18:00:00Z")]
    fills = [fill(fs[0], fill_id="utc-first", ts="2026-10-01T02:00:00+05:00"),  # 2026-09-30T21:00:00Z
             fill(fs[1], fill_id="utc-second", ts="2026-10-01T00:30:00Z"),
             fill(fs[2], fill_id="utc-third", ts="2026-10-01T01:00:00Z")]
    report, samples = build(fs, [resolution(0, 0, day=1)], fills)
    t = report["trades"]
    assert t["eligible"] == 2 and t["dropped_by_reason"] == {"re_entry_cap": 1}
    kept = {s["forecast_id"] for s in samples if s.get("kind") == "trade" and s["eligible"]}
    assert kept == {fs[0]["forecast_id"], fs[1]["forecast_id"]}  # the two earliest UTC fills keep the slots


# ------------------------------------------------------------------ CLI


def _write(path: Path, rows) -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


def test_cli_writes_the_report_set(tmp_path, capsys):
    fs, rs = world(12)
    args = ["--forecasts", str(_write(tmp_path / "f.jsonl", fs)), "--resolutions",
            str(_write(tmp_path / "r.jsonl", rs)), "--ledger", str(tmp_path / "absent.jsonl"),
            "--out-dir", str(tmp_path / "out"), "--now", NOW]
    assert cal.main(args) == cal.EXIT_OK
    folder = tmp_path / "out" / "2026-11-20"
    report = json.loads((folder / "report.json").read_text(encoding="utf-8"))
    assert report["forecasts"]["eligible"] == 12 and report["forecasts"]["metrics"]["status"] == "insufficient"
    assert report["inputs"]["ledger"]["present"] is False and report["inputs"]["forecasts"]["sha256"]
    assert "insufficient" in (folder / "report.md").read_text(encoding="utf-8")
    assert len((folder / "samples.jsonl").read_text(encoding="utf-8").splitlines()) == 12
    assert "insufficient" in capsys.readouterr().out


def test_cli_corrupt_input_writes_nothing(tmp_path):
    (tmp_path / "f.jsonl").write_bytes(b'{"torn": ')
    out = tmp_path / "out"
    code = cal.main(["--forecasts", str(tmp_path / "f.jsonl"), "--resolutions", str(tmp_path / "r.jsonl"),
                     "--ledger", str(tmp_path / "l.jsonl"), "--out-dir", str(out), "--now", NOW])
    assert code == cal.EXIT_CORRUPT_INPUT == 3 and not out.exists()


def test_cli_schema_violation_writes_nothing(tmp_path):
    bad = forecast(1, 0.5)
    bad.pop("note")
    out = tmp_path / "out"
    code = cal.main(["--forecasts", str(_write(tmp_path / "f.jsonl", [bad])), "--resolutions",
                     str(tmp_path / "r.jsonl"), "--ledger", str(tmp_path / "l.jsonl"), "--out-dir", str(out),
                     "--now", NOW])
    assert code == cal.EXIT_CORRUPT_INPUT and not out.exists()


def test_cli_invalid_ledger_writes_nothing(tmp_path):
    (tmp_path / "l.jsonl").write_bytes(b'{"fill_id": "a", "ts": "x"\n')
    out = tmp_path / "out"
    code = cal.main(["--forecasts", str(tmp_path / "f.jsonl"), "--resolutions", str(tmp_path / "r.jsonl"),
                     "--ledger", str(tmp_path / "l.jsonl"), "--out-dir", str(out), "--now", NOW])
    assert code == cal.EXIT_LEDGER_INVALID == 4 and not out.exists()


def test_cli_write_failure(tmp_path):
    blocker = tmp_path / "out"
    blocker.write_text("a file, not a folder", encoding="utf-8")
    code = cal.main(["--forecasts", str(tmp_path / "f.jsonl"), "--resolutions", str(tmp_path / "r.jsonl"),
                     "--ledger", str(tmp_path / "l.jsonl"), "--out-dir", str(blocker), "--now", NOW])
    assert code == cal.EXIT_WRITE_FAILED == 5


@pytest.mark.parametrize("argv", [["--bogus"], ["--now", "yesterday"], ["--now", "2026-10-01T00:00:00"]])
def test_cli_usage_errors(argv):
    assert cal.main(argv) == cal.EXIT_USAGE == 2


def test_cli_rewrite_replaces_the_whole_set(tmp_path):
    fs, rs = world(3)
    args = ["--forecasts", str(_write(tmp_path / "f.jsonl", fs)), "--resolutions",
            str(_write(tmp_path / "r.jsonl", rs)), "--ledger", str(tmp_path / "l.jsonl"),
            "--out-dir", str(tmp_path / "out"), "--now", NOW]
    assert cal.main(args) == 0 and cal.main(args) == 0
    assert sorted(p.name for p in (tmp_path / "out" / "2026-11-20").iterdir()) == [
        "report.json", "report.md", "samples.jsonl"]


# ------------------------------------------------------------------ boundaries


def test_offline_and_read_only():
    tree = ast.parse((ROOT / "src" / "calibration.py").read_text(encoding="utf-8"))
    mods = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    mods |= {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not mods & {"urllib", "urllib.request", "http.client", "socket", "ssl", "src.scanner", "src.resolutions",
                       "src.risk_gate", "src.paper_ledger"}, mods
    text = (ROOT / "src" / "calibration.py").read_text(encoding="utf-8")
    for banned in ("bus(", "public_get", "strategies.toml", "halt(", "append_jsonl"):
        assert banned not in text, banned


def test_trade_groups_follow_the_design_tiers():
    assert cal.tier(0.03, th.CALIBRATION_EDGE_TIERS) == "[0.02, 0.04)"
    assert cal.tier(0.01, th.CALIBRATION_EDGE_TIERS) == "<0.02"
    assert cal.tier(0.5, th.CALIBRATION_EDGE_TIERS) == ">=0.08"
    assert cal.tier(None, th.CALIBRATION_EDGE_TIERS) == "(unknown)"
    assert cal.price_band(0.97) == "[0.9, 1]" and cal.price_band(0.1) == "[0.1, 0.2)"
    fs, rs = world(2)
    fills = [fill(fs[0], edge=0.05), fill(fs[1], side="sell", edge=0.1)]
    report, samples = build(fs, rs, fills)
    t = report["trades"]
    assert set(t["by_side"]) == {"buy", "sell"} and set(t["by_edge_tier"]) == {"[0.04, 0.08)", ">=0.08"}
    assert set(t["by_expiry_tier"]) == {">=336h"}  # 2026-10-01T06:00Z -> 2026-10-20
    assert all(g["status"] == "insufficient" and g["brier"] is None for g in t["by_side"].values())
    assert report["group_count"] >= 6
    m = t["metrics"]
    assert m["log_loss_model"] is not None and m["mean_r_minus_e"] is not None and "brier_skill_ci" not in m


def test_report_carries_the_fixed_sentences():
    report, _ = build(*world(2))
    text = cal.render_markdown(report)
    assert "UMA dispute status not checked" in text and "Not a promise of profit" in text


# ------------------------------------------------------------------ Task DV (#321 Codex follow-up)


def test_an_out_of_bounds_fill_dropped_by_another_filter_still_rejects_its_decision():
    # P1: the early fill is dropped for missing_market_mid first; its time must still reject the good fill.
    f = forecast(0, 0.7, ts="2026-10-01T06:00:00Z")
    good = fill(f, fill_id="good", ts="2026-10-01T08:00:00Z")
    early = fill(f, fill_id="early", ts="2026-10-01T05:00:00Z")
    early["calibration_problems"] = ["missing_market_mid"]
    t = build([f], [resolution(0, 0, day=1)], [good, early])[0]["trades"]
    assert t["eligible"] == 0
    assert t["dropped_by_reason"] == {"fill_calibration_problems": 1, "fill_before_forecast": 1}


def test_an_after_close_fill_dropped_first_still_rejects_its_decision():
    f = forecast(0, 0.7, ts="2026-10-01T00:00:00Z")
    late = fill(f, fill_id="late", ts="2026-10-01T12:00:00Z")
    late.pop("calibration_problems")  # dropped as fill_missing_identity before any time check
    t = build([f], [resolution(0, 0, day=1)], [fill(f, fill_id="ok"), late])[0]["trades"]
    assert t["eligible"] == 0 and "fill_not_before_close" in t["dropped_by_reason"]


@pytest.mark.parametrize("ts", ["0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00"])
def test_a_boundary_fill_time_is_dropped_and_counted_not_raised(ts):
    assert cal._parse_ts(ts) is None
    f = forecast(0, 0.7)
    t = build([f], [resolution(0, 0)], [fill(f, ts=ts)])[0]["trades"]
    assert t["eligible"] == 0 and t["dropped_by_reason"] == {"fill_ts_invalid": 1}


@pytest.mark.parametrize("ts", ["0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00"])
def test_a_boundary_forecast_time_is_dropped_not_raised(ts):
    f = forecast(0, 0.7, ts=ts)
    t = build([f], [resolution(0, 0)], [fill(f)])[0]["trades"]
    assert t["eligible"] == 0 and t["dropped_by_reason"] == {"fill_forecast_ts_invalid": 1}
