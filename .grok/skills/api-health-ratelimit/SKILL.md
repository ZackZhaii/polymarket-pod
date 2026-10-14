---
name: api-health-ratelimit
description: Use when handling public Gamma/CLOB HTTP errors, defining a Sentinel health probe, or proposing heartbeat intervals for polymarket-pod. Matches podlib.public_get retry behavior. No trading endpoints.
---

# api-health-ratelimit

Only `podlib.public_get` may touch the network. Allowlist: Gamma markets prefix + CLOB `/book` (`podlib.GAMMA_PREFIX`, `BOOK_PREFIX`). Phase 0 paper; no keys.

## Allowed

- Document retry/backoff exactly as coded.
- Offline tests that mock `urlopen` / `_sleep` (`tests/test_public_get_retry.py`).
- Propose heartbeat / probe interval changes as Reflector proposals needing Poly风控 + Poly哨兵 + human approval before code lands. The coded values (`HEARTBEAT_RUNNER_EVERY_S` 30 s / `HEARTBEAT_RUNNER_TIMEOUT_S` 120 s, `HEARTBEAT_RECORDER_*`, `PUBLIC_GET_DEADLINE_S` 30 s) live in `src/thresholds.py` / `docs/THRESHOLDS.md`.

## Forbidden

- Retrying HTTP 4xx other than **429** — code never retries them (`podlib._retryable`: `HTTPError` with `code >= 500` or `code == 429`). A 429 on CLOB `/time` (`podlib.TIME_URL`) is not retried either (Task DQ).
- Calling `/order`, `/auth`, or any banned token URL (`public_get` guard).
- Installing SDKs or adding a second HTTP client.
- Treating a proposal as live config. Do not edit `config/risk.toml`.

## Steps

### 1. Current retry (source of truth)

- Attempts: `podlib.PUBLIC_GET_ATTEMPTS = 3`
- Backoff: `PUBLIC_GET_BACKOFF_S * 2**n` → 0.5 s, then 1.0 s (`podlib.public_get`)
- Retried: `TRANSIENT_NETWORK_ERRORS` (URLError, OSError, `http.client.HTTPException`, `ssl.SSLError`, `socket.timeout`) and HTTP 5xx (`HTTPError` is checked first, so 4xx never counts as a URLError/OSError retry)
- Not retried (raised at once, not wrapped): HTTP 4xx other than 429 as `HTTPError` (tests cover 400, 404, 422), `json.JSONDecodeError` from the body, and the URL allowlist / banned-token guard (`RuntimeError`, raised before any request). A 429 on `/time` raises `PublicGetError` after one attempt (Task DQ).
- Exhaustion: `podlib.PublicGetError(url, attempts, last_error)`; `str()` names the attempt count and the last error
- Timeout per attempt: `public_get(url, timeout=20.0)`, clipped to the time left in the call's deadline; tests patch `podlib._sleep` and `podlib._monotonic` so they never wait

### 2. 429 / Retry-After

- **Coded (Task DP, #309):** a 429 is retried inside the same `PUBLIC_GET_ATTEMPTS` budget after waiting its `Retry-After` as integer seconds (`podlib._retry_after_s`, ASCII digits only, capped at `PUBLIC_GET_RETRY_AFTER_MAX_S` = 30 s); a missing, HTTP-date, negative or fractional header falls back to the `PUBLIC_GET_BACKOFF_S * 2**n` backoff. Exhausted: `PublicGetError`.
- **Coded (Task DQ):** one `public_get` call never runs past `PUBLIC_GET_DEADLINE_S` = 30 s (`src/thresholds.py`, M2 pending): each attempt's timeout is clipped to the time left and a wait that would end past the deadline is not taken (`PublicGetError` at once), so a `Retry-After: 30` storm fails a GET in about one request. CLOB `/time` never retries a 429. m2_runner beats its heartbeat (throttled) after every GET inside a tick, and m1_run's start-up clock check beats after every asset, so a storm cannot stale either heartbeat (120 s). KILL is read at the start of each m2_runner tick, so a KILL file can be acted on at most one tick late.

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

Only `PublicGetError` is caught there. Since Task DP a 429 is retried and, still failing (attempts or the `PUBLIC_GET_DEADLINE_S` deadline exhausted), becomes `PublicGetError` (exit 3, nothing written). Any other 4xx or a JSON error from a Gamma page is not caught by the scanner and ends the run with a Python traceback (interpreter exit 1, nothing written). In `book.snapshot` any exception on one token prints `book_skip token=<12> reason=<ExceptionName>` and that token is skipped.

## Verification

- `python -m pytest tests/test_public_get_retry.py tests/test_public_get_deadline.py tests/test_ratelimit_storm.py` — 429 retried via Retry-After within the 30 s deadline; `/time` 429 not retried; other 4xx not retried; 5xx retried; SSL EOF retried; one m2_runner tick and the m1_run start-up each end within 120 s in a 429 storm.
- No new network hosts beyond allowlist.
- Proposals only in `playbook/proposals.md` / bus `proposal`; risk.toml sha unchanged.
