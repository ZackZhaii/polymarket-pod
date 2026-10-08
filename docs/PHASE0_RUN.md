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

## Risk file

`config/risk.toml` is unchanged:

- `mode = "paper"`
- `live_enabled = false`
- `require_human_gate = true`
- `max_order_pct = 0.01`
- `max_daily_loss_pct = 0.03`

SHA256 before and after the run: `0aea23af1e938d330b5b4fab2c6abdec9f475f16807d177a5860a4da32aebc04`.

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
  `/markets/keyset`, CLOB `/book`, the WS market channel); order/auth paths, the user channel, ports, credentials in
  the query and other hosts fail. Docstrings and comments may also link docs.polymarket.com and github.com.
- `thresholds_doc`: `docs/THRESHOLDS.md` agrees with `src/thresholds.py` (Task AC table parser).
- `e2e`: the end-to-end offline gate below (Task AJ). It loads the checked repo's own `scripts/e2e_offline.py`,
  runs the real pipeline on `tests/fixtures/e2e/` and compares it exactly with `golden.json`. About 0.1 s; no
  pytest needed.

Exit codes: 0 every check passed (`risk_sha_head` may be `SKIP`), 1 a check failed, 2 usage error or no repo root.
It writes nothing in the repo (the `e2e` check keeps its scratch files in a temp folder outside the repo and
removes it) and adds no CI: running it in CI is the user's decision.

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
unchanged, the next fill books once), a ledger torn before the call (`invalid_risk_state`, nothing booked) and
during the call (Executor halt `ledger_integrity`, nothing booked), and two consecutive stale checks (Sentinel
halt, that market's paper quote cancelled). Every run also checks: no network, every bus message
`exchange_action` none and no order flag, fills only from a RiskGate allow, unique fill ids, risk.toml at the pin
and byte-identical, `live_enabled` false at every stage, no default `data/` or `logs/` path written.

Not connected in code yet (the gate builds or skips them, and says so): no src module turns a NewsForecaster
signal into an Intent (`build_intent` in the harness does: model_prob from the signal, price/spread/book_ts from
the book row, liquidity from the scanner row; fee_rate and slippage are fixture inputs); Endgame signals carry no
model probability, so they stop at the signal; nothing consumes a Sentinel or Executor halt yet (later intents
are not blocked by it).

Golden updates are explicit: `python -m pytest tests/test_e2e_offline.py --update-golden` rewrites
`tests/fixtures/e2e/golden.json`, and that run fails on purpose. Review `git diff tests/fixtures/e2e/golden.json`
and rerun without the flag. The flag is off by default (a test checks it); the guard, a plain pytest run and
environment variables never write golden.

## Git

`git status` does not list `.env`. Ignored paths include `.env`, `data/`, and `logs/`.

## M1 acceptance (Task AI)

The 24-hour read-only WebSocket book run and its offline report are `python -m src.m1_run` and
`python -m src.m1_report`. Commands, outputs and the M2-pending acceptance criteria: `docs/M1_ACCEPTANCE.md`.

## Not in this phase

No live trading, no key material, no CLOB authentication, no withdraw, no automatic size-up, and no geo-bypass steps. A single-leg fill is directional exposure, not risk-free arbitrage. Nothing here is a promise of profit.
