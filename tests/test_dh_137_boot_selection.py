"""Task DH (#137): boot_selection drops markets ending before the M2 end and every sports market.

Vectors from the 2026-10-13 offline report (poly-scan-137-boot-vectors-20261014.md); fixture rows are
in 24h-volume order, now = 2026-10-14T00:00Z, N = M1_TOP_MARKETS = 5. Offline; no network, no orders.
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

import src.m2_start as m2_start
from src.thresholds import M1_TOP_MARKETS

FIXTURE = Path(__file__).parent / "fixtures" / "boot137_markets.jsonl"
NOW = datetime(2026, 10, 14, tzinfo=timezone.utc)


def _ids(assets):
    """Market ids from the selected token ids (fixture tokens are t<id>a / t<id>b)."""
    return [int(t[1:-1]) for t in assets[::2]]


def _root(tmp_path, rows=None):
    path = tmp_path / "data" / "markets.jsonl"
    path.parent.mkdir(parents=True)
    if rows is None:
        shutil.copy(FIXTURE, path)
    else:
        path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return tmp_path


def test_n_is_still_five():
    assert M1_TOP_MARKETS == 5


@pytest.mark.parametrize("m2_end, expected", [
    (datetime(2026, 10, 21, tzinfo=timezone.utc), [2, 4, 5, 7, 8]),
    (datetime(2026, 10, 22, tzinfo=timezone.utc), [2, 4, 7, 8, 9]),
])
def test_report_vectors(tmp_path, m2_end, expected):
    assets, why = m2_start.boot_selection(_root(tmp_path), NOW, m2_end)
    assert why is None and _ids(assets) == expected


def test_without_m2_end_sports_are_still_all_dropped(tmp_path):
    """Row 6 (next-day sports single) slipped past same_day_sports before #137; now any sports row is out."""
    assets, why = m2_start.boot_selection(_root(tmp_path), NOW)
    assert why is None and _ids(assets) == [2, 3, 4, 5, 7]


def _row(i, end, tags=("politics",), category="politics", ts="2026-10-14T00:00:00Z"):
    return {"id": str(i), "ts": ts, "version": "v1", "token_ids": [f"t{i}a", f"t{i}b"], "token_ids_problem": None,
            "end_date": end, "tags": list(tags), "category": category, "volume_24h_usd": 1000.0 - i}


END = datetime(2026, 10, 21, tzinfo=timezone.utc)


def test_tennis_single_ending_after_m2_end_is_dropped_by_tag(tmp_path):
    """Gamma puts a tennis single's endDate ~1 week after the match: the end date alone cannot catch it."""
    rows = [_row(1, "2026-10-21T02:00:00Z", tags=("sports", "tennis"), category=None), _row(2, "2099-01-01T00:00:00Z")]
    assets, why = m2_start.boot_selection(_root(tmp_path, rows), NOW, END)
    assert why is None and _ids(assets) == [2]


@pytest.mark.parametrize("tags, category", [((" Sports ",), None), ((), "SPORTS"), (("tennis", "sports"), "x")])
def test_sports_by_tag_or_category_any_case(tmp_path, tags, category):
    rows = [_row(1, "2099-01-01T00:00:00Z", tags=tags, category=category), _row(2, "2099-01-01T00:00:00Z")]
    assets, _ = m2_start.boot_selection(_root(tmp_path, rows), NOW, END)
    assert _ids(assets) == [2]


def test_end_exactly_at_m2_end_is_kept(tmp_path):
    rows = [_row(1, "2026-10-21T00:00:00Z")]
    assets, why = m2_start.boot_selection(_root(tmp_path, rows), NOW, END)
    assert why is None and _ids(assets) == [1]


@pytest.mark.parametrize("end", [None, "", "not a date", 12345])
def test_missing_or_bad_end_date_fails_closed(tmp_path, end):
    rows = [_row(1, end), _row(2, "2099-01-01T00:00:00Z")]
    assets, _ = m2_start.boot_selection(_root(tmp_path, rows), NOW, END)
    assert _ids(assets) == [2]


def test_all_filtered_refuses_and_never_falls_back_to_an_older_batch(tmp_path):
    rows = [_row(1, "2099-01-01T00:00:00Z", ts="2026-10-01T00:00:00Z"), _row(2, "2026-10-15T00:00:00Z")]
    assets, why = m2_start.boot_selection(_root(tmp_path, rows), NOW, END)
    assert assets == [] and why is not None and "no usable assets" in why


def test_start_passes_its_m2_end_to_boot_selection(tmp_path, no_sentinel):
    """The real start path filters by --m2-end: a market ending before it is never booted."""
    from tests.test_m2_start import Rig, _chain, _start
    rig = Rig(tmp_path / "pod")
    path = rig.root / "data" / "markets.jsonl"
    rows = [_row(1, "2098-12-31T00:00:00Z", ts="2026-10-20T00:00:00Z"), _row(2, "2099-06-01T00:00:00Z", ts="2026-10-20T00:00:00Z")]
    with path.open("a", encoding="utf-8") as handle:
        handle.write("".join(json.dumps(r) + "\n" for r in rows))
    assert _start(rig) == m2_start.EXIT_OK  # END = 2099-01-01
    assert _chain(rig.root)[0]["assets"] == ["t2a", "t2b"]
