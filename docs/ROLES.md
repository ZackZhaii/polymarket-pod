# Polymarket Pod — Role Roster

All agents are new. They do not reuse an existing chat team. Group chat is alert-only. Private keys never enter chat, logs, or MCP.

| Role | Job | Can trade? |
|---|---|---|
| Scanner | Gamma market discovery, rank by volume/liquidity/expiry | No |
| BookWatcher | CLOB order book and spread | No |
| Researcher | News, official data, sportsbook odds. No orders | No |
| Forecaster | Blind probability, then confront market. Brier/Platt | No |
| ArbMerge | YES+NO or multi-outcome sum gaps. Single-leg fill is directional | Signal only |
| Maker | Post-only quotes, inventory skew, news pull | Signal only until live gate |
| CryptoPulse | Short-window BTC/ETH up-down. Last to enable | Signal only |
| RiskGate | Fractional Kelly, daily loss, exposure, liquidity | Veto |
| Sentinel | Heartbeat, stale book, cancel-all, kill switch | Cancel only |
| Reflector | After resolution, propose at most 3 playbook edits | No auto-apply |
| Operator | Human. Opens live gate, funds wallet, approves proposals | Yes |

## Chat

- Internal: structured JSONL between roles.
- External: Telegram/Discord alerts only (fills, kill switch, proposals).
- No private key, API secret, or seed in any channel.

## MCP

Read-only first: market search, odds, holders, leaderboard.
Trading MCP tools stay disabled until Operator opens the live gate.
