"""Task DT: book()'s post-write check finds our row by fill_id, not by "last line".

Another process may append right after our locked replace; that must not turn a successful
book into a false post_write:not_appended. A truly missing or altered row still fails closed.
Offline; every ledger lives in tmp_path.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src import ledger_store
from src.fees import booked_fee_usd, ledger_fee
from src.ledger_store import LedgerIntegrityError


def _fill(market: str) -> dict:
    return dict(ts="2026-10-13T00:00:00Z", market=market, token_id="t", side="buy",
                shares=10.0, notional_usd=4.0, fee_rate=0.02, market_price=0.4, effective_price=0.4,
                fee_usd=booked_fee_usd(ledger_fee(0.02, 0.4, 0.4, 4.0, 10.0)),
                hypothetical=False, counts_toward_risk=True, risk_decision="allow")


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _after_append(monkeypatch, mutate):
    real = ledger_store.atomic_append

    def wrapped(path, expected, line):
        real(path, expected, line)
        mutate(path, real)
    monkeypatch.setattr(ledger_store, "atomic_append", wrapped)
    return real


def test_other_process_appends_right_after_us_still_succeeds(tmp_path, monkeypatch):
    ledger = tmp_path / "ledger.jsonl"
    ledger_store.book(ledger, _fill("m-0"))
    other: dict = {}

    def other_writer(path, real):
        monkeypatch.setattr(ledger_store, "atomic_append", real)
        other.update(ledger_store.book(path, _fill("m-other")))  # a genuine, valid later append
    _after_append(monkeypatch, other_writer)
    ours = ledger_store.book(ledger, _fill("m-ours"))
    ids = [row["fill_id"] for row in _rows(ledger)]
    assert ids[-2:] == [ours["fill_id"], other["fill_id"]]
    assert ledger_store.verify(ledger_store.read_ledger(ledger)[1]) == []


def test_our_row_missing_raises_not_appended(tmp_path, monkeypatch):
    ledger = tmp_path / "ledger.jsonl"
    ledger_store.book(ledger, _fill("m-0"))
    before = ledger.read_bytes()
    _after_append(monkeypatch, lambda path, real: path.write_bytes(before))  # our row vanished
    with pytest.raises(LedgerIntegrityError) as exc:
        ledger_store.book(ledger, _fill("m-ours"))
    assert any(p.startswith("post_write:not_appended:") for p in exc.value.problems)


@pytest.mark.parametrize("change", ["value", "extra", "dropped"])
def test_our_row_content_mismatch_raises(tmp_path, monkeypatch, change):
    ledger = tmp_path / "ledger.jsonl"
    ledger_store.book(ledger, _fill("m-0"))

    def tamper(path, real):
        rows = _rows(path)
        last = rows[-1]
        if change == "value":
            last["ts"] = "2026-10-13T00:00:01Z"
        elif change == "extra":
            last["note"] = "x"
        else:
            last.pop("ts")
        path.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows), encoding="utf-8")
    _after_append(monkeypatch, tamper)
    with pytest.raises(LedgerIntegrityError) as exc:
        ledger_store.book(ledger, _fill("m-ours"))
    assert all(p.startswith("post_write:") for p in exc.value.problems)
    assert not any("not_appended" in p for p in exc.value.problems)
    if change != "dropped":
        assert any(p.startswith("post_write:row_mismatch:") for p in exc.value.problems)


def test_mismatch_helper_names_field():
    read = ledger_store.parse_ledger(b'{"fill_id":"a","x":1}\n')
    assert ledger_store._post_write_row_problems(read, "a", {"fill_id": "a", "x": 1}) == []
    assert ledger_store._post_write_row_problems(read, "a", {"fill_id": "a", "x": 2}) == ["row_mismatch:x:a"]
    assert ledger_store._post_write_row_problems(read, "b", {"fill_id": "b"}) == ["not_appended:b"]
