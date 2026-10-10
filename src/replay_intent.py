"""Issue #80 step 1: one single-shot offline replay entry for the Intent builder (shadow only).

Reads one fixed JSON bundle (``schema_version`` 1) holding exactly one raw bus signal line, one
scanner market row, that market's book rows, the strategy, the caller's ``fee_rate`` and a
timezone-aware ``now``. It then runs the three existing adapters and ``build_order_intent`` once
(``src/intent_builder.py``: no algorithm is re-derived here) and prints one JSON object with either
the intent or the complete ordered rejection reasons.

One signal, one market, one run: no multi-signal bundle, no history selection, no loop. Nothing is
defaulted — a missing fee, book, tick size or minimum order size keeps the builder's own rejection
reasons. Read only: the current ``config/risk.toml`` is pinned-checked and then used in memory (this
entry writes no bus, no halt and no ledger, and it calls no RiskGate, executor or network). Kelly is
reported as the builder's shadow value only.

    python -m src.replay_intent --bundle path/to/bundle.json

Exit codes: docs/EXIT_CODES.md.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_CORRUPT_BUNDLE = 3
EXIT_UNSUPPORTED_BUNDLE = 4
EXIT_PIN_DRIFT = 6

BUNDLE_SCHEMA_VERSION = 1
BUNDLE_KEYS = frozenset({"schema_version", "signal", "market", "books", "strategy", "fee_rate", "now"})
# The intent fields the replay prints; the whole OrderIntent is not needed to read a shadow result.
INTENT_VIEW = ("intent_id", "idempotency_key", "signal_id", "strategy", "market_id", "token_id", "side",
               "limit_price", "exec_price", "shares", "size_usd", "size_pct", "edge", "edge_yes", "edge_no",
               "kelly_raw", "kelly_fraction", "kelly_frac", "kelly_size_pct", "fee_rate", "created_at",
               "expires_at")


class BundleError(Exception):
    """A bundle this single-shot entry refuses, carrying the exit code of the refusal."""

    def __init__(self, code: int, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


def _object(value: Any, name: str) -> dict:
    if not isinstance(value, dict):
        raise BundleError(EXIT_CORRUPT_BUNDLE, f"{name} must be a JSON object")
    return value


def parse_now(value: Any) -> datetime:
    """The bundle's ``now``: an ISO-8601 string that must carry a timezone (never the wall clock)."""
    if not isinstance(value, str) or not value.strip():
        raise BundleError(EXIT_CORRUPT_BUNDLE, "now must be a zoned ISO-8601 string")
    try:
        now = datetime.fromisoformat(value.strip().replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        raise BundleError(EXIT_CORRUPT_BUNDLE, "now must be a zoned ISO-8601 string") from None
    if now.tzinfo is None or now.utcoffset() is None:
        raise BundleError(EXIT_CORRUPT_BUNDLE, "now must carry a timezone offset")
    return now


def load_bundle(path: Path | str) -> dict:
    """Load and validate the fixed bundle: one signal, one market, its book rows, fee_rate and now."""
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise BundleError(EXIT_CORRUPT_BUNDLE, f"bundle unreadable: {type(exc).__name__}") from None
    try:
        bundle = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BundleError(EXIT_CORRUPT_BUNDLE, f"bundle is not valid JSON: {type(exc).__name__}") from None
    bundle = _object(bundle, "bundle")
    version = bundle.get("schema_version")
    if isinstance(version, bool) or version != BUNDLE_SCHEMA_VERSION:
        raise BundleError(EXIT_CORRUPT_BUNDLE, f"schema_version must be {BUNDLE_SCHEMA_VERSION}")
    extra = sorted(set(bundle) - BUNDLE_KEYS)
    if extra:
        raise BundleError(EXIT_UNSUPPORTED_BUNDLE,
                          "unsupported bundle keys (one signal, no history and no loop): " + ", ".join(extra))
    missing = sorted(BUNDLE_KEYS - set(bundle))
    if missing:
        raise BundleError(EXIT_CORRUPT_BUNDLE, "bundle is missing: " + ", ".join(missing))
    if isinstance(bundle["signal"], list):
        raise BundleError(EXIT_UNSUPPORTED_BUNDLE, "signal must be one bus row, not a list of signals")
    _object(bundle["signal"], "signal")
    market_row = _object(bundle["market"], "market")
    if not isinstance(bundle["books"], list):
        raise BundleError(EXIT_CORRUPT_BUNDLE, "books must be a list of book rows")
    market_id = str(market_row.get("id") or "")
    for index, row in enumerate(bundle["books"]):
        row = _object(row, f"books[{index}]")
        if str(row.get("market_id") or "") != market_id:
            raise BundleError(EXIT_UNSUPPORTED_BUNDLE,
                              f"books[{index}] is for market {row.get('market_id')!r}, not {market_id!r} "
                              "(one market per bundle)")
    if not isinstance(bundle["strategy"], str) or not bundle["strategy"].strip():
        raise BundleError(EXIT_CORRUPT_BUNDLE, "strategy must be a non-empty string")
    fee_rate = bundle["fee_rate"]
    if fee_rate is not None and (isinstance(fee_rate, bool) or not isinstance(fee_rate, (int, float))):
        raise BundleError(EXIT_CORRUPT_BUNDLE, "fee_rate must be a number or null (never a default)")
    parse_now(bundle["now"])
    return bundle


def replay(bundle: dict, cfg: dict) -> dict:
    """One ``build_order_intent`` on the bundle's single signal / market / books, nothing defaulted."""
    from src import intent_builder

    fee_rate = bundle["fee_rate"]
    signal = intent_builder.signal_from_bus(bundle["signal"])
    context = intent_builder.market_context_from_row(
        bundle["market"], fee_rate=None if fee_rate is None else float(fee_rate))
    books = {str(row.get("token_id") or ""): intent_builder.book_snapshot_from_row(row)
             for row in bundle["books"]}
    result = intent_builder.build_order_intent(signal, context, books, cfg, now=parse_now(bundle["now"]),
                                               strategy=bundle["strategy"], side="buy", held_shares=0.0)
    intent = result.intent
    return {"schema_version": BUNDLE_SCHEMA_VERSION, "shadow": True, "strategy": bundle["strategy"],
            "intent": None if intent is None else {name: getattr(intent, name) for name in INTENT_VIEW},
            "reasons": list(result.reasons), "first_reason": result.first_reason,
            # Kelly here is the shadow value build_order_intent records; it sizes nothing.
            "kelly_shadow": result.detail.get("kelly_frac"), "detail": result.detail}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.replay_intent",
                                     description="single-shot offline Intent-builder replay (shadow, read only)")
    parser.add_argument("--bundle", required=True,
                        help="the schema_version 1 JSON bundle (one signal, one market, its book rows)")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse would exit 2 behind the exit-code registry's back
        return EXIT_OK if exc.code == 0 else EXIT_USAGE

    from src import podlib
    # Task AS pin check, read only: this entry records no halt (it writes no bus and no ledger).
    from src.pins import risk_file_problems

    problems = risk_file_problems()
    if problems:
        print(f"replay_intent: refused: config/risk.toml off its pins ({', '.join(problems[:5])}); "
              "orders=none, nothing replayed", file=sys.stderr)
        return EXIT_PIN_DRIFT
    try:
        bundle = load_bundle(args.bundle)
    except BundleError as exc:
        print(f"replay_intent: {exc.reason}", file=sys.stderr)
        return exc.code
    print(json.dumps(replay(bundle, dict(podlib.load_risk())), default=str, sort_keys=True))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
