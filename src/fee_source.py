"""Issue #117 (Task BZ): per-market fee rate from Gamma, the M2-A Gamma-only slice. Pure + an injected cache.

``feeSchedule.rate`` is the market's fee rate (2026-10-10 public reads: 0.03-0.07, exponent 1, takerOnly true on
fee markets; every field null on free ones). ``makerBaseFee`` / ``takerBaseFee`` (1000 on every fee market) are a
signing cap, NOT a rate, and are never read. The fee formula (src/fees.py) is unchanged.

Rules (anything else is ``fee_rate`` None with a reason; the caller rejects: intent_builder's ``missing_fee_data``):
- ``feesEnabled`` must be a real bool (missing / null / "true" / 1 -> ``fees_enabled_unknown``; issue #116).
- True: ``feeSchedule`` an object with ``rate`` a finite number (not a string or bool) in [0, FEE_RATE_MAX],
  ``exponent`` a finite number >= 1 (below 1 the linear formula is not guaranteed conservative:
  ``fee_schedule_unsupported``) and ``takerOnly`` exactly True.
- False: ``feeSchedule`` null/absent, or with a rate of exactly 0 -> 0.0. A non-zero rate on a market Gamma calls free
  is ``fee_schedule_on_free_market``.
- A getter that raises or returns a non-object is ``gamma_unreadable``.

NOT in this slice (issue #117 stays open): the CLOB ``fd`` cross-check (waits on #155, the /clob-markets allowlist),
so every quote says ``cross_checked`` False; the fee_source / fetched_at / schedule_sha pass-through to the
OrderIntent, risk_gate.Intent and fill; and the runner wiring (M2-C). This module fetches nothing itself: FeeCache
calls a getter its caller supplies. No network, no new URL, no orders, no config/risk.toml edits.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone

from src.thresholds import FEE_CACHE_TTL_S, FEE_NEG_CACHE_S, FEE_RATE_MAX

SOURCE_GAMMA = "gamma"

FEES_ENABLED_UNKNOWN = "fees_enabled_unknown"
FEE_SCHEDULE_MISSING = "fee_schedule_missing"
FEE_RATE_INVALID = "fee_rate_invalid"
FEE_RATE_OUT_OF_RANGE = "fee_rate_out_of_range"
FEE_SCHEDULE_UNSUPPORTED = "fee_schedule_unsupported"
FEE_NOT_TAKER_ONLY = "fee_not_taker_only"
FEE_SCHEDULE_ON_FREE_MARKET = "fee_schedule_on_free_market"
GAMMA_UNREADABLE = "gamma_unreadable"
PROBLEMS = (FEES_ENABLED_UNKNOWN, FEE_SCHEDULE_MISSING, FEE_RATE_INVALID, FEE_RATE_OUT_OF_RANGE,
            FEE_SCHEDULE_UNSUPPORTED, FEE_NOT_TAKER_ONLY, FEE_SCHEDULE_ON_FREE_MARKET, GAMMA_UNREADABLE)


@dataclass(frozen=True)
class FeeQuote:
    """One market's fee as read from Gamma. ``fee_rate`` None means rejected (``problem`` says why)."""

    market_id: str
    fee_rate: float | None
    problem: str | None
    fee_source: str = SOURCE_GAMMA
    fee_gamma_fetched_at: str | None = None
    fee_schedule_sha: str | None = None
    cross_checked: bool = False  # the CLOB fd cross-check is not in this slice (#117, waits on #155)


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def schedule_sha(market: Mapping[str, object]) -> str | None:
    """sha256 of the canonical JSON of {feesEnabled, feeSchedule} (recorded only); None when not serialisable."""
    body = {"feesEnabled": market.get("feesEnabled"), "feeSchedule": market.get("feeSchedule")}
    try:
        text = json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_gamma_fee(market: Mapping[str, object]) -> tuple[float | None, str | None]:
    """(fee_rate, None) or (None, problem) for one raw Gamma market object. Pure."""
    enabled = market.get("feesEnabled")
    if not isinstance(enabled, bool):
        return None, FEES_ENABLED_UNKNOWN
    schedule = market.get("feeSchedule")
    if enabled is False:
        if schedule is None:
            return 0.0, None
        if not isinstance(schedule, Mapping):
            return None, FEE_SCHEDULE_ON_FREE_MARKET
        raw_rate = schedule.get("rate")
        if raw_rate is None:
            return 0.0, None
        rate = _finite_number(raw_rate)
        if rate is None:
            return None, FEE_RATE_INVALID
        return (0.0, None) if rate == 0.0 else (None, FEE_SCHEDULE_ON_FREE_MARKET)
    if not isinstance(schedule, Mapping):
        return None, FEE_SCHEDULE_MISSING
    rate = _finite_number(schedule.get("rate"))
    if rate is None:
        return None, FEE_RATE_INVALID
    if not 0.0 <= rate <= FEE_RATE_MAX:
        return None, FEE_RATE_OUT_OF_RANGE
    exponent = _finite_number(schedule.get("exponent"))
    if exponent is None or exponent < 1.0:
        return None, FEE_SCHEDULE_UNSUPPORTED
    if schedule.get("takerOnly") is not True:
        return None, FEE_NOT_TAKER_ONLY
    return rate, None


def resolve_fee(market_id: str, gamma_market: object, *, fetched_at: str | None = None) -> FeeQuote:
    """The Gamma-only quote for one market. A non-object (unreadable) response is GAMMA_UNREADABLE."""
    if not isinstance(gamma_market, Mapping):
        return FeeQuote(str(market_id), None, GAMMA_UNREADABLE, fee_gamma_fetched_at=fetched_at)
    rate, problem = parse_gamma_fee(gamma_market)
    return FeeQuote(str(market_id), rate, problem, fee_gamma_fetched_at=fetched_at,
                    fee_schedule_sha=schedule_sha(gamma_market))


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class FeeCache:
    """Per-market FeeQuote cache over an injected ``getter(market_id) -> Gamma market object``.

    A usable quote is reused for FEE_CACHE_TTL_S, a rejected or unreadable one for FEE_NEG_CACHE_S; after that the
    getter is called again. An expired quote is never returned instead of a fresh read: a failing re-read gives a
    GAMMA_UNREADABLE quote (fee_rate None). ``clock`` is monotonic seconds, ``wall`` the ISO fetched_at stamp.
    """

    def __init__(self, getter: Callable[[str], object], *, ttl_s: float = FEE_CACHE_TTL_S,
                 neg_ttl_s: float = FEE_NEG_CACHE_S, clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], str] = _utc_now_iso) -> None:
        self._getter = getter
        self._ttl_s = float(ttl_s)
        self._neg_ttl_s = float(neg_ttl_s)
        self._clock = clock
        self._wall = wall
        self._entries: dict[str, tuple[float, FeeQuote]] = {}

    def get(self, market_id: str) -> FeeQuote:
        key = str(market_id)
        now = self._clock()
        cached = self._entries.get(key)
        if cached is not None:
            expires, quote = cached
            if now < expires:
                return quote
            del self._entries[key]  # expired: never a fallback
        fetched_at = self._wall()
        try:
            raw = self._getter(key)
        except Exception:  # noqa: BLE001 - any read failure is an unreadable fee (fail closed), never free
            raw = None
        quote = resolve_fee(key, raw, fetched_at=fetched_at)
        ttl = self._ttl_s if quote.fee_rate is not None else self._neg_ttl_s
        self._entries[key] = (now + ttl, quote)
        return quote

    def fee_rate(self, market_id: str) -> float | None:
        """The rate for intent_builder.MarketContext.fee_rate: None (-> missing_fee_data) unless usable."""
        return self.get(market_id).fee_rate

    def invalidate_all(self) -> None:
        self._entries.clear()
