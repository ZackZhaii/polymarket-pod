# Paper loop handoff

Use this when a Grok Bot chat gets long. Start a new chat. Do not carry the old transcript.
Read this file, `docs/PHASE0_RUN.md`, and `config/risk.toml`. Then do one task below.

Phase 0 work lands on `dev` (branch from `origin/dev`; PRs use base `dev`; Codex may prepare dev→main PRs for human review and merge). Integration roles and author recusal follow [CODEX_COORDINATION.md](CODEX_COORDINATION.md). Scanner, public book, and gated paper ledger already run.
`live_enabled` is false. Do not change that. Do not read `.env`. Do not place orders.

## How to split chats

One chat, one task, one finish line. Do not start a second task in the same chat.
When the diff is committed, write five lines at the bottom of this file under `## Latest` and stop.
Branch from `origin/dev`; pull requests target `dev`. Ordinary `dev` PRs are integrated by an authorized Codex seat that did not participate in their implementation, per [CODEX_COORDINATION.md](CODEX_COORDINATION.md). Codex may prepare `dev` -> `main` pull requests; humans review and merge them. KEY/HOLD and other reserved matters still require specific human approval.
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

## Task BL — WS recorder telemetry (Poly盘口 follow-ups)

Status: done.

Requested by Poly指挥 from Poly盘口's review of BI / BK. Done by grok-bot on dev@cd4bd52. Logging only: no
window, deadline or threshold changed.

- `ping` event per PING (`seq`, per connection) and `pong` event per PONG (`rtt_ms`; `unexpected` with
  `rtt_ms` null when no PING was outstanding). `ping_latency` summaries unchanged.
- Inbound frames counted: report.inbound_frames (every frame) and report.data_frames (BK's business frames).
- `disconnected` now carries `silence_s`, `data_silence_s` (from subscribe when nothing came),
  `last_inbound_ts`, `last_data_ts` (null: nothing on that connection), `pong_wait_since_ts` (the PING that
  armed AW's deadline; null: no PONG awaited) and the connection's `inbound_frames` / `data_frames`.
- Backoff waits check the wall clock against the monotonic clock as a session does (clock_jump_s): a jump writes
  `clock_jump` (`phase` backoff, `jump_s`, `attempt`) and counts report.clock_jumps_in_backoff; the wait and the
  reconnect go on unchanged. A session `clock_jump` disconnect writes the same event (`phase` session).
- m1_run manifest counts add `pongs`, `inbound_frames`, `data_frames`, `clock_jumps_in_backoff`.
- Tests: `tests/test_ws_telemetry.py`; exact `disconnected` events in `tests/test_ws_silence.py` updated.

## Task DK — Issue #247: m2_runner writes an alert line on KILL (exit code unchanged)

Status: done.

Non-KEY part split from KEY #243, claimed by Poly桥接. Done by grok-bot on dev@9a25e07.

- When `<run dir>/KILL` stops the runner, `iteration` appends one row to the Sentinel's `logs/alerts.jsonl`
  (`sentinel_tick.ALERTS_PATH`, same row shape): `from` / `proc` `m2_runner`, `alert` `kill_file`, `severity`
  `info`, `detail` (the KILL path, `exit_code=0`), `exchange_action` `none`. At most once per stop
  (`RunnerState.kill_alerted`); each start that finds KILL writes its own line.
- Unchanged: KILL still exits 0, no getter is called, recovery is untouched, nothing restarts the runner
  (auto-restart stays KEY #243).
- A failed alert write is `WriteFailed`: exit 4 like a journal / events write, never dropped silently.
- docs/EXIT_CODES.md notes it. Tests: `tests/test_m2_runner_kill_alert.py` (offline).

## Task DM — Issue #293: make m2_runner_clock tests deterministic

Status: done.

Claimed by Poly桥接. Done by grok-bot on NoisePC (dev@ffe32ab).

- Flaky under full-suite load: `test_bad_book_skips_one_signal_and_the_next_signal_can_commit[-6-future_book_ts]`
  and `test_malformed_book_skips_one_signal_without_blocking_the_next[None]` (alone 70/70; under load fail once then pass).
- Cause: `env` captured wall `datetime.now` for runner `clock`, but `paper_ledger.commit_if_allowed` →
  `RiskGate.evaluate(now=None)` reads live `podlib` / `book_staleness` / `wash_detect` datetime; wall drift past
  `INTENT_TTL_S` (or past a synthetic `future_book_ts`) broke the second-signal allow.
- Fix (tests only): fixed `FROZEN_NOW` plus `_freeze_wall` on those three modules in `tests/test_m2_runner_clock.py`
  `env` fixture. No `src/`, threshold, or `config/risk.toml` change.

## Task DP — Issue #199: public_get retries HTTP 429 using Retry-After

Status: done.

Claimed by Poly桥接. Done by grok-bot on NoisePC (dev@043e9ce).

- `src/podlib.py` `public_get`: HTTP **429** is now retryable. It waits the response's `Retry-After` as integer
  seconds (`_retry_after_s`; ASCII digits only, capped at `PUBLIC_GET_RETRY_AFTER_MAX_S` = 30 s) through the
  patchable `_sleep`; a missing, HTTP-date, negative or fractional header falls back to Task K's
  `PUBLIC_GET_BACKOFF_S * 2**n`. 429 counts against the same `PUBLIC_GET_ATTEMPTS` budget as network/TLS errors
  and 5xx; exhausted, it raises `PublicGetError` (Scanner: `network_error`, exit 3, fails closed).
- Unchanged: every other 4xx (400/401/403/404/422 …) is raised at once, even with a Retry-After header; 5xx and
  transient network errors keep the exponential backoff (5xx ignores Retry-After).
- `docs/THRESHOLDS.md` operational row for `podlib.PUBLIC_GET_RETRY_AFTER_MAX_S` (code constant, not risk.toml).
- Tests: `tests/test_public_get_retry.py` (offline; urlopen mocked, `_sleep` recorded). The old case that pinned
  429 as not retried is replaced by the 429 cases.

## Task DO — Issue #237: maker paper buy checked under the commit lock; bids over 1 record a skip

Status: done (PR #310, HOLD until Poly风控 「通过」 on head).

Claimed by Poly桥接. Done by grok-bot on NoisePC.

- `src/maker.py` `quote` buy path takes the paper ledger's `commit.lock` (outer, same order as
  `paper_ledger.commit_if_allowed`), then re-checks inside it: book age (`book_problem`, `MAX_BOOK_AGE_S`), Issue #145
  `pair_problem`, and `opposite_leg_blocked(load_state(..., quotes_path=<actual path>))`. A lock timeout, unreadable
  state, or a state with `ledger_problems` / non-finite ledger rows refuses (`structural_arb_disabled`), nothing written.
- A buy whose `quotes_path` does not resolve to `paper_ledger.QUOTES_PATH` (the only file `commit_if_allowed` reads)
  is refused (`structural_arb_disabled`) before any file is written, closing the custom-file reverse bypass.
- Paper cycle records a skip when the best bids across a market's outcomes sum above 1. A present best_bid / best_ask
  too large for float marks the market `invalid_book_price` (skipped, no crash, no buy); NaN / inf / out-of-range
  prices stay left out (no buy); `tests/test_310_paper_cycle_bad_bid.py`.
- Tests: Poly套利's 8 #237 tests unchanged; `tests/test_310_maker_ledger_fail_closed.py`,
  `tests/test_310_maker_custom_quotes_path.py` (custom file then default, and the reverse),
  `tests/test_310_maker_book_age_in_lock.py` (book 119 s before lock, 121 s after: refused, no quote line).

## Task DR — Issue #305: paper_session pin check first; kill_alert Exception → exit 4

Status: done.

Claimed by Poly桥接. Done by grok-bot on NoisePC (dev@3a31906). Follow-up of Poly风控's #292 / #295 reviews.
Tightening only; pin path change, so Poly风控 must comment “通过” on the head before merge.

- `src/paper_session.py` `run`: the pin check is the first step, through `pins.refuse_start` (like m2_runner /
  paper_cycle / book), before any risk.toml read. A missing or unreadable file, a directory, or a sha computation
  that raises is `reason=risk_toml_pin_drift` with a global halt record and `ok=False` (was an uncaught
  `FileNotFoundError` / `RuntimeError`); no markets / books / sessions write, no getter call.
- The unchanged-file snapshot (`before`), the loaded config and the pin check use one path (`risk_path`, else
  `RISK_PATH`); the config is parsed from the checked bytes (re-read sha must still be pinned, else drift).
- `src/m2_runner.py` `kill_alert`: any `Exception` while building or writing the alert line becomes `WriteFailed`
  (exit 4), not only `OSError`; KeyboardInterrupt / SystemExit pass through. docs/EXIT_CODES.md notes both.
- Tests: `tests/test_dr_305_pin_first_kill_alert.py` (offline).

## Task DS — paper_session reread-then-refuse test; #305 Task DQ leftovers renamed to DR

Status: done (tests + docs/comments only).

Claimed by Poly桥接. Done by grok-bot on NoisePC (dev@a0569a2). Follow-up of Task DR (#315).

- Offline tests for `src/paper_session.py` `run`'s reread-then-refuse branch: the real `pins.refuse_start` passes,
  then the file is deleted, edited off its sha, swapped for a directory, or the reread raises, before
  `_pinned_snapshot` reads it again. Each is `reason=risk_toml_pin_drift`, `ok=False`, one global halt recorded by
  the reread (none by the passing check), no markets / books / sessions write, no getter call, no parse.
- Renamed the #305 leftovers that kept the old letter: `tests/test_dq_305_pin_first_kill_alert.py` →
  `tests/test_dr_305_pin_first_kill_alert.py`, and `Task DQ` → `Task DR` for #305 in `src/paper_session.py`,
  `src/m2_runner.py` (comment) and `docs/EXIT_CODES.md`. Task DQ of KEY PR #314 untouched. No behaviour change.
- Tests: `tests/test_dr_305_pin_first_kill_alert.py` section 3 (offline).

## Task DT — ledger_store.book() post-write check by fill_id, not "last line"

Status: done.

Claimed by Poly桥接 (Poly指挥 ruling on the ledger post-write false alarm). Done by grok-bot on the box.
Ledger fix, so Poly风控 must comment “通过” on the head before merge.

- `src/ledger_store.py` `book`: after the locked replace, our row no longer has to be the last line (another
  process may append right after us). The re-read ledger must verify clean, contain our `fill_id`, and the
  stored row must equal what we wrote field by field (`_post_write_row_problems`). Missing is
  `post_write:not_appended:<id>`; a differing, missing or extra field is `post_write:row_mismatch:<field>:<id>`.
  Fail closed as before; the lock is not widened.
- Tests: `tests/test_dt_ledger_post_write_verify.py` (offline); `tests/test_ledger_concurrent_book.py` unchanged
  and green under repeated runs.

## Task DQ — Poly哨兵 on #309: public_get deadline, /time 429 not retried, in-tick heartbeats

Status: done (PR is KEY + HOLD: Poly风控 and Poly哨兵 both 通过, then 家伟 approves in chat before merge).

Claimed by Poly桥接. Done by grok-bot (dev@3a31906). Follows Task DP (#309 kept, not rolled back).

- `src/thresholds.py` `PUBLIC_GET_DEADLINE_S = 30.0` (M2 pending, re-exported as `podlib.PUBLIC_GET_DEADLINE_S`;
  import check `0 < PUBLIC_GET_DEADLINE_S <= HEARTBEAT_RUNNER_TIMEOUT_S / 4`). `podlib.public_get` bounds the whole
  call (attempts, socket timeouts, Retry-After / backoff waits): each attempt's timeout is clipped to the time
  left, a wait that would end past the deadline is not taken (`PublicGetError` at once). New `deadline_s=` kwarg.
- CLOB `/time` (`TIME_URL`) never retries a 429 (`PublicGetError` after one attempt); 5xx/network still retried.
- `src/m2_runner.py`: `GuardedGetter(after=...)` / `RunnerState.on_get` beat the heartbeat (throttled to
  `HEARTBEAT_RUNNER_EVERY_S`) after every GET inside a tick (the /time check, settlement, each scan page, each
  signal's fee and /book reads). KILL is still read at the start of each tick: at most one tick late (documented).
- `src/m1_run.py`: `clock_check(beat=...)` beats the `m1_run` heartbeat (`starting`) after each asset read.
- Docs: `.grok/skills/api-health-ratelimit/SKILL.md`, `docs/RECONCILE.md`, `docs/THRESHOLDS.md` match the code.
- Tests (offline, fake clock): `tests/test_public_get_deadline.py`, `tests/test_ratelimit_storm.py` (429 storm with
  Retry-After 30: one m2_runner tick and the m1_run start-up each end within 120 s; heartbeat gaps < 120 s).
  config/risk.toml unchanged (0aea23af…bc04), `live_enabled=false`.

## Task DV — calibration: out-of-bounds fill rejects its whole decision; boundary timestamps (#321 follow-up)

Status: done.

Claimed by Poly桥接 (Codex review of #321 at a3b850b). Done by grok-bot on the box. Paper-only, calibration-only.

- `src/calibration.py` `trade_samples`: the issue #70 time bounds (forecast.ts <= fill.ts < closed_ts) are checked
  on every counted fill of the decision, including fills already dropped by another calibration filter, so an
  out-of-bounds fill hidden by e.g. `missing_market_mid` still rejects the whole decision (P1).
- `_parse_ts`: `OverflowError` from the UTC conversion (`0001-01-01T00:00:00+01:00`, `9999-12-31T23:59:59-01:00`)
  returns None, so the row is dropped and counted (`fill_ts_invalid` / `fill_forecast_ts_invalid`), never raised (P2).
- `tests/test_dt_ledger_post_write_verify.py`: the mismatch test asserts the exact
  `post_write:row_mismatch:<field>:<fill_id>` (#323 Codex P3).
- Tests: `tests/test_calibration.py` Task DV section (offline). No thresholds changed.

## Task DU — Issue #316: public_get hard wall-clock deadline (body read included); finite deadline_s

Status: done.

Claimed by Poly桥接 (issue #316, Poly风控 / Codex follow-ups of #314). Done by grok-bot on NoisePC. Tightening only,
non-KEY; Poly风控 must comment “通过” on the head before merge. `PUBLIC_GET_DEADLINE_S` stays 30.0.

- `src/podlib.py` `public_get`: `PUBLIC_GET_DEADLINE_S` is now a hard wall-clock cap on the whole call, connect,
  headers and body read included. Each attempt runs in a short-lived daemon worker (`_attempt`) that the caller
  waits for at most the time left; the worker reads the body in chunks (`_read_body`, `read1`), re-clips the socket
  timeout to the time left before every read and drops a chunk that arrives past the deadline. Past the deadline:
  `PublicGetError` at once (not retried), never partial data or a late OK. Before: a 1 byte/s server returned OK
  after 43 s with deadline 10 s.
- `deadline_s=` must be a finite number > 0 (int or float, not bool): NaN, ±inf, 0, negatives, bools and strings
  raise `ValueError` before any network call (Codex P2 on #314).
- 429 / Retry-After (Task DP), /time 429 not retried and the between-retry deadline checks (Task DQ) unchanged.
- Tests: `tests/test_public_get_deadline.py` Task DU section (offline): a real 127.0.0.1 drip server (ephemeral
  port, own thread, no proxy) for a slow body (Content-Length and chunked) and slow headers, asserting
  `PublicGetError` within deadline + 0.75 s; fake-clock chunk-read checks; NaN/inf/0/negative/bool/str deadlines.
  config/risk.toml unchanged (0aea23af…bc04), `live_enabled=false`.

## Task DW — Issue #329: m2_start boot classifier_errors in the chain row and runs/m2/current.json

Status: done.

Claimed by Poly桥接 (issue #329). Done by grok-bot on NoisePC. Observability only, non-KEY: no selection, threshold or
halt change.

- `src/m2_start.py` `start`: the boot start (empty chain) now calls `boot_selection_detail` and records its
  `classifier_errors` (markets dropped because the boot classifier raised, Task DN) in the `runs/m2-chain.jsonl` row and
  in `runs/m2/current.json`, through the same atomic writes and fail-closed exits (a failed append or current.json
  write still stops the order, exit 5). A later start reuses the chain's `assets`, classifies nothing and records
  `classifier_errors: null`. `CHAIN_FIELDS` stays the required core set, so older rows without the field still read.
  `-DryRun` shows the field in the chain line it would append.
- `classifier_errors > 0` keeps the discard-and-continue behaviour; the selected assets are identical to a run
  without the failing row; no new halt.
- Docs: `docs/PHASE0_RUN.md` (chain row and current.json fields) and the `src/m2_start.py` docstrings.
- Tests: `tests/test_m2_start_assets.py` Task DW section (offline): a row whose `boot_eligible` raises gives
  `classifier_errors == 1` in chain + current with assets and `--assets` identical to the clean run; zero errors give
  0; reboot records null and never re-classifies; old rows without the field still reboot; dry-run shows the field;
  a failed current.json write still stops the boot. config/risk.toml unchanged (0aea23af…bc04), `live_enabled=false`.

## Task DX – Issue #141: reject multi-leg/complete-set signals; close #141

Status: done.

Claimed by Poly桥接 (issue #141). Done by grok-bot on NoisePC. Non-KEY verify-and-close: the
tightening code already landed on `dev` as Task BX (#177); this task re-verifies acceptance on NoisePC
and closes the issue. Proposal item 2 (re-enable conditions) stays unlock-only and is not implemented.

- Already on `dev` (Task BX, PR #177): `src/structural_gate.py` rejects any multi-leg / complete-set
  candidate as `structural_arb_disabled` (ArbBinary YES+NO, NegRiskArb all-outcome, legs >= 2, and
  fail-closed unknown leg counts); records on the bus (`gap_signal: rejected`, `reject_reason`) and in
  paper_cycle skips (never silent drop, never an intent or order). `src/arb_binary.py` / `src/neg_risk.py`
  mark every gap the same way. Markets past `endDate` (`expired`) or with non-empty
  `umaResolutionStatus` / `umaResolutionStatuses` (`market_expired_or_resolving`) get no gap path
  (BookWatcher skip + `resolving_market` skip); scanner applies the same UMA predicate every scan.
- Not in this PR (and not required to close #141): event-group completeness as a re-enable gate, or a
  <=2s cross-leg `book_ts` skew cap – those are unlock criteria only.
- Tests re-run on NoisePC (offline, `--basetemp` outside the repo): `tests/test_structural_arb_disabled.py`,
  `tests/test_structural_gate_gaps.py`, related paper_cycle / sell-set rejects; full `python -m pytest`;
  `python scripts/guard.py --require-git`. config/risk.toml sha unchanged (0aea23af…bc04), `live_enabled=false`.

## Task EA — Issue #245: EXIT_CODES.md aligns with m2_runner / secret_scan reality

Status: done.

Claimed by Poly桥接 (issue #245). Done by grok-bot on NoisePC. Docs plus the registry checker, non-KEY: make
`docs/EXIT_CODES.md` match the code without changing thresholds, risk.toml, or behaviour.

- `src/m2_runner.py`: exits 2 / 3 / 7 return from `main` before `_run` creates the Heartbeat, so
  Sentinel never observes them. Their Sentinel column is `不可见` (`PRE_HEARTBEAT_EXITS` in
  `src/exit_codes.py`); on-call finds 2 / 7 via m2_start exit 5 and
  `runs/m2/start-logs/<stamp>/m2_runner.log`, or the supervisor exit code (availability blind spot).
  Exit 3 is a normal refusal and must never write an m2_runner heartbeat. Drop the inaccurate
  "before any read" claim; tick order is preflight → settlement (may read Gamma) → clock → daily
  loss / disk → scan → ingest/seal → decide → commit (Task EC).
- `src/secret_scan.py`: exit 2 covers unenumerable directories (`ScanError`, #53 / #270) and a bad
  BIP39 word list; exit 1 is intentional `EXIT_SECRET` (not an uncaught crash).
- `src/m1_run.py`: 24 h → 6 h (`M1_RUN_TARGET_S=21600` / `M1_MIN_RUN_S=21540`, #171; also Refs #220).
- Tests: `tests/test_ea_exit_codes_docs.py` (5 offline cases); `tests/test_exit_codes.py` (28) still green;
  full pytest at merge head bfb454a 5609 passed, 0 failed. Merged as PR #338 (247af98).
  Full pytest + `python scripts/guard.py --require-git`. config/risk.toml sha unchanged
  (0aea23af…bc04), `live_enabled=false`.

## Task CI — PR #195 [KEY]: leg marks need only the side the leg trades into

Status: done (merged c8f796d after 家伟 approved "批准 #195"; registered here by Task EC).

- `src/leg_marks.py` `liquidation_mark`: a long needs only usable bids, a short only usable asks. An empty or
  broken opposite side no longer makes the leg `mark_invalid`; `crossed_book` only when both sides are usable.
- Loosens #192's valuation, so KEY. `tests/test_leg_marks.py` covers both directions.
  config/risk.toml unchanged (0aea23af…bc04), `live_enabled=false`.

## Task EC — follow-ups from the #338 / #336 reviews

Status: done. Claimed by Poly桥接 at Poly指挥's request. Docs and tests only, non-KEY.

- `docs/EXIT_CODES.md`: m2_runner tick order is scan → ingest/seal → decide → commit (`_scan`, `_ingest`,
  `_decide` with `commit_if_allowed` inside); m2_start reports exit 5 as soon as it sees m2_runner exit
  (`wait_for` polls `handle.poll()`), without waiting for the start timeout.
- `docs/loop/state.json`: the EA entry gives the real test counts and names the `src/exit_codes.py` change.
- `tests/test_public_get_deadline.py`: the TLS fixture skips cleanly when the openssl CLI is missing or fails.
- `tests/test_calibration_mid_kelly.py` (Codex 6179446901 P2): the operator CLI test stubs `paper_ledger.bus`
  and asserts the repo's `logs/bus.jsonl` is untouched.
- Registers Task CI (#195). config/risk.toml unchanged (0aea23af…bc04), `live_enabled=false`.

## Task ED — Issue #138: m1_rest heartbeat error_streak + Sentinel rest_error_streak

Status: done (PR #343, merge b766a2a). Claimed by Poly桥接 as Task ED (issue #138). Non-KEY: monitoring tighten only.

- `src/m1_run.py` RestSampler: each sample round tracks consecutive all-asset failures as
  `error_streak` (one success clears); the m1_rest heartbeat `progress` includes `error_streak`.
- `src/thresholds.py` / `docs/THRESHOLDS.md`: `REST_ERROR_STREAK_ALERT = 3` (M2 pending).
- `src/sentinel_tick.py`: `error_streak >= 3` → warn `rest_error_streak` (latched; re-arms after
  clear); wrong type (non-int / bool) → global halt `heartbeat_malformed:m1_rest`
  (sev1, fail closed; not just a warn). No risk.toml change.
- Tests: RestSampler streak + clear; Sentinel alert once / re-arm; malformed streak. Closes #138.


## Task EE — follow-ups to #343 (Task ED)

Status: done. Claimed by Poly桥接 at Poly指挥's request. Docs, lint and tests only, non-KEY.

- PR #343 / LOOP / THRESHOLDS wording: a non-int / bool `error_streak` is a global halt
  `heartbeat_malformed:m1_rest` (`sentinel_tick._rest_error_streak`), not only a warn.
- `src/sentinel_tick.py`: F541 (f-string without placeholders) in the malformed-streak halt.
- Tests: a stop-seal mid-round leaves `error_streak` unchanged and publishes no heartbeat;
  a heartbeat without `error_streak` counts as 0 (no alert, no halt). No threshold value changed.

## Task EF — Issue #350: fees._finite fails closed on 10**400 (no OverflowError)

Status: done (codex-local, 2026-10-16). Registered by Poly指挥 (Risk gaps backlog #46, pure code). Non-KEY, non-HOLD. Registration baseline ab7739a; implementation baseline 269e053.

- `src/fees.py` `_finite` returns None on huge ints (±10**400); `fee_per_share` returns None when fee_rate,
  market_price or effective_price overflows. `ledger_store.verify` reports a problem and `paper_ledger.load_state`
  returns `daily_loss_usd=None` with `ledger_problems` (invalid_risk_state); nothing crashes.
- Tests and acceptance: issue #350 comment 6206469807 (20 strict xfails + control). Boundary: src/fees.py (+ ledger
  store / paper ledger only to catch this overflow), new tests. Full pytest + guard 12/12.
  config/risk.toml unchanged (0aea23af…bc04), `live_enabled=false`.
- Implemented: fees._finite catches conversion OverflowError; ledger_store rejects overflow in the three fee inputs
  as invalid_fee, preserving the existing invalid-price notional fallback. paper_ledger needs no change.
- The draft cases are 21 normal assertions with a complete conservation snapshot and exact invalid_fee reasons.
  Focused suite: 347 passed. Final released-state full suite: 5638 passed, 8 skipped, 9 existing xfailed, 0 failed.
  First full run's sole failure (active claim against released-state assertion) is retained in the handoff evidence.

## Task EG — Issue #349: three settlement-blocking prefixes actually written

Status: done (PR to dev from task-eg/settlement-market-halts). Claimed by Poly桥接 as Task EG (issue #349).
Registered by Poly指挥 (Risk gaps backlog #47, pure code). Non-KEY, non-HOLD: tighten only. Baseline ab7739a.

- `settlement_result_changed`, `settlement_conflict`, `invalid_settlement_payout` are in the blocking prefix tuple
  but no `halt(` ever writes them. Write a scope=market, blocks_settlement halt (global halt kept). The three prefixes
  stay and `blocks_settlement` keeps returning True for them; removing or weakening one is a risk change (Poly风控 +
  家伟), not this task. Existing rejection tests (`tests/test_runner_settlement.py`) stay unchanged.
- Tests and acceptance: issue #349 comment 6206470263 (4 strict xfails + control; correction comment below it). Boundary: src/settlement.py, new
  tests. Full pytest + guard 12/12. config/risk.toml unchanged (0aea23af…bc04), `live_enabled=false`.
- Done: `src/settlement.py` `plan` now also returns `Plan.market_halts` (market, reason): `settlement_conflict:*`
  (two confirmed snapshots of one condition disagree, or a token in two conditions) for every ledger market on the
  condition's tokens / settled from it; `settlement_result_changed:<market>|<token>` (new check: a booked settlement
  row whose confirmed snapshot now names another condition, winner or payout; nothing is settled). `invalid_payout_halts`
  flags a booked settlement row paid outside {0, 1} (`invalid_settlement_payout:<market>|<token>`; ledger verify
  still refuses it: global invalid_risk_state as before). `record_market_halts` writes them with `sentinel.halt`
  scope=market (no duplicate of an active one); it never lifts or replaces a halt.
- Minimal hooks outside the boundary (needed to write the halts where the problems arise):
  `paper_ledger.apply_settlements` records the planned market halts before returning the problems (the callers'
  global invalid_risk_state is unchanged) and the invalid-payout halts when the ledger does not verify;
  `m2_runner.preflight` records the invalid-payout halts after its global ledger halt. Prefix tuple unchanged.
- Tests: `tests/test_349_settlement_blocking_prefixes.py` (#349 draft without xfail markers + per-prefix end to
  end: global and market halt; a global resume alone leaves the market blocked while other markets settle; the
  market settles after its own market halt is resumed; commit path; token in two conditions; clean control).
  `tests/test_runner_settlement.py` unchanged.

## Task EI — C0: isolated strict execution record contracts, raw JSON decoder and canonical hashes

Status: done (normal completion/release proposed at 2026-11-03T07:32:43Z). Product #373 head43547b8104f712a36bf4014b9c536bc6b1bad2d0 was independently integrated into actual dev96c8f618efd247700b2968a7aa36bb228c4588a2 after current-head full CI and non-author QC passed. This branch only proposes normal LOOP/state completion; actual queue finality still requires non-author integration/readback. The original one-hour claim was not renewed or backdated; product work was frozen within its valid window. No new phase/runtime/trading authority is opened.

- Input authority and seven byte-bound GitHub-readable snapshots: [C0 packet](superpowers/plans/2026-10-25-execution-s0/README.md). README routes historical paths and records fresh approval; snapshot bytes stay unchanged.
- Future exclusive files: `src/execution/__init__.py` (empty, no imports), `src/execution/contracts.py`, `tests/execution/test_contracts.py`, `tests/fixtures/execution/contracts.json`, plus normal LOOP/state completion. Only Record/ValidationError/validate_record/canonical_sha256/decode_record; strict 14 kinds/nested unions, deep copies, zero IO, raw duplicate-key rejection, bounded finite types, noncircular hashes.
- Future acceptance: all seven named C0 tests/parameter sets execute without skip/xfail, full pytest + guard --require-git, schema/fixture SHA freeze. This registration implements none of C0 and claims no C0 test results.
- Later P/T/L/A/S/E/V are not registered or authorized to start by this PR. No live/SDK/account/funds/runtime authority. config/risk.toml unchanged, live_enabled=false; runtime/real orders NOT_RUN, profitability UNKNOWN.

## Task EJ — CI: exact queue metadata fast lane with unconditional Guard

Status: done (normal completion proposed at 2026-11-03T08:53:03Z). Product #377/1148f5b was independently accepted into actual devc17f99a16e3e0058542fcfdb729b93af9b8a4e3e after full current-head CI/Guard and approved-patch byte verification. This normal metadata PR and its merge push provide native fast-branch observations; final adoption/report remains PENDING until both actual results and target dev are read back. No branch done label substitutes for those results; no runtime/main/trading authority.

- Registration test maintenance: tests/test_loop_claim.py previously assumed no task can follow EI. Preserve all historical closed-task assertions and synthetic EI phases, explicitly require every task before EI to stay done, reject historical F accidentally todo/doing, allow the actual later task and its matching sole1h claim, and cover a later task in todo/doing/done. No loop_claim implementation or approved CI patch byte changes; this registration includes tests and therefore requires full CI.
- Exact approved patch SHA256: df8601357adc1341e0c74d63d38ebd96e4f26f2cc92f87654f1d017d6a10b76b. Future product write domain is only .github/workflows/ci.yml, plus normal LOOP/state lifecycle. The registration PR publishes the byte-exact approved patch and key evidence; local paths are not remote delivery.
- Fast path: nonempty whole PR merge-result or whole push diff, all raw paths exactly docs/LOOP.md or docs/loop/state.json. Guard always runs; product/config/tests/CI/other/uncertain scope runs full pytest. Keep check identity, pinned actions, readonly contents and persist-credentials:false; no new dependency, event, service or account route.
- Shared acceptance: 35 actual Git classifier cases and24 outcome/output combinations passed and independently reviewed; output=true followed by failed scope must choose full. First implementation itself needs exact-head full CI/Guard; actual metadata-only PR and push must demonstrate fast/Guard success/full skipped before claiming native adoption. Record actual elapsed times, failures and UNKNOWN usage; do not claim theoretical savings are measured.
- Writer/controller: Codex01a19d50-3e49-7bd1-9af3-c3d664c94c51, surface codex-local. Non-author final integrator: existing Codex01a15d32-5ffb-7373-8ca9-d94e8e1f04ca (actual role ACK), which must not implement CI or registration. Same acceptance for writer/reviewer; no selfmerge. Normal1h budget only after visible accepted claim; safe freeze before expiry, no --now/automatic renewal.
- Human approval covers this specific CI/KEY patch through normal gates only. Original risk SHA/nine keys, paper/live=false/human gate and all other KEY/HOLD/runtime boundaries remain. No M2 runtime, SDK/account/funds/trading or main release authority.


## Task EK — M2 first slice: strict offline input evidence inventory

Status: done (normal completion proposed at 2026-11-03T13:32:28Z; accepted product PR #383 merged into actual dev ff0a7cc66552668d30b66c563e1d1b9649eb0bc8; completion/claimnull becomes global only after non-author integration of this metadata PR).

Historical claim status retained: Status: doing (new explicitly dispatched normal EK claim proposed, 2026-11-03T12:26:07Z to 2026-11-03T13:26:07Z; 2026-11-03 21:26:07 JST to 2026-11-03 22:26:07 JST). #381 abort was independently integrated into actual devea9288658f555539758f7bba83faec10638daf4d; EK was sole todo/claimnull. Old expired attempt, late-test violation and unaccepted candidate remain preserved. This is not automatic renewal: product remains CLOSED until this normal claim is independently integrated, actualdev unexpired fullclaim read back and a new explicit OPEN. Safe freeze 2026-11-03 22:21:07 JST; no --now/claim_hours change.

- Deliver a real read-only inventory_inputs callable in src/m2_report.py with small fixture/tests, accepting explicit local paths and JSON/JSONL formats. Return each source/format/rawSHA256/bytes/records/problems; hash and parse the same raw bytes, keep bad-readable-file fingerprints/diagnostics and null unknown fields. Overall status is always NOT_EVALUATED; completeness INVENTORIED/INCOMPLETE/INVALID is input integrity, never M2 PASS, uptime, score or profit. Missing/empty/damaged evidence cannot become PASS; partial valid JSONL rows cannot masquerade as a complete denominator.
- Strict UTF-8, arbitrary-depth duplicate-key rejection, NaN/Infinity and parsed overflow/non-finite rejection. JSONL requires complete JSON-object lines and final newline, rejecting blank/truncated lines without silently skipping. Preserve request source; repeated path, dot-segment aliases, existing samefile and hardlinks cannot count as independent inputs; same content in distinct files is not automatically an alias. No external host/account authentication.
- Call leaves all input bytes/mtime unchanged; import performs zero IO. Use owned small JSON/JSONL fixtures, independent expected hashes and direct empty/missing/duplicate/non-finite/bad-encoding/partial-line/alias controls. Do not read .env/account files, default runs or actual heartbeat/ledger paths; no directory discovery/network/subprocess/runner or writing output.
- Unique product owner/author controller: Codex01a15d32-5ffb-7373-8ca9-d94e8e1f04ca, surface codex-local, personally responsible for implementation. Future exclusive absolute paths (not created by registration): D:/projects/polymarket-pod/.worktrees/m2-input-inventory-20261103/src/m2_report.py; D:/projects/polymarket-pod/.worktrees/m2-input-inventory-20261103/tests/test_m2_report.py; D:/projects/polymarket-pod/.worktrees/m2-input-inventory-20261103/tests/fixtures/m2_report/. Normal LOOP/state is the same controller's separate metadata worktree; no other writer owns these files.
- Shared packet FIRST_WORK_ORDER.md SHA256a787a87b127749331651d0cdc053a56d80926dc4afa4c28b063638587c463e83; PRE_M2_DISPATCH.md SHA25668c2315b7cd9198515ffc57e342eb8bbb56504fa7a340e9c3cf0ac31325e3a02; READER_REUSE.md SHA256e4b45986d8b914b53f67bd1430b65145984e45f642df87b84117478bc69b3211. Required acceptance and exact shared packet are published in the registration PR; D-drive paths are not remote-readable delivery. Existing calibration/podlib/C0 readers have different contracts, not newly alleged business defects; no refactor of those modules.
- Acceptance/lease: after actual enqueue, only first todo and one normal globally visible1h claim plus fresh readback/explicit OPEN. Estimated coding20-35min/targeted+Guard10-15min are not guarantees; 1h includes wall time and freeze5min before expiry, no --now/auto-renew. Necessary targeted+Guard once, freeze exact head/stop writer; product full CI and real non-author business QC in parallel, preserve prior green evidence and actual failures.
- Non-author coordinating/QC/final integration seat: existing Codex01a19d50-3e49-7bd1-9af3-c3d664c94c51, which writes none of this task source/tests/queue; it arranges separate Astra QC with this same acceptance. The author must not reuse its former EJ integrator role to self-merge this new M2 task. Expected-head integration and actual-dev readback remain required.
- No CLI/explicit output writing/metrics/scoring/collector/backfill/supervisor in this slice; leave all existing business readers/C0/runner/ledger/heartbeat/risk/threshold/dependency/CI bytes unchanged. #121/#122 running and #342/#347/#352/#354/#363 applicable blockers stay separate; no M1/M2 startup/system/main/live/SDK/account/funds or trading authority. Paper/live=false/human gate and risk originalSHA remain unchanged.

## Task EL — M2 second slice: single-manifest declared window

Status: done (normal completion proposed at 2026-11-03T14:55:00Z; accepted product PR #387 merged into actual dev d92639711f1bd6191ff37627337852c247e2d1fc; global done/claimnull only after non-author integration/readback of this metadata PR).

Historical claim status retained: Status: doing (normal codex-local/EL claim proposed 2026-11-03T13:57:13Z to 2026-11-03T14:57:13Z; safe freeze 2026-11-03T14:52:13+00:00. Product CLOSED until this claim is independently integrated and actualdev unexpired fullclaim/risk read back with explicit PRODUCT OPEN; no --now/renew/claim_hours change).

Historical registration status retained: Status: todo (human-directed registration proposal only; no claim, product tree or implementation; global enqueue only after independent dev integration/readback).

- Actual author/controller/sole implementation owner: Source Codex01a15d32-5ffb-7373-8ca9-d94e8e1f04ca, codex-local; role ACK published PR384comment6605507753. Source owns this registration/claim/product/release and cannot selfmerge. Root01a19d50-3e49-7bd1-9af3-c3d664c94c51 writes none of this task source/tests/queue, coordinates true non-author Astra using identical acceptance and performs final expected-head dev integration/readback.
- Shared SECOND_WORK_ORDER.md raw5652bytes SHA256cbc8a2e2db220146f184b34c990af65865ea70b5b80f472e7d20e8275e2dd54f; registration PR publishes necessary criteria and original byte packet/base64/SHA for every seat. SECOND_SLICE_PREP.md SHA9d12871e066e7b54753a607f42424d7555925c95152d4b9998bc8b99a6b5adfd is historical preparation only, not current queue/authority.
- Future exclusive absolute product paths, verified absent and not created by registration: D:/projects/polymarket-pod/.worktrees/m2-manifest-window-next/src/m2_report.py; D:/projects/polymarket-pod/.worktrees/m2-manifest-window-next/tests/test_m2_report.py; D:/projects/polymarket-pod/.worktrees/m2-manifest-window-next/tests/fixtures/m2_report/manifest-window/. Normal LOOP/state is Source-only in separate metadata worktrees. Minimum reuse/API frozen by author in docstring/tests after legal OPEN; no m1_run/book_staleness/m1_report/C0/runner/ledger/heartbeat/risk/thresholds/dependencies/CI/launcher changes.
- Condition1: real callable for one explicit authorized local m1_run/1 JSON manifest only. Trace source/format/rawSHA256/bytes/records/problems/input integrity. SHA, strict parse and declaration facts use one captured raw byte string, never inventory-then-reopen. Preserve EK inventory_inputs public contract/old68 semantic coverage, UTF8/nested duplicate/non-finite/overflow/damaged-nullrecords/file-change/UNC-before-anyFS/env/alias/immutability/import-zero-IO boundaries. No directory discovery, actual runs/accounts/heartbeat/ledger/network/process access.
- Condition2: only correct-schema object declarations yield original schema/status/started_at/ended_at fields. status=completed/exit_code=0 remain unverified declarations, never proof of actual success. Wrong schema/root cannot impersonate a recording; input integrity and declaration-field problems are separate and unknowns remain null.
- Condition3: only timezone-aware string endpoints normalize to UTC. Valid complete end>=start yields integer microsecond declared_interval; equality gives0, never PASS. Missing/invalid/naive/date-only/non-string/reversed endpoints have explicit problems/null. Never fill from elapsed_s/progress.last_flush_at/plannedm2_end/current.updated_ts/journal/filemtime/now. Invalid or incomplete raw evidence publishes no trusted window facts.
- Condition4: total status always NOT_EVALUATED; metrics/denominator/actual healthy-online duration null/uncomputed. Declared elapsed/endpoints are not continuous healthy coverage. No uptime/score/70/99%/95% new line, M2PASS, account/host authentication, CLI/output-writing/multi-run/chain/collector/backfill/supervisor/runtime.
- Condition5: independent literal control start2026-11-03T09:00:00+09:00/end2026-11-03T00:02:00Z =>00:00-00:02UTC/120000000us plus independent rawSHA; zero length; running/missing-end with fallback bait; naive/date-only/non-string/reversed/wrongschema/badsource/single-read binding; UNC zeroFS/local positive and old inventory compatibility. Purposeful counterexamples, no implementation-derived expectations or broad re-audit.
- Gates: this registration must merge into actualdev before first-todo normal1hclaimPR; non-author full valid claim/risk readback and explicit PRODUCT OPEN precede product tree/implementation. ActualUTC/hard stop at every product write/test entry, freeze5min before expiry, no --now/renew/claim_hours changes. Engineering20-35min+targeted/Guard10-15min estimates only, lease includes wall time; insufficient budget means STOPPED/HOLD. Freeze exacthead/API/clean/STOPPED then current-head fullCI and true non-author QC parallel; reuse unchanged proof explicitly, no duplicate oldfull/C0 baseline. Immutable freeze manifest/publication receipt separate. Accepted product then normal actual-time done/release PR/non-author integration.
- Human2026-11-03 continued ordinary offline M2 tools authorization; not bot-created out-of-backlog work. EK completed at actualdev10ff78bce821a44f70aca7c2c8c347e7ceeef951, no redo. risk original9keys/SHA0aea23af1e938d330b5b4fab2c6abdec9f475f16807d177a5860a4da32aebc04 and paper/livefalse/human gate remain. #122/runtime blockers/main/KEY/Windows/oncall/threshold/account/SDK/funds/trading gates not released; no certain T0/date/profit,168h/14d not compressed.

## Task EM — M2 forward first slice: explicit heartbeat snapshot declarations

Status: done (normal completion proposed at 2026-11-03T16:12:45Z; accepted product PR #391 merged into actualdev 9b606ffa6c122031f3fda6da0eb07397daca37fe; global85done/claimnull requires this metadata PR non-author integration/readback).

Historical claim status retained: Status: doing (normal codex-local/EM claim proposed 2026-11-03T15:23:46Z to 2026-11-03T16:23:46Z; safe freeze 2026-11-03T16:18:46+00:00. Product remains CLOSED until non-author integration plus actualdev complete validclaim/risk readback and explicit PRODUCT OPEN; no --now/renew/claim_hours change).

Historical registration status retained: Status: todo (human-directed registration proposal only; not a global queue entry until non-author dev merge/readback; no claim/product tree/implementation).

- Source Codex01a15d32-5ffb-7373-8ca9-d94e8e1f04ca/codex-local actually ACKed author/controller/sole personal implementation responsibility at PR388comment6607559516. Source owns registration/normalclaim/product/publication/release and cannot selfmerge/reuse former EJ integration identity. Root01a19d50-3e49-7bd1-9af3-c3d664c94c51 coordinates identical acceptance/real separate Astra QC/non-author final expected-head dev integration, with root/children writing none of this task source/tests/queue. Do not revert others' edits.
- Shared current work order FORWARD_FIRST_SLICE_BOUND.md raw9354bytes SHA256901bb30aba825d7e21f757b0c97a0ffa026bc4407f3c994beb2e8a35d848bc60, all five groups below. Registration PR publishes full text/exact base64/bytes/SHA with URL, not just a D-drive path. Original PREPARED/NOT_REGISTERED headers/old pointers remain history, not global state/authority; adopts this bounded packet under prior specific human continuous M2 offline construction scope.
- Future exclusive product domains verified absent and not created here: D:/projects/polymarket-pod/.worktrees/m2-heartbeat-declaration-next/src/m2_report.py; D:/projects/polymarket-pod/.worktrees/m2-heartbeat-declaration-next/tests/test_m2_report.py. Only2files, small tmp_path fixtures, no external fixture directory/CLI/output/docs project. Source-only normal LOOP/state lifecycle stays in independent metadata trees. API/docstring/key shape frozen by author within common conditions after legal OPEN.
- Group1 valid declaration: one explicit local JSON heartbeat, candidate summarize_heartbeat_snapshot(spec, *, expected_proc), preserving exact input rawSHA/bytes/records and original full protocol object when valid. Legal extra fields, nullable git/risk/live identities, UNKNOWN, paused, finished+exit_code stay unchanged. Top status always NOT_EVALUATED with null metrics/denominator/healthy_online_us, never online/healthy/PASS.
- Group2 shape/proc: invalid format or missing/invalid expected_proc explicit nonempty-string requirement rejects before source IO, no defaults/JSONL/list/rawrecord or SHA overrides. Missing required fields/wrong types/naive or invalid timestamps/finished without exit/proc mismatch/nonobject cannot publish valid declaration; readable input evidence is retained separately. New/future ts/running/exit0 never proves health.
- Group3 strict source: reuse accepted m2_report._one(spec,0,{},{},formats=(json,)) capture and existing heartbeat.validate_record for complete document; strict nested duplicate keys/NaN/Infinity/overflow/UTF8/missing/empty/filechange/unknown identity/env/UNC/alias boundaries remain. Bad or incomplete raw gives null declaration; valid UNKNOWN/null is never invented or filled as normal. Do not edit heartbeat.py or the two existing public m2_report contracts.
- Group4 same bytes/no rewrite: input SHA and protocol declaration from the same captured raw and strict parse, no inventory-then-reopen or reserialized hash. Independently computed raw fixture SHA and unchanged original bytes/mtime; input integrity/problems separate from declaration/problems. File declaration/hash does not mean historical raw has been archived/replayable later. Reuse unchanged EK/EL proof explicitly, no broad old investigations.
- Group5 no runtime effects/compatibility: module import/API do not read marker/risk/accounts, instantiate Heartbeat, call read_beat/risk_identity/git_sha/beat/network/subprocess/sleep, write outputs/heartbeat/locks/ledger or scan directories. Reuse validator minimally (e.g. local import), only tmp_path fixtures. Existing inventory_inputs/summarize_recording_manifest public contracts unchanged; current-head necessary fullCI/Guard and true nonauthor QC must finish, not substitute old green for new code.
- Stage gates: actualdev enqueue/firsttodo -> normal visible valid1hclaim -> nonauthor completeclaim/risk readback -> explicit PRODUCT OPEN before product tree/implementation/tests. Target12-18min implementation+6-8min targeted/Guard+3-5min preflight,21-31min freeze estimate only; real1h includes wall time, freeze5min before expiry and actualUTC gate every product write/test start. Budget insufficient means truthful STOPPED/HOLD, no --now/renew/claim_hours/backdate. Freeze exacthead/API/clean/STOPPED; nativecurrentheadfull and trueAstra parallel, immutable freeze manifest/publication receipt separate, normal actual-time release after acceptance by author PR/non-author integration.
- Current actualdev928009976741e0e15e1325b9dc4087f848bf3a24 confirmed84done/claimnull/no todo before registration. EK/EL not reopened; no old C0/EK/EL/Windowsfull/349/92issue rerun. No sampling/30s history/archive/slots/missing-samples/denominator/fetch/collector/supervisor/health-age/hostauth/threshold work in this slice, no claim of completed forward observation. risk original9keys/SHA0aea23af1e938d330b5b4fab2c6abdec9f475f16807d177a5860a4da32aebc04/paper/livefalse/human gate unchanged. main/KEY/runtime/system/Windows/oncall/threshold/account/SDK/funds/trading remain their separate gates.

## Task EN — Issue #365: fee_source overflowing integer conversion

Status: todo (user acceleration registration proposal only; non-author current-task role ACK and actualdev enqueue/readback required; no claim/product tree/implementation).

- Source01a15d32-5ffb-7373-8ca9-d94e8e1f04caactually ACKs sole personal author/controller for registration/claim/product/release, cannot selfmerge. Proposed genuine non-author Root01a19d50-3e49-7bd1-9af3-c3d664c94c51 must actually ACK no old365/newtask source/tests/queue authorship and current authorization; history EJ/EM identity is not automatic qualification.
- Human2026-11-04JST A/B/C acceleration explicitly includes365nonKEY. Original approved patch GitHub365comment6335158599 raw7253bytes/SHA655f3ecda22153cf27ad61810df3c93765f6b11725d2e2fdc4977b6e59a9513b; nonKEY approval6341811287. Shared ISSUE365_WORK_ORDER.md SHAacee691ca3950518f8da078689848fc51da01231480ba864013b66c7090770fc, fullpacket/bytes/SHA published registrationPR. Historical72green/oldbase is not currenthead proof.
- Future exclusive absolute domains, verified absent/not created: D:/projects/polymarket-pod/.worktrees/issue365-fee-source-overflow-next/src/fee_source.py; D:/projects/polymarket-pod/.worktrees/issue365-fee-source-overflow-next/tests/test_365_fee_source_overflow.py. Only twofiles; existing fee tests/m2_runner_rig reused unchanged. Source-only LOOP/state separate metadata trees.
- Only production change: catch OverflowError at existing float(value) in _finite_number -> None. Preserve other exception/type/finite/validfee paths and crashed->GLOBAL. No broader except/clamp/threshold/risk/runner/heartbeat/sentinel/fees refactor. Actualdiff decides nonKEY; unexpected broader scope stops reviewable/HOLD.
- Acceptance: direct _finite_number, parse_gamma_fee, FeeCache.get, validresolve, runner skips malformedmarket and unrelatedcrash stillGLOBAL sixgroups. Fresh independent RED/GREEN, no inherited20xfail/newxfail; original tests/compatiblefee regressions plus Guard/current-head nativefull and true nonauthor QC. Only offline ownedtmp/mock fixtures, no actual pipeline/network/.env/keys/ledger/PolyPod tasks.
- De-duplicate365from361 if365 first merges; standalone365 is not blocked by342KEY adoption. Does not start361/364 scan or decide348, no A oldbranch changes/KEYmerge. CurrentEM truly85done/claimnull at 2e48ad2c0df6c08b69cffbb1d951840584c1de24; A writer-stop ACK requests are separate coordination and do not justify bypassing firsttodo.
- Stages: nonauthorACK/enqueueactualdev -> normal valid visible1hclaim/fullclaim+risk readback -> explicitPRODUCT OPEN. ActualUTC each productwrite/test, freeze5min beforeexpiry/no --now/renew/claim_hours/backdate; estimate10-15minimplementation+5-10mintargeted/Guard not guarantee. Freeze exacthead/API/cleanSTOPPED/no pendingwriter, immutablemanifest/receipt separate; genuineacceptance/devmerge then actualtime release PR/nonauthor integration. Originalrisk9keys0aea23af1e938d330b5b4fab2c6abdec9f475f16807d177a5860a4da32aebc04/paper/livefalse/humangate, main/KEY/runtime/account/trading restrictions remain.

## Task EO — #353 strict single-ID Gamma keyset migration

Status: todo. Registration only; no product tree/claim/PRODUCT OPEN.

- Human acceleration353; freshactualdeva6f1194060b6d4b77b4fca00ac1bdd29ca3ed874 verified86done/claimnull after EN365 close. Source01a15d32 actualACK solepersonal author/controller, no selfmerge. Root01a19d50 actualnewEO nonauthornativeACK independentofold353/newsrc/tests/queue and finalordinarydev role; public currenttaskACK to be readback onregistration.
- Sameworkorder raw7885bytes/SHAae819f8dde69c9ad06cbbbb80cbd093ee28691b425b0389d0560d6b0abed336c; original353diff62337bytes/SHA8bebfe1afb06def78dcf6be0ef2214ec7dfa59a589b2af41fa9222eecf547e22, public353comments6341812409/6335159130/6341810909. Old5729green is historical, actualheadriskreview/CI/QC required.
- Futureexclusive producttree D:/projects/polymarket-pod/.worktrees/issue353-keyset-next absent/uncreated. All authorized absolute domains:
  - D:/projects/polymarket-pod/.worktrees/issue353-keyset-next/src/m2_runner.py
  - D:/projects/polymarket-pod/.worktrees/issue353-keyset-next/src/resolution_watch.py
  - D:/projects/polymarket-pod/.worktrees/issue353-keyset-next/tests/m2_runner_rig.py
  - D:/projects/polymarket-pod/.worktrees/issue353-keyset-next/tests/test_353_keyset_spec_vectors.py
  - D:/projects/polymarket-pod/.worktrees/issue353-keyset-next/tests/test_gamma_keyset_353.py
  - D:/projects/polymarket-pod/.worktrees/issue353-keyset-next/tests/test_m2_runner_recovery.py
  - D:/projects/polymarket-pod/.worktrees/issue353-keyset-next/tests/test_resolution_watch_gaps.py
  - D:/projects/polymarket-pod/.worktrees/issue353-keyset-next/tests/test_runner_settlement.py
  - D:/projects/polymarket-pod/.worktrees/issue353-keyset-next/tests/fixtures/gamma_keyset_353/
  - D:/projects/polymarket-pod/.worktrees/issue353-keyset-next/tests/fixtures/gamma_keyset_spec_353/
  - D:/projects/polymarket-pod/.worktrees/issue353-keyset-next/tests/test_349_settlement_blocking_prefixes.py
- Last349test domain is Root nativeacceptedcurrentbase delta353comment6611310081: only5linked m-other fixture/testIDs toconsistentlegalnumeric, retainallGLOBAL/market halt/resume/settlement assertions; no production349/ledger changes. No skip/xfail/weakening. Otheroldliteral in ledgeronlytest stays unchanged/outofscope.
- Preserve strictanynext_cursor-key rejection/ASCII-IDguard/originalstrip semantics, nolegacyfallback/version/structural, resolutionclosedtrue/defaulttwo reads/realbool/duplicate/missing/views reasons; read/classifyfail no newsettlement. No newtransaction semantics forpartialappendfailure. Sharedrig must distinguish scan-vs-id keyset and retainfeeobject/365 unexpectedexceptionGLOBAL controls; fee_source/test365 unchanged.
- Currentbase realRED/newtargeted+necessaryfee/runner/settlement/365compat+Guard thenfreeze exacthead/API/docstrings cleanSTOPPED; Root trueAstra/currentheadfull/riskdiffreview inparallel. Only historicalfixtures/mock-ownedtmp/zeronetwork, no claim ofcurrentAPI/online/profit. Generickeysetcursor docs do not prove per-IDkeyabsence; contract unchanged/actualavailabilityUNKNOWN.
- Firstregistrationactualdev/EOtodo, thennewnormal1hclaim nonauthorvisibleactualdev/riskreadback andRootexplicitOPEN beforeengineering; no oldENlease/--now/renew/claim_hours/backdate, realUTC eachwrite/test/freezebefore5min, ~30mincode+targeted estimate only. InsufficientSTOPPED/HOLD; originalrisk/livefalse/humangate/main/KEY/runtime/realledger/.env/PolyPod boundaries retained.

## Task EP — #345 /time getter exception observations only

Status: done. Only the EP /time getter-observation slice was accepted by actual non-author Root and merged in #403 (dev 1e197d268f303122f44e87d7b7854b311eb3aae6); this does not close whole issue #345. Normal truthful release done_at: 2026-11-03T20:05:50Z.

Historical registration status (retained): todo. Registration only; no product tree/claim/PRODUCT OPEN. The following bullets preserve the original registration plan/history; the current accepted product and review are linked in Latest.

- Source01a15d32 actualnewEPACK personalsoleauthor/controller, no selfmerge. Root01a19d50 actualnewEPnativeACK notoldEO/EJrole: independentofold345/newsrc/tests/queue, QCorganization/ordinarydevfinalintegration; publicRootreadback onregistration. Original87done/claimnull atactualdevc1789cd99b2e2592270e1e94805c41d27ad636bb; dev/main/all4openPRs highestEO/nextEP.
- Sharedworkorder10837rawbytes/SHA5e6a0be5636c843eab8c6af441f7c835bc321745613607af98edf692b2cb09b7 freezes exactcontract/budget/tests/stop. Raw345issue/comments6234368807/6244213873/6268472782/6268473999 retained+hashes, Pythonfence9322bytes/SHAa907a224415606a670fa4fae5233340c79b97753578b8f70b52ea21a3e04d577. Historical25case/17strictxfail notcandidateproof; removeallmarkers andobsoleteabsencebaseline, retainalloriginalbehaviorcontrols.
- Exactly3 futureexclusive domains (tree absent/uncreated):
  - D:/projects/polymarket-pod/.worktrees/issue345-time-observation-next/src/m2_runner.py
  - D:/projects/polymarket-pod/.worktrees/issue345-time-observation-next/tests/test_m2_runner_time_failures.py
  - D:/projects/polymarket-pod/.worktrees/issue345-time-observation-next/tests/test_m2_runner_heartbeat_clock.py
- Externalprogress exactly2newtopkeys: time_check_failures_total:int and time_check_failures_by_class:{425,timeout,5xx,4xx_other,other}->int. Sixnumbers, sumclasses=total, perprocessfresh0/mixedmonotonic/successnotreset/progresscopy. last_time_check oldts/result/low_s/high_s/rtt_s unchanged, onlyaddhttp_status:int|null/exc_type:str|null; nongttererror=null/null, oldmissingUNKNOWNnot0. Do notclaimcrossrestartcumulative.
- exc_type=effectiveexceptiontypename, notouterserialized/nothirdrootfield. PublicGetError explicitcause beforelast_error, URLError exceptionreason beforecause, HTTPError beforeTimeout/URLError; max8knownwrapperlinks/identitycycleguard, noimplicitcontext/messages/stringguessing.425exact,5xx,other4xxincluding429,Timeoutfamilyincl_DeadlineExceeded,otherfallback. GetterException onlyonceperlogicalcheck; localclock/RTT/invalidserver/successnotincrement. Metadatahelpers do notchangecontrol.
- Preserve sent/getter/after/received order, GETcalls/attempts/425-429noextraretry, clock_unverified/skew/anomaly(_since), pausedreasonrecovery andpreflight/settlement order/oldcountervalues. DirectGuardedGetter RefusedURL stillraises; existing_clock_problem catchesgetterRefusedURL toclock_unverified remains. GuardedGetteritselfunchanged; no newhalt/pause/veto/KILL/resume/threshold/alerts/events/retry.
- CurrentbaseRED/newtime+necessaryclock/heartbeat/guard/publicgetcompatibility+Guard; no addedskip/xfail, thirdtestdomainonlystrictnewfieldcompatwithold5values/controlassertsretained. Oldheartbeatread/validator/sentinelunchanged/nozero fill. Freezeexacthead/API/docstring cleanSTOPPED before5minsafe, Rootcurrentfull+truenonauthorCodexQC inparallel; nofixed107/488counts orautomaticoldAstraretry. Notwhole345/book/settlement/writeoff/Gammaclosure, no361/364scan assumed.
- RegistrationactualdevEPfirsttodo -> newnormal1hclaimPR/nonauthoractualfullclaim+riskreadback -> RootexplicitOPEN beforeengineering. OldEOclearednotreused; no --now/renew/claim_hours/backdate, actualUTC eachwrite/test, ~30mincode+targetedestimate only/HOLDifinsufficient. Onlyownedtmp/mockgetter/networktripwire; noheartbeatprotocol/sentinel/podlib/retry/threshold/CI/risk/deps/otherproduction/API/realledger/.env/keys/PolyPod/M1M2/main/KEY/account/trading. Originalrisk0aea23af...bc04/paper/livefalse/humangate retained.

## Task EQ — #345 logical /book getter failures only

Status: done. Only the EQ /book getter-observation slice was accepted by actual non-author Root at 2026-11-03T20:58:04Z and merged via #407 (dev f357e43d2b6f3319124828977fe9ab875e9d243a); not whole #345. Normal truthful release done_at: 2026-11-03T21:00:33Z.

Historical claim status (retained): doing in this proposed normal claim; product CLOSED until non-author dev integration, full valid claim/risk readback and explicit Root PRODUCT OPEN.

Historical registration status (retained): todo. Registration only; no product tree/claim/PRODUCT OPEN.

- New actual Source01a15d32 sole personal author/controller ACK, no selfmerge. Root01a19d50 actual new-task native ACK independent of old345/newbook source/tests/queue; genuine Codex QC organization and ordinary dev integration, public ACK/readback required. Original88tasks done/claimnull at dev 229dddb913e67b7b030ee56d3297945b3a7e9ab7; dev/main/all4openPR fixed heads highestEP/nextEQ. No competing source-owned open acceleration PR.
- Same shared work order 10925rawbytes/SHA 681876affcc071c15d89a68e6aba3465ca74774c62bd1f7e9a8263d9c4be0d8b; issue345body2036bytes/SHA80b36a71abaabf4a56a5a9e7dc470da533ace878c4ce902a163e1069d797fafe, historical general comment6268472782/6176bytes/SHAefdb50698a32ab224950e5572127d18764ced1a2948bccf5638a17717852305e. No dedicated public book draft found; prior EP/time/Gamma passes not book proof. Exact bytes/URLs published on registration PR, not just D: paths.
- Only three future exclusive domains (product tree absent/uncreated):
  - D:/projects/polymarket-pod/.worktrees/issue345-book-observation-next/src/m2_runner.py
  - D:/projects/polymarket-pod/.worktrees/issue345-book-observation-next/tests/test_m2_runner_book_failures.py
  - D:/projects/polymarket-pod/.worktrees/issue345-book-observation-next/tests/test_m2_runner_heartbeat_clock.py (only strict new book progress key; retain original13 semantics/time/control assertions).
- progress adds only book_get_failures_total:int, per RunnerState initially0/monotonic/success-not-reset/read-only; legacy missing stays UNKNOWN, not0/crossrestart. Internal _book_get_observed(state:RunnerState,getter:Callable[[str],Any],url:str)->Any only observes the original injected book getter Exception excluding RefusedURL once then bare raises; BaseException original. _decide signature/control retained, only its existing fetch_depth getter argument adapted; never wrap shared /time globally or add GuardedGetter instances.
- book.fetch_depth already catches get(url) Exception and returns None; outer_decide-only counting would miss failures. book.py unchanged; preserve original None/continue and direct RefusedURL vs fetch_depth broadcatch vs decide direct-reraise entrypoints separately. No extra GET/attempt/after/clock/IO; no count for normalNone/nonobject/emptytoken/noGET/post-get parse/book_ts/localclock/otherpaths. No new classification/lastbook/event/alert/threshold/halt/pause/veto/KILL/resume/control.
- Currentbase realRED/newbookGREEN/no newskip-xfail; real public_get owned fake opener proves attempts-vs-onecount; mixed token failure/success, zero-read/parsing/RefusedURL/BaseException/hooks, real_decide/iteration missingbook and recovery/oldEPtime and actual legacy/new heartbeat-validator-read-Sentinel controls. Twelve necessary existing compatibility nodes fixed in work order; reuse untouched AST/blobs, no EP68/81/375/C0/349/Windowsfull/92issue/six-lane intake repeat. Domain insufficiency -> STOPPED concrete delta toRoot.
- Registration -> ordinary firsttodo new1h claimPR actualdev fullclaim/risk readback -> explicit RootOPEN. Estimate20–30min code+targeted only; actualUTC each productwrite/test and freeze>=5min before actualexpiry, no --now/renew/backdate/claim_hours/oldEPlease. Exacthead/API/docstrings immutable cleanSTOPPED handoff; newhead native full once and genuinely independent Codex QC parallel; nonauthor finaldev accept/freshreadback then normaltruthfulrelease. Metadata59+Guard/exactnativefast only at metadata stages.
- Original9keyrisk0aea23af...bc04/paper/livefalse/humangate unchanged. No book.py/heartbeatprotocol/sentinel/podlib/retry/threshold/risk/CI/dependency/API/real ledger/account/sdk/.env/keys/system/PolyPod/M1M2/runtime/trading/KEY/main changes. #361/#364 actualledger permission/readiness remainsUNKNOWN; only book slice, not entire #345 closure.

## Task ER — #345 settlement logical getter exceptions only

Status: done. Only the ER settlement-getter observation slice was accepted by actual non-author Root at 2026-11-03T22:17:48Z and merged via #411 head f53a850d7eef5660dc45fd8961446e97985dcfad (dev 6e377bb266b6e3df93ae8a861398cf60e7ed60c7); not whole #345. Normal truthful release done_at: 2026-11-03T22:20:22Z.

Historical claim status (retained): doing in this proposed normal claim; product CLOSED until non-author dev integration, full valid claim/risk readback and explicit Root PRODUCT OPEN.

Historical registration status (retained): todo. Registration only; no claim/product tree/product tests/PRODUCT OPEN.

- Actual new Source01a15d32 sole personal author/controller/implementation-lifecycle writer ACK; cannot selfmerge. Root01a19d50 actual newtask native nonauthor ACK, no settlement source/tests/queue participation; public exactpacket readback ACK required, genuine existingCodex QC newtask role separately. OldEQ/EP/EJ rights not reused. Actualdev427a5f0ce6f802f7830ab94c799556366ab974b3 has89done/claimnull; dev/main/all4openPR highestEQ/nextunusedER; Source0openaccelerationPR beforethis one.
- Shared12421rawbyte work order SHA 7b3dc61b85287c046429a7dda31365aa9f88bab5532b8e421cd892c0879e9742; issue345body2036bytes/SHA80b36a71abaabf4a56a5a9e7dc470da533ace878c4ce902a163e1069d797fafe. Root candidate4441bytes/SHAa85002867436524de2bf4dcbef69b79fe4e3045e950b77a0aace17d2b24330fa staticPREPARED_ONLY, imports/tests/runtime0, notimplementation/QCPASS/queue authority. Exact new packet/prep bytes published on registration PR; local paths alone not delivery.
- Future exclusive product domains, tree/newtest absent and uncreated:
  - D:/projects/polymarket-pod/.worktrees/issue345-settlement-observation-next/src/m2_runner.py
  - D:/projects/polymarket-pod/.worktrees/issue345-settlement-observation-next/tests/test_m2_runner_settlement_get_failures.py
  - D:/projects/polymarket-pod/.worktrees/issue345-settlement-observation-next/tests/test_m2_runner_heartbeat_clock.py (only one strict new key, original13book-time/control semantics retained).
- External onlysettlement_get_failures_total:int perRunnerState0/cumulative/successnotreset/read-only; legacy missingUNKNOWNnot0/nocrossrestart. Internal _settlement_get_observed(state:RunnerState,getter:Callable[[str],Any],url:str)->Any callsoriginal once/returns exactresult, RefusedURLnocount/bare raise, othergetterException+1/bare raise, BaseExceptionoriginal; no detail/cause classification.
- Only adapter in _settle_tick collect FIRSTgetterargument; originalgamma still reaches writeoff. SharedGamma/URLs cannot identify settlement; reader already catches getterException and collect.ok=false also has nonGET failures, so no globalwrap/URLguess/percollect counter. First/secondview/twomarketslogicalcounts, realpublic_get retriesonce; getterdecodecounts, normalNone/badschema/missing/HOLD/localclock/history/append/ledgererrors do not themselvescount. No holdings/allblocked/bad-id-only0settlementGET; scan/fee/writeoff/time/book notmiscount.
- Preserve directGuardedGetter RefusedURL0inner-after, originalreader broadcatch-to-failure anddirectouter propagation separately; after order, BaseException/WriteFailed/GLOBAL/marketblock/settlement_skipped/apply/recovery unchanged. No inventedMarketFatal. Preserve real multirowpartialappend prefix onlaterwritefailure; no transaction/rollback change. No newhalt/pause/veto/KILL/resume/event/alert/threshold/classification/lastdetails/retry/networkIO.
- Currentbase realRED/newGREEN/no newskip-xfail; actualcollect/iteration positive/negative, realpublic_get owned fakeopener multiattempt controls, sharedroutes/noGET/nonGETfailures/legacy actualvalidator-read-SentinelUNKNOWN andprogress, actualsyntheticledger via realbook/version/coherentclocks. Workorder freezes12neededexistingcompat nodes, notfixedcount; others reused byAST/blob. No EQ49/27,EP68/81/375,oldC0/349/Windowsfull/92issue/oldbaseline/six-lane intake repeat.
- Registration accept/freshfirsttodo -> normalnew1hclaimPR/fullvisiblevalidclaim-riskreadback -> explicitRootOPEN. Code+targeted20-30minestimate only; fullwallclock inclmetadatawait, actualUTC perwrite/test, freeze>=5minbeforeexpiry; insufficientSTOPPED/HOLD, no --now/renew/backdate/claim_hours/oldEQlease. Frozenexacthead/API/docstrings/byteSHA/cleanSTOPPED handoff; newheadnativefull once+genuineindependentCodexQC parallel, ordinarynonauthor devaccept/freshreadback thennormaltruthfulrelease. Metadata59+Guard/nativeexactfast only here.
- Original9keyrisk0aea23af...bc04/paper/livefalse/humangate retained. No resolution_watch/book/podlib/heartbeat-Sentinel/ledger/threshold/risk/CI/dependencies/actualAPI/realledger/.env/keys/SDK/account/system/PolyPod/M1M2/runtime/trading/KEY/main changes. #361/#364 ledger readiness remainsUNKNOWN/noactualpermission ordirty0assumption. Onlysettlementlogicalgetter slice, notwhole345/collect-failure/fee/writeoff/threshold closure.

## Task ES — #121 forward per-token book fetch completion evidence only

Status: done. Only ES/#121 forward per-token book fetch evidence was accepted by actual nonauthor Root and merged via #415 head f54b1a0059e03e9a1cd96d9b508e1f5fd8cb0ea0 (dev d8fc52d176e94bc45667e68a74cda5be8c494483); not whole #121 scoring/backfill/PIT or M2 runtime approval. Normal truthful release done_at: 2026-11-03T23:19:41Z.

Historical claim status (retained): doing in the proposed normal claim; product was CLOSED until nonauthor dev integration, complete valid claim/risk readback and explicit Root PRODUCT OPEN.

Historical registration status (retained): todo. Registration only; no claim, product tree, code or tests, or PRODUCT OPEN.

- Source `01a15d32-5ffb-7373-8ca9-d94e8e1f04ca` personally ACKed this new task as sole author/controller, implementation and lifecycle writer; cannot selfmerge. Root `01a19d50-3e49-7bd1-9af3-c3d664c94c51` actually native-ACKed the new task without implementation participation; public same-packet ACK and genuine existing Codex new-task QC role remain pending. No ER/EJ role or lease reuse.
- Shared work order: **12611 raw UTF-8 bytes / SHA256 `92089d7bd1de6b8bee4ac7205154546b6446ee623fbd2ad407aca34d3fd26ad0`**. Exact bytes, relevant plan excerpts and immutable handoff will be publicly delivered on registration PR, not by inaccessible local paths alone. Issue #121 body1990bytes/SHA `c1fdfaf946515a7c5d32039e1b957015cbf36e1df2acbfca9825115298f1c772`; excerpt packet2265bytes/SHA `1a84903fd00e8493f1dd5d1b85b7c3c5169577b824e48a820dedfe0f84a0233c`.
- Existing #121 is OPEN/ready/unassigned. Root's 5604byte preparation/SHA `1a9a2caff573f7ad1c318ccebfd6de2f2bfe6773bba0a94a4f669fcf66a42dfb` is historical PREPARED_ONLY, not current queue, permission or QC. NEXT_INCREMENT and LIVE_PLAN are fixed Root artifact snapshots, not Git-tracked files. Actual dev `4ed133808c09af6b7520ea7997e912c1d931ee52` has90done/claimnull; dev/main/all4openheads highestER/nextunusedES; Source0open before this registration. #244/#362 remain separate ready candidates, not reserved/closed by this slice.
- Only two future exclusive absolute product domains (tree and new test absent/uncreated):
  - `D:/projects/polymarket-pod/.worktrees/m2-book-fetch-evidence-next/src/m2_runner.py`
  - `D:/projects/polymarket-pod/.worktrees/m2-book-fetch-evidence-next/tests/test_m2_runner_fetch_evidence.py`
- Freeze one new field on the same existing built row: `book_fetch_evidence[token_id] = {"fetched_at": original_fetched_at.isoformat(timespec="microseconds"), "source": "clob_public_book"}`. Preserve original microseconds and offset, exactly two entry keys, original parent forecast/intent/decision/idempotency association. Keep `_decide` signature and `_stamp` unchanged; add concise contract docstring before GREEN.
- Save the original datetime beside the same accepted `books[token]`; duplicate tokens follow the retained snapshot, partial books record only actual accepted tokens. No extra clock/GET/after/retry/source/events/files; no batchnow/last-token/built-ts/book-ts/current-time substitution. Nonbuilt paths unchanged, missing historical/token evidence remains UNKNOWN and is never backfilled. No assertion that every forecast or all #121 requirements are covered.
- Same acceptance for writer and nonauthor reviewer: true current-base RED/new GREEN; hand-calculated two-token distinct fetch times/full microseconds/original offsets; original clock/GET/after order, actual decision/iteration positive control, partial/duplicate/None/error/stale/pre-resume/nonbuilt/recovery and WriteFailed; only eight necessary existing compatibility nodes named in shared order. Preserve fees/fills/allow-veto/book_age/resume/journal order/counters and noop writeoff. Bounded strict-consumer search found no mandatory third domain; author-side support is not independent QC. Any necessary third-domain/control change stops for a concrete Root scope delta.
- Lifecycle: nonauthor registration acceptance/fresh firsttodo -> separate normal new1h claim PR -> actualdev complete valid lock/risk readback and explicit Root PRODUCT OPEN -> sole writer candidate/clean STOPPED handoff -> current frozen-head native full and genuine nonauthor QC in parallel -> nonauthor ordinarydev acceptance/merge/fresh readback -> separately dispatched normal truthful release. Code/targeted20–30min is an estimate; full1h includes metadata wallclock, actualUTC per product write/test, hard freeze5min before expiry. No --now/renew/backdate/claim_hours/ER lease reuse.
- Registration only existing59+Guard12/native exact metadata fast; no old ER/EQ/EP/C0/349/Windows/full/history or six-lane intake repeats. Frozen manifests never overwritten; actual failure/fixture correction/model usage UNKNOWN retained. Original9keyrisk `0aea23af...bc04`/paper/live_enabled=false/humangate; no risk/threshold/CI/dependency/API/real ledger/.env/keys/SDK/account/system/PolyPod/M1M2/runtime/trading/KEY/main change. #361/#364 ledger readiness remains UNKNOWN; not whole #121 scoring/backfill completion.

## Task ET — M2 returned fee quote evaluation facts only

Status: done. Only ET returned fee quote evaluation facts were accepted PASS_WITH_LIMITS by actual nonauthor Root and merged via #419 head17ec7bec2f13fa90e4bc13da95183bc716766de7 (dev75f6dbf45bee1485d9c5f65c2b40658eb94b961e). Not GET/parse-rate/percentages, whole M2/#122/#345 or runtime approval. Normal truthful release done_at: 2026-11-04T00:28:45Z.

Historical claim status (retained): doing in the proposed normal claim; product was CLOSED until nonauthor dev integration, complete visible valid claim/risk readback and explicit Root PRODUCT OPEN.

Historical registration status (retained): todo. Registration only; no claim/product tree/product code or tests/PRODUCT OPEN.

- Shared final work order **8864 raw UTF-8 bytes / SHA256 `038340090641fdbcf9dca68dc17a150696acf26c2044dd62fbdd5b0e8236ea25`**; exact bytes and frozen handoff published on registration PR. Root [four-domain/error-boundary ACK](https://github.com/ZackZhaii/polymarket-pod/pull/416#issuecomment-6618317065) accepts complete10288byte [scope proposal](https://github.com/ZackZhaii/polymarket-pod/pull/416#issuecomment-6618273600)/SHA `1f2ebd8eff1935d7c0230f51788462a71bc5cb96680d3547ee3ad366c8b77a32`, with raw6063byte prep and relevant NEXT_INCREMENT48–52 excerpt publicly delivered there. Static prep/plans are historical artifact inputs, not Git paths/queue/QC PASS/permission.
- Source `01a15d32-5ffb-7373-8ca9-d94e8e1f04ca` actual new sole author/controller/implementation and lifecyclewriter; cannot selfmerge. Root `01a19d50-3e49-7bd1-9af3-c3d664c94c51` actual uninvolved newtask authorized ordinarydev integrator/QC organizer. Existing nonimplementation Codex actual newQCrole ACK/4groups2spec preparation viaRoot; exactfinalpacket alignment receipt pending,0calls notPASS. Author static support not independentQC; no ES/EJ authority/PASS/lease reuse.
- Actualdev `e83853bfe2404f59138ea9e5641174f11d6d7b52` has91done/claimnull; dev/main/all4openheads highestES/nextunusedET, Source0open beforethis1. #244/#362 remain separate OPEN/ready/unassigned candidates, not formaltodo/closed/reserved. Existing #122 remains KEY; no thresholds/runtime/system/95% adoption or whole #345/M2 closure.
- Four future exclusive absolute product domains; tree absent/uncreated:
  - `D:/projects/polymarket-pod/.worktrees/m2-fee-quote-observation-next/src/m2_runner.py`
  - `D:/projects/polymarket-pod/.worktrees/m2-fee-quote-observation-next/tests/test_m2_runner_fee_observation.py`
  - `D:/projects/polymarket-pod/.worktrees/m2-fee-quote-observation-next/tests/test_m2_runner.py`
  - `D:/projects/polymarket-pod/.worktrees/m2-fee-quote-observation-next/tests/test_m2_runner_fetch_evidence.py`
- Lasttwo only7affected legacy nodes/parameters in common packet: partition EXACT newstep and separately assertcount/position/fields, keep all original strict sequence/index/parent-link/allow-veto/no-commit/WriteFailed and ES built closed-keyset on old projection; no unknownstep blanket filtering. Other early-return emptyjournal/recovery assertions untouched. Bounded static search found no production closed-step consumer; not an executed testfailure or independentQC.
- One original journal call after original `FeeCache.get` returns, before original None branch: `fee_quote_evaluated`, exact9topkeys `ts, step, forecast_id, market_id, iteration, iteration_now, fee_available, cache_hit, quote`. Quote exact7originalattrs `market_id, fee_rate, problem, fee_source, fee_gamma_fetched_at, fee_schedule_sha, cross_checked`; rawvalues retained. Zero fee available/None missing; `cache_hit="UNKNOWN"`. Original batchnow fullµs/offset is not new evaluation/GET time; quote source stamp before underlying read retained oncache, scheduleSHA normalized fee value notrawbytes, cross_checked onlyreturnedflag/notpassedcheck proof.
- One logical returnedquote fact includes positive/negative cache and repeatforecasts, not GET/attempt/unique-market/parse-rate/coverage. Original pre-get exits0facts; original getterException converted to negativequote DOES yield a fact, getitself notreturning0fakefacts. Later book/build/skipped/veto cannot erase a successfully written fact. No extra get/getter/clock/after/retry/privatecache/TTL/feeformula/counter/decision/ES field change; no percentages/backfill.
- Root explicitly accepts new syncjournal failure point as ordinary instrumentation: quote/cache/GET/after mayalreadyoccur; currentforecast missingcounter/skip-evals/books/build/commit notyet and not executed after observation WriteFailed. Original propagation to iteration ACT_WRITE_FAILED/runexit4; no swallow/retry/fallback/newhalt/pause/KILL. Earlier effects/partialbytes possible, no rollback/atomicity/no-new-failure-point claim. Healthy old decisions/oldrelativeeventorder remain.
- Same4groups: real positive/negativecache2facts1GET; earlyexit/get-no-return0; postquote failedbook/build/veto retainsfact; realdecide/healthyiteration/syntheticledger/networktripwire/newWriteFailed preciseboundary. TruecurrentbaseRED/newGREEN+only7affectedoldnodes/necessaryGuard, docstring preserving ES contract beforeGREEN, AST/raw-Git/GREEN→freeze proof. No separateoldES/ER/EQ/EP/C0/349/Windows/full/matrix/intake reruns; later oneexacthead nativefull+genuineQC parallel.
- Registration accepted/freshfirsttodo -> NEW normal1hclaim PR -> actual complete visible valid claim/risk readback + explicit Root PRODUCT OPEN -> solewriter -> frozen cleanSTOPPED/no pending implementation tool-child -> nonauthor devaccept/merge/freshreadback -> separate normaltruthfulrelease. Code20–30minestimate, whole1h includesmetadatawallclock/actualUTCperwrite-test/freeze5minbeforeexpiry. No --now/renew/backdate/claim_hours/oldESlease. Metadata59+Guard/nativefastonly now; immutable manifests/no append and actual failure/precision/harness history preserved.
- Original9keyrisk `0aea23af...bc04`/paper/live_enabled=false/humangate; noKEY/main/runtime/M1M2starts/actualledger361-364/accountsSDKfundssystem/NoisePC PolyPod tasks/.env/keys/threshold/reportscore/failed-route retry. LegacyUNKNOWN/readerNOT_EVALUATED/ledgerreadinessUNKNOWN; not allforecast/PIT/M2 run acceptance. No Owner operation required.

## Task EU — M2 fetch metadata on existing scan events only

Status: doing in this proposed normal claim; product CLOSED until nonauthor dev integration, complete visible valid claim/risk readback and explicit Root PRODUCT OPEN.

Historical registration status (retained): todo. Registration only; no claim/product tree/product code or tests/PRODUCT OPEN.

- Same shared work order **10215rawbytes/SHA256 `f4cf3de6d783a71b4993e5bd1562bba2a332fcf759f13c51ba60f14ba18c1f57`**; exact raworder/input/frozenhandoff publicly delivered on registrationPR, not inaccessible localpaths. Root6241rawbyte prep/SHA `b321fd391645ef4231075c77763a61e120b2fc4aa9b38431ec8bb161e8943ec2` is historical592df/PREPARED_ONLY, not queue/APIapproval/QCPASS; Root artifacts not Gitpaths. Current scanner0a44e19e.../SHA0042d2ad...0979 unchanged; runner24b9fbab... ETdelta outside_scan.
- Source01a15d32-5ffb-7373-8ca9-d94e8e1f04ca actual NEW soleauthor/controller/implementation+lifecyclewriter/no selfmerge. Root01a19d50-3e49-7bd1-9af3-c3d664c94c51 actualnewtasknativeACK/uninvolved authorizedordinarydev integrator-QCorganizer/implementation0. Genuine existingCodex NEWrole/samepacket ACK pending; authorstatichelpers notindependentQC, noET/EJ PASS/role/lease reuse.
- Actualdev e2ce52d822fd7187fa31883b17c421adae3618b1 has92done/claimnull, dev/main/all4openheads highestET/nextEU/source0open beforethisone. #244/#362 remain other OPEN/ready/unassigned candidates, not reserved/closed/currenttodo. New formalpriority/claim/conflict stops this candidate.
- Two future exclusive full domains, tree/newtest absent/uncreated:
  - `D:/projects/polymarket-pod/.worktrees/m2-scan-fetch-facts-next/src/m2_runner.py`
  - `D:/projects/polymarket-pod/.worktrees/m2-scan-fetch-facts-next/tests/test_m2_runner_scan_fetch_facts.py`
- Only nested `fetch` on ORIGINAL `scanned` and `scan_incomplete`, originalevent API/names/oldkwargs/ts/writepositions unchanged; `scan_failed` no fakefetch. Planned pure `_scan_fetch_facts(meta:object)->dict[str,Any]|None`: fresh shallowdict only PRESENT whitelist `pages,fetched,stop_reason,complete,cap_hit,max_markets,server_min_liquidity,base_url`; rawvalues/nullFalse0 unchanged, missingabsent/otherkeysignored. WhitelistSUBSET, not fullsource8keyclaim (optionalerror9th/given7noURL). NondicthelperNone marks observationUNKNOWN only, never controls/normalizes originalbranch.
- Keep original `result.get("fetch",{}).get("complete")` truthiness, stopreasonexpression and try/catch unchanged: missingfetch/{} oldincomplete/None reason, explicitNone-list-scalar fetch or nondictresult originaloutside-tryfailure/no fakeevent. No strictbool/default0/False/success/URL repair; actualrawmetadataonly, no newcollector/validation.
- Originalsuccessadoption/scan_at/scanscount/candidates before scanned, incompletefailurecount/oldscanretained before incomplete, scan_failed originalerror remain. Original candidates/eventOSError->WriteFailed/iteration/exit4 positions and prior/partialeffects unchanged; no addedGET/clock/after/retry/event/file/writepoint/rollback/atomicclaim or changes scanner/pagination/filters/limits/counters/thresholds/ET-ES fields.
- pages=returnedgettercalls notattempts, fetched collectedrows notstrictunique, scanned/passed existingrowcounts notmarketcoverage. completeTrue/given not allmarkets/no-truncation/snapshot proof; pageinternalcap/nocursor staticboundary remainsunfixed/notremotevalidated. No denominator/percentages/95%/reportscore/historybackfill or KEY122 adoption.
- Same4groups: realsyntheticmultipage-duplicates/givenfields; incompletecursor/no_new/cap/networkerror+oldscan; missing/malformed/truthycomplete unchanged; real_scan/healthyiteration/candidate-eventWriteFailed/ownedtripwire. Boundedstaticconsumers foundno closedfield/wholeeventtest needing3rddomain; no executedfailure/QC implied. Onlynecessary oldfailedscan node, no existingtestfileedits/oldET-ES-C0-Windows/full/matrix/intake reruns. CurrentbaseRED/newGREEN/Guard, helper_scan docs beforeGREEN, AST/raw-Git/GREENfreeze proof; lateronefrozenheadnativefull+genuineQCparallel.
- Registrationaccepted/freshfirsttodo -> NEWnormal1hclaimPR -> actual complete visiblevalidlock-risk/RootexplicitOPEN -> solewriter -> cleanfrozenSTOPPED/no pendingimplementationchild handoff -> nonauthordevaccept/merge/freshreadback -> separatelynormaltruthfulrelease. Code20–30minestimate only/whole1hincludesmetadatawallclock/actualUTCperwrite-test/freeze5minearly; no --now/renew/backdate/claim_hours/oldETlease. Extra domain/collector/control requirement concreteSTOP beforewrite. Metadata59+Guard/nativefastonly now; frozenhistory neverappend and laterpublication separate.
- Original9keyrisk0aea23af...bc04/paper/live_enabled=false/humangate/noKEY/main/runtime/M1M2start/actualledger-accountSDKfundssystemNoisePC/.env/keys/threshold/reportscore/failedroute retry. LegacyUNKNOWN/readerNOT_EVALUATED/361-364realledgerUNKNOWN/notM2-122-345-PIT-historyacceptance. No Owner operation needed.

## Task EV — M2 single raw heartbeat capture, memory-only

Status: todo (registration proposed only; product CLOSED, no claim or product tree).

- Human-directed next ordinary offline M2 necessary sub-slice. Same [10148-byte order/original4074-byte prep/8439-byte evidence](https://github.com/ZackZhaii/polymarket-pod/pull/424#issuecomment-6620597431), order SHA2564c70f46b0c1e4a6565bd484fd1a1a854ed098d80e71cf5de04b56e96450893e7. Current report blobeb76c254cdbd3ac01fa9faa8f2a8ed4fb86fad13 includes EM, not oldb6c498c anchor. Original packets remain immutable.
- Actual new Source01a15d32 personally soleauthor/controller/implementation+lifecyclewriter [ACK6620491518](https://github.com/ZackZhaii/polymarket-pod/pull/424#issuecomment-6620491518), [precisionACK6620686785](https://github.com/ZackZhaii/polymarket-pod/pull/424#issuecomment-6620686785); no selfmerge/EU-EJ role reuse. Actual new uninvolved authorized Root01a19d50 source-test-queueimplementation0 [samepacket/precisionACK6620638508](https://github.com/ZackZhaii/polymarket-pod/pull/424#issuecomment-6620638508). Genuine existing nonimplementationCodex01a1a074 [fullread/newrole/precisionACK6620738277](https://github.com/ZackZhaii/polymarket-pod/pull/424#issuecomment-6620738277),0productcalls/notPASS. Author static helpers are not independentQC.
- New callable capture_heartbeat_once(spec, *, expected_proc, capture_id), one explicit local JSON. Preserve original EM report and add only capture={capture_id,expected_proc,started_at_utc,finished_at_utc,raw_base64}; same single raw read/SHA/bytes/strict parse/declaration, no secondread or reserializedSHA. RawNone vs empty retained; bad/incomplete/changed evidence no trusted records/declaration. status always NOT_EVALUATED/null metrics-denominator-healthy_online_us; INVALID only original completeness. Invalidrequiredargs reject before path/code/clock, literalstrings-or-null/no invalid-object conversion; valid nonempty/whitespace unchanged. NewAPI only2UTC attempt stamps/no duration/reverse repair, oldAPIs no newclock.
- Minimal private capture triple/original _one2-result adapter/shared EM stage; preserve original strictJSON/identity/alias/UNC-env/change checks/old APIs and fixed protocolclosure AST. No ordinary heartbeat/podlib import/fallback. Business file output allowed domain NONE: no archive/sidecar/lock/outputroot adoption/sampling/runtime. Fullarchive owned-root/alias-sidecar/original-lock-loading IO/xb-fsync-failure semantics are DEFERRED separatecontract, not new genericOwnergate.
- Future exclusive product domains, verified absent: D:/projects/polymarket-pod/.worktrees/m2-heartbeat-archive-next/src/m2_report.py and D:/projects/polymarket-pod/.worktrees/m2-heartbeat-archive-next/tests/test_m2_report.py. Only2files/tmp_path, no new fixturefile/third domain. Do not modify heartbeat/podlib/halt_store/risk/CI/dependencies/system/businessstate.
- Four samepacket groups: exact one-read/base64/SHA/declaration/time/invariance; missing-empty-malformed/incomplete/sourcechange/identity/validator boundaries; invalidargs/UNC-env/clock/no-output; coldimport/oldAPI compatibility+10necessaryoldnodes17instances. TruebaseRED/newGREEN/Guard, docs beforeGREEN/raw-GitLF/GREENfreeze; one fixedhead nativefull and genuinely nonimplementationCodex QC parallel after legalOPEN. UnchangedAST/blob proof scoped, no old C0/EK-EU/Windows/matrix/intake repeats.
- Registration nonauthoraccept/freshfirsttodo -> normal NEW1hclaimPR -> completeactualvisiblevalidclaim+risk readback -> Root explicit PRODUCT OPEN -> solewriter/frozenPR -> nonauthordevaccept/readback -> separately dispatched truthful release. Claimwallclock targetT+24freeze/lateSTOP, targetallchainT+47-50, hardsafeT+55; estimate not guarantee. ActualUTC per futureproductwrite/test, no --now/renew/backdate/claim_hours/oldEUlease. Third-domain/newIO/oldsemantic drift STOPtoRoot.
- Registration only preserves original93tasks/settings/assignment/claim/allproduct-risk-CIblobs; EVtodo addition/normalLatest. Metadata59+Guard/nativeexactfast only now, no producttree/source-tests/claim/OPEN. Originalrisk0aea23af...bc04/paper/livefalse/humangate/KEY-main-runtime/actualledger/NoisePCtasks/accountSDKfunds/.env-key/threshold-throughline boundaries and legacyUNKNOWN/readerNOT_EVALUATED remain.

## Latest
- 2026-11-04T03:07:21Z: EV memory-only same-read heartbeat capture accepted/merged via [PR #427](https://github.com/ZackZhaii/polymarket-pod/pull/427), head4660638ae2347eacc7bfbad0d03259f8c3f8beac; normal real --done release proposed from freshactualdev185402b9f5986d853915efb66348c8b81e5a31b0. CLI supplies actualdone_at and clears originalclaim, no --now-renew-backdate-claim_hours-newtodo.
- Actual nonauthor Root final [427comment6621396955](https://github.com/ZackZhaii/polymarket-pod/pull/427#issuecomment-6621396955): PASS_WITH_LIMITS onlymemorycapture. Native [41762638940/job126353634269](https://github.com/ZackZhaii/polymarket-pod/actions/runs/41762638940/job/126353634269) SUCCESS6742passed/2skip/9xfail693.58s/Guard12. Genuine nonimplementationCodex4groups/38explicitfunctioncalls/20observations/134checks passed/0productdefects-blocked-foreignwrites; preserve1preimport ownSyntaxError/0productimports-calls, correctedbeforeonlybusinessrun/no greenrerun, notwhole-runfirstpass;11artifactSHA/STOPPED/ownedcleanexact noforce cleanup verified. Exactmodel-usage-costUNKNOWN, functioncalls notmodelcalls.
- Author truebaseRED2missingAPI/0fixtureerrors then firstGREEN37new+17necessaryinstances/10oldnodes54pass/XMLsuite0.722s/Guard12; allpreGREEN-GREEN-freeze raw/GitLF/API-doc/oldAST/testprefix proof retained. Same10148byteorder4c70f46b/Root6620638508precision/Source6620686785/genuine6620738277 unchanged. No product/full/QC/oldmatrix/intake rerun. Staticauto6621275454 pytest0 advisory/historicalregistration wording does not change currentstate/Latest; preserve oldhistory.
- OnlyLOOP/state: original93tasks/settings/assignment/EVfinish/two productblobs/risk-CI-allotherblobs unchanged; branch94done/claimnull notglobalclear until nonauthor devintegration/freshreadback. Source01a15d32 samepersonalso lifecyclewriter/no selfmerge, actualuninvolved authorizedordinarydev Root01a19d50 finalgate/implementation0. SourcecleanSTOPPED afterreleasePR/no pending implementation tools-children; metadata59+Guard/nativeexactfastonly.
- Originalrisk0aea23af...bc04/paper/live_enabled=false/humangate/KEY-main-runtime/actualledger-accountSDKfunds-systemNoisePC-.env-key-threshold-throughline unchanged. OriginalEVclaim02:26:31-03:26:31Z/safe03:21:31Z notextended; productfreeze02:43:02.364739Z beforeT24. Onlymemorycapture, archive-output-lock-sidecar-persistence/sampling-health-M2coverage/Windows/hostauth DEFERRED/notcertified, legacyUNKNOWN/readerNOT_EVALUATED.
- 2026-11-04T02:26:31Z: EV memory-only raw capture normal NEW1h claim proposed from freshactualdevb388192da17df75aec4bae9c99d8699fa31695c6 after nonauthor registration425 accepted [425comment6620842746](https://github.com/ZackZhaii/polymarket-pod/pull/425#issuecomment-6620842746). Verified93done/EVactualfirst-onlytodo/claimnull/originalrisk before normalclaim; branchlock notglobal untildevintegration.
- Complete actualclaim surface=codex-local/task=EV/since=2026-11-04T02:26:31Z/until=2026-11-04T03:26:31Z; safe_freeze_utc=2026-11-04T03:21:31Z; T+24 checkpoint=2026-11-04T02:50:31Z. Real new1h/no --now-renew-backdate-claim_hours-oldEUlock; full wallclock includesmetadata/waiting, targetlateSTOP/HOLD not silentlychanged.
- Same10148byteorder SHA4c70f46b0c1e4a6565bd484fd1a1a854ed098d80e71cf5de04b56e96450893e7 public424comment6620597431/Rootnew-role+precision6620638508/Sourceprecision6620686785/genuineCodex actualfullreadnewrole6620738277 retained. Source01a15d32 personalsoleauthor/controller/lifecycle/no selfmerge; Root01a19d50 actualuninvolved authorizedordinarydev integrator/QCorganizer. Preparation0productcalls notQCPASS; no oldEU-EJ role reuse.
- OnlyLOOP/state; original93done/tasks/settings/assignment/EVfinish/allproduct-risk-CIblobs unchanged. Producttreeabsent/no source-tests/OPEN. Nonauthor merge/freshactualcompletevisiblevalidclaim+risk and RootexplicitPRODUCTOPEN required beforeengineering; no third domain/newIO. SourcecleanSTOPPED afterclaimPR/no pendingimplementation tools-children; metadata59+Guard/nativeexactfastonly/no product-full-QC-oldmatrix-intake repeats.
- Originalrisk0aea23af...bc04/paper/live_enabled=false/humangate/KEY-main-runtime-actualledger-accountSDKfunds-systemNoisePC-.env-key-threshold-throughline boundaries retained. Memory-only, no archive-sidecar-lock-outputdomain/sampling-health-M2acceptance. Invalid completeness/statusNOT_EVALUATED/nullmetrics/literalstr-or-null/validwhitespace precision unchanged, legacyUNKNOWN/readerNOT_EVALUATED.
- 2026-11-04T02:19:26.990916Z: EV memory-only single raw heartbeat capture registration proposed from freshactualdeve677a0d06262dd7b8dc192eaa44aa26edb610d55;93oldtasks/settings/assignment unchanged/EVonlytodo/claimnull/no claim-producttree-source-tests-OPEN.
- Same10148byteorder SHA4c70f46b0c1e4a6565bd484fd1a1a854ed098d80e71cf5de04b56e96450893e7 public424comment6620597431 plus Rootprecision6620638508/SourceprecisionACK6620686785. Actualnew Source01a15d32 personallysoleauthor/controller/lifecycle/no selfmerge; Root01a19d50 genuineuninvolved authorizedordinarydev integrator/QCorganizer, no EU-EJ reuse.
- GenuineexistingCodex newfullpacket/precision/readACK424comment6620738277 actuallyverified4068bytes/SHA441f209f9dad2ae3f7d920b68ea5e1e4bfd5058ac4e7eb0d33b390d51f3cac86/0productcalls-notPASS; no duplicate QC/filler tests. Only2future absolute productfiles and same4groups/10necessaryoldnodes17instances; raw-single-read/memory output, no archive-lock-sidecar-outputroot.
- OnlyLOOP/state; original93tasks/settings/claim/product-risk-CIblobs unchanged; dev/main/all4openheads highestEU/EVunused/Source0open before1. Registrationaccepted/actualfirsttodo then separateNEW1hclaim/completevisiblevalidlock-risk/RootexplicitOPEN beforeengineering. Metadata59+Guard/nativeexactfastonly; SourcecleanSTOPPED afterregistrationPR/no pendingimplementationtool-child, no oldfull/matrix/intake rerun.
- Originalrisk0aea23af...bc04/paper/live_enabled=false/humangate/KEY-main-runtime-actualledger-accountSDKfunds-systemNoisePC-.env-key-threshold-throughline unchanged. Originalarchive-lifecycle IO deferred, no new sampling/health/coverage/M2acceptance; legacyUNKNOWN/readerNOT_EVALUATED. Same targetT+24/lateSTOP/safeT+55/no --now-renew-backdate-claim_hours-oldEUlease; not a guarantee.
- 2026-11-04T01:42:07Z: EU existing-scan-event fetch facts accepted and merged via [PR #423](https://github.com/ZackZhaii/polymarket-pod/pull/423), head20ff24b8ce0091e8d8bb6a95f5c67b899f0d072f; normal --done release proposed from fresh actualdev a3055b7847b16ad7945bf6fc427833f8be569c8a. CLI supplied the real done_at and cleared the original claim; no --now/renew/backdate/claim_hours/new todo.
- Actual nonauthor Root final [423comment6620182473](https://github.com/ZackZhaii/polymarket-pod/pull/423#issuecomment-6620182473): PASS_WITH_LIMITS for this offline slice. Current-head native [41753555299/job126324177578](https://github.com/ZackZhaii/polymarket-pod/actions/runs/41753555299/job/126324177578) SUCCESS6705passed/2skip/9xfail786.28s/Guard12. Genuine nonimplementation Codex4groups/38explicitfunctioncalls/15observations/110checks passed/productdefects0/blocked0/foreignwrites0; preserve1own max_pages API/IndexError adapter history, only unclosed tail corrected/no green-prefix rerun, not whole-run firstpass. Exactmodel/usageUNKNOWN;15artifactSHA/ownedcleanup verified. Static auto and selfreported DSH are advisory, not independent execution or desktop connection evidence.
- Author current-base RED2 missing-fetch failures then firstGREEN31new+one necessary existing node32passed0.85s/Guard12; productrework0/fixturefailures0/preGREENcorrections0. Same10215byte order SHA f4cf3de6d783a71b4993e5bd1562bba2a332fcf759f13c51ba60f14ba18c1f57 and immutable5203byte product handoff/raw-Git/GREEN-freeze/API-docstrings/whole-AST proof remain. Only original scanned/incomplete event fields observe present raw fetch facts; scanner/control/ET/ES unchanged. No product/full/QC/oldmatrix/intake rerun in release.
- Only LOOP/state lifecycle metadata: original92tasks/settings/assignment/EUfinish/two productblobs/all other blobs unchanged; branch93done/claimnull is not global clearance until nonauthor dev integration and fresh readback. Source01a15d32 sole lifecyclewriter/no selfmerge; Root01a19d50 actual uninvolved authorized ordinarydev integrator. After releasePR Source cleanSTOPPED/no pending implementation tools-children; metadata59+Guard/nativeexactfast only.
- Originalrisk0aea23af...bc04/paper/live_enabled=false/humangate and KEY/main/runtime/actualledger/accountSDK/funds/systemNoisePC/.env-key/threshold restrictions remain. Originalclaim01:00:03-02:00:03Z/safe01:55:03Z not extended; returned fetch facts do not certify unique markets/coverage/95%/untruncated/realAPI/M2-122-345/PIT/history acceptance. Known synthetic pagecap/no-nextcursor completeTrue remains; legacyUNKNOWN/readerNOT_EVALUATED/361-364actualledgerUNKNOWN retained.
- 2026-11-04T01:00:03Z: EU existing-scan-event fetchfacts normal NEW1h claim proposed from freshactualdev50a8a5a6b8b0c11f9e421d8ed413ed74ce30f005 after nonauthor registration421/public newRoot-genuineCodex fullsamepacket ACK [421comment6619551003](https://github.com/ZackZhaii/polymarket-pod/pull/421#issuecomment-6619551003). Verified92done/EUonlyfirsttodo/claimnull/originalrisk beforeclaim; branchlock notglobal untildevintegration.
- Completeactualnormalclaim surface=codex-local/task=EU/since=2026-11-04T01:00:03Z/until=2026-11-04T02:00:03Z/safe_freeze_utc=2026-11-04T01:55:03Z; actualnew1h/no --now-renew-backdate-claim_hours-clearedETlease. Wholebudget includesmetadatawallclock/actualUTC per future productwrite-test.
- Same10215byte workorder SHA f4cf3de6d783a71b4993e5bd1562bba2a332fcf759f13c51ba60f14ba18c1f57 [421comment6619526827](https://github.com/ZackZhaii/polymarket-pod/pull/421#issuecomment-6619526827), actualNEWSource-Root-genuineCodex role/fullpacket ACK421comment6619551003; preparation0calls/tests notQCPASS. Source01a15d32 soleauthor/controller/lifecyclewriter/no selfmerge; Root01a19d50 actualuninvolved authorizedordinarydev integrator/QCorganizer. Twofutureproductdomains only/no3rddomain/no oldET-EJPASS.
- OnlyLOOP/state lifecyclemetadata; original92tasks/settings/EUfinish/allproduct-risk-CIblobs unchanged. Producttreeabsent/no code-tests/OPEN; Rootmust integrate/freshread completevisiblevalid actualdevclaim/firsttask/risk and explicitlyOPEN before engineering. SourcecleanSTOPPED afterPR/no pendingimplementation tools-children; metadata59+Guard/nativeexactfastonly/no product/full/QC/oldmatrix rerun.
- Originalrisk0aea23af...bc04/paper/live_enabled=false/humangate/KEY-main-runtime-realledger-accountSDKsystemNoisePC-.env-key-threshold restrictions retained; fetchfacts notmarketcoverage/95%/untruncated/allmarket/M2-122-345/PIT/history acceptance, legacyUNKNOWN/readerNOT_EVALUATED.
- 2026-11-04T00:53:05Z: EU existing-scan-event fetchfacts registration proposed from actualdev e2ce52d822fd7187fa31883b17c421adae3618b1;92oldtasks/settings unchanged/EUonlytodo/claimnull/no claim-producttree-code-tests-OPEN.
- Same10215rawbyte workorder SHA f4cf3de6d783a71b4993e5bd1562bba2a332fcf759f13c51ba60f14ba18c1f57 and Root6241byte staticprep publicly published with frozenhandoff on registrationPR. Source01a15d32 actualnewsoleauthor/lifecycle/no selfmerge; Root01a19d50 actualnewuninvolved authorizedordinarydev integrator/QCorganizer; genuineNEWQCrole/samepacket ACKpending/notoldETPASS.
- Only2future full productdomains/producer+newtest; boundedstaticsearchfoundno required3rddomain/legacyhunk. Attach present8keywhitelistsubset to originalscanned/incomplete only, keep rawmissing/null/non-dict originalerrors/truthiness and oldfields/GET-IOeventcounts/adoption-counter-candidates-WriteFailedpositions. No scanner/filter/pagination/threshold fixes.
- Fetchfacts notcoverage/unique/API-complete/percentages/95%; pagecap/nocursor completeTrue staticlimitation retained/notremotevalidated. ProductCLOSED until separateNEWnormal1hclaim/actualvisiblevalidlock-risk/RootexplicitOPEN; metadata59+Guard/nativefastonly/no product/full/oldmatrix-intake rerun. SourcecleanSTOPPED afterPR/no pendingimplementationtool-child.
- Originalrisk0aea23af...bc04/paper/live_enabled=false/humangate/protectedKEY-main-runtime-realledger-accountSDKsystemNoisePC-.env-key restrictions unchanged; legacyUNKNOWN/readerNOT_EVALUATED/361-364realledgerUNKNOWN/notM2-122-345/PIT/historyacceptance;244-362remainothercandidates.
- 2026-11-04T00:28:45Z: ET returnedfeequote-fact slice accepted/merged via419/head17ec7bec2f13fa90e4bc13da95183bc716766de7; normal --done release proposed from freshactualdev75f6dbf45bee1485d9c5f65c2b40658eb94b961e. ETdoing->done/actual automatic done_at/originalclaim cleared, no --now/renew/backdate/claim_hours/oldESlease/newtodo.
- Actual nonauthor Root final [419comment6619087531](https://github.com/ZackZhaii/polymarket-pod/pull/419#issuecomment-6619087531) PASS_WITH_LIMITS/offlineETonly, merge75f6dbf45bee1485d9c5f65c2b40658eb94b961e. Currenthead native [41745104275/job126296009331](https://github.com/ZackZhaii/polymarket-pod/actions/runs/41745104275/job/126296009331) SUCCESS6674passed/2skip/9xfail773.23s/Guard12; genuine nonimplementationCodex4groups/65explicitfunctioncalls/18observations/118checks firstpass/0harness-productdefects-blocked-foreignwrites;12artifactSHA/ownedcleanup verified. Exactmodel/usageUNKNOWN, functioncalls notmodelcost.
- Source truebaseRED4missing-fee-row failures then firstGREEN20new+12params/7approvedlegacy nodes32passed1.96s/Guard12. PreGREEN staticfixture clockcoherence correction/originalRED retained,0executedfixturefailures/productrework. Common8864byteorder/frozen6747byteproducthandoff/raw-vsGit/GREEN->freeze/docstring/AST/oldstrictcontrols remainimmutable. Automated6619067202 APPROVE executedpytest0 due sandbox/tmp and is onlystaticadvisory, notgenuineQC; no further product/full/QC/oldmatrix rerun.
- Release onlyLOOP/state; old91tasks/settings/ETfinish/all4productblobs/risk/CI unchanged, branch92done/claimnull; globalclear waits nonauthor devmerge/freshreadback. Source01a15d32 sole lifecyclewriter cleanSTOPPED afterPR/no pending implementation tools-children/no selfmerge; Root01a19d50 actualuninvolved authorizedordinarydev integrator. Onlymetadata59+Guard/nativeexactfast now.
- Originalrisk0aea23af...bc04/paper/live_enabled=false/humangate; quote-return facts notGET/cache-hit/parse-rate/percentages/95% orKEY122 adoption, legacyUNKNOWN/readerNOT_EVALUATED/361-364actualledgerUNKNOWN. NoM2/122/345/history/run approval; KEY/main/runtime/accountsSDKfundssystemNoisePCtasks/.env/keys/threshold/reportscore/failedroute restrictions unchanged.
- 2026-11-03T23:52:35Z: ET returnedfeequote facts normal NEW1h claim proposed from freshactualdevbc7bf063fb1907ef0df90077ef5712ce8052fc45, after nonauthor registration417 and public Root/new independentCodex same8864byte finalpacket ACK [417comment6618485925](https://github.com/ZackZhaii/polymarket-pod/pull/417#issuecomment-6618485925). Verified91done/ETonlyfirsttodo/claimnull/originalrisk beforeclaim; branchlock notglobal untildevintegration.
- Complete actual normalclaim surface=codex-local/task=ET/since=2026-11-03T23:52:35Z/until=2026-11-04T00:52:35Z/safe_freeze_utc=2026-11-04T00:47:35Z. Actual1h crossesUTCdate aswritten; no --now/renew/backdate/claim_hours/oldESlease. Wholebudget includesmetadatawallclock and actualUTC per future productwrite-test.
- Samefinal8864byteworkorder SHA038340090641fdbcf9dca68dc17a150696acf26c2044dd62fbdd5b0e8236ea25 [417comment6618422779](https://github.com/ZackZhaii/polymarket-pod/pull/417#issuecomment-6618422779); Root4domain/newWriteFailedboundary ACK416comment6618317065 and actual finalsamepacket genuineQCrole ACK417comment6618485925 preserved,4groupsprepare0calls notPASS. Source01a15d32 soleactualauthor/controller/lifecyclewriter/no selfmerge; Root01a19d50 actualuninvolved authorizedordinarydev integrator.
- OnlyLOOP/state lifecyclemetadata; original91tasks/settings/ETfinish/allproduct-risk-CIblobs unchanged. Producttreeabsent/no productcode-tests/OPEN; Rootmust integrate/freshread completevalidlock/firsttask/risk and explicitlyOPEN before engineering. SourcecleanSTOPPED afterPR/no pending implementation tool-child; metadata59+Guard/nativefastonly, no product/full/QC/oldmatrix rerun.
- Originalrisk0aea23af...bc04/paper/live_enabled=false/humangate; KEY/main/runtime/actualledger/accountsSDKfundssystemNoisePCtasks/.env/keys/thresholds untouched. Logicalquote-return facts notGET/parse-rate/percentages; oldmissingUNKNOWN/readerNOT_EVALUATED, notM2/122/345complete.
- 2026-11-03T23:44:03Z: ET returned-fee-quote facts registration proposed from actualdev e83853bfe2404f59138ea9e5641174f11d6d7b52,91oldtasks/settings unchanged/ETonlytodo/claimnull. No claim/product tree/product code or tests/OPEN.
- Final same8864byte workorder SHA038340090641fdbcf9dca68dc17a150696acf26c2044dd62fbdd5b0e8236ea25; Root actual4domain/newWriteFailed boundary ACK416comment6618317065, immutable10288byte proposal/input public416comment6618273600. Exact final bytes/frozenhandoff public on registrationPR; finalQC alignment pending, notPASS.
- Source01a15d32 sole newauthor/controller/lifecyclewriter STOPPED afterPR/no selfmerge; Root01a19d50 actual uninvolved authorized ordinarydev integrator/QC organizer. Only fourfuturedomains, lasttwo7oldnodes precise step-partition/independentnewasserts/alloldstrictcontrols retained; no silent scope expansion.
- Fee facts count originalget returns includingcache, notGET/parse-rate/coverage;9topkeys/7rawquoteattrs/UNKNOWNcache/source-before-read/batchnow distinct. Explicit newIOWriteFailed beforecurrent missingcounter/books/build/commit reusesexit4, no swallow/retry/rollback claim. Normalnew1h/actualvalidlock/RootOPEN required later. Metadata59+Guard/nativefastonly.
- Originalrisk0aea23af...bc04/paper/live_enabled=false/humangate; KEY/main/runtime/actualledger/accountsSDKsystemNoisePC/.env/keys/threshold/reportscore untouched. LegacyUNKNOWN/readerNOT_EVALUATED/361-364ledgerUNKNOWN; not122/345/M2complete;244-362remain other candidates.
- 2026-11-03T23:19:41Z: ES/#121 forward per-token fetch-evidence-only product accepted/merged via415/headf54b1a0059e03e9a1cd96d9b508e1f5fd8cb0ea0; normal --done release proposed from freshactualdevd8fc52d176e94bc45667e68a74cda5be8c494483. ESdoing->done with actual automatic done_at/originalclaim cleared, no newclaim/--now/renew/backdate/claim_hours.
- Actual nonauthor Root final accept [415comment6617954256](https://github.com/ZackZhaii/polymarket-pod/pull/415#issuecomment-6617954256), merge d8fc52d176e94bc45667e68a74cda5be8c494483. Currenthead native [41735880615/job126264827328](https://github.com/ZackZhaii/polymarket-pod/actions/runs/41735880615/job/126264827328) SUCCESS6654passed/2skip/9xfail782.58s/Guard12. Genuine independent Codex4groups/40explicitcalls/13observations/104checks allpass/0productdefects; 11artifacts SHA and owned cleanup verified. Exact returned model/usage UNKNOWN; functioncalls not modelcalls/cost.
- Source actualbaseRED3 missing-field KeyErrors then firstGREEN18new+8necessarycompat26passed1.78s/Guard12. PreGREEN static OrderIntent-vs-riskIntent ID projection correction and originalRED file preserved; independent preimport nested-AnnAssign ASTnormalizer harness error corrected before productcalls, no business rerun/product rework. Original12611byteworkorder/4573bytehandoff/raw-vsGitSHA/docstrings/AST/truepartial-duplicate bounds and UNKNOWN/PASS_WITH_LIMITS histories retained.
- Release onlyLOOP/state; old90tasks/settings/ESfinish/bothproductblobs/risk/CI unchanged, branch91done/claimnull, globalrelease waits nonauthor devmerge/freshreadback. Source01a15d32 sole lifecyclewriter STOPPED afterPR/clean/no pending implementation tool-child/no selfmerge; Root01a19d50 actual uninvolved ordinarydev integrator. Only necessary newmetadata59+Guard/native exactfast, no product/full/QC/oldmatrix repeats.
- Originalrisk0aea23af...bc04/paper/live_enabled=false/humangate retained; historical missing fetch evidence UNKNOWN/reader overall NOT_EVALUATED, not whole121 scoring/backfill/PIT/allforecast/M2 run acceptance. #361/#364 actualledger UNKNOWN; no futuretodo/KEY/main/runtime/orders/actualledger/accounts-SDK-system-NoisePCtasks-.env-keys-threshold changes.
- 2026-11-03T22:47:51Z: ES/#121 normal new claim proposed from freshactualdev295deb7b43e96b66b6185a86375ac9a5e17250c2, after nonauthor registration413 and public Root/new independent Codex same12611byte packet ACK [413comment6617420306](https://github.com/ZackZhaii/polymarket-pod/pull/413#issuecomment-6617420306). Verified90done/ESonlyfirsttodo/claimnull/originalrisk before normal claim; branch lock is not global before dev integration.
- Complete normal claim: surface=codex-local/task=ES/since=2026-11-03T22:47:51Z/until=2026-11-03T23:47:51Z/safe_freeze_utc=2026-11-03T23:42:51Z. Fresh actual1h; no --now/renew/backdate/claim_hours/oldER lease reuse. Full real wallclock includes metadata wait; actualUTC per future product write/test.
- Same frozen work order12611bytes/SHA92089d7bd1de6b8bee4ac7205154546b6446ee623fbd2ad407aca34d3fd26ad0 [413comment6617371703](https://github.com/ZackZhaii/polymarket-pod/pull/413#issuecomment-6617371703). Source01a15d32 actual sole author/controller/lifecycle writer; no selfmerge. Root01a19d50 actual uninvolved ordinarydev integrator/QC organizer; genuine existingCodex01a1a074 actual newrole/fullpacket ACK, preparation0productcalls is not PASS.
- Only LOOP/state lifecyclemetadata; original90tasks/settings/products/risk/CI and ES acceptance finish unchanged. Product tree absent, no product code/tests/OPEN; product remains CLOSED until actual valid complete dev claim/firsttask/risk readback and explicit Root OPEN. Source lifecycle phase STOPPED after PR/clean/no pending implementation tools or children; existing metadata59+Guard/nativefast only, no old product suites/full repeat.
- Originalrisk0aea23af...bc04/paper/live_enabled=false/humangate retained; KEY/main/runtime/orders/real ledger/accounts/system/NoisePC PolyPod/.env/keys untouched. #361/#364 readiness UNKNOWN; only future built fetch evidence, not entire #121 scoring/backfill completion.
- 2026-11-03T22:41:44Z: ES/#121 forward per-token fetch evidence registration proposed from actualdev4ed133808c09af6b7520ea7997e912c1d931ee52, 90oldtasks unchanged/ESonlytodo/claimnull; no claim/product tree/product code or tests/OPEN.
- Shared12611byte work order SHA92089d7bd1de6b8bee4ac7205154546b6446ee623fbd2ad407aca34d3fd26ad0; issue121 and exact relevant plan excerpts publicly handed off on registration PR; historical Root prep is not queue/QC/permission.
- Source01a15d32 actual new sole author/controller/lifecycle writer STOPPED after PR/no selfmerge; Root01a19d50 actual new nonauthor ACK, public exact-packet/independent Codex new-task ACK pending. Only future runner/newfetch-test domains, no product tree yet.
- Built-only token evidence preserves same-fetch microseconds/offset and existing association/control; missing old data UNKNOWN/no timestamp fabrication. Normal fresh1h separate claim/readback/explicitOPEN required. This metadata-only PR uses existing59+Guard/nativefast; no old matrices/full/intake repeat.
- Originalrisk0aea23af...bc04/paper/live_enabled=false/humangate retained; KEY/main/runtime/orders/real ledger/NoisePC PolyPod/.env/keys untouched; #361/#364 readiness UNKNOWN, not whole #121 completion.
- 2026-11-03T22:20:22Z: ER/#345 logical settlement-getter slice accepted and merged via product411/currentheadf53a850d7eef5660dc45fd8961446e97985dcfad; normal --done release proposed from freshactualdev6e377bb266b6e3df93ae8a861398cf60e7ed60c7. ERdoing->done with actual automatic done_at and originalclaim cleared; no newclaim/renew/--now/backdate/claim_hours.
- ActualnonauthorRoot final accept [411comment6616812153](https://github.com/ZackZhaii/polymarket-pod/pull/411#issuecomment-6616812153), merge 6e377bb266b6e3df93ae8a861398cf60e7ed60c7. Currenthead native [41727358995/job126235537298](https://github.com/ZackZhaii/polymarket-pod/actions/runs/41727358995/job/126235537298) SUCCESS6636passed/2skip/9xfail675.46s/Guard12; oldhead1fail-6635pass native preserved, not first-passfull success.
- Source original58new+16compat/Guard12; root-approved fourth strict-key literal compat fixed oldEQ baseline, only failednode1GREEN/Guard12 thennewheadfull. TrueCodex6group business controls closed/0productdefects, originalHOLDfixtureexpectation andscanintervalharness failures retained/correctedtail; independentdelta checked1literal andoriginal3blobs equal, explicitly reusedbusinessproof. Old12421byteorder/6521bytehandoff/new3825compat-handoff andallrawfailure/QChistory immutable.
- Release onlyLOOP/state; old89tasks/settings/all4productfiles/risk/CI unchanged, branch90done/claimnull, globalrelease waits nonauthor devmerge/freshreadback. ERcurrentStatusactualdone/settlement-getter-only; registration/claimhistory preserved. Source01a15d32 sole lifecyclewriter STOPPED afterPR/no pendingimplementation, no selfmerge; actualnonauthorRoot01a19d50integration. Onlynewrelease metadata59+Guard/nativeexactfast, no product/oldmatrix/full/QCrerun.
- Originalrisk0aea23af...bc04/paper/live_enabled=false/humangate retained. Notentire345closure; remainingfee/writeoff/Ownerthresholds and #361/#364 actualledger readiness stay separate/UNKNOWN. NoKEY/main/runtime/API/real ledger/accounts/SDK/system/.env/keys/PolyPodtasks/trading changes.
- 2026-11-03T21:30:50Z: ER/#345 settlement-getter-only normal new claim proposed from freshactualdev32fbea975cd165f7733ef49bcafa2aa85da36c69 after nonauthor registration409; verified89done/ERonlyfirsttodo/claimnull/originalrisk. Branchclaim notglobal before nonauthorintegration/readback.
- Complete normalclaim surface=codex-local/task=ER/since=2026-11-03T21:30:50Z/until=2026-11-03T22:30:50Z/safe_freeze_utc=2026-11-03T22:25:50Z. New1h actualcurrenttime, no --now/renew/backdate/claim_hours oroldEQlease.
- Same12421byte commonworkorder SHA7b3dc61b85287c046429a7dda31365aa9f88bab5532b8e421cd892c0879e9742 public409comment6615801667; Root actualnewER publicACK/registrationaccept409comment6615867864 with existingindependentCodexnewtaskACK/6groupPREPARED0productcalls. Source01a15d32 solepersonalwriter/controller cannotselfmerge; actualnonauthorRoot01a19d50integration.
- OnlyLOOP/state lifecyclemetadata; original89tasks/settings/products/risk/CI unchanged. Futureissue345-settlement-observation-next absent/uncreated, no productcode/test/implementationchild. CurrentERStatus proposeddoing/historyretained; productCLOSED untilRootactualdev fullvalidclaim/riskreadback andexplicitOPEN.
- Required newclaim metadata59+Guard/currentheadnativeexactfast then immutable cleanSTOPPED/no pendingimplementation handoff. No repeatregistrationmetadata/product/full/QC/oldEQ-EP/intake orsecondtask. Originalrisk0aea23af...bc04/paper/livefalse/humangate/KEY/main/runtime/API/ledger/accounts/system/.env/keys/PolyPod/trading boundaries retained.
- 2026-11-03T21:22:02Z: propose ER/#345 settlement-getter-only registration from freshactualdev427a5f0ce6f802f7830ab94c799556366ab974b3, verified89done/no todo-doing/claimnull/originalrisk. Branch90tasks (old89done/ERonlytodo), no claim; not globally enqueued until nonauthor merge.
- Source01a15d32 actual newER personalsoleauthor/controller/lifecyclewriter ACK; Root01a19d50 actual newtask native nonauthor ACK, public exactpacket readback on registration. Source0openbefore this1; dev/main/all4openheads checked highestEQ/nextER. Prior EQ405-408 complete, no oldlease or oldrights reuse.
- Shared12421byte work order SHA7b3dc61b85287c046429a7dda31365aa9f88bab5532b8e421cd892c0879e9742 freezes exact one-counter/helper/collect-firstarg adapter/3full domains/12compat nodes and counter/control/partialappend boundaries. Root4441byte staticprep SHAa8500286...330fa and issue345body originalbytes pinned; new packets published remotely, notlocalpath-only.
- Registration ONLY LOOP/state, original89tasks/settings/products/risk/CI/claimnull unchanged. Futureissue345-settlement-observation-next absent/uncreated; no claim/OPEN/productwrites-tests. After actualregistration firsttodo readback, separate normal1hclaim+fullvalidrisk-lock readback+explicitRootOPEN required.
- Localmetadata59+Guard/currentheadnativeexactfast then immutable cleanSTOPPED/no pendingimplementation child-tools handoff. No product/full/QC/intake reruns or secondtask. Originalpaper/livefalse/humangate/KEY/main/runtime/API/ledger/account/system/.env/keys/PolyPod boundaries retained.
- 2026-11-03T21:00:33Z: EQ/#345 book-only slice accepted and merged via product407; normal --done release proposed from fresh actualdevf357e43d2b6f3319124828977fe9ab875e9d243a. EQ doing->done with actual automatic done_at; originalclaim cleared, no newclaim/renewal/--now/backdate/claim_hours.
- Product head b67bb7f601e0a789f922d4d7e429160430cb7f83, nonauthor final accept [407comment6615261748](https://github.com/ZackZhaii/polymarket-pod/pull/407#issuecomment-6615261748), merge f357e43d2b6f3319124828977fe9ab875e9d243a. Native exacthead full [41715157205/job126193113877](https://github.com/ZackZhaii/polymarket-pod/actions/runs/41715157205/job/126193113877): 6578passed/2skip/9xfail,775.68s,Guard12.
- Genuine independent existingCodex first-pass6groups/57explicitcalls/14observations/77checks passed,0productdefects/harnessfailures/blockedoperations. Source49new+27necessarycompat/Guard12; initial21testexpectation failures/3rootcauses retained and corrected, productlogic rework0. Frozen6270bytehandoff/65ee4021...75c98 and10925byteworkorder/681876af...e0d8b remain immutable; helperdocstring/rawprecision supplement preserved at407comment6614978250.
- Release onlyLOOP/state, original88tasks/settings/allproducts/risk/CI unchanged; branch89done/claimnull, global release waits nonauthor devmerge/freshreadback. EQ currentStatus is actualdone/book-only; originalregistration/claimhistory retained. Source01a15d32 sole lifecyclewriter/controller STOPPED afterPR/no pendingimplementation, no selfmerge; actualnonauthorRoot01a19d50 integration. Required existingmetadata59+Guard/nativefast only, no product/full/QC rerun.
- Original risk0aea23af...bc04/paper/live_enabled=false/humangate unchanged. Not entire345 closure; Ownerthresholds/otherpaths and361-364 realledger readiness remain separate/UNKNOWN. NoKEY/main/runtime/realAPI/ledger/accounts/system/.env/keys/PolyPodtasks/trading changes.
- 2026-11-03T20:29:59Z: EQ/#345 book-only normal new claim proposed from fresh actualdev244ae2e6c27a4026fd3a03413963d23530a86d31 after nonauthor registration405; verified88done/EQonlyfirsttodo/claimnull/originalrisk. Branch claim is not globally visible until nonauthor integration/readback.
- Complete claim surface=codex-local/task=EQ/since=2026-11-03T20:29:59Z/until=2026-11-03T21:29:59Z/safe_freeze_utc=2026-11-03T21:24:59Z. Normal currenttime1h, no --now/renew/backdate/claim_hours or oldEPlease reuse.
- Same10925byte shared work order SHA681876affcc071c15d89a68e6aba3465ca74774c62bd1f7e9a8263d9c4be0d8b public405comment6614634665; Root actual newEQ publicACK/registrationaccept405comment6614668646 and independent existingCodex newtaskACK/prepared0calls recorded there. Source01a15d32 personalsolewriter cannotselfmerge; Root01a19d50 actualnonauthorintegrator.
- Only LOOP/state lifecycle metadata; original88 tasks/settings/products/risk/CI unchanged. Futureissue345-book-observation-next absent/uncreated, no productcode/test/implementationchild; productCLOSED until Rootactualdev complete validclaim/risk readback andexplicitOPEN. CurrentEQStatus reflects proposeddoing, originalregistrationhistory retained.
- Metadata59+Guard/currentheadnativeexactfast then immutable cleanSTOPPED/no pendingimplementation handoff. No product/QC/full/intake repeat, secondtask or leaseexpansion. Originalrisk0aea23af...bc04/paper/livefalse/humangate/KEY/main/runtime/API/realledger/.env/keys/PolyPod/system/account/trading retained.
- 2026-11-03T20:24:28Z: propose EQ/#345 book-only observation registration from fresh actualdev229dddb913e67b7b030ee56d3297945b3a7e9ab7, verified88done/no todo/doing/claimnull/originalrisk. Branch89tasks (88done/EQonlytodo), no claim; not globally enqueued until nonauthor merge.
- New actual Source01a15d32 personal sole author/controller ACK; Root01a19d50 actual new-task native nonauthor ACK, public readback on this PR. Source cannot selfmerge. dev/main/all4openPR heads checked highestEP/nextEQ; source0open accelerationPR beforethis one.
- Shared10925byte book work order SHA681876affcc071c15d89a68e6aba3465ca74774c62bd1f7e9a8263d9c4be0d8b freezes exact one-key schema, getter-boundary API/RefusedURL entrypoints/3 full domains/12 selected compatibility nodes/time budget and stops. Original issue345 input bytes fixed; no public dedicated book implementation draft found, old time/Gamma evidence not candidateproof.
- Registration only LOOP/state, original88tasks/settings/products/risk/CI/claimnull unchanged. Product tree absent, no product code/test/claim/OPEN. After registration actualdev/readback, firsttodo normalnew1hclaim and explicit RootOPEN required; no EP lease reuse/renewal/backdate.
- Existing localmetadata59+Guard/currentheadnativeexactfast then immutable STOPPED/clean/no pending implementation child/tools handoff to nonauthorRoot. No product/full/QC rerun in this phase; originalpaper/livefalse/KEY/main/runtime/ledger/API/keys/PolyPod boundaries retained.
- 2026-11-03T20:05:50Z: EP/#345 /time-only slice accepted and merged via product #403; normal --done release proposed from fresh actual dev 1e197d268f303122f44e87d7b7854b311eb3aae6. EP doing -> done with actual automatic done_at; claim cleared. No new claim, renewal or backdate.
- Product head 953004aa32846689407fdde7e13a2f1816b3aa76; actual non-author acceptance [403comment6614181714](https://github.com/ZackZhaii/polymarket-pod/pull/403#issuecomment-6614181714), merge 1e197d268f303122f44e87d7b7854b311eb3aae6. Native current-head full [41706344835/job126162553431](https://github.com/ZackZhaii/polymarket-pod/actions/runs/41706344835/job/126162553431): 6529 passed / 2 skipped / 9 xfailed, 765.74s; Guard12.
- Genuine independent existing Codex QC: PASS_WITH_LIMITS, 7 groups / 90 explicit calls / 48 observations / 268 passing checks / 0 product defects. Two owned fixture failures and one postflight guard event preserved, with necessary corrected controls closed; raw history not relabelled. Shared10837byte work order SHA5e6a0be5636c843eab8c6af441f7c835bc321745613607af98edf692b2cb09b7 and frozen product handoff unchanged.
- Release metadata only LOOP/state: original87 tasks/settings/products/risk/CI unchanged, branch88done/claimnull; actual global release waits non-author dev integration/readback. EP current Status corrected to done/time-only while original registration history remains. Source01a15d32 sole lifecycle writer/controller STOPPED after PR, no self-merge; Root01a19d50 actual non-author integrator. Required local/native metadata fast checks only; no product/QC/full rerun.
- Not whole #345 closure: other data paths and Owner thresholds remain outside this slice. Original risk0aea23af...bc04 / paper / live_enabled=false / human gate retained; no KEY/main/runtime/API/real ledger/.env/keys/NoisePC PolyPod task or trading action.
- 2026-11-03T19:25:48Z: EP/#345 timegetterobservation normalnewclaim proposed fromfreshactualdev8074c1e8cb95c22ba9af1a011f38efd50a8e4dd2 after nonauthorregistration401, verified87done/EPonlyfirsttodo/claimnull/originalrisk. Branchclaim notglobal untilnonauthorintegration/readback.
- Completeclaim surface=codex-local/task=EP/since=2026-11-03T19:25:48Z/until=2026-11-03T20:25:48Z/safe_freeze_utc=2026-11-03T20:20:48Z. Original1h/normalcurrenttime, no --now/renew/backdate/claim_hours oroldEOreuse.
- Same10837byte commonworkorder5e6a0be5636c843eab8c6af441f7c835bc321745613607af98edf692b2cb09b7 public401comment6613285589; RootnewEPactualnonauthorACK/registrationaccept401comment6613363865. Source01a15d32 solepersonalwriter/controller cannotselfmerge; Root01a19d50 actualnonauthorintegration. No new scope/contract.
- OnlythismetadataLOOP/state; other87tasks/settings/products/risk/CIunchanged. Futureissue345-time-observation-next absent/uncreated, no productcode/test/implementationchild. ProductCLOSED untilRootactualdevvalidcompleteclaim/riskreadback andexplicitOPEN; onlyoriginal3domains/timegetterexception slice, notwhole345.
- Metadata59+Guard/currentheadnativeexactfast thenimmutablehandoff cleanSTOPPED/no pendingimplementation toRoot. No product/full/oldbaseline reruns or secondtask; originalrisk0aea23af...bc04/paper/livefalse/humangate/main/KEY/runtime/API/realledger/.env/keys/PolyPod/system/account/trading boundaries retained.
- 2026-11-04JST: proposeEP345/timegetterobservationregistration fromactualdevc1789cd99b2e2592270e1e94805c41d27ad636bb, verified87done/claimnull/dev-main-all4openPRhighestEO/nextEP. Branch87done/EPtodo/claimnull notglobalenqueue.
- Source01a15d32 actualnewEPpersonalsolewriterACK; Root01a19d50 actualnewEPnonauthornativeACK/QCorganization/finalordinarydevintegration, notoldEO/EJrole. TwoSourceexplorers read-only/noexecution, nottrueQC. No selfmerge.
- Sameworkorder10837bytes/SHA5e6a0be5636c843eab8c6af441f7c835bc321745613607af98edf692b2cb09b7, rawdraft9322bytes/a907a224...4d577 preserved; only3fullfuturepaths/2progresskeys+2lastcheckfields/effectiveexceptionclassification andoldcontrolprotection, no all345closure/thresholds.
- Registrationonly, producttreeabsent/uncreated/no claim/OPEN/engineering/producttest. Original87tasks/settings/products/risk/CI/claimnull same; trueoldheartbeatmissingUNKNOWNnot0, old25/strictxfailnotcurrentproof, fieldabsencebaseline to bereplaced without weakeningbehavior.
- Metadata59+Guard/nativeexactfast thenimmutablehandoff cleanSTOPPED/no pendingimplementationchild toRoot. Futurefirsttodo/newnormal1hclaim/nonauthoractualdevreadback/explicitOPEN mandatory, no oldEOlease/renew/backdate; paper/livefalse/humangate/main/KEY/runtime/realdata/account/trading restrictions unchanged.
- 2026-11-03T18:43:59Z: EO/#353 normal done/release proposed from freshactualdevb62a6dd9b455dd860f4bd23d008e40286692b8b8 after nonauthoraccepted product399. Entireactualtree equals acceptedhead3bfc9b1cddebfb2175642cf22ec3da9f42852e1b; originalcodex-local/EOclaim17:58:33Z-18:58:33Z/risk verified. Normalrelease --surface codex-local --done usestruecurrenttime, no --now/renew/backdate.
- Branch87done/claimnull not global until Root nonauthorintegration/readback. Source01a15d32 uniqueauthor/controller cannotselfmerge; Root01a19d50 remains actualEO nonauthorintegrator. No nexttask/productwrite/retest/claim in this phase; other86tasks/settings/40productfiles/CI/risk unchanged.
- Acceptedhead nativefull41691620402/job126111379432 SUCCESS6461pass/2skip/9xfail781.41s+Guard12; author617GREEN and realRED XML reused. Trueindependent Code seat01a1a074 actual72calls/36observations/107passingchecks, zero productfail/outofscope; reportSHA603d7ef1b89b4523a23b499710b9914b712d1e1d813ee10ef4409c3ed1c6fb84, finalaccept399comment6612455681.
- Astra EXECUTE capacityrejected/notexecuted, no AstraPASS claimed/retry; actualindependentCodex route/settings unchanged underexistingauthorizedQC. One preparedmissing-version harnessfailure preserved, derivedownedv1 positivecontrol explicit; RootunnecessaryOwnerwait corrected, no nonreply-as-approval. Productrework0; public399comment6612097881/final6612455681 preserveevidence.
- Same7885byte workorder SHAae819f8dde69c9ad06cbbbb80cbd093ee28691b425b0389d0560d6b0abed336c; onlyLOOP/state normalclose, metadata59+Guard/nativeexactfast thenimmutablecleanSTOPPEDhandoff toRoot. Risk0aea23af...bc04/paper/livefalse/humangate, main/KEY/runtime/API/realledger/.env/keys/PolyPod/system/account/trading boundaries retained.
- 2026-11-03T17:58:33Z: EO/#353 normal claim proposed from actualdevbbe7a29324b696434ed6a20798e503c883066057 after nonauthor registration397; verified86done/EOonlyfirsttodo/claimnull and originalrisk. Branchclaim not global untilnonauthor integration/readback.
- Complete newclaim surface=codex-local/task=EO/since=2026-11-03T17:58:33Z/until=2026-11-03T18:58:33Z/safe_freeze_utc=2026-11-03T18:53:33Z. Originalclaim_hours1, normalcurrenttime claim; no --now/renew/backdate or oldENlease reuse.
- Same7885byte commonworkorder SHAae819f8dde69c9ad06cbbbb80cbd093ee28691b425b0389d0560d6b0abed336c public397comment6611475587; actualRootnewEO nonauthorACK/349testscope/registrationaccept397comment6611497296. Source01a15d32 solepersonal author/controller, Root01a19d50 true nonauthorintegration, no selfmerge.
- Only currentmetadata tree LOOP/state changed, other86tasks/settings/products/risk/CI unchanged. Futureissue353-keyset-next stillabsent/uncreated, no productcode/test/child. WaitRoot actualdev completevalidclaim/riskreadback and explicitPRODUCT OPEN before engineering; original11domains/acceptance retained.
- Metadata59+Guard/nativeexactfast atthishead thenimmutablehandoff cleanSTOPPED/no pendingimplementationcommand orchild toRoot. Native metadata green not productfull/QC; no extraA/KEY/main/runtime/API/realledger/.env/keys/PolyPod tasks or thresholds/trading rights.
- 2026-11-04 JST: proposeEO353registration atactualdeva6f1194060b6d4b77b4fca00ac1bdd29ca3ed874 after EN365actual86done/claimnull; dev/main/all4openPRs highestEN/nextEO. Branch86done/EOtodo/claimnull notglobal untilnonauthorintegration.
- Source01a15d32 actualACK uniqueauthor/controller; Root01a19d50 actualnewEO nativeACK independentofold353/currentimplementation, pluslimited349testdomain accepted. No oldEN role/lease reuse, no selfmerge.
- Sharedworkorder7885bytes/SHAae819f8dde69c9ad06cbbbb80cbd093ee28691b425b0389d0560d6b0abed336c; approvedoriginal353patch62337bytes/8bebfe1a...7e22 verified exactly. Strictkeyset contract and all11fullpaths are in TaskEO/sharedworkorder; no product tree/claim/OPEN/engineering/test.
- Rootaccepted349test5IDadaptation preservesGLOBAL/market halt/resume/settlementpositiveassertions; retain justaccepted365 fee/GLOBAL behavior and all laterfixes. Newhead riskreview/targeted/full/trueQC later, old5729/fixtures notcurrentproof.
- OnlyLOOP/state addsEO; original86tasks/settings/claimnull/products/risk/CI untouched. Metadata59+Guard/nativeexactfast thencleanSTOPPED/immutablehandoff; normalfirsttodo/newclaim/explicitOPEN stillmandatory, paper/livefalse/humangate retained.
- 2026-11-03T17:29:43Z: EN/#365 normal done/release proposed from freshactualdevf4867f8c67751faa37e4c578bcf0c09cfbebea00 after genuinely accepted/merged product395. Verified originalclaim codex-local/EN16:51:44Z-17:51:44Z unchanged and both productblobs exacthead7f9edc2c852d934fabcbb1164ce96cb3fd317127; normal release --surface codex-local --done, no --now/renew/backdate.
- Branch86done/claimnull is not global until Root nonauthor merges/readsactualdev. Source01a15d32 sole author/controller cannot selfmerge; actual EN Root01a19d50 remains nonauthor of source/tests/queue and final ordinarydevintegrator. No nexttask engineering in this stage.
- Product395 exacthead fullCI41680842952/job126073740284 SUCCESS6369pass/2skip/9xfail762.01s+Guard12; trueAstra samepacket21observations/92checks zero fail/skip, acceptedcomment6610825076. Prior111 targeted/RED20/92/full are reused, no product or oldfull rerun for release.
- Same shared3826byte workorder acee691ca3950518f8da078689848fc51da01231480ba864013b66c7090770fc;365 minimal OverflowError->None accepted;361 must subtractduplicate hunk/marks after this. Only state/LOOP Latest lifecycle changed, other85tasks/settings/products/risk/CI unchanged.
- Metadata59+Guard/nativeexactfast releasehead, then immutablehandoff cleanSTOPPED/no pending implementation tools or child for Root integration/readback. Risk0aea23af...bc04/paper/livefalse/humangate remain, no main/KEY/runtime/network/realledger/.env/secrets/PolyPod task action.
- 2026-11-03T16:51:44Z: EN/#365 normal claim proposed from actualdev806f304ddfcd6a78af5401560f5a2c7b5dc558a3; first/only todo EN, original85done/claimnull, risk pin checked. Branch claim is not global until nonauthor merge/readback.
- Complete normal claim: surface=codex-local; task=EN; since=2026-11-03T16:51:44Z; until=2026-11-03T17:51:44Z; safe_freeze_utc=2026-11-03T17:46:44Z. claim_hours remains1; no --now/renew/backdate or lease modification.
- Source01a15d32actualACK sole author/controller; Root01a19d50 actual new-task nonauthor roleACK393comment6610013825 and shared3826byte packet acee691c...70fc apply. Source cannot selfmerge; original approved365patch655f3ecd...9513b/current-head acceptance remain.
- Only this metadata tree LOOP/state changes. Producttree issue365-fee-source-overflow-next remains absent/uncreated; no product writes/tests/children. Actual dev fullclaim+risk readback plus explicit PRODUCT OPEN are mandatory before engineering; A/otherready/KEY work not claimed.
- Metadata59 and Guard/nativeexactfast required for this claim head, then cleanSTOPPED/immutable manifest to Root; no full product CI claim from metadata green. Paper/livefalse/humangate/risk/main/runtime/ledger/PolyPod-task boundaries unchanged.
- 2026-11-04 JST: propose EN registration for user-listed nonKEY365 at actualdev2e48ad2c0df6c08b69cffbb1d951840584c1de24, verified85done/claimnull after EM close; dev/main/all4openPR letters highestEM/nextEN. Branch85done/ENtodo/claimnull is not global enqueue.
- Source01a15d32actualACK sole author/controller; existing Root01a19d50proposed nonauthor must ACK new-task independence before final integration. Governance registration only, no second product engineering/claim/tree/testing; no selfmerge.
- Shared workorder SHAacee691ca3950518f8da078689848fc51da01231480ba864013b66c7090770fc and approved7253byte365patch655f3ecd...9513b are public/mirrored inputs. Minimal OverflowError->None only, no newxfail/broadexcept/GLOBAL/threshold change, currenthead tests/QC required.
- A363/342readiness/stopACK requests proceed separately; choose independentready365 to avoid idle claim waiting, and subtract duplicate from361 later. All userA/B/C were read-only audited; KEY/HOLD remains unmerged, ledger scans NOT_RUN, PolyPod tasks untouched.
- Only LOOP/state adds EN; existing85tasks/settings/products/risk/CI unchanged. Metadata59+Guard/nativeexactfast then sourcecleanSTOPPED for nonauthor intake; future firsttodo/normalclaim/explicitOPEN mandatory, paper/livefalse/humangate retained.
- 2026-11-03T16:12:45Z: EM normal done/release proposed from freshactualdev9b606ffa6c122031f3fda6da0eb07397daca37fe; original fullclaim codex-local/EM15:23:46Z-16:23:46Z verified unchanged before release. Actual current time via release --surface codex-local --done, no --now/newclaim/renew/backdate; branch85done/claimnull not global until non-author integration/readback.
- Product #391 head1fa7a7ac584e51de1ed7c5e2ba8214ecb4898414 accepted by actual non-author root01a19d50 and merged9b606ffa6c122031f3fda6da0eb07397daca37fe. Current-head full41668126756/job126028877066 SUCCESS6295passed/2skip/9xfail/Guard12; original174GREEN retained, no product/full rerun.
- True non-author sameSPEC Astra30calls/488checks0fail/skip PASS_WITH_LIMITS offline,12originalAST/actualvalidator executions and snapshot/code-source IO separately proven. Code remains NOT_EVALUATED/nullmetrics/denominator/healthy_online_us, no health/history/runtime/identity authentication.
- Product froze15:48:52.867374Z before16:18:46Z safe point in original validclaim; failedtest-path assertion and all old evidence/manifests preserved. Accepted2productblobs/heartbeat/risk/CI untouched here; other84tasks/settings exactly unchanged.
- Source01a15d32sole EM lifecycle author, root01a19d50genuine non-author integrator; metadata59+Guard/exact nativefast only, then cleanSTOPPED with no EM implementation child. New user acceleration intake is separate read-only analysis, no other writer/claim/task engineering; no main/KEY/runtime/system/account/trading rights.
- 2026-11-04 JST: normal same-EM claim proposed from actualdev5130b593aa0f9aa7521246ec03432ca6e3aac558 after #389 non-author enqueue/readback; codex-local/EM 2026-11-03T15:23:46Z to 2026-11-03T16:23:46Z, safe freeze 2026-11-03T16:18:46+00:00. Branchclaim is not globally visible until nonauthor integration, no --now/renew/claim_hours change.
- Source fresh verified84done/EMsole todo/claimnull and original risk before status/claim only in this independent metadata tree; other84task objects/settings unchanged. Product candidate m2-heartbeat-declaration-next remains absent/uncreated, no product probing/implementation/tests.
- Same5groups/two exclusive absolute product files and author avoidance remain from shared FORWARD_FIRST_SLICE_BOUND raw9354bytes/SHA901bb30aba825d7e21f757b0c97a0ffa026bc4407f3c994beb2e8a35d848bc60, public PR389comment6607656914 independently decoded. Source01a15d32sole author/controller; root01a19d50genuine nonauthor integrator, source cannot selfmerge.
- Product CLOSED until this claimPR nonauthor actualdev integration/full validclaim+risk readback and explicit OPEN. RealAstra17inputs/23manualscenes PREPARED/productcalls0 is not PASS; cold-import module-body reminder is for later legal OPEN verification, not a proven defect or current product probe.
- Only LOOP/state normal claim lifecycle; metadata59+Guard/nativeexactfast then sourcecleanSTOPPED. Actual1h includes wall time, actualUTC each product write/test entry, freeze5min before expiry, budget insufficient STOPPED/HOLD. No old registration/EK/EL/C0/full/Windows349 reruns or main/KEY/runtime/system/threshold/account/trading authority.
- 2026-11-04 JST: propose human-directed EM registration at actualdev928009976741e0e15e1325b9dc4087f848bf3a24 after EK/EL accepted and released; verified84done/claimnull/no todo and dev/main/all4openPR letters, highestEL/nextEM. Registration branch84done/EMtodo/claimnull is not yet global enqueue.
- Source01a15d32actual author/controller ACK PR388comment6607559516, sole writer and no selfmerge; root01a19d50coordinates common conditions/trueAstra/nonauthor integration and writes none of this task source/tests/queue. Shared9354byte packet SHA901bb30aba825d7e21f757b0c97a0ffa026bc4407f3c994beb2e8a35d848bc60 publicly mirrored in registration PR.
- Scope one explicit local heartbeat JSON same-raw SHA/protocol declarations; invalid format/expectedproc before IO, legal nullable/UNKNOWN/paused/finished preserved, input/declaration problems separate, alwaysNOT_EVALUATED/nullmetrics/denominator/healthy_online_us. No sampling/health/history/collector/runtime claims.
- Future m2-heartbeat-declaration-next product tree remains absent; only its src/m2_report.py and tests/test_m2_report.py exclusive paths, tmp_path fixtures only. No claim/product creation/implementation/testing before nonauthor enqueue+normalclaim readback+explicit OPEN; actualUTC and freeze5min before expiry, no --now/renew.
- Registration only LOOP/state, other84task objects/settings/products/risk/CI unchanged; necessary metadata59/Guard/native exactfast then sourcecleanSTOPPED and nonauthor finalgate. No old business/full reruns or main/KEY/runtime/system/account/trading authority; paper/livefalse/human gate unchanged.
- 2026-11-03T14:55:00Z: EL normal done/release proposed from accepted actualdevd92639711f1bd6191ff37627337852c247e2d1fc; original complete codex-local/EL13:57:13Z-14:57:13Z claim fresh verified unchanged. Normal release --surface codex-local --done uses actual current time, no --now/newclaim/renew/backdate. Branch84done/claimnull not global until non-author dev integration/readback.
- Product #387 exacthead2d36177732053718b72d7df01342a4a77703502a independently accepted and merged d92639711f1bd6191ff37627337852c247e2d1fc. Current-head fullCI41655362516/job125984090789 SUCCESS6239passed/2skipped/9xfailed/Guard12; Source118GREEN retained, no product/full rerun in this closure.
- True non-author Astra functionality24calls/26inputs/349checksPASS plus freshWindows2node fixture deltaPASS; original4blobs identical and explicit reuse accepted. P2 real CRLF RED/fixture-local attrs repair/new-head proof remain preserved; all5accepted productblobs untouched here.
- Product froze14:36:16.684124Z before14:52:13Z safe cutoff under original valid1hclaim. Raw declaration interval stays NOT_EVALUATED/nullmetrics/denominator, never healthy coverage/M2run/profit approval. Historical failures, source/fixture bytes and immutable manifests/publication receipts remain intact.
- Source01a15d32authors only LOOP/state closure; root01a19d50is actual non-author final integrator. Other83tasks/settings/risk/CI unchanged; metadata59+Guard and exact nativefast only. Source cleanSTOPPED after PR, no next task/collector/main/KEY/runtime/Windows/account/trading authority; paper/livefalse/human gate unchanged.
- 2026-11-03 JST: normal EL claim proposed from actualdev5d98c2489f4a76ec77b3c1ca3b886bec5323de1f after non-author #385 enqueue/readback; codex-local/EL 2026-11-03T13:57:13Z to 2026-11-03T14:57:13Z, safe freeze 2026-11-03T14:52:13+00:00. No --now/renew/claim_hours change; branchclaim is not yet globally visible.
- Source fresh verified83done/ELsole todo/claimnull and risk originalSHA before normal status/claim; other83task objects/settings unchanged. Product candidate m2-manifest-window-next remains absent/uncreated, no product imports/tests or implementation.
- Same shared SECOND_WORK_ORDER raw5652bytes/SHAcbc8a2e2db220146f184b34c990af65865ea70b5b80f472e7d20e8275e2dd54f published PR385comment6605610419; all5acceptance conditions and3exclusive absolute domains unchanged. Source01a15d32sole author/controller, root01a19d50genuine non-author final integrator; author cannot selfmerge.
- Product remains CLOSED until currentclaimPR non-author actualdev integration and fresh complete validclaim/risk readback plus explicit OPEN. Then author freezes API/minimal reuse, checks actualUTC at every write/test entry, freeze5min before expiry; insufficient budget means STOPPED/HOLD.
- This PR only normal LOOP/state claim lifecycle; metadata59/Guard then cleanSTOPPED/currenthead native fastCI. No repeat registration/EK/C0/full, no main/KEY/threshold/runtime/Windows/accounts/SDK/funds/trading authority; paper/livefalse/human gate unchanged.
- 2026-11-03 JST: propose human-directed EL registration after completed EK at actualdev10ff78bce821a44f70aca7c2c8c347e7ceeef951; verified83done/claimnull/no todo and dev/main/all4openPR letters. Registration branch83done/ELtodo/claimnull is not yet global enqueue.
- Source01a15d32 actually ACKed sole author/controller; root01a19d50 is this task non-author QC organizer/final integrator, no implementation/queue writes. Shared SECOND_WORK_ORDER raw5652bytes/SHAcbc8a2e2db220146f184b34c990af65865ea70b5b80f472e7d20e8275e2dd54f will be mirrored publicly as exact original bytes.
- Scope only one explicit local m1_run/1 manifest declaration summary tied to same captured bytes/SHA: original fields, UTC endpoints, integer microsecond interval when valid end>=start. Always NOT_EVALUATED/nullmetrics/denominator, no healthy-uptime inference or fallback.
- Product candidate m2-manifest-window-next and3exclusive absolute domains remain absent/uncreated. No claim or product work before registration actualdev readback, normal visible1hclaim and explicit OPEN; freeze5min before expiry, no --now/renew/new runtime authority.
- Only LOOP/state registration changes; all83existing tasks/settings/products/risk/CI unchanged. Metadata59/Guard validation, then PR/clean/STOPPED and non-author finalgate. paper/livefalse/human gate; no main/KEY/runtime/threshold/accounts/trading or duplicate EK/C0 tests.
- 2026-11-03T13:32:28Z: EK normal completion/release proposed with actual current time using release --surface codex-local --done; source fresh verified actual dev ff0a7cc66552668d30b66c563e1d1b9649eb0bc8 and unchanged original full claim before release. Branch83done/claimnull is a proposal until non-author dev merge; no --now/newclaim/renewal.
- Product #383 exact head366181986d5374e8d581bdf6d3d0605c3dcdf336 was independently accepted and merged as ff0a7cc66552668d30b66c563e1d1b9649eb0bc8. Native fullCI41642451957/job125939125538 SUCCESS:6189passed/2skipped/9xfailed, Guard12; source68GREEN/Guard12 retained, no product rerun.
- True non-author Astra current-head delta12calls/14inputs/108checks0fail accepted; original24scenarios/30inputs/262checks reused by explicit AST proof. UNC P1 closed with pure memory sentinels and local/alias controls; offline inventory remains NOT_EVALUATED with null metrics/denominator, not M2 run acceptance.
- Product frozen13:13:30.984693Z before13:21:07Z safe cutoff; original claim12:26:07Z-13:26:07Z expired only while awaiting CI. First expired attempt/late tests, abort #381, metadata and UNC review/repair evidence remain preserved; normal release does not authorize or backdate expired engineering.
- Source01a15d32 authors only this LOOP/state lifecycle PR; root01a19d50 is the non-author final integrator. Other82task objects/settings, accepted4product blobs, risk and CI unchanged. Metadata59 and Guard only; paper/livefalse/human gate, no main/runtime/account/trading or next-task dispatch.
- 2026-11-03 JST: new explicit same-EK dispatch after independently integrated #381 abort, baseea9288658f555539758f7bba83faec10638daf4d; normal1h codex-local/EK claim proposed 2026-11-03T12:26:07Z to 2026-11-03T13:26:07Z (2026-11-03 21:26:07 JST to 2026-11-03 22:26:07 JST), safe freeze 2026-11-03 22:21:07 JST, no --now/renew/claim_hours change.
- Other82 task objects/settings/risk/CI/products unchanged. Old four candidate rawSHA verified against LEASE_EXPIRED_HANDOFF before this metadata-only proposal; product worktree not modified, advanced, imported or tested.
- Limited future repair target: two actual failing boundaries (captured bytes survive metadata failure, invalid NUL source) plus prove/refute and fix the static-only lexical alias collision with positive/negative physical-identity controls; no broad new audit/features. Same #379 FIRST_WORK_ORDER SHAa787a87b127749331651d0cdc053a56d80926dc4afa4c28b063638587c463e83.
- Product remains CLOSED pending non-author actual claim merge/readback and new OPEN. Source01a15d32 sole implementation/metadata author; root01a19d50 genuine non-author reviewer/integrator writes no source/tests/queue. Author cannot selfmerge or infer new permission from old44GREEN.
- On product OPEN, verify unchanged candidate hashes and safelyff-only original product branch to actualclaimdev, no reset/clean/overwrite. Check actualUTC/hard cutoff at every write/test entry, target30min convergence, freeze5min before expiry; targeted/Guard then frozen producthead fullCI and AstraQC parallel. Paper/livefalse/humangate and all runtime/main/account/trading restrictions unchanged.
- 2026-11-03 JST: EK incomplete normal abort/requeue proposed at 2026-11-03T12:19:21Z using release --surface codex-local --todo without --now; original full claim still matched fresh actualdev2289119 before change. Branch82done/EKtodo/claimnull; other82task objects/settings unchanged, no new claim or task.
- Budget/quality incident: safe freeze11:36:50Z and lease11:41:50Z were missed; source added/executed2 boundary tests at12:12Z after expiry, then stopped. Historical44RED to44GREEN at11:01Z is not certification of the now46case tree; new2RED/0error and all logs are retained. Product Guard/fullCI/Astra execution NOT_RUN.
- Unaccepted candidate remains untracked in the isolated product worktree, HEAD2289119/no product commit/PR. Unfixed: raw fingerprint lost on post-read metadata failure, NUL source completeness, and static-only lexical-path false alias with symlink/.. or case-sensitive paths. No repair/rollback or source test rerun after stop.
- Author01a15d32 owns only this normal LOOP/state abort proposal and preserved external candidate/LEASE_EXPIRED_HANDOFF SHA4180da008942545d1660a9c36174bc1dde4d8e7105189fd9c79e5cf1b05db272; public incident#380comment6603586435. Non-author coordinator01a19d50 handles review/integration and a lawful subsequent handoff; author cannot selfmerge or infer new lease/budget.
- Verification now applies only to isolated abort metadata with unchanged queue/pin/Guard checks, never the expired uncommitted product. No --done/renewal/new implementation/runtime/threshold/CI/main/account/trading action; paper/livefalse/human gate and risk originalSHA stay unchanged.
- 2026-11-03 JST: normal codex-local/EK claim proposed by loop_claim without --now from 2026-11-03T10:41:50Z to 2026-11-03T11:41:50Z (2026-11-03 19:41:50 JST to 2026-11-03 20:41:50 JST); safe freeze5min earlier at 2026-11-03 20:36:50 JST.
- Actual base21410a6982d2236bd1647de21ded3e78eedb1b03 independently integrated human-directed #379; EK sole todo and82done/claimnull before claim. Other82 task objects and all non-lifecycle settings remain unchanged, claim_hours1.
- Source author/controller/unique product owner01a15d32 writes only this normal LOOP/state claim now; product worktree m2-input-inventory-20261103 is not created. Non-author coordinator/integrator01a19d50 writes no source/tests/queue and source cannot selfmerge.
- Product stays stopped until this claim is independently integrated, actualdev valid fullclaim read back and explicit OPEN. Same FIRST_WORK_ORDER SHAa787a87b127749331651d0cdc053a56d80926dc4afa4c28b063638587c463e83 published in#379comment6601332543 and same strict input-inventory acceptance apply.
- No new product/test/fixture/risk/CI/input/runtime/threshold/main/account/trading change, automatic renewal or --now. Metadata59+Guard and current-head native fast checks required; future product fullCI/non-authorQC separate. Paper/livefalse/human gate unchanged.
- 2026-11-03 JST: human-directed M2 first offline input-inventory slice EK registration proposed; next unused ID verified across fresh dev/main and all4 open PRs including#1. Existing82 task objects/settings unchanged, claim=null; no claim/product worktree/implementation.
- Source author/controller/unique product owner01a15d32 accepted this new role; coordinating non-author01a19d50 writes none of its source/tests/queue and handles review/final integration. Old EJ roles do not authorize self-merge of M2.
- Deliver only explicit JSON/JSONL strict raw-byte input evidence and completeness; fixed NOT_EVALUATED, bad/missing/alias/partial evidence cannot PASS. Same FIRST_WORK_ORDER packet SHA and necessary criteria published to GitHub for writer and non-author Astra.
- Scope only future src/m2_report.py, tests/test_m2_report.py, tests/fixtures/m2_report in the named fresh product worktree plus normal separate LOOP/state lifecycle; now only registration metadata. No CLI/output/scoring/metrics/runtime or existing reader/C0/risk/CI changes.
- Gate: non-author actual enqueue then first-todo valid visible normal1h claim/OPEN; safe freeze5min before expiry, no renewal/--now. Metadata native fast59+Guard/current-head checks; future product full CI and independent QC. Paper/livefalse/human gate preserved; no M1/M2 start/main/accounts/trading authority.
- 2026-11-03 JST: normalEJ completion release --surface codex-local --done at 2026-11-03T08:53:03Z, no --now/renewal; this branch82done/claimnull is a proposal pending non-author integration and actual-dev readback.
- Accepted product #377 head1148f5b1c33f91ba1e0dfc586f4564abc1d6642e, actual devc17f99a16e3e0058542fcfdb729b93af9b8a4e3e; exact approved patchdf8601357adc1341e0c74d63d38ebd96e4f26f2cc92f87654f1d017d6a10b76b and workflowGitblob5e2cb5fc4884cfb56a0f25bcd8518568a4d4414653432d5061c3ae5706f009ab retained.
- Product acceptance:59targeted/Guard12, independent byte/branch review, actualCI41604644724/job125811342794 full6119passed with original2skip/9xfail; self-CI classifiedfull. Original35Git/24condition cases reused, no repeated Windowsfull.
- Native evidence: this real LOOP/state-only completionPR must run Guard+59targeted and skipfull; after merge verify the actualpush likewise. Until those receipts, nativefast adoption and measured savings remainPENDING; no extra empty canaryPR.
- Source author01a19d50 stops and recuses; non-author01a15d32 handles freshhead/base/scope/requiredCI/queue/risk and expected-head integration. Other81tasks/settings/product/CI/risk unchanged; paper/live=false/human gate, no M2run/SDK/account/funds/API/PAYG/main permission.
- 2026-11-03 JST: normal1h codex-local/EJ claim proposed from 2026-11-03T08:18:55Z to 2026-11-03T09:18:55Z, generated by loop_claim without --now; branch doing is not a globally visible lock.
- Actual base 0af0ac8aab93b0e9bd3bc9bf7da7c024ff67c5a9 integrated human-approved registration #375; EJ is sole todo, original81 tasks complete, claim previouslynull and claim_hours1 unchanged.
- Future writer/controller01a19d50 owns only the approved CI patch plus normal lifecycle; source01a15d32 remains non-author integrator and must not write these claim/CI bytes.
- Engineering stays stopped until this claim is independently integrated, valid actual-dev lease read back and OPEN; approved patch df8601357adc1341e0c74d63d38ebd96e4f26f2cc92f87654f1d017d6a10b76b and shared acceptance unchanged.
- Only LOOP/state claim metadata; no product/test/workflow/risk/input change, automatic renewal/account/API/PAYG/runtime/trading/main permission. Guard and current workflow checks remain required.
- 2026-11-03 JST: human-approved exact metadata CI fast-lane task EJ registration proposed after actual EI/C0 closure5b2097b; existing81 task objects and non-task settings unchanged, claim=null. No implementation/claim is included.
- Authority: direct human reply "批准该 CI 提速补丁"; exact patch SHA256 df8601357adc1341e0c74d63d38ebd96e4f26f2cc92f87654f1d017d6a10b76b, independently reviewed; other KEY/HOLD/runtime gates unchanged.
- Acceptance: unconditional Guard; fast only whole nonempty LOOP/state diff; product/config/tests/CI/other/unknown full. Existing actual35 Git cases/24 condition combinations reviewed; first CI implementation full, real metadata PR+push branch observation required.
- Assignment: future sole writer/controller01a19d50/codex-local recuses from final merge; existing non-author01a15d32 actually ACKed integration. Shared criteria and byte-exact patch/evidence published in this registration PR; no local path masquerades as remote input.
- Scope: registration LOOP/state plus necessary tests/test_loop_claim.py future-task regression maintenance; approved CI patch/workflow still unchanged. Risk originalSHA/nine keys and paper/live=false/human gate preserved. No engineering before actual first todo/normal visible1h claim; no new runtime/account/API/PAYG/trading/daemon or main permission.
- 2026-11-03 JST: EI/C0 normal completion/release proposed with release --surface codex-local --done (no --now) at 2026-11-03T07:32:43Z; EI=done/claim=null on this branch, remaining80 tasks and all non-lifecycle settings preserved. Actual dev queue changes only after non-author integration/readback.
- Product: #373 frozen head43547b8104f712a36bf4014b9c536bc6b1bad2d0 merged by authorized non-author into actual dev96c8f618efd247700b2968a7aa36bb228c4588a2. Original writer STOPPED; two known same-record binding gaps closed; distinct cancel-operation/target IDs and legitimate UNKNOWN/HOLD/opaque controls preserved.
- Acceptance: original RED405/24 expected missed-rejection failures to GREEN405 without skip/xfail, all7 groups and old363 case IDs retained; independent Astra55vectors/110observations PASS. Exact product-head CI41595325583/job125781218205 SUCCESS:6114passed/2skipped/9xfailed,629.62s,guard12/0/0. This final metadata head still requires its own actual CI.
- Freeze GitblobSHA256: contracts.py628941f7c99ec4d2b62540771cc1840ea36db222efd74c9200823d5b0d2bf27d; test_contracts.py1eb3432713403eebe76a9457681a7c0769ed78137e0edd6413044342e7a25e4d; contracts.json unchanged536626ca27d17eb177d0f8b82f2b36c908d8c4412ebca53b9c7adcd902ca36f3; initializer remains empty. No product/test/fixture/config/CI/input edits.
- Handoff: source metadata author recuses; authorized non-author Codex seat01a19d50-3e49-7bd1-9af3-c3d664c94c51 handles final checks/integration. Only normal completion of the original unchanged expired claim; no new claim/renewal/task or early CI-fast-lane adoption. paper/live=false/human gate and risk pins unchanged; runtime NOT_RUN,profitability UNKNOWN.
- 2026-11-03 JST: one normal claim proposal for existing reopened EI/codex-local, generated without --now, from 2026-11-03T05:51:53Z to 2026-11-03T06:51:53Z; claim_hours remains1, remaining80 task objects and non-lifecycle settings unchanged.
- Source base: actual dev c48d9fe2d7b786823f59f6452f4ebfe0f257b8b9 after independent #371 integration; only normal LOOP/state claim changes here, no product/test/fixture/input/config/dependency/CI modification. Prior green baseline is reused; no duplicate original full run.
- Writer: original sole implementation seat01a18b97-6be9-7c81-9330-cb3ab972c341 freshly ACKed READY/PAUSED/no pending writes/tests/uncommitted/unpushed/children/subprocesses; source controller owns metadata, original writer only contracts.py and its tests after explicit opening, fixture/init stay frozen.
- Acceptance: reuse17 known vectors/34validate-decode observations and seven original groups; fix only known CANCEL_REQUESTED target contradictions and known per-command StoreBinding contradictions under UNKNOWN account. Legitimate UNKNOWN/HOLD, distinct operation/target IDs, differing payload/proposal preimages and prior FULL/old boundaries remain valid; no external account/host/path authentication.
- Gate: branch claim is not a global visible lock. No product work until non-author actual-dev claim integration/readback while unexpired and explicit OPEN; no automatic renewal or future --now. Source author recuses from merge; current authorized independent seat01a19d50-3e49-7bd1-9af3-c3d664c94c51 handles final checks. paper/live=false/human gate and original risk pins unchanged; M2 and all later phase/runtime/trading permissions separate.
- 2026-11-02 JST: propose reopening only existing EI for two independently reproduced postmerge C0 same-record consistency gaps; original #369 merge d4dcabb and prior363/318/CI results remain historical facts, not proof these new vectors are closed.
- State: EI done to todo, prior done_at retained in this history/note instead of an active completion field, claim stays null; all other80 tasks/non-claim settings preserved, claim_hours remains1; no new task ID or source/test/config/CI edit.
- Acceptance boundary for the next lawful corrective round: bind known CANCEL_REQUESTED target fields without equating a cancel operation command_id to its target ID; compare known command/store binding even when account is UNKNOWN, preserving legitimate UNKNOWN/HOLD/empty/unbaselined cases. Only supplied same-record relationships; no external account/host/path authentication.
- Workflow: after non-author registration integration/readback, obtain one normal fresh legal EI claim and explicit opening before the original sole writer adds isolated RED/fix/GREEN; validate/decode parity, original7groups and fixed schema/fixture evidence remain required. No empty-queue implementation or automatic renewal.
- Handoff: this is a queue-only concrete proposal, not accepted/reclaimed/repaired; author cannot self-merge. Existing independent chat authorization names only #370/#369, so any new-PR receiving scope must be explicitly extended before cross-chat dispatch. paper/live=false/human gate, risk pins, main/KEY/HOLD/runtime and trading boundaries remain unchanged.
- 2026-11-02 JST: EI/C0 normal completion and release proposed with the standard release command at 2026-11-01T18:09:35Z; branch EI=done/claim=null, remaining80 task objects and all other state fields preserved. Actual dev changes only after non-author integration/readback.
- Product: 4461cfbe10154e6ceb194f22bd35c4346f0cf6eb accepted by the independent Codex seat (#369 comment6561845954); original writer stopped/clean/no pending. Round2 target RED363/11 expected missing-rejection failures to GREEN363/0skip/xfail, all7 named groups and prior349 case IDs retained; source Spec/TaskQuality PASS.
- Independent checks: reviewer actually ran363 tests plus318 independent controls (180 previous +138 new); current candidate CI41334350646 logs6072passed/2skipped/9xfailed and guard12/12. Final completion head still requires its own CI; product-byte equality alone does not move an unverified PASS.
- Freeze GitblobSHA256: contracts.py da4f0012a01030594bace973bd9438011532250dbe0200e7eaf4be0438c55446; test_contracts.py a1e02cad697fa9c4991b295e89ad29e1da5458b6b1cc8ce2595422f89ec67928; contracts.json unchanged536626ca27d17eb177d0f8b82f2b36c908d8c4412ebca53b9c7adcd902ca36f3; initializer remains empty. Final delta only normal LOOP/state metadata.
- Handoff: author recuses from merge, authorized independent Codex seat performs final checks/integration. No source rewrite, automatic lease renewal, new task, runtime/credentials/account/SDK/trading authority; paper/live=false/human gate and original risk pins unchanged. Finality/coverage claims are structural declarations, profitability remains UNKNOWN.
- 2026-11-02 JST: source controller verified actual dev 1f44d775618691e390f0bf69abd1fdfc437d644e and independent product opening #370 comment6561698190; queue metadata synchronized into the existing #369 branch, all four b610 product blobs unchanged, original sole writer remains the only product author.
- 2026-11-02 JST: one bounded continuation of existing EI/codex-local proposed with the normal claim command (no --now), 2026-11-01T17:44:41Z to 2026-11-01T18:44:41Z; claim_hours stays 1, all 81 task objects and non-claim state fields are preserved.
- Scope: docs/LOOP.md and docs/loop/state.json only, from actual dev df2095da15186b41f33768f08cdb1a525d4d87b6. No new task, completion/release, product, test, source snapshot, config, dependency or CI modification.
- Checks: current clean queue/pin baseline 43 passed without skips/xfails; this proposed queue tree receives the same focused regression and guard --require-git before handoff; exact PR-head CI remains independently required.
- Product: #369/b6104a9 remains draft/NEEDS_FIXES; the original sole implementer re-ACKed stopped/no pending writes or children. FULL requires final_cash_settlement or final cash settlement as an exact declared member per non-author ruling #369 comment6517237204; actual coverage/finality is not authenticated by that declaration.
- Handoff: the branch claim is not a global lock before non-author integration/readback; no self-merge, no automatic second renewal, no product resume on this branch proposal, and no later lane/runtime/trading authority. paper/live=false/human gate and risk pins unchanged.
- 2026-10-30: EI/C0 four-file candidate 393e4eb frozen; 109 synthetic fixtures cover 14 kinds and nested unions, five public names, strict decoder/hash/copy/zero-IO boundary. EI remains doing pending full CI and independent acceptance.
- TDD: original business RED 151 failures/224 cases with no collection errors; later failures retained; final seven groups/314 cases passed with zero skip/xfail. Candidate local guard --require-git 12/12 passed.
- Freeze uses committed Git blob bytes: contracts.py SHA256 54e57cf69055f80edea5e41237716763a30f3c70b92ce2f174e6d7ef048ada2d; contracts.json SHA256 536626ca27d17eb177d0f8b82f2b36c908d8c4412ebca53b9c7adcd902ca36f3 (CRLF checkout representation recorded separately).
- Review pending: FULL capability marker and permitted UNKNOWN intent-lineage versus COMPLETE semantics; no review/CI success or C0 completion inferred from the prior claim CI. Product writer is stopped; normal completion state waits for the acceptance gates.
- Gate: own visible codex-local/EI claim expires 2026-10-30T07:30:51Z; no automatic renewal, later P/T/L/A/S/E/V remain unregistered, paper/live=false/human gate and risk pins unchanged; author does not self-merge this draft.
- 2026-10-30: EI/C0 claim proposed by the sole codex-local controller, from 2026-10-30T06:30:51Z until 2026-10-30T07:30:51Z; EG remains done and all other task objects are preserved.
- Scope: normal LOOP/state claim plus the existing tests/test_loop_claim.py repository-state test and lifecycle fixture; no src/loop_claim.py or C0 product files changed.
- Checks: clean baseline 40 passed; old assertions failed on the lawful claim and done fixture; revised actual/todo/doing/done coverage and focused queue/pin regression 43 passed, with no skip/xfail.
- Authority: successful #360 actual-dev CI 41012871813; approved C0 packet under docs/superpowers/plans/2026-10-25-execution-s0 and bounded claim work order SHA256 c49aa018b1be06b859272d9241558ccc886dd0fbdeed89c760c8efe0921aad2b.
- Handoff: branch claim is not a globally visible lock until this base=dev PR merges; source author does not self-merge, pauses after the PR, and waits for explicit unexpired actual-dev claim verification before C0; paper/live=false/human gate and risk pins unchanged.
- 2026-10-25: Proposed EI registration for human-directed C0 only; effective enqueue requires this base=dev PR to merge.
- Queue preserved: all 80 existing tasks unchanged, EG stays first todo and claim=null; EI appended after EG without claim/completion.
- Inputs: seven exact-byte snapshots and SHA bindings under docs/superpowers/plans/2026-10-25-execution-s0; README routes historical paths.
- Future C0: four files, five interfaces, seven strict tests, full pytest/guard and schema/fixture SHA freeze; no C0 implementation/result claimed now.
- Gates: EG closes and EI first todo with legal 1h claim; later P/T/L/A/S/E/V unregistered; trading off, runtime NOT_RUN, profit UNKNOWN.
- 2026-10-16: Task EG PR #360 review 6215585310 (Poly风控; Codex 6215527516): settlement.plan rewritten collect-first/decide-second (every pair of a condition's confirmed snapshots compared; token_in_two_conditions over all confirmed snapshots incl. conflicting ones; halts over the token union; result order-independent, permutation tests); commit_if_allowed's invalid_risk_state halt also writes the invalid-payout market halts (covers an empty resolutions history; apply_settlements itself still does not read the ledger without a snapshot). Tighten only. risk.toml unchanged (0aea23af…bc04), live_enabled=false.
- 2026-10-16: Task EG PR #360 review 6215179366 (Poly风控; Codex 6214998517): settlement_conflict market halts are planned after the snapshot loop over the union of every token any confirmed snapshot of the condition(s) names (condition conflict and token_in_two_conditions alike), so legacy(a)+[a,b]+[x,y] halts mA/mB/mX/mY in all 6 orders. Tighten only. risk.toml unchanged (0aea23af…bc04), live_enabled=false.
- 2026-10-16: Task EG PR #360 review 6214537139 (Poly风控; Codex 6214486218): settlement.plan also compares each snapshot with the condition's first explicit mapping, so legacy + [a,b] + [a,c] is upgrade_token_mapping_conflict in all 6 orders (fail closed); e2e commit_if_allowed veto on ledger_verify_failed. Tighten only. risk.toml unchanged (0aea23af…bc04), live_enabled=false.
- 2026-10-16: Task EG PR #360 review 6214147426 (Poly风控; Codex 6214035038, flash 6214128436): apply_settlements treats a raising verify (payout-only 10**400) as a veto problem plus the invalid_settlement_payout market halt; settlement.plan checks result changes before returning on any conflict (every market gets its halt); agreeing legacy/full snapshots merge order-independently, removal judged only from an explicit mapping (a moved winner still halts). Tighten only. risk.toml unchanged (0aea23af…bc04), live_enabled=false.
- 2026-10-16: Task EG PR #360 review (Codex 6213482409 / flash 6213576333 / Poly风控 6213662139): settlement._number treats 10**400 as invalid (invalid_settlement_payout market halt, no OverflowError; runner records it even when verify raises); a settled token dropped from its still-confirmed condition mapping is settlement_result_changed + market halt; sentinel.halt failures and record_market_halts problems reach the caller (apply_settlements problems, preflight reasons). Tighten only. risk.toml unchanged (0aea23af…bc04), live_enabled=false.
- 2026-10-16: Task EG (#349) done by Poly桥接: settlement_conflict / settlement_result_changed / invalid_settlement_payout are now also scope=market settlement-blocking halts (global invalid_risk_state kept, no prefix removed); minimal hooks in paper_ledger.apply_settlements and m2_runner.preflight. Non-KEY, tighten only. risk.toml unchanged (0aea23af…bc04), live_enabled=false.
- 2026-10-16: Task EF (#350), codex-local: oversized fee_rate/market_price/effective_price fail closed; ledger reports invalid_fee, load_state loss=None.
- Validation: TDD 20 expected failures + 1 control; focused 347 passed; released-state full pytest 5638 passed, 8 skipped, 9 existing xfailed, 0 failed. First full queue-state failure retained.
- Non-author QC: internal capability_audit checked frozen code/tests, 12 oversized-integer cases, 12 existing-price-fallback cases and 14 finite/type controls; actual receipts accompany the PR.
- Safety: risk.toml unchanged (0aea23af…bc04), live_enabled=false; no threshold/fee formula/signing/order/runtime change; paper_ledger unchanged.
- Handoff: EF released done, claim=null; EG (#349) remains todo and was not taken. State is proposed through this base=dev PR; author does not self-merge.
- 2026-10-16: Registered Task EF (#350, fees._finite overflow) then Task EG (#349, settlement-blocking prefixes) as todo; Risk gaps pure code, non-KEY. #353 (Gamma keyset, after #342) takes the next letter when registered. risk.toml unchanged (0aea23af…bc04), live_enabled=false.
- 2026-10-15: Task EE: #343 follow-ups — malformed m1_rest error_streak documented as global halt heartbeat_malformed:m1_rest (not warn-only); sentinel_tick F541 fix; tests: seal mid-round does not count toward error_streak, missing field counts as 0. Non-KEY, no threshold change. risk.toml unchanged (0aea23af…bc04), live_enabled=false.
- 2026-10-15: Task ED (issue #138): m1_rest heartbeat progress.error_streak (consecutive all-asset REST sample failures; success clears); Sentinel warn rest_error_streak at REST_ERROR_STREAK_ALERT=3 (M2 pending); malformed streak → global halt heartbeat_malformed:m1_rest. Monitoring tighten only, non-KEY. risk.toml unchanged (0aea23af…bc04), live_enabled=false.
- 2026-10-15: Task EC: EXIT_CODES.md tick order (scan → ingest/seal → decide → commit) and m2_start exit 5 on child exit (no timeout wait); EA state entry with real test counts and the src/exit_codes.py change; TLS deadline test skips without openssl; operator CLI veto test uses a bus stub and asserts logs/bus.jsonl untouched (Codex 6179446901 P2); Task CI (#195) registered. Docs/tests only, non-KEY. risk.toml unchanged (0aea23af…bc04), live_enabled=false.
- 2026-10-15: Task CI (PR #195, KEY, approved by 家伟): leg marks need only the side the leg trades into (long: bids, short: asks); merged c8f796d. risk.toml unchanged (0aea23af…bc04), live_enabled=false.
- 2026-10-15: Task EB (issue #223): m1_run manifest `counts.recorder.<asset>.<name>` persists ws_recorder's internal counts per asset (duplicate_level_removal, group_held, group_resolved, group_unresolved, group_replaced_by_book, stale_resubscribe_snapshot), also in every `progress` snapshot and the console-close partial counts (same atomic ManifestWriter path as counts.resyncs). m1_report only displays them (no criterion; N/A for older runs). Observability only, non-KEY: no check, threshold or M1 criterion changed. risk.toml unchanged (0aea23af…bc04), live_enabled=false.
- 2026-10-15: Task EA (issue #245): EXIT_CODES.md aligns with m2_runner pre-heartbeat exits 2/3/7, settlement-before-scan wording, secret_scan exit 2 directory/word-list, m1_run 24h→6h (Refs #220). m2_runner 2/3/7 Sentinel column 不可见 (on-call: m2_start exit 5, start-logs m2_runner.log, supervisor). risk.toml unchanged (0aea23af…bc04), live_enabled=false.
- 2026-10-15: Task DZ (issue #210): operator CLI no longer defaults --fee-rate to 0.0 (default None / missing_fee_data); CLI intents without book depth/levels refuse with missing_book_levels before booking (no --asks added). Tighten-only. risk.toml unchanged (0aea23af…bc04), live_enabled=false.
- 2026-10-15: Task DZ PR #336 P1 fix (Poly risk option 1): run_cli_intent always refuses missing_book_levels; any programmatic args.asks (True/string/zero or tiny depth/NaN/inf) is ignored and commit_if_allowed is never reached. No --asks flag. tests/test_fill_realism_147.py parametric junk-asks cases. risk.toml unchanged, live_enabled=false.

- 2026-10-15: Task DX (issue #141): verify-and-close. Multi-leg / complete-set gaps stay
  `structural_arb_disabled` (bus + skips); past-endDate / UMA markets emit no gap signal. Code from Task BX
  (#177); re-enable conditions (item 2) stay unlock-only. risk.toml unchanged (0aea23af…bc04), `live_enabled=false`.


- 2026-10-14: Task DW (issue #329): m2_start's boot start records classifier_errors in its chain row and
  runs/m2/current.json (null on later starts that reuse the chain's assets). Observability only: selection and
  halts unchanged. risk.toml unchanged (0aea23af…bc04), `live_enabled=false`.

- 2026-10-14: Task DU (issue #316): public_get's deadline is a hard wall-clock cap (connect, headers, body read);
  a slow-drip answer is PublicGetError at the deadline; deadline_s must be finite and > 0. PUBLIC_GET_DEADLINE_S
  unchanged (30 s). risk.toml unchanged (0aea23af…bc04), `live_enabled=false`.

- 2026-10-14: Task DV (#321 follow-up): an out-of-bounds fill rejects its whole decision even when another
  calibration filter dropped it first; boundary timestamps that overflow UTC are dropped and counted, not raised.
  risk.toml unchanged (0aea23af…bc04), `live_enabled=false`.

- 2026-10-14: Task DQ (Poly哨兵 on #309, KEY): one public_get call bounded by PUBLIC_GET_DEADLINE_S = 30 s
  (M2 pending), /time does not retry 429, m2_runner beats between GETs, m1_run clock check beats per asset;
  429-storm regressions (< 120 s). risk.toml unchanged (0aea23af…bc04), `live_enabled=false`.

- 2026-10-14: Task DT: ledger book() post-write check finds our row by fill_id and compares it field by field
  (a later append by another process is no longer a false not_appended). risk.toml unchanged (0aea23af…bc04),
  `live_enabled=false`.

- 2026-10-14: Task DS: offline tests for paper_session's reread-then-refuse branch (file gone / edited / directory /
  reread raising after a passing pin check is `risk_toml_pin_drift` with a halt, nothing written); #305 Task DQ
  leftovers renamed to DR. Tests and docs only. risk.toml unchanged (0aea23af…bc04), `live_enabled=false`.

- 2026-10-14: Task DR (issue #305): paper_session runs the pin check first (missing / unreadable risk.toml is
  `risk_toml_pin_drift` with a halt, not an exception; one path for snapshot and check); m2_runner kill_alert
  turns any Exception into exit 4. risk.toml unchanged (0aea23af…bc04), `live_enabled=false`.

- 2026-10-14: Task DO (issue #237, PR #310): maker paper buy re-checked under commit.lock (book age, pair,
  opposite leg, ledger validity), fail closed; bids over 1 record a skip. risk.toml unchanged (0aea23af…bc04), `live_enabled=false`.

- 2026-10-14: Task DP (issue #199): public_get retries HTTP 429, waiting Retry-After integer seconds (capped at
  30 s, backoff fallback) inside the same PUBLIC_GET_ATTEMPTS budget; other 4xx still raise at once. risk.toml
  unchanged (0aea23af…bc04), `live_enabled=false`.

- 2026-10-14: Task DM (issue #293): freeze podlib/book_staleness/wash_detect clock in m2_runner_clock tests so
  RiskGate commit path matches runner fixture time. Tests only. risk.toml unchanged (0aea23af…bc04), `live_enabled=false`.

- 2026-10-14: Task DK (issue #247): m2_runner writes one `kill_file` line to `logs/alerts.jsonl` on KILL; exit
  code stays 0, no auto-restart (KEY #243). risk.toml unchanged (0aea23af…bc04), `live_enabled=false`.
- 2026-10-10: Task BL: WS recorder `ping` / `pong` events, inbound frame counts, richer `disconnected` events,
  backoff `clock_jump` events, PONGs and frame counts in the m1_run manifest. Logging only.
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
