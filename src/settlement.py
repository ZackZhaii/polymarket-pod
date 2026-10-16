"""Issue #126 (Task CG, F2b): book resolved markets' open legs as settlement rows. Paper only, offline.

The paper ledger had no settlement: a losing leg stayed open forever. With Task CF (#124) a closed market's
leg has no book, so it was valued at 0 against its cost on every UTC day, and the same loss counted again
each day. Settling it closes the leg once:

* only a confirmed snapshot in data/resolutions.jsonl settles (``status`` resolved and ``uma_checked``
  true; read strictly with ``resolutions.read_history``). An unconfirmed (closed only) snapshot settles
  nothing and is not read as a 0 loss; the leg is reported in ``unsettled_closed_legs``;
* a leg matches a snapshot by token: the token is in the snapshot's ``outcome_token_ids``, or (a legacy
  snapshot without the mapping) it is the ``winning_token_id``. Payout: 1 for the winning token, else 0;
* the confirmed snapshots of one condition must agree exactly (winner, final prices, closed_ts, token
  mapping: ``resolutions.content_conflict``), and a token may belong to one condition only. Any
  contradiction is a problem and nothing at all is settled (the caller halts: invalid_risk_state);
* issue #349 (Task EG): each such contradiction is ALSO a scope=market halt of the ledger markets it is about
  (``Plan.market_halts``, written by ``record_market_halts``), with the reason prefix that blocks settlement
  (SETTLEMENT_BLOCKING_HALT_PREFIXES): settlement_conflict (two confirmed results for one condition, or a token in
  two conditions), settlement_result_changed (a booked settlement row whose confirmed snapshot now says another
  condition, winner or payout) and invalid_settlement_payout (a booked settlement row paid outside {0, 1}:
  ``invalid_payout_halts``). The global halt is kept; an Operator resume of only the global halt leaves that
  market unsettled until its own market halt is resumed;
* the row closes the whole open leg at the payout (src/ledger_store.py, (e)): a long is sold, a short
  bought back. Its realized P&L belongs to the row's ``ts``, the time it is booked (src/daily_loss.py), so a
  settled loss counts once, on the booking day, and the leg is gone on every later day. A gain never
  offsets a loss (Task AR);
* the fill_id is derived from (condition_id, market, token_id): booking it again is skipped, so settlement
  is idempotent. The same id with other content is ``conflicting_fill_id`` (Task AF).

This is the minimal settlement that does not need #120's ``decision_id`` (nothing here books twice: the
derived fill_id is the dedupe key). Pure planning here; paper_ledger.apply_settlements books the rows.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from typing import Any, Iterable

from src.ledger_store import (
    FILL_ID_FIELD,
    KIND_FIELD,
    SETTLEMENT_DECISION,
    SETTLEMENT_KIND,
    SETTLEMENT_PAYOUTS,
    counts,
    is_settlement,
    leg_key,
)
from src.resolutions import content_conflict
from src.thresholds import LEDGER_OPEN_EPS

SETTLEMENT_ID_PREFIX = "settle-"

# PR #211 review (Poly风控, issuecomment-6106764972): a MARKET-level halt does not block that market's settlement,
# except when the halt says the market's own close / settlement data contradicts itself. Then the data that would
# settle it is not trusted: no settlement for that market until the Operator resumes the halt. A halt is matched by
# the start of its reason; every other market halt (a stale book, a cancelled quote, ...) settles as usual.
SETTLEMENT_BLOCKING_HALT_PREFIXES = (
    "closed_flag_contradiction_after_writeoff",  # #197 OQ-8: written off, then the market reads open again
    "settlement_result_changed",                 # a booked settlement read again with another result
    "settlement_conflict",                       # two confirmed results for one leg / condition
    "invalid_settlement_payout",                 # a payout outside {0, 1}
)


def blocks_settlement(halt_reason: object) -> bool:
    """True when a market halt with this reason must block that market's settlement (see above)."""
    return isinstance(halt_reason, str) and halt_reason.strip().startswith(SETTLEMENT_BLOCKING_HALT_PREFIXES)


STRATEGY = "settlement"
MARKET_HALT_SOURCE = "Sentinel"  # issue #349: who records the settlement market halts (as the global ones)


def settlement_fill_id(condition_id: str, market: str, token_id: str) -> str:
    digest = hashlib.sha256(f"{condition_id}|{market}|{token_id}".encode("utf-8")).hexdigest()
    return SETTLEMENT_ID_PREFIX + digest[:32]


def confirmed(row: dict[str, Any]) -> bool:
    return row.get("uma_checked") is True and row.get("status") == "resolved"


def _tokens(row: dict[str, Any]) -> list[str]:
    tokens = row.get("outcome_token_ids")
    if isinstance(tokens, list):
        return [str(token) for token in tokens]
    return [str(row.get("winning_token_id"))]  # legacy snapshot: only the winner is proven


@dataclass
class Plan:
    rows: list[dict[str, Any]] = field(default_factory=list)       # settlement rows to book, in order
    unsettled_closed_legs: list[str] = field(default_factory=list)  # "market|token": closed, not confirmed
    problems: list[str] = field(default_factory=list)
    # issue #349 (Task EG): (market, reason) market halts for the problems above; reason starts with a blocking prefix
    market_halts: list[tuple[str, str]] = field(default_factory=list)


def _plan_market_halt(result: Plan, reason: str, markets: Iterable[str]) -> None:
    """Issue #349 (Task EG): the problem ``reason`` is also a market halt of each of ``markets`` (once each)."""
    for market in sorted({str(market) for market in markets if market}):
        if (market, reason) not in result.market_halts:
            result.market_halts.append((market, reason))


def _defer_market_halt(conflicts: list[tuple[str, list[str]]], reason: str, conditions: list[str]) -> None:
    """PR #360 review 6215179366: a settlement_conflict market halt, planned after the snapshot loop over the
    union of every token the conditions' confirmed snapshots name (plan() below)."""
    conflicts.append((reason, conditions))


def _markets_touching(rows: Iterable[dict[str, Any]], tokens: Iterable[str], condition: str | None = None) -> list[str]:
    """Issue #349: the ledger markets a settlement contradiction is about: every row (open, closed or settled) on
    one of ``tokens``, or a settlement row booked from ``condition``."""
    wanted = {str(token) for token in tokens}
    found: set[str] = set()
    for row in rows:
        market = row.get("market")
        if not market:
            continue
        if str(row.get("token_id")) in wanted or (
                condition is not None and is_settlement(row) and str(row.get("condition_id")) == condition):
            found.add(str(market))
    return sorted(found)


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):  # PR #360 review (P1): 10**400 is invalid (None), never an OverflowError
        return None
    return number if math.isfinite(number) else None


def invalid_payout_halts(rows: Iterable[dict[str, Any]]) -> list[tuple[str, str]]:
    """Issue #349 (Task EG): a booked settlement row paid outside {0, 1} (market_price, effective_price or
    settlement_payout not exactly 0 or 1) -> its market halt ``invalid_settlement_payout:<market>|<token>``.

    ledger_store.verify already refuses such a row (invalid_settlement: the global invalid_risk_state, unchanged);
    the callers whose ledger does not verify add this market halt, so a global resume alone cannot settle it."""
    result = Plan()
    for row in rows:
        if not isinstance(row, dict) or not is_settlement(row):
            continue
        payouts = [_number(row.get(name)) for name in ("market_price", "effective_price", "settlement_payout")]
        if all(payout in SETTLEMENT_PAYOUTS for payout in payouts):
            continue
        market, token = str(row.get("market") or ""), str(row.get("token_id"))
        _plan_market_halt(result, f"invalid_settlement_payout:{market}|{token}", [market])
    return result.market_halts


def record_market_halts(halts: Iterable[tuple[str, str]], *, quotes_path: Any = None, emit: Any = None) -> list[str]:
    """Issue #349 (Task EG): write each planned (market, reason) as a scope=market halt (src/sentinel.halt: the
    halt state first, that market's open paper quotes cancelled, a bus line). Only adds halts: it never lifts or
    replaces one, and the caller still records its global invalid_risk_state. An identical active market halt is
    not written again. Never raises; returns a problem per halt that could not be written (the caller halts)."""
    from src import halt_store, sentinel  # local: planning above stays pure and settlement's import time unchanged

    problems: list[str] = []
    for market, reason in halts:
        try:
            if halt_store.has_active_halt(scope=halt_store.SCOPE_MARKET, market=market, reason_prefix=reason) is True:
                continue
            message = sentinel.halt(reason=reason, market=market, scope=halt_store.SCOPE_MARKET,
                                    source=MARKET_HALT_SOURCE, to="Executor", quotes_path=quotes_path, emit=emit,
                                    extra={"blocks_settlement": True})
            # PR #360 review (P2): sentinel.halt reports a failed step in its returned ``failures`` (it already
            # wrote the global fallback); pass it to the caller instead of swallowing it
            failures = message.get("failures") if isinstance(message, dict) else ["no_message"]
            if failures:
                problems.append(f"settlement_market_halt_failed:{market}:{','.join(str(f) for f in failures)}")
        except Exception as exc:  # noqa: BLE001 - reported to the caller (a global halt), never raised
            problems.append(f"settlement_market_halt_failed:{market}:{type(exc).__name__}")
    return problems


def open_legs(rows: Iterable[dict[str, Any]]) -> dict[tuple[str, str], float]:
    """Signed open shares per leg over the counted rows (ledger order), as ledger_store replays them."""
    legs: dict[tuple[str, str], float] = {}
    for row in rows:
        if not counts(row):
            continue
        qty = float(row.get("shares") or 0.0)
        key = leg_key(row.get("market"), row.get("token_id"))
        legs[key] = legs.get(key, 0.0) + (qty if row.get("side") == "buy" else -qty)
    return {key: shares for key, shares in legs.items() if abs(shares) > LEDGER_OPEN_EPS}


def plan(ledger_rows: list[dict[str, Any]], ledger_ids: list[str], resolutions: list[dict[str, Any]],
         now: str) -> Plan:
    """The settlement rows to book now (none for a leg already settled), and the closed-but-unconfirmed legs."""
    result = Plan()
    booked = set(ledger_ids)
    final: dict[str, tuple[float, dict[str, Any]]] = {}
    pending: set[str] = set()
    by_condition: dict[str, dict[str, Any]] = {}
    mapped_by_condition: dict[str, dict[str, Any]] = {}  # the first explicit outcome_token_ids mapping per condition
    full_mapping: set[str] = set()  # PR #360 review (P2): conditions with an explicit outcome_token_ids mapping
    # PR #360 review 6215179366 (P1): every token any confirmed snapshot of a condition names (agreeing or not), and
    # the conflicts found; their market halts are written AFTER the loop over that whole union (fail closed), so a
    # market seen in another snapshot of the condition (before or after the conflicting one) is never left out
    seen_tokens: dict[str, set[str]] = {}
    conflicts: list[tuple[str, list[str]]] = []  # (reason, conditions it is about)
    for snap in resolutions:
        if not confirmed(snap):
            pending.update(_tokens(snap))
            continue
        seen_tokens.setdefault(str(snap.get("condition_id")), set()).update(_tokens(snap))
        # PR #194 review (P1): consistency is per condition, not per token. Two confirmed snapshots of one
        # condition with another winner or token mapping ([a,b]->a vs [c,b]->c) are a contradiction.
        condition = str(snap.get("condition_id"))
        prior = by_condition.get(condition)
        if prior is not None:
            # PR #360 review 6214537139 (P1): compare with the first snapshot AND the first explicit mapping of the
            # condition, so [legacy, [a,b], [a,c]] is a mapping conflict in every order (fail closed)
            conflict = None
            for base in (prior, mapped_by_condition.get(condition)):
                if base is None or base is snap:
                    continue
                try:
                    conflict = content_conflict(base, snap)
                except (KeyError, TypeError, ValueError):
                    conflict = "unreadable"
                if conflict is not None:
                    prior = base
                    break
            if conflict is not None:
                result.problems.append(f"settlement_conflict:{condition}:{conflict}")
                _defer_market_halt(conflicts, f"settlement_conflict:{condition}:{conflict}", [condition])
                continue
            # PR #360 review (P2): an agreeing snapshot of the same condition is merged (a legacy snapshot and a
            # full one, in either order, give the same tokens), never dropped
        else:
            by_condition[condition] = snap
        if isinstance(snap.get("outcome_token_ids"), list):
            full_mapping.add(condition)
            mapped_by_condition.setdefault(condition, snap)
        winner = str(snap.get("winning_token_id"))
        for token in _tokens(snap):
            payout = 1.0 if token == winner else 0.0
            other = final.get(token)
            if other is not None and other[1].get("condition_id") != snap.get("condition_id"):
                result.problems.append(f"settlement_conflict:{token}:token_in_two_conditions")
                # PR #360 review 6215179366: both conditions' whole token union, not only the shared token
                _defer_market_halt(conflicts, f"settlement_conflict:{token}:token_in_two_conditions",
                                   [str(other[1].get("condition_id")), condition])
                continue
            if other is None:
                final[token] = (payout, snap)
    for reason, conds in conflicts:
        markets: set[str] = set()
        for cond in conds:
            markets.update(_markets_touching(ledger_rows, seen_tokens.get(cond, set()), cond))
        _plan_market_halt(result, reason, markets)
    # Issue #349 (Task EG): a booked settlement read again with another result. The confirmed snapshot its token
    # maps to now names another condition, winner or payout than the booked row: a contradiction (settle nothing)
    # and a market halt of that market. PR #360 review (P1): checked BEFORE returning on any other problem, so every
    # market with its own contradiction gets its own halt in the same plan.
    for row in ledger_rows:
        if not isinstance(row, dict) or not is_settlement(row):
            continue
        market, token = str(row.get("market") or ""), str(row.get("token_id"))
        hit = final.get(token)
        if hit is None:
            # PR #360 review (P1/P2): the token left the EXPLICIT mapping of its still-confirmed condition (winner
            # a -> x, [a,b] -> [a,x]): the result changed. A legacy snapshot (no mapping) proves only its winner,
            # so it never proves a removal; no confirmed snapshot for the token nor its condition: nothing to compare.
            cond_snap = by_condition.get(str(row.get("condition_id")))
            winner_moved = cond_snap is not None and str(row.get("winning_token_id")) != str(
                cond_snap.get("winning_token_id"))  # any confirmed snapshot names the condition's winner
            if str(row.get("condition_id")) not in full_mapping and not winner_moved:
                continue
        else:
            payout, snap = hit
            if (str(row.get("condition_id")) == str(snap["condition_id"])
                    and str(row.get("winning_token_id")) == str(snap["winning_token_id"])
                    and _number(row.get("settlement_payout")) == payout):
                continue
        result.problems.append(f"settlement_result_changed:{market}|{token}")
        _plan_market_halt(result, f"settlement_result_changed:{market}|{token}", [market])
    if result.problems:
        return result  # any contradiction: settle nothing at all
    for (market, token), shares in sorted(open_legs(ledger_rows).items()):
        if not token:
            continue
        hit = final.get(token)
        if hit is None:
            if token in pending:
                result.unsettled_closed_legs.append(f"{market}|{token}")
            continue
        payout, snap = hit
        fill_id = settlement_fill_id(str(snap["condition_id"]), market, token)
        if fill_id in booked:
            result.problems.append(f"settled_leg_still_open:{market}|{token}")
            continue
        qty = abs(shares)
        result.rows.append({
            FILL_ID_FIELD: fill_id,
            "ts": now,
            KIND_FIELD: SETTLEMENT_KIND,
            "strategy": STRATEGY,
            "market": market,
            "token_id": token,
            "side": "sell" if shares > 0 else "buy",
            "shares": qty,
            "market_price": payout,
            "effective_price": payout,
            "notional_usd": qty * payout,
            "fee_rate": 0.0,
            "fee_usd": 0.0,
            "hypothetical": False,
            "counts_toward_risk": True,
            "risk_decision": SETTLEMENT_DECISION,
            "condition_id": str(snap["condition_id"]),
            "winning_token_id": str(snap["winning_token_id"]),
            "closed_ts": snap.get("closed_ts"),
            "settlement_payout": payout,
            "note": "Paper settlement of a resolved market (UMA confirmed). No exchange action.",
        })
    return result
