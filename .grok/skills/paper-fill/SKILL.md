---
name: paper-fill
description: Use when a Phase 0 simulated fill is proposed in polymarket-pod. Requires a fresh RiskGate allow, then Executor writes logs/paper.jsonl. A single-leg fill is directional exposure, not locked profit.
---

# paper-fill

Only `Executor` writes a fill, and only after `RiskGate` returns allow (`podlib.bus`: `fill` from anyone else raises; payload `risk_decision` must be `allow`). Bus roles: `RiskGate` vetoes to `Executor`; `Executor` fills to `Sentinel` (`paper_ledger.commit_if_allowed`). ROSTER's "polymarket-client" cell is not Phase 0: no SDK is installed (`requirements.txt`).

## Allowed

- `python src/paper_ledger.py` with no args: the local self-check (`paper_ledger.run_self_check`).
- An operator paper intent through the same CLI (`paper_ledger.run_cli_intent`). It still calls `evaluate` and still cannot set `hypothetical`.
- Reading `logs/paper.jsonl` (`podlib.PAPER_PATH`) and `logs/bus.jsonl`.

## Forbidden

- A fill without a fresh in-memory `Decision.allow is True`. A bus line alone is not enough (`commit_if_allowed` re-checks and raises).
- Live orders, cancels, withdraws, size-up, or editing `config/risk.toml`. `paper_ledger.main` returns 1 if the file's bytes change during the run.
- Wash, self-trade, spoof, layering. `risk_gate.BANNED_STRATEGY_TOKENS = ("wash", "self-trade", "self_trade", "spoof", "layering")` veto `banned_behavior`.
- Calling a one-leg fill locked profit. The ledger stamps `paper_ledger.DIRECTIONAL` on every fill note. `src/gaps.py` sets `locked_profit: false` even when every leg is shown.
- `kelly_fraction` sizing. Notional is `min(max_order_pct, 0.01) * PAPER_EQUITY_USD` with `PAPER_EQUITY_USD = 1000`, so at most $10 (`risk_gate.evaluate`). Kelly cannot raise it.

## Steps

1. Build an `Intent` (`risk_gate.Intent`): `strategy, market, side (buy|sell), model_prob, market_price, assumed_slippage, fee_rate, liquidity_usd, spread, token_id, book_ts`. `hypothetical=True` is legal only for `strategy == "phase0-gate-test"`; anything else vetoes `hypothetical_strategy_not_allowed`.
2. Call `risk_gate.evaluate(intent, cfg, state)`. `state` comes from `paper_ledger.load_state(market, token_id)` (Task P): `open_positions` (open legs, keyed by market + token_id), `leg_already_open` (this exact token is open), `exposure_usd`, `leg_exposure_usd`, `daily_loss_usd` (fees today; hypothetical rows and `counts_toward_risk=false` are skipped). Arb-pair tokens never offset each other: one filled leg is one full directional position, a filled pair is two.
3. Edge (`risk_gate.evaluate`): buy = `model_prob - (market_price + slippage) - fee_rate * market_price`; sell = `(market_price - slippage) - model_prob - fee_rate * market_price`. `edge <= 0` vetoes `non_positive_edge`.
4. On veto, the bus gets `from=RiskGate, to=Executor, type=veto` and nothing is appended to `logs/paper.jsonl`. On allow, a `signal` is written, then the fill, then `from=Executor, to=Sentinel, type=fill` with `risk_decision=allow`.
5. Veto `reasons` actually produced by `evaluate`: `phase0_pin:<key=got expected ...>` (`podlib.pin_problems` over `mode, live_enabled, require_human_gate, max_order_pct, max_daily_loss_pct`); `banned_behavior`; `hypothetical_strategy_not_allowed`; `side_must_be_single_leg`; `model_prob_out_of_range`; `market_price_out_of_range`; `negative_cost_assumption`; `invalid_gate_threshold`; `missing_liquidity`; `invalid_liquidity`; `liquidity_below_min`; `missing_spread`; `invalid_spread`; `negative_spread`; `spread_above_max`; `missing_book_ts`; `invalid_book_ts`; `future_book_ts`; `stale_book`; `invalid_max_book_age`; `invalid_now`; `effective_price_out_of_range`; `non_positive_edge`; `zero_notional`; `invalid_risk_cap`; `invalid_risk_state`; `max_open_positions`; `max_daily_loss`.
6. Book rule: `book_ts` missing, unparseable, in the future, or older than `book_staleness.MAX_BOOK_AGE_S` (120 s) vetoes. CLI `--book-ts` omitted vetoes `missing_book_ts` (`paper_ledger.run_cli_intent` passes `args.book_ts` straight through). The self-check stamps `book_ts` with `utc_now()`.
7. Caps apply to hypothetical intents too (Task M). An already-open leg (`leg_already_open is True`, same market and token_id) skips only the position cap; the other leg of the same market is a new position. `max_open_positions = 5`, `max_daily_loss_pct = 0.03` (`config/risk.toml`). A missing or invalid cap vetoes `invalid_risk_cap`; bad state vetoes `invalid_risk_state`.

## Verification

- Self-check must print `self_check pin_blocked=True no_edge_blocked=True wide_spread_blocked=True banned_blocked=True hypothetical_allowed=True new_fills=1` (Python bools) and `risk_toml_unchanged=true`, else exit 1 (`paper_ledger.main`). A public book used as both model and price must print `realistic_no_edge_blocked=True` or `realistic_no_edge=skipped`.
- Operator intent: exit 0 prints `ALLOW` or `VETO`; exit 2 when strategy, market, model_prob, market_price, liquidity, or spread is missing.
- Fill fields in `logs/paper.jsonl`: `ts, strategy, market, token_id, side, model_prob, market_price, edge, edge_formula, assumed_slippage, fee_rate, fee_usd, effective_price, notional_usd, shares, spread, liquidity_usd, book_ts, hypothetical, counts_toward_risk, risk_decision, note`.
- Ledger location: `logs/paper.jsonl` (gitignored). Bus: `logs/bus.jsonl`. Neither is a wallet.
- `python -m pytest tests/test_risk_gate.py tests/test_hypothetical_caps.py tests/test_single_leg_exposure.py` passes offline. `live_enabled` stays false.
