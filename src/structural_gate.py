"""Issue #141 (Task BX): multi-leg structural arbitrage is disabled, and markets that are past their end or in
UMA resolution give no signal. Pure, offline, stdlib only. No network, no orders, no config/risk.toml edits.

1. ``structural_arb_disabled``: any signal or intent that needs two or more legs filled together (ArbBinary's
   YES+NO complete set, NegRiskArb's all-YES / all-NO set, any other multi-leg intent) is rejected, whatever its
   edge. It is recorded (bus + skips in src.paper_cycle), never dropped silently and never turned into an order.
   Nothing here re-enables it; the conditions to lift it are #141's proposal item 2, an Operator decision.
2. ``market_expired_or_resolving``: Gamma's ``umaResolutionStatus`` is a non-empty string (proposed, disputed,
   resolved, ...), or ``umaResolutionStatuses`` (a JSON string) parses to a non-empty list. A value that does not
   parse, or is any other non-empty type, counts as non-empty (fail closed). An end date already past is the
   existing ``expired`` (src.expiry_rule); when both apply, both reasons are listed. The scanner applies this on
   every scan (src.scanner.rejection_reasons) and src.paper_cycle applies the same predicate before any gap path.
3. ``outcome_set_incomplete``: a neg_risk event whose outcome set is not fully live (an inactive placeholder, a
   live leg not accepting orders, a closed leg not resolved NO, or a negRiskAugmented event with no live "Other").
   Reported next to ``structural_arb_disabled`` (detail only; the multi-leg reject already applies).
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime

from src.expiry_rule import EXPIRED, INVALID_END_DATE, INVALID_NOW, hours_to_expiry, parse_end_date

STRUCTURAL_ARB_DISABLED = "structural_arb_disabled"
MARKET_EXPIRED_OR_RESOLVING = "market_expired_or_resolving"
OUTCOME_SET_INCOMPLETE = "outcome_set_incomplete"

# Report order when several apply (resolution first, then the structural reject and its detail).
REASON_ORDER = (EXPIRED, INVALID_END_DATE, INVALID_NOW, MARKET_EXPIRED_OR_RESOLVING, STRUCTURAL_ARB_DISABLED,
                OUTCOME_SET_INCOMPLETE)

# A signal or intent with this many legs or more is structural (complete-set) and is rejected.
MULTI_LEG_MIN = 2


def _non_empty_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def uma_resolving(status: object = None, statuses: object = None) -> bool:
    """True when the market is in (or past) UMA resolution. Fail closed on anything unreadable.

    ``status``: None or "" is not resolving; a non-empty string is; any other type (a number, a list, ...) is.
    ``statuses``: None, "" or a JSON string of an empty list is not resolving; a JSON string of a non-empty list is;
    a string that is not JSON, or JSON that is not a list, is resolving; a real list counts by its length; any other
    type is resolving.
    """
    if status is not None and not (isinstance(status, str) and not status.strip()):
        return True
    if statuses is None:
        return False
    if isinstance(statuses, str):
        if not statuses.strip():
            return False
        try:
            parsed = json.loads(statuses)
        except ValueError:
            return True
        return not isinstance(parsed, list) or bool(parsed)
    if isinstance(statuses, (list, tuple)):
        return bool(statuses)
    return True


def _first(market: Mapping[str, object], *keys: str) -> object:
    for key in keys:
        if key in market:
            return market[key]
    return None


def market_uma_resolving(market: Mapping[str, object]) -> bool:
    """uma_resolving for a raw Gamma market (camelCase keys) or a scanner row (snake_case keys)."""
    return uma_resolving(_first(market, "uma_resolution_status", "umaResolutionStatus"),
                         _first(market, "uma_resolution_statuses", "umaResolutionStatuses"))


def resolution_problems(market: Mapping[str, object], now: datetime) -> list[str]:
    """``expired`` and/or ``market_expired_or_resolving`` for one market at ``now`` (an aware datetime).

    The end date is ``end_date`` (scanner row) or ``endDate`` (Gamma). Past or equal to now -> expired. A present but
    unparseable end date -> invalid_end_date; a bad ``now`` -> invalid_now (fail closed). A missing end date adds
    nothing here (the scanner's expiry rule rejects it before a row is stored).
    """
    reasons: list[str] = []
    end = _first(market, "end_date", "endDate")
    if end is not None and not (isinstance(end, str) and not end.strip()):
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            reasons.append(INVALID_NOW)
        elif parse_end_date(end) is None:
            reasons.append(INVALID_END_DATE)
        else:
            remaining = hours_to_expiry(end, now)
            if remaining is not None and remaining <= 0:
                reasons.append(EXPIRED)
    if market_uma_resolving(market):
        reasons.append(MARKET_EXPIRED_OR_RESOLVING)
    return reasons


def _prices(value: object) -> list[float] | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    if not isinstance(value, (list, tuple)):
        return None
    out: list[float] = []
    for item in value:
        try:
            out.append(float(item))
        except (TypeError, ValueError):
            return None
    return out


def _resolved_no(market: Mapping[str, object]) -> bool:
    """A closed neg_risk leg whose YES settled at 0 (outcomePrices [0, 1]): it cannot win, so the set is complete."""
    prices = _prices(market.get("outcomePrices"))
    return bool(prices) and len(prices) == 2 and prices[0] == 0.0 and prices[1] == 1.0


def _title(market: Mapping[str, object]) -> str:
    return str(market.get("groupItemTitle") or market.get("slug") or market.get("id") or "")


def outcome_set_detail(event: Mapping[str, object], markets: Sequence[Mapping[str, object]]) -> dict:
    """Why a neg_risk event's outcome set is not fully live. ``incomplete`` is True when any list is non-empty."""
    inactive: list[str] = []
    not_accepting: list[str] = []
    closed_not_no: list[str] = []
    live_other = False
    live = 0
    for market in markets:
        if market.get("closed") is True:
            if not _resolved_no(market):
                closed_not_no.append(_title(market))
            continue
        if market.get("active") is not True:
            inactive.append(_title(market))
            continue
        if market.get("acceptingOrders") is not True:
            not_accepting.append(_title(market))
            continue
        live += 1
        if market.get("negRiskOther") is True:
            live_other = True
    augmented = event.get("negRiskAugmented") is True
    without_other = augmented and not live_other
    return {
        "inactive_outcomes": inactive,
        "not_accepting_orders": not_accepting,
        "closed_not_resolved_no": closed_not_no,
        "negRiskAugmented": augmented,
        "has_live_other": live_other,
        "augmented_without_live_other": without_other,
        "live_leg_count": live,
        "incomplete": bool(inactive or not_accepting or closed_not_no or without_other),
    }


def _ordered(reasons: list[str]) -> list[str]:
    unique = list(dict.fromkeys(reasons))
    return sorted(unique, key=lambda item: REASON_ORDER.index(item) if item in REASON_ORDER else len(REASON_ORDER))


def structural_reasons(*, legs: int, now: datetime, markets: Sequence[Mapping[str, object]] = (),
                       event: Mapping[str, object] | None = None,
                       event_markets: Sequence[Mapping[str, object]] | None = None) -> list[str]:
    """Every #141 reason for one candidate signal or intent; [] passes.

    ``legs``: how many legs must fill together (a non-int or < 1 is treated as multi-leg: fail closed).
    ``markets``: the live market(s) the candidate trades; each is checked with resolution_problems.
    ``event`` + ``event_markets``: a neg_risk event and all its markets, for outcome_set_incomplete (multi-leg only).
    """
    reasons: list[str] = []
    for market in markets:
        reasons.extend(resolution_problems(market, now))
    multi = isinstance(legs, bool) or not isinstance(legs, int) or legs < 1 or legs >= MULTI_LEG_MIN
    if multi:
        reasons.append(STRUCTURAL_ARB_DISABLED)
        if event is not None and event_markets is not None and outcome_set_detail(event, event_markets)["incomplete"]:
            reasons.append(OUTCOME_SET_INCOMPLETE)
    return _ordered(reasons)
