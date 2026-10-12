---
name: incident-runbook
description: Use during a Phase 0 polymarket-pod incident (halt, stale book, API down, ledger corruption, heartbeat loss). Severity levels, playbooks, blameless postmortem. Sentinel halt cancels paper quotes only.
---

# incident-runbook

Phase 0: paper only. `sentinel.halt` rewrites `logs/quotes.jsonl` and emits `halt` with `exchange_action=none` — **no CLOB cancel** (`src/sentinel.py`). Never edit `config/risk.toml` during an incident.

## Allowed

- Call `sentinel.halt(reason=..., market=...)` when a playbook says so (keyword-only arguments).
- Read `logs/bus.jsonl`, `logs/paper.jsonl`, `data/books.jsonl`, `docs/LOOP.md` for timeline.
- File a blameless postmortem as a Reflector proposal or PR doc (base=`dev`).

## Forbidden

- Live kill switch, withdraw, order cancel on the exchange.
- Deleting bus/paper history to "fix" an incident.
- Flipping `live_enabled` or merging your own PR.
- Blame language naming individuals in the postmortem; focus on systems.

## Severity

| sev | meaning | Phase 0 response |
|---|---|---|
| SEV-1 | risk.toml pins drifted / suspected live path | Stop all bots; Operator verifies `podlib.pin_problems` / sha; no fills |
| SEV-2 | API down or incomplete scan (exit 3) | Pause scanner/book loops; halt optional |
| SEV-3 | stale books / missing book_ts mass vetoes | paper_cycle stale signals; fix book_ts plumbing |
| SEV-4 | single-market oddity / test failure | ticket via loop backlog; no halt |

## Playbooks

### Halt (paper)

1. Reason non-empty (`sentinel.halt` raises otherwise).
2. `halt` → cancel open rows in `logs/quotes.jsonl` (`cancel_open_quotes`); bus `from=Sentinel,to=Executor,type=halt`.
3. Confirm payload `exchange_action=none`, `paper_quotes_cancelled` count.
4. Do not claim exchange orders were cancelled.

### Stale book

1. Detect: `book_staleness.book_age_problem`; `paper_cycle._book_problem` uses the same check (Task R) on the row's exchange `book_ts`, never the recorder `ts` (Task T): older than `STALE_AFTER_S` (120 s; exactly 120 s is fresh), future, missing or unparseable `book_ts`, or any recorded `book_ts_problem` is stale.
2. Bus: BookWatcher → Sentinel `stale=true`, `stale_after_s`, `stale_reasons`, `gap_signal=skipped`, `skip_reason=stale_book`, `exchange_action=none` (`paper_cycle.run`). A stale market emits no gap signal (Task T). Rows with an unusable recorder `ts` show up as BookWatcher `skipped_row=true` signals (`missing_row_ts` / `invalid_row_ts`).
3. Exchange time: `book.record` carries the `/book` `timestamp` as `book_ts`; `book_ts=null` with `book_ts_problem` means the exchange gave none (or an unusable one).
4. RiskGate vetoes missing/stale `book_ts` — expected, not a SEV-1.
5. If mass stale: check clock skew; re-run `book.py`; do not loosen 120 s without Operator proposal (Task L).

### Wash / self-trade vetoes (Task S)

1. Symptom: RiskGate vetoes with `cross_side_same_leg`, `self_trade_structure` or `wash_same_market` even though the strategy name is clean, or `wash_input_missing` / `wash_input_invalid` on every intent.
2. `cross_side_same_leg`: an own paper quote is still `open` on the opposite side of the same leg in `logs/quotes.jsonl`. Expected while it rests; `sentinel.halt(reason=..., market=...)` cancels paper quotes. Never edit the quote file by hand.
3. `wash_same_market`: an own opposite-side paper fill on the same leg within `WASH_WINDOW_S` (300 s code constant; a proposal pending the user's decision at M2, not a risk.toml key). Expected; do not shorten the window to get a fill through.
4. `wash_input_missing` / `wash_input_invalid`: the caller did not pass `paper_ledger.load_state` history, or a same-market row in `paper.jsonl` / `quotes.jsonl` is malformed (bad side, unparseable `ts`, unknown quote status). Fail closed by design: SEV-4, fix the producer or the row via a PR; never bypass the check.
5. `banned_behavior` (name keywords) still fires independently.

### API down

1. Symptom: `PublicGetError`, scanner exit 3, `stop_reason=network_error`. A 4xx/429 on a Gamma page is not retried and ends the scanner with a traceback (exit 1); see `api-health-ratelimit`.
2. No markets/books written for that run (incomplete path).
3. Retry later with same allowlist; do not add alternate hosts.
4. Optional: `sentinel.halt(reason="api_down")` if Maker quotes should pause.

### Ledger corruption

1. Symptom: lines silently missing from `paper.jsonl` (`podlib.read_jsonl` skips undecodable lines without an error), or `risk_gate.evaluate` vetoes `invalid_risk_state` on the state `paper_ledger.load_state` built.
2. Freeze new operator intents. Do not hand-edit fills to invent balances.
3. Copy the file aside; open a `dev` PR with a repair plan. Operator approves any rewrite.
4. Re-run `paper_ledger` self-check only after pins verified.

### Heartbeat loss

1. Code has **no heartbeat** — TBD (not in repo). COORDINATION item 9 is a proposal.
2. Until implemented: treat "no bus lines for an unexpected long wall time" as operator judgment only; do not invent a threshold in chat and encode it without a PR.
3. When in doubt, paper halt: `sentinel.halt(reason="suspected_heartbeat_loss")`.

## Blameless postmortem template

```markdown
## Incident <id> — <UTC date>
- Summary:
- Severity:
- Timeline (UTC Z bus ts):
- Detection:
- Impact (paper only? markets? fills?):
- What went well:
- What went poorly (systems, not people):
- Action items (PR links, owners, no auto-merge):
- risk.toml unchanged? (sha)
```

## Verification

- After halt: quotes cancelled; bus `type=halt`; no new `fill` from the halt path (`tests/test_roles.py` sentinel test).
- Postmortem does not recommend `live_enabled=true`.
- `live_enabled=false` throughout.
