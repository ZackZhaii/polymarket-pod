# Polymarket Pod Handoff

Independent project. Do not mix with coffee StarHive or existing trading pods.

## Operator boundary

- You fund a dedicated small wallet and keep keys only in local `.env` (gitignored).
- This repo stays paper-first. `config/risk.toml` has `live_enabled = false`.
- No private keys, seed phrases, or API secrets in chat, logs, or git.
- No spoofing, wash trading, self-trading, or volume farming.
- Mainland China crypto rules and geo access are the operator's responsibility. Do not add VPN/bypass steps.
- Do not promise returns. Paper for 2-4 weeks before any human live gate.

## Repo

https://github.com/ZackZhaii/polymarket-pod

Local directory (new, isolated):

```text
D:/projects/polymarket-pod
```

or Osaka VPS:

```text
~/polymarket-pod
```

## What to build now (Phase 0-1 only)

1. Clone this repo into the isolated directory.
2. Keep scanner read-only (Gamma public API).
3. Add a JSONL bus at `logs/bus.jsonl` per `docs/PROTOCOL.md`.
4. Add a paper ledger at `logs/paper.jsonl` (simulated fills, no SDK signing).
5. Role stubs only: Scanner, BookWatcher, ArbBinary, Maker, NewsForecaster, RiskGate, Sentinel, Reflector.
6. RiskGate must reject before any paper fill. Caps from `config/risk.toml`.
7. Reflector writes proposals to `playbook/proposals.md`. It must not edit risk.toml.
8. Tests for sum-to-one arb math and risk veto. No live client.

## Later, only after human gate

- Install `polymarket-client`.
- Wire AsyncSecureClient behind `live_enabled` and a second confirm flag.
- Data API v2 reconciliation and redeem.
- MCP read-only first. Trading MCP stays preview.

## References (study, do not copy profit claims)

- Official SDK: https://github.com/Polymarket/py-sdk
- Maker: https://github.com/warproxxx/poly-maker
- Sports MM autopsy: https://github.com/kachence/polymm
- NegRisk paper scanner: https://github.com/matthewnyc2/arbitrage
