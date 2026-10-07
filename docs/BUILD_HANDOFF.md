# BUILD HANDOFF — polymarket-pod

Paste the block below into a **new** Grok Build / OpenClaw / DSH project.
Do not attach this project to the coffee swarm or any existing StarHive domain.

## Isolation

- Local path: `D:\\projects\\polymarket-pod` (or `~/projects/polymarket-pod`)
- Git remote: `https://github.com/ZackZhaii/polymarket-pod` (private)
- OpenClaw workspace name: `polymarket-pod` only
- Grok Build project name: `polymarket-pod` only
- Do not read or write coffee vaults, WeChat client files, or other trading pods

## Prompt to paste

```
你是 polymarket-pod 的独立工程代理。只在本仓库工作。

目标：把已有 Phase 0 骨架推进到可跑的只读扫描 + paper ledger。不开实盘。

硬约束：
- 禁止读写任何私钥、API secret、seed、.env 真值。只允许 .env.example。
- config/risk.toml 保持 mode=paper、live_enabled=false、require_human_gate=true。
- 不实现下单、不调 CLOB 认证接口、不写提现、不自动加仓。
- 不提供翻墙或绕过地理限制的步骤。合规由操作员自行承担。
- 不承诺收益。不把单边成交当无风险套利。
- 禁止对敲、自成交、刷量、帼骗。

现有文件：
- docs/ROSTER.md 角色表
- docs/PROTOCOL.md  logs/bus.jsonl 消息协议
- docs/ROLES.md
- docs/SOURCES.md
- config/risk.toml
- src/scanner.py

本次只做：
1. git clone 本仓到独立目录，建 venv，补 requirements.txt（只用标准库：无私钥依赖）。
2. 让 src/scanner.py 能拉 Gamma 公开市场，写入 data/markets.jsonl（gitignore data/）。
3. 加 src/book.py：公开 CLOB /book，记录 best bid/ask 与 spread。不下单。
4. 加 src/paper_ledger.py：本地模拟成交，必须先有 RiskGate allow。字段：策略、模型概率、市价、edge、假设滑点、费用。
5. 加 logs/bus.jsonl 追加写入，遵守 docs/PROTOCOL.md。
6. 加 .gitignore：.env、data/、logs/、__pycache__。
7. 加 .env.example，只有空键名：POLY_PRIVATE_KEY、POLY_API_KEY、POLY_API_SECRET、POLY_API_PASSPHRASE。注释写明 Phase 0/1 不读这些变量。
8. README 补一段：如何跑 scanner，如何确认 live_enabled=false。

不要做：
- 不要安装或调用会下单的 MCP。
- 不要把本项目注册进咖啡业务 OpenClaw workspace。
- 不要自动切 live。

完成标准：
- python src/scanner.py 能打印至少 5 条公开市场。
- data/markets.jsonl 有记录。
- config/risk.toml 仍是 paper / live_enabled false。
- git status 不含 .env。
```

## Operator still does by hand

- Create the isolated folder and open it in Grok Build
- Fund nothing yet
- Do not paste keys into the Build chat
- After 2 weeks of paper logs, decide whether to open a live gate
