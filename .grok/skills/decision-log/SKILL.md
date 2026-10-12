---
name: decision-log
description: Use when recording a Phase 0 paper decision (allow or veto) in polymarket-pod. Every decision logs inputs, model_prob, executable price, edge, risk rules hit, and verdict. Signal and action stay separated; append-only.
---

# decision-log

Writers: `risk_gate.evaluate`, `paper_ledger.commit_if_allowed` / `_decision_payload`. Bus roles: RiskGate → Executor (`veto` or allow `signal`); Executor → Sentinel (`fill`). Phase 0 paper; no live orders.

## Allowed

- Append decision records to `logs/bus.jsonl` and, on allow, `logs/paper.jsonl` (`podlib.PAPER_PATH`).
- Include the fields RiskGate already computes (`risk_gate.Decision`: allow, reasons, edge, notional_usd, shares, fee_usd, effective_price, assumed_slippage, fee_rate, formula).
- Separate **signal** (probability / gap / book) from **action** (paper fill). NewsForecaster emits probability only (`forecaster.emit_probability`, `order: false`).

## Forbidden

- Silent allows. Every allow/veto must hit the bus (`commit_if_allowed`).
- Treating a bus `signal` as a fill. Only `type=fill` from Executor with `risk_decision=allow` is an action (`podlib.bus`).
- Rewriting or deleting prior decision lines (append-only `append_jsonl`).
- Auto-applying Reflector/Quant proposals (`quant.propose` / `reflector.propose` set `changes_risk_toml=false`).
- Editing `config/risk.toml` or flipping `live_enabled`.

## Steps

### 1. Record inputs (Intent)

The `risk_gate.Intent` fields are:

- `strategy, market, token_id, side`
- `model_prob, market_price, assumed_slippage, fee_rate`
- `liquidity_usd, spread, book_ts, hypothetical, note`

What is logged today (check before relying on it):

- Bus `veto` / allow `signal` payload (`paper_ledger._decision_payload`): `strategy, model_prob, market_price, edge, edge_formula, assumed_slippage, fee_rate, fee_usd, reasons, hypothetical, risk_decision, note` (the allow `signal` adds `decision="allow"`). Market is the envelope `market`. `note` is the fixed `paper_ledger.DIRECTIONAL` text, not `Intent.note`.
- **Not** in the veto/allow payload: `token_id, side, liquidity_usd, spread, book_ts` and `Intent.note`. So a veto cannot be fully reconstructed from the bus today. Adding them is a code PR (TBD, not in repo); do not claim it is done.
- The allow `fill` row (`logs/paper.jsonl` and the bus `fill`) carries every Intent field plus `effective_price, notional_usd, shares, counts_toward_risk`, and `note = DIRECTIONAL + Intent.note`.

Cite: `risk_gate.Intent`, CLI in `paper_ledger.build_parser` (`--book-ts`; omitted → veto `missing_book_ts`).

### 2. Executable price and edge

- Buy effective = `market_price + assumed_slippage`; sell = `market_price - assumed_slippage` (`risk_gate.evaluate`).
- Edge formulas: `EDGE_FORMULA_BUY` / `EDGE_FORMULA_SELL` in `risk_gate.py`.
- `edge <= 0` → reason `non_positive_edge`.

### 3. Risk rules hit

`Decision.reasons` is copied verbatim into the bus payload as `reasons` (`paper_ledger._decision_payload`). Full reason vocabulary is listed in the `paper-fill` skill (phase0_pin, banned_behavior, book_* , max_open_positions, max_daily_loss, …). Task S adds the structured wash/self-trade reasons from `src/wash_detect.py`: `cross_side_same_leg`, `self_trade_structure`, `wash_same_market`, `wash_input_missing`, `wash_input_invalid` (log them verbatim like any other reason). `wash_same_market` uses `WASH_WINDOW_S = 300` s, a code constant that is only a proposal pending the user's decision at M2. The history behind these vetoes (`recent_fills`, `own_quotes` from `paper_ledger.load_state`) is not in the bus payload.

### 4. Verdict and separation

| verdict | bus | paper.jsonl |
|---|---|---|
| veto | RiskGate → Executor `type=veto`, `risk_decision=veto` | no row |
| allow (gate test / operator) | RiskGate → Executor `type=signal` (`decision=allow`), then Executor → Sentinel `type=fill` | one fill row with `risk_decision=allow` |

Re-check in memory: `commit_if_allowed` raises if `decision.allow is not True` even after the allow signal.

### 5. Append-only locations

- Decisions/signals/vetoes/fills: `logs/bus.jsonl`
- Fills only: `logs/paper.jsonl`
- Narrative proposals: `playbook/proposals.md` (not a decision log)

## Verification

- For a veto fixture: bus has `veto` + reasons; paper line count unchanged.
- For an allow: paper row includes `model_prob, market_price, edge, edge_formula, book_ts, risk_decision`.
- `python -m pytest tests/test_risk_gate.py tests/test_single_leg_exposure.py` offline.
- `live_enabled=false`.
