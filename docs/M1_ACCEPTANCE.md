# M1 acceptance: the 6-hour WebSocket book run (Task AI; 6 h since 2026-10-11, 家伟批准)

M1 is accepted on one 6-hour (`M1_RUN_TARGET_S`; it was 24 hours until 2026-10-11, 家伟批准), read-only recording of the public order books, run on the box and
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

# The 6-hour acceptance run (M1_RUN_TARGET_S, the default --max-runtime; 2026-10-11 家伟批准):
mkdir -p data/m1-runs
RUN=data/m1-runs/m1-$(date -u +%Y%m%dT%H%MZ)
nohup python -m src.m1_run --out "$RUN" --max-runtime 21600 > "$RUN.log" 2>&1 &
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
Such a run can still be reported, but `run_completed` fails.

When it has finished (exit 0 after 6 h):

```bash
python -m src.m1_report "$RUN"; echo "exit=$?"   # 0 PASS, 1 FAIL, 2 not a run dir
less "$RUN/report.md"                             # report.json has the same data, machine-readable
```

## What the run writes

| Path | Content |
|---|---|
| `manifest.json` | Written atomically at start (`status: running`) and at the end (`completed`, `interrupted`, `no_snapshot` or `failed`). Includes `started_at`/`ended_at` (UTC, ms), `elapsed_s`, the assets with market id, question and outcome, the selection (scan ts, skipped rows), settings, a snapshot of `src/thresholds.py`, versions (Python, websockets, schema `m1_run/1`), git sha and dirty flag, the `config/risk.toml` sha256 and `live_enabled` at start and end with the pin, the files written, the counts, the exit code and the stop signal. |
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
- 5: stopped by SIGINT or SIGTERM.

## Acceptance criteria (M2 pending)

The numbers live in `src/thresholds.py` and `docs/THRESHOLDS.md`. They are conservative first values for the user to confirm or change at M2.
The report applies all of them; any FAIL fails the run.

| Criterion | Rule | Default |
|---|---|---|
| run_completed | manifest `completed`, exit 0, ended by its deadline (not a signal) | — |
| run_duration | `elapsed_s >= M1_MIN_RUN_S` | 21540 s (6 h − 1 min; was 86340 s; 2026-10-11 家伟批准, KEY) |
| risk_pinned | risk.toml sha256 equals the pin at start and at end, `live_enabled` false at both | pin `0aea23af…aebc04` |
| integrity | every listed file present, 0 unreadable lines, row counts equal the manifest, no unknown asset | — |
| coverage | every asset fresh for `>= M1_MIN_COVERAGE` of the run | 0.99 |
| book_age_p99 | every asset's p99 book age `<= MAX_BOOK_AGE_S` | 120 s |
| reconnects | connect attempts after the first (reconnects plus failed connects) `<= M1_MAX_RECONNECTS` | 48 |
| gap_resyncs | resyncs not caused by a disconnect, per asset, `<= M1_MAX_GAP_RESYNCS_PER_ASSET` | 24 |
| rest_matched | REST samples compared `>= M1_MIN_REST_MATCHED` | 50 (a default 6 h run takes about 148) |
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

## Limits of this check

- One run on one machine: it measures this recorder against this network for one day, not the exchange.
- The REST comparison is approximate. REST and WS can legitimately differ for a few hundred ms around a change,
  which is why the mismatch rate is a threshold, not zero.
- Coverage uses the recorder clock against exchange `book_ts`. A box clock that runs ahead or behind by
  seconds shifts ages by that much; the manifest records the times so this can be checked.
