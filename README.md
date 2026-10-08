# polymarket-pod

Paper-first multi-role Polymarket pod.

- Phase 0: read-only scanner, public book snapshot, gated paper ledger.
- No private keys in this repo. Phase 0 does not read `.env`.
- `live_enabled` stays false until a human gate.
- A single-leg fill is directional exposure, not risk-free arbitrage.
- Not financial advice. Not a promise of profit. Mainland China crypto restrictions are the operator's responsibility. This repo does not include geo-bypass steps.

## Run the Phase 0 scanner

From the repo root. Dependencies are the Python 3 standard library only.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python src/scanner.py
python src/book.py
python src/paper_ledger.py
```

Windows, if this machine has no `D:` drive, use `%USERPROFILE%\projects\polymarket-pod` and activate with `.venv\Scripts\activate`.

`python src/scanner.py` prints public Gamma markets and appends `data/markets.jsonl`. It does not place orders.

`python src/book.py` reads the public CLOB `/book` endpoint and records best bid, best ask, and spread in `data/books.jsonl`.

`python src/paper_cycle.py` reads those local files only. It emits YES+NO or multi-outcome gap signals and a staleness signal. It does not use the network, does not write a paper fill, and does not place an order. A gap is not locked profit.

`python src/paper_ledger.py` with no arguments runs a local gate self-check. A simulated fill is written only after `RiskGate` returns allow. The default check does not invent an edge on a real market: a public price compared with the same price is vetoed. The one allow path is labeled `phase0-gate-test` and is not a forecast.

Operator paper intent (still simulated, still gated):

```bash
python src/paper_ledger.py \
  --strategy manual-paper \
  --market MARKET_ID \
  --model-prob 0.62 \
  --market-price 0.55 \
  --liquidity 20000 \
  --spread 0.02 \
  --slippage 0.005 \
  --fee-rate 0
```

Edge for a buy is `model_prob - (market_price + assumed_slippage) - fee_rate * market_price`. Non-positive edge is vetoed. Order notional is `1000 * max_order_pct` and is clipped at 1%. `kelly_fraction` is not used to increase size.

## Confirm live trading is off

```bash
python -c "from pathlib import Path; t=Path('config/risk.toml').read_text(encoding='utf-8'); assert 'mode = \"paper\"' in t; assert 'live_enabled = false' in t; assert 'require_human_gate = true' in t; assert 'max_order_pct = 0.01' in t; assert 'max_daily_loss_pct = 0.03' in t; print(t)"
```

Expected pins: `mode=paper`, `live_enabled=false`, `require_human_gate=true`, `max_order_pct=0.01`, `max_daily_loss_pct=0.03`. The paper ledger refuses to fill if those pins drift. Do not set `live_enabled` to true in Phase 0.

`git status` must not list `.env`. Real keys stay out of git. `.env.example` only has empty names, and Phase 0/1 do not read them.

See `docs/ROSTER.md` and `docs/PHASE0_RUN.md`.
