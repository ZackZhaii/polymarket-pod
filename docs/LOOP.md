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
Task letters: the next letter unused on `dev`, `main` and every open pull request. After Z come AA, AB, AC, … (after AZ comes BA, and so on).
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

## Task T — Task R follow-ups: book age by the exchange book_ts; stale markets emit no gap; latest row by parsed time

Status: done.

Approved by Poly指挥, done by grok-bot. After Task R, `paper_cycle` and `paper_ledger.realistic_no_edge` still judged
book age by the recorder's `ts`, a stale market still emitted a gap signal, and "latest row" compared timestamp strings.

Finish when book age is read from the exchange's `book_ts`, stale markets emit no gap signal, latest rows are chosen by
parsed time, and `python -m pytest` passes with no network and no credentials.

- `src/paper_cycle.py`: `_row_book_problem(row, ...)` reads the row's `book_ts` (from `book.book_timestamp`), never `ts`. No
  `book_ts` is `missing_book_ts`; a non-null `book_ts_problem` is always stale. `STALE_AFTER_S` stays 120 (exactly 120 s is fresh).
- A stale market emits no ArbBinary/NegRiskArb gap signal. Its BookWatcher stale signal is the skip record (`gap_signal=skipped`,
  `skip_reason=stale_book`), and `run()` returns `skipped` / `skips`.
- `book_staleness.latest_by_time` picks the latest market/book row by parsed recorder `ts`, not string order (ties keep the later
  row). A missing or unparseable `ts` row is skipped as `missing_row_ts` / `invalid_row_ts` and recorded (BookWatcher
  `skipped_row` signal plus `skips`). The newest recording wins even without a `book_ts`, so the leg turns stale (fail closed).
- `paper_ledger.realistic_no_edge`: same selection, newest first; passes `book_ts` (not `ts`) to RiskGate; returns `skipped_rows`.
- Historical "Forecaster" mentions in docs stay as they are. No threshold change. Do not edit `config/risk.toml`.

## Task U — non-finite numbers (NaN, +inf, -inf) veto; fee_rate=NaN can no longer skip the edge check

Status: done.

Approved by Poly指挥 (security fix, ahead of Endgame #10), done by grok-bot. `NaN` compares False with everything, so
`fee_rate=NaN` made the edge `NaN`, `edge <= 0` was False and `non_positive_edge` never fired.

Finish when every number in RiskGate's edge math and in paper_ledger's fee/position math must be finite, a non-finite
value vetoes (fail closed), and `python -m pytest` passes with no network and no credentials.

- `src/risk_gate.py`: `model_prob`, `market_price`, `assumed_slippage` and `fee_rate` must each be a finite number. NaN/inf
  vetoes `non_finite_<field>`; a non-number (string, None, bool) vetoes `invalid_<field>` instead of crashing. Then the edge
  is forced to -1.0 (`non_positive_edge`), and `fee_usd` / `shares` in the decision stay finite. The old range checks run unchanged.
- A non-finite or non-numeric `max_order_pct` vetoes `invalid_max_order_pct` with zero notional (it was a NaN notional that
  skipped `zero_notional`). The 1% clip and `PINS` are unchanged.
- `src/paper_ledger.py`: `load_state` returns an invalid risk state (`open_positions` None, so `invalid_risk_state`) when a
  counted row has a non-finite or non-numeric `shares`, `notional_usd` or `fee_usd`; a NaN row can no longer hide an open
  leg (`abs(nan) > eps` is False). Absent values keep their old meaning; hypothetical rows are still ignored.
- `src/maker.py`: `quote` raises on a non-finite price or size. `src/sports_devig.py`: non-finite odds, price or slippage
  emit nothing (NaN odds or slippage emitted a NaN edge). Forecaster, quant, endgame and gaps already reject NaN/inf.
- Tests: `tests/test_non_finite_params.py`, NaN, +inf and -inf for each parameter (fee_rate NaN and inf included).
- No threshold change. Do not edit `config/risk.toml`.

## Task V — skill security auditor: resolved-path containment; outside entries skipped and reported; errors are FAIL

Status: done.

Approved by Poly指挥 (security fix, ahead of Endgame #10), done by grok-bot. The auditor checked containment with
`str(target).startswith(str(root))`, so a symlink to a sibling such as `target-evil/.env` counted as inside `target`.
Files behind outside links were still read, and read, stat and decode errors were swallowed, so the verdict could stay PASS.

Finish when containment uses resolved paths, outside entries are never read, any error is FAIL, and `python -m pytest`
passes with no network and no credentials.

- `.grok/skills/cx-skill-security-auditor/scripts/skill_security_auditor.py`: `is_within` resolves both paths (symlinks
  included) and compares whole components with `os.path.commonpath` (normcased); another drive or an unresolvable path is outside.
- `collect_items` walks once without following directory links. An entry that resolves outside the target (symlink, or
  junction on Windows) is never read or descended into, is listed under `skipped` and is a CRITICAL finding
  (`FS-SYMLINK`, or `FS-OUTSIDE`). Every scan step (filesystem, code, prompt injection, dependencies) uses that list.
- `read_text_checked` reads bytes and decodes strict UTF-8. Any read, decode, stat or walk error is listed under `errors`
  (JSON `errors`, `summary.errors`) and makes the verdict FAIL (exit 1). Nothing is swallowed into a PASS.
- Tests: `tests/test_skill_auditor_containment.py` covers a prefix sibling, symlink escapes (skipped cleanly where symlinks are
  unavailable), read/decode/stat errors that turn into FAIL, and CLI exit codes. All 39 existing skills keep identical findings and verdicts.
- The deny list, `.grok/config.toml` and CI are unchanged. No threshold change. Do not edit `config/risk.toml`.

## Task W — Endgame unfiltered path: a read-only, fail-closed second scan for the 0.98–0.99 band (backlog #10)

Status: done.

Approved by Poly指挥, done by grok-bot. The scanner drops prices above `scanner_max_price` (0.97) as `long_tail_price`,
so markets in Endgame's 0.98–0.99 band never reached Endgame (COORDINATION backlog #10).

Finish when Endgame gets its band through a separate read-only path, the scanner is unchanged, nothing bypasses RiskGate,
and `python -m pytest` passes with no network and no credentials.

- `src/endgame_scan.py` (`python src/endgame_scan.py`): fetches the same public Gamma keyset pages through
  `scanner.fetch_all_markets` (GET only, no keys). It applies every scanner rule except `long_tail_price`: closed, version,
  24 h expiry, accepting orders, bid/ask, liquidity, spread and price.
- Any non-finite price, bid, ask, spread or liquidity fails closed (`non_finite_<field>`). Only first-outcome prices inside
  `endgame.PRICE_MIN..PRICE_MAX` (inclusive) stay; others are `outside_endgame_band`.
- Candidates go to `endgame.emit`: a signal to RiskGate only, within Endgame's 48 h window. With the scanner's 24 h minimum
  this leaves 24–48 h to resolution. There is no fill, order or ledger write, and RiskGate still decides every intent.
- Fail closed: drifted paper pins exit 4 before any fetch; an incomplete fetch exits 3 and writes nothing; fewer than 5 markets exit 1.
  No band market is exit 0 with no signal.
- The scanner is untouched: default output, filters, exit codes and `data/markets.jsonl` are unchanged. Backlog #10 is struck through.
- Since Task Y the shared `scanner.rejection_reasons` gives `non_finite_<field>` first; `band_problems` re-checks with the same
  names as a second fail-closed guard and never counts a reason twice (`tests/test_endgame_scan.py`).
- No threshold change. Do not edit `config/risk.toml`.

## Task X — RiskGate minimum edge: MIN_EDGE = 0.02 (code constant), edge_below_min veto (backlog #6)

Status: done.

Approved by Poly指挥, done by grok-bot. RiskGate only required edge > 0. `risk.toml` has no `min_edge`; the 0.02 bar was the
Forecaster's temporary one (COORDINATION #6, note added in 4568f05; no code in the history ever enforced it).

Finish when an edge below 0.02 vetoes, `non_positive_edge` stays, `risk.toml` is unchanged, and `python -m pytest`
passes with no network and no credentials.

- `src/risk_gate.py`: `MIN_EDGE = 0.02` is a code constant, not a `risk.toml` key. It is a threshold proposal for the user to decide
  at M2 (like `MIN_HOURS_TO_EXPIRY` and `WASH_WINDOW_S`). A config `min_edge` cannot lower it.
- `0 < edge < MIN_EDGE` vetoes `edge_below_min`. `edge <= 0` still vetoes `non_positive_edge` only (not doubled).
- 0.02 is inclusive: an edge of exactly 0.02 passes. `EDGE_TOLERANCE = 1e-12` only absorbs float rounding
  (0.425 − 0.405 = 0.01999…96). The edge is after slippage and fees.
- Non-finite or missing inputs fail closed as in Task U (`non_finite_<field>` / `invalid_<field>`, edge −1.0).
- Tests: `tests/test_min_edge.py` covers 0.0199 / 0.02 / 0.05 on buy and sell, fees counted, NaN/inf/None, and that config cannot lower it.
  A trace of every `evaluate` call in the suite found no existing fixture with `0 < edge < 0.02`, so no fixture changed.
- Do not edit `config/risk.toml`.

## Task Y — scanner rejects non-finite numbers (NaN, +inf, -inf) as `non_finite_<field>`

Status: done.

Approved by Poly指挥, done by grok-bot. `as_float("NaN")` returns `nan`, and every comparison with NaN is False, so a market
with a NaN spread, liquidity, bid or ask slipped past `wide_spread`, `low_liquidity` and `missing_bid_ask` and passed the scanner.

Finish when every non-finite parsed number rejects the market, exit codes and the output format are unchanged apart from the
new reasons, `risk.toml` is unchanged, and `python -m pytest` passes with no network and no credentials.

- `src/scanner.py`: `non_finite_reasons(row)` checks `best_bid`, `best_ask`, `spread`, `liquidity_usd`, `volume_usd`,
  `volume_24h_usd`, every `outcome_prices` element and `price`. Each NaN or ±inf gives `non_finite_<field>`
  (`NON_FINITE_FIELDS`, all in `REASONS`).
- Ranking: after the structural reasons (`closed` … `not_accepting_orders`), before `missing_bid_ask`, `low_liquidity`,
  `wide_spread`, `missing_price` and `long_tail_price`, so `first_reason` names the bad number, not a filter it fooled.
- `yes_price`: a non-finite `lastTradePrice` is returned (and rejected as `non_finite_price`) instead of silently
  falling through to `outcomePrices`. Finite markets get exactly the same price and reasons as before.
- No new row keys: `data/markets.jsonl` rows, the summary line and exit codes 0/1/2/3 are unchanged; only rows that
  were wrongly passing are now rejected.
- `src/endgame_scan.py` (PR #25) calls `scanner.rejection_reasons`, so it gets the same `non_finite_*` names; its own
  band check uses the same names and dedups. Checked with #25's code on top of Y: all tests pass.
- Tests: `tests/test_scanner_non_finite.py` covers each field with `'NaN'`, `'nan'`, `'inf'`, `'-Infinity'` and
  `float('nan')`, the reason order, finite bad values keeping their old reasons, and `main()` output and exit codes.
- Do not edit `config/risk.toml`. No new threshold.

## Task Z — Sentinel stale-book alarm: 120 s per check, paper halt after 2 consecutive stale or failed checks (backlog #9)

Status: done.

Approved by Poly指挥, done by grok-bot. Sentinel proposed a 120 s staleness limit and an alarm only after 2 consecutive
failures (30 s gave false alarms; COORDINATION #9). Before this task `src/sentinel.py` only had a manual `halt`; nothing
raised a Sentinel alarm automatically, so adding a 2-in-a-row alarm loosens nothing.

Finish when Sentinel halts after 2 consecutive stale or failed checks, every per-check block is unchanged, `risk.toml`
is unchanged, and `python -m pytest` passes with no network and no credentials.

- `src/sentinel.py`: `SENTINEL_STALE_S = 120` (equal to RiskGate's `MAX_BOOK_AGE_S`) and `SENTINEL_STALE_CONSECUTIVE = 2`
  are code constants, not `risk.toml` keys. They are threshold proposals for the user at M2. A caller may only make them stricter.
- `StaleWatch.check(market, book_ts, now, failed=False)`: stale is any `book_age_problem` reason (missing, unparseable,
  non-finite or future `book_ts`), a `now` that is not an aware datetime, or `failed=True` (fail closed). Exactly 120 s is fresh.
- At 2 stale checks in a row for a market it calls `sentinel.halt` once (that market's paper quotes cancelled,
  `exchange_action=none`, reason `stale_book_consecutive: ...`). Later stale checks do not halt again; a fresh check resets.
- Per-check blocks are untouched: RiskGate still vetoes a single stale book, and `paper_cycle` still sends no gap signal and
  the BookWatcher stale signal for every stale check. `StaleWatch` was not wired into `paper_cycle` here (Task AA wires it).
- `to_dict` / `from_dict` keep the streak between runs; a malformed state raises instead of quietly resetting.
- Tests: `tests/test_sentinel_stale.py` (one stale no alarm, two halt once, reset, per market, missing/NaN/inf/naive/future
  stamps, failed checks, the 120 s boundary, RiskGate still vetoing one stale book, stricter-only limits, state round trip).
- Do not edit `config/risk.toml`.

## Task AA — Sentinel StaleWatch wired into paper_cycle; the streak survives between runs (backlog #15)

Status: done.

Approved by Poly指挥, done by grok-bot. Task Z added `StaleWatch` but nothing fed it, so the 2-in-a-row counter lived only in
memory and never reached 2 across separate `paper_cycle` runs. This task wires it in and keeps the counter in a file.
Every per-check block is unchanged, and every new path fails closed, so nothing is loosened.

Finish when two consecutive stale `paper_cycle` runs halt that market's paper quotes, every state-file problem counts as stale,
`risk.toml` is unchanged, and `python -m pytest` passes with no network and no credentials.

- State file: `logs/sentinel_stale.json` (`podlib.SENTINEL_STATE_PATH`; `logs/` is gitignored). `paper_cycle.run` takes
  `sentinel_state_path` and `quotes_path` so tests never touch the real files (`tests/conftest.py` isolates them for every test).
- Each run checks every market with at least one booked token once, at Sentinel's own `SENTINEL_STALE_S` (120 s). A caller's
  looser `stale_after_s` keeps its per-check meaning but cannot reset the Sentinel streak. The market is stale if any booked token is.
- Fail closed: an unreadable file (`state_unreadable`), bad JSON (`state_corrupt`), a wrong shape, NaN or looser limits
  (`state_malformed`), or a file deleted after a prior run (`state_missing`, detected by the `.seen` marker next to it) counts
  as one stale check for every checked market before its own check. A failed save (`state_write_failed`) counts as one more after it.
  A first run (no file, no marker) starts at 0. Deleting both the file and the marker looks like a first run (documented limit).
- Writes are atomic: a temp file in the same folder, flush and fsync, then `os.replace`; a failed write leaves the old file
  and no temp file behind.
- At 2 in a row `sentinel.halt` cancels only that market's paper quotes (`exchange_action=none`), once per streak; a fresh check resets.
  The BookWatcher stale signal, the skipped gap signal and RiskGate's single-stale veto are unchanged.
- The result has a `sentinel` block: `checked`, `state_problem`, `write_problem`, `halts`, `state_path`.
- Task letters after Z: AA, AB, AC, … (`AGENTS.md`, `docs/COORDINATION.md`, this file).
- Tests: `tests/test_stalewatch_paper_cycle.py` (first run, two runs halt, reset, unbooked markets, corrupt/malformed/NaN/looser
  state, invalid UTF-8, missing after a prior run, unreadable path, write failure, atomic replace, looser `stale_after_s`,
  missing or future `book_ts`, pin drift touches no state).
- Do not edit `config/risk.toml`.

## Task AB — paper_session uses the current scanner path; book age from the CLOB timestamp at an evaluation clock (backlog #16)

Status: done.

Approved by Poly指挥, done by grok-bot. astra's review found that `paper_session` still used the legacy single unfiltered
Gamma page (`fetch_markets` + `visible_rows`: no version, non-finite, expiry, spread or liquidity rules) and judged book age
at the session start stamp, taken in whole seconds before any book was read, so a fresh book aged about 0 s or looked future.

Finish when the session only persists and books markets that pass the scanner, every scanner fail-closed exit writes nothing,
book age comes from the CLOB `book_ts` at a clock read after the books, `risk.toml` is unchanged, and `python -m pytest`
passes with no network and no credentials.

- `src/paper_session.py` calls `scanner.scan` with `scanner.thresholds_from(cfg)`: keyset paging with the
  `liquidity_num_min` pre-filter, version-aware outcome IDs (`positionIds` for v2), `non_finite_*`, expiry, spread,
  liquidity and long-tail rules. Only passing rows go to `data/markets.jsonl` (bus signals for the top `BUS_TOP_N`).
- Fail closed, nothing written: a bad threshold (`invalid_scanner_threshold`, before any read), a failed read
  (`public_read_failed`), incomplete paging (`markets_incomplete`), fewer than 5 scanned (`too_few_markets`, the scanner's
  own floor) or nothing passing (`no_markets`).
- Books: the first `book_limit` (5) passing markets that have outcome IDs; `book.record` keeps the recorder `ts` and the
  CLOB `book_ts` apart as before.
- `paper_cycle` gets `now` from `clock()` (default `eval_clock()`, UTC with milliseconds), read once after the books are
  recorded. The session stamp still stamps rows. Missing, unparseable or future `book_ts`, or a broken clock, stay stale;
  exactly 120 s is fresh. The result and `logs/sessions.jsonl` add `scanned` and `evaluated_at`.
- A local clock running behind the exchange makes fresh books look future and therefore stale (fail closed, no tolerance added).
- Tests: `tests/test_paper_session_scanner_path.py` (scanner rejections never persisted or booked, NaN/inf fields, v2
  `positionIds`, unknown version, too few, incomplete paging, the liquidity pre-filter, bad thresholds, clock read after the
  books, 120 s boundary, stale, missing, future and unparseable stamps, broken clock). `tests/test_paper_session.py`
  keeps every assertion; its fixture adds four scanner-rejected markets for the 5-market floor and a clock.
- Do not edit `config/risk.toml`.

## Task AC — one module for the risk and threshold code constants; docs/THRESHOLDS.md (backlog #17)

Status: done.

Approved by Poly指挥, done by grok-bot. The threshold code constants were spread over a dozen modules with no single list,
so a value could drift from its twin or from the docs. This task gathers them; no value and no behaviour changes.

Finish when every risk and threshold code constant lives in `src/thresholds.py`, the old names still work, `docs/THRESHOLDS.md`
lists every threshold and a test keeps both in sync, `risk.toml` is unchanged, and `python -m pytest` passes with no network
and no credentials.

- `src/thresholds.py` (imports nothing from `src`): `MAX_BOOK_AGE_S`, `MIN_EDGE`, `EDGE_TOLERANCE`, `PAPER_EQUITY_USD`,
  `PAPER_CYCLE_STALE_AFTER_S`, `SENTINEL_STALE_S`, `SENTINEL_STALE_CONSECUTIVE`, `WASH_WINDOW_S`, `MIN_HOURS_TO_EXPIRY`,
  `SCANNER_MIN_PRICE`, `SCANNER_MAX_PRICE`, `MIN_SCANNED_MARKETS`, `ENDGAME_PRICE_MIN`, `ENDGAME_PRICE_MAX`,
  `ENDGAME_WINDOW_HOURS`, `SPORTS_DEVIG_ASSUMED_SLIPPAGE`, `LEDGER_OPEN_EPS`, `REFLECTOR_MAX_PER_UTC_DAY`.
- Modules import them and keep their old names as the same objects (`risk_gate.MIN_EDGE`, `paper_cycle.STALE_AFTER_S`,
  `endgame.PRICE_MIN`, `paper_session.SESSION_MIN_SCANNED`, ...). The literal `5` in `scanner.main` and `endgame_scan.main`
  became `MIN_SCANNED_MARKETS` (same messages, same exit codes).
- Import-time relations (explicit `RuntimeError`): `SENTINEL_STALE_S == MAX_BOOK_AGE_S`, `PAPER_CYCLE_STALE_AFTER_S == MAX_BOOK_AGE_S`,
  the scanner band, the Endgame band above it, and a non-empty 24–48 h Endgame window. `src/sentinel.py` keeps its own guard.
- `docs/THRESHOLDS.md`: one table of code constants (value, unit, meaning, where, source task, M2 pending), the operational
  limits that stay in their modules, and the `risk.toml` values read-only (governed by the file and its sha).
- Tests: `tests/test_thresholds.py` (frozen values, every old name, doc rows vs module and re-exports, M2 column,
  operational rows, risk.toml rows vs the file, each relation firing on a bad edit, no second literal definition in `src`).
- Do not edit `config/risk.toml`; its parameters are not moved into code. No threshold value changes.

## Paste this into a new chat

```text
你是 polymarket-pod 的纸面循环入口。先读 AGENTS.md、docs/LINK.md、docs/LOOP.md、docs/loop/state.json、config/risk.toml。
bot 先 git fetch origin，在 .worktrees/<名字> 里从 origin/dev 开 worktree（只有 Operator 本机 Grok Build 在主目录 git pull），然后只领取第一件 todo。做完释放，开 PR（base=dev），不要推 main 或 dev，不要自己合并，然后停止。
禁止读 .env 和任何密钥。禁止改 live_enabled。禁止下单、提现、自动加仓。
禁止对敲、自成交、刷量、幌骗。单边成交不是无风险套利。不承诺收益。
```

## Task AD — endgame_scan also selects the second outcome (backlog #18)

Status: done.

Approved by Poly指挥, done by grok-bot. `endgame_scan` only checked the first outcome against Endgame's band, so a market
whose second outcome sat at 0.98–0.99 never reached Endgame (COORDINATION backlog #18).

Finish when a market is selected if exactly one of its two outcomes is in the band, signals name the outcome and token,
nothing else is loosened, and `python -m pytest` passes with no network and no credentials.

- `endgame_scan.band_decision` returns (problems, selection). Outcome 0 is `scanner.yes_price` (unchanged), outcome 1 is
  Gamma's `outcomePrices[1]`. The band is `ENDGAME_PRICE_MIN..ENDGAME_PRICE_MAX` from `src/thresholds.py` (inclusive); no new numbers.
- Both prices are needed for the decision: missing is `missing_price`, non-finite is `non_finite_price` /
  `non_finite_outcome_prices` (existing names, counted once). Both in the band is `both_outcomes_in_band`; one in the band and
  the other above it is `other_outcome_above_band`. Neither is picked.
- The selected outcome needs its own token id (version-aware `scanner.select_token_ids`), else the scanner's token problem
  name (`missing_token_ids`, `invalid_token_ids`, `token_count_mismatch`).
- Rows carry `endgame_outcome_index`, `endgame_outcome`, `endgame_token_id` and `endgame_price`; the result has `selected_outcomes`.
  `endgame.emit` adds `outcome_index`, `outcome` and `token_id` to the RiskGate signal when given (payload unchanged otherwise)
  and emits nothing for a selection without a token id.
- Same scanner rules, 24–48 h window, `MIN_SCANNED_MARKETS` and exit codes. Tightening: first-outcome rows now also need a
  finite second price and their own token id.
- Tests: `tests/test_endgame_second_outcome.py`. `tests/test_endgame_scan.py` fixtures now carry Gamma's `outcomes`,
  `outcomePrices` and `clobTokenIds`; no assertion changed.

## Task AE — one fee module: stress_fee and ledger_fee (backlog #19)

Status: done.

Approved by Poly指挥, done by grok-bot. RiskGate charged fees twice with two price bases: the edge stress used
`fee_rate * market_price` per share, the booked fee (`Decision.fee_usd`, written by paper_ledger) used `notional_usd * fee_rate`,
i.e. the effective (fill) price. A sell was booked cheaper than it was stressed (COORDINATION backlog #19).

Finish when one module computes every fee, no fee is lower than before on any input, invalid inputs still fail closed,
and `python -m pytest` passes with no network and no credentials.

- `src/fees.py` (pure, imports nothing from `src`): `fee_per_share = max(fee_rate * market_price, fee_rate * effective_price, 0)`.
  `stress_fee` is the RiskGate edge stress (USD per share); `ledger_fee = max(notional_usd * fee_rate, shares * fee_per_share)`
  is the booked `Decision.fee_usd`; `booked_fee_usd` is paper_ledger's 8-decimal rounding (unchanged). `risk_gate` and
  `paper_ledger` do no fee arithmetic of their own (a test walks their AST).
- `tests/test_fee_characterisation.py` recorded the old fees (the first commit asserts equality with the old code) and now
  asserts on a 24,624-point grid (prices near 0 and 1 and invalid, sizes, fee rates, slippages, buy/sell/other) that every
  fee is >= its old value, every edge <= its old value, and nothing newly allows.
- More conservative: a buy's edge is lower by `fee_rate * assumed_slippage` (0.43 / 0.40 / 0.005 / 0.02: 0.017 -> 0.0169;
  model 0.433: 0.020 passed, now 0.0199 `edge_below_min`); a sell's booked fee is higher by `market / effective`
  (10 USD at 0.40, slippage 0.005, fee 0.02: 0.2 -> 0.20253165), so `daily_loss_usd` grows faster. A negative fee_rate
  (still `negative_cost_assumption`) is no longer a negative fee. Buy booked fees and sell edges are unchanged.
- Fail closed as Task U: a non-finite or non-number fee_rate or price gives edge -1 and fee 0.0 in a vetoed decision;
  `invalid_fee` backstops a clean intent whose fee cannot be computed; a non-finite ledger `fee_usd` is still invalid state.
- No new threshold: fee_rate comes from the intent; there is no fee constant in code and no fee key in `config/risk.toml`.
  `docs/THRESHOLDS.md` is unchanged. Tests: `tests/test_fee_characterisation.py`, `tests/test_fees.py`.

## Task AF — paper ledger integrity: fill ids, atomic writes, conservation check (backlog #20)

Status: done.

Approved by Poly指挥, done by grok-bot. The paper ledger could count a re-appended fill twice, a crash mid-append left a torn
line that `read_jsonl` silently skipped, and nothing checked that cash and positions still added up (COORDINATION backlog #20).

Finish when fills are deduped by id, writes are atomic, cash / positions / cost basis are checked on every load and write,
any mismatch halts booking, existing ledgers load exactly as before, and `python -m pytest` passes with no network.

- `src/ledger_store.py` (standard library only; imports `src.fees` and `src.thresholds`). paper_ledger reads the ledger
  strictly through it: a malformed, non-object, undecodable or unterminated (torn) line is an integrity problem, not skipped.
- (a) Ids: new fills carry `fill_id = uuid4().hex`. Same id + identical row (canonical JSON): ignored idempotently, counted once
  (`duplicate_fill_rows`). Same id + different row: `conflicting_fill_id`. Bad id: `invalid_fill_id`. The old schema had no ids,
  so id-less rows before the first id'd row are legacy rows with the derived id `legacy:<line>:<sha256(canonical JSON)>`;
  the line number keeps two identical legacy rows two fills (no exposure hidden). An id-less row after that: `missing_fill_id`.
- (b) Atomic write: old bytes + new line go to a temp file in the same directory, flush + fsync, a check that the ledger did
  not change meanwhile (`ledger_changed_during_write`), `os.replace`, then a directory fsync on POSIX. Any failure removes the
  temp file and leaves the old ledger byte-for-byte intact; tests simulate a half write, fsync and replace errors, an
  interrupt, a concurrent writer and a stale temp file. `commit_if_allowed` returns allow False with `ledger_write_failed`.
- (c) Conservation: replay counted rows from `PAPER_EQUITY_USD`: cash = start - buys + sells - fees; per leg shares and cost
  basis (buy notional - sell notional; no notional = 1 USD per share, as `leg_positions`). Every id'd row stores
  `cash_after_usd`, `leg_shares_after`, `leg_cost_basis_after_usd`; each must match within `LEDGER_OPEN_EPS`, and `fee_usd` must
  be >= `booked_fee_usd(ledger_fee(...))` recomputed from the row (the booked fee takes the higher of that and the decision's
  fee: +1e-8 USD on about 0.14% of fills, never lower). Checked on load, on the candidate before the write and after the replace.
- Fail closed: any problem gives the Task U invalid state plus `ledger_problems`, so RiskGate vetoes `invalid_risk_state`;
  `commit_if_allowed` also re-verifies inside `book` and on a problem emits an Executor -> Sentinel `halt` (no exchange
  action), returns allow False and books nothing. The fill bus message is sent only after a verified write.
- Legacy ledgers: `tests/fixtures/ledger/*.jsonl` (rows without notional or token, string numbers, NaN hypothetical rows,
  identical repeats, the pre-AF commit row shape) load to exactly the values the pre-AF code returned, with LF and CRLF.
- No new threshold: `LEDGER_OPEN_EPS` and `PAPER_EQUITY_USD` are reused (THRESHOLDS.md notes the reuse). `config/risk.toml`
  unchanged. Tests: `tests/test_ledger_integrity.py`.

## Latest

- 2026-10-09: Task AF done by grok-bot (approved by Poly指挥, backlog #20). The paper ledger is checked by `src/ledger_store.py` (standard library only).
- Fills carry `fill_id`: identical repeats count once, a changed row under the same id or an id-less row after id'd rows is invalid state.
- Writes are atomic (temp file, fsync, os.replace); cash, leg shares and cost basis are conserved within LEDGER_OPEN_EPS or booking halts.
- `python -m pytest` passed offline (1141 passed; on Windows 1137 passed, 4 symlink tests skipped). `config/risk.toml` unchanged (committed sha256 0aea23af…bc04), `live_enabled=false`.
- Queue empty. M2 pending: MAX_BOOK_AGE_S, MIN_EDGE, SENTINEL_STALE_S, SENTINEL_STALE_CONSECUTIVE, WASH_WINDOW_S, MIN_HOURS_TO_EXPIRY.
