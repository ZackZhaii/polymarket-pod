---
name: resolution-rules
description: Use when checking how a Polymarket market resolves before a paper forecast or fill in polymarket-pod. Read resolution text from Gamma fields we already fetch when present; flag ambiguity. UMA dispute status needs The Graph key — out of Phase 0.
---

# resolution-rules

Official overview: https://docs.polymarket.com/concepts/resolution.md (UMA Optimistic Oracle for prediction markets; Chainlink TWAP for up-down). Phase 0: read-only; no redeem/split/merge on-chain.

## Allowed

- Read resolution-related fields **from public Gamma payloads already retrieved** by `scanner.fetch_all_markets` / fixtures (`tests/fixtures/gamma_markets.json`).
- Flag ambiguous rules in a NewsForecaster `note` or Reflector proposal.
- Refuse paper intent when rules are unreadable or contradictory (operator judgment → RiskGate veto via not trading).

## Forbidden

- Calling The Graph, UMA portal APIs, or any keyed endpoint for dispute status — **needs API key → not in Phase 0** (TBD until Operator approves a keyless alternative).
- On-chain redeem / CTF ops (`docs/ROSTER.md` Reconciler is no-trade; not implemented as live).
- Inventing resolution outcomes to mark `y` in Brier labels without a cited source.

## Steps

### 1. What we actually normalize today

`scanner.normalize` persists: `question, slug, condition_id, version, end_date, closed, fees_enabled (a real Gamma bool, else None = unknown, never free; #116), accepting_orders, outcomes, outcome_prices, …` — it does **not** currently copy `description`, `resolutionSource`, or UMA status fields.

- If raw Gamma JSON in a fixture/response contains `description` / `resolutionSource` / similar: read them for research notes, and file a PR to add them to `normalize` if we want them in `data/markets.jsonl`.
- Until then: mark structured resolution capture as **TBD (not in repo)**; bots may only quote `question` + `end_date` from normalized rows.

### 2. Ambiguity flags (heuristic, document in note)

Flag when any apply:

- Question depends on undefined thresholds ("significant", "major") with no linked source in the text you can see.
- End date passed but `closed=false` still (stale Gamma row) — do not treat as resolved.
- Multiple plausible official sources disagree (news conflict) — probability skill must not pretend certainty (`untrusted-input`).
- Neg-risk / multi-outcome "Other" placeholder (official neg-risk docs: avoid trading unnamed placeholders).

### 3. UMA dispute status

Resolution.md flow: propose → 2h challenge → possible DVM vote. Tracking live disputes via The Graph indexed data typically needs a key.

- Phase 0: **do not query**. Note in reports: "UMA dispute status not checked (keyed API)".
- After resolution is public and market `closed=true` in Gamma, Quant may label `y` for Brier from a **saved** fixture, not a live scrape inside pytest.

### 4. Tie-in to paper path

- Endgame band 0.98–0.99 is filtered out by scanner long-tail (`scanner.MAX_PRICE = 0.97`, overridable only by `scanner_max_price` in risk.toml, which the Operator owns) — COORDINATION backlog item 10; Endgame needs unfiltered data (separate task).
- Expiry: `expiry_rule.MIN_HOURS_TO_EXPIRY` (24h) rejects near-expiry before forecast/fill.

## Verification

- Any resolution claim cites Gamma field or official docs URL, or is marked TBD.
- No Graph/UMA keyed calls in `src/` or tests.
- `live_enabled=false`.
