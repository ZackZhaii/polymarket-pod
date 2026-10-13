"""Task DB (issue #266): calibration.py edge-input hardening. Offline, synthetic inputs only.

Reference: Poly预测 fixed-bin vectors (poly-forecast-calibration-fixedbin-vectors-20261013.json,
sha 2c73aaba...), groups F-06..F-10, F-14, W-16 and B-04; the values are inlined so the tests never
read that file.

- F-06..F-10: a p that is NaN, +-inf or outside [0, 1] is rejected by reliability() with an explicit
  ValueError, never a bare StopIteration out of the bin lookup.
- F-14: a y outside {0, 1} is rejected with a ValueError (a hit rate above 1 is never reported).
- W-16: a bin's mean_p is the correctly rounded mean of its p values, and mean_p_outside_wilson is
  decided exactly (score-test form of the Wilson interval), not by a float sum / n that rounds across
  the bound.
- B-04: a brier_skill computed against a near-zero market Brier (q = 1e-9 -> about -1e16) is reported
  unchanged but carries brier_skill_flag = "degenerate_market_reference" (never silently clipped).
"""
from __future__ import annotations

import math
from fractions import Fraction

import pytest

from src import calibration as cal
from src import thresholds as th
from tests.test_calibration import build, fill, forecast, resolution

FILLER = [(0.95, 1)] * 90  # the vectors' filler: 90 samples in the last fixed bin
W16_P = 0.10779126740630102  # the float Wilson lower bound of 3 hits in 10 (vector W-16)


def _fixed(pairs):
    pairs = list(pairs)
    assert len(pairs) >= th.CALIBRATION_FIXED_BINS_MIN_SAMPLES
    return pairs


# ------------------------------------------------------------------ F-06..F-10: p not a probability


@pytest.mark.parametrize("bad_p", [math.nan, math.inf, -math.inf, -0.01, 1.01])
@pytest.mark.parametrize("n", [100, 20])  # fixed bins and equal-frequency bins
def test_reliability_rejects_a_p_that_is_not_a_probability(bad_p, n):
    pairs = [(0.5, i % 2) for i in range(n - 1)] + [(bad_p, 0)]
    with pytest.raises(ValueError, match="p"):
        cal.reliability(pairs)


@pytest.mark.parametrize("bad_p", [True, None, "0.5"])
def test_reliability_rejects_a_p_that_is_not_a_number(bad_p):
    with pytest.raises(ValueError):
        cal.reliability([(0.5, 0)] * 99 + [(bad_p, 0)])


def test_reliability_never_leaks_stop_iteration():
    try:
        cal.reliability([(0.5, 0)] * 99 + [(math.nan, 0)])
    except StopIteration:  # pragma: no cover - the bug this guards
        pytest.fail("bare StopIteration from the bin lookup")
    except ValueError:
        pass


def test_reliability_still_accepts_the_closed_unit_interval():
    rel = cal.reliability(_fixed([(0.0, 0), (1.0, 1)] * 50))
    assert rel["kind"] == "fixed"
    assert rel["bins"][0]["n"] == 50 and rel["bins"][-1]["n"] == 50 and rel["ece"] == 0.0


# ------------------------------------------------------------------ F-14: y not in {0, 1}


@pytest.mark.parametrize("bad_y", [2, -1, 0.5, math.nan, None, "1"])
@pytest.mark.parametrize("n", [100, 20])
def test_reliability_rejects_an_outcome_that_is_not_0_or_1(bad_y, n):
    pairs = [(0.5, i % 2) for i in range(n - 1)] + [(0.5, bad_y)]
    with pytest.raises(ValueError, match="y"):
        cal.reliability(pairs)


def test_hit_rate_never_exceeds_one():
    """Vector F-14 at cf01ae1: y=2 gave hit_rate 0.51 from 50 real hits; now refused outright."""
    pairs = [(0.5, 1)] * 49 + [(0.5, 0)] * 50 + [(0.5, 2)]
    with pytest.raises(ValueError):
        cal.reliability(pairs)


# ------------------------------------------------------------------ W-16: Wilson boundary


def test_w16_mean_p_is_the_exact_mean_and_the_flag_is_exact():
    """10 samples at the float Wilson lower bound of 3/10: the exact bound is 4.2e-18 above the float
    one, so the exact mean (= W16_P) lies just outside. At cf01ae1 sum/10 rounded mean_p up one ulp to
    0.10779126740630103 and the flag said inside."""
    focus = [(W16_P, 1)] * 3 + [(W16_P, 0)] * 7
    rel = cal.reliability(_fixed(focus + FILLER))
    b = rel["bins"][1]
    assert b["range"] == [0.1, 0.2] and b["n"] == 10 and b["hit_rate"] == 0.3
    assert b["mean_p"] == W16_P  # not 0.10779126740630103
    assert b["wilson"] == [0.10779126740630102, 0.6032218525388546]
    assert b["mean_p_outside_wilson"] is True
    assert rel["bins"][-1]["mean_p_outside_wilson"] is True  # 0.95 vs 90/90 hits (vector W-16)
    assert rel["ece"] == pytest.approx(0.06422087325936994, abs=1e-15)


def test_w16_flag_flips_exactly_where_the_score_test_does():
    """The flag is the exact Wilson rule for the z wilson() uses: n (hits/n - m)^2 > z^2 m (1 - m). With that z
    the exact lower bound of 3/10 lies 2-3 ulps above the printed float bound, so the flag flips there."""
    for p, outside in ((0.10779126740630102, True), (0.10779126740630104, True),
                       (0.10779126740630106, False), (0.10779126740630107, False)):
        rel = cal.reliability(_fixed([(p, 1)] * 3 + [(p, 0)] * 7 + FILLER))
        assert rel["bins"][1]["mean_p"] == p and rel["bins"][1]["mean_p_outside_wilson"] is outside, p


def test_outside_wilson_matches_the_interval_away_from_the_bounds():
    for hits, p, outside in ((3, 0.3, False), (3, 0.05, True), (3, 0.7, True), (0, 0.11, False),
                             (10, 0.95, False)):
        rel = cal.reliability(_fixed([(p, 1)] * hits + [(p, 0)] * (10 - hits) + [(0.55, 1)] * 90))
        b = next(b for b in rel["bins"] if b["n"] == 10)
        low, high = b["wilson"]
        assert b["mean_p_outside_wilson"] is outside, (hits, p)
        assert (not low <= b["mean_p"] <= high) is outside, (hits, p)


def test_mean_p_is_correctly_rounded():
    values = [0.123, 0.123, 0.122, 0.146, 0.129, 0.102, 0.184, 0.156, 0.164, 0.119]
    assert sum(values) / len(values) == 0.13679999999999998  # float sum / n rounds the wrong way here
    rel = cal.reliability(_fixed([(v, 0) for v in values] + FILLER))
    assert rel["bins"][1]["mean_p"] == 0.1368 == float(sum(map(Fraction, values), Fraction(0)) / len(values))


# ------------------------------------------------------------------ B-04: BSS against a degenerate market


def _trade(i, p, q, y):
    return {"kind": "trade", "condition_id": f"0xc{i}", "event_id": None, "p": p, "q": q, "y": y,
            "expected_usd": 0.1, "realized_usd": 0.0, "re_entry": False, "resolution_day": "2026-10-01"}


def test_brier_skill_against_a_near_zero_market_brier_is_flagged_not_clipped():
    samples = [_trade(i, 0.1, 1e-9, 0) for i in range(10)]
    m = cal.trade_section(samples)
    raw = 1.0 - cal.brier([(0.1, 0)] * 10) / cal.brier([(1e-9, 0)] * 10)
    assert m["brier_skill"] == raw and m["brier_skill"] < -1e15  # number unchanged (vector B-04)
    assert m["brier_skill_flag"] == "degenerate_market_reference"


@pytest.mark.parametrize("p,q,y,skill", [(0.1, 0.2, 0, 0.75), (0.9, 0.8, 1, 0.75)])  # vectors B-01 / B-02
def test_brier_skill_with_a_normal_market_is_ok(p, q, y, skill):
    m = cal.trade_section([_trade(i, p, q, y) for i in range(10)])
    assert m["brier_skill"] == pytest.approx(skill) and m["brier_skill_flag"] == "ok"


def test_brier_skill_flag_is_none_without_a_skill():
    assert cal.trade_section([])["brier_skill_flag"] is None  # vector B-05
    exact_market = [_trade(i, 0.3, float(i % 2), i % 2) for i in range(10)]  # vector B-03: BS_q = 0
    m = cal.trade_section(exact_market)
    assert m["brier_skill"] is None and m["brier_skill_flag"] is None


def test_report_markdown_carries_the_brier_skill_flag():
    fs = [forecast(i, 0.1) for i in range(3)]
    rs = [resolution(i, 1) for i in range(3)]  # token t{i}-0 loses: y = 0
    fills = [fill(f, market_mid=1e-9) for f in fs]
    report, _ = build(fs, rs, fills)
    m = report["trades"]["metrics"]
    assert report["trades"]["eligible"] == 3 and m["brier_skill"] < -1e15
    assert m["brier_skill_flag"] == "degenerate_market_reference"
    assert "degenerate_market_reference" in cal.render_markdown(report)
