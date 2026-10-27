# 真实执行接入设计 SPEC（待审阅）

状态：`PROPOSED_DESIGN / NOT_IMPLEMENTED / NOT_RUNTIME_ACTIVE / LIVE_AUTHORIZATION_NOT_GRANTED`。本文件是架构规格，不是实施计划、入队任务、发布批准或真实适配器。日期：2026-10-25 JST。

## 目的、来源与边界

用户希望加快可审查的上线准备，同时保留 live OFF。目标是定义最小真实订单闭环，让以后的人能够判断“确实提交/成交/撤销/结算了什么”，再决定是否允许有限通道试验。成功不等于获得收益。当前试验资金、累计最大亏损、账户资格、密钥托管、返回模型与真实成本均 UNKNOWN。

权威代码基线：`D:/projects/polymarket-pod` 的 `origin/dev=0cb71883af8d94f92795ba4791020d5b91252562`；本轮用 `git show origin/dev:<path>` 读取，未将根目录 main 当 dev。证据编号见 EVIDENCE.json。共享条件：相邻 `00-control/BRIEF.md`，SHA256 `98cc06af1630f9f62ba5d84f5adaf20332a018e387e703ce810741d459d7167a`；START.json 是开工快照，不是持续运行证明。

本轮仅写本文件夹，未读取 .env、账户、凭据，未运行应用/测试/SDK/交易。所有接口及新路径均为提案，尚不存在或未改。没有批准日期、收益率、上线日期或自筹资金。

## 现状事实与推论

| 事实 | 证据 | 意义 |
|---|---|---|
| runner 常量 executor=False，造 intent 强制 paper=True，调用 paper_ledger | E01–E03 | 当前真实订单入口缺失，不能配置切换 |
| live/mode/human gate不符即全局 halt；RiskGate 与 builder 还检查 Phase0 pins | E04、E09 | live flag=true 会停，不能当启用方式 |
| 本地 allow 构造 fill 并写 PAPER_PATH，然后发 Executor fill | E03 | allow/本地 fill 不是交易所成交 |
| halt 撤本地 paper quote、exchange_action=none | E05 | KILL/halt 不证明真实挂单取消 |
| 账本从 PAPER_EQUITY_USD 回放；对账比 paper 和公开钱包数量 | E06、E07 | 不能直接承载真实账户现金、冻结、费用、转账与赎回 |
| resolution collector+settlement 处理纸面结算；writeoff hook 是 no-op | E08 | 结果确定、持仓可赎回、实际到账必须区分 |
| guard 禁订单依赖、限制 URL；no-live测试要求 executor不变 | E10 | 增加适配器必须显式审查项目边界，不能让旧guard静默放宽 |

推论：需要新执行与账户事件语义，不能把纸面账本改名为 live。已有 paper 的 fsync/journal/锁/显式 halt 是可复用模式，不是已通过真实环境验证的模块。

## 备选方案与推荐

| 方案 | 范围、优点 | 成本和限制 | 选择 |
|---|---|---|---|
| A 隔离的最小适配层 | 保留 M2 paper runner、paper账本与guard；新单一入口、持久订单状态、账户投影、受限 transport；复用纯计算/身份/记录模式 | 需要明确live政策边界与新schema；不能只包一层SDK；最危险的是共享函数偷偷夹带paper assumptions | 推荐，先单账户、单venue、单腿限价buy、无做市/自动补腿/杠杆/自动资金移动 |
| B 全面重写 scanner/forecast/risk/executor/ledger | 可统一事件模型 | 影响校准与历史证据；重做已通过的paper回归，较难证明无行为漂移 | 不推荐作为最小接入；真需求出现再独立设计 |
| C 仅人工在官方界面做交易 | 能独立验证账户基本通道 | 无法证明本项目提交、重启、撤单闭环；人工记录也不能冒充自动集成验收 | 仅在Owner另行决定时可作为外部证据，不代替A |

A 的设计范围不含做市奖励、complete-set套利、负风险转换、跨市场双腿、自动赎回或策略规模放大。每个新增能力另立规格和审批。真实卖出仅作为已持仓减仓扩展；通道首轮是否允许减仓、持有至结算由Owner明确，不由程序猜测。

## 三阶段与晋级条件

| 阶段 | 允许动作（未来获准后） | 出口证据 | 不能据此得出 |
|---|---|---|---|
| S0 shadow/offline contract | 纯脱敏夹具、故障注入、影子决策；无sign/submit/cancel/资金操作，在线公开读取也须另核范围 | 所有关键合同用例；paper no-live不退化；完整provenance；独审 | 账户可用、真实成交、盈利 |
| S1 approved bounded channel test | 独立的人签名授权清单约束单账户/市场token/方向/限价/最大总花费/最大累计亏损/次数/失效时间/撤单与处置权限；默认次数0 | 真实外部订单ID、成交证据、撤单终态、账户前后余额/持仓/费用、断线恢复及收口回执 | 策略有正EV、可扩大资金、下一轮仍获准 |
| S2 strategy live | S1通过；另有PIT/OOS/校准/净成本/集中度/资金与累计损失批准；生产告警和人工响应就绪 | 策略、模型、风险policy、数据截止和授权绑定的promotion receipt | 长期收益保证、自动扩仓 |

S0→S1至少需要：书面SPEC批准，再书面实施计划+执行方式批准；Operator合法立项且项目“不许自行演进实盘”限制已获适用的人审决定；适配器与依赖/URL/签名边界获批；离线独审通过；账户资格/托管与真实API合同已核验；填写资金/损失/单次权限。任何UNKNOWN不自动通过。S1→S2是独立经济晋级。S1为测管道可能不符合策略edge；只能有显式的 `channel_test` 审批和限额，不能伪造概率或放松production策略gate来通关。

## 推荐数据流与职责

既有 `m2_runner -> build_order_intent(paper=True) -> paper_ledger` 原样保留。拟议入口 `execution_controller` 从一份封存intent/人工通道测试提案读取，接收新鲜账户快照与政策授权，得到 `ExecutionPermit`，原子预留预算并持久化命令后才调用transport一次。成交observations单独落地，账户投影据真实事件更新，再独立对账。

- `Intent` 表达“想做什么”，不证明已执行；信号概率和paper fill不能进入真实账户投影。
- `ExecutionPermit` 表达一次短时批准，绑定 intent/policy/config/account快照hash、市场、token、方向、价格、股数、最坏费用和总成本上限、到期时间、授权receipt。permit不可转移、不可扩大数量、不能跨进程重复花费。
- transport只负责映射已核官方API合同，禁止自行计算仓位、补单、重试、恢复或改policy。凭据只在未来获准的隔离transport边界处理，不进入这些导出。
- controller负责持久状态、单writer锁、reserve/submit/cancel/observe/recover。对账是不同读者；差异触发停新单并保留观察/撤单能力。
- 策略风险只消耗真实账户state：现金可用/冻结、已成交持仓、所有开放订单剩余敞口、UNKNOWN提交最坏预留、未最终费用、其他资金事件。允许不是成交，部分成交立即计单腿方向敞口。

## 非可执行机器合同（字段与不变量）

这是文档合同，不是代码/SDK签名或当前API保证。价格/数量/金额用定点十进制字符串+明确asset/unit，禁止float混算、NaN/inf、隐含零。原始响应允许脱敏hash与受控证据定位，禁止签名/secret入日志。

| 记录 | 必需字段 |
|---|---|
| ExecutionCommand v1 | command_id、run_id、mode(channel_test/strategy_live)、account_ref(非secret)、venue、intent_id、decision_id、idempotency_key、canonical_payload_sha256、permit_id、token/condition、side、limit_price、shares、max_all_in_cost、fee_bound、expires_at、created_at、schema_version |
| ExecutionPermit v1 | permit_id、command_id、policy_sha、account_snapshot_sha、book_snapshot_sha、approval_receipt_ref、allowed_actions、max_notional、max_cumulative_loss、max_attempts、expiry、gate_result、reasons |
| OrderObservation v1 | observation_id、command_id、exchange_order_id可空但空有reason、external_event_id、source、source_timestamp、observed_at、status、cumulative_fill_shares、remaining_shares、actual_fee或UNKNOWN、evidence_ref/hash、chain_finality状态 |
| AccountSnapshot v1 | snapshot_id、account_ref、asof+observed_at、cash_total/available/reserved、collateral_asset/chain、per_token settled/pending/available shares、open_orders完整性/页游标、fills watermark、fee/deposit/withdraw/transfer/redeem事件、source/freshness/coverage、problems |
| ExecutionReceipt v1 | command_id、intent/policy/approval/source hashes、最终状态或UNRESOLVED、all event IDs、实际成本/费用/余额变动、残余订单/持仓/预留、stop_new_orders/cancel_confirmed分别列、reconcile_verdict、UNKNOWN清单 |

外部 event 唯一性须由已核API合同证明；不能拿 `transaction_hash+token_id+side` 当唯一fill键（E07明确已有多fill语义）。重复相同事件只应用一次；相同ID不同内容全局停新单。签名报文hash、交易所order hash/ID、client key的关系必须由官方合同和夹具证明；未证明时保持UNKNOWN，不能声称exactly once。

## 状态与提交不确定性

| 状态 | 进入证据 | 允许下一步 | 风险处理 |
|---|---|---|---|
| PROPOSED | 封存intent/命令 | 验证并拒绝或reserve | 不调用transport |
| RESERVED | permit有效+预算原子预留+日志fsync | 提交前再查halt/过期/账户版本 | 预留最坏all-in成本 |
| SUBMITTING | 完整规范payload/hash已fsync，记录本次attempt | 一次submit | 进程崩溃也视可能已发出 |
| ACK_OPEN | 官方订单ID和有效受理证据 | 观察、受权撤单 | 剩余股数×限价+费用预留 |
| SUBMIT_UNKNOWN | 超时/连接丢失/不可解析响应/提交后落盘失败/崩溃于SUBMITTING | 仅按order/hash/账户成交与开放订单作只读调查；或已授权范围撤单 | 禁新单、禁止盲重试/换ID补单，保留最大预留 |
| PARTIALLY_FILLED | 可核外部fill与累计数 | 观察余单或获权撤余单 | 已成交仓位+未成交预留同时计入；不补另一腿 |
| CANCEL_REQUESTED | 撤单请求已持久化/发送 | 继续查单与成交 | 不是已撤，余单仍可能成交 |
| CANCELED_CONFIRMED | 可信最终订单状态，累计成交与填充明细一致，余量不可再成交 | 对账后释放余量预留 | 已成交仓位不释放、不写成零 |
| FILLED_CONFIRMED | 最终量/实际fill明细/费用状态可核 | 对账，持有或另获权处置 | 无活跃订单不等于无持仓 |
| REJECTED_CONFIRMED | 官方确定拒绝且无受理/成交矛盾 | 对账后释放预留 | 网络错误/HTTP异常不自动等于拒绝 |
| RECONCILIATION_HOLD | 缺页/陈旧/数量或现金差异/未知订单 | 停新单，保留控制面，人工解决 | 不强行改账使差异消失 |

终态可以收到迟到事件：以唯一外部fill核对累计量，发现终态与新证据冲突即HOLD并重新核对，不丢掉迟到fill。重复请求的安全性只有当官方幂等语义已验证且请求字节完全一致才可考虑；本最小通道设计不自动重发UNKNOWN提交。一次“查不到”尤其索引滞后，不证明未提交。

## 撤单、部分成交和恢复

撤单目标必须是账户范围/订单ID范围内已经核实的自身订单；不存在无批准的全账户取消。撤单权限单独保留，禁止因停新单而切掉控制面。cancel transport返回成功仅是请求结果，必须再查最终状态及期间成交；余量释放在确认与对账后。批量cancel部分失败逐订单保持UNKNOWN并报警，不能输出“全部已撤”。

重启第一步获取唯一writer锁，然后读取不可变事件、重建reserve和非终态，校验配置/授权/时钟；在任何新submit前读取真实开放订单、成交、账户现金与持仓，完成游标连续性、分页、去重与freshness检查。每个SUBMITTING一律按SUBMIT_UNKNOWN恢复。非本程序订单、人工资金活动、不明fill/账本缺口均HOLD，不能删掉它们或只对程序订单看似守恒。重启不可恢复过期试验授权，也不得自动恢复strategy_live。

## 资金与结算证明

真实账户账不从 PAPER_EQUITY_USD 初始化。Owner批准的初始现金/持仓快照与实际资金来源匹配后才开放预留；可用现金应扣真实冻结、未成交最大预算、UNKNOWN预算、未确定费用，避免同时将交易所冻结与本地同一reserve重复扣算。每个来源与重叠规则须在新schema中显式标识。总现金=可用+冻结等身份关系按官方来源口径校验，不能混用USD/USDC或不同链/抵押品。

市场解析“已resolved”只给估值/可赎回候选；`redeemable`不等于到账。最小方案不自动赎回/转账：Owner在外部获准操作后，以链ID、tx hash、block/finality、token余额变化、抵押品到账、实际费用和账户投影建立SettlementReceipt；索引/API与链证据不一致为PENDING/HOLD，不提前记现金。重组/撤销证据不得覆盖历史，追加反向事件并HOLD。finality要求、官方数据来源、账户产品类型和赎回机制本轮UNKNOWN，必须在S1前选定并验收。仅Gamma结果+paper settlement行不足。

## 风险、权限与故障默认

现有 paper pin/guard不移除、不改成全局allow。新增live policy必须是Owner审阅的新profile/新边界，旧paper验收仍要求完全无订单依赖、凭据读取、订单URL。`risk_gate.evaluate`目前含Phase0 pins；不能传假的paper cfg、猴子补丁pin或跳过它宣称“复用风控”。未来提案应将经回归证明的纯约束函数提取为共享内核，原evaluate保持旧行为；live wrapper独立做真实账户cap/真实费用/授权检查，差异逐条评审。channel_test与strategy_live分开policy；两者都不能绕过自成交/资格/账户完整性/价格数量/资金/累计损失/急停。

fail closed指停止新增风险，保留事件落盘、账户观察与获准撤单。缺账户、缺fee、不可验证时钟、陈旧book、坏日志、锁失败、审批过期、预留失败、取消未知、账差，均不得submit。不能把current paper 1%/3%/$1000读为实盘资金或累计损失授权。

## 回滚与人工动作

分开显示 `NEW_ORDERS_STOPPED`、`CANCEL_REQUESTED`、`CANCEL_CONFIRMED`、`POSITIONS_REMAIN`、`CASH_RECONCILED`。第一动作是失效permit并停新单，接着按既定撤单权限撤已知余单并逐单确认；保留观察直到确认。KILL进程不能代替撤单确认，回退版本不能删除order journal或UNKNOWN预留。存在非终态/真实持仓时，不允许以卸载/恢复paper配置宣称回滚完成；人工通过已批准官方渠道核对与处置，才写外部证据。确认真实通道无活单且账差收口后，方可停止控制面。历史receipt append-only，原sealed/package不改。

## 立项、估算与未决选择

`docs/LINK.md:31–34`禁止任何入口自行把实盘/客户端/密钥访问加队列。已有backlog #7只涉及Kelly影子→真正定尺寸，并不授权live；#47/EG只补settlement-blocking halt（E11），已有PR360由共享START/BRIEF提供，未在本lane重新在线核验。本设计不领取EG，不发新字母、不登记队列、不创建PR。需要Operator明确提出并合法入队新范围；KEY/HOLD/配置/客户端/账户审批分别满足，普通技术review不代替Owner。

规划估算（不是交期或收益承诺；按熟悉仓库工程师、单账户单token限价buy、已可用官方合同假设）：A文档/合同细化与独审2–4人日；隔离原型+故障夹具5–9人日；账户/撤单/恢复/结算证据设计与回归4–8人日；部署演练与独审3–5人日，合计14–26人日。阶段可部分重叠，但不能以并行略过关卡。B全面重写约30–60人日且不确定性更大。S1外部操作时长取决资格/市场/结算，UNKNOWN；人工响应成本、基础设施、模型token、官方费用/转账费、资金占用均UNKNOWN，不以零计。资金损失预算由Owner给出，工程工时不能推算安全试金额。

Owner需决定：①是否批准此书面SPEC作为后续计划输入；②首轮授权资本、全期累计损失、总单数、过期时间与撤单/持仓处置权限；③托管/账户/资格与官方合同核验负责者及允许边界；④通道试验和策略经济晋级的独立验收者。当前均未批准/UNKNOWN；停在设计审阅。

## 官方协议版本兼容关卡（实施前证据，当前UNKNOWN）

不得假定旧版SDK的 `tokenId`、新版文档的 `asset_id`、签名domain、chain/collateral、order type、认证层次、授权/approval或redeem路径跨版本通用。本lane未在线核验官方协议；03-profit-research的官方资料是候选输入，仍需在实施前以批准时的官方schema snapshot固定兼容矩阵：API/SDK版本与发布hash、网络/chain、资产标识及与condition/outcome映射、账户/签名类型、签名domain及expiry/nonce、client key或orderhash幂等语义、submit/cancel/query/fill事件字段、费用口径、余额/冻结来源、抵押品单位、结算/赎回机制、资格限制。每行需要官方URL+检索日期+schema/hash+脱敏contract fixture；缺项UNKNOWN会阻止适配器实现中的相应行为及S1，而不是按旧例推断。当前不安装SDK、不生成签名、不选择账户或approval路线。

补充：资产矩阵应明确CTF token ID与Protocol V2 position ID的适用关系，不能把同名asset字段直接连接旧token_id；抵押品可能有不同产品单位，pUSD/USDC及其兑换/可用/冻结定义必须独立证明。min_order_size、tick_size、每市场feeSchedule、舍入方向与所有订单类型支持同属必填矩阵。03 lane对当前官方文档的反馈尚待其封存SOURCES精确URL核对，本lane不把该反馈写成独立官方已验证事实；这些字段保持UNKNOWN。maker rebate/reward不进入最低成本抵扣，除非已实际获得并有账户证据。
