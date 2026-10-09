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

Optional (M1): `python -m src.ws_recorder --assets TOKEN_ID ... --max-runtime 300` records the public CLOB
WebSocket market channel into the same `data/books.jsonl` row format (read-only, no auth, no orders).
It needs the optional `websockets` package: `pip install -r requirements-ws.txt`. Exit codes: 0 ok,
1 no book recorded, 2 usage error, 3 `websockets` not installed, 4 output write failed.

`python src/paper_cycle.py` reads those local files only. It emits YES+NO or multi-outcome gap signals and a staleness signal. It does not use the network, does not write a paper fill, and does not place an order. A gap is not locked profit.

`python src/paper_session.py` is one observation pass: the scanner's filtered public Gamma walk (`scanner.scan`, same rules and fail-closed exits), public CLOB `/book` for the first five passing markets, then `paper_cycle`, which judges book age from the CLOB timestamp at a clock read after the books. It appends `logs/sessions.jsonl` and does not write a paper fill. Run it when you want another paper sample. It does not start live trading.

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

Edge for a buy is `model_prob - (market_price + assumed_slippage) - fee_rate * max(market_price, effective_price)` (`src/fees.py`). Non-positive edge is vetoed, and an edge below `MIN_EDGE` (0.02) too. The `--slippage` default is `ASSUMED_SLIPPAGE` (0.005, `src/thresholds.py`). Order notional is `1000 * max_order_pct` and is clipped at 1%. `kelly_fraction` is never used for size (only shadow-reported by `src/intent_builder.py`).

## Confirm live trading is off

```bash
python -c "from pathlib import Path; t=Path('config/risk.toml').read_text(encoding='utf-8'); assert 'mode = \"paper\"' in t; assert 'live_enabled = false' in t; assert 'require_human_gate = true' in t; assert 'max_order_pct = 0.01' in t; assert 'max_daily_loss_pct = 0.03' in t; print(t)"
```

Expected pins: `mode=paper`, `live_enabled=false`, `require_human_gate=true`, `max_order_pct=0.01`, `max_daily_loss_pct=0.03`. The paper ledger refuses to fill if those pins drift. Do not set `live_enabled` to true in Phase 0.

`git status` must not list `.env`. Real keys stay out of git. `.env.example` only has empty names, and Phase 0/1 do not read them.

See `docs/ROSTER.md` and `docs/PHASE0_RUN.md`.

三端一起推进时，以 GitHub `main` 为唯一交接：本机 Grok Build 读 `AGENTS.md`，网页端和 Grok Bot 读 `docs/prompts/`。队列在 `docs/loop/state.json`。规则在 `docs/LINK.md`。一次一件，做完就停。实盘开关保持关闭。
