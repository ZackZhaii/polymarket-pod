# Paste into a NEW OpenClaw workspace: polymarket-pod

Isolated agents. No shared memory with coffee ops.

Agents:
- scanner: Gamma read-only
- book: order book notes, no orders
- research: external repos and failure cases
- risk: veto using config/risk.toml
- reflector: proposals only
- operator-proxy: summarizes for the human, cannot enable live

Rules:
- Paper only.
- No keys in prompts, skills, or logs.
- Skills allowed now: market-scan, paper-fill, calibration-report.
- Do not install trading MCP with go_live.
- Bus file: logs/bus.jsonl

First task: read docs/HANDOFF.md and emit a Phase 0 runbook in Chinese.
