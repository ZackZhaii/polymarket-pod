"""Issue #197 (Task CK, KEY-approved "close-then-write-off"): confirm that a held market closed; plan its write-offs.

家伟 approved Poly风控's rule on 2026-10-11 09:47 (Beijing), issue #197 comment 6104331631, with Poly研究's trigger
(/workspace/pod-bots/poly-research-closed-flag.md). Poly风控's rulings issuecomment-6106669720 and -6106700917 decide
the failure handling below. Paper only; no risk.toml change.

Read (``gamma_read``). Each held market is read from Gamma with an explicit ``/markets?id=<id>&closed=true`` (the
default query without ``closed`` filters a closed market out, so it is never used to see a close; it is read only
to see that a market is open). The market in neither list is **unknown** (``gamma_market_missing``), never "open"
or "closed". The URL path is the allowlisted Gamma ``/markets``; no new host.

Trigger (``predicate``), on one read, all of: ``closed is True``; ``acceptingOrders is False``; ``closedTime`` a
zoned timestamp (Gamma's ``2025-03-25 00:21:27+00`` too) not in the future. ``closed`` that is not a JSON boolean,
or ``acceptingOrders`` that is neither a boolean nor null, is ``close_read_invalid_type`` (no truth conversion).
``endDate``, ``active``, ``uma*``, an empty list or a WebSocket message never trigger anything.

Confirmation (``CloseTracker``, in memory: a restart re-confirms, #124 keeps the worst case meanwhile): a second
read at least MARKET_CLOSE_RECHECK_S (30 s, no upper bound) after the first qualifying one, that qualifies too with
the same closedTime. A second source (CLOB ``accepting_orders is False`` / Data ``tokens[].closed is True``) can
confirm only once its endpoints are allowlisted (#155 / #115, KEY); until then the runner uses the re-read
(``expected_reread_only``). **No halt before a write-off**: a read failure, an invalid type, a re-read that does
not qualify, a changed closedTime or a disagreeing second source drops the pending read; nothing is written off
this tick and the leg keeps its #124 worst-case unrealized loss.

After the write-off (``reopen_contradiction``): the market is still read on every tick until its settlement is
booked, only to find a contradiction. ``closed is False`` or ``acceptingOrders is True`` is a halt
(``closed_flag_contradiction_after_writeoff``, invalid_risk_state; the write-off is not rolled back, never
repeated), except within MARKET_CLOSE_REOPEN_GRACE_S of the first closed read: Gamma's by-id view needs 33-309 s to
settle on closed=true, so an early not-closed read is lag, not a contradiction. A read failure, an empty list,
a non-boolean value or another closedTime is ignored.

Write-off (``plan_writeoffs``): one ledger row per open leg of a confirmed market (YES and NO separately, never
netted), keyed by leg_key: fill_id = ``writeoff-`` + sha256(market|token)[:32]. ``writeoff_usd`` is the leg's
whole cost basis at average cost (a long: what it paid, fees excluded: they were counted on the fill day; a short:
shares x 1 - sale proceeds), plus (RiskGate's correction, issuecomment-6109537257, tightening only) the start-of-day
mark above that cost for the shares held since the start of the UTC day (src/leg_marks.writeoff_sod_uplift, from the
day's snapshot; none yet today: 0), booked as ``writeoff_cost_usd`` + ``writeoff_sod_uplift_usd``. Shares bought
today stay at cost. A leg already written off is skipped (restart-safe). Two different write-off rows for
one key are a ledger integrity problem (src/ledger_store.py (f)): invalid_risk_state.
"""
from __future__ import annotations

import dataclasses

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urlencode

from src import leg_marks, podlib
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
from src.thresholds import CLOCK_SKEW_MAX_S, LEDGER_OPEN_EPS, MARKET_CLOSE_RECHECK_S, MARKET_CLOSE_REOPEN_GRACE_S

WRITEOFF_ID_PREFIX = "writeoff-"
STRATEGY = "writeoff"
BY_REREAD = "gamma_reread"
BY_CLOB = "clob_accepting_orders"
BY_DATA = "data_tokens_closed"

# close reasons (one per market per tick; the vector codes of /workspace/pod-bots/poly-forecast-197-vectors.json)
CONFIRMED = "writeoff_confirmed_reread"
AWAITING = "awaiting_confirmation"
TOO_SOON = "reread_too_soon"
TIME_CHANGED = "confirmation_failed_closed_time_changed"
READ_FAILED = "close_read_failed"
INVALID_TYPE = "close_read_invalid_type"
ALREADY = "already_written_off"
CLOCK_DISAGREE = "confirmation_failed:wall_monotonic_disagree"  # PR #342: no confirmation across a clock jump
HALT_REOPENED = "closed_flag_contradiction_after_writeoff"
HALT_CLOCK_ROLLBACK = "writeoff_close_first_seen_in_future"  # PR #342: clock rollback after a write-off
HALT_CLOSE_UNPARSEABLE = "writeoff_close_record_unparseable"  # PR #342: remaining legs cannot be resumed


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
    # PR #342 (Codex 6185166823): the monotonic stamp taken when the read finished (None: not measured)
    observed_mono: float | None = None


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


def predicate(obs: Observation, now: datetime) -> str | None:
    """None when the read qualifies (§A); else why not (a vector close-reason code)."""
    if obs.problem is not None:
        return READ_FAILED
    if not isinstance(obs.closed, bool) or not (obs.accepting_orders is None or isinstance(obs.accepting_orders, bool)):
        return INVALID_TYPE
    if obs.closed is not True:
        return "not_closed"
    if obs.accepting_orders is not False:
        return "accepting_orders_not_false"
    raw = obs.raw_closed_time
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return "closed_time_missing"
    if obs.closed_time is None:
        return "closed_time_invalid"
    if obs.closed_time > now:
        return "closed_time_in_future"
    return None


def qualifies(obs: Observation, now: datetime) -> bool:
    return predicate(obs, now) is None


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


def observation_from_row(market_id: str, row: Mapping[str, Any], now: datetime) -> Observation:
    raw = row.get("closedTime")
    return Observation(market=str(market_id), observed_at=now, closed=row.get("closed"),
                       accepting_orders=row.get("acceptingOrders"), closed_time=parse_end_date(raw),
                       raw_closed_time=raw)


def failed(market_id: str, now: datetime, problem: str) -> Observation:
    return Observation(str(market_id), now, problem=problem)


def gamma_read(getter: Callable[[str], Any], market_id: str, now: datetime, *,
               clock: Callable[[], datetime] | None = None,
               monotonic: Callable[[], float] | None = None) -> Observation:
    """Read one held market (see ``_gamma_read``). PR #342 review (Codex 6185166823 P1): with ``clock`` /
    ``monotonic`` the observation is stamped when the closed=true response completed (wall and monotonic), not at
    the tick start and not after the default view, so neither a slow read nor a slow default view can change the
    confirmation interval."""
    stamps: dict[str, Any] = {}

    def stamp() -> None:  # PR #342 (Codex 6185588835 P1): when the closed=true response completes, so a
        if clock is not None:  # slow default-view read can never pad the confirmation interval
            stamps["observed_at"] = clock()
        if monotonic is not None:
            stamps["observed_mono"] = monotonic()

    obs = _gamma_read(getter, market_id, now, stamp)
    if not stamps:
        stamp()  # the closed=true read itself failed: stamp the failure now
    return dataclasses.replace(obs, **stamps) if stamps else obs


def _gamma_read(getter: Callable[[str], Any], market_id: str, now: datetime,
                stamp: Callable[[], None] | None = None) -> Observation:
    """Read one held market: ``closed=true`` first (the only read that can show a close), then the default query
    (it can only show the market open). In neither list: unknown (a read failure). In both: unknown too (Gamma's
    views split for a few minutes around a close)."""
    rows: dict[bool, dict[str, Any]] = {}
    for closed in (True, False):
        try:
            data = getter(gamma_url(market_id, closed=closed))
            if closed and stamp is not None:
                stamp()
        except Exception as exc:  # noqa: BLE001 - any failed read is a read failure (never a halt by itself)
            return failed(market_id, now, f"gamma_read_failed:{type(exc).__name__}")
        row, problem = _match(data, market_id)
        if problem:
            return failed(market_id, now, problem)
        if row is not None:
            rows[closed] = row
    if not rows:
        return failed(market_id, now, "gamma_market_missing")
    if len(rows) == 2:
        return failed(market_id, now, "gamma_views_disagree")
    if False in rows and rows[False].get("closed") is True:
        return failed(market_id, now, "gamma_default_view_closed")  # a close is only seen through closed=true
    return observation_from_row(market_id, rows.get(True) or rows[False], now)


def second_source_confirms(source: str, raw: object, token_ids: Iterable[str] = ()) -> bool:
    """CLOB ``/markets/{condition_id}``: ``accepting_orders is False``; Data ``/v2/tokens``: every held token
    ``closed is True``. Anything else confirms nothing. Not wired: those endpoints are not allowlisted (KEY)."""
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

    def step(self, obs: Observation, now: datetime) -> tuple[Close | None, str]:
        """Feed one read of a market that is not written off; return (the Close it confirms, the close reason)."""
        pending = self.first.get(obs.market)
        reason = predicate(obs, now)
        if reason is not None:
            if pending is None:
                return None, reason
            del self.first[obs.market]  # rulings ① / N1: drop the pending read, no halt
            if reason in (READ_FAILED, INVALID_TYPE):
                return None, reason + "_pending_dropped"
            return None, "confirmation_failed:" + reason
        if pending is None:
            self.first[obs.market] = obs
            return None, AWAITING
        if pending.closed_time != obs.closed_time:
            del self.first[obs.market]  # this read does not open a new pending either (WO-14)
            return None, TIME_CHANGED
        wall = (obs.observed_at - pending.observed_at).total_seconds()
        if pending.observed_mono is not None or obs.observed_mono is not None:
            # PR #342 review (Codex 6185166823 P1): the interval is measured on the monotonic clock; a wall
            # clock that disagrees with it (a jump either way) confirms nothing (the pending read is dropped).
            if pending.observed_mono is None or obs.observed_mono is None:
                del self.first[obs.market]
                return None, CLOCK_DISAGREE
            mono = obs.observed_mono - pending.observed_mono
            if abs(wall - mono) > CLOCK_SKEW_MAX_S:
                del self.first[obs.market]
                return None, CLOCK_DISAGREE
            if mono < MARKET_CLOSE_RECHECK_S:
                return None, TOO_SOON
        if wall < MARKET_CLOSE_RECHECK_S:
            return None, TOO_SOON
        del self.first[obs.market]
        return Close(obs.market, obs.closed_time, pending.observed_at, obs.observed_at, BY_REREAD), CONFIRMED  # type: ignore[arg-type]

    def update(self, obs: Observation, now: datetime) -> Close | None:
        return self.step(obs, now)[0]

    def confirm_by_second_source(self, market: str, source: str, raw: object, now: datetime,
                                 token_ids: Iterable[str] = ()) -> Close | None:
        """A second source after a qualifying Gamma read confirms at once (never on its own). Not wired (KEY)."""
        first = self.first.get(str(market))
        if first is None or first.observed_at > now or not second_source_confirms(source, raw, token_ids):
            return None
        del self.first[str(market)]
        return Close(first.market, first.closed_time, first.observed_at, now, source)  # type: ignore[arg-type]

    def drop(self, market: str) -> None:
        self.first.pop(str(market), None)


def writeoff_fill_id(market: str, token_id: str) -> str:
    """The write-off key is the leg (Poly风控: unique key leg_key)."""
    digest = hashlib.sha256(f"{market}|{token_id}".encode("utf-8")).hexdigest()
    return WRITEOFF_ID_PREFIX + digest[:32]


def open_legs(rows: Iterable[dict[str, Any]]) -> dict[tuple[str, str], float]:
    legs: dict[tuple[str, str], float] = {}
    for row in rows:
        if not counts(row):
            continue
        qty = float(row.get("shares") or 0.0)
        key = leg_key(row.get("market"), row.get("token_id"))
        legs[key] = legs.get(key, 0.0) + (qty if row.get("side") == "buy" else -qty)
    return {key: shares for key, shares in legs.items() if abs(shares) > LEDGER_OPEN_EPS}


def held_markets(rows: Iterable[dict[str, Any]]) -> dict[str, list[str]]:
    """Open legs per market (counted rows, ledger order): {market: [token_id, ...]}."""
    out: dict[str, list[str]] = {}
    for market, token in sorted(open_legs(rows)):
        if market:
            out.setdefault(market, []).append(token)
    return out


def written_off_legs(rows: Iterable[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    """Open legs that carry a write-off row now: {(market, token): the write-off row}."""
    shares: dict[tuple[str, str], float] = {}
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        if not counts(row):
            continue
        key = leg_key(row.get("market"), row.get("token_id"))
        if is_writeoff(row):
            out[key] = row
            continue
        qty = float(row.get("shares") or 0.0)
        shares[key] = shares.get(key, 0.0) + (qty if row.get("side") == "buy" else -qty)
        if abs(shares[key]) <= LEDGER_OPEN_EPS:
            out.pop(key, None)
    return out


def reopen_contradiction(obs: Observation, first_closed_seen: datetime | None, now: datetime) -> str | None:
    """A written-off market read again: the halt reason, or None (ignored). See the module docstring.

    PR #342 review (Codex 6183403293, fail closed): a first closed read stamped after ``now`` is a clock rollback,
    a halt on every read; the reopen grace applies only for 0 <= elapsed < MARKET_CLOSE_REOPEN_GRACE_S."""
    elapsed = None if first_closed_seen is None else (now - first_closed_seen).total_seconds()
    if elapsed is not None and elapsed < 0:
        return HALT_CLOCK_ROLLBACK
    if obs.problem is not None:
        return None
    if obs.closed is False or obs.accepting_orders is True:
        if elapsed is not None and 0 <= elapsed < MARKET_CLOSE_REOPEN_GRACE_S:
            return None  # Gamma's by-id lag right after a close, not a contradiction
        return HALT_REOPENED
    return None


def already_reason(obs: Observation, closed_ts: object) -> str:
    if obs.problem is not None or not isinstance(obs.closed, bool) or not (
            obs.accepting_orders is None or isinstance(obs.accepting_orders, bool)):
        return ALREADY + "_read_failed"
    if obs.closed_time is not None and obs.closed_time != parse_end_date(closed_ts):
        return ALREADY + "_closed_time_changed"
    return ALREADY


RESUMED = "writeoff_resumed_remaining_legs"


def _close_from_row(market: str, row: dict[str, Any]) -> Close | None:
    """The confirmed close a leg's write-off row records (None when the row's stamps do not parse)."""
    stamps = [parse_end_date(row.get(name)) for name in ("closed_ts", "close_first_seen_ts", "close_confirmed_ts")]
    if any(stamp is None for stamp in stamps) or not isinstance(row.get("close_confirmed_by"), str):
        return None
    return Close(market, stamps[0], stamps[1], stamps[2], row["close_confirmed_by"])


@dataclass
class Step:
    """One tick's close detection (Poly风控 tick step 3)."""

    closes: list[Close] = field(default_factory=list)
    reasons: dict[str, str] = field(default_factory=dict)
    halts: list[str] = field(default_factory=list)  # "closed_flag_contradiction_after_writeoff:<market>"


def detect(tracker: CloseTracker, ledger_rows: list[dict[str, Any]], reads: Mapping[str, Observation],
           now: datetime) -> Step:
    """Feed this tick's reads of the held markets. Pure apart from the tracker's pending reads."""
    out = Step()
    held = held_markets(ledger_rows)
    written = written_off_legs(ledger_rows)
    for market in sorted(held):
        obs = reads.get(market)
        if obs is None:
            continue
        rows = [written[(market, token)] for token in held[market] if (market, token) in written]
        if rows:  # written off: no new write-off, read only to find a contradiction (N3)
            tracker.drop(market)
            first_seen = min((parse_end_date(row.get("close_first_seen_ts")) for row in rows
                              if parse_end_date(row.get("close_first_seen_ts")) is not None), default=None)
            halt = reopen_contradiction(obs, first_seen, now)
            if halt is not None:
                out.halts.append(f"{halt}:{market}")
            else:
                out.reasons[market] = already_reason(obs, rows[0].get("closed_ts"))
                # PR #342 review (Codex 6183035627): completion is per leg. A write-off interrupted between two
                # leg rows leaves an open leg of this confirmed-closed market without a row: write it off now,
                # from the close already confirmed (plan_writeoffs skips every leg that has its row).
                if any((market, token) not in written for token in held[market]):
                    resumed = _close_from_row(market, rows[0])
                    if resumed is None:  # never a silent skip: the open leg would keep no write-off forever
                        out.halts.append(f"{HALT_CLOSE_UNPARSEABLE}:{market}")
                    else:
                        out.closes.append(resumed)
                        out.reasons[market] = RESUMED
            continue
        close, reason = tracker.step(obs, now)
        out.reasons[market] = reason
        if close is not None:
            out.closes.append(close)
    for market in list(tracker.first):
        if market not in held:
            tracker.drop(market)  # settled or flat: nothing left to confirm
    return out


@dataclass
class WriteoffPlan:
    rows: list[dict[str, Any]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def plan_writeoffs(ledger_rows: list[dict[str, Any]], ledger_ids: list[str], closes: Iterable[Close],
                   now: str, snapshot: Mapping[str, Any] | None = None) -> WriteoffPlan:
    """The write-off rows to book now for the confirmed closes (none for a leg already written off).

    ``snapshot`` is the start-of-day snapshot of ``now``'s UTC day (src/daily_marks), None when there is none yet.
    """
    result = WriteoffPlan()
    day = daily_loss(ledger_rows, now[:10], ledger_ids)
    if day.problems:
        result.problems = [f"writeoff_ledger_invalid:{problem}" for problem in day.problems]
        return result
    pos = leg_marks.positions(ledger_rows, now[:10], ledger_ids)
    problems = pos.problems + leg_marks.snapshot_problems(pos, snapshot)
    if problems:
        result.problems = [f"writeoff_marks_invalid:{problem}" for problem in problems]
        return result
    already = written_off_legs(ledger_rows)
    held = held_markets(ledger_rows)
    for close in closes:
        for token in held.get(close.market, []):
            key = leg_key(close.market, token)
            if key in already:
                continue  # written off once: never again (restart-safe)
            leg = day.legs.get(key)
            amount = None if leg is None else writeoff_amount(leg)
            if leg is None or amount is None:
                continue
            uplift = leg_marks.writeoff_sod_uplift(pos, snapshot, key)
            result.rows.append({
                FILL_ID_FIELD: writeoff_fill_id(close.market, token),
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
                "writeoff_usd": amount + uplift,
                "writeoff_cost_usd": amount,
                "writeoff_sod_uplift_usd": uplift,
                **({} if snapshot is None else {"writeoff_sod_late": snapshot.get("late") is True}),
                # #197 option A (家伟 2026-10-14): the snapshot's lateness; a clock anomaly makes it late and the
                # write-off is still booked at the late reference. Recomputed by leg_marks.writeoff_uplift_problems.
                "closed_ts": close.closed_ts,
                "close_first_seen_ts": close.first_seen.isoformat(),
                "close_confirmed_ts": close.confirmed_at.isoformat(),
                "close_confirmed_by": close.confirmed_by,
                "note": "Paper write-off of a closed market's open leg at its full cost, start-of-day shares at "
                        "max(cost, start-of-day mark) (#197, KEY-approved; RiskGate correction 6109537257). "
                        "No exchange action; the leg holds its limits until it settles.",
            })
    return result


def bus_events(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """The write-off bus events for rows (at least once; consumers dedupe by ``key``, the leg's fill_id)."""
    return [{"key": row.get(FILL_ID_FIELD), "leg_key": f"{row.get('market')}|{row.get('token_id')}",
             "market": row.get("market"), "token_id": row.get("token_id"), "writeoff_usd": row.get("writeoff_usd"),
             "closed_ts": row.get("closed_ts"), "ts": row.get("ts")} for row in rows]
