---
name: pod-loop
description: Use when starting, claiming, or finishing one polymarket-pod paper-loop task (src/loop_claim.py, docs/LOOP.md, docs/loop/state.json). One task, PR only, never merge, never touch live_enabled.
---

# pod-loop

Queue: `docs/loop/state.json` (`loop_claim.STATE_PATH`), read by humans in `docs/LOOP.md`. Surfaces: `grok-build-local`, `grok-web`, `grok-bot` (`loop_claim.SURFACES`). Poly* bots claim as `grok-bot`. `claim_hours` is 1 and `cadence_minutes` is 30 in the committed state file.

## Allowed

- `python src/loop_claim.py status` — prints `{"claim": ..., "next": <id or null>}`.
- `python src/loop_claim.py claim --surface <surface> --task <letter>` — only the first `todo`, or a refresh of your own unexpired claim (`loop_claim.claim_state`). Sets `status=doing` and `claim.until` to now plus `claim_hours` (default 3 only when the key is missing; the file sets 1).
- `python src/loop_claim.py release --surface <surface> --done` or `--todo` — only the surface that holds the claim (`loop_claim.release_state`). `--done` stamps `done_at`. Exactly one of `--done` / `--todo`.
- Reading `docs/LINK.md` before claiming. Expired claims may be taken over after checking that `dev` does not already have the task done (`docs/LINK.md`).

## Forbidden

- `git push origin main` or `git push origin dev`, pushing queue files to either, or merging your own PR. Bots never merge (`AGENTS.md`, `docs/LINK.md`). `.grok/config.toml` denies `git push origin main`, `gh pr merge`, and GitHub merge/push/update tools.
- Editing `config/risk.toml` or `live_enabled`. `loop_claim.pins_ok` runs `podlib.pin_problems` first and exits `EXIT_PIN = 2` with `live_enabled must stay false; queue not changed` when any pin drifts.
- Claiming a task that is not the first `todo`: exit `EXIT_CLOSED = 4`, `task is not open`. Someone else's unexpired claim: exit `EXIT_BUSY = 3`, `claim busy`.
- A second task in the same session. No live-trading task, even when the queue is empty (`docs/loop/status.json`: `next: null`).
- Inventing a task. A bot may only PR a pure-code item from the `docs/COORDINATION.md` risk-gap backlog. Each bot keeps at most 2 open PRs, all kinds counted (self-picked tasks, docs, verification; `AGENTS.md`).

## Steps

1. Sync: bots `git fetch origin` and work in `git worktree add .worktrees/<name> -b <branch> origin/dev`. Only Operator's local Grok Build pulls in the main directory (`AGENTS.md`). `.worktrees/` goes in `.git/info/exclude`, not `.gitignore`. Remove the worktree when done.
2. `python src/loop_claim.py status`. Stop if `claim` is held and unexpired by someone else, if `live_enabled` is not false, or if `next` is null.
3. Claim the first `todo` only. Commit the queue change on a branch and open a PR with base `dev`. Commit identity is per command: `git -c user.name=ZackZhaii -c user.email=17690114@qq.com` — do not change global git config.
4. Task letter: the next letter unused on `dev`, on `main`, and in every open PR (`AGENTS.md`). Read the last id from `docs/loop/state.json` on `origin/dev` and from open PRs; do not trust a letter written in a skill.
5. Do the one task. Then `python -m pytest` with no network and no credentials.
6. Release `--done` in the same PR. Write five lines under `docs/LOOP.md` `## Latest`: what was done, the veto or exit evidence, pytest count, risk.toml unchanged plus `live_enabled=false`, and the queue state (see the M block for shape). Then stop. Poly指挥 merges into `dev`; `dev` -> `main` is a Poly指挥 PR that a human reviews and merges.

## Verification

- `python src/loop_claim.py status` exits 0 and shows `claim: null` before you claim.
- `python -m pytest` passes offline.
- `git show HEAD:config/risk.toml | sha256sum` prints `0aea23af1e938d330b5b4fab2c6abdec9f475f16807d177a5860a4da32aebc04` (`docs/COORDINATION.md`, `docs/PHASE0_RUN.md`). A different worktree hash with an empty `git diff -- config/risk.toml` is CRLF only.
- `live_enabled = false` and `require_human_gate = true` still in the file.
