# Grok Bot ↔ 本机 Grok Build

这份契约只服务 polymarket-pod。不要把本仓库接进咖啡业务或其他 StarHive 域。

云端对话不能附着到你电脑里正在跑的 Grok Build 进程。
本机和云端之间的链接是这个 Git 仓库，不是远程控制你的电脑。

`config/risk.toml` 保持 `mode = "paper"`、`live_enabled = false`、`require_human_gate = true`。
谁都不读 `.env`。谁都不下单。

## 三条线

1. 本机 Grok Build
   - 目录只允许 `D:\\projects\\polymarket-pod` 或 `~/projects/polymarket-pod`（大阪 VPS 用 `~/polymarket-pod`）。
   - 新建一个名字就叫 `polymarket-pod` 的 Build 项目。不要复用咖啡或 StarHive 项目。
   - 每次新开一个对话。先读 `docs/LOOP.md`。只做状态不是 done 的下一件。做完就停。
   - 本机必须已经安装并登录 Grok Build CLI（`grok` 或 `agent`）。云端替不了这一步。

2. Grok Bot
   - Bot 必须和上面那个目录在同一台机器上，并且这个 Bot 有 Shell。
   - Bot 不自己改交易代码。它只把 LOOP 的下一件任务交给本机 Grok Build，然后停。
   - 不要把私钥、seed、API secret 贴进 Bot。
   - 第三方 Windows 桥 [sec-knight/grokbot-build-relay](https://github.com/sec-knight/grokbot-build-relay) 不是 xAI 官方。用之前自己读脚本。不要对它加 `--always-approve`。
   - 没有这个桥也可以：在同一台机器的 Grok Bot 里说「进入本仓库目录，用本机 grok / agent 无界面跑 LOOP 的下一件任务」。

3. 云端定时任务 `polymarket-pod-paper-loop`
   - 每天 10:30（Asia/Tokyo）读这个 GitHub 仓库。
   - 有未完成任务才开一个 Pull Request。不直接推 `main`，不自己合并。
   - 任务都是 done 就停，不发明实盘功能。
   - 本机用 `git pull` 接上已合并的 PR。这就是持续推进。

## 给本机 Grok Build 的开场白

```text
你是 polymarket-pod 的纸面工程代理。只在本仓库工作。
先读 docs/LOOP.md、docs/GROK_LINK.md、docs/PHASE0_RUN.md、config/risk.toml。
只做 LOOP.md 里还没勾掉的下一件任务。做完就停。
禁止读 .env 和任何密钥。禁止改 live_enabled。禁止下单、提现、自动加仓。
禁止对敲、自成交、刷量、幌骗。单边成交不是无风险套利。不承诺收益。
```

## 给 Grok Bot 的指令

粘到这个 Bot 的说明里。不要粘密钥。

```text
你只负责 polymarket-pod，目录是 D:\projects\polymarket-pod（没有 D 盘就用 ~/projects/polymarket-pod）。
不要打开咖啡、微信或其他交易目录。
每次只做一件事：读 docs/LOOP.md。若还有状态不是 done 的任务，用本机 Grok Build（grok 或 agent，无界面）在该目录执行那一件，然后停止并报告改了哪些文件。
若全部 done，不要改代码。只回复：live_enabled 仍是 false，今天没有新任务。
禁止读 .env。禁止安装会下单的客户端。禁止把 live_enabled 改成 true。
禁止对敲、自成交、刷量、幌骗。不承诺收益。
```

若使用第三方桥，命令形状如下。路径按本机改。先读那个仓库的脚本再跑。

```text
cd C:\path\to\grokbot-build-relay
.\scripts\bridge.ps1 prompt "读 docs/LOOP.md，只做下一件未完成任务。禁止 live，禁止读 .env。" -Cwd "D:\projects\polymarket-pod"
```

## 操作员仍然亲自做

- 在本机建好隔离目录，并登录 Grok Build CLI。
- 合并云端开出的 Pull Request 之前先看 diff。
- 不要把密钥贴进任何聊天。
- 纸面样本没有跑够 2 周之前，不要打开实盘闸。
