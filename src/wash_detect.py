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
- mint_pair_cross (issue #145): a buy intent while we have an uncancelled own buy quote on the
  OTHER token of the same binary market, and intent price + quote price >= 1 - PAIR_CROSS_TOLERANCE.
  The exchange pairs complementary buys by minting a YES/NO pair, so the two orders would fill against
  each other: a self-trade.
- merge_pair_cross (issue #145): a sell intent while we have an uncancelled own sell quote on the other
  token, and the prices sum to <= 1 + PAIR_CROSS_TOLERANCE (complementary sells pair by merging).
  The boundary counts as a pair (fail closed). Filled arb legs (recent_fills) are not resting orders and
  are still not flagged; only resting own quotes are.
- wash_input_missing / wash_input_invalid: the history or a field the check needs is
  missing or unparseable. Fail closed: never read as "no history". For the pair checks (only when an own
  open same-side quote rests in the same market on another or an unknown token): the market's tokens are
  not exactly two known, distinct tokens (``market_pair`` missing, a multi-outcome / neg-risk market, a
  token that is not one of the two), a token is unknown, or a price is missing or not a finite number in
  (0, 1).
"""
from __future__ import annotations

from datetime import datetime, timezone

from src.book_staleness import parse_book_ts

# Code constant, not a risk.toml key. Threshold for the Operator to review (like Task O's 24 h).
from src.thresholds import WASH_WINDOW_S  # noqa: E402 - Task AC: value in src/thresholds.py
from src.thresholds import PAIR_CROSS_TOLERANCE  # noqa: E402 - Task CH (#145)

CROSS_SIDE_SAME_LEG = "cross_side_same_leg"
SELF_TRADE_STRUCTURE = "self_trade_structure"
WASH_SAME_MARKET = "wash_same_market"
WASH_INPUT_MISSING = "wash_input_missing"
WASH_INPUT_INVALID = "wash_input_invalid"
MINT_PAIR_CROSS = "mint_pair_cross"
MERGE_PAIR_CROSS = "merge_pair_cross"
MARKET_PAIR_KEY = "market_pair"  # state key: the market's two token ids (None/absent: unknown)

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


def binary_pair(row: object) -> tuple[str, str] | None:
    """Issue #145: the two token ids of a clean binary market row (scanner's markets.jsonl), else None.

    None (unknown complement, so the pair check fails closed) for: not a dict, ``token_ids`` not exactly two
    distinct non-empty strings, a ``token_ids_problem``, ``outcomes`` present but not two, or a neg-risk flag
    (``neg_risk`` / ``negRisk``) that is True.
    """
    if not isinstance(row, dict):
        return None
    tokens = row.get("token_ids")
    if (not isinstance(tokens, list) or len(tokens) != 2
            or not all(isinstance(t, str) and t.strip() for t in tokens) or tokens[0].strip() == tokens[1].strip()):
        return None
    if row.get("token_ids_problem"):
        return None
    outcomes = row.get("outcomes")
    if outcomes is not None and (not isinstance(outcomes, list) or len(outcomes) != 2):
        return None
    if row.get("neg_risk") is True or row.get("negRisk") is True:
        return None
    return (tokens[0].strip(), tokens[1].strip())


def _price(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if 0.0 < number < 1.0 else None  # NaN and inf fail the range test too


def pair_quotes(market: object, token_id: object, quotes: object) -> bool:
    """True when an own open quote in ``market`` rests on another (or an unknown) token: the pair check
    then needs the market's two tokens. Unusable input is True too (the caller fails closed)."""
    if not isinstance(quotes, list) or not isinstance(market, str):
        return True
    leg = _token(token_id)
    for row in quotes:
        if not isinstance(row, dict):
            return True
        if row.get("market") != market or row.get("status") in CLOSED_STATUSES:
            continue
        row_leg = _token(row.get("token_id"))
        if leg is None or row_leg is None or leg == "" or row_leg == "" or row_leg != leg:
            return True
    return False


def pair_cross_problems(
    *,
    market: object,
    token_id: object,
    side: object,
    price: object,
    quotes: object,
    market_pair: object,
    tolerance: float = PAIR_CROSS_TOLERANCE,
) -> list[str]:
    """Issue #145: mint_pair_cross / merge_pair_cross against our own resting quotes on the other token.

    Shared by RiskGate (taker intents, through wash_problems) and the Maker (maker.quote). Only an own open
    quote on the same side in the same market on another (or an unknown) token is relevant; anything else
    returns []. A relevant quote whose pairing cannot be decided returns [wash_input_invalid] (fail closed).
    """
    if not isinstance(quotes, list) or not isinstance(market, str) or not market.strip() or side not in SIDES:
        return [WASH_INPUT_INVALID]
    leg = _token(token_id)
    if leg is None:
        return [WASH_INPUT_INVALID]
    reasons: set[str] = set()
    for row in quotes:
        if not isinstance(row, dict) or not isinstance(row.get("market"), str):
            return [WASH_INPUT_INVALID]
        if row["market"] != market:
            continue
        status, row_side, row_leg = row.get("status"), row.get("side"), _token(row.get("token_id"))
        if not isinstance(status, str) or row_leg is None or row_side not in SIDES:
            return [WASH_INPUT_INVALID]
        if status in CLOSED_STATUSES:
            continue
        if status != RESTING_STATUS:
            return [WASH_INPUT_INVALID]
        if row_side != side or (leg and row_leg and row_leg == leg):
            continue  # opposite side (cross_side_same_leg's business) or our own same-token stack
        if not leg or not row_leg:
            return [WASH_INPUT_INVALID]  # an unknown token may be the complement
        pair = market_pair if isinstance(market_pair, (tuple, list)) else None
        if pair is None or len(pair) != 2 or {leg, row_leg} != {str(t) for t in pair}:
            return [WASH_INPUT_INVALID]
        mine, theirs = _price(price), _price(row.get("price"))
        if mine is None or theirs is None:
            return [WASH_INPUT_INVALID]
        if side == "buy" and mine + theirs >= 1.0 - tolerance:
            reasons.add(MINT_PAIR_CROSS)
        if side == "sell" and mine + theirs <= 1.0 + tolerance:
            reasons.add(MERGE_PAIR_CROSS)
    return [reason for reason in (MINT_PAIR_CROSS, MERGE_PAIR_CROSS) if reason in reasons]


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
    price: object = None,
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

    # Issue #145: complementary-leg mint / merge pairing against our own resting quotes.
    pair = pair_cross_problems(market=market, token_id=token_id, side=side, price=price, quotes=quotes,
                               market_pair=state.get(MARKET_PAIR_KEY))
    if WASH_INPUT_INVALID in pair:
        return [WASH_INPUT_INVALID]
    reasons.update(pair)

    order = (CROSS_SIDE_SAME_LEG, MINT_PAIR_CROSS, MERGE_PAIR_CROSS, SELF_TRADE_STRUCTURE, WASH_SAME_MARKET)
    return [reason for reason in order if reason in reasons]
