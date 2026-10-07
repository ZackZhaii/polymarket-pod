"""Phase 0 paper ledger.

Simulated fills are written only after RiskGate.evaluate returns allow.
No CLOB authentication, no order POST, no withdraw, no size-up.
A single-leg fill is directional exposure, not risk-free arbitrage.
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
    RISK_PATH,
    append_jsonl,
    bus,
    load_risk,
    read_jsonl,
    utc_now,
)
from src.risk_gate import Decision, Intent, evaluate

DIRECTIONAL = "Single-leg fill is directional exposure, not risk-free arbitrage."


def load_state(market: str) -> dict[str, Any]:
    shares: dict[str, float] = {}
    daily_fees = 0.0
    today = utc_now()[:10]
    for row in read_jsonl(PAPER_PATH):
        if row.get("hypothetical") or row.get("counts_toward_risk") is False:
            continue
        name = str(row.get("market") or "")
        qty = float(row.get("shares") or 0.0)
        signed = qty if row.get("side") == "buy" else -qty
        shares[name] = shares.get(name, 0.0) + signed
        if str(row.get("ts") or "").startswith(today):
            daily_fees += float(row.get("fee_usd") or 0.0)
    return {
        "open_positions": sum(1 for qty in shares.values() if abs(qty) > 1e-9),
        "market_already_open": abs(shares.get(market, 0.0)) > 1e-9,
        "daily_loss_usd": daily_fees,
    }


def commit_if_allowed(intent: Intent, cfg: dict[str, Any]) -> dict[str, Any]:
    """Ask RiskGate. Append a paper fill only when the same call allows it."""
    state = load_state(intent.market)
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
    fill = {
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
        "fee_usd": round(decision.fee_usd, 8),
        "effective_price": round(decision.effective_price, 8),
        "notional_usd": round(decision.notional_usd, 8),
        "shares": round(decision.shares, 8),
        "spread": intent.spread,
        "liquidity_usd": intent.liquidity_usd,
        "hypothetical": intent.hypothetical,
        "counts_toward_risk": not intent.hypothetical,
        "risk_decision": "allow",
        "note": f"{DIRECTIONAL} {intent.note}".strip(),
    }
    append_jsonl(PAPER_PATH, fill)
    bus("Executor", "Sentinel", "fill", intent.market, fill)
    return {"allow": True, "fill": fill}


def _decision_payload(intent: Intent, decision: Decision) -> dict[str, Any]:
    return {
        "strategy": intent.strategy,
        "model_prob": intent.model_prob,
        "market_price": intent.market_price,
        "edge": round(decision.edge, 8),
        "edge_formula": decision.formula,
        "assumed_slippage": decision.assumed_slippage,
        "fee_rate": decision.fee_rate,
        "fee_usd": round(decision.fee_usd, 8),
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
    }
    defaults.update(kwargs)
    return Intent(**defaults)


def run_self_check(cfg: dict[str, Any]) -> dict[str, Any]:
    """Prove vetoes fire, and that one labeled hypothetical fill needs allow."""
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
    """Use a public book price with model_prob == price. Must veto. Not a forecast."""
    books = read_jsonl(BOOKS_PATH)
    markets = {str(row.get("id")): row for row in read_jsonl(MARKETS_PATH)}
    fallback = None
    chosen = None
    for book in reversed(books):
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
            note="Public ask used as both model and price. Must not fill. Not a forecast.",
        ),
        cfg,
    )
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
        print(("ALLOW" if result["allow"] else "VETO"), result.get("fill") or result.get("decision"))
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
