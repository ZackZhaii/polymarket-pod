"""Filler tests for src/ledger_store.py: the write-path refusals of ``book`` that nothing else pins.

tests/test_ledger_integrity.py covers the integrity layer end to end (ids, dedupe, atomic writes,
conservation, task U numbers, the issue #68 control fields) and tests/test_non_finite_params.py
covers the *read* path: ``load_state`` ignores a non-finite number in a non-counting (hypothetical)
legacy row. This file only adds the branches no test in the repo reaches:

* ``non_finite_value`` (src/ledger_store.py:414): the JSON guard (``allow_nan=False``) that catches a
  non-finite number the task U number check does not look at -- either because the row does not
  count (hypothetical) or because the field is not one of LEDGER_NUMBER_FIELDS. A ledger line
  cannot hold NaN/inf, so the write is refused instead of writing a line nothing can read back.
* ``pre_write:not_appended`` (src/ledger_store.py:418-419): the candidate ledger does not carry the
  row we are about to write (here: a value JSON rewrites, so what would land is not what we built).
* ``post_write:not_appended`` (src/ledger_store.py:425-426): the append returned but the re-read
  ledger is missing the new id (here: an append that silently books nothing).
* the caller-supplied ``fill_id`` refusal: the read path's ``invalid_fill_id`` rule applied to the
  row ``book`` is about to write (a reserved ``legacy:`` prefix, a non-string, or JSON null).

Every refused book must leave the old ledger byte-for-byte intact (or uncreated) and no temp file
behind. Offline, tmp_path only: no real logs/, no network, no orders, paper mode only.
"""
from __future__ import annotations

import math
from pathlib import Path

import pytest

from src import ledger_store
from src.ledger_store import FILL_ID_FIELD, LedgerIntegrityError


def _row(**extra):
    """A valid modern fill (the shape paper_ledger writes), overridable per test."""
    row = dict(ts="2026-10-11T00:00:00Z", market="m", token_id="t", side="buy", shares=10.0,
               notional_usd=4.0, fee_rate=0.0, market_price=0.4, effective_price=0.4, fee_usd=0.0,
               hypothetical=False, counts_toward_risk=True, risk_decision="allow")
    row.update(extra)
    return row


def _tmp_files(path: Path) -> list[str]:
    return sorted(p.name for p in path.parent.iterdir() if p.name.endswith(".tmp"))


def _refused(path: Path, row: dict, problems: list[str]) -> None:
    """book() must refuse this row with exactly these problems and write nothing."""
    with pytest.raises(LedgerIntegrityError) as caught:
        ledger_store.book(path, row)
    assert caught.value.problems == problems
    assert not path.exists() and _tmp_files(path) == []


# --- the JSON guard: non_finite_value -------------------------------------------------------------


@pytest.mark.parametrize("field,value", [("shares", math.nan), ("fee_usd", math.inf)],
                         ids=["nan-shares", "inf-fee"])
def test_a_non_counting_fill_with_a_non_finite_number_cannot_be_written(tmp_path, field, value):
    """A hypothetical row is skipped by the number check (it counts toward nothing) but a NaN share
    count still cannot be encoded: the fill is refused instead of producing an unreadable line."""
    path = tmp_path / "l.jsonl"
    row = _row(fill_id="gap-hypo", hypothetical=True, counts_toward_risk=False, **{field: value})
    _refused(path, row, ["non_finite_value:gap-hypo"])


@pytest.mark.parametrize("field,value", [("market_price", math.nan), ("fee_rate", math.inf)],
                         ids=["nan-market-price", "inf-fee-rate"])
def test_a_non_finite_number_outside_the_ledger_number_fields_cannot_be_written(tmp_path, field, value):
    """The task U check looks at shares / notional_usd / fee_usd only; the JSON guard is the second
    line of defence and refuses the row before anything is applied or written."""
    path = tmp_path / "l.jsonl"
    _refused(path, _row(fill_id="gap-other-field", **{field: value}), ["non_finite_value:gap-other-field"])


# --- a caller-supplied id that could forge or break a row ------------------------------------------


@pytest.mark.parametrize("bad", ["legacy:1:abc", 5, None], ids=["reserved-prefix", "int", "null"])
def test_book_refuses_a_caller_supplied_fill_id_that_is_not_a_fresh_id(tmp_path, bad):
    """A ``legacy:`` id would pretend the row predates the control-field rules; the same read-path
    rule is applied to the row being written and nothing is appended."""
    path = tmp_path / "l.jsonl"
    _refused(path, _row(**{FILL_ID_FIELD: bad}), ["pre_write:invalid_fill_id:1"])
    assert ledger_store.book(path, _row(fill_id="gap-fresh"))[FILL_ID_FIELD] == "gap-fresh"
    assert ledger_store.verify(ledger_store.read_ledger(path)[1]) == []


# --- the two not_appended guards ------------------------------------------------------------------


def test_a_row_that_does_not_survive_the_json_round_trip_is_refused_before_the_write(tmp_path):
    """JSON rewrites the tuple to a list, so the row that would land is not the row we built. The
    candidate ledger is checked before the append: nothing is written and no id is claimed."""
    path = tmp_path / "l.jsonl"
    _refused(path, _row(fill_id="gap-round-trip", note=("a", "b")), ["pre_write:not_appended:gap-round-trip"])


def test_an_append_that_silently_books_nothing_is_caught_after_the_replace(tmp_path, monkeypatch):
    path = tmp_path / "l.jsonl"
    monkeypatch.setattr(ledger_store, "atomic_append", lambda *args, **kwargs: None)  # a lost append
    _refused(path, _row(fill_id="gap-lost"), ["post_write:not_appended:gap-lost"])


def test_a_lost_append_keeps_the_earlier_fill_and_reports_the_missing_id(tmp_path, monkeypatch):
    path = tmp_path / "l.jsonl"
    first = ledger_store.book(path, _row(fill_id="gap-first"))
    before = path.read_bytes()
    monkeypatch.setattr(ledger_store, "atomic_append", lambda *args, **kwargs: None)
    with pytest.raises(LedgerIntegrityError) as caught:
        ledger_store.book(path, _row(fill_id="gap-second"))
    assert caught.value.problems == ["post_write:not_appended:gap-second"]
    assert path.read_bytes() == before  # the first, provably booked fill is untouched
    _, read = ledger_store.read_ledger(path)
    assert read.ids == [first[FILL_ID_FIELD]] and ledger_store.verify(read) == []
