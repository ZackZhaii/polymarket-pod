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
  the Sentinel reads that state, not the 1 (ws_recorder / m1_run / halt_consumer crash = global halt, other = alert).
- `--help` exits 0. Usage errors are each module's `EXIT_USAGE` (never argparse's own exit).
- Pin drift (Task AS, gaps 5 and 6, `src/pins.py`): config/risk.toml is checked as a file (sha256
  `0aea23af…bc04`, CRLF normalised, and every key with its value and type, no unknown key) at start by scanner,
  book, ws_recorder, m1_run, endgame_scan, resolutions and loop_claim, before every paper_cycle cycle, and while
  running by ws_recorder and m1_run at least every `PIN_RECHECK_S`. Drift records one global halt
  (`risk_toml_pin_drift`, shared with the Sentinel tick) and exits with the module's pin code. A check that
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

`python -m src.loop_claim status|claim|release`: the paper-loop queue. Start/finish heartbeat.

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
| 4 | `EXIT_WRITE_FAILED` | writing the run directory failed | global halt |
| 5 | `EXIT_INTERRUPTED` | stopped by SIGINT/SIGTERM (Operator stop) | log |
| 6 | `EXIT_PIN_DRIFT` | config/risk.toml off its pins at start (no run dir created) or while running (PIN_RECHECK_S); global halt recorded | global halt |

## `src/paper_cycle.py`

`python -m src.paper_cycle [--loop]`: gap / staleness signals and the Sentinel StaleWatch. Heartbeat every cycle.

| Code | Name | Meaning | Sentinel |
|---|---|---|---|
| 0 | `EXIT_OK` | ran | - |
| 1 | `EXIT_PIN` | a phase-0 pin failed: the in-memory config, or (Task AS) the risk file before a cycle (global halt recorded) | global halt |
| 2 | `EXIT_USAGE` | usage error | alert |

## `src/paper_ledger.py`

`python -m src.paper_ledger`: one paper intent through RiskGate into the ledger, or the self-check. No heartbeat.

| Code | Name | Meaning | Sentinel |
|---|---|---|---|
| 0 | `EXIT_OK` | decided (allowed, or vetoed with reasons) / self-check passed | - |
| 1 | `EXIT_FAILED` | ledger halted, risk.toml changed during the run, no decision, or self-check failed | - |
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

| Code | Name | Meaning | Sentinel |
|---|---|---|---|
| 0 | `EXIT_OK` | snapshot written | - |
| 3 | `EXIT_INCOMPLETE` | Gamma paging incomplete: nothing written | alert |
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
| 2 | `EXIT_USAGE` | usage error | alert |
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
