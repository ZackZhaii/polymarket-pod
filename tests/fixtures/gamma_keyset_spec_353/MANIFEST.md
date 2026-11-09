# Gamma keyset fixtures (2026-10-21 22:30 北京, dev 0cb7188)
19 次公开 GET，间隔 1.5s，UA=polymarket-pod-phase0。只保留 pod 实际读取的 13 个字段和响应头（content-type/cache-control/deprecation/sunset/warning），不含任何地址或密钥。
- id-<id>-legacy.json：`/markets?id=<id>`（closed 样本另加 `&closed=true`）
- id-<id>-keyset.json：`/markets/keyset?id=<id>`（同上），含 top_keys 和 next_cursor
- 候选 5 个：562831、601819、601826、562830、2589812；已结算样本：6006152
- probe-multi：重复 id= 三个；probe-missing / missing_legacy：不存在的 id；probe-empty：`id=` 为空；probe-closed_wrong_view：已结算 id 不带 closed=true
- request-log.json：每个请求的状态码和响应头
