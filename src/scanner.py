"""Phase 0 read-only Gamma scanner. No keys. No orders.

Walks public Gamma /markets/keyset, filters by config/risk.toml
(min_liquidity_usd, max_spread) plus long-tail and expiry rules,
and ranks survivors by 24h volume. Signals only; never edits risk.toml.
Outcome IDs come from positionIds for V2 markets and clobTokenIds for V1, chosen
by Gamma's `version` (select_token_ids); a missing or unknown version rejects the market.
A market needs a zoned end date at least MIN_HOURS_TO_EXPIRY (24 h, a code
constant in src/expiry_rule.py) away; otherwise it fails closed.
"""
from __future__ import annotations

import json
import math
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.podlib import (  # noqa: E402
    MARKETS_PATH,
    RISK_PATH,
    PublicGetError,
    append_jsonl,
    as_float,
    bus,
    json_list,
    load_risk,
    parse_risk_toml,
    public_get,
    utc_now,
)
from src.expiry_rule import MIN_HOURS_TO_EXPIRY, expiry_problem  # noqa: E402

# Legacy single page. Still used by paper_session (top 20 by liquidity, unfiltered).
GAMMA = (
    "https://gamma-api.polymarket.com/markets"
    "?closed=false&limit=20&order=liquidityNum&ascending=false"
)
# Gamma /markets returns 422 for offset > ~2000 and points to /markets/keyset.
GAMMA_KEYSET = (
    "https://gamma-api.polymarket.com/markets/keyset"
    "?closed=false&active=true&order=volume24hr&ascending=false"
)
PAGE_LIMIT = 100
MAX_MARKETS = 50000
MAX_PAGES = 1000

# Only the top N passing markets (by 24h volume) go to the bus; all go to data/markets.jsonl.
BUS_TOP_N = 50

# Long-tail band. Optional risk.toml overrides: scanner_min_price, scanner_max_price.
MIN_PRICE = 0.03
MAX_PRICE = 0.97

# main() exit codes. 3 also covers a page that still fails after public_get's retries.
EXIT_OK = 0
EXIT_TOO_FEW_SCANNED = 1
EXIT_NONE_PASSED = 2
EXIT_INCOMPLETE = 3

# Order matters: the first entry that fails is the market's "first" reason.
REASONS = (
    "closed",
    "missing_version",
    "unsupported_version",
    "expired",
    "missing_end_date",
    "invalid_end_date",
    "near_expiry",
    "invalid_min_hours_to_expiry",
    "invalid_now",
    "not_accepting_orders",
    "missing_bid_ask",
    "low_liquidity",
    "wide_spread",
    "missing_price",
    "long_tail_price",
)


def fetch_markets() -> list[dict]:
    data = public_get(GAMMA)
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        rows = data.get("data", [])
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    return []


def fetch_all_markets(
    getter: Callable[[str], Any] | None = None,
    *,
    max_markets: int = MAX_MARKETS,
    page_limit: int = PAGE_LIMIT,
    max_pages: int = MAX_PAGES,
    server_min_liquidity: float | None = None,
) -> tuple[list[dict], dict[str, Any]]:
    """Walk Gamma keyset pages until next_cursor is empty, a cap is hit, or paging stalls.

    server_min_liquidity adds Gamma's liquidity_num_min pre-filter (the active,
    unclosed universe is >200k markets). Rows are still re-checked client-side.
    Stalls (a repeated cursor, or a page with no unseen market) stop the walk and
    are reported as incomplete instead of spinning up to max_pages requests.
    A page that still fails after public_get's retries stops the walk with
    stop_reason "network_error" (incomplete, so main() exits 3 and writes nothing).
    """
    get = getter or public_get
    rows: list[dict] = []
    seen: set[str] = set()
    used_cursors: set[str] = set()
    cursor = ""
    pages = 0
    stop = "exhausted"
    error = ""
    base = f"{GAMMA_KEYSET}&limit={int(page_limit)}"
    if server_min_liquidity is not None:
        base += f"&liquidity_num_min={float(server_min_liquidity):g}"
    while True:
        if len(rows) >= max_markets:
            stop = "max_markets"
            break
        if pages >= max_pages:
            stop = "max_pages"
            break
        url = base
        if cursor:
            used_cursors.add(cursor)
            url += "&after_cursor=" + quote(cursor, safe="")
        try:
            data = get(url)
        except PublicGetError as exc:
            stop = "network_error"
            error = str(exc)
            break
        pages += 1
        if isinstance(data, dict):
            batch = data.get("markets") or data.get("data") or []
            cursor = str(data.get("next_cursor") or "")
        elif isinstance(data, list):
            batch, cursor = data, ""
        else:
            batch, cursor = [], ""
        added = 0
        for market in batch:
            if not isinstance(market, dict):
                continue
            key = str(market.get("id") or market.get("conditionId") or "")
            if key and key in seen:
                continue
            seen.add(key)
            rows.append(market)
            added += 1
            if len(rows) >= max_markets:
                break
        if not batch or not cursor:
            cursor = ""
            break
        if added == 0:
            stop = "no_new_rows"
            break
        if cursor in used_cursors:
            stop = "cursor_repeat"
            break
    cap_hit = stop in ("max_markets", "max_pages") and bool(cursor)
    if stop == "max_markets" and not cursor:
        stop = "exhausted"
    meta = {
        "pages": pages,
        "fetched": len(rows),
        "stop_reason": stop,
        "cap_hit": cap_hit,
        "complete": stop == "exhausted",
        "max_markets": max_markets,
        "server_min_liquidity": server_min_liquidity,
    }
    if error:
        meta["error"] = error
    return rows, meta


def _finite_number(key: str, value: Any) -> float:
    """Accept only a finite int or float. Booleans, strings, inf and nan fail closed."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RuntimeError(f"risk.toml scanner thresholds must be numbers: {key}")
    number = float(value)
    if not math.isfinite(number):
        raise RuntimeError(f"risk.toml scanner thresholds must be finite numbers: {key}")
    return number


def thresholds_from(cfg: dict[str, Any]) -> dict[str, float]:
    """Validate scanner thresholds from an already-parsed risk config. Fails closed."""
    for key in ("min_liquidity_usd", "max_spread"):
        if key not in cfg:
            raise RuntimeError(f"risk.toml missing scanner key: {key}")
    limits = {
        "min_liquidity_usd": _finite_number("min_liquidity_usd", cfg["min_liquidity_usd"]),
        "max_spread": _finite_number("max_spread", cfg["max_spread"]),
        "min_price": _finite_number("scanner_min_price", cfg.get("scanner_min_price", MIN_PRICE)),
        "max_price": _finite_number("scanner_max_price", cfg.get("scanner_max_price", MAX_PRICE)),
    }
    if limits["min_liquidity_usd"] < 0:
        raise RuntimeError("min_liquidity_usd must be >= 0")
    if limits["max_spread"] <= 0:
        raise RuntimeError("max_spread must be > 0")
    if not 0.0 <= limits["min_price"] < limits["max_price"] <= 1.0:
        raise RuntimeError("price band must satisfy 0 <= scanner_min_price < scanner_max_price <= 1")
    return limits


def load_thresholds(path: Path = RISK_PATH) -> dict[str, float]:
    """Read scanner thresholds with the same flat parser as podlib.load_risk. Read-only."""
    return thresholds_from(parse_risk_toml(path.read_text(encoding="utf-8")))


def parse_utc(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.strptime(text[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def yes_price(market: dict[str, Any]) -> tuple[float | None, str]:
    """YES / first-outcome price: bid/ask mid, else lastTradePrice, else outcomePrices[0]."""
    bid = as_float(market.get("bestBid"))
    ask = as_float(market.get("bestAsk"))
    if bid is not None and ask is not None and 0 < bid <= ask < 1:
        return (bid + ask) / 2.0, "mid"
    last = as_float(market.get("lastTradePrice"))
    if last is not None and last > 0:
        return last, "last_trade"
    prices = json_list(market.get("outcomePrices"))
    first = as_float(prices[0]) if prices else None
    if first is not None:
        return first, "outcome_prices_0"
    return None, "none"


# Outcome IDs (docs.polymarket.com, Polymarket Protocol V2 migration): Gamma's market-level
# `version` selects the field even when both are present. "v2" -> positionIds (array of
# decimal strings); "v1" -> clobTokenIds (JSON-encoded array). A missing version
# (missing_version) or any other value (unsupported_version) rejects the market; no field
# is guessed from presence. With a valid version, bad or absent IDs leave token_ids empty
# (token_ids_problem), so no book is fetched downstream; that is not a filter reason.
TOKEN_FIELD_BY_VERSION = {"v1": "clobTokenIds", "v2": "positionIds"}
VERSION_REASONS = ("missing_version", "unsupported_version")


def _decode_ids(value: Any, *, decimal: bool) -> list[str] | None:
    """Decode a list, or a JSON-encoded list, of outcome IDs.

    Returns [] when absent or empty and None when malformed: not a list, or any
    element that is not a non-empty string or an int (bools, floats, None and nested
    values are malformed). With decimal=True every ID must be ASCII digits.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    if not isinstance(value, list):
        return None
    ids: list[str] = []
    for item in value:
        if isinstance(item, bool):
            return None
        if isinstance(item, int):
            text = str(item)
        elif isinstance(item, str):
            text = item.strip()
        else:
            return None
        if not text or (decimal and not (text.isascii() and text.isdigit())):
            return None
        ids.append(text)
    return ids


def select_token_ids(market: dict[str, Any]) -> tuple[list[str], str | None, str | None]:
    """Return (token_ids, source_field, problem) for one raw Gamma market.

    problem is None, missing_version, unsupported_version, missing_token_ids,
    invalid_token_ids or token_count_mismatch; token_ids is [] whenever problem is set.
    missing_version and unsupported_version also reject the market (rejection_reasons).
    """
    version = market.get("version")
    if version is None or version == "":
        return [], None, "missing_version"
    # Exact strings only (docs: "Treat versions other than v1 and v2 as unsupported").
    if not isinstance(version, str) or version not in TOKEN_FIELD_BY_VERSION:
        return [], None, "unsupported_version"
    field = TOKEN_FIELD_BY_VERSION[version]
    ids = _decode_ids(market.get(field), decimal=version == "v2")
    if ids is None:
        return [], field, "invalid_token_ids"
    if not ids:
        return [], field, "missing_token_ids"
    outcomes = json_list(market.get("outcomes"))
    if outcomes and len(outcomes) != len(ids):
        return [], field, "token_count_mismatch"
    return ids, field, None


def normalize(market: dict[str, Any], ts: str) -> dict[str, Any]:
    tokens, token_source, token_problem = select_token_ids(market)
    outcomes = [str(item) for item in json_list(market.get("outcomes"))]
    prices = [as_float(item) for item in json_list(market.get("outcomePrices"))]
    best_bid = as_float(market.get("bestBid"))
    best_ask = as_float(market.get("bestAsk"))
    spread = as_float(market.get("spread"))
    if spread is None and best_bid is not None and best_ask is not None:
        spread = best_ask - best_bid
    price, price_source = yes_price(market)
    accepting = market.get("acceptingOrders")
    return {
        "ts": ts,
        "id": str(market.get("id") or ""),
        "question": market.get("question") or "",
        "slug": market.get("slug") or "",
        "condition_id": market.get("conditionId") or "",
        "token_ids": tokens,
        "version": market.get("version") if isinstance(market.get("version"), str) else None,
        "token_id_source": token_source,
        "token_ids_problem": token_problem,
        "outcomes": outcomes,
        "outcome_prices": prices,
        "liquidity_usd": as_float(market.get("liquidityNum") if market.get("liquidityNum") is not None else market.get("liquidity")),
        "volume_usd": as_float(market.get("volumeNum") if market.get("volumeNum") is not None else market.get("volume")),
        "volume_24h_usd": as_float(market.get("volume24hr")) or 0.0,
        "end_date": market.get("endDate") or "",
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread": spread,
        "price": price,
        "price_source": price_source,
        "accepting_orders": None if accepting is None else bool(accepting),
        "fees_enabled": bool(market.get("feesEnabled")),
        "closed": bool(market.get("closed")),
        "source": "gamma_public",
    }


def rejection_reasons(
    row: dict[str, Any],
    limits: dict[str, float],
    now: datetime,
    min_hours_to_expiry: float = MIN_HOURS_TO_EXPIRY,
) -> list[str]:
    """Every failed rule for one normalized row, in REASONS order.

    End dates go through expiry_problem: missing, unparseable, naive, date-only,
    non-string, past, or closer than min_hours_to_expiry all fail closed.
    """
    reasons: list[str] = []
    if row["closed"]:
        reasons.append("closed")
    if row["token_ids_problem"] in VERSION_REASONS:
        reasons.append(row["token_ids_problem"])
    expiry = expiry_problem(row["end_date"], now, min_hours_to_expiry)
    if expiry is not None:
        reasons.append(expiry)
    if row["accepting_orders"] is False:
        reasons.append("not_accepting_orders")
    bid, ask = row["best_bid"], row["best_ask"]
    # Gamma reports a one-sided book as bestBid 0 or bestAsk 1; treat that as missing.
    if bid is None or ask is None or bid <= 0 or ask >= 1:
        reasons.append("missing_bid_ask")
    liquidity = row["liquidity_usd"]
    if liquidity is None or liquidity < limits["min_liquidity_usd"]:
        reasons.append("low_liquidity")
    spread = row["spread"]
    if spread is None or spread > limits["max_spread"] + 1e-12:
        reasons.append("wide_spread")
    price = row["price"]
    if price is None:
        reasons.append("missing_price")
    elif price < limits["min_price"] or price > limits["max_price"]:
        reasons.append("long_tail_price")
    return sorted(reasons, key=REASONS.index)


def days_to_expiry(end_date: Any, now: datetime) -> float | None:
    end = parse_utc(end_date)
    if end is None:
        return None
    return (end - now).total_seconds() / 86400.0


def filter_markets(
    raw_markets: list[dict],
    limits: dict[str, float],
    ts: str,
    now: datetime | None = None,
    min_hours_to_expiry: float = MIN_HOURS_TO_EXPIRY,
) -> dict[str, Any]:
    """Split raw Gamma rows into passing rows (24h volume desc) and rejection counts.

    now defaults to the scan timestamp. min_hours_to_expiry is a code default, not a
    risk.toml key; a bad value rejects every market (invalid_min_hours_to_expiry).
    """
    now_dt = now or parse_utc(ts) or datetime.now(timezone.utc)
    passing: list[dict[str, Any]] = []
    first_reason: Counter = Counter()
    any_reason: Counter = Counter()
    token_problems: Counter = Counter()
    scanned = 0
    for market in raw_markets:
        row = normalize(market, ts)
        if not (row["question"] or row["slug"] or row["id"]):
            continue
        scanned += 1
        if row["token_ids_problem"] and row["token_ids_problem"] not in VERSION_REASONS:
            token_problems[row["token_ids_problem"]] += 1
        reasons = rejection_reasons(row, limits, now_dt, min_hours_to_expiry)
        if reasons:
            first_reason[reasons[0]] += 1
            any_reason.update(reasons)
            continue
        row["days_to_expiry"] = days_to_expiry(row["end_date"], now_dt)
        passing.append(row)
    passing.sort(key=lambda item: item["volume_24h_usd"], reverse=True)
    return {
        "ts": ts,
        "scanned": scanned,
        "passed": len(passing),
        "rows": passing,
        "first_reason": dict(first_reason),
        "any_reason": dict(any_reason),
        "token_id_problems": dict(token_problems),
        "limits": dict(limits),
        "min_hours_to_expiry": min_hours_to_expiry,
    }


def visible_rows(raw_markets: list[dict], ts: str) -> list[dict[str, Any]]:
    rows = []
    for market in raw_markets:
        row = normalize(market, ts)
        if row["question"] or row["slug"] or row["id"]:
            rows.append(row)
    return rows


def persist_markets(
    rows: list[dict[str, Any]],
    markets_path: Path = MARKETS_PATH,
    bus_path: Path | None = None,
    bus_limit: int | None = None,
) -> int:
    """Append every row to markets_path; emit a bus signal for the first bus_limit rows.

    bus_limit=None keeps the old one-signal-per-row behaviour (paper_session, 20 rows).
    """
    for index, row in enumerate(rows):
        append_jsonl(markets_path, row)
        if bus_limit is not None and index >= bus_limit:
            continue
        bus(
            "Scanner",
            "BookWatcher",
            "signal",
            row["id"],
            {
                "question": row["question"],
                "slug": row["slug"],
                "liquidity_usd": row["liquidity_usd"],
                "spread": row["spread"],
                "source": "gamma_public",
            },
            path=bus_path,
        )
    return len(rows)


def scan(
    *,
    raw_markets: list[dict] | None = None,
    getter: Callable[[str], Any] | None = None,
    risk_path: Path = RISK_PATH,
    limits: dict[str, float] | None = None,
    ts: str | None = None,
    max_markets: int = MAX_MARKETS,
    server_prefilter: bool = True,
    now: datetime | None = None,
    min_hours_to_expiry: float = MIN_HOURS_TO_EXPIRY,
) -> dict[str, Any]:
    """Fetch (or take) raw markets and filter them. No writes."""
    limits = limits if limits is not None else load_thresholds(risk_path)
    stamp = ts or utc_now()
    if raw_markets is None:
        raw_markets, meta = fetch_all_markets(
            getter,
            max_markets=max_markets,
            server_min_liquidity=limits["min_liquidity_usd"] if server_prefilter else None,
        )
    else:
        meta = {
            "pages": 0,
            "fetched": len(raw_markets),
            "stop_reason": "given",
            "cap_hit": False,
            "complete": True,
            "max_markets": None,
            "server_min_liquidity": None,
        }
    result = filter_markets(raw_markets, limits, stamp, now=now, min_hours_to_expiry=min_hours_to_expiry)
    result["fetch"] = meta
    return result


def main(
    *,
    getter: Callable[[str], Any] | None = None,
    markets_path: Path = MARKETS_PATH,
    bus_path: Path | None = None,
    max_markets: int = MAX_MARKETS,
    bus_top_n: int = BUS_TOP_N,
    ts: str | None = None,
    min_hours_to_expiry: float = MIN_HOURS_TO_EXPIRY,
) -> int:
    cfg = load_risk()
    result = scan(
        getter=getter,
        limits=thresholds_from(cfg),
        max_markets=max_markets,
        ts=ts,
        min_hours_to_expiry=min_hours_to_expiry,
    )
    rows = result["rows"]
    fetch = result["fetch"]
    mode = cfg.get("mode")
    live = cfg.get("live_enabled")
    print(
        f"scanned={result['scanned']} passed={result['passed']} "
        f"stop={fetch['stop_reason']} cap_hit={str(fetch['cap_hit']).lower()} "
        f"server_min_liquidity={fetch['server_min_liquidity']} "
        f"min_hours_to_expiry={result['min_hours_to_expiry']:g} mode={mode} "
        f"live_enabled={str(live).lower()} keys=none orders=none"
    )
    print("rejected_first=" + ", ".join(f"{k}:{v}" for k, v in sorted(result["first_reason"].items())))
    if result.get("token_id_problems"):
        # Not a rejection: these rows pass but carry no outcome IDs, so no book is fetched.
        print("token_id_problems=" + ", ".join(f"{k}:{v}" for k, v in sorted(result["token_id_problems"].items())))
    if not fetch["complete"]:
        print(f"scanner failed: pagination incomplete ({fetch['stop_reason']}); nothing written", file=sys.stderr)
        if fetch.get("error"):
            print(f"scanner network error: {fetch['error']}", file=sys.stderr)
        return EXIT_INCOMPLETE
    if result["scanned"] < 5:
        print("scanner failed: fewer than 5 public markets", file=sys.stderr)
        return EXIT_TOO_FEW_SCANNED
    if not rows:
        print("scanner failed: zero markets passed the filters", file=sys.stderr)
        return EXIT_NONE_PASSED
    persist_markets(rows, markets_path, bus_path, bus_limit=bus_top_n)
    for row in rows[:10]:
        print(
            f"- {row['slug']} liq={row['liquidity_usd']:.0f} vol24h={row['volume_24h_usd']:.0f} "
            f"bid={row['best_bid']} ask={row['best_ask']} days={row['days_to_expiry']:.1f}"
        )
    print(f"wrote={markets_path} rows={len(rows)} bus_signals={min(len(rows), bus_top_n)}")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
