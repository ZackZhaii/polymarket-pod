"""Phase 0 paper ledger.

Simulated fills are written only after RiskGate.evaluate returns allow.
No CLOB authentication, no order POST, no withdraw, no size-up.
A single-leg fill is directional exposure, not risk-free arbitrage.
Positions are netted per leg (market + token_id); the tokens of an arb pair never
offset each other (backlog item 5, Task P).
Task AF (backlog #20): the ledger is read strictly, fills carry ids and are deduped, writes are
atomic and cash / positions are conservation-checked on every load and write (src/ledger_store.py).
Any integrity problem is an invalid risk state and halts booking.
Task AL (Sentinel design gap 7): invalid_risk_state and the Executor's ledger_integrity /
ledger_write_failed halt are now persisted global halts (src/halt_store.py): every later intent
vetoes until the Operator resumes it.
Issue #63: the CLI now checks the whole risk file (sha256 and every key, ``src.pins.refuse_start``)
right after argument parsing and before the config is read. A file that drifted before the run was
only caught by the five in-memory ``podlib.PINS`` RiskGate holds, so a changed max_open_positions
could still book a fill while the file stayed byte-identical during the run. Drift is refused with
the existing EXIT_FAILED and a recorded global halt; the two entry points (operator intent and the
default self-check) both stop there. ``evaluate(cfg)`` still takes an in-memory config.
Issue #56: the quotes history that RiskGate's structural wash check reads is read strictly
(``read_quotes``), for every ``load_state`` return path. The generic ``podlib.read_jsonl`` keeps its
loose behaviour for every other caller; here a quotes file that cannot be read in full is never a
short history: ``own_quotes`` is None and RiskGate vetoes the existing ``wash_input_invalid``.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.podlib import (  # noqa: E402
    BOOKS_PATH,
    MARKETS_PATH,
    PAPER_EQUITY_USD,
    PAPER_PATH,
    QUOTES_PATH,
    RISK_PATH,
    bus,
    load_risk,
    read_jsonl,
    utc_now,
)
from src import calibration_ids  # noqa: E402 - Task AX (M3 step 2)
from src import category  # noqa: E402 - issue #131: category buckets on the fill
from src.book_staleness import latest_by_time, parse_book_ts  # noqa: E402
from src.fees import booked_fee_usd, ledger_fee  # noqa: E402 - Task AE: one fee module
from src import ledger_store  # noqa: E402 - Task AF: ledger integrity
from src import sentinel  # noqa: E402 - Task AL: ledger halts and invalid_risk_state are global halts
from src.thresholds import ASSUMED_SLIPPAGE  # noqa: E402 - Task AT: the one assumed slippage
from src.ledger_store import (  # noqa: E402,F401 - Task AF: moved here, re-exported unchanged
    LEDGER_NUMBER_FIELDS,
    LedgerIntegrityError,
    leg_key,
    non_finite_ledger_rows,
)
from src.ledger_store import counts as _counts  # noqa: E402 - Task AF: moved, same rule
from src.risk_gate import Decision, Intent, evaluate  # noqa: E402
from src.daily_loss import daily_loss  # noqa: E402 - Task AR: fees + realized losses
from src.expiry_rule import hours_to_expiry, parse_end_date  # noqa: E402 - Task BA: hours at entry

DIRECTIONAL = "Single-leg fill is directional exposure, not risk-free arbitrage."


from src.thresholds import LEDGER_OPEN_EPS as OPEN_EPS  # noqa: E402 - Task AC


def leg_positions(rows: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, float]]:
    """Net shares per leg, plus exposure at the leg's average fill price.

    Only a buy and a sell of the same token offset each other (closing). The two
    tokens of an arb pair are separate legs and never offset each other, so a
    single filled leg is a full directional position (backlog item 5).
    Exposure is abs(net shares) * (gross notional / gross shares) over that leg's
    rows; a row without notional_usd is valued at 1 USD per share, the most a
    binary token can pay out.
    """
    legs: dict[tuple[str, str], dict[str, float]] = {}
    for row in rows:
        if not _counts(row):
            continue
        qty = float(row.get("shares") or 0.0)
        notional = row.get("notional_usd")
        gross_usd = abs(qty) * 1.0 if notional is None else abs(float(notional))
        leg = legs.setdefault(leg_key(row.get("market"), row.get("token_id")),
                              {"shares": 0.0, "gross_shares": 0.0, "gross_usd": 0.0})
        leg["shares"] += qty if row.get("side") == "buy" else -qty
        leg["gross_shares"] += abs(qty)
        leg["gross_usd"] += gross_usd
    for leg in legs.values():
        open_qty = abs(leg["shares"])
        if open_qty <= OPEN_EPS or leg["gross_shares"] <= 0:
            leg["exposure_usd"] = 0.0
        else:
            leg["exposure_usd"] = open_qty * leg["gross_usd"] / leg["gross_shares"]
    return legs


def read_quotes(path: Path) -> tuple[list[dict[str, Any]] | None, list[str]]:
    """Read the paper quotes file strictly (issue #56): the whole history, or nothing.

    ``podlib.read_jsonl`` silently skips a bad JSON or non-object line, so one damaged uncancelled
    opposite quote could turn the history into an empty one and let RiskGate's structural wash check
    (``src/wash_detect.py``) allow a fill. This reader is used only for the quotes boundary of
    ``load_state``; the generic reader keeps its behaviour for every other caller.

    Returns ``(rows, [])`` for a file that reads cleanly (a missing file, an empty file and blank
    lines stay an empty history, as before). Any other outcome is unusable: ``(None, problems)``
    with one entry per defect, naming the line number and the reason only (a line is never echoed).
    A defect is a non-empty line that is not one JSON object (bad or truncated JSON,
    ``malformed_row:<line>``; an array, null, number or string, ``non_object_row:<line>``), a last
    row without its closing newline (`unterminated_last_row:<line>`, the torn append of
    ``podlib.append_jsonl``), an unreadable file (``unreadable_quotes:<error>``) and bytes that are
    not UTF-8 (``undecodable_quotes``). No threshold, no halt: RiskGate vetoes the existing
    ``wash_input_invalid``, so nothing is booked.
    """
    try:
        data = path.read_bytes() if path.exists() else b""
    except OSError as exc:
        return None, [f"unreadable_quotes:{type(exc).__name__}"]
    if not data:
        return [], []
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None, ["undecodable_quotes"]
    lines = text.split("\n")
    rows: list[dict[str, Any]] = []
    problems: list[str] = []
    for number, line in enumerate(lines, start=1):
        line = line.rstrip("\r")
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            problems.append(f"malformed_row:{number}")
            continue
        if not isinstance(row, dict):
            problems.append(f"non_object_row:{number}")
            continue
        rows.append(row)
    if lines[-1].strip():
        problems.append(f"unterminated_last_row:{len(lines)}")
    if problems:
        return None, problems
    return rows, []


def load_state(market: str, token_id: str = "") -> dict[str, Any]:
    """Risk state for an intent on one leg (market + token_id).

    open_positions counts open legs, not markets. leg_already_open is True only
    when this exact token already has an open position; another leg of the same
    market does not count. exposure_usd is the gross sum over open legs. leg_net_shares is this
    leg's net shares (buys minus sells; issue #123: RiskGate reads its sign to tell an add from a
    reduction, and vetoes a same-direction add as add_to_open_leg).
    recent_fills (every ledger row) and own_quotes (every paper quote row) are the
    history RiskGate's structured wash / self-trade check reads (Task S).
    Task AF: rows come from the strict ledger reader, deduped by fill_id. Any integrity problem
    (parse, ids, Task U numbers, conservation) is listed in ledger_problems and gives the same
    invalid state as Task U, so RiskGate vetoes invalid_risk_state and nothing more is booked.
    Issue #68: a modern fill whose risk-control fields (hypothetical, counts_toward_risk,
    risk_decision) are not exactly valid is an integrity problem too, rejected before it is
    replayed into cash, positions or the daily loss.
    Issue #56: own_quotes comes from the strict quotes reader, once, for every return path. A
    quotes file that cannot be read in full is not a short history: own_quotes is None,
    quote_problems names the lines, and RiskGate vetoes the existing wash_input_invalid.
    """
    _, ledger = ledger_store.read_ledger(PAPER_PATH)
    rows = ledger.rows
    own_quotes, quote_problems = read_quotes(QUOTES_PATH)
    if quote_problems:
        own_quotes = None  # issue #56: a partial quote history is not this history (fail closed)
    bad_rows = non_finite_ledger_rows(rows)
    problems = ledger_store.verify(ledger)
    if bad_rows or problems:
        # Fail closed (Task U / AF): RiskGate vetoes invalid_risk_state; nothing is netted.
        return {
            "open_positions": None,
            "leg_already_open": False,
            "exposure_usd": None,
            "leg_exposure_usd": None,
            "leg_net_shares": None,
            "daily_loss_usd": None,
            "non_finite_ledger_rows": bad_rows,
            "ledger_problems": problems,
            "duplicate_fill_rows": ledger.duplicates_ignored,
            "recent_fills": rows,
            "own_quotes": own_quotes,
            "quote_problems": quote_problems,
        }
    legs = leg_positions(rows)
    # Task AR (backlog #4): today's booked fees plus today's gross realized losses (UTC day).
    day = daily_loss(rows, utc_now()[:10], ledger.ids)
    if day.problems:
        return {
            "open_positions": None,
            "leg_already_open": False,
            "exposure_usd": None,
            "leg_exposure_usd": None,
            "leg_net_shares": None,
            "daily_loss_usd": None,
            "daily_loss_problems": day.problems,
            "ledger_problems": list(day.problems),
            "duplicate_fill_rows": ledger.duplicates_ignored,
            "recent_fills": rows,
            "own_quotes": own_quotes,
            "quote_problems": quote_problems,
        }
    own = legs.get(leg_key(market, token_id))
    return {
        "open_positions": sum(1 for leg in legs.values() if abs(leg["shares"]) > OPEN_EPS),
        "leg_already_open": own is not None and abs(own["shares"]) > OPEN_EPS,
        "exposure_usd": sum(leg["exposure_usd"] for leg in legs.values()),
        "leg_exposure_usd": 0.0 if own is None else own["exposure_usd"],
        "leg_net_shares": 0.0 if own is None else own["shares"],  # issue #123: the open leg's direction
        "daily_loss_usd": day.total_usd,
        "daily_fees_usd": day.fees_usd,
        "daily_realized_loss_usd": day.realized_loss_usd,
        "daily_realized_gain_usd": day.realized_gain_usd,
        "ledger_problems": [],
        "duplicate_fill_rows": ledger.duplicates_ignored,
        "recent_fills": rows,
        "own_quotes": own_quotes,
        "quote_problems": quote_problems,
    }


def commit_if_allowed(intent: Intent, cfg: dict[str, Any]) -> dict[str, Any]:
    """Ask RiskGate. Append a paper fill only when the same call allows it.

    Task AF: the fill gets a fill_id and the post-fill cash / leg state, and is written atomically
    by ledger_store.book after the whole ledger is re-verified. An integrity problem halts (a halt
    bus message, allow False, nothing booked); a failed write leaves the old ledger intact.
    """
    state = load_state(intent.market, intent.token_id)
    decision = evaluate(intent, cfg, state)
    summary = _decision_payload(intent, decision)
    if not decision.allow:
        bus("RiskGate", "Executor", "veto", intent.market, summary)
        if "invalid_risk_state" in decision.reasons:
            # Task AL gap 7: an unusable ledger state is no longer a one-off veto but a global halt.
            problems = [str(p) for p in state.get("ledger_problems") or []]
            if state.get("non_finite_ledger_rows"):
                problems.append(f"non_finite_ledger_rows={state.get('non_finite_ledger_rows')}")
            sentinel.halt(reason="invalid_risk_state", market=intent.market, scope="global",
                          quotes_path=QUOTES_PATH, emit=_emit,
                          extra={"ledger_problems": problems, "strategy": intent.strategy,
                                 "token_id": intent.token_id})
            return {"allow": False, "decision": summary, "halted": True, "halt_reason": "invalid_risk_state",
                    "ledger_problems": problems}
        veto: dict[str, Any] = {"allow": False, "decision": summary}
        # Issue #56: a quotes file that could not be read in full shows up as wash_input_invalid.
        # Name its per-line problems on the veto too (diagnosis only; nothing is booked either way).
        if state.get("quote_problems"):
            veto["quote_problems"] = list(state["quote_problems"])
        return veto

    summary["decision"] = "allow"
    bus("RiskGate", "Executor", "signal", intent.market, summary)
    # Re-check the in-memory decision. A bus line is not enough on its own.
    if decision.allow is not True:
        raise RuntimeError("refusing fill without a fresh RiskGate allow")
    effective_price = round(decision.effective_price, 8)
    notional_usd = round(decision.notional_usd, 8)
    shares = round(decision.shares, 8)
    # Task AF: the booked fee is also never below ledger_fee recomputed from the values the row
    # stores (they are rounded), so the conservation check can recompute it exactly. Higher wins.
    stored_fee = ledger_fee(intent.fee_rate, intent.market_price, effective_price, notional_usd, shares)
    fee_usd = booked_fee_usd(decision.fee_usd)
    if stored_fee is not None:
        fee_usd = max(fee_usd, booked_fee_usd(stored_fee))
    fill = {
        "fill_id": ledger_store.new_fill_id(),
        "ts": utc_now(),
        "strategy": intent.strategy,
        "market": intent.market,
        "token_id": intent.token_id,
        "side": intent.side,
        "model_prob": intent.model_prob,
        "market_price": intent.market_price,
        "edge": round(decision.edge, 8),
        "edge_formula": decision.formula,
        "assumed_slippage": intent.assumed_slippage,
        "fee_rate": intent.fee_rate,
        "fee_usd": fee_usd,
        "effective_price": effective_price,
        "notional_usd": notional_usd,
        "shares": shares,
        "spread": intent.spread,
        "liquidity_usd": intent.liquidity_usd,
        "book_ts": intent.book_ts,
        "hypothetical": intent.hypothetical,
        "counts_toward_risk": not intent.hypothetical,
        "risk_decision": "allow",
        "note": f"{DIRECTIONAL} {intent.note}".strip(),
    }
    # Task AX (M3 step 2): identity fields, then the join check. A fill that cannot be joined is still
    # booked (RiskGate already decided; nothing here changes that) but says why in calibration_problems.
    fill.update(identity_fields(intent))
    # Task BA: market context at commit (mid, top of book, hours to expiry, shadow Kelly multiplier).
    # A value that cannot be computed stays None and is named in calibration_problems.
    market, market_problems = market_fields(intent, fill["ts"])
    fill.update(market)
    # Task BC: which path built the intent (intent_builder / operator_cli / gate_check), so the calibration
    # report can tell a fill without book context from one with genuinely missing data.
    context, context_problems = fill_context(intent)
    fill["fill_context"] = context
    # Task BF: the market's Polymarket event, for calibration clustering. None when unknown or unusable;
    # never a calibration problem (the report falls back to condition_id).
    fill.update(event_fields(intent))
    # Issue #131: the category buckets RiskGate classified the market into (for M3 calibration grouping).
    fill["category_buckets"] = category.bucket_label(intent.tags)
    fill["calibration_problems"] = sorted(set(calibration_ids.fill_problems(fill)) | set(market_problems)
                                          | set(context_problems))
    try:
        fill = ledger_store.book(PAPER_PATH, fill)
    except LedgerIntegrityError as exc:
        return _halt(intent, summary, "ledger_integrity", exc.problems)
    except OSError as exc:
        return _halt(intent, summary, "ledger_write_failed", [f"{type(exc).__name__}: {exc}"])
    bus("Executor", "Sentinel", "fill", intent.market, fill)
    return {"allow": True, "fill": fill}


def _halt(intent: Intent, summary: dict[str, Any], reason: str, problems: list[str]) -> dict[str, Any]:
    """Task AF: no fill. Report a halt (paper only, no exchange action) and return allow False.

    Task AL gap 7: the halt is also persisted as a global halt (Sentinel's halt path, written first)."""
    sentinel.halt(reason=reason, market=intent.market, scope="global", source="Executor", to="Sentinel",
                  quotes_path=QUOTES_PATH, emit=_emit, extra={"ledger_problems": problems, "strategy": intent.strategy, "token_id": intent.token_id})
    decision = dict(summary, decision="halt")
    return {"allow": False, "halted": True, "halt_reason": reason, "ledger_problems": problems,
            "decision": decision}


def _emit(*args: Any, **kwargs: Any) -> Any:
    """This module's bus writer, looked up at call time (callers and tests may replace ``bus``)."""
    return bus(*args, **kwargs)


def identity_fields(intent: Intent) -> dict[str, Any]:
    """Task AX: the calibration identity of a fill (calibration_ids.FILL_ID_FIELDS), from the intent.

    decision_id is the intent's own when given (it must then match the derivation, or the fill is
    flagged decision_id_mismatch); otherwise derived from (strategy, market, token_id, forecast_id),
    or None when any part is missing. Nothing is invented.
    """
    decision_id = intent.decision_id or calibration_ids.make_decision_id(
        intent.strategy, intent.market, intent.token_id, intent.forecast_id)
    return {
        "decision_id": decision_id,
        "forecast_id": intent.forecast_id,
        "forecast_ts": intent.forecast_ts,
        "condition_id": intent.condition_id,
        "outcome_index": intent.outcome_index,
        "outcome": intent.outcome,
        "category": intent.category,
        "tags": None if intent.tags is None else list(intent.tags),
        "end_date": intent.end_date,
        "model_version": intent.model_version,
    }


def event_fields(intent: Intent) -> dict[str, Any]:
    """Task BF: calibration_ids.FILL_EVENT_FIELDS from the intent, each calibration_ids.event_value
    (a non-blank string, or None). Never raises, never a problem, never read by RiskGate."""
    return {"event_id": calibration_ids.event_value(getattr(intent, "event_id", None)),
            "event_slug": calibration_ids.event_value(getattr(intent, "event_slug", None))}


def _price(value: object) -> tuple[float | None, str | None]:
    """A top-of-book price: a finite number strictly between 0 and 1 (bool and str refused)."""
    if value is None:
        return None, "missing"
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value < 1:
        return None, "invalid"
    return float(value), None


def market_fields(intent: Intent, fill_ts: str) -> tuple[dict[str, Any], list[str]]:
    """Task BA: calibration_ids.FILL_MARKET_FIELDS for a fill committed at ``fill_ts``, and their problems.

    best_bid / best_ask: the intent's own top of book (the data RiskGate's caller already had), each a
    finite price in (0, 1), else None (missing_ / invalid_<field>). market_mid = (bid + ask) / 2 when
    both are usable and not crossed (bid <= ask); else None (missing_market_mid if a side is unusable,
    invalid_market_mid if crossed). hours_to_expiry_at_entry = end_date - fill ts in hours (negative
    when already past, as computed), None if end_date is missing or not a zoned ISO time.
    kelly_fraction_used: intent_builder's shadow Kelly multiplier as given, a finite number in (0, 1],
    else None. Task BC: kelly_frac_raw, intent_builder's per-trade raw (full) Kelly fraction, same rule.
    Recorded only: nothing reads these for a decision or a size. Never invented.
    """
    problems: list[str] = []
    bid, bid_problem = _price(intent.best_bid)
    ask, ask_problem = _price(intent.best_ask)
    for name, problem in (("best_bid", bid_problem), ("best_ask", ask_problem)):
        if problem:
            problems.append(f"{problem}_{name}")
    mid = None
    if bid is None or ask is None:
        problems.append("missing_market_mid")
    elif bid > ask:
        problems.append("invalid_market_mid")
    else:
        mid = round((bid + ask) / 2, 8)
    hours = None
    if intent.end_date is None:
        problems.append("missing_hours_to_expiry_at_entry")
    else:
        now = parse_end_date(fill_ts)
        hours = hours_to_expiry(intent.end_date, now) if now is not None else None
        if hours is None or not math.isfinite(hours):
            hours = None
            problems.append("invalid_hours_to_expiry_at_entry")
        else:
            hours = round(hours, 6)
    kelly, kelly_problem = _fraction(intent.kelly_fraction_used)
    if kelly_problem:
        problems.append(f"{kelly_problem}_kelly_fraction_used")
    kelly_raw, raw_problem = _fraction(intent.kelly_frac_raw)
    if raw_problem:
        problems.append(f"{raw_problem}_kelly_frac_raw")
    return ({"market_mid": mid, "best_bid": bid, "best_ask": ask, "hours_to_expiry_at_entry": hours,
             "kelly_fraction_used": kelly, "kelly_frac_raw": kelly_raw}, problems)


def _fraction(value: object) -> tuple[float | None, str | None]:
    """A Kelly fraction: a finite number in (0, 1] (bool and str refused); else None and missing / invalid."""
    if value is None:
        return None, "missing"
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= 1:
        return None, "invalid"
    return float(value), None


def fill_context(intent: Intent) -> tuple[str | None, list[str]]:
    """Task BC: the fill's fill_context (calibration_ids.FILL_CONTEXTS) as the caller stamped it on the intent.

    Never inferred from missing fields: None -> missing_fill_context, an unknown value -> None +
    invalid_fill_context. Recorded only; RiskGate never reads it.
    """
    value = intent.fill_context
    if value is None:
        return None, ["missing_fill_context"]
    if value not in calibration_ids.FILL_CONTEXTS:
        return None, ["invalid_fill_context"]
    return value, []


def _decision_payload(intent: Intent, decision: Decision) -> dict[str, Any]:
    return {
        "forecast_id": intent.forecast_id,  # Task AX: joins a veto or allow back to its sealed forecast
        "strategy": intent.strategy,
        "model_prob": intent.model_prob,
        "market_price": intent.market_price,
        "edge": round(decision.edge, 8),
        "edge_formula": decision.formula,
        "assumed_slippage": decision.assumed_slippage,
        "fee_rate": decision.fee_rate,
        "fee_usd": booked_fee_usd(decision.fee_usd),
        "reasons": decision.reasons,
        "hypothetical": intent.hypothetical,
        "risk_decision": "allow" if decision.allow else "veto",
        "note": DIRECTIONAL,
    }


def _intent(**kwargs: Any) -> Intent:
    defaults: dict[str, Any] = {
        "strategy": "phase0-gate-test",
        "market": "phase0-self-check",
        "side": "buy",
        "model_prob": 0.5,
        "market_price": 0.5,
        "assumed_slippage": ASSUMED_SLIPPAGE,
        "fee_rate": 0.0,
        "liquidity_usd": 20000.0,
        "spread": 0.01,
        "token_id": "",
        "hypothetical": True,
        "note": "Gate test only. Not a forecast and not a claim of edge.",
        "fill_context": calibration_ids.FILL_CONTEXT_GATE_CHECK,  # Task BC: no book context
        # Issue #131: the synthetic gate-test market's tag, so only the check under test can veto.
        "tags": ("phase0-gate-test",),
        # Synthetic gate-test touch, stamped now so only the check under test can veto.
        "book_ts": utc_now(),
    }
    defaults.update(kwargs)
    return Intent(**defaults)


def run_self_check(cfg: dict[str, Any]) -> dict[str, Any]:
    """Prove vetoes fire, and that one labeled hypothetical fill needs allow.

    The hypothetical allow is checked against the real fills' open positions and daily
    loss like any other intent (Task M), so a full book vetoes it.
    """
    before = len(read_jsonl(PAPER_PATH))
    drifted = dict(cfg)
    drifted["live_enabled"] = True
    pin_result = commit_if_allowed(
        _intent(market="phase0-self-check:pin", note="In-memory pin drift. File not modified."),
        drifted,
    )
    no_edge = commit_if_allowed(
        _intent(
            market="phase0-self-check:no-edge",
            model_prob=0.50,
            market_price=0.50,
            note="Model probability equals the touch. Costs make edge non-positive.",
        ),
        cfg,
    )
    wide = commit_if_allowed(
        _intent(
            market="phase0-self-check:spread",
            model_prob=0.80,
            market_price=0.40,
            spread=0.20,
            note="Spread wider than max_spread.",
        ),
        cfg,
    )
    banned = commit_if_allowed(
        _intent(
            strategy="wash-probe",
            market="phase0-self-check:banned",
            model_prob=0.80,
            market_price=0.40,
            hypothetical=False,
            note="Banned behavior must not fill.",
        ),
        cfg,
    )
    allowed = commit_if_allowed(
        _intent(
            market="phase0-self-check:hypothetical-allow",
            model_prob=0.70,
            market_price=0.40,
            note="Hypothetical probability to prove the allow path. Not a forecast.",
        ),
        cfg,
    )
    after = len(read_jsonl(PAPER_PATH))
    return {
        "pin_blocked": not pin_result["allow"],
        "no_edge_blocked": not no_edge["allow"],
        "wide_spread_blocked": not wide["allow"],
        "banned_blocked": not banned["allow"],
        "hypothetical_allowed": allowed["allow"],
        "new_fills": after - before,
        "allowed": allowed,
    }


def realistic_no_edge(cfg: dict[str, Any]) -> dict[str, Any] | None:
    """Use a public book price with model_prob == price. Must veto. Not a forecast.

    Task T: candidates are the latest row per leg by the recorder's parsed ``ts`` (times, not
    strings), newest first; rows with a missing or unparseable ``ts`` are skipped and returned in
    ``skipped_rows``. Book age is the exchange's ``book_ts`` on the row, never ``ts``; a row
    without one reaches RiskGate as book_ts=None and vetoes missing_book_ts (fail closed).
    """
    books = read_jsonl(BOOKS_PATH)
    markets = {str(row.get("id")): row for row in read_jsonl(MARKETS_PATH)}
    latest, skipped = latest_by_time(
        books, lambda row: (str(row.get("market_id") or ""), str(row.get("token_id") or ""))
    )
    ordered = sorted(
        latest.values(), key=lambda row: parse_book_ts(row.get("ts")), reverse=True  # type: ignore[arg-type,return-value]
    )
    fallback = None
    chosen = None
    for book in ordered:
        ask = book.get("best_ask")
        spread = book.get("spread")
        if not isinstance(ask, (int, float)) or not isinstance(spread, (int, float)):
            continue
        if not 0.0 < float(ask) < 1.0:
            continue
        market = markets.get(str(book.get("market_id")))
        liquidity = None if market is None else market.get("liquidity_usd")
        if not isinstance(liquidity, (int, float)):
            continue
        candidate = (book, float(liquidity))
        if fallback is None:
            fallback = candidate
        # Prefer a touch that stays inside (0, 1) after the default slippage.
        if 0.0 < float(ask) + ASSUMED_SLIPPAGE < 1.0:
            chosen = candidate
            break
    if chosen is None:
        chosen = fallback
    if chosen is None:
        return None
    book, liquidity = chosen
    result = commit_if_allowed(
        Intent(
            strategy="no-edge-check",
            market=str(book.get("market_id") or ""),
            side="buy",
            model_prob=float(book["best_ask"]),
            market_price=float(book["best_ask"]),
            assumed_slippage=ASSUMED_SLIPPAGE,
            fee_rate=0.0,
            liquidity_usd=float(liquidity),
            spread=float(book["spread"]),
            token_id=str(book.get("token_id") or ""),
            hypothetical=False,
            # The exchange's own book time (Task R/T), never the recorder's ts.
            book_ts=book.get("book_ts"),
            # Task BA: the same book row's top of book (recorded on the fill only; RiskGate never reads it).
            best_bid=book.get("best_bid"),
            best_ask=book.get("best_ask"),
            fill_context=calibration_ids.FILL_CONTEXT_GATE_CHECK,  # Task BC: a gate check, never a sample
            note="Public ask used as both model and price. Must not fill. Not a forecast.",
        ),
        cfg,
    )
    result["skipped_rows"] = skipped
    return result


def run_cli_intent(args: argparse.Namespace, cfg: dict[str, Any]) -> dict[str, Any]:
    intent = Intent(
        strategy=args.strategy,
        market=args.market,
        side=args.side,
        model_prob=args.model_prob,
        market_price=args.market_price,
        assumed_slippage=args.slippage,
        fee_rate=args.fee_rate,
        liquidity_usd=args.liquidity,
        spread=args.spread,
        token_id=args.token_id or "",
        hypothetical=False,
        book_ts=args.book_ts,
        note=args.note or "Operator paper intent. Not a promise of profit.",
        fill_context=calibration_ids.FILL_CONTEXT_OPERATOR_CLI,  # Task BC: no book context
        # Issue #131: the market's Gamma tag slugs; none given vetoes missing_category.
        tags=tuple(t.strip() for t in (getattr(args, "tags", None) or "").split(",") if t.strip()) or None,
    )
    return commit_if_allowed(intent, cfg)


def slippage_arg(text: str) -> float:
    """Task AU: --slippage must be a finite number >= ASSUMED_SLIPPAGE (the floor RiskGate also enforces)."""
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
    if not math.isfinite(value):
        raise argparse.ArgumentTypeError(f"must be finite: {text!r}")
    if value < ASSUMED_SLIPPAGE:
        raise argparse.ArgumentTypeError(f"{value} is below the slippage floor ASSUMED_SLIPPAGE={ASSUMED_SLIPPAGE}")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Phase 0 paper ledger. RiskGate required. No live orders.")
    parser.add_argument("--strategy", default="")
    parser.add_argument("--market", default="")
    parser.add_argument("--side", choices=("buy", "sell"), default="buy")
    parser.add_argument("--model-prob", type=float, default=None)
    parser.add_argument("--market-price", type=float, default=None)
    parser.add_argument("--slippage", type=slippage_arg, default=ASSUMED_SLIPPAGE,
                        help=f"Assumed slippage, price units per share; at least ASSUMED_SLIPPAGE={ASSUMED_SLIPPAGE}.")
    parser.add_argument("--fee-rate", type=float, default=0.0)
    parser.add_argument("--liquidity", type=float, default=None)
    parser.add_argument("--spread", type=float, default=None)
    parser.add_argument("--token-id", default="")
    parser.add_argument(
        "--book-ts",
        default=None,
        help="UTC ISO time of the book snapshot, e.g. 2026-10-09T01:00:00Z. Missing or older than 120 s vetoes.",
    )
    parser.add_argument("--tags", default=None,
                        help="comma-separated Gamma tag slugs of the market (issue #131: none vetoes missing_category)")
    parser.add_argument("--note", default="")
    parser.add_argument(
        "--self-check",
        action="store_true",
        help="Run gate tests. This is the default when no operator intent is given.",
    )
    return parser


EXIT_OK = 0
# Issue #63: also the refusal when config/risk.toml is off its file pins at start (global halt recorded).
EXIT_FAILED = 1  # halted (ledger integrity / write), risk file pin drift, risk.toml changed during the run, or self-check failed
EXIT_USAGE = 2


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return EXIT_OK if exc.code == 0 else EXIT_USAGE
    from src.pins import refuse_start  # issue #63: the risk file pins (sha256 + every key) before any read

    if refuse_start("paper_ledger"):
        return EXIT_FAILED
    before_bytes = RISK_PATH.read_bytes()
    cfg = load_risk()
    live = cfg.get("live_enabled")
    print(
        f"paper_equity_usd={PAPER_EQUITY_USD} mode={cfg.get('mode')} "
        f"live_enabled={str(live).lower()} require_human_gate={str(cfg.get('require_human_gate')).lower()} "
        f"max_order_pct={cfg.get('max_order_pct')} max_daily_loss_pct={cfg.get('max_daily_loss_pct')} "
        "orders=none"
    )
    operator_intent = args.model_prob is not None or args.market_price is not None or bool(args.strategy)
    if operator_intent:
        missing = [
            name
            for name, value in (
                ("strategy", args.strategy),
                ("market", args.market),
                ("model_prob", args.model_prob),
                ("market_price", args.market_price),
                ("liquidity", args.liquidity),
                ("spread", args.spread),
            )
            if value is None or value == ""
        ]
        if missing:
            print("missing operator fields: " + ", ".join(missing), file=sys.stderr)
            return EXIT_USAGE
        result = run_cli_intent(args, cfg)
        label = "ALLOW" if result["allow"] else "HALT" if result.get("halted") else "VETO"
        print(label, result.get("fill") or result.get("decision"))
        if result.get("halted"):
            print(f"paper ledger halted: {result['halt_reason']} {result['ledger_problems']}", file=sys.stderr)
            return EXIT_FAILED
        if RISK_PATH.read_bytes() != before_bytes:
            print("risk.toml changed during paper run; refusing", file=sys.stderr)
            return EXIT_FAILED
        return EXIT_OK if result["allow"] or result.get("decision", {}).get("reasons") else EXIT_FAILED

    report = run_self_check(cfg)
    realistic = realistic_no_edge(cfg)
    print(
        "self_check "
        f"pin_blocked={report['pin_blocked']} "
        f"no_edge_blocked={report['no_edge_blocked']} "
        f"wide_spread_blocked={report['wide_spread_blocked']} "
        f"banned_blocked={report['banned_blocked']} "
        f"hypothetical_allowed={report['hypothetical_allowed']} "
        f"new_fills={report['new_fills']}"
    )
    if report["hypothetical_allowed"]:
        fill = report["allowed"]["fill"]
        print(
            "hypothetical_fill "
            f"strategy={fill['strategy']} model_prob={fill['model_prob']} "
            f"market_price={fill['market_price']} edge={fill['edge']} "
            f"assumed_slippage={fill['assumed_slippage']} fee_usd={fill['fee_usd']}"
        )
    if realistic is None:
        print("realistic_no_edge=skipped no complete public book yet")
    else:
        reasons = realistic.get("decision", {}).get("reasons")
        print(f"realistic_no_edge_blocked={not realistic['allow']} reasons={reasons}")
    unchanged = RISK_PATH.read_bytes() == before_bytes
    print(f"risk_toml_unchanged={str(unchanged).lower()} path={RISK_PATH}")
    ok = (
        report["pin_blocked"]
        and report["no_edge_blocked"]
        and report["wide_spread_blocked"]
        and report["banned_blocked"]
        and report["hypothetical_allowed"]
        and report["new_fills"] == 1
        and unchanged
        and (realistic is None or realistic["allow"] is False)
    )
    if not ok:
        print("paper ledger self-check failed", file=sys.stderr)
        return EXIT_FAILED
    print(DIRECTIONAL)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
