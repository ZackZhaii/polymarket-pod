# Roadmap decisions

Five Operator / Poly指挥 decisions that govern this repo. Changing them is an Operator
action, not a bot task. Recorded 2026-10-09 (Task AM) from `docs/COORDINATION.md`,
`docs/LOOP.md`, `docs/PHASE0_RUN.md`, `AGENTS.md`, and Task AH.

## 1. Phase 0 is paper only

`config/risk.toml` stays `mode = "paper"`, `live_enabled = false`, `require_human_gate = true`.
Nobody reads `.env`, signs, places or cancels an exchange order, installs a trading MCP, or
sends outbound messages. A single-leg fill is directional exposure, not locked profit. Wash,
self-trade, spoofing, withdraw, size-up and geo-bypass are forbidden.

## 2. Daily truth is `origin/dev`

The shared source of truth is GitHub `ZackZhaii/polymarket-pod` branch `dev`. Work branches
from `origin/dev` in `.worktrees/<name>`. Pull requests use base `dev`. Bots do not push
`main` or `dev`, do not force-push, and do not merge their own PRs. `main` only receives a
`dev` → `main` PR opened by Poly指挥 and merged after human review. Queue state
(`docs/loop/state.json`, `docs/loop/status.json`, `docs/LOOP.md` `## Latest`) also travels
by PR (Operator 2026-10-08).

## 3. `config/risk.toml` is pinned

The committed file sha256 is `0aea23af1e938d330b5b4fab2c6abdec9f475f16807d177a5860a4da32aebc04`
(`git show HEAD:config/risk.toml`). Bot PRs do not edit `config/risk.toml`. Threshold ideas
go in the PR text or wait for M2; they do not land as file edits. Windows `core.autocrlf`
can change the working-tree hash; trust the committed hash.

## 4. CI is KEY: guard plus full pytest, no secrets

Task AH added the local M0 guard and left CI with the user. This decision wires
`python scripts/guard.py --require-git` and full `python -m pytest` into GitHub Actions
(`.github/workflows/ci.yml`). The workflow runs only on pull requests and on pushes to
`dev` and `main`. `permissions: contents: read`. No secrets. Third-party actions are pinned
to a full commit SHA. Poly指挥 reviews and merges the CI PR.

## 5. M2 thresholds stay code constants until the Operator decides

Until M2, these stay in `src/thresholds.py` / `docs/THRESHOLDS.md` and are not moved into
`risk.toml`: `MAX_BOOK_AGE_S`, `MIN_EDGE`, `SENTINEL_STALE_S`, `SENTINEL_STALE_CONSECUTIVE`,
`WASH_WINDOW_S`, `MIN_HOURS_TO_EXPIRY`, `WS_ROW_REFRESH_S`, the eight M1 criteria
(`M1_MIN_RUN_S`, `M1_MIN_COVERAGE`, `M1_MAX_RECONNECTS`, `M1_MAX_GAP_RESYNCS_PER_ASSET`,
`M1_REST_MATCH_WINDOW_S`, `M1_MIN_REST_MATCHED`, `M1_MAX_TOP_MISMATCH_RATE`,
`M1_MAX_REST_UNMATCHED_RATE`), and Task AK's reconcile numbers (`RECONCILE_SHARES_TOL`,
`DATA_API_POSITIONS_DUST_SHARES`, `DATA_API_TRADES_MIN_SHARES`, `DATA_API_MAX_LAG_S`,
`DATA_API_IDENTITY_TOL_USDC`). Callers may only make a limit stricter. Live trading, turning
`live_enabled` on, and size-up are not queue tasks.
