"""Task BB (M3 step 4): structured Reflector proposals from a calibration report. Synthetic reports only."""
from __future__ import annotations

import ast
import copy
import json
import random
from functools import lru_cache
from pathlib import Path

import pytest

from src import calibration as cal
from src import reflector
from src import reflector_proposals as rp
from src import thresholds as th
from src.podlib import RISK_PATH, read_jsonl
from tests.test_calibration import fill, forecast, resolution

ROOT = Path(__file__).resolve().parents[1]
NOW = "2026-11-20T09:00:00Z"
REF = {"path": "reports/calibration/2026-11-20/report.json", "sha256": "ab" * 32, "generated_at": NOW}


@lru_cache(maxsize=None)
def _base_json() -> str:
    """A real src.calibration report with both sections sufficient (40 markets, 7 days, one strategy)."""
    rng = random.Random(5)
    fs, rs, fills = [], [], []
    for i in range(40):
        p = round(rng.uniform(0.2, 0.8), 3)
        f = forecast(i, p)
        fs.append(f)
        rs.append(resolution(i, 0 if rng.random() < p else 1, day=(i % 7) + 1))
        fills.append(fill(f, price=0.5))
    report, _ = cal.build_report(fs, rs, fills, generated_at=NOW)
    assert report["forecasts"]["metrics"]["status"] == report["trades"]["metrics"]["status"] == "sufficient"
    return json.dumps(report)


def base() -> dict:
    report = json.loads(_base_json())
    # Neutral values: every interval crosses its threshold, so no rule fires.
    report["forecasts"]["metrics"]["calibration"].update(slope=1.0, slope_ci=[0.8, 1.2])
    report["forecasts"]["metrics"]["reliability"]["ece"] = 0.01
    for item in report["forecasts"]["metrics"]["reliability"]["bins"]:
        item["mean_p_outside_wilson"] = False
    report["trades"]["metrics"].update(brier_skill_ci=[-0.1, 0.1], realized_usd_ci=[-5.0, 5.0], realization=1.0)
    return report


def overconfident(report: dict, slope: float = 0.7, ci=(0.5, 0.9)) -> dict:
    report["forecasts"]["metrics"]["calibration"].update(slope=slope, slope_ci=list(ci))
    return report


def run(report: dict, tmp_path: Path, now: str = NOW) -> dict:
    return rp.run(report, REF, now=now, jsonl_path=tmp_path / "proposals.jsonl", md_path=tmp_path / "proposals.md",
                  bus_path=tmp_path / "bus.jsonl")


def rows(tmp_path: Path) -> list[dict]:
    path = tmp_path / "proposals.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


# ------------------------------------------------------------------ rules


def test_neutral_report_gives_observations_only(tmp_path):
    result = run(base(), tmp_path)
    assert result["written"] == [] and result["observations"]
    assert not (tmp_path / "proposals.jsonl").exists() and not (tmp_path / "bus.jsonl").exists()


@pytest.mark.parametrize("section", ["forecasts", "trades"])
def test_an_insufficient_section_never_proposes(tmp_path, section):
    report = overconfident(base())
    report["trades"]["metrics"].update(brier_skill_ci=[-0.3, -0.1])
    metrics = report[section]["metrics"]
    metrics.update(status="insufficient", verdict="insufficient", short_of=["samples 3 < 30"])
    result = run(report, tmp_path)
    assert any("insufficient" in note for note in result["observations"])
    assert all(r["evidence"]["sample"]["section"] != section for r in rows(tmp_path))


def test_both_insufficient_writes_nothing(tmp_path):
    report = overconfident(base())
    for section in ("forecasts", "trades"):
        report[section]["metrics"].update(status="insufficient", verdict="insufficient")
    assert run(report, tmp_path)["written"] == []
    assert not (tmp_path / "proposals.md").exists()


def test_overconfidence_proposes_a_min_edge_tightening(tmp_path):
    result = run(overconfident(base()), tmp_path)
    assert result["written"] == ["2026-11-20-01"]
    (row,) = rows(tmp_path)
    assert tuple(row) == rp.PROPOSAL_FIELDS
    assert row["type"] == "param_change" and row["target"] == {
        "strategy": "news-forecaster", "key": "MIN_EDGE", "file": "src/thresholds.py"}
    assert row["current_value"] == th.MIN_EDGE == 0.02 and row["proposed_value"] == 0.03
    assert row["direction"] == "tighten" and row["loosens_risk_check"] is False
    assert row["approval"]["required"] is True and row["approval"]["required_from"] == "Operator"
    assert row["exchange_action"] == "none" and row["applied_by_code"] is False
    assert row["scope"] == {"env": "paper", "live_requires_human_approval": True}
    assert row["status"] == {"state": "proposed", "approver": None, "decided_at": None, "applied_at": None}
    ev = row["evidence"]
    assert ev["report"] == REF and ev["sections"] == ["forecasts.metrics.calibration.slope_ci"]
    assert ev["sample"]["n"] == 40 and ev["sample"]["n_markets"] == 40 and ev["data_versions"]["bootstrap_seed"]
    assert row["confidence"]["intervals"] == {"calibration_slope": [0.5, 0.9]}
    (message,) = read_jsonl(tmp_path / "bus.jsonl")
    assert message["from"] == "Reflector" and message["to"] == "Operator" and message["type"] == "proposal"
    assert message["payload"]["exchange_action"] == "none" and message["payload"]["proposal"] == row
    md = (tmp_path / "proposals.md").read_text(encoding="utf-8")
    assert "proposal 2026-11-20-01 param_change" in md and reflector._count_day(md, "2026-11-20") == 1


@pytest.mark.parametrize(("slope", "ci"), [(0.7, (0.5, 1.05)), (0.85, (0.7, 0.95)), (0.7, (0.5, 0.9))])
def test_the_whole_interval_must_be_past_the_threshold(tmp_path, slope, ci):
    written = run(overconfident(base(), slope, ci), tmp_path)["written"]
    assert written == ([] if (ci[1] >= 1 or slope >= 0.8) else ["2026-11-20-01"])


def test_disable_wins_over_tightening_for_the_same_strategy(tmp_path):
    report = overconfident(base(), 0.4, (0.2, 0.7))  # severe: disable rule
    report["trades"]["metrics"].update(brier_skill_ci=[-0.3, -0.05])
    run(report, tmp_path)
    (row,) = rows(tmp_path)
    assert row["type"] == "disable" and row["target"] == {"strategy": "news-forecaster", "key": "enabled",
                                                          "file": None}
    assert row["proposed_value"] == "disabled" and row["direction"] == "tighten"
    assert set(row["evidence"]["sections"]) == {"trades.metrics.brier_skill_ci",
                                                "forecasts.metrics.calibration.slope_ci"}
    assert not (ROOT / "config" / "strategies.toml").exists()


def test_realized_losses_disable_only_with_low_realization(tmp_path):
    report = base()
    report["trades"]["metrics"].update(realized_usd_ci=[-9.0, -1.0], realization=0.5)
    assert run(report, tmp_path)["written"] == []
    report["trades"]["metrics"]["realization"] = 0.1
    assert len(run(report, tmp_path)["written"]) == 1 and rows(tmp_path)[0]["type"] == "disable"


def test_several_strategies_without_their_own_interval_never_disable(tmp_path):
    report = base()
    report["trades"]["metrics"].update(brier_skill_ci=[-0.3, -0.05])
    report["trades"]["by_strategy"]["other"] = {"n": 1, "markets": 1, "status": "insufficient", "brier": None}
    result = run(report, tmp_path)
    assert result["written"] == [] and any("several strategies" in n for n in result["observations"])


def test_ece_with_a_flagged_bin_proposes_a_band_rule(tmp_path):
    report = base()
    report["forecasts"]["metrics"]["reliability"]["ece"] = 0.09
    bins = report["forecasts"]["metrics"]["reliability"]["bins"]
    bins[1]["mean_p_outside_wilson"] = True
    run(report, tmp_path)
    (row,) = rows(tmp_path)
    assert row["target"]["price_band"] == bins[1]["range"] and row["proposed_value"] == 0.03
    report["forecasts"]["metrics"]["reliability"]["ece"] = 0.04
    assert run(report, tmp_path / "x")["written"] == []


# ------------------------------------------------------------------ limits


def _three(report: dict) -> dict:
    """Three drafts: disable s1 (trades), slope and band tightening for news-forecaster (forecasts)."""
    overconfident(report)
    report["trades"]["by_strategy"] = {"s1": report["trades"]["by_strategy"]["news-forecaster"]}
    report["trades"]["metrics"].update(brier_skill_ci=[-0.3, -0.05])
    report["forecasts"]["metrics"]["reliability"]["ece"] = 0.09
    report["forecasts"]["metrics"]["reliability"]["bins"][0]["mean_p_outside_wilson"] = True
    return report


def test_daily_cap_keeps_disable_first_and_defers_the_rest(tmp_path):
    md = tmp_path / "proposals.md"
    reflector.propose("an earlier note", proposals_path=md, bus_path=tmp_path / "bus.jsonl", now=NOW)
    result = run(_three(base()), tmp_path)
    assert th.REFLECTOR_MAX_PER_UTC_DAY == 3
    assert result["written"] == ["2026-11-20-02", "2026-11-20-03"] and len(result["deferred"]) == 1
    assert [r["type"] for r in rows(tmp_path)] == ["disable", "param_change"]
    assert reflector._count_day(md.read_text(encoding="utf-8"), "2026-11-20") == 3
    nxt = run(_three(base()), tmp_path, now="2026-11-21T00:00:01Z")
    assert len(nxt["written"]) == 1 and [s["reason"] for s in nxt["skipped"]] == ["already_open"] * 2


def test_an_open_target_is_not_proposed_twice(tmp_path):
    run(overconfident(base()), tmp_path)
    again = run(overconfident(base()), tmp_path, now="2026-11-25T00:00:00Z")
    assert again["written"] == [] and again["skipped"][0]["reason"] == "already_open"


def _reject(tmp_path: Path, decided_at: str) -> None:
    path = tmp_path / "proposals.jsonl"
    data = rows(tmp_path)
    data[0]["status"].update(state="rejected", approver="KEY", decided_at=decided_at)
    path.write_text("".join(json.dumps(r) + "\n" for r in data), encoding="utf-8")


def test_a_rejected_target_waits_out_the_cooldown(tmp_path):
    run(overconfident(base()), tmp_path)
    _reject(tmp_path, "2026-11-20T12:00:00Z")
    soon = run(overconfident(base()), tmp_path, now="2026-11-26T00:00:00Z")
    assert soon["skipped"][0]["reason"] == "rejected_recently"
    later = run(overconfident(base()), tmp_path, now="2026-11-28T00:00:00Z")
    assert later["written"] == ["2026-11-28-01"]


def test_a_rejected_target_returns_early_with_enough_new_samples(tmp_path):
    run(overconfident(base()), tmp_path)
    _reject(tmp_path, "2026-11-20T12:00:00Z")
    report = overconfident(base())
    report["forecasts"]["metrics"]["n"] = 40 + th.REFLECTOR_REJECT_NEW_SAMPLES
    assert run(report, tmp_path, now="2026-11-22T00:00:00Z")["written"] == ["2026-11-22-01"]


def test_loosening_is_marked_for_key_and_unmarked_loosening_is_refused():
    target = {"strategy": "all", "key": "MIN_EDGE", "file": "src/thresholds.py"}
    assert rp.direction(target, 0.02, 0.03) == "tighten"
    assert rp.direction(target, 0.02, 0.01) == "loosen"
    assert rp.direction({"key": "max_order_pct"}, 0.01, 0.005) == "loosen"  # unknown key: fail closed
    assert rp.direction({"key": "enabled"}, "disabled", "enabled") == "loosen"
    draft = copy.deepcopy(rp.candidates(overconfident(base()), REF)[0][0])
    draft["proposed_value"] = 0.01
    row = rp.finish(draft, "2026-11-20-01", NOW)
    assert row["loosens_risk_check"] is True and row["approval"]["required_from"] == "KEY"
    assert rp.validate(row) == []
    row["approval"]["required_from"] = "Operator"
    assert "a loosening proposal must require KEY's approval" in rp.validate(row)
    row = rp.finish(draft, "x", NOW)
    row["direction"], row["loosens_risk_check"] = "tighten", False
    assert "direction / loosens_risk_check do not match the values" in rp.validate(row)


@pytest.mark.parametrize("mutate", [
    lambda r: r.update(exchange_action="place_order"),
    lambda r: r.update(applied_by_code=True),
    lambda r: r.update(scope={"env": "live", "live_requires_human_approval": False}),
    lambda r: r["target"].update(file="config/risk.toml"),
    lambda r: r["target"].update(file="config/strategies.toml"),
    lambda r: r.update(type="apply"),
])
def test_validate_refuses_anything_beyond_a_paper_proposal(mutate):
    row = rp.finish(rp.candidates(overconfident(base()), REF)[0][0], "2026-11-20-01", NOW)
    assert rp.validate(row) == []
    mutate(row)
    assert rp.validate(row)


def test_no_write_to_config_risk_or_thresholds(tmp_path):
    with pytest.raises(RuntimeError):
        reflector.propose("x", proposals_path=ROOT / "src" / "thresholds.py", bus_path=tmp_path / "b.jsonl")
    for bad in (ROOT / "config" / "notes.md", ROOT / "config" / "strategies.toml", tmp_path / "x.toml", RISK_PATH):
        assert reflector.refused_path(bad)
        with pytest.raises(ValueError):
            rp.run(base(), REF, now=NOW, jsonl_path=bad, md_path=tmp_path / "m.md", bus_path=tmp_path / "b.jsonl")
    assert not reflector.refused_path(tmp_path / "proposals.jsonl")


def test_nothing_in_the_repo_applies_a_proposal():
    for path in sorted((ROOT / "src").glob("*.py")) + sorted((ROOT / "scripts").glob("*.py")):
        if path.name == "reflector_proposals.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        mods = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        mods |= {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        mods |= {f"{n.module}.{a.name}" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
        assert not any("reflector_proposals" in m for m in mods), path.name
        assert "proposals.jsonl" not in path.read_text(encoding="utf-8"), path.name
    text = (ROOT / "src" / "reflector_proposals.py").read_text(encoding="utf-8")
    tree = ast.parse(text)
    mods = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    mods |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not mods & {"urllib", "urllib.request", "socket", "ssl", "http.client", "src.risk_gate",
                       "src.paper_ledger", "src.scanner"}
    assert "write_text" not in text and "RISK_PATH.write" not in text


# ------------------------------------------------------------------ CLI and fail-closed


def _cli(tmp_path: Path, report_path: Path | None, *extra: str) -> int:
    args = ["--jsonl", str(tmp_path / "p.jsonl"), "--md", str(tmp_path / "p.md"), "--bus", str(tmp_path / "b.jsonl"),
            "--now", NOW, *extra]
    if report_path is not None:
        args = ["--report", str(report_path), *args]
    return rp.main(args)


def _write(path: Path, report: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report if isinstance(report, str) else json.dumps(report), encoding="utf-8")
    return path


def _nothing_written(tmp_path: Path) -> bool:
    return not any((tmp_path / name).exists() for name in ("p.jsonl", "p.md", "b.jsonl"))


def test_cli_writes_proposals_and_leaves_risk_untouched(tmp_path, capsys):
    before = RISK_PATH.read_bytes(), (ROOT / "src" / "thresholds.py").read_bytes()
    assert _cli(tmp_path, _write(tmp_path / "r" / "report.json", overconfident(base()))) == rp.EXIT_OK == 0
    assert "written=1" in capsys.readouterr().out
    assert len(read_jsonl(tmp_path / "p.jsonl")) == 1 and len(read_jsonl(tmp_path / "b.jsonl")) == 1
    assert (RISK_PATH.read_bytes(), (ROOT / "src" / "thresholds.py").read_bytes()) == before
    assert not (ROOT / "config" / "strategies.toml").exists()
    row = read_jsonl(tmp_path / "p.jsonl")[0]
    assert row["evidence"]["report"]["sha256"] and row["evidence"]["report"]["generated_at"] == NOW


def test_cli_missing_report(tmp_path, monkeypatch):
    monkeypatch.setattr(rp, "REPORTS_DIR", tmp_path / "none")
    assert _cli(tmp_path, tmp_path / "absent.json") == rp.EXIT_REPORT_MISSING == 3
    assert _cli(tmp_path, None) == 3 and _nothing_written(tmp_path)


def test_latest_report_is_the_newest_dated_folder(tmp_path):
    _write(tmp_path / "2026-11-01" / "report.json", base())
    _write(tmp_path / "2026-11-03" / "report.json", base())
    (tmp_path / "2026-11-05").mkdir()  # no report.json: skipped
    assert rp.latest_report(tmp_path) == tmp_path / "2026-11-03" / "report.json"


def _corrupt_cases():
    def edit(fn):
        def make():
            report = overconfident(base())
            fn(report)
            return report
        return make
    return [
        lambda: "not json",
        lambda: '{"a": NaN}',
        lambda: "[1]",
        edit(lambda r: r.update(complete=False)),
        edit(lambda r: r.update(schema_version=2)),
        edit(lambda r: r.pop("trades")),
        edit(lambda r: r["forecasts"]["metrics"].update(verdict="insufficient")),
        edit(lambda r: r["forecasts"]["metrics"].update(status="maybe")),
        edit(lambda r: r["forecasts"]["metrics"]["calibration"].update(slope="0.7")),
        edit(lambda r: r["forecasts"]["metrics"]["calibration"].update(slope_ci=[0.9, 0.5])),
        edit(lambda r: r["trades"]["metrics"].update(n=-1)),
        edit(lambda r: r["forecasts"]["metrics"]["reliability"].update(bins="none")),
    ]


@pytest.mark.parametrize("make", _corrupt_cases())
def test_cli_corrupt_report_writes_nothing(tmp_path, make):
    assert _cli(tmp_path, _write(tmp_path / "r.json", make())) == rp.EXIT_REPORT_CORRUPT == 4
    assert _nothing_written(tmp_path)


def test_cli_corrupt_proposals_ledger_writes_nothing(tmp_path):
    (tmp_path / "p.jsonl").write_bytes(b'{"proposal_id": "x"')
    assert _cli(tmp_path, _write(tmp_path / "r.json", overconfident(base()))) == rp.EXIT_PROPOSALS_CORRUPT == 5
    assert not (tmp_path / "p.md").exists() and not (tmp_path / "b.jsonl").exists()


def test_cli_write_failure(tmp_path):
    (tmp_path / "p.md").mkdir()  # a directory where the markdown file should be
    assert _cli(tmp_path, _write(tmp_path / "r.json", overconfident(base()))) == rp.EXIT_WRITE_FAILED == 6


@pytest.mark.parametrize("extra", [["--bogus"], ["--now", "today"]])
def test_cli_usage(tmp_path, extra):
    assert _cli(tmp_path, tmp_path / "r.json", *extra) == rp.EXIT_USAGE == 2


def test_cli_refuses_config_paths(tmp_path):
    report = _write(tmp_path / "r.json", overconfident(base()))
    assert rp.main(["--report", str(report), "--jsonl", str(ROOT / "config" / "p.jsonl"), "--now", NOW]) == 2
    assert rp.main(["--report", str(report), "--md", str(tmp_path / "x.toml"), "--now", NOW]) == 2
    assert not (ROOT / "config" / "p.jsonl").exists()


def test_end_to_end_from_the_calibration_cli(tmp_path):
    """A real calibration run (insufficient: 12 markets) feeds the Reflector, which proposes nothing."""
    rng = random.Random(1)
    fs = [forecast(i, round(rng.uniform(0.1, 0.9), 3)) for i in range(12)]
    rs = [resolution(i, i % 2) for i in range(12)]
    (tmp_path / "f.jsonl").write_text("".join(json.dumps(r) + "\n" for r in fs), encoding="utf-8")
    (tmp_path / "r.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rs), encoding="utf-8")
    assert cal.main(["--forecasts", str(tmp_path / "f.jsonl"), "--resolutions", str(tmp_path / "r.jsonl"),
                     "--ledger", str(tmp_path / "l.jsonl"), "--out-dir", str(tmp_path / "out"), "--now", NOW]) == 0
    assert _cli(tmp_path, tmp_path / "out" / "2026-11-20" / "report.json") == 0
    assert _nothing_written(tmp_path)
