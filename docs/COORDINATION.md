# Coordination (2026-10-08)

Phase 0：只读扫描 + 纸面信号。`config/risk.toml` 固定 mode=paper、live_enabled=false、require_human_gate=true（sha256 0aea23af…bc04）。
谁都不读 `.env`，不签名，不下单，不撤单，不装交易 MCP，不对外发消息。单边成交是方向敞口，不是无风险套利。

## 唯一协调面
- 真相源：GitHub `ZackZhaii/polymarket-pod` 的 `main`（本文写于 26667aa 之上）。
- 任务队列：`docs/loop/state.json`（机器可读，`src/loop_claim.py` 领取/释放）+ `docs/LOOP.md`（人读）。两处必须同时更新。
- 规则：一个对话只领一件、只领第一件 todo（loop_claim 强制顺序），做完释放并在 `## Latest` 写五行。
- 新任务只由 Operator 加入。bot 不自造任务，不加实盘任务。
- `D:\AI_Workspace\OpenClaw\workspace\polymarket-pod` 是平行草稿树，不合并。
- **分支 `docs/grok-link` / PR #1：不合并（operator decision 2026-10-08）。** 其中 "Task F — Grok link" 的字母与 main 上 Task F（Endgame）冲突，以 main 为准。

## 角色表：ROSTER.md 为权威（operator decision 2026-10-08）
ROLES.md 仅作历史参考；ROLES 独有的 Researcher / ArbMerge / CryptoPulse / Operator 不作为总线角色名。

| Bot | ROSTER 角色（总线 `from` 用此名） | Phase 0 权限 |
|---|---|---|
| Poly指挥 | （无总线角色；协调/收口） | 分发、审查、起草，不交易，不推 main |
| Poly扫描 | Scanner | 只读 Gamma |
| Poly盘口 | BookWatcher | 只读 CLOB /book，报陈旧 |
| Poly研判 | NewsForecaster | 新闻/X 概率，只出 signal，不出单 |
| Poly预测 | NewsForecaster（概率）/ 交给 QuantResearcher 校准 | 只出概率，不出单 |
| Poly研究 | QuantResearcher | 回测、Brier、参数提案 |
| Poly套利 | ArbBinary + NegRiskArb | signal only |
| Poly做市 | Maker | 纸面报价 signal |
| Poly风控 | RiskGate | veto |
| Poly哨兵 | Sentinel | halt（只撤本地纸面报价） |
| Poly执行 | Executor | 只写本地 paper ledger，必须先有 RiskGate allow；Phase 0 不新增 fill |
| Poly技能 | （ROSTER 无；工具维护） | 维护只读 skill：market-scan / paper-fill / calibration-report |
| Poly反思 | Reflector | ≤3 条/UTC 日提案，不改 risk.toml |
ROSTER 中暂无 bot 的角色：Endgame（main 已有 `src/endgame.py` 信号桩）、SportsDevig（main 已有 `src/sports_devig.py` 信号桩，Task H）、Reconciler。

注：`src/forecaster.py` 现用 `from="Forecaster"`，ROSTER 里叫 NewsForecaster —— 记入 backlog，改名需单独任务。
注：loop_claim 的 surface 只有 grok-build-local / grok-web / grok-bot；Poly* bot 领取时用 `grok-bot`。

## 总线
`logs/bus.jsonl`，字段 `ts(UTC Z) / from / to / type∈{signal,veto,fill,proposal,halt} / market / payload`。
只有 Executor 能写 fill，且 payload.risk_decision=allow。

## 队列
| 字母 | 状态 | 标题 | Owner |
|---|---|---|---|
| A–E | done | tests / reflector / stubs / offline cycle / public session | — |
| F | done | Endgame signal only（grok-web） | — |
| G | done | Offline Brier proposal（`src/quant.py`，grok-build-local，5332d4a） | QuantResearcher（Poly研究） |
| H | done | SportsDevig signal only（`src/sports_devig.py`，grok-web，26667aa） | — |
| I | done | Scanner filter, long-tail band, keyset paging（代码在已合并的 PR #2；grok-web 于 2026-10-08 验收后关闭） | Scanner（Poly扫描） |

**顺序：** Operator 2026-10-08 决定 scanner 过滤任务排在 G 前面。它原本编号 H，但 grok-web 在 26667aa 把字母 H 用于 SportsDevig
并直接登记为 done，所以 scanner 过滤任务改为 **Task I**。G、H、I 都已完成。队列里没有 todo，`src/loop_claim.py` 不会放行任何任务。

**人工加入规则：** main 的 `## Latest` 写着 "Next code task starts only when a human adds one here and in `docs/loop/state.json`"。
Task I 通过 PR #2 加入，**Operator 合并 PR #2 就是这次 "human add"**。合并前 I 不在 main 上，任何 bot 都不能领取。

I 的代码随 PR #2 提交（Poly扫描 编写，Poly指挥 两轮审查），并已合并进 main（`fa362cf`）。
grok-web 于 2026-10-08T10:09:10Z 做完验收：离线 `python -m pytest` 59 passed；一次公开只读 `python src/scanner.py` 退出码 0（scanned=15338，passed=8641，stop=exhausted，bus_signals=50）。
结果写在 `docs/LOOP.md` 的 `## Latest`。本收口不改 `config/risk.toml`，也不合并 PR #3。

## risk.toml 校验
- 用**已提交内容**算 SHA：`git show HEAD:config/risk.toml | sha256sum`，应为 `0aea23af1e938d330b5b4fab2c6abdec9f475f16807d177a5860a4da32aebc04`。
- Windows 上 `core.autocrlf=true` 会把工作区文件转成 CRLF，工作区 `sha256sum config/risk.toml` 得到 `b896736a…`，这不代表文件被改。
  只看已提交内容的 hash；工作区 hash 不同但 `git diff -- config/risk.toml` 为空时，只是换行差异。

## 流程风险（提交 Operator 决定，本 PR 不做处理）
- grok-web 在 26667aa **直接推送到 main**（Task H — SportsDevig），没有走 PR 审查。该任务从未以 todo 进入队列、也没有经 loop_claim 领取，
  而是直接登记为 done（`done_at` 09:10:00Z 晚于提交时间 09:09:05Z）。
- 风险：绕过审查；字母冲突（本 PR 因此从 H 改为 I）；与 "新任务只由 Operator 加入" 和 "只领第一件 todo" 的规则不一致。
- 可选措施（需 Operator 决定）：main 分支保护（要求 PR）、或约定 bot 只推分支。

## Risk gaps backlog（只记录，不在 Phase 0 改 risk.toml；每条需 Operator 另立任务）
1. 自成交/对敲检测只靠策略名关键词（wash / self-trade / spoof / layering），没有基于成交对手或同市场双向挂单的检测。
2. `risk_gate.py` 没有盘口陈旧检查（不看 book 时间戳）。
3. ROSTER 写的 category caps（类别上限）既不在 risk.toml，也不在代码里。
4. `hypothetical=True` 会跳过 max_open_positions 和 max_daily_loss 检查（仅 phase0-gate-test 可用，但仍是旁路）。
5. 单边套利成交必须计为方向敞口：ledger/risk 需按单腿记净头寸，不能按 "套利对" 抵消。
6. risk.toml 没有 `min_edge` 参数（Forecaster 临时用了 0.02）；现在只要求 edge > 0。
7. risk_gate 不使用 `kelly_fraction`，所有单子一律 1% 封顶（这是保守的，但参数形同虚设）。
8. scanner 没有最短剩余到期时间规则（今天到期的盘也会通过；2026-10-08 实跑 8,409 个通过里有 944 个剩余不到 1 天）。
9. Sentinel 提议陈旧阈值 120s / 连续 2 次失败才报警（30s 会误报）；risk.toml 与代码尚无此参数。
10. Endgame 需要未过滤数据：scanner 排除 >0.97 后，0.98–0.99 区间市场不会进入 scanner 输出。
11. （审查新增）`risk_gate.evaluate` 对缺失的 `min_liquidity_usd` / `max_spread` 用默认值 0 / 1，属于缺键放行（fail-open）；scanner 新代码已是缺键报错，两者不一致。
12. （审查新增）Forecaster 总线名与 ROSTER 的 NewsForecaster 不一致。
13. ~~scanner 阈值接受布尔和无穷（`max_spread = true` 读成 1.0，`1e999` 读成 inf）。~~ **已在 PR #2（Task I）修复**：只接受有限的 int/float，bool、字符串、inf、nan 都报错。
