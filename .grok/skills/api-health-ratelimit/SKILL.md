---
name: api-health-ratelimit
description: Use when handling public Gamma/CLOB HTTP errors, defining a Sentinel health probe, or proposing heartbeat intervals for polymarket-pod. Matches podlib.public_get retry behavior. No trading endpoints.
---

# api-health-ratelimit

Only `podlib.public_get` may touch the network. Allowlist: Gamma markets prefix + CLOB `/book` (`podlib.GAMMA_PREFIX`, `BOOK_PREFIX`). Phase 0 paper; no keys.

## Allowed

- Document retry/backoff exactly as coded.
- Offline tests that mock `urlopen` / `_sleep` (`tests/test_public_get_retry.py`).
- Propose heartbeat / probe intervals as Reflector proposals needing Poly风控 + Poly哨兵 + human approval before code lands.

## Forbidden

- Retrying HTTP 4xx (including **429**) — code never retries them (`podlib._retryable`: only `HTTPError` with `code >= 500`).
- Calling `/order`, `/auth`, or any banned token URL (`public_get` guard).
- Installing SDKs or adding a second HTTP client.
- Treating a proposal as live config. Do not edit `config/risk.toml`.

## Steps

### 1. Current retry (source of truth)

- Attempts: `podlib.PUBLIC_GET_ATTEMPTS = 3`
- Backoff: `PUBLIC_GET_BACKOFF_S * 2**n` → 0.5 s, then 1.0 s (`podlib.public_get`)
- Retried: `TRANSIENT_NETWORK_ERRORS` (URLError, OSError, `http.client.HTTPException`, `ssl.SSLError`, `socket.timeout`) and HTTP 5xx (`HTTPError` is checked first, so 4xx never counts as a URLError/OSError retry)
- Not retried (raised at once, not wrapped): HTTP 4xx as `HTTPError` (tests cover 400, 404, 422, **429**), `json.JSONDecodeError` from the body, and the URL allowlist / banned-token guard (`RuntimeError`, raised before any request)
- Exhaustion: `podlib.PublicGetError(url, attempts, last_error)`; `str()` names the attempt count and the last error
- Timeout per attempt: `public_get(url, timeout=20.0)`; tests patch `podlib._sleep` so they never wait

### 2. 429 / Retry-After

- **Today:** 429 raises immediately; `Retry-After` is **not read** — TBD (not in repo).
- **Proposal (needs Poly风控/Poly哨兵 approval):** on 429 only, sleep `Retry-After` seconds (cap TBD, e.g. 60) once, then resume the same attempt budget; still never POST orders. Until approved, leave code as-is and surface 429 to the operator.

### 3. Health probe definition (Sentinel)

`src/sentinel.py` is a stub: halt + cancel local paper quotes only. No probe — TBD (not in repo).

**Proposed probe (paper, read-only):**

1. `GET` one Gamma keyset page (`scanner.GAMMA_KEYSET + "&limit=1"`) via `public_get`.
2. `GET` one CLOB `/book?token_id=` from the latest `data/books.jsonl` or markets token list.
3. Success = both return within timeout (default 20 s) after the normal retry policy.
4. Failure = `PublicGetError` or scanner-style `stop_reason=network_error` (`scanner.fetch_all_markets`).
5. On failure: emit bus `signal` to Sentinel (or call `sentinel.halt` only when Operator/playbook says so). `exchange_action` stays `none`.

### 4. Heartbeat interval proposal

- COORDINATION backlog item 9: alert after **2 consecutive** failures; 30 s too noisy; 120 s book stale is code.
- Heartbeat period: **TBD (not in repo)**. Suggested proposal text for Reflector: "Sentinel heartbeat every 60 s; alert after 2 consecutive probe failures; do not change risk.toml."
- Must be approved by Poly风控 + Poly哨兵 + 家伟 before implementation.

### 5. Scanner incomplete path

When a page still fails after retries: `fetch_all_markets` catches `PublicGetError`, sets `stop_reason=network_error`, `complete=false` and keeps the message in `error`; `scanner.main` prints `scanner network error: ...` to stderr, exits **3** and writes nothing. Treat exit 3 as unhealthy for that run.

Only `PublicGetError` is caught there. A 4xx (including 429) or a JSON error from a Gamma page is not caught by the scanner and ends the run with a Python traceback (interpreter exit 1, nothing written). In `book.snapshot` any exception on one token prints `book_skip token=<12> reason=<ExceptionName>` and that token is skipped.

## Verification

- `python -m pytest tests/test_public_get_retry.py` — 429 not retried; 5xx retried; SSL EOF retried.
- No new network hosts beyond allowlist.
- Proposals only in `playbook/proposals.md` / bus `proposal`; risk.toml sha unchanged.
