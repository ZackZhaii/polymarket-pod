"""RiskGate for Phase 0 paper intents.

Vetoes before any simulated fill. A missing, unparseable, future or stale
book timestamp vetoes too (src/book_staleness.py, 120 s code default).
max_open_positions and max_daily_loss apply to every intent, hypothetical ones
included; a missing or invalid cap or state value vetoes. Open positions are legs
(market + token), so each filled leg of an arb pair takes its own slot.
Wash / self-trade: the strategy-name keyword ban stays, and src/wash_detect.py adds
structured checks on top (opposite resting quote or recent opposite fill on the same leg,
own counterparty). Either firing vetoes; missing or invalid history vetoes too.
Task AL (Sentinel design gap 1): an active halt vetoes. A global halt vetoes every intent, a
market halt every intent on that market; an unreadable, corrupt or lost halt state file, or
logs/HALT, is a global halt (src/halt_store.py). Only an Operator resume lifts a halt.
Issue #76: an intent carrying its own validity window (intent_created_at / intent_expires_at) has
it re-checked at the decision clock; an expired or invalid window vetoes (veto only, never allows).
Issue #104: the 1% clip is an all-in cap. Booked shares are min(intent.shares, the shares whose cost at
max(effective, intent.limit_price) plus the booked fee fits max_order_pct * PAPER_EQUITY_USD), on the
INTENT_SHARE_STEP grid; notional_usd = shares * effective. RiskGate only ever books fewer shares than the
intent asked for, never more; an invalid intent size or a size that cannot fit vetoes
(invalid_intent_shares / invalid_intent_limit_price / order_cost_above_clip).
Does not place orders, sign messages, read secrets, or edit config/risk.toml.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone

from src import fees  # Task AE: the one fee module (stress_fee / ledger_fee)
from src import halt_store  # Task AL: the halt consumer (persisted Sentinel / Executor halts)
from src import podlib  # issue #76: the decision clock is read through podlib.datetime
from src.book_staleness import book_age_problem, parse_book_ts
from src.podlib import PAPER_EQUITY_USD, pin_problems
from src.thresholds import EDGE_TOLERANCE, MAX_BOOK_AGE_S, MIN_EDGE  # Task AC: values in src/thresholds.py
from src.thresholds import ASSUMED_SLIPPAGE  # Task AU: the slippage floor (Task AT's one slippage)
from src.thresholds import INTENT_SHARE_STEP  # issue #104: booked shares on intent_builder's share grid
from src.wash_detect import wash_problems

BANNED_STRATEGY_TOKENS = ("wash", "self-trade", "self_trade", "spoof", "layering")
# Task AE: the fee per share is charged on max(market_price, effective_price) (src/fees.py).
EDGE_FORMULA_BUY = "model_prob - (market_price + assumed_slippage) - fee_rate * max(market_price, effective_price)"
EDGE_FORMULA_SELL = "(market_price - assumed_slippage) - model_prob - fee_rate * max(market_price, effective_price)"


@dataclass
class Intent:
    strategy: str
    market: str
    side: str
    model_prob: float
    market_price: float
    assumed_slippage: float
    fee_rate: float
    liquidity_usd: float | None = None
    spread: float | None = None
    token_id: str = ""
    hypothetical: bool = False
    note: str = ""
    # UTC time of the order book snapshot behind market_price/spread. Missing means veto.
    book_ts: str | float | None = None
    # Who is on the other side of the fill (counterparty or maker), when known. Paper fills
    # against the public book have none (None). Our own account/role/strategy means veto.
    counterparty: str | None = None
    # Task AX (M3 calibration step 2): identity for the forecast -> fill -> resolution join. RiskGate
    # never reads these; paper_ledger copies them onto the fill. None = unknown (never guessed).
    decision_id: str | None = None
    forecast_id: str | None = None
    forecast_ts: str | None = None
    condition_id: str | None = None
    outcome_index: int | None = None
    outcome: str | None = None
    category: str | None = None
    tags: tuple[str, ...] | None = None
    end_date: str | None = None
    model_version: str | None = None
    # Task BA (M3 step 2, rest of design 1.5): market context at commit for the calibration baseline.
    # RiskGate never reads these either (it keeps using market_price and spread); paper_ledger derives
    # market_mid / hours_to_expiry_at_entry from them. kelly_fraction_used is intent_builder's shadow
    # Kelly multiplier: recorded only, never used for size. None = unknown (never guessed).
    best_bid: float | None = None
    best_ask: float | None = None
    kelly_fraction_used: float | None = None
    # Task BC: intent_builder's per-trade raw (full) Kelly fraction kelly_raw, recorded next to the multiplier
    # (kelly_frac = kelly_fraction_used * kelly_frac_raw), and which path built the intent (calibration_ids
    # FILL_CONTEXTS: intent_builder / operator_cli / gate_check). Recorded only: RiskGate never reads either,
    # and neither ever changes a decision or a size.
    kelly_frac_raw: float | None = None
    fill_context: str | None = None
    # Task BF: the market's Polymarket event (Gamma ``events``), copied onto the fill for calibration
    # clustering. Recorded only: RiskGate never reads either; None = unknown (never guessed).
    event_id: str | None = None
    event_slug: str | None = None
    # Issue #76: the intent's own validity window, carried as text (intent_builder's created_at /
    # expires_at ISO Z stamps). Both None (a legacy intent) keeps the pre-issue behaviour. Once
    # either is set, intent_time_problems below re-checks the pair at the decision clock: a missing
    # half, a stamp that is not a zoned ISO string, a future created_at, expires_at <= created_at or
    # now >= expires_at vetoes. Unlike the calibration fields above, these decide — but only by
    # vetoing: no existing check moved and no allowance was added.
    intent_created_at: str | None = None
    intent_expires_at: str | None = None
    # Issue #104: intent_builder's own size (shares on the INTENT_SHARE_STEP grid) and limit price (the worst
    # book level, on the tick). Both None (a legacy / operator intent): RiskGate sizes from its clip alone.
    # When set they only shrink: booked shares <= shares, and the all-in cost is checked at
    # max(effective_price, limit_price). Not finite or not > 0 (or limit_price >= 1) vetoes.
    shares: float | None = None
    limit_price: float | None = None


@dataclass
class Decision:
    allow: bool
    reasons: list[str]
    edge: float
    notional_usd: float
    shares: float
    fee_usd: float
    effective_price: float
    assumed_slippage: float
    fee_rate: float
    formula: str


def _finite_number(value: object) -> float | None:
    """Accept only a finite int or float. Bools, strings, nan, and inf are rejected."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return number


def gate_thresholds(cfg: dict) -> dict[str, float]:
    """Return min_liquidity_usd and max_spread, or raise RuntimeError naming the key.

    Fails closed like the scanner thresholds: a missing key, a bool, a string,
    None, inf or nan raises. There is no default. evaluate turns the error into
    the invalid_gate_threshold veto, so a bad config can never allow.
    """
    checked: dict[str, float] = {}
    for key in ("min_liquidity_usd", "max_spread"):
        if key not in cfg:
            raise RuntimeError(f"risk.toml missing gate key: {key}")
        number = _finite_number(cfg[key])
        if number is None:
            raise RuntimeError(f"risk.toml gate key must be a finite number: {key}")
        checked[key] = number
    if checked["min_liquidity_usd"] < 0:
        raise RuntimeError("risk.toml gate key out of range: min_liquidity_usd must be >= 0")
    if checked["max_spread"] <= 0:
        raise RuntimeError("risk.toml gate key out of range: max_spread must be > 0")
    return checked


def _cap_number(cfg: dict, key: str) -> float:
    if key not in cfg:
        raise RuntimeError(f"risk.toml missing cap key: {key}")
    number = _finite_number(cfg[key])
    if number is None:
        raise RuntimeError(f"risk.toml cap key must be a finite number: {key}")
    return number


def risk_caps(cfg: dict) -> dict[str, float]:
    """Return max_open_positions and max_daily_loss_pct, or raise RuntimeError naming the key.

    Same fail-closed rule as gate_thresholds: missing, bool, string, None, inf or nan
    raises, and there is no default. max_open_positions must be a whole number >= 0;
    max_daily_loss_pct must be > 0. evaluate turns the error into invalid_risk_cap.
    """
    positions = _cap_number(cfg, "max_open_positions")
    if positions < 0 or not positions.is_integer():
        raise RuntimeError("risk.toml cap key out of range: max_open_positions must be a whole number >= 0")
    loss_pct = _cap_number(cfg, "max_daily_loss_pct")
    if loss_pct <= 0:
        raise RuntimeError("risk.toml cap key out of range: max_daily_loss_pct must be > 0")
    return {"max_open_positions": int(positions), "max_daily_loss_pct": loss_pct}


# Task X (COORDINATION #6): minimum edge after slippage and fees. A code constant, not a
# risk.toml key: a threshold proposal for the user to decide at M2 (like MIN_HOURS_TO_EXPIRY
# and WASH_WINDOW_S). 0.02 is the bar the Forecaster used as a stopgap. Inclusive: an edge of
# exactly 0.02 passes; EDGE_TOLERANCE only absorbs float rounding (0.425 - 0.405 = 0.01999...96).
# MIN_EDGE and EDGE_TOLERANCE are imported above from src/thresholds.py (Task AC).

NUMERIC_INTENT_FIELDS = ("model_prob", "market_price", "assumed_slippage", "fee_rate")


def _number_problem(field: str, value: object) -> str | None:
    """Task U: NaN / +inf / -inf -> non_finite_<field>; a non-number -> invalid_<field>."""
    if _finite_number(value) is not None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"non_finite_{field}"
    return f"invalid_{field}"


def _as_float(value: object) -> float:
    """A number for the unchanged range checks below; a non-number becomes NaN (it never passes)."""
    return float(value) if isinstance(value, (int, float)) else math.nan


def _risk_state(state: dict) -> tuple[int, float] | None:
    """Return (open_positions, daily_loss_usd), or None when either is missing or invalid."""
    positions = _finite_number(state.get("open_positions"))
    loss = _finite_number(state.get("daily_loss_usd"))
    if positions is None or positions < 0 or not positions.is_integer():
        return None
    if loss is None or loss < 0:
        return None
    return int(positions), loss


# Issue #76: the intent's own validity window. The window is carried as text, never as an epoch
# number or a datetime object, so a non-string stamp is refused outright (parse_book_ts's stricter
# string rules: a date, a time and a zone, or None).
def _intent_window_stamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    return parse_book_ts(value)


def _evaluation_now(now: object) -> datetime | None:
    """Issue #76: the decision clock as an aware UTC datetime, or None when it is unusable.

    The injected ``now`` counts only as an aware datetime (the same rule book_age_problem applies);
    when ``now`` is None the wall clock is read through ``podlib.datetime`` — the same module the
    offline gate freezes together with book_staleness, so both time checks see one clock. A ``now``
    that is neither None nor an aware datetime (a string, a naive datetime, ...) is unusable.
    """
    if now is None:
        return podlib.datetime.now(timezone.utc)
    return parse_book_ts(now) if isinstance(now, datetime) else None


def intent_time_problems(intent_created_at: object, intent_expires_at: object, now: object) -> list[str]:
    """Issue #76: re-check the intent's own validity window. Veto only; never raises.

    Both stamps None (a legacy intent) returns [] and reads no clock: the pre-issue behaviour is
    unchanged. Once either stamp is set: a missing half (missing_intent_created_at /
    missing_intent_expires_at), a stamp that is not a zoned ISO string (invalid_intent_created_at /
    invalid_intent_expires_at), an unusable decision clock (invalid_intent_now), a created_at in the
    future (future_intent_created_at), expires_at <= created_at (intent_expires_not_after_created)
    or now >= expires_at (intent_expired; exactly at expires_at is expired) vetoes. The book
    staleness check on book_ts still runs separately and unchanged.
    """
    created_set = intent_created_at is not None
    expires_set = intent_expires_at is not None
    if not created_set and not expires_set:
        return []  # a legacy intent: nothing new to check and no clock read
    problems: list[str] = []
    if not created_set:
        problems.append("missing_intent_created_at")
    if not expires_set:
        problems.append("missing_intent_expires_at")
    created = _intent_window_stamp(intent_created_at) if created_set else None
    expires = _intent_window_stamp(intent_expires_at) if expires_set else None
    if created_set and created is None:
        problems.append("invalid_intent_created_at")
    if expires_set and expires is None:
        problems.append("invalid_intent_expires_at")
    if created is None or expires is None:
        return problems
    if expires <= created:
        problems.append("intent_expires_not_after_created")
        return problems
    current = _evaluation_now(now)
    if current is None:
        problems.append("invalid_intent_now")
        return problems
    if created > current:
        problems.append("future_intent_created_at")
    if current >= expires:
        problems.append("intent_expired")
    return problems


def _floor_to_step(shares: float) -> float:
    """Issue #104: down to the INTENT_SHARE_STEP grid (intent_builder's rounding); never up."""
    if not math.isfinite(shares) or shares <= 0:
        return 0.0
    stepped = round(math.floor(shares / INTENT_SHARE_STEP + 1e-9) * INTENT_SHARE_STEP, 10)
    if stepped > shares:  # the float guard above must never round up
        stepped = round(stepped - INTENT_SHARE_STEP, 10)
    return max(stepped, 0.0)


def _intent_size(intent: Intent) -> tuple[float | None, list[str]]:
    """Issue #104: the intent's own shares (None: not given) and any problem with shares / limit_price."""
    problems: list[str] = []
    shares = None
    if intent.shares is not None:
        shares = _finite_number(intent.shares)
        if shares is None or shares <= 0:
            problems.append("invalid_intent_shares")
            shares = None
    if intent.limit_price is not None:
        limit = _finite_number(intent.limit_price)
        if limit is None or not 0.0 < limit < 1.0:
            problems.append("invalid_intent_limit_price")
    return shares, problems


def evaluate(
    intent: Intent,
    cfg: dict,
    state: dict,
    *,
    now: object = None,
    max_book_age_s: object = MAX_BOOK_AGE_S,
    halt_path: object = None,
    halt_fallback_path: object = None,
) -> Decision:
    """Return allow only when every Phase 0 check passes.

    The book must be fresh: book_ts present, parseable, not in the future and at most
    max_book_age_s old at ``now`` (default: the clock). Otherwise it vetoes; it never
    raises for this. max_book_age_s is a code default, not a risk.toml key.

    max_open_positions and max_daily_loss are checked for every intent; hypothetical=True
    no longer skips them. A missing or invalid cap (invalid_risk_cap) or state value
    (invalid_risk_state) vetoes instead of reading 0.

    The position-cap exemption needs state["leg_already_open"] is True: this exact
    token is already held. Another open leg of the same market (the other side of
    an arb pair) does not exempt it; that is a new directional position (Task P).

    Wash / self-trade (Task S): besides the keyword ban, state must carry the paper
    history ``recent_fills`` and ``own_quotes`` (paper_ledger.load_state does). An
    uncancelled own quote on the opposite side of the same leg (cross_side_same_leg), an
    own counterparty (self_trade_structure) or an opposite fill on the same leg within
    WASH_WINDOW_S (wash_same_market) vetoes. Missing history is wash_input_missing,
    malformed history wash_input_invalid; both veto.

    Task AU: assumed_slippage must be a finite number >= ASSUMED_SLIPPAGE (src/thresholds.py, inclusive);
    below it vetoes slippage_below_floor, missing / non-finite vetoes invalid_ / non_finite_assumed_slippage.

    Issue #76: an intent that carries its own validity window (intent_created_at / intent_expires_at,
    both set by intent_builder.to_risk_intent) has it re-checked at the same decision clock as the
    book staleness check: a missing half, a stamp that is not a zoned ISO string, a future
    created_at, expires_at <= created_at or now >= expires_at vetoes (intent_time_problems). An
    intent with both stamps None (a legacy intent) keeps the old behaviour. Veto only: an intent
    inside its window changes no decision.

    Edge must be at least MIN_EDGE (0.02, inclusive; code constant, Task X): 0 < edge < MIN_EDGE
    vetoes edge_below_min, edge <= 0 vetoes non_positive_edge.

    Task AL: any active halt for intent.market (market or global scope, a bad halt state file or
    logs/HALT) vetoes with its halt_* reason. halt_path / halt_fallback_path default to
    src/halt_store.py's paths.

    Size is the 1% clip, all-in (issue #104): shares * max(effective, intent.limit_price) + fee <= the clip,
    and never more than intent.shares. kelly_fraction is not applied and cannot increase size.
    A single-leg fill is directional exposure, not risk-free arbitrage.
    """
    reasons: list[str] = []
    # Task AL: halts first. halt_problems never raises; a broken check is halt_check_failed (veto).
    reasons.extend(halt_store.halt_problems(intent.market, halt_path, halt_fallback_path))
    drifted = pin_problems(cfg)
    if drifted:
        reasons.append("phase0_pin:" + ";".join(drifted))

    strategy = intent.strategy.lower()
    if any(token in strategy for token in BANNED_STRATEGY_TOKENS):
        reasons.append("banned_behavior")
    # Structured check on top of the keyword ban (Task S). A clean name does not exempt.
    reasons.extend(
        wash_problems(
            market=intent.market,
            token_id=intent.token_id,
            side=intent.side,
            strategy=intent.strategy,
            counterparty=intent.counterparty,
            state=state,
            now=now,
        )
    )
    if intent.hypothetical and intent.strategy != "phase0-gate-test":
        reasons.append("hypothetical_strategy_not_allowed")
    if intent.side not in {"buy", "sell"}:
        reasons.append("side_must_be_single_leg")
    # Task U: every number in the edge math must be finite. NaN compares False with
    # everything, so without this a NaN fee_rate made the edge NaN and skipped
    # non_positive_edge. A non-finite or non-number input vetoes and the edge fails closed.
    number_problems = [
        problem
        for field in NUMERIC_INTENT_FIELDS
        if (problem := _number_problem(field, getattr(intent, field))) is not None
    ]
    reasons.extend(number_problems)
    model_prob = _as_float(intent.model_prob)
    market_price = _as_float(intent.market_price)
    assumed_slippage = _as_float(intent.assumed_slippage)
    fee_rate = _as_float(intent.fee_rate)

    if not 0.0 < model_prob < 1.0:
        reasons.append("model_prob_out_of_range")
    if not 0.0 < market_price < 1.0:
        reasons.append("market_price_out_of_range")
    if assumed_slippage < 0 or fee_rate < 0:
        reasons.append("negative_cost_assumption")
    # Task AU: an intent may not assume less slippage than the one ASSUMED_SLIPPAGE (a smaller value
    # inflates the edge). Inclusive floor: exactly ASSUMED_SLIPPAGE passes. A missing / non-number /
    # non-finite slippage already vetoed above (invalid_ / non_finite_assumed_slippage).
    slippage_number = _finite_number(intent.assumed_slippage)
    if slippage_number is not None and slippage_number < ASSUMED_SLIPPAGE:
        reasons.append("slippage_below_floor")

    try:
        thresholds = gate_thresholds(cfg)
    except RuntimeError:
        reasons.append("invalid_gate_threshold")
        thresholds = None

    liquidity = None if intent.liquidity_usd is None else _finite_number(intent.liquidity_usd)
    if intent.liquidity_usd is None:
        reasons.append("missing_liquidity")
    elif liquidity is None:
        reasons.append("invalid_liquidity")
    elif thresholds is not None and liquidity < thresholds["min_liquidity_usd"]:
        reasons.append("liquidity_below_min")

    spread = None if intent.spread is None else _finite_number(intent.spread)
    if intent.spread is None:
        reasons.append("missing_spread")
    elif spread is None:
        reasons.append("invalid_spread")
    elif spread < 0:
        reasons.append("negative_spread")
    elif thresholds is not None and spread > thresholds["max_spread"]:
        reasons.append("spread_above_max")

    book_problem = book_age_problem(intent.book_ts, now, max_book_age_s)
    if book_problem is not None:
        reasons.append(book_problem)
    # Issue #76: the intent's own validity window, re-checked at the same decision clock. A legacy
    # intent (both stamps None) adds nothing and reads no clock.
    reasons.extend(intent_time_problems(intent.intent_created_at, intent.intent_expires_at, now))

    if intent.side == "buy":
        effective = market_price + assumed_slippage
        formula = EDGE_FORMULA_BUY
    elif intent.side == "sell":
        effective = market_price - assumed_slippage
        formula = EDGE_FORMULA_SELL
    else:
        effective = market_price
        formula = ""

    if not 0.0 < effective < 1.0:
        reasons.append("effective_price_out_of_range")

    # Task AE: one fee module (src/fees.py). stress_fee is None only for a non-finite or
    # non-number fee_rate or price, which is already a Task U number problem: edge -1 (veto).
    stress = fees.stress_fee(intent.fee_rate, intent.market_price, effective)
    fee_per_share = 0.0 if stress is None else stress
    if intent.side == "buy":
        edge = model_prob - effective - fee_per_share
    elif intent.side == "sell":
        edge = effective - model_prob - fee_per_share
    else:
        edge = -1.0
    if number_problems or stress is None or not math.isfinite(edge):
        edge = -1.0  # fail closed: a non-finite input never yields a usable edge
    if edge <= 0:
        reasons.append("non_positive_edge")
    elif edge < MIN_EDGE - EDGE_TOLERANCE:
        reasons.append("edge_below_min")  # Task X: 0 < edge < MIN_EDGE; config cannot lower it

    # Hard clip at 1% even if a caller passes a looser in-memory config.
    try:
        max_order_pct = float(cfg.get("max_order_pct", 0.0))
    except (TypeError, ValueError):
        max_order_pct = math.nan
    if not math.isfinite(max_order_pct):
        reasons.append("invalid_max_order_pct")
        max_order_pct = 0.0
    clip = min(max_order_pct, 0.01)
    cap_usd = PAPER_EQUITY_USD * clip
    if cap_usd <= 0:
        reasons.append("zero_notional")

    # Caps apply to hypothetical intents too (backlog item 4); there is no bypass flag.
    try:
        caps = risk_caps(cfg)
    except RuntimeError:
        reasons.append("invalid_risk_cap")
        caps = None
    current = _risk_state(state)
    if current is None:
        reasons.append("invalid_risk_state")
    if caps is not None and current is not None:
        open_positions, daily_loss = current
        already_open = state.get("leg_already_open") is True
        if open_positions >= caps["max_open_positions"] and not already_open:
            reasons.append("max_open_positions")
        if PAPER_EQUITY_USD <= 0 or daily_loss / PAPER_EQUITY_USD >= caps["max_daily_loss_pct"]:
            reasons.append("max_daily_loss")

    # Issue #104: size inside the all-in cap: shares * cost_price + fee <= cap_usd, never above the
    # intent's own shares. cost_price = max(effective, intent.limit_price): the fill may come at either.
    intent_shares, size_problems = _intent_size(intent)
    reasons.extend(size_problems)
    cost_price = effective
    limit_price = _finite_number(intent.limit_price) if intent.limit_price is not None else None
    if limit_price is not None and not size_problems and limit_price > cost_price:
        cost_price = limit_price
    per_share = fees.fee_per_share(intent.fee_rate, intent.market_price, effective)
    shares = 0.0
    if (math.isfinite(effective) and effective > 0 and math.isfinite(cost_price) and per_share is not None
            and cap_usd > 0):
        shares = _floor_to_step(cap_usd / (cost_price + per_share))
        if intent_shares is not None:
            shares = min(shares, intent_shares)
    notional = shares * effective if shares > 0 else 0.0
    # Task AE: the booked fee comes from the same module; never below notional * fee_rate.
    booked = fees.ledger_fee(intent.fee_rate, intent.market_price, effective, notional, shares)
    if booked is None:
        if not reasons:
            reasons.append("invalid_fee")  # backstop: no allow without a valid fee
        booked = 0.0
    fee_usd = booked
    if cap_usd > 0 and not (shares > 0 and shares * cost_price + fee_usd <= cap_usd):
        if not reasons:
            reasons.append("order_cost_above_clip")  # issue #104: nothing fits, or the float guard tripped
    return Decision(
        allow=not reasons,
        reasons=reasons,
        edge=edge,
        notional_usd=notional,
        shares=shares,
        fee_usd=fee_usd,
        effective_price=effective,
        assumed_slippage=intent.assumed_slippage,
        fee_rate=intent.fee_rate,
        formula=formula,
    )
