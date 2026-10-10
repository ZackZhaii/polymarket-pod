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

## Task AG — read-only WebSocket order-book recorder, public market channel (backlog #21)

Status: done.

Approved by Poly指挥, done by grok-bot. `book.py` only polls REST, so the book can move between polls; M1 needs a live,
read-only recorder that never keeps writing a book it can no longer vouch for (COORDINATION backlog #21).

Finish when a pure core turns market-channel messages into per-asset books with exchange timestamps, any gap or
inconsistency writes one price-less invalidation row and forces a fresh snapshot before priced rows resume, the rows feed `book_staleness` / `paper_cycle` / `StaleWatch`
unchanged, the dependency is optional and fenced by (a)-(d), and `python -m pytest` passes with no network.

- `src/ws_recorder.py`. Sources (in the module docstring): docs.polymarket.com market channel and WSS overview pages.
  URL `wss://ws-subscriptions-clob.polymarket.com/ws/market`; subscribe `{"assets_ids": [...], "type": "market",
  "initial_dump": true}`; text `PING` every `WS_PING_INTERVAL_S`; events book, price_change, tick_size_change,
  last_trade_price (others are counted and ignored). The initial dump arrives as a JSON list of book events.
- Core (`BookCore`, no clock, no I/O): book snapshot replaces the asset's book; price_change deltas (size "0" removes)
  apply to copies per asset, so nothing is half-applied. `book_ts` = exchange ms timestamp via `book.book_timestamp`.
  No sequence numbers exist and the hash algorithm is undocumented (the hash is recorded, not verified), so gaps are
  detected from the documented fields: invalid/non-finite price or size, timestamp regression, crossed book, removal of an
  unknown level, reported best_bid/best_ask differing from the local book, tick or market mismatch, malformed message.
- Resync: the asset's state is discarded, a live asset gets one invalidation row (no prices, `book_ts_problem=invalid_book_ts`,
  `ws_state=resync`, so consumers fail closed at once: a tightening, approved by Poly指挥), and after that row nothing
  more is written for the asset (no priced row, no second marker) until a fresh snapshot.
  The runner unsubscribes + resubscribes that asset; no snapshot within `WS_RESYNC_TIMEOUT_S` means reconnect. Any
  disconnect invalidates every asset; reconnects back off `min(WS_BACKOFF_CAP_S, WS_BACKOFF_BASE_S * 2**n)`.
- Rows: a superset of `book.snapshot` (plus `ws_event`, `ws_state`, `book_hash`), written on a snapshot, a top-of-book
  change or every `WS_ROW_REFRESH_S` of exchange time. Append-safe: O_APPEND, one write per line, a torn last line is
  sealed on open, NaN refused. market_id / question / outcome come from `data/markets.jsonl`; no bus messages.
- Safety (Poly指挥: websockets is not a trading dependency if all hold): (a) `websockets==17.2.0` only in optional
  `requirements-ws.txt`, imported lazily inside the adapter; (b) `check_market_url` before any connect: exact wss,
  host and path; (c) no headers, `check_outgoing_frame` allows only PING and public market (un)subscribe frames;
  (d) tests scan requirements files and src imports for order-capable packages.
- CLI `python -m src.ws_recorder --assets ID [ID ...] --max-runtime S [--out] [--markets]`: exit 0 rows written,
  1 no snapshot, 2 usage, 3 websockets missing, 4 write failed. New constants: `WS_BACKOFF_BASE_S`, `WS_BACKOFF_CAP_S`,
  `WS_RESYNC_TIMEOUT_S`, `WS_PING_INTERVAL_S` (operational) and `WS_ROW_REFRESH_S` (staleness, M2 pending).
  `config/risk.toml` unchanged. Tests: `tests/test_ws_recorder.py` (fake transport only).

## Task AH — local M0 guard script: scripts/guard.py (backlog #22)

Status: done.

Approved by Poly指挥, done by grok-bot. The M0 safety rules (risk.toml pin, `live_enabled=false`, no order-capable
package, public read-only URLs only, THRESHOLDS.md in sync) were spread over many tests; there was no one command to run
before and after a change (COORDINATION backlog #22).

Finish when `python scripts/guard.py` runs offline from any cwd inside the repo, prints one PASS/FAIL line per check with
the reason, exits 0 only if all pass, each check fails on a deliberately bad temp copy, and the real repo passes.

- `scripts/guard.py`, standard library only (plus the repo's own `src.podlib.parse_risk_toml` and `src.thresholds`, so
  risk.toml and the constants are read exactly as the app reads them). Read-only: no writes, no bytecode, no network.
- Checks: `risk_sha_worktree` (CRLF normalised) and `risk_sha_head` (`git show HEAD:config/risk.toml`; SKIP without git
  or outside a checkout, FAIL with `--require-git`); `live_enabled` (the bool false, plus the other `podlib.PINS`);
  `banned_dependencies` (requirements*.txt, pyproject.toml); `banned_imports` (src/ and scripts/, including literal
  `importlib.import_module` / `__import__`); `url_allowlist` (src/); `thresholds_doc`.
- Single sources: `ORDER_CAPABLE` (Task AG's list plus the relayer and EIP-712 signing helpers) and the Task AC table
  parser (`markdown_tables`) live in the guard; `tests/test_ws_recorder.py` and `tests/test_thresholds.py` use them.
- URL allowlist, built from what `src/` uses today: `https://gamma-api.polymarket.com/markets`, `/markets/keyset`,
  `https://clob.polymarket.com/book`, `wss://ws-subscriptions-clob.polymarket.com/ws/market`. Order/auth paths, the user
  channel, relayer hosts, ports, user info, credentials in the query, host-less URL pieces and other hosts fail.
  Docstrings and comments may also link docs.polymarket.com and github.com. The Data API is not used, so not allowed.
- `thresholds_doc` mirrors Task AC: every constant once with value and type, the relations, documented re-exports (also
  through podlib) and every renamed import documented, operational rows, risk.toml rows, the pinned sha in the doc.
- Exit codes: 0 all pass (`risk_sha_head` may SKIP), 1 a check failed, 2 usage error or no repo root. How to run:
  `docs/PHASE0_RUN.md` (Guard). No CI config added: CI stays with the user. Tests: `tests/test_guard.py`.
- Also (Poly指挥): AG's doc sentences (`src/ws_recorder.py` docstring, this file, THRESHOLDS.md, state.json) now say a
  live asset that loses sync gets one price-less invalidation row, then nothing until a fresh snapshot.

## Task AI — M1 24-hour acceptance tooling: src/m1_run.py, src/m1_report.py (backlog #23)

Status: done.

Requested by Poly指挥, done by grok-bot. M1 is accepted on a 24-hour read-only run of the Task AG recorder on the box;
there was no long-run entry (rotation, graceful stop, provenance) and no way to judge a run (COORDINATION backlog #23).

Finish when `python -m src.m1_run --out RUN_DIR --max-runtime 86400` records books, events and REST samples with a run
manifest and stops cleanly on SIGINT/SIGTERM, `python -m src.m1_report RUN_DIR` turns a run dir into report.md/report.json
with a PASS/FAIL verdict, the criteria are M2-pending constants, and `python -m pytest` passes with no network.

- `src/m1_run.py`: assets from `data/markets.jsonl` only (latest scan batch by parsed ts, 24h-volume order, version-aware
  `token_ids` without `token_ids_problem`, `--top` markets, both tokens; `--assets` overrides). It never scans Gamma.
  Writes `books/`, `events/`, `rest/` as `<stream>-NNNNNN.jsonl` through `JsonlAppender`, rotated by size (`--rotate-mb`)
  or age (`--rotate-s`). A daemon thread samples the public CLOB `/book` via `book.snapshot` at start and every
  `--rest-every` s for the first `--rest-assets` assets. `manifest.json` (atomic replace) at start and end: times (ms),
  elapsed, assets with metadata, selection, settings, thresholds, Python/websockets/schema versions, git sha + dirty,
  risk.toml sha256 and `live_enabled` at start and end with the pin, files, counts, exit code, stop signal.
- Exit codes: 0 ran to the deadline with a snapshot, 1 no snapshot, 2 usage (also a non-empty `--out`), 3 websockets
  missing (checked before anything is written), 4 write failed (books, events, REST or manifest), 5 SIGINT/SIGTERM.
- `src/ws_recorder.py`: `Recorder` gets two optional hooks. `stop` is polled at least every `WS_STOP_POLL_S` (also during
  backoff); a stop ends the run like the deadline (no invalidation rows). `on_event` receives connected, connect_failed,
  disconnected, backoff, resync (asset, reason), resubscribe, live and end. Without them nothing changes (AG tests as before).
- `src/m1_report.py`: offline, reads only the run dir. Per-asset coverage = share of 1 s points (`M1_REPORT_STEP_S`) where
  the latest WS row is a live book with `0 <= t - book_ts <= MAX_BOOK_AGE_S` (the `book_age_problem` rule, integer µs);
  book-age p50/p90/p99/max; resyncs by reason (gap versus `disconnect_*`), reconnects, connect failures; REST vs WS by
  nearest exchange `book_ts` within `M1_REST_MATCH_WINDOW_S`, best bid/ask equal at 6 decimals, mismatches listed; integrity
  (listed files present, no unreadable line, counts equal the manifest). Exit 0 PASS, 1 FAIL, 2 usage.
- Criteria (M2 pending, conservative): `M1_MIN_RUN_S` 86340, `M1_MIN_COVERAGE` 0.99 per asset, p99 age <= `MAX_BOOK_AGE_S`,
  `M1_MAX_RECONNECTS` 48, `M1_MAX_GAP_RESYNCS_PER_ASSET` 24, `M1_MIN_REST_MATCHED` 50, `M1_MAX_TOP_MISMATCH_RATE` 0.02,
  `M1_MAX_REST_UNMATCHED_RATE` 0.10, `M1_REST_MATCH_WINDOW_S` 30; plus completed run, risk pin at start and end, integrity.
  Operational: `M1_TOP_MARKETS` 5, `M1_ROTATE_BYTES` 64 MiB, `M1_ROTATE_S` 3600, `M1_REST_EVERY_S` 600, `M1_REST_ASSETS` 4,
  `M1_REPORT_STEP_S` 1, `WS_STOP_POLL_S` 1. Relations checked at import (THRESHOLDS.md).
- In `src/` (not `scripts/`): `python -m` from the repo root on every OS, and the guard's URL scan covers it. No new URL.
  Commands for the box: `docs/M1_ACCEPTANCE.md`. Tests: `tests/test_m1_run.py`, `tests/test_m1_report.py`.

## Task AJ — End-to-end offline hard gate: scripts/e2e_offline.py, tests/test_e2e_offline.py, guard check e2e (backlog #24)

Status: done.

Requested by Poly指挥, done by grok-bot. Every module had unit tests, but nothing ran the real pipeline end to end on
fixed data against a committed expected output, so loosening one stage could pass (COORDINATION backlog #24).

Finish when the real pipeline runs offline on `tests/fixtures/e2e/`, matches `golden.json` exactly, breaks no invariant,
the guard fails on any drift, golden changes only with `--update-golden`, and `python -m pytest` passes with no network.

- `scripts/e2e_offline.py`: scanner rows (`scanner.scan`, `persist_markets`) -> recorded CLOB `/book` bodies through
  `book.record`'s getter -> `paper_cycle.run` (gap/stale signals, Sentinel check 1) -> `endgame_scan` signals ->
  `forecaster.emit_probability` -> `build_intent` -> `paper_ledger.commit_if_allowed` (RiskGate with `fees.py`, MIN_EDGE,
  staleness, wash, non_finite, caps) -> paper fill -> `ledger_store.book` -> `verify` -> `paper_cycle.run` (check 2).
- Only the clock (frozen `datetime` in `podlib`, `book_staleness`, `wash_detect`; every `utc_now` follows) and I/O paths
  (temp bus/ledger/quotes/markets/books/Sentinel state, the getter, a concurrent-writer ledger path) are replaced.
  `socket`, `urllib`, `http.client`, `podlib.urlopen` are tripwires; a swallowed touch still fails.
- Scenarios: two allows, stale book, `NaN` ask, wash, edge 0.015, sixth leg, duplicate fill replay, ledger torn before
  and during the call, two consecutive stale checks (Sentinel halt, that market's quote cancelled).
- Invariants: no network, `exchange_action` none and no order flag on every bus message, fills only from an allow,
  unique fill ids, conservation, risk.toml at the pin and byte-identical, `live_enabled` false at every stage, no default
  data path written. Fixture clock 2026-09-01, so an unpatched clock read makes fresh books stale and breaks golden.
- Gate: `scripts/guard.py` check `e2e` (8 checks) loads the checked repo's own harness by path and runs it in-process
  (stdlib + src, ~0.1 s; no pytest, so it works in the M1 venv). `tests/test_e2e_offline.py` runs it plus one test per
  scenario/invariant. Golden: `python -m pytest tests/test_e2e_offline.py --update-golden` (off by default, tested;
  that run fails on purpose). Docs: `docs/PHASE0_RUN.md` "E2E offline gate", `tests/fixtures/e2e/README.md`.
- Not connected in src yet (signal -> Intent: Task AO, shadow): signal -> Intent (harness `build_intent`; fee_rate/slippage are fixture inputs), Endgame
  -> Intent (no model probability), halt consumers (nothing blocks later intents after a halt). No new threshold or URL.

## Task AK — Data API v2 reconciliation: read-only src/reconcile.py on /v2/positions and /v2/trades (backlog #25)

Status: done.

Requested by Poly指挥, done by grok-bot. Data API v1 retires on 2026-10-24 and the ROSTER Reconciler (Data API v2
positions / fills / redeem) did not exist, so any reconciliation would have been written against v1 or not at all
(COORDINATION backlog #25).

Finish when the read-only reconciliation path uses only Data API v2, every missing or malformed field fails closed,
only the exact v2 paths are allowlisted, and `python -m pytest` passes with no network.

- `src/reconcile.py` (stdlib): `GET /v2/status` first every round (age_seconds + serving.lag_seconds <=
  `DATA_API_MAX_LAG_S`, polygon/137, null fails), then `GET /v2/positions?user=W&status=OPEN&filter_type=TOKENS&filter_amount=0.1` and
  `GET /v2/trades?user=W&taker_only=false&start=1`, first page `limit=500`, then `cursor=` until `next_cursor` is null
  (trades re-send the same filters every page, per the v2 feed rule). Sources: docs.polymarket.com migration guide and
  Data API v2 overview, the v2 openapi.json schemas, Poly研究's research_dataapi_ws_m2.md, and a live public read (2026-10-09).
- Fail closed: envelope (`data` missing / null / not a list, `pagination` missing, `has_more` not bool or disagreeing
  with `next_cursor`, cursor loop, `MAX_PAGES` with a cursor left, any fetch error) and every used row field
  (positions: proxy_wallet, token_id, condition_id, current_size, status; trades: proxy_wallet, side, token_id,
  condition_id, size, price, timestamp, transaction_hash), plus the four position identities (current_value, unrealized,
  total_pnl, total_cost) within `DATA_API_IDENTITY_TOL_USDC`. No coercion: a string, bool, null, NaN or inf is malformed.
  Trades dedupe exact repeats on transaction_hash + token_id + side; differing rows under one key stay separate fills.
- Compares per token the `ledger_store` replay with the API: open shares vs `current_size`, buy / sell shares vs BUY /
  SELL trade sizes, within `RECONCILE_SHARES_TOL`; legs/fills below the API's 0.1 / 0.01-share floors are unverifiable
  and fail. Exit 0 / 1 mismatch / 2 usage / 3 Data API unusable / 4 ledger invalid; 1, 3, 4 write one Reconciler `halt`.
- `podlib.public_get` and guard `url_allowlist` admit exactly `/v2/status`, `/v2/positions`, `/v2/trades` (7 endpoints);
  v1 `/positions`, `/trades`, `/closed-positions`, `/v1/market-positions` and other v2 routes stay refused (tested).
- New numbers (M2 pending): `RECONCILE_SHARES_TOL = 1e-06`, `DATA_API_POSITIONS_DUST_SHARES = 0.1`,
  `DATA_API_TRADES_MIN_SHARES = 0.01` (the two floors are Data API contract facts), `DATA_API_MAX_LAG_S = 120.0`,
  `DATA_API_IDENTITY_TOL_USDC = 1e-06`. Operational: `reconcile.PAGE_LIMIT`,
  `reconcile.MAX_PAGES`. Docs: `docs/RECONCILE.md`, `docs/PHASE0_RUN.md`. Tests: `tests/test_reconcile.py`.
- Not on the paper pipeline or the e2e gate (unchanged): the Phase 0 ledger is paper, so a real wallet mismatches by
  design. Ambiguities resolved fail-closed (docs/RECONCILE.md). No key, no order, config/risk.toml unchanged.

## Task AL — Halt consumer: src/halt_store.py, resume bus type, global escalation, e2e halt scenarios, guard check halt (backlog #26)

Status: done.

Requested by Poly指挥 (ruling on Poly哨兵's halt/heartbeat design review of dev@f760e51), done by grok-bot. A halt
was a bus line nobody read: RiskGate did not check it, the Maker could re-quote, there was no resume, and
`invalid_risk_state` only vetoed (design gaps 1, 3, 7, 8, 10). Heartbeat (gap 2) is a later task.

Finish when every halt is persisted and obeyed by RiskGate, the Maker and paper_cycle in its scope, a bad halt file
is a global halt, only an Operator resume releases one, the e2e gate and guard prove it, and pytest passes offline.

- `src/halt_store.py`: `logs/halt_state.json` (version 1, atomic temp + fsync + `os.replace`, `.seen` marker), records
  `{id, scope market|global, market, reason, source, ts, active, resolved}`. `halt_problems(market)` never raises:
  `halt_market:<id>`, `halt_global:<id>`, `halt_state_{missing,unreadable,corrupt,malformed}`, `halt_file`
  (`logs/HALT` exists) or `halt_check_failed`. A bad state file is kept as evidence; new halts then go to `logs/HALT`.
- Gap 1: `risk_gate.evaluate` adds the halt reasons first (veto); `maker.quote` raises `HaltActive` before and after
  its append (a racing quote is cancelled by the Maker); `paper_cycle.run` skips gap signals of a halted market
  (`halted_market`, `halted_markets`), every market under a global halt.
- Gap 3: `podlib.BUS_TYPES` + `resume`; `bus()` accepts it only `from=Operator`, `to=Sentinel`, with `approved_by`,
  `halt_ref.id`, scope global|market, no exchange action, and no market resume of a global `halt_ref`; Operator may
  send nothing else. `halt_store.resume` / `python -m src.halt_store resume` checks Operator, approval, root cause,
  postmortem, an active `halt_ref`, matching scope (market resume never lifts global; global resume never lifts a
  market halt), logs the resume on the bus, then releases. `halt-file` / `halt-state-problem` move the evidence aside.
- Gap 7: `paper_ledger.commit_if_allowed` turns `invalid_risk_state` into a global Sentinel halt (result `halted`);
  the Executor's `ledger_integrity` / `ledger_write_failed` halts are global halts. Both stay until resumed.
- Gap 8: `sentinel.halt` writes the halt record first, then cancels quotes with an atomic rewrite, then the bus;
  a failed record, cancel or bus write writes `logs/HALT` and is reported in `failures`, never raised.
- Gap 10: a leg or market without a book row is stale (`missing_book`) and counts for StaleWatch.
- e2e (`halt_consumer` in golden; corrupt-ledger cases now carry `global_halts`): halt blocks later intents and the
  Maker on its market only; corrupt halt file vetoes an otherwise allowed intent and all gap signals; market resume
  of a global halt rejected and global still blocks after a market resume; Sentinel/Executor resume, Operator
  without approval and a non-Operator resume bus line rejected. Guard check `halt` (9 lines) checks these by meaning
  and against golden. Golden rewritten only with `--update-golden`. Tests: `tests/test_halt_consumer.py`.
- No new threshold (no M2 addition), no URL, risk.toml unchanged. Docs: PROTOCOL.md, COORDINATION (Operator bus
  exception, #26), PHASE0_RUN "Halts and resume".

## Task AM — GitHub Actions CI (guard --require-git + full pytest) and docs/DECISIONS.md

Status: done.

Assigned by Operator 2026-10-09, done by grok-build-local. Task AH added `scripts/guard.py` and left CI
with the user. This task is that CI, plus a written record of the five roadmap decisions.

Finish when `.github/workflows/ci.yml` runs `python scripts/guard.py --require-git` and full
`python -m pytest` only on pull requests and on pushes to `dev` and `main`, actions are pinned to a
full commit SHA, `permissions: contents: read`, no secrets are used, `docs/DECISIONS.md` records the
five decisions, and `python -m pytest` passes with no network and no credentials.

- Triggers: `pull_request` and `push` to `dev` and `main` only. No `workflow_dispatch`, no schedule.
- `permissions: contents: read`. Checkout sets `persist-credentials: false`. No `secrets:` key.
- Actions pinned: `actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1` (v7.0.1),
  `actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97` (v7.0.0). Python 3.12; pytest from
  `requirements-dev.txt`.
- `docs/DECISIONS.md`: (1) Phase 0 paper only, (2) daily truth is `origin/dev`, (3) `risk.toml` pin,
  (4) CI is KEY, (5) M2 thresholds stay code constants until the Operator decides.
- Letter AM: AK landed as PR #39 and AL as PR #40 while this worktree was open. AN is the unpublished
  local worktree `task-an/m3-resolution-snapshot`. AM is the next letter unused on `origin/dev`,
  `origin/main`, unclosed PRs, and remaining local/remote branches.
- Tests: `tests/test_ci_workflow.py`. Do not edit `config/risk.toml`.

## Task AN — M3 calibration step 1: read-only settlement snapshot (docs/M3_CALIBRATION.md, src/resolutions.py)

Status: done.

Requested by Poly指挥 (M3 calibration prerequisite step 1); done by grok-build-local. The Poly指挥 design
draft was not in the OpenClaw workspace; `docs/M3_CALIBRATION.md` records the dispatch. Letter AL is merged
(PR #40). Letter AM is a local in-progress worktree, so this PR uses AN.

Finish when closed Gamma markets are snapshotted only after a 48 h hold, required fields fail closed, offline
tests pass, and `config/risk.toml` is unchanged.

- `src/resolutions.py` walks public Gamma `/markets/keyset?closed=true&order=closedTime` through
  `scanner.fetch_all_markets(..., base_url=)` (no new host). `closedTime` must be a zoned ISO time
  (`expiry_rule.parse_end_date`); `endDate` is never the close clock. Write `data/resolutions.jsonl` (gitignored)
  only when `now - closedTime >= RESOLUTION_HOLD_HOURS` (48 h, boundary passes) and every required field is
  present: `condition_id`, unique 1.0/0.0 `outcome_prices_final`, version-aware `winning_token_id`, `closed_ts`,
  `snapshot_ts`, `status`, `uma_checked`. Missing `umaResolutionStatus` skips the row. `uma_checked` is true only
  for exact `resolved`; a unique winner with another UMA status is written as `status=closed`.
- Incomplete paging writes nothing (exit 3). Paper-pin drift writes nothing (exit 4) and does not fetch. A
  complete empty universe is exit 0. Dedup by `condition_id`. No bus, no fill, no order.
- `RESOLUTION_HOLD_HOURS = 48.0` in `src/thresholds.py` (re-exported; not coupled to `ENDGAME_WINDOW_HOURS`;
  not M2 pending). Tests: `tests/test_resolutions.py`. Docs: `docs/M3_CALIBRATION.md`, `docs/PHASE0_RUN.md`.

## Task AO — Signal -> OrderIntent: pure, tightening-only src/intent_builder.py, Kelly shadow, e2e shadow gate (SPEC_signal_to_intent)

Status: done.

Requested by Poly指挥 (queue: signal→intent after AK/AL), done by grok-bot from Poly预测's
`forecaster/SPEC_signal_to_intent.md` (baseline dev@f760e51). No src module turned a signal into a RiskGate
Intent; the e2e harness glue `build_intent` was the only producer. Letters AM (CI workflow) and AN (M3
resolution snapshot) were taken by the user's Grok Build sessions, so this is AO.

Finish when a pure builder produces an OrderIntent or canonical reasons, is never looser than RiskGate, keeps Kelly
in shadow, runs in the e2e gate in shadow mode, and `python -m pytest` and guard --require-git pass offline.

- `src/intent_builder.py` (stdlib + fees, book_staleness, expiry_rule, thresholds, podlib.pin_problems,
  risk_gate): `ProbabilitySignal`, `BookSnapshot`, `MarketContext`, `OrderIntent`, `IntentResult`;
  `build_order_intent`, `to_risk_intent`, `combine_probability`, `walk_asks`, `side_edge`, `kelly_size`, pure
  adapters `signal_from_bus` / `book_snapshot_from_row` / `market_context_from_row`. No clock read (`now`
  injected, else `invalid_now`), no file, bus or network; inputs never mutated (T-PURE).
- Edge per side with the conservative interval end, the book-walk price, `INTENT_ASSUMED_SLIPPAGE` and
  `fees.stress_fee` unchanged; bit-for-bit RiskGate's edge (T-EQ). Better side wins, tie -> outcome 0. Reasons in the
  SPEC 5 order; any reason means no intent. Price band by source (news [0.03, 0.97], endgame_confirm [0.98, 0.99]);
  a risk.toml band override can only narrow it. A raw Endgame signal is `missing_model_prob`.
- Size = RiskGate's 1% clip, shares rounded down at the tick-rounded limit, under `min_order_size` rejected. Kelly
  (kelly_fraction 0.15) is SHADOW only: kelly_raw / kelly_frac / Kelly size reported, never used;
  `kelly_not_enforced_by_riskgate` marks intents whose Kelly is below the clip. Enforcing it needs RiskGate
  `Intent.size_usd` (KEY, backlog #7).
- Deviations: open-only (sell -> `sell_requires_position`, or `sell_not_supported` with a position; no reduce path
  yet); extra fail-closed codes `invalid_outcome_index`, `market_mismatch`, `not_binary_market`,
  `invalid_book_levels`; `not_accepting_orders` also for unknown (None); blend freshness = strictest source limit.
- e2e phase 1 (shadow): each decision in golden gains `intent_builder` (only additions; RiskGate decisions,
  fills, cash unchanged). The run fails if the builder's Intent differs from `build_intent`'s on the same token or
  if it produces an intent RiskGate vetoes for an edge/price/spread/liquidity/staleness reason (tested by loosening
  it). Golden rewritten only with `--update-golden`.
- New numbers (src/thresholds.py, docs/THRESHOLDS.md): `INTENT_ASSUMED_SLIPPAGE = 0.005`, `INTENT_TTL_S = 60.0`,
  `MAX_SIGNAL_AGE_NEWS_S = 3600.0`, `MAX_SIGNAL_AGE_SPORTS_DEVIG_S = 300.0`, `MAX_SIGNAL_AGE_ENDGAME_CONFIRM_S = 600.0`,
  `MAX_SOURCE_LOGIT_GAP = 1.0` (M2 pending; each can only reject); `INTENT_SHARE_STEP = 0.01` (rounding). No
  risk.toml key, no URL, no order code. Tests: `tests/test_intent_builder.py` (T-* plan and properties P1-P5,
  fixed seeds), `tests/test_e2e_offline.py`. config/risk.toml unchanged, `live_enabled` false.

## Task AQ — Secret-string scan of the bus and logs: src/secret_scan.py, bus() filter, guard check secret_scan (Sentinel design gap 9)

Status: done.

Finish when a private key, mnemonic or credential value on the bus or in logs/ is a global halt, the matched text
is never echoed, the repo's own files pass, and `python -m pytest` plus `python scripts/guard.py --require-git` pass.

- Source: Poly哨兵's sentinel_design.md (event matrix, SEV-1: report only file:line / category / hash prefix, never
  echo; no bare 0x+64hex; key-name context reusing guard.py's words, PEM blocks, 12/24 consecutive BIP39 words), gap 9.
- `src/secret_scan.py` (stdlib + thresholds). Rules: `key_context:<word>` (a credential key name, then `:` or `=`,
  quotes / JSON escaping / `Bearer` allowed, then a value of >= `SECRET_VALUE_MIN_LEN` token characters with a letter
  and a digit; the words are guard.py `CREDENTIAL_WORDS` plus private/secret key, mnemonic, seed/recovery phrase,
  password, access/auth/refresh token, authorization), `pem_private_key` (a BEGIN ... PRIVATE KEY armour line of any
  type), `bip39_mnemonic` (>= `MNEMONIC_MIN_WORDS` consecutive words of the pinned list `src/bip39_english.txt` on one
  line, separated by whitespace, commas, semicolons, bars or list numbers). A transaction hash, condition id or token
  id alone never hits.
- A finding is `file:line rule=<rule> len=<n> sha256=<8 hex>`; the text is not kept, printed, raised or stored.
- Runtime: `podlib.bus()` scans each message before writing it. A hit (or a scan that cannot run) is not written; a
  global halt is recorded (halt_store, source Sentinel) and `SecretRefused` raised. `python -m src.secret_scan
  [--path P] [--no-halt]` scans logs/ (and any `--path`, e.g. an M1 RUN dir): a hit or an unreadable file is a global
  `sentinel.halt` (record, paper quotes cancelled, halt bus line); exit 0 clean, 1 secret, 2 scan error, 3 usage.
  `.env` files are never opened. Lifting it is the Operator's resume after the key is rotated and the logs cleaned.
- Guard check 10 `secret_scan`: tracked files (`git ls-files`; without git a walk that skips .git, .worktrees,
  virtualenvs, caches, data/, logs/) and logs/, read only, plus a self-test on fresh random canaries (a weakened rule
  or one that flags a bare hash FAILs). The repo's own files and fixtures pass.
- New numbers (src/thresholds.py, docs/THRESHOLDS.md, M2 pending): `SECRET_VALUE_MIN_LEN = 16`,
  `MNEMONIC_MIN_WORDS = 12` (must stay <= 12), `SECRET_HASH_PREFIX_CHARS = 8`. No risk.toml key, no URL, no order code.
- Later (not here): bus() write-error handling and ts monotonicity (rest of gap 9), a resident Sentinel tick that
  runs the scan (gaps 2/12). Tests: `tests/test_secret_scan.py`, `tests/test_guard.py` (fake secrets made at run time).

## Task AP — Sentinel heartbeat: heartbeat files, resident Sentinel tick, event matrix (Poly哨兵 design gaps 2, 4, 11, 12, 13, 14)

Status: done.

Requested by Poly指挥 (ruling on Poly哨兵's sentinel design; heartbeat was left open by AL), done by grok-bot on
dev@9f1b010; merged origin/dev with AQ (#44, private-key scan) before the PR.

Finish when every loop writes a heartbeat at the design cadence, a resident Sentinel tick halts on timeouts
(stale = halt, fail closed) and applies the event matrix, the AL follow-ups hold, the e2e gate covers it, and
`python -m pytest` and guard --require-git pass offline.

- `src/heartbeat.py`: `logs/heartbeat/<proc>.json` (proc, pid, host, started_at, ts, seq, git_sha, risk_sha256,
  live_enabled, mode, state, exit_code, progress), atomic with a `.seen` marker; a write failure never raises.
  Unreadable, corrupt, malformed, lost after the marker or future-stamped = stale. `run_main` for start/finish CLIs
  (scanner, endgame_scan, loop_claim) carries exit_streak / last_success_ts. ws_recorder and m1_run beat every
  `HEARTBEAT_RECORDER_EVERY_S` (also while backing off, `paused`), m1_rest after every sample, paper_cycle every cycle.
- `src/sentinel_tick.py` (resident, `SENTINEL_TICK_S`; `--once` is check-only and writes no Sentinel heartbeat):
  own heartbeat, risk.toml pins, each process heartbeat (timeouts, pin fields, exit codes by (module, code), recorder
  reconnect / gap-resync window), ledger verify. Halts through `sentinel.halt` into halt_store, deduped by an active
  halt with the same reason, so a condition still there after an Operator resume is halted on the next tick; one-off
  exit events once per run (`logs/sentinel_tick.json`). Alerts: `logs/alerts.jsonl` + Sentinel->Operator signal; a
  failed bus write writes `logs/HALT`.
- Consumers (`halt_store.active_halts`): once the resident Sentinel has beaten, its heartbeat older than
  `SENTINEL_HEARTBEAT_TIMEOUT_S`, missing, corrupt or finished is a stored global halt (Operator resume only).
- AL follow-ups: Reconciler exits 1/3/4 are a global halt in halt_store; a resumed stale-book market halt re-alarms
  on the next stale check (no fresh check needed).
- Gap 11: `paper_cycle --recording-dir` reads the run's rotated `books/books-*.jsonl` (empty = every leg stale);
  `--loop --every <= 60`. Gap 14: all checked markets stale `ALL_STALE_CONSECUTIVE_CYCLES` cycles = global halt.
- e2e: golden `sentinel_heartbeat` scenarios a-i (recorder stale, corrupt heartbeat, exit codes, Sentinel lost,
  recorder storm, all stale, stale resume re-alarm, Reconciler halt, recording dir), on their own bus; golden only via
  `--update-golden`. guard check 11 `heartbeat` (after AQ's check 10 `secret_scan`).
- New numbers (M2 pending): `HEARTBEAT_RECORDER_EVERY_S = 10`, `HEARTBEAT_RECORDER_TIMEOUT_S = 120`,
  `HEARTBEAT_REST_TIMEOUT_S = 900`, `SCANNER_HANG_S = 1800`, `SCANNER_MAX_SUCCESS_AGE_S = 86400`,
  `SCANNER_INCOMPLETE_CONSECUTIVE = 2`, `HEARTBEAT_PAPER_CYCLE_EVERY_S = 60`, `HEARTBEAT_PAPER_CYCLE_TIMEOUT_S = 180`,
  `HEARTBEAT_PAPER_CYCLE_HALT_FACTOR = 2`, `HEARTBEAT_CONSUMER_EVERY_S = 5`, `HEARTBEAT_CONSUMER_TIMEOUT_S = 30`,
  `SENTINEL_TICK_S = 30`, `SENTINEL_HEARTBEAT_TIMEOUT_S = 90`, `RECORDER_ESCALATION_WINDOW_S = 600`,
  `RECORDER_RECONNECT_ALERT_COUNT = 4`, `RECORDER_GAP_RESYNC_HALT_COUNT = 3`, `ALL_STALE_CONSECUTIVE_CYCLES = 2`.
  No risk.toml key, no URL, no order code. Tests: `tests/test_heartbeat.py`, `tests/test_sentinel_tick.py`,
  `tests/test_heartbeat_loops.py`, `tests/test_guard.py`, `tests/test_e2e_offline.py`. config/risk.toml unchanged,
  `live_enabled` false.
- Later: resume precondition signal, WS hash/tick resync, the Sentinel tick running AQ's secret scan; no halt_consumer process yet.


## Task AR — daily_loss counts realized losses (backlog #4, tightening only)

Status: done.

Requested by Poly指挥 (backlog #4; SPEC_signal_to_intent inconsistency 12: `daily_loss_usd` summed only fees, so
`max_daily_loss` was a daily fee cap), done by grok-bot on dev@9ec24df.

Finish when RiskGate's `max_daily_loss` check counts today's realized losses as well as today's fees, nothing it
counted before drops out, the day boundary is explicit and corrupt ledger data fails closed.

- `src/daily_loss.py` (pure): `daily_loss_usd = today's booked fees + today's gross realized losses`. Realized P&L is
  replayed per leg (market + token_id, `ledger_store.leg_key`) over the rows that count toward risk
  (`ledger_store.counts`), in ledger order, at average cost: a sell closing a long realizes proceeds minus average
  cost, a buy closing a short average proceeds minus cost, a remainder opens the other side. Fees stay out of P&L
  (counted once). A missing notional (legacy rows) is valued at the worst price (buy 1 USD/share, sell 0).
- Gains are reported (`daily_realized_gain_usd`) but never offset a loss or the fees; netting is left to the Operator.
- Day boundary: the UTC calendar day of `utc_now()` (the existing convention). A loss belongs to the UTC day of the
  closing fill's `ts`. A row counts toward today when its `ts` starts with today's date (old rule) or its UTC date is
  today or later.
- Fail closed: a counted row with a missing/unparseable `ts`, a side other than buy/sell, or shares / notional / fee
  not finite and >= 0 gives `daily_loss_problems` + `ledger_problems`, `daily_loss_usd = None`, RiskGate
  `invalid_risk_state` and the Task AL global halt.
- `paper_ledger.load_state` also returns `daily_fees_usd`, `daily_realized_loss_usd`, `daily_realized_gain_usd`.
- e2e: golden `daily_loss` cases a-f (loss closed at 00:00:00Z today vetoes, the same loss closed 23:59:59Z
  yesterday allows, a gain does not offset, 29 USD allows, fees + loss over the cap veto, unparseable ts = global
  halt); golden only via `--update-golden`. Tests: `tests/test_daily_loss.py`, `tests/test_e2e_offline.py`,
  `tests/test_ledger_integrity.py` (legacy_mixed now also counts its 0.05 USD realized loss).
- No new number. `max_daily_loss_pct` and config/risk.toml unchanged, `live_enabled` false.

## Task AT — One assumed slippage (`ASSUMED_SLIPPAGE`) and a ROLES.md that matches RiskGate (no Kelly sizing)

Status: done.

Requested by Poly指挥 (queue: after EXIT_CODES and daily_loss-realized; SPEC_signal_to_intent inconsistencies 1 and 2),
done by grok-bot on dev@08ae2ae. Ran in parallel with Task AS (EXIT_CODES + runtime pins).

Finish when every assumed slippage reads one constant, no copy can drift, no number or check moves, and
`docs/ROLES.md` no longer says RiskGate sizes with fractional Kelly.

- `src/thresholds.py`: new `ASSUMED_SLIPPAGE = 0.005` (M2 pending), the one source of truth.
  `SPORTS_DEVIG_ASSUMED_SLIPPAGE` (Task H) and `INTENT_ASSUMED_SLIPPAGE` (Task AO) are now aliases
  (`= ASSUMED_SLIPPAGE`); `consistency_problems` fails the import if either differs (looser or tighter) and keeps
  `0 <= ASSUMED_SLIPPAGE < 1`.
- `src/paper_ledger.py`: the four literal 0.005s (CLI `--slippage` default, self-check intent default, the
  no-edge check's touch filter and its intent) read `ASSUMED_SLIPPAGE`.
- All copies were already 0.005, so the strictest value is that value: no threshold, edge, veto or golden changed.
- Tests: `tests/test_slippage_unified.py` (one literal only, aliases are names, drifting copies fail at import, no
  float 0.005 literal in any `src/` or `scripts/` file but thresholds.py, every user and the e2e fixtures equal the
  constant, ROLES.md RiskGate row, RiskGate never reads `kelly_fraction`); `tests/test_thresholds.py` gains the
  constant, the re-export `paper_ledger.ASSUMED_SLIPPAGE` and import-failure edits. `docs/THRESHOLDS.md` row and
  relations.
- `docs/ROLES.md`: RiskGate = hard 1% order cap, daily loss, exposure, liquidity, spread, edge, staleness, halts;
  Kelly only in shadow mode in `src/intent_builder.py`, never used for size (enforcing it is KEY, backlog #7).
  README edge formula updated to the Task AE fee (`max(market_price, effective_price)`). COORDINATION #7 annotated
  (stays open).
- `config/risk.toml` unchanged, `live_enabled` false. Not done (would be a new veto, left for review): a RiskGate
  floor that vetoes an `assumed_slippage` below `ASSUMED_SLIPPAGE` (the paper_ledger CLI still accepts `--slippage 0`).

## Task AS — Exit-code registry and runtime pin checks (Poly哨兵 design gaps 4, 5, 6)

Status: done.

Requested by Poly指挥 (queue after AP: EXIT_CODES / pins), done by grok-bot on dev@9ec24df;
merged origin/dev with AR (#46, daily_loss) and AT (#47, one ASSUMED_SLIPPAGE) before the PR.

Finish when every entry point's exit codes are in one registry kept in sync with the code by a test and a guard
check, the long-running and data-writing processes check config/risk.toml (sha + every key) at start and while
running and refuse + halt globally on drift, and `python -m pytest` and guard --require-git pass offline.

- Gap 4: `docs/EXIT_CODES.md` lists the 18 entry points (every `src/` and `scripts/` file with a `__main__` block):
  each `EXIT_*` code, its meaning and the resident Sentinel's action from `src/sentinel_tick.py` `EXIT_EVENTS`.
  `src/exit_codes.py` (stdlib, `ast` only, never imports the modules) fails on an undocumented or changed constant, a
  documented code the module lacks, a literal exit code in `main` / `sys.exit`, `parser.error` or `parse_args`
  outside `try/except SystemExit`, a wrong Sentinel column, an `EXIT_EVENTS` entry without a row. Run by
  `tests/test_exit_codes.py` and guard check 12 `exit_codes`. Literal codes in main were replaced with names.
- Gap 6: `src/pins.py` `RISK_FILE_PINS` pins all 9 keys of config/risk.toml with value and type, plus the sha256
  (CRLF normalised); an unknown, missing or retyped key, an unreadable / unparseable file or a check that cannot run
  is drift. `podlib.PINS` stays at 5 keys (tests vary the in-memory config). The Sentinel tick checks the same pins.
- Gap 5: `refuse_start` at start of scanner, book, ws_recorder, m1_run (exit 6, new), endgame_scan, resolutions
  (exit 4), loop_claim (exit 2) and before every paper_cycle cycle (exit 1); `PinWatch` re-checks ws_recorder and
  m1_run at least every `PIN_RECHECK_S` and stops the run (exit 6, m1 manifest `pin_drift`). Drift records one global
  Sentinel halt `risk_toml_pin_drift` (shared with the Sentinel tick, deduped; `logs/HALT` if recording fails).
  Only the Operator resumes.
- Behaviour changes: loop_claim usage errors exit 5 (was argparse's 2 = `EXIT_PIN`, a global halt); paper_cycle exit
  1 is a Sentinel global halt (was an alert); scanner / ws_recorder / m1_run exit 6 = global halt. Usage errors of
  halt_store, paper_ledger, loop_claim go through their own `EXIT_USAGE`.
- New number (M2 pending): `PIN_RECHECK_S = 60` (`<= SENTINEL_HEARTBEAT_TIMEOUT_S`). No risk.toml change, no URL,
  no order code. Tests: `tests/test_exit_codes.py`, `tests/test_pins.py`, `tests/test_guard.py`,
  `tests/test_sentinel_tick.py`, `tests/test_ws_recorder.py`, `tests/test_m1_run.py`, `tests/test_thresholds.py`.
- Left: resolutions exit 4 still means pin drift or a bad snapshot clock (documented in the registry).

## Task AU — RiskGate slippage floor (tightening; Poly指挥 approved)

Status: done.

Requested by Poly指挥 after Task AT (#47) unified the slippage, done by grok-bot on dev@6368f80.

Finish when no intent can assume less slippage than `ASSUMED_SLIPPAGE`, including an Operator CLI intent.

- `risk_gate.evaluate`: a finite `assumed_slippage < ASSUMED_SLIPPAGE` (0.005, `src/thresholds.py`, inclusive floor)
  vetoes `slippage_below_floor`; missing / non-number vetoes `invalid_assumed_slippage`, NaN/inf
  `non_finite_assumed_slippage` (Task U, unchanged). A negative slippage also keeps `negative_cost_assumption`.
  Every intent, hypothetical gate tests included.
- `paper_ledger` CLI: `--slippage` is parsed by `slippage_arg` (finite and >= the floor, else argparse error, exit 2,
  before risk.toml, the ledger or the bus is touched). Default stays `ASSUMED_SLIPPAGE`.
- The intent builder's `INTENT_ASSUMED_SLIPPAGE` is the floor itself, so its intents still pass (tested in
  `tests/test_intent_builder.py` and in the e2e run).
- e2e: golden `slippage_floor` (0 and 0.0049 veto `slippage_below_floor`, 0.005 and 0.01 allow, NaN and missing veto,
  the builder's intent passes, CLI refuses 0 / 0.0049 / nan / -0.01 and accepts 0.005 / 0.01); golden only via
  `--update-golden`. Tests: `tests/test_slippage_floor.py`; `tests/test_fee_characterisation.py` (the recorded
  sell point with slippage 0.0 that allowed now vetoes, checked separately).
## Task AV — Split resolutions exit 4 (pin drift vs. bad snapshot clock)

Status: done.

Requested by Poly指挥 (left open by Task AS), done by grok-bot on dev@690a63b.

Finish when resolutions has one exit code per meaning, the Sentinel acts on both, the registry and the matrix agree,
and `python -m pytest` and guard --require-git pass offline.

- `src/resolutions.py`: `EXIT_PIN = 4` retired. Pin drift (risk file pins via `refuse_start`, global halt recorded,
  or the in-memory `podlib.PINS`) is `EXIT_PIN_DRIFT = 6`, the code scanner / book / ws_recorder / m1_run use. A
  snapshot clock that is not a zoned ISO timestamp is `EXIT_BAD_CLOCK = 5`. Neither fetches nor writes.
- `__main__` runs through `heartbeat.run_main("resolutions", main)` (start/finish heartbeat, like scanner), so the
  resident Sentinel sees its exit codes. `src/sentinel_tick.py`: `PROC_SPECS["resolutions"]` (`SCANNER_HANG_S`,
  started_at clock, hang = alert); `EXIT_EVENTS` (resolutions, 6) and (resolutions, 5) = global halt. The design
  has no row for the clock; the stricter action was chosen (as for `heartbeat_bad_time`).
- `docs/EXIT_CODES.md`: resolutions section 0 / 3 alert / 5 global halt / 6 global halt. PHASE0_RUN and
  COORDINATION #27 / #31 annotated.
- Tests: `tests/test_resolutions.py`, `tests/test_pins.py`, `tests/test_sentinel_tick.py`, `tests/test_exit_codes.py`.
- No new number. config/risk.toml unchanged, `live_enabled` false.

## Task AW — WS recorder resync hardening (market channel)

Status: done.

Requested by Poly指挥 from the WS part of Poly研究's `research_dataapi_ws_m2.md`, done by grok-bot on dev@23625ae
(dev@316d4e1, AV #50, merged in).

Finish when the recorder re-snapshots on a tick change, reconnects when PONGs stop, fails closed on unknown events for
its assets, records a snapshot-hash check, and `python -m pytest` and guard --require-git pass offline.

- `tick_size_change`: a valid change (old tick = the tick held, new tick valid) now resyncs the asset
  (`tick_size_change`: price-less invalidation row, unsubscribe/subscribe for a fresh snapshot); no price on the
  new tick is trusted before that snapshot. Not a gap: it is not counted toward the Task AP gap-resync escalation.
  Invalid or mismatched ticks keep their reasons (`invalid_tick_size`, `tick_size_mismatch`), which are gaps.
- PONG: a PING still unanswered `WS_PONG_TIMEOUT_S` (30 s, new, M2 pending) after it was sent disconnects
  (`pong_timeout`); every asset resyncs (`disconnect_pong_timeout`) and the reconnect counts toward the
  reconnect escalation. PINGs keep going every 10 s; a later PING does not restart the wait.
- Unknown `event_type`: fail closed. One naming a subscribed asset (`asset_id`, `assets_ids`,
  `price_changes[].asset_id`) or its market resyncs those assets (`unknown_event`, a gap); one naming nothing
  resyncs every asset; one naming only other assets/markets is counted (`ignored_event:<kind>`) and ignored.
  Churn risk: a new server event type for our assets will resync them on every occurrence.
- Snapshot hash: `book_hash_check()` = SHA-1 hex of compact JSON (market, asset_id, timestamp, hash="", bids, asks,
  min_order_size, tick_size, neg_risk, last_trade_price, in that order). The algorithm is not officially
  published (Poly研究: older clients / Nautilus). Per Poly指挥's ruling it is recorded, not enforced
  (`BOOK_HASH_ENFORCED = False`): each row carries `book_hash_check` (match / mismatch / incomplete / absent /
  uncomputable) and the run counts `hash_check:*`. The M1 recording (m1-20261008T2259Z) keeps only the top of book,
  so 0 snapshots could be checked; its WS book frames also never carried `min_order_size`, so on today's frames the
  check reads `incomplete`. Supplemental: 10/10 live REST `/book` bodies matched the field order above (0/10 with
  neg_risk before tick_size). The enforced path (any result but `match` resyncs, `book_hash_mismatch`, a gap) is
  tested but off.
- No new URL, dependency or outgoing frame. `WS_PONG_TIMEOUT_S` in `src/thresholds.py` (relation
  `0 < WS_PING_INTERVAL_S < WS_PONG_TIMEOUT_S`) and `docs/THRESHOLDS.md`.
- Tests (`tests/test_ws_recorder.py`, offline, synthetic frames; the fake connection answers PING unless told not
  to; `tests/test_m1_run.py` fake answers PING). config/risk.toml unchanged, `live_enabled` false.

## Task AX — M3 calibration step 2: identity fields and the sealed forecast log

Status: done.

Requested by Poly指挥 (Poly反思's M3 calibration design section 1.5, prerequisite 2; the NewsForecaster
payload per the signal->intent ruling), done by grok-bot on dev@316d4e1.

Finish when every forecast is sealed in `logs/forecasts.jsonl` before its signal, scanner rows, the signal,
the intent and every paper fill carry the identity the forecast -> fill -> resolution join needs, a fill that
cannot join says why (never an invented id), no RiskGate decision moves, and `python -m pytest`, the e2e gate
and guard --require-git pass offline.

- `src/calibration_ids.py` (stdlib): `forecast_id` (`fc_` + sha256 of the forecast's content), `decision_id`
  (`dc_` + sha256 of strategy, market, token, forecast_id; None when a part is missing), row schemas,
  `fill_problems` / `forecast_problems`, `append_forecast` (one O_APPEND write + fsync, idempotent, refuses a
  torn tail, wrong keys or nan).
- `src/forecaster.py`: seals the row first (no signal if it cannot); payload adds `forecast_id`,
  `forecast_ts`, `model_version` and, when given, `outcome_index`, `outcome`, `token_id`, `condition_id`,
  `p_low`, `p_high`. `MODEL_VERSION = "news-forecaster-stub/1"`.
- `src/scanner.py`: rows add `category`, `tags` (keyset URL `include_tag=true`); neither filters.
- `src/risk_gate.py` `Intent`, `src/intent_builder.py` (`ProbabilitySignal`, `MarketContext.gamma_category` /
  `tags`, `OrderIntent`, `to_risk_intent`), `src/paper_ledger.py` fills: identity fields plus
  `calibration_problems`. RiskGate never reads them (test).
- e2e gate: new golden section `calibration_ids` (written with --update-golden; no existing value moved).
- Docs: `docs/M3_CALIBRATION.md` Step 2. No new number; `config/strategies.toml` not created.

## Task AY — Opt-in raw WS book snapshots and an offline hash check

Status: done.

Requested by Poly指挥 after Task AW (#51), done by grok-bot on dev@92d406e (dev@d2af163, AX #58, merged in).

Finish when an M1 run can keep the full book frames the snapshot hash is computed over, without changing default
disk use, and an offline tool can say whether the AW algorithm matches them; `python -m pytest` and guard
--require-git pass offline.

- `src/ws_recorder.py`: `Recorder(raw_books=...)` receives `{"ts", "asset_id", "frame"}` for every `book` frame
  of a subscribed asset, the parsed frame as received (all levels, the hash, `min_order_size` / `tick_size` when
  sent), applied or rejected. Without it nothing is kept. Rows, resyncs and freshness are unchanged.
- `src/m1_run.py --raw-books` (default off): `raw_books/raw_books-NNNNNN.jsonl` in the run dir, whole-line
  appends (`JsonlAppender`, one write per line), rotated like the other streams. No file passes
  `min(--rotate-mb, WS_RAW_BOOK_FILE_MAX_BYTES)` (16 MiB); after `WS_RAW_BOOK_RUN_MAX_BYTES` (256 MiB) further
  frames are dropped and counted (one `raw_books_capped` event) and the recording goes on. Frames that are not
  strict JSON (NaN) or alone bigger than a file are dropped and counted. Manifest: `settings.raw_books` and a
  `raw_books` summary (files, saved, bytes, dropped, caps). m1_report ignores the directory.
- `src/ws_hash_check.py` (`python -m src.ws_hash_check <run dir | raw_books dir | file> [--json]`): opens files
  read-only, recomputes each frame with `ws_recorder.book_hash_check`, reports match / mismatch / incomplete /
  absent / uncomputable / unreadable overall and per asset. Exit 0 only when at least one frame was checked and
  all matched, 1 otherwise, 2 usage. `BOOK_HASH_ENFORCED` stays False; enforcing is a later decision.
- `docs/EXIT_CODES.md` section (19 entry points now), `docs/M1_ACCEPTANCE.md` note, `docs/THRESHOLDS.md` rows.
- New numbers (M2 pending): `WS_RAW_BOOK_FILE_MAX_BYTES = 16 MiB`, `WS_RAW_BOOK_RUN_MAX_BYTES = 256 MiB`
  (relation: whole numbers, `0 < file cap <= run cap`).
- Tests: `tests/test_ws_raw_snapshots.py` (synthetic frames, sockets patched to fail; secret scan clean on the raw
  files). No new URL, dependency or outgoing frame. config/risk.toml unchanged, `live_enabled` false.

## Task AZ — M3 calibration step 3: offline calibration report (src/calibration.py)

Status: done.

Requested by Poly指挥 (Poly反思's M3 calibration design sections 1-3 and 6, step 3), done by grok-bot on
dev@d2af163. AY (#59) ran in parallel; merged in from dev (dev@39aebfa).

Finish when forecasts and fills join the settlement snapshot offline, the design's metrics are reported, every
dropped row is counted by reason (never imputed), corrupt input exits with an error and writes nothing, the
minimums say `insufficient` instead of a verdict, and `python -m pytest` and guard --require-git pass offline.

- `src/calibration.py`: `python -m src.calibration` writes `reports/calibration/<UTC date>/` (report.json,
  report.md, samples.jsonl). B (forecasts) and A (trades by decision_id) samples; Brier, log loss,
  reliability + Wilson, ECE, slope / intercept, market-clustered bootstrap intervals; Brier vs market,
  skill, E / R / realization; groups by category, strategy, model_version (and side, edge tier, expiry tier,
  price band for A).
- Exit codes 0 / 2 / 3 corrupt input / 4 ledger fails verify / 5 write failed in `docs/EXIT_CODES.md`
  (20 entry points with AY's ws_hash_check; no heartbeat, Sentinel `-`).
- New numbers (M3 pending): `CALIBRATION_*` in `src/thresholds.py` and `docs/THRESHOLDS.md`.
- Docs: `docs/M3_CALIBRATION.md` Step 3. `reports/calibration/` git-ignored. No e2e/guard change (the e2e
  fixtures have no resolutions to join).

## Task BA — Market context on paper fills; ws_recorder --raw-books

Status: done.

Requested by Poly指挥 (rest of m3_calibration_design.md 1.5 after Task AX), done by grok-bot on dev@39aebfa.
AZ (#60, src/calibration.py) runs in parallel; this task does not touch src/calibration.py.

Finish when every paper fill records its market context at commit without inventing a value, RiskGate
provably reads none of it, the standalone recorder can save raw book frames like m1_run, and
`python -m pytest` and guard --require-git pass offline.

- `risk_gate.Intent`: `best_bid`, `best_ask`, `kelly_fraction_used` (None default). RiskGate never reads
  them (source check and identical decisions for any values, `tests/test_fill_market_fields.py`).
- Sources: `intent_builder.to_risk_intent` passes the chosen token's BookSnapshot `best_bid` / `best_ask` and
  the shadow Kelly multiplier it used (`kelly_fraction`, risk.toml 0.15); the e2e harness intent passes the
  same from its /book row and risk.toml; the paper_ledger no-edge check passes its book row's top.
- `paper_ledger.market_fields` (fill, at commit): `best_bid` / `best_ask` finite in (0, 1);
  `market_mid` = (bid + ask) / 2 when both usable and bid <= ask; `hours_to_expiry_at_entry` = end_date −
  fill ts (hours, may be negative); `kelly_fraction_used` in (0, 1]. Otherwise None and `missing_<field>` /
  `invalid_<field>` (crossed book: `invalid_market_mid`) in the stored `calibration_problems`. The join check
  `fill_problems` is unchanged. Consequence: the AZ report drops fills whose stored problems are non-empty.
- e2e golden (`--update-golden`): only the new `market_fields` of the three calibration fills.
- `RotatingJsonl` / `RawBookSink` moved to `src/ws_recorder.py` (m1_run re-exports them).
  `python -m src.ws_recorder ... --raw-books DIR` (off by default; DIR new or empty, else exit 2) writes
  `DIR/raw_books-NNNNNN.jsonl` with the same writer and caps (16 MiB per file, 256 MiB per run).
- Docs: `docs/M3_CALIBRATION.md` Step 2b, `docs/EXIT_CODES.md`, `docs/THRESHOLDS.md` wording.
- No new number, no decision, threshold or size moved. config/risk.toml unchanged, `live_enabled` false.

## Task BB — M3 calibration step 4: structured Reflector proposals (src/reflector_proposals.py)

Status: done.

Requested by Poly指挥 (Poly反思's M3 calibration design sections 4 and 5, step 4), done by grok-bot on
dev@e667e4a. BA (#62) ran in parallel; merged in from dev (dev@ff21802).

Finish when the Reflector turns a calibration report.json into structured, tightening-only proposals (jsonl, md,
bus with `exchange_action: none`) within the daily cap, never applies one, never writes config/, a .toml or
thresholds.py, proposes nothing from an `insufficient` section, marks loosening for KEY, fails closed on a missing
or corrupt report, and `python -m pytest` and guard --require-git pass offline.

- `src/reflector_proposals.py`: rules (disable, MIN_EDGE tightening, price-band tightening), `PROPOSAL_FIELDS`
  schema, `validate`, rejected-target cooldown, CLI exit 0 / 2 / 3 missing / 4 corrupt report / 5 corrupt
  proposals.jsonl / 6 write failed (`docs/EXIT_CODES.md`, 21 entry points).
- `src/reflector.py`: `propose(payload=...)`, `exchange_action: none` on every proposal, `refused_path`.
- New numbers (M3 pending): `REFLECTOR_*` rule thresholds; `REFLECTOR_MAX_PER_UTC_DAY` (3) now M3 pending.
- Docs: `docs/M3_CALIBRATION.md` Step 4. Letter note: PR #61 (pod-flash) titled itself "Task AZ" but added only
  a test file and no queue entry; AZ stays #60.

## Task BC — Calibration at market_mid; both Kelly values; fill_context and no_book_context

Status: done.

Requested by Poly指挥 after Task BA (#62), done by grok-bot on dev@ff21802. BB (#66,
src/reflector_proposals.py) reads report.json in parallel, so report changes only add keys.

Finish when the trade sample scores the market at the decision-time mid, both Kelly values are on the fill
and provably unread by RiskGate and sizing, fills without book context have their own drop reason stamped
at fill time, and `python -m pytest` and guard --require-git pass offline.

- `src/calibration.py`: q = the first fill's `market_mid` (`price_source=market_mid`). Design 3 allows
  "mid if there is one, else `market_price`, with price_source noted"; that fallback is kept only for a fill
  with no `market_mid` key (booked before Task BA). A recorded but unusable mid is never replaced: it is a
  `fill_calibration_problems` drop (stored `missing_market_mid`), or `fill_market_mid_missing` if the stored
  list hides it. `trades.price_sources` counts samples per source; samples carry `price_source`.
- `risk_gate.Intent`: `kelly_frac_raw` (intent_builder `kelly_raw`, unscaled; `kelly_frac = kelly_fraction_used x
  kelly_frac_raw`) and `fill_context` (`intent_builder` / `operator_cli` / `gate_check`, from
  `calibration_ids.FILL_CONTEXTS`). Both on the fill; null + `missing_` / `invalid_` in `calibration_problems`.
  RiskGate and sizing never read them (source scan, identical decisions and fill sizes; `tests/test_calibration_mid_kelly.py`).
- Report: `fill_context` in `operator_cli` / `gate_check` -> `no_book_context` (checked first, market-field
  problems not counted). A fill without the key (before BC) is judged as before; nothing is inferred from
  missing fields. A stored null / unknown context is a data problem (`missing_` / `invalid_fill_context`).
- `report.json` stays `schema_version` 1 and only gains keys. e2e golden (`--update-golden`): only
  `fill_context` and `market_fields.kelly_frac_raw` on the three calibration fills.
- Docs: `docs/M3_CALIBRATION.md` step 2c and the drop table. No new number, no decision, threshold or size
  moved. config/risk.toml unchanged, `live_enabled` false.

## Task BD — Calibration intervals per edge tier and category; intercept CI; event-cluster share

Status: done.

Requested by Poly指挥 after Task BC (#67), done by grok-bot on dev@cfefc52. Report changes are additive only
(BB's src/reflector_proposals.py reads report.json); the six missing Reflector rules are a later task.

Finish when judged by_category / by_edge_tier groups carry bootstrap intervals, a sufficient forecast section
carries an intercept interval and verdict, both sections report the largest-cluster share flagged under the
existing minimums, and `python -m pytest` and guard --require-git pass offline.

- `src/calibration.py`: `group_counts(..., intervals=True)` on `forecasts.by_category`, `trades.by_category`,
  `trades.by_edge_tier`: `intervals` = Brier, ECE, slope, intercept with market-clustered bootstrap intervals
  (`CALIBRATION_BOOTSTRAP_*`, fixed seed); trades add `brier_market`, `delta_brier(_ci)`, `realization(_ci)`.
  Groups below `CALIBRATION_GROUP_MIN_*` get `intervals: null`.
- Sufficient B section: `calibration.intercept_ci`, `ece_ci`, verdict `calibration_intercept`
  (`positive_bias` / `negative_bias` / `no_clear_bias` / `intercept_not_estimable`).
- `event_cluster` on both sections: largest cluster share; `insufficient` below the section minimums, else
  `concentrated` above `CALIBRATION_MAX_CLUSTER_SHARE` (0.30, design 3, new M3-pending number in
  src/thresholds.py and docs/THRESHOLDS.md only), else `ok`. No event id is recorded, so the cluster is the
  market (condition_id): a lower bound on event concentration. Reported only.
- Tests: `tests/test_calibration_intervals.py`; two exact-shape assertions in tests/test_calibration.py widened
  for the added keys. Docs: `docs/M3_CALIBRATION.md` (Task BD subsection, Step 4 not-implemented list).
- No decision, threshold, size or proposal moved. config/risk.toml unchanged, `live_enabled` false.

## Task BE — M3 Reflector rules after BB: category delta Brier, edge-tier realization, intercept, cluster share

Status: done.

Requested by Poly指挥 after Task BD (#71), done by grok-bot on dev@71386b6. Reads only the intervals BD added to
report.json; the report is unchanged.

Finish when the Reflector proposes from the per-category, per-edge-tier and intercept intervals, carries the
event-cluster share on every proposal, still only tightens and never applies, fails closed on corrupt new
fields, gives nothing new for a report from before BD, and `python -m pytest` and guard --require-git pass offline.

- `src/reflector_proposals.py`: `disable` of one category (`target.category`) when a judged
  `trades.by_category` group's `delta_brier_ci` lies wholly above 0 (not when the strategy itself is disabled).
- `param_change` MIN_EDGE to the top of the failing edge tiers (`target.edge_tier`): walk up from the trade edge
  tier holding MIN_EDGE; each judged tier with `realization_ci` wholly below `REFLECTOR_EDGE_TIER_REALIZATION_MAX`
  extends the run. A failing tier above a passing or unjudged one, and the open top tier, are observations only.
- Forecast `calibration.intercept_ci` wholly off 0 (positive or negative bias) joins the MIN_EDGE + step
  proposal (one target, one proposal; a new one when the slope rule does not fire).
- `evidence.event_cluster` (cluster_key, flag, largest_share, max_share) on every proposal; a `concentrated`
  section is stated in the proposal's `risk` and in the observations. Nothing is gated on it.
- Disable before param_change under the daily cap. Corrupt group intervals / intercept_ci / event_cluster: exit 4.
- New number (M3 pending): `REFLECTOR_EDGE_TIER_REALIZATION_MAX` 0.25 (src/thresholds.py, docs/THRESHOLDS.md).
- Not done: the two-batch Kelly rule (`kelly_fraction` is in config/risk.toml, which no proposal may name, and
  the rule is about raising Kelly) and the cumulative-loss disable (no threshold in the repo). Both need
  Poly反思's numbers and a decision on where they live.
- Tests: `tests/test_reflector_rules_be.py`. No decision, threshold, size or gate moved. config/risk.toml
  unchanged, `live_enabled` false.

## Task BF — Event id on forecasts and fills; event_id as the calibration cluster key

Status: done.

Requested by Poly指挥 as the follow-up flagged in Task BD (#71), done by grok-bot on dev@f8bb307. Report only;
the Reflector (BE) reads `event_cluster` unchanged.

Finish when forecasts and fills record the Polymarket event (Gamma market `events`) without ever crashing on
missing or malformed data, the calibration report clusters `event_cluster` by event_id with a per-sample
condition_id fallback and says which key it used, rows from before BF stay valid, and `python -m pytest` and
guard --require-git pass offline.

- `src/calibration_ids.py`: `scanner_event(market)` -> `(event_id, event_slug)` from Gamma's `events` list (the
  one id all usable entries name; none, malformed or conflicting -> `(None, None)`, never guessed);
  `event_value` (non-blank string or int, else None). `FILL_EVENT_FIELDS` / `FORECAST_EVENT_FIELDS` =
  `event_id`, `event_slug`, appended to `FORECAST_ROW_FIELDS`. Never a calibration problem, never a join field.
- Recorded on: scanner rows (`data/markets.jsonl`), `MarketContext`, `OrderIntent`, `risk_gate.Intent` (RiskGate
  never reads them: source scan + identical decisions), paper fills (`paper_ledger.event_fields`) and
  `logs/forecasts.jsonl` rows (`emit_probability(event_id=, event_slug=)`; not part of `forecast_id`, not in the
  signal payload). The e2e harness passes the scanner row's event through; the golden is unchanged.
- `src/calibration.py`: a sample's cluster is its event_id, else its condition_id. A trade takes its first
  fill's event_id, else the joined forecast's; both usable and different -> condition_id. `event_cluster` gains
  `event_id_samples`, `condition_id_fallback_samples`; `cluster_key` = `event_id` / `condition_id` / `mixed`.
  Forecast rows without the event keys (before BF) are read as before; a malformed event value falls back.
  Bootstrap intervals stay market-clustered. No new number (`CALIBRATION_MAX_CLUSTER_SHARE` 0.30 as before).
- Tests: `tests/test_event_id_clusters.py`; the test forecast helper and the harness-shape assertion gain the
  two keys. Docs: `docs/M3_CALIBRATION.md` (Task BF subsection), `docs/THRESHOLDS.md` row note.
- No decision, threshold, size, gate or proposal moved. config/risk.toml unchanged, `live_enabled` false.

## Task BG — Calibration bootstrap intervals resample by event (the event_cluster key)

Status: done.

Requested by Poly指挥 as the open point Task BF (#74) left, done by grok-bot on dev@67ef100. Report only;
the Reflector (BE) reads the intervals unchanged.

Finish when every calibration bootstrap interval resamples the same clusters as `event_cluster` (event_id,
else condition_id per sample), the report says which key it resampled by, samples without an event id give
the intervals they gave before, the seed and resample count are unchanged, and `python -m pytest` and guard
--require-git pass offline.

- `src/calibration.py`: `cluster_bootstrap` clusters by `cluster_name` (BF): markets of one event are drawn
  together; an event id and a condition id with the same text stay apart; `key=` keeps a plain-field bootstrap.
  Applies to the overall Brier, ECE, slope, intercept, the trade delta Brier, skill, R and mean(R - E) and every
  judged `by_category` / `by_edge_tier` group's intervals. Fewer than two clusters: no interval.
- `forecasts.metrics`, `trades.metrics` and judged group `intervals` gain `bootstrap_cluster_key` (`event_id` /
  `condition_id` / `mixed`) and `bootstrap_clusters`; the markdown cluster line and a report note name them.
- Same `CALIBRATION_BOOTSTRAP_SEED` / `_RESAMPLES` / `CALIBRATION_CI_LEVEL`; no new number. report.json
  schema_version 1, keys only added.
- Tests: `tests/test_event_id_bootstrap.py` (one event drawn together, fallback, unusable ids, mixed, name
  collision, determinism, pre-BF intervals unchanged); BF's "stay market-clustered" test now checks the event
  bootstrap; the BD interval key set gains the two keys. Docs: `docs/M3_CALIBRATION.md` (Task BG subsection),
  `docs/THRESHOLDS.md` row note.
- No decision, threshold, size, gate or proposal moved. config/risk.toml unchanged, `live_enabled` false.

## Task BH — WS recorder: reconnect at once after a suspend / wake

Status: done.

Requested by Poly指挥 after the M1 24h run m1-20261008T2259Z (git f760e51, before AW #51) failed: after each of
18 host suspend / wake cycles the WS connection was dead but undetected for minutes (coverage 0.858, p99 book age
~4,020 s, REST unmatched 0.146). Done by grok-bot on dev@d1b94fa.

Finish when a post-wake dead connection is replaced within 30 s offline (fake clocks and connection), the
reason is counted like any disconnect, and `python -m pytest` and guard --require-git pass.

- Already on dev (AW #51): a PING every WS_PING_INTERVAL_S; the first unanswered PING disconnects after
  WS_PONG_TIMEOUT_S (`pong_timeout`). After a wake the overdue PING goes out on the first loop pass, so with a
  monotonic clock that runs through the suspend AW catches the dead socket within 30 s. No silence watchdog was
  added (no gap found; no duplicate threshold).
- Added: each session-loop pass compares the wall-clock step (`Recorder(wall=...)`, default `time.time`) with
  the monotonic step; the wall clock ahead by more than WS_PING_INTERVAL_S (CLOCK_MONOTONIC stops during a
  suspend) disconnects at once with `clock_jump` (`disconnected` event with `jump_s`, every asset resyncs
  `disconnect_clock_jump`), then the normal backoff, reconnect and full resync. A wall step back is ignored.
- Counted in report.disconnects, the reconnect escalation and m1_report's reconnect_attempts (no exemption).
  M1_MAX_RECONNECTS stays 48: 18 wakes would use 18 of it.
- Tests: `tests/test_ws_clock_jump.py` (replay: paused 1-120 min, socket dead on resume, reconnect within 30 s,
  both clock behaviours; no false reconnect on drift or a step back). No new number; THRESHOLDS wording only.

## Task BI — WS recorder: inbound-silence watchdog and PING / PONG latency summary

Status: done.

Requested by Poly指挥 (queue order 1) after Poly盘口's analysis of M1 run m1-20261008T2259Z (git f760e51, before
AW #51): 13 awake-but-dead stretches, 657-1,270 s each (median 952 s), every one after a pause and ended by the
peer's close; healthy global data silence max 38.7 s (none above 45 s). Done by grok-bot on dev@5440002.

Finish when a connection silent for WS_SILENCE_TIMEOUT_S is replaced offline, any frame (data or PONG) restarts
the window, 38.7 s of silence never trips it, the latency summary is checked, and `python -m pytest` and guard
--require-git pass.

- Finding: on current dev AW's `pong_timeout` would have ended each of the 13 within about 40 s (the first
  unanswered PING, at most WS_PING_INTERVAL_S after the last PONG, plus WS_PONG_TIMEOUT_S). Polymarket answers
  the text `PING` with the text frame `PONG`, which the recorder recognises; only a PONG clears the deadline, and
  recv never blocks past it. f760e51 had no PONG deadline at all.
- Added: no inbound frame of any kind for WS_SILENCE_TIMEOUT_S (monotonic; opens at subscribe) disconnects with
  `silent` (`disconnected` event, every asset resyncs `disconnect_silent`), then the normal backoff, reconnect and
  full resync. Not a duplicate of `pong_timeout`: it looks only at what actually arrives, not at PING / PONG
  bookkeeping, and fires at once when the process was paused >= 60 s with the monotonic clock running (before
  any PING goes out). While PONGs flow it cannot fire; a stalled data feed with live PONGs is not caught (a
  data-only window would be a separate decision).
- `ping_latency` event every WS_LATENCY_SUMMARY_PINGS PINGs (periodic) and after each connection
  (connection_end): pings, pongs, unanswered, unexpected_pongs, rtt_n, RTT p50 / p99 / max ms, last PING / PONG
  times. RunReport.pongs added.
- Counted in report.disconnects, the reconnect escalation and m1_report's reconnect_attempts (no exemption).
  M1_MAX_RECONNECTS stays 48.
- New numbers: WS_SILENCE_TIMEOUT_S 60 (M2 pending), WS_SILENCE_OBSERVED_HEALTHY_MAX_S 38.7 (evidence, M2
  pending), WS_LATENCY_SUMMARY_PINGS 60 (operational); checks WS_PONG_TIMEOUT_S < WS_SILENCE_TIMEOUT_S and
  38.7 < WS_SILENCE_TIMEOUT_S. Tests: `tests/test_ws_silence.py`; `tests/test_ws_clock_jump.py` updated.

## Task BK — WS recorder: market-data silence window (Poly风控 F6)

Status: done.

Requested by Poly指挥 after #140 (Task BI) merged: a server that answers PONGs but sends no market data must be
judged dead (Poly风控 F6). Done by grok-bot on dev@d5026a8.

- Kept: BI's any-frame window (`silent`, WS_SILENCE_TIMEOUT_S 60 s, PONG counts) and AW's `pong_timeout`.
- Added: a second window timed only by market business frames: a JSON event object with a non-empty
  `event_type` (book, price_change, tick_size_change, last_trade_price, or an unknown type, which is market
  traffic too). PONG, undecodable and malformed frames never count. None for WS_DATA_SILENCE_S disconnects with
  `data_silent` (`disconnected` event, every asset resyncs `disconnect_data_silent`), then the normal backoff,
  reconnect and full resync; counted in report.disconnects, the escalation and M1's reconnect cap.
- Subscription-wide. M1 evidence: across 5 markets the healthy data silence peaked at 38.7 s; per market the row
  gaps reached 84.9 s, so a one-market subscription could come close to 90 s.
- New number: WS_DATA_SILENCE_S 90 (M2 pending); import-time check WS_SILENCE_TIMEOUT_S <= WS_DATA_SILENCE_S <=
  MAX_BOOK_AGE_S. Deadline checks now use the same sum as the recv wait (a float rounding could spin a fake clock).
- Tests: `tests/test_ws_silence.py` (PONGs but no data -> data_silent at ~90 s; market events restart it; 38.7 s
  gaps never trip it; which frames count); live fakes in the clock-jump / recorder tests now send trades.

## Task BJ — Issue #104: RiskGate sizes inside the all-in 1% clip, never above intent_builder

Status: done.

P1 from Poly预测's review (B2): intent_builder sized 20 shares @ 0.50, to_risk_intent passed no size, and RiskGate
booked its own $10 / 0.505 = 19.80 shares plus a $0.40 fee: $10.40, 1.04% of equity. Poly风控's interim rule: the
1% cap includes fees. Done by grok-bot on dev@d5026a8.

- Root cause: two sizings. RiskGate set notional = 1% of equity regardless of the intent, divided by its
  slippage price, and added the fee on top; intent_builder floored cap / limit_price without slippage or fee.
- Fix (only shrinks): `risk_gate.Intent.shares` / `limit_price` carry intent_builder's size. Booked shares =
  min(intent.shares, floor_to_INTENT_SHARE_STEP(cap / (max(effective, limit) + fee_per_share))); notional_usd =
  shares * effective; fee = fees.ledger_fee. So shares * max(effective, limit) + fee <= cap. A legacy intent
  (no size) is sized the same all-in way. Invalid shares / limit veto; nothing fitting vetoes
  `order_cost_above_clip`. No threshold or risk.toml change.
- e2e golden regenerated via --update-golden (sizes, cash, cost basis only).
- Tests: `tests/test_riskgate_intent_size.py` (the repro, never above the intent, limit above effective,
  legacy all-in, invalid sizes, nothing fits, fixed-seed fuzz of intent_builder intents).

## Latest

- 2026-10-10: Task BJ (issue #104): RiskGate books min(intent_builder's shares, what fits the all-in 1% clip);
  cost plus fee never above $10. No threshold change.
- 2026-10-10: Task BK: WS recorder `data_silent` after WS_DATA_SILENCE_S=90 s with no market event (PONG does
  not count, Poly风控 F6); BI's any-frame `silent` (60 s) kept.
- 2026-10-10: Task BI: WS recorder inbound-silence watchdog (`silent` after WS_SILENCE_TIMEOUT_S=60 s of no
  frame of any kind) and `ping_latency` summary events. AW's `pong_timeout` unchanged. Blocks the M1 rerun.
- 2026-10-10: Task BH: WS recorder reconnects at once after a suspend / wake (`clock_jump`, `jump_s`); AW's
  `pong_timeout` covers a monotonic clock that runs through the suspend. No new threshold. Blocks the M1 rerun.
- 2026-10-10: Task BG: calibration bootstrap intervals resample by event_id, else condition_id per sample (the
  `event_cluster` key); `bootstrap_cluster_key` / `bootstrap_clusters` recorded. Reported only.
- Task BF (merged #74): Polymarket event id / slug (Gamma `events`) on scanner rows, forecasts and fills;
  `event_cluster` clusters by event_id with per-sample condition_id fallback (`cluster_key` event_id /
  condition_id / mixed, fallback count). Recorded and reported only.
- Task BE (merged #73): Reflector rules from BD's intervals: per-category delta Brier disable, per-edge-tier
  realization MIN_EDGE tightening (`REFLECTOR_EDGE_TIER_REALIZATION_MAX`, M3 pending), intercept joins the
  MIN_EDGE + step proposal, `event_cluster` on proposal evidence. Proposals only; tightening only.
- Task BD (merged #71): calibration report intervals per category and edge tier (Brier, ECE, slope, intercept;
  trades also delta Brier and realization), intercept CI + verdict, `event_cluster` share flagged against
  `CALIBRATION_MAX_CLUSTER_SHARE` (M3 pending). Report only; additive keys.
- Task BC (merged #67): calibration q = `market_mid` (pre-BA fills fall back to `market_price`, labelled);
  `kelly_frac_raw` recorded next to `kelly_fraction_used`; `fill_context` stamped at fill time and the report
  drops operator CLI / gate-check fills as `no_book_context`. Recorded only; RiskGate and size never read them.
- Task BB (merged #66): `python -m src.reflector_proposals` turns a calibration report.json into structured,
  tightening-only proposals (never applied; loosening needs KEY). BC only adds report keys.
- Task BA (merged #62): fill market context and `ws_recorder --raw-books`. Task AZ (merged #60): offline
  calibration report.
- pytest + guard (12 checks) pass; risk.toml unchanged (0aea23af…bc04), `live_enabled=false`.
- Open: run M1 with `--raw-books` before deciding on hash enforcement; Operator choice on netting realized
  gains (AR); the two-batch Kelly rule and cumulative-loss disable (need Poly反思's numbers); verify live
  Gamma `events` id / slug in an M1 run. Queue empty after BH.
