# C0 registration packet — queue task EI

This branch proposes EI after existing EG; EI is not claimed to be currently queued on `dev`. Implementation may begin only after this registration PR merges into `dev`, EG closes, EI becomes the first `todo`, a legal one-hour claim is acquired, and project pins still pass. This PR neither claims nor implements C0.

## Fresh approval and boundaries

User approval, literal:

> 同意这份正式实施计划，按项目门禁合法入队，先完成C0，再按依赖分工并行实施。启用最大量Agents协助，尽快完成上线准备，本轮保持交易关闭。

This approval permits lawful enqueue and the approved dependency order. This PR registers C0 only as queue ID EI. Later P/T/L/A/S/E/V packages are not registered or authorized to start by this PR. The snapshots' prior `NOT_IMPLEMENTED` / `NOT_AUTHORIZED` / pending-review text records their historical sealing state. Current approval authority is recorded here without editing those bytes. No new runtime, live, SDK, account, credentials, signing, funds, deployment or trading authority is granted. `mode=paper`, `live_enabled=false`, `require_human_gate=true` remain mandatory.

## GitHub-readable C0 source routing

All snapshot Windows paths are historical provenance, never remote reading instructions. Read the seven actual relative inputs below. Resolve original absolute paths under historical root `C:/Users/Noise/.codex/visualizations/2026/10/21/01a15d32-5ffb-7373-8ca9-d94e8e1f04ca/` by the source suffix table. Resolve `00-control/` and `05-accounting/` relative references against historical `implementation-plan-20261025/`. The master's `../launch-profit-prep-20261025/02-execution-design/SPEC.md` and C0's absolute SPEC path both resolve to `snapshots/SPEC.md`.

| Original relative source path | Actual C0 input | C0 role |
|---|---|---|
| `implementation-plan-20261025/00-control/C0_CONTRACT_TASK.md` | [C0 task](snapshots/C0_CONTRACT_TASK.md) | Strict schemas, interfaces, tests and hashes |
| `implementation-plan-20261025/05-accounting/ACCOUNTING_PLAN.md` | [Accounting plan](snapshots/ACCOUNTING_PLAN.md) | Only referenced account_event lines 74–83 and settlement lines 93–97 nested shapes; C0 overrides apply |
| `implementation-plan-20261025/IMPLEMENTATION_PLAN.md` | [Master implementation plan](snapshots/IMPLEMENTATION_PLAN.md) | C0 scope, dependency order and RF context |
| `implementation-plan-20261025/00-control/INTERFACE_LOCK.md` | [Interface lock](snapshots/INTERFACE_LOCK.md) | Shared names and signatures |
| `implementation-plan-20261025/00-control/C0_SCHEMA_DECISIONS.md` | [Schema decisions](snapshots/C0_SCHEMA_DECISIONS.md) | Normative integration decisions |
| `implementation-plan-20261025/00-control/COVERAGE_FINAL.json` | [Final coverage](snapshots/COVERAGE_FINAL.json) | Planned acceptance ownership, PLANNED_NOT_RUN |
| `launch-profit-prep-20261025/02-execution-design/SPEC.md` | [Approved SPEC](snapshots/SPEC.md) | Design boundary and acceptance intent |

The master's downstream lane links, historical ACCEPTANCE/launch master/START/endpoint/health links and other component plans are contextual provenance, not required inputs to implement C0. This packet does not authorize starting those tasks. ACCOUNTING_PLAN supplies the two referenced nested tables only; its A implementation tasks are outside C0. C0 contract tables, master integration overrides and schema decisions settle field conflicts.

[Source bindings](SOURCE_BINDINGS.json) record each original source suffix, actual snapshot path, SHA256 and bytes. All six plan copies match INPUT_FREEZE_V2 SHA256 `02b258a7f1fed951e025ae2a836491cc44b87b08b29355d909775cb52bf4d18f`; approved plan delivery ZIP SHA256 is `d454f4f517f06706ff9c4bd941208cceb616ccbc525ecca59ad80a2e5a9cbda5`. SPEC bytes match the approved original delivery ZIP SHA256 `078b152de683738a14d686e437ebdb769945d334b15293471188db30998fffdd`. Digests bind supplied bytes, not live permission or evidence authenticity. Original bytes and hashes remain preserved.

## Controller implementation interpretation

[Implementation notes](IMPLEMENTATION_NOTES.md) are this registration controller's current interpretations after nine static preflight lanes. They are supplied for independent non-author review, not part of the original seven approved snapshots and not C0 implementation or test results. Their separate root-source binding and SHA are recorded in SOURCE_BINDINGS.json. They introduce no additional record kind, public function, dependency, permission, runtime or risk threshold; all queue and approval boundaries above remain in force. The note neither authorizes later lanes nor waives missing evidence or HOLD.

## Future C0 exclusive write domain

Only after the queue gates pass, C0 may create exactly four files plus its normal LOOP/state completion:

- `src/execution/__init__.py` — empty, no imports.
- `src/execution/contracts.py` — `Record = dict[str, object]`, `ValidationError(ValueError)`, `validate_record(kind: str, value: Record) -> Record`, `canonical_sha256(value: Record) -> str`, `decode_record(kind: str, raw: bytes) -> Record` only.
- `tests/execution/test_contracts.py` — contract/decoder/hash acceptance.
- `tests/fixtures/execution/contracts.json` — complete union fixtures.

C0 requires strict 14 top-level kinds and nested unions, deep validated copies, zero IO, raw duplicate-key rejection, bounded finite types and noncircular hash preimages. No other source, exports, configuration, dependencies or runtime integration belongs to C0. The one-hour budget begins on its future legal claim, not during this registration.

All seven named tests and their parameter sets must actually execute without skip/xfail:

- `test_contract_roundtrip_all_kinds_and_unions`
- `test_contract_rejects_noncanonical_numeric_and_duplicate_json`
- `test_hash_preimages_are_non_circular_and_bound`
- `test_contract_inputs_are_not_mutated`
- `test_contract_import_has_zero_io`
- `test_account_cash_identity_and_overlap_schema`
- `test_receipt_subtypes_cannot_hide_unknowns`

Future C0 must use its prescribed RED/GREEN distinction (import/collection errors are not business RED), full pytest plus `python -B scripts/guard.py --require-git`, and schema/fixture SHA freeze for downstream consumers and non-author review. This PR creates none of those four files and claims no C0 test results. Runtime and real-order validation remain NOT_RUN; profitability remains UNKNOWN.
