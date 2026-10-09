# Thresholds

Every risk and threshold number in Phase 0, in one place (Task AC, backlog #17).
Code constants live in `src/thresholds.py`; the modules import them from there and keep their old
names as re-exports. `config/risk.toml` keys stay in that file and are listed here read-only.
`tests/test_thresholds.py` parses the tables below and fails if a name, value or re-export drifts
from the code or the file. Changing a value is a threshold change: rows marked M2 `yes` are
proposals waiting for the user's decision at M2. Task AC moved no value and changed no behaviour.

## Code constants (`src/thresholds.py`)

| Name | Value | Unit | Meaning | Where | Source | M2 pending |
|---|---|---|---|---|---|---|
| `MAX_BOOK_AGE_S` | `120.0` | s | A book whose exchange `book_ts` is older than this (or missing, unparseable, future) is stale: RiskGate vetoes, paper_cycle sends no gap signal. Exactly 120 s is fresh. | `src/thresholds.py`; re-exported as `book_staleness.MAX_BOOK_AGE_S`, `risk_gate.MAX_BOOK_AGE_S`, `sentinel.MAX_BOOK_AGE_S` | Task L (1a92216), backlog #2 | yes |
| `MIN_EDGE` | `0.02` | price | Minimum edge after slippage and fees; `0 < edge < 0.02` vetoes `edge_below_min`. Inclusive; a config `min_edge` cannot lower it. | `src/thresholds.py`; re-exported as `risk_gate.MIN_EDGE` | Task X (b225337), backlog #6 | yes |
| `EDGE_TOLERANCE` | `1e-12` | price | Float rounding allowance for the `MIN_EDGE` comparison only. | `src/thresholds.py`; re-exported as `risk_gate.EDGE_TOLERANCE` | Task X (b225337) | no |
| `PAPER_EQUITY_USD` | `1000.0` | USD | Local paper bankroll: order size is `max_order_pct` of it, daily loss is measured against it. Also the starting cash of the paper-ledger conservation check (Task AF). Not a wallet. | `src/thresholds.py`; re-exported as `podlib.PAPER_EQUITY_USD`, `risk_gate.PAPER_EQUITY_USD`, `maker.PAPER_EQUITY_USD` | Phase 0 base (65a08e1) | no |
| `PAPER_CYCLE_STALE_AFTER_S` | `120` | s | paper_cycle's per-check stale limit (BookWatcher stale signal, no gap). Must equal `MAX_BOOK_AGE_S`. | `src/thresholds.py`; re-exported as `paper_cycle.STALE_AFTER_S` | Task D (5b697f1), aligned in Tasks R/T | no (follows `MAX_BOOK_AGE_S`) |
| `SENTINEL_STALE_S` | `120.0` | s | Sentinel's per-check stale limit for StaleWatch. Must equal `MAX_BOOK_AGE_S`; callers may only make it stricter. | `src/thresholds.py`; re-exported as `sentinel.SENTINEL_STALE_S` | Task Z (PR #28, 46f58b6), backlog #9 | yes |
| `SENTINEL_STALE_CONSECUTIVE` | `2` | checks | Consecutive stale or failed checks before Sentinel halts that market's paper quotes (once per streak). | `src/thresholds.py`; re-exported as `sentinel.SENTINEL_STALE_CONSECUTIVE` | Task Z (PR #28, 46f58b6), backlog #9 | yes |
| `WASH_WINDOW_S` | `300.0` | s | An own opposite paper fill on the same market within this window vetoes `wash_same_market`. | `src/thresholds.py`; re-exported as `wash_detect.WASH_WINDOW_S` | Task S (1dc5ec3), backlog #1 | yes |
| `MIN_HOURS_TO_EXPIRY` | `24.0` | h | A market must end at least this long after now, or it fails `near_expiry` (scanner, endgame_scan, paper_session). | `src/thresholds.py`; re-exported as `expiry_rule.MIN_HOURS_TO_EXPIRY`, `scanner.MIN_HOURS_TO_EXPIRY`, `endgame_scan.MIN_HOURS_TO_EXPIRY` | Task O (d91959d), backlog #8 | yes |
| `SCANNER_MIN_PRICE` | `0.03` | price | Long-tail band floor: a YES price below it fails `long_tail_price`. Default for the optional risk.toml key `scanner_min_price` (not in the file). | `src/thresholds.py`; re-exported as `scanner.MIN_PRICE` | Task I (PR #2, 63c9e82) | no |
| `SCANNER_MAX_PRICE` | `0.97` | price | Long-tail band ceiling: a YES price above it fails `long_tail_price`. Default for the optional risk.toml key `scanner_max_price` (not in the file). | `src/thresholds.py`; re-exported as `scanner.MAX_PRICE` | Task I (PR #2, 63c9e82) | no |
| `MIN_SCANNED_MARKETS` | `5` | markets | Fewer scanned markets means a broken public read: scanner exits 1, endgame_scan exits 1, paper_session writes nothing (`too_few_markets`). | `src/thresholds.py`; re-exported as `scanner.MIN_SCANNED_MARKETS`, `endgame_scan.MIN_SCANNED_MARKETS`, `paper_session.SESSION_MIN_SCANNED` | Task I (63c9e82); Task W (PR #25); Task AB (PR #30) | no |
| `ENDGAME_PRICE_MIN` | `0.98` | price | Endgame band floor (inclusive). | `src/thresholds.py`; re-exported as `endgame.PRICE_MIN` | Task F (ea1a84d); Task W (PR #25) | no |
| `ENDGAME_PRICE_MAX` | `0.99` | price | Endgame band ceiling (inclusive). | `src/thresholds.py`; re-exported as `endgame.PRICE_MAX` | Task F (ea1a84d); Task W (PR #25) | no |
| `ENDGAME_WINDOW_HOURS` | `48.0` | h | Endgame signals only markets ending within this many hours. With `MIN_HOURS_TO_EXPIRY` the endgame_scan window is 24–48 h. | `src/thresholds.py`; re-exported as `endgame.WINDOW_HOURS` | Task F (ea1a84d) | no |
| `SPORTS_DEVIG_ASSUMED_SLIPPAGE` | `0.005` | price | SportsDevig signals only if fair probability exceeds the price plus this slippage. | `src/thresholds.py`; re-exported as `sports_devig.ASSUMED_SLIPPAGE` | Task H (26667aa) | no |
| `LEDGER_OPEN_EPS` | `1e-09` | shares | A leg with at most this many open shares counts as closed (position count). Also the paper-ledger conservation tolerance (Task AF: cash, leg shares and cost basis per fill). | `src/thresholds.py`; re-exported as `paper_ledger.OPEN_EPS` | Task P (bbde4e2) | no |
| `REFLECTOR_MAX_PER_UTC_DAY` | `3` | proposals/day | Reflector writes at most this many proposals per UTC day. | `src/thresholds.py`; re-exported as `reflector.MAX_PER_UTC_DAY` | Task B (bd06620) | no |
| `WS_BACKOFF_BASE_S` | `1.0` | s | WebSocket recorder reconnect backoff base: `base * 2**attempt`, reset after a healthy connection. Operational, not a risk threshold. | `src/thresholds.py` | Task AG | no (operational) |
| `WS_BACKOFF_CAP_S` | `60.0` | s | Cap on the WebSocket recorder reconnect backoff. Operational, not a risk threshold. | `src/thresholds.py` | Task AG | no (operational) |
| `WS_RESYNC_TIMEOUT_S` | `30.0` | s | An asset waiting this long for a fresh snapshot (after subscribe or a detected gap) makes the recorder reconnect. Also the connect timeout. A waiting asset writes no priced rows: only the one price-less invalidation row written when it left live (already stale), so this never makes a book look fresh. Operational. | `src/thresholds.py` | Task AG | no (operational) |
| `WS_PING_INTERVAL_S` | `10.0` | s | Application heartbeat: the text frame `PING` this often (Polymarket market-channel docs). Operational. | `src/thresholds.py` | Task AG, docs.polymarket.com | no (operational) |
| `WS_ROW_REFRESH_S` | `30.0` | s (exchange time) | A live book with an unchanged top still gets a new row once the exchange time of its latest applied event is this far past the last row, so `book_ts` keeps up with a live stream. Rows lag the stream by at most this, never lead it. Staleness-relevant; must stay below `MAX_BOOK_AGE_S`. | `src/thresholds.py` | Task AG | yes |
| `WS_STOP_POLL_S` | `1.0` | s | With a stop callback (the M1 run's SIGINT/SIGTERM handling) the recorder checks it at least this often, also while backing off. Operational; must stay <= `WS_PING_INTERVAL_S`. | `src/thresholds.py` | Task AI | no (operational) |
| `M1_TOP_MARKETS` | `5` | markets | Default `src.m1_run --top`: markets taken from the latest scanner batch (24h volume order), both outcome tokens each. Decides what is recorded, not what passes. | `src/thresholds.py` | Task AI | no (operational) |
| `M1_ROTATE_BYTES` | `67108864` | bytes | An M1 run JSONL file (books, events, rest) is rotated before it would pass 64 MiB (`--rotate-mb`). | `src/thresholds.py` | Task AI | no (operational) |
| `M1_ROTATE_S` | `3600.0` | s | ... or once it has been open this long (`--rotate-s`). | `src/thresholds.py` | Task AI | no (operational) |
| `M1_REST_EVERY_S` | `600.0` | s | The M1 run reads the public CLOB `/book` for the REST-vs-WS comparison at start and then this often (`--rest-every`). | `src/thresholds.py` | Task AI | no (operational) |
| `M1_REST_ASSETS` | `4` | assets | ... for the first N recorded assets (`--rest-assets`). | `src/thresholds.py` | Task AI | no (operational) |
| `M1_REPORT_STEP_S` | `1.0` | s | `src.m1_report` timeline resolution: coverage and book age are evaluated at every step of the run window. Must be <= `WS_ROW_REFRESH_S`. | `src/thresholds.py` | Task AI | no (operational) |
| `M1_MIN_RUN_S` | `86340.0` | s | M1 acceptance: the run lasted at least 24 h minus 1 min, ended by its deadline (not a signal) with exit 0. Conservative first value. | `src/thresholds.py` | Task AI | yes |
| `M1_MIN_COVERAGE` | `0.99` | fraction of run | M1 acceptance: every asset had a fresh book (`0 <= t - book_ts <= MAX_BOOK_AGE_S`, the `book_age_problem` rule) for at least this share of the run. Each asset's p99 book age must also be <= `MAX_BOOK_AGE_S`. Conservative first value. | `src/thresholds.py` | Task AI | yes |
| `M1_MAX_RECONNECTS` | `48` | connect attempts | M1 acceptance: connect attempts after the first (reconnects plus failed connects) over the run, at most 2 per hour. Conservative first value. | `src/thresholds.py` | Task AI | yes |
| `M1_MAX_GAP_RESYNCS_PER_ASSET` | `24` | resyncs/asset | M1 acceptance: resyncs not caused by a disconnect (gaps, regressions, invalid data, a non-JSON frame) per asset over the run. Conservative first value. | `src/thresholds.py` | Task AI | yes |
| `M1_REST_MATCH_WINDOW_S` | `30.0` | s (exchange time) | A REST `/book` sample is compared with the live WS row whose exchange `book_ts` is nearest, if within this; otherwise it is unmatched. Must be <= `WS_ROW_REFRESH_S`. | `src/thresholds.py` | Task AI | yes |
| `M1_MIN_REST_MATCHED` | `50` | samples | M1 acceptance: at least this many REST samples compared (a full default run takes about 580). Conservative first value. | `src/thresholds.py` | Task AI | yes |
| `M1_MAX_TOP_MISMATCH_RATE` | `0.02` | fraction | M1 acceptance: compared REST samples whose best bid or best ask differs from the WS row. Conservative first value. | `src/thresholds.py` | Task AI | yes |
| `M1_MAX_REST_UNMATCHED_RATE` | `0.1` | fraction | M1 acceptance: REST samples with no live WS row within `M1_REST_MATCH_WINDOW_S` (or an unusable REST `book_ts`). Conservative first value. | `src/thresholds.py` | Task AI | yes |
| `RECONCILE_SHARES_TOL` | `1e-06` | shares | Reconciler (Task AK): ledger open shares vs Data API v2 `current_size`, and ledger buy/sell shares vs v2 BUY/SELL trade sizes, per token, may differ by at most this (one base unit of a 6-decimal token); more is a mismatch (exit 1). Must be >= `LEDGER_OPEN_EPS`. | `src/thresholds.py` (used by `src/reconcile.py`) | Task AK | yes |
| `DATA_API_POSITIONS_DUST_SHARES` | `0.1` | shares | Data API v2 `/v2/positions` default dust floor (`filter_type=TOKENS`), sent explicitly as `filter_amount`. An open ledger leg below it cannot be verified: `position_below_data_api_floor` (fails). Contract fact, not a choice. | `src/thresholds.py` (used by `src/reconcile.py`) | Task AK, docs.polymarket.com Data API v2 openapi | yes |
| `DATA_API_TRADES_MIN_SHARES` | `0.01` | shares | Data API v2 `/v2/trades` smallest served fill (`filter_amount` default; 0 means the same). A ledger fill below it cannot be verified: `ledger_fill_below_data_api_trade_floor` (fails). Contract fact, not a choice. | `src/thresholds.py` (used by `src/reconcile.py`) | Task AK, docs.polymarket.com Data API v2 openapi | yes |
| `DATA_API_MAX_LAG_S` | `120.0` | s | Reconciler reads `/v2/status` first each round; snapshot `age_seconds` + `serving.lag_seconds` above this (or either missing/null, or the chain not polygon/137) fails closed (`status_lag_too_large`, exit 3). | `src/thresholds.py` (used by `src/reconcile.py`) | Task AK, Poly研究 research_dataapi_ws_m2.md | yes |
| `DATA_API_IDENTITY_TOL_USDC` | `1e-06` | USDC | Each `/v2/positions` row must satisfy the documented identities (`current_value = current_size * current_price`, `unrealized_pnl = current_value - entry_cost_usdc`, `total_pnl = realized_pnl + unrealized_pnl`, `total_cost_usdc = entry_cost_usdc + entry_fees_usdc`) within this, or it is malformed (exit 3). Live rows on 2026-10-09 were within 1.2e-10. | `src/thresholds.py` (used by `src/reconcile.py`) | Task AK, Poly研究 research_dataapi_ws_m2.md | yes |
| `INTENT_ASSUMED_SLIPPAGE` | `0.005` | price | Signal -> OrderIntent: slippage added to the book-walk price before the edge counts (`edge = p_side - (exec_price + slippage) - fee`); passed to RiskGate as `assumed_slippage`, so both compute the same edge. | `src/thresholds.py` (used by `src/intent_builder.py`) | Task AO, SPEC_signal_to_intent 2.1 / 7-2 | yes |
| `INTENT_SHARE_STEP` | `0.01` | shares | OrderIntent shares are rounded down to a multiple of this; below the book's `min_order_size` is `size_below_min_order` (never rounded up). | `src/thresholds.py` (used by `src/intent_builder.py`) | Task AO, SPEC 3 | no (rounding) |
| `INTENT_TTL_S` | `60.0` | s | An OrderIntent expires at the earliest of `now + INTENT_TTL_S`, `book_ts + MAX_BOOK_AGE_S` and `end_date - MIN_HOURS_TO_EXPIRY`. Must be <= `MAX_BOOK_AGE_S`. | `src/thresholds.py` (used by `src/intent_builder.py`) | Task AO, SPEC 4 | yes |
| `MAX_SIGNAL_AGE_NEWS_S` | `3600.0` | s | A NewsForecaster (or forecaster_override) signal older than this is `stale_signal`; missing, unparseable or future `signal_ts` also rejects. | `src/thresholds.py` (used by `src/intent_builder.py`) | Task AO, SPEC 1.3 | yes |
| `MAX_SIGNAL_AGE_SPORTS_DEVIG_S` | `300.0` | s | Same for a SportsDevig signal. A blend uses the smallest of the three limits. | `src/thresholds.py` (used by `src/intent_builder.py`) | Task AO, SPEC 1.3 | yes |
| `MAX_SIGNAL_AGE_ENDGAME_CONFIRM_S` | `600.0` | s | Same for an `endgame_confirm` probability (a raw Endgame signal has none: `missing_model_prob`). | `src/thresholds.py` (used by `src/intent_builder.py`) | Task AO, SPEC 1.3 | yes |
| `MAX_SOURCE_LOGIT_GAP` | `1.0` | log-odds | Two model sources for one outcome whose point estimates differ by more than this are `source_conflict` (no blend, no intent). | `src/thresholds.py` (used by `src/intent_builder.py`) | Task AO, SPEC 1.3 | yes |
| `RESOLUTION_HOLD_HOURS` | `48.0` | h | A closed market is snapshotted only after it has been closed this long (`closedTime`). Exactly 48 h passes. Same number as `ENDGAME_WINDOW_HOURS` by coincidence; they are not coupled. | `src/thresholds.py`; re-exported as `resolutions.RESOLUTION_HOLD_HOURS` | Task AN | no |
| `SECRET_VALUE_MIN_LEN` | `16` | chars | Secret scan (Sentinel gap 9): a value after a credential key name (`private_key=`, `"api_secret": ...`, `Authorization: Bearer ...`) is a secret when it has at least this many token characters with a letter and a digit. Polymarket keys, API secrets and passphrases are all longer. Must be in [8, 32]. | `src/thresholds.py` (used by `src/secret_scan.py`) | Task AQ, Poly哨兵 sentinel_design.md (event matrix) | yes |
| `MNEMONIC_MIN_WORDS` | `12` | words | This many consecutive BIP39 English words on one line is a mnemonic (12, 15, 18, 21 and 24-word phrases all hit). Must be in [3, 12], never above the shortest standard phrase. | `src/thresholds.py` (used by `src/secret_scan.py`) | Task AQ, sentinel_design.md (12/24 BIP39 words) | yes |
| `SECRET_HASH_PREFIX_CHARS` | `8` | hex chars | A finding reports this many hex characters of the matched value's sha256 (to tell hits apart; the text is never echoed). Must be in [4, 12]. | `src/thresholds.py` (used by `src/secret_scan.py`) | Task AQ, sentinel_design.md (哈希前缀) | yes |

Relations checked when `src/thresholds.py` is imported (an explicit `RuntimeError`, not `assert`):
`SENTINEL_STALE_S == MAX_BOOK_AGE_S`, `PAPER_CYCLE_STALE_AFTER_S == MAX_BOOK_AGE_S`,
`SENTINEL_STALE_CONSECUTIVE` a whole number >= 1, `0 <= SCANNER_MIN_PRICE < SCANNER_MAX_PRICE <= 1`,
`SCANNER_MAX_PRICE < ENDGAME_PRICE_MIN <= ENDGAME_PRICE_MAX < 1`, `0 < MIN_HOURS_TO_EXPIRY < ENDGAME_WINDOW_HOURS`,
`0 < WS_BACKOFF_BASE_S <= WS_BACKOFF_CAP_S`, `0 < WS_PING_INTERVAL_S < WS_RESYNC_TIMEOUT_S`, `0 < WS_ROW_REFRESH_S < MAX_BOOK_AGE_S`,
`0 < WS_STOP_POLL_S <= WS_PING_INTERVAL_S`; M1 (Task AI): `M1_TOP_MARKETS`, `M1_ROTATE_BYTES`, `M1_REST_ASSETS`,
`M1_MIN_REST_MATCHED` whole numbers >= 1, `M1_MAX_RECONNECTS` and `M1_MAX_GAP_RESYNCS_PER_ASSET` whole numbers >= 0,
`M1_ROTATE_S`, `M1_REST_EVERY_S`, `M1_MIN_RUN_S` > 0, `0 < M1_REPORT_STEP_S <= WS_ROW_REFRESH_S`, `0 < M1_MIN_COVERAGE <= 1`,
both M1 rates in `[0, 1)`, `0 < M1_REST_MATCH_WINDOW_S <= WS_ROW_REFRESH_S`, and a full run can collect enough REST
samples: `M1_MIN_RUN_S / M1_REST_EVERY_S * M1_REST_ASSETS >= M1_MIN_REST_MATCHED`; reconcile (Task AK):
`0 < RECONCILE_SHARES_TOL < DATA_API_TRADES_MIN_SHARES <= DATA_API_POSITIONS_DUST_SHARES` and
`LEDGER_OPEN_EPS <= RECONCILE_SHARES_TOL`, `DATA_API_MAX_LAG_S > 0`, `DATA_API_IDENTITY_TOL_USDC > 0`, `RESOLUTION_HOLD_HOURS > 0` (not equal to `ENDGAME_WINDOW_HOURS`; the shared 48 is coincidence); intent builder (Task AO): `0 <= INTENT_ASSUMED_SLIPPAGE < 1`, `0 < INTENT_SHARE_STEP <= 1`, `0 < INTENT_TTL_S <= MAX_BOOK_AGE_S`, every `MAX_SIGNAL_AGE_*_S > 0`, `MAX_SOURCE_LOGIT_GAP > 0`; secret scan (Task AQ): `SECRET_VALUE_MIN_LEN` a whole number in [8, 32], `MNEMONIC_MIN_WORDS` in [3, 12], `SECRET_HASH_PREFIX_CHARS` in [4, 12].
`src/sentinel.py` keeps its own `SENTINEL_STALE_S != MAX_BOOK_AGE_S` guard as well.

## Operational limits (stay in their modules)

Paging, retry and sampling limits. They decide how much is read, not what passes, so they were not moved.

| Name | Value | Unit | Meaning | Where | Source | M2 pending |
|---|---|---|---|---|---|---|
| `scanner.PAGE_LIMIT` | `100` | markets/page | Gamma keyset page size. | `src/scanner.py` | Task I (PR #2, 63c9e82) | no |
| `scanner.MAX_MARKETS` | `50000` | markets | Cap on one keyset walk; hitting it with a cursor left is incomplete (exit 3). | `src/scanner.py` | Task I (PR #2, 63c9e82) | no |
| `scanner.MAX_PAGES` | `1000` | pages | Cap on keyset pages per walk. | `src/scanner.py` | Task I (PR #2, 63c9e82) | no |
| `scanner.BUS_TOP_N` | `50` | markets | Only the top N passing markets (24h volume) become Scanner bus signals. | `src/scanner.py` | Task I (PR #2, 63c9e82) | no |
| `podlib.PUBLIC_GET_ATTEMPTS` | `3` | attempts | Tries per public GET on transient network/TLS errors. | `src/podlib.py` | Task K (9c41d5a) | no |
| `podlib.PUBLIC_GET_BACKOFF_S` | `0.5` | s | Retry backoff base (0.5 s, then 1 s). | `src/podlib.py` | Task K (9c41d5a) | no |
| `paper_session.BOOK_LIMIT` | `5` | markets | A paper session books the first N passing markets. | `src/paper_session.py` | Task E | no |
| `m1_report.MISMATCH_LIST_LIMIT` | `200` | rows | The M1 report lists at most this many REST-vs-WS mismatches (all are counted). | `src/m1_report.py` | Task AI | no |
| `reconcile.PAGE_LIMIT` | `500` | rows/page | Data API v2 first-page size for `/v2/positions` and `/v2/trades` (doc maximum 1000; later pages use the cursor's size). | `src/reconcile.py` | Task AK | no |
| `reconcile.MAX_PAGES` | `200` | pages | Cap on one cursor walk; hitting it with a cursor left is `incomplete_walk` (exit 3, fails closed). | `src/reconcile.py` | Task AK | no |

Not gathered either: `book.record(per_market=2)` (books at most 2 outcomes per market) is a function default,
and `paper_ledger`'s self-check uses a fixed 0.005 slippage as fixture data.

## config/risk.toml (read-only)

Listed for completeness. These values are governed by `config/risk.toml` itself and its pinned sha256
(`0aea23af1e938d330b5b4fab2c6abdec9f475f16807d177a5860a4da32aebc04`), not by this table or by code; nothing here
is copied into `src/thresholds.py`. `mode`, `live_enabled`, `require_human_gate`, `max_order_pct` and
`max_daily_loss_pct` are also pinned by `podlib.PINS`: any drift stops the run.

| Name | Value | Unit | Meaning | Where | Source | M2 pending |
|---|---|---|---|---|---|---|
| `mode` | `"paper"` | — | Phase 0 runs paper only. Pinned. | `risk.toml: mode` | config/risk.toml | file-governed |
| `live_enabled` | `false` | — | No live trading. Pinned. | `risk.toml: live_enabled` | config/risk.toml | file-governed |
| `max_order_pct` | `0.01` | fraction of equity | Order size cap (1% clip). Pinned. | `risk.toml: max_order_pct` | config/risk.toml | file-governed |
| `max_daily_loss_pct` | `0.03` | fraction of equity | Daily loss cap; reaching it vetoes. Pinned. | `risk.toml: max_daily_loss_pct` | config/risk.toml | file-governed |
| `max_open_positions` | `5` | legs | Open legs (market + token) cap. | `risk.toml: max_open_positions` | config/risk.toml | file-governed |
| `min_liquidity_usd` | `10000` | USD | Scanner and RiskGate liquidity floor (and the Gamma pre-filter). | `risk.toml: min_liquidity_usd` | config/risk.toml | file-governed |
| `max_spread` | `0.04` | price | Scanner and RiskGate spread ceiling. | `risk.toml: max_spread` | config/risk.toml | file-governed |
| `kelly_fraction` | `0.15` | fraction | Not used by RiskGate today (backlog #7). `src/intent_builder.py` (Task AO) computes a Kelly size with it in shadow mode only (reported, never used for size). | `risk.toml: kelly_fraction` | config/risk.toml | file-governed |
| `require_human_gate` | `true` | — | A human gate is required. Pinned. | `risk.toml: require_human_gate` | config/risk.toml | file-governed |
