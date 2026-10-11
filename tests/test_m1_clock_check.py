"""Task CM (B, C): m1_run's start-up clock check and m1_report's future-timestamp count.

The M1 6 h run m1-20261010T1907Z failed coverage on every asset because the recording host's clock was behind the
exchange (about 0.25 s at start, drifting about 0.1 s/h): rows whose exchange book_ts is later than the recorder's
ts are "future" and not fresh (/workspace/pod-bots/m1-6h-fail-analysis.md). m1_run now refuses to start (exit 7,
nothing written) when one public /book read per asset shows the local clock behind by more than
M1_MAX_CLOCK_BEHIND_S, and m1_report counts the future points per asset (reporting only; coverage unchanged).
Offline: fake transport, fake /book getter, fake clock.
"""
from __future__ import annotations

import pytest

import src.m1_report as rep
import src.m1_run as r
from src.thresholds import M1_MAX_CLOCK_BEHIND_S
from tests.test_m1_run import A, B, C, D, BASE_MS, Clock, _book, _clock_getter, _main, _manifest


def _check(lead_ms_by_token, *, clock=None):
    clock = clock or Clock()
    calls: list[str] = []

    def get(url):
        calls.append(url)
        token = url.rsplit("=", 1)[1]
        lead = lead_ms_by_token[token]
        if isinstance(lead, BaseException):
            raise lead
        body = {"bids": [{"price": "0.48", "size": "1"}], "asks": [{"price": "0.52", "size": "1"}]}
        if lead is not None:
            body["timestamp"] = str(BASE_MS + lead)
        return body
    return r.clock_check(list(lead_ms_by_token), {}, getter=get, now=clock.now), calls


def test_a_synced_clock_passes_and_every_asset_is_read_once_from_the_public_book():
    result, calls = _check({A: -200, B: -350})
    assert result["ok"] and result["reason"] is None and result["usable"] == 2 and result["requested"] == 2
    assert result["max_behind_s"] == -0.2 and result["source"] == "clob_rest_book" and result["bound"] == "lower"
    assert calls == [f"https://clob.polymarket.com/book?token_id={A}", f"https://clob.polymarket.com/book?token_id={B}"]


def test_the_largest_lead_decides_and_the_limit_itself_passes():
    assert _check({A: -500, B: int(M1_MAX_CLOCK_BEHIND_S * 1000)})[0]["ok"]
    result, _ = _check({A: -500, B: int(M1_MAX_CLOCK_BEHIND_S * 1000) + 1})
    assert not result["ok"] and result["reason"] == "clock_behind"
    assert result["max_behind_s"] == pytest.approx(M1_MAX_CLOCK_BEHIND_S + 0.001)


def test_the_failed_runs_start_offset_is_refused():
    # m1-20261010T1907Z: book_ts - ts max was already +0.10..0.65 s; 0.25 s behind is refused.
    assert _check({A: 250, B: -100})[0]["reason"] == "clock_behind"


def test_no_usable_answer_fails_closed():
    result, _ = _check({A: OSError("down"), B: None})
    assert not result["ok"] and result["reason"] == "no_usable_sample" and result["usable"] == 0
    assert result["unusable"] == 2 and result["max_behind_s"] is None


def test_one_usable_answer_is_enough():
    result, _ = _check({A: OSError("down"), B: -100})
    assert result["ok"] and result["usable"] == 1 and result["unusable"] == 1


# --- m1_run ---------------------------------------------------------------------------------------


def test_m1_run_records_a_passed_check_in_the_manifest(tmp_path):
    check_calls: list[str] = []
    code, out, _, _, _ = _main(tmp_path, [[_book(A), _book(B)]], ["--top", "1"],
                               clock_getter=_clock_getter(-150, check_calls))
    assert code == r.EXIT_OK
    check = _manifest(out)["clock_check"]
    assert check["ok"] and check["usable"] == 2 and check["max_behind_s"] == -0.15
    assert check["limit_s"] == M1_MAX_CLOCK_BEHIND_S and [s["token_id"] for s in check["samples"]] == [A, B]
    assert len(check_calls) == 2


@pytest.mark.parametrize("getter", [_clock_getter(400), lambda url: (_ for _ in ()).throw(OSError("down"))])
def test_m1_run_refuses_to_start_behind_or_unverified_and_writes_nothing(tmp_path, getter, capsys):
    code, out, transport, calls, _ = _main(tmp_path, [[_book(A)]], [], clock_getter=getter)
    assert code == r.EXIT_CLOCK_BEHIND == 7
    assert not out.exists() and transport.urls == [] and calls == []  # no WS connect, no REST sample
    assert "refusing to start" in capsys.readouterr().err


def test_without_a_separate_getter_the_rest_getter_is_used(tmp_path):
    code, out, transport, calls, _ = _main(tmp_path, [[_book(A)]], ["--assets", A, "--rest-assets", "0"],
                                           clock_getter=None)
    # the harness REST getter's book is 1.5 s after the fake clock's start: the clock check refuses
    assert code == r.EXIT_CLOCK_BEHIND and calls == [f"https://clob.polymarket.com/book?token_id={A}"]
    assert not out.exists() and transport.urls == []


# --- m1_report ------------------------------------------------------------------------------------


def _row(ts_s, book_s, *, state="live"):
    base = 1_791_000_000
    iso = lambda s: rep.datetime.fromtimestamp(base + s, rep.timezone.utc).isoformat(timespec="milliseconds")
    row = {"ts": iso(ts_s), "book_ts": None if book_s is None else iso(book_s), "book_ts_problem": None, "ws_state": state}
    if state == "resync":
        row["book_ts_problem"] = "invalid_book_ts"
    return row, base


def test_report_counts_future_points_without_changing_coverage():
    rows = []
    for ts_s, book_s in ((0.0, -0.1), (2.5, 3.2), (5.0, 4.9)):  # the 2.5 s row is 0.7 s in the future
        row, base = _row(ts_s, book_s)
        rows.append(row)
    start, end = base * 1_000_000, (base + 7) * 1_000_000
    result = rep.timeline(rows, start, end, 1_000_000, 120_000_000)
    # points 0..7: t=3 sees book 3.2 (future); t=0..2 and 4..7 are fresh (t=4: 4 - 3.2 >= 0)
    assert result["points"] == 8 and result["future_points"] == 1 and result["future_share"] == 1 / 8
    assert result["coverage"] == 7 / 8  # unchanged rule: the future point is not fresh


def test_report_json_and_markdown_show_future_points_and_the_verdict_is_unchanged(tmp_path):
    from tests.test_m1_report import A as RA, good_streams, write_run
    clean = rep.build_report(write_run(tmp_path, good_streams(), name="clean"))
    assert clean["verdict"] == "PASS" and all(item["future_points"] == 0 for item in clean["assets"].values())
    streams = good_streams()
    # asset A's rows say book_ts 0.7 s after they were written (a recorder clock 0.7 s slow)
    shifted = []
    for row in streams["books"]:
        if row["token_id"] == RA:
            written = rep.parse_book_ts(row["ts"])
            row = dict(row, book_ts=(written + rep.timedelta(seconds=0.7)).isoformat(timespec="milliseconds")
                       .replace("+00:00", "Z"))
        shifted.append(row)
    streams["books"] = shifted
    report = rep.build_report(write_run(tmp_path, streams, name="slow"))
    item = report["assets"][RA]
    # one row every 30 s, each opens a 0.7 s future window that holds one 1 s point -> 1/30 of the points
    assert item["future_points"] > 0 and item["future_share"] == pytest.approx(1 / 30, rel=0.01)
    assert item["coverage"] == pytest.approx(clean["assets"][RA]["coverage"] - item["future_share"], abs=1e-9)
    assert "coverage" in report["failed"]  # the unchanged rule: future points are not fresh
    md = rep.render_markdown(report)
    assert "| Future ts |" in md and f"{item['future_points']} ({item['future_share']:.4f})" in md
