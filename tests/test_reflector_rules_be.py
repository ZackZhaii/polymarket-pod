"""Task BE: the Reflector rules the M3 design lists after Task BB. Synthetic reports only, offline.

Edge-tier realization (MIN_EDGE to the top of the failing bottom tiers), per-category delta Brier (disable one
category), calibration intercept (MIN_EDGE + step), and the event-cluster share on every proposal's evidence.
All tightening only, all proposals only (never applied).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src import reflector_proposals as rp
from src import thresholds as th
from src.podlib import RISK_PATH
from tests.test_reflector_proposals import NOW, REF, base, overconfident, rows, run

ROOT = Path(__file__).resolve().parents[1]
STRATEGY = "news-forecaster"


def _group(n: int = 20, markets: int = 20, **intervals) -> dict:
    """A judged trade group whose intervals all cross their thresholds unless overridden."""
    iv = {"brier": 0.2, "brier_ci": [0.1, 0.3], "ece": 0.02, "ece_ci": [0.0, 0.05], "slope": 1.0,
          "slope_ci": [0.6, 1.4], "intercept": 0.0, "intercept_ci": [-0.4, 0.4], "brier_market": 0.21,
          "delta_brier": -0.01, "delta_brier_ci": [-0.05, 0.03], "realization": 0.9, "realization_ci": [0.2, 1.6]}
    iv.update(intervals)
    return {"n": n, "markets": markets, "status": "sufficient", "brier": iv["brier"], "brier_market": 0.21,
            "realized_usd": 1.0, "intervals": iv}


def _tiers(report: dict, **groups) -> dict:
    report["trades"]["by_edge_tier"] = dict(groups)
    return report


def _losing(**over) -> dict:
    return _group(realization=-0.5, realization_ci=[-1.2, 0.1], **over)


# ------------------------------------------------------------------ edge-tier realization


def test_bottom_edge_tier_without_realization_raises_min_edge_to_its_top(tmp_path):
    report = _tiers(base(), **{"[0.02, 0.04)": _losing(), "[0.04, 0.08)": _group()})
    run(report, tmp_path)
    (row,) = rows(tmp_path)
    assert row["type"] == "param_change" and row["target"] == {
        "strategy": STRATEGY, "key": "MIN_EDGE", "file": "src/thresholds.py", "edge_tier": "[0.02, 0.04)"}
    assert row["current_value"] == th.MIN_EDGE == 0.02 and row["proposed_value"] == 0.04
    assert row["direction"] == "tighten" and row["loosens_risk_check"] is False
    assert row["approval"]["required_from"] == "Operator" and row["applied_by_code"] is False
    assert row["evidence"]["sections"] == ["trades.by_edge_tier.[0.02, 0.04).intervals.realization_ci"]
    assert row["evidence"]["sample"]["section"] == "trades"
    assert row["confidence"]["intervals"] == {"realization[0.02, 0.04)": [-1.2, 0.1]}
    assert rp.validate(row) == []


def test_contiguous_failing_tiers_from_the_bottom_raise_to_the_highest_top(tmp_path):
    report = _tiers(base(), **{"[0.02, 0.04)": _losing(), "[0.04, 0.08)": _losing(), ">=0.08": _group()})
    run(report, tmp_path)
    (row,) = rows(tmp_path)
    assert row["proposed_value"] == 0.08 and row["target"]["edge_tier"] == "[0.04, 0.08)"
    assert len(row["evidence"]["sections"]) == 2


def test_open_top_tier_failing_too_is_only_an_observation_beyond_the_last_bound(tmp_path):
    report = _tiers(base(), **{"[0.02, 0.04)": _losing(), "[0.04, 0.08)": _losing(), ">=0.08": _losing()})
    result = run(report, tmp_path)
    (row,) = rows(tmp_path)
    assert row["proposed_value"] == 0.08
    assert any(">=0.08" in note and "no upper bound" in note for note in result["observations"])


def test_a_failing_middle_tier_alone_is_only_an_observation(tmp_path):
    report = _tiers(base(), **{"[0.02, 0.04)": _group(), "[0.04, 0.08)": _losing()})
    result = run(report, tmp_path)
    assert result["written"] == []
    assert any("[0.04, 0.08)" in note and "not the bottom" in note for note in result["observations"])


@pytest.mark.parametrize("bottom", [
    {"n": 3, "markets": 3, "status": "insufficient", "brier": None, "brier_market": None, "realized_usd": None,
     "intervals": None},
    "absent",
])
def test_an_unjudged_bottom_tier_blocks_the_tier_rule(tmp_path, bottom):
    groups = {"[0.04, 0.08)": _losing()}
    if bottom != "absent":
        groups["[0.02, 0.04)"] = bottom
    assert run(_tiers(base(), **groups), tmp_path)["written"] == []


@pytest.mark.parametrize("ci", [[-1.2, th.REFLECTOR_EDGE_TIER_REALIZATION_MAX], [-1.2, 0.9], None])
def test_tier_interval_must_be_wholly_below_the_threshold(tmp_path, ci):
    report = _tiers(base(), **{"[0.02, 0.04)": _group(realization=-0.5, realization_ci=ci)})
    assert run(report, tmp_path)["written"] == []


def test_tiers_below_min_edge_are_ignored(tmp_path):
    report = _tiers(base(), **{"<0.02": _losing(), "[0.02, 0.04)": _group()})
    assert run(report, tmp_path)["written"] == []


def test_a_disabled_strategy_gets_no_tier_tightening(tmp_path):
    report = _tiers(base(), **{"[0.02, 0.04)": _losing()})
    report["trades"]["metrics"].update(brier_skill_ci=[-0.3, -0.05])
    run(report, tmp_path)
    assert [r["type"] for r in rows(tmp_path)] == ["disable"]


# ------------------------------------------------------------------ per-category delta Brier


def test_category_worse_than_market_proposes_disabling_that_category(tmp_path):
    report = base()
    report["trades"]["by_category"] = {"Politics": _group(delta_brier=0.04, delta_brier_ci=[0.01, 0.07]),
                                       "Sports": _group()}
    run(report, tmp_path)
    (row,) = rows(tmp_path)
    assert row["type"] == "disable" and row["target"] == {
        "strategy": STRATEGY, "key": "enabled", "file": None, "category": "Politics"}
    assert row["current_value"] == "enabled" and row["proposed_value"] == "disabled"
    assert row["direction"] == "tighten" and row["approval"]["required_from"] == "Operator"
    assert row["evidence"]["sections"] == ["trades.by_category.Politics.intervals.delta_brier_ci"]
    assert row["confidence"]["intervals"] == {"delta_brier[Politics]": [0.01, 0.07]}
    assert rp.validate(row) == []


@pytest.mark.parametrize("ci", [[0.0, 0.07], [-0.01, 0.07], None])
def test_category_rule_needs_the_whole_interval_above_zero(tmp_path, ci):
    report = base()
    report["trades"]["by_category"] = {"Politics": _group(delta_brier=0.04, delta_brier_ci=ci)}
    assert run(report, tmp_path)["written"] == []


def test_unknown_category_and_unjudged_groups_never_disable(tmp_path):
    report = base()
    report["trades"]["by_category"] = {
        "(unknown)": _group(delta_brier_ci=[0.01, 0.07]),
        "Sports": {"n": 3, "markets": 3, "status": "insufficient", "brier": None, "brier_market": None,
                   "realized_usd": None, "intervals": None}}
    assert run(report, tmp_path)["written"] == []


def test_a_strategy_disable_covers_its_categories(tmp_path):
    report = base()
    report["trades"]["metrics"].update(brier_skill_ci=[-0.3, -0.05])
    report["trades"]["by_category"] = {"Politics": _group(delta_brier_ci=[0.01, 0.07])}
    run(report, tmp_path)
    assert [r["target"] for r in rows(tmp_path)] == [{"strategy": STRATEGY, "key": "enabled", "file": None}]


def test_several_strategies_scope_category_rules_to_all(tmp_path):
    report = base()
    report["trades"]["by_strategy"]["other"] = {"n": 1, "markets": 1, "status": "insufficient", "brier": None}
    report["trades"]["by_category"] = {"Politics": _group(delta_brier_ci=[0.01, 0.07])}
    run(report, tmp_path)
    (row,) = rows(tmp_path)
    assert row["target"]["strategy"] == "all" and row["target"]["category"] == "Politics"


# ------------------------------------------------------------------ calibration intercept


@pytest.mark.parametrize(("ci", "word"), [([0.1, 0.9], "positive"), ([-0.9, -0.1], "negative")])
def test_intercept_bias_proposes_a_min_edge_tightening(tmp_path, ci, word):
    report = base()
    report["forecasts"]["metrics"]["calibration"].update(intercept=sum(ci) / 2, intercept_ci=ci)
    run(report, tmp_path)
    (row,) = rows(tmp_path)
    assert row["target"] == {"strategy": STRATEGY, "key": "MIN_EDGE", "file": "src/thresholds.py"}
    assert row["proposed_value"] == round(th.MIN_EDGE + th.REFLECTOR_MIN_EDGE_STEP, 10) == 0.03
    assert row["evidence"]["sections"] == ["forecasts.metrics.calibration.intercept_ci"]
    assert row["confidence"]["intervals"] == {"calibration_intercept": ci}
    assert word in row["rationale"]


@pytest.mark.parametrize("ci", [[-0.1, 0.9], [0.0, 0.5], None])
def test_intercept_interval_touching_zero_proposes_nothing(tmp_path, ci):
    report = base()
    report["forecasts"]["metrics"]["calibration"].update(intercept=0.3, intercept_ci=ci)
    assert run(report, tmp_path)["written"] == []


def test_slope_and_intercept_share_one_min_edge_proposal(tmp_path):
    report = overconfident(base())
    report["forecasts"]["metrics"]["calibration"].update(intercept=0.5, intercept_ci=[0.1, 0.9])
    result = run(report, tmp_path)
    (row,) = rows(tmp_path)
    assert result["skipped"] == []
    assert row["evidence"]["sections"] == ["forecasts.metrics.calibration.slope_ci",
                                           "forecasts.metrics.calibration.intercept_ci"]
    assert set(row["confidence"]["intervals"]) == {"calibration_slope", "calibration_intercept"}


def test_intercept_rule_skips_a_strategy_being_disabled(tmp_path):
    report = overconfident(base(), 0.4, (0.2, 0.7))
    report["forecasts"]["metrics"]["calibration"].update(intercept=0.5, intercept_ci=[0.1, 0.9])
    run(report, tmp_path)
    assert [r["type"] for r in rows(tmp_path)] == ["disable"]


# ------------------------------------------------------------------ event-cluster share


def test_every_proposal_carries_its_section_event_cluster(tmp_path):
    run(overconfident(base()), tmp_path)
    (row,) = rows(tmp_path)
    assert row["evidence"]["event_cluster"] == {"cluster_key": "condition_id", "flag": "ok",
                                                "largest_share": 0.025, "max_share": th.CALIBRATION_MAX_CLUSTER_SHARE}


def test_a_concentrated_section_is_flagged_on_its_proposals_and_noted(tmp_path):
    report = overconfident(base())
    report["forecasts"]["metrics"]["event_cluster"].update(flag="concentrated", largest_share=0.45,
                                                            largest_cluster="0xc1")
    result = run(report, tmp_path)
    (row,) = rows(tmp_path)
    assert row["evidence"]["event_cluster"]["flag"] == "concentrated"
    assert "concentrated" in row["risk"] and "0.45" in row["risk"]
    assert any(note.startswith("forecasts: event_cluster concentrated") for note in result["observations"])


def test_a_report_without_event_cluster_still_works(tmp_path):
    report = overconfident(base())
    del report["forecasts"]["metrics"]["event_cluster"]
    run(report, tmp_path)
    (row,) = rows(tmp_path)
    assert row["evidence"]["event_cluster"] is None


# ------------------------------------------------------------------ fail closed, compat, limits


def test_a_report_before_task_bd_gives_no_new_rule(tmp_path):
    report = base()
    for section in ("forecasts", "trades"):
        for key in list(report[section]):
            if key in ("by_category", "by_edge_tier"):
                for group in report[section][key].values():
                    group.pop("intervals", None)
        report[section]["metrics"].pop("event_cluster", None)
    report["forecasts"]["metrics"]["calibration"].pop("intercept_ci", None)
    del report["trades"]["by_edge_tier"]
    assert run(report, tmp_path)["written"] == []


@pytest.mark.parametrize("mutate", [
    lambda r: r["trades"]["by_edge_tier"].__setitem__("[0.02, 0.04)", _group(realization_ci=[0.5, 0.1])),
    lambda r: r["trades"]["by_edge_tier"].__setitem__("[0.02, 0.04)", _group(realization_ci="x")),
    lambda r: r["trades"]["by_edge_tier"]["[0.02, 0.04)"].__setitem__("intervals", [1, 2]),
    lambda r: r["trades"].__setitem__("by_category", []),
    lambda r: r["trades"]["by_category"].__setitem__("Politics", _group(delta_brier_ci=[0.1, float("nan")])),
    lambda r: r["forecasts"]["metrics"]["calibration"].__setitem__("intercept_ci", [0.2]),
    lambda r: r["forecasts"]["metrics"].__setitem__("event_cluster", "ok"),
    lambda r: r["forecasts"]["metrics"]["event_cluster"].__setitem__("flag", "fine"),
])
def test_cli_corrupt_new_fields_write_nothing(tmp_path, mutate):
    report = _tiers(base(), **{"[0.02, 0.04)": _group()})
    report["trades"]["by_category"] = {"Politics": _group()}
    mutate(report)
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report), encoding="utf-8")  # NaN is written as NaN: still refused
    jsonl = tmp_path / "proposals.jsonl"
    code = rp.main(["--report", str(path), "--jsonl", str(jsonl), "--md", str(tmp_path / "p.md"),
                    "--bus", str(tmp_path / "bus.jsonl"), "--now", NOW])
    assert code == rp.EXIT_REPORT_CORRUPT and not jsonl.exists()


def test_daily_cap_puts_every_disable_before_any_param_change(tmp_path):
    report = _tiers(overconfident(base()), **{"[0.02, 0.04)": _losing()})
    report["trades"]["by_category"] = {"A": _group(delta_brier_ci=[0.01, 0.07]),
                                       "B": _group(delta_brier_ci=[0.01, 0.07])}
    result = run(report, tmp_path)
    types = [r["type"] for r in rows(tmp_path)]
    assert types == ["disable", "disable", "param_change"] and len(result["deferred"]) == 1
    assert len(types) == th.REFLECTOR_MAX_PER_UTC_DAY


def test_new_rules_never_touch_risk_or_thresholds(tmp_path):
    risk = RISK_PATH.read_bytes()
    thresholds = (ROOT / "src" / "thresholds.py").read_bytes()
    report = _tiers(overconfident(base()), **{"[0.02, 0.04)": _losing()})
    report["trades"]["by_category"] = {"A": _group(delta_brier_ci=[0.01, 0.07])}
    report["forecasts"]["metrics"]["calibration"].update(intercept=0.5, intercept_ci=[0.1, 0.9])
    run(report, tmp_path)
    assert RISK_PATH.read_bytes() == risk and (ROOT / "src" / "thresholds.py").read_bytes() == thresholds
    assert all(r["direction"] == "tighten" and r["loosens_risk_check"] is False for r in rows(tmp_path))
    assert not (ROOT / "config" / "strategies.toml").exists()


def test_edge_tier_threshold_is_m3_pending_and_in_range():
    assert th.REFLECTOR_EDGE_TIER_REALIZATION_MAX == 0.25
    assert 0 <= th.REFLECTOR_EDGE_TIER_REALIZATION_MAX < 1
    doc = (ROOT / "docs" / "THRESHOLDS.md").read_text(encoding="utf-8")
    assert "`REFLECTOR_EDGE_TIER_REALIZATION_MAX` | `0.25`" in doc
