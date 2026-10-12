---
name: backtest-methodology
description: Use when designing or reviewing an offline paper backtest or experiment report for polymarket-pod. Docs only — snapshot replay, no look-ahead, fee/slippage per Polymarket docs. No live API in tests; proposals never auto-apply.
---

# backtest-methodology

Docs skill for QuantResearcher (`docs/ROSTER.md`). Code today: `quant.brier` on a local fixture only (`src/quant.py`) — full replay engine is TBD (not in repo). Stdlib / offline fixtures only.

## Allowed

- Define methodology for snapshot replay over `data/markets.jsonl`, `data/books.jsonl`, `logs/paper.jsonl`.
- Cite official fee / neg-risk / rewards facts from https://docs.polymarket.com (fetched `llms.txt` → fees, negative-risk, liquidity-rewards).
- Emit at most a `proposal` (`quant.propose` / `reflector.propose`); never edit `config/risk.toml`.

## Forbidden

- Look-ahead: using future book/resolve data to decide a past fill.
- Live downloads inside pytest. Save fixtures under `tests/fixtures/`.
- Claiming locked profit on one-leg fills (`paper_ledger.DIRECTIONAL`).
- Installing backtest frameworks or trading SDKs.

## Steps

### 1. Snapshot replay

1. Order events by bus/book `ts` ascending (canonical `Z` UTC — see `bus-contract`). For books, `ts` is the recorder clock; `book_ts` is the exchange's own `/book` time (null with `book_ts_problem` when unusable, Task R). Use `book_ts` for book age and never back-fill it from `ts`.
2. At time t, Decision may read only rows with `ts <= t`.
3. Book used for a fill must supply `book_ts`; age rule same as `book_staleness.book_age_problem` (`MAX_BOOK_AGE_S` = 120 s, exactly 120 s fresh, future stale) unless the experiment explicitly documents a different research lag (research ≠ production gate).

### 2. Fee and slippage model (official)

From https://docs.polymarket.com/trading/fees.md:

- Taker fee: `fee = C × feeRate × p × (1 - p)` (C = shares, p = price). Makers pay 0.
- Category `feeRate` examples: Crypto 0.07, Sports 0.05, Finance/Politics/Mentions/Tech 0.04, Economics/Culture/Weather/Other 0.05, **Geopolitics 0**.
- Fees fund Maker Rebates; rounded to 5 decimals.

**Code conflict to flag in every report:** `risk_gate.evaluate` uses `fee_per_share = fee_rate * market_price` in the edge and `fee_usd = notional_usd * fee_rate` (both linear), **not** `feeRate × p × (1-p)`. The CLI default `--fee-rate` is `0.0` (`paper_ledger.build_parser`). Paper experiments must state which formula they used. Do not "fix" risk_gate inside a backtest PR without Operator approval.

Default paper slippage today: CLI `--slippage` default `0.005` (`paper_ledger.build_parser`). Treat as assumed cost, not exchange truth.

Neg-risk (https://docs.polymarket.com/concepts/negative-risk.md): No in one outcome convertible to Yes in all others; `neg_risk.emit_gap` is signal-only in Phase 0.

Rewards (https://docs.polymarket.com/programs/liquidity-rewards.md): daily UTC maker rewards from resting quotes; min payout $1; scoring uses midpoint spread samples — **not simulated in repo** (TBD).

### 3. Out-of-sample and promotion gate

- Split by **market endDate** or by calendar week; never random-shuffle timestamps (breaks causality).
- Promotion-gate ledger (paper): a strategy advances only if (a) offline pytest green, (b) Brier or PnL vs fixture beats the documented baseline, (c) Reflector proposal filed, (d) Poly风控 + human gate — still `live_enabled=false`.
- `quant.propose` refuses a second proposal on the same bus file (`already_proposed`).

### 4. Experiment report template

```markdown
## Experiment <id>
- Hypothesis:
- Method: (fixture paths, fee formula choice, slippage, no-look-ahead proof)
- Result: (Brier / paper PnL / veto rates — not a profit promise)
- Limits: (sample size, fee-model mismatch, missing rewards sim)
- Next: (proposal text only; no risk.toml edit)
```

## Verification

- Report states fee formula used and notes the risk_gate mismatch if applicable.
- No network in tests; `live_enabled=false`; proposals `changes_risk_toml=false`.
