"""Task AG (M1): read-only recorder for Polymarket's PUBLIC CLOB WebSocket market channel.

Read-only market data. No authentication, no keys, no user channel, no order endpoints and no
CLOB SDK. It writes local JSONL book rows in the same shape as ``src/book.py`` so book_staleness,
paper_cycle and Sentinel's StaleWatch read them unchanged.

Sources (checked 2026-10-09):
- https://docs.polymarket.com/api-reference/wss/market (AsyncAPI): server
  ``wss://ws-subscriptions-clob.polymarket.com/ws/market``; subscription frame
  ``{"assets_ids": [...], "type": "market", "initial_dump": true}``; update frame
  ``{"operation": "subscribe"|"unsubscribe", "assets_ids": [...]}``; events ``book``
  (asset_id, market, bids/asks of {price, size} strings, timestamp in Unix ms as a string, hash of
  the book content), ``price_change`` (market, timestamp, price_changes[] of asset_id, price, size
  = new aggregate size with "0" removing the level, side BUY/SELL, hash of the ORDER that caused the
  change, optional best_bid/best_ask), ``last_trade_price`` (asset_id, market, price, size, side,
  fee_rate_bps, timestamp, transaction_hash), ``tick_size_change`` (asset_id, market,
  old_tick_size, new_tick_size, timestamp); ``best_bid_ask``, ``new_market`` and
  ``market_resolved`` only with custom_feature_enabled, which this recorder does not request.
- https://docs.polymarket.com/market-data/websocket/overview : application heartbeat, send the
  text frame ``PING`` every 10 s; the server answers ``PONG``.
- https://github.com/Polymarket/agent-skills/blob/main/websocket.md : channel table (market =
  no auth, user = auth; this module refuses any other URL), ``size "0"`` removes a level.

What the protocol gives for integrity, and what this recorder does with it:
- There is no sequence number, so a dropped frame cannot be seen directly. ``book.hash`` hashes
  the book, but its algorithm is not officially published; ``price_change[].hash`` is an order hash,
  not a book hash. Task AW: the algorithm Poly研究 found in older clients / Nautilus (set ``hash`` to
  "", serialise market, asset_id, timestamp, hash, bids, asks, min_order_size, tick_size, neg_risk,
  last_trade_price in that order as compact JSON, SHA-1 hex) is computed for every snapshot and the
  result recorded (row ``book_hash_check``: match / mismatch / incomplete / absent / uncomputable,
  and counts ``hash_check:*``). It is NOT enforced (BOOK_HASH_ENFORCED = False, Poly指挥 ruling): it
  could not be validated on WS snapshots, because the M1 recording keeps only the top of book and
  the hash, not the levels it was computed over. With BOOK_HASH_ENFORCED = True anything but
  ``match`` resyncs the asset (``book_hash_mismatch``, a gap resync).
- Gaps and inconsistencies are therefore detected from what the stream does carry, and each one
  puts the asset into resync: an exchange timestamp older than the last applied one
  (``timestamp_regression``); a delta that removes a level the local book does not have
  (``unknown_level_removed``); a crossed or locked book after a snapshot or delta
  (``crossed_book``); the ``best_bid``/``best_ask`` a price_change reports disagreeing with the
  local book (``top_of_book_mismatch``, checked when the local side is non-empty); a ``market``
  that differs from the snapshot's (``market_mismatch``); a tick_size_change whose old tick is
  not the tick we hold (``tick_size_mismatch``); Task AW: any valid tick_size_change too
  (``tick_size_change``: the book is re-snapshotted before a price on the new tick is trusted; not a
  gap, so it is not counted toward the gap-resync escalation); an event type this recorder does not
  handle that names a subscribed asset or its market, or names nothing (``unknown_event``); a non-finite or out-of-range price, size, tick
  or timestamp, a bad side, a duplicate level or a malformed message (``invalid_*``,
  ``malformed_message``). A frame that is not JSON resyncs every asset (``undecodable_message``).
- Resync = if the asset was live, write one price-less invalidation row at once (see Rows below),
  then discard its local book and wait for a fresh ``book`` snapshot; deltas for it are ignored and
  no further row is written for it until then (the invalidation row is the only row a resyncing
  asset ever gets, and it can only make the book look stale, never fresh). The recorder asks for one (unsubscribe + subscribe for that asset); if
  none arrives within WS_RESYNC_TIMEOUT_S it reconnects. Any disconnect resyncs every asset.
  Reconnects back off exponentially: WS_BACKOFF_BASE_S * 2**attempt, capped at WS_BACKOFF_CAP_S,
  reset after a connection on which every asset was live.
- Task AW: a PING still unanswered by a PONG after WS_PONG_TIMEOUT_S means the connection is dead:
  the recorder disconnects (``pong_timeout``, every asset resyncs) and reconnects; it counts toward
  the Task AP reconnect escalation like any other disconnect.

Safety rails (Poly指挥 ruling: websockets is not a trading dependency if all hold):
- check_market_url: an allowlist enforced before connecting (Recorder and the adapter): scheme
  ``wss``, host ``ws-subscriptions-clob.polymarket.com`` and path ``/ws/market`` exactly.
- check_outgoing_frame: every frame sent is PING or the public market subscription / update;
  no auth, API key, signature or passphrase field and no user-channel subscription. The adapter
  passes no headers.
- No order-capable package is imported or required (tests scan requirements*.txt and src).

Rows (``data/books.jsonl`` by default) carry every key ``book.snapshot`` writes, plus
``ws_event``, ``ws_state`` and ``book_hash``. ``book_ts`` is the exchange ``timestamp`` of the
latest message applied to that book, converted by ``book.book_timestamp`` (ISO 8601 UTC, ms, Z);
the local receive time is only ever ``ts`` (the recorder time, as in book.py). A row is written
on every snapshot, whenever the top of book or the tick changes, and when the exchange time of the
latest applied event is WS_ROW_REFRESH_S past the last written row (so a live book with a quiet
top keeps a current book_ts; rows can lag the stream by that much, never lead it). When a live asset goes into
resync (gap, invalid value, disconnect), one invalidation row is written at once: no prices,
``book_ts`` null and ``book_ts_problem`` = ``invalid_book_ts``, so paper_cycle, RiskGate and
StaleWatch treat the leg as stale immediately instead of trusting the last row until it ages out.
``market_id``, ``question``, ``outcome`` and ``liquidity_usd`` come from the local
``data/markets.jsonl`` (scanner output); an asset not found there gets ``market_id`` "" and
paper_cycle ignores its rows. Each row is one complete line written by a single append to a file
opened in append mode; a torn last line left by a crash is sealed with a newline first.

Transport is injectable. The real adapter imports the optional ``websockets`` package lazily,
inside ``WebsocketsTransport.connect`` only (``pip install -r requirements-ws.txt``); the core and
the tests never need it.

CLI: ``python -m src.ws_recorder --assets TOKEN [TOKEN ...] --max-runtime SECONDS
[--out data/books.jsonl] [--markets data/markets.jsonl]``. Exit codes: 0 ran until
--max-runtime and wrote at least one book row; 1 wrote no book row (no usable snapshot);
2 usage error; 3 the optional ``websockets`` package is not installed; 4 writing the output file
failed (the recorder stops instead of reconnecting); 6 config/risk.toml off its pins at start or
while running (Task AS, src/pins.py; a global halt is recorded). Registry: docs/EXIT_CODES.md.

Task AP (Sentinel design gaps 2 and 13): the CLI writes logs/heartbeat/ws_recorder.json every
HEARTBEAT_RECORDER_EVERY_S (10 s, also while backing off) and a last beat with the exit code; the
heartbeat progress carries the sliding-window reconnect and gap-resync counts (class Escalation)
that src/sentinel_tick.py escalates on. Nothing here changes what a row says or when a book is fresh.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.book import book_timestamp  # noqa: E402
from src.book_staleness import INVALID_BOOK_TS, latest_by_time  # noqa: E402
from src.podlib import BOOKS_PATH, MARKETS_PATH, read_jsonl, utc_now  # noqa: E402
from src.thresholds import (  # noqa: E402
    HEARTBEAT_RECORDER_EVERY_S,
    RECORDER_ESCALATION_WINDOW_S,
    RECORDER_GAP_RESYNC_HALT_COUNT,
    RECORDER_RECONNECT_ALERT_COUNT,
    WS_BACKOFF_BASE_S,
    WS_BACKOFF_CAP_S,
    WS_PING_INTERVAL_S,
    WS_PONG_TIMEOUT_S,
    WS_RESYNC_TIMEOUT_S,
    WS_ROW_REFRESH_S,
    WS_STOP_POLL_S,
)

MARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
ALLOWED_SCHEME = "wss"
ALLOWED_HOST = "ws-subscriptions-clob.polymarket.com"
ALLOWED_PATH = "/ws/market"
SOURCE = "clob_ws_market"
PING = "PING"
PONG = "PONG"
AWAITING = "awaiting_snapshot"
LIVE = "live"
SIDES = ("BUY", "SELL")
# Task AW: the snapshot hash is recorded, not enforced, until it is validated on real WS snapshots.
BOOK_HASH_ENFORCED = False
BOOK_HASH_FIELDS = ("market", "asset_id", "timestamp", "hash", "bids", "asks", "min_order_size", "tick_size",
                    "neg_risk", "last_trade_price")
# Resync reasons that are not gaps in the stream (not counted toward the gap-resync escalation).
NOT_A_GAP = frozenset({"tick_size_change"})

EXIT_OK = 0
EXIT_NO_ROWS = 1
EXIT_USAGE = 2
EXIT_NO_WEBSOCKETS = 3
EXIT_WRITE_FAILED = 4
EXIT_PIN_DRIFT = 6  # Task AS: config/risk.toml off its pins at start or while running (global halt recorded)


# --- values -----------------------------------------------------------------------------------


def _number(raw: object) -> float | None:
    """A finite number from a decimal string or an int/float. Bools, NaN, inf and junk are None."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
    elif isinstance(raw, str) and raw.strip():
        try:
            value = float(raw)
        except ValueError:
            return None
    else:
        return None
    return value if math.isfinite(value) else None


def _price(raw: object) -> float | None:
    """A CLOB price: finite and strictly inside (0, 1)."""
    value = _number(raw)
    return value if value is not None and 0.0 < value < 1.0 else None


def _size(raw: object, *, allow_zero: bool) -> float | None:
    value = _number(raw)
    if value is None or value < 0.0 or (value == 0.0 and not allow_zero):
        return None
    return value


def _top_value(raw: object) -> tuple[bool, float | None]:
    """(ok, value) for an optional best_bid/best_ask: absent or blank is (True, None)."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return True, None
    value = _number(raw)
    if value is None or not 0.0 <= value <= 1.0:
        return False, None
    return True, value


def _exchange_time(raw: object) -> tuple[int, str] | None:
    """(ms, ISO) for the exchange's Unix-ms timestamp, converted exactly like book.book_timestamp."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        millis = raw
    elif isinstance(raw, str) and raw.strip().isascii() and raw.strip().isdigit():
        millis = int(raw.strip())
    else:
        return None
    iso, problem = book_timestamp(raw)
    if problem is not None or iso is None or millis < 0:
        return None
    return millis, iso


def book_hash_check(message: dict[str, Any]) -> str:
    """Task AW: the snapshot hash by the unofficial algorithm (see the module docstring).

    "match" / "mismatch", "absent" (no hash string), "incomplete" (a hashed field is missing from the
    frame, so the server's input cannot be rebuilt) or "uncomputable" (not serialisable).
    """
    claimed = message.get("hash")
    if not isinstance(claimed, str) or not claimed:
        return "absent"
    if any(name not in message for name in BOOK_HASH_FIELDS):
        return "incomplete"
    body = {name: "" if name == "hash" else message[name] for name in BOOK_HASH_FIELDS}
    try:
        text = json.dumps(body, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        return "uncomputable"
    return "match" if hashlib.sha1(text.encode("utf-8")).hexdigest() == claimed else "mismatch"


# --- core -------------------------------------------------------------------------------------


@dataclass
class AssetBook:
    asset_id: str
    state: str = AWAITING
    market: str | None = None
    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)
    last_ms: int | None = None
    last_iso: str | None = None
    book_hash: str | None = None
    hash_check: str | None = None
    tick_size: float | None = None
    min_order_size: float | None = None
    last_top: tuple | None = None
    last_row_ms: int | None = None
    awaiting_since: float | None = None

    def discard(self, since: float | None) -> None:
        """Resync: drop every piece of local book state and wait for a fresh snapshot.

        The caller (BookCore._resync) has already written the invalidation row if the asset was live.
        """
        self.state = AWAITING
        self.market = None
        self.bids = {}
        self.asks = {}
        self.last_ms = None
        self.last_iso = None
        self.book_hash = None
        self.hash_check = None
        self.tick_size = None
        self.min_order_size = None
        self.last_top = None
        self.last_row_ms = None
        self.awaiting_since = since


@dataclass
class Outcome:
    rows: list[dict[str, Any]] = field(default_factory=list)
    resyncs: list[tuple[str, str]] = field(default_factory=list)
    counts: Counter = field(default_factory=Counter)


class _Reject(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _crossed(bids: dict[float, float], asks: dict[float, float]) -> bool:
    return bool(bids) and bool(asks) and max(bids) >= min(asks)


def _levels(raw: object) -> dict[float, float]:
    """Snapshot levels; any bad, zero-size or duplicate level rejects the whole snapshot."""
    if not isinstance(raw, list):
        raise _Reject("malformed_message")
    levels: dict[float, float] = {}
    for item in raw:
        if not isinstance(item, dict):
            raise _Reject("malformed_message")
        price = _price(item.get("price"))
        if price is None:
            raise _Reject("invalid_price")
        size = _size(item.get("size"), allow_zero=False)
        if size is None:
            raise _Reject("invalid_size")
        if price in levels:
            raise _Reject("duplicate_level")
        levels[price] = size
    return levels


class BookCore:
    """Pure per-asset book state machine. No I/O, no clock: callers pass ``ts`` and ``mono``."""

    def __init__(self, assets: Iterable[str], meta: dict[str, dict[str, Any]] | None = None) -> None:
        self.books = {asset: AssetBook(asset) for asset in assets}
        self.meta = meta or {}

    # -- state ---------------------------------------------------------------------------------

    def awaiting(self) -> list[str]:
        return [asset for asset, book in self.books.items() if book.state != LIVE]

    def all_live(self) -> bool:
        return all(book.state == LIVE for book in self.books.values())

    def oldest_awaiting_since(self) -> float | None:
        times = [book.awaiting_since for book in self.books.values()
                 if book.state != LIVE and book.awaiting_since is not None]
        return min(times) if times else None

    def start_waiting(self, mono: float) -> None:
        """A (re)subscription went out: every asset not yet live waits from now."""
        for book in self.books.values():
            if book.state != LIVE:
                book.awaiting_since = mono

    def invalidate_all(self, reason: str, ts: str, mono: float | None = None) -> Outcome:
        out = Outcome()
        for asset in self.books:
            self._resync(asset, reason, ts, mono, out)
        return out

    # -- input ---------------------------------------------------------------------------------

    def handle_text(self, text: str, ts: str, mono: float | None = None) -> Outcome:
        out = Outcome()
        if text.strip() == PONG:
            out.counts["pong"] += 1
            return out
        try:
            message = json.loads(text)
        except ValueError:
            for asset in self.books:
                self._resync(asset, "undecodable_message", ts, mono, out)
            return out
        self.handle(message, ts, mono, out)
        return out

    def handle(self, message: Any, ts: str, mono: float | None = None, out: Outcome | None = None) -> Outcome:
        out = out if out is not None else Outcome()
        if isinstance(message, list):  # the initial dump arrives as a list of book events
            for item in message:
                self.handle(item, ts, mono, out)
            return out
        if not isinstance(message, dict):
            for asset in self.books:
                self._resync(asset, "malformed_message", ts, mono, out)
            return out
        kind = message.get("event_type")
        if kind == "book":
            self._on_book(message, ts, mono, out)
        elif kind == "price_change":
            self._on_price_change(message, ts, mono, out)
        elif kind == "tick_size_change":
            self._on_tick_size_change(message, ts, mono, out)
        elif kind == "last_trade_price":
            self._on_last_trade(message, ts, mono, out)
        elif isinstance(kind, str) and kind:
            self._on_unknown(message, kind, ts, mono, out)
        else:
            for asset in self._attributable(message):
                self._resync(asset, "malformed_message", ts, mono, out)
        return out

    def _attributable(self, message: dict[str, Any]) -> list[str]:
        """Assets a malformed message could belong to: its asset, else its market's, else all."""
        asset = message.get("asset_id")
        if isinstance(asset, str) and asset in self.books:
            return [asset]
        market = message.get("market")
        if isinstance(market, str) and market:
            matched = [name for name, book in self.books.items() if book.market == market]
            if matched:
                return matched
        return list(self.books)

    def _on_unknown(self, message: dict[str, Any], kind: str, ts: str, mono: float | None, out: Outcome) -> None:
        """Task AW: an event type this recorder does not handle. Fail closed: it resyncs every subscribed
        asset it names (asset_id, assets_ids, price_changes[].asset_id) or whose market it names; one that
        names nothing resyncs every asset; one that names only other assets/markets is ignored."""
        out.counts[f"unknown_event:{kind}"] += 1
        named: set[str] = set()
        identified = False
        asset = message.get("asset_id")
        if isinstance(asset, str) and asset:
            identified = True
            named.add(asset)
        for key in ("assets_ids", "price_changes"):
            items = message.get(key)
            if isinstance(items, list):
                for item in items:
                    value = item.get("asset_id") if isinstance(item, dict) else item
                    if isinstance(value, str) and value:
                        identified = True
                        named.add(value)
        market = message.get("market")
        if isinstance(market, str) and market:
            identified = True
        targets = [name for name, book in self.books.items()
                   if name in named or (isinstance(market, str) and market and book.market == market)]
        if not identified:
            targets = list(self.books)
        if not targets:
            out.counts[f"ignored_event:{kind}"] += 1
            return
        for name in targets:
            self._resync(name, "unknown_event", ts, mono, out)

    def _subscribed(self, message: dict[str, Any], out: Outcome) -> AssetBook | None:
        asset = message.get("asset_id")
        if not isinstance(asset, str) or not asset:
            return None
        book = self.books.get(asset)
        if book is None:
            out.counts["unsubscribed_asset"] += 1
        return book

    def _on_book(self, message: dict[str, Any], ts: str, mono: float | None, out: Outcome) -> None:
        out.counts["book"] += 1
        asset = message.get("asset_id")
        if not isinstance(asset, str) or not asset:
            for name in self._attributable(message):
                self._resync(name, "malformed_message", ts, mono, out)
            return
        book = self._subscribed(message, out)
        if book is None:
            return
        try:
            stamp = _exchange_time(message.get("timestamp"))
            if stamp is None:
                raise _Reject("invalid_timestamp")
            market = message.get("market")
            if not isinstance(market, str) or not market:
                raise _Reject("malformed_message")
            if book.state == LIVE and book.market != market:
                raise _Reject("market_mismatch")
            if book.state == LIVE and book.last_ms is not None and stamp[0] < book.last_ms:
                raise _Reject("timestamp_regression")
            bids = _levels(message.get("bids"))
            asks = _levels(message.get("asks"))
            if _crossed(bids, asks):
                raise _Reject("crossed_book")
            book_hash = message.get("hash")
            if book_hash is not None and not isinstance(book_hash, str):
                raise _Reject("malformed_message")
            tick = book.tick_size if book.state == LIVE else None
            if "tick_size" in message:
                tick = _price(message.get("tick_size"))
                if tick is None:
                    raise _Reject("invalid_tick_size")
            min_size = book.min_order_size if book.state == LIVE else None
            if "min_order_size" in message:
                min_size = _size(message.get("min_order_size"), allow_zero=True)
                if min_size is None:
                    raise _Reject("invalid_size")
            hash_check = book_hash_check(message)
            out.counts[f"hash_check:{hash_check}"] += 1
            if BOOK_HASH_ENFORCED and hash_check != "match":
                raise _Reject("book_hash_mismatch")
        except _Reject as reject:
            self._resync(asset, reject.reason, ts, mono, out)
            return
        book.state = LIVE
        book.market = market
        book.bids, book.asks = bids, asks
        book.last_ms, book.last_iso = stamp
        book.book_hash = book_hash
        book.hash_check = hash_check
        book.tick_size = tick
        book.min_order_size = min_size
        book.awaiting_since = None
        out.counts["snapshot_applied"] += 1
        self._emit(book, ts, "book", out, force=True)

    def _on_price_change(self, message: dict[str, Any], ts: str, mono: float | None, out: Outcome) -> None:
        out.counts["price_change"] += 1
        changes = message.get("price_changes")
        stamp = _exchange_time(message.get("timestamp"))
        market = message.get("market")
        if not isinstance(changes, list) or not changes or any(
                not isinstance(item, dict) or not isinstance(item.get("asset_id"), str) for item in changes):
            for asset in self._attributable(message):
                self._resync(asset, "malformed_message", ts, mono, out)
            return
        grouped: dict[str, list[dict[str, Any]]] = {}
        for item in changes:
            grouped.setdefault(item["asset_id"], []).append(item)
        for asset, items in grouped.items():
            book = self.books.get(asset)
            if book is None:
                out.counts["unsubscribed_asset"] += 1
                continue
            if book.state != LIVE:
                out.counts["ignored_awaiting_snapshot"] += 1
                continue
            try:
                if stamp is None:
                    raise _Reject("invalid_timestamp")
                if market != book.market:
                    raise _Reject("market_mismatch")
                if book.last_ms is not None and stamp[0] < book.last_ms:
                    raise _Reject("timestamp_regression")
                bids, asks = dict(book.bids), dict(book.asks)
                reported: tuple[float | None, float | None] | None = None
                for item in items:
                    side = item.get("side")
                    if side not in SIDES:
                        raise _Reject("invalid_side")
                    price = _price(item.get("price"))
                    if price is None:
                        raise _Reject("invalid_price")
                    size = _size(item.get("size"), allow_zero=True)
                    if size is None:
                        raise _Reject("invalid_size")
                    ok_bid, best_bid = _top_value(item.get("best_bid"))
                    ok_ask, best_ask = _top_value(item.get("best_ask"))
                    if not (ok_bid and ok_ask):
                        raise _Reject("invalid_top_of_book")
                    if "best_bid" in item or "best_ask" in item:
                        reported = (best_bid, best_ask)
                    levels = bids if side == "BUY" else asks
                    if size == 0.0:
                        if price not in levels:
                            raise _Reject("unknown_level_removed")
                        del levels[price]
                    else:
                        levels[price] = size
                if _crossed(bids, asks):
                    raise _Reject("crossed_book")
                if reported is not None:
                    if bids and reported[0] is not None and reported[0] != max(bids):
                        raise _Reject("top_of_book_mismatch")
                    if asks and reported[1] is not None and reported[1] != min(asks):
                        raise _Reject("top_of_book_mismatch")
            except _Reject as reject:
                self._resync(asset, reject.reason, ts, mono, out)
                continue
            book.bids, book.asks = bids, asks
            book.last_ms, book.last_iso = stamp
            out.counts["price_change_applied"] += 1
            self._emit(book, ts, "price_change", out)

    def _on_tick_size_change(self, message: dict[str, Any], ts: str, mono: float | None, out: Outcome) -> None:
        out.counts["tick_size_change"] += 1
        if not isinstance(message.get("asset_id"), str):
            for asset in self._attributable(message):
                self._resync(asset, "malformed_message", ts, mono, out)
            return
        book = self._subscribed(message, out)
        if book is None:
            return
        if book.state != LIVE:
            out.counts["ignored_awaiting_snapshot"] += 1
            return
        try:
            stamp = _exchange_time(message.get("timestamp"))
            if stamp is None:
                raise _Reject("invalid_timestamp")
            if message.get("market") != book.market:
                raise _Reject("market_mismatch")
            if book.last_ms is not None and stamp[0] < book.last_ms:
                raise _Reject("timestamp_regression")
            old_tick = _price(message.get("old_tick_size"))
            new_tick = _price(message.get("new_tick_size"))
            if old_tick is None or new_tick is None:
                raise _Reject("invalid_tick_size")
            if book.tick_size is not None and old_tick != book.tick_size:
                raise _Reject("tick_size_mismatch")
        except _Reject as reject:
            self._resync(book.asset_id, reject.reason, ts, mono, out)
            return
        # Task AW: a valid tick change still re-snapshots the asset (Nautilus resync rule): prices on the
        # new tick are only trusted from a fresh book snapshot, so the asset invalidates and resubscribes.
        out.counts["tick_size_resnapshot"] += 1
        self._resync(book.asset_id, "tick_size_change", ts, mono, out)

    def _on_last_trade(self, message: dict[str, Any], ts: str, mono: float | None, out: Outcome) -> None:
        """A trade does not change the aggregated book; it is only validated (bad values resync)."""
        out.counts["last_trade_price"] += 1
        if not isinstance(message.get("asset_id"), str):
            for asset in self._attributable(message):
                self._resync(asset, "malformed_message", ts, mono, out)
            return
        book = self._subscribed(message, out)
        if book is None:
            return
        reason = None
        if _price(message.get("price")) is None:
            reason = "invalid_price"
        elif _size(message.get("size"), allow_zero=False) is None:
            reason = "invalid_size"
        elif message.get("side") not in SIDES:
            reason = "invalid_side"
        elif _exchange_time(message.get("timestamp")) is None:
            reason = "invalid_timestamp"
        if reason is not None:
            self._resync(book.asset_id, reason, ts, mono, out)

    # -- output --------------------------------------------------------------------------------

    def _base_row(self, book: AssetBook, ts: str) -> dict[str, Any]:
        meta = self.meta.get(book.asset_id, {})
        return {
            "ts": ts,
            "book_ts": None,
            "book_ts_problem": None,
            "market_id": str(meta.get("market_id") or ""),
            "question": meta.get("question") or "",
            "condition_id": book.market or meta.get("condition_id") or "",
            "token_id": book.asset_id,
            "outcome": meta.get("outcome") or "",
            "best_bid": None,
            "best_bid_size": None,
            "best_ask": None,
            "best_ask_size": None,
            "spread": None,
            "tick_size": None,
            "min_order_size": None,
            "liquidity_usd": meta.get("liquidity_usd"),
            "source": SOURCE,
        }

    def _emit(self, book: AssetBook, ts: str, event: str, out: Outcome, *, force: bool = False) -> None:
        best_bid = max(book.bids) if book.bids else None
        best_ask = min(book.asks) if book.asks else None
        top = (best_bid, None if best_bid is None else book.bids[best_bid],
               best_ask, None if best_ask is None else book.asks[best_ask], book.tick_size)
        refresh_due = (book.last_ms is not None and book.last_row_ms is not None
                       and book.last_ms - book.last_row_ms >= WS_ROW_REFRESH_S * 1000)
        if not force and top == book.last_top and not refresh_due:
            return
        book.last_top = top
        book.last_row_ms = book.last_ms
        row = self._base_row(book, ts)
        row.update({
            "book_ts": book.last_iso,
            "best_bid": None if best_bid is None else round(best_bid, 6),
            "best_bid_size": top[1],
            "best_ask": None if best_ask is None else round(best_ask, 6),
            "best_ask_size": top[3],
            "spread": None if best_bid is None or best_ask is None else round(best_ask - best_bid, 6),
            "tick_size": book.tick_size,
            "min_order_size": book.min_order_size,
            "ws_event": event,
            "ws_state": LIVE,
            "book_hash": book.book_hash,
            "book_hash_check": book.hash_check,
        })
        out.rows.append(row)

    def _resync(self, asset: str, reason: str, ts: str, mono: float | None, out: Outcome) -> None:
        book = self.books[asset]
        was_live = book.state == LIVE
        # An asset already waiting keeps its original wait start, so repeated bad frames
        # cannot postpone the resync timeout forever.
        book.discard(mono if was_live or book.awaiting_since is None else book.awaiting_since)
        out.resyncs.append((asset, reason))
        out.counts[f"resync:{reason}"] += 1
        if was_live:
            row = self._base_row(book, ts)
            row.update({"book_ts_problem": INVALID_BOOK_TS, "ws_event": "resync", "ws_state": "resync",
                        "resync_reason": reason, "book_hash": None})
            out.rows.append(row)


# --- market metadata --------------------------------------------------------------------------


def asset_meta(markets_rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """token_id -> Gamma market metadata from the latest row per market (scanner output).

    A token listed by two different markets is ambiguous and left unmapped (fail closed).
    """
    latest, _ = latest_by_time(markets_rows, lambda row: str(row.get("id") or "") or None)
    meta: dict[str, dict[str, Any]] = {}
    ambiguous: set[str] = set()
    for market_id, row in latest.items():
        tokens = row.get("token_ids") or []
        outcomes = row.get("outcomes") or []
        if not isinstance(tokens, list):
            continue
        for index, token in enumerate(tokens):
            token = str(token)
            if token in meta and meta[token]["market_id"] != market_id:
                ambiguous.add(token)
            meta[token] = {
                "market_id": market_id,
                "question": row.get("question") or "",
                "outcome": str(outcomes[index]) if isinstance(outcomes, list) and index < len(outcomes) else "",
                "condition_id": row.get("condition_id") or "",
                "liquidity_usd": row.get("liquidity_usd"),
            }
    for token in ambiguous:
        meta.pop(token, None)
    return meta


# --- output file ------------------------------------------------------------------------------


class JsonlAppender:
    """Append-safe JSONL sink: one complete line per os.write on an O_APPEND file descriptor."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._fd: int | None = None
        self.rows = 0

    def _open(self) -> int:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)
        size = os.fstat(fd).st_size
        if size:
            with open(self.path, "rb") as handle:
                handle.seek(size - 1)
                if handle.read(1) != b"\n":
                    _write_all(fd, b"\n")  # seal a torn line left by a crash
        return fd

    def write(self, row: dict[str, Any]) -> None:
        line = (json.dumps(row, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")
        if self._fd is None:
            self._fd = self._open()
        _write_all(self._fd, line)
        self.rows += 1

    def close(self) -> None:
        if self._fd is None:
            return
        try:
            os.fsync(self._fd)
        except OSError:
            pass
        os.close(self._fd)
        self._fd = None


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


# --- transport --------------------------------------------------------------------------------


class TransportClosed(Exception):
    """The connection is gone (closed by either side, or a network error)."""


class TransportUnavailable(RuntimeError):
    """The optional websockets package is not installed."""


class Connection(Protocol):
    def send(self, text: str) -> None: ...

    def recv(self, timeout: float) -> str | None:
        """The next text frame, None on timeout; raises TransportClosed when disconnected."""

    def close(self) -> None: ...


class Transport(Protocol):
    def connect(self, url: str, timeout: float) -> Connection: ...


class UrlNotAllowed(ValueError):
    """Only the public market channel may be connected to."""


class FrameNotAllowed(ValueError):
    """Only PING and the public market subscription frames may be sent."""


def check_market_url(url: object) -> str:
    """Allowlist, enforced before any connection: exactly wss://ws-subscriptions-clob.polymarket.com/ws/market.

    Scheme, host and path must match exactly; a port, user info, query, fragment, trailing slash,
    extra path segment, ws://, another host or the user channel (/ws/user) raises UrlNotAllowed.
    """
    if not isinstance(url, str) or url != MARKET_WS_URL:
        raise UrlNotAllowed(f"refusing WebSocket URL other than the public market channel: {url!r}")
    parts = urlsplit(url)
    if (parts.scheme != ALLOWED_SCHEME or parts.netloc != ALLOWED_HOST or parts.hostname != ALLOWED_HOST
            or parts.path != ALLOWED_PATH or parts.port is not None or parts.username or parts.password
            or parts.query or parts.fragment):
        raise UrlNotAllowed(f"refusing WebSocket URL other than the public market channel: {url!r}")
    return url


_SUBSCRIBE_KEYS = frozenset({"assets_ids", "type", "initial_dump"})
_UPDATE_KEYS = frozenset({"operation", "assets_ids"})


def check_outgoing_frame(text: object) -> str:
    """Allowlist for every frame sent: the text PING, the public market subscription
    {"assets_ids", "type": "market", "initial_dump": true}, or an update
    {"operation": "subscribe"|"unsubscribe", "assets_ids"}. Nothing else (no auth, no markets,
    no user channel, no extra keys) leaves the process."""
    if text == PING:
        return PING
    if not isinstance(text, str):
        raise FrameNotAllowed("frame must be text")
    try:
        frame = json.loads(text)
    except ValueError as exc:
        raise FrameNotAllowed("frame is not JSON") from exc
    if not isinstance(frame, dict):
        raise FrameNotAllowed("frame must be an object")
    keys = frozenset(frame)
    if keys == _SUBSCRIBE_KEYS:
        if frame["type"] != "market" or frame["initial_dump"] is not True:
            raise FrameNotAllowed("only the public market subscription is allowed")
    elif keys == _UPDATE_KEYS:
        if frame["operation"] not in ("subscribe", "unsubscribe"):
            raise FrameNotAllowed("bad operation")
    else:
        raise FrameNotAllowed(f"frame keys not allowed: {sorted(keys)}")
    ids = frame["assets_ids"]
    if not isinstance(ids, list) or not ids or not all(isinstance(item, str) for item in ids):
        raise FrameNotAllowed("assets_ids must be a non-empty list of strings")
    try:
        validate_assets(ids)
    except ValueError as exc:
        raise FrameNotAllowed(str(exc)) from exc
    return text


class WebsocketsTransport:
    """The real adapter. ``websockets`` is optional and imported here only."""

    def connect(self, url: str, timeout: float) -> Connection:
        check_market_url(url)  # before importing or opening anything
        try:
            from websockets.exceptions import ConnectionClosed  # optional: requirements-ws.txt
            from websockets.sync.client import connect
        except ImportError as exc:
            raise TransportUnavailable(
                "the optional websockets package is not installed: pip install -r requirements-ws.txt") from exc
        # Application heartbeat (text PING) instead of protocol pings. No extra headers, no auth:
        # only these two keyword arguments are passed (tests/test_ws_recorder.py inspects them).
        socket = connect(url, open_timeout=timeout, ping_interval=None)
        return _WebsocketsConnection(socket, ConnectionClosed)


class _WebsocketsConnection:
    def __init__(self, socket: Any, closed: type[BaseException]) -> None:
        self.socket = socket
        self.closed = closed

    def send(self, text: str) -> None:
        check_outgoing_frame(text)
        try:
            self.socket.send(text)
        except (self.closed, OSError) as exc:
            raise TransportClosed(str(exc)) from exc

    def recv(self, timeout: float) -> str | None:
        try:
            data = self.socket.recv(timeout=timeout)
        except TimeoutError:
            return None
        except (self.closed, OSError) as exc:
            raise TransportClosed(str(exc)) from exc
        return data if isinstance(data, str) else bytes(data).decode("utf-8", errors="replace")

    def close(self) -> None:
        try:
            self.socket.close()
        except Exception:  # noqa: BLE001 - closing a dead socket must not mask the reason
            pass


# --- runner -----------------------------------------------------------------------------------


def backoff_delay(attempt: int, base: float = WS_BACKOFF_BASE_S, cap: float = WS_BACKOFF_CAP_S) -> float:
    """base * 2**attempt seconds, capped; attempt 0 is the first retry."""
    if attempt <= 0:
        return min(base, cap)
    if attempt >= 64:
        return cap
    return min(cap, base * (2 ** attempt))


def subscribe_frame(assets: list[str]) -> str:
    return json.dumps({"assets_ids": assets, "type": "market", "initial_dump": True})


def resubscribe_frames(assets: list[str]) -> list[str]:
    return [json.dumps({"operation": "unsubscribe", "assets_ids": assets}),
            json.dumps({"operation": "subscribe", "assets_ids": assets})]


@dataclass
class RunReport:
    connects: int = 0
    connect_failures: int = 0
    disconnects: Counter = field(default_factory=Counter)
    rows: int = 0
    backoffs: list[float] = field(default_factory=list)
    counts: Counter = field(default_factory=Counter)
    pings: int = 0
    unmapped_assets: list[str] = field(default_factory=list)
    ended_by: str = ""  # "deadline" or "stopped" (Task AI)


class Escalation:
    """Task AP (Sentinel design gap 13): sliding-window counts the recorder reports in its heartbeat.

    ``reconnects_in_window``: connect attempts after the first (a failed connect or a disconnect)
    within RECORDER_ESCALATION_WINDOW_S. ``gap_resyncs_in_window``: per asset, resyncs not caused by a
    disconnect within the window. src/sentinel_tick.py alerts at RECORDER_RECONNECT_ALERT_COUNT
    reconnects and halts the asset's market at RECORDER_GAP_RESYNC_HALT_COUNT gap resyncs.
    """

    def __init__(self, window_s: float = RECORDER_ESCALATION_WINDOW_S,
                 monotonic: Callable[[], float] | None = None) -> None:
        self.window_s = float(window_s)
        self.monotonic = monotonic or time.monotonic
        self.reconnects: deque[float] = deque()
        self.gaps: dict[str, deque[float]] = {}

    def _prune(self, queue: deque[float], now: float) -> None:
        while queue and now - queue[0] > self.window_s:
            queue.popleft()

    def reconnect(self) -> bool:
        """Record one; True when the alert count is reached by it."""
        now = self.monotonic()
        self.reconnects.append(now)
        self._prune(self.reconnects, now)
        return len(self.reconnects) >= RECORDER_RECONNECT_ALERT_COUNT

    def gap_resync(self, asset: str) -> bool:
        now = self.monotonic()
        queue = self.gaps.setdefault(asset, deque())
        queue.append(now)
        self._prune(queue, now)
        return len(queue) >= RECORDER_GAP_RESYNC_HALT_COUNT

    def snapshot(self) -> dict[str, Any]:
        now = self.monotonic()
        self._prune(self.reconnects, now)
        gaps = {}
        for asset, queue in sorted(self.gaps.items()):
            self._prune(queue, now)
            if queue:
                gaps[asset] = len(queue)
        return {"window_s": self.window_s, "reconnects_in_window": len(self.reconnects),
                "gap_resyncs_in_window": gaps}


def validate_assets(assets: Iterable[str]) -> list[str]:
    cleaned: list[str] = []
    for asset in assets:
        if not isinstance(asset, str) or not asset or asset != asset.strip() or any(c.isspace() for c in asset):
            raise ValueError(f"bad asset id: {asset!r}")
        if asset not in cleaned:
            cleaned.append(asset)
    if not cleaned:
        raise ValueError("no asset ids")
    return cleaned


class Recorder:
    def __init__(
        self,
        assets: Iterable[str],
        *,
        transport: Transport,
        sink: Any,
        max_runtime_s: float,
        meta: dict[str, dict[str, Any]] | None = None,
        url: str = MARKET_WS_URL,
        monotonic: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
        now: Callable[[], str] | None = None,
        stop: Callable[[], bool] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        heartbeat: Any = None,
    ) -> None:
        """``stop`` (Task AI): polled at least every WS_STOP_POLL_S; once true the run ends like a deadline
        (graceful shutdown). ``on_event`` (Task AI) receives one dict per connect, disconnect, backoff,
        resync, resubscribe, asset going live and end of run. Both are optional; without them the
        recorder behaves exactly as in Task AG.
        ``heartbeat`` (Task AP): a src.heartbeat.Heartbeat; beaten at least every
        HEARTBEAT_RECORDER_EVERY_S from the main loop, also while backing off (state ``paused``), with
        the gap-13 escalation counts in its progress (written at once when a count is reached)."""
        check_market_url(url)
        runtime = _number(max_runtime_s)
        if runtime is None or runtime <= 0:
            raise ValueError("max_runtime_s must be a finite number > 0")
        self.assets = validate_assets(assets)
        self.core = BookCore(self.assets, meta)
        self.transport = transport
        self.sink = sink
        self.max_runtime_s = runtime
        self.url = url
        self.monotonic = monotonic or time.monotonic
        self.sleep = sleep or time.sleep
        self.now = now or utc_now
        self.stop = stop
        self.on_event = on_event
        self.report = RunReport(unmapped_assets=[a for a in self.assets if a not in (meta or {})])
        self.meta = dict(meta or {})
        self.heartbeat = heartbeat
        self.escalation = Escalation(monotonic=self.monotonic)

    def _stopped(self) -> bool:
        return self.stop is not None and bool(self.stop())

    def _event(self, name: str, **fields: Any) -> None:
        if self.on_event is not None:
            self.on_event({"ts": self.now(), "event": name, **fields})

    def progress(self) -> dict[str, Any]:
        """Heartbeat progress: counts plus the gap-13 escalation window (with each asset's market id)."""
        esc = self.escalation.snapshot()
        markets = {asset: str((self.meta.get(asset) or {}).get("market_id") or "")
                   for asset in esc["gap_resyncs_in_window"]}
        return {"rows": self.report.rows, "connects": self.report.connects,
                "connect_failures": self.report.connect_failures, "assets": len(self.assets),
                "assets_live": len(self.assets) - len(self.core.awaiting()), **esc, "asset_markets": markets}

    def _beat(self, state: str = "running", *, force: bool = False) -> None:
        if self.heartbeat is not None:
            self.heartbeat.beat(state, self.progress(), force=force)

    def _write(self, out: Outcome, before: set[str] | None = None) -> None:
        for row in out.rows:
            self.sink.write(row)
            self.report.rows += 1
        self.report.counts.update(out.counts)
        escalate = False
        for asset, reason in out.resyncs:
            if not reason.startswith("disconnect_") and reason not in NOT_A_GAP:
                escalate = self.escalation.gap_resync(asset) or escalate
        if escalate:
            self._beat(force=True)
        if self.on_event is not None:
            for asset, reason in out.resyncs:
                self._event("resync", asset=asset, reason=reason)
            if before is not None:
                awaiting = set(self.core.awaiting())
                for asset in self.assets:
                    if asset in before and asset not in awaiting:
                        self._event("live", asset=asset)

    def _send(self, connection: Connection, frame: str) -> None:
        connection.send(check_outgoing_frame(frame))

    def _pause(self, attempt: int, deadline: float) -> None:
        delay = min(backoff_delay(attempt), max(0.0, deadline - self.monotonic()))
        self.report.backoffs.append(delay)
        self._event("backoff", delay_s=delay, attempt=attempt)
        self._beat("paused", force=True)
        if delay <= 0:
            return
        if self.stop is None and self.heartbeat is None:
            self.sleep(delay)
            return
        # Short slices: a shutdown is not held up by a long backoff, and the heartbeat keeps beating.
        step = min(WS_STOP_POLL_S if self.stop is not None else delay,
                   HEARTBEAT_RECORDER_EVERY_S if self.heartbeat is not None else delay)
        end = self.monotonic() + delay
        while not self._stopped():
            left = end - self.monotonic()
            if left <= 0:
                break
            self.sleep(min(left, step))
            self._beat("paused")

    def run(self) -> RunReport:
        deadline = self.monotonic() + self.max_runtime_s
        attempt = 0
        self._beat(force=True)
        while self.monotonic() < deadline and not self._stopped():
            try:
                connection = self.transport.connect(
                    self.url, min(WS_RESYNC_TIMEOUT_S, max(0.0, deadline - self.monotonic())))
            except TransportUnavailable:
                raise
            except Exception as exc:  # noqa: BLE001 - any connect failure backs off and retries
                self.report.connect_failures += 1
                self._event("connect_failed", error=type(exc).__name__)
                if self.escalation.reconnect():
                    self._beat(force=True)
                self._pause(attempt, deadline)
                attempt += 1
                continue
            self.report.connects += 1
            try:
                self._event("connected", connects=self.report.connects)
                reason, healthy = self._session(connection, deadline)
            finally:
                connection.close()
            if reason in ("deadline", "stopped"):
                break
            self.report.disconnects[reason] += 1
            self._event("disconnected", reason=reason, healthy=healthy)
            if self.escalation.reconnect():
                self._beat(force=True)
            self._write(self.core.invalidate_all(f"disconnect_{reason}", self.now()))
            if healthy:
                attempt = 0
            self._pause(attempt, deadline)
            attempt += 1
        self.report.ended_by = "stopped" if self._stopped() else "deadline"
        self._event("end", ended_by=self.report.ended_by)
        return self.report

    def _session(self, connection: Connection, deadline: float) -> tuple[str, bool]:
        healthy = False
        try:
            self._send(connection, subscribe_frame(self.assets))
        except TransportClosed:
            return "closed", healthy
        self.core.start_waiting(self.monotonic())
        next_ping = self.monotonic() + WS_PING_INTERVAL_S
        pong_due: float | None = None  # Task AW: first unanswered PING's send time + WS_PONG_TIMEOUT_S
        while True:
            current = self.monotonic()
            if current >= deadline:
                return "deadline", healthy
            if self._stopped():
                return "stopped", healthy
            self._beat()
            try:
                if pong_due is not None and current >= pong_due:
                    return "pong_timeout", healthy
                if current >= next_ping:
                    self._send(connection, PING)
                    self.report.pings += 1
                    next_ping = current + WS_PING_INTERVAL_S
                    if pong_due is None:
                        pong_due = current + WS_PONG_TIMEOUT_S
                oldest = self.core.oldest_awaiting_since()
                if oldest is not None and current - oldest >= WS_RESYNC_TIMEOUT_S:
                    return "resync_timeout", healthy
                waits = [deadline - current, next_ping - current]
                if self.stop is not None:
                    waits.append(WS_STOP_POLL_S)
                if self.heartbeat is not None:
                    waits.append(HEARTBEAT_RECORDER_EVERY_S)
                if oldest is not None:
                    waits.append(oldest + WS_RESYNC_TIMEOUT_S - current)
                if pong_due is not None:
                    waits.append(pong_due - current)
                text = connection.recv(max(0.0, min(waits)))
                if text is None:
                    continue
                before = set(self.core.awaiting())
                out = self.core.handle_text(text, self.now(), self.monotonic())
                if out.counts["pong"]:
                    pong_due = None
                self._write(out, before)
                fresh = [asset for asset, _ in out.resyncs if asset not in before]
                fresh = list(dict.fromkeys(fresh))
                if fresh:
                    self._event("resubscribe", assets=fresh)
                    for frame in resubscribe_frames(fresh):
                        self._send(connection, frame)
                if self.core.all_live():
                    healthy = True
            except TransportClosed:
                return "closed", healthy


# --- CLI --------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.ws_recorder",
        description="M1 read-only recorder for the public Polymarket CLOB WebSocket market channel. No orders.",
        epilog="Exit codes: 0 ran until --max-runtime and wrote at least one book row; 1 wrote no book row; "
               "2 usage error; 3 the optional websockets package is not installed (requirements-ws.txt); "
               "4 writing the output file failed; 6 config/risk.toml off its pins (global halt recorded). "
               "See docs/EXIT_CODES.md.",
    )
    parser.add_argument("--assets", nargs="+", required=True, help="Token (asset) ids; spaces or commas.")
    parser.add_argument("--max-runtime", type=float, required=True, help="Stop after this many seconds.")
    parser.add_argument("--out", type=Path, default=BOOKS_PATH, help="JSONL book rows (default data/books.jsonl).")
    parser.add_argument("--markets", type=Path, default=MARKETS_PATH,
                        help="Scanner markets.jsonl used to map tokens to market ids (read-only).")
    return parser


def main(argv: list[str] | None = None, *, transport: Transport | None = None) -> int:
    """Task AP: the run writes logs/heartbeat/ws_recorder.json (src.heartbeat) every
    HEARTBEAT_RECORDER_EVERY_S and a final beat with the exit code (exit 4 is a global halt for the
    resident Sentinel, src/sentinel_tick.py)."""
    from src.heartbeat import Heartbeat  # local: keeps the module import light

    hb = Heartbeat("ws_recorder", every_s=HEARTBEAT_RECORDER_EVERY_S)
    hb.beat("starting", force=True)
    try:
        code = _main(argv, transport=transport, heartbeat=hb)
    except BaseException:
        hb.finish(None, crashed=True)
        raise
    hb.finish(code)
    return code


def _main(argv: list[str] | None, *, transport: Transport | None, heartbeat: Any) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return EXIT_USAGE if exc.code else EXIT_OK
    assets = [part for item in args.assets for part in item.split(",") if part]
    try:
        assets = validate_assets(assets)
        runtime = _number(args.max_runtime)
        if runtime is None or runtime <= 0:
            raise ValueError("--max-runtime must be a finite number > 0")
    except ValueError as exc:
        print(f"usage error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    from src.pins import PinWatch, refuse_start  # Task AS (gap 5): runtime pin checks

    if refuse_start("ws_recorder"):
        return EXIT_PIN_DRIFT
    watch = PinWatch("ws_recorder")
    meta = asset_meta(read_jsonl(args.markets))
    sink = JsonlAppender(args.out)
    recorder = Recorder(assets, transport=transport or WebsocketsTransport(), sink=sink,
                        max_runtime_s=runtime, meta=meta, heartbeat=heartbeat, stop=watch.drifted)
    try:
        report = recorder.run()
    except TransportUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_NO_WEBSOCKETS
    except OSError as exc:
        print(f"ws_recorder: writing {args.out} failed: {exc}", file=sys.stderr)
        return EXIT_WRITE_FAILED
    finally:
        try:
            sink.close()
        except OSError:
            pass
    resyncs = {key.split(":", 1)[1]: value for key, value in sorted(report.counts.items()) if key.startswith("resync:")}
    print(
        f"ws_recorder rows={report.rows} snapshots={report.counts['snapshot_applied']} "
        f"price_changes={report.counts['price_change_applied']} connects={report.connects} "
        f"connect_failures={report.connect_failures} disconnects={dict(report.disconnects)} "
        f"resyncs={resyncs} pings={report.pings} unmapped_assets={len(report.unmapped_assets)} orders=none"
    )
    print(f"wrote={args.out}")
    if watch.problems:
        return EXIT_PIN_DRIFT
    if report.counts["snapshot_applied"] < 1:
        print("ws_recorder: no book snapshot recorded", file=sys.stderr)
        return EXIT_NO_ROWS
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
