# Codex 主导协调与集成

Owner 2026-10-16 决定：日常指挥逐步由 Codex 主导，普通 `dev` PR 的最后一道集成放在 Codex；每任务持续复盘分工与质量。所有既有例行频率/触发方式不改。
本权责修订合入 `dev` 后成为各端共同规则；文档、issue或聊天回复不证明其他端已读取/采用。保留原封存证据，旧协作提案与本协议冲突时以已合入的本协议为准。

## 单一协调面与职责

GitHub `dev` 的 AGENTS/LINK/LOOP/state/risk 是项目事实源；issue/PR保存提案、验收和证据，不新增队列或总控平台。KnowledgeVault只提供经审通用经验，既有维护者独占canonical写权；跨端ACK/采用/反馈未验证时保持 `NOT_VERIFIED`。

| 席位 | 主要职责 | 交付/边界 |
|---|---|---|
| Codex | intake、优先级建议、单writer派活、独立QC组织、普通dev最终集成 | 优先级建议须按原规则入队，不越过第一todo；可自行实现但必须回避自己PR的合并 |
| Grok Build | 有边界代码、fixtures、复现和测试 | 独立worktree、一任务一writer；不与Codex竞争写同一任务 |
| DSH | 获准的长期研究、反证、非作者QC | 短轮、固定输入SHA/预算/停止条件；审查角色本身不授予合并权 |
| Poly指挥 | 空闲时批量短问答、专长证据、实际阻塞 | 标记消息、保留历史、不打断任务；不默认调度/最终集成，不承接重复长研究 |
| Owner | 优先级/范围例外与保留审批 | main、live、risk/风控阈值、KEY/HOLD、运行启动仍逐项人审 |

这些是协作席位，不改变ROSTER总线角色、Operator独有resume权或交易/运行权限。现有例行任务暂按原频率执行，不能借主控迁移改频率、触发方式或新建轮询。

## 单writer交接

每项使用现有GitHub issue/PR：任务ID、源/输入版本与SHA、唯一writer/surface、独占写域、验收条件、预算/到期/停止条件、非作者审查席、交付路径。
所有工程入口仍从最新origin/dev开独立worktree，读开工文件、核风险钉及live_enabled=false；只领第一todo、锁1小时，一轮一件，pytest及必需guard后释放、开base=dev PR即停。
领取/释放/Latest仍走PR，未合dev的分支领取不等于全局锁可见；可见性或原writer停止状态不确定则HOLD。无todo就停，不自动造任务或复跑不确定效果。
所有资料给远端时必须实际发布为GitHub文件/评论/附件并附URL+SHA；D盘路径不能当远端可访问交付。ACK、实际采用和结果反馈分别留证。

## 普通dev PR的最后一道门

“普通”指已获准范围内的代码/测试/文档，且实际diff不触及KEY/HOLD、risk/阈值、live、运行权限或需人决策的阶段闸门；标题未写KEY不构成分类证明。不明就升级Owner。
Codex合并前核：最新PR head/base SHA、授权/队列范围、必需检查与测试证据、未决审查/冲突、风险钉、非作者QC和writer关闭/交接；证据过期、head变或检查失败先停，重核后再决定。
记录 `PR / head SHA / author seat / reviewer seat / integrator seat / checks / decision / merge SHA / dev SHA`；合并后读取目标dev结果，RPC成功不等于已集成。

- 当前Codex主控会话或其实现worker写过该PR变更，均视作该主控参与实现，不能最终合并自己的PR。须在PR中明确另一个未参与实现、已获授权的Codex集成席/会话或Owner接手；未明确则HOLD。
- 同账户、同线程换标签，或主控给实现child换名，不构成非作者。记录真实作者/审查者/集成人及审查产物；GitHub无法区分同账户操作者时，不声称平台已强制职责分离，不伪造独立approve。独立性或权限不明交Owner，不新建聊天绕规。
- 非作者QC通过不自动给审查者合并执行权；DSH/Poly审查只产出QC，另一个Codex集成席仍须明确有效授权，不能拿一次性授权复用到后来PR。
- Codex可准备并开dev→main PR，main仍由人审查合并；不直推dev/main。本次迁移不批量批准#342、#354或任何KEY/HOLD事项，不批准交易或运行启动。

## 每任务内嵌复盘

在同一PR/交付回执补一小段，不新增定时巡检：验收耗时（冻结交付至接受）、首轮通过/失败、返工次数/原因、关键漏项、Bot发送/拒发次数及实际模型用量。未测字段保留 `UNKNOWN`，不从调用次数推算费用。
执行者给一项有效做法和一项下一轮改进，非作者指出最重要遗漏；只有经证据支持的改进才进入下一合法任务/验收包。通用经验交维护者intake，不直接写canonical。
按相同任务难度/验收口径比较质量、耗时和返工；证据不足标 `NON_COMPARABLE`，不宣称节省或胜率已提升。

## 纸面研究的胜率与质量

研究任务须先获准并预登记问题、样本/时间窗、基线、指标、停止与失败条件；固定PIT可得性，防未来信息泄漏，按事件簇隔离训练/验证/留出样本，不用同事件重复样本制造独立性。
联合报告胜率及不确定性、含费用/滑点/成交假设后的净期望、概率校准/Brier、回撤/方向敞口及缺失/失败样本；不能用胜率或纸面回测替代经济性/运行验收。
DSH做反证与稳健性审查，Codex收口可复现证据，Grok Build实现获准的离线工具；新数据权限、预算、阈值或阶段闸门需求升级Owner。保持paper、live_enabled=false，不承诺提升、不启动runtime。
方法依据：[Gneiting/Raftery 2007](https://sites.stat.washington.edu/people/raftery/Research/PDF/Gneiting2007jasa.pdf)支持用proper scoring规则评价概率预测，故本项目建议保留Brier/校准证据；[Bailey等2015](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf)讨论多次回测选择的过拟合，故须记录总尝试数及失败，不将单次holdout通过当经济性证明。这些是研究设计依据，未证明本项目交易胜率提升。

## 迁移顺序

1. 独立审查并由有权非作者/Owner合入本权责PR；本PR作者不得自行合并。只改治理入口/协议，不改队列、risk、代码、运行或排期。
2. 各端刷新dev后回ACK本协议commit/SHA、责任和未采用项；未验证DSH/Grok Build实际模型、账号、额度与交接链前，沿用人工固定包，不能宣称已自动接通。
3. 先闭环一件合法第一todo：唯一writer、非作者QC、符合作者回避的最终集成、目标dev验证、任务复盘。Codex既有任务不临时转给Grok Build并行重做。
4. 根据实际漏项/返工/耗时调整下一任务分工；长期研究留Codex+DSH，Grok Build扩充确定性工程，Poly只收短批次专长问题。频率或权限另需新的明确决定及对应生效证据。
