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
from dataclasses import dataclass, field
from typing import Any, Iterable

from src.ledger_store import (
    FILL_ID_FIELD,
    KIND_FIELD,
    SETTLEMENT_DECISION,
    SETTLEMENT_KIND,
    counts,
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
    "writeoff_close_first_seen_in_future",       # #197 / PR #342: clock rollback after the market's write-off
    "writeoff_close_record_unparseable",         # #197 / PR #342: the market's recorded close cannot be read
    "settlement_result_changed",                 # a booked settlement read again with another result
    "settlement_conflict",                       # two confirmed results for one leg / condition
    "invalid_settlement_payout",                 # a payout outside {0, 1}
)


def blocks_settlement(halt_reason: object) -> bool:
    """True when a market halt with this reason must block that market's settlement (see above)."""
    return isinstance(halt_reason, str) and halt_reason.strip().startswith(SETTLEMENT_BLOCKING_HALT_PREFIXES)
STRATEGY = "settlement"


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
    for snap in resolutions:
        if not confirmed(snap):
            pending.update(_tokens(snap))
            continue
        # PR #194 review (P1): consistency is per condition, not per token. Two confirmed snapshots of one
        # condition with another winner or token mapping ([a,b]->a vs [c,b]->c) are a contradiction.
        condition = str(snap.get("condition_id"))
        prior = by_condition.get(condition)
        if prior is not None:
            try:
                conflict = content_conflict(prior, snap)
            except (KeyError, TypeError, ValueError):
                conflict = "unreadable"
            if conflict is not None:
                result.problems.append(f"settlement_conflict:{condition}:{conflict}")
            continue
        by_condition[condition] = snap
        winner = str(snap.get("winning_token_id"))
        for token in _tokens(snap):
            payout = 1.0 if token == winner else 0.0
            other = final.get(token)
            if other is not None and other[1].get("condition_id") != snap.get("condition_id"):
                result.problems.append(f"settlement_conflict:{token}:token_in_two_conditions")
                continue
            final[token] = (payout, snap)
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
