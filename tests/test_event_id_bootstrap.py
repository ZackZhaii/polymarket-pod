"""Task BG (M3): the calibration bootstrap intervals resample by the same cluster as event_cluster.

- cal.cluster_bootstrap resamples clusters, where a sample's cluster is its event_id when usable, else its
  condition_id (cal.cluster_name, Task BF). Markets of one event are drawn together; samples without a
  usable event id fall back per sample to their market. An event id and a condition id with the same text
  stay separate clusters.
- The report records the key it resampled by: ``bootstrap_cluster_key`` = ``event_id`` / ``condition_id`` /
  ``mixed`` (BF's cluster_key convention) and ``bootstrap_clusters`` (how many clusters were resampled), on
  forecasts.metrics, trades.metrics and every judged group's ``intervals``.
- Same fixed seed and resample count (CALIBRATION_BOOTSTRAP_SEED / _RESAMPLES); rows without event ids give
  exactly the intervals they gave before BG. Report only: no new number, no threshold, size or gate moved.
Offline, synthetic rows only.
"""
from __future__ import annotations

import random
from collections import Counter

import pytest

from src import calibration as cal
from src import thresholds as th
from tests.test_calibration import build, fill, forecast, resolution, world


def with_event(row: dict, event_id) -> dict:
    return {**row, "event_id": event_id, "event_slug": None if event_id is None else f"slug-{event_id}"}


def sample(cid: str, event=None, p: float = 0.6, y: int = 1) -> dict:
    return {"condition_id": cid, "event_id": event, "p": p, "y": y}


def draws(samples):
    """Run cluster_bootstrap with a statistic that records every resample's condition_id counts."""
    seen: list[Counter] = []

    def stat(drawn):
        seen.append(Counter(s["condition_id"] for s in drawn))
        return float(len(drawn))

    ci = cal.cluster_bootstrap(samples, stat)
    return ci, seen


# ------------------------------------------------------------------ which clusters are resampled

def test_two_markets_of_one_event_resample_together():
    samples = [sample("0xa1", "E1"), sample("0xa2", "E1"), sample("0xb"), sample("0xc")]
    _, seen = draws(samples)
    assert len(seen) == th.CALIBRATION_BOOTSTRAP_RESAMPLES
    assert all(c["0xa1"] == c["0xa2"] for c in seen)  # never drawn apart
    assert any(c["0xa1"] >= 1 for c in seen) and any(c["0xa1"] == 0 for c in seen)
    # three clusters (E1, 0xb, 0xc) are drawn per resample, so a draw holds 3 + (count of E1) samples
    assert all(sum(c.values()) == 3 + c["0xa1"] for c in seen)


def test_without_event_ids_each_market_is_its_own_cluster():
    samples = [sample(f"0xc{i}") for i in range(6)]
    _, seen = draws(samples)
    assert all(sum(c.values()) == 6 for c in seen)
    assert any(c["0xc0"] != c["0xc1"] for c in seen)  # markets drawn independently
    assert cal.bootstrap_clusters(samples) == ("condition_id", 6)


@pytest.mark.parametrize("bad", [None, "", "  ", True, 1.5, [1], {"id": "E1"}])
def test_an_unusable_event_id_falls_back_to_the_market(bad):
    samples = [sample("0xa1", bad), sample("0xa2", bad), sample("0xb", "E2")]
    _, seen = draws(samples)
    assert any(c["0xa1"] != c["0xa2"] for c in seen)  # not merged into one cluster
    assert cal.bootstrap_clusters(samples) == ("mixed", 3)


def test_mixed_event_and_market_clusters():
    samples = [sample("0xa1", "E1"), sample("0xa2", "E1"), sample("0xa3", "E1"), sample("0xb"), sample("0xc", 7)]
    assert cal.bootstrap_clusters(samples) == ("mixed", 3)  # E1, 0xb, 7
    _, seen = draws(samples)
    assert all(c["0xa1"] == c["0xa2"] == c["0xa3"] for c in seen)
    assert cal.bootstrap_clusters([sample("0xa1", "E1"), sample("0xb", "E2")]) == ("event_id", 2)
    assert cal.bootstrap_clusters([]) == ("condition_id", 0)


def test_an_event_id_never_merges_with_a_condition_id_of_the_same_text():
    samples = [sample("0xa", "0xb"), sample("0xb")]
    assert cal.bootstrap_clusters(samples) == ("mixed", 2)
    _, seen = draws(samples)
    assert any(c["0xa"] != c["0xb"] for c in seen)


def test_a_single_event_has_no_interval():
    samples = [sample(f"0xc{i}", "E1", y=i % 2) for i in range(10)]
    assert cal.cluster_bootstrap(samples, lambda d: cal.brier(cal._pairs(d))) is None
    plain = [dict(s, event_id=None) for s in samples]
    assert cal.cluster_bootstrap(plain, lambda d: cal.brier(cal._pairs(d))) is not None


# ------------------------------------------------------------------ determinism and old rows

def test_same_seed_and_resample_count():
    assert cal.thresholds_used()["bootstrap_resamples"] == th.CALIBRATION_BOOTSTRAP_RESAMPLES
    assert cal.thresholds_used()["bootstrap_seed"] == th.CALIBRATION_BOOTSTRAP_SEED


def test_event_clustered_bootstrap_is_deterministic():
    fs, rs = world(80)
    fs = [with_event(f, f"E{i // 3}") for i, f in enumerate(fs)]
    first, second = build(fs, rs)[0], build(fs, rs)[0]
    assert first == second
    assert first["forecasts"]["metrics"]["brier_ci"] is not None
    # input order does not change the interval (clusters are sorted, the statistic is order-free)
    rng = random.Random(3)
    samples = [sample(f"0xc{i}", f"E{i // 3}", p=rng.uniform(0.1, 0.9), y=rng.random() < 0.5) for i in range(40)]
    stat = lambda d: cal.brier(cal._pairs(d))  # noqa: E731
    shuffled = samples[:]
    rng.shuffle(shuffled)
    assert cal.cluster_bootstrap(samples, stat) == cal.cluster_bootstrap(shuffled, stat)


def test_rows_without_event_ids_keep_their_intervals():
    """Pre-BF rows (or event ids that are one per market) resample exactly as the market bootstrap did."""
    fs, rs = world(80)
    plain = build(fs, rs)[0]["forecasts"]["metrics"]
    legacy = cal.cluster_bootstrap(_forecast_samples(fs, rs),
                                   lambda d: cal.brier(cal._pairs(d)), key="condition_id")
    assert plain["brier_ci"] == legacy
    one_each = build([with_event(f, f"E{i}") for i, f in enumerate(fs)], rs)[0]["forecasts"]["metrics"]
    assert one_each["bootstrap_cluster_key"] == "event_id" and plain["bootstrap_cluster_key"] == "condition_id"
    assert one_each["brier_ci"] == plain["brier_ci"] and one_each["calibration"] == plain["calibration"]


def _forecast_samples(fs, rs):
    return [s for s in build(fs, rs)[1] if s["eligible"] and "q" not in s]


def test_one_event_changes_the_intervals():
    fs, rs = world(80)
    plain = build(fs, rs)[0]["forecasts"]["metrics"]
    grouped = build([with_event(f, f"E{i // 8}") for i, f in enumerate(fs)], rs)[0]["forecasts"]["metrics"]
    assert grouped["bootstrap_clusters"] == 10 and plain["bootstrap_clusters"] == 80
    assert grouped["brier"] == plain["brier"]  # point values unchanged
    assert grouped["brier_ci"] != plain["brier_ci"]
    single = build([with_event(f, "E1") for f in fs], rs)[0]["forecasts"]["metrics"]
    assert single["brier_ci"] is None and single["calibration"]["slope_ci"] is None
    assert single["verdict"]["calibration_slope"] == "slope_not_estimable"


# ------------------------------------------------------------------ the report says which key it used

def test_the_forecast_section_records_the_bootstrap_key():
    fs, rs = world(80)
    fs = [with_event(f, f"E{i // 2}") if i % 4 else f for i, f in enumerate(fs)]
    report = build(fs, rs)[0]
    m = report["forecasts"]["metrics"]
    assert m["bootstrap_cluster_key"] == "mixed" == m["event_cluster"]["cluster_key"]
    for name, group in report["forecasts"]["by_category"].items():
        if group["intervals"] is not None:
            assert group["intervals"]["bootstrap_cluster_key"] in {"event_id", "condition_id", "mixed"}
            assert group["intervals"]["bootstrap_clusters"] >= 2
    markdown = cal.render_markdown(report)
    assert "bootstrap clustered by mixed" in markdown
    assert any("bootstrap" in note and "event_id" in note for note in report["notes"])


def test_an_insufficient_section_still_says_its_key():
    fs, rs = world(5)
    m = build([with_event(f, "E1") for f in fs], rs)[0]["forecasts"]["metrics"]
    assert m["status"] == "insufficient" and "brier_ci" not in m
    assert m["bootstrap_cluster_key"] == "event_id" and m["bootstrap_clusters"] == 1


def test_trade_intervals_resample_by_event():
    rng = random.Random(11)
    fs, rs, fills = [], [], []
    for i in range(80):
        p = round(rng.uniform(0.1, 0.9), 3)
        f = with_event(forecast(i, p, category="Politics" if i % 2 else "Sports"), f"E{i // 4}")
        fs.append(f)
        rs.append(resolution(i, 0 if rng.random() < p else 1, day=(i % 7) + 1))
        fills.append(fill(f, price=round(min(0.95, max(0.05, p - 0.04)), 3), edge=(0.03, 0.05)[i % 2]))
    report = build(fs, rs, fills)[0]
    t = report["trades"]["metrics"]
    assert t["status"] == "sufficient"
    assert t["bootstrap_cluster_key"] == "event_id" and t["bootstrap_clusters"] == 20  # fill event via forecast
    plain = build([with_event(f, None) for f in fs], rs, fills)[0]["trades"]["metrics"]
    assert plain["bootstrap_cluster_key"] == "condition_id" and plain["bootstrap_clusters"] == 80
    assert t["delta_brier"] == plain["delta_brier"] and t["delta_brier_ci"] != plain["delta_brier_ci"]
    for group in report["trades"]["by_edge_tier"].values():
        if group["intervals"] is not None:
            assert group["intervals"]["bootstrap_cluster_key"] == "event_id"
