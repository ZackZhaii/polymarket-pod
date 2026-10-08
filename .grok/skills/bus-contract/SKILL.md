---
name: bus-contract
description: Use when writing or validating logs/bus.jsonl, Gamma normalize rows, or CLOB book snapshots in polymarket-pod. Defines the Phase 0 event and response field contract, one UTC timestamp format, and offline fixture tests. No live API calls in tests.
---

# bus-contract

Phase 0 paper. Stdlib only. No keys. Bus append-only (`podlib.append_jsonl`). Reflector proposals never auto-apply. PRs base=`dev`; bots never merge.

## Allowed

- Document and validate the envelopes below against real writers: `podlib.bus`, `scanner.normalize` / `persist_markets`, `book.snapshot` / `record`, `paper_ledger.commit_if_allowed`.
- Offline fixture tests under `tests/fixtures/` (saved Gamma/CLOB JSON). Mock `urlopen` like `tests/test_public_get_retry.py`.
- A stdlib-only validator outline (no third-party schema lib).

## Forbidden

- Live HTTP in tests. Trading, signing, wallet, or editing `config/risk.toml`.
- Inventing bus `type` values outside `podlib.BUS_TYPES = {signal, veto, fill, proposal, halt}`.
- Emitting `fill` from anyone but `Executor` with `payload.risk_decision == "allow"` (`podlib.bus`).

## Steps

### 1. Canonical UTC timestamp

- Write with `podlib.utc_now()` → `"%Y-%m-%dT%H:%M:%SZ"` (always `Z`).
- **Canonical parse (proposal, needs code PR):** prefer `book_staleness.parse_book_ts` rules for every `ts` / `book_ts`: ISO with time + zone (`Z` or offset), aware datetime, or finite epoch seconds. Reject naive, date-only, numeric strings, bool, nan, inf.
- **Known drift (do not paper over):**
  - `paper_cycle._book_problem` delegates to `book_staleness.book_age_problem` (Task R): fresh means `0 <= age <= 120` s; a future, missing or unparseable `ts`, an unparseable `now` and a broken limit all count as stale, and the stale signal carries `stale_reasons`.
  - `book_staleness.parse_book_ts`: broader ISO / epoch; rejects naive.
  - `scanner.parse_utc`: `Z` → `+00:00`, else `fromisoformat`, else naive `strptime` **treated as UTC** (still lenient; not touched by Task R).
  - `book.record` bus payload carries `book_ts` and `book_ts_problem` (Task R). `book_ts` is the exchange's own CLOB `/book` `timestamp` (Unix ms string, or a non-negative int, or an aware ISO string), converted by `book.book_timestamp` to ISO 8601 UTC with milliseconds and `Z`. When unusable it is `null` and `book_ts_problem` is `missing_book_ts` or `invalid_book_ts` (floats, signs, decimals, bools, naive times); otherwise `book_ts_problem` is `null`. The recorder clock `ts` is never used as `book_ts`.
  - Still open after Task R: `paper_cycle` judges age from row `ts` (recorder clock) and `paper_ledger.realistic_no_edge` sets `Intent.book_ts=book.get("ts")`. Switching to exchange `book_ts` is a pending Poly指挥 decision.

### 2. Bus envelope (`podlib.bus`)

Required keys: `ts`, `from`, `to`, `type`, `market`, `payload`.  
`from`/`to` go through `podlib.canonical_role` (`Forecaster` → `NewsForecaster`). Role names: `docs/ROSTER.md` via `docs/COORDINATION.md`.

| type | who may emit | notes |
|---|---|---|
| signal | Scanner, BookWatcher (book to RiskGate; stale to Sentinel), NewsForecaster, ArbBinary, NegRiskArb, Maker, Endgame, SportsDevig, RiskGate (allow path) | payload role-specific |
| veto | RiskGate | reasons list |
| fill | Executor only | `risk_decision=allow` |
| proposal | QuantResearcher, Reflector (both to `Operator`) | `changes_risk_toml=false`; never mutates risk.toml |
| halt | Sentinel | `exchange_action=none` |

### 3. Fields we actually read

**Gamma → `scanner.normalize`:** `id, question, slug, conditionId, version, clobTokenIds|positionIds, outcomes, outcomePrices, liquidityNum|liquidity, volumeNum|volume, volume24hr, endDate, bestBid, bestAsk, spread, lastTradePrice, acceptingOrders, feesEnabled, closed`. Output also: `token_id_source, token_ids_problem, price, price_source, volume_24h_usd, source=gamma_public`; `scanner.filter_markets` adds `days_to_expiry` to passing rows.

**CLOB `/book` → `book.snapshot`:** from response `bids, asks, tick_size, min_order_size, market, timestamp`; written row: `ts, book_ts, book_ts_problem, market_id, question, condition_id, token_id, outcome, best_bid, best_bid_size, best_ask, best_ask_size, spread, tick_size, min_order_size, liquidity_usd, source=clob_public_book`. Bus payload (`book.record`, BookWatcher → RiskGate): `token_id, outcome, best_bid, best_ask, spread, book_ts, book_ts_problem, source`.

**Paper fill (`paper_ledger.commit_if_allowed`):** see `paper-fill` skill field list; carries `book_ts` (copied from `Intent.book_ts`).

### 4. Stdlib validator outline (docs → future `src/bus_validate.py`)

```python
# Outline only — not in repo yet (TBD).
REQUIRED_BUS = ("ts", "from", "to", "type", "market", "payload")
def validate_bus_line(row: dict) -> list[str]:
    problems = []
    for k in REQUIRED_BUS:
        if k not in row: problems.append(f"missing:{k}")
    if row.get("type") not in frozenset({"signal","veto","fill","proposal","halt"}):
        problems.append("bad_type")
    if book_staleness.parse_book_ts(row.get("ts")) is None:
        problems.append("bad_ts")
    # fill: from==Executor and payload.risk_decision==allow
    return problems
```

`book_ts` means the exchange's own book timestamp, never the recorder clock `ts`. Never copy `ts` into `book_ts`. If the book had none, carry `book_ts: null` plus `book_ts_problem`; RiskGate then vetoes `missing_book_ts` (fail closed).

### 5. Offline fixtures

- Keep real-shaped JSON under `tests/fixtures/` (already: `gamma_markets.json` with `version: v1`).
- Add CLOB book fixtures the same way (none committed yet; `tests/test_book_ts_consistency.py` builds `/book` payloads inline). Tests must not call the network (`test_scanner_v2_tokens`, `test_public_get_retry`, `test_book_ts_consistency` patterns).

## Verification

- Cite every required field back to a writer function above.
- `python -m pytest` offline; `live_enabled=false`; committed `risk.toml` sha unchanged (`docs/COORDINATION.md`).
- Flag any new writer that omits `book_ts` on a path that later calls `risk_gate.evaluate`.
