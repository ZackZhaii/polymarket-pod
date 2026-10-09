"""Task AY: opt-in raw book snapshots (src.m1_run --raw-books) and the offline hash check
(src.ws_hash_check). Synthetic frames only; nothing here opens a socket."""
from __future__ import annotations

import json
import socket

import pytest

from src import m1_run as r
from src import secret_scan
from src import ws_hash_check as h
from src import ws_recorder as w
from src.thresholds import WS_RAW_BOOK_FILE_MAX_BYTES, WS_RAW_BOOK_RUN_MAX_BYTES
from tests.test_m1_run import Clock as M1Clock, _main as m1_main, _manifest, _rows
from tests.test_ws_recorder import TS, A, B, Clock, FakeTransport, ListSink, _book, _ch, _hashed_book, _pc


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("network used")
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


def _recorder(sessions, raw=None, assets=(A,), runtime=5):
    clock = Clock()
    sink = ListSink()
    rec = w.Recorder(list(assets), transport=FakeTransport(sessions, clock), sink=sink, max_runtime_s=runtime,
                     monotonic=clock.monotonic, sleep=clock.sleep, now=lambda: TS, raw_books=raw)
    rec.run()
    return rec, sink


# --- the recorder hook ------------------------------------------------------------------------------


def test_raw_capture_is_off_by_default():
    rec, _ = _recorder([[_book()]])
    assert rec.raw_books is None and rec.core.capture_raw is False


def test_every_subscribed_book_frame_is_handed_over_as_received():
    frames = [_hashed_book(), _book("999"), _pc(_ch()), _book(bids=(("nan", "1"),), ts=1791496217153 + 2000),
              _book(B, bids=(("0.1", "1"), ("0.2", "2"), ("0.3", "3")))]
    saved: list[dict] = []
    rec, sink = _recorder([frames], raw=saved.append, assets=(A, B))
    assert [row["asset_id"] for row in saved] == [A, A, B]  # not the unsubscribed one, not the price_change
    assert saved[0] == {"ts": TS, "asset_id": A, "frame": json.loads(json.dumps(_hashed_book()))}
    assert saved[1]["frame"]["bids"] == [{"price": "nan", "size": "1"}]  # a rejected snapshot is kept too
    assert len(saved[2]["frame"]["bids"]) == 3  # every level, not just the top
    assert rec.report.counts["resync:invalid_price"] == 1


def test_raw_capture_changes_no_book_row():
    frames = [_hashed_book(), _pc(_ch()), _book(ts=1791496217153 + 9)]
    _, plain = _recorder([list(frames)])
    _, captured = _recorder([list(frames)], raw=[].append)
    assert plain.rows == captured.rows


# --- m1_run --raw-books -----------------------------------------------------------------------------


def test_m1_run_writes_no_raw_books_without_the_flag(tmp_path):
    code, out, *_ = m1_main(tmp_path, [[_hashed_book(), _book(B)]], ["--top", "1", "--rest-assets", "0"])
    assert code == r.EXIT_OK
    manifest = _manifest(out)
    assert manifest["settings"]["raw_books"] is False and "raw_books" not in manifest
    assert not (out / "raw_books").exists()


def test_m1_run_saves_raw_books_in_the_run_dir(tmp_path, capsys):
    deep = _book(B, bids=tuple((f"0.{40 - i}", "5") for i in range(10)), min_order_size="5", tick_size="0.01")
    code, out, *_ = m1_main(tmp_path, [[_hashed_book(), deep, _pc(_ch())]],
                            ["--top", "1", "--rest-assets", "0", "--raw-books"])
    assert code == r.EXIT_OK
    manifest = _manifest(out)
    raw = manifest["raw_books"]
    assert manifest["settings"]["raw_books"] is True
    assert raw["files"] == ["raw_books/raw_books-000001.jsonl"] and raw["saved"] == 2 and raw["dropped"] == {}
    assert raw["file_max_bytes"] == WS_RAW_BOOK_FILE_MAX_BYTES and raw["run_max_bytes"] == WS_RAW_BOOK_RUN_MAX_BYTES
    data = (out / raw["files"][0]).read_bytes()
    assert data.endswith(b"\n") and len(data) == raw["bytes"]
    rows = [json.loads(line) for line in data.splitlines()]
    assert [row["frame"]["asset_id"] for row in rows] == [A, B]
    assert len(rows[1]["frame"]["bids"]) == 10 and rows[1]["frame"]["min_order_size"] == "5"
    assert len(_rows(out, "books")) == 3  # two snapshots and the price change, as without the flag
    capsys.readouterr()
    assert h.main([str(out)]) == h.EXIT_NOT_VALIDATED  # one match, one incomplete (no neg_risk etc.)
    printed = capsys.readouterr().out
    assert "frames=2 match=1 mismatch=0 incomplete=1" in printed and "all_match=no" in printed


def test_the_secret_scan_passes_on_raw_book_files(tmp_path):
    code, out, *_ = m1_main(tmp_path, [[_hashed_book(), _book(B)]], ["--top", "1", "--rest-assets", "0", "--raw-books"])
    assert code == r.EXIT_OK
    findings, errors = secret_scan.scan_paths([out / "raw_books"], out)
    assert findings == [] and errors == []
    assert secret_scan.main(["--no-halt", "--path", str(out / "raw_books")]) == secret_scan.EXIT_CLEAN


# --- the caps ---------------------------------------------------------------------------------------


def _row(n, size=200):
    return {"ts": TS, "asset_id": A, "frame": dict(_book(), pad="x" * size, n=n)}


def test_no_raw_file_passes_its_cap_and_the_run_cap_stops_saving(tmp_path):
    events: list[dict] = []
    line = r._line_bytes(_row(0))
    sink = r.RawBookSink(tmp_path / "raw_books", file_max_bytes=line * 3, run_max_bytes=line * 7, max_age_s=3600,
                         on_event=events.append, now=lambda: TS)
    for n in range(12):
        sink.write(_row(n))
    sink.close()
    files = sorted((tmp_path / "raw_books").glob("raw_books-*.jsonl"))
    assert [p.name for p in files] == ["raw_books-000001.jsonl", "raw_books-000002.jsonl", "raw_books-000003.jsonl"]
    assert all(p.stat().st_size <= line * 3 for p in files)
    assert sum(p.stat().st_size for p in files) == sink.bytes == line * 7 and sink.saved == 7
    assert sink.dropped == {"run_cap": 5}
    assert [e["event"] for e in events] == ["raw_books_capped"]  # once, not per dropped frame
    kept = [json.loads(x)["frame"]["n"] for p in files for x in p.read_bytes().splitlines()]
    assert kept == list(range(7))


def test_unwritable_and_oversized_frames_are_dropped_and_counted(tmp_path):
    sink = r.RawBookSink(tmp_path / "raw", file_max_bytes=500, run_max_bytes=5000, max_age_s=3600)
    sink.write({"ts": TS, "asset_id": A, "frame": dict(_book(), bad=float("nan"))})
    sink.write(_row(1, size=1000))
    sink.write(_row(2, size=10))
    sink.close()
    assert sink.dropped == {"unserialisable": 1, "too_large": 1} and sink.saved == 1


def test_raw_caps_must_be_ordered(tmp_path):
    with pytest.raises(ValueError):
        r.RawBookSink(tmp_path, file_max_bytes=10, run_max_bytes=5, max_age_s=1)


def test_m1_run_file_cap_is_also_bounded_by_rotate_mb(tmp_path):
    code, out, *_ = m1_main(tmp_path, [[_hashed_book(), _book(B)]],
                            ["--top", "1", "--rest-assets", "0", "--raw-books", "--rotate-mb", "0.0005"])
    assert code == r.EXIT_OK
    raw = _manifest(out)["raw_books"]
    assert raw["file_max_bytes"] == int(0.0005 * 1048576) and len(raw["files"]) == 2 and raw["saved"] == 2


# --- src.ws_hash_check ------------------------------------------------------------------------------


def _write_raw(path, frames, extra_lines=()):
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"ts": TS, "asset_id": f.get("asset_id"), "frame": f}) for f in frames] + list(extra_lines)
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    return path


def test_hash_check_all_match(tmp_path, capsys):
    path = _write_raw(tmp_path / "raw_books" / "raw_books-000001.jsonl",
                      [_hashed_book(), dict(reversed(list(_hashed_book().items())))])
    assert h.main([str(tmp_path)]) == h.EXIT_ALL_MATCH
    assert "files=1 frames=2 match=2 mismatch=0 incomplete=0 absent=0 uncomputable=0 unreadable=0 all_match=yes" \
        in capsys.readouterr().out
    assert h.check_files([path])["per_asset"] == {A: {"match": 2}}


def test_hash_check_reports_every_outcome(tmp_path, capsys):
    raw = tmp_path / "raw_books"
    _write_raw(raw / "raw_books-000001.jsonl", [_hashed_book(), _hashed_book(hash="f" * 40), _book(),
                                                 _hashed_book(hash="")], ["{torn", json.dumps({"frame": _pc(_ch())})])
    _write_raw(raw / "raw_books-000002.jsonl", [_hashed_book(asset_id=B)])
    before = {p: p.read_bytes() for p in raw.iterdir()}
    assert h.main([str(raw), "--json"]) == h.EXIT_NOT_VALIDATED
    report = json.loads(capsys.readouterr().out)
    assert report["files"] == 2 and report["frames"] == 5
    assert report["counts"] == {"match": 1, "mismatch": 2, "incomplete": 1, "absent": 1, "uncomputable": 0,
                                "unreadable": 2}
    assert report["per_asset"][B] == {"mismatch": 1}  # asset_id is hashed: changing it breaks the hash
    assert {p: p.read_bytes() for p in raw.iterdir()} == before  # read-only
    assert w.BOOK_HASH_ENFORCED is False


def test_hash_check_with_no_frames_is_not_validated(tmp_path):
    assert h.main([str(tmp_path)]) == h.EXIT_NOT_VALIDATED


@pytest.mark.parametrize("argv", [[], ["--nope", "x"], ["/no/such/path/anywhere"]])
def test_hash_check_usage_errors(argv):
    assert h.main(argv) == h.EXIT_USAGE


def test_hash_check_never_enables_enforcement():
    source = (h.ROOT / "src" / "ws_hash_check.py").read_text(encoding="utf-8")
    assert "BOOK_HASH_ENFORCED =" not in source and "open(path, \"rb\")" in source
