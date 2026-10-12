---
name: polymarket-public-readonly
description: Use before any network call, dependency change, or MCP change in polymarket-pod. Enforces the Phase 0 public-GET allowlist that podlib.public_get actually permits, and blocks order, auth, wallet, and trading-SDK paths.
---

# polymarket-public-readonly

Phase 0 is paper. `config/risk.toml` pins `mode = "paper"`, `live_enabled = false`, `require_human_gate = true` (`podlib.PINS`). Runtime is the Python standard library only (`requirements.txt`). No private keys. Trading MCP stays disabled.

## Allowed

Only what `podlib.public_get` accepts, and only as GET (`podlib.py:public_get`):

- `https://gamma-api.polymarket.com/markets...` (`podlib.GAMMA_PREFIX`). Callers today:
  - `scanner.GAMMA`: `GET /markets?closed=false&limit=20&order=liquidityNum&ascending=false` (`scanner.fetch_markets`, used by `paper_session.run`).
  - `scanner.GAMMA_KEYSET`: `GET /markets/keyset?closed=false&active=true&order=volume24hr&ascending=false&limit=100`, plus optional `&liquidity_num_min=` and `&after_cursor=` (`scanner.fetch_all_markets`).
- `https://clob.polymarket.com/book?...` (`podlib.BOOK_PREFIX`). Caller today: `book.snapshot` builds `GET /book?token_id=<id>`.
- Request headers only: `User-Agent: polymarket-pod-phase0`, `Accept: application/json`. Method is hard-coded `GET`.
- Retries (`podlib.PUBLIC_GET_ATTEMPTS = 3`, backoff `podlib.PUBLIC_GET_BACKOFF_S * 2**n` = 0.5 s, 1 s) cover `podlib.TRANSIENT_NETWORK_ERRORS` (`URLError`, `OSError`, `http.client.HTTPException`, `ssl.SSLError`, `socket.timeout`) and HTTP 5xx. HTTP 4xx and the URL guard are never retried. Exhaustion raises `podlib.PublicGetError`.

## Forbidden

- Any URL that does not start with `GAMMA_PREFIX` or `BOOK_PREFIX` (`public_get` raises `RuntimeError`).
- Any URL whose lowercased text contains `/order`, `/auth`, `api-key`, `secret`, `passphrase`, `signature`, or `withdraw` (`public_get` banned tokens). That covers `POST`/`DELETE /order` and `/orders`, `/auth/api-key`, `/auth/derive-api-key`, and relayer/bridge/withdraw paths even if someone hides them under an allowed prefix.
- `POLY_*` auth headers (`POLY_API_KEY`, `POLY_API_SECRET`, `POLY_API_PASSPHRASE`, `POLY_PRIVATE_KEY`, signature headers). `requirements.txt` says Phase 0 reads none of these. Do not read `.env`.
- Any websocket. The user channel does not exist in this repo. The market channel is not implemented either; it stays a future option under `book-watch`, never the user channel.
- Importing or installing `py-clob-client`, `py-clob-client-v2`, `polymarket-client`, `py-sdk`, or `@polymarket/clob-client`. `requirements.txt` bans the archived client, and `.grok/config.toml` `[permission].deny` blocks installing `py-clob-client`, `py-clob-client-v2`, `polymarket-client`, `py-sdk`, `@polymarket/*` and the Polymarket MCP servers. `docs/SOURCES.md` lists the SDKs under "Reference components (study, do not copy claims)".
- Adding an endpoint inside a skill or a chat. New endpoints need a PR plus Poly风控 (RiskGate) approval, then the human gate (家伟).

## Steps

1. Before adding a URL, confirm it matches one prefix above and contains no banned token.
2. Keep the call on `podlib.public_get`. Do not open a second HTTP client.
3. Do not add a dependency. `requirements.txt` stays a comment-only file.
4. Do not print response bodies that could contain account data; these endpoints are public market data only.

## Verification

- `python -m pytest` passes offline (tests mock `urlopen`; no network, no credentials).
- `git show HEAD:config/risk.toml | sha256sum` equals `0aea23af1e938d330b5b4fab2c6abdec9f475f16807d177a5860a4da32aebc04` (`docs/COORDINATION.md`). On Windows, `core.autocrlf=true` makes the worktree hash differ; an empty `git diff -- config/risk.toml` means only line endings changed.
- `live_enabled` is still false.

## Backlog (Operator, not done here)

Partial guard today: `tests/test_roles.py::test_modules_import_and_do_not_call_the_network` checks only four stubs (`arb_binary`, `maker`, `forecaster`, `sentinel`) for `urlopen`, `clob.polymarket`, `py_clob_client`. No repo-wide guard exists. Outline for a future `tests/test_public_allowlist.py`:

- Read every `src/**/*.py` as text. Fail if any line matches a forbidden import: `py_clob_client`, `py_clob_client_v2`, `polymarket_client`, `py_sdk`, `clob-client`.
- Fail if any string literal contains `wss://`, `/auth/api-key`, `/auth/derive-api-key`, `POLY_API_KEY`, `POLY_API_SECRET`, `POLY_API_PASSPHRASE`, `POLY_PRIVATE_KEY`, or a method other than GET on a polymarket host.
- Allow the documented guard text in `podlib.public_get` (the banned-token tuple) and comments that name the ban.
- Mark the test `pytest.mark.skip` until the Operator files it as a queue task. Do not add it in a skill-only change.
