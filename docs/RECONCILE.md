# Reconciler on Data API v2 (Task AK)

Polymarket retires Data API v1 on **2026-10-24**. The Reconciler (`docs/ROSTER.md`: "Data API v2 positions /
fills / redeem", may not trade) reads only Data API v2. It is read-only: no key, no order, no signing, nothing
written to the ledger. `config/risk.toml` is untouched and `live_enabled` stays `false`.

## Sources (checked 2026-10-09)

- Migration guide: https://docs.polymarket.com/migrate/data-api-v1-to-v2 (v1 retired 2026-10-24; `/positions` ->
  `/v2/positions`, `/closed-positions` -> `/v2/positions?status=CLOSED`, `/trades` -> `/v2/trades`; `{data,
  pagination}` envelope; cursor-only paging; snake_case response fields).
- Data API v2 overview: https://docs.polymarket.com/api-reference/data-api/overview (no auth; follow
  `pagination.next_cursor` until `null`; `has_more` is exact; a missing or `null` numeric field means unavailable,
  never zero; feeds must re-send identical filters on every page).
- List trades: https://docs.polymarket.com/api-reference/feeds/list-trades
- Poly研究 research note `research_dataapi_ws_m2.md` (2026-10-09, read-only; Data API part only): field mapping v1 ->
  v2, cursor-only paging, `start=1`, rate limits, the position identities, the dedupe key
  `transaction_hash + token_id + side`, and "read `/v2/status` first every round".
- The machine-readable contract the overview links (`openapi.json` under the v2 routes, version 0.1.0): schemas
  `PositionsPage`, `Position`, `TradesPage`, `Trade`, `Pagination`. A live public read of both routes on
  2026-10-09 returned exactly those field names (the test fixtures follow that shape with a synthetic wallet).

## Endpoints (the only three allowlisted in `scripts/guard.py` and `podlib.public_get`)

| Route | Query | Replaces |
|---|---|---|
| `GET https://data-api.polymarket.com/v2/status` | none (exact URL) | new in v2; read first every round |
| `GET https://data-api.polymarket.com/v2/positions` | `user`, `status=OPEN`, `filter_type=TOKENS`, `filter_amount=0.1`, `limit=500` (first page) / `cursor` | v1 `/positions` |
| `GET https://data-api.polymarket.com/v2/trades` | `user`, `taker_only=false`, `start=1`, `limit=500` (first page) / `cursor`, same filters on every page | v1 `/trades` |

`status=OPEN` is the superset that also returns `REDEEMABLE` rows. `taker_only=false` includes the wallet's maker
fills; `start=1` asks for full history (the default floors at three years). The v1 routes, `/v2/positions/combos`
and every other v2 route stay refused.

## What fails closed

Any problem is a FAIL with a non-zero exit; nothing is coerced or defaulted to a passing value.

- Freshness (`/v2/status`, first, every round): `data.age_seconds` and `data.serving.lag_seconds` must be integers
  >= 0 (`null` means "not measured", so it fails: `status_invalid_*`), and `age_seconds + lag_seconds` (the snapshot is
  that much older on top of the lag it reports) must be <= `DATA_API_MAX_LAG_S` (`status_lag_too_large`);
  `ingestion.network` must be `polygon` and `ingestion.chain_id` 137 (`status_wrong_chain`). On any of these nothing
  else is read. Exit 3.

- Envelope: body not an object; `data` missing, `null` (`data_null`) or not a list; `pagination` missing;
  `has_more` not a bool; `next_cursor` missing, empty or not a string; `has_more` disagreeing with `next_cursor`;
  a repeated cursor (`cursor_loop`); `MAX_PAGES` reached with a cursor left (`incomplete_walk`); any fetch error
  (network, TLS, HTTP 4xx/5xx after `public_get`'s retries, invalid JSON). Exit 3.
- Position row (`/v2/positions`): `proxy_wallet` (0x + 40 hex, must be the requested wallet), `token_id` (decimal
  digits), `condition_id` (0x + 64 hex), `current_size` (finite JSON number >= 0), `status` (`OPEN` or
  `REDEEMABLE`), and the documented identities within `DATA_API_IDENTITY_TOL_USDC`:
  `current_value = current_size * current_price`, `unrealized_pnl = current_value - entry_cost_usdc`,
  `total_pnl = realized_pnl + unrealized_pnl`, `total_cost_usdc = entry_cost_usdc + entry_fees_usdc`. So
  `current_price`, `current_value`, `entry_cost_usdc`, `entry_fees_usdc`, `total_cost_usdc`, `realized_pnl`,
  `unrealized_pnl`, `total_pnl` must be finite numbers too (`position_invalid_<field>`), and a broken identity is
  `position_identity_broken_<field>`. A token served twice is `duplicate_remote_token`. Exit 3.
- Trade row (`/v2/trades`): `proxy_wallet`, `side` (`BUY`/`SELL`), `token_id`, `condition_id`, `size` (finite > 0),
  `price` (finite, 0..1), `timestamp` (integer > 0), `transaction_hash` (0x + 64 hex). Exit 3.
- Trade dedupe on `transaction_hash + token_id + side` (research note): an exact repeat of a row counts once
  (`trades_deduped` in the report). Rows that share the key but differ are kept as separate fills: a live read on
  2026-10-09 had one transaction with two fills of the same token and side (102.73 and 27.27 shares at 0.22), so
  dropping one would hide shares. Either way a wrong guess makes the ledger comparison fail, never pass.
- Numbers must be JSON numbers: a string, bool, `null`, NaN or inf is malformed (v2 serves all amounts as numbers).
- Ledger (`src/ledger_store.py`): any integrity problem, a counted fill without token_id or with invalid shares,
  an open leg without token_id, a token under two markets. Exit 4.
- Comparison per token (exit 1): ledger open shares vs `current_size` (`position_mismatch`,
  `missing_remote_position`, `unexpected_remote_position`), ledger buy / sell shares vs the wallet's BUY / SELL
  trade sizes (`trade_flow_mismatch`), a short ledger leg, and anything the API cannot show: an open leg below
  the 0.1-share positions floor (`position_below_data_api_floor`) or a fill below the 0.01-share trades floor
  (`ledger_fill_below_data_api_trade_floor`).

Fields the Reconciler does not use (titles, PnL columns, icons, profile fields) are not checked.

## Ambiguities, resolved fail-closed

1. `/v2/positions` `filter_amount` "defaults to the 0.1 dust floor" and "invalid values fall back to the default";
   the docs do not say whether `0` lifts the floor. We send the documented `0.1` and treat a ledger leg below it
   as unverifiable (fail), never as matched.
2. `/v2/trades` serves fills of at least 0.01 shares ("0 means the same"). A smaller ledger fill fails.
3. v2 trades are fills only. Redeems, merges, splits and transfers move positions without a trade row, and the
   paper ledger has no such events; any of them shows up as a mismatch (fail), never as a silent pass.
4. The research note says to dedupe on `transaction_hash + token_id + side`; live data shows that key is not
   unique per fill (see above). We dedupe exact repeats only; this deviation is deliberate and recorded here.
5. The size of a "too large" lag is not documented; `DATA_API_MAX_LAG_S = 120` s is our first value (M2 pending).
6. Positions are only read with `status=OPEN`: a ledger leg the wallet already closed or redeemed is
   `missing_remote_position`.

## Numbers (M2 pending, `src/thresholds.py` and `docs/THRESHOLDS.md`)

- `RECONCILE_SHARES_TOL = 1e-06` shares: largest allowed ledger-vs-API difference per token.
- `DATA_API_POSITIONS_DUST_SHARES = 0.1`, `DATA_API_TRADES_MIN_SHARES = 0.01`: Data API contract facts.
- `DATA_API_MAX_LAG_S = 120.0` s: largest `age_seconds + serving.lag_seconds` accepted from `/v2/status`.
- `DATA_API_IDENTITY_TOL_USDC = 1e-06` USDC: position-row identity tolerance (live rows: within 1.2e-10).
- Rate limits (research note, per 10 s: 800 total, trades 300, positions 200, status 100): one round makes
  1 + pages(positions) + pages(trades) requests, far below them; a 429 is not retried as success (`public_get`
  raises, the round fails closed, exit 3).
- Operational, in the module: `reconcile.PAGE_LIMIT = 500`, `reconcile.MAX_PAGES = 200`.

## Phase 0 note

The paper ledger is not on chain, so reconciling it against a real wallet reports mismatches by design; the
tool is for M2 and later. It is not on the paper pipeline and not part of the e2e gate (`scripts/e2e_offline.py`
is unchanged). Its tests run offline on recorded-shape fixtures in `tests/fixtures/data_api_v2/` with every
network path patched to raise.
