"""Issue #118 (Task CD, M2-B): the NewsForecaster inbox ``poly_forecast_inbox/1`` and its anti-leak seal.

M2's forecasts come from Poly研判 as one JSON file per forecast, delivered by atomic rename into
``runs/m2/inbox/pending/<UTC date>/<uuid>.json`` (spec: /workspace/pod-bots/poly-forecast-m2-runner-spec.md
section B). This module checks one file and seals it. It never reads a book, never opens a socket, uses no
key and calls no model. A file that fails any check is left where it is, untouched, and nothing is sealed or
sent (fail closed).

Rules (each rejection reason has a test):
- ``schema_error``: not a JSON object, wrong ``schema``, an unknown key, a missing or ill-typed field
  (``author``, ``model_version``, ``market_id``, ``condition_id``, ``outcome``, ``as_of`` as
  ``YYYY-MM-DDTHH:MM:SS[.fff]Z``, ``sources`` a non-empty list of ``{"url": http(s) URL, ...}``, ``rationale``
  non-empty text, ``market_price_seen`` a bool), or numbers that are not numbers.
- ``non_finite``: ``p``, ``p_low`` or ``p_high`` is NaN or infinite.
- ``missing_outcome_index`` / ``invalid_outcome_index``, ``missing_token_id``, ``missing_interval`` (either
  bound absent): #105's ids are mandatory here, never defaulted.
- ``invalid_probability``: not ``0 < p_low <= p <= p_high < 1``.
- ``interval_too_narrow``: ``(p_high - p_low) / 2 < FORECAST_MIN_HALF_WIDTH`` (M2 pending).
- ``price_seen``: ``market_price_seen`` is true (blind first, then the market).
- ``price_source_leak``: a source URL on polymarket.com or any of its subdomains. Stricter than the spec,
  which would allow the resolution-rules page: on polymarket.com the rules are shown on the market page
  next to its prices, so no polymarket.com URL is accepted as a research source.
- ``unknown_market`` (not in the scanner rows given), ``condition_mismatch``, ``token_mismatch`` (the token or
  the outcome label is not the scanner's ``token_ids[outcome_index]`` / ``outcomes[outcome_index]``).
- ``outcome_may_be_known``: at ``now`` the market is closed, not accepting orders (unknown counts as not),
  in UMA resolution (src/structural_gate.py) or closer to its end than MIN_HOURS_TO_EXPIRY
  (src/expiry_rule.py; missing or unparseable end date too).
- ``stale_forecast``: ``as_of`` after ``now`` or more than FORECAST_MAX_ASOF_LAG_S (M2 pending) before it.

Sealing (``ingest_one``): a valid file is sealed through ``forecaster.emit_probability(require_ids=True)``
(the forecast row first, then the bus signal), with ``note`` = ``inbox_sha256=<sha256 of the file bytes>``,
so FORECAST_ROW_FIELDS is unchanged and the sources / rationale can be traced from the archived file. The
file is then moved to ``archive/<date>/<name>`` and made read-only. Idempotent: a file whose sha256 is
already in a sealed row is a ``duplicate`` (nothing written; a byte-identical pending copy is removed once
the archive holds it); an archived file of the same name with other bytes is ``inbox_conflict``; a sealed
forecast_id with other content is refused by calibration_ids (``forecast_log_error``, no signal). Category,
tags, end date and event come from the scanner row, never from the inbox file.

Seal before book: ``read_book`` (optional, injected) is the only way this module reaches a book, and it is
called once, only after the forecast row and the signal exist; ``book_fetched_at`` must then be strictly
after ``forecast_ts`` (else ``forecast_not_sealed_before_book`` and the book is discarded).

``write_candidates`` writes the price-free candidate list Poly研判 forecasts from: a whitelist of fields
(no bid, ask, spread, price, outcomePrices, lastTradePrice or volume), written atomically.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urlsplit

from src import forecaster
from src.calibration_ids import ForecastLogError
from src.expiry_rule import expiry_problem
from src.podlib import BOOK_PREFIX, utc_now_ms
from src.structural_gate import market_uma_resolving
from src.thresholds import FORECAST_MAX_ASOF_LAG_S, FORECAST_MIN_HALF_WIDTH

INBOX_SCHEMA = "poly_forecast_inbox/1"
REQUIRED = ("schema", "author", "model_version", "market_id", "condition_id", "outcome_index", "outcome",
            "token_id", "p", "p_low", "p_high", "as_of", "sources", "rationale", "market_price_seen")
OPTIONAL = ("category", "end_date", "event_id", "event_slug")
SOURCE_KINDS = ("rss", "official", "resolution_source", "other")
SOURCE_KEYS = ("url", "title", "published_at", "kind")
# Candidate fields Poly研判 may see (a whitelist: anything else, prices included, never leaves the scanner row).
CANDIDATE_FIELDS = ("market_id", "question", "condition_id", "token_ids", "outcomes", "end_date", "category",
                    "tags", "description", "resolution_source", "event_id", "event_slug")
PRICE_FIELDS = ("best_bid", "best_ask", "spread", "price", "outcome_prices", "outcomePrices", "lastTradePrice",
                "last_trade_price", "bestBid", "bestAsk", "volume", "volume_usd", "volume_24h_usd", "volumeNum",
                "volume24hr", "liquidity_usd", "liquidity", "liquidityNum", "price_source")
REASONS = ("schema_error", "non_finite", "missing_outcome_index", "invalid_outcome_index", "missing_token_id",
           "missing_interval", "invalid_probability", "interval_too_narrow", "price_seen", "price_source_leak",
           "unknown_market", "condition_mismatch", "token_mismatch", "outcome_may_be_known", "stale_forecast",
           "invalid_now", "inbox_conflict", "unreadable_file", "forecast_log_error",
           "forecast_not_sealed_before_book")
SEALED, DUPLICATE, REJECTED = "sealed", "duplicate", "rejected"
_AS_OF = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")


@dataclass(frozen=True)
class InboxResult:
    path: str
    sha256: str
    status: str  # sealed | duplicate | rejected
    reasons: tuple[str, ...]
    forecast_id: str | None
    forecast_ts: str | None
    book_fetched_at: str | None = None


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _aware(value: object) -> bool:
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None


def parse_as_of(value: object) -> datetime | None:
    """``YYYY-MM-DDTHH:MM:SS[.fff]Z`` as an aware UTC datetime; anything else None."""
    if not isinstance(value, str) or not _AS_OF.match(value):
        return None
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return None


# The Polymarket registrable domain, taken from the pinned CLOB book endpoint rather than spelled out here, so the
# guard's host-literal allowlist stays as it is. Any host on it (www, clob, gamma, docs, ...) counts as a price page.
_PRICE_DOMAIN = ".".join((urlsplit(BOOK_PREFIX).hostname or "").lower().split(".")[-2:])


def _leaks_prices(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    return host == _PRICE_DOMAIN or host.endswith("." + _PRICE_DOMAIN)


def _source_problems(sources: object) -> tuple[bool, bool]:
    """(schema_ok, leak) for the sources list."""
    if not isinstance(sources, list) or not sources:
        return False, False
    leak = False
    for item in sources:
        if not isinstance(item, dict) or set(item) - set(SOURCE_KEYS) or not _text(item.get("url")):
            return False, leak
        url = item["url"].strip()
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return False, leak
        if "kind" in item and item["kind"] not in SOURCE_KINDS:
            return False, leak
        for key in ("title", "published_at"):
            if key in item and not isinstance(item[key], str):
                return False, leak
        leak = leak or _leaks_prices(url)
    return True, leak


def _ordered(reasons: Iterable[str]) -> list[str]:
    return sorted(dict.fromkeys(reasons), key=REASONS.index)


def validate_inbox_row(row: Mapping, *, market_row: Mapping | None, now: datetime) -> list[str]:
    """Every reason this inbox row cannot be sealed at ``now`` against its scanner row ([] = it can)."""
    if not isinstance(row, Mapping):
        return ["schema_error"]
    reasons: list[str] = []
    if not _aware(now):
        reasons.append("invalid_now")
    keys = set(row)
    if row.get("schema") != INBOX_SCHEMA or keys - set(REQUIRED) - set(OPTIONAL):
        reasons.append("schema_error")
    for key in ("author", "model_version", "market_id", "condition_id", "outcome", "rationale"):
        if not _text(row.get(key)):
            reasons.append("schema_error")
    for key in OPTIONAL:
        if key in row and row[key] is not None and not isinstance(row[key], str):
            reasons.append("schema_error")
    seen = row.get("market_price_seen")
    if not isinstance(seen, bool):
        reasons.append("schema_error")
    elif seen:
        reasons.append("price_seen")
    sources_ok, leak = _source_problems(row.get("sources"))
    if not sources_ok:
        reasons.append("schema_error")
    if leak:
        reasons.append("price_source_leak")
    as_of = parse_as_of(row.get("as_of"))
    if as_of is None:
        reasons.append("schema_error")
    elif _aware(now) and (as_of > now or now - as_of > timedelta(seconds=FORECAST_MAX_ASOF_LAG_S)):
        reasons.append("stale_forecast")

    # ids (#105): never defaulted
    index = row.get("outcome_index")
    if index is None:
        reasons.append("missing_outcome_index")
    elif isinstance(index, bool) or not isinstance(index, int) or index < 0:
        reasons.append("invalid_outcome_index")
        index = None
    token = row.get("token_id")
    if token is None or (isinstance(token, str) and not token.strip()):
        reasons.append("missing_token_id")
        token = None
    elif not isinstance(token, str):
        reasons.append("schema_error")
        token = None

    # probability and interval
    values = {key: row.get(key) for key in ("p", "p_low", "p_high")}
    if values["p_low"] is None or values["p_high"] is None:
        reasons.append("missing_interval")
    usable = True
    for key, value in values.items():
        if value is None:
            usable = False
            if key == "p":
                reasons.append("schema_error")
        elif not _number(value):
            reasons.append("schema_error")
            usable = False
        elif not math.isfinite(value):
            reasons.append("non_finite")
            usable = False
    if usable:
        p, low, high = float(values["p"]), float(values["p_low"]), float(values["p_high"])
        if not 0.0 < low <= p <= high < 1.0:
            reasons.append("invalid_probability")
        elif (high - low) / 2.0 < FORECAST_MIN_HALF_WIDTH:
            reasons.append("interval_too_narrow")

    # the scanner's market
    if market_row is None:
        reasons.append("unknown_market")
    else:
        if str(market_row.get("id") or "") != str(row.get("market_id") or ""):
            reasons.append("unknown_market")
        if not _text(market_row.get("condition_id")) or market_row.get("condition_id") != row.get("condition_id"):
            reasons.append("condition_mismatch")
        tokens = [str(t) for t in (market_row.get("token_ids") or [])]
        outcomes = [str(o) for o in (market_row.get("outcomes") or [])]
        if index is not None:
            if index >= len(tokens):
                reasons.append("invalid_outcome_index")
            elif token is not None and (token != tokens[index] or index >= len(outcomes)
                                        or row.get("outcome") != outcomes[index]):
                reasons.append("token_mismatch")
        if _aware(now) and (market_row.get("closed") is not False or market_row.get("accepting_orders") is not True
                            or market_uma_resolving(market_row)
                            or expiry_problem(market_row.get("end_date"), now) is not None):
            reasons.append("outcome_may_be_known")
    return _ordered(reasons)


def _sealed_shas(forecasts_path: Path) -> set[str]:
    """inbox_sha256 values already in sealed rows. A damaged file raises ForecastLogError (fail closed)."""
    path = Path(forecasts_path)
    if not path.exists():
        return set()
    out: set[str] = set()
    try:
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            if not line.strip():
                continue
            note = json.loads(line).get("note")
            if isinstance(note, str) and note.startswith("inbox_sha256="):
                out.add(note[len("inbox_sha256="):])
    except (OSError, ValueError, AttributeError) as exc:
        raise ForecastLogError(f"forecasts file unreadable: {exc}") from exc
    return out


def _archive_target(path: Path, archive_dir: Path) -> Path:
    return Path(archive_dir) / path.parent.name / path.name


def ingest_one(path: Path, *, markets: Mapping[str, Mapping], now: datetime, forecasts_path: Path,
               bus_path: Path, archive_dir: Path,
               read_book: Callable[[Mapping, str], Any] | None = None) -> InboxResult:
    """Check one pending inbox file and, if valid, seal it (forecast row, then bus signal), then archive it.

    Rejected files stay where they are. ``read_book(market_row, token_id)``, when given, is called once and only
    after the seal; its result is not returned (the runner reads its own book), only when it was called."""
    path = Path(path)
    try:
        data = path.read_bytes()
    except OSError:
        return InboxResult(str(path), "", REJECTED, ("unreadable_file",), None, None)
    sha = hashlib.sha256(data).hexdigest()

    def rejected(*reasons: str) -> InboxResult:
        return InboxResult(str(path), sha, REJECTED, tuple(reasons), None, None)

    target = _archive_target(path, archive_dir)
    if target.exists():
        try:
            same = hashlib.sha256(target.read_bytes()).hexdigest() == sha
        except OSError:
            return rejected("inbox_conflict")
        if not same:
            return rejected("inbox_conflict")
    try:
        if sha in _sealed_shas(forecasts_path):
            if target.exists():
                path.unlink()  # a byte-identical copy of an archived, sealed file
            else:
                _archive(path, target)
            return InboxResult(str(path), sha, DUPLICATE, (), None, None)
    except ForecastLogError:
        return rejected("forecast_log_error")
    except OSError:
        return InboxResult(str(path), sha, DUPLICATE, (), None, None)
    try:
        row = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return rejected("schema_error")
    market_row = markets.get(str(row.get("market_id") or "")) if isinstance(row, dict) else None
    reasons = validate_inbox_row(row, market_row=market_row, now=now)
    if reasons:
        return rejected(*reasons)
    assert market_row is not None
    try:
        message = forecaster.emit_probability(
            market=str(row["market_id"]), model_prob=float(row["p"]), note=f"inbox_sha256={sha}",
            bus_path=bus_path, forecasts_path=forecasts_path, token_id=row["token_id"],
            outcome_index=row["outcome_index"], outcome=row["outcome"], condition_id=row["condition_id"],
            p_low=float(row["p_low"]), p_high=float(row["p_high"]), category=market_row.get("category"),
            tags=market_row.get("tags"), end_date=market_row.get("end_date") or None,
            model_version=row["model_version"], event_id=market_row.get("event_id"),
            event_slug=market_row.get("event_slug"), require_ids=True)
    except ForecastLogError:
        return rejected("forecast_log_error")
    except ValueError:
        return rejected("schema_error")
    payload = message.get("payload") or {}
    forecast_id, forecast_ts = payload.get("forecast_id"), payload.get("forecast_ts")
    try:
        _archive(path, target)
    except OSError:
        pass  # sealed; the sha in the sealed row makes the next pass a duplicate that archives it
    if read_book is None:
        return InboxResult(str(path), sha, SEALED, (), forecast_id, forecast_ts)
    read_book(market_row, str(row["token_id"]))
    fetched = utc_now_ms()
    sealed_at = parse_as_of(forecast_ts) if isinstance(forecast_ts, str) else None
    fetched_at = parse_as_of(fetched)
    if sealed_at is None or fetched_at is None or not sealed_at < fetched_at:
        return InboxResult(str(path), sha, SEALED, ("forecast_not_sealed_before_book",), forecast_id, forecast_ts,
                           fetched)
    return InboxResult(str(path), sha, SEALED, (), forecast_id, forecast_ts, fetched)


def _archive(path: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(path, target)
    os.chmod(target, 0o444)


def candidate_row(market: Mapping) -> dict[str, Any]:
    """One price-free candidate: only CANDIDATE_FIELDS (the scanner's ``id`` becomes ``market_id``)."""
    source = dict(market)
    source.setdefault("market_id", source.get("id"))
    out: dict[str, Any] = {}
    for key in CANDIDATE_FIELDS:
        if key in source:
            out[key] = source[key]
    return out


def write_candidates(markets: Iterable[Mapping], path: Path) -> int:
    """Write the price-free candidate list (one JSON object per line), atomically. Returns the row count."""
    rows = [candidate_row(market) for market in markets]
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    text = "".join(json.dumps(row, ensure_ascii=False, allow_nan=False, sort_keys=True) + "\n" for row in rows)
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, target)
    return len(rows)

