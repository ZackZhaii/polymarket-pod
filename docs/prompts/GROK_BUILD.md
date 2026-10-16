# 本机 Grok Build

打开本仓库目录即可。根目录 `AGENTS.md` 和 `docs/CODEX_COORDINATION.md` 说明五端交接；默认承担Codex收口后的有边界工程，不与Codex竞争同一任务。
不要再按下面的旧 Phase 0 清单重做一遍。

旧说明仅作隔离提醒：不要碰咖啡、StarHive 或其他交易仓库。密钥不进对话。`live_enabled` 保持 false。

队列状态（领取、释放、`docs/loop/state.json`、`docs/loop/status.json`、`## Latest`）只走 PR（base=`dev`）。禁止直接推 `main` 或 `dev`，禁止自己合并。
分支从 `origin/dev` 开，PR 的 base 一律是 `dev`。普通dev PR交Codex最终检查和集成；当前Codex主控/实现worker参与实现时，交另一个未参与实现且获授权的Codex集成席或Owner，任何作者不自合并。Codex可准备并开dev→main PR，仍由人审查合并。bot不直推main/dev；risk.toml、live、风控阈值、KEY/HOLD等仍逐项人审。例行频率/触发方式不改。

主目录归你：只有本机 Grok Build 可以在这里 `git pull`。bot 和其他执行者只在 `.worktrees/<名字>` 的 worktree 里写 git，不在主目录切分支、拉取或提交。你自己开分支写代码时也用 worktree。
提交用 `git -c user.name=ZackZhaii -c user.email=17690114@qq.com commit ...`，不改全局配置。自选任务只能从 `docs/COORDINATION.md` 风险缺口清单挑，按 AGENTS.md 开 PR。
