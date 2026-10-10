# Phase 0 run

Date: 2026-10-08 (JST). Result: pass.

This page records the first Phase 0 run. That run used a separate Linux checkout; it is not where work happens now. The shared checkout is `D:\projects\polymarket-pod` on the Operator's PC. Bots work in `.worktrees/<name>` from `origin/dev` (`AGENTS.md`). `requirements.txt` lists no packages (standard library only; no order SDK). No `.env` was created or read. No order, auth, or withdraw endpoint was called.

## Scanner

The scanner has changed since that run (Tasks I, K, O and Q). This is the current `src/scanner.py` on `dev`; the full filter list is in `.grok/skills/market-scan/SKILL.md`.

- Public reads only. It walks Gamma `GET /markets/keyset` (`order=volume24hr`, 100 per page, `liquidity_num_min` from `min_liquidity_usd`). No keys, no orders.
- Filters: `config/risk.toml` `min_liquidity_usd` and `max_spread`, plus the 0.03–0.97 long-tail price band.
- Minimum time to expiry (Task O): a market needs a zoned end date at least 24 h away (`expiry_rule.MIN_HOURS_TO_EXPIRY`, a code constant, not a risk.toml key). Exactly 24 h passes; less is `near_expiry`.
- Version rule (Task Q): Gamma `version` picks the outcome-ID field. `v2` reads `positionIds`, `v1` reads `clobTokenIds`. A missing version is rejected as `missing_version`, any other value as `unsupported_version`. Both rank right after `closed`. With a valid version but bad IDs, the row passes with empty `token_ids` and a `token_ids_problem`, so no book is fetched.
- Stdout: `scanned= passed= stop= cap_hit= server_min_liquidity= min_hours_to_expiry= mode= live_enabled=false keys=none orders=none`, then `rejected_first=reason:count` (and `token_id_problems=` when any).
- Exit codes (`scanner.EXIT_*`):
  - 0: ok. It writes `data/markets.jsonl` (gitignored) and up to 50 bus signals.
  - 1: fewer than 5 markets scanned.
  - 2: zero markets passed the filters.
  - 3: pagination incomplete, including a network error after `public_get` retries (Task K).
  - Codes 1–3 write nothing.

The 2026-10-08 run itself used the earlier single-page sample (`active_sample=20`, public `GET /markets`). It exited 0 and wrote 20 rows to gitignored `data/markets.jsonl`.

## Book

`python src/book.py` exited 0.

- Each `python src/book.py` run appended 10 public `GET /book` snapshots for the first 5 scanned markets (both outcome tokens). This verification ran it twice, so gitignored `data/books.jsonl` currently has 20 lines.
- Two-sided example: Hillary Clinton 2028 nomination. Yes bid 0.001 / ask 0.002 / spread 0.001. No bid 0.998 / ask 0.999 / spread 0.001.
- Several longshot tokens were one-sided (`spread=null`). RiskGate vetoes a missing spread. No order was sent.
- Records appended to gitignored `data/books.jsonl`.

## Paper ledger

`python src/paper_ledger.py` exited 0. RiskGate ran before every simulated fill.

- In-memory `live_enabled=true` was vetoed. `config/risk.toml` was not modified.
- Model probability equal to price was vetoed (`non_positive_edge`) after 0.005 assumed slippage.
- Spread 0.20 was vetoed (`spread_above_max`; cap is 0.04).
- Strategy name `wash-probe` was vetoed (`banned_behavior`) and did not fill.
- One labeled hypothetical fill was allowed: strategy `phase0-gate-test`, model_prob 0.70, market_price 0.40, edge 0.295, assumed_slippage 0.005, fee_usd 0, notional $10 (1% of the $1,000 paper bankroll). This probability is a gate fixture, not a forecast and not a claim of profit.
- A real public ask (Hillary Yes, price 0.002, same number used as model probability) was vetoed with `non_positive_edge`. It was not filled.
- `logs/bus.jsonl` received `signal`, `veto`, and `fill` lines in the `docs/PROTOCOL.md` shape. The only `fill` is from `Executor` and carries `risk_decision=allow`.
- `logs/paper.jsonl` is gitignored.

### Slippage floor (Task AU)

RiskGate vetoes any intent whose `assumed_slippage` is below `ASSUMED_SLIPPAGE` (0.005, `src/thresholds.py`)
with `slippage_below_floor`; exactly 0.005 passes. A missing or non-number slippage vetoes
`invalid_assumed_slippage`, NaN/inf `non_finite_assumed_slippage`. This holds for every intent (hypothetical
gate tests and the intent builder, whose `INTENT_ASSUMED_SLIPPAGE` is the floor itself). The
`paper_ledger` CLI also refuses `--slippage` below the floor, non-finite or not a number: argparse error, exit
code 2, before `risk.toml`, the ledger or the bus is read or written. The default stays 0.005.

## Risk file

`config/risk.toml` is unchanged:

- `mode = "paper"`
- `live_enabled = false`
- `require_human_gate = true`
- `max_order_pct = 0.01`
- `max_daily_loss_pct = 0.03`

SHA256 before and after the run: `0aea23af1e938d330b5b4fab2c6abdec9f475f16807d177a5860a4da32aebc04`.

### What `max_daily_loss` counts (Task AR)

RiskGate vetoes `max_daily_loss` when `daily_loss_usd / PAPER_EQUITY_USD >= max_daily_loss_pct`.
`daily_loss_usd` (`src/daily_loss.py`, read by `paper_ledger.load_state`) is today's booked fees plus
today's **gross realized losses** of the paper ledger. Before Task AR it was the fees only.

- Realized P&L is replayed per leg (market + token_id) over the rows that count toward risk, in
  ledger order, at average cost. A sell that closes a long realizes proceeds minus average cost; a
  buy that closes a short realizes average proceeds minus cost. Fees are counted once, separately.
- Realized gains are reported (`daily_realized_gain_usd`) but never offset a loss or the fees.
- Day boundary: the UTC calendar day of the clock (00:00:00Z inclusive to the next 00:00:00Z).
  A loss belongs to the UTC day of the closing fill's `ts`, whenever the position was opened. A row
  whose `ts` starts with today's date, or whose UTC date is today or later, counts toward today.
- A counted row with a missing or unparseable `ts`, a side that is not buy/sell, or shares, notional
  or fee that is not a finite number >= 0 makes the state invalid: RiskGate vetoes
  `invalid_risk_state` and the paper ledger raises a global halt.
- `load_state` also returns `daily_fees_usd`, `daily_realized_loss_usd` and `daily_realized_gain_usd`.

## Guard (M0)

Run the local guard before and after any change, from any folder inside the repo. It is offline, read-only and
standard library only:

```
python scripts/guard.py                 # or: python ../scripts/guard.py from docs/, etc.
python scripts/guard.py --root PATH     # check another checkout (PATH may be any folder inside it)
python scripts/guard.py --require-git   # treat a missing git / HEAD as FAIL instead of SKIP
```

It prints one `PASS`/`FAIL` line per check with the reason, then a summary line:

- `risk_sha_worktree`, `risk_sha_head`: `config/risk.toml` sha256 is `0aea23af…bc04`, for the working-tree file
  (CRLF normalised) and for `git show HEAD:config/risk.toml`. Without git or outside a checkout the HEAD line is `SKIP`.
- `live_enabled`: `live_enabled = false` (read with `src.podlib.parse_risk_toml`) and the other `podlib.PINS` hold.
- `banned_dependencies`, `banned_imports`: no py-clob-client, other clob package, web3, eth_account or other
  order-capable package (`ORDER_CAPABLE` in `scripts/guard.py`, the single list) in `requirements*.txt` /
  `pyproject.toml`, or imported anywhere in `src/` or `scripts/`.
- `url_allowlist`: every URL in `src/` code is an allowlisted public read-only endpoint (Gamma `/markets` and
  `/markets/keyset`, CLOB `/book`, the WS market channel, and since Task AK only the Data API v2 `/v2/status`, `/v2/positions`
  and `/v2/trades` reads of `src/reconcile.py`); the retired v1 Data API routes, other v2 routes, order/auth paths, the user channel, ports, credentials in
  the query and other hosts fail. Docstrings and comments may also link docs.polymarket.com and github.com.
- `thresholds_doc`: `docs/THRESHOLDS.md` agrees with `src/thresholds.py` (Task AC table parser).
- `e2e`: the end-to-end offline gate below (Task AJ). It loads the checked repo's own `scripts/e2e_offline.py`,
  runs the real pipeline on `tests/fixtures/e2e/` and compares it exactly with `golden.json`. About 0.1 s; no
  pytest needed.
- `halt`: the Task AL halt consumer on the same offline run.
- `secret_scan` (Task AQ): no private key, mnemonic or credential value in any tracked file or in `logs/`
  (`src/secret_scan.py`; reports file:line, rule, length and a sha256 prefix only), plus a self-test on fresh
  random canaries. `.env` files are never opened. The guard itself records no halt; for that run
  `python -m src.secret_scan` (exit 0 clean, 1 secret found and a global Sentinel halt, 2 scan error and a
  global halt, 3 usage; `--path RUN_DIR` adds a folder, `--no-halt` only reports).
- `heartbeat` (Task AP): the same harness's heartbeat scenarios must hold
  semantically (stale / corrupt heartbeat and a lost Sentinel are global halts, exit-code matrix, gap-resync storm,
  all-markets-stale, stale-resume re-alarm, Reconciler halt stored, recording dir read).
- `exit_codes` (Task AS): `docs/EXIT_CODES.md` lists every entry point under `src/` and `scripts/` with exactly its
  `EXIT_*` codes and the resident Sentinel's action per code (`src/exit_codes.py`, `ast` only: no literal exit code in
  `main`, no argparse exit outside the registry, Sentinel column = `src/sentinel_tick.py` `EXIT_EVENTS`).

Exit codes: 0 every check passed (`risk_sha_head` may be `SKIP`), 1 a check failed, 2 usage error or no repo root.
It writes nothing in the repo (the `e2e` check keeps its scratch files in a temp folder outside the repo and
removes it). Task AM runs the same command in GitHub Actions as `python scripts/guard.py --require-git`
together with full pytest (`.github/workflows/ci.yml`; PRs and pushes to `dev`/`main` only).

## E2E offline gate (Task AJ)

A hard gate on the whole paper pipeline, offline, on fixed sanitised recordings (`tests/fixtures/e2e/`, see its
README). Run both before merging:

```
python scripts/guard.py                          # check e2e: the pipeline vs golden.json (stdlib + src only)
python -m pytest tests/test_e2e_offline.py       # the same run plus one test per scenario and invariant
```

Stages, all real modules (`scripts/e2e_offline.py`): Gamma rows -> `scanner.scan` -> `scanner.persist_markets`;
recorded CLOB `/book` bodies -> `book.record` (through its getter hook) -> `paper_cycle.run` (gap/stale signals,
Sentinel StaleWatch check 1) -> `endgame_scan` signals; forecasts -> `forecaster.emit_probability` -> Intent ->
`paper_ledger.commit_if_allowed` (RiskGate with `fees.py`, MIN_EDGE, staleness, wash, non_finite, caps) -> paper
fill -> `ledger_store.book` -> `ledger_store.verify` (conservation) -> `paper_cycle.run` (Sentinel check 2).
Only the clock (frozen `datetime` in `podlib`, `book_staleness`, `wash_detect`) and I/O paths (temp bus, ledger,
quotes, markets, books and Sentinel state; the `/book` getter; a concurrent-writer ledger path) are replaced.
`socket`, `urllib`, `http.client` and `podlib.urlopen` are tripwires: a touch fails the gate even if caught.

Scenarios: two allows (one fee-free, one with fees), a stale book (veto `stale_book`, no fill), a `NaN` ask from
the recorder (veto `non_finite_market_price` ...), wash against a fill 2 minutes old (`wash_same_market`), edge
0.015 (`edge_below_min`), a sixth leg (`max_open_positions`), a duplicate fill replay (deduped, cash and positions
unchanged; the next intent re-buys the open alpha leg and vetoes `add_to_open_leg`, issue #123), a ledger torn before the call (`invalid_risk_state`, nothing booked) and
during the call (Executor halt `ledger_integrity`, nothing booked), and two consecutive stale checks (Sentinel
halt, that market's paper quote cancelled). Every run also checks: no network, every bus message
`exchange_action` none and no order flag, fills only from a RiskGate allow, unique fill ids, risk.toml at the pin
and byte-identical, `live_enabled` false at every stage, no default `data/` or `logs/` path written.

Signal -> Intent (Task AO): `src/intent_builder.py` is the pure, tightening-only builder (SPEC_signal_to_intent).
The gate runs it in shadow mode (phase 1): RiskGate still receives the harness `build_intent` Intent (model_prob
from the signal, price/spread/book_ts from the book row, liquidity from the scanner row; fee_rate and slippage are
fixture inputs), the builder's reasons are recorded under `intent_builder` in golden (charlie `stale_book`, delta
`non_positive_edge`, echo `sell_requires_position`, foxtrot `edge_below_min`, alpha/bravo/golf none), its Intent
must equal `build_intent`'s field for field on the same token, and it must reject anything RiskGate vetoes for an
edge, price, spread, liquidity or staleness reason. Kelly is shadow only (computed, never sized); enforcing the
builder (phase 2) and a Kelly size need RiskGate `Intent.size_usd` (KEY, backlog #7). Endgame signals carry no
model probability (`missing_model_prob`). (Task AL connected the halt consumer: see "Halts and resume".)

Task AL halt scenarios (`halt_consumer` in golden, also guard check `halt`): (a) the Sentinel market halt from
check 2 vetoes a later intent on that market (`halt_market`) and the Maker refuses to quote it, while another
market is unaffected; (b) a corrupt halt state file vetoes an intent that is otherwise allowed
(`halt_state_corrupt`) and paper_cycle emits no gap signal for any market; (c) a market resume of a global halt is
rejected and, after a valid market resume, the global halt still blocks; (d) resumes from Sentinel or Executor, an
Operator resume without `approved_by`, and a `resume` bus line not from Operator are all rejected. The two
corrupt-ledger cases each leave a global halt (`invalid_risk_state`, `ledger_integrity`) in their own folder.

Golden updates are explicit: `python -m pytest tests/test_e2e_offline.py --update-golden` rewrites
`tests/fixtures/e2e/golden.json`, and that run fails on purpose. Review `git diff tests/fixtures/e2e/golden.json`
and rerun without the flag. The flag is off by default (a test checks it); the guard, a plain pytest run and
environment variables never write golden.

## Reconciliation on Data API v2 (Task AK)

Data API v1 is retired on 2026-10-24. `src/reconcile.py` is the read-only Reconciler on Data API v2 only; see
`docs/RECONCILE.md` for the endpoints, the fields that fail closed and the doc ambiguities.

```bash
python -m src.reconcile --user 0x<public proxy wallet> [--ledger logs/paper.jsonl] [--json]
```

It reads `/v2/status` first every round and stops if the data lag exceeds `DATA_API_MAX_LAG_S`.
Exit codes: 0 reconciled, 1 mismatch, 2 usage, 3 Data API unusable (fetch failed, too stale, malformed or missing
field, broken position identity, incomplete walk), 4 ledger invalid. Every 1/3/4 writes one `halt` bus message from Reconciler
(`exchange_action: none`). No key, no order, nothing written to the ledger. In Phase 0 the ledger is paper,
so against a real wallet it reports mismatches by design; it is not on the paper pipeline or the e2e gate.

## Halts and resume (Task AL)

Every halt is persisted in `logs/halt_state.json` (atomic write) before quotes are cancelled and the bus line is
written. RiskGate, the Maker and paper_cycle read it: a `market` halt blocks that market, a `global` halt every
market. An unreadable, corrupt, malformed or lost halt file, `logs/HALT`, or a failed check is a global halt.
`invalid_risk_state` and the Executor's `ledger_integrity` / `ledger_write_failed` halts are global. If the halt
record or the bus write or the quote cancel fails, `logs/HALT` is written (global). Nothing lifts a halt on its own.

Only the Operator (家伟) resumes, one halt at a time, after the checks in Poly哨兵's design (postmortem written,
risk.toml sha and PINS unchanged, `python scripts/guard.py --require-git` exit 0, pytest green, fresh data):

```
python -m src.halt_store status                       # active halts; exit 3 when any is active
python -m src.halt_store resume --scope market --halt-id H0001-... --market <id> \
    --approved-by 家伟 --root-cause "..." --postmortem "PR #NN" --checks '{"guard": "exit 0"}'
python -m src.halt_store resume --scope global --halt-id halt-file ...           # logs/HALT (renamed aside)
python -m src.halt_store resume --scope global --halt-id halt-state-problem ...  # bad halt file (moved aside)
```

The resume is written to the bus first (`type: resume`, `from: Operator`, `to: Sentinel`, `halt_ref`,
`approved_by`); if that fails nothing is released. A market resume never lifts a global halt, and a global resume
does not lift a market halt. Bots never run `resume`. No exchange action anywhere.

## Heartbeats and the resident Sentinel (Task AP)

Each loop writes `logs/heartbeat/<proc>.json` (atomic; a `.seen` marker beside it): ws_recorder and m1_run every
10 s (also while backing off), m1_rest after every REST sample, paper_cycle every cycle (`--every` <= 60 s),
scanner / endgame_scan / loop_claim at start and finish, the Sentinel every tick. The resident Sentinel:

```
python -m src.sentinel_tick            # resident: a tick every 30 s (SENTINEL_TICK_S); never resumes anything
python -m src.sentinel_tick --once     # one check-only tick, writes no Sentinel heartbeat; exit 3 if it raised
python -m src.paper_cycle --loop --recording-dir runs/<m1 run>   # read the run's rotated books/books-*.jsonl
```

Timeouts (src/thresholds.py, M2 pending): ws_recorder / m1_run 120 s and halt_consumer 30 s = global halt;
m1_rest 900 s = alert; scanner / endgame_scan / loop_claim started but not finished after 1800 s = alert;
paper_cycle 180 s = alert, 360 s = global halt. A heartbeat that is unreadable, corrupt, malformed, future-stamped,
lost after its marker exists or stamped off the risk pins is a global halt (fail closed). Exit codes per
(module, code): ws_recorder / m1_run 4, endgame_scan 4, loop_claim 2 = global halt; scanner 1 / 2 and scanner 3
twice in a row, m1_run 1 / 2 / 3 and any other non-zero = alert; m1_run 5 = log. Recorder: 4 reconnects in 600 s =
alert, 3 gap resyncs of one asset in 600 s = that market halted. All checked markets stale 2 cycles in a row =
global halt. Alerts go to `logs/alerts.jsonl` plus a Sentinel->Operator bus `signal`. Every code by module:
`docs/EXIT_CODES.md` (Task AS).

Once the resident Sentinel has beaten, RiskGate, the Maker and paper_cycle treat its heartbeat older than 90 s,
missing, corrupt or finished as a stored global halt (`sentinel_heartbeat_*`): stopping the Sentinel halts
everything until the Operator resumes it after restarting the Sentinel. Halts are released only by the Operator
resume above; a condition that still holds is halted again on the next tick or check. To retire a process for
good, let it finish cleanly, or (Operator) remove its heartbeat file and `.seen` marker.

A failed heartbeat write is the same kind of stop (issue #79): `Heartbeat.beat` records the global halt
`heartbeat_write_failed:<proc>` in the halt store at once (deduplicated, `logs/HALT` fallback, no bus, no
`.seen` needed), on the first write as well as on every later one. A good write afterwards does not lift it;
only the Operator resume does. When neither halt path can be written the beat keeps reporting the write failure
and nothing claims a persisted halt. The Sentinel tick state goes through the same generic writer and is never
a heartbeat event.

## Resolutions snapshot (Task AN)

M3 calibration step 1. Design: `docs/M3_CALIBRATION.md`. Read-only; no bus, no order, no key.

```bash
python -m src.resolutions
```

Walks Gamma `/markets/keyset?closed=true`. A row lands in gitignored `data/resolutions.jsonl` only after the
market has been closed 48 h (`closedTime`, `RESOLUTION_HOLD_HOURS`) with a unique 1.0/0.0 winner and every
required field present. Missing fields skip that row. Incomplete paging writes nothing (exit 3). Paper-pin
drift writes nothing (exit 4). Calibration consumers must filter `uma_checked=true`.


## Git

`git status` does not list `.env`. Ignored paths include `.env`, `data/`, and `logs/`.

## M1 acceptance (Task AI)

The 24-hour read-only WebSocket book run and its offline report are `python -m src.m1_run` and
`python -m src.m1_report`. Commands, outputs and the M2-pending acceptance criteria: `docs/M1_ACCEPTANCE.md`.

## Not in this phase

No live trading, no key material, no CLOB authentication, no withdraw, no automatic size-up, and no geo-bypass steps. A single-leg fill is directional exposure, not risk-free arbitrage. Nothing here is a promise of profit.

## Runtime pin checks (Task AS)

`src/pins.py` checks config/risk.toml as a file: sha256 `0aea23af…bc04` (CRLF normalised) and all 9 keys with value
and type (`RISK_FILE_PINS`), no unknown key. scanner, book, ws_recorder, m1_run, resolutions (exit 6), endgame_scan
(exit 4) and loop_claim (exit 2) check it at start, paper_cycle before every cycle (exit 1), ws_recorder and m1_run
again at least every `PIN_RECHECK_S` (60 s) while running (stop, exit 6, m1 manifest status `pin_drift`). Drift, or a
check that cannot run, records one global Sentinel halt `risk_toml_pin_drift` (the same one the Sentinel tick
records) and nothing is fetched or written. Restoring the file does not lift it: fix the file, then the Operator
resume above. Exit codes of every entry point: `docs/EXIT_CODES.md`.
