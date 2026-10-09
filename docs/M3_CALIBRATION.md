# M3 calibration (Task AN = step 1)

M3 scores paper probabilities against settled markets (Brier) and may later propose
threshold changes through Reflector. It never edits `config/risk.toml`, never sizes up,
and never places an order. `live_enabled` stays `false`.

Poly指挥's design draft was not found in the OpenClaw workspace; this file is the
design for the repo, taken from the operator dispatch for step 1.

## Steps

1. **This PR.** Read-only settlement snapshot: Gamma closed markets, held 48 h after
   `closedTime`, written to `data/resolutions.jsonl`.
2. Later: join paper forecasts / signals to those labels (`uma_checked=true` only).
3. Later: Brier (and related) report. `src/quant.py` today scores a local fixture only.
4. Later: Reflector proposals. A human applies any `risk.toml` change.

This PR does not join, does not score, and does not propose.

## Snapshot (`src/resolutions.py`)

```
python -m src.resolutions
python src/resolutions.py
```

- Public GET of Gamma `/markets/keyset?closed=true&order=closedTime&ascending=false`
  through `scanner.fetch_all_markets(..., base_url=...)`. Same host and path the
  scanner already uses; `closed=true` is a query flag, not a new endpoint.
- No `liquidity_num_min` (settled books do not need live depth).
- Paper pins are checked first. Drift → exit 4, no fetch, no write.
- Incomplete paging (cap, cursor repeat, empty page, network error) → exit 3,
  **write nothing** (existing `data/resolutions.jsonl` is left unchanged).
- A complete walk with zero eligible rows is exit 0 and writes nothing. There is no
  `MIN_SCANNED_MARKETS` floor: an empty closed universe is valid.
- No bus message, no fill, no order.

A row is written only when **all** of the following hold. Anything missing or
unusable **skips that row** (fail closed). It does not fail the run.

| Field | Rule |
|---|---|
| `closed` | JSON boolean `true` (not the string `"true"`) |
| `closedTime` | zoned ISO with a time (`expiry_rule.parse_end_date`); never `endDate` |
| hold | `now - closedTime >= RESOLUTION_HOLD_HOURS` (48 h; the boundary passes) |
| `conditionId` | non-empty string |
| `umaResolutionStatus` | present non-empty string; missing/blank skips the row |
| `outcomePrices` | finite numbers; exactly one `1.0` and the rest `0.0` |
| outcome tokens | `scanner.select_token_ids` (v2 `positionIds`, v1 `clobTokenIds`) |
| `winning_token_id` | token at the unique `1.0` index |

`data/resolutions.jsonl` columns (no extras):

`condition_id`, `winning_token_id`, `outcome_prices_final`, `closed_ts`,
`snapshot_ts`, `status`, `uma_checked`

- `closed_ts` / `snapshot_ts` are UTC `Z`.
- `uma_checked` is true only when `umaResolutionStatus` is exactly `resolved`.
- `status` is `resolved` when `uma_checked` is true, else `closed` (unique 1/0
  winner after 48 h, UMA not yet `resolved`).
- Dedup by `condition_id` (existing file + the current walk). First write wins.
- `data/` is gitignored; the JSONL is not committed.

Skip reasons (first match): `not_closed`, `missing_closed_ts`, `invalid_closed_ts`,
`future_closed_ts`, `hold_pending`, `missing_condition_id`, `missing_uma_status`,
`missing_outcome_prices`, `non_finite_outcome_prices`, `no_unique_winner`, the
scanner token problems (`missing_version`, `unsupported_version`, …),
`token_winner_mismatch`, `duplicate`.

A 50/50 book, two `1.0`s, or any non-0/1 remainder is `no_unique_winner`.

## Consumers

Calibration joins (step 2+) must keep only `uma_checked=true`. This snapshot still
writes `uma_checked=false` rows when a unique 1/0 winner exists after 48 h, so a
later UMA resolution is not silently treated as a label.

`RESOLUTION_HOLD_HOURS = 48.0` lives in `src/thresholds.py`. It is not coupled to
`ENDGAME_WINDOW_HOURS` (same number by coincidence). Not M2 pending: the dispatch
fixed 48 h.

## Out of scope here

Brier, joining forecasts, Reflector proposals, bus fills, new URL hosts, any
`config/risk.toml` edit, live trading.
