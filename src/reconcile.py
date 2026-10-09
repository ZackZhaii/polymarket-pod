"""Task AK: read-only Reconciler on Polymarket Data API v2. Standard library only.

Data API v1 is retired on 2026-10-24 (https://docs.polymarket.com/migrate/data-api-v1-to-v2), so
the reconciliation path reads only the three v2 routes it needs, all public and unauthenticated
(https://docs.polymarket.com/api-reference/data-api/overview; contract published as the v2
openapi.json next to the routes):

    GET /v2/status                                                              (freshness, first each round)
    GET /v2/positions?user=W&status=OPEN&filter_type=TOKENS&filter_amount=0.1   (replaces v1 /positions)
    GET /v2/trades?user=W&taker_only=false&start=1                              (replaces v1 /trades)

It compares them with the local paper ledger (src/ledger_store.py) and never writes the ledger,
never places, signs or cancels anything, and needs no key: the wallet address is public data passed
on the command line. live_enabled stays false; in Phase 0 the ledger is paper, so against a real
wallet this reports mismatches by design (no paper fill exists on chain). It is the M2 tool.

Fail closed. Anything the code cannot prove is a problem, and any problem is a FAIL (never a pass,
never a default that hides exposure):

* Freshness first (/v2/status, every round): ``data.age_seconds`` and ``data.serving.lag_seconds``
  must be integers >= 0 (``null`` = not measured = fail) and their sum at most DATA_API_MAX_LAG_S;
  ``ingestion.chain_id`` must be 137 and ``ingestion.network`` "polygon". Otherwise nothing else is read.
* Envelope (both routes): the body must be an object with a ``data`` list and a ``pagination``
  object; ``data: null`` (the documented "not servable" miss) is ``data_null``. ``has_more`` must be
  a bool and agree with ``next_cursor`` (a non-empty string iff ``has_more``); a repeated cursor is
  ``cursor_loop``; hitting MAX_PAGES with a cursor left is ``incomplete_walk``.
* Position rows: ``proxy_wallet`` (must be the requested wallet), ``token_id`` (decimal digits),
  ``condition_id`` (0x + 64 hex), ``current_size`` (finite JSON number >= 0; v2 says a missing or
  null number means unavailable, never zero) and ``status`` (OPEN or REDEEMABLE on an OPEN request).
  The documented row identities must hold within DATA_API_IDENTITY_TOL_USDC, so ``current_price``,
  ``current_value``, ``entry_cost_usdc``, ``entry_fees_usdc``, ``total_cost_usdc``, ``realized_pnl``,
  ``unrealized_pnl`` and ``total_pnl`` must be finite numbers too:
  current_value = current_size * current_price, unrealized_pnl = current_value - entry_cost_usdc,
  total_pnl = realized_pnl + unrealized_pnl, total_cost_usdc = entry_cost_usdc + entry_fees_usdc.
  A token served twice is ``duplicate_remote_token``.
* Trade rows: ``proxy_wallet``, ``side`` (BUY/SELL), ``token_id``, ``condition_id``, ``size``
  (finite > 0), ``price`` (finite, 0..1), ``timestamp`` (int > 0) and ``transaction_hash``
  (0x + 64 hex). A string where a number is due, a bool, NaN or inf is malformed, not coerced.
  Dedupe key transaction_hash + token_id + side: an exact repeat of a row is counted once; rows
  sharing the key but differing (one transaction filling several orders, seen live) are separate
  fills and all count. Either way a wrong guess makes the ledger comparison fail, never pass.
* Ledger: any ledger_store integrity problem, a counted fill without a token_id, a fill without
  finite shares, a token booked under two markets, or a short (negative) leg.
* Comparison, per token: ledger open shares vs ``current_size`` and ledger buy / sell shares vs the
  wallet's BUY / SELL trade sizes, each within RECONCILE_SHARES_TOL. A position the API cannot serve
  (below its DATA_API_POSITIONS_DUST_SHARES floor) or a fill below DATA_API_TRADES_MIN_SHARES cannot
  be verified and is a problem too.

Ambiguities in the docs, resolved fail-closed (see docs/RECONCILE.md): whether filter_amount=0 lifts
the positions dust floor (we send the documented default and treat sub-floor legs as unverifiable);
v2 trades are fills only, so redeems, merges, splits and transfers show up as mismatches.

CLI: ``python -m src.reconcile --user 0x<40 hex> [--ledger PATH] [--bus PATH] [--json]``
Exit codes: 0 reconciled; 1 mismatch (data valid, numbers disagree); 2 usage; 3 Data API unusable
(network/HTTP error, malformed or missing field, incomplete walk); 4 ledger invalid. Every non-zero
exit except 2 writes one ``halt`` bus message from Reconciler (``exchange_action: none``).
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode

from src import ledger_store
from src.podlib import (BUS_PATH, DATA_API_POSITIONS_PREFIX, DATA_API_STATUS_URL, DATA_API_TRADES_PREFIX,
                        PAPER_PATH, bus, public_get)
from src.thresholds import (DATA_API_IDENTITY_TOL_USDC, DATA_API_MAX_LAG_S, DATA_API_POSITIONS_DUST_SHARES,
                            DATA_API_TRADES_MIN_SHARES, LEDGER_OPEN_EPS, RECONCILE_SHARES_TOL)

# Operational: how much is read, not what passes. Doc maximum page size is 1000.
PAGE_LIMIT = 500
MAX_PAGES = 200

EXIT_OK, EXIT_MISMATCH, EXIT_USAGE, EXIT_REMOTE, EXIT_LEDGER = 0, 1, 2, 3, 4

ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
CONDITION_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
TX_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
TOKEN_RE = re.compile(r"^[0-9]{1,100}$")
OPEN_STATUSES = frozenset({"OPEN", "REDEEMABLE"})
SIDES = frozenset({"BUY", "SELL"})
POLYGON_CHAIN = ("polygon", 137)
IDENTITY_FIELDS = ("current_price", "current_value", "entry_cost_usdc", "entry_fees_usdc", "total_cost_usdc",
                   "realized_pnl", "unrealized_pnl", "total_pnl")

Getter = Callable[[str], Any]


class RemoteError(RuntimeError):
    """The Data API answer cannot be used (fetch failed, malformed, incomplete). Fail closed."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = list(problems)
        super().__init__("data api v2: " + ", ".join(self.problems))


@dataclass
class Report:
    user: str
    exit_code: int = EXIT_OK
    problems: list[str] = field(default_factory=list)
    positions_remote: int = 0
    trades_remote: int = 0
    tokens_checked: int = 0
    trades_deduped: int = 0
    lag_s: int | None = None
    pages: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.exit_code == EXIT_OK and not self.problems

    def as_dict(self) -> dict[str, Any]:
        return {"user": self.user, "ok": self.ok, "exit_code": self.exit_code, "problems": self.problems,
                "positions_remote": self.positions_remote, "trades_remote": self.trades_remote,
                "tokens_checked": self.tokens_checked, "trades_deduped": self.trades_deduped,
                "lag_s": self.lag_s, "pages": self.pages, "live_enabled": False,
                "exchange_action": "none"}


# ---------------------------------------------------------------- strict field readers

def _number(value: Any) -> float | None:
    """A finite JSON number. bool, str, None, NaN and inf are not numbers here (no coercion)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _text(value: Any, pattern: re.Pattern[str]) -> str | None:
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


def positions_url(user: str, cursor: str | None = None) -> str:
    params: dict[str, Any] = {"user": user, "status": "OPEN", "filter_type": "TOKENS",
                              "filter_amount": DATA_API_POSITIONS_DUST_SHARES}
    params.update({"cursor": cursor} if cursor else {"limit": PAGE_LIMIT})
    return DATA_API_POSITIONS_PREFIX + urlencode(params)


def trades_url(user: str, cursor: str | None = None) -> str:
    # Feed rule (v2 docs): re-send identical filters on every page, the cursor carries only the anchor.
    params: dict[str, Any] = {"user": user, "taker_only": "false", "start": 1}
    params.update({"cursor": cursor} if cursor else {"limit": PAGE_LIMIT})
    return DATA_API_TRADES_PREFIX + urlencode(params)


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def check_status(getter: Getter) -> int:
    """Read /v2/status; return the total lag in seconds. Raises RemoteError if stale or unprovable."""
    try:
        body = getter(DATA_API_STATUS_URL)
    except Exception as exc:  # noqa: BLE001
        raise RemoteError([f"status_fetch_failed:{type(exc).__name__}"]) from exc
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        raise RemoteError(["status_missing_data"])
    serving = data.get("serving")
    ingestion = data.get("ingestion")
    age = _count(data.get("age_seconds"))
    lag = _count(serving.get("lag_seconds")) if isinstance(serving, dict) else None
    problems = []
    if age is None:
        problems.append("status_invalid_age_seconds")
    if lag is None:
        problems.append("status_invalid_lag_seconds")
    if not isinstance(ingestion, dict) or (ingestion.get("network"), ingestion.get("chain_id")) != POLYGON_CHAIN \
            or isinstance(ingestion.get("chain_id"), bool):
        problems.append("status_wrong_chain")
    if problems:
        raise RemoteError(problems)
    if age + lag > DATA_API_MAX_LAG_S:
        raise RemoteError([f"status_lag_too_large:{age + lag}"])
    return age + lag


def walk(route: str, user: str, getter: Getter) -> tuple[list[Any], int]:
    """Follow next_cursor until null. Returns (rows, pages). Raises RemoteError on any doubt."""
    build = positions_url if route == "positions" else trades_url
    rows: list[Any] = []
    seen: set[str] = set()
    cursor: str | None = None
    for page in range(1, MAX_PAGES + 1):
        url = build(user, cursor)
        try:
            body = getter(url)
        except Exception as exc:  # noqa: BLE001 - network, HTTP 4xx/5xx, JSON: all unusable
            raise RemoteError([f"{route}_fetch_failed:{type(exc).__name__}"]) from exc
        if not isinstance(body, dict):
            raise RemoteError([f"{route}_envelope_not_object"])
        if "data" not in body:
            raise RemoteError([f"{route}_missing_data"])
        if body["data"] is None:
            raise RemoteError([f"{route}_data_null"])
        if not isinstance(body["data"], list):
            raise RemoteError([f"{route}_data_not_list"])
        pagination = body.get("pagination")
        if not isinstance(pagination, dict):
            raise RemoteError([f"{route}_missing_pagination"])
        has_more = pagination.get("has_more")
        if not isinstance(has_more, bool):
            raise RemoteError([f"{route}_invalid_has_more"])
        if "next_cursor" not in pagination:
            raise RemoteError([f"{route}_missing_next_cursor"])
        nxt = pagination["next_cursor"]
        if nxt is not None and not (isinstance(nxt, str) and nxt):
            raise RemoteError([f"{route}_invalid_next_cursor"])
        if has_more != (nxt is not None):
            raise RemoteError([f"{route}_pagination_inconsistent"])
        rows.extend(body["data"])
        if nxt is None:
            return rows, page
        if nxt in seen:
            raise RemoteError([f"{route}_cursor_loop"])
        seen.add(nxt)
        cursor = nxt
    raise RemoteError([f"{route}_incomplete_walk:{MAX_PAGES}"])


def parse_positions(rows: list[Any], user: str) -> tuple[dict[str, float], list[str]]:
    """token_id -> current_size, plus every row problem (any problem makes the API answer unusable)."""
    sizes: dict[str, float] = {}
    problems: list[str] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            problems.append(f"position_not_object:{index}")
            continue
        token = _text(row.get("token_id"), TOKEN_RE)
        label = token or f"#{index}"
        if not isinstance(row.get("proxy_wallet"), str) or not ADDRESS_RE.fullmatch(row["proxy_wallet"]):
            problems.append(f"position_invalid_proxy_wallet:{label}")
        elif row["proxy_wallet"].lower() != user.lower():
            problems.append(f"position_foreign_wallet:{label}")
        if token is None:
            problems.append(f"position_invalid_token_id:{index}")
        if _text(row.get("condition_id"), CONDITION_RE) is None:
            problems.append(f"position_invalid_condition_id:{label}")
        size = _number(row.get("current_size"))
        if size is None or size < 0:
            problems.append(f"position_invalid_current_size:{label}")
        if row.get("status") not in OPEN_STATUSES:
            problems.append(f"position_invalid_status:{label}")
        values = {name: _number(row.get(name)) for name in IDENTITY_FIELDS}
        missing = [name for name, value in values.items() if value is None]
        problems.extend(f"position_invalid_{name}:{label}" for name in missing)
        if not missing and size is not None and size >= 0:
            v = values
            identities = (
                ("current_value", v["current_value"], size * v["current_price"]),
                ("unrealized_pnl", v["unrealized_pnl"], v["current_value"] - v["entry_cost_usdc"]),
                ("total_pnl", v["total_pnl"], v["realized_pnl"] + v["unrealized_pnl"]),
                ("total_cost_usdc", v["total_cost_usdc"], v["entry_cost_usdc"] + v["entry_fees_usdc"]),
            )
            for name, served, derived in identities:
                if abs(served - derived) > DATA_API_IDENTITY_TOL_USDC:
                    problems.append(f"position_identity_broken_{name}:{label}")
        if token is not None and size is not None and size >= 0:
            if token in sizes:
                problems.append(f"duplicate_remote_token:{token}")
            else:
                sizes[token] = size
    return sizes, problems


def parse_trades(rows: list[Any], user: str) -> tuple[dict[str, dict[str, float]], list[str], int]:
    """token_id -> {"BUY": shares, "SELL": shares}, every row problem, and exact repeats ignored."""
    flows: dict[str, dict[str, float]] = {}
    problems: list[str] = []
    seen: dict[tuple[str, str, str], list[str]] = {}
    deduped = 0
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            problems.append(f"trade_not_object:{index}")
            continue
        label = f"#{index}"
        bad = False
        wallet = row.get("proxy_wallet")
        if not isinstance(wallet, str) or not ADDRESS_RE.fullmatch(wallet):
            problems.append(f"trade_invalid_proxy_wallet:{label}")
            bad = True
        elif wallet.lower() != user.lower():
            problems.append(f"trade_foreign_wallet:{label}")
            bad = True
        side = row.get("side")
        if side not in SIDES:
            problems.append(f"trade_invalid_side:{label}")
            bad = True
        token = _text(row.get("token_id"), TOKEN_RE)
        if token is None:
            problems.append(f"trade_invalid_token_id:{label}")
            bad = True
        if _text(row.get("condition_id"), CONDITION_RE) is None:
            problems.append(f"trade_invalid_condition_id:{label}")
            bad = True
        size = _number(row.get("size"))
        if size is None or size <= 0:
            problems.append(f"trade_invalid_size:{label}")
            bad = True
        price = _number(row.get("price"))
        if price is None or not 0.0 <= price <= 1.0:
            problems.append(f"trade_invalid_price:{label}")
            bad = True
        stamp = row.get("timestamp")
        if isinstance(stamp, bool) or not isinstance(stamp, int) or stamp <= 0:
            problems.append(f"trade_invalid_timestamp:{label}")
            bad = True
        if _text(row.get("transaction_hash"), TX_RE) is None:
            problems.append(f"trade_invalid_transaction_hash:{label}")
            bad = True
        if not bad:
            key = (row["transaction_hash"].lower(), token, side)
            text = json.dumps(row, sort_keys=True)
            if text in seen.setdefault(key, []):
                deduped += 1  # exact repeat (e.g. a row served twice across pages): count once
                continue
            seen[key].append(text)
            leg = flows.setdefault(token, {"BUY": 0.0, "SELL": 0.0})
            leg[side] += size
    return flows, problems, deduped


# ---------------------------------------------------------------- ledger side

@dataclass
class LedgerView:
    shares: dict[str, float] = field(default_factory=dict)  # token -> open shares (counted rows)
    flows: dict[str, dict[str, float]] = field(default_factory=dict)  # token -> BUY/SELL shares
    problems: list[str] = field(default_factory=list)


def ledger_view(path: Path) -> LedgerView:
    view = LedgerView()
    _, read = ledger_store.read_ledger(path)
    problems = ledger_store.verify(read)
    if problems:
        view.problems = [f"ledger_invalid:{p}" for p in problems]
        return view
    _, legs, _ = ledger_store.replay(read.rows, read.ids)
    market_of: dict[str, str] = {}
    for (market, token), leg in legs.items():
        if not token:
            if abs(leg["shares"]) > LEDGER_OPEN_EPS:
                view.problems.append(f"ledger_leg_without_token_id:{market}")
            continue
        if token in market_of and market_of[token] != market:
            view.problems.append(f"ledger_token_in_two_markets:{token}")
            continue
        market_of[token] = market
        view.shares[token] = leg["shares"]
    for row, fill_id in zip(read.rows, read.ids):
        if not ledger_store.counts(row):
            continue
        token = str(row.get("token_id") or "")
        if not token:
            view.problems.append(f"ledger_fill_without_token_id:{fill_id}")
            continue
        shares = _number(row.get("shares"))
        if shares is None or shares <= 0:
            view.problems.append(f"ledger_fill_invalid_shares:{fill_id}")
            continue
        if shares < DATA_API_TRADES_MIN_SHARES:
            view.problems.append(f"ledger_fill_below_data_api_trade_floor:{fill_id}")
        side = "BUY" if row.get("side") == "buy" else "SELL"
        leg = view.flows.setdefault(token, {"BUY": 0.0, "SELL": 0.0})
        leg[side] += shares
    return view


# ---------------------------------------------------------------- comparison

def compare(view: LedgerView, remote_sizes: dict[str, float],
            remote_flows: dict[str, dict[str, float]]) -> tuple[list[str], int]:
    """Mismatches between a valid ledger view and a valid remote view. Returns (problems, tokens)."""
    problems: list[str] = []
    tokens = sorted(set(view.shares) | set(remote_sizes) | set(view.flows) | set(remote_flows))
    for token in tokens:
        local = view.shares.get(token, 0.0)  # absent from the ledger = never booked = 0 shares
        if local < -LEDGER_OPEN_EPS:
            problems.append(f"ledger_short_leg:{token}")
            continue
        local_open = local > LEDGER_OPEN_EPS
        if token in remote_sizes:
            if abs(remote_sizes[token] - (local if local_open else 0.0)) > RECONCILE_SHARES_TOL:
                kind = "position_mismatch" if local_open else "unexpected_remote_position"
                problems.append(f"{kind}:{token}")
        elif local_open:
            if local < DATA_API_POSITIONS_DUST_SHARES:
                problems.append(f"position_below_data_api_floor:{token}")
            else:
                problems.append(f"missing_remote_position:{token}")
        local_flow = view.flows.get(token, {"BUY": 0.0, "SELL": 0.0})
        remote_flow = remote_flows.get(token, {"BUY": 0.0, "SELL": 0.0})
        for side in ("BUY", "SELL"):
            if abs(local_flow[side] - remote_flow[side]) > RECONCILE_SHARES_TOL:
                problems.append(f"trade_flow_mismatch:{token}:{side}")
    return problems, len(tokens)


def reconcile(user: str, ledger_path: Path, getter: Getter = public_get) -> Report:
    report = Report(user=user)
    if not ADDRESS_RE.fullmatch(user or ""):
        report.exit_code = EXIT_USAGE
        report.problems = ["invalid_user_address"]
        return report
    view = ledger_view(ledger_path)
    if view.problems:
        report.exit_code = EXIT_LEDGER
        report.problems = view.problems
        return report
    try:
        report.lag_s = check_status(getter)
        position_rows, report.pages["positions"] = walk("positions", user, getter)
        sizes, problems = parse_positions(position_rows, user)
        trade_rows, report.pages["trades"] = walk("trades", user, getter)
        flows, trade_problems, report.trades_deduped = parse_trades(trade_rows, user)
        problems += trade_problems
        if problems:
            raise RemoteError(problems)
    except RemoteError as exc:
        report.exit_code = EXIT_REMOTE
        report.problems = exc.problems
        return report
    report.positions_remote, report.trades_remote = len(position_rows), len(trade_rows)
    mismatches, report.tokens_checked = compare(view, sizes, flows)
    if mismatches:
        report.exit_code = EXIT_MISMATCH
        report.problems = mismatches
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.reconcile",
        description="Read-only reconciliation of the paper ledger against Polymarket Data API v2 "
                    "(/v2/status first, then /v2/positions, /v2/trades). No keys, no orders; fails closed.",
        epilog="Exit codes: 0 reconciled; 1 mismatch; 2 usage; 3 Data API unusable; 4 ledger invalid.",
    )
    parser.add_argument("--user", required=True, help="public proxy wallet address (0x + 40 hex)")
    parser.add_argument("--ledger", type=Path, default=PAPER_PATH, help="paper ledger (default logs/paper.jsonl)")
    parser.add_argument("--bus", type=Path, default=BUS_PATH, help="bus file for the halt message")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    return parser


def main(argv: list[str] | None = None, getter: Getter = public_get) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return EXIT_OK if exc.code == 0 else EXIT_USAGE
    report = reconcile(args.user, args.ledger, getter)
    if report.exit_code not in (EXIT_OK, EXIT_USAGE):
        bus("Reconciler", "Sentinel", "halt", "*",
            {"reason": "reconcile_failed", "exit_code": report.exit_code, "problems": report.problems[:50],
             "exchange_action": "none", "live_enabled": False}, path=args.bus)
    if args.json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, sort_keys=True))
    else:
        verdict = "PASS" if report.ok else "FAIL"
        print(f"reconcile {verdict} exit={report.exit_code} tokens={report.tokens_checked} "
              f"positions={report.positions_remote} trades={report.trades_remote}")
        for problem in report.problems[:50]:
            print(f"  {problem}")
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
