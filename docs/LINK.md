# 五端链接

五个入口不共享聊天记录：本机 Grok Build、Grok 网页端、Grok Bot、Codex 桌面、DSH（DeepSeek Harness）。它们只通过这个私有仓库交接：日常集成在 `dev`，Codex 主导协调和普通 PR 最终集成；Codex 可准备并开 `dev`→`main` PR，仍由人审查合并。统一协议见 [CODEX_COORDINATION.md](CODEX_COORDINATION.md)。

| 入口 | surface | 怎么接上 |
|---|---|---|
| 本机 Grok Build | `grok-build-local` | 打开本仓库目录。根上的 `AGENTS.md` 就是开工说明 |
| Grok 网页 | `grok-web` | 读 `docs/prompts/GROK_WEB.md`。每 30 分钟看一次队列；白天每小时还有一次保底 |
| Grok Bot | `grok-bot` | 读 `docs/prompts/GROK_BOT.md`。Poly指挥主要提供空闲时批量短问答、专长证据和实际阻塞 |
| Codex 桌面 | `codex-local` | 读 `docs/prompts/CODEX.md`。协调/编码/集成席：任务收口、单 writer、独审、普通 dev 最终门；作者回避 |
| DSH（DeepSeek Harness） | `dsh` | 读 `docs/prompts/DSH.md`。获准的长期研究、反证、非作者审查；领取任务时仍同队列同纪律 |

不要把本仓库注册进咖啡 OpenClaw workspace。

## 锁定

队列文件是 `docs/loop/state.json`。领取把第一件 `todo` 改成 `doing`，并写上 surface 和 1 小时截止时间。30 分钟一轮只为了发现过期领取和空闲队列，不是为了并行做两件任务。

领取、释放、`docs/loop/status.json` 和 `docs/LOOP.md` 的 `## Latest` 都是队列状态。Operator 2026-10-08 决定：这些改动也走 PR（base=`dev`），禁止直接推 `main` 或 `dev`。开分支，提交，开 PR，然后停止。不要 `git push origin main` 或 `git push origin dev`，不要自己合并。有权非作者合并进 `dev` 前，其他入口仍以 `dev` 上的队列为准，不会看见分支上的领取。

过期的领取可以接手。接手之前先看 `dev` 上这件任务是否已经做完；做完了就不要再实现一遍。

没有变化就不要开空 PR。释放写在 PR 里：任务标成 `done`，清掉 claim，并在 `## Latest` 写下五行。

## 工作区

写 git 的执行者都在 `.worktrees/<名字>` 里开 worktree 干活（`.worktrees/` 写进 `.git/info/exclude`），做完 `git worktree remove`。主目录留给 Operator 本机 Grok Build，只有它在主目录 `git pull`。提交用 `git -c user.name=ZackZhaii -c user.email=17690114@qq.com commit ...`，不改全局配置。

分支从 `origin/dev` 开，PR 的 base 一律是 `dev`。普通 `dev` PR 由 Codex 做最终集成；当前主控/实现worker参与实现时，交另一个未参与实现且获授权的Codex集成席或Owner，见 [CODEX_COORDINATION.md](CODEX_COORDINATION.md)。Codex 可准备并开 `dev`→`main` PR，仍由人审查合并。bot 不直推 `main` 或 `dev`，不自合并。升级给 Operator 的事项（risk.toml、live、风控阈值、KEY/HOLD 等）仍逐项人审。

## 不许演进到实盘

队列空了就停。新任务进队列只有两条路：人直接写进 `docs/LOOP.md` 和 `docs/loop/state.json`；或 bot 从 `docs/COORDINATION.md` 风险缺口清单挑一条纯代码项，开 PR 登记到这两个文件。PR 合并进 `dev` 就是 Operator 加入，关闭 PR 就是拒绝。清单外的任务 bot 不许自造。每个 bot 同时最多开着 2 个 PR；任务字母用 `dev`、`main` 和未关闭 PR 都没用过的下一个。
不允许任何一个入口自己把实盘、下单客户端或密钥访问加进队列。
