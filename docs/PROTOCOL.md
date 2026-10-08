# Group chat protocol

Channel: `logs/bus.jsonl`

Message:
```json
{"ts":"UTC","from":"Scanner","to":"RiskGate","type":"signal|veto|fill|proposal|halt","market":"","payload":{}}
```

Rules:
- NewsForecaster and Researcher never emit orders.
- Only Executor emits paper fills, and only after RiskGate `allow`.
- Sentinel may emit `halt`, which cancels paper quotes.
- Reflector proposals land in `playbook/proposals.md` and do not change live params.
- No spoofing, no self-trade, no wash volume, no auto-withdraw, no auto size-up.
