---
name: calibration-report
description: Use when scoring Phase 0 probabilities with Brier or filing a parameter proposal in polymarket-pod (src/quant.py, src/reflector.py). Proposals never edit config/risk.toml.
---

# calibration-report

Bus role for the score: `QuantResearcher` (`quant.propose`). That is the ROSTER name. ROLES.md says `NewsForecaster` does "Brier/Platt"; Platt is not implemented anywhere in `src/` (TBD (not in repo)). The bus stub `src/forecaster.py` emits `from=NewsForecaster` (`forecaster.BUS_NAME`, Task N) and is probability-only, not a Brier scorer. Bot name Poly研究 maps to QuantResearcher (`docs/COORDINATION.md`).

## Allowed

- `quant.brier(rows)`: mean of `(p - y)^2` over `{p, y}` rows. `p` must be a non-bool number in `[0, 1]`; `y` must be `0` or `1` (bool is rejected). Empty input raises `ValueError`.
- `quant.propose(rows=None, bus_path=...)`: scores `quant.FIXTURE` when `rows` is omitted (three local rows, no network) and emits one bus `proposal` to `Operator`. Payload: `brier, n, text, effect=proposal_only, changes_risk_toml=false, changes_size_caps=false, exchange_action=none`.
- `reflector.propose(text, market=...)`: appends one block to `playbook/proposals.md` and emits `from=Reflector, to=Operator, type=proposal`. Cap is `reflector.MAX_PER_UTC_DAY = 3`, counted by `## <YYYY-MM-DD>` headings (`reflector._count_day`).

## Forbidden

- Editing `config/risk.toml` or any size cap. `quant.py` never opens it. `reflector.propose` raises if the target path is `RISK_PATH`, and returns `refusing_live_change` when the text contains `live_enabled = true` or `mode = "live"` (`reflector.FORBIDDEN`).
- A second QuantResearcher proposal on the same bus file: `propose` returns `{accepted: false, reason: already_proposed, brier}` and writes nothing.
- A fourth Reflector proposal the same UTC day: `{accepted: false, reason: daily_cap}`. Empty text returns `reason: empty`.
- Reading a Brier number as a promise of profit. The module docstring says it is not.
- Auto-applying a proposal. ROSTER: Reflector needs human approval. Poly风控 approval plus the human gate (家伟) still stand before any parameter change.

## Steps

1. Score a local fixture with `quant.brier`. Do not fetch outcomes from the network in this module.
2. Emit at most one `quant.propose` per bus file. Record the score in the PR text if a threshold change is being suggested; do not touch risk.toml.
3. Narrative proposals go through `reflector.propose` (or `python src/reflector.py`, which files the fixed "keep paper mode" note). Confirm `risk.toml` bytes are unchanged: `reflector.main` returns 1 if they differ.
4. Leave the proposal in `playbook/proposals.md` for the Operator. A proposal is not a queue task and not a fill.

## Verification

- `python -m pytest tests/test_quant.py tests/test_reflector.py` passes offline, no network, no credentials.
- Bus line type is `proposal`, never `fill`.
- `git show HEAD:config/risk.toml | sha256sum` is still `0aea23af1e938d330b5b4fab2c6abdec9f475f16807d177a5860a4da32aebc04`. `live_enabled=false`.
- Output locations: bus jsonl (caller-supplied `bus_path`, default `logs/bus.jsonl`) and, for Reflector, `playbook/proposals.md` (`reflector.PROPOSALS_PATH`).
