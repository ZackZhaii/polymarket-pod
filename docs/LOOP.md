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

Status: todo.

Finish when `src/quant.py` scores a local fixture with Brier `mean((p-y)^2)`, emits at most one proposal, and `python -m pytest` passes offline.

- No network and no credentials.
- The proposal does not edit `config/risk.toml` or any size cap.

## Paste this into a new chat

```text
你是 polymarket-pod 的纸面循环入口。先读 AGENTS.md、docs/LINK.md、docs/LOOP.md、docs/loop/state.json、config/risk.toml。
git pull 之后只领取第一件 todo。做完释放并推送，然后停止。
禁止读 .env 和任何密钥。禁止改 live_enabled。禁止下单、提现、自动加仓。
禁止对敲、自成交、刷量、幌骗。单边成交不是无风险套利。不承诺收益。
```

## Latest

- 2026-10-08: Task F done by grok-web. Endgame only signals inside 0.98-0.99 and 48 hours.
- No fill, no order, no network. `config/risk.toml` unchanged, `live_enabled=false`.
- Monitor cadence is 30 minutes. Claim lock is 1 hour so a stuck claim can be recovered.
- Next todo is Task G. One task only. Do not start a second task in the same run.
- Queue empty means stop. Do not add a live-trading task.
