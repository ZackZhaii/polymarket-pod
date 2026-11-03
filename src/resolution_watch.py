"""Issue #206 (Task CO): collect settlement snapshots for the markets the paper ledger holds, from Gamma.

The M2 runner never wrote data/resolutions.jsonl (only the standalone ``resolutions`` CLI did), so
``paper_ledger.apply_settlements`` had nothing to settle from. Each runner tick now reads, for every market with
an open leg only, Gamma ``/markets/keyset?id=<id>&closed=true`` and ``/markets/keyset?id=<id>`` (#353; the default lists open
markets only; both are the allowlisted Gamma ``/markets/keyset`` path, no new host). A closed market's row goes through the
existing classifier (``resolutions.snapshot``: closedTime + RESOLUTION_HOLD_HOURS, UMA status, unique winner,
token mapping, the history's first-write / one-upgrade rule) and new rows are appended to the history.

Collection failure (Poly风控's ruling on #197, issuecomment-6106669720 ②): an exception or HTTP error, a response
that is not a list, the market in neither list (an empty ``[]``), the market twice, or ``closed`` /
``acceptingOrders`` that is not a JSON boolean. Then nothing is written and the caller books no settlement this
tick: no halt; the legs keep their #124 (worst-case) accounting and the next tick retries. Repeated failures are
left to the existing heartbeat / staleness rules (no new threshold).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlencode

from src import podlib, resolutions


@dataclass
class Collected:
    written: list[dict[str, Any]] = field(default_factory=list)  # rows appended to the history now
    closed_markets: list[str] = field(default_factory=list)
    skip_reasons: dict[str, int] = field(default_factory=dict)    # classifier reasons (hold_pending, ...)
    failures: list[str] = field(default_factory=list)              # non-empty: nothing was written

    @property
    def ok(self) -> bool:
        return not self.failures


def gamma_url(market_id: str, *, closed: bool) -> str:
    query = {"id": market_id, "closed": "true"} if closed else {"id": market_id}
    return podlib.GAMMA_PREFIX + "/keyset?" + urlencode(query)


def valid_gamma_id(market_id: object) -> bool:
    """Issue #353: a Gamma market id is a non-empty string of ASCII digits (checked before any request)."""
    text = str(market_id).strip() if market_id is not None else ""
    return bool(text) and text.isascii() and text.isdigit()


def keyset_rows(data: object, market_id: str) -> list[dict[str, Any]] | None:
    """Issue #353: the rows of a single-id /markets/keyset answer, or None for a bad response.

    The answer must be a dict whose ``markets`` is a list of dicts that all carry this id; a ``next_cursor``
    key (the id filter may have been ignored) or any row of another id is a bad response. ``$schema`` and other
    top-level keys are ignored.
    """
    if not isinstance(data, dict) or "next_cursor" in data:
        return None
    markets = data.get("markets")
    if not isinstance(markets, list):
        return None
    if not all(isinstance(row, dict) and str(row.get("id")) == str(market_id) for row in markets):
        return None
    return list(markets)


def read_market(getter: Callable[[str], Any], market_id: str) -> tuple[dict[str, Any] | None, str | None]:
    """One held market's Gamma row (closed view first), or (None, failure)."""
    if not valid_gamma_id(market_id):
        return None, "gamma_bad_id"  # Issue #353: no request for an empty / non-numeric id
    found: list[dict[str, Any]] = []
    for closed in (True, False):
        try:
            data = getter(gamma_url(market_id, closed=closed))
        except Exception as exc:  # noqa: BLE001 - any failed read is a collection failure (no halt)
            return None, f"gamma_read_failed:{type(exc).__name__}"
        rows = keyset_rows(data, market_id)
        if rows is None:
            return None, "gamma_bad_response"
        if len(rows) > 1:
            return None, "gamma_duplicate_market"
        found.extend(rows)
    if not found:
        return None, "gamma_market_missing"
    row = found[0]
    for name in ("closed", "acceptingOrders"):
        if not isinstance(row.get(name), bool):
            return None, f"gamma_non_bool_{name}"
    if len(found) == 2 and found[1].get("closed") is not row.get("closed"):
        return None, "gamma_views_disagree"
    return row, None


def collect(getter: Callable[[str], Any], market_ids: Iterable[str], now: datetime, snapshot_ts: str,
            path: Path) -> Collected:
    """Read every held market; append the new confirmed / closed snapshots. Any failure: write nothing."""
    out = Collected()
    raw: list[dict[str, Any]] = []
    for market_id in sorted({str(m) for m in market_ids if str(m)}):
        row, failure = read_market(getter, market_id)
        if failure is not None:
            out.failures.append(f"{market_id}:{failure}")
            continue
        if row is not None and row.get("closed") is True:
            out.closed_markets.append(market_id)
            raw.append(row)
    if out.failures or not raw:
        return out
    try:
        history = resolutions.read_history(path)
    except (resolutions.HistoryError, OSError) as exc:
        out.failures.append(f"resolutions_history_unreadable:{type(exc).__name__}")
        return out
    result = resolutions.snapshot(raw, now, snapshot_ts, history=history)
    out.skip_reasons = dict(result["first_reason"])
    for row in result["rows"]:
        try:
            resolutions.persist_resolutions(path, [row])
        except OSError as exc:  # a failed append: the next tick re-reads the history and retries the rest
            out.failures.append(f"resolutions_write_failed:{type(exc).__name__}")
            return out
        out.written.append(row)
    return out
