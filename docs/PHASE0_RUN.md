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

## Git

`git status` does not list `.env`. Ignored paths include `.env`, `data/`, and `logs/`.

## Not in this phase

No live trading, no key material, no CLOB authentication, no withdraw, no automatic size-up, and no geo-bypass steps. A single-leg fill is directional exposure, not risk-free arbitrage. Nothing here is a promise of profit.
