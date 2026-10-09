"""Task BD (M3): the calibration report carries intervals per edge tier and per category, a CI on the
calibration intercept, and the event-cluster share.

- by_category (forecast and trade samples) and by_edge_tier (trade sample) groups that meet the group
  minimums get an ``intervals`` object: Brier, ECE and calibration slope / intercept with market-clustered
  bootstrap intervals (fixed seed), and for trades delta Brier (model - market) and realization with theirs.
  A group below its minimums gets ``intervals: None`` (no interval is computed from too few samples).
- The overall forecast section adds ``calibration.intercept_ci``, ``ece_ci`` and an intercept verdict when it
  is sufficient; nothing when it is insufficient.
- Both sections report ``event_cluster``: the largest cluster's share of the samples, flagged
  ``insufficient`` below the section minimums, else ``concentrated`` above CALIBRATION_MAX_CLUSTER_SHARE
  (M3 pending), else ``ok``. No event id is recorded yet, so the cluster is the market (condition_id).
Offline, synthetic rows only.
"""
from __future__ import annotations

import random

import pytest

from src import calibration as cal
from src import thresholds as th
from tests.test_calibration import build, fill, forecast, resolution, world

INTERVAL_KEYS = {"brier", "brier_ci", "ece", "ece_ci", "slope", "slope_ci", "intercept", "intercept_ci"}
TRADE_INTERVAL_KEYS = INTERVAL_KEYS | {"brier_market", "delta_brier", "delta_brier_ci", "realization",
                                       "realization_ci"}


def _inside(value, ci):
    return ci is not None and ci[0] <= value <= ci[1]


def trade_world(n: int, seed: int = 11, *, edges=(0.03, 0.05)):
    """n markets with one buy fill each; edges cycle through ``edges``; outcomes drawn from p."""
    rng = random.Random(seed)
    fs, rs, fills = [], [], []
    for i in range(n):
        p = round(rng.uniform(0.1, 0.9), 3)
        f = forecast(i, p, category="Politics" if i % 2 else "Sports")
        fs.append(f)
        rs.append(resolution(i, 0 if rng.random() < p else 1, day=(i % 7) + 1))
        fills.append(fill(f, price=round(min(0.95, max(0.05, p - 0.04)), 3), edge=edges[i % len(edges)]))
    return fs, rs, fills


# ------------------------------------------------------------------ threshold

def test_the_cluster_share_limit_is_m3_pending_from_design_3():
    assert th.CALIBRATION_MAX_CLUSTER_SHARE == 0.30
    assert 0 < th.CALIBRATION_MAX_CLUSTER_SHARE <= 1
    assert cal.thresholds_used()["max_cluster_share"] == th.CALIBRATION_MAX_CLUSTER_SHARE


# ------------------------------------------------------------------ intercept CI (overall)

def test_a_sufficient_forecast_section_has_an_intercept_interval_and_verdict():
    m = build(*world(80))[0]["forecasts"]["metrics"]
    assert m["status"] == "sufficient"
    c = m["calibration"]
    assert _inside(c["intercept"], c["intercept_ci"])
    assert _inside(m["reliability"]["ece"], m["ece_ci"])
    assert m["verdict"]["calibration_intercept"] in {"no_clear_bias", "positive_bias", "negative_bias"}
    assert "calibration_slope" in m["verdict"]  # existing verdict kept


def test_a_biased_model_gets_a_negative_intercept_verdict():
    """p twice the odds of the truth: outcomes happen less often than predicted, so the intercept is below 0."""
    rng = random.Random(5)
    fs, rs = [], []
    for i in range(300):
        truth = rng.uniform(0.1, 0.5)
        p = min(0.97, truth + 0.3)
        fs.append(forecast(i, round(p, 4)))
        rs.append(resolution(i, 0 if rng.random() < truth else 1))
    m = build(fs, rs)[0]["forecasts"]["metrics"]
    assert m["calibration"]["intercept_ci"][1] < 0
    assert m["verdict"]["calibration_intercept"] == "negative_bias"


def test_an_insufficient_section_has_no_intercept_interval():
    m = build(*world(th.CALIBRATION_MIN_SAMPLES - 1))[0]["forecasts"]["metrics"]
    assert m["verdict"] == "insufficient"
    assert "intercept_ci" not in m["calibration"] and "ece_ci" not in m


def test_intercept_helper():
    pairs = [(0.2, 0), (0.8, 1), (0.3, 1), (0.7, 0), (0.5, 1), (0.4, 0)]
    fit = cal.logistic_fit(pairs)
    assert cal._intercept([{"p": p, "y": y} for p, y in pairs]) == pytest.approx(fit[0])
    assert cal._intercept([{"p": 0.5, "y": 1}] * 4) is None


# ------------------------------------------------------------------ group intervals

def test_sufficient_forecast_categories_get_intervals_and_small_ones_none():
    fs, rs = world(60)
    fs[0] = forecast(0, fs[0]["model_prob"], category="Rare")
    groups = build(fs, rs)[0]["forecasts"]["by_category"]
    assert groups["Rare"]["status"] == "insufficient" and groups["Rare"]["intervals"] is None
    big = groups["Politics"]
    assert big["status"] == "sufficient" and set(big["intervals"]) == INTERVAL_KEYS
    iv = big["intervals"]
    assert iv["brier"] == big["brier"] and _inside(iv["brier"], iv["brier_ci"])
    assert _inside(iv["ece"], iv["ece_ci"])
    assert iv["slope"] is None or iv["slope_ci"] is None or iv["slope_ci"][0] <= iv["slope_ci"][1]


def test_strategy_and_model_version_groups_stay_as_they_were():
    groups = build(*world(60))[0]["forecasts"]
    for key in ("by_strategy", "by_model_version"):
        for g in groups[key].values():
            assert "intervals" not in g


def test_trade_edge_tiers_and_categories_get_intervals():
    fs, rs, fills = trade_world(60)
    t = build(fs, rs, fills)[0]["trades"]
    assert set(t["by_edge_tier"]) == {"[0.02, 0.04)", "[0.04, 0.08)"}
    for groups in (t["by_edge_tier"], t["by_category"]):
        for name, g in groups.items():
            assert g["status"] == "sufficient", name
            iv = g["intervals"]
            assert set(iv) == TRADE_INTERVAL_KEYS
            assert iv["delta_brier"] == pytest.approx(iv["brier"] - iv["brier_market"])
            assert _inside(iv["delta_brier"], iv["delta_brier_ci"])
            assert iv["brier_market"] == g["brier_market"]
            if iv["realization"] is not None:
                assert iv["realization_ci"] is None or iv["realization_ci"][0] <= iv["realization_ci"][1]
    for g in t["by_side"].values():
        assert "intervals" not in g


def test_a_small_trade_tier_has_no_intervals():
    fs, rs, fills = trade_world(30)
    fills[0] = fill(fs[0], edge=0.5)
    t = build(fs, rs, fills)[0]["trades"]
    assert t["by_edge_tier"][">=0.08"]["status"] == "insufficient"
    assert t["by_edge_tier"][">=0.08"]["intervals"] is None


def test_group_intervals_are_deterministic():
    fs, rs, fills = trade_world(40)
    assert build(fs, rs, fills)[0] == build(fs, rs, fills)[0]


# ------------------------------------------------------------------ event-cluster share

def test_cluster_share_below_the_minimums_is_flagged_insufficient():
    m = build(*world(10))[0]["forecasts"]["metrics"]
    ec = m["event_cluster"]
    assert ec["flag"] == "insufficient" and ec["status"] == "insufficient"
    assert ec["cluster_key"] == "condition_id" and ec["n_clusters"] == 10
    assert ec["largest_share"] == pytest.approx(0.1) and ec["max_share"] == th.CALIBRATION_MAX_CLUSTER_SHARE


def test_cluster_share_ok_when_spread_over_markets():
    m = build(*world(60))[0]["forecasts"]["metrics"]
    ec = m["event_cluster"]
    assert ec["status"] == "sufficient" and ec["flag"] == "ok"
    assert ec["largest_share"] == pytest.approx(1 / 60) and ec["n"] == 60


def test_cluster_share_helper_flags_concentration_and_breaks_ties_by_name():
    samples = ([{"condition_id": "0xb"}] * 4 + [{"condition_id": "0xa"}] * 4
               + [{"condition_id": f"0x{i}"} for i in range(2)])
    out = cal.cluster_share(samples, status="sufficient")
    assert out["largest_cluster"] == "0xa" and out["largest_cluster_n"] == 4
    assert out["largest_share"] == pytest.approx(0.4) and out["flag"] == "concentrated"
    assert cal.cluster_share(samples, status="insufficient")["flag"] == "insufficient"
    empty = cal.cluster_share([], status="insufficient")
    assert empty["n"] == 0 and empty["largest_share"] is None and empty["flag"] == "insufficient"


def test_trade_section_reports_the_cluster_share_with_re_entries():
    fs, rs, fills = trade_world(40)
    # a second decision on market 0: a later forecast on the same market, filled once
    f2 = forecast(0, fs[0]["model_prob"], ts="2026-10-01T05:00:00Z")
    fs.append(f2)
    fills.append(fill(f2, ts="2026-10-03T00:00:00Z", fill_id="re-entry"))
    m = build(fs, rs, fills)[0]["trades"]["metrics"]
    ec = m["event_cluster"]
    assert ec["n"] == m["n"] and ec["n_clusters"] == m["markets"]
    assert ec["largest_cluster"] == "0xc0" and ec["largest_cluster_n"] == 2
    assert ec["flag"] == ("ok" if 2 / m["n"] <= th.CALIBRATION_MAX_CLUSTER_SHARE else "concentrated")


def test_markdown_shows_the_new_numbers():
    report, _ = build(*world(60))
    text = cal.render_markdown(report)
    assert "intercept" in text and "event cluster" in text and "ECE" in text
