"""M3 calibration step 2 (Task AX): identity fields for the forecast -> fill -> resolution join.

Standard library only. No network, no orders, no risk decision. Nothing here changes what RiskGate
allows; it only names rows so a later, offline calibration report can join

    logs/forecasts.jsonl (forecast_id, condition_id, token_id)
      -> logs/paper.jsonl fills (forecast_id, decision_id, condition_id, token_id)
      -> data/resolutions.jsonl (condition_id, winning_token_id)

Rules (Poly反思 M3 design section 1.5, docs/M3_CALIBRATION.md "Step 2"):

- ``forecast_id`` is minted once, by the forecaster, when the forecast is sealed: ``fc_`` + the first
  32 hex chars of sha256 over the canonical JSON of the forecast's own content (source, ts, market,
  condition, outcome, token, probability and interval, model_version). Same content, same id.
- ``decision_id`` is derived, never minted: ``dc_`` + sha256 over (strategy, market_id, token_id,
  forecast_id). Every fill of one strategy on one token under one forecast shares it. If any input
  is missing there is no decision_id (None) - an id is never invented to make a row look joinable.
- A row that lacks an id the join needs is booked as before (no trading change) but carries
  ``calibration_problems`` (``missing_<field>`` ...). The calibration join must drop such rows and
  count them; it never repairs them.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any, Mapping

SCHEMA_VERSION = 1
ID_HEX_CHARS = 32  # format, not a threshold: 128 bits of sha256 (same width as intent_builder intent_id)
FORECAST_PREFIX = "fc_"
DECISION_PREFIX = "dc_"

# Identity fields every paper fill row carries (None when unknown; never guessed).
FILL_ID_FIELDS = ("decision_id", "forecast_id", "forecast_ts", "condition_id", "outcome_index", "outcome",
                  "category", "tags", "end_date", "model_version")
# The subset the forecast -> fill -> resolution join cannot do without (token_id is a fill column already).
FILL_JOIN_FIELDS = ("decision_id", "forecast_id", "forecast_ts", "condition_id", "outcome_index", "token_id",
                    "end_date", "model_version")

# Task BA: market context every paper fill row carries (None when it cannot be computed; never guessed).
# A None here is listed in the fill's stored calibration_problems (missing_<field> / invalid_<field>,
# see paper_ledger.market_fields); fill_problems (the join check) does not look at them.
FILL_MARKET_FIELDS = ("market_mid", "best_bid", "best_ask", "hours_to_expiry_at_entry", "kelly_fraction_used",
                      "kelly_frac_raw")
# Task BC: kelly_frac_raw is intent_builder's per-trade raw (full) Kelly fraction (OrderIntent.kelly_raw);
# kelly_fraction_used stays the shadow multiplier (risk.toml kelly_fraction). Both recorded only.

# Task BC: which path built a fill's intent, stamped on the fill at commit as fill_context. Only the
# intent_builder path carries the book / scanner context a calibration sample needs; the operator CLI
# (python -m src.paper_ledger --market ...) and the gate checks (self-check, realistic no-edge check) do
# not, and the calibration report drops them as no_book_context, apart from genuine missing-data reasons.
FILL_CONTEXT_INTENT_BUILDER = "intent_builder"
FILL_CONTEXT_OPERATOR_CLI = "operator_cli"
FILL_CONTEXT_GATE_CHECK = "gate_check"
FILL_CONTEXTS = (FILL_CONTEXT_INTENT_BUILDER, FILL_CONTEXT_OPERATOR_CLI, FILL_CONTEXT_GATE_CHECK)
NO_BOOK_CONTEXTS = frozenset({FILL_CONTEXT_OPERATOR_CLI, FILL_CONTEXT_GATE_CHECK})

# logs/forecasts.jsonl: one row per sealed forecast, these keys in this order (no extras).
FORECAST_ROW_FIELDS = ("schema_version", "forecast_id", "ts", "source", "strategy", "market_id", "condition_id",
                       "outcome_index", "outcome", "token_id", "model_prob", "p_low", "p_high", "category", "tags",
                       "end_date", "model_version", "note", "sealed_before_book_read", "calibration_problems")
FORECAST_JOIN_FIELDS = ("forecast_id", "ts", "market_id", "condition_id", "outcome_index", "token_id",
                        "model_prob", "end_date", "model_version")


class ForecastLogError(RuntimeError):
    """logs/forecasts.jsonl cannot take a complete row (torn tail, unreadable, or the write failed)."""


def _sha(parts: object) -> str:
    text = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:ID_HEX_CHARS]


def present(value: object) -> bool:
    """A usable id value: a non-blank string, or an int that is not a bool."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, str) and value.strip() != ""


def make_forecast_id(*, source: str, ts: str, market_id: str, condition_id: str | None,
                     outcome_index: int | None, token_id: str | None, model_prob: float,
                     p_low: float | None, p_high: float | None, model_version: str) -> str:
    """Deterministic id of one sealed forecast (its content, including its own ts)."""
    return FORECAST_PREFIX + _sha({"source": source, "ts": ts, "market_id": market_id, "condition_id": condition_id,
                                   "outcome_index": outcome_index, "token_id": token_id, "model_prob": model_prob,
                                   "p_low": p_low, "p_high": p_high, "model_version": model_version})


def make_decision_id(strategy: object, market_id: object, token_id: object, forecast_id: object) -> str | None:
    """Shared by every fill of (strategy, market, token) under one forecast; None if any part is missing."""
    parts = [strategy, market_id, token_id, forecast_id]
    if not all(present(p) for p in parts):
        return None
    return DECISION_PREFIX + _sha([str(p) for p in parts])


def scanner_category(market: Mapping[str, Any]) -> str | None:
    """Gamma ``category`` as given (a non-blank string), else None. Never derived from tags."""
    value = market.get("category")
    return value.strip() if isinstance(value, str) and value.strip() else None


def scanner_tags(market: Mapping[str, Any]) -> list[str] | None:
    """Gamma ``tags`` (include_tag=true) as their slugs, in Gamma's order.

    None when Gamma sent no ``tags`` key or something that is not a list (unknown); [] when the list is
    empty. A tag without a usable slug falls back to its label; a tag with neither is skipped.
    """
    raw = market.get("tags")
    if not isinstance(raw, list):
        return None
    out: list[str] = []
    for tag in raw:
        if isinstance(tag, Mapping):
            value = tag.get("slug") if present(tag.get("slug")) else tag.get("label")
        else:
            value = tag
        if isinstance(value, str) and value.strip() and value.strip() not in out:
            out.append(value.strip())
    return out


def _valid_index(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def fill_problems(row: Mapping[str, Any]) -> list[str]:
    """Why this fill cannot join a forecast and a resolution ([] = joinable). Sorted, stable."""
    problems = [f"missing_{name}" for name in FILL_JOIN_FIELDS if name != "outcome_index" and not present(row.get(name))]
    index = row.get("outcome_index")
    if index is None:
        problems.append("missing_outcome_index")
    elif not _valid_index(index):
        problems.append("invalid_outcome_index")
    expected = make_decision_id(row.get("strategy"), row.get("market"), row.get("token_id"), row.get("forecast_id"))
    if present(row.get("decision_id")) and expected is not None and row.get("decision_id") != expected:
        problems.append("decision_id_mismatch")
    return sorted(set(problems))


def forecast_problems(row: Mapping[str, Any]) -> list[str]:
    """Why this forecast row cannot be joined to a resolution ([] = joinable)."""
    problems = [f"missing_{name}" for name in FORECAST_JOIN_FIELDS
                if name not in ("outcome_index", "model_prob") and not present(row.get(name))]
    index = row.get("outcome_index")
    if index is None:
        problems.append("missing_outcome_index")
    elif not _valid_index(index):
        problems.append("invalid_outcome_index")
    prob = row.get("model_prob")
    if isinstance(prob, bool) or not isinstance(prob, (int, float)) or not math.isfinite(prob) or not 0 < prob < 1:
        problems.append("invalid_model_prob")
    return sorted(set(problems))


def append_forecast(path: Path, row: Mapping[str, Any]) -> None:
    """Append one complete forecast row atomically (one O_APPEND write of the whole line, then fsync).

    Fails closed (ForecastLogError, nothing written): unknown or missing keys, a value JSON cannot
    encode exactly (nan/inf), an existing file whose last byte is not a newline (torn tail), or an
    OS error. A row with the same forecast_id is not written twice.
    """
    if tuple(row) != FORECAST_ROW_FIELDS:
        raise ForecastLogError("forecast row keys differ from FORECAST_ROW_FIELDS")
    try:
        line = json.dumps(dict(row), ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n"
    except (TypeError, ValueError) as exc:
        raise ForecastLogError(f"forecast row is not exact JSON: {exc}") from exc
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            data = path.read_bytes()
            if data and not data.endswith(b"\n"):
                raise ForecastLogError(f"{path.name} has a torn last line; refusing to append")
            needle = ('"forecast_id":' + json.dumps(row["forecast_id"])).encode("utf-8")
            if needle in data:
                return  # the same sealed forecast is already on file (idempotent)
        payload = line.encode("utf-8")
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)
        try:
            written = os.write(fd, payload)
            if written != len(payload):
                raise ForecastLogError(f"short write to {path.name} ({written} of {len(payload)} bytes)")
            os.fsync(fd)
        finally:
            os.close(fd)
    except ForecastLogError:
        raise
    except OSError as exc:
        raise ForecastLogError(f"{type(exc).__name__}: {exc}") from exc
