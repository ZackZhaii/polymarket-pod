"""Issue #126 (Task CG, F2b): book resolved markets' open legs as settlement rows. Paper only, offline.

The paper ledger had no settlement: a losing leg stayed open forever. With Task CF (#124) a closed market's
leg has no book, so it was valued at 0 against its cost on every UTC day, and the same loss counted again
each day. Settling it closes the leg once:

* only a confirmed snapshot in data/resolutions.jsonl settles (``status`` resolved and ``uma_checked``
  true; read strictly with ``resolutions.read_history``). An unconfirmed (closed only) snapshot settles
  nothing and is not read as a 0 loss; the leg is reported in ``unsettled_closed_legs``;
* a leg matches a snapshot by token: the token is in the snapshot's ``outcome_token_ids``, or (a legacy
  snapshot without the mapping) it is the ``winning_token_id``. Payout: 1 for the winning token, else 0;
* two confirmed snapshots that disagree about a token's payout settle nothing for it (a problem);
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
from src.thresholds import LEDGER_OPEN_EPS

SETTLEMENT_ID_PREFIX = "settle-"
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
    for snap in resolutions:
        tokens = _tokens(snap)
        winner = str(snap.get("winning_token_id"))
        for token in tokens:
            if not confirmed(snap):
                pending.add(token)
                continue
            payout = 1.0 if token == winner else 0.0
            prior = final.get(token)
            if prior is not None and (prior[0] != payout or prior[1].get("condition_id") != snap.get("condition_id")):
                result.problems.append(f"settlement_conflict:{token}")
                final[token] = (float("nan"), snap)
                continue
            final.setdefault(token, (payout, snap))
    for (market, token), shares in sorted(open_legs(ledger_rows).items()):
        if not token:
            continue
        hit = final.get(token)
        if hit is None:
            if token in pending:
                result.unsettled_closed_legs.append(f"{market}|{token}")
            continue
        payout, snap = hit
        if payout != payout:  # conflict (NaN): settle nothing
            continue
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
