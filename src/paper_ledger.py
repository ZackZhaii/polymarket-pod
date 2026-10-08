"""Phase 0 paper ledger.

Simulated fills are written only after RiskGate.evaluate returns allow.
No CLOB authentication, no order POST, no withdraw, no size-up.
A single-leg fill is directional exposure, not risk-free arbitrage.
Positions are netted per leg (market + token_id); the tokens of an arb pair never
offset each other (backlog item 5, Task P).
Task AF (backlog #20): the ledger is read strictly, fills carry ids and are deduped, writes are
atomic and cash / positions are conservation-checked on every load and write (src/ledger_store.py).
Any integrity problem is an invalid risk state and halts booking.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.podlib import (  # noqa: E402
    BOOKS_PATH,
    MARKETS_PATH,
    PAPER_EQUITY_USD,
    PAPER_PATH,
    QUOTES_PATH,
    RISK_PATH,
    bus,
    load_risk,
    read_jsonl,
    utc_now,
)
from src.book_staleness import latest_by_time, parse_book_ts  # noqa: E402
from src.fees import booked_fee_usd, ledger_fee  # noqa: E402 - Task AE: one fee module
from src import ledger_store  # noqa: E402 - Task AF: ledger integrity
from src.ledger_store import (  # noqa: E402,F401 - Task AF: moved here, re-exported unchanged
    LEDGER_NUMBER_FIELDS,
    LedgerIntegrityError,
    leg_key,
    non_finite_ledger_rows,
)
from src.ledger_store import counts as _counts  # noqa: E402 - Task AF: moved, same rule
from src.risk_gate import Decision, Intent, evaluate  # noqa: E402

DIRECTIONAL = "Single-leg fill is directional exposure, not risk-free arbitrage."


from src.thresholds import LEDGER_OPEN_EPS as OPEN_EPS  # noqa: E402 - Task AC


def leg_positions(rows: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, float]]:
    """Net shares per leg, plus exposure at the leg's average fill price.

    Only a buy and a sell of the same token offset each other (closing). The two
    tokens of an arb pair are separate legs and never offset each other, so a
    single filled leg is a full directional position (backlog item 5).
    Exposure is abs(net shares) * (gross notional / gross shares) over that leg's
    rows; a row without notional_usd is valued at 1 USD per share, the most a
    binary token can pay out.
    """
    legs: dict[tuple[str, str], dict[str, float]] = {}
    for row in rows:
        if not _counts(row):
            continue
        qty = float(row.get("shares") or 0.0)
        notional = row.get("notional_usd")
        gross_usd = abs(qty) * 1.0 if notional is None else abs(float(notional))
        leg = legs.setdefault(leg_key(row.get("market"), row.get("token_id")),
                              {"shares": 0.0, "gross_shares": 0.0, "gross_usd": 0.0})
        leg["shares"] += qty if row.get("side") == "buy" else -qty
        leg["gross_shares"] += abs(qty)
        leg["gross_usd"] += gross_usd
    for leg in legs.values():
        open_qty = abs(leg["shares"])
        if open_qty <= OPEN_EPS or leg["gross_shares"] <= 0:
            leg["exposure_usd"] = 0.0
        else:
            leg["exposure_usd"] = open_qty * leg["gross_usd"] / leg["gross_shares"]
    return legs


def load_state(market: str, token_id: str = "") -> dict[str, Any]:
    """Risk state for an intent on one leg (market + token_id).

    open_positions counts open legs, not markets. leg_already_open is True only
    when this exact token already has an open position; another leg of the same
    market does not count. exposure_usd is the gross sum over open legs.
    recent_fills (every ledger row) and own_quotes (every paper quote row) are the
    history RiskGate's structured wash / self-trade check reads (Task S).
    Task AF: rows come from the strict ledger reader, deduped by fill_id. Any integrity problem
    (parse, ids, Task U numbers, conservation) is listed in ledger_problems and gives the same
    invalid state as Task U, so RiskGate vetoes invalid_risk_state and nothing more is booked.
    """
    _, ledger = ledger_store.read_ledger(PAPER_PATH)
    rows = ledger.rows
    bad_rows = non_finite_ledger_rows(rows)
    problems = ledger_store.verify(ledger)
    if bad_rows or problems:
        # Fail closed (Task U / AF): RiskGate vetoes invalid_risk_state; nothing is netted.
        return {
            "open_positions": None,
            "leg_already_open": False,
            "exposure_usd": None,
            "leg_exposure_usd": None,
            "daily_loss_usd": None,
            "non_finite_ledger_rows": bad_rows,
            "ledger_problems": problems,
            "duplicate_fill_rows": ledger.duplicates_ignored,
            "recent_fills": rows,
            "own_quotes": read_jsonl(QUOTES_PATH),
        }
    legs = leg_positions(rows)
    daily_fees = 0.0
    today = utc_now()[:10]
    for row in rows:
        if _counts(row) and str(row.get("ts") or "").startswith(today):
            daily_fees += float(row.get("fee_usd") or 0.0)
    own = legs.get(leg_key(market, token_id))
    return {
        "open_positions": sum(1 for leg in legs.values() if abs(leg["shares"]) > OPEN_EPS),
        "leg_already_open": own is not None and abs(own["shares"]) > OPEN_EPS,
        "exposure_usd": sum(leg["exposure_usd"] for leg in legs.values()),
        "leg_exposure_usd": 0.0 if own is None else own["exposure_usd"],
        "daily_loss_usd": daily_fees,
        "ledger_problems": [],
        "duplicate_fill_rows": ledger.duplicates_ignored,
        "recent_fills": rows,
        "own_quotes": read_jsonl(QUOTES_PATH),
    }


def commit_if_allowed(intent: Intent, cfg: dict[str, Any]) -> dict[str, Any]:
    """Ask RiskGate. Append a paper fill only when the same call allows it.

    Task AF: the fill gets a fill_id and the post-fill cash / leg state, and is written atomically
    by ledger_store.book after the whole ledger is re-verified. An integrity problem halts (a halt
    bus message, allow False, nothing booked); a failed write leaves the old ledger intact.
    """
    state = load_state(intent.market, intent.token_id)
    decision = evaluate(intent, cfg, state)
    summary = _decision_payload(intent, decision)
    if not decision.allow:
        bus("RiskGate", "Executor", "veto", intent.market, summary)
        return {"allow": False, "decision": summary}

    summary["decision"] = "allow"
    bus("RiskGate", "Executor", "signal", intent.market, summary)
    # Re-check the in-memory decision. A bus line is not enough on its own.
    if decision.allow is not True:
        raise RuntimeError("refusing fill without a fresh RiskGate allow")
    effective_price = round(decision.effective_price, 8)
    notional_usd = round(decision.notional_usd, 8)
    shares = round(decision.shares, 8)
    # Task AF: the booked fee is also never below ledger_fee recomputed from the values the row
    # stores (they are rounded), so the conservation check can recompute it exactly. Higher wins.
    stored_fee = ledger_fee(intent.fee_rate, intent.market_price, effective_price, notional_usd, shares)
    fee_usd = booked_fee_usd(decision.fee_usd)
    if stored_fee is not None:
        fee_usd = max(fee_usd, booked_fee_usd(stored_fee))
    fill = {
        "fill_id": ledger_store.new_fill_id(),
        "ts": utc_now(),
        "strategy": intent.strategy,
        "market": intent.market,
        "token_id": intent.token_id,
        "side": intent.side,
        "model_prob": intent.model_prob,
        "market_price": intent.market_price,
        "edge": round(decision.edge, 8),
        "edge_formula": decision.formula,
        "assumed_slippage": intent.assumed_slippage,
        "fee_rate": intent.fee_rate,
        "fee_usd": fee_usd,
        "effective_price": effective_price,
        "notional_usd": notional_usd,
        "shares": shares,
        "spread": intent.spread,
        "liquidity_usd": intent.liquidity_usd,
        "book_ts": intent.book_ts,
        "hypothetical": intent.hypothetical,
        "counts_toward_risk": not intent.hypothetical,
        "risk_decision": "allow",
        "note": f"{DIRECTIONAL} {intent.note}".strip(),
    }
    try:
        fill = ledger_store.book(PAPER_PATH, fill)
    except LedgerIntegrityError as exc:
        return _halt(intent, summary, "ledger_integrity", exc.problems)
    except OSError as exc:
        return _halt(intent, summary, "ledger_write_failed", [f"{type(exc).__name__}: {exc}"])
    bus("Executor", "Sentinel", "fill", intent.market, fill)
    return {"allow": True, "fill": fill}


def _halt(intent: Intent, summary: dict[str, Any], reason: str, problems: list[str]) -> dict[str, Any]:
    """Task AF: no fill. Report a halt (paper only, no exchange action) and return allow False."""
    payload = {"reason": reason, "ledger_problems": problems, "exchange_action": "none",
               "strategy": intent.strategy, "token_id": intent.token_id}
    bus("Executor", "Sentinel", "halt", intent.market, payload)
    decision = dict(summary, decision="halt")
    return {"allow": False, "halted": True, "halt_reason": reason, "ledger_problems": problems,
            "decision": decision}


def _decision_payload(intent: Intent, decision: Decision) -> dict[str, Any]:
    return {
        "strategy": intent.strategy,
        "model_prob": intent.model_prob,
        "market_price": intent.market_price,
        "edge": round(decision.edge, 8),
        "edge_formula": decision.formula,
        "assumed_slippage": decision.assumed_slippage,
        "fee_rate": decision.fee_rate,
        "fee_usd": booked_fee_usd(decision.fee_usd),
        "reasons": decision.reasons,
        "hypothetical": intent.hypothetical,
        "risk_decision": "allow" if decision.allow else "veto",
        "note": DIRECTIONAL,
    }


def _intent(**kwargs: Any) -> Intent:
    defaults: dict[str, Any] = {
        "strategy": "phase0-gate-test",
        "market": "phase0-self-check",
        "side": "buy",
        "model_prob": 0.5,
        "market_price": 0.5,
        "assumed_slippage": 0.005,
        "fee_rate": 0.0,
        "liquidity_usd": 20000.0,
        "spread": 0.01,
        "token_id": "",
        "hypothetical": True,
        "note": "Gate test only. Not a forecast and not a claim of edge.",
        # Synthetic gate-test touch, stamped now so only the check under test can veto.
        "book_ts": utc_now(),
    }
    defaults.update(kwargs)
    return Intent(**defaults)


def run_self_check(cfg: dict[str, Any]) -> dict[str, Any]:
    """Prove vetoes fire, and that one labeled hypothetical fill needs allow.

    The hypothetical allow is checked against the real fills' open positions and daily
    loss like any other intent (Task M), so a full book vetoes it.
    """
    before = len(read_jsonl(PAPER_PATH))
    drifted = dict(cfg)
    drifted["live_enabled"] = True
    pin_result = commit_if_allowed(
        _intent(market="phase0-self-check:pin", note="In-memory pin drift. File not modified."),
        drifted,
    )
    no_edge = commit_if_allowed(
        _intent(
            market="phase0-self-check:no-edge",
            model_prob=0.50,
            market_price=0.50,
            note="Model probability equals the touch. Costs make edge non-positive.",
        ),
        cfg,
    )
    wide = commit_if_allowed(
        _intent(
            market="phase0-self-check:spread",
            model_prob=0.80,
            market_price=0.40,
            spread=0.20,
            note="Spread wider than max_spread.",
        ),
        cfg,
    )
    banned = commit_if_allowed(
        _intent(
            strategy="wash-probe",
            market="phase0-self-check:banned",
            model_prob=0.80,
            market_price=0.40,
            hypothetical=False,
            note="Banned behavior must not fill.",
        ),
        cfg,
    )
    allowed = commit_if_allowed(
        _intent(
            market="phase0-self-check:hypothetical-allow",
            model_prob=0.70,
            market_price=0.40,
            note="Hypothetical probability to prove the allow path. Not a forecast.",
        ),
        cfg,
    )
    after = len(read_jsonl(PAPER_PATH))
    return {
        "pin_blocked": not pin_result["allow"],
        "no_edge_blocked": not no_edge["allow"],
        "wide_spread_blocked": not wide["allow"],
        "banned_blocked": not banned["allow"],
        "hypothetical_allowed": allowed["allow"],
        "new_fills": after - before,
        "allowed": allowed,
    }


def realistic_no_edge(cfg: dict[str, Any]) -> dict[str, Any] | None:
    """Use a public book price with model_prob == price. Must veto. Not a forecast.

    Task T: candidates are the latest row per leg by the recorder's parsed ``ts`` (times, not
    strings), newest first; rows with a missing or unparseable ``ts`` are skipped and returned in
    ``skipped_rows``. Book age is the exchange's ``book_ts`` on the row, never ``ts``; a row
    without one reaches RiskGate as book_ts=None and vetoes missing_book_ts (fail closed).
    """
    books = read_jsonl(BOOKS_PATH)
    markets = {str(row.get("id")): row for row in read_jsonl(MARKETS_PATH)}
    latest, skipped = latest_by_time(
        books, lambda row: (str(row.get("market_id") or ""), str(row.get("token_id") or ""))
    )
    ordered = sorted(
        latest.values(), key=lambda row: parse_book_ts(row.get("ts")), reverse=True  # type: ignore[arg-type,return-value]
    )
    fallback = None
    chosen = None
    for book in ordered:
        ask = book.get("best_ask")
        spread = book.get("spread")
        if not isinstance(ask, (int, float)) or not isinstance(spread, (int, float)):
            continue
        if not 0.0 < float(ask) < 1.0:
            continue
        market = markets.get(str(book.get("market_id")))
        liquidity = None if market is None else market.get("liquidity_usd")
        if not isinstance(liquidity, (int, float)):
            continue
        candidate = (book, float(liquidity))
        if fallback is None:
            fallback = candidate
        # Prefer a touch that stays inside (0, 1) after the default slippage.
        if 0.0 < float(ask) + 0.005 < 1.0:
            chosen = candidate
            break
    if chosen is None:
        chosen = fallback
    if chosen is None:
        return None
    book, liquidity = chosen
    result = commit_if_allowed(
        Intent(
            strategy="no-edge-check",
            market=str(book.get("market_id") or ""),
            side="buy",
            model_prob=float(book["best_ask"]),
            market_price=float(book["best_ask"]),
            assumed_slippage=0.005,
            fee_rate=0.0,
            liquidity_usd=float(liquidity),
            spread=float(book["spread"]),
            token_id=str(book.get("token_id") or ""),
            hypothetical=False,
            # The exchange's own book time (Task R/T), never the recorder's ts.
            book_ts=book.get("book_ts"),
            note="Public ask used as both model and price. Must not fill. Not a forecast.",
        ),
        cfg,
    )
    result["skipped_rows"] = skipped
    return result


def run_cli_intent(args: argparse.Namespace, cfg: dict[str, Any]) -> dict[str, Any]:
    intent = Intent(
        strategy=args.strategy,
        market=args.market,
        side=args.side,
        model_prob=args.model_prob,
        market_price=args.market_price,
        assumed_slippage=args.slippage,
        fee_rate=args.fee_rate,
        liquidity_usd=args.liquidity,
        spread=args.spread,
        token_id=args.token_id or "",
        hypothetical=False,
        book_ts=args.book_ts,
        note=args.note or "Operator paper intent. Not a promise of profit.",
    )
    return commit_if_allowed(intent, cfg)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Phase 0 paper ledger. RiskGate required. No live orders.")
    parser.add_argument("--strategy", default="")
    parser.add_argument("--market", default="")
    parser.add_argument("--side", choices=("buy", "sell"), default="buy")
    parser.add_argument("--model-prob", type=float, default=None)
    parser.add_argument("--market-price", type=float, default=None)
    parser.add_argument("--slippage", type=float, default=0.005)
    parser.add_argument("--fee-rate", type=float, default=0.0)
    parser.add_argument("--liquidity", type=float, default=None)
    parser.add_argument("--spread", type=float, default=None)
    parser.add_argument("--token-id", default="")
    parser.add_argument(
        "--book-ts",
        default=None,
        help="UTC ISO time of the book snapshot, e.g. 2026-10-09T01:00:00Z. Missing or older than 120 s vetoes.",
    )
    parser.add_argument("--note", default="")
    parser.add_argument(
        "--self-check",
        action="store_true",
        help="Run gate tests. This is the default when no operator intent is given.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    before_bytes = RISK_PATH.read_bytes()
    cfg = load_risk()
    live = cfg.get("live_enabled")
    print(
        f"paper_equity_usd={PAPER_EQUITY_USD} mode={cfg.get('mode')} "
        f"live_enabled={str(live).lower()} require_human_gate={str(cfg.get('require_human_gate')).lower()} "
        f"max_order_pct={cfg.get('max_order_pct')} max_daily_loss_pct={cfg.get('max_daily_loss_pct')} "
        "orders=none"
    )
    operator_intent = args.model_prob is not None or args.market_price is not None or bool(args.strategy)
    if operator_intent:
        missing = [
            name
            for name, value in (
                ("strategy", args.strategy),
                ("market", args.market),
                ("model_prob", args.model_prob),
                ("market_price", args.market_price),
                ("liquidity", args.liquidity),
                ("spread", args.spread),
            )
            if value is None or value == ""
        ]
        if missing:
            print("missing operator fields: " + ", ".join(missing), file=sys.stderr)
            return 2
        result = run_cli_intent(args, cfg)
        label = "ALLOW" if result["allow"] else "HALT" if result.get("halted") else "VETO"
        print(label, result.get("fill") or result.get("decision"))
        if result.get("halted"):
            print(f"paper ledger halted: {result['halt_reason']} {result['ledger_problems']}", file=sys.stderr)
            return 1
        if RISK_PATH.read_bytes() != before_bytes:
            print("risk.toml changed during paper run; refusing", file=sys.stderr)
            return 1
        return 0 if result["allow"] or result.get("decision", {}).get("reasons") else 1

    report = run_self_check(cfg)
    realistic = realistic_no_edge(cfg)
    print(
        "self_check "
        f"pin_blocked={report['pin_blocked']} "
        f"no_edge_blocked={report['no_edge_blocked']} "
        f"wide_spread_blocked={report['wide_spread_blocked']} "
        f"banned_blocked={report['banned_blocked']} "
        f"hypothetical_allowed={report['hypothetical_allowed']} "
        f"new_fills={report['new_fills']}"
    )
    if report["hypothetical_allowed"]:
        fill = report["allowed"]["fill"]
        print(
            "hypothetical_fill "
            f"strategy={fill['strategy']} model_prob={fill['model_prob']} "
            f"market_price={fill['market_price']} edge={fill['edge']} "
            f"assumed_slippage={fill['assumed_slippage']} fee_usd={fill['fee_usd']}"
        )
    if realistic is None:
        print("realistic_no_edge=skipped no complete public book yet")
    else:
        reasons = realistic.get("decision", {}).get("reasons")
        print(f"realistic_no_edge_blocked={not realistic['allow']} reasons={reasons}")
    unchanged = RISK_PATH.read_bytes() == before_bytes
    print(f"risk_toml_unchanged={str(unchanged).lower()} path={RISK_PATH}")
    ok = (
        report["pin_blocked"]
        and report["no_edge_blocked"]
        and report["wide_spread_blocked"]
        and report["banned_blocked"]
        and report["hypothetical_allowed"]
        and report["new_fills"] == 1
        and unchanged
        and (realistic is None or realistic["allow"] is False)
    )
    if not ok:
        print("paper ledger self-check failed", file=sys.stderr)
        return 1
    print(DIRECTIONAL)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
