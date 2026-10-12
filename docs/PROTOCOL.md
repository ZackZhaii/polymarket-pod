# Group chat protocol

Channel: `logs/bus.jsonl`

Message:
```json
{"ts":"UTC","from":"Scanner","to":"RiskGate","type":"signal|veto|fill|proposal|halt|resume","market":"","payload":{}}
```

Rules:
- NewsForecaster and Researcher never emit orders.
- Only Executor emits paper fills, and only after RiskGate `allow`.
- Sentinel may emit `halt`, which cancels paper quotes. (Executor also emits `halt` for a ledger failure.)
- Task AL: every halt is persisted (`logs/halt_state.json`) before quotes are cancelled and the bus line written.
  RiskGate, Maker and paper_cycle block the halted scope (one market, or every market for `global`) until a
  `resume`. An unreadable or corrupt halt file, or `logs/HALT`, is a global halt.
- `resume` (Task AL) is the only way to lift a halt, and only the Operator (the human) may send it:
  `{"from":"Operator","to":"Sentinel","type":"resume","payload":{"scope":"global|market","halt_ref":{"id":"..."},
  "root_cause":"...","postmortem":"PR #...","checks":{},"approved_by":"家伟","exchange_action":"none"}}`.
  `bus()` refuses a resume not from Operator, not to Sentinel, without `approved_by` or `halt_ref.id`, or a market
  resume of a global `halt_ref`; Operator may send nothing but `resume`. Sent only by
  `python -m src.halt_store resume` (checks the halt is active and the scope matches; a market resume can never
  lift a global halt). Nothing resumes automatically.
- Task AQ: `bus()` scans every message before writing it (`src/secret_scan.py`). A private key, mnemonic or
  credential value is never written: a global halt is recorded and the call raises. Only file:line, rule, length
  and a sha256 prefix are ever reported, never the text. Never put keys, secrets or phrases in a payload.
- Reflector proposals land in `playbook/proposals.md` and do not change live params.
- No spoofing, no self-trade, no wash volume, no auto-withdraw, no auto size-up.
