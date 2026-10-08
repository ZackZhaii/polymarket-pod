---
name: bus-observability
description: Use when adding span-like fields to polymarket-pod bot steps, writing a daily bus summary, or proposing alert thresholds. Convention only — no OpenTelemetry SDK, no live trading.
---

# bus-observability

Phase 0 paper. Append-only `logs/bus.jsonl` (`podlib.BUS_PATH`). Naming borrows OpenTelemetry GenAI agent span conventions (https://github.com/open-telemetry/semantic-conventions-genai) as **labels only** — do not install any OTel SDK (`requirements.txt` is comment-only / stdlib).

## Allowed

- Document span-like fields on each bot step and a daily rollup script outline (stdlib `json` + `collections.Counter`).
- Read existing bus writers: `podlib.bus`, `scanner.persist_markets`, `book.record`, `paper_cycle.run`, `paper_ledger.commit_if_allowed`, `sentinel.halt`, `quant.propose`, `reflector.propose`.
- Propose alert thresholds as Reflector text (`reflector.propose`); never auto-apply.

## Forbidden

- Adding `opentelemetry-*`, exporters, or any non-stdlib dependency.
- Writing secrets, API keys, or wallet addresses into bus payloads (`docs/ROSTER.md`: never put keys in the bus).
- Auto-halt or live cancel from an observability rule. Halt stays `sentinel.halt` with `exchange_action=none`.
- Editing `config/risk.toml`.

## Steps

### 1. Span-like fields (per step)

Every bus line already has `ts, from, to, type, market, payload` (`podlib.bus`). Extend **payload** (not a new top-level schema) with:

| field | meaning | cite |
|---|---|---|
| `role` | same as `from` after `canonical_role` | `podlib.canonical_role` |
| `event` | short verb: `scan_page`, `book_snap`, `stale_flag`, `gate_veto`, `gate_allow`, `paper_fill`, `halt`, `propose` | writer function name |
| `market_id` | copy of `market` when non-empty | envelope |
| `book_ts` | the exchange's own `/book` timestamp when the step used a book (never the recorder clock `ts`) | already on the `book.record` bus payload with `book_ts_problem` (Task R); required for gate/fill |
| `latency_ms` | wall time of the step | TBD (not in repo) — measure locally, do not invent defaults in code without a PR |
| `outcome` | `ok` / `veto` / `skip` / `error` / `stale` | map from exit reasons |

OTel-style name (convention only): `gen_ai.agent.<role>.<event>` e.g. `gen_ai.agent.BookWatcher.book_snap`. Do not emit OTLP.

### 2. Daily summary (offline)

Outline for a future `python -c` / small stdlib script (TBD not shipped):

1. `podlib.read_jsonl(BUS_PATH)` for one UTC day (`ts[:10]`).
2. Count by `(from, type)`, veto `reasons`, stale signals, fills, halts, proposals.
3. Print: signals, vetoes, fills, halts, proposals, distinct markets, oldest/newest `ts`.
4. Never delete or rewrite bus lines.

### 3. Alert thresholds (proposals only)

| signal | proposal value | status |
|---|---|---|
| book age | 120 s (exactly 120 s is fresh) | code: `book_staleness.MAX_BOOK_AGE_S`, `paper_cycle.STALE_AFTER_S` (same check since Task R) |
| consecutive stale before alert | 2 | backlog item 9 in `docs/COORDINATION.md` — TBD (not in repo) |
| heartbeat interval | TBD | Sentinel stub has none (`src/sentinel.py`) |
| API probe failure | scanner exit 3 / `stop_reason=network_error` | `scanner.fetch_all_markets` |

File proposals via `reflector.propose` (≤3/UTC day). Need Poly风控 + Poly哨兵 + human gate before code changes.

## Verification

- A new payload field is documented here and in the PR; bus `type` set unchanged.
- Daily summary runs offline on a copied jsonl; no network.
- `live_enabled=false`; risk.toml untouched.
