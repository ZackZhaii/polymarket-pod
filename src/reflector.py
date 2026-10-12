"""Reflector. Writes proposals only. Never edits config/risk.toml or size caps."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.podlib import RISK_PATH, bus, utc_now  # noqa: E402

PROPOSALS_PATH = ROOT / "playbook" / "proposals.md"
from src.thresholds import REFLECTOR_MAX_PER_UTC_DAY as MAX_PER_UTC_DAY  # noqa: E402 - Task AC
HEADER = (
    "# Proposals\n\n"
    "Reflector only. These notes do not change config/risk.toml or any size cap.\n"
)
FORBIDDEN = (
    "live_enabled = true",
    "live_enabled=true",
    'mode = "live"',
    'mode="live"',
)


def propose(
    text: str,
    market: str = "",
    *,
    proposals_path: Path = PROPOSALS_PATH,
    bus_path: Path | None = None,
    now: str | None = None,
    payload: dict | None = None,
) -> dict:
    """Append one proposal and a bus `proposal` line. Never emits a fill.

    Task BB: ``payload`` is an optional structured proposal (src/reflector_proposals.py schema); it rides in the
    bus payload under ``proposal``. Every Reflector bus proposal says ``exchange_action: none``.
    """
    if refused_path(proposals_path):
        raise RuntimeError("refusing to write risk.toml, config/ or src/thresholds.py")
    cleaned = " ".join(text.split())
    if not cleaned:
        return {"accepted": False, "reason": "empty"}
    lowered = cleaned.lower()
    if any(token in lowered for token in FORBIDDEN):
        return {"accepted": False, "reason": "refusing_live_change"}
    ts = now or utc_now()
    day = ts[:10]
    existing = proposals_path.read_text(encoding="utf-8") if proposals_path.exists() else ""
    if _count_day(existing, day) >= MAX_PER_UTC_DAY:
        return {"accepted": False, "reason": "daily_cap", "ts": ts}
    proposals_path.parent.mkdir(parents=True, exist_ok=True)
    if not existing:
        existing = HEADER
    block = (
        f"\n## {ts}\n\n"
        f"- market: {market or '(none)'}\n"
        "- effect: proposal only; does not change config/risk.toml or any size cap\n"
        f"- text: {cleaned}\n"
    )
    proposals_path.write_text(existing + block, encoding="utf-8")
    message = bus(
        "Reflector",
        "Operator",
        "proposal",
        market,
        {
            "text": cleaned,
            "effect": "proposal_only",
            "changes_risk_toml": False,
            "changes_size_caps": False,
            "exchange_action": "none",
            **({"proposal": payload} if payload is not None else {}),
        },
        path=bus_path,
    )
    return {"accepted": True, "reason": "written", "ts": message["ts"], "bus_type": message["type"]}


def refused_path(path: Path) -> bool:
    """Task BB: the Reflector never writes config/ (risk.toml, a strategy switch), any .toml, or thresholds.py."""
    resolved = Path(path).resolve()
    config = (ROOT / "config").resolve()
    return (resolved == RISK_PATH.resolve() or resolved == (ROOT / "src" / "thresholds.py").resolve()
            or resolved.suffix.lower() == ".toml" or config == resolved or config in resolved.parents)


def _count_day(text: str, day: str) -> int:
    prefix = f"## {day}"
    return sum(1 for line in text.splitlines() if line.startswith(prefix))


EXIT_OK = 0
EXIT_REFUSED = 1  # the proposal was not accepted, or risk.toml changed during the run


def main() -> int:
    before = RISK_PATH.read_bytes()
    result = propose(
        "Keep paper mode. One-leg fills are directional exposure, not locked profit. "
        "Do not change config/risk.toml or any size cap.",
        market="",
    )
    if RISK_PATH.read_bytes() != before:
        print("risk.toml changed; refusing", file=sys.stderr)
        return EXIT_REFUSED
    print(
        f"accepted={str(result['accepted']).lower()} reason={result['reason']} "
        "changes_risk_toml=false"
    )
    return EXIT_OK if result["accepted"] else EXIT_REFUSED


if __name__ == "__main__":
    raise SystemExit(main())
