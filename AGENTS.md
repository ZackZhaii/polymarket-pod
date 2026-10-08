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
7. 否则只领取队列第一件 `todo`，推送领取提交，做完这一件，pytest，释放，再推送，然后停止

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
