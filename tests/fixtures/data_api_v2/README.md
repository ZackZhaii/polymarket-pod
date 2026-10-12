# Data API v2 fixtures (Task AK)

Recorded shape: one live public read each of `GET /v2/positions` and `GET /v2/trades` on 2026-10-09
(field names and types exactly as served; every field of the openapi `Position` / `Trade` schemas present),
then sanitised: synthetic wallet `0x1111...1111`, synthetic token / condition ids, transaction hashes,
sizes and prices; profile, title and icon fields blanked. Two pages per route to exercise the cursor walk.

Ledger these pages reconcile with (tests/test_reconcile.py builds it with ledger_store.book):
token A buy 10, sell 4 (open 6); token B buy 20, buy 5 (open 25).
