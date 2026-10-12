"""Issue #197 (Task CK): Poly预测's v3 vectors (tests/fixtures/poly_forecast_197_vectors_v3.json, 44 vectors).

Each vector is replayed tick by tick through the production code, in Poly风控's tick order
(issuecomment-6106669720): restart (pending reads dropped, write-off events re-sent) -> ledger verify (a halt ends
the tick) -> settlement (src/settlement.plan + ledger_store.book) -> close detection and write-off
(src/market_close.detect, paper_ledger.apply_writeoffs) -> daily loss (src/daily_loss + paper_ledger.daily_unrealized
with the vector's books). Confirmation is the Gamma re-read only (``expected_reread_only`` where a vector has one:
the CLOB / Data second source is not allowlisted). The vector's proposed event_id (sha of condition|market|token) is
not asserted: the key here is the leg (market|token); keys are compared by leg_key.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

import pytest

from src import daily_loss as daily_loss_mod
from src import halt_store, ledger_store, market_close, paper_ledger, settlement

FIXTURE = Path(__file__).parent / "fixtures" / "poly_forecast_197_vectors_v3.json"
SHA256_PREFIX = "23626341"
DATA = json.loads(FIXTURE.read_text(encoding="utf-8"))
VECTORS = DATA["vectors"]
TOL = 1e-6


def test_the_vector_file_is_poly_forecasts_v3():
    # the committed (LF) bytes: a Windows checkout with core.autocrlf=true has CRLF in the working copy
    assert hashlib.sha256(FIXTURE.read_bytes().replace(b"\r\n", b"\n")).hexdigest().startswith(SHA256_PREFIX)
    assert DATA["version"] == "v3" and len(VECTORS) == 44


def _dt(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def _fill(row: dict) -> dict:
    return {"fill_id": row["fill_id"], "ts": row["ts"], "market": row["market"], "token_id": row["token_id"],
            "condition_id": row.get("condition_id"), "side": row["side"], "shares": row["shares"],
            "notional_usd": row["notional_usd"], "fee_rate": 0.0, "market_price": row["effective_price"],
            "effective_price": row["effective_price"], "fee_usd": row["fee_usd"], "hypothetical": False,
            "counts_toward_risk": True, "risk_decision": "allow"}


def _observation(market: str, read: object, now: datetime) -> market_close.Observation:
    if isinstance(read, dict) and "_error" in read:
        return market_close.failed(market, now, f"gamma_read_failed:{read['_error']}")
    if not isinstance(read, dict):  # [] : unknown
        return market_close.failed(market, now, "gamma_market_missing")
    return market_close.observation_from_row(market, read, now)


def _resolution(res: dict, t: str) -> dict:
    tokens = list(res["outcome_token_ids"])
    return {"condition_id": res["condition_id"], "winning_token_id": res["winning_token_id"],
            "outcome_token_ids": tokens,
            "outcome_prices_final": [1.0 if tok == res["winning_token_id"] else 0.0 for tok in tokens],
            "closed_ts": res["closed_ts"], "snapshot_ts": t, "status": res.get("status", "resolved"),
            "uma_checked": res.get("uma_checked", True)}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(paper_ledger, "PAPER_PATH", tmp_path / "paper.jsonl")
    monkeypatch.setattr(paper_ledger, "QUOTES_PATH", tmp_path / "quotes.jsonl")
    monkeypatch.setattr(paper_ledger, "MARK_BOOKS_PATH", tmp_path / "books.jsonl")
    monkeypatch.setattr(paper_ledger, "DAILY_MARKS_DIR", tmp_path / "daily_marks")
    monkeypatch.setattr(paper_ledger, "SETTLEMENT_RESOLUTIONS_PATH", tmp_path / "resolutions.jsonl")
    monkeypatch.setattr(paper_ledger, "_LAST_EVAL_DAY", None)
    monkeypatch.setattr(paper_ledger, "bus", lambda *args, **kwargs: None)
    monkeypatch.setattr(halt_store, "HALT_STATE_PATH", tmp_path / "halt_state.json")
    monkeypatch.setattr(halt_store, "HALT_FALLBACK_PATH", tmp_path / "HALT")
    return tmp_path / "paper.jsonl"


def _seed_writeoffs(path: Path, seeds: list[dict]) -> None:
    first: dict[str, dict] = {}
    for seed in seeds:
        src = seed["row"]
        market, token = src["market"], src["token_id"]
        row = {"fill_id": market_close.writeoff_fill_id(market, token), "ts": src["ts"], "kind": "writeoff",
               "strategy": "writeoff", "market": market, "token_id": token, "side": "sell", "shares": 0.0,
               "market_price": 0.0, "effective_price": 0.0, "notional_usd": 0.0, "fee_rate": 0.0, "fee_usd": 0.0,
               "hypothetical": False, "counts_toward_risk": True, "risk_decision": "writeoff",
               "writeoff_shares": src["shares"], "writeoff_usd": src["cost_basis_usd"],
               "closed_ts": src["evidence"]["closed_time"], "close_first_seen_ts": src["evidence"]["first_read_t"],
               "close_confirmed_ts": src["evidence"]["confirm_t"], "close_confirmed_by": "gamma_reread"}
        key = row["fill_id"]
        if key not in first:
            first[key] = ledger_store.book(path, row)
            continue
        line = dict(first[key])
        line["writeoff_usd"] = src["cost_basis_usd"]  # same content: an exact duplicate; else a conflict
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, sort_keys=True) + "\n")


def _distinct_writeoffs(rows: list[dict]) -> set[str]:
    return {f"{r['market']}|{r['token_id']}" for r in rows if ledger_store.is_writeoff(r)}


@pytest.mark.parametrize("vector", VECTORS, ids=[v["id"] for v in VECTORS])
def test_poly_forecast_197_vector(env, vector):
    expected = vector.get("expected_reread_only") or vector["expected"]
    inputs = vector["inputs"]
    fills = sorted(inputs["ledger"], key=lambda r: r["ts"])
    seeds = list(inputs.get("ledger_writeoff_rows") or [])
    tracker = market_close.CloseTracker()
    books: dict[tuple[str, str], dict] = {}
    resolutions: list[dict] = []
    halted: list[str] = []
    resend = True  # a process start
    bus_legs: set[str] = set()
    settled_markets: set[str] = set()
    for step, exp in zip(inputs["steps"], expected["steps"], strict=True):
        t, now = step["t"], _dt(step["t"])
        label = f"{vector['id']} @ {t}"
        while fills and fills[0]["ts"] <= t:
            ledger_store.book(env, _fill(fills.pop(0)))
        if seeds:
            _seed_writeoffs(env, seeds)
            seeds = []
        if step.get("restart"):
            tracker, resend = market_close.CloseTracker(), True
        for key, book in (step.get("books") or {}).items():
            market, token = key.split("|", 1)
            if book is None:
                books.pop((market, token), None)
            else:
                books[(market, token)] = book
        if step.get("resolution"):
            resolutions.append(_resolution(step["resolution"], t))
        _, read = ledger_store.read_ledger(env)
        problems = ledger_store.verify(read)
        if problems and not halted:
            halted = [f"ledger:{p}" for p in problems]
        if halted:
            assert exp["risk_state"] == "invalid_risk_state", (label, halted)
            assert exp["daily_loss_usd"] is None, label
            assert len(_distinct_writeoffs(read.rows)) == exp["ledger_writeoff_rows"], label
            continue
        if resend:  # at least once: every written-off unsettled leg again after a start (consumers dedupe)
            bus_legs |= {f"{m}|{tok}" for m, tok in market_close.written_off_legs(read.rows)}
            resend = False
        # 3) settlement first
        settled_now: set[str] = set()
        if resolutions:
            planned = settlement.plan(read.rows, list(read.ids), resolutions, t)
            assert planned.problems == [], (label, planned.problems)
            for row in planned.rows:
                ledger_store.book(env, row)
                settled_now.add(row["market"])
            _, read = ledger_store.read_ledger(env)
        # 4) close detection + write-off
        reads = {m: _observation(m, g, now) for m, g in (step.get("gamma") or {}).items()}
        held = market_close.held_markets(read.rows)
        found = market_close.detect(tracker, read.rows, {m: o for m, o in reads.items() if m in held}, now)
        if found.halts:
            halted = found.halts
            assert exp["risk_state"] == "invalid_risk_state", (label, found.halts)
            assert any(h.startswith(exp["halt_reason"]) for h in found.halts), (label, found.halts, exp["halt_reason"])
            assert len(_distinct_writeoffs(read.rows)) == exp["ledger_writeoff_rows"], label
            continue
        assert exp["risk_state"] == "ok", (label, exp.get("halt_reason"))
        if found.closes:
            out = paper_ledger.apply_writeoffs(found.closes, t)
            assert out["problems"] == [], (label, out["problems"])
            if not step.get("crash_after_ledger_write"):
                bus_legs |= {f"{r['market']}|{r['token_id']}" for r in out["booked"]}
            else:
                resend = False  # the process died: the next start (restart) re-sends
            _, read = ledger_store.read_ledger(env)
        reasons = dict(found.reasons)
        for market in reads:
            if market in held:
                continue
            if market in settled_now:
                reasons[market] = "settled_no_writeoff"
            elif market in settled_markets and any(ledger_store.is_writeoff(r) and r["market"] == market
                                                   for r in read.rows):
                reasons[market] = "settled_polling_stopped"  # written off, then settled: polling ended
            else:
                reasons[market] = "no_open_leg"
        settled_markets |= settled_now
        assert reasons == exp["close_reasons"], label
        assert len(_distinct_writeoffs(read.rows)) == exp["ledger_writeoff_rows"], label
        assert ledger_store.verify(read) == [], label
        # bus: every required key (by leg) has an event by the end of this tick
        required = {e["leg_key"] for e in expected.get("writeoff_events", [])
                    if e["event_id"] in set(exp.get("bus_keys_required_by_now") or [])}
        assert required <= bus_legs, (label, required - bus_legs)
        # 5) daily loss
        day = daily_loss_mod.daily_loss(read.rows, t[:10], list(read.ids))
        assert day.problems == [], (label, day.problems)
        if t[:10] not in set(inputs.get("late_days") or []) and paper_ledger._LAST_EVAL_DAY != t[:10]:
            paper_ledger._LAST_EVAL_DAY = paper_ledger._previous_day(t[:10])  # the runner ran yesterday: not late
        unrealized = paper_ledger.daily_unrealized(read.rows, list(read.ids), t, books)
        assert unrealized["problems"] == [], (label, unrealized["problems"])
        parts = exp["parts"]
        got = {"fees_usd": day.fees_usd, "realized_loss_usd": day.realized_loss_usd - day.writeoff_usd,
               "writeoff_usd": day.writeoff_usd, "unrealized_usd": unrealized["loss_usd"]}
        for name, value in parts.items():
            assert got[name] == pytest.approx(value, abs=TOL), (label, name, got, parts)
        total = day.total_usd + unrealized["loss_usd"]
        assert total == pytest.approx(exp["daily_loss_usd"], abs=TOL), (label, got)
        assert day.realized_gain_usd == pytest.approx(exp["realized_gain_usd"], abs=TOL), label
        open_legs = sorted(f"{m}|{tok}" for m, tok in market_close.open_legs(read.rows))
        assert open_legs == sorted(exp["occupancy"]["legs"]) and len(open_legs) == exp["occupancy"]["count"], label
    assert bool(halted) == bool(expected.get("halted")), vector["id"]
