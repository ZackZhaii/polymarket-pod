---
name: ecc-error-handling
description: Error-handling and retry patterns for polymarket-pod (stdlib Python). Use when adding or reviewing exceptions, fail-closed checks, exit codes, or retries around public reads. Retry rules match podlib.public_get exactly; 4xx/429 are never retried without Operator approval.
---

# Error Handling (polymarket-pod)

Trimmed from the ECC error-handling skill. TypeScript/React/Go/FastAPI sections, Sentry/Datadog SDKs and every non-stdlib dependency were removed. Phase 0 paper; stdlib only (`requirements.txt` lists no packages). Installing any package or error-tracking SDK: 需 Operator 批准.

## Core principles

1. **Fail closed.** A missing, unparseable or out-of-range input must veto or stop, never pass. Examples on `dev`: `risk_gate.evaluate` reasons (`invalid_gate_threshold`, `invalid_risk_cap`, `invalid_risk_state`, `missing_book_ts`, `invalid_book_ts`, `future_book_ts`, `stale_book`), `scanner` rejection reasons, `book.book_timestamp` returning `book_ts=None` plus a reason.
2. **Fail loudly at the boundary.** Raise with a specific message (`RuntimeError("risk.toml missing required keys: ...")`, `RuntimeError("only Executor may emit fills")`) or return a named reason string; do not return a silent default.
3. **Never swallow errors silently.** Every `except` must handle (named reason / exit code), re-raise, or log to stderr. Known exception to flag, not copy: `podlib.read_jsonl` skips undecodable lines without a message.
4. **Errors are part of the contract.** Document every reason string and exit code a caller can see (skills `paper-fill`, `market-scan`, `book-watch`, `bus-contract`).
5. **No secrets in errors.** Messages and bus payloads never carry keys, `.env` content, or wallet data.

## Python patterns used here

- Reason strings over exception hierarchies for gates: `Decision.reasons: list[str]`, all reasons collected, any reason = veto.
- A narrow exception type when a caller must branch: `podlib.PublicGetError(url, attempts, last_error)`; `scanner.fetch_all_markets` catches only that one.
- Exit codes for CLIs (`scanner.EXIT_*`: 0 ok, 1 fewer than 5 scanned, 2 zero passed, 3 incomplete/network; `book.main`: 0, 1 no snapshot, 2 no markets file; `loop_claim`: 0, 2 pin, 3 busy, 4 closed).
- Bool and non-finite numbers rejected explicitly (`_finite_number` helpers): `True` is not 1, `nan`/`inf` never pass.

## Retry taxonomy (source of truth: `podlib.public_get` on dev)

| Failure | Retried? | What happens |
|---|---|---|
| `URLError`, `OSError` (incl. `ConnectionResetError`, `TimeoutError`/`socket.timeout`, `ssl.SSLError`/`SSLEOFError`), `http.client.HTTPException` (`TRANSIENT_NETWORK_ERRORS`) | yes | up to `PUBLIC_GET_ATTEMPTS = 3` attempts, sleeping `PUBLIC_GET_BACKOFF_S * 2**n` = 0.5 s then 1.0 s (no jitter) |
| `HTTPError` 5xx | yes | same budget (`podlib._retryable`: `code >= 500`) |
| `HTTPError` 4xx, including **429** | **no** | raised at once as `HTTPError`; `Retry-After` is not read |
| `json.JSONDecodeError` on the body | no | raised at once |
| URL outside Gamma `/markets` or CLOB `/book`, or a banned token (`/order`, `/auth`, `api-key`, `secret`, `passphrase`, `signature`, `withdraw`) | no | `RuntimeError` before any request |
| every attempt transient-failed | — | `PublicGetError`; message names the attempt count and last error |

Per-attempt timeout: `public_get(url, timeout=20.0)`. Tests patch `podlib._sleep` so they never wait (`tests/test_public_get_retry.py`).

Callers on dev:

- `scanner.fetch_all_markets`: `PublicGetError` → `stop_reason=network_error`, `complete=false`; `scanner.main` exits **3**, prints `scanner network error: ...`, writes nothing. A 4xx/429 or JSON error is not caught there and ends the run with a traceback (interpreter exit 1, nothing written).
- `book.snapshot`: any exception on one token → stderr `book_skip token=<12> reason=<ExceptionName>`, token skipped; `book.main` exits 1 only when no snapshot was written.
- `paper_session.run`: any exception from the market fetch → `reason=public_read_failed`.

Changing any of this (retrying 429, honouring `Retry-After`, more attempts, jitter, a second HTTP client): 需 Operator 批准, as a PR to `dev` with offline tests; see `api-health-ratelimit` for the 429 proposal.

## Checklist (before opening a PR)

- [ ] Every new `except` handles with a named reason/exit code, re-raises, or logs to stderr.
- [ ] Missing/unparseable/bool/nan/inf inputs veto or stop (tests cover each).
- [ ] New reason strings and exit codes are listed in the relevant skill and the PR body.
- [ ] Retries only through `podlib.public_get`; no new retry loop, no 4xx retry.
- [ ] `python -m pytest` passes offline; `config/risk.toml` sha unchanged; `live_enabled=false`.
