"""NewsForecaster stub (docs/ROSTER.md). Probability only. Never emits an order or a fill.

The bus name used to be "Forecaster"; podlib.canonical_role maps that legacy name here.

Task AX (M3 calibration step 2): every forecast is sealed first, as one row in logs/forecasts.jsonl
(src/calibration_ids.py FORECAST_ROW_FIELDS), and only then sent to RiskGate. The signal payload
carries the same forecast_id, forecast_ts and model_version, plus outcome_index, outcome, token_id,
condition_id and the interval p_low / p_high when the caller gives them (Poly指挥's signal->intent
ruling). A field the caller does not give stays absent from the payload (intent_builder keeps its
legacy defaults) and is listed in the forecast row's calibration_problems; it is never guessed. If
the forecast row cannot be written, no signal is sent (fail closed).
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Sequence

from src import calibration_ids
from src.podlib import FORECASTS_PATH, AppendLockError, append_lock, bus, utc_now

BUS_NAME = "NewsForecaster"
STRATEGY = "news-forecaster"
SOURCE = "news"
# Version of the probability model behind emit_probability. Bump it whenever the way p is produced
# changes, so calibration never pools two models. The stub has no model of its own: callers pass p.
MODEL_VERSION = "news-forecaster-stub/1"


def _prob(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    if not 0.0 < value < 1.0:
        raise ValueError(f"{name} out of range")
    return float(value)


def _opt_str(name: str, value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-blank string when given")
    return value.strip()


def emit_probability(
    *,
    market: str,
    model_prob: float,
    note: str = "",
    bus_path: Path | None = None,
    token_id: str | None = None,
    outcome_index: int | None = None,
    outcome: str | None = None,
    condition_id: str | None = None,
    p_low: float | None = None,
    p_high: float | None = None,
    category: str | None = None,
    tags: Sequence[str] | None = None,
    end_date: str | None = None,
    model_version: str = MODEL_VERSION,
    strategy: str = STRATEGY,
    forecasts_path: Path | None = None,
    event_id: object = None,
    event_slug: object = None,
) -> dict:
    p = _prob("model_prob", model_prob)
    if (p_low is None) != (p_high is None):
        raise ValueError("p_low and p_high go together")
    low = high = None
    if p_low is not None:
        low, high = _prob("p_low", p_low), _prob("p_high", p_high)
        if not low <= p <= high:
            raise ValueError("need p_low <= model_prob <= p_high")
    if outcome_index is not None and (isinstance(outcome_index, bool) or not isinstance(outcome_index, int)
                                      or outcome_index < 0):
        raise ValueError("outcome_index must be an int >= 0 when given")
    token = _opt_str("token_id", None if token_id is None else str(token_id))
    condition = _opt_str("condition_id", condition_id)
    label = _opt_str("outcome", outcome)
    ends = _opt_str("end_date", end_date)
    version = _opt_str("model_version", model_version)
    if version is None:
        raise ValueError("model_version is required")
    tag_list = None if tags is None else [str(t) for t in tags]
    ts = utc_now()
    forecast_id = calibration_ids.make_forecast_id(
        source=SOURCE, ts=ts, market_id=market, condition_id=condition, outcome_index=outcome_index,
        token_id=token, model_prob=p, p_low=low, p_high=high, model_version=version)
    note_text = note or "Probability only. Not an order."
    row = {
        "schema_version": calibration_ids.SCHEMA_VERSION,
        "forecast_id": forecast_id,
        "ts": ts,
        "source": SOURCE,
        "strategy": strategy,
        "market_id": market,
        "condition_id": condition,
        "outcome_index": outcome_index,
        "outcome": label,
        "token_id": token,
        "model_prob": p,
        "p_low": low,
        "p_high": high,
        "category": _opt_str("category", category),
        "tags": tag_list,
        "end_date": ends,
        "model_version": version,
        "note": note_text,
        # Sealed here, before RiskGate / the intent builder reads any book for this forecast.
        "sealed_before_book_read": True,
        "calibration_problems": [],
        # Task BF: the market's Polymarket event (scanner row event_id / event_slug), for calibration
        # clustering only. Anything unusable is None (calibration_ids.event_value), never an error and
        # never a calibration problem; not part of forecast_id and not sent to RiskGate.
        "event_id": calibration_ids.event_value(event_id),
        "event_slug": calibration_ids.event_value(event_slug),
    }
    row["calibration_problems"] = calibration_ids.forecast_problems(row)
    # Seal first. A ForecastLogError propagates and nothing reaches the bus. Issue #162: the duplicate
    # check and the append hold the forecasts file's append lock; a lock not taken is a ForecastLogError.
    target = Path(forecasts_path) if forecasts_path is not None else FORECASTS_PATH
    try:
        with append_lock(target):
            calibration_ids.append_forecast(target, row)
    except AppendLockError as exc:
        raise calibration_ids.ForecastLogError(str(exc)) from exc
    payload: dict = {
        "model_prob": p,
        "order": False,
        "note": note_text,
        "forecast_id": forecast_id,
        "forecast_ts": ts,
        "model_version": version,
    }
    optional = {"outcome_index": outcome_index, "outcome": label, "token_id": token, "condition_id": condition,
                "p_low": low, "p_high": high}
    payload.update({key: value for key, value in optional.items() if value is not None})
    return bus(BUS_NAME, "RiskGate", "signal", market, payload, path=bus_path)
