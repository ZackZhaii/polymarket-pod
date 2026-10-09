"""M3 calibration report (Task AZ, step 3). Offline, read-only, no network, no orders.

Joins the sealed forecasts (logs/forecasts.jsonl) and the paper fills (logs/paper.jsonl, via
forecast_id / decision_id) to the settlement snapshot (data/resolutions.jsonl, written by
src/resolutions.py) and scores them as Poly反思's M3 calibration design (sections 1-3) describes:

- B, forecast sample: one forecast per market (the first sealed one), p = model_prob for its token,
  y = 1 when that token won. Brier, log loss, reliability bins with Wilson intervals, ECE, logistic
  calibration slope / intercept, and counts and Brier by category, strategy and model_version.
- A, trade sample: one sample per decision_id (at most two decisions per market), p = the first
  fill's model_prob, q = its market_mid (Task BC; design 3: the decision-time mid, else market_price
  with price_source noted, which only a fill booked before Task BA can reach). Brier of p and of q,
  delta (model - market), expected (edge x shares) and realized paper P&L.

Task BC: a fill whose fill_context says it was built without book context (the operator CLI or a
gate check, calibration_ids.NO_BOOK_CONTEXTS) is dropped as no_book_context, apart from the genuine
missing-data reasons; a fill whose market_mid key is present but not a usable price is dropped as
fill_market_mid_missing (never replaced by market_price).

Task BD: by_category (both samples) and by_edge_tier (trades) groups that meet the group minimums carry
``intervals`` (Brier, ECE, calibration slope and intercept, and for trades delta Brier and realization,
each with a market-clustered bootstrap interval); a group below them carries ``intervals: None``. A
sufficient forecast section adds the intercept interval, the ECE interval and an intercept verdict. Both
sections report ``event_cluster``: the largest cluster's share of the samples, flagged ``insufficient``
below the section minimums, else ``concentrated`` above CALIBRATION_MAX_CLUSTER_SHARE, else ``ok``.
No event id is recorded yet, so a cluster is one market (condition_id): a lower bound on concentration.

Fail closed. Nothing is imputed: a row with calibration_problems, a missing join, a pending,
UMA-unconfirmed, disputed, void or ambiguous resolution, or a token / outcome that disagrees with
the snapshot is dropped and counted by reason in the report. A corrupt input file (torn line,
bad JSON, wrong schema, conflicting duplicate) is an error exit and no report is written; a
ledger that fails ledger_store.verify is its own error exit. Below the M3-pending minimums
(src/thresholds.py CALIBRATION_*) a section is "insufficient" and carries no verdict.

The report never changes a threshold, a size, config/risk.toml or any trading behaviour, and it
proposes nothing (proposals are step 4). Exit codes: docs/EXIT_CODES.md.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import NormalDist
from typing import Any, Iterable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import calibration_ids  # noqa: E402
from src import ledger_store  # noqa: E402
from src.podlib import FORECASTS_PATH, PAPER_PATH, RESOLUTIONS_PATH  # noqa: E402
from src.thresholds import (  # noqa: E402
    CALIBRATION_BIN_EDGES,
    CALIBRATION_BOOTSTRAP_RESAMPLES,
    CALIBRATION_BOOTSTRAP_SEED,
    CALIBRATION_CI_LEVEL,
    CALIBRATION_EDGE_TIERS,
    CALIBRATION_EXPIRY_TIERS_H,
    CALIBRATION_EQUAL_FREQ_BINS,
    CALIBRATION_EQUAL_FREQ_MIN_PER_BIN,
    CALIBRATION_FIXED_BINS_MIN_SAMPLES,
    CALIBRATION_GROUP_MIN_MARKETS,
    CALIBRATION_GROUP_MIN_SAMPLES,
    CALIBRATION_LOGLOSS_CLIP,
    CALIBRATION_MAX_CLUSTER_SHARE,
    CALIBRATION_MAX_DECISIONS_PER_MARKET,
    CALIBRATION_MIN_MARKETS,
    CALIBRATION_MIN_RESOLUTION_DAYS,
    CALIBRATION_MIN_SAMPLES,
)

REPORT_DIR = ROOT / "reports" / "calibration"
SCHEMA_VERSION = 1

EXIT_OK = 0  # report written (each section says sufficient or insufficient)
EXIT_USAGE = 2
EXIT_CORRUPT_INPUT = 3  # an input file is corrupt or off its schema; no report written
EXIT_LEDGER_INVALID = 4  # logs/paper.jsonl fails ledger_store.verify; no report written
EXIT_WRITE_FAILED = 5  # the report files could not be written

# Design 1.1: never samples (gate tests, probes, the self-check, fixtures).
EXCLUDED_STRATEGIES = frozenset({"phase0-gate-test", "no-edge-check", "wash-probe"})
EXCLUDED_MARKET_PREFIXES = ("phase0-self-check", "e2e-")
RESOLUTION_FIELDS = ("condition_id", "winning_token_id", "outcome_prices_final", "closed_ts", "snapshot_ts",
                     "status", "uma_checked")  # src/resolutions.py ROW_FIELDS (tested equal)
NOTES = (
    "Labels are only data/resolutions.jsonl rows with uma_checked=true and status=resolved; nothing is imputed.",
    "UMA dispute status not checked (beyond umaResolutionStatus=resolved).",
    "Not a promise of profit. Paper only. This report proposes nothing and changes no setting.",
    "q is the first fill's market_mid (price_source=market_mid). Only a fill booked before Task BA, with no "
    "market_mid key at all, uses market_price instead (price_source=market_price, design 3); a recorded but "
    "null mid is a drop (fill_market_mid_missing), never a fallback. trades.price_sources counts both.",
    "Fills built without book context (fill_context operator_cli / gate_check) are dropped as no_book_context.",
    "event_cluster: no event id is recorded yet, so each market (condition_id) is its own cluster; markets of one "
    "event count separately, so the share is a lower bound on event concentration. Reported only.",
    "Group intervals (by_category, by_edge_tier) exist only for groups at the group minimums; read conclusions "
    "from intervals only and mind the number of groups.",
)


class CorruptInput(Exception):
    """An input file cannot be read as complete, schema-conforming JSONL."""


# ------------------------------------------------------------------ reading (strict)

def _reject_constant(name: str) -> float:
    raise ValueError(f"non-finite JSON constant {name}")


def read_strict_jsonl(path: Path, label: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Every line one JSON object, the file ending in a newline. Absent file = no rows."""
    path = Path(path)
    meta: dict[str, Any] = {"path": str(path), "present": path.exists(), "rows": 0, "sha256": None}
    if not path.exists():
        return [], meta
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise CorruptInput(f"{label}: unreadable: {type(exc).__name__}") from exc
    meta["sha256"] = hashlib.sha256(data).hexdigest()
    if data and not data.endswith(b"\n"):
        raise CorruptInput(f"{label}: last line is torn (no trailing newline)")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CorruptInput(f"{label}: not UTF-8") from exc
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(text.split("\n")[:-1] if text else [], 1):
        if not line.strip():
            raise CorruptInput(f"{label}:{number}: blank line")
        try:
            row = json.loads(line, parse_constant=_reject_constant)
        except ValueError as exc:
            raise CorruptInput(f"{label}:{number}: not JSON ({exc})") from exc
        if not isinstance(row, dict):
            raise CorruptInput(f"{label}:{number}: not a JSON object")
        rows.append(row)
    meta["rows"] = len(rows)
    return rows, meta


def _number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _str_or_none(value: object) -> bool:
    return value is None or isinstance(value, str)


def check_forecast_rows(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Schema of logs/forecasts.jsonl (calibration_ids.FORECAST_ROW_FIELDS); id -> row."""
    by_id: dict[str, dict[str, Any]] = {}
    want = set(calibration_ids.FORECAST_ROW_FIELDS)
    for number, row in enumerate(rows, 1):
        where = f"forecasts:{number}"
        if set(row) != want:
            raise CorruptInput(f"{where}: keys differ from FORECAST_ROW_FIELDS")
        if not (isinstance(row["forecast_id"], str) and row["forecast_id"] and isinstance(row["ts"], str)
                and _number(row["model_prob"]) and isinstance(row["calibration_problems"], list)
                and all(isinstance(p, str) for p in row["calibration_problems"])
                and all(_str_or_none(row[k]) for k in ("market_id", "condition_id", "token_id", "strategy",
                                                        "category", "end_date", "model_version", "outcome"))
                and (row["tags"] is None or (isinstance(row["tags"], list)
                                             and all(isinstance(t, str) for t in row["tags"])))):
            raise CorruptInput(f"{where}: a field has the wrong type")
        prior = by_id.get(row["forecast_id"])
        if prior is not None and prior != row:
            raise CorruptInput(f"{where}: forecast_id {row['forecast_id']} repeated with different content")
        by_id[row["forecast_id"]] = row
    return by_id


def check_resolution_rows(rows: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], set[str]]:
    """Schema of data/resolutions.jsonl; condition_id -> row, plus disputed ids (conflicting rows)."""
    by_condition: dict[str, dict[str, Any]] = {}
    disputed: set[str] = set()
    for number, row in enumerate(rows, 1):
        where = f"resolutions:{number}"
        if set(row) != set(RESOLUTION_FIELDS):
            raise CorruptInput(f"{where}: keys differ from the resolutions schema")
        prices = row["outcome_prices_final"]
        if not (isinstance(row["condition_id"], str) and row["condition_id"].strip()
                and isinstance(row["winning_token_id"], str) and isinstance(prices, list)
                and all(_number(p) for p in prices) and isinstance(row["closed_ts"], str)
                and _parse_ts(row["closed_ts"]) is not None and isinstance(row["snapshot_ts"], str)
                and isinstance(row["status"], str) and isinstance(row["uma_checked"], bool)):
            raise CorruptInput(f"{where}: a field has the wrong type")
        key = row["condition_id"]
        prior = by_condition.get(key)
        if prior is not None and _label_view(prior) != _label_view(row):
            disputed.add(key)  # design 1.4: snapshots disagree -> disputed, excluded (not corrupt)
        by_condition.setdefault(key, row)
    return by_condition, disputed


def _label_view(row: Mapping[str, Any]) -> tuple:
    return (row["winning_token_id"], tuple(row["outcome_prices_final"]), row["status"], row["uma_checked"])


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str) or "T" not in value:
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


# ------------------------------------------------------------------ labels

def label(resolution: Mapping[str, Any] | None, disputed: bool, outcome_index: object,
          token_id: object) -> tuple[int | None, str | None]:
    """(y, None) for a usable label, else (None, drop reason). Never guesses."""
    if resolution is None:
        return None, "resolution_pending"
    if disputed:
        return None, "resolution_disputed"
    if resolution["status"] != "resolved" or resolution["uma_checked"] is not True:
        return None, ("resolution_not_uma_resolved" if resolution["status"] == "closed"
                      else "resolution_ambiguous_status")
    prices = resolution["outcome_prices_final"]
    if sorted(prices) != [0.0] * (len(prices) - 1) + [1.0] or len(prices) < 2:
        return None, "resolution_void"
    if isinstance(outcome_index, bool) or not isinstance(outcome_index, int) or not 0 <= outcome_index < len(prices):
        return None, "outcome_index_out_of_range"
    by_index = 1 if prices[outcome_index] == 1.0 else 0
    by_token = 1 if str(token_id) == resolution["winning_token_id"] else 0
    if by_index != by_token:
        return None, "token_outcome_mismatch"
    return by_index, None


def _excluded(strategy: object, market: object) -> str | None:
    if strategy in EXCLUDED_STRATEGIES:
        return "excluded_strategy"
    if isinstance(market, str) and market.startswith(EXCLUDED_MARKET_PREFIXES):
        return "excluded_market"
    return None


# ------------------------------------------------------------------ metrics (pure)

def brier(pairs: Sequence[tuple[float, int]]) -> float | None:
    return sum((p - y) ** 2 for p, y in pairs) / len(pairs) if pairs else None


def _clip(p: float) -> float:
    return min(max(p, CALIBRATION_LOGLOSS_CLIP), 1.0 - CALIBRATION_LOGLOSS_CLIP)


def log_loss(pairs: Sequence[tuple[float, int]]) -> float | None:
    if not pairs:
        return None
    return -sum(math.log(_clip(p)) if y else math.log(1.0 - _clip(p)) for p, y in pairs) / len(pairs)


def _z() -> float:
    return NormalDist().inv_cdf(0.5 + CALIBRATION_CI_LEVEL / 2.0)


def wilson(hits: int, n: int) -> list[float] | None:
    if n <= 0:
        return None
    z = _z()
    phat = hits / n
    denom = 1.0 + z * z / n
    centre = (phat + z * z / (2 * n)) / denom
    half = z * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n)) / denom
    low = 0.0 if hits == 0 else max(0.0, centre - half)
    high = 1.0 if hits == n else min(1.0, centre + half)
    return [low, high]


def reliability(pairs: Sequence[tuple[float, int]]) -> dict[str, Any]:
    """Fixed design bins at >= CALIBRATION_FIXED_BINS_MIN_SAMPLES samples, else equal-frequency bins."""
    n = len(pairs)
    bins: list[dict[str, Any]] = []
    if n >= CALIBRATION_FIXED_BINS_MIN_SAMPLES:
        kind = "fixed"
        edges = CALIBRATION_BIN_EDGES
        groups: list[list[tuple[float, int]]] = [[] for _ in edges[:-1]]
        for p, y in pairs:
            index = next(i for i in range(len(edges) - 1)
                         if edges[i] <= p < edges[i + 1] or (i == len(edges) - 2 and p == edges[-1]))
            groups[index].append((p, y))
        ranges = [(edges[i], edges[i + 1]) for i in range(len(edges) - 1)]
    else:
        kind = "equal_frequency"
        count = max(1, min(CALIBRATION_EQUAL_FREQ_BINS, n // CALIBRATION_EQUAL_FREQ_MIN_PER_BIN)) if n else 0
        ordered = sorted(pairs)
        groups = [ordered[i * n // count:(i + 1) * n // count] for i in range(count)] if count else []
        ranges = [(g[0][0], g[-1][0]) if g else (None, None) for g in groups]
    ece = 0.0
    for (low, high), group in zip(ranges, groups):
        size = len(group)
        hits = sum(y for _, y in group)
        mean_p = sum(p for p, _ in group) / size if size else None
        rate = hits / size if size else None
        interval = wilson(hits, size)
        if size:
            ece += size / n * abs(mean_p - rate)  # type: ignore[operator]
        judged = size >= CALIBRATION_GROUP_MIN_SAMPLES
        bins.append({"range": [low, high], "n": size, "mean_p": mean_p, "hit_rate": rate, "wilson": interval,
                     "mean_p_outside_wilson": (not interval[0] <= mean_p <= interval[1])  # type: ignore[index]
                     if judged and interval is not None else None})
    return {"kind": kind, "bins": bins, "ece": ece if n else None}


def logistic_fit(pairs: Sequence[tuple[float, int]], iterations: int = 50) -> tuple[float, float] | None:
    """y ~ a + b * logit(p) by Newton-Raphson; None when not estimable (one class, separation, singular)."""
    if len({y for _, y in pairs}) < 2 or len(pairs) < 3:
        return None
    xs = [math.log(_clip(p) / (1.0 - _clip(p))) for p, _ in pairs]
    ys = [y for _, y in pairs]
    a = b = 0.0
    for _ in range(iterations):
        g0 = g1 = h00 = h01 = h11 = 0.0
        for x, y in zip(xs, ys):
            eta = a + b * x
            mu = 1.0 / (1.0 + math.exp(-eta)) if eta >= 0 else math.exp(eta) / (1.0 + math.exp(eta))
            w = mu * (1.0 - mu)
            g0 += y - mu
            g1 += (y - mu) * x
            h00 += w
            h01 += w * x
            h11 += w * x * x
        det = h00 * h11 - h01 * h01
        if det <= 1e-12:
            return None
        da = (h11 * g0 - h01 * g1) / det
        db = (h00 * g1 - h01 * g0) / det
        a, b = a + da, b + db
        if not (math.isfinite(a) and math.isfinite(b)) or abs(b) > 50 or abs(a) > 50:
            return None  # separation: coefficients run away
        if abs(da) < 1e-10 and abs(db) < 1e-10:
            return a, b
    return None


def cluster_bootstrap(samples: Sequence[Mapping[str, Any]], statistic, key: str = "condition_id"
                      ) -> list[float] | None:
    """Percentile interval of ``statistic`` over markets resampled with replacement (fixed seed)."""
    clusters: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for sample in samples:
        clusters[sample[key]].append(sample)
    names = sorted(clusters)
    if len(names) < 2:
        return None
    rng = random.Random(CALIBRATION_BOOTSTRAP_SEED)
    values: list[float] = []
    for _ in range(CALIBRATION_BOOTSTRAP_RESAMPLES):
        drawn = [s for _ in names for s in clusters[names[rng.randrange(len(names))]]]
        value = statistic(drawn)
        if value is not None and math.isfinite(value):
            values.append(value)
    if len(values) < CALIBRATION_BOOTSTRAP_RESAMPLES // 2:
        return None  # the statistic is mostly not estimable: no interval
    values.sort()
    tail = (1.0 - CALIBRATION_CI_LEVEL) / 2.0
    low = values[int(math.floor(tail * (len(values) - 1)))]
    high = values[int(math.ceil((1.0 - tail) * (len(values) - 1)))]
    return [low, high]


def sufficiency(samples: Sequence[Mapping[str, Any]], *, min_samples: int, min_markets: int,
                min_days: int | None) -> dict[str, Any]:
    n = len(samples)
    markets = len({s["condition_id"] for s in samples})
    days = len({s["resolution_day"] for s in samples})
    short = []
    if n < min_samples:
        short.append(f"samples {n} < {min_samples}")
    if markets < min_markets:
        short.append(f"markets {markets} < {min_markets}")
    if min_days is not None and days < min_days:
        short.append(f"resolution days {days} < {min_days}")
    return {"status": "insufficient" if short else "sufficient", "short_of": short, "n": n, "markets": markets,
            "resolution_days": days}


def _pairs(samples: Iterable[Mapping[str, Any]], key: str = "p") -> list[tuple[float, int]]:
    return [(float(s[key]), int(s["y"])) for s in samples]


def _slope(samples: Sequence[Mapping[str, Any]]) -> float | None:
    fit = logistic_fit(_pairs(samples))
    return None if fit is None else fit[1]


def _intercept(samples: Sequence[Mapping[str, Any]]) -> float | None:
    """Task BD: the calibration intercept a of y ~ a + b * logit(p); None when not estimable."""
    fit = logistic_fit(_pairs(samples))
    return None if fit is None else fit[0]


def _ece(samples: Sequence[Mapping[str, Any]]) -> float | None:
    return reliability(_pairs(samples))["ece"] if samples else None


def _delta_brier(samples: Sequence[Mapping[str, Any]]) -> float | None:
    if not samples:
        return None
    return brier(_pairs(samples, "p")) - brier(_pairs(samples, "q"))  # type: ignore[operator]


def _realization(samples: Sequence[Mapping[str, Any]]) -> float | None:
    expected = sum(s["expected_usd"] for s in samples)
    return sum(s["realized_usd"] for s in samples) / expected if expected > 0 else None


def group_intervals(samples: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Task BD: point values and market-clustered bootstrap intervals for one judged group."""
    fit = logistic_fit(_pairs(samples))
    out: dict[str, Any] = {
        "brier": brier(_pairs(samples)), "brier_ci": cluster_bootstrap(samples, lambda d: brier(_pairs(d))),
        "ece": _ece(samples), "ece_ci": cluster_bootstrap(samples, _ece),
        "slope": None if fit is None else fit[1], "slope_ci": cluster_bootstrap(samples, _slope),
        "intercept": None if fit is None else fit[0], "intercept_ci": cluster_bootstrap(samples, _intercept),
    }
    if samples and "q" in samples[0]:
        out["brier_market"] = brier(_pairs(samples, "q"))
        out["delta_brier"] = _delta_brier(samples)
        out["delta_brier_ci"] = cluster_bootstrap(samples, _delta_brier)
        out["realization"] = _realization(samples)
        out["realization_ci"] = cluster_bootstrap(samples, _realization)
    return out


def cluster_share(samples: Sequence[Mapping[str, Any]], *, status: str, key: str = "condition_id") -> dict[str, Any]:
    """Task BD: share of the samples in the largest cluster (ties: the smallest name), flagged.

    ``insufficient`` when the section is below its minimums (no flag is judged), else ``concentrated``
    when the share exceeds CALIBRATION_MAX_CLUSTER_SHARE, else ``ok``. Reported only; gates nothing.
    """
    counts = Counter(str(s[key]) for s in samples)
    n = len(samples)
    largest = min(counts.items(), key=lambda item: (-item[1], item[0])) if counts else None
    share = largest[1] / n if largest else None
    if status != "sufficient":
        flag = "insufficient"
    else:
        flag = "concentrated" if share is not None and share > CALIBRATION_MAX_CLUSTER_SHARE else "ok"
    return {"cluster_key": key, "n": n, "n_clusters": len(counts),
            "largest_cluster": largest[0] if largest else None, "largest_cluster_n": largest[1] if largest else 0,
            "largest_share": share, "max_share": CALIBRATION_MAX_CLUSTER_SHARE, "status": status, "flag": flag}


# ------------------------------------------------------------------ groups

def tier(value: float | None, bounds: Sequence[float], unit: str = "") -> str:
    """Label of the half-open tier holding ``value``: "<b0", "[b_i, b_i+1)", ">=b_last"; "(unknown)" for None."""
    if value is None or not math.isfinite(value):
        return "(unknown)"
    if value < bounds[0]:
        return f"<{bounds[0]:g}{unit}"
    for low, high in zip(bounds, bounds[1:]):
        if value < high:
            return f"[{low:g}, {high:g}){unit}"
    return f">={bounds[-1]:g}{unit}"


def price_band(value: float) -> str:
    edges = CALIBRATION_BIN_EDGES
    for low, high in zip(edges, edges[1:]):
        if value < high or high == edges[-1]:
            return f"[{low:g}, {high:g})" if high != edges[-1] else f"[{low:g}, {high:g}]"
    return "(unknown)"


def _hours_between(start: object, end: object) -> float | None:
    a, b = _parse_ts(start), _parse_ts(end)
    return None if a is None or b is None else (b - a).total_seconds() / 3600.0


# ------------------------------------------------------------------ samples

def forecast_samples(forecasts: Mapping[str, dict[str, Any]], resolutions: Mapping[str, dict[str, Any]],
                     disputed: set[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], Counter, Counter]:
    """B sample. Returns (eligible, dropped rows with reason, reason counts, calibration_problem counts)."""
    reasons: Counter = Counter()
    problem_counts: Counter = Counter()
    dropped: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []

    def drop(row: Mapping[str, Any], reason: str) -> None:
        reasons[reason] += 1
        dropped.append({"kind": "forecast", "forecast_id": row.get("forecast_id"), "market_id": row.get("market_id"),
                        "condition_id": row.get("condition_id"), "reason": reason})

    for row in sorted(forecasts.values(), key=lambda r: (r["ts"], r["forecast_id"])):
        excluded = _excluded(row.get("strategy"), row.get("market_id"))
        if excluded:
            drop(row, excluded)
            continue
        problems = sorted(set(row["calibration_problems"]) | set(calibration_ids.forecast_problems(row)))
        if problems:
            problem_counts.update(problems)
            drop(row, "forecast_calibration_problems")
            continue
        candidates.append(row)
    seen: set[str] = set()
    eligible: list[dict[str, Any]] = []
    for row in candidates:
        condition = row["condition_id"]
        if condition in seen:
            drop(row, "duplicate_market_forecast")  # design 1.3: the first sealed forecast per market only
            continue
        seen.add(condition)
        resolution = resolutions.get(condition)
        y, reason = label(resolution, condition in disputed, row["outcome_index"], row["token_id"])
        if reason:
            drop(row, reason)
            continue
        eligible.append({"kind": "forecast", "forecast_id": row["forecast_id"], "market_id": row["market_id"],
                         "condition_id": condition, "token_id": row["token_id"],
                         "outcome_index": row["outcome_index"], "p": float(row["model_prob"]), "y": y,
                         "strategy": row["strategy"], "model_version": row["model_version"],
                         "category": row["category"], "tags": row["tags"],
                         "resolution_day": resolution["closed_ts"][:10]})  # type: ignore[index]
    return eligible, dropped, reasons, problem_counts


def trade_samples(fills: Sequence[dict[str, Any]], forecasts: Mapping[str, dict[str, Any]],
                  resolutions: Mapping[str, dict[str, Any]], disputed: set[str]
                  ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], Counter, Counter]:
    """A sample: one per decision_id, at most CALIBRATION_MAX_DECISIONS_PER_MARKET per market."""
    reasons: Counter = Counter()
    problem_counts: Counter = Counter()
    dropped: list[dict[str, Any]] = []
    decisions: dict[str, list[dict[str, Any]]] = {}

    def drop(ref: Mapping[str, Any], reason: str, count: int = 1) -> None:
        reasons[reason] += count
        dropped.append({"kind": "trade", "fill_id": ref.get("fill_id"), "decision_id": ref.get("decision_id"),
                        "market_id": ref.get("market"), "fills": count, "reason": reason})

    for row in fills:
        if not ledger_store.counts(row) or row.get("risk_decision") != "allow":
            continue  # not a trade sample at all (hypothetical / not counted); not a drop
        if row.get("fill_context") in calibration_ids.NO_BOOK_CONTEXTS:
            drop(row, "no_book_context")  # Task BC: stamped at fill time; never inferred from missing fields
            continue
        excluded = _excluded(row.get("strategy"), row.get("market"))
        if excluded:
            drop(row, excluded)
            continue
        if "calibration_problems" not in row:
            drop(row, "fill_missing_identity")  # booked before Task AX
            continue
        stored = row["calibration_problems"] if isinstance(row["calibration_problems"], list) else ["unreadable"]
        problems = sorted(set(map(str, stored)) | set(calibration_ids.fill_problems(row)) | set(_context_problems(row)))
        if problems:
            problem_counts.update(problems)
            drop(row, "fill_calibration_problems")
            continue
        decisions.setdefault(row["decision_id"], []).append(row)
    per_market: Counter = Counter()
    eligible: list[dict[str, Any]] = []
    ordered = sorted(decisions.items(), key=lambda item: (str(item[1][0].get("ts")), item[0]))
    for decision_id, rows in ordered:
        first = rows[0]
        ref = {"decision_id": decision_id, "market": first.get("market")}
        same = {(r["forecast_id"], r["token_id"], r["condition_id"], r["outcome_index"]) for r in rows}
        if len(same) != 1:
            drop(ref, "decision_inconsistent", len(rows))
            continue
        forecast = forecasts.get(first["forecast_id"])
        if forecast is None:
            drop(ref, "fill_forecast_not_found", len(rows))
            continue
        if (forecast.get("condition_id"), forecast.get("market_id")) != (first["condition_id"], first["market"]):
            drop(ref, "fill_forecast_mismatch", len(rows))
            continue
        condition = first["condition_id"]
        resolution = resolutions.get(condition)
        y, reason = label(resolution, condition in disputed, first["outcome_index"], first["token_id"])
        if reason:
            drop(ref, reason, len(rows))
            continue
        if per_market[condition] >= CALIBRATION_MAX_DECISIONS_PER_MARKET:
            drop(ref, "re_entry_cap", len(rows))
            continue
        q, price_source = market_probability(first)
        if q is None:
            drop(ref, "fill_market_mid_missing", len(rows))
            continue
        per_market[condition] += 1
        bought = sum(float(r["shares"]) for r in rows if r.get("side") == "buy")
        sold = sum(float(r["shares"]) for r in rows if r.get("side") == "sell")
        cost = sum(float(r["notional_usd"]) + float(r["fee_usd"]) for r in rows if r.get("side") == "buy")
        proceeds = sum(float(r["notional_usd"]) - float(r["fee_usd"]) for r in rows if r.get("side") == "sell")
        realized = (bought - sold) * y + proceeds - cost  # type: ignore[operator]
        expected = sum(float(r["edge"]) * float(r["shares"]) for r in rows)
        eligible.append({"kind": "trade", "decision_id": decision_id, "forecast_id": first["forecast_id"],
                         "market_id": first["market"], "condition_id": condition, "token_id": first["token_id"],
                         "side": first.get("side"), "p": float(first["model_prob"]),
                         "q": q, "price_source": price_source, "fill_context": first.get("fill_context"),
                         "y": y, "fills": len(rows),
                         "re_entry": per_market[condition] > 1, "expected_usd": expected, "realized_usd": realized,
                         "strategy": first.get("strategy"), "model_version": first.get("model_version"),
                         "category": first.get("category"),
                         "edge_tier": tier(float(first["edge"]), CALIBRATION_EDGE_TIERS),
                         "expiry_tier": tier(_hours_between(first.get("ts"), first.get("end_date")),
                                             CALIBRATION_EXPIRY_TIERS_H, "h"),
                         "price_band": price_band(float(first["market_price"])),
                         "resolution_day": resolution["closed_ts"][:10]})  # type: ignore[index]
    return eligible, dropped, reasons, problem_counts


def _context_problems(row: Mapping[str, Any]) -> list[str]:
    """Task BC: a stored fill_context must be a known one (a row from before Task BC has no key: no problem)."""
    if "fill_context" not in row:
        return []
    value = row["fill_context"]
    if value is None:
        return ["missing_fill_context"]
    return [] if value in calibration_ids.FILL_CONTEXTS else ["invalid_fill_context"]


def market_probability(fill: Mapping[str, Any]) -> tuple[float | None, str]:
    """Task BC: q for a trade sample and its price_source.

    market_mid when the fill records one (Task BA onward). A fill that has the key but no usable mid
    (null, non-finite, outside (0, 1)) gives (None, "market_mid"): the caller drops it, never falling
    back. Only a fill with no market_mid key at all (booked before Task BA) uses its market_price, as
    design section 3 allows ("mid if there is one, else market_price, with price_source noted").
    """
    if "market_mid" in fill:
        mid = fill["market_mid"]
        if isinstance(mid, bool) or not isinstance(mid, (int, float)) or not math.isfinite(mid) or not 0 < mid < 1:
            return None, "market_mid"
        return float(mid), "market_mid"
    return float(fill["market_price"]), "market_price"


# ------------------------------------------------------------------ report (pure)

def forecast_section(samples: Sequence[dict[str, Any]]) -> dict[str, Any]:
    pairs = _pairs(samples)
    fit = logistic_fit(pairs)
    gate = sufficiency(samples, min_samples=CALIBRATION_MIN_SAMPLES, min_markets=CALIBRATION_MIN_MARKETS,
                       min_days=CALIBRATION_MIN_RESOLUTION_DAYS)
    section: dict[str, Any] = {
        **gate,
        "brier": brier(pairs), "log_loss": log_loss(pairs),
        "base_rate": (sum(y for _, y in pairs) / len(pairs)) if pairs else None,
        "mean_p": (sum(p for p, _ in pairs) / len(pairs)) if pairs else None,
        "reliability": reliability(pairs),
        "calibration": {"intercept": None if fit is None else fit[0], "slope": None if fit is None else fit[1]},
        "event_cluster": cluster_share(samples, status=gate["status"]),  # Task BD
        "verdict": None,
    }
    if gate["status"] == "sufficient":
        section["brier_ci"] = cluster_bootstrap(samples, lambda s: brier(_pairs(s)))
        section["ece_ci"] = cluster_bootstrap(samples, _ece)  # Task BD
        slope_ci = cluster_bootstrap(samples, _slope)
        section["calibration"]["slope_ci"] = slope_ci
        intercept_ci = cluster_bootstrap(samples, _intercept)  # Task BD
        section["calibration"]["intercept_ci"] = intercept_ci
        if slope_ci is None:
            slope_verdict = "slope_not_estimable"
        elif slope_ci[1] < 1.0:
            slope_verdict = "overconfident"
        elif slope_ci[0] > 1.0:
            slope_verdict = "underconfident"
        else:
            slope_verdict = "no_clear_miscalibration"
        if intercept_ci is None:
            intercept_verdict = "intercept_not_estimable"
        elif intercept_ci[0] > 0:
            intercept_verdict = "positive_bias"  # outcomes happen more often than p says
        elif intercept_ci[1] < 0:
            intercept_verdict = "negative_bias"  # outcomes happen less often than p says
        else:
            intercept_verdict = "no_clear_bias"
        section["verdict"] = {"calibration_slope": slope_verdict, "calibration_intercept": intercept_verdict}
    else:
        section["verdict"] = "insufficient"
    return section


def _skill(drawn: Sequence[Mapping[str, Any]]) -> float | None:
    market = brier(_pairs(drawn, "q"))
    return None if not market else 1.0 - brier(_pairs(drawn, "p")) / market  # type: ignore[operator]


def trade_section(samples: Sequence[dict[str, Any]]) -> dict[str, Any]:
    model = _pairs(samples, "p")
    market = _pairs(samples, "q")
    gate = sufficiency(samples, min_samples=CALIBRATION_MIN_SAMPLES, min_markets=CALIBRATION_MIN_MARKETS,
                       min_days=CALIBRATION_MIN_RESOLUTION_DAYS)
    bs_model, bs_market = brier(model), brier(market)
    expected = sum(s["expected_usd"] for s in samples)
    realized = sum(s["realized_usd"] for s in samples)
    section: dict[str, Any] = {
        **gate,
        "brier_model": bs_model, "brier_market": bs_market,
        "delta_brier": None if bs_model is None else bs_model - bs_market,  # type: ignore[operator]
        "brier_skill": (1.0 - bs_model / bs_market) if bs_model is not None and bs_market else None,
        "log_loss_model": log_loss(model), "log_loss_market": log_loss(market),
        "mean_r_minus_e": ((realized - expected) / len(samples)) if samples else None,
        "expected_usd": expected, "realized_usd": realized,
        "realization": realized / expected if expected > 0 else None,
        "re_entries": sum(1 for s in samples if s["re_entry"]),
        "event_cluster": cluster_share(samples, status=gate["status"]),  # Task BD
        "verdict": None,
    }
    if gate["status"] == "sufficient":
        def delta(drawn):
            return brier(_pairs(drawn, "p")) - brier(_pairs(drawn, "q"))  # type: ignore[operator]
        ci = cluster_bootstrap(samples, delta)
        section["delta_brier_ci"] = ci
        section["brier_skill_ci"] = cluster_bootstrap(samples, _skill)
        section["realized_usd_ci"] = cluster_bootstrap(samples, lambda d: sum(s["realized_usd"] for s in d))
        section["mean_r_minus_e_ci"] = cluster_bootstrap(
            samples, lambda d: sum(s["realized_usd"] - s["expected_usd"] for s in d) / len(d))
        if ci is None:
            section["verdict"] = {"model_vs_market": "not_estimable"}
        elif ci[0] > 0:
            section["verdict"] = {"model_vs_market": "worse_than_market"}
        elif ci[1] < 0:
            section["verdict"] = {"model_vs_market": "better_than_market"}
        else:
            section["verdict"] = {"model_vs_market": "inconclusive"}
    else:
        section["verdict"] = "insufficient"
    return section


def group_counts(samples: Sequence[dict[str, Any]], key: str, *, intervals: bool = False) -> dict[str, Any]:
    """Counts (and a Brier when judged) per group; with ``intervals`` (Task BD) also group_intervals or None."""
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for sample in samples:
        value = sample.get(key)
        groups["(unknown)" if value in (None, "") else str(value)].append(sample)
    out = {}
    for name in sorted(groups):
        rows = groups[name]
        gate = sufficiency(rows, min_samples=CALIBRATION_GROUP_MIN_SAMPLES,
                           min_markets=CALIBRATION_GROUP_MIN_MARKETS, min_days=None)
        judged = gate["status"] == "sufficient"
        out[name] = {"n": gate["n"], "markets": gate["markets"], "status": gate["status"],
                     "brier": brier(_pairs(rows)) if judged else None}
        if rows and "q" in rows[0]:
            out[name]["brier_market"] = brier(_pairs(rows, "q")) if judged else None
            out[name]["realized_usd"] = sum(r["realized_usd"] for r in rows) if judged else None
        if intervals:
            out[name]["intervals"] = group_intervals(rows) if judged else None
    return out


def thresholds_used() -> dict[str, Any]:
    return {"min_samples": CALIBRATION_MIN_SAMPLES, "min_markets": CALIBRATION_MIN_MARKETS,
            "min_resolution_days": CALIBRATION_MIN_RESOLUTION_DAYS,
            "group_min_samples": CALIBRATION_GROUP_MIN_SAMPLES, "group_min_markets": CALIBRATION_GROUP_MIN_MARKETS,
            "fixed_bins_min_samples": CALIBRATION_FIXED_BINS_MIN_SAMPLES, "bin_edges": list(CALIBRATION_BIN_EDGES),
            "equal_freq_bins": CALIBRATION_EQUAL_FREQ_BINS,
            "equal_freq_min_per_bin": CALIBRATION_EQUAL_FREQ_MIN_PER_BIN,
            "bootstrap_resamples": CALIBRATION_BOOTSTRAP_RESAMPLES, "bootstrap_seed": CALIBRATION_BOOTSTRAP_SEED,
            "ci_level": CALIBRATION_CI_LEVEL, "logloss_clip": CALIBRATION_LOGLOSS_CLIP,
            "edge_tiers": list(CALIBRATION_EDGE_TIERS), "expiry_tiers_h": list(CALIBRATION_EXPIRY_TIERS_H),
            "max_decisions_per_market": CALIBRATION_MAX_DECISIONS_PER_MARKET,
            "max_cluster_share": CALIBRATION_MAX_CLUSTER_SHARE, "status": "M3 pending"}


def build_report(forecast_rows: list[dict[str, Any]], resolution_rows: list[dict[str, Any]],
                 fill_rows: list[dict[str, Any]], *, generated_at: str,
                 inputs: Mapping[str, Any] | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Pure: the report dict and the sample table. Raises CorruptInput on a schema problem."""
    forecasts = check_forecast_rows(forecast_rows)
    resolutions, disputed = check_resolution_rows(resolution_rows)
    b_rows, b_dropped, b_reasons, b_problems = forecast_samples(forecasts, resolutions, disputed)
    a_rows, a_dropped, a_reasons, a_problems = trade_samples(fill_rows, forecasts, resolutions, disputed)
    report = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "complete": True,
        "inputs": dict(inputs or {}),
        "thresholds": thresholds_used(),
        "forecasts": {
            "rows": len(forecast_rows), "distinct": len(forecasts), "eligible": len(b_rows),
            "dropped": sum(b_reasons.values()), "dropped_by_reason": dict(sorted(b_reasons.items())),
            "calibration_problems": dict(sorted(b_problems.items())),
            "metrics": forecast_section(b_rows),
            "by_category": group_counts(b_rows, "category", intervals=True),
            "by_strategy": group_counts(b_rows, "strategy"),
            "by_model_version": group_counts(b_rows, "model_version"),
        },
        "trades": {
            "eligible": len(a_rows), "dropped": sum(b for b in a_reasons.values()),
            "dropped_by_reason": dict(sorted(a_reasons.items())),
            "calibration_problems": dict(sorted(a_problems.items())),
            "price_sources": dict(sorted(Counter(s["price_source"] for s in a_rows).items())),  # Task BC
            "metrics": trade_section(a_rows),
            "by_category": group_counts(a_rows, "category", intervals=True),
            "by_strategy": group_counts(a_rows, "strategy"),
            "by_model_version": group_counts(a_rows, "model_version"),
            "by_side": group_counts(a_rows, "side"),
            "by_edge_tier": group_counts(a_rows, "edge_tier", intervals=True),
            "by_expiry_tier": group_counts(a_rows, "expiry_tier"),
            "by_price_band": group_counts(a_rows, "price_band"),
        },
        "resolutions": {"rows": len(resolution_rows), "conditions": len(resolutions), "disputed": len(disputed),
                        "uma_resolved": sum(1 for r in resolutions.values()
                                            if r["status"] == "resolved" and r["uma_checked"] is True)},
        "notes": list(NOTES),
    }
    report["group_count"] = sum(len(section[key]) for section in (report["forecasts"], report["trades"])
                                for key in section if key.startswith("by_"))
    samples = ([dict(s, eligible=True) for s in b_rows] + [dict(s, eligible=False) for s in b_dropped]
               + [dict(s, eligible=True) for s in a_rows] + [dict(s, eligible=False) for s in a_dropped])
    return report, samples


def _fmt(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.4f}"
    if isinstance(value, list):
        return "[" + ", ".join(_fmt(v) for v in value) + "]"
    return str(value)


def _cluster_line(metrics: Mapping[str, Any]) -> str:
    ec = metrics.get("event_cluster") or {}
    return (f"- event cluster ({ec.get('cluster_key')}): largest {_fmt(ec.get('largest_cluster'))} holds "
            f"{ec.get('largest_cluster_n')} of {ec.get('n')} ({_fmt(ec.get('largest_share'))}; limit "
            f"{_fmt(ec.get('max_share'))}), {ec.get('n_clusters')} clusters: **{ec.get('flag')}**")


def render_markdown(report: Mapping[str, Any]) -> str:
    f, t = report["forecasts"], report["trades"]
    fm, tm = f["metrics"], t["metrics"]
    lines = [f"# M3 calibration report ({report['generated_at']})", "",
             "Offline and read-only. Paper only; not a promise of profit; proposes nothing.", "",
             "## Forecast sample (B)", "",
             f"- status: **{fm['status']}** ({'; '.join(fm['short_of']) or 'all minimums met'})",
             f"- eligible {f['eligible']} of {f['distinct']} forecasts; markets {fm['markets']}; "
             f"resolution days {fm['resolution_days']}",
             f"- dropped {f['dropped']}: {json.dumps(f['dropped_by_reason'], sort_keys=True)}",
             f"- Brier {_fmt(fm['brier'])} (CI {_fmt(fm.get('brier_ci'))}); log loss {_fmt(fm['log_loss'])}; "
             f"ECE {_fmt(fm['reliability']['ece'])} (CI {_fmt(fm.get('ece_ci'))})",
             f"- calibration slope {_fmt(fm['calibration']['slope'])} (CI {_fmt(fm['calibration'].get('slope_ci'))}),"
             f" intercept {_fmt(fm['calibration']['intercept'])} (CI {_fmt(fm['calibration'].get('intercept_ci'))})",
             _cluster_line(fm),
             f"- verdict: {json.dumps(fm['verdict'])}", "",
             f"Reliability ({fm['reliability']['kind']} bins):", "",
             "| range | n | mean p | hit rate | Wilson |", "|---|---|---|---|---|"]
    for b in fm["reliability"]["bins"]:
        lines.append(f"| {_fmt(b['range'])} | {b['n']} | {_fmt(b['mean_p'])} | {_fmt(b['hit_rate'])} | "
                     f"{_fmt(b['wilson'])} |")
    lines += ["", "## Trade sample (A)", "",
              f"- status: **{tm['status']}** ({'; '.join(tm['short_of']) or 'all minimums met'})",
              f"- eligible {t['eligible']} decisions; dropped {t['dropped']} fills: "
              f"{json.dumps(t['dropped_by_reason'], sort_keys=True)}",
              f"- market probability q by source: {json.dumps(t.get('price_sources', {}), sort_keys=True)}",
              f"- Brier model {_fmt(tm['brier_model'])}, market {_fmt(tm['brier_market'])}, delta "
              f"{_fmt(tm['delta_brier'])} (CI {_fmt(tm.get('delta_brier_ci'))}), skill {_fmt(tm['brier_skill'])} "
              f"(CI {_fmt(tm.get('brier_skill_ci'))})",
              f"- log loss model {_fmt(tm['log_loss_model'])}, market {_fmt(tm['log_loss_market'])}",
              f"- expected {_fmt(tm['expected_usd'])} USD, realized {_fmt(tm['realized_usd'])} USD, realization "
              f"{_fmt(tm['realization'])}; mean(R-E) {_fmt(tm['mean_r_minus_e'])} "
              f"(CI {_fmt(tm.get('mean_r_minus_e_ci'))})",
              _cluster_line(tm),
              f"- verdict: {json.dumps(tm['verdict'])}", "", "## Groups (forecast sample)", ""]
    for key in ("by_category", "by_strategy", "by_model_version"):
        lines.append(f"- {key}: " + json.dumps(f[key], sort_keys=True))
    lines += ["", "## Groups (trade sample)", ""]
    for key in ("by_category", "by_strategy", "by_model_version", "by_side", "by_edge_tier", "by_expiry_tier",
                "by_price_band"):
        lines.append(f"- {key}: " + json.dumps(t[key], sort_keys=True))
    lines += ["", f"Groups reported: {report['group_count']} (conclusions read from intervals only).",
              "", "## Notes", ""] + [f"- {note}" for note in report["notes"]]
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ CLI

def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def write_report(out_dir: Path, report: Mapping[str, Any], samples: Sequence[Mapping[str, Any]]) -> Path:
    day = str(report["generated_at"])[:10]
    folder = Path(out_dir) / day
    folder.mkdir(parents=True, exist_ok=True)
    body = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    table = "".join(json.dumps(s, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n" for s in samples)
    final = folder / "report.json"
    if final.exists():
        final.unlink()  # a failed rewrite must not leave an older report.json beside new partial files
    _atomic_write(folder / "samples.jsonl", table.encode("utf-8"))
    _atomic_write(folder / "report.md", render_markdown(report).encode("utf-8"))
    _atomic_write(final, body.encode("utf-8"))  # last: its presence means the set is complete
    return folder


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.calibration",
        description="Offline M3 calibration report (forecasts + fills joined to the settlement snapshot). "
                    "Read-only; no network; no orders.",
        epilog="Exit codes: 0 report written; 2 usage error; 3 corrupt input (nothing written); "
               "4 paper ledger fails verify (nothing written); 5 report write failed. See docs/EXIT_CODES.md.")
    parser.add_argument("--forecasts", type=Path, default=None, help="default logs/forecasts.jsonl")
    parser.add_argument("--resolutions", type=Path, default=None, help="default data/resolutions.jsonl")
    parser.add_argument("--ledger", type=Path, default=None, help="default logs/paper.jsonl")
    parser.add_argument("--out-dir", type=Path, default=None, help="default reports/calibration")
    parser.add_argument("--now", default=None, help="report time, zoned ISO (default: now, UTC)")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return EXIT_OK if exc.code == 0 else EXIT_USAGE
    now = datetime.now(timezone.utc) if args.now is None else _parse_ts(args.now)
    if now is None:
        print("calibration: usage error: --now must be a zoned ISO timestamp", file=sys.stderr)
        return EXIT_USAGE
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    forecasts_path = args.forecasts or FORECASTS_PATH
    resolutions_path = args.resolutions or RESOLUTIONS_PATH
    ledger_path = args.ledger or PAPER_PATH
    try:
        forecast_rows, f_meta = read_strict_jsonl(forecasts_path, "forecasts")
        resolution_rows, r_meta = read_strict_jsonl(resolutions_path, "resolutions")
    except CorruptInput as exc:
        print(f"calibration: corrupt input, no report written: {exc}", file=sys.stderr)
        return EXIT_CORRUPT_INPUT
    try:
        data, read = ledger_store.read_ledger(Path(ledger_path))
    except OSError as exc:
        print(f"calibration: ledger unreadable, no report written: {type(exc).__name__}", file=sys.stderr)
        return EXIT_LEDGER_INVALID
    problems = ledger_store.verify(read)
    if problems:
        print(f"calibration: paper ledger fails verify, no report written: {problems[:5]}", file=sys.stderr)
        return EXIT_LEDGER_INVALID
    l_meta = {"path": str(ledger_path), "present": Path(ledger_path).exists(), "rows": len(read.rows),
              "sha256": hashlib.sha256(data).hexdigest() if data else None}
    try:
        report, samples = build_report(forecast_rows, resolution_rows, list(read.rows), generated_at=stamp,
                                       inputs={"forecasts": f_meta, "resolutions": r_meta, "ledger": l_meta})
    except CorruptInput as exc:
        print(f"calibration: corrupt input, no report written: {exc}", file=sys.stderr)
        return EXIT_CORRUPT_INPUT
    try:
        folder = write_report(args.out_dir or REPORT_DIR, report, samples)
    except (OSError, ValueError) as exc:
        print(f"calibration: report write failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_WRITE_FAILED
    fm, tm = report["forecasts"]["metrics"], report["trades"]["metrics"]
    print(f"calibration forecasts={report['forecasts']['eligible']}/{report['forecasts']['distinct']} "
          f"({fm['status']}) trades={report['trades']['eligible']} ({tm['status']}) "
          f"dropped_forecasts={report['forecasts']['dropped']} dropped_fills={report['trades']['dropped']} "
          f"report={folder}")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
