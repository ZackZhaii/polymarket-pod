"""Phase 0 shared helpers. Standard library only. No secrets. No orders."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
LOG_DIR = ROOT / "logs"
RISK_PATH = ROOT / "config" / "risk.toml"
BUS_PATH = LOG_DIR / "bus.jsonl"
PAPER_PATH = LOG_DIR / "paper.jsonl"
MARKETS_PATH = DATA_DIR / "markets.jsonl"
BOOKS_PATH = DATA_DIR / "books.jsonl"
QUOTES_PATH = LOG_DIR / "quotes.jsonl"

# Local paper bankroll. Not a wallet. Sizing never exceeds the 1% clip.
PAPER_EQUITY_USD = 1000.0
BUS_TYPES = frozenset({"signal", "veto", "fill", "proposal", "halt"})

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


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def bus(
    frm: str,
    to: str,
    kind: str,
    market: str,
    payload: dict[str, Any],
    path: Path | None = None,
) -> dict[str, Any]:
    if kind not in BUS_TYPES:
        raise RuntimeError(f"bus type not allowed: {kind}")
    if kind == "fill" and frm != "Executor":
        raise RuntimeError("only Executor may emit fills")
    if kind == "fill" and payload.get("risk_decision") != "allow":
        raise RuntimeError("fill refused without RiskGate allow")
    message = {
        "ts": utc_now(),
        "from": frm,
        "to": to,
        "type": kind,
        "market": market,
        "payload": payload,
    }
    append_jsonl(path or BUS_PATH, message)
    return message


def public_get(url: str, timeout: float = 20.0) -> Any:
    """GET a public read endpoint. Rejects order and auth URLs."""
    if not (url.startswith(GAMMA_PREFIX) or url.startswith(BOOK_PREFIX)):
        raise RuntimeError("refusing URL outside public Gamma markets and CLOB /book")
    lowered = url.lower()
    banned = ("/order", "/auth", "api-key", "secret", "passphrase", "signature", "withdraw")
    if any(token in lowered for token in banned):
        raise RuntimeError("refusing authenticated or order endpoint")
    request = Request(
        url,
        headers={"User-Agent": "polymarket-pod-phase0", "Accept": "application/json"},
        method="GET",
    )
    with urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
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
