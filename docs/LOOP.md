# Paper loop handoff

Use this when a Grok Bot chat gets long. Start a new chat. Do not carry the old transcript.
Read this file, `docs/PHASE0_RUN.md`, and `config/risk.toml`. Then do one task below.

Phase 0 work lands on `dev` (branch from `origin/dev`; PRs use base `dev`; Poly指挥 opens dev→main PRs). Scanner, public book, and gated paper ledger already run.
`live_enabled` is false. Do not change that. Do not read `.env`. Do not place orders.

## How to split chats

One chat, one task, one finish line. Do not start a second task in the same chat.
When the diff is committed, write five lines at the bottom of this file under `## Latest` and stop.
Branch from `origin/dev`; pull requests target `dev`. Poly指挥 merges into `dev` and opens `dev` -> `main` pull requests for human review.
Never push `main` or `dev`, never merge your own pull request, keep at most 2 open pull requests per bot. Escalated items are never auto-merged.
The next chat starts from that block.

A single-leg fill is directional exposure, not risk-free arbitrage. Do not promise profit.
No wash trades, no self-trades, no spoofing, no withdraw, no size-up, no geo-bypass steps.

## Task A — tests

Status: done.

Finish when `python -m pytest` passes with no network and no credentials.

- Add stdlib-only tests for YES+NO ask sum and multi-outcome ask sum.
- A sum below 1 is a gap signal, not a locked profit. One filled leg is directional.
- Add tests that RiskGate vetoes: pin drift, non-positive edge, wide spread, banned strategy names.
- Tests must not read `.env` and must not import an order SDK.

## Task B — reflector

Status: done.

Finish when a proposal lands in `playbook/proposals.md` and `config/risk.toml` is byte-for-byte unchanged.

- At most 3 proposals per UTC day.
- Bus type is `proposal` from `Reflector`. It does not emit `fill`.
- Proposals do not edit `config/risk.toml` or any size cap.

## Task C — remaining role stubs

Status: done.

Finish when these modules import and only append to `logs/bus.jsonl`: ArbBinary, Maker, Forecaster, Sentinel.

- Forecaster never emits an order or a fill.
- Sentinel may emit `halt` only. Halt cancels paper quotes in the local ledger. It does not call CLOB.
- ArbBinary may emit a `signal` when the displayed ask sum is below 1. It must not emit a fill.
- Maker emits paper quote signals only. No post, no cancel on the exchange.

## Task D — offline paper cycle

Status: done.

Finish when `python src/paper_cycle.py` reads local jsonl, emits signals only, and `python -m pytest` passes.

- NegRiskArb emits a signal when three or more displayed asks sum to less than 1.
- BookWatcher emits staleness. It does not call the exchange and does not auto-halt.
- The cycle must not write a paper fill and must not edit `config/risk.toml`.

## Task E — one public paper session

Status: done.

Finish when `python src/paper_session.py` can record one public read, then the offline cycle, and `python -m pytest` passes with no credentials.

- Scanner uses the public Gamma markets endpoint. BookWatcher uses public CLOB `/book` only.
- The session appends `logs/sessions.jsonl`. It does not write a paper fill.
- `live_enabled` stays false. The command does not read `.env` and does not call an order endpoint.
- A gap remains a signal, not locked profit.

## Task F — Endgame signal only

Status: done.

Finish when `src/endgame.py` emits a signal for a fixture price between 0.98 and 0.99 whose market ends within 48 hours, and `python -m pytest` passes with no network and no credentials.

- Signal only. No fill and no order.
- Do not edit `config/risk.toml`.

## Task G — offline Brier proposal

Status: done.

Finish when `src/quant.py` scores a local fixture with Brier `mean((p-y)^2)`, emits at most one proposal, and `python -m pytest` passes offline.

- No network and no credentials.
- The proposal does not edit `config/risk.toml` or any size cap.

## Task H — SportsDevig signal only

Status: done.

Finish when `src/sports_devig.py` removes the overround from a two-way decimal-odds fixture and emits a signal only if fair probability exceeds market price plus slippage.

- Signal only. No fill, no quote, and no order.
- A positive edge is not locked profit. One leg is directional.
- No network. Do not edit `config/risk.toml`.

## Task I — scanner filter, long-tail band, keyset paging

Status: done.

Owner: Scanner (Poly扫描). Claim as surface `grok-bot` (any surface may verify).
Order: the operator ordered this task ahead of G on 2026-10-08. G (5332d4a) and H — SportsDevig (26667aa) were already done,
so I is the only todo. Merging PR #2 is the human add the `## Latest` rule asks for.
The code ships in PR #2. Whoever claims I verifies it and closes it.

Finish when `src/scanner.py` walks public Gamma `/markets/keyset` (24h volume desc, `liquidity_num_min` = `min_liquidity_usd`),
filters by `min_liquidity_usd` and `max_spread` from `config/risk.toml`, drops expired, closed, not-accepting, one-sided
and long-tail (YES mid < 0.03 or > 0.97) markets, reports first-reason rejection counts, and `python -m pytest`
passes with no network and no credentials. Record one public `python src/scanner.py` run (exit code, passed, bus lines) under `## Latest`.

- Missing, non-numeric, boolean, non-finite or invalid scanner keys raise: `max_spread <= 0`, `min_liquidity_usd < 0`,
  or a band outside `0 <= scanner_min_price < scanner_max_price <= 1`. No looser default.
- Paging stops on a repeated cursor (including A->B->A) or a page with no new markets, and reports `stop_reason`.
- `MAX_MARKETS = 50000`. Today's filtered universe is about 15,000 markets (about 150 pages).
- Every passing market goes to `data/markets.jsonl`. Only the top 50 by 24h volume become Scanner `signal` lines.
- Exit codes: 0 ok, 1 fewer than 5 scanned, 2 nothing passed, 3 paging incomplete (cap or stall).
  Codes 1-3 write nothing to `data/markets.jsonl` or the bus.
- Public reads only. No order, auth or signing endpoint. Do not edit `config/risk.toml`.
- Out of scope: paper_session switch, Endgame >0.97 feed, minimum time-to-expiry.

## Task J — risk_gate fails closed on missing thresholds

Status: done.

Risk-gap backlog item 11. Letter I is left for the scanner pull request (PR #2) so the two branches do not collide.

Finish when `src/risk_gate.py` has no default for `min_liquidity_usd` or `max_spread`, a bad threshold can only veto, and `python -m pytest` passes with no network and no credentials.

- `gate_thresholds(cfg)` accepts only a finite int or float, like the scanner thresholds. A missing key, bool, string, None, inf or nan raises `RuntimeError`.
- `min_liquidity_usd < 0` or `max_spread <= 0` also raises. There is no looser default.
- `evaluate` turns that error into the `invalid_gate_threshold` veto. It never raises for this and never allows.
- A present but nan, inf or bool intent liquidity or spread vetoes with `invalid_liquidity` or `invalid_spread`.
- Do not edit `config/risk.toml`. Parameter ideas go into the pull request text as a proposal only.

## Task K — public_get retry/backoff; scanner exits 3 on network failure

Status: done.

Self-picked by grok-bot. Two public `python src/scanner.py` runs on NoisePC (2026-10-08, Task I acceptance) crashed with
`URLError [SSL: UNEXPECTED_EOF_WHILE_READING]` and exit 1, which collides with the documented "fewer than 5 scanned" code.

Finish when `public_get` retries transient network/TLS failures, the scanner reports a page that still fails as paging incomplete
(exit 3, nothing written), and `python -m pytest` passes with no network and no credentials.

- `podlib.public_get` retries `URLError`, `ssl.SSLError` (including SSL EOF), `ConnectionResetError`, socket timeouts and HTTP 5xx.
  `PUBLIC_GET_ATTEMPTS = 3`, backoff `PUBLIC_GET_BACKOFF_S * 2**n` (0.5 s, 1 s). HTTP 4xx and the URL guard are never retried.
- When every attempt fails it raises `PublicGetError` (attempts, last error, URL). The scanner stops with `stop_reason=network_error`.
- Exit codes are unchanged: 3 now also covers a page that still fails after retries. Codes 1-3 write nothing.
- Tests mock `urlopen` and patch `podlib._sleep`, so they never touch the network or sleep. Do not edit `config/risk.toml`.

## Task L — order book staleness check; RiskGate vetoes a missing or stale book

Status: done.

Self-picked by grok-bot (Poly指挥 dispatch). Risk-gap backlog item 2: `risk_gate.py` never looked at the book timestamp.
120 s is Sentinel's proposal (backlog item 9).

Finish when a pure, offline helper flags a missing, unparseable, future or old book timestamp, RiskGate vetoes on it fail closed,
and `python -m pytest` passes with no network and no credentials.

- `src/book_staleness.py`: `book_age_problem(book_ts, now, max_age_s)` returns `None` when `0 <= age <= max_age_s`, else
  `missing_book_ts`, `invalid_book_ts`, `future_book_ts` or `stale_book`. A bad `max_age_s` or `now` returns a reason too.
- Accepted: ISO 8601 with a time and a zone (`Z` or offset), an aware datetime, or finite epoch seconds. Naive, date-only,
  numeric text, bool, nan and inf are invalid, not guessed.
- `Intent.book_ts` is new. `evaluate(..., now=None, max_book_age_s=MAX_BOOK_AGE_S)` adds the reason and never allows on it.
- `MAX_BOOK_AGE_S = 120` is a code constant. It is not read from `config/risk.toml`; moving it there is a proposal for the Operator.
- `paper_ledger` passes `book_ts` through (self-check stamps now; CLI `--book-ts`, missing vetoes). Do not edit `config/risk.toml`.

## Task M — hypothetical intents no longer skip the position and daily-loss caps

Status: done.

Self-picked by grok-bot (Poly指挥 dispatch). Risk-gap backlog item 4: `hypothetical=True` skipped `max_open_positions`
and `max_daily_loss` in `risk_gate.evaluate` (gate-test strategy only, but still a bypass).

Finish when both caps apply to every intent, a missing or invalid cap or state value vetoes fail closed,
and `python -m pytest` passes with no network and no credentials.

- `risk_gate.evaluate` drops the `if not intent.hypothetical` branch. Hypothetical and real intents get the same cap reasons;
  an already-open market is still exempt from the position cap.
- `risk_caps(cfg)` reads `max_open_positions` (whole number >= 0) and `max_daily_loss_pct` (> 0) with no default.
  Missing, bool, string, None, inf or nan gives `invalid_risk_cap`.
- State `open_positions` (whole number >= 0) and `daily_loss_usd` (>= 0) missing or invalid gives `invalid_risk_state`
  instead of reading 0. `paper_ledger.load_state` always supplies both.
- No new threshold. `config/risk.toml` is unchanged; the keys it already has are now enforced for hypothetical intents too.

## Task N — Forecaster bus name matches ROSTER (NewsForecaster)

Status: done.

Self-picked by grok-bot (Poly指挥 dispatch). Risk-gap backlog item 12: `src/forecaster.py` wrote `from="Forecaster"`,
but docs/ROSTER.md and docs/COORDINATION.md call the role NewsForecaster.

Finish when the forecaster emits the ROSTER name, older callers and older bus lines still work,
and `python -m pytest` passes with no network and no credentials.

- `forecaster.BUS_NAME = "NewsForecaster"`; `emit_probability` uses it. Still probability only, never an order or a fill.
- `podlib.ROLE_ALIASES = {"Forecaster": "NewsForecaster"}` and `canonical_role(name)`. `bus()` writes the canonical
  `from` / `to`, so a script still passing `Forecaster` keeps working and the line says NewsForecaster.
- Existing `logs/bus.jsonl` lines are not rewritten; readers compare via `canonical_role`. No code reader matched on the old name.
- No risk parameter. `config/risk.toml` is unchanged.

## Task O — scanner minimum time-to-expiry; markets closer than 24 h do not pass

Status: done.

Self-picked by grok-bot (Poly指挥 dispatch). Risk-gap backlog item 8: the scanner let markets expiring today through
(2026-10-08 run: 944 of 8,409 passing markets had under 1 day left).

Finish when a pure, offline rule drops markets with less than the minimum time to expiry, fails closed on a bad end date,
and `python -m pytest` passes with no network and no credentials.

- `src/expiry_rule.py`: `expiry_problem(end_date, now, min_hours)` returns `None` when `end - now >= min_hours` (boundary passes),
  else `near_expiry`, `expired`, `missing_end_date` or `invalid_end_date`. A bad `min_hours` or `now` returns a reason too.
- Accepted end dates: ISO 8601 strings with a date, a time and a zone (`Z` or offset). Naive, date-only, numeric, bool and other
  non-string values are invalid, not guessed (the old scanner read naive and date-only strings as UTC).
- `MIN_HOURS_TO_EXPIRY = 24` is a code constant. It is not read from `config/risk.toml`; moving it there is a proposal for the Operator.
- `scanner.filter_markets` / `scan` / `main` take `min_hours_to_expiry` (and `now` for `scan`); the summary line prints it.
  Exit codes are unchanged; if nothing is left, main exits 2 as before. Do not edit `config/risk.toml`.

## Task P — single-leg arb fill is directional exposure; positions net per leg, never per pair

Status: done.

Self-picked by grok-bot (Poly指挥 dispatch). Risk-gap backlog item 5: the ledger netted shares per market, so a sell on one
token of a pair cancelled a buy on the other, and a filled pair took only one `max_open_positions` slot.

Finish when paper positions are netted per leg (market + token), one filled leg counts as a full directional position,
and `python -m pytest` passes with no network and no credentials.

- `src/paper_ledger.py`: `leg_positions(rows)` nets shares per `(market, token_id)`. Only a buy and a sell of the same token
  offset each other (closing). The two tokens of an arb pair never offset; a row with no `token_id` is its own unknown leg.
- `load_state(market, token_id)`: `open_positions` counts open legs; `leg_already_open` is True only for that exact token;
  `exposure_usd` / `leg_exposure_usd` = abs(net shares) x the leg's average fill price (1 USD per share if notional is missing).
- `src/risk_gate.py`: the `max_open_positions` exemption needs `leg_already_open is True`. Another open leg of the same market
  (the other side of a pair) is a new position. No new threshold. Do not edit `config/risk.toml`.

## Task Q — scanner picks outcome IDs by Gamma `version`; missing or unknown version rejects the market

Status: done.

Queued by Poly指挥, done by grok-bot. Gamma puts Polymarket Protocol V2 outcome IDs in `positionIds`; the scanner read only
`clobTokenIds`, so V2 markets came through with no token IDs and never got a book.

Finish when the scanner picks outcome IDs by Gamma's `version`, rejects markets without a known version, and `python -m pytest` passes with
no network and no credentials.

- `src/scanner.py`: `select_token_ids(market)` returns `(token_ids, source_field, problem)`. `version` `v2` reads `positionIds`,
  `v1` reads `clobTokenIds` (list or JSON-encoded list), even when both are present and differ (docs.polymarket.com, V2 migration).
- `version` missing/null/empty is the rejection reason `missing_version`; any other value (exact match only, so `V2`, `v3`,
  `2` all count) is `unsupported_version`. Both rank right after `closed`. No field is guessed from which IDs are present.
- Valid version but missing or malformed IDs, non-decimal V2 IDs, or an ID count that does not match `outcomes`: `token_ids`
  stays empty with a `token_ids_problem` (not a rejection, as before); no book is fetched downstream. Exit codes unchanged.

## Task R — book timestamps read one way: paper_cycle uses book_staleness; book.record puts the book's own book_ts on the bus

Status: done.

Assigned by Poly指挥, done by grok-bot. `paper_cycle._age_seconds` had its own strict `%Y-%m-%dT%H:%M:%SZ` parser and
counted a future book as fresh, while RiskGate (`src/book_staleness.py`) read offsets, fractions and epoch seconds and
vetoed a future book. `book.record` never put a book timestamp on the bus.

Finish when both read book time the same way and `python -m pytest` passes with no network and no credentials.

- `src/paper_cycle.py`: `_age_seconds` is gone. `_book_problem(ts, now, stale_after_s)` calls `book_age_problem`, so
  fresh means `0 <= age <= 120` (exactly 120 s is fresh, 121 s stale). Future, missing or unparseable `ts`, an
  unparseable `now` (no wall-clock fallback) and a broken limit all count as stale. The stale signal adds `stale_reasons`.
  `STALE_AFTER_S` stays 120. No threshold changes and no risk.toml edit.
- `src/book.py`: `book_timestamp(raw)` reads the /book `timestamp` (Unix milliseconds as a string, per docs.polymarket.com;
  a non-negative int the same) or an aware ISO string, and returns ISO 8601 UTC with milliseconds and `Z`. Floats, signs,
  decimals, bools and naive times are `invalid_book_ts`; absent or blank is `missing_book_ts`.
- `book.snapshot` rows and the `book.record` bus payload carry `book_ts` and `book_ts_problem`. The recorder clock stays in
  `ts` and is never copied into `book_ts`. No usable timestamp: the snapshot is still recorded with `book_ts=null`, the
  reason and a stderr line, and RiskGate vetoes `missing_book_ts` on it (fail closed).

## Task S — structured wash/self-trade detection; a clean strategy name no longer gets through

Status: done.

Assigned by Poly指挥, done by grok-bot. Risk-gap backlog item 1: RiskGate caught wash/self-trade only by strategy-name
keywords (`wash`, `self-trade`, `self_trade`, `spoof`, `layering`), so a clean name trading against itself passed.

Finish when RiskGate vetoes structural wash/self-trade on top of the keyword ban and `python -m pytest` passes with no
network and no credentials.

- `src/wash_detect.py`: `wash_problems(...)` is pure and offline. Leg = market + token_id (a missing token_id is treated as
  possibly the same token). The two tokens of an arb pair are different legs and are not flagged (Task P).
- `cross_side_same_leg`: an uncancelled own paper quote (`quotes.jsonl` status `open`) rests on the opposite side of the
  same leg. No time window. `self_trade_structure`: `Intent.counterparty` is our own account, a pod role or one of our strategies.
- `wash_same_market` (hardening): an own paper fill on the opposite side of the same leg within `WASH_WINDOW_S = 300` s.
  300 s is a code constant, not a risk.toml key; the Operator decides whether it moves. It also blocks closing a leg within 300 s.
- Fail closed: missing history (`recent_fills` / `own_quotes`, supplied by `paper_ledger.load_state`) is `wash_input_missing`;
  malformed rows, an unknown quote status, a bad `now`/market or an unusable counterparty is `wash_input_invalid`. Both veto.

## Paste this into a new chat

```text
你是 polymarket-pod 的纸面循环入口。先读 AGENTS.md、docs/LINK.md、docs/LOOP.md、docs/loop/state.json、config/risk.toml。
bot 先 git fetch origin，在 .worktrees/<名字> 里从 origin/dev 开 worktree（只有 Operator 本机 Grok Build 在主目录 git pull），然后只领取第一件 todo。做完释放，开 PR（base=dev），不要推 main 或 dev，不要自己合并，然后停止。
禁止读 .env 和任何密钥。禁止改 live_enabled。禁止下单、提现、自动加仓。
禁止对敲、自成交、刷量、幌骗。单边成交不是无风险套利。不承诺收益。
```

## Latest

- 2026-10-09: Task S done by grok-bot (assigned by Poly指挥). RiskGate adds structured wash/self-trade checks on top of the name keywords.
- Same leg: own uncancelled opposite quote vetoes `cross_side_same_leg`; own opposite fill within 300 s vetoes `wash_same_market`.
- Own account/role/strategy as counterparty vetoes `self_trade_structure`. Missing or malformed history vetoes (fail closed).
- `python -m pytest` passed offline (445 passed). `config/risk.toml` unchanged (committed sha256 0aea23af…bc04), `live_enabled=false`.
- Queue empty. No next todo. `WASH_WINDOW_S` = 300 s awaits Operator review. A single-leg fill is directional exposure, not locked profit.
