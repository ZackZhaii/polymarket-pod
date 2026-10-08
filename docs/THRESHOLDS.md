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
| `PAPER_EQUITY_USD` | `1000.0` | USD | Local paper bankroll: order size is `max_order_pct` of it, daily loss is measured against it. Not a wallet. | `src/thresholds.py`; re-exported as `podlib.PAPER_EQUITY_USD`, `risk_gate.PAPER_EQUITY_USD`, `maker.PAPER_EQUITY_USD` | Phase 0 base (65a08e1) | no |
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
| `LEDGER_OPEN_EPS` | `1e-09` | shares | A leg with at most this many open shares counts as closed (position count). | `src/thresholds.py`; re-exported as `paper_ledger.OPEN_EPS` | Task P (bbde4e2) | no |
| `REFLECTOR_MAX_PER_UTC_DAY` | `3` | proposals/day | Reflector writes at most this many proposals per UTC day. | `src/thresholds.py`; re-exported as `reflector.MAX_PER_UTC_DAY` | Task B (bd06620) | no |

Relations checked when `src/thresholds.py` is imported (an explicit `RuntimeError`, not `assert`):
`SENTINEL_STALE_S == MAX_BOOK_AGE_S`, `PAPER_CYCLE_STALE_AFTER_S == MAX_BOOK_AGE_S`,
`SENTINEL_STALE_CONSECUTIVE` a whole number >= 1, `0 <= SCANNER_MIN_PRICE < SCANNER_MAX_PRICE <= 1`,
`SCANNER_MAX_PRICE < ENDGAME_PRICE_MIN <= ENDGAME_PRICE_MAX < 1`, `0 < MIN_HOURS_TO_EXPIRY < ENDGAME_WINDOW_HOURS`.
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
| `kelly_fraction` | `0.15` | fraction | Not used by RiskGate today (backlog #7). | `risk.toml: kelly_fraction` | config/risk.toml | file-governed |
| `require_human_gate` | `true` | — | A human gate is required. Pinned. | `risk.toml: require_human_gate` | config/risk.toml | file-governed |
