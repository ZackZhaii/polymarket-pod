"""Issue #197 (Task CK, KEY-approved "close-then-write-off"): confirm that a held market closed; plan its write-offs.

家伟 approved Poly风控's rule on 2026-10-11 09:47 (Beijing), issue #197 comment 6104331631, with Poly研究's
trigger (/workspace/pod-bots/poly-research-closed-flag.md). Paper only; no risk.toml key changes.

Trigger (``qualifies``), on one Gamma market row, all three:

* ``closed is True`` (a JSON boolean, nothing else),
* ``acceptingOrders is False``,
* ``closedTime`` a zoned timestamp (``expiry_rule.parse_end_date``: Gamma's ``2025-03-25 00:21:27+00`` too) that is
  not in the future.

Confirmation (``CloseTracker``): one qualifying read never writes anything off. A close is confirmed by

* a second qualifying Gamma read at least MARKET_CLOSE_RECHECK_S (30 s, in [30, 60]) after the first, with the
  same closedTime (a read that does not qualify in between starts over), or
* a second source after the first qualifying read: CLOB ``accepting_orders is False`` or Data API
  ``tokens[].closed is True`` (``second_source_confirms``; the runner does not call either API today, its
  allowlist has Gamma only, so it uses the re-read).

Never a trigger on its own: ``endDate``, ``active``, ``uma*`` (none is read here), an empty list (``gamma_read``
reports it as a read failure, never as closed or open), a WebSocket ``market_resolved``.

Held legs are queried actively (``gamma_read``): the scanner lists ``closed=false`` markets only, and Gamma's
``/markets`` filters closed markets out unless ``closed=true`` is passed, so each held market is read by id
twice, with and without ``closed=true``.

Write-off (``plan_writeoffs``): for every open leg of a confirmed market one ledger row (src/ledger_store.py
(f)) whose ``writeoff_usd`` is the leg's whole cost (src/daily_loss.writeoff_amount). The row's fill_id is
derived from (market, token_id, closed_ts): booking it again is skipped, so a restart never writes a leg off
twice. Any read failure or contradiction (a written-off market that reads open again or with another
closedTime, a leg written off for another closedTime) is a problem: the caller records invalid_risk_state as a
global halt.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterable
from urllib.parse import urlencode

from src import podlib
from src.daily_loss import daily_loss, writeoff_amount
from src.expiry_rule import parse_end_date
from src.ledger_store import (
    FILL_ID_FIELD,
    KIND_FIELD,
    WRITEOFF_DECISION,
    WRITEOFF_KIND,
    counts,
    is_writeoff,
    leg_key,
)
from src.thresholds import LEDGER_OPEN_EPS, MARKET_CLOSE_RECHECK_S

WRITEOFF_ID_PREFIX = "writeoff-"
STRATEGY = "writeoff"
BY_REREAD = "gamma_reread"
BY_CLOB = "clob_accepting_orders"
BY_DATA = "data_tokens_closed"


@dataclass(frozen=True)
class Observation:
    """One Gamma read of a market's close state. ``problem`` set: the read failed (nothing else is meaningful)."""

    market: str
    observed_at: datetime
    closed: object = None
    accepting_orders: object = None
    closed_time: datetime | None = None
    raw_closed_time: object = None
    problem: str | None = None


@dataclass(frozen=True)
class Close:
    """A confirmed close of ``market``."""

    market: str
    closed_time: datetime
    first_seen: datetime
    confirmed_at: datetime
    confirmed_by: str

    @property
    def closed_ts(self) -> str:
        return self.closed_time.isoformat()


def qualifies(obs: Observation, now: datetime) -> bool:
    """The approved trigger on one read: closed is True, acceptingOrders is False, closedTime parseable, not future."""
    return (obs.problem is None and obs.closed is True and obs.accepting_orders is False
            and obs.closed_time is not None and obs.closed_time <= now)


def gamma_url(market_id: str, *, closed: bool) -> str:
    query = {"id": market_id, "closed": "true"} if closed else {"id": market_id}
    return podlib.GAMMA_PREFIX + "?" + urlencode(query)


def _match(data: object, market_id: str) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(data, list):
        return None, "gamma_bad_response"
    found = [row for row in data if isinstance(row, dict) and str(row.get("id")) == str(market_id)]
    if len(found) > 1:
        return None, "gamma_duplicate_market"
    return (found[0] if found else None), None


def observation_from_row(market_id: str, row: dict[str, Any], now: datetime) -> Observation:
    raw = row.get("closedTime")
    return Observation(market=str(market_id), observed_at=now, closed=row.get("closed"),
                       accepting_orders=row.get("acceptingOrders"), closed_time=parse_end_date(raw),
                       raw_closed_time=raw)


def gamma_read(getter: Callable[[str], Any], market_id: str, now: datetime) -> Observation:
    """Read one held market by id, with ``closed=true`` and without (the default filters closed markets out).

    Both lists empty is a read failure (never "open", never "closed"). A market in both lists with another
    close state is read as not qualifying (Gamma's views can split for a minute or two around a close).
    """
    rows = []
    for closed in (True, False):
        try:
            data = getter(gamma_url(market_id, closed=closed))
        except Exception as exc:  # noqa: BLE001 - any failed read is a read failure (the caller halts)
            return Observation(str(market_id), now, problem=f"gamma_read_failed:{type(exc).__name__}")
        row, problem = _match(data, market_id)
        if problem:
            return Observation(str(market_id), now, problem=problem)
        if row is not None:
            rows.append(row)
    if not rows:
        return Observation(str(market_id), now, problem="gamma_market_missing")
    views = [observation_from_row(market_id, row, now) for row in rows]
    if len(views) == 2 and qualifies(views[0], now) != qualifies(views[1], now):
        return Observation(str(market_id), now, closed=False, accepting_orders=None)  # split views: not closed
    return views[0]


def second_source_confirms(source: str, raw: object, token_ids: Iterable[str] = ()) -> bool:
    """CLOB ``/markets/{condition_id}``: ``accepting_orders is False``; Data ``/v2/tokens``: every held token
    ``closed is True``. Anything else (missing, null, a string) confirms nothing."""
    if source == BY_CLOB:
        return isinstance(raw, dict) and raw.get("accepting_orders") is False
    if source == BY_DATA:
        tokens = raw.get("tokens") if isinstance(raw, dict) else raw
        wanted = {str(token) for token in token_ids}
        if not isinstance(tokens, list) or not wanted:
            return False
        seen = {str(row.get("token_id")): row.get("closed") for row in tokens if isinstance(row, dict)}
        return all(seen.get(token) is True for token in wanted)
    return False


@dataclass
class CloseTracker:
    """First qualifying reads per market (in memory: a restart re-reads; the ledger keeps every write-off)."""

    first: dict[str, Observation] = field(default_factory=dict)

    def update(self, obs: Observation, now: datetime) -> Close | None:
        """Feed one Gamma read; return the Close it confirms (re-read), else None."""
        if obs.problem is not None:
            return None  # the caller treats the failure; the pending first read stays as it was
        if not qualifies(obs, now):
            self.first.pop(obs.market, None)  # a read that does not qualify starts over
            return None
        first = self.first.get(obs.market)
        if first is None or first.closed_time != obs.closed_time:
            self.first[obs.market] = obs
            return None
        if (obs.observed_at - first.observed_at).total_seconds() < MARKET_CLOSE_RECHECK_S:
            return None
        return Close(obs.market, obs.closed_time, first.observed_at, obs.observed_at, BY_REREAD)  # type: ignore[arg-type]

    def confirm_by_second_source(self, market: str, source: str, raw: object, now: datetime,
                                 token_ids: Iterable[str] = ()) -> Close | None:
        """A second source after a qualifying Gamma read confirms at once (never on its own)."""
        first = self.first.get(str(market))
        if first is None or first.observed_at > now or not second_source_confirms(source, raw, token_ids):
            return None
        return Close(first.market, first.closed_time, first.observed_at, now, source)  # type: ignore[arg-type]


def writeoff_fill_id(market: str, token_id: str, closed_ts: str) -> str:
    digest = hashlib.sha256(f"{market}|{token_id}|{closed_ts}".encode("utf-8")).hexdigest()
    return WRITEOFF_ID_PREFIX + digest[:32]


def held_markets(rows: Iterable[dict[str, Any]]) -> dict[str, list[str]]:
    """Open legs per market (counted rows, ledger order): {market: [token_id, ...]}."""
    legs: dict[tuple[str, str], float] = {}
    for row in rows:
        if not counts(row):
            continue
        qty = float(row.get("shares") or 0.0)
        key = leg_key(row.get("market"), row.get("token_id"))
        legs[key] = legs.get(key, 0.0) + (qty if row.get("side") == "buy" else -qty)
    out: dict[str, list[str]] = {}
    for (market, token), shares in sorted(legs.items()):
        if abs(shares) > LEDGER_OPEN_EPS and market:
            out.setdefault(market, []).append(token)
    return out


def written_off_legs(rows: Iterable[dict[str, Any]]) -> dict[tuple[str, str], str]:
    """Open legs that carry a write-off row now: {(market, token): closed_ts}."""
    shares: dict[tuple[str, str], float] = {}
    out: dict[tuple[str, str], str] = {}
    for row in rows:
        if not counts(row):
            continue
        key = leg_key(row.get("market"), row.get("token_id"))
        if is_writeoff(row):
            out[key] = str(row.get("closed_ts"))
            continue
        qty = float(row.get("shares") or 0.0)
        shares[key] = shares.get(key, 0.0) + (qty if row.get("side") == "buy" else -qty)
        if abs(shares[key]) <= LEDGER_OPEN_EPS:
            out.pop(key, None)
    return out


def contradiction(obs: Observation, closed_ts: str, now: datetime) -> str | None:
    """A read of a market whose legs are written off must still qualify, with the same closedTime."""
    if obs.problem is not None:
        return None  # a read failure is reported as such by the caller
    if not qualifies(obs, now):
        return "reads_open_after_writeoff"
    if obs.closed_time is None or obs.closed_time != parse_end_date(closed_ts):
        return "closed_time_changed"
    return None


@dataclass
class WriteoffPlan:
    rows: list[dict[str, Any]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def plan_writeoffs(ledger_rows: list[dict[str, Any]], ledger_ids: list[str], closes: Iterable[Close],
                   now: str) -> WriteoffPlan:
    """The write-off rows to book now for the confirmed closes (none for a leg already written off)."""
    result = WriteoffPlan()
    day = daily_loss(ledger_rows, now[:10], ledger_ids)
    if day.problems:
        result.problems = [f"writeoff_ledger_invalid:{problem}" for problem in day.problems]
        return result
    booked = set(ledger_ids)
    already = written_off_legs(ledger_rows)
    held = held_markets(ledger_rows)
    for close in closes:
        for token in held.get(close.market, []):
            key = leg_key(close.market, token)
            fill_id = writeoff_fill_id(close.market, token, close.closed_ts)
            if key in already:
                if fill_id not in booked or parse_end_date(already[key]) != close.closed_time:
                    result.problems.append(f"writeoff_conflict:{close.market}|{token}:closed_time_changed")
                continue  # written off once: never again (restart-safe)
            if fill_id in booked:
                result.problems.append(f"writeoff_conflict:{close.market}|{token}:id_booked_leg_not_written_off")
                continue
            leg = day.legs.get(key)
            amount = None if leg is None else writeoff_amount(leg)
            if leg is None or amount is None:
                result.problems.append(f"writeoff_conflict:{close.market}|{token}:leg_not_open")
                continue
            result.rows.append({
                FILL_ID_FIELD: fill_id,
                "ts": now,
                KIND_FIELD: WRITEOFF_KIND,
                "strategy": STRATEGY,
                "market": close.market,
                "token_id": token,
                "side": "sell" if leg["shares"] > 0 else "buy",
                "shares": 0.0,
                "market_price": 0.0,
                "effective_price": 0.0,
                "notional_usd": 0.0,
                "fee_rate": 0.0,
                "fee_usd": 0.0,
                "hypothetical": False,
                "counts_toward_risk": True,
                "risk_decision": WRITEOFF_DECISION,
                "writeoff_shares": abs(leg["shares"]),
                "writeoff_usd": amount,
                "closed_ts": close.closed_ts,
                "close_first_seen_ts": close.first_seen.isoformat(),
                "close_confirmed_ts": close.confirmed_at.isoformat(),
                "close_confirmed_by": close.confirmed_by,
                "note": "Paper write-off of a closed market's open leg at its full cost (#197, KEY-approved). "
                        "No exchange action; the leg holds its limits until it settles.",
            })
    if result.problems:
        result.rows = []  # any contradiction: write nothing off at all (the caller halts)
    return result
