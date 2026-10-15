"""Issue #197 (Task CK): Poly预测's v3 vectors (tests/fixtures/poly_forecast_197_vectors_v3.json, v3.2, 46 vectors; WO-43: OQ-N7 global halt, then the #214 market block after the Operator resume).

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
SHA256_PREFIX = "0f1f210b"  # v3.3 (Task CK, #225 regen of WO-27/WO-43 + WO-27-ONTIME); v3.2 was 214f058e
DATA = json.loads(FIXTURE.read_text(encoding="utf-8"))
VECTORS = DATA["vectors"]
TOL = 1e-6


def test_the_vector_file_is_poly_forecasts_v3_3():
    # the committed (LF) bytes: a Windows checkout with core.autocrlf=true has CRLF in the working copy
    assert hashlib.sha256(FIXTURE.read_bytes().replace(b"\r\n", b"\n")).hexdigest().startswith(SHA256_PREFIX)
    assert DATA["version"] == "v3.3" and len(VECTORS) == 47


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
    return {"condition_id": res["condition_id"], "market": res.get("market"), "winning_token_id": res["winning_token_id"],
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
    monkeypatch.setattr(paper_ledger, "_LAST_EVAL_DAY", None)  # isolation only; it no longer decides ``late``
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


# WO-29 / WO-30 predate #195 (merged c8f796d: a long marks on its bid side alone). Their expectations are NOT
# hand-edited: Poly预测 is producing v3.3 vectors; strict xfail until then.
_AWAITING_V33 = {"WO-29", "WO-30"}
_VECTOR_PARAMS = [pytest.param(v, id=v["id"], marks=pytest.mark.xfail(
    strict=True, reason="awaiting v3.3 vectors (#195 semantics)")) if v["id"] in _AWAITING_V33
    else pytest.param(v, id=v["id"]) for v in VECTORS]


@pytest.mark.parametrize("vector", _VECTOR_PARAMS)
def test_poly_forecast_197_vector(env, vector):
    _run(env, vector)


def _run(env, vector, *, clock_ok: bool = True, check_parts: bool = True) -> dict[str, dict]:
    """Replay one vector; returns {t: {"late", "unrealized_usd", "daily_loss_usd"}} as computed by the repo."""
    seen: dict[str, dict] = {}
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
    blocked: set[str] = set()  # v3.2 OQ-N7 / INV-13: the #214 market block, survives the global resume
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
        if step.get("resolution") and not halted:  # under a halt nothing settles: the read is not kept (WO-43)
            resolutions.append(_resolution(step["resolution"], t))
        if step.get("operator_resume"):  # the Operator clears the global halt only; the market block stays
            assert halted and exp["operator_resume"] and any(h.startswith(exp["operator_resume"]) for h in halted), label
            halted = []
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
        assert sorted(blocked) == sorted(exp.get("settlement_blocked_markets") or []), label
        open_markets = market_close.held_markets(read.rows)
        skipped = sorted({r["market"] for r in resolutions if r.get("market") in blocked and r["market"] in open_markets
                          and r["snapshot_ts"] == t})
        assert skipped == sorted(exp.get("settlement_skipped_blocked") or []), (label, skipped)
        usable = [r for r in resolutions if r.get("market") not in blocked]
        if usable:
            planned = settlement.plan(read.rows, list(read.ids), usable, t)
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
            for reason in found.halts:  # #214: a contradicting market's settlement is blocked as well
                market = reason.rsplit(":", 1)[-1]
                if settlement.blocks_settlement(reason.split(":", 1)[0]) and market in reads:
                    blocked.add(market)
            assert exp["risk_state"] == "invalid_risk_state", (label, found.halts)
            assert any(h.startswith(exp["halt_reason"]) for h in found.halts), (label, found.halts, exp["halt_reason"])
            assert len(_distinct_writeoffs(read.rows)) == exp["ledger_writeoff_rows"], label
            continue
        assert exp["risk_state"] == "ok", (label, exp.get("halt_reason"))
        if found.closes:
            out = paper_ledger.apply_writeoffs(found.closes, t, books, clock_ok=clock_ok)
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
        # Poly风控 (#225 / Task CK): no "ran yesterday => on time" assumption and no override of ``late``: the
        # snapshot's lateness is src/day_boundary.day_start_late on the step's own time (the vectors model a
        # verified clock, clock_ok=True; a clock anomaly is covered by tests/test_da_day_start_clock.py).
        unrealized = paper_ledger.daily_unrealized(read.rows, list(read.ids), t, books, clock_ok=clock_ok)
        assert unrealized["problems"] == [], (label, unrealized["problems"])
        seen[t] = {"late": unrealized["late"], "unrealized_usd": unrealized["loss_usd"],
                   "daily_loss_usd": day.total_usd + unrealized["loss_usd"]}
        if not check_parts:
            continue
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
    if check_parts:
        assert bool(halted) == bool(expected.get("halted")), vector["id"]
    return seen


# --- Poly风控's exact #225 values (Task CK): late first evaluations, the on-time control, its clock anomaly ----

def _vector(vid: str) -> dict:
    return next(v for v in VECTORS if v["id"] == vid)


@pytest.mark.parametrize("vid, t, late, loss", [
    ("WO-27", "2026-10-14T13:00:00Z", True, 0.30),          # first evaluation at 13:00: late
    ("WO-43", "2026-10-13T09:00:00Z", True, 0.15),          # first evaluation at 09:00: late
    ("WO-27-ONTIME", "2026-10-14T00:00:30Z", False, 0.0),   # control: the harness writes it at 00:00
    ("WO-27-ONTIME", "2026-10-14T13:00:00Z", False, 0.0),
])
def test_poly_risk_225_exact_values(env, vid, t, late, loss):
    seen = _run(env, _vector(vid))
    assert seen[t]["late"] is late and seen[t]["unrealized_usd"] == pytest.approx(loss, abs=TOL), seen[t]
    exp = next(e for e in _vector(vid)["expected"]["steps"] if e["t"] == t)
    assert exp["parts"]["unrealized_usd"] == pytest.approx(loss, abs=TOL)  # the file carries the same value


def test_poly_risk_225_control_with_clock_anomaly_is_late_030(env):
    """The same 00:00 snapshot written with clock_ok=False is late: max(cost, previous day's mark) -> $0.30."""
    seen = _run(env, _vector("WO-27-ONTIME"), clock_ok=False, check_parts=False)
    for t in ("2026-10-14T00:00:30Z", "2026-10-14T13:00:00Z"):
        assert seen[t]["late"] is True and seen[t]["unrealized_usd"] == pytest.approx(0.30, abs=TOL), (t, seen[t])
