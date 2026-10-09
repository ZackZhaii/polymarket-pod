"""Task AO: signal -> OrderIntent (forecaster/SPEC_signal_to_intent.md). Pure, tightening only.

``build_order_intent`` turns one probability signal, the scanner row of its market, the order book
snapshots of both outcome tokens and the parsed config/risk.toml into either an ``OrderIntent`` or a
list of reason codes. ``to_risk_intent`` turns an OrderIntent into the ``risk_gate.Intent`` that
RiskGate already understands. It is the producer and pre-filter *before* RiskGate, never a
replacement: RiskGate still re-checks every intent and keeps its veto.

Rules (all enforced by tests/test_intent_builder.py):

- Pure and deterministic: no clock read (``now`` is injected and must be an aware datetime), no
  file, no bus, no network, no order. Inputs are never mutated.
- Tightening only: every check can only reject or shrink. A config value can never loosen a code
  constant (a scanner_min_price / scanner_max_price override only narrows the band). If RiskGate
  would veto an intent for an edge, price, spread, liquidity or staleness reason, IB rejects it too.
- Edge per side with the conservative end of the probability interval (p_low to buy outcome 0,
  1 - p_high to buy outcome 1), the book-walk average price, INTENT_ASSUMED_SLIPPAGE and
  ``fees.stress_fee`` unchanged: the edge is bit-for-bit the one RiskGate computes for the same
  intent (same expression, same fee call).
- Kelly is SHADOW only (Poly指挥 / M2 #7): kelly_raw, kelly_frac and the Kelly size are computed and
  reported, never used for size. The size IB reports is RiskGate's own 1% clip
  (PAPER_EQUITY_USD * min(max_order_pct, 0.01)), rounded down to whole share steps at the tick-rounded
  limit price; under min_order_size it is rejected, never rounded up. Enforcing a Kelly size needs a
  RiskGate Intent.size_usd field (KEY, backlog #7) and is not done here.
- Opening is buy only. A sell (reducing a position) is not built here: no position means
  sell_requires_position, a position means sell_not_supported (fails closed until a reduce path exists).
- Every reason is collected in the canonical order of SPEC section 5; any reason means no intent.
  Informational marks on an intent: kelly_not_enforced_by_riskgate, top_of_book_only.

Imports only the standard library, src.fees, src.book_staleness, src.expiry_rule, src.thresholds,
src.podlib.pin_problems and src.risk_gate (Intent, BANNED_STRATEGY_TOKENS). Paper only.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from src import fees
from src.calibration_ids import make_decision_id
from src.book_staleness import book_age_problem, parse_book_ts
from src.expiry_rule import expiry_problem, parse_end_date
from src.podlib import pin_problems
from src.risk_gate import BANNED_STRATEGY_TOKENS, Intent, gate_thresholds
from src.thresholds import (
    EDGE_TOLERANCE,
    ENDGAME_PRICE_MAX,
    ENDGAME_PRICE_MIN,
    INTENT_ASSUMED_SLIPPAGE,
    INTENT_SHARE_STEP,
    INTENT_TTL_S,
    MAX_BOOK_AGE_S,
    MAX_SIGNAL_AGE_ENDGAME_CONFIRM_S,
    MAX_SIGNAL_AGE_NEWS_S,
    MAX_SIGNAL_AGE_SPORTS_DEVIG_S,
    MAX_SOURCE_LOGIT_GAP,
    MIN_EDGE,
    MIN_HOURS_TO_EXPIRY,
    PAPER_EQUITY_USD,
    SCANNER_MAX_PRICE,
    SCANNER_MIN_PRICE,
)

# The same hard 1% clip RiskGate applies (src/risk_gate.py: clip = min(max_order_pct, 0.01)).
_HARD_CLIP_PCT = 0.01

SOURCES = ("news", "sports_devig", "endgame_confirm", "forecaster_override", "blend")
# A raw Endgame signal nominates a market but carries no model probability (SPEC 1.2).
NO_PROBABILITY_SOURCES = ("endgame",)

# Signal freshness per source. forecaster_override follows news; a blend gets the strictest limit.
MAX_SIGNAL_AGE_BY_SOURCE = {
    "news": MAX_SIGNAL_AGE_NEWS_S,
    "sports_devig": MAX_SIGNAL_AGE_SPORTS_DEVIG_S,
    "endgame_confirm": MAX_SIGNAL_AGE_ENDGAME_CONFIRM_S,
    "forecaster_override": MAX_SIGNAL_AGE_NEWS_S,
    "blend": min(MAX_SIGNAL_AGE_NEWS_S, MAX_SIGNAL_AGE_SPORTS_DEVIG_S, MAX_SIGNAL_AGE_ENDGAME_CONFIRM_S),
}

# Canonical reason order (SPEC section 5, plus the few codes this module adds, each next to its kin).
REASON_ORDER = (
    # A config / mode
    "phase0_pin", "live_not_permitted", "banned_behavior", "invalid_kelly_fraction", "invalid_max_order_pct",
    "invalid_gate_threshold",
    # B signal
    "missing_signal", "unsupported_signal_source", "missing_model_prob", "invalid_model_prob",
    "non_finite_model_prob", "model_prob_out_of_range", "invalid_outcome_index", "market_mismatch",
    "invalid_now", "missing_signal_ts", "invalid_signal_ts", "future_signal_ts", "stale_signal",
    "source_conflict", "duplicate_intent",
    # C market
    "missing_token_ids", "not_binary_market", "token_mismatch", "missing_condition_id", "closed",
    "not_accepting_orders", "missing_end_date", "invalid_end_date", "expired", "near_expiry",
    "ambiguous_resolution", "category_banned", "missing_fee_data", "invalid_fee_rate",
    "negative_cost_assumption",
    # D book
    "missing_book", "token_book_mismatch", "missing_book_ts", "invalid_book_ts", "future_book_ts", "stale_book",
    "missing_spread", "invalid_spread", "negative_spread", "spread_above_max", "missing_liquidity",
    "invalid_liquidity", "liquidity_below_min", "invalid_book_levels", "insufficient_depth", "missing_tick_size",
    "missing_min_order_size",
    # E price band
    "price_out_of_band",
    # F edge
    "invalid_fee", "effective_price_out_of_range", "non_positive_edge", "edge_below_min",
    # G size / side
    "kelly_non_positive", "zero_notional", "size_below_min_order", "sell_requires_position", "sell_not_supported",
)
_RANK = {name: index for index, name in enumerate(REASON_ORDER)}
INFO_MARKS = ("kelly_not_enforced_by_riskgate", "top_of_book_only")


# ---------------------------------------------------------------- data types

@dataclass(frozen=True)
class ProbabilitySignal:
    signal_id: str
    source: str  # news | sports_devig | endgame_confirm | forecaster_override | blend (endgame: no p)
    market_id: str
    outcome_index: int  # the outcome p is about; legacy NewsForecaster rows = 0
    p: float | None
    p_low: float | None = None
    p_high: float | None = None
    token_id: str | None = None
    signal_ts: str | float | None = None
    note: str = ""
    # Task AX (M3 step 2): the sealed forecast behind a NewsForecaster signal (None for legacy rows).
    forecast_id: str | None = None
    forecast_ts: str | None = None
    model_version: str | None = None


@dataclass(frozen=True)
class BookSnapshot:
    market_id: str
    token_id: str
    asks: tuple[tuple[float, float], ...]  # (price, size) ascending; one level when only top of book
    bids: tuple[tuple[float, float], ...]
    best_bid: float | None
    best_ask: float | None
    spread: float | None
    book_ts: str | float | None
    tick_size: float | None
    min_order_size: float | None


@dataclass(frozen=True)
class MarketContext:
    market_id: str
    condition_id: str
    token_ids: tuple[str, ...]
    outcomes: tuple[str, ...]
    end_date: str
    liquidity_usd: float | None
    fee_rate: float | None  # resolved by the runner (SPEC 7-3); None -> missing_fee_data
    accepting_orders: bool | None
    closed: bool
    category: str | None = None
    resolution_flag: str | None = None  # "ambiguous" -> ambiguous_resolution
    # Task AX (M3 step 2): Gamma's own labels from the scanner row, for calibration grouping only.
    # Never read by any check (category_banned reads ``category``, which only the caller sets).
    gamma_category: str | None = None
    tags: tuple[str, ...] | None = None


@dataclass(frozen=True)
class WalkResult:
    avg_price: float
    worst_price: float
    filled_usd: float
    shares: float
    complete: bool
    valid: bool = True  # False: a level is not a finite (0 < price < 1, size > 0) pair or asks not ascending


@dataclass(frozen=True)
class EdgeBreakdown:
    p_side: float
    exec_price: float
    slippage: float
    fee_per_share: float | None
    edge: float
    effective_price: float = math.nan


@dataclass(frozen=True)
class SizeBreakdown:
    kelly_raw: float
    kelly_frac: float
    size_pct: float
    size_usd: float
    shares: float
    limit_price: float
    kelly_size_pct: float = 0.0  # shadow only: min(kelly_frac, size_pct) - never used for size
    kelly_size_usd: float = 0.0


@dataclass(frozen=True)
class OrderIntent:
    intent_id: str
    idempotency_key: str
    signal_id: str
    signal_source: str
    strategy: str
    market_id: str
    condition_id: str
    outcome_index: int
    outcome: str
    token_id: str
    side: str
    limit_price: float
    exec_price: float
    best_ask: float | None
    best_bid: float | None
    spread: float | None
    liquidity_usd: float | None
    size_usd: float
    shares: float
    p_model: float
    p_low: float
    p_high: float
    p_side: float
    assumed_slippage: float
    fee_rate: float
    fee_per_share: float
    edge: float
    edge_yes: float
    edge_no: float
    kelly_raw: float
    kelly_fraction: float
    kelly_frac: float
    kelly_size_pct: float
    kelly_size_usd: float
    size_pct: float
    reasons: tuple[str, ...]
    created_at: str
    expires_at: str
    book_ts: str | float | None
    signal_ts: str | float | None
    paper: bool = True
    note: str = ""
    counterparty: None = None
    # Task AX (M3 step 2): calibration identity, copied onto the risk Intent and the paper fill.
    decision_id: str | None = None
    forecast_id: str | None = None
    forecast_ts: str | None = None
    model_version: str | None = None
    category: str | None = None
    tags: tuple[str, ...] | None = None
    end_date: str | None = None


@dataclass(frozen=True)
class IntentResult:
    intent: OrderIntent | None
    reasons: tuple[str, ...]
    first_reason: str | None
    detail: dict = field(default_factory=dict)


# ---------------------------------------------------------------- small helpers

def _finite(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _number_problem(name: str, value: object) -> str | None:
    """RiskGate naming: NaN / inf -> non_finite_<name>; any other non-number -> invalid_<name>."""
    if _finite(value) is not None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"non_finite_{name}"
    return f"invalid_{name}"


def _aware(value: object) -> bool:
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _ordered(reasons: list[str]) -> tuple[str, ...]:
    """Dedupe and sort into the canonical order (phase0_pin:<...> ranks as phase0_pin; unknown codes last)."""
    seen: list[str] = []
    for reason in reasons:
        if reason not in seen:
            seen.append(reason)
    return tuple(sorted(seen, key=lambda r: (_RANK.get(r.split(":", 1)[0], len(_RANK)), seen.index(r))))


def _logit(p: float) -> float:
    return math.log(p / (1.0 - p))


def _sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    z = math.exp(x)
    return z / (1.0 + z)


def _interval(signal: ProbabilitySignal) -> tuple[object, object, object]:
    low = signal.p if signal.p_low is None else signal.p_low
    high = signal.p if signal.p_high is None else signal.p_high
    return signal.p, low, high


def probability_problems(signal: ProbabilitySignal) -> list[str]:
    """Reasons the signal's probability (and interval) is unusable; [] when 0 < p_low <= p <= p_high < 1."""
    if signal.source in NO_PROBABILITY_SOURCES or signal.p is None:
        return ["missing_model_prob"]
    out: list[str] = []
    for value in _interval(signal):
        problem = _number_problem("model_prob", value)
        if problem is not None and problem not in out:
            out.append(problem)
    if out:
        return out
    p, low, high = (float(v) for v in _interval(signal))  # type: ignore[arg-type]
    if not 0.0 < low <= p <= high < 1.0:
        return ["model_prob_out_of_range"]
    return []


def outcome_zero_interval(signal: ProbabilitySignal) -> tuple[float, float, float]:
    """(p, p_low, p_high) for outcome 0. Only call after probability_problems(signal) == []."""
    p, low, high = (float(v) for v in _interval(signal))  # type: ignore[arg-type]
    if signal.outcome_index == 1:
        return 1.0 - p, 1.0 - high, 1.0 - low
    return p, low, high


def _valid_outcome_index(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value in (0, 1)


# ---------------------------------------------------------------- pure building blocks

def walk_asks(asks: Sequence[tuple[float, float]], size_usd: float) -> WalkResult:
    """Buy up to size_usd USD up the ask ladder (ascending). Never extrapolates past the given levels.

    A level that is not a finite (0 < price < 1, size > 0) pair, or a ladder that is not ascending,
    makes the walk invalid (valid=False, complete=False). One touched level gives exactly its price.
    """
    budget = _finite(size_usd)
    levels: list[tuple[float, float]] = []
    valid = budget is not None and budget > 0
    for level in asks or ():
        try:
            price, size = level
        except (TypeError, ValueError):
            valid = False
            break
        price_f, size_f = _finite(price), _finite(size)
        if price_f is None or size_f is None or not 0.0 < price_f < 1.0 or size_f <= 0:
            valid = False
            break
        if levels and price_f < levels[-1][0]:
            valid = False
            break
        levels.append((price_f, size_f))
    if not valid or not levels:
        return WalkResult(math.nan, math.nan, 0.0, 0.0, False, valid and bool(levels))
    remaining = budget
    filled_usd = 0.0
    shares = 0.0
    touched: list[float] = []
    for price, size in levels:
        if remaining <= 0:
            break
        take = min(size, remaining / price)
        if take <= 0:
            break
        touched.append(price)
        shares += take
        cost = take * price
        filled_usd += cost
        remaining -= cost
        if take < size:  # this level covered the rest of the budget
            remaining = 0.0
    complete = remaining <= budget * 1e-12
    if not touched:
        return WalkResult(math.nan, math.nan, 0.0, 0.0, False, True)
    avg = touched[0] if len(set(touched)) == 1 else filled_usd / shares
    return WalkResult(avg, touched[-1], filled_usd, shares, complete, True)


def side_edge(p_side: float, exec_price: float, fee_rate: float,
              slippage: float = INTENT_ASSUMED_SLIPPAGE) -> EdgeBreakdown:
    """Buy-side edge, the very expression RiskGate evaluates: p - (price + slippage) - stress_fee.

    stress_fee None (non-finite input) or any non-finite number -> edge -1 (fail closed).
    """
    effective = exec_price + slippage if _finite(exec_price) is not None and _finite(slippage) is not None else math.nan
    stress = fees.stress_fee(fee_rate, exec_price, effective)
    if stress is None or _finite(p_side) is None:
        return EdgeBreakdown(p_side, exec_price, slippage, None, -1.0, effective)
    edge = p_side - effective - stress
    if not math.isfinite(edge):
        edge = -1.0
    return EdgeBreakdown(p_side, exec_price, slippage, stress, edge, effective)


def kelly_size(edge: EdgeBreakdown, *, kelly_fraction: float, max_order_pct: float,
               equity_usd: float, worst_price: float, tick_size: float,
               min_order_size: float) -> tuple[SizeBreakdown | None, list[str]]:
    """Size from the 1% clip; Kelly computed in shadow only (never makes the size larger or smaller).

    size_pct = min(max_order_pct, 0.01) (RiskGate's clip, what it books). limit_price = worst touched
    level rounded up to the tick; shares rounded DOWN to INTENT_SHARE_STEP; shares < min_order_size is
    size_below_min_order (never rounded up). kelly_raw = edge / (1 - c_all) with c_all the all-in cost per
    share; kelly_frac = kelly_fraction * kelly_raw; the shadow Kelly size is min(kelly_frac, size_pct).
    """
    reasons: list[str] = []
    c_all = edge.effective_price + (edge.fee_per_share if edge.fee_per_share is not None else math.nan)
    if _finite(c_all) is None or not 0.0 < c_all < 1.0 or edge.edge <= 0:
        kelly_raw = 0.0 if edge.edge <= 0 else math.nan
    else:
        kelly_raw = edge.edge / (1.0 - c_all)
    if edge.edge > 0 and not (math.isfinite(kelly_raw) and kelly_raw > 0):
        reasons.append("kelly_non_positive")
    kelly_frac = kelly_fraction * kelly_raw if math.isfinite(kelly_raw) else 0.0
    size_pct = min(max_order_pct, _HARD_CLIP_PCT)
    if not size_pct > 0 or not equity_usd > 0:
        reasons.append("zero_notional")
        return None, reasons
    cap_usd = equity_usd * size_pct
    kelly_size_pct = max(0.0, min(kelly_frac, size_pct))
    steps = worst_price / tick_size
    limit_steps = math.ceil(steps - 1e-9)  # up to the tick; the levels already sit on it (float guard only)
    limit_price = round(limit_steps * tick_size, 10)
    if not 0.0 < limit_price < 1.0:
        reasons.append("effective_price_out_of_range")
        return None, reasons
    shares = math.floor((cap_usd / limit_price) / INTENT_SHARE_STEP + 1e-9) * INTENT_SHARE_STEP
    shares = round(shares, 10)
    if shares * limit_price > cap_usd:  # the float guard above must never round up past the cap
        shares = round(shares - INTENT_SHARE_STEP, 10)
    if shares < min_order_size or shares <= 0:
        reasons.append("size_below_min_order")
    size_usd = shares * limit_price
    return SizeBreakdown(kelly_raw, kelly_frac, size_pct, size_usd, shares, limit_price,
                         kelly_size_pct, equity_usd * kelly_size_pct), reasons


def combine_probability(signals: Sequence[ProbabilitySignal],
                        override: ProbabilitySignal | None = None) -> tuple[ProbabilitySignal | None, list[str]]:
    """One probability per (market, outcome): an override alone, else one signal, else a log-odds blend.

    Override: source forecaster_override with a note, used alone. Blend: every signal on one market,
    each valid; logit spread of the point estimates (outcome 0) above MAX_SOURCE_LOGIT_GAP is
    source_conflict (no blend). Weights 1/sigma^2 from the logit half-width when every source has an
    interval, else equal. The blended interval is the union of the intervals (conservative); the
    signal time is the oldest component's, so the blend is never fresher than its stalest input.
    """
    if override is not None:
        if override.source != "forecaster_override" or not str(override.note or "").strip():
            return None, ["unsupported_signal_source"]
        problems = probability_problems(override)
        return (None, problems) if problems else (override, [])
    items = list(signals or ())
    if not items:
        return None, ["missing_signal"]
    if len(items) == 1:
        problems = probability_problems(items[0])
        return (None, problems) if problems else (items[0], [])
    reasons: list[str] = []
    for item in items:
        if item.source not in SOURCES or item.source in ("blend", "forecaster_override"):
            reasons.append("unsupported_signal_source")
        if not _valid_outcome_index(item.outcome_index):
            reasons.append("invalid_outcome_index")
        reasons.extend(probability_problems(item))
    if len({item.market_id for item in items}) != 1:
        reasons.append("market_mismatch")
    tokens = {(item.token_id, item.outcome_index) for item in items if item.token_id}
    if len({tok for tok, _ in tokens}) > 2 or len({idx for _, idx in tokens}) != len({tok for tok, _ in tokens}):
        reasons.append("source_conflict")
    if reasons:
        return None, list(_ordered(reasons))
    zero = [outcome_zero_interval(item) for item in items]
    logits = [_logit(p) for p, _, _ in zero]
    if max(logits) - min(logits) > MAX_SOURCE_LOGIT_GAP:
        return None, ["source_conflict"]
    widths = [(_logit(high) - _logit(low)) / 2.0 for _, low, high in zero]
    if all(width > 0 for width in widths):
        weights = [1.0 / (width * width) for width in widths]
    else:
        weights = [1.0] * len(items)
    blended = _sigmoid(sum(w * x for w, x in zip(weights, logits)) / sum(weights))
    low = min(item[1] for item in zero)
    high = max(item[2] for item in zero)
    blended = min(max(blended, low), high)
    stamps = [parse_book_ts(item.signal_ts) for item in items]
    if any(stamp is None for stamp in stamps):
        signal_ts = next(item.signal_ts for item, stamp in zip(items, stamps) if stamp is None)
    else:
        oldest = min(range(len(items)), key=lambda i: stamps[i])  # type: ignore[arg-type,return-value]
        signal_ts = items[oldest].signal_ts
    token0 = next((tok for tok, idx in tokens if idx == 0), None)
    ids = sorted(item.signal_id for item in items)
    blend_id = hashlib.sha256(json.dumps(ids, separators=(",", ":")).encode("utf-8")).hexdigest()
    note = "blend of " + ", ".join(sorted(f"{item.source}:{item.signal_id}" for item in items))
    return ProbabilitySignal(signal_id=blend_id, source="blend", market_id=items[0].market_id, outcome_index=0,
                             p=blended, p_low=low, p_high=high, token_id=token0, signal_ts=signal_ts,
                             note=note), []


def idempotency_key(signal_id: str, token_id: str, side: str, limit_price: float, shares: float,
                    book_ts: object) -> str:
    """sha256 of the canonical JSON of the fields that make an intent unique (same replay, same key)."""
    payload = [str(signal_id), str(token_id), str(side), repr(float(limit_price)), repr(float(shares)),
               book_ts if isinstance(book_ts, str) else repr(book_ts)]
    return hashlib.sha256(json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("ascii")).hexdigest()


# ---------------------------------------------------------------- the builder

def _price_band(source: str, cfg: Mapping[str, object]) -> tuple[float, float]:
    if source == "endgame_confirm":
        return ENDGAME_PRICE_MIN, ENDGAME_PRICE_MAX
    low, high = SCANNER_MIN_PRICE, SCANNER_MAX_PRICE
    # risk.toml may override the scanner band; here an override can only narrow it (tightening only).
    cfg_low, cfg_high = _finite(cfg.get("scanner_min_price")), _finite(cfg.get("scanner_max_price"))
    if cfg_low is not None:
        low = max(low, cfg_low)
    if cfg_high is not None:
        high = min(high, cfg_high)
    return low, high


def _book_usable(book: object, market_id: str, token_id: str) -> bool:
    return isinstance(book, BookSnapshot) and book.token_id == token_id and book.market_id == market_id


def build_order_intent(signal: ProbabilitySignal | None, market: MarketContext, books: Mapping[str, BookSnapshot],
                       cfg: Mapping[str, object], *, now: datetime, strategy: str, paper: bool = True,
                       side: str = "buy", seen_keys: frozenset[str] = frozenset(), held_shares: float = 0.0,
                       banned_categories: frozenset[str] = frozenset()) -> IntentResult:
    """Return an OrderIntent, or every reason it cannot be one (SPEC section 5 order). Never raises on data."""
    reasons: list[str] = []
    detail: dict = {"kelly_mode": "shadow"}
    now_ok = _aware(now)

    # A. config / mode
    drifted = pin_problems(dict(cfg))
    if drifted:
        reasons.append("phase0_pin:" + ";".join(drifted))
    if paper is not True and cfg.get("live_enabled") is not True:
        reasons.append("live_not_permitted")
    if any(token in str(strategy).lower() for token in BANNED_STRATEGY_TOKENS):
        reasons.append("banned_behavior")
    kelly_fraction = _finite(cfg.get("kelly_fraction"))
    if kelly_fraction is None or not 0.0 < kelly_fraction <= 1.0:
        reasons.append("invalid_kelly_fraction")
        kelly_fraction = None
    max_order_pct = _finite(cfg.get("max_order_pct"))
    if max_order_pct is None:
        reasons.append("invalid_max_order_pct")
    try:
        thresholds = gate_thresholds(dict(cfg))
    except RuntimeError:
        reasons.append("invalid_gate_threshold")
        thresholds = None

    # B. signal
    p0 = low0 = high0 = None
    if not isinstance(signal, ProbabilitySignal):
        reasons.append("missing_signal")
        signal = None
    else:
        if signal.source not in SOURCES and signal.source not in NO_PROBABILITY_SOURCES:
            reasons.append("unsupported_signal_source")
        if not _valid_outcome_index(signal.outcome_index):
            reasons.append("invalid_outcome_index")
        problems = probability_problems(signal)
        reasons.extend(problems)
        if not problems and _valid_outcome_index(signal.outcome_index):
            p0, low0, high0 = outcome_zero_interval(signal)
        if signal.market_id != market.market_id:
            reasons.append("market_mismatch")
        if not now_ok:
            reasons.append("invalid_now")
        elif signal.signal_ts is None or (isinstance(signal.signal_ts, str) and not signal.signal_ts.strip()):
            reasons.append("missing_signal_ts")
        else:
            stamp = parse_book_ts(signal.signal_ts)
            if stamp is None:
                reasons.append("invalid_signal_ts")
            else:
                age = (now - stamp).total_seconds()
                limit = MAX_SIGNAL_AGE_BY_SOURCE.get(signal.source, 0.0)
                if age < 0:
                    reasons.append("future_signal_ts")
                elif age > limit:
                    reasons.append("stale_signal")
                detail["signal_age_s"] = age

    # C. market
    tokens = tuple(str(t) for t in (market.token_ids or ()) if t is not None and str(t))
    if not tokens:
        reasons.append("missing_token_ids")
    elif len(tokens) != 2 or len(market.token_ids) != 2:
        reasons.append("not_binary_market")
    binary = len(tokens) == 2 and len(market.token_ids) == 2
    if (signal is not None and signal.token_id and binary and _valid_outcome_index(signal.outcome_index)
            and str(signal.token_id) != tokens[signal.outcome_index]):
        reasons.append("token_mismatch")
    if not str(market.condition_id or "").strip():
        reasons.append("missing_condition_id")
    if market.closed is not False:
        reasons.append("closed")
    if market.accepting_orders is not True:
        reasons.append("not_accepting_orders")
    if now_ok:
        problem = expiry_problem(market.end_date, now)
        if problem is not None:
            reasons.append(problem)
    if market.resolution_flag == "ambiguous":
        reasons.append("ambiguous_resolution")
    if market.category is not None and market.category in banned_categories:
        reasons.append("category_banned")
    fee_rate = _finite(market.fee_rate)
    if market.fee_rate is None:
        reasons.append("missing_fee_data")
    elif fee_rate is None:
        reasons.append("invalid_fee_rate")
    elif fee_rate < 0:
        reasons.append("negative_cost_assumption")

    # A sell is not built here (opening is buy only; SPEC 7-1): stop before any buy-side edge or size math.
    if side != "buy":
        held = _finite(held_shares)
        reasons.append("sell_requires_position" if side == "sell" and (held is None or held <= 0)
                       else "sell_not_supported")
        ordered = _ordered(reasons)
        return IntentResult(None, ordered, ordered[0], detail)

    # Edge per side (both are buys of their own token), probe size = the 1% clip.
    clip_pct = min(max_order_pct, _HARD_CLIP_PCT) if max_order_pct is not None else 0.0
    probe_usd = PAPER_EQUITY_USD * clip_pct
    sides: list[dict] = []
    for index in (0, 1):
        token = tokens[index] if binary else ""
        book = books.get(token) if token else None
        usable = _book_usable(book, market.market_id, token)
        walk = walk_asks(book.asks, probe_usd) if usable and probe_usd > 0 else None  # type: ignore[union-attr]
        p_side = None if p0 is None else (low0 if index == 0 else 1.0 - high0)  # type: ignore[operator]
        if walk is not None and walk.valid and walk.complete and p_side is not None:
            breakdown = side_edge(p_side, walk.avg_price, market.fee_rate if fee_rate is not None else math.nan)
        else:
            breakdown = EdgeBreakdown(p_side if p_side is not None else math.nan,
                                      walk.avg_price if walk is not None else math.nan,
                                      INTENT_ASSUMED_SLIPPAGE, None, -1.0)
        sides.append({"token": token, "book": book if usable else None, "raw_book": book, "walk": walk,
                      "edge": breakdown})
    edge_yes, edge_no = sides[0]["edge"].edge, sides[1]["edge"].edge
    chosen = 0 if abs(edge_yes - edge_no) <= EDGE_TOLERANCE or edge_yes >= edge_no else 1
    pick = sides[chosen]
    breakdown: EdgeBreakdown = pick["edge"]
    detail.update({"outcome_index": chosen, "edge_yes": edge_yes, "edge_no": edge_no, "probe_usd": probe_usd})

    # D. book of the chosen side
    book = pick["book"]
    raw_book = pick["raw_book"]
    if raw_book is None:
        reasons.append("missing_book")
    elif book is None:
        reasons.append("token_book_mismatch")
    if book is not None:
        if now_ok:
            problem = book_age_problem(book.book_ts, now, MAX_BOOK_AGE_S)
            if problem is not None:
                reasons.append(problem)
        spread = None if book.spread is None else _finite(book.spread)
        if book.spread is None:
            reasons.append("missing_spread")
        elif spread is None:
            reasons.append("invalid_spread")
        elif spread < 0:
            reasons.append("negative_spread")
        elif thresholds is not None and spread > thresholds["max_spread"]:
            reasons.append("spread_above_max")
    liquidity = None if market.liquidity_usd is None else _finite(market.liquidity_usd)
    if market.liquidity_usd is None:
        reasons.append("missing_liquidity")
    elif liquidity is None:
        reasons.append("invalid_liquidity")
    elif thresholds is not None and liquidity < thresholds["min_liquidity_usd"]:
        reasons.append("liquidity_below_min")
    walk: WalkResult | None = pick["walk"]
    if book is not None:
        if walk is None or not walk.valid:
            if probe_usd > 0:
                reasons.append("invalid_book_levels")
        elif not walk.complete:
            reasons.append("insufficient_depth")
        tick = _finite(book.tick_size)
        min_order = _finite(book.min_order_size)
        if tick is None or tick <= 0:
            reasons.append("missing_tick_size")
        if min_order is None or min_order < 0:
            reasons.append("missing_min_order_size")
    else:
        tick = min_order = None

    # E. price band (by source)
    source = signal.source if signal is not None else ""
    band = _price_band(source, cfg)
    exec_price = breakdown.exec_price
    if _finite(exec_price) is not None and not band[0] <= exec_price <= band[1]:
        reasons.append("price_out_of_band")
    detail["price_band"] = band

    # F. edge
    if breakdown.fee_per_share is None and p0 is not None and walk is not None and walk.valid and walk.complete:
        reasons.append("invalid_fee")  # stress_fee returned None (a non-finite fee_rate or price): edge is -1
    effective = breakdown.effective_price
    if _finite(effective) is not None and not 0.0 < effective < 1.0:
        reasons.append("effective_price_out_of_range")
    edge = breakdown.edge
    if edge <= 0:
        reasons.append("non_positive_edge")
    elif edge < MIN_EDGE - EDGE_TOLERANCE:
        reasons.append("edge_below_min")
    detail["edge"] = edge

    # G. size (1% clip; Kelly shadow)
    size: SizeBreakdown | None = None
    if max_order_pct is not None and clip_pct <= 0:
        reasons.append("zero_notional")
    elif (max_order_pct is not None and kelly_fraction is not None and tick is not None and tick > 0
          and min_order is not None and min_order >= 0 and walk is not None and walk.valid and walk.complete
          and breakdown.fee_per_share is not None):
        size, size_reasons = kelly_size(breakdown, kelly_fraction=kelly_fraction, max_order_pct=max_order_pct,
                                        equity_usd=PAPER_EQUITY_USD, worst_price=walk.worst_price, tick_size=tick,
                                        min_order_size=min_order)
        reasons.extend(size_reasons)
        if size is not None:
            detail.update({"kelly_raw": size.kelly_raw, "kelly_frac": size.kelly_frac, "size_pct": size.size_pct,
                           "kelly_size_pct": size.kelly_size_pct, "size_usd": size.size_usd,
                           "shares": size.shares, "limit_price": size.limit_price})

    # B #12: duplicate (needs the key, so it is computed last but ordered in group B)
    key = None
    if signal is not None and size is not None and binary:
        key = idempotency_key(signal.signal_id, pick["token"], "buy", size.limit_price, size.shares,
                              book.book_ts if book is not None else None)
        if key in seen_keys:
            reasons.append("duplicate_intent")
        detail["idempotency_key"] = key

    ordered = _ordered(reasons)
    if ordered:
        return IntentResult(None, ordered, ordered[0], detail)

    # Every check passed, so every value below is set; re-check explicitly (no assert: it must survive -O).
    if (signal is None or size is None or book is None or walk is None or key is None or kelly_fraction is None
            or fee_rate is None or p0 is None or low0 is None or high0 is None or not now_ok):  # pragma: no cover
        return IntentResult(None, ("internal_inconsistency",), "internal_inconsistency", detail)
    marks = ["top_of_book_only"] if len(book.asks) == 1 else []
    if size.kelly_frac < size.size_pct:
        marks.insert(0, "kelly_not_enforced_by_riskgate")
    expiry_end = parse_end_date(market.end_date)
    book_stamp = parse_book_ts(book.book_ts)
    expires = min(now + timedelta(seconds=INTENT_TTL_S), book_stamp + timedelta(seconds=MAX_BOOK_AGE_S),  # type: ignore[operator]
                  expiry_end - timedelta(hours=MIN_HOURS_TO_EXPIRY))  # type: ignore[operator]
    outcomes = tuple(market.outcomes or ())
    intent = OrderIntent(
        intent_id=key[:32], idempotency_key=key, signal_id=signal.signal_id, signal_source=signal.source,
        strategy=strategy, market_id=market.market_id, condition_id=market.condition_id, outcome_index=chosen,
        outcome=str(outcomes[chosen]) if len(outcomes) > chosen else "", token_id=pick["token"], side="buy",
        limit_price=size.limit_price, exec_price=breakdown.exec_price, best_ask=book.best_ask,
        best_bid=book.best_bid, spread=book.spread, liquidity_usd=market.liquidity_usd, size_usd=size.size_usd,
        shares=size.shares, p_model=p0, p_low=low0, p_high=high0, p_side=breakdown.p_side,  # type: ignore[arg-type]
        assumed_slippage=INTENT_ASSUMED_SLIPPAGE, fee_rate=market.fee_rate,  # type: ignore[arg-type]
        fee_per_share=breakdown.fee_per_share, edge=edge, edge_yes=edge_yes,  # type: ignore[arg-type]
        edge_no=edge_no, kelly_raw=size.kelly_raw, kelly_fraction=kelly_fraction, kelly_frac=size.kelly_frac,
        kelly_size_pct=size.kelly_size_pct, kelly_size_usd=size.kelly_size_usd, size_pct=size.size_pct,
        reasons=tuple(marks), created_at=_iso(now), expires_at=_iso(expires), book_ts=book.book_ts,
        signal_ts=signal.signal_ts, paper=True, note=signal.note, counterparty=None,
        decision_id=make_decision_id(strategy, market.market_id, pick["token"], signal.forecast_id),
        forecast_id=signal.forecast_id, forecast_ts=signal.forecast_ts, model_version=signal.model_version,
        category=market.category if market.category is not None else market.gamma_category, tags=market.tags,
        end_date=market.end_date or None,
    )
    return IntentResult(intent, (), None, detail)


def to_risk_intent(oi: OrderIntent) -> Intent:
    """The risk_gate.Intent for this OrderIntent (never hypothetical, no counterparty). Size is not
    passed: RiskGate books its own 1% clip until a size_usd field exists (KEY, backlog #7)."""
    return Intent(
        strategy=oi.strategy,
        market=oi.market_id,
        side=oi.side,
        model_prob=oi.p_side,
        market_price=oi.exec_price,
        assumed_slippage=oi.assumed_slippage,
        fee_rate=oi.fee_rate,
        liquidity_usd=oi.liquidity_usd,
        spread=oi.spread,
        token_id=oi.token_id,
        hypothetical=False,
        note=oi.note,
        book_ts=oi.book_ts,
        counterparty=None,
        decision_id=oi.decision_id,
        forecast_id=oi.forecast_id,
        forecast_ts=oi.forecast_ts,
        condition_id=oi.condition_id or None,
        outcome_index=oi.outcome_index,
        outcome=oi.outcome or None,
        category=oi.category,
        tags=oi.tags,
        end_date=oi.end_date,
        model_version=oi.model_version,
    )


# ---------------------------------------------------------------- adapters (pure; the runner reads the files)

def signal_id_for(row: Mapping[str, object]) -> str:
    """sha256 of a bus row's ts + from + market (SPEC section 4: signal_id)."""
    parts = [str(row.get("ts") or ""), str(row.get("from") or ""), str(row.get("market") or "")]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def signal_from_bus(row: Mapping[str, object]) -> ProbabilitySignal | None:
    """A NewsForecaster / SportsDevig / Endgame bus signal as a ProbabilitySignal; anything else is None.

    NewsForecaster: p = model_prob, outcome_index from the payload or 0 (legacy rows). SportsDevig:
    p = fair_prob, outcome 0; its own edge is not used. Endgame: no probability (missing_model_prob).
    """
    if row.get("type") != "signal":
        return None
    payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else {}
    frm = row.get("from")
    common = {"signal_id": str(payload.get("forecast_id") or signal_id_for(row)), "market_id": str(row.get("market") or ""),
              "signal_ts": row.get("ts"), "note": str(payload.get("note") or ""),
              # Task AX: carried, never synthesised (signal_id above may be a hash; forecast_id is not)
              "forecast_id": _id_or_none(payload.get("forecast_id")),
              "forecast_ts": _id_or_none(payload.get("forecast_ts")),
              "model_version": _id_or_none(payload.get("model_version"))}
    index = payload.get("outcome_index", 0)
    token = payload.get("token_id")
    token = str(token) if token not in (None, "") else None
    if frm == "NewsForecaster":
        return ProbabilitySignal(source="news", outcome_index=index, p=payload.get("model_prob"),  # type: ignore[arg-type]
                                 p_low=payload.get("p_low"), p_high=payload.get("p_high"), token_id=token,  # type: ignore[arg-type]
                                 **common)
    if frm == "SportsDevig":
        return ProbabilitySignal(source="sports_devig", outcome_index=0, p=payload.get("fair_prob"),  # type: ignore[arg-type]
                                 token_id=token, **common)
    if frm == "Endgame":
        return ProbabilitySignal(source="endgame", outcome_index=index, p=None, token_id=token, **common)  # type: ignore[arg-type]
    return None


def _id_or_none(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def book_snapshot_from_row(row: Mapping[str, object]) -> BookSnapshot:
    """A data/books.jsonl (or ws) row: top of book only, so one ask and one bid level at most."""
    def level(price_key: str, size_key: str) -> tuple[tuple[float, float], ...]:
        price, size = row.get(price_key), row.get(size_key)
        if price is None or size is None:
            return ()
        return ((price, size),)  # type: ignore[return-value]
    return BookSnapshot(market_id=str(row.get("market_id") or ""), token_id=str(row.get("token_id") or ""),
                        asks=level("best_ask", "best_ask_size"), bids=level("best_bid", "best_bid_size"),
                        best_bid=row.get("best_bid"), best_ask=row.get("best_ask"),  # type: ignore[arg-type]
                        spread=row.get("spread"), book_ts=row.get("book_ts"),  # type: ignore[arg-type]
                        tick_size=row.get("tick_size"), min_order_size=row.get("min_order_size"))  # type: ignore[arg-type]


def market_context_from_row(row: Mapping[str, object], *, fee_rate: float | None, category: str | None = None,
                            resolution_flag: str | None = None) -> MarketContext:
    """A data/markets.jsonl scanner row. fee_rate is resolved by the caller (no src module derives it yet)."""
    return MarketContext(market_id=str(row.get("id") or ""), condition_id=str(row.get("condition_id") or ""),
                         token_ids=tuple(str(t) for t in (row.get("token_ids") or ())),  # type: ignore[union-attr]
                         outcomes=tuple(str(o) for o in (row.get("outcomes") or ())),  # type: ignore[union-attr]
                         end_date=row.get("end_date") or "", liquidity_usd=row.get("liquidity_usd"),  # type: ignore[arg-type]
                         fee_rate=fee_rate, accepting_orders=row.get("accepting_orders"),  # type: ignore[arg-type]
                         closed=row.get("closed") is not False, category=category, resolution_flag=resolution_flag,
                         gamma_category=_id_or_none(row.get("category")), tags=_tags(row.get("tags")))


def _tags(value: object) -> tuple[str, ...] | None:
    """Scanner row tags (Task AX): a list of strings -> tuple; anything else (missing) -> None."""
    if not isinstance(value, (list, tuple)):
        return None
    return tuple(str(item) for item in value if isinstance(item, str) and item)
