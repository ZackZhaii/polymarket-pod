"""Coverage gaps: the start-of-day snapshot envelope (``runs/m2/daily_marks/<UTC day>.json``).

``src/daily_marks.read_snapshot`` / ``create_snapshot`` / ``leg_problems`` were only exercised through
``tests/test_leg_marks.py``, which asserts that *some* problem appears ("snap is None and problems"). The
individual fail-closed branches had no test: no test pinned a problem code, the index of a bad leg, the
"legs is not a list" guard, a NaN mark (which only the sha256 digest refuses), or the write side's
crash/race paths. The code decides the reference a leg's unrealized loss is measured against, so a
wrongly accepted snapshot is a wrong risk number; every rejection here must be the exact code the caller
turns into ``invalid_risk_state`` (a global halt, Task AL).

Offline only: hand-written files under ``tmp_path``; no network, no real ledger, logs or snapshot folder.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from src import daily_marks, paper_ledger

DAY = "2026-09-01"
NOW = "2026-09-01T12:00:00Z"
BOOK_TS = "2026-09-01T11:59:30Z"


def leg(key="m|t", shares=100.0, basis=50.0, mark=40.0, source="bid_ladder", book_ts=BOOK_TS, invalid=False):
    return {"leg_key": key, "shares": shares, "cost_basis_usd": basis, "mark_usd": mark,
            "mark_source": source, "book_ts": book_ts, "mark_invalid": invalid}


def write_snap(folder, legs, *, day=DAY, created_at=NOW, late=False, sha=None, extra=None, drop=None):
    """Write the day's file by hand with a matching sha256 unless ``sha`` / ``extra`` / ``drop`` is given."""
    body: dict = {"day": day, "created_at": created_at, "late": late, "legs": legs}
    if extra is not None:
        body.update(extra)
    if drop is not None:
        body.pop(drop)
    body["sha256"] = daily_marks.digest(body) if sha is None else sha
    path = daily_marks.snapshot_path(folder, DAY)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def files(folder):
    return sorted(p.name for p in Path(folder).iterdir())


# --- reading: every damaged field gets its own code -----------------------------------------------------

def test_a_missing_snapshot_is_an_empty_day_and_not_a_problem(tmp_path):
    assert daily_marks.read_snapshot(tmp_path, DAY) == (None, [])
    assert files(tmp_path) == []


@pytest.mark.parametrize("label, kwargs, expected", [
    ("sha256 is not a string", {"sha": 5}, ["daily_marks_sha_mismatch"]),
    ("created_at is not a string", {"created_at": 5}, ["daily_marks_bad_header"]),
    ("late is not a bool", {"late": 1}, ["daily_marks_bad_header"]),
    ("the day is another day", {"day": "2026-08-31"}, ["daily_marks_day_mismatch"]),
    ("a field is missing", {"drop": "late"}, ["daily_marks_bad_fields"]),
    ("an unknown field appears", {"extra": {"unknown": 1}}, ["daily_marks_bad_fields"]),
], ids=["sha_not_str", "created_at_not_str", "late_not_bool", "wrong_day", "missing_field", "extra_field"])
def test_a_damaged_snapshot_envelope_is_refused_with_its_own_code(tmp_path, label, kwargs, expected):
    write_snap(tmp_path, [leg()], **kwargs)
    snap, problems = daily_marks.read_snapshot(tmp_path, DAY)
    assert (snap, problems) == (None, expected), label


def test_a_non_finite_mark_is_refused_by_the_digest_and_never_read_as_a_price(tmp_path):
    """NaN / inf survive json.loads; the digest (allow_nan=False) refuses them, and the leg check names them too."""
    for bad in (float("nan"), float("inf"), float("-inf")):
        path = daily_marks.snapshot_path(tmp_path, DAY)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"day": DAY, "created_at": NOW, "late": False, "sha256": "x",
                                    "legs": [dict(leg(), mark_usd=bad)]}), encoding="utf-8")
        assert daily_marks.read_snapshot(tmp_path, DAY) == (
            None, ["daily_marks_sha_mismatch", "daily_marks_bad_leg:0"]), bad


def test_an_unreadable_or_unparseable_snapshot_is_a_problem_not_a_missing_day(tmp_path, monkeypatch):
    write_snap(tmp_path, [leg()])
    path = daily_marks.snapshot_path(tmp_path, DAY)
    path.write_bytes(b"\xff\xfe")  # not valid utf-8
    assert daily_marks.read_snapshot(tmp_path, DAY) == (None, ["daily_marks_unparseable"])
    path.write_text("{not json", encoding="utf-8")
    assert daily_marks.read_snapshot(tmp_path, DAY) == (None, ["daily_marks_unparseable"])

    write_snap(tmp_path, [leg()])

    def denied(self):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "read_bytes", denied)
    assert daily_marks.read_snapshot(tmp_path, DAY) == (None, ["daily_marks_unreadable:PermissionError"])


@pytest.mark.parametrize("legs", [{"m|t": leg()}, "m|t", None, 3],
                         ids=["a_dict", "a_string", "null", "an_int"])
def test_legs_that_are_not_a_list_are_refused_before_any_leg_is_read(tmp_path, legs):
    write_snap(tmp_path, legs)
    assert daily_marks.read_snapshot(tmp_path, DAY) == (None, ["daily_marks_legs_not_list"])


# --- reading: a bad leg is named by index, duplicates are not merged ------------------------------------

@pytest.mark.parametrize("change", [
    {"mark_invalid": "no"},
    {"shares": True},
    {"cost_basis_usd": -1.0},
    {"book_ts": 5},
    {"leg_key": 7},
    {"unknown": 1},
], ids=["mark_invalid_str", "bool_shares", "negative_basis", "int_book_ts", "int_leg_key", "extra"])
def test_one_bad_leg_after_a_good_one_is_named_by_its_index(tmp_path, change):
    write_snap(tmp_path, [leg(), dict(leg(), **change)])
    snap, problems = daily_marks.read_snapshot(tmp_path, DAY)
    assert snap is None
    assert problems == ["daily_marks_bad_leg:1"]


def test_a_repeated_leg_key_cannot_merge_two_positions(tmp_path):
    """A second leg with a key already seen is refused: the two marks must never be netted into one."""
    write_snap(tmp_path, [leg(mark=40.0), leg(mark=90.0)])
    assert daily_marks.read_snapshot(tmp_path, DAY) == (None, ["daily_marks_bad_leg:1"])


def test_a_snapshot_with_only_good_legs_still_reads(tmp_path):
    write_snap(tmp_path, [leg(), leg(key="n|t", shares=0.0, basis=0.0, mark=0.0)])
    snap, problems = daily_marks.read_snapshot(tmp_path, DAY)
    assert problems == []
    assert [row["leg_key"] for row in snap["legs"]] == ["m|t", "n|t"]


# --- writing: only once, and never a half-written or unverified file ------------------------------------

def test_a_stale_temp_file_from_a_crashed_writer_is_replaced_and_not_left_behind(tmp_path):
    stale = Path(tmp_path) / f".{DAY}.{os.getpid()}.tmp"
    stale.write_text("half a snapshot from a crashed writer", encoding="utf-8")
    snap = daily_marks.create_snapshot(tmp_path, DAY, NOW, False, [leg()])
    assert daily_marks.read_snapshot(tmp_path, DAY) == (snap, [])
    assert files(tmp_path) == [f"{DAY}.json"]  # the stale temp is gone, only the day file remains


def test_a_non_finite_leg_is_never_written_and_leaves_no_day_file(tmp_path):
    with pytest.raises(ValueError):
        daily_marks.create_snapshot(tmp_path, DAY, NOW, False, [leg(mark=float("nan"))])
    assert files(tmp_path) == []
    assert daily_marks.read_snapshot(tmp_path, DAY) == (None, [])  # still the day's first evaluation


def test_losing_the_exclusive_create_race_writes_nothing(tmp_path, monkeypatch):
    """The day file is created with os.link so a second writer loses; FileExistsError must be SnapshotExists."""
    def taken(src, dst):
        raise FileExistsError(str(dst))

    monkeypatch.setattr(daily_marks.os, "link", taken)
    with pytest.raises(daily_marks.SnapshotExists):
        daily_marks.create_snapshot(tmp_path, DAY, NOW, False, [leg()])
    assert files(tmp_path) == []  # nothing replaced, no temp left behind


def test_a_directory_fsync_that_cannot_be_opened_still_writes_the_snapshot(tmp_path, monkeypatch):
    real_open = os.open

    def no_dir_fsync(path, flags, *args, **kwargs):
        if Path(path) == Path(tmp_path):
            raise OSError("the folder cannot be opened for fsync")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(daily_marks.os, "open", no_dir_fsync)
    snap = daily_marks.create_snapshot(tmp_path, DAY, NOW, False, [leg()])
    assert daily_marks.read_snapshot(tmp_path, DAY) == (snap, [])


# --- the consumer: an unusable snapshot makes the loss unavailable, not zero ----------------------------

def test_a_corrupt_snapshot_makes_daily_unrealized_unavailable_with_that_code(tmp_path, monkeypatch):
    write_snap(tmp_path, [leg()], late=None)  # a tampered header: late is not a bool any more
    monkeypatch.setattr(paper_ledger, "DAILY_MARKS_DIR", tmp_path)
    monkeypatch.setattr(paper_ledger, "MARK_BOOKS_PATH", tmp_path / "books.jsonl")
    result = paper_ledger.daily_unrealized([], [], NOW, books={})
    assert result["problems"] == ["daily_marks_bad_header"]
    assert (result["loss_usd"], result["late"]) == (None, None)
