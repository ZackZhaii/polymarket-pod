"""Structured wash / self-trade detection for RiskGate (Task S, backlog item 1).

The strategy-name keyword ban in risk_gate stays. These checks look at structure, so a
clean strategy name does not get through. Pure and offline: the caller passes the paper
history (paper_ledger.load_state supplies it). No network, no orders, no secrets.

Leg = market + token_id, the same key the ledger nets on. A row or intent without a
token_id is an unknown leg of that market and is treated as possibly the same token
(fail closed). The two tokens of an arb pair (buy YES, buy NO) are different legs and
are not flagged here; that is directional exposure (Task P), not a wash.

Veto reasons:
- cross_side_same_leg: an uncancelled own paper quote (quotes.jsonl status "open") rests
  on the opposite side of the same leg. No time window: uncancelled means still resting.
- self_trade_structure: the intent's counterparty / maker is our own account, a pod role
  or one of our strategies.
- wash_same_market: an own paper fill on the opposite side of the same leg within
  WASH_WINDOW_S seconds (hardening; any strategy, hypothetical rows included).
- wash_input_missing / wash_input_invalid: the history or a field the check needs is
  missing or unparseable. Fail closed: never read as "no history".
"""
from __future__ import annotations

from datetime import datetime, timezone

from src.book_staleness import parse_book_ts

# Code constant, not a risk.toml key. Threshold for the Operator to review (like Task O's 24 h).
from src.thresholds import WASH_WINDOW_S  # noqa: E402 - Task AC: value in src/thresholds.py

CROSS_SIDE_SAME_LEG = "cross_side_same_leg"
SELF_TRADE_STRUCTURE = "self_trade_structure"
WASH_SAME_MARKET = "wash_same_market"
WASH_INPUT_MISSING = "wash_input_missing"
WASH_INPUT_INVALID = "wash_input_invalid"

FILLS_KEY = "recent_fills"
QUOTES_KEY = "own_quotes"

SIDES = ("buy", "sell")
RESTING_STATUS = "open"
CLOSED_STATUSES = frozenset({"cancelled"})
# Our own account labels and the ROSTER roles that can rest or fill paper orders.
OWN_ACCOUNT_IDS = frozenset({"self", "own", "pod", "paper-pod", "maker", "executor"})


def _token(value: object) -> str | None:
    """'' for an unknown leg, the id for a named leg, None for an unusable value."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return None


def _same_leg(a: str, b: str) -> bool:
    return a == b or a == "" or b == ""


def _opposite(side: str) -> str:
    return "sell" if side == "buy" else "buy"


def _now(now: object) -> datetime | None:
    if now is None:
        return datetime.now(timezone.utc)
    return parse_book_ts(now) if isinstance(now, datetime) else None


def _norm(value: str) -> str:
    return value.strip().casefold()


def wash_problems(
    *,
    market: object,
    token_id: object,
    side: object,
    strategy: object,
    counterparty: object,
    state: object,
    now: object = None,
    window_s: float = WASH_WINDOW_S,
) -> list[str]:
    """Return veto reasons (empty list = no structured wash / self-trade found)."""
    if not isinstance(state, dict) or FILLS_KEY not in state or QUOTES_KEY not in state:
        return [WASH_INPUT_MISSING]
    fills, quotes = state[FILLS_KEY], state[QUOTES_KEY]
    if not isinstance(fills, list) or not isinstance(quotes, list):
        return [WASH_INPUT_INVALID]
    current = _now(now)
    leg = _token(token_id)
    if (
        current is None
        or not isinstance(market, str)
        or not market.strip()
        or leg is None
        or side not in SIDES
        or not isinstance(strategy, str)
    ):
        return [WASH_INPUT_INVALID]
    opposite = _opposite(str(side))
    reasons: set[str] = set()

    own_ids = set(OWN_ACCOUNT_IDS)
    if strategy.strip():
        own_ids.add(_norm(strategy))

    for row in fills:
        if not isinstance(row, dict) or not isinstance(row.get("market"), str):
            return [WASH_INPUT_INVALID]
        name = row.get("strategy")
        if isinstance(name, str) and name.strip():
            own_ids.add(_norm(name))
        if row["market"] != market:
            continue
        row_leg = _token(row.get("token_id"))
        row_side = row.get("side")
        stamp = parse_book_ts(row.get("ts"))
        if row_leg is None or row_side not in SIDES or stamp is None:
            return [WASH_INPUT_INVALID]
        # A future-stamped fill counts as inside the window (fail closed).
        if (
            row_side == opposite
            and _same_leg(leg, row_leg)
            and (current - stamp).total_seconds() <= window_s
        ):
            reasons.add(WASH_SAME_MARKET)

    for row in quotes:
        if not isinstance(row, dict) or not isinstance(row.get("market"), str):
            return [WASH_INPUT_INVALID]
        if row["market"] != market:
            continue
        status = row.get("status")
        row_leg = _token(row.get("token_id"))
        row_side = row.get("side")
        if not isinstance(status, str) or row_leg is None or row_side not in SIDES:
            return [WASH_INPUT_INVALID]
        if status in CLOSED_STATUSES:
            continue
        if status != RESTING_STATUS:
            return [WASH_INPUT_INVALID]
        if row_side == opposite and _same_leg(leg, row_leg):
            reasons.add(CROSS_SIDE_SAME_LEG)

    if counterparty is not None:
        if not isinstance(counterparty, str) or not counterparty.strip():
            return [WASH_INPUT_INVALID]
        if _norm(counterparty) in own_ids:
            reasons.add(SELF_TRADE_STRUCTURE)

    order = (CROSS_SIDE_SAME_LEG, SELF_TRADE_STRUCTURE, WASH_SAME_MARKET)
    return [reason for reason in order if reason in reasons]
