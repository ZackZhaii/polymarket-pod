---
name: market-scan
description: Use when running or reading polymarket-pod's Phase 0 Gamma scanner (src/scanner.py) — public market discovery, filter reasons, exit codes, and Scanner bus signals. No orders.
---

# market-scan

Role name on the bus: `Scanner` (`scanner.persist_markets`). ROSTER and ROLES agree on Scanner. Bot name Poly扫描 is not a bus name.

## Allowed

- `python src/scanner.py` from the repo root. It calls `podlib.public_get` only (`scanner.main` -> `scan` -> `fetch_all_markets`).
- Read `config/risk.toml`. Scanner keys it enforces (`scanner.thresholds_from`): `min_liquidity_usd` (must be >= 0) and `max_spread` (must be > 0). Optional overrides `scanner_min_price` / `scanner_max_price` fall back to `scanner.MIN_PRICE = 0.03` and `scanner.MAX_PRICE = 0.97`, and must satisfy `0 <= min < max <= 1`.
- Append passing rows to `data/markets.jsonl` and at most `scanner.BUS_TOP_N = 50` bus lines (`scanner.persist_markets`). Both files are written only on exit 0.

## Forbidden

- Editing `config/risk.toml`. Missing, bool, string, non-numeric, inf, or nan thresholds raise `RuntimeError` (`scanner._finite_number`); there is no looser default.
- Any order, auth, or signing endpoint. `live_enabled` stays false.
- Treating a passing market as a trade. Signals go to `BookWatcher` and carry `source: gamma_public` only.

## Steps

1. Run `python src/scanner.py`. Walk is `GET /markets/keyset`, `order=volume24hr`, `ascending=false`, `limit=100` (`scanner.PAGE_LIMIT`), `liquidity_num_min` set from `min_liquidity_usd`. Caps: `scanner.MAX_MARKETS = 50000`, `scanner.MAX_PAGES = 1000`.
2. Read stdout: `scanned= passed= stop= cap_hit= server_min_liquidity= min_hours_to_expiry= mode= live_enabled= keys=none orders=none`, then `rejected_first=reason:count`.
3. Interpret `stop_reason` (`scanner.fetch_all_markets`): `exhausted` (complete), `max_markets`, `max_pages`, `no_new_rows`, `cursor_repeat` (including A->B->A), `network_error` (a page still failing after `public_get` retries; `meta["error"]` holds the `PublicGetError` text).
4. A row is scanned only when it has a question, slug, or id (`scanner.filter_markets`). First rejection wins, in `scanner.REASONS` order (`scanner.rejection_reasons`):
   - `closed` — `closed` true.
   - End date (`expiry_rule.expiry_problem`, one reason at most; `expiry_rule.MIN_HOURS_TO_EXPIRY = 24.0`, a code constant, not a risk.toml key):
     `expired` — end <= now; `missing_end_date` — `endDate` missing or blank; `invalid_end_date` — unparseable, naive, date-only or non-string;
     `near_expiry` — under 24 h left (exactly 24 h passes); `invalid_min_hours_to_expiry` — bad threshold; `invalid_now` — naive or bad clock.
   - `not_accepting_orders` — `acceptingOrders` is false.
   - `missing_bid_ask` — bid/ask missing, bid <= 0, or ask >= 1 (Gamma's one-sided book).
   - `low_liquidity` — liquidity missing or < `min_liquidity_usd` (`liquidityNum`, else `liquidity`).
   - `wide_spread` — spread missing or > `max_spread` (+1e-12). Spread falls back to ask-bid.
   - `missing_price` — no YES price; `long_tail_price` — price outside the band.
5. YES price (`scanner.yes_price`): bid/ask mid when `0 < bid <= ask < 1`, else `lastTradePrice` if > 0, else `outcomePrices[0]`.
6. Survivors sort by `volume24hr` descending. Top 10 print. All survivors go to `data/markets.jsonl`; only the top 50 become bus `signal` lines, `from=Scanner`, `to=BookWatcher`, payload `question, slug, liquidity_usd, spread, source` (`scanner.persist_markets`).

## Verification

- Exit codes (`scanner.main`, constants `EXIT_*`): 0 ok; 1 fewer than 5 scanned (`EXIT_TOO_FEW_SCANNED`); 2 zero passed (`EXIT_NONE_PASSED`); 3 pagination incomplete, including `network_error` (`EXIT_INCOMPLETE`). Codes 1–3 write nothing to `data/markets.jsonl` or the bus.
- `python -m pytest tests/test_scanner.py tests/test_scanner_min_expiry.py tests/test_public_get_retry.py` passes with no network. Retry tests patch `podlib._sleep`.
- `live_enabled=false` and the committed risk.toml sha256 still match `docs/COORDINATION.md`.
- Do not promise that a passed market is an edge. Markets with under 24 h to expiry are rejected (`near_expiry`, Task O; COORDINATION backlog item 8).
