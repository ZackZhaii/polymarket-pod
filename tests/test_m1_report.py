"""Task AI (M1 acceptance): the offline report src.m1_report on synthetic run directories. Offline only."""
from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src import m1_report as rep
from src import m1_run as run
from src.book_staleness import book_age_problem
from src.thresholds import (
    M1_MAX_GAP_RESYNCS_PER_ASSET,
    M1_MAX_RECONNECTS,
    M1_MIN_REST_MATCHED,
    M1_REST_MATCH_WINDOW_S,
    MAX_BOOK_AGE_S,
)

A, B = "111", "222"
BASE = datetime(2026, 10, 9, tzinfo=timezone.utc)
DAY = 86400


def iso(seconds: float) -> str:
    return (BASE + timedelta(seconds=seconds)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def ws_row(asset, written, book_at, bid=0.48, ask=0.52):
    return {"ts": iso(written), "book_ts": iso(book_at), "book_ts_problem": None, "token_id": asset, "market_id": "m1",
            "best_bid": bid, "best_ask": ask, "ws_state": "live", "ws_event": "book", "source": "clob_ws_market"}


def invalid_row(asset, written, reason="disconnect_closed"):
    return {"ts": iso(written), "book_ts": None, "book_ts_problem": "invalid_book_ts", "token_id": asset,
            "market_id": "m1", "best_bid": None, "best_ask": None, "ws_state": "resync", "resync_reason": reason}


def rest_row(asset, book_at, bid=0.48, ask=0.52, problem=None):
    return {"ts": iso(book_at), "book_ts": None if problem else iso(book_at), "book_ts_problem": problem,
            "token_id": asset, "best_bid": bid, "best_ask": ask, "sample": 1}


def manifest(elapsed=DAY, **extra):
    data = {
        "schema": "m1_run/1", "status": "completed", "started_at": iso(0), "ended_at": iso(elapsed),
        "elapsed_s": float(elapsed), "max_runtime_s": float(DAY), "exit_code": 0, "stop_signal": None,
        "assets": [{"token_id": A, "market_id": "m1", "outcome": "Yes", "question": "Q?"},
                   {"token_id": B, "market_id": "m1", "outcome": "No", "question": "Q?"}],
        "git": {"sha": "f" * 40, "dirty": False}, "version": {"python": "3.13", "websockets": "17.2.0"},
        "risk": {"pinned_sha256": run.PINNED_RISK_SHA256,
                 "start": {"sha256": run.PINNED_RISK_SHA256, "live_enabled": False},
                 "end": {"sha256": run.PINNED_RISK_SHA256, "live_enabled": False}},
        "counts": {"ended_by": "deadline"},
    }
    data.update(extra)
    return data


def good_streams(elapsed=DAY, every=30.0, rest_every=600.0):
    books, rest = [], []
    for asset in (A, B):
        t = 1.0
        while t <= elapsed:
            books.append(ws_row(asset, t, t - 0.2))
            t += every
        t = 5.0
        while t <= elapsed:
            rest.append(rest_row(asset, t))
            t += rest_every
    events = [{"ts": iso(0), "event": "start"}, {"ts": iso(0.1), "event": "connected", "connects": 1},
              {"ts": iso(elapsed), "event": "end", "ended_by": "deadline"}]
    return {"books": books, "events": events, "rest": rest}


def write_run(tmp_path: Path, streams: dict, man: dict | None = None, split: int = 1, name="run") -> Path:
    run_dir = tmp_path / name
    run_dir.mkdir()
    man = dict(man or manifest())
    files = {}
    for stream, rows in streams.items():
        (run_dir / stream).mkdir()
        files[stream] = []
        chunk = max(1, -(-len(rows) // split)) if rows else 1
        for index in range(0, max(len(rows), 1), chunk):
            rel = f"{stream}/{stream}-{index // chunk + 1:06d}.jsonl"
            (run_dir / rel).write_text("".join(json.dumps(row) + "\n" for row in rows[index:index + chunk]),
                                       encoding="utf-8")
            files[stream].append(rel)
    man.setdefault("files", files)
    man.setdefault("counts", {})
    man["counts"] = {"book_rows": len(streams["books"]), "events": len(streams["events"]),
                     "rest_rows": len(streams["rest"]), **man["counts"]}
    (run_dir / "manifest.json").write_text(json.dumps(man), encoding="utf-8")
    return run_dir


# --- the whole verdict ---------------------------------------------------------------------------


def test_a_clean_24h_run_passes(tmp_path):
    report = rep.build_report(write_run(tmp_path, good_streams()))
    assert report["verdict"] == "PASS", report["failed"]
    assert report["assets"][A]["coverage"] >= 0.9999 and report["assets"][A]["age_s"]["max"] <= 31
    assert report["rest_vs_ws"]["matched"] == 288 and report["rest_vs_ws"]["mismatched"] == 0
    assert report["events"]["reconnects"] == 0 and report["integrity_problems"] == []


def test_rotated_files_read_the_same_as_one_file(tmp_path):
    one = rep.build_report(write_run(tmp_path, good_streams(), name="one"))
    many = rep.build_report(write_run(tmp_path, good_streams(), split=7, name="many"))
    assert len(many["streams"]["books"]["files"]) == 7
    for key in ("assets", "events", "rest_vs_ws", "verdict"):
        assert one[key] == many[key]


def test_a_20_minute_hole_fails_coverage(tmp_path):
    streams = good_streams()
    streams["books"] = [row for row in streams["books"]
                        if not (row["token_id"] == A and 40_000 <= rep.to_us(row["ts"]) / 1e6 - BASE.timestamp() <= 41_200)]
    report = rep.build_report(write_run(tmp_path, streams))
    assert report["verdict"] == "FAIL" and "coverage" in report["failed"]
    assert report["assets"][A]["coverage"] < 0.99 < report["assets"][B]["coverage"]


def test_p99_age_fails_when_rows_come_too_rarely(tmp_path):
    report = rep.build_report(write_run(tmp_path, good_streams(every=150.0)))
    assert "book_age_p99" in report["failed"] and "coverage" in report["failed"]


def test_a_short_run_fails_duration(tmp_path):
    report = rep.build_report(write_run(tmp_path, good_streams(elapsed=3600), manifest(elapsed=3600)))
    assert "run_duration" in report["failed"] and "rest_matched" in report["failed"]


@pytest.mark.parametrize("change", [
    {"status": "interrupted", "exit_code": 5, "stop_signal": "SIGTERM"},
    {"exit_code": 1},
    {"counts": {"ended_by": "stopped"}},
])
def test_an_interrupted_or_failed_run_is_not_complete(tmp_path, change):
    report = rep.build_report(write_run(tmp_path, good_streams(), manifest(**change)))
    assert report["failed"] == ["run_completed"]


def test_the_end_event_must_say_deadline(tmp_path):
    streams = good_streams()
    streams["events"][-1]["ended_by"] = "stopped"
    assert rep.build_report(write_run(tmp_path, streams))["failed"] == ["run_completed"]


@pytest.mark.parametrize("side, state", [
    ("start", {"sha256": "0" * 64, "live_enabled": False}),
    ("end", {"sha256": "0" * 64, "live_enabled": False}),
    ("end", {"sha256": run.PINNED_RISK_SHA256, "live_enabled": True}),
    ("end", None),
    ("start", {"sha256": run.PINNED_RISK_SHA256, "live_enabled": None}),
])
def test_risk_must_be_pinned_at_start_and_end(tmp_path, side, state):
    man = manifest()
    man["risk"] = {**man["risk"], side: state}
    assert rep.build_report(write_run(tmp_path, good_streams(), man))["failed"] == ["risk_pinned"]


def test_an_unfinished_run_fails_integrity(tmp_path):
    man = manifest(status="running", ended_at=None, elapsed_s=None, exit_code=None, files={})
    report = rep.build_report(write_run(tmp_path, good_streams(), man))
    assert report["verdict"] == "FAIL" and {"run_completed", "run_duration", "integrity", "coverage"} <= set(report["failed"])
    assert report["integrity_problems"] == ["manifest has no usable started_at/ended_at (run unfinished?)"]
    assert report["streams"]["books"]["rows"] == len(good_streams()["books"])  # unlisted files are still read
    assert any("files on disk not in the manifest" in item for item in report["warnings"])


def test_an_unreadable_line_fails_integrity(tmp_path):
    run_dir = write_run(tmp_path, good_streams())
    with open(run_dir / "books" / "books-000001.jsonl", "ab") as handle:
        handle.write(b'{"torn": \n')
    report = rep.build_report(run_dir)
    assert report["failed"] == ["integrity"] and report["streams"]["books"]["unreadable_lines"] == 1


def test_a_missing_listed_file_fails_integrity(tmp_path):
    run_dir = write_run(tmp_path, good_streams(), split=3)
    (run_dir / "rest" / "rest-000002.jsonl").unlink()
    report = rep.build_report(run_dir)
    assert "integrity" in report["failed"] and any("missing" in item for item in report["integrity_problems"])


def test_a_row_count_disagreeing_with_the_manifest_fails_integrity(tmp_path):
    run_dir = write_run(tmp_path, good_streams())
    data = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    data["counts"]["book_rows"] += 1
    (run_dir / "manifest.json").write_text(json.dumps(data), encoding="utf-8")
    assert rep.build_report(run_dir)["failed"] == ["integrity"]


def test_rows_for_unknown_assets_or_without_a_time_fail_integrity(tmp_path):
    streams = good_streams()
    streams["books"].append(ws_row("999", 10, 9))
    assert "integrity" in rep.build_report(write_run(tmp_path, streams, name="unknown"))["failed"]
    streams = good_streams()
    streams["books"].append({**ws_row(A, 10, 9), "ts": "soon"})
    assert "integrity" in rep.build_report(write_run(tmp_path, streams, name="nots"))["failed"]


def test_a_dirty_tree_is_a_warning_not_a_failure(tmp_path):
    report = rep.build_report(write_run(tmp_path, good_streams(), manifest(git={"sha": "a" * 40, "dirty": True})))
    assert report["verdict"] == "PASS" and any("dirty" in item for item in report["warnings"])


# --- coverage and book age -----------------------------------------------------------------------


def _us(seconds):
    return rep.to_us(iso(seconds))


def test_freshness_matches_book_age_problem_at_every_point():
    rng = random.Random(7)
    rows, t = [], 0.0
    while t < 2000:
        t += rng.choice([0.4, 3.0, 50.0, 119.9, 121.0])
        if rng.random() < 0.1:
            rows.append(invalid_row(A, t))
        else:
            rows.append(ws_row(A, t, t + rng.choice([-130.0, -120.0, -119.999, -5.0, 0.0, 0.3])))
    result = rep.timeline(rows, _us(0), _us(2000), 1_000_000, int(MAX_BOOK_AGE_S * 1e6))
    fresh = 0
    for second in range(0, 2001):
        latest = [row for row in rows if rep.to_us(row["ts"]) <= _us(second)]
        if not latest or latest[-1]["ws_state"] == "resync":
            continue
        now = BASE + timedelta(seconds=second)
        fresh += book_age_problem(latest[-1]["book_ts"], now, MAX_BOOK_AGE_S) is None
    assert result["points"] == 2001 and result["coverage"] == pytest.approx(fresh / 2001)


def test_age_boundary_120s_is_fresh_and_a_future_book_is_not():
    rows = [ws_row(A, 0.0, -120.0)]
    assert rep.timeline(rows, _us(0), _us(0), 1_000_000, 120_000_000)["coverage"] == 1.0
    rows = [ws_row(A, 0.0, -120.001)]
    assert rep.timeline(rows, _us(0), _us(0), 1_000_000, 120_000_000)["coverage"] == 0.0
    rows = [ws_row(A, 0.0, 0.5)]
    result = rep.timeline(rows, _us(0), _us(1), 1_000_000, 120_000_000)
    assert result["coverage"] == 0.5 and result["age_s"]["p50"] == -0.5


def test_an_invalidation_row_makes_the_book_unavailable_until_the_next_live_row():
    rows = [ws_row(A, 0, 0), invalid_row(A, 10), ws_row(A, 20, 19.5)]
    result = rep.timeline(rows, _us(0), _us(29), 1_000_000, 120_000_000)
    assert result["coverage"] == pytest.approx(20 / 30) and result["no_book_share"] == pytest.approx(10 / 30)


def test_no_row_before_the_first_snapshot_is_no_book():
    result = rep.timeline([ws_row(A, 5, 4.9)], _us(0), _us(9), 1_000_000, 120_000_000)
    assert result["no_book_share"] == 0.5 and result["coverage"] == 0.5


def test_rows_out_of_file_order_are_sorted_by_write_time():
    rows = [ws_row(A, 20, 19), ws_row(A, 0, 0)]
    assert rep.timeline(rows, _us(0), _us(29), 1_000_000, 120_000_000)["coverage"] == 1.0


def test_percentiles_are_nearest_rank():
    values = list(range(1, 101))
    assert [rep.percentile(values, q) for q in (50, 90, 99, 100)] == [50, 90, 99, 100]
    assert rep.percentile([], 50) is None and rep.percentile([3.0], 99) == 3.0


# --- reconnects and resyncs ----------------------------------------------------------------------


def _with_events(extra):
    streams = good_streams()
    streams["events"] = streams["events"][:-1] + extra + streams["events"][-1:]
    return streams


def test_reconnect_attempts_at_the_limit_pass_and_one_more_fails(tmp_path):
    at_limit = [{"ts": iso(100 + i), "event": "connected"} for i in range(M1_MAX_RECONNECTS - 1)]
    at_limit.append({"ts": iso(200), "event": "connect_failed", "error": "OSError"})
    report = rep.build_report(write_run(tmp_path, _with_events(at_limit), name="ok"))
    assert report["events"]["reconnect_attempts"] == M1_MAX_RECONNECTS and report["verdict"] == "PASS"
    over = at_limit + [{"ts": iso(300), "event": "connect_failed", "error": "TimeoutError"}]
    report = rep.build_report(write_run(tmp_path, _with_events(over), name="over"))
    assert report["failed"] == ["reconnects"]
    assert report["events"]["connect_failure_errors"] == {"OSError": 1, "TimeoutError": 1}


def test_gap_resyncs_per_asset_ignore_disconnects(tmp_path):
    gaps = [{"ts": iso(100 + i), "event": "resync", "asset": A, "reason": "unknown_level_removed"}
            for i in range(M1_MAX_GAP_RESYNCS_PER_ASSET)]
    drops = [{"ts": iso(500 + i), "event": "resync", "asset": A, "reason": "disconnect_closed"} for i in range(40)]
    report = rep.build_report(write_run(tmp_path, _with_events(gaps + drops), name="ok"))
    assert report["verdict"] == "PASS" and report["events"]["gap_resyncs_by_asset"] == {A: 24, B: 0}
    assert report["events"]["resyncs_by_reason"] == {"disconnect_closed": 40, "unknown_level_removed": 24}
    more = gaps + [{"ts": iso(900), "event": "resync", "asset": A, "reason": "timestamp_regression"}]
    assert rep.build_report(write_run(tmp_path, _with_events(more), name="over"))["failed"] == ["gap_resyncs"]


def test_disconnect_reasons_are_counted(tmp_path):
    extra = [{"ts": iso(10), "event": "disconnected", "reason": "closed", "healthy": True},
             {"ts": iso(20), "event": "disconnected", "reason": "resync_timeout", "healthy": False},
             {"ts": iso(30), "event": "connected"}, {"ts": iso(40), "event": "connected"}]
    events = rep.build_report(write_run(tmp_path, _with_events(extra)))["events"]
    assert events["disconnects"] == {"closed": 1, "resync_timeout": 1} and events["reconnects"] == 2


# --- REST vs WS ----------------------------------------------------------------------------------


def _window():
    return int(M1_REST_MATCH_WINDOW_S * 1e6)


def test_rest_is_matched_to_the_nearest_ws_row_in_exchange_time():
    ws = {A: [ws_row(A, 100, 100, bid=0.40), ws_row(A, 110, 110, bid=0.45)]}
    result = rep.compare_rest([rest_row(A, 106, bid=0.45)], ws, _window())
    assert result["matched"] == 1 and result["mismatched"] == 0
    result = rep.compare_rest([rest_row(A, 104, bid=0.45)], ws, _window())
    assert result["mismatched"] == 1
    item = result["mismatches"][0]
    assert item["sides"] == ["bid"] and item["dt_s"] == -4.0 and item["ws_best_bid"] == 0.40


def test_rest_matching_uses_exchange_time_not_write_time():
    ws = {A: [ws_row(A, 500, 100, bid=0.40), ws_row(A, 101, 400, bid=0.45)]}
    assert rep.compare_rest([rest_row(A, 102, bid=0.40)], ws, _window())["mismatched"] == 0


def test_rest_outside_the_window_or_without_ws_is_unmatched():
    ws = {A: [ws_row(A, 0, 0)]}
    edge = M1_REST_MATCH_WINDOW_S
    result = rep.compare_rest([rest_row(A, edge), rest_row(A, edge + 0.001), rest_row(B, 1)], ws, _window())
    assert result["matched"] == 1 and result["unmatched"] == 2
    assert result["unmatched_reasons"] == {"no_ws_row_in_window": 1, "no_ws_row": 1}


def test_invalidation_rows_and_bad_rest_times_never_match():
    ws = {A: [invalid_row(A, 5)]}
    result = rep.compare_rest([rest_row(A, 5), rest_row(A, 5, problem="invalid_book_ts")], ws, _window())
    assert result["matched"] == 0 and result["unmatched_reasons"] == {"no_ws_row": 1, "rest_book_ts_unusable": 1}


def test_prices_compare_at_six_decimals_and_one_sided_books_compare_none():
    ws = {A: [ws_row(A, 0, 0, bid=0.1 + 0.2, ask=None)]}
    result = rep.compare_rest([rest_row(A, 0, bid=0.3, ask=None)], ws, _window())
    assert result["matched"] == 1 and result["mismatched"] == 0
    result = rep.compare_rest([rest_row(A, 0, bid=0.3, ask=0.5)], ws, _window())
    assert result["mismatches"][0]["sides"] == ["ask"]


def test_mismatch_and_unmatched_rates_against_the_criteria(tmp_path):
    streams = good_streams()
    rest = streams["rest"]
    bad = max(1, int(len(rest) * 0.03))
    for row in rest[:bad]:
        row["best_ask"] = 0.53
    report = rep.build_report(write_run(tmp_path, streams, name="mismatch"))
    assert report["failed"] == ["rest_top_mismatch"] and report["rest_vs_ws"]["mismatches_listed"] == bad
    streams = good_streams()
    for row in streams["rest"][: int(len(streams["rest"]) * 0.11)]:
        row["book_ts"] = iso(-1000)
    report = rep.build_report(write_run(tmp_path, streams, name="unmatched"))
    assert report["failed"] == ["rest_unmatched"]


def test_too_few_rest_samples_fail(tmp_path):
    streams = good_streams()
    streams["rest"] = streams["rest"][: M1_MIN_REST_MATCHED - 1]
    report = rep.build_report(write_run(tmp_path, streams))
    assert report["failed"] == ["rest_matched"]


def test_the_mismatch_list_is_capped_but_everything_is_counted(monkeypatch):
    monkeypatch.setattr(rep, "MISMATCH_LIST_LIMIT", 3)
    ws = {A: [ws_row(A, 0, 0)]}
    result = rep.compare_rest([rest_row(A, 1, bid=0.1)] * 5, ws, _window())
    assert result["mismatched"] == 5 and result["mismatches_listed"] == 3


# --- CLI ------------------------------------------------------------------------------------------


def test_cli_pass_writes_markdown_and_json(tmp_path, capsys):
    run_dir = write_run(tmp_path, good_streams())
    assert rep.main([str(run_dir)]) == rep.EXIT_PASS
    data = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    md = (run_dir / "report.md").read_text(encoding="utf-8")
    assert data["verdict"] == "PASS" and md.startswith("# M1 acceptance report: PASS")
    assert "| coverage | PASS |" in md and "m1_report PASS" in capsys.readouterr().out


def test_cli_fail_exits_1_and_lists_mismatches(tmp_path):
    streams = good_streams()
    for row in streams["rest"][:20]:
        row["best_bid"] = 0.47
    run_dir = write_run(tmp_path, streams)
    md_path, json_path = tmp_path / "x.md", tmp_path / "x.json"
    assert rep.main([str(run_dir), "--out-md", str(md_path), "--out-json", str(json_path)]) == rep.EXIT_FAIL
    md = md_path.read_text(encoding="utf-8")
    assert "| rest_top_mismatch | FAIL |" in md
    assert "| Token | REST book_ts | WS book_ts | dt s | REST bid | WS bid | REST ask | WS ask |" in md
    assert md.count("| 0.47 | 0.48 | 0.52 | 0.52 |") == 20
    assert json.loads(json_path.read_text(encoding="utf-8"))["failed"] == ["rest_top_mismatch"]
    assert not (run_dir / "report.md").exists()


@pytest.mark.parametrize("setup", ["missing", "no_manifest", "bad_json", "wrong_schema"])
def test_cli_usage_errors_exit_2(tmp_path, setup):
    run_dir = tmp_path / "run"
    if setup != "missing":
        run_dir.mkdir()
    if setup == "bad_json":
        (run_dir / "manifest.json").write_text("{", encoding="utf-8")
    if setup == "wrong_schema":
        (run_dir / "manifest.json").write_text(json.dumps({"schema": "other"}), encoding="utf-8")
    assert rep.main([str(run_dir)]) == rep.EXIT_USAGE
    assert rep.main(["--bogus"]) == rep.EXIT_USAGE


# --- end to end: m1_run output feeds m1_report ------------------------------------------------------


def test_a_real_m1_run_directory_reports(tmp_path, monkeypatch):
    import test_m1_run as runner_tests  # same tests/ folder (rootdir on sys.path via conftest)

    sessions = [[runner_tests._book(runner_tests.A), runner_tests._book(runner_tests.B)]]
    code, out, _, _, _ = runner_tests._main(tmp_path, sessions, ["--top", "1", "--rest-assets", "2"])
    assert code == run.EXIT_OK
    report = rep.build_report(out)
    assert report["integrity_problems"] == [] and report["rest_vs_ws"]["matched"] == 2
    assert report["rest_vs_ws"]["mismatched"] == 0 and report["events"]["connects"] == 1
    assert set(report["failed"]) == {"run_duration", "rest_matched", "coverage"}  # 60 s, not 24 h
    monkeypatch.setattr(rep, "M1_MIN_RUN_S", 59.0)
    monkeypatch.setattr(rep, "M1_MIN_REST_MATCHED", 2)
    monkeypatch.setattr(rep, "M1_MIN_COVERAGE", 0.95)
    assert rep.main([str(out)]) == rep.EXIT_PASS


def test_a_resync_row_never_counts_as_a_book_even_with_a_stamp():
    marked = {**ws_row(A, 0, 0), "ws_state": "resync"}
    problem = {**ws_row(A, 0, 0), "book_ts_problem": "invalid_book_ts"}
    for row in (marked, problem):
        result = rep.timeline([row], _us(0), _us(9), 1_000_000, 120_000_000)
        assert result["coverage"] == 0.0 and result["no_book_share"] == 1.0
        assert rep.compare_rest([rest_row(A, 0)], {A: [row]}, _window())["matched"] == 0


def test_percentile_rounds_the_rank_up():
    assert rep.percentile(list(range(1, 11)), 99) == 10 and rep.percentile(list(range(1, 11)), 91) == 10


# --- Task BT: an unfinished run's observed span and the run-duration note -----------------------------


def _killed(tmp_path, **extra):
    """The M1 rehearsal failure: killed at ~4h45m of 6h; manifest stuck at running with no ended_at."""
    streams = good_streams(elapsed=17_000)
    man = manifest(status="running", ended_at=None, elapsed_s=None, exit_code=None, max_runtime_s=21600.0,
                   counts={}, **extra)
    man["risk"] = dict(man["risk"], end=None)
    run_dir = write_run(tmp_path, streams, man)
    data = json.loads((run_dir / "manifest.json").read_text())
    data["counts"] = {}  # m1_run never wrote counts
    (run_dir / "manifest.json").write_text(json.dumps(data))
    return run_dir


def test_a_killed_run_still_fails_but_reports_the_span_it_covered_from_its_rows(tmp_path):
    report = rep.build_report(_killed(tmp_path))
    assert report["verdict"] == "FAIL"
    for name in ("run_completed", "run_duration", "risk_pinned", "integrity", "coverage", "book_age_p99"):
        assert name in report["failed"]  # the criteria are unchanged: an unfinished run never passes
    observed = report["observed_span"]
    assert observed["source"] == "last_row_ts" and observed["span_s"] == pytest.approx(16_981.0, abs=30)
    assert "NOT A CRITERION" in observed["label"]
    assert observed["assets"][A]["coverage"] >= 0.999 and observed["assets"][A]["age_s"]["p99"] <= 31
    text = rep.render_markdown(report)
    assert "## Observed span (not a criterion; source: last_row_ts)" in text and "p99 s (span)" in text


def test_the_observed_span_ends_at_the_last_progress_flush(tmp_path):
    report = rep.build_report(_killed(tmp_path, progress={"last_flush_at": iso(12_000), "book_rows": 1}))
    observed = report["observed_span"]
    assert observed["source"] == "progress.last_flush_at" and observed["span_s"] == 12_000.0
    assert observed["end"] == iso(12_000)


def test_an_interrupted_run_reports_its_span_from_ended_at(tmp_path):
    streams = good_streams(elapsed=17_000)
    man = manifest(elapsed=17_000, status="interrupted", exit_code=5, stop_signal="CTRL_CLOSE_EVENT",
                   stop_reason="console:CTRL_CLOSE_EVENT", max_runtime_s=21600.0,
                   counts={"partial": True, "ended_by": "console"})
    report = rep.build_report(write_run(tmp_path, streams, man))
    assert "run_completed" in report["failed"] and report["verdict"] == "FAIL"
    assert report["observed_span"]["source"] == "ended_at" and report["run"]["stop_reason"] == "console:CTRL_CLOSE_EVENT"
    assert any("counts are partial" in item for item in report["warnings"])


def test_a_completed_run_has_no_observed_span_section(tmp_path):
    report = rep.build_report(write_run(tmp_path, good_streams()))
    assert report["observed_span"] is None and "Observed span" not in rep.render_markdown(report)


def test_a_rehearsal_shorter_than_m1_min_run_s_says_run_duration_cannot_pass(tmp_path):
    from src.thresholds import M1_MIN_RUN_S, M1_RUN_TARGET_S
    assert M1_MIN_RUN_S == 21540.0  # 6 h minus 60 s (KEY-approved 2026-10-11 家伟批准; was 86340)
    report = rep.build_report(write_run(tmp_path, good_streams(elapsed=3600),
                                        manifest(elapsed=3600, max_runtime_s=3600.0)))
    assert "run_duration" in report["failed"]
    assert any("run_duration cannot pass by design" in item for item in report["warnings"])
    assert "run_duration cannot pass by design" in rep.render_markdown(report)
    # the 6 h acceptance run itself: no such warning, and run_duration passes on its elapsed time
    six = rep.build_report(write_run(tmp_path, good_streams(elapsed=21600),
                                     manifest(elapsed=21600, max_runtime_s=M1_RUN_TARGET_S), name="six"))
    assert not any("cannot pass by design" in item for item in six["warnings"])
    assert six["criteria"]["run_duration"]["pass"] is True
    edge = rep.build_report(write_run(tmp_path, good_streams(elapsed=21600),
                                      manifest(elapsed=21600, max_runtime_s=M1_MIN_RUN_S - 1), name="edge"))
    assert any("cannot pass by design" in item for item in edge["warnings"])
