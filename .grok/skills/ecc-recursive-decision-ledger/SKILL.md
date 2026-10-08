---
name: ecc-recursive-decision-ledger
description: Append-only ledger for repeated paper rollouts, parameter searches, or ensemble comparisons in polymarket-pod, with explicit marks and a promotion gate that never enables live trading. Field names follow decision-log and paper_ledger._decision_payload.
---

# Recursive Decision Ledger (polymarket-pod)

Trimmed from the ECC recursive-decision-ledger skill. External trackers (Linear/Jira), other projects' paths, auto-commit hooks, and the free-form `tools:` list were removed. Repeated rollouts are evidence, not proof; recursive confidence is never approval.

## Where records go

- Per-decision records already exist: RiskGate/Executor lines on `logs/bus.jsonl` (`decision-log`). Do not add new bus `type`s; `podlib.BUS_TYPES` is fixed.
- A rollout ledger is research output. Keep it as append-only JSONL outside the bus, e.g. a local file under the gitignored `logs/` (path convention only, TBD, not in repo). Summaries go in a PR body or a Reflector proposal (`reflector.propose`, at most 3 per UTC day), never in `config/risk.toml`.

## Ledger record (one line per rollout)

- `rollout_id`, `ts` (`podlib.utc_now()` format, `Z`)
- `prior_winner`, `prior_watchlist`
- `fresh_inputs`: files and their newest `ts` / `book_ts` (exchange time), with any `book_ts_problem`
- `search_space`, `method`, `trials`, `effective_trials`
- `candidates`: each with the decision fields used by `paper_ledger._decision_payload`: `strategy, model_prob, market_price, edge, edge_formula, assumed_slippage, fee_rate, fee_usd, reasons, hypothetical, risk_decision`, plus `market` and `token_id`
- `mark` per candidate: `accept`, `watch`, `reject`, `decay_watch`, `needs_replay`
- `coherence`: matches prior winner? matches latest rollout?
- `promotion_gate`: result and reason

Never rewrite or delete earlier lines; correct with a new dated line.

## Rollout loop

1. Load the prior ledger.
2. Capture fresh inputs at step zero (check them with `analyze-data-quality`).
3. Run the bounded search offline (fixtures or local jsonl; no network inside tests).
4. Score each candidate through `risk_gate.evaluate` semantics; a veto reason is a `reject` or `watch`, never overridden.
5. Compare with the prior winner and the latest rollout.
6. Downgrade on drift, stale or missing `book_ts`, failed replay, or tail risk (single-leg fills are directional exposure).
7. Append the ledger line before writing any summary.

## Coherence mark

```text
Ensemble matches prior winner: true
Latest rollout match: false
Live promotion allowed: false
Reason: Phase 0 paper; live_enabled=false
```

`Live promotion allowed` is always `false` in Phase 0.

## Promotion gate (paper only)

A candidate may move from `watch` to `accept` only when it beats the prior winner on the stated metric (e.g. Brier via `quant.brier`), replay checks pass, every risk limit came from code or `config/risk.toml` unchanged, and the evidence is in the ledger. Promotion beyond paper (any live step, risk.toml change, threshold change): 需 Operator 批准.

## Summary shape

```text
Rollout 15: prior winner holds; edge down 17% on replay. Mark: watch, not live.
Next gate: 20 replays with book_ts age <= 120 s.
```
