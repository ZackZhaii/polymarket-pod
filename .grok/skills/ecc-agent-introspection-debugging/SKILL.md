---
name: ecc-agent-introspection-debugging
description: Structured self-debugging when a polymarket-pod agent run loops, stalls, or drifts. Capture, diagnose, contained recovery, report. Uses the bus timeline (logs/bus.jsonl), git/worktree state and test output; never reads .env or keys.
---

# Agent Introspection Debugging (polymarket-pod)

Trimmed from the ECC agent-introspection-debugging skill. Production APM agents, dotenv/key reads, shell that prints `.env`, and references to skills that are not in this repo were removed. Never read `.env` or any credential file while debugging.

## When to use

- The same command or tool call is repeated without progress.
- A test keeps failing after a "fix".
- Files, branch, or worktree state differ from what you expect.
- A public read keeps failing.

## Phase 1: capture

```markdown
## Failure capture
- Task / PR:
- Goal in progress:
- Error (exact text):
- Last successful step:
- Last failed command:
- Repeated pattern:
- State to verify: worktree path, branch, `git status`, origin/dev SHA
```

## Phase 2: diagnose

| Pattern | Likely cause | Check |
|---|---|---|
| same command repeated | no exit condition | last N commands |
| `PublicGetError`, scanner exit 3, `stop_reason=network_error` | network/TLS after 3 attempts | stderr `scanner network error: ...`; `api-health-ratelimit` |
| HTTP 429 / 4xx | not retried by `public_get` (by design) | do not loop; report; retrying 429 needs Operator approval |
| TLS handshake failure on `git push` | transient network | retry the same push a few times with a pause; never force-push |
| "unreachable" machine, command may have run | interrupted session | check state (branch, files, origin) before re-running |
| file missing after write / stale diff | wrong cwd, wrong worktree, line endings | `git worktree list`, `git status`, path |
| test still failing | wrong hypothesis | run the single failing test; `diagnosing-bugs` |
| behaviour differs from a skill | doc drift | read the code on `origin/dev` (`ecc-living-docs-governance`) |

Bus timeline (`bus-observability`): read `logs/bus.jsonl` for the run's `ts` range and count by `(from, type)`, veto `reasons`, stale signals with `stale_reasons`, and books with `book_ts=null` (`book_ts_problem`). Read-only; never edit bus or paper lines.

## Phase 3: contained recovery

- Stop retrying; restate the objective in one sentence.
- Verify state directly (git, files, test output) instead of trusting memory.
- Narrow to one command, one file, or one test.
- Escalate when the next step is outside the rules (merge, push to `dev`/`main`, risk.toml, `.grok/config.toml` permissions, package installs): 需 Operator 批准.

Do not claim actions you did not run.

## Phase 4: report

```markdown
## Agent self-debug report
- Task:
- Failure:
- Root cause:
- Recovery action:
- Result: success | partial | blocked
- Evidence:
- Follow-up:
```
