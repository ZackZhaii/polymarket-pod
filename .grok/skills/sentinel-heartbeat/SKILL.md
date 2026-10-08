---
name: sentinel-heartbeat
description: Use when checking whether polymarket-pod's Sentinel is healthy, or when a paper halt is proposed (src/sentinel.py). Halt cancels local paper quotes only. Never calls CLOB.
---

# sentinel-heartbeat

Bus role: `Sentinel`, and it may emit only `halt` (`sentinel.halt` -> `podlib.bus`, `to=Executor`). ROSTER and ROLES agree on Sentinel. Bot name Poly哨兵 is not a bus name. `src/sentinel.py` is a stub: it does not read the clock, the bus, book files, or any API. Callers pass book timestamps and an aware `now` to `StaleWatch.check` (Task Z).

## Allowed

- `sentinel.halt(reason, market="", ...)` with a non-empty reason. It rewrites `logs/quotes.jsonl` (`podlib.QUOTES_PATH`) and appends one bus line.
- `sentinel.cancel_open_quotes`: rows with `status == "open"` (and matching `market`, when given) become `status=cancelled`, `cancelled_ts`, `cancelled_by=Sentinel`. Other rows are untouched. Returns the count.
- Bus payload exactly: `reason`, `paper_quotes_cancelled` (int), `exchange_action: "none"` (`sentinel.halt`).
- `sentinel.StaleWatch` (Task Z, backlog #9): per market, a check is stale when `book_staleness.book_age_problem` returns a reason at `SENTINEL_STALE_S = 120` (missing, unparseable, non-finite or future `book_ts`, a broken `now`, or `failed=True`). After `SENTINEL_STALE_CONSECUTIVE = 2` stale checks in a row it calls `halt` once (reason `stale_book_consecutive: ...`); a fresh check resets the streak. `to_dict`/`from_dict` carry the streak between runs; a malformed state raises.

## Forbidden

- Any CLOB call, cancel, or kill switch against the exchange. `exchange_action` is the string `none`. There is no live cancel path in this file.
- A halt that deletes quotes, edits `config/risk.toml`, or writes a `fill`.
- An empty reason (`sentinel.halt` raises `ValueError`).
- Loosening Task Z: `StaleWatch` accepts only stricter limits (`max_age_s` <= 120, `consecutive` 1..2). Both constants are M2 proposals in code, not risk.toml keys. The 2-in-a-row rule governs only the Sentinel halt; RiskGate still vetoes every single stale book and paper_cycle still skips the gap signal. No heartbeat interval, no bus-freshness window, and no API-health probe exist in `src/sentinel.py`: TBD (not in repo).

## Steps

1. What the stub does today: cancel matching open rows in `logs/quotes.jsonl`, then emit `halt`. Quotes arrive from `maker.quote` (`status=open`, `venue=paper`). `StaleWatch` escalates book staleness to that halt; it is not yet wired into `paper_cycle`, so a caller must feed it checks. Nothing else is monitored.
2. Freshness you can actually check, with other modules, before asking for a halt:
   - Bus file `logs/bus.jsonl` exists and its last `ts` parses as UTC `Z`. There is no freshness threshold in code: do not invent one.
   - Book snapshots: `book_staleness.book_age_problem` at `MAX_BOOK_AGE_S = 120`, and `paper_cycle` which already emits `stale=true` to `Sentinel` after 120 s. BookWatcher does not auto-halt.
   - API health: `podlib.public_get` retries 3 times; `scanner.main` exits 3 with `stop_reason=network_error` when a page still fails. No separate health endpoint exists.
3. Phase-upgrade checklist, all three required, none optional: Poly风控 (RiskGate role) approval, Poly哨兵 confirmation that this halt path still reports `exchange_action=none` and the paper-quote cancel works, and the human gate (家伟) in person. `require_human_gate` stays true. Do not flip `live_enabled`.
4. A halt is not a phase upgrade and not a fill. Single-leg exposure rules in `paper-fill` still apply.

## Verification

- After `halt`, every targeted open quote in `logs/quotes.jsonl` is `cancelled`, and the bus line is `type=halt`, `from=Sentinel`, `exchange_action=none`.
- `python -m pytest tests/test_roles.py` passes offline. `test_sentinel_halt_cancels_paper_quotes_only` proves a market-scoped halt cancels only that market's open quote, emits `halt` with `exchange_action=none`, and writes no `fill`.
- `live_enabled=false`. Committed risk.toml sha256 unchanged (`docs/COORDINATION.md`).
