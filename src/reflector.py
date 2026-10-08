"""Reflector. Writes proposals only. Never edits config/risk.toml or size caps."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.podlib import RISK_PATH, bus, utc_now  # noqa: E402

PROPOSALS_PATH = ROOT / "playbook" / "proposals.md"
MAX_PER_UTC_DAY = 3
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
) -> dict:
    """Append one proposal and a bus `proposal` line. Never emits a fill."""
    if proposals_path.resolve() == RISK_PATH.resolve():
        raise RuntimeError("refusing to write risk.toml")
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
        },
        path=bus_path,
    )
    return {"accepted": True, "reason": "written", "ts": message["ts"], "bus_type": message["type"]}


def _count_day(text: str, day: str) -> int:
    prefix = f"## {day}"
    return sum(1 for line in text.splitlines() if line.startswith(prefix))


def main() -> int:
    before = RISK_PATH.read_bytes()
    result = propose(
        "Keep paper mode. One-leg fills are directional exposure, not locked profit. "
        "Do not change config/risk.toml or any size cap.",
        market="",
    )
    if RISK_PATH.read_bytes() != before:
        print("risk.toml changed; refusing", file=sys.stderr)
        return 1
    print(
        f"accepted={str(result['accepted']).lower()} reason={result['reason']} "
        "changes_risk_toml=false"
    )
    return 0 if result["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
