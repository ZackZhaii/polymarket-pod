# M3 calibration (Task AN = step 1)

M3 scores paper probabilities against settled markets (Brier) and may later propose
threshold changes through Reflector. It never edits `config/risk.toml`, never sizes up,
and never places an order. `live_enabled` stays `false`.

Poly指挥's design draft was not found in the OpenClaw workspace; this file is the
design for the repo, taken from the operator dispatch for step 1.

## Steps

1. **This PR.** Read-only settlement snapshot: Gamma closed markets, held 48 h after
   `closedTime`, written to `data/resolutions.jsonl`.
2. **Task AX (done, prerequisite part).** Identity fields on forecasts, scanner rows and paper
   fills, and the sealed forecast log `logs/forecasts.jsonl`, so forecasts and fills can be
   joined to those labels. The join itself (`uma_checked=true` only) comes with step 3.
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

## Step 2: identity fields (Task AX)

Design: Poly反思's M3 calibration design, section 1.5. Helpers: `src/calibration_ids.py`
(standard library only). Data plumbing only: RiskGate never reads these fields, no veto, size,
threshold or golden decision moved, and `config/strategies.toml` is not created.

Join path:

```
logs/forecasts.jsonl (forecast_id, market_id, condition_id, token_id)
  -> logs/paper.jsonl fill (forecast_id, decision_id, condition_id, token_id)
  -> data/resolutions.jsonl (condition_id, winning_token_id)
```

### Ids

- `forecast_id` = `fc_` + first 32 hex of sha256 over the canonical JSON of the forecast's
  own content: source, ts, market_id, condition_id, outcome_index, token_id, model_prob,
  p_low, p_high, model_version. Minted once, by `forecaster.emit_probability`, when it seals
  the forecast. Same content (including the second-resolution ts) = same id.
- `decision_id` = `dc_` + first 32 hex of sha256 over `[strategy, market, token_id,
  forecast_id]`. Derived, never minted: every fill of one strategy on one token under one
  forecast shares it. Any part missing -> `null`. A caller-supplied `decision_id` that does not
  match the derivation is flagged `decision_id_mismatch`.
- `model_version`: `forecaster.MODEL_VERSION` (`news-forecaster-stub/1`) unless the caller
  passes its own. Bump it whenever the way p is produced changes.

### `logs/forecasts.jsonl`

One row per sealed forecast, keys in this order, no extras (`FORECAST_ROW_FIELDS`):

| Key | Type | Rule |
|---|---|---|
| `schema_version` | int | `1` |
| `forecast_id` | str | see above |
| `ts` | str | UTC `Z`, second resolution; also the signal's `forecast_ts` |
| `source` | str | `news` |
| `strategy` | str | default `news-forecaster` |
| `market_id` | str | Gamma market `id` (bus `market`) |
| `condition_id` | str / null | from the caller (scanner row) |
| `outcome_index` | int / null | the outcome p is about |
| `outcome` | str / null | its label |
| `token_id` | str / null | that outcome's token |
| `model_prob` | float | 0 < p < 1, finite |
| `p_low`, `p_high` | float / null | both or neither; `p_low <= model_prob <= p_high` |
| `category` | str / null | Gamma `category` |
| `tags` | list[str] / null | Gamma tag slugs; null = unknown, `[]` = none |
| `end_date` | str / null | scanner `end_date` |
| `model_version` | str | required |
| `note` | str | |
| `sealed_before_book_read` | bool | `true`: written before RiskGate / the intent builder reads a book for it |
| `calibration_problems` | list[str] | `missing_<field>` / `invalid_<field>` for the join fields; `[]` = joinable |

Writing: `calibration_ids.append_forecast`, one `O_APPEND` write of the whole line plus
`fsync`. Refused (`ForecastLogError`, nothing written): wrong keys, a value that is not exact
JSON (nan/inf), a torn last line already in the file, an OS error. A row whose `forecast_id` is
already on file is not written again. The forecast is written **before** the bus signal; if it
cannot be written, no signal is sent.

### Signal payload (NewsForecaster -> RiskGate)

Always: `model_prob`, `order: false`, `note`, `forecast_id`, `forecast_ts`, `model_version`.
When the caller gives them (Poly指挥's signal->intent ruling): `outcome_index`, `outcome`,
`token_id`, `condition_id`, `p_low`, `p_high`. A field not given stays absent, so
`intent_builder` keeps its legacy reading (outcome 0, point probability); the forecast row
lists it in `calibration_problems`. `intent_builder.signal_from_bus` carries `forecast_id`,
`forecast_ts` and `model_version` into `ProbabilitySignal`, then `OrderIntent` and
`to_risk_intent`.

### Scanner rows (`data/markets.jsonl`)

New `category` (Gamma `category`, else null) and `tags` (Gamma tag slugs; null when Gamma sent
none). The scanner keyset URL gains `include_tag=true` (same host and path, read-only). Neither
field filters or ranks a market. `intent_builder.market_context_from_row` keeps them as
`gamma_category` / `tags`; `category_banned` still reads only the caller's `category`.

### Paper fills (`logs/paper.jsonl`)

Every fill booked by `paper_ledger.commit_if_allowed` adds `decision_id`, `forecast_id`,
`forecast_ts`, `condition_id`, `outcome_index`, `outcome`, `category`, `tags`, `end_date`,
`model_version` (null when unknown) and `calibration_problems`. The RiskGate bus summary
(allow or veto) carries `forecast_id`.

Fail closed: a fill that misses a join field (`decision_id`, `forecast_id`, `forecast_ts`,
`condition_id`, `outcome_index`, `token_id`, `end_date`, `model_version`) is still booked,
because RiskGate already decided and identity is never a veto, but it lists every missing
field in `calibration_problems`. The calibration join must drop and count those rows; it never
repairs or guesses an id. Older fills have no such fields and count as not joinable.

## Step 2b: market context on the fill (Task BA)

The rest of design 1.5. Every paper fill also carries `calibration_ids.FILL_MARKET_FIELDS`, computed
in `paper_ledger.market_fields` at commit from data the intent already carries (no new read):

- `best_bid`, `best_ask`: the top of book of the filled token as the intent's producer saw it
  (intent_builder: the chosen token's BookSnapshot; e2e harness: the latest /book row; the
  paper_ledger no-edge check: the book row it used). Each a finite price in (0, 1), else null.
- `market_mid`: `(best_bid + best_ask) / 2`, rounded to 8 places, only when both sides are usable and
  not crossed (`bid <= ask`); else null. The calibration baseline should move from `market_price`
  (the ask on a buy) to this mid in a later step; src/calibration.py is not changed here.
- `hours_to_expiry_at_entry`: `end_date` minus the fill's `ts`, in hours (6 places; negative if the
  end has passed, as computed). Null when `end_date` is missing or not a zoned ISO time.
- `kelly_fraction_used`: intent_builder's shadow Kelly multiplier (risk.toml `kelly_fraction`, 0.15)
  that its shadow Kelly size was computed with. Recorded only: RiskGate sizes with its 1% clip and
  never reads it. Null for intents not built by intent_builder (operator CLI, gate checks).

A null is never invented: each one adds `missing_<field>` (no input) or `invalid_<field>` (input
not usable; `invalid_market_mid` = crossed book) to the fill's stored `calibration_problems`.
The join check `fill_problems` is unchanged, so older fills are not re-flagged; but the
calibration report drops any fill whose stored problems are non-empty, so a fill without this
context is not a calibration sample. RiskGate reads none of these fields (tested: source and
identical decisions with any values). No new number, no decision or size moved.

## Out of scope here

Brier, joining forecasts, Reflector proposals, bus fills, new URL hosts, any
`config/risk.toml` edit, live trading.
