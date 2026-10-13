"""Task CX (#225, D3) coverage gaps: the day's ``last_marks/<UTC day>.json`` file and its consumer.

``src/daily_marks.read_last_marks`` / ``write_last_marks`` were only exercised through
``paper_ledger.daily_unrealized``; the validation branches that decide whether the previous UTC day's last
valid mark may become a late start-of-day reference (a risk number) had no direct test. Every check here is
fail closed: an unusable price, a wrong day, a tampered digest or an unreadable file must never turn into a
reference, and the caller must see a problem (``invalid_risk_state`` -> global halt, Task AL).

Offline only: hand-written files under ``tmp_path``; no network, no real ledger, logs or snapshot folder.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src import daily_marks, leg_marks, paper_ledger

DAY = "2026-09-01"
PREV = "2026-08-31"
NOW = "2026-09-01T12:00:00Z"
PREV_BOOK_TS = "2026-08-31T23:59:00Z"
PREV_AT = "2026-08-31T23:59:30Z"


def entry(per_share=0.6, book_ts=PREV_BOOK_TS, at=PREV_AT):
    return {"per_share_usd": per_share, "book_ts": book_ts, "at": at}


def write_raw(folder, day=DAY, legs=None, *, text=None):
    """Write the day's file by hand with a matching sha256 unless ``text`` (raw bytes) is given."""
    path = daily_marks.last_marks_path(folder, day)
    path.parent.mkdir(parents=True, exist_ok=True)
    if text is None:
        body = {"day": day, "updated_at": NOW, "legs": {} if legs is None else legs}
        body["sha256"] = daily_marks.digest(body)
        text = json.dumps(body, sort_keys=True, indent=1)
    path.write_text(text, encoding="utf-8")
    return path


def _tamper(folder, day, **change):
    """Rewrite the file's first leg field without re-hashing: the digest no longer matches."""
    path = daily_marks.last_marks_path(folder, day)
    body = json.loads(path.read_text(encoding="utf-8"))
    body["legs"]["m|t"].update(change)
    path.write_text(json.dumps(body, sort_keys=True, indent=1), encoding="utf-8")
    return path


def _legs_in(folder, day):
    return sorted(p.name for p in daily_marks.last_marks_path(folder, day).parent.iterdir())


# --- the file's own checks ----------------------------------------------------------------------------

def test_a_missing_last_marks_file_is_not_a_problem_and_sits_under_last_marks(tmp_path):
    assert daily_marks.last_marks_path(tmp_path, DAY) == Path(tmp_path) / "last_marks" / f"{DAY}.json"
    assert daily_marks.read_last_marks(tmp_path, DAY) == (None, [])  # nothing to add yet: never a problem
    empty = write_raw(tmp_path, legs={})
    assert daily_marks.read_last_marks(tmp_path, DAY) == ({}, [])  # an empty day reads as no marks, not a lie
    assert _legs_in(tmp_path, DAY) == [empty.name]


def test_write_then_read_round_trips_and_replaces_the_day(tmp_path):
    daily_marks.write_last_marks(tmp_path, DAY, NOW, {"m|t": entry(0.6)})
    assert daily_marks.read_last_marks(tmp_path, DAY) == ({"m|t": entry(0.6)}, [])
    # Unlike the start-of-day snapshot (written once), the last marks are replaced on every evaluation.
    daily_marks.write_last_marks(tmp_path, DAY, NOW, {"m|t": entry(0.4), "n|t": entry(0.9)})
    legs, problems = daily_marks.read_last_marks(tmp_path, DAY)
    assert problems == []
    assert sorted(legs) == ["m|t", "n|t"] and legs["m|t"]["per_share_usd"] == 0.4
    assert _legs_in(tmp_path, DAY) == [f"{DAY}.json"]  # the temp file is gone


@pytest.mark.parametrize("per_share,usable", [
    (0.0001, True),
    (1.0, True),          # exactly 1.0 is a valid price
    (0.0, False),         # 0.0 would be a free reference for a long: refused
    (-0.5, False),
    (1.0000001, False),
    (True, False),        # a JSON true is not the price 1.0
    ("0.6", False),       # nor is a string
])
def test_only_a_price_strictly_inside_zero_and_one_is_usable(tmp_path, per_share, usable):
    write_raw(tmp_path, legs={"m|t": entry(per_share)})
    legs, problems = daily_marks.read_last_marks(tmp_path, DAY)
    assert (legs is not None) is usable
    assert problems == ([] if usable else ["daily_marks_last_bad_legs"])


def test_a_nan_price_is_never_usable(tmp_path):
    """json.loads accepts the NaN literal; the digest refuses it, so the file is a bad header (fail closed)."""
    write_raw(tmp_path, text=json.dumps({
        "day": DAY, "updated_at": NOW, "sha256": "x",
        "legs": {"m|t": {"per_share_usd": float("nan"), "book_ts": None, "at": PREV_AT}},
    }))
    assert daily_marks.read_last_marks(tmp_path, DAY) == (None, ["daily_marks_last_bad_header"])


def test_a_wrong_day_or_a_tampered_digest_is_a_bad_header(tmp_path):
    write_raw(tmp_path, day=DAY, legs={"m|t": entry(0.6)})
    _tamper(tmp_path, DAY, per_share_usd=0.9)  # the sha256 still describes 0.6
    assert daily_marks.read_last_marks(tmp_path, DAY) == (None, ["daily_marks_last_bad_header"])
    write_raw(tmp_path, day=DAY, legs={"m|t": entry(0.6)})
    body = json.loads(daily_marks.last_marks_path(tmp_path, DAY).read_text(encoding="utf-8"))
    body["day"] = PREV
    body["sha256"] = daily_marks.digest(body)
    daily_marks.last_marks_path(tmp_path, DAY).write_text(json.dumps(body), encoding="utf-8")
    assert daily_marks.read_last_marks(tmp_path, DAY) == (None, ["daily_marks_last_bad_header"])


def test_a_bad_field_set_or_a_bad_leg_shape_is_refused(tmp_path):
    write_raw(tmp_path, legs={"m|t": entry(0.6)})
    body = json.loads(daily_marks.last_marks_path(tmp_path, DAY).read_text(encoding="utf-8"))
    body.pop("updated_at")  # a field goes missing from the header
    daily_marks.last_marks_path(tmp_path, DAY).write_text(json.dumps(body), encoding="utf-8")
    assert daily_marks.read_last_marks(tmp_path, DAY) == (None, ["daily_marks_last_bad_fields"])
    write_raw(tmp_path, legs=[entry(0.6)])  # a list, not {"leg_key": leg}
    assert daily_marks.read_last_marks(tmp_path, DAY) == (None, ["daily_marks_last_bad_legs"])
    for bad, usable in (({"book_ts": None}, True), ({"book_ts": 5}, False), ({"at": None}, False)):
        write_raw(tmp_path, legs={"m|t": entry(0.6, **bad)})  # a leg field missing, null or not a string
        legs, problems = daily_marks.read_last_marks(tmp_path, DAY)
        assert (legs is not None) is usable, bad
        assert problems == ([] if usable else ["daily_marks_last_bad_legs"]), bad


def test_an_unreadable_or_unparseable_file_is_a_problem(tmp_path, monkeypatch):
    write_raw(tmp_path, text="{not json")
    assert daily_marks.read_last_marks(tmp_path, DAY) == (None, ["daily_marks_last_unparseable"])
    daily_marks.last_marks_path(tmp_path, DAY).write_bytes(b"\xff\xfe")  # not valid utf-8
    assert daily_marks.read_last_marks(tmp_path, DAY) == (None, ["daily_marks_last_unparseable"])

    def denied(self):
        raise PermissionError("denied")

    write_raw(tmp_path, legs={"m|t": entry(0.6)})
    monkeypatch.setattr(Path, "read_bytes", denied)
    assert daily_marks.read_last_marks(tmp_path, DAY) == (None, ["daily_marks_last_unreadable:PermissionError"])


def test_write_refuses_a_non_finite_price_and_leaves_the_day_intact(tmp_path):
    daily_marks.write_last_marks(tmp_path, DAY, NOW, {"m|t": entry(0.6)})
    before = daily_marks.last_marks_path(tmp_path, DAY).read_bytes()
    with pytest.raises(ValueError):
        daily_marks.write_last_marks(tmp_path, DAY, NOW, {"m|t": entry(float("nan"))})
    assert daily_marks.last_marks_path(tmp_path, DAY).read_bytes() == before
    assert _legs_in(tmp_path, DAY) == [f"{DAY}.json"]  # no half-written temp file


# --- the consumer: the late start-of-day reference ------------------------------------------------------

def _start(shares=100.0, basis=45.0):
    return leg_marks.LegPosition(shares=shares, basis=basis, carried=shares)


def test_the_previous_day_marks_become_a_reference_only_for_the_listed_legs(tmp_path, monkeypatch):
    monkeypatch.setattr(paper_ledger, "DAILY_MARKS_DIR", tmp_path)
    daily_marks.write_last_marks(tmp_path, PREV, PREV_AT, {"m|t": entry(0.6)})
    start = {("m", "t"): _start(), ("n", "t"): _start(), ("z", "t"): _start(shares=0.0, basis=0.0)}
    current = {key: leg_marks.Mark(50.0, leg_marks.SOURCE_INVALID, None, True) for key in start}
    out, problems = paper_ledger._previous_day_marks(DAY, start, current)
    assert problems == []
    assert (out[("m", "t")].value_usd, out[("m", "t")].source) == (pytest.approx(60.0),
                                                                   leg_marks.SOURCE_PREV_DAY_LAST)
    assert out[("m", "t")].mark_invalid is False and out[("m", "t")].book_ts == PREV_BOOK_TS
    assert out[("n", "t")] is current[("n", "t")]  # no mark for it yesterday: the live mark stands
    assert out[("z", "t")] is current[("z", "t")]  # a closed leg never takes a reference
    assert paper_ledger._previous_day(DAY) == PREV
    assert paper_ledger._previous_day("garbage") is None  # an unusable day has no previous day


def test_a_corrupt_previous_day_file_fails_the_late_reference_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(paper_ledger, "DAILY_MARKS_DIR", tmp_path)
    write_raw(tmp_path, day=PREV, legs={"m|t": entry(0.6)})
    _tamper(tmp_path, PREV, per_share_usd=0.05)  # a forged "cheap" mark must not hide a loss
    start, current = {("m", "t"): _start()}, {("m", "t"): leg_marks.Mark(50.0, leg_marks.SOURCE_INVALID)}
    out, problems = paper_ledger._previous_day_marks(DAY, start, current)
    assert problems == ["daily_marks_last_bad_header"]
    assert out == current  # nothing is taken from the file; the caller makes the loss unavailable
