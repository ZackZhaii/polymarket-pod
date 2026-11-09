# Interface lock v1 — planning only, root is exclusive editor

Final normative additions/field names are locked in C0_CONTRACT_TASK.md and IMPLEMENTATION_PLAN.md supersession table: decode_record raw ingress; store.controller_lock short phases only; global store events; one canonical receipt union; journal-head state_sha256; fake_transport_factory. Original component intake is preserved but must consume these final decisions. No S0 schema TBD remains.

Formal future root D:/projects/polymarket-pod. Pure S0paths below are proposals under src/execution; real venue adapter is S1-only. Existingm2_runner/paper_ledger/pins/risk.toml unchanged. Use stdlibDecimal internally, canonicaldecimalstrings inrecords, zonedUTCstrings fornow. Allpublicrecords are validated JSON dictionaries: `Record = dict[str, object]`; immutable-by-contract validatedcopies, never unvalidatedSDKpayloads. Conceptualrecordnames ExecutionCommand/ExecutionPermit/OrderObservation/AccountSnapshot/ExecutionReceipt are aliases/contracts incontracts.py, notoverlappingORMclasses. Exactschemafields deriveapprovedSPEC; rootC0mustdefineallrequiredschemas+canonicalhash+validationbeforeconsumers. ValidationError(ValueError) incontracts; StoreConflict(RuntimeError) instore; no retries default.

## C0: contracts.py (root plan task)
`validate_record(kind: str, value: Record) -> Record`
`canonical_sha256(value: Record) -> str`
`decode_record(kind: str, raw: bytes) -> Record`
Kinds: proposal,command,permit,observation,account_snapshot,approval,policy,book_snapshot,account_event,settlement_evidence,receipt,store_event,store_snapshot,controller_result. Unknownkind/missingfield/unsupportedextra/invalidfiniteunit/time/schema rejects. Permit/defaultapproval budget0/disallowedactions. AllfieldsfromSPEC+taskexactschemas; nofundingvaluesinvented. PublicrecordvalidationpreservesUNKNOWNonlywhereexplicitallowed; riskrequiredUNKNOWNdeniespolicy.

## store.py: one authoritative SQLite transaction boundary
`class ExecutionStore: __init__(self, db_path: Path) -> None`
`reserve(self, command: Record, permit: Record, *, expected_version: int, now: str) -> Record`
`append_event(self, command_id: str, event: Record, *, expected_version: int) -> Record`
`snapshot(self) -> Record`
`controller_lock(self) -> ContextManager[None]`
Recordreturnedcontains globalversion/hash/command/reservations/status; reserveandcommandatomic, expiry/hashes/expectedglobalversion recheckedinsidetransaction. Authoritativeeventsappend-only, applicationsfromknownstates/events; notgenericwritearbitraryaccountcash. Eachrelease/consume/filltransitionatomic. Reserveoverlapnativeprocessproofrequired. JSONLexportsfromconsistentreadtransaction only. Don'tassume SQLitewal acceptableonallFS; plannotfixturetestingactualyet.

## policy.py: no IO/no reserve itself
`authorize(proposal: Record, account: Record, book: Record, approval: Record, policy: Record, *, now: str) -> Record`
Return exactkeys allowed:bool, permit:Record|None, reasons:list[str]. Onlyallowedtrueifall requireddata/caps/freshnessscopevalid; defaultbudget0deny. Channeltestandstrategy-liveapprovalscopesdistinct. Reasonsstablecodes. Storagecommitrechecksreserve-sensitivefacts; nofakepaperconfigtoskippins.

## transport_contract.py: typing.Protocol, S0fakeonly
`class Transport(Protocol)` methods:
`submit_once(self, command: Record) -> Record`
`request_cancel(self, order_ref: Record) -> Record`
`read_order(self, order_ref: Record) -> Record`
`read_open_orders(self, account_ref: str) -> Record`
`read_fills(self, account_ref: str, *, cursor: str | None) -> Record`
`read_account(self, account_ref: str) -> Record`
Returnstrictrecords keyedtosourceevidence+status+complete/freshness; unknown/errornotemptyaccountzero. ActualSDKmethodnames/signaturedomain/assetmap/feeSchedule/finality UNKNOWNuntilofficialevidencegate. No transportmayresize/retryorcalculatepolicy. Uncertainnetworkexceptions convertedbycontrollertounknown; cancellation200notconfirmed.

## controller.py: lifecycle owns submission, no networkelsewhere
`propose(proposal: Record, account: Record, book: Record, approval: Record, policy: Record, *, store: ExecutionStore, now: str) -> Record`
`submit_once(command_id: str, *, store: ExecutionStore, transport: Transport, now: str) -> Record`
`observe(command_id: str, observation: Record, *, store: ExecutionStore, now: str) -> Record`
`request_cancel(command_id: str, *, store: ExecutionStore, transport: Transport, now: str) -> Record`
`recover(*, store: ExecutionStore, transport: Transport, now: str) -> Record`
proposecallsauthorize+reserve once. SUBMITTINGdurablebeforetransport; after restartSUBMITTING=>SUBMIT_UNKNOWN no automaticresend. OutcomesnotexactlyonceAPIclaim. Observeimmutableexternaltruth deduplicatedbyverifiedexternalIDnottransactionhashalone. RequestCancelpermissionseparateandretainsreserveuntilconfirmedremaining; recoveryreconcilesfirst,noautoresumeexpiredapproval.

## account_projection.py / reconcile.py / settlement_receipt.py
`project_account(start: Record, events: list[Record]) -> Record`
`reconcile(local: Record, external: Record, *, now: str) -> Record`
`validate_settlement_evidence(evidence: Record, *, expected: Record) -> Record`
Externalbaselineactualfundingasset/watermark, notPAPER_EQUITY. Coveredfill/fee/cancelreserve/fundingevents, sameIDconflictHOLD. Resolutionnotcash/redeemreceipt; finality/assetunknownHOLD. No automaticredeem/deposit/withdraw methods.

## safety.py / receipt_export.py
`stop_new_orders(reason: str, *, store: ExecutionStore, now: str) -> Record`
`build_receipt(state: Record, reconciliation: Record, *, now: str) -> Record`
Stoplatchdurable; approvedcancel/observecontrolplane canremainalive, neworderoff. Nobulkcancelmagic orflattenclaim; pendingpositions/unknowncommands explicit. Exportredactssecrets/signatures, nooverridepaperbus/schema/calibration. Reports includechannel_test/strategy_live mode andincomplete/UNKNOWN, nofakeprofit.

## Dependency and exclusive ownership
C0rootcontracts->parallelP(policy),L(store+lifecycle),T(fakeTransport),A(accountprojection) asplanDAG; Lcontrollerintegration consumesP/T/A; S(safety+receipt) consumesL/A; E(economics) separatemode-researchplan; V(testplan) mapsall andassemblesS0regression. FutureS1actualadapterrequiresnewboundaryapproval/evidence. 01gatesownsexistingformalPRactions only. Rootwillchoosefutureimplementationlanesperlegitimatequeue andfinalplanreview; parallelplanningdoesnotauthorizeparallelqueueitems.
