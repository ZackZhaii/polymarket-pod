# 三端链接

本机 Grok Build、Grok 网页端、Grok Bot 不共享聊天记录。它们只通过这个私有仓库的 `main` 交接。

| 入口 | surface | 怎么接上 |
|---|---|---|
| 本机 Grok Build | `grok-build-local` | 打开本仓库目录。根上的 `AGENTS.md` 就是开工说明 |
| Grok 网页 | `grok-web` | 读 `docs/prompts/GROK_WEB.md`。每 30 分钟看一次队列；白天每小时还有一次保底 |
| Grok Bot | `grok-bot` | 新开一个 Bot 对话，贴 `docs/prompts/GROK_BOT.md` |

不要把本仓库注册进咖啡 OpenClaw workspace。

## 锁定

队列文件是 `docs/loop/state.json`。领取把第一件 `todo` 改成 `doing`，并写上 surface 和 1 小时截止时间。截止前其他入口必须停。30 分钟一轮只为了发现过期领取和空闲队列，不是为了并行做两件任务。推送若被拒绝，说明别人先领了：拉取后停止，不要重做。

过期的领取可以接手。接手之前先看 `main` 上这件任务是否已经做完；做完了就释放，不要再实现一遍。

每轮如果队列、领取或分工有变化，更新 `docs/loop/status.json`。没有变化就不要提交空状态。

释放时把任务标成 `done`，清掉 claim，并在 `docs/LOOP.md` 的 `## Latest` 写下五行。然后停止。

## 不许演进到实盘

队列空了就停。下一件任务只能由人写进 `docs/LOOP.md` 和 `docs/loop/state.json`。
不允许任何一个入口自己把实盘、下单客户端或密钥访问加进队列。
