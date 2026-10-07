# Paste into a NEW Grok Build project named polymarket-pod

You are the builder for an isolated Polymarket paper pod. Work only in this project directory. Do not touch coffee, StarHive, or other trading repos.

Repo: https://github.com/ZackZhaii/polymarket-pod
Read docs/HANDOFF.md, docs/ROSTER.md, docs/PROTOCOL.md, config/risk.toml first.

Mission for this session:
1. Clone or sync the repo.
2. Implement Phase 0-1 only: read-only Gamma scanner, JSONL bus, paper ledger, risk veto, role stubs.
3. Never create or read a private key. If `.env` exists, ignore secret fields.
4. live_enabled must remain false. Do not add order signing.
5. Add pytest for arb math (YES+NO, multi-outcome sum) and risk rejection.
6. Write a short STATUS.md: what runs, what is paper, what the human must do next.

Done when `python src/scanner.py` prints market titles and `pytest` passes without network credentials.
