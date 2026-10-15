"""Task EB (issue #223): m1_run persists the ws_recorder's internal counts per asset in manifest ``counts.recorder``.

#185 (Task CC) and #201 (Task CM) added counts that explain why no gap resync fired (``duplicate_level_removal``,
``group_held`` / ``group_resolved`` / ``group_unresolved`` / ``group_replaced_by_book``,
``stale_resubscribe_snapshot``). They lived only in the recorder's memory, so a run dir could not tell "no burst"
from "the burst was absorbed by the fix". Observability only: no judgement, threshold or M1 criterion changes.

- ws_recorder counts each of them per asset too (``Outcome.asset_counts`` / ``RunReport.asset_counts``); the
  global ``counts`` are unchanged.
- m1_run writes ``counts.recorder.<asset>.<name>`` (every selected asset, every name, 0 when never counted) in
  the final manifest, in the console-close partial manifest and in every ``progress`` snapshot (same atomic
  ManifestWriter path as ``counts.resyncs``).
- m1_report only displays them (no criterion); a manifest without them (an older run) shows N/A, no error.

Uses the real frames in tests/fixtures/ws_resync/. Offline: fake transport, fake clock, no network, no order.
"""
from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path

import pytest

from src import m1_report as rep
from src import m1_run as r
from src import ws_recorder as w
from src.thresholds import WS_GROUP_FLUSH_S
from tests.test_m1_report import good_streams, manifest as report_manifest, write_run
from tests.test_m1_run import _main, _manifest, _market, _markets_file
from tests.test_m1_run_console import Console

FIX = Path(__file__).parent / "fixtures" / "ws_resync"
YES = "107540403103917101723616128435236138591116364556711257437238922612332295492509"
NO = "18368161459732947229970694377522451372979676356971809107906485726590890158916"
TS = "2030-01-01T00:00:00Z"
NAMES = ("duplicate_level_removal", "group_held", "group_resolved", "group_unresolved",
         "group_replaced_by_book", "stale_resubscribe_snapshot")


def _frames(name):
    return [json.loads(line) for line in (FIX / name).read_text(encoding="utf-8").splitlines() if line.strip()]


def _replay(name):
    core, out = w.BookCore([YES, NO]), w.Outcome()
    frames = _frames(name)
    for frame in frames + [{"t": frames[-1]["t"] + WS_GROUP_FLUSH_S, "raw": None}]:
        for part in (core.flush_groups(TS, frame["t"]),
                     *([core.handle_text(frame["raw"], TS, frame["t"])] if frame["raw"] is not None else [])):
            out.counts.update(part.counts)
            for asset, counter in part.asset_counts.items():
                out.asset_counts.setdefault(asset, Counter()).update(counter)
    return out


# --- ws_recorder: per-asset counts add up to the global ones -----------------------------------------


def test_the_counted_names_are_the_six_internal_counts():
    assert w.RECORDER_ASSET_COUNTS == NAMES


@pytest.mark.parametrize("name", ["dup_removal_window.jsonl", "live_top_mismatch_window.jsonl",
                                  "minimal_vector.jsonl", "real_crossed_vector.jsonl"])
def test_per_asset_counts_sum_to_the_global_counts(name):
    out = _replay(name)
    assert set(out.asset_counts) <= {YES, NO}
    for key in NAMES:
        assert sum(counter[key] for counter in out.asset_counts.values()) == out.counts[key], key
    assert sum(sum(counter.values()) for counter in out.asset_counts.values()) == sum(out.counts[k] for k in NAMES)


def test_duplicate_removals_are_counted_on_the_asset_they_happened_to():
    out = _replay("dup_removal_window.jsonl")
    assert out.counts["duplicate_level_removal"] == 4
    assert sum(out.asset_counts[a]["duplicate_level_removal"] for a in out.asset_counts) == 4


def test_held_groups_are_counted_per_asset():
    out = _replay("minimal_vector.jsonl")
    assert out.counts["group_held"] == 2 and out.asset_counts[YES]["group_held"] + out.asset_counts[NO]["group_held"] == 2


def test_recorder_asset_counts_lists_every_asset_and_every_name_with_zeros():
    report = w.RunReport()
    report.asset_counts["a"] = Counter(group_held=3)
    assert w.recorder_asset_counts(report, ["a", "b"]) == {
        "a": {**{key: 0 for key in NAMES}, "group_held": 3}, "b": {key: 0 for key in NAMES}}


# --- m1_run: the manifest carries what the recorder counted ------------------------------------------


def _fixture_run(tmp_path, monkeypatch, name="dup_removal_window.jsonl"):
    seen = {}
    real = r.Recorder

    class Spy(real):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            seen["recorder"] = self

    monkeypatch.setattr(r, "Recorder", Spy)
    script = []
    for frame in _frames(name):
        try:
            script.append(json.loads(frame["raw"]))
        except ValueError:
            continue
    markets = _markets_file(tmp_path, [_market("m1", [YES, NO])])
    code, out, *_ = _main(tmp_path, [script], ["--assets", YES, NO, "--rest-assets", "0"], markets=markets)
    return code, out, seen["recorder"]


def test_final_manifest_counts_equal_the_in_memory_recorder_counts(tmp_path, monkeypatch):
    code, out, recorder = _fixture_run(tmp_path, monkeypatch)
    assert code == r.EXIT_OK
    stored = _manifest(out)["counts"]["recorder"]
    assert set(stored) == {YES, NO}
    for asset in (YES, NO):
        assert set(stored[asset]) == set(NAMES)
        for key in NAMES:
            assert stored[asset][key] == recorder.report.asset_counts.get(asset, Counter())[key], (asset, key)
    for key in NAMES:
        assert sum(stored[asset][key] for asset in stored) == recorder.report.counts[key], key
    assert sum(stored[asset]["duplicate_level_removal"] for asset in stored) >= 1  # the fixture really exercises it


def test_the_existing_counts_are_unchanged_next_to_the_new_ones(tmp_path, monkeypatch):
    code, out, recorder = _fixture_run(tmp_path, monkeypatch, "live_top_mismatch_window.jsonl")
    counts = _manifest(out)["counts"]
    assert counts["snapshots"] == recorder.report.counts["snapshot_applied"]
    assert counts["resyncs"] == {key.split(":", 1)[1]: value for key, value in recorder.report.counts.items()
                                 if key.startswith("resync:")}
    assert sum(counts["recorder"][asset]["group_held"] for asset in counts["recorder"]) == \
        recorder.report.counts["group_held"]


def test_progress_snapshots_carry_the_recorder_counts(tmp_path, monkeypatch):
    """While the run is blocked after the vector's frames, the progress thread's next snapshot (written through
    ManifestWriter.progress, the same atomic path) carries exactly the in-memory per-asset counts."""
    seen, spied, memory = [], {}, {}
    real_progress, real_recorder = r.ManifestWriter.progress, r.Recorder

    def spy(self, snapshot):
        seen.append(snapshot)
        return real_progress(self, snapshot)

    class Spy(real_recorder):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            spied["recorder"] = self

    monkeypatch.setattr(r.ManifestWriter, "progress", spy)
    monkeypatch.setattr(r, "Recorder", Spy)

    def wait_for_progress():
        memory.update(w.recorder_asset_counts(spied["recorder"].report, [YES, NO]))
        deadline = time.monotonic() + 10
        while not any(item.get("recorder") == memory for item in seen) and time.monotonic() < deadline:
            time.sleep(0.01)

    script = [json.loads(frame["raw"]) for frame in _frames("minimal_vector.jsonl")[:4]] + [wait_for_progress]
    code, out, *_ = _main(tmp_path, [script], ["--assets", YES, NO, "--rest-assets", "0"],
                          markets=_markets_file(tmp_path, [_market("m1", [YES, NO])]), progress_every_s=0.02)
    assert code == r.EXIT_OK
    assert sum(item["group_held"] for item in memory.values()) == 2  # the vector's two held states
    assert any(item.get("recorder") == memory for item in seen)
    assert all(set(item["recorder"]) == {YES, NO} and set(item["recorder"][YES]) == set(NAMES) for item in seen)
    assert set(_manifest(out)["progress"]["recorder"]) == {YES, NO}


def test_the_console_close_partial_manifest_carries_the_recorder_counts(tmp_path, monkeypatch):
    monkeypatch.setattr(r, "M1_CONSOLE_CLOSE_WAIT_S", 0.2)
    console, seen, spied = Console(), {}, {}
    real = r.Recorder

    class Spy(real):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            spied["recorder"] = self

    monkeypatch.setattr(r, "Recorder", Spy)

    def close():
        seen["memory"] = w.recorder_asset_counts(spied["recorder"].report, [YES, NO])
        assert console.handler(r.CTRL_CLOSE_EVENT) is True
        seen["manifest"] = json.loads((tmp_path / "run" / "manifest.json").read_text(encoding="utf-8"))

    script = [json.loads(frame["raw"]) for frame in _frames("minimal_vector.jsonl")] + [close]
    code, out, *_ = _main(tmp_path, [script], ["--assets", YES, NO, "--rest-assets", "0", "--max-runtime", "86400"],
                          markets=_markets_file(tmp_path, [_market("m1", [YES, NO])]), console=console,
                          progress_every_s=3600.0)
    fast = seen["manifest"]["counts"]
    assert fast["partial"] is True and fast["recorder"] == seen["memory"]
    assert sum(item["group_held"] for item in fast["recorder"].values()) >= 1
    assert code == r.EXIT_INTERRUPTED and "recorder" in _manifest(out)["counts"]


def test_report_names_match_the_recorder_names():
    assert rep.RECORDER_COUNT_NAMES == w.RECORDER_ASSET_COUNTS


# --- m1_report: display only, N/A for older runs ----------------------------------------------------


def _counts(**per_asset):
    return {asset: {**{key: 0 for key in NAMES}, **values} for asset, values in per_asset.items()}


def test_report_shows_the_recorder_counts_and_never_judges_them(tmp_path):
    stored = _counts(**{"111": {"duplicate_level_removal": 4, "group_held": 2}, "222": {"group_resolved": 1}})
    base = rep.build_report(write_run(tmp_path, good_streams(), name="base"))
    report = rep.build_report(write_run(tmp_path, good_streams(), report_manifest(
        counts={"ended_by": "deadline", "recorder": stored}), name="with"))
    assert report["recorder_counts"]["source"] == "counts"
    assert report["recorder_counts"]["per_asset"] == stored
    assert report["recorder_counts"]["totals"]["duplicate_level_removal"] == 4
    assert report["recorder_counts"]["totals"]["group_resolved"] == 1
    assert report["verdict"] == base["verdict"] == "PASS" and report["criteria"].keys() == base["criteria"].keys()
    md = rep.render_markdown(report)
    assert "Recorder internal counts" in md and "not a criterion" in md and "duplicate_level_removal" in md


def test_an_older_run_without_the_field_shows_na_without_error(tmp_path):
    report = rep.build_report(write_run(tmp_path, good_streams()))
    assert report["recorder_counts"] == {"source": None, "per_asset": None, "totals": None}
    assert report["verdict"] == "PASS"
    md = rep.render_markdown(report)
    assert "Recorder internal counts" in md and "N/A" in md


@pytest.mark.parametrize("bad", ["x", 3, None, [], {"111": "x"}, {"111": {"group_held": "2"}},
                                 {"111": {"group_held": True}}, {"111": {"group_held": -1}}])
def test_a_malformed_field_is_shown_as_na_never_an_error(tmp_path, bad):
    report = rep.build_report(write_run(tmp_path, good_streams(), report_manifest(
        counts={"ended_by": "deadline", "recorder": bad})))
    assert report["verdict"] == "PASS"
    rec = report["recorder_counts"]
    if rec["per_asset"] is not None:
        assert all(value is None or (isinstance(value, int) and value >= 0)
                   for item in rec["per_asset"].values() for value in item.values())
    assert "Recorder internal counts" in rep.render_markdown(report)


def test_a_killed_run_falls_back_to_the_last_progress_snapshot(tmp_path):
    stored = _counts(**{"111": {"group_held": 5}})
    man = report_manifest(status="running", exit_code=None, ended_at=None,
                          progress={"last_flush_at": "2026-10-09T01:00:00.000Z", "recorder": stored})
    report = rep.build_report(write_run(tmp_path, good_streams(), man))
    assert report["recorder_counts"]["source"] == "progress"
    assert report["recorder_counts"]["totals"]["group_held"] == 5
    assert "last progress" in rep.render_markdown(report)
