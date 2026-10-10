"""Phase 0 shared helpers. Standard library only. No secrets. No orders."""
from __future__ import annotations

import http.client
import json
import socket
import ssl
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
LOG_DIR = ROOT / "logs"
RISK_PATH = ROOT / "config" / "risk.toml"
BUS_PATH = LOG_DIR / "bus.jsonl"
PAPER_PATH = LOG_DIR / "paper.jsonl"
SESSIONS_PATH = LOG_DIR / "sessions.jsonl"
MARKETS_PATH = DATA_DIR / "markets.jsonl"
BOOKS_PATH = DATA_DIR / "books.jsonl"
RESOLUTIONS_PATH = DATA_DIR / "resolutions.jsonl"
# Task AX (M3 step 2): one sealed NewsForecaster forecast per row (src/calibration_ids.py).
FORECASTS_PATH = LOG_DIR / "forecasts.jsonl"
QUOTES_PATH = LOG_DIR / "quotes.jsonl"
# Sentinel StaleWatch streaks between paper_cycle runs (Task AA). Not a risk.toml key.
SENTINEL_STATE_PATH = LOG_DIR / "sentinel_stale.json"

# Local paper bankroll. Not a wallet. Sizing never exceeds the 1% clip.
from src.thresholds import PAPER_EQUITY_USD  # noqa: E402 - Task AC: value in src/thresholds.py
from src.secret_scan import check_message as _secret_check  # noqa: E402 - Task AQ: bus secret filter
BUS_TYPES = frozenset({"signal", "veto", "fill", "proposal", "halt", "resume"})
# Task AL: ``resume`` lifts one halt. Only the Operator (the human, via ``python -m src.halt_store
# resume``) may send it, and "Operator" may send nothing else: it is not a bus role otherwise.
RESUME_FROM = "Operator"
RESUME_SCOPES = frozenset({"global", "market"})
# Bus role names follow docs/ROSTER.md. A legacy name is still accepted by bus() and written
# as its ROSTER name; readers of older logs/bus.jsonl lines compare via canonical_role().
ROLE_ALIASES = {"Forecaster": "NewsForecaster"}

# Ledger refuses to run if config/risk.toml drifts off these pins.
PINS = {
    "mode": "paper",
    "live_enabled": False,
    "require_human_gate": True,
    "max_order_pct": 0.01,
    "max_daily_loss_pct": 0.03,
}

GAMMA_PREFIX = "https://gamma-api.polymarket.com/markets"
BOOK_PREFIX = "https://clob.polymarket.com/book?"
# Task AK: the Data API v2 reads the Reconciler needs (plus DATA_API_STATUS_URL below) (v1 is retired 2026-10-24). Exact routes
# with their "?" so /v2/positions/combos and every other v2 route stay refused.
DATA_API_POSITIONS_PREFIX = "https://data-api.polymarket.com/v2/positions?"
DATA_API_TRADES_PREFIX = "https://data-api.polymarket.com/v2/trades?"
# Data freshness, read first on every reconciliation round. Exact URL (no query, no sub-path).
DATA_API_STATUS_URL = "https://data-api.polymarket.com/v2/status"
PUBLIC_PREFIXES = (GAMMA_PREFIX, BOOK_PREFIX, DATA_API_POSITIONS_PREFIX, DATA_API_TRADES_PREFIX)

# public_get retries transient network/TLS failures (SSL EOF, resets, timeouts, 5xx).
# Waits are PUBLIC_GET_BACKOFF_S * 2**n between attempts (0.5 s, 1 s). HTTP 4xx is never retried.
PUBLIC_GET_ATTEMPTS = 3
PUBLIC_GET_BACKOFF_S = 0.5
_sleep = time.sleep  # tests patch this so they never sleep

# OSError covers ConnectionResetError, socket.timeout/TimeoutError and ssl.SSLError (incl. SSLEOFError).
TRANSIENT_NETWORK_ERRORS = (URLError, OSError, http.client.HTTPException, ssl.SSLError, socket.timeout)


class PublicGetError(RuntimeError):
    """A public read still failed on a transient network/TLS error after every retry."""

    def __init__(self, url: str, attempts: int, last_error: BaseException) -> None:
        self.url = url
        self.attempts = attempts
        self.last_error = last_error
        super().__init__(
            f"public GET failed after {attempts} attempts: {type(last_error).__name__}: {last_error} ({url})"
        )


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_now_ms() -> str:
    """UTC now with milliseconds and Z (``2026-10-10T12:59:04.634Z``), the precision of a CLOB book_ts.

    Issue #128: a clock that compares itself with a millisecond book_ts must not be cut to whole seconds,
    or a book stamped within the current second reads as from the future (future_book_ts). Truncated to
    the millisecond, never rounded up, so it is never later than the real clock. utc_now() (seconds) stays
    for row and log stamps.
    """
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_risk_toml(text: str) -> dict[str, Any]:
    """Parse the flat key = value file. Not a general TOML parser."""
    parsed: dict[str, Any] = {}
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if "=" not in line:
            raise ValueError(f"risk.toml:{lineno}: expected key = value")
        key, raw_value = (part.strip() for part in line.split("=", 1))
        if not key:
            raise ValueError(f"risk.toml:{lineno}: empty key")
        parsed[key] = _parse_scalar(raw_value, lineno)
    return parsed


def _parse_scalar(value: str, lineno: int) -> Any:
    if value == "true":
        return True
    if value == "false":
        return False
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    try:
        if any(ch in value for ch in ".eE"):
            return float(value)
        return int(value)
    except ValueError as exc:
        raise ValueError(f"risk.toml:{lineno}: unsupported value") from exc


def load_risk() -> dict[str, Any]:
    text = RISK_PATH.read_text(encoding="utf-8")
    cfg = parse_risk_toml(text)
    missing = [key for key in PINS if key not in cfg]
    if missing:
        raise RuntimeError("risk.toml missing required keys: " + ", ".join(missing))
    return cfg


def pin_problems(cfg: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for key, expected in PINS.items():
        if cfg.get(key) != expected:
            problems.append(f"{key}={cfg.get(key)!r} expected {expected!r}")
    return problems


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def canonical_role(name: str) -> str:
    """Return the ROSTER bus name for a legacy role name; any other name is returned unchanged."""
    return ROLE_ALIASES.get(name, name)


def bus(
    frm: str,
    to: str,
    kind: str,
    market: str,
    payload: dict[str, Any],
    path: Path | None = None,
) -> dict[str, Any]:
    frm = canonical_role(frm)
    to = canonical_role(to)
    if kind not in BUS_TYPES:
        raise RuntimeError(f"bus type not allowed: {kind}")
    if kind == "fill" and frm != "Executor":
        raise RuntimeError("only Executor may emit fills")
    if kind == "fill" and payload.get("risk_decision") != "allow":
        raise RuntimeError("fill refused without RiskGate allow")
    if kind == "resume" or frm == RESUME_FROM:
        _check_resume(frm, to, kind, payload)
    message = {
        "ts": utc_now(),
        "from": frm,
        "to": to,
        "type": kind,
        "market": market,
        "payload": payload,
    }
    # Task AQ (Sentinel design gap 9): a message holding a private key, mnemonic or credential value
    # is never written; a global halt is recorded and SecretRefused raised (nothing echoed).
    _secret_check(message)
    append_jsonl(path or BUS_PATH, message)
    return message


def _check_resume(frm: str, to: str, kind: str, payload: dict[str, Any]) -> None:
    """Task AL: a resume is from=Operator to=Sentinel, names one halt and who approved it."""
    if kind != "resume":
        raise RuntimeError("Operator is not a bus role except as the sender of resume")
    if frm != RESUME_FROM:
        raise RuntimeError("only Operator may emit resume")
    if to != "Sentinel":
        raise RuntimeError("resume goes to Sentinel")
    if not isinstance(payload, dict):
        raise RuntimeError("resume payload must be a dict")
    approved = payload.get("approved_by")
    if not isinstance(approved, str) or not approved.strip():
        raise RuntimeError("resume needs approved_by")
    if payload.get("scope") not in RESUME_SCOPES:
        raise RuntimeError("resume scope must be global or market")
    ref = payload.get("halt_ref")
    if not isinstance(ref, dict) or not isinstance(ref.get("id"), str) or not ref.get("id"):
        raise RuntimeError("resume needs halt_ref with an id")
    if ref.get("scope") == "global" and payload.get("scope") != "global":
        raise RuntimeError("a market resume cannot lift a global halt")
    if payload.get("exchange_action", "none") != "none":
        raise RuntimeError("resume carries no exchange action")


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, HTTPError):
        return exc.code >= 500
    return isinstance(exc, TRANSIENT_NETWORK_ERRORS)


def public_get(
    url: str,
    timeout: float = 20.0,
    *,
    attempts: int | None = None,
    backoff_s: float | None = None,
) -> Any:
    """GET a public read endpoint. Rejects order and auth URLs.

    Transient network/TLS errors and HTTP 5xx are retried with exponential backoff.
    HTTP 4xx and JSON errors are raised at once. If every attempt fails, raises PublicGetError.
    """
    if not (url.startswith(PUBLIC_PREFIXES) or url == DATA_API_STATUS_URL):
        raise RuntimeError("refusing URL outside public Gamma markets, CLOB /book and Data API v2 positions/trades/status")
    lowered = url.lower()
    banned = ("/order", "/auth", "api-key", "secret", "passphrase", "signature", "withdraw")
    if any(token in lowered for token in banned):
        raise RuntimeError("refusing authenticated or order endpoint")
    request = Request(
        url,
        headers={"User-Agent": "polymarket-pod-phase0", "Accept": "application/json"},
        method="GET",
    )
    tries = max(1, int(PUBLIC_GET_ATTEMPTS if attempts is None else attempts))
    base = PUBLIC_GET_BACKOFF_S if backoff_s is None else float(backoff_s)
    for attempt in range(tries):
        try:
            with urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8")
            break
        except Exception as exc:  # noqa: BLE001 - filtered by _retryable
            if not _retryable(exc):
                raise
            if attempt + 1 >= tries:
                raise PublicGetError(url, tries, exc) from exc
            _sleep(base * (2 ** attempt))
    return json.loads(body)


def json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            row = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows
