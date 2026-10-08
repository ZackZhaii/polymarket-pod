---
name: book-watch
description: Use when reading public CLOB order books or judging book freshness in polymarket-pod (src/book.py, src/book_staleness.py, src/paper_cycle.py). Public /book only; never the user channel.
---

# book-watch

Bus role: `BookWatcher` (`book.record`, `paper_cycle.run`). ROSTER and ROLES agree on BookWatcher. Bot name Poly盘口 is not a bus name.

## Allowed

- `python src/book.py`. Reads the latest timestamp's rows in `data/markets.jsonl`, first 5 markets (`book.latest_markets`), first 2 outcome tokens each (`book.record` `per_market=2`), via `GET https://clob.polymarket.com/book?token_id=...` (`book.snapshot`).
- `python src/paper_cycle.py` for an offline staleness pass over local jsonl. No network.
- Freshness check `book_staleness.book_age_problem(book_ts, now, max_age_s)` with `book_staleness.MAX_BOOK_AGE_S = 120.0`. Fresh means `0 <= age <= 120` (the boundary is fresh). `paper_cycle` uses the same check (`paper_cycle._book_problem` -> `book_age_problem`, `STALE_AFTER_S = 120`), so a future, missing or unparseable `ts` is stale there too (Task R).

## Forbidden

- Any user-channel websocket, auth, or order call. `book.py` never cancels or posts.
- A market-channel websocket. It is not implemented. Treat it as a future option only, and never the user channel. Adding it needs a PR plus Poly风控 approval.
- Auto-halt. BookWatcher emits `signal`, not `halt` (`paper_cycle.run` sends staleness to `Sentinel` and sets `exchange_action: none`).
- Editing `config/risk.toml`. 120 seconds is a code constant on purpose (Task L); moving it into risk.toml is an Operator proposal.

## Steps

1. Run `python src/scanner.py` first. `book.main` exits 2 with `no markets in data/markets.jsonl` when the file is empty.
2. Run `python src/book.py`. One token that fails (network, HTTP, non-dict body) prints `book_skip token=<12 chars> reason=<Exc>` to stderr and is skipped (`book.snapshot`). A book with no usable `timestamp` is still recorded, prints `book_ts_problem token=<12 chars> reason=<missing_book_ts|invalid_book_ts>` to stderr, and carries `book_ts=null`.
3. Best bid is the max price level, best ask the min (`book.level`). Spread is `round(ask - bid, 6)`, or null when either side is missing.
4. Each snapshot appends to `data/books.jsonl` with fields: `ts, book_ts, book_ts_problem, market_id, question, condition_id, token_id, outcome, best_bid, best_bid_size, best_ask, best_ask_size, spread, tick_size, min_order_size, liquidity_usd, source=clob_public_book` (`book.snapshot`).
5. Bus line: `from=BookWatcher`, `to=RiskGate`, `type=signal`, payload `token_id, outcome, best_bid, best_ask, spread, book_ts, book_ts_problem, source` (`book.record`). `ts` is the recorder's clock; `book_ts` is the book's own /book `timestamp` (Unix ms string) as ISO 8601 UTC with milliseconds (`book.book_timestamp`), never the recorder's clock. No usable timestamp: `book_ts=null` plus the reason, and RiskGate vetoes `missing_book_ts` on it. RiskGate reads `Intent.book_ts`, which `paper_ledger` must pass through.
6. Offline: `python src/paper_cycle.py`. A book whose `ts` is older than 120 s, in the future, missing or unparseable (or an unparseable `now`) emits `from=BookWatcher`, `to=Sentinel`, payload `stale=true, stale_after_s=120, stale_reasons=[...], exchange_action=none` (`paper_cycle.run`, `_book_problem`). Exactly 120 s is fresh. It also emits gap signals only; `fills` stays 0.

## Verification

- `book.main`: exit 0 when at least one snapshot was written; exit 1 on `book failed: no public book snapshot`; exit 2 when no markets file. Stdout ends with `book_snapshots=<n> mode=... live_enabled=false orders=none`.
- `book_age_problem` reasons, all veto strings: `missing_book_ts`, `invalid_book_ts` (naive, date-only, numeric text, bool, nan, inf), `future_book_ts`, `stale_book`, `invalid_max_book_age` (limit missing or <= 0), `invalid_now` (`book_staleness.py`). Accepted stamps: ISO 8601 with a time and a zone (`Z` or offset), an aware datetime, or finite epoch seconds.
- `python -m pytest tests/test_book_staleness.py tests/test_book_ts_consistency.py` passes offline. `live_enabled` stays false.
