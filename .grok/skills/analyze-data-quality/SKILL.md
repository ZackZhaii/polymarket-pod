---
name: analyze-data-quality
description: Check whether polymarket-pod's local jsonl data (data/markets.jsonl, data/books.jsonl, logs/bus.jsonl, logs/paper.jsonl) is trustworthy before analysis, calibration, or a paper decision. Freshness, grain, nulls, duplicates, ranges, and cross-field consistency with stdlib Python only.
---

# Analyze Data Quality (polymarket-pod)

Trimmed from the data-analytics plugin skill. Notebook/pandas/plotly/jupyter steps, warehouse/BI connectors, shared plugin instructions, automation launchers, and telemetry were removed (`agents/openai.yaml` dropped). Inputs are local files only; no network. Installing pandas or similar: 需 Operator 批准.

## Inputs and grain

| File | Writer | Grain | Key |
|---|---|---|---|
| `data/markets.jsonl` | `scanner.persist_markets` (rows from `scanner.normalize`) | one market per scan run | (`id`, `ts`) |
| `data/books.jsonl` | `book.record` / `book.snapshot` | one outcome token per book run | (`market_id`, `token_id`, `ts`) |
| `logs/bus.jsonl` | `podlib.bus` | one message | none (append-only) |
| `logs/paper.jsonl` | `paper_ledger.commit_if_allowed` | one paper fill | none (append-only) |

`data/` and `logs/` are gitignored. Work on a copy if you need to transform anything; never rewrite or delete these files.

Read with `json` line by line, not only `podlib.read_jsonl`: `read_jsonl` silently skips undecodable and non-object lines, so first compare raw non-empty line count with parsed row count and report the difference.

## Checks

1. **Completeness.** Null/empty rate per field and per run (`ts`). Expected nulls are not defects: `book_ts=null` with a `book_ts_problem` (the exchange gave no usable `/book` timestamp), `spread=null` on one-sided books, empty `token_ids` with a `token_ids_problem`. Unexpected: `book_ts=null` without a problem, missing `ts`, missing `market_id`/`token_id`.
2. **Uniqueness.** Duplicate keys at the grain above; a duplicate (`market_id`, `token_id`, `ts`) usually means the same book run was recorded twice.
3. **Validity / ranges.** Prices in (0, 1); `best_bid <= best_ask`; `spread >= 0` and equal to `round(best_ask - best_bid, 6)` when both exist; sizes >= 0; `model_prob` in (0, 1); `risk_decision` in {`allow`, `veto`}; bus `type` in `podlib.BUS_TYPES`; token IDs are decimal strings (never floats).
4. **Timestamps.** Parse every `ts`/`book_ts` with `book_staleness.parse_book_ts` (aware ISO or epoch seconds; naive, date-only and numeric strings are invalid). Flag `book_ts` after its row `ts` (future relative to the recorder) and `book_ts` older than `book_staleness.MAX_BOOK_AGE_S` (120 s) relative to `ts`. Never fill a missing `book_ts` from `ts`.
5. **Consistency.** `token_ids` count equals `outcomes` count; `version` is `v1`/`v2` on passing rows; every `fill` on the bus is from `Executor` with `risk_decision=allow` and has a matching `logs/paper.jsonl` row; hypothetical fills have `counts_toward_risk=false`.
6. **Freshness and volume.** Newest `ts` per file vs now; rows per run over time; sudden drops in rows or in the share of two-sided books.
7. **Look-ahead (for backtests).** No row used for a decision at time t has `ts` or `book_ts` after t (`backtest-methodology`).

## Severity

- Critical: invalid or missing timestamps on rows that feed RiskGate, fills without `risk_decision=allow`, duplicated fills.
- High: large null spikes, mismatched token/outcome counts, prices out of range.
- Medium: explainable gaps (network-incomplete scan runs, one-sided books).
- Low: cosmetic issues.

## Report

1. files, grain, raw vs parsed line counts
2. checks run
3. findings: what failed, counts/rates/dates, why it matters, severity, likely cause, smallest fix or test
4. assumptions and open questions

Recommend an offline pytest (fixture under `tests/fixtures/`) only for stable rules. Do not change thresholds or `config/risk.toml`; propose instead (`reflector.propose`).
