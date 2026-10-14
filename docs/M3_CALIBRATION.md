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
- Paper pins are checked first. Drift → exit 6, no fetch, no write.
- Incomplete paging (cap, cursor repeat, empty page, network error) → exit 3,
  **write nothing** (existing `data/resolutions.jsonl` is left unchanged).
- The stored `data/resolutions.jsonl` is read strictly **before** the fetch. A torn last line (no
  closing newline), a blank line, bad JSON or NaN/Infinity, a non-object, another key, or a value
  this module could not have written → exit 3 (history unusable), **nothing fetched and nothing
  appended**. `podlib.read_jsonl` is not used for this file: a line it silently skipped is exactly
  the row a later append would duplicate.
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
- Dedup by `condition_id` (existing file + the current walk). For every condition the first
  snapshot written stays the first row; the one allowed second row is the confirmation upgrade
  below (issue #57). Any other repeat appends nothing.
- `data/` is gitignored; the JSONL is not committed.

Skip reasons (first match): `not_closed`, `missing_closed_ts`, `invalid_closed_ts`,
`future_closed_ts`, `hold_pending`, `missing_condition_id`, `missing_uma_status`,
`missing_outcome_prices`, `non_finite_outcome_prices`, `no_unique_winner`, the
scanner token problems (`missing_version`, `unsupported_version`, …),
`token_winner_mismatch`, `duplicate`. A `condition_id` already in the file adds
`already_confirmed` (the stored row is already confirmed), `duplicate` (an unconfirmed repeat, or a
repeat inside one walk) or one of the `upgrade_*` reasons below.

A 50/50 book, two `1.0`s, or any non-0/1 remainder is `no_unique_winner`.

### Confirmation upgrade (issue #57)

AN's first-write-wins is revised for exactly one case: a `condition_id` whose stored row is the
**unconfirmed** snapshot (`uma_checked` false, `status` `closed`) is confirmed once when a later
walk sees the same settlement as exactly `resolved`.

- One row is appended (after the stored bytes, which are never rewritten) only when all of these
  hold: `umaResolutionStatus` is exactly `resolved` (`uma_checked` true, `status` `resolved`); the
  `winning_token_id`, the `outcome_prices_final` values and the `closed_ts` instant equal the
  stored row's; both `snapshot_ts` values are zoned ISO timestamps; and the new `snapshot_ts` is not
  before the stored one. The confirmation row repeats the stored settlement content and differs
  only in `snapshot_ts`, `status` and `uma_checked`.
- At most one confirmation per `condition_id`. Nothing is appended for a repeated confirmation, a
  status regression (the stored row is confirmed and this walk does not confirm it), an
  unconfirmed repeat, or a repeat inside one walk: the first row of a condition in one walk wins.
- A disagreement about the settlement refuses the upgrade, is counted under its own reason and is
  never merged — the newest winner is never chosen: `upgrade_winner_conflict` (another winning
  token), `upgrade_prices_conflict` (other final prices), `upgrade_closed_ts_conflict` (another
  close time), `upgrade_clock_regression` (a `snapshot_ts` before the stored one). The same names
  report a disagreement with an already confirmed row.
- Every reason is printed in the run's `skipped_first=` line; the run's last line reports
  `written=` and `upgrades=`.
- The consumer rule is unchanged: a join keeps only `uma_checked=true`, so the unconfirmed first row
  is still never a label. Two rows for one condition are now expected after an upgrade; the step 3
  reader (`src/calibration.py`) keeps the first row and counts rows of one condition that differ as
  `resolution_disputed`. Making that reader prefer the confirmed row is a separate change, not part
  of this item.

## Consumers

Calibration joins (step 2+) must keep only `uma_checked=true`. This snapshot still
writes `uma_checked=false` rows when a unique 1/0 winner exists after 48 h, so a
later UMA resolution is not silently treated as a label; since issue #57 that later
resolution is appended as a second, confirmed row (`status=resolved`), never by
rewriting the first one.

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
JSON (nan/inf), a damaged log already on file (not UTF-8, a blank line, a complete line that is
not a JSON object, a row without a usable `forecast_id`, a torn last line), a `forecast_id`
already sealed with different content, an OS error. A row is not written again: the top-level
`forecast_id` of every complete line is parsed and its canonical JSON compared with this row's
(key order and whitespace do not matter; `true` and `1` are different content), and every line
is read before that is decided. The same `forecast_id` with another `category`, `note` or
`end_date` is therefore refused instead of being silently dropped. The forecast is written
**before** the bus signal; if it cannot be written, no signal is sent.

### Signal payload (NewsForecaster -> RiskGate)

Always: `model_prob`, `order: false`, `note`, `forecast_id`, `forecast_ts`, `model_version`.
When the caller gives them (Poly指挥's signal->intent ruling): `outcome_index`, `outcome`,
`token_id`, `condition_id`, `p_low`, `p_high`. A field not given stays absent; the forecast row
lists it in `calibration_problems`. Issue #105: `intent_builder` no longer reads a missing
`outcome_index` as outcome 0. A signal without `outcome_index` is rejected (`missing_outcome_index`),
a NewsForecaster signal without `token_id` is rejected (`missing_token_id`), and only one of
`p_low` / `p_high` is rejected (`incomplete_interval`). Neither bound given is still a point probability. `intent_builder.signal_from_bus` carries `forecast_id`,
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
  not crossed (`bid <= ask`); else null. The calibration report scores the market at this mid
  from Task BC (step 2c).
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

## Step 2c: mid as the market probability, both Kelly values, fill context (Task BC)

- **q = `market_mid`.** The trade sample's market probability is the first fill's `market_mid`
  (`price_source=market_mid`). Design section 3 says "mid if there is one, else `market_price`, with
  `price_source` noted"; that fallback is kept only for a fill booked before Task BA, which has no
  `market_mid` key at all (`price_source=market_price`). A fill that records the key but no usable mid
  (null, non-finite, outside (0, 1)) is never re-scored at `market_price`: it is dropped and counted
  (normally as `fill_calibration_problems` with `missing_market_mid` / `invalid_market_mid`, which
  paper_ledger stores; if the stored list hides it, as `fill_market_mid_missing`). `report.json`
  `trades.price_sources` counts the samples per source, and each trade sample carries `price_source`.
  `price_band` still bands the entry `market_price`.
- **Kelly.** `kelly_fraction_used` stays the shadow multiplier (risk.toml `kelly_fraction`, 0.15).
  New `kelly_frac_raw` = intent_builder's per-trade raw (full) Kelly fraction `kelly_raw =
  edge / (1 - c_all)`, so `kelly_frac = kelly_fraction_used x kelly_frac_raw`. Same rule: a finite
  number in (0, 1], else null + `missing_` / `invalid_kelly_frac_raw`. Recorded only: RiskGate and the
  order size never read it (source scan + identical decisions and fill sizes with any value).
- **`fill_context`.** `risk_gate.Intent.fill_context`, stamped by the caller and copied onto the fill:
  `intent_builder` (`intent_builder.to_risk_intent`; the e2e harness stands in for it), `operator_cli`
  (`python -m src.paper_ledger --market ...`), `gate_check` (self-check and the realistic no-edge check).
  A missing or unknown value stays null and adds `missing_` / `invalid_fill_context`. The report drops
  `operator_cli` / `gate_check` fills as `no_book_context` (before the strategy exclusion, and without
  counting their market-field problems), so genuine missing-data drops stay apart. A fill from before
  Task BC has no `fill_context` key; nothing is inferred from its missing fields, and it is judged as before.

`report.json` keeps `schema_version` 1 and only adds keys (`trades.price_sources`, sample
`price_source` / `fill_context`), so Task BB's reader is unaffected. No new number; no decision,
threshold or size moved.

## Step 3: the offline calibration report (Task AZ)

Design: Poly反思's M3 calibration design, sections 1-3 and 6. Code: `src/calibration.py` (pure
`build_report`, plus a CLI). Read-only on its inputs, no network, no bus message, no halt, no proposal
(step 4), no threshold or trading change; `config/strategies.toml` is not created.

```
python -m src.calibration [--forecasts logs/forecasts.jsonl] [--resolutions data/resolutions.jsonl]
                          [--ledger logs/paper.jsonl] [--out-dir reports/calibration] [--now <zoned ISO>]
```

Writes `reports/calibration/<UTC date>/samples.jsonl`, `report.md` and, last, `report.json` (an older
`report.json` is removed first, so a failed rewrite never leaves one beside partial files). The folder is
git-ignored. Exit codes: `docs/EXIT_CODES.md` (0 written, 2 usage, 3 corrupt input, 4 ledger fails verify,
5 write failed; 3 and 4 write nothing).

### Samples

- **B, forecasts**: the first sealed forecast (by `ts`, then `forecast_id`) per `condition_id`, and only
  one whose `ts` is provably before that market's `resolution.closed_ts`; p =
  `model_prob`, y = 1 when its token won.
- **A, trades**: counted (`ledger_store.counts`), `risk_decision=allow` fills grouped by `decision_id`; at
  most `CALIBRATION_MAX_DECISIONS_PER_MARKET` decisions per market. p = the first fill's `model_prob`, q =
  its `market_mid` (Task BC, step 2c; `market_price` only for a pre-Task BA fill). Expected `E = sum(edge x shares)`; realized
  `R = (bought - sold shares) x y + sell proceeds - buy cost` (fees included).

A label is used only when the resolution row is `status=resolved`, `uma_checked=true`, its prices are one
1.0 and the rest 0.0, and the forecast's `outcome_index` and `token_id` agree with it (the winning index
and `winning_token_id` must point the same way).

Issue #70 (no result leakage): a forecast sample must be sealed **strictly before** `resolution.closed_ts`.
Both times are zoned ISO strings parsed to UTC (`_parse_ts`) before the comparison, so the same instant
written in any offset is the same moment. A forecast whose time cannot be proved (its `ts` does not parse
as a zoned ISO time) or that was sealed at the close or after it is dropped and counted
(`forecast_ts_invalid`, `forecast_not_before_close`); nothing is imputed and an unprovable row never takes
the market's first-forecast slot.

### Dropped and counted, never imputed

Each reason is counted in `report.json` (`dropped_by_reason`) and every dropped row is in `samples.jsonl`
with its reason.

| Reason | Sample | When |
|---|---|---|
| `no_book_context` | A | Task BC: `fill_context` is `operator_cli` or `gate_check` (checked first) |
| `excluded_strategy` / `excluded_market` | A, B | phase0-gate-test, no-edge-check, wash-probe; `phase0-self-check*`, `e2e-*` |
| `forecast_calibration_problems` | B | the stored list or a recomputed `forecast_problems` is non-empty (also counted per problem) |
| `forecast_ts_invalid` | B | issue #70: its `ts` does not parse as a zoned ISO time (unparseable, or no timezone), so when it was sealed cannot be proved (a blank / missing `ts` is `missing_ts` under `forecast_calibration_problems`) |
| `forecast_not_before_close` | B | issue #70: its `ts` is not strictly before `resolution.closed_ts` (sealed at the close or after it, the same instant in any offset counting as equal; a close that cannot be parsed is unproven too) |
| `duplicate_market_forecast` | B | not the first forecast for its market |
| `fill_missing_identity` | A | a fill booked before Task AX (no `calibration_problems`) |
| `fill_calibration_problems` | A | stored or recomputed `fill_problems` (plus a missing / unknown stored `fill_context`) non-empty (counted per problem) |
| `decision_inconsistent` | A | fills of one decision_id disagree on forecast, token, condition or outcome |
| `fill_forecast_not_found` / `fill_forecast_mismatch` | A | its forecast_id is not on file / points at another market |
| `resolution_pending` | A, B | no resolution row for the condition |
| `resolution_not_uma_resolved` | A, B | `status=closed` (UMA not confirmed) |
| `resolution_ambiguous_status` | A, B | any other status |
| `resolution_disputed` | A, B | two rows for one condition disagree |
| `resolution_void` | A, B | prices are not exactly one 1.0 and the rest 0.0 |
| `outcome_index_out_of_range` / `token_outcome_mismatch` | A, B | the outcome cannot be matched to the snapshot |
| `re_entry_cap` | A | a third or later decision on one market |
| `fill_market_mid_missing` | A | Task BC: the first fill records `market_mid` but it is not a usable price |

Corrupt input is an error exit, not a partial report: a torn last line, a blank line, non-UTF-8, bad JSON or
NaN, a non-object, keys other than `FORECAST_ROW_FIELDS` / the resolutions schema, a wrong type, or a
`forecast_id` repeated with different content (exit 3); a ledger that fails `ledger_store.verify` (exit 4).
An absent file is empty input (recorded as `present: false`), and the section then says `insufficient`.

### Metrics

- B: Brier, log loss (p clipped to `CALIBRATION_LOGLOSS_CLIP`), base rate, reliability bins (fixed
  `CALIBRATION_BIN_EDGES` from `CALIBRATION_FIXED_BINS_MIN_SAMPLES`, else `CALIBRATION_EQUAL_FREQ_BINS`
  equal-frequency bins of at least `CALIBRATION_EQUAL_FREQ_MIN_PER_BIN`) with Wilson intervals, ECE,
  logistic calibration slope / intercept (`y ~ a + b logit(p)`, Newton; `None` when not estimable).
  Groups: category, strategy, model_version.
- A: Brier of p and of q, delta and Brier skill, log loss of both, E, R, realization `sum R / sum E`,
  mean(R - E), re-entries. Groups: category, strategy, model_version, side, edge tier
  (`CALIBRATION_EDGE_TIERS`), hours to expiry at the first fill (`CALIBRATION_EXPIRY_TIERS_H`), price band.
- Intervals: clustered bootstrap (Task BG: by event, else market; `CALIBRATION_BOOTSTRAP_RESAMPLES`, seed
  `CALIBRATION_BOOTSTRAP_SEED`, level `CALIBRATION_CI_LEVEL`) for Brier, slope, delta Brier, skill, R and
  mean(R - E). The report lists how many groups it shows.

### Minimums and verdicts (M3 pending)

A section needs `CALIBRATION_MIN_SAMPLES` (30) samples from `CALIBRATION_MIN_MARKETS` (20) markets over
`CALIBRATION_MIN_RESOLUTION_DAYS` (5) UTC days of `closed_ts`; a group needs `CALIBRATION_GROUP_MIN_SAMPLES`
(10) from `CALIBRATION_GROUP_MIN_MARKETS` (5) markets. Below that the status is `insufficient`, the verdict
is the string `insufficient`, no interval is computed and a group shows no Brier. Above it, verdicts read
intervals only: B `calibration_slope` = `overconfident` (slope CI below 1), `underconfident` (above 1),
`no_clear_miscalibration`, or `slope_not_estimable`; A `model_vs_market` = `worse_than_market` (delta Brier
CI above 0), `better_than_market` (below 0), `inconclusive`, or `not_estimable`. These are observations, not
proposals. Every report states "UMA dispute status not checked" and "Not a promise of profit".

### Group intervals, intercept CI and event-cluster share (Task BD)

- `forecasts.by_category`, `trades.by_category` and `trades.by_edge_tier`: every group gains `intervals`.
  A group at the group minimums gets `{brier, brier_ci, ece, ece_ci, slope, slope_ci, intercept,
  intercept_ci}` (model p against y; same clustered bootstrap, seed and level as above); trade groups
  add `brier_market`, `delta_brier`, `delta_brier_ci` (model - market) and `realization`, `realization_ci`
  (sum R / sum E, None when sum E <= 0). A group below them gets `intervals: null`; nothing is computed from it.
  Other group keys (strategy, model_version, side, expiry tier, price band) are unchanged.
- A sufficient B section adds `calibration.intercept_ci`, `ece_ci` and the verdict key
  `calibration_intercept` = `positive_bias` (CI above 0: outcomes happen more often than p says),
  `negative_bias` (CI below 0), `no_clear_bias`, or `intercept_not_estimable`. An insufficient section adds
  none of them (the verdict stays the string `insufficient`).
- Both sections add `event_cluster`: `{cluster_key, n, n_clusters, largest_cluster, largest_cluster_n,
  largest_share, max_share, status, flag}`. `flag` is `insufficient` when the section is below its minimums,
  else `concentrated` when `largest_share` > `CALIBRATION_MAX_CLUSTER_SHARE` (0.30, design 3, M3 pending),
  else `ok`. Ties go to the smallest name. Before Task BF no event id was recorded, so `cluster_key` was
  `condition_id` (one market per cluster) and the share a lower bound; see the Task BF subsection below.
  Reported only; no verdict, proposal or gate reads it yet.
- `report.json` stays `schema_version` 1 and only gains keys; `thresholds.max_cluster_share` is listed.

### Event id and the event-cluster key (Task BF)

- Source: Gamma market rows carry an `events` list (the scanner keyset already sends `include_tag=true`).
  `calibration_ids.scanner_event(market)` gives `(event_id, event_slug)`: the one event id every usable entry
  (an object whose `id` is a non-blank string or an int) names, and the first usable slug of that event.
  No `events` list, no usable entry, or entries naming different events -> `(None, None)`; never guessed and
  never an error. `calibration_ids.event_value` is the shared rule (non-blank string stripped, int as its
  decimal string, anything else None).
- Recorded on: scanner rows (`data/markets.jsonl` `event_id`, `event_slug`) -> `MarketContext` ->
  `OrderIntent` -> `risk_gate.Intent` -> paper fills (`FILL_EVENT_FIELDS`, next to the Task BA market fields),
  and `logs/forecasts.jsonl` rows (`FORECAST_EVENT_FIELDS`, the last two keys of `FORECAST_ROW_FIELDS`;
  `emit_probability(event_id=, event_slug=)`). Not part of `forecast_id`, not in the signal payload, never a
  `calibration_problems` entry and never a join field. RiskGate, sizing and every gate never read them.
- Old rows stay valid: a forecast row without the two event keys (written before BF) is read as before; an
  event key with an unusable value is read as no event. A fill without them has no event.
- `event_cluster`: a sample's cluster is its `event_id` when usable, else its `condition_id` (per sample). A
  trade sample takes its first fill's `event_id`, else its joined forecast's; when both are usable and
  differ it falls back to `condition_id`. `cluster_key` is `event_id` (every sample has one), `condition_id`
  (none has; also an empty section) or `mixed`; `event_id_samples` and `condition_id_fallback_samples` count
  them. With `condition_id` or `mixed`, markets of one event without an event id still count separately, so
  the share is a lower bound. Same flag rule and the same `CALIBRATION_MAX_CLUSTER_SHARE` (no new number).
- The bootstrap intervals (Task BD) stayed market-clustered (`condition_id`) in BF; Task BG clusters them by
  event (below). The markdown cluster line names the key and the fallback count. Reflector proposals (Task BE) carry `cluster_key` through unchanged.

### Event-clustered bootstrap intervals (Task BG)

- Every bootstrap interval (overall Brier, ECE, slope, intercept; trade delta Brier, skill, R, mean(R - E);
  every judged group's `intervals`) resamples the same clusters as `event_cluster`: a sample's `event_id` when
  usable, else its `condition_id`, per sample. Markets of one event are drawn together. An event id and a
  condition id with the same text are different clusters. Fewer than two clusters: the interval is `null`.
- `forecasts.metrics`, `trades.metrics` and every judged group's `intervals` gain `bootstrap_cluster_key`
  (`event_id` / `condition_id` / `mixed`, BF's convention) and `bootstrap_clusters` (clusters resampled); the
  markdown cluster line names both. Same seed, resample count and level; samples without an event id (rows
  from before BF) give exactly the intervals they gave before. With `condition_id` or `mixed`, markets of one
  event without an id are still drawn apart, so those intervals can be too narrow.
- No new number; report.json schema_version 1, keys only added; Reflector (BE) reads the intervals unchanged.

## Step 4: structured Reflector proposals (Task BB)

Design: Poly反思's M3 calibration design, sections 4 and 5. Code: `src/reflector_proposals.py`;
`reflector.propose` gains an optional `payload` and every Reflector bus proposal now says
`exchange_action: none`.

```
python -m src.reflector_proposals [--report reports/calibration/<date>/report.json] [--jsonl playbook/proposals.jsonl]
                                  [--md playbook/proposals.md] [--bus logs/bus.jsonl] [--now <zoned ISO>]
```

The report is read-only input (default: the newest dated folder). Each proposal is one row in
`playbook/proposals.jsonl`, one `## <UTC ts>` summary block in `playbook/proposals.md` (written by
`reflector.propose`, so `REFLECTOR_MAX_PER_UTC_DAY` counts it) and one bus message
`Reflector -> Operator`, type `proposal`, with `exchange_action: none` and the row under `proposal`.

### Proposal schema (`PROPOSAL_FIELDS`, keys in this order)

| Key | Rule |
|---|---|
| `schema_version` | `1` |
| `proposal_id` | `<UTC date>-NN`, NN = the day's Reflector proposal count + 1 |
| `created_at` | UTC `Z` |
| `type` | `disable` or `param_change` |
| `target` | `{strategy, key, file[, price_band]}`; `file` is `src/thresholds.py` or null (a disable names no file: no strategy switch exists and `config/strategies.toml` is not created) |
| `current_value`, `proposed_value` | e.g. `0.02` -> `0.03`, `enabled` -> `disabled` |
| `direction` | `tighten` / `loosen`, computed from the key (`MIN_EDGE`: higher is tighter; `enabled`: disabled is tighter; any other key counts as loosen) |
| `loosens_risk_check` | true when `direction` is `loosen` |
| `approval` | `{required: true, required_from: "KEY" if it loosens else "Operator", note}` |
| `evidence` | `report` {path, sha256, generated_at}; `sections` (report refs, e.g. `forecasts.metrics.calibration.slope_ci`); `sample` {section, n, n_markets, resolution_days}; `metrics`; `data_versions` (input sha256s, bootstrap seed) |
| `confidence` | `{ci_level, intervals}`: the intervals the rule read |
| `rationale`, `expected_impact`, `risk`, `rollback` | text |
| `scope` | `{env: paper, live_requires_human_approval: true}` |
| `status` | `{state: proposed, approver: null, decided_at: null, applied_at: null}`; humans move it to approved / rejected / applied |
| `exchange_action` | `none` |
| `applied_by_code` | `false` |

### Rules (tightening only; intervals must be wholly past the threshold)

- `disable` (one strategy; the report has no per-strategy interval, so only when the section holds a single
  strategy): trade Brier-skill interval below 0; or realized P&L interval below 0 with realization below
  `REFLECTOR_DISABLE_REALIZATION_MAX`; or calibration slope below `REFLECTOR_DISABLE_SLOPE_MAX` with its
  interval upper bound below `REFLECTOR_DISABLE_SLOPE_CI_HIGH_MAX`. One proposal per strategy, all reasons in it.
- `param_change` MIN_EDGE -> MIN_EDGE + `REFLECTOR_MIN_EDGE_STEP`: slope in [`REFLECTOR_DISABLE_SLOPE_MAX`,
  `REFLECTOR_TIGHTEN_SLOPE_MAX`) with interval upper bound below `REFLECTOR_TIGHTEN_SLOPE_CI_HIGH_MAX`; and,
  for the worst price band, ECE above `REFLECTOR_ECE_MAX` with a judged bin outside its Wilson interval. Not
  for a strategy being disabled the same day.
- Task BE, `disable` one category (`target.category`, e.g. `{strategy, key: enabled, file: null, category:
  Politics}`): a judged `trades.by_category` group's `intervals.delta_brier_ci` lies wholly above 0 (model worse
  than market). Scope is the single trade strategy, else `all`; skipped when the strategy itself is disabled;
  `(unknown)` never.
- Task BE, `param_change` MIN_EDGE by edge tier (`target.edge_tier`): starting at the trade edge tier that holds
  MIN_EDGE (labels from `thresholds.edge_tiers`), each judged tier whose `intervals.realization_ci` lies wholly
  below `REFLECTOR_EDGE_TIER_REALIZATION_MAX` (0.25, M3 pending) extends a run; MIN_EDGE is proposed at the top
  of the highest bounded tier of the run (0.02 -> 0.04 for `[0.02, 0.04)`). A failing tier above a passing or
  unjudged one is an observation only (raising MIN_EDGE past it would cut the good tier), and so is a failing
  open top tier (`>=0.08`) beyond the last bound.
- Task BE, intercept: `forecasts.metrics.calibration.intercept_ci` wholly above 0 (positive bias) or below 0
  (negative bias) adds its reason, ref and interval to the MIN_EDGE + `REFLECTOR_MIN_EDGE_STEP` proposal, or
  makes that proposal when the slope rule does not fire. Not for a strategy being disabled.
- Task BE, event-cluster share: every proposal's `evidence.event_cluster` = `{cluster_key, flag, largest_share,
  max_share}` of its section (null for a report before BD); a `concentrated` section is stated in the
  proposal's `risk` and in the run's observations. It gates nothing. Since Task BF `cluster_key` may be
  `event_id`, `condition_id` or `mixed` (the report's key, carried through as is).
- Order under the daily cap: strategy disables, category disables, then MIN_EDGE (slope / intercept), band and
  edge-tier tightenings. A report from before BD (no group `intervals`, no `intercept_ci`, no
  `event_cluster`) gives no new proposal; a wrongly typed new field is a corrupt report (exit 4).
- Not implemented yet: the two-batch Kelly rule (`kelly_fraction` is in config/risk.toml, which no proposal may
  name, and the rule concerns raising Kelly, a loosening for KEY) and the cumulative-loss disable (its
  threshold is not in the repo). Both need Poly反思's numbers and a decision on where they live.

### Limits

- A section with status `insufficient` gives no proposal (both insufficient = no write at all).
- At most `REFLECTOR_MAX_PER_UTC_DAY` (3) Reflector proposals per UTC day (08:00 Beijing); disable first, the
  rest are reported as deferred.
- A target already `proposed`/`approved` is skipped; a `rejected` one waits `REFLECTOR_REJECT_COOLDOWN_DAYS`
  unless its sample grew by `REFLECTOR_REJECT_NEW_SAMPLES`.
- `validate` refuses a row that acts on an exchange, says it was applied by code, leaves paper scope, names a
  file outside `src/thresholds.py`, or loosens without `required_from: KEY`.
- No write to config/ (risk.toml, a strategy switch), any `.toml` or `src/thresholds.py`
  (`reflector.refused_path`); nothing in the repo reads proposals.jsonl to apply it (tested).
- Fail closed: missing report exit 3, corrupt / incomplete report exit 4, corrupt proposals.jsonl exit 5,
  write failure exit 6; none of them writes a proposal (`docs/EXIT_CODES.md`).

## Out of scope here

Applying a proposal (always a human, in a separate task), bus fills, new URL hosts, any
`config/risk.toml` edit, live trading.
