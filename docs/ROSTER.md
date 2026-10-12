# Polymarket Pod Roster

Independent trading roles. Not the design chat team.
Default mode: paper. Live switch is off and requires a human gate.

| Role | Duty | May trade |
|---|---|---|
| Scanner | Gamma active markets, rewards, expiry, liquidity | no |
| BookWatcher | CLOB book / WS staleness | no |
| ArbBinary | YES+NO ask sum < 1, depth-sized; disabled, every gap recorded as a `structural_arb_disabled` reject (#141) | no |
| NegRiskArb | multi-outcome ask sum < 1; disabled, every gap recorded as a `structural_arb_disabled` reject (#141) | no |
| Maker | post-only quotes, inventory skew, rewards | paper only |
| Endgame | near-resolution 0.98-0.99 convergence | paper only |
| SportsDevig | de-vig external odds, quote, hedge | paper only |
| NewsForecaster | news/X probability only | no |
| QuantResearcher | backtest, Brier, parameter proposals | no |
| RiskGate | 1% clip, daily loss, spread, category caps | veto |
| Executor | polymarket-client, paper ledger | paper only |
| Reconciler | Data API v2 positions / fills / redeem | no |
| Reflector | <=3 proposals/day, human approve | no |
| Sentinel | heartbeat, cancel-all, kill switch | cancel only |

Group chat is an append-only JSONL bus. Alerts may mirror to Telegram later. Never put keys in the bus.
