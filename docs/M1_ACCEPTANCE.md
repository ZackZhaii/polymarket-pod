# M1 acceptance: the 24-hour WebSocket book run (Task AI)

M1 is accepted on one 24-hour, read-only recording of the public order books, run on the box and
judged offline. Two commands do it:

- `python -m src.m1_run` records. It uses the WS market channel through `src.ws_recorder` (Task AG)
  and samples the public CLOB `/book` through `src.book.snapshot` every 10 minutes for comparison.
- `python -m src.m1_report RUN_DIR` reads only the run directory. It writes `report.md` and
  `report.json` and exits 0 for PASS, 1 for FAIL.

No orders, no auth, no keys, no `.env`; `config/risk.toml` is only hashed (start and end), never
written, and `live_enabled` stays `false`. The only network reads are the two allowlisted public
endpoints above (`scripts/guard.py` url_allowlist, Task AH). Assets come from the scanner output already on
disk; `m1_run` never scans Gamma itself.

## Running it on the box

From a clean checkout of the `dev` commit being accepted (Linux shell):

```bash
cd polymarket-pod                       # the checkout to accept
git status --short                      # expect no modified tracked files (the manifest records sha and dirty)
python scripts/guard.py --require-git   # M0 guard (Task AH): must print 7 PASS lines and exit 0
python3 -m venv .venv-m1 && . .venv-m1/bin/activate
pip install -r requirements-ws.txt      # websockets, exact pin; nothing else
python -m src.scanner                   # must exit 0: writes data/markets.jsonl (assets come from here)

# Optional 2-minute smoke run first (its report FAILs on duration; that is expected):
python -m src.m1_run --out data/m1-runs/smoke-$(date -u +%Y%m%dT%H%MZ) --max-runtime 120 --rest-every 30

# The 24-hour run:
mkdir -p data/m1-runs
RUN=data/m1-runs/m1-$(date -u +%Y%m%dT%H%MZ)
nohup python -m src.m1_run --out "$RUN" --max-runtime 86400 > "$RUN.log" 2>&1 &
echo $! > "$RUN.pid"
```

`data/` is gitignored, so the run never dirties the tree. `--out` must be a new or empty directory;
the log and pid files sit next to it, not inside. Defaults are `--top 5` markets (both outcome tokens,
10 assets), `--rest-every 600`, `--rest-assets 4`, `--rotate-mb 64` and `--rotate-s 3600`. To pin
the assets instead of taking the top of the latest scan, use `--assets TOKEN [TOKEN ...]`
(metadata still comes from `data/markets.jsonl`).

Optional (Task AY): `--raw-books` also saves every `book` frame of a recorded asset, as received (all
levels, the hash, `min_order_size` / `tick_size` when sent), to `raw_books/raw_books-NNNNNN.jsonl`, for
validating the snapshot hash offline with `python -m src.ws_hash_check <run dir>` (read-only; exit 0 only
when every saved frame matched). Off by default. Capped at `WS_RAW_BOOK_FILE_MAX_BYTES` (16 MiB) per file
and `WS_RAW_BOOK_RUN_MAX_BYTES` (256 MiB) per run; past the run cap frames are dropped and counted
(manifest `raw_books`, one `raw_books_capped` event) and the recording goes on. The acceptance report
ignores this directory.

While it runs:

```bash
tail -f "$RUN.log"                                   # one summary line at the end
python -m json.tool "$RUN/manifest.json" | head -40  # status "running", assets, git sha, risk sha at start
tail -n 5 "$RUN"/events/events-*.jsonl               # connects, disconnects, resyncs, rest_error
ls -l "$RUN"/books "$RUN"/rest
```

Stopping early: `kill -TERM $(cat "$RUN.pid")` (or Ctrl-C in a foreground run). The run stops within about
1 s (`WS_STOP_POLL_S`) and closes its files. It finishes the manifest with status `interrupted` and exits 5.
Such a run can still be reported, but `run_completed` fails. The manifest's `stop_reason` says why
(`signal:SIGTERM`, `console:CTRL_C_EVENT`, ...).

Console close, logoff, shutdown (Windows, Task BT). Windows gives a process about 5 s after it closes its
console, logs the user off or shuts down. m1_run registers a console handler (`SetConsoleCtrlHandler`):
Ctrl+C / Ctrl+Break are an Operator stop as above; on a close, logoff or shutdown it stops the run, waits at
most `M1_CONSOLE_CLOSE_WAIT_S` (2 s) for the normal finish and otherwise writes the manifest itself
(atomically): status `interrupted`, `ended_at`, `elapsed_s`, exit_code 5, `stop_reason`
`console:CTRL_CLOSE_EVENT` (or LOGOFF / SHUTDOWN), the files written so far and `counts.partial: true`. The
m1_run heartbeat ends `crashed`, so the Sentinel still treats it as a global halt (as it did the stale
heartbeat of the killed process). While running, m1_run also rewrites the manifest every
`M1_MANIFEST_PROGRESS_S` (60 s) with `progress` (`last_flush_at`, `elapsed_s`, the files, rows written,
progress write count and failures; a failed progress write is counted and logged as a
`manifest_progress_write_failed` event, never fatal). A run killed without any warning still shows the span it
covered.

Operator step (Windows, documentation only; nothing here registers or changes a task). The 6 h rehearsal
on NoisePC died when the interactive scheduled task's console was closed (LastTaskResult 0xC000013A). For
long runs:

1. In Task Scheduler, set the task to "Run whether user is logged on or not" (S4U logon, no stored password;
   `schtasks /Change /TN <task> /RU <user> /NP` or the task's General tab), so no console belongs to an
   interactive session that can be closed or logged off.
2. Start python in its own process group and console, so a console close or Ctrl+C in the launcher does not
   reach it, e.g. from PowerShell
   `Start-Process -WindowStyle Hidden python -ArgumentList '-m','src.m1_run',...` or from Python
   `subprocess.Popen(..., creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS)`.
   Stop such a run with `taskkill /PID <pid>` only as a last resort (no handler runs on a hard kill; the
   manifest then keeps its last `progress`).

When it has finished (exit 0 after 24 h):

```bash
python -m src.m1_report "$RUN"; echo "exit=$?"   # 0 PASS, 1 FAIL, 2 not a run dir
less "$RUN/report.md"                             # report.json has the same data, machine-readable
```

## What the run writes

| Path | Content |
|---|---|
| `manifest.json` | Written atomically at start (`status: running`) and at the end (`completed`, `interrupted`, `no_snapshot` or `failed`). Includes `started_at`/`ended_at` (UTC, ms), `elapsed_s`, the assets with market id, question and outcome, the selection (scan ts, skipped rows), settings, a snapshot of `src/thresholds.py`, versions (Python, websockets, schema `m1_run/1`), git sha and dirty flag, the `config/risk.toml` sha256 and `live_enabled` at start and end with the pin, the files written, the counts, the exit code, the stop signal and `stop_reason`. Task BT: rewritten every `M1_MANIFEST_PROGRESS_S` while running with `progress`; after a console close the handler may write it (`counts.partial: true`). |
| `books/books-NNNNNN.jsonl` | WS book rows, the same shape as `data/books.jsonl` plus `ws_state`. Includes the price-less invalidation rows on a resync or disconnect. |
| `events/events-NNNNNN.jsonl` | `start`, `connected`, `connect_failed` (error type), `disconnected` (reason, healthy), `backoff`, `resync` (asset, reason), `resubscribe`, `live`, `rest_error`, `end`, `finish`. |
| `rest/rest-NNNNNN.jsonl` | REST `/book` snapshots (book.py row shape plus `sample` and `received_at`). |

Every file is append-only, one write per JSON line (`ws_recorder.JsonlAppender`). A file is rotated before it would pass the
size limit or once it has been open for the age limit.

Exit codes of `m1_run`:

- 0: ran to `--max-runtime` with at least one snapshot.
- 1: no snapshot.
- 2: usage error (also a non-empty `--out`, or no usable assets).
- 3: websockets is missing (checked before anything is written).
- 4: an output write failed.
- 5: stopped by SIGINT, SIGTERM, SIGBREAK, Ctrl+C / Ctrl+Break, or (Windows) a console close, logoff or shutdown (heartbeat `crashed` in that case).

## Acceptance criteria (M2 pending)

The numbers live in `src/thresholds.py` and `docs/THRESHOLDS.md`. They are conservative first values for the user to confirm or change at M2.
The report applies all of them; any FAIL fails the run.

| Criterion | Rule | Default |
|---|---|---|
| run_completed | manifest `completed`, exit 0, ended by its deadline (not a signal) | — |
| run_duration | `elapsed_s >= M1_MIN_RUN_S` | 86340 s (24 h − 1 min) |
| risk_pinned | risk.toml sha256 equals the pin at start and at end, `live_enabled` false at both | pin `0aea23af…aebc04` |
| integrity | every listed file present, 0 unreadable lines, row counts equal the manifest, no unknown asset | — |
| coverage | every asset fresh for `>= M1_MIN_COVERAGE` of the run | 0.99 |
| book_age_p99 | every asset's p99 book age `<= MAX_BOOK_AGE_S` | 120 s |
| reconnects | connect attempts after the first (reconnects plus failed connects) `<= M1_MAX_RECONNECTS` | 48 |
| gap_resyncs | resyncs not caused by a disconnect, per asset, `<= M1_MAX_GAP_RESYNCS_PER_ASSET` | 24 |
| rest_matched | REST samples compared `>= M1_MIN_REST_MATCHED` | 50 (a default run takes about 580) |
| rest_top_mismatch | best bid or best ask differs, as a share of compared samples, `<= M1_MAX_TOP_MISMATCH_RATE` | 0.02 |
| rest_unmatched | samples with no live WS row within `M1_REST_MATCH_WINDOW_S` (or an unusable REST `book_ts`) `<= M1_MAX_REST_UNMATCHED_RATE` | 30 s window, 0.10 |

Definitions:

- Fresh means the `book_staleness.book_age_problem` rule, evaluated every `M1_REPORT_STEP_S` (1 s) of the
  run window on the recorder clock. At each point, the latest WS row written for the asset must be a live
  book with `0 <= t - book_ts <= MAX_BOOK_AGE_S`. An invalidation row, a future `book_ts` or no row yet is
  not fresh. Book-age p50/p90/p99/max are over the points that have a live book; the share without one is
  reported too. Points use exact integer microseconds, so the 120 s boundary matches `book_age_problem`.
- REST vs WS: each REST sample is compared with the live WS row of the same token whose exchange
  `book_ts` is nearest. Prices are compared rounded to 6 decimals. A missing side compares as null.
  Mismatches are listed (up to `m1_report.MISMATCH_LIST_LIMIT`) with both prices and the time difference.
- Warnings, not failures: a dirty git tree or unknown sha at start, and files on disk that the manifest does not list.
  Task BT adds two: partial counts (a manifest written by the console-close handler), and `max_runtime_s` below
  `M1_MIN_RUN_S`. M1_MIN_RUN_S is 86340 s (24 h − 1 min) and unchanged, so a 6 h rehearsal can never pass
  run_duration; the report says so instead of leaving it to be read as a defect.
- Observed span (Task BT; not a criterion). For a run that did not complete, the report adds an
  "Observed span" section: coverage and book age per asset over the span the run actually covered, from
  `started_at` to `ended_at` if the manifest was finished, else to `progress.last_flush_at`, else to the latest
  row `ts` on disk (a run killed before progress existed). The criteria above still FAIL such a run.

## Limits of this check

- One run on one machine: it measures this recorder against this network for one day, not the exchange.
- The REST comparison is approximate. REST and WS can legitimately differ for a few hundred ms around a change,
  which is why the mismatch rate is a threshold, not zero.
- Coverage uses the recorder clock against exchange `book_ts`. A box clock that runs ahead or behind by
  seconds shifts ages by that much; the manifest records the times so this can be checked.
