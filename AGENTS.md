# polymarket-pod

纸面项目。你是入口之一：`grok-build-local`（本机 Grok Build）、`grok-web`、`grok-bot`、`codex-local`（Codex 桌面，协调/编码/集成席）、`dsh`（DeepSeek Harness，研究/验证席）。
共享记忆只有 GitHub 仓库：https://github.com/ZackZhaii/polymarket-pod 。日常集成在 `dev`；`main` 只收 `dev`→`main` PR
不要相信上一轮聊天。不要碰咖啡业务、StarHive 或其他交易仓库。
Codex 主导日常协调与普通 `dev` PR 的最终集成；具体分工、作者回避和复盘见 `docs/CODEX_COORDINATION.md`。所有既有例行频率/触发方式不改。
下方领取/无 todo 停止条款约束工程实现；明确获准的协调、治理、审查和集成按统一协议核范围执行，不授予自行造代码任务或越过队列的权限。

## 每次开工

1. 同步 `dev`：Operator 本机 Grok Build 在主目录 `git pull --ff-only`；bot 和其他执行者只 `git fetch origin`，再从 `origin/dev` 开 worktree（见下方“写 git”）
2. 读 `docs/LINK.md`、`docs/LOOP.md`、`docs/loop/state.json`、`config/risk.toml`
3. `python src/loop_claim.py status`
4. `live_enabled` 不是 false，或风控钉被改过：停止，不要改文件
5. 有未过期的别人领取：停止
6. 没有 `todo`：停止。只可按下方“自选任务”开 PR。不要发明实盘任务，也不要发明清单外的任务
7. 否则只领取队列第一件 `todo`。锁定 1 小时。做完这一件，pytest，释放。队列状态只走 PR（base=`dev`），禁止直接推 `main` 或 `dev`，禁止自己合并。开完 PR 就停止。不要在同一轮做第二件。监控是每 30 分钟看一次，有人持有未过期领取就停。

领取和释放：

```bash
python src/loop_claim.py claim --surface grok-build-local --task F
python src/loop_claim.py release --surface grok-build-local --done
```

网页端把 surface 写成 `grok-web`，Grok Bot 写成 `grok-bot`，Codex 桌面写 `codex-local`，DSH 写 `dsh`。DSH 默认做获准的研究、反证与非作者审查，不抢队列第一件 `todo`；确实领取任务时与其他入口同队列同纪律。

## 写 git

- 写 git 的执行者都在独立 worktree 里干活：`git worktree add .worktrees/<名字> ...`，做完 `git worktree remove`。
  `.worktrees/` 写进 `.git/info/exclude`，不写 `.gitignore`
- 主目录留给 Operator 本机 Grok Build。只有它可以在主目录 `git pull`；别的执行者在主目录只做 `git fetch` 和 worktree 增删
- 提交身份：`git -c user.name=ZackZhaii -c user.email=17690114@qq.com commit ...`。不改全局 git 配置
- 分支从 `origin/dev` 开，PR 的 base 一律是 `dev`。普通 `dev` PR 由 Codex 按 `docs/CODEX_COORDINATION.md` 做最终检查并集成；当前主控/实现worker参与实现时，交另一个未参与实现且获授权的Codex集成席或Owner，任何作者都不自合并。Codex 可准备并开 `dev`→`main` PR，仍由人审查合并。bot 不直推 `main` 或 `dev`。升级给 Operator 的事项（risk.toml、live、风控阈值、KEY/HOLD 等）仍逐项人审，永不自动合并

## 自选任务

- bot 只能从 `docs/COORDINATION.md` 的风险缺口清单（Risk gaps backlog）挑纯代码项，不许自造清单外的任务
- 以 PR 提出：同一个 PR 把任务登记进 `docs/LOOP.md` 和 `docs/loop/state.json`。PR 合并进 `dev` 就是 Operator 加入队列，关闭 PR 就是拒绝
- 每个 bot 同时最多开着 2 个 PR（自选任务、文档、验收都算）
- 任务字母：开 PR 时用 `dev`、`main` 和所有未关闭 PR 都没用过的下一个字母；Z 之后依次用 AA、AB、AC……（AZ 之后是 BA，以此类推）。

## 硬约束

- 不读 `.env`，不写密钥，不把密钥放进提交
- 不把 `live_enabled` 改成 true，不安装交易客户端，不下单，不提现，不加仓
- 不做对敲、自成交、刷量、幌骗，不写绕过地理限制的步骤
- 单边成交是方向敞口，不是锁定利润。不承诺收益
- 一次只做一件任务
- 队列状态（`docs/loop/state.json`、`docs/loop/status.json`、`docs/LOOP.md` 的领取、释放和 `## Latest`）禁止 `git push origin main` 和 `git push origin dev`。开分支，开 PR（base=`dev`），等有权非作者按 `docs/CODEX_COORDINATION.md` 集成
