# Exit codes (Task AS, Poly哨兵 design gap 4)

Exit codes collide across modules (scanner 2 = no market passed, loop_claim 2 = pin drift), so a code is only
meaningful with its module: always read it as (module, code). This is the registry of every entry point under
`src/` and `scripts/` (every file with a `__main__` block). `tests/test_exit_codes.py` and the guard check
`exit_codes` (`src/exit_codes.py`, read with `ast` only) fail when a module and its section disagree: an
undocumented `EXIT_*` constant, a documented code the module does not define, a literal exit code in `main`, an
unguarded argparse (which would exit 2 on its own), or a Sentinel column that differs from `src/sentinel_tick.py`.

Sentinel column (what the resident Sentinel does when a heartbeat finishes with that code; `EXIT_EVENTS`):
`global halt`, `alert`, `alert x2` (alert only on the 2nd run in a row, `SCANNER_INCOMPLETE_CONSECUTIVE`), `log`,
`-` (0, or a module the Sentinel does not watch because it writes no heartbeat).

For every entry point:

- An uncaught exception ends Python with 1 and a traceback. The heartbeat wrappers write state `crashed` first, and
  the Sentinel reads that state, not the 1 (ws_recorder / m1_run / halt_consumer / m2_runner crash = global halt, other = alert).
- `--help` exits 0. Usage errors are each module's `EXIT_USAGE` (never argparse's own exit).
- Pin drift (Task AS, gaps 5 and 6, `src/pins.py`): config/risk.toml is checked as a file (sha256
  `0aea23af…bc04`, CRLF normalised, and every key with its value and type, no unknown key) at start by scanner,
  book, ws_recorder, m1_run, endgame_scan, resolutions, loop_claim and paper_ledger (issue #63: its existing exit 1),
  before every paper_cycle cycle, and while
  running by ws_recorder and m1_run at least every `PIN_RECHECK_S`. Drift records one global halt
  (`risk_toml_pin_drift`, shared with the Sentinel tick) and exits with the module's own refusal code. A check that
  cannot run is drift. Only the Operator resumes.

## `src/book.py`

`python -m src.book`: one public /book snapshot per scanned market leg into data/books.jsonl. No heartbeat.

| Code | Name | Meaning | Sentinel |
|---|---|---|---|
| 0 | `EXIT_OK` | at least one snapshot written | - |
| 1 | `EXIT_NO_SNAPSHOT` | no public book snapshot written | - |
| 2 | `EXIT_NO_MARKETS` | no markets in data/markets.jsonl (run the scanner first) | - |
| 6 | `EXIT_PIN_DRIFT` | refused to start: config/risk.toml off its pins (Task AS); global halt recorded, nothing fetched | - |

## `src/calibration.py`

`python -m src.calibration`: offline M3 calibration report (Task AZ). Reads logs/forecasts.jsonl,
data/resolutions.jsonl and logs/paper.jsonl; writes reports/calibration/<UTC date>/ (report.json, report.md,
samples.jsonl). Read-only on its inputs, no network, no halt. No heartbeat.

| Code | Name | Meaning | Sentinel |
|---|---|---|---|
| 0 | `EXIT_OK` | report written; each section says `sufficient` or `insufficient` (insufficient is not an error) | - |
| 2 | `EXIT_USAGE` | usage error (bad flag, `--now` not a zoned ISO time) | - |
| 3 | `EXIT_CORRUPT_INPUT` | forecasts or resolutions file corrupt (torn last line, bad JSON, wrong keys or types, a forecast_id repeated with different content); no report written | - |
| 4 | `EXIT_LEDGER_INVALID` | logs/paper.jsonl unreadable or fails `ledger_store.verify`; no report written | - |
| 5 | `EXIT_WRITE_FAILED` | the report files could not be written (report.json is written last, so a partial set has none) | - |

## `src/endgame_scan.py`

`python -m src.endgame_scan`: Endgame candidates (signals only). Start/finish heartbeat.

| Code | Name | Meaning | Sentinel |
|---|---|---|---|
| 0 | `EXIT_OK` | scan complete | - |
| 1 | `EXIT_TOO_FEW_SCANNED` | fewer than MIN_SCANNED_MARKETS markets scanned | alert |
| 3 | `EXIT_INCOMPLETE` | Gamma paging or network failed | alert |
| 4 | `EXIT_NOT_PAPER` | refused: paper pins drifted (in-memory config, or since Task AS the risk file sha / any key; the file check records a global halt) | global halt |

## `src/halt_store.py`

`python -m src.halt_store status|resume`: list halts; the Operator's resume. No heartbeat.

| Code | Name | Meaning | Sentinel |
|---|---|---|---|
| 0 | `EXIT_OK` | status: no active halt; resume: released | - |
| 1 | `EXIT_REJECTED` | resume rejected (bad --checks JSON, unknown or wrong-scope halt, missing approval, bus write failed) | - |
| 2 | `EXIT_USAGE` | usage error (argparse) | - |
| 3 | `EXIT_ACTIVE` | status: at least one halt is active | - |

## `src/loop_claim.py`

`python -m src.loop_claim status|claim|release`: the paper-loop queue. Start/finish heartbeat. Issue #163: claim and release run under `<state>.lock`
(halt_store.StateLock); a lock not taken within HALT_STATE_LOCK_TIMEOUT_S is also `EXIT_BUSY` (3), nothing changed.

| Code | Name | Meaning | Sentinel |
|---|---|---|---|
| 0 | `EXIT_OK` | done | - |
| 2 | `EXIT_PIN` | refused: pins drifted (podlib.PINS, or since Task AS the risk file sha / any key); queue unchanged | global halt |
| 3 | `EXIT_BUSY` | another surface holds the claim | alert |
| 4 | `EXIT_CLOSED` | the task is not open | alert |
| 5 | `EXIT_USAGE` | usage error (Task AS: was argparse 2, which collided with EXIT_PIN) | alert |

## `src/m1_report.py`

`python -m src.m1_report`: offline M1 acceptance report of a run directory. No heartbeat.

| Code | Name | Meaning | Sentinel |
|---|---|---|---|
| 0 | `EXIT_PASS` | verdict PASS | - |
| 1 | `EXIT_FAIL` | verdict FAIL | - |
| 2 | `EXIT_USAGE` | usage error | - |

## `src/m1_run.py`

`python -m src.m1_run`: the M1 24 h WS + REST recording run. Heartbeats m1_run (every 10 s) and m1_rest (per sample).

| Code | Name | Meaning | Sentinel |
|---|---|---|---|
| 0 | `EXIT_OK` | ran to --max-runtime with at least one book snapshot | - |
| 1 | `EXIT_NO_ROWS` | no book snapshot recorded | alert |
| 2 | `EXIT_USAGE` | usage error | alert |
| 3 | `EXIT_NO_WEBSOCKETS` | the optional websockets package is missing | alert |
| 4 | `EXIT_WRITE_FAILED` | writing the run directory failed, or (issue #82) the REST sampler thread was still alive after the 120 s finishing join (`rest_join_timeout` event, manifest `failed`; the thread is sealed, not killed) | global halt |
| 5 | `EXIT_INTERRUPTED` | stopped by SIGINT/SIGTERM/SIGBREAK or Ctrl+C/Ctrl+Break (Operator stop). Task BT: a Windows console close / logoff / shutdown also finishes the manifest `interrupted` with exit_code 5, but the m1_run heartbeat ends `crashed` (global halt), not this log-only exit | log |
| 6 | `EXIT_PIN_DRIFT` | config/risk.toml off its pins at start (no run dir created) or while running (PIN_RECHECK_S); global halt recorded | global halt |
| 7 | `EXIT_CLOCK_BEHIND` | Task CM: refused to start (no run dir): the local clock is behind the exchange by more than `M1_MAX_CLOCK_BEHIND_S` (max of `book_ts` - local receive time over one public `/book` read per asset), or no `/book` answer was usable to check it. Sync the host clock and start again | alert |

## `src/m2_start.py`

`python -m src.m2_start --reason R [--m2-end T] [--dry-run]` (wrapper `scripts/m2_start.ps1`): the M2 start order on NoisePC
(issue #133). Sentinel first, then m1_run, paper_cycle, and m2_runner once it exists, each after the one before passed its
check. Writes no heartbeat; never resumes, never stops what it started.

| Code | Name | Meaning | Sentinel |
|---|---|---|---|
| 0 | `EXIT_OK` | every step started and passed its check (`--dry-run`: the plan passed the refusals) | - |
| 2 | `EXIT_USAGE` | usage error: blank --reason, no or bad --m2-end, --m2-end differs from the chain | - |
| 3 | `EXIT_REFUSED` | refused before anything started: M1 clone, runs/m2/KILL, bad chain (incl. missing/malformed/inconsistent `assets`, #276), no usable boot assets, M2 window over, a process already running, run dir exists, m2_runner not watched | - |
| 4 | `EXIT_SENTINEL_NOT_FRESH` | the launched Sentinel never beat fresh within SENTINEL_HEARTBEAT_TIMEOUT_S; nothing else started | - |
| 5 | `EXIT_NOT_READY` | the chain append or a runs/m2/current.json write failed, or a later process was not ready within its Sentinel timeout; nothing after it started | - |
| 6 | `EXIT_PIN_DRIFT` | config/risk.toml off its pins; refused, global halt recorded (`--dry-run`: none recorded) | - |

## `src/m2_runner.py`

`python -m src.m2_runner --run-dir runs/m2 [--max-runtime S] [--once]`: the M2 paper runner (issue #119). Every iteration checks
KILL, the live flags, the risk.toml pins, halts, the ledger, the daily loss breaker and disk before any read, seal or commit.
Heartbeat every HEARTBEAT_RUNNER_EVERY_S (`paused` while paused); single instance by `runs/m2/runner.lock`. Paper only, no executor.

Tick order: KILL / live / pins / halts / ledger, then settlement (#206), then the independent clock check, then the daily
loss breaker, disk, and only then scan / decide. Clock check (C222, #222 option B): one public read-only GET of the CLOB
`https://clob.polymarket.com/time` (exact URL only; guard and `podlib.public_get` refuse every variant). The whole
local-minus-server uncertainty interval (request round trip plus ±1 s rounding of the integer seconds) must fit inside
±`CLOCK_SKEW_MAX_S` (5 s). Two pause reasons, neither an exit code (the runner keeps beating `paused`, nothing is scanned or
decided, settlement still runs that tick):

- `clock_unverified`: /time unreachable or malformed (timeout, HTTP error, non-numeric, bool, NaN/inf, out of range), an
  unusable local clock, or an interval too wide to certify either way. Fail closed.
- `clock_skew`: the whole interval lies beyond ±5 s, a confirmed skew. Sync the host clock.

Switching between `clock_unverified` and `clock_skew` is one clock pause episode: `paused_since` keeps its first stamp (a
`paused` event is still written on each switch), so the Sentinel's `runner_paused_too_long` (`RUNNER_PAUSED_ALERT_S`, 6 h)
fires on a flapping clock too. `paused_since` clears only when a tick runs again; any other pause reason restamps it.

Issue #289 (Task DJ): the runner heartbeat `progress` also carries three read-only clock observation fields. They
never feed a decision, a pause or a halt (no new alert either; alert thresholds on them are M2 pending, #289 item 2):

- `clock_anomaly`: the clock anomaly the runner currently holds, `null` when clear, else `clock_unverified` or
  `clock_skew` (the value that makes the day-start snapshot late, `clock_ok=False`). It can stay set while the runner
  is **not** paused: from startup (fail closed, #275) and after a clock pause it clears only on a tick with a clean
  CLOB /time check and at least one fresh book with a valid book_ts, so a run with no pending signal, no readable
  book, or a missing / invalid book_ts keeps it set with `paused_reason` null.
- `clock_anomaly_since`: UTC stamp of when the current anomaly episode began (`null` when `clock_anomaly` is null).
  The startup `clock_unverified` is stamped at runner start (`recover`), a later anomaly at the tick that set it.
  Like `paused_since`, a switch between `clock_unverified` and `clock_skew` is the same episode and keeps the first
  stamp; it resets only when the anomaly clears, and a later anomaly gets a new stamp.
- `last_time_check`: the last CLOB /time check, `null` before the first one (a tick stopped earlier, e.g. by KILL, a
  halt or a pin, keeps the previous value). An object `{ts, result, low_s, high_s, rtt_s}`: `ts` the tick's stamp,
  `result` `ok` / `clock_unverified` / `clock_skew`, `low_s` / `high_s` the certified local-minus-server interval in
  seconds (including the ±1 s server quantization) and `rtt_s` the request time, rounded to ms; the three are
  `null` when the check could not measure (unreachable / malformed /time, unusable local clock).

| Code | Name | Meaning | Sentinel |
|---|---|---|---|
| 0 | `EXIT_OK` | `runs/m2/KILL` found, `--max-runtime` reached or `--once` done | - |
| 2 | `EXIT_USAGE` | usage error (an unknown argument, `--max-runtime` not > 0) | alert |
| 3 | `EXIT_ALREADY_RUNNING` | another m2_runner holds `runner.lock` (no heartbeat written, nothing done) | alert |
| 4 | `EXIT_WRITE_FAILED` | a journal / events / candidates write failed | global halt |
| 6 | `EXIT_PIN_OR_LIVE` | config/risk.toml off its pins or a live flag (`live_enabled` not false, `mode` not paper, `require_human_gate` not true); global halt recorded (`risk_toml_pin_drift` / `m2_live_flag`) | global halt |
| 7 | `EXIT_SELF_CHECK` | the start self-check failed (run dir or inbox not writable) | alert |

## `src/paper_audit.py`

`python -m src.paper_audit --ledger PATH --bus PATH [--json]`: offline, read-only cross-check of the
paper ledger against Executor bus fills, joined by explicit fill_id (issue #69). Standard library
and first-party `src` only, no network, no halt, nothing written; the report goes to stdout. No
heartbeat. Acceptance 2: the ledger must pass `ledger_store.verify` first (only then is it joined),
and the bus is parsed strictly — bad JSON, non-object rows, a torn tail and unreadable files are
named failures. Acceptance 3: each joined fill's full payload and the message's outer `market` are
compared with a type-sensitive normalized JSON comparison (key order / formatting normalized;
`10` / `10.0` / `true` / `"10.0"` stay distinct); the outer `bus.ts` is ignored. Acceptance 4 +
NEW-3b: every bus fill must be the Executor→Sentinel message the pipeline writes; identical
duplicate copies count once (the extra-copy count is reported) while copies that contradict each
other are a named `duplicate_conflict`; orphans on either side fail; settlement rows (`kind`
"settlement", ledger-only by design) are counted separately (`ledger.settlements`), are not
orphans, must still carry a fill_id, and a bus fill with a settlement's fill_id is a named
problem. Review P1 on PR #202: the remaining checks (UNKNOWN markers, SHA256 and counts) are later
acceptance items, so the tool never reports PASS — a problem-free run ends verdict `unknown`.

| Code | Name | Meaning | Sentinel |
|---|---|---|---|
| 0 | `EXIT_OK` | reserved for the fully checked pass (every #69 check); not reachable before the remaining acceptance items land | - |
| 1 | `EXIT_MISMATCH` | a problem (the ledger failed `ledger_store.verify`, an orphan / join problem on either side, a payload or outer-market mismatch, a non-finite number), or no problem but verdict `unknown` (the two logs are not validated as agreeing) | - |
| 2 | `EXIT_USAGE` | usage error, or an input that cannot be read in full (missing, unreadable, undecodable bytes, a malformed or non-object line, a torn last row, a conflicting fill_id) | - |

## `src/paper_cycle.py`

`python -m src.paper_cycle [--loop]`: gap / staleness signals and the Sentinel StaleWatch. Heartbeat every cycle.

| Code | Name | Meaning | Sentinel |
|---|---|---|---|
| 0 | `EXIT_OK` | ran (issue #75: a round whose markets/books input is still changing after the one bounded re-read is skipped, `ok=false reason=input_pending`, and exits 0 too: a single transient append is never a halt; issue #277: consecutive such rounds are counted as `input_pending_streak` in the StaleWatch state and the heartbeat progress, cleared by a normal cycle, and at `ALL_STALE_CONSECUTIVE_CYCLES` in a row they raise the `all_markets_stale` global halt, the reason naming input_pending and the count; the exit code stays 0 and `--loop` keeps running) | - |
| 1 | `EXIT_PIN` | a phase-0 pin failed: the in-memory config, or (Task AS) the risk file before a cycle (global halt recorded) | global halt |
| 2 | `EXIT_USAGE` | usage error | alert |

## `src/paper_ledger.py`

`python -m src.paper_ledger`: one paper intent through RiskGate into the ledger, or the self-check. No heartbeat.

| Code | Name | Meaning | Sentinel |
|---|---|---|---|
| 0 | `EXIT_OK` | decided (allowed, or vetoed with reasons) / self-check passed | - |
| 1 | `EXIT_FAILED` | refused to start: config/risk.toml off its pins (sha256 / any key, issue #63; global halt recorded, no fill and no self-check), ledger halted, risk.toml changed during the run, no decision, or self-check failed | - |
| 2 | `EXIT_USAGE` | usage error or missing operator fields | - |

## `src/paper_session.py`

`python -m src.paper_session`: one offline paper session. No heartbeat.

| Code | Name | Meaning | Sentinel |
|---|---|---|---|
| 0 | `EXIT_OK` | session ok and no fill booked | - |
| 1 | `EXIT_FAILED` | session not ok, or a fill was booked | - |

## `src/reconcile.py`

`python -m src.reconcile`: Data API v2 reconciliation (Task AK). No heartbeat; exits 1/3/4 record their own global halt.

| Code | Name | Meaning | Sentinel |
|---|---|---|---|
| 0 | `EXIT_OK` | ledger and Data API agree | - |
| 1 | `EXIT_MISMATCH` | positions or trades differ (global halt recorded) | - |
| 2 | `EXIT_USAGE` | usage error | - |
| 3 | `EXIT_REMOTE` | Data API unavailable, lagging or invalid (global halt recorded) | - |
| 4 | `EXIT_LEDGER` | local ledger unreadable or invalid (global halt recorded) | - |

## `src/replay_intent.py`

`python -m src.replay_intent --bundle path/to/bundle.json [--out path/to/result.json]` (issue #80):
one single-shot offline Intent-builder replay on one fixed bundle (schema_version 1: one raw bus
signal row, one scanner market row, that market's book rows, strategy, fee_rate, a zoned now). One
signal and one run only, no bus, no ledger, no halt, no RiskGate, no executor and no network; it
prints the intent or the complete ordered rejection reasons, with the SHA256 of the bundle's raw
bytes (`input_sha256`). With `--out` the same JSON is published atomically (temp file, fsync,
hardlink into place): an existing output is refused, never overwritten, and never into config/, a
.toml or src/thresholds.py. The pin check is read only, so drift is refused here without recording
a global halt.

| Code | Name | Meaning | Sentinel |
|---|---|---|---|
| 0 | `EXIT_OK` | the single replay ran; the printed JSON holds an intent or the builder's ordered reasons (with `--out`: also published) | - |
| 2 | `EXIT_USAGE` | usage error (missing `--bundle`; `--help` exits 0), or `--out` points into config/, a .toml or src/thresholds.py | - |
| 3 | `EXIT_CORRUPT_BUNDLE` | bundle unreadable, not JSON, wrong `schema_version`, missing keys, bad types, or a `now` without a timezone | - |
| 4 | `EXIT_UNSUPPORTED_BUNDLE` | not a single-shot bundle: extra keys (history, loop), a list of signals, or a book row for another market | - |
| 5 | `EXIT_OUTPUT_EXISTS` | refused: `--out` already exists; nothing overwritten, no result published | - |
| 6 | `EXIT_PIN_DRIFT` | refused: config/risk.toml off its pins (read-only check; this entry records no halt); nothing replayed | - |
| 7 | `EXIT_WRITE_FAILED` | refused: publishing the result JSON failed (e.g. no such folder); the destination is untouched | - |

## `src/reflector.py`

`python -m src.reflector`: one Reflector proposal (paper). No heartbeat.

| Code | Name | Meaning | Sentinel |
|---|---|---|---|
| 0 | `EXIT_OK` | proposal accepted | - |
| 1 | `EXIT_REFUSED` | not accepted, or risk.toml changed during the run | - |

## `src/reflector_proposals.py`

`python -m src.reflector_proposals`: M3 step 4 (Task BB). Reads one calibration `report.json` (default: the newest
`reports/calibration/<UTC date>/report.json`) read-only and writes structured proposals to `playbook/proposals.jsonl`,
`playbook/proposals.md` (via `reflector.propose`, same daily cap) and the bus (`proposal`, `exchange_action: none`).
Proposals only: never applied, never writes config/, a .toml or src/thresholds.py. No heartbeat.

| Code | Name | Meaning | Sentinel |
|---|---|---|---|
| 0 | `EXIT_OK` | ran; zero or more proposals written (zero = observations only, e.g. every section `insufficient`) | - |
| 2 | `EXIT_USAGE` | usage error, or an output path in config/, a .toml or src/thresholds.py | - |
| 3 | `EXIT_REPORT_MISSING` | no report.json found; no proposal written | - |
| 4 | `EXIT_REPORT_CORRUPT` | report.json unreadable, not JSON, not `complete`, unknown schema_version, or a field the rules read has the wrong type; no proposal written | - |
| 5 | `EXIT_PROPOSALS_CORRUPT` | playbook/proposals.jsonl torn or off its schema; no proposal written | - |
| 6 | `EXIT_WRITE_FAILED` | writing a proposal failed, or config/risk.toml changed during the run | - |

## `src/resolutions.py`

`python -m src.resolutions`: M3 resolution snapshot (Task AN). Start/finish heartbeat (Task AV).

Task AV split the old exit 4 (`EXIT_PIN`, which meant pin drift or a bad snapshot clock); 4 is retired.
issue #57: exit 3 also covers an unusable stored `data/resolutions.jsonl` (torn last line, blank line, bad
JSON or NaN, a non-object, another key, or a row the writer could not have written), read strictly before
the fetch; it keeps the Sentinel alert and appends nothing.

| Code | Name | Meaning | Sentinel |
|---|---|---|---|
| 0 | `EXIT_OK` | snapshot written (zero or more rows, including confirmation upgrades) | - |
| 3 | `EXIT_INCOMPLETE` | Gamma paging incomplete, or the stored `data/resolutions.jsonl` history is unusable; nothing fetched or written | alert |
| 5 | `EXIT_BAD_CLOCK` | refused: the snapshot clock is not a zoned ISO timestamp; nothing fetched or written | global halt |
| 6 | `EXIT_PIN_DRIFT` | refused to start: paper pins drifted (config/risk.toml file sha / any key, global halt recorded, or the in-memory podlib PINS); nothing fetched or written | global halt |

## `src/scanner.py`

`python -m src.scanner`: Gamma scan into data/markets.jsonl. Start/finish heartbeat.

| Code | Name | Meaning | Sentinel |
|---|---|---|---|
| 0 | `EXIT_OK` | scan complete, markets passed | - |
| 1 | `EXIT_TOO_FEW_SCANNED` | fewer than MIN_SCANNED_MARKETS markets scanned | alert |
| 2 | `EXIT_NONE_PASSED` | no market passed the filters | alert |
| 3 | `EXIT_INCOMPLETE` | Gamma paging or network failed | alert x2 |
| 6 | `EXIT_PIN_DRIFT` | refused to start: config/risk.toml off its pins (Task AS); global halt recorded, nothing fetched | global halt |

## `src/secret_scan.py`

`python -m src.secret_scan`: secret-string scan of logs/ (Task AQ). No heartbeat.

| Code | Name | Meaning | Sentinel |
|---|---|---|---|
| 0 | `EXIT_CLEAN` | nothing found | - |
| 1 | `EXIT_SECRET` | a secret found: global halt recorded (unless --no-halt) | - |
| 2 | `EXIT_SCAN_ERROR` | a file could not be scanned: global halt recorded (unless --no-halt) | - |
| 3 | `EXIT_USAGE` | usage error | - |

## `src/sentinel_tick.py`

`python -m src.sentinel_tick [--once]`: the resident Sentinel (Task AP). Its own heartbeat is read by the consumers, not by this matrix.

| Code | Name | Meaning | Sentinel |
|---|---|---|---|
| 0 | `EXIT_OK` | ran (--once: raised nothing) | - |
| 2 | `EXIT_USAGE` | usage error | - |
| 3 | `EXIT_RAISED` | --once raised a halt or an alert | - |

## `src/ws_hash_check.py`

`python -m src.ws_hash_check <dir>`: offline, read-only recomputation of the WS snapshot hash over raw frames saved
by `src.m1_run --raw-books` (Task AY). No heartbeat, no network, writes nothing.

| Code | Name | Meaning | Sentinel |
|---|---|---|---|
| 0 | `EXIT_ALL_MATCH` | at least one frame checked and every frame matched | - |
| 1 | `EXIT_NOT_VALIDATED` | no frame, or not every frame matched (mismatch, incomplete, absent, uncomputable or unreadable) | - |
| 2 | `EXIT_USAGE` | usage error (bad arguments, no such path, a file that cannot be read) | - |

## `src/ws_recorder.py`

`python -m src.ws_recorder`: WS market-channel book recorder. Heartbeat every 10 s.

| Code | Name | Meaning | Sentinel |
|---|---|---|---|
| 0 | `EXIT_OK` | ran to --max-runtime with at least one book snapshot | - |
| 1 | `EXIT_NO_ROWS` | no book snapshot recorded | alert |
| 2 | `EXIT_USAGE` | usage error (also a `--raw-books` DIR that is not new or empty, Task BA) | alert |
| 3 | `EXIT_NO_WEBSOCKETS` | the optional websockets package is missing | alert |
| 4 | `EXIT_WRITE_FAILED` | writing the output file failed | global halt |
| 6 | `EXIT_PIN_DRIFT` | config/risk.toml off its pins at start or while running (PIN_RECHECK_S); global halt recorded | global halt |

## `scripts/e2e_offline.py`

`python scripts/e2e_offline.py`: the offline end-to-end gate (Task AJ).

| Code | Name | Meaning | Sentinel |
|---|---|---|---|
| 0 | `EXIT_OK` | gate passed | - |
| 1 | `EXIT_FAILED` | a problem (golden mismatch, invariant, network touched) | - |

## `scripts/guard.py`

`python scripts/guard.py [--require-git]`: the M0 guard.

| Code | Name | Meaning | Sentinel |
|---|---|---|---|
| 0 | `EXIT_OK` | every check passed (risk_sha_head may SKIP without git) | - |
| 1 | `EXIT_FAILED` | a check failed | - |
| 2 | `EXIT_USAGE` | usage error or no repo root | - |
