# polymarket-pod

纸面项目。你是三个入口之一：`grok-build-local`（本机 Grok Build）、`grok-web`、`grok-bot`。
共享记忆只有 GitHub `main`：https://github.com/ZackZhaii/polymarket-pod
不要相信上一轮聊天。不要碰咖啡业务、StarHive 或其他交易仓库。

## 每次开工

1. `git pull --ff-only`
2. 读 `docs/LINK.md`、`docs/LOOP.md`、`docs/loop/state.json`、`config/risk.toml`
3. `python src/loop_claim.py status`
4. `live_enabled` 不是 false，或风控钉被改过：停止，不要改文件
5. 有未过期的别人领取：停止
6. 没有 `todo`：停止。不要发明实盘任务
7. 否则只领取队列第一件 `todo`。锁定 1 小时。做完这一件，pytest，释放。队列状态只走 PR，禁止直接推 `main`，禁止自己合并。人合并后停止。不要在同一轮做第二件。监控是每 30 分钟看一次，有人持有未过期领取就停。

领取和释放：

```bash
python src/loop_claim.py claim --surface grok-build-local --task F
python src/loop_claim.py release --surface grok-build-local --done
```

网页端把 surface 写成 `grok-web`，Grok Bot 写成 `grok-bot`。

## 硬约束

- 不读 `.env`，不写密钥，不把密钥放进提交
- 不把 `live_enabled` 改成 true，不安装交易客户端，不下单，不提现，不加仓
- 不做对敲、自成交、刷量、幌骗，不写绕过地理限制的步骤
- 单边成交是方向敞口，不是锁定利润。不承诺收益
- 一次只做一件任务
- 队列状态（`docs/loop/state.json`、`docs/loop/status.json`、`docs/LOOP.md` 的领取、释放和 `## Latest`）禁止 `git push origin main`。开分支，开 PR，等人合并
