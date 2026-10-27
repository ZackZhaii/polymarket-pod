# External Account Accounting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (the user's chosen method) to implement this plan task-by-task after the integrated plan is reviewed and authorized. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a pure, externally evidenced account projection, independent reconciliation, and manual-settlement evidence validator without importing paper capital or moving funds.

**Architecture:** C0 validates strict records before these three pure functions consume them. Account projection derives balances and exposure from an approved external baseline plus immutable, deduplicated events; independent reconciliation compares complete evidence at a common watermark. SQLite store/lifecycle owns atomic persistence and reservation changes; these functions never mutate store or call transport.

**Tech Stack:** Python standard library `decimal.Decimal`, `copy`, `hashlib`, strict C0 JSON contracts; existing pytest for future tests. No new SDK or dependency.

**Spec:** `C:/Users/Noise/.codex/visualizations/2026/10/21/01a15d32-5ffb-7373-8ca9-d94e8e1f04ca/launch-profit-prep-20261025/02-execution-design/SPEC.md`; adjacent `ACCEPTANCE.md` and `IMPACT_MAP.md`. Current shared interface: `../00-control/INTERFACE_LOCK.md`, SHA256 `841dee78058355af8256f40a95d7a9aed8f4af3dd1efdb70f796ef8768fc85c4`.

## Global Constraints

- Planning only: `PROPOSED / NOT_ADOPTED / NOT_IMPLEMENTED / NOT_RUNTIME_ACTIVE`; live authorization is `NOT_GRANTED`. Nothing below is a current test result.
- Formal source `D:/projects/polymarket-pod` is READ_ONLY in this task. Baseline `origin/dev=0cb71883af8d94f92795ba4791020d5b91252562`; no queue/claim/PR/commit or product implementation now.
- Preserve `mode="paper"`, `live_enabled=false`, `require_human_gate=true`; actual capital and cumulative loss limits remain `UNKNOWN`.
- Prices, shares, and money are canonical finite decimal strings; integer fields require exact int, not bool. No float conversion, implicit zero, implicit USD/USDC/pUSD equivalence, or tolerance copied from paper.
- New paths below are future paths in an authorized worktree, not writes to the current checkout. Existing `src/reconcile.py`, `src/settlement.py`, paper ledger/bus/schema and their public behavior stay unchanged.
- C0 schemas/canonical hashes must land before A. L owns store transaction/command/reservation consistency. All record results are validated copies; no mutable alias to the caller.
- S0 uses synthetic offline evidence only. Actual event identity, asset mapping, rounding, finality and cash-reservation semantics are S1 official-evidence gates, still UNKNOWN. A fixture proves a contract, not a venue fact.
- No automatic redeem/deposit/withdraw/transfer, signing, retries, account queries, new transport, or financial-move method. Manual receipt evidence does not grant authority to perform its action.

## Review Focus

1. One transaction contains multiple fills: only a verified external event identity deduplicates (A1 tests `test_same_tx_distinct_verified_fill_ids_both_apply`, `test_same_event_id_conflict_holds`).
2. Final/late/reorg evidence conflicts with a baseline or terminal state: retain history, pending exposure and HOLD; never silently omit it (A1 `test_late_prebaseline_event_holds`, `test_reorg_requires_append_only_reversal`).
3. Cash frozen externally overlaps a local reserve or a pending fee: subtract proven overlapping liability exactly once; unknown overlap cannot create spending headroom (A1 `test_external_local_reservation_overlap_once`, A2 `test_unknown_overlap_holds`).
4. External/manual orders and money activity, missing pages or stale evidence: compare all account cash, positions, orders and event coverage, not only this program's orders (A2 tests below).
5. Resolved/redeemable, nonbinary payouts, unknown collateral/fee/finality: no early cash, no binary payout guess, and no hidden fee zero (A3 tests below).

## File map and locked public interfaces

Future creates under `D:/projects/polymarket-pod`:

| File | Exclusive responsibility |
|---|---|
| `src/execution/account_projection.py` | `project_account(start: Record, events: list[Record]) -> Record` |
| `src/execution/reconcile.py` | `reconcile(local: Record, external: Record, *, now: str) -> Record` |
| `src/execution/settlement_receipt.py` | `validate_settlement_evidence(evidence: Record, *, expected: Record) -> Record` |
| `tests/execution/test_account_projection.py` | Baseline, dedup, cash, position, reserve and event invariants |
| `tests/execution/test_account_reconcile.py` | A-owned pure reconciliation; L separately owns `test_restart_reconcile.py` |
| `tests/execution/test_settlement_receipt.py` | Manual evidence validation, no movement |
| `tests/fixtures/execution/accounting/*.json` | C0-valid synthetic records, identified below |

Imports are `src.execution.contracts.Record`, `validate_record`, `canonical_sha256`, and `ValidationError`; no import from `paper_ledger`, `ledger_store`, `podlib`, `m2_runner`, or `src.reconcile`. No private replacement public functions or aliases to different signatures.

## C0 exact schema requirements for integration

This section supplies root C0 requirements; root is the sole contract author. Record kinds remain the interface lock's kinds. Projection returns an `account_snapshot`; reconciliation and settlement validation use strict `receipt` subtypes `receipt_type="reconciliation"` and `receipt_type="settlement_validation"`, subject to root's integrated wrapper decision. Do not implement a competing validator. Unsupported extra keys reject at every nested level. All source references are redacted locators+SHA256, never raw authenticated responses/signatures. Missing required syntax raises `ValidationError`; valid but incomplete/unprovable economics returns HOLD/PENDING with stable problems and no permission.

### AccountSnapshot v1 (input baseline, local projection and independent external comparison)

Exact top-level keys:

`schema_version, snapshot_id, account_ref, venue, collateral_asset, chain_id, unit_scale, asof, observed_at, valid_until, status, source, source_evidence_ref, source_evidence_sha256, coverage, watermark, cash_total, cash_external_available, cash_external_reserved, cash_local_additional_reserved, cash_reserved, cash_available, positions, open_orders, reserve_groups, funding_events, projection, problems`.

- `schema_version=1`; `status` is `COMPLETE|HOLD`. Nonsecret `account_ref` and `venue` bind every child. `chain_id`/`unit_scale` are exact nonnegative ints (bool rejected; actual valid chain/scale fixed by evidence profile). `collateral_asset` is exact asset identity, not a display currency. All dates are zoned UTC strings.
- The six cash fields are decimal strings, or explicit `"UNKNOWN"` only for unprovable HOLD snapshots. Valid complete `cash_total` and external cash fields are nonnegative. A negative derived available balance is preserved as a signed diagnostic with HOLD, never clamped to zero or offered for reservation.
- `source` exact keys: `namespace, identity_contract_ref, identity_contract_sha256, account_product_ref, asset_contract_ref, cash_semantics_ref, finality_policy_ref`; each is a nonempty string or UNKNOWN. Any UNKNOWN in risk-critical source blocks COMPLETE. S0 source refs are explicitly synthetic, not S1 evidence.
- `coverage` exact keys: `cash_complete, positions_complete, open_orders_complete, fills_complete, funding_complete, fees_complete, pagination_complete, cursors_contiguous` (bool); `pages` (exact int >=0); `next_cursor` (null only on complete walk); `missing_ranges` (list of strings). Partial/unknown walk is HOLD even when returned rows are empty.
- `watermark` exact keys: `stream_id, cursor, source_sequence, block_number, block_hash, finalized_through, included_event_ids_sha256, baseline_approval_ref`; each position is string/int or explicitly UNKNOWN where the synthetic/evidence profile permits. `source_sequence` and `block_number` must not be bool. `baseline_approval_ref` binds the Owner-reviewed starting snapshot and its included event set. Cursor ordering is defined by a verified source contract, never lexicographic guessing.
- `positions[]` exact keys: `condition_id, token_id, outcome_index, settled_shares, pending_in_shares, pending_out_shares, available_shares, reserved_shares, position_evidence_ref`. Shares carry the `shares` unit; token mapping evidence must cover condition/outcome, including nonbinary outcomes. Unexpected short or over-reserved position is HOLD. Incoming pending fills immediately contribute to directional exposure; pending outgoing quantities do not erase exposure until externally final.
- `open_orders[]` exact keys: `exchange_order_id, command_id, ownership, condition_id, token_id, side, limit_price, cumulative_fill_shares, remaining_shares, status, fee_bound_remaining, external_reserved_amount, evidence_ref, evidence_sha256`. `ownership=LOCAL|EXTERNAL|UNKNOWN`; `command_id` can be null only for externally discovered/unknown orders. Unrecognized/manual order still appears and blocks new orders until reviewed. Order absence is usable only with complete account coverage and matching watermark.
- `reserve_groups[]` exact keys: `reservation_id, command_id, exchange_order_id, liability_kind, local_amount, external_reservation_ids, external_overlap_amount, mapping_evidence_ref, source_version, source_sha256`. `liability_kind=OPEN_ORDER|SUBMIT_UNKNOWN|PENDING_DEBIT|PENDING_FEE`; order ID nullable with UNKNOWN reserve retained. External-overlap IDs cannot be reused across groups. Quantities must be for the same account/chain/asset, with nonnegative overlap <= local_amount and <= proved external covered amount. Unproven mapping is HOLD, never presumed overlap.
- `funding_events` is the complete list of included account-event IDs covering fees, deposit, withdrawal, transfer and redemption observations, not a netted unexplained cash adjustment.
- `projection` exact keys: `baseline_sha256, applied_event_ids, duplicate_event_ids, pending_event_ids, conflict_event_ids, journal_event_ids_sha256, source_version, source_sha256`. Lists contain canonical namespaced verified IDs; hashes bind content. Source version/hash bind the immutable L snapshot that supplied reserve groups, not a second authority.
- `problems` is a sorted unique list of stable codes. A COMPLETE account has `problems=[]`; no field required for risk can be UNKNOWN.

### AccountEvent v1

Exact top-level keys:

`schema_version, event_id, account_ref, venue, source_namespace, external_event_id, identity_contract_ref, event_type, source_sequence, source_timestamp, observed_at, command_id, exchange_order_id, transaction_hash, block_number, block_hash, finality, asset, cash_delta, token_deltas, fill, fee, reservation_change, reverses_event_id, evidence_ref, evidence_sha256`.

- `event_id` is the canonical namespaced identity derived from already verified `source_namespace+external_event_id`, not tx hash alone. `identity_contract_ref` proves uniqueness. Same event ID+canonical economic payload is a no-op; same ID+different payload => `EVENT_ID_CONFLICT` HOLD. Observation retrieval timestamps are metadata and must not turn an identical economic event into a conflicting fill.
- `event_type=FILL|FEE|ORDER_UPDATE|RESERVATION_UPDATE|DEPOSIT|WITHDRAWAL|TRANSFER|REDEMPTION|REVERSAL`. `finality=FINALIZED|PENDING|REORGED|UNKNOWN`; source-specific finality acceptance is an evidence profile, not a magic confirmation count.
- `asset` exact keys `chain_id, collateral_asset, unit_scale`; matches snapshot or HOLD `ASSET_MISMATCH`. A fee in another asset is tracked as UNKNOWN valuation/HOLD, never subtracted as same currency.
- `cash_delta` is a signed decimal string or UNKNOWN. `token_deltas[]` exact keys `condition_id, token_id, settled_delta, pending_in_delta, pending_out_delta` (decimal strings). Only actual finalized evidence can credit available collateral or decrease settled exposure. No arbitrary caller-authored cash delta is accepted: type, supporting fill/funding evidence and amount identity must agree.
- `fill` is null except FILL, otherwise exact keys `fill_id, condition_id, token_id, side, shares, price, gross_cash_amount, fee_event_ids, cumulative_fill_shares, remaining_shares`. `fill_id` is separately verified if source event ID describes more than one fill; normalize each uniquely evidenced fill, otherwise HOLD `EVENT_ID_UNVERIFIED`. One tx can have many fills. No tx/token/side synthetic uniqueness claim.
- `fee` is null except known fee evidence, otherwise exact keys `fee_id, amount, asset, linked_event_id, included_in_cash_delta, finality`. Fee ID+link prevents applying an inclusive fill debit and the same fee twice. Unknown fee amount remains UNKNOWN; still-known exposure is retained and reservation cannot be released.
- `reservation_change` is null or exact keys `reservation_id, operation, amount, external_overlap_amount, source_version, source_sha256`; operation `CREATE|CONSUME|RELEASE|RECLASSIFY`. Only L's committed reserve snapshot/transition is accepted; a public order update alone cannot fabricate local reserve release. Atomic storage treatment belongs to L.
- Nullable transaction/block/order fields have explicit identity/finality reason in source evidence; null is never proof of finality or rejection. `reverses_event_id` is nonnull only REVERSAL, which references exact original event and verified reversing evidence. Do not overwrite/rewrite original event; reorg always HOLD even if an explicit reversal can compute an explanatory projection.

### Reconciliation receipt subtype

Exact keys: `schema_version, receipt_type, account_ref, verdict, now, local_snapshot_sha256, external_snapshot_sha256, common_watermark, checked_dimensions, differences, residual_orders, residual_positions, residual_reservations, unknowns, problems, stop_new_orders, exchange_action`.

`receipt_type="reconciliation"`; `verdict=MATCH|HOLD`; `checked_dimensions` exactly `cash, positions, open_orders, fills, funding, fees, reserves, finality` mapped to `MATCH|MISMATCH|UNKNOWN`; `differences[]` exact `dimension, key, local_value, external_value, evidence_ref`. `common_watermark` is the validated watermark shape. Residual arrays retain corresponding account rows; do not imply emptiness when coverage is incomplete. `stop_new_orders=(verdict=="HOLD")`; `exchange_action="none"`. A MATCH can have positions/open orders; it proves reconciliation, not flatness or an authorization. Missing/expired evidence is HOLD even if numeric balances happen to match.

### Settlement evidence and receipt subtype

`settlement_evidence` exact keys: `schema_version, evidence_id, account_ref, venue, condition_id, token_id, asset, shares_redeemed, payout_per_share, gross_collateral_credit, fees, net_collateral_credit, token_balance_before, token_balance_after, collateral_before, collateral_after, external_event_ids, transaction_hash, chain_id, block_number, block_hash, finality, finality_policy_ref, receipt_status, action_origin, owner_action_ref, account_before_sha256, account_after_sha256, source_refs, evidence_sha256`.

- `fees[]` exact keys `fee_id, amount, asset, included_in_balance_delta, evidence_ref`; no gas conversion to collateral without a verified conversion source, and no implicit zero. `source_refs[]` exact `source, ref, sha256, observed_at, complete`.
- `action_origin="HUMAN_EXTERNAL"`, nonempty `owner_action_ref`; `receipt_status=RESOLVED_ONLY|REDEEMABLE|PENDING|FINALIZED|REORGED|UNKNOWN`. `payout_per_share` may be a verified nonbinary decimal; it is never guessed as 0 or 1. An unsupported payoff/asset conversion returns HOLD, without performing it.
- `expected` is a C0 `settlement_evidence` expectation record binding account/condition/token/asset/before snapshot/manual-action scope and finality-policy/source references; target action status FINALIZED is an expectation, not evidence. Unknown expected account/asset/finality blocks VERIFIED. `expected` monetary expected values may be UNKNOWN only where explicitly allowed to compare against actual evidence, never to imply approval or waive a discrepancy.
- Result exact keys: `schema_version, receipt_type, evidence_id, expected_sha256, evidence_sha256, verdict, settlement_stage, cash_credit_verified, verified_net_collateral_credit, fee_totals_by_asset, external_event_ids, finality, problems, unknowns, exchange_action`.
- `receipt_type="settlement_validation"`; `verdict=VERIFIED|PENDING|HOLD`; `settlement_stage=RESOLVED_ONLY|REDEEMABLE|PENDING|CASH_FINAL|REORGED|UNKNOWN`. `verified_net_collateral_credit` is UNKNOWN unless final cash identities and all required fees are proven. `exchange_action="none"`; output is evidence only and never books money itself.

## Accounting invariants and ordering

1. Start is a complete Owner-reviewed external snapshot at an explicit cursor/block/hash with included-event-set digest. It is not `PAPER_EQUITY_USD`; missing initial balance or approval does not create zero capital. It must represent all activity through the baseline watermark exactly once.
2. Only post-baseline contiguous events apply. A pre-baseline event explicitly proved included is skipped once. A newly discovered earlier event without inclusion proof => `BASELINE_COVERAGE_GAP` HOLD; no silent skip/rebase. Rebuild requires a separately reviewed baseline and append-only reconciliation evidence, not in-place history edits.
3. Event delivery order does not change known additive cash/position deltas; source sequence/cumulative order observations determine order state. Contradictory cumulative fill/remaining totals, a gap, or a late fill inconsistent with terminal observations => HOLD while preserving the fill and exposure. A duplicate whose only retrieval metadata changed stays duplicate.
4. Finalized cash = external baseline cash + uniquely applied finalized cash deltas, with separately linked fees applied exactly once. Pending incoming credit is not spendable; pending debit/fee liability stays reserved. Pending fills immediately enter pending directional exposure. A reorg records its original evidence and an explicit reversal, never deletes history, never automatically resumes orders.
5. External source identity: `cash_total = cash_external_available + cash_external_reserved`. Local effective identity: `cash_reserved = cash_external_reserved + cash_local_additional_reserved`; `cash_available = cash_total - cash_reserved`.
6. `cash_local_additional_reserved = sum(local_amount - external_overlap_amount)` over disjoint reserve groups. Each covered external unit and local liability can appear once. Example: total 10, external reserved 0.5, local liability 0.51, proven overlap 0.5 => additional 0.01, effective reserved 0.51, available 9.49. Do not deduct 0.5 plus 0.51. Unknown overlap produces HOLD and no new permit; it never grants a larger available amount.
7. After buy 0.4 shares at 0.5 and known fee 0.01, finalized cash moves 10 -> 9.79; settled shares +0.4. If original order is 1 share and remaining fee bound is 0.01, remaining liability is 0.6*0.5+0.01=0.31; available becomes 9.48 when reserved only once. Exposure includes 0.4 filled shares plus remaining 0.6; a cancel request does not change these figures. Fill consumed cost, cash debit and remaining reservation change must be one L transaction.
8. A terminal order has reserve released only after complete consistent fill, fee, final order and account evidence. Cancellation acknowledgement/HTTP200 is not this evidence. Same terminal state with newly discovered fill changes projection/exposure and HOLD, never drops the fill.
9. External/manual orders, deposits, withdrawals, transfers, fees and redemptions must be covered. An unrecognized but valid external money event can be displayed in a candidate projection; it remains HOLD pending approved classification, never creates approved capital or clears a mismatch by adjustment.
10. Reconciliation compares same account/asset/product and common finalized cutoff plus pending/reserve state, not snapshots from different cutoffs. For cash, compare factual total/external available/external reserved; an independent external source does not attest local additional reserve. Validate local derived available/reserved using the L snapshot and prove external overlaps against external rows; do not report the intentional local additional reserve as an unexplained remote cash mismatch. It does not rewrite either input. Unknown product, precision, rounding, source freshness, token mapping, fee or finality prevents MATCH. S0 fixture uses exact equal decimal identities; S1 tolerance/rounding must be explicitly verified and reviewed.
11. Resolution is valuation information; REDEEMABLE/PENDING does not credit cash. Final manual settlement requires identified tx+block/hash/finality, token decrease, collateral net increase, all fees and consistent before/after account evidence. Receipt validator returns a fact; applying an independently verified account event remains L's append-only operation.

## Synthetic fixture contract

Future fixture files: `baseline_10.json`, `fill_04.json`, `fill_06_same_tx.json`, `fee_001.json`, `partial_after_04.json`, `manual_redeem_final.json`. Test helper `load(name: str) -> Record` reads and validates a fixture; helper `variant(record: Record, **changes: object) -> Record` makes a deep copy then validates it. Helpers live in the owning test module; C0-rejected variants use direct deep-copy mutations before the function call.

Baseline B has cash_total/available `"10"`, all reserves `"0"`, no positions/orders/events, source namespace `synthetic-account-v1`, source_sequence 100, cursor `"c100"`, block 100/hash `"synthetic-block-100"`, finalized_through `"100"`, asof/observed_at `2026-10-25T00:00:00Z`, valid_until `2026-10-25T00:01:00Z`, all coverage flags true, pages 1, next_cursor null, no gaps, account `"fixture-account"`, venue `"fixture-venue"`, collateral `"fixture-collateral"`, chain 999999, scale 6. These are test-only identifiers, never a Polymarket protocol assertion. Baseline approval `"fixture-owner-baseline"` explicitly includes no post-100 events.

F1: verified ID `synthetic-account-v1:fill-1`, seq101, tx `"synthetic-tx-1"`, FINALIZED BUY 0.4 at 0.5, gross 0.2, cash_delta -0.2, token settled delta +0.4. F2: distinct verified ID `...:fill-2`, seq102, same tx/order/token/side, BUY0.6 at0.5, gross0.3, cash_delta-0.3. Fees separately reference F1 via fee ID `fee-1`, amount0.01, cash_delta-0.01, `included_in_cash_delta=false` relative to fill; the fee event itself is the sole debit. The valid fee-zero case needs an explicit fee evidence row with amount `"0"`; absent fee is UNKNOWN, not zero.

Final manual redemption R: token before1/after0, collateral before10/after10.59, gross0.6, explicit collateral fee0.01, net0.59, verified payout0.6, shares1, exact same asset/chain, FINALIZED synthetic block/evidence, HUMAN_EXTERNAL owner reference. This fixture deliberately prevents binary payout assumptions.

### Task A1: External baseline and deterministic account projection

**Files:** Create `src/execution/account_projection.py`, `tests/execution/test_account_projection.py`, and the five account/fill/fee fixtures above in `tests/fixtures/execution/accounting/`.

**Interfaces:** Consumes C0 `validate_record(kind: str, value: Record) -> Record` and `canonical_sha256(value: Record) -> str`; consumes validated L reserve groups plus source version/hash embedded in baseline/events. Produces locked `project_account(start: Record, events: list[Record]) -> Record`, a new account_snapshot preserving all known and unknown residual exposure. No store mutation.

- [ ] **Step 1: Write failing tests with these exact names and assertions.** Use deepcopy inputs and assert they equal originals after every call.

| Test | Exact result assertions |
|---|---|
| `test_baseline_missing_external_cash_rejects` | missing `cash_total` raises `ValidationError`; no PAPER_EQUITY fallback |
| `test_missing_baseline_approval_holds` | `status == "HOLD"`; `"BASELINE_UNAPPROVED" in problems` |
| `test_decimal_and_bool_invalid_reject` | float `0.4`, `"NaN"`, `"Infinity"`, `chain_id=True` each raises `ValidationError` |
| `test_same_tx_distinct_verified_fill_ids_both_apply` | B+[F1,F2,explicit zero fees] gives `cash_total == "9.5"`, settled shares `"1"`, two applied fill IDs |
| `test_exact_repeat_applies_once` | B+[F1,F1,fee1] gives cash `"9.79"`, shares `"0.4"`; F1 in duplicates once |
| `test_repeat_with_new_observed_at_is_same_economic_event` | only retrieval time changes; same cash/shares as prior row; no EVENT_ID_CONFLICT |
| `test_same_event_id_conflict_holds` | F1 repeated with shares0.5 gives HOLD, EVENT_ID_CONFLICT, no silently selected authoritative winner |
| `test_unverified_tx_only_identity_holds` | identity ref UNKNOWN with transaction present gives EVENT_ID_UNVERIFIED/HOLD |
| `test_out_of_order_fill_delivery_same_projection` | F1/F2 delivery permutations have identical cash/positions/applied ID set and economic projection hash |
| `test_late_prebaseline_event_holds` | seq99 absent from inclusion proof gives BASELINE_COVERAGE_GAP/HOLD; evidence ID retained |
| `test_fee_inclusive_debit_not_subtracted_twice` | F1 inclusive debit-0.21 plus linked informational fee0.01 with its own cash_delta0 and included_in_cash_delta true gives total9.79, never9.78 |
| `test_unknown_fee_retains_reserve_and_holds` | fee amount UNKNOWN => FEE_UNKNOWN/HOLD and no release, positive known filled exposure retained |
| `test_external_local_reservation_overlap_once` | total10/external0.5/local0.51/overlap0.5 => additional0.01/reserved0.51/available9.49 |
| `test_partial_fill_cash_position_remaining_reserve` | finalized 0.4 fill+0.01 fee gives total9.79, settled0.4, remaining0.6, reserve0.31, available9.48 |
| `test_cancel_request_does_not_release_reserve` | request-only order event leaves remaining reserve0.31 and settled0.4 |
| `test_terminal_late_fill_kept_and_holds` | late identified fill appended after terminal updates shares/cash evidence, retains ID, gives TERMINAL_FILL_CONFLICT/HOLD |
| `test_pending_credit_not_spendable` | pending deposit5 leaves total10 and available10; deposit ID in pending list; HOLD |
| `test_pending_fill_keeps_exposure_and_debit_liability` | pending F1 adds pending_in0.4, no final cash credit, debit0.2 remains reserved |
| `test_reorg_requires_append_only_reversal` | REORGED event without reversal retains original history and REORG/HOLD; verified reversal restores numeric baseline but still HOLD |
| `test_foreign_asset_or_token_mapping_holds` | mismatched collateral/chain or nonproven token/condition gives ASSET_MISMATCH/TOKEN_MAPPING_UNKNOWN, never conversion |

- [ ] **Step 2: Run `python -m pytest tests/execution/test_account_projection.py -q` in the authorized worktree.** Expected initial FAIL: module missing/import error; once C0 exists, tests fail for missing projection implementation. Record exact failing assertion and command in future evidence.
- [ ] **Step 3: Implement `project_account(start: Record, events: list[Record]) -> Record` in `src/execution/account_projection.py`.** Apply C0 copies, baseline inclusion checks, economic-ID dedup, exact Decimal event math, pending/finality separation, reserve overlap invariants above. Return validated account_snapshot; never mutate store or infer API IDs.
- [ ] **Step 4: Run the Step 2 command; expected all PASS.** Also run C0 contract suite for newly registered nested shapes; exact C0 file/command comes from root Task C0, not a second validator here.
- [ ] **Step 5: Future local commit after tests and authorization.** `git add src/execution/account_projection.py tests/execution/test_account_projection.py tests/fixtures/execution/accounting`; `git -c user.name=ZackZhaii -c user.email=17690114@qq.com commit -m "feat(execution): project externally evidenced account events"`. No push/merge; queue rules still apply.

### Task A2: Independent cash, position, order and funding reconciliation

**Files:** Create `src/execution/reconcile.py` and A-owned `tests/execution/test_account_reconcile.py`. L exclusively owns separate `tests/execution/test_restart_reconcile.py` for restart/controller integration; this plan does not edit that file.

**Interfaces:** Consumes C0 validated account snapshots (A1 result and independent external result) at common watermark; produces locked `reconcile(local: Record, external: Record, *, now: str) -> Record` and exact reconciliation receipt above. L may commit `ACCOUNT_RECONCILED` only from valid matching evidence+snapshot hashes+coverage/watermark; function itself writes nothing. HOLD requires L/S durable stop latch, not arbitrary account overwrite.

- [ ] **Step 1: Write failing tests with exact assertions.** `now="2026-10-25T00:00:30Z"` for fresh B.

| Test | Exact result assertions |
|---|---|
| `test_complete_same_watermark_match_not_flat_claim` | identical snapshots with known position/order => verdict MATCH, all eight checked dimensions MATCH, residual position/order retained, exchange_action none |
| `test_cash_match_but_position_or_order_mismatch_holds` | equal cash + unexpected external position/order => HOLD, stop_new_orders true, dimension MISMATCH |
| `test_external_manual_order_holds_even_if_cash_matches` | ownership EXTERNAL order retained in residual_orders; EXTERNAL_ORDER_UNREVIEWED |
| `test_external_deposit_transfer_withdrawal_not_adjusted_away` | unreviewed deposit/transfer/withdrawal event gives EXTERNAL_MONEY_UNREVIEWED; both inputs byte-equivalent to originals |
| `test_missing_page_cursor_gap_or_stale_holds` | each false coverage flag/non-null next_cursor/missing range/now past valid_until => COVERAGE_INCOMPLETE/CURSOR_GAP/ACCOUNT_STALE, verdict HOLD |
| `test_unknown_missing_cash_fee_is_not_zero` | UNKNOWN cash/fee => checked dimension UNKNOWN and HOLD; no MATCH despite other equality |
| `test_different_cutoffs_do_not_compare_as_equal` | different cursor/block/finalized cutoff without common complete prefix => WATERMARK_MISMATCH/HOLD |
| `test_unknown_overlap_holds` | duplicate external overlap ID or unproven mapping => RESERVE_OVERLAP_UNPROVEN/HOLD; no released reserve |
| `test_known_local_additional_reserve_not_remote_cash_mismatch` | remote factual total10/external reserved0.5 plus local additional0.01 => cash dimension MATCH, local effective available9.49 retained; remote source's available9.5 is not mistaken for local spendable cash |
| `test_reused_external_reserve_cannot_cover_two_local_groups` | sum overlap exceeds covered external amount => RESERVE_OVERLAP_REUSED/HOLD |
| `test_currency_chain_or_product_mismatch_holds` | display-equivalent but different collateral/chain/product => ASSET_MISMATCH/ACCOUNT_PRODUCT_MISMATCH/HOLD |
| `test_cash_deficit_holds_without_clamp` | liabilities11 vs total10 => derived available-1 retained, CASH_DEFICIT/HOLD |
| `test_missing_finality_or_fee_event_coverage_holds` | cash equal but missing finality/fee coverage => UNKNOWN dimension/HOLD |
| `test_reconcile_is_pure_and_repeatable` | repeated call same dictionaries+now => equal result; no file/socket/transport call permitted |

- [ ] **Step 2: Run `python -m pytest tests/execution/test_account_reconcile.py -q`.** Expected initial module/function missing FAIL.
- [ ] **Step 3: Implement `reconcile(local: Record, external: Record, *, now: str) -> Record` in `src/execution/reconcile.py`.** Compare complete same-cutoff evidence and exact asset units, then return explanatory MATCH/HOLD receipt. Do not repair books, release funds, make calls or clear a stop latch.
- [ ] **Step 4: Run `python -m pytest tests/execution/test_account_projection.py tests/execution/test_account_reconcile.py tests/test_reconcile.py tests/test_reconcile_ledger_gaps.py tests/test_reconcile_usage_gaps.py -q`; expected all PASS.** L integration then tests recovery reconciles before submit, complete pagination and reserve version/hash revalidation. Existing public CLI/exit/ledger semantics must retain their own tests.
- [ ] **Step 5: Future authorized commit:** `git add src/execution/reconcile.py tests/execution/test_account_reconcile.py`; `git -c user.name=ZackZhaii -c user.email=17690114@qq.com commit -m "feat(execution): reconcile complete external account evidence"`.

### Task A3: Manual settlement evidence receipt without funds movement

**Files:** Create `src/execution/settlement_receipt.py`, `tests/execution/test_settlement_receipt.py`, `tests/fixtures/execution/accounting/manual_redeem_final.json`.

**Interfaces:** Consumes C0 `settlement_evidence` and expectation records bound to A1/A2 before/after hashes. Produces locked `validate_settlement_evidence(evidence: Record, *, expected: Record) -> Record`, exact settlement_validation receipt above. No automatic booking, transport, redeem, transfer, withdrawal or financial method.

- [ ] **Step 1: Write failing tests with these exact names/assertions.**

| Test | Exact result assertions |
|---|---|
| `test_resolved_or_redeemable_does_not_credit_cash` | stages RESOLVED_ONLY/REDEEMABLE => verdict PENDING, cash_credit_verified false, verified_net_collateral_credit UNKNOWN |
| `test_pending_redemption_is_not_cash_final` | receipt_status PENDING => PENDING, no cash credit |
| `test_final_manual_receipt_proves_nonbinary_net_cash` | R => VERIFIED/CASH_FINAL, credit true, verified_net0.59, collateral fee0.01, exchange_action none |
| `test_balance_or_token_delta_mismatch_holds` | collateral_after10.60 or token_after0.1 => BALANCE_DELTA_MISMATCH/TOKEN_DELTA_MISMATCH and HOLD |
| `test_fee_unknown_or_foreign_fee_currency_holds` | missing/UNKNOWN fee amount or unproven foreign asset conversion => FEE_UNKNOWN/FEE_ASSET_UNKNOWN and HOLD; no implicit zero |
| `test_unknown_finality_or_missing_block_hash_holds` | finality UNKNOWN/missing required block/hash => FINALITY_UNKNOWN/HOLD or strict ValidationError for missing field |
| `test_reorg_never_overwrites_prior_receipt` | REORGED evidence => HOLD/REORGED, no cash-credit verification; original input unchanged |
| `test_wrong_account_chain_token_or_source_holds` | each mismatch vs expected => scoped stable mismatch problem and HOLD |
| `test_no_manual_owner_reference_holds` | UNKNOWN owner_action_ref => OWNER_ACTION_UNVERIFIED/HOLD; never invoke an action |
| `test_settlement_evidence_requires_verified_event_ids` | transaction only/no verified external event IDs => EVENT_ID_UNVERIFIED/HOLD |
| `test_no_financial_io` | filesystem writes/socket/transport tripwires receive zero calls for every result branch; module exposes no redeem/deposit/withdraw/transfer method |

- [ ] **Step 2: Run `python -m pytest tests/execution/test_settlement_receipt.py -q`; expected initial module missing FAIL.**
- [ ] **Step 3: Implement `validate_settlement_evidence(evidence: Record, *, expected: Record) -> Record` in `src/execution/settlement_receipt.py`.** Validate scoped before/after evidence, exact units/fees and finality; return VERIFIED only for observed cash-final evidence, otherwise explanatory PENDING/HOLD. No binary assumption or actual funds action.
- [ ] **Step 4: Run `python -m pytest tests/execution/test_account_projection.py tests/execution/test_account_reconcile.py tests/execution/test_settlement_receipt.py tests/test_reconcile.py tests/test_settlement.py tests/test_runner_settlement.py tests/test_settlement_risk_loss.py -q`; expected all PASS.** Root V subsequently runs preserved guard/pins/no-live/import-tree and whole-paper regression; A does not claim those pass now.
- [ ] **Step 5: Future authorized commit:** `git add src/execution/settlement_receipt.py tests/execution/test_settlement_receipt.py tests/fixtures/execution/accounting/manual_redeem_final.json`; `git -c user.name=ZackZhaii -c user.email=17690114@qq.com commit -m "feat(execution): validate manual settlement cash evidence"`.

## Evidence gates, acceptance map and handoff

| Acceptance | A-owned evidence | Other owner/remaining gate |
|---|---|---|
| X07 | A1 partial fill cash+positions+remaining reserve | P caps and L atomic consume |
| X08 | A1 unique IDs/multi-fill/repeat/conflict/late fill | T official external identity contract before S1 |
| X09 | A2 external/manual order/money/completeness | L restart before-submit and expired approval tests |
| X10 | A1/A2 cash/reserve/source units/coverage/freshness | C0 strict fields; T official cash/freezing/fee definitions |
| X11 | A3 resolved/pending/final/reorg/nonbinary/fees | S1 actual manual evidence/finality profile UNKNOWN |
| X06, X12 | A1 cancellation retains liabilities; A2 residuals | L confirmed cancellation and S stop/export |
| X14 | Unknown fees/assets remain UNKNOWN | E economics and S receipt consumers |
| X16 | Existing reconcile/settlement tests retained | V guard/import/tree/paper whole-regression |

**S1 STOP conditions:** no verified event-ID uniqueness per source; unidentified multi-fill row; account product/collateral/chain/precision/rounding or cash freezing semantics UNKNOWN; finality profile unknown; missing fee asset/amount; baseline ownership/inclusion/watermark unproved; any unexplained external order or funding activity. S0 can finish with synthetic contract tests while all these remain blockers for actual account adoption.

**Not verified now:** actual account balance, actual API field or cursor behavior, fees, redemption mechanics, finality, current source test results, runtime durability, real event uniqueness. No network/provider/account test ran here. Configuration from coordinator says gpt-6.1-sol/high; returned model identity/usage/cost UNKNOWN; external provider executions 0.

**Self-review:** All three locked signatures preserved; no product code or function bodies written. Exact fields, event identity, multi-fill, late/reorg, external money, reserve overlap and manual settlement have assigned tests. A2's exclusive test path is fixed as `tests/execution/test_account_reconcile.py`; L's restart path stays separate. Root must settle receipt subtype wrapper in integrated C0 before consumers execute; this is a planning integration decision, not permission to improvise runtime schemas. No plan in this lane authorizes implementation or activation.
