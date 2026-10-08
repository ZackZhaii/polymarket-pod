# Paper loop handoff

Use this when a Grok Bot chat gets long. Start a new chat. Do not carry the old transcript.
Read this file, `docs/PHASE0_RUN.md`, and `config/risk.toml`. Then do one task below.

Phase 0 is on `main` at `65a08e1`. Scanner, public book, and gated paper ledger already run.
`live_enabled` is false. Do not change that. Do not read `.env`. Do not place orders.

## How to split chats

One chat, one task, one finish line. Do not start a second task in the same chat.
When the diff is committed, write five lines at the bottom of this file under `## Latest` and stop.
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

## Paste this into a new chat

```text
你是 polymarket-pod 的纸面循环入口。先读 AGENTS.md、docs/LINK.md、docs/LOOP.md、docs/loop/state.json、config/risk.toml。
git pull 之后只领取第一件 todo。做完释放，开 PR，不要推 main，不要自己合并，然后停止。
禁止读 .env 和任何密钥。禁止改 live_enabled。禁止下单、提现、自动加仓。
禁止对敲、自成交、刷量、幌骗。单边成交不是无风险套利。不承诺收益。
```

## Latest

- 2026-10-08: Task I (scanner filter) verified and closed by grok-web: public `python src/scanner.py` exit 0, scanned=15360 passed=8659 stop=exhausted bus_signals=50.
- Task J (risk_gate fails closed, PR #3) done: no default for `min_liquidity_usd` / `max_spread`; bad thresholds veto with `invalid_gate_threshold`, nan/inf/bool intent quotes veto too.
- PR #3 merged main again after I closed; queue keeps I done and J done. data/markets.jsonl not committed. Signals only; no order and no credentials.
- `python -m pytest` passed offline (83 passed). `config/risk.toml` unchanged (committed sha256 0aea23af…bc04), `live_enabled=false`.
- Queue empty. No next todo. Do not add a live-trading task. A single-leg fill is directional exposure, not locked profit.
