"""PR #486 / issue #347 (Task FH) P1: a settlement row is never persisted once the wall clock has crossed
to a later UTC day than the row's stamp — per write, mid-batch, on both paths.

Before: the day-boundary check ran once at ``apply_settlements`` entry (and only when the caller passed
both ``now`` and ``clock``). A crossing after that check — between two rows of one batch, or inside the
write itself — booked rows stamped with yesterday, so the write day's ``max_daily_loss`` never saw the
realized loss (REPORT-C: a $50 loss over the $30 cap let the next-day probe fill). The commit path
(``commit_if_allowed`` -> ``_commit_locked`` -> ``_apply_settlements_locked``) passed no clock at all, so
it had no protection of any kind.

Now (this file's contract):
- ``_apply_settlements_locked`` rechecks the UTC day before every persisted write (and again under the
  write mutex, before each atomic replacement / retry, via ``ledger_store.book(before_replace=...)``):
  a crossing defers the rest of the batch (``deferred: settlement_deferred_day_boundary``), keeps the
  rows already booked, adds no problem and no halt, and the next round books only what is missing.
- The commit path injects the same wall clock, reports the deferral on its result, and refuses to book a
  fill in that tick (RiskGate still evaluates the pre-booking ledger; the un-booked realized loss must
  not become spendable margin).

Determinism: the boundary crossing is a patched clock flipped at the exact write boundary (the same
construction as the independent reproductions in .dsh/qc/repro/), never a real wait for midnight.
Offline: no network, no keys, no orders, every file in tmp_path.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

import pytest

from src import halt_store, ledger_store, paper_ledger
from src.daily_loss import daily_loss
from src.fees import booked_fee_usd, ledger_fee
from src.podlib import load_risk, utc_now_ms

DAY1 = "2026-09-01T23:59:59Z"  # the plan / caller stamp, just before UTC midnight
DAY2 = "2026-09-02T00:00:01Z"  # the wall clock once the boundary is crossed
RESOLUTION = {"condition_id": "0xc", "winning_token_id": "yes", "outcome_prices_final": [1.0, 0.0],
              "outcome_token_ids": ["yes", "no"], "closed_ts": "2026-08-29T00:00:00Z",
              "snapshot_ts": "2026-08-31T01:00:00Z", "status": "resolved", "uma_checked": True}
RESOLUTION2 = {"condition_id": "0xd", "winning_token_id": "yes2", "outcome_prices_final": [1.0, 0.0],
               "outcome_token_ids": ["yes2", "no2"], "closed_ts": "2026-08-29T00:00:00Z",
               "snapshot_ts": "2026-08-31T01:00:00Z", "status": "resolved", "uma_checked": True}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _dt(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _settlement_rows(path: Path) -> list[dict]:
    return [row for row in ledger_store.read_ledger(path)[1].rows if ledger_store.is_settlement(row)]


def _book_leg(path: Path, fill_id: str, market: str, token: str, shares: float = 100.0) -> None:
    price = 0.5
    ledger_store.book(path, dict(
        fill_id=fill_id, ts="2026-08-31T20:00:00Z", market=market, token_id=token, side="buy",
        shares=shares, notional_usd=shares * price, fee_rate=0.0, market_price=price,
        effective_price=price, fee_usd=booked_fee_usd(ledger_fee(0.0, price, price, shares * price, shares)),
        hypothetical=False, counts_toward_risk=True, risk_decision="allow"))


@pytest.fixture
def led(tmp_path, monkeypatch):
    """Isolated paths; ``utc_now`` reads the mutable ``box``. Each test seeds its own legs."""
    ledger, res = tmp_path / "paper.jsonl", tmp_path / "resolutions.jsonl"
    for name, value in {"PAPER_PATH": ledger, "QUOTES_PATH": tmp_path / "q.jsonl",
                        "MARK_BOOKS_PATH": tmp_path / "b.jsonl", "DAILY_MARKS_DIR": tmp_path / "dm",
                        "SETTLEMENT_RESOLUTIONS_PATH": res}.items():
        monkeypatch.setattr(paper_ledger, name, value)
    monkeypatch.setattr(paper_ledger, "bus", lambda *a, **k: None)
    box = {"now": DAY1}
    monkeypatch.setattr(paper_ledger, "utc_now", lambda: box["now"])
    monkeypatch.setattr(halt_store, "HALT_STATE_PATH", tmp_path / "halt_state.json")
    monkeypatch.setattr(halt_store, "HALT_FALLBACK_PATH", tmp_path / "HALT")
    return {"ledger": ledger, "res": res, "box": box}


def _seed(led: dict, *, shares: float = 100.0) -> None:
    """One losing leg (cost ``shares`` x 0.5) + its confirmed resolution: exactly one settlement row to
    book, realizing the full cost (the token pays 0). The default 100 shares cost 50 USD, over the
    0.03 x PAPER_EQUITY_USD 1000 = 30 USD daily cap — the REPORT-C red baseline."""
    _book_leg(led["ledger"], "x486-0000", "m", "no", shares=shares)
    led["res"].write_text(json.dumps(RESOLUTION) + "\n", encoding="utf-8")


def _add_second_market(led: dict) -> None:
    """A second resolved market with its own token ids: a two-row settlement batch."""
    _book_leg(led["ledger"], "x486-0001", "m2", "no2")
    with led["res"].open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(RESOLUTION2) + "\n")


def _flip_at_the_write(monkeypatch, box: dict, *, when: str) -> None:
    """Wrap ``ledger_store.book`` so the wall clock flips to DAY2 at a settlement row's write boundary.

    ``when="before"``: the flip lands inside the write, after the loop-head check and before the atomic
    replacement (only the in-write guard can catch it). ``when="after"``: the flip lands after the first
    settlement row is fully persisted (a mid-batch crossing before the next row's check). The wrapper
    flips once; every later read (the next row, the retry round, the probe's own fill) stays on DAY2.
    """
    real = ledger_store.book
    state = {"flipped": False}

    def wrapped(path: Path, fill: dict, **kwargs):
        if not state["flipped"] and when == "before" and ledger_store.is_settlement(fill):
            box["now"] = DAY2
            state["flipped"] = True
        out = real(path, fill, **kwargs)
        if not state["flipped"] and when == "after" and ledger_store.is_settlement(fill):
            box["now"] = DAY2
            state["flipped"] = True
        return out

    monkeypatch.setattr(ledger_store, "book", wrapped)


def _probe(**kwargs):
    """The REPORT-C probe: a hypothetical gate-test intent with real edge, allowed only under the cap.

    book_ts is the real wall clock (utc_now_ms, not this module's patched utc_now): RiskGate's staleness
    check reads the real clock, and a stale book would veto for a reason this file is not about."""
    return paper_ledger._intent(market="probe-486", model_prob=0.70, market_price=0.40,
                                book_ts=utc_now_ms(), **kwargs)


# ------------------------------------------------------------- public entry, per write
def test_public_entry_plan_already_on_the_next_day_defers(led):
    """Scenario A pin: the caller's stamp and the clock are already a day apart before anything writes."""
    _seed(led)
    out = paper_ledger.apply_settlements(DAY1, clock=lambda: _dt(DAY2))
    assert out["booked"] == [] and out["problems"] == []
    assert out["deferred"] == paper_ledger.SETTLEMENT_DAY_BOUNDARY
    assert _settlement_rows(led["ledger"]) == []


def test_public_entry_write_moment_crossing_defers_via_the_in_write_guard(led, monkeypatch):
    """The crossing lands inside the write itself: only a check under the write mutex can catch it."""
    _seed(led)
    before = _sha(led["ledger"])
    _flip_at_the_write(monkeypatch, led["box"], when="before")
    out = paper_ledger.apply_settlements(DAY1, clock=lambda: _dt(led["box"]["now"]))
    assert out["booked"] == [] and out["problems"] == []
    assert out["deferred"] == paper_ledger.SETTLEMENT_DAY_BOUNDARY
    assert _settlement_rows(led["ledger"]) == [] and _sha(led["ledger"]) == before
    assert not halt_store.active_halts()


def test_public_entry_mid_batch_crossing_keeps_row_one_and_next_round_books_only_row_two(led, monkeypatch):
    """Scenario B + the retry: row one is booked on day 1, row two defers, and the next round (a fresh
    read of the same files, as after a restart) books exactly the missing row on day 2 — no duplicate,
    no gap. The clock callable reads the same mutable box the flip wrapper moved."""
    _seed(led)
    _add_second_market(led)
    _flip_at_the_write(monkeypatch, led["box"], when="after")
    out = paper_ledger.apply_settlements(DAY1, clock=lambda: _dt(led["box"]["now"]))
    assert [row["market"] for row in out["booked"]] == ["m"]
    assert out["deferred"] == paper_ledger.SETTLEMENT_DAY_BOUNDARY and out["problems"] == []
    rows = _settlement_rows(led["ledger"])
    assert len(rows) == 1 and rows[0]["market"] == "m" and rows[0]["ts"].startswith("2026-09-01")
    # next round, wall clock on day 2 (the wrapper already flipped once and never flips again)
    again = paper_ledger.apply_settlements("2026-09-02T00:00:05Z",
                                           clock=lambda: _dt(led["box"]["now"]))
    assert again["problems"] == [] and "deferred" not in again
    assert [row["market"] for row in again["booked"]] == ["m2"]
    rows = sorted(_settlement_rows(led["ledger"]), key=lambda row: row["market"])
    assert len(rows) == 2
    assert rows[0]["ts"].startswith("2026-09-01") and rows[1]["ts"].startswith("2026-09-02")
    # idempotent: nothing left to book, and a third round writes nothing
    third = paper_ledger.apply_settlements("2026-09-02T00:00:06Z", clock=lambda: _dt(led["box"]["now"]))
    assert third["booked"] == [] and third["problems"] == [] and len(_settlement_rows(led["ledger"])) == 2


# ------------------------------------------------------------- commit path (scenario C)
def test_commit_path_write_moment_crossing_defers_and_books_neither_row_nor_probe(led, monkeypatch):
    """C-1 on the commit path: the settlement write crosses the boundary, so this tick defers the row,
    reports the deferral, vetoes the probe (no halt, no problem) and books no fill.

    The seed costs 20 USD (40 shares), under the 30 USD cap, on purpose: RiskGate alone would allow
    this probe, so the veto measured here is the deferral guard itself, not the daily-loss cap."""
    _seed(led, shares=40.0)
    before = _sha(led["ledger"])
    _flip_at_the_write(monkeypatch, led["box"], when="before")
    result = paper_ledger.commit_if_allowed(_probe(), load_risk())
    assert result["allow"] is False
    assert result["decision"]["reasons"] == [paper_ledger.SETTLEMENT_DAY_BOUNDARY]
    assert result["settlement_deferred"] == paper_ledger.SETTLEMENT_DAY_BOUNDARY
    assert _settlement_rows(led["ledger"]) == [] and _sha(led["ledger"]) == before
    assert len(ledger_store.read_ledger(led["ledger"])[1].rows) == 1  # only the seeded leg: no probe fill
    assert not halt_store.active_halts() and not result.get("halted")


def test_commit_path_mid_batch_crossing_keeps_row_one_defers_row_two_vetoes_probe(led, monkeypatch):
    """C-1's multi-row shape: row one persists on day 1, row two defers, and the probe still may not
    book against a day whose realized loss is one row short. The still-open second leg also carries its
    cost as an unrealized loss (no book: the worst mark), so RiskGate vetoes on its own here; the
    deferral rides along on the veto — never a halt, never a problem."""
    _seed(led)
    _add_second_market(led)
    _flip_at_the_write(monkeypatch, led["box"], when="after")
    result = paper_ledger.commit_if_allowed(_probe(), load_risk())
    assert result["allow"] is False
    assert result["settlement_deferred"] == paper_ledger.SETTLEMENT_DAY_BOUNDARY
    rows = _settlement_rows(led["ledger"])
    assert len(rows) == 1 and rows[0]["market"] == "m" and rows[0]["ts"].startswith("2026-09-01")
    assert len(ledger_store.read_ledger(led["ledger"])[1].rows) == 3  # two legs + one settlement: no fill
    assert not halt_store.active_halts() and not result.get("halted")


def test_commit_path_report_c_red_baseline_no_allow_over_cap_and_day_attribution(led, monkeypatch):
    """REPORT-C end to end: a realized $50 loss over the $30 cap must not become spendable on the write
    day, and the loss must be attributed to the day the row actually lands on."""
    _seed(led)
    cap = load_risk()["max_daily_loss_pct"] * 1000.0
    assert cap == pytest.approx(30.0) and 50.0 > cap  # the seeded loss (cost 50, pays 0) exceeds it
    # tick 1: the settlement write crosses the boundary -> deferred; the probe may not book
    _flip_at_the_write(monkeypatch, led["box"], when="before")
    first = paper_ledger.commit_if_allowed(_probe(), load_risk())
    assert first["allow"] is False and first["settlement_deferred"] == paper_ledger.SETTLEMENT_DAY_BOUNDARY
    assert _settlement_rows(led["ledger"]) == [] and not halt_store.active_halts() and not first.get("halted")
    # tick 2 (the next commit retries the settlement): wall clock on day 2, row lands on day 2
    second = paper_ledger.commit_if_allowed(_probe(), load_risk())
    rows = _settlement_rows(led["ledger"])
    assert len(rows) == 1 and rows[0]["ts"].startswith("2026-09-02")
    assert second["allow"] is False and "max_daily_loss" in second["decision"]["reasons"]
    # the write day's risk input sees the loss; the row's stamp day owns it, not the plan day
    assert paper_ledger.load_state("m", "no")["daily_loss_usd"] == pytest.approx(50.0)
    ledger_rows = ledger_store.read_ledger(led["ledger"])[1].rows
    assert daily_loss(ledger_rows, "2026-09-02", None).total_usd == pytest.approx(50.0)
    # a day-1 reader also counts the day-2 row (counts_today never skips a future-dated row), so no
    # evaluation day can be blind to the loss — the red hole was the write day reading 0.0
    assert daily_loss(ledger_rows, "2026-09-01", None).total_usd == pytest.approx(50.0)
