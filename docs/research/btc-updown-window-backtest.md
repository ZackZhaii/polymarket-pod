# BTC 5m / 15m Up/Down: time-window entry paper backtest (Task FI, docs only)

Offline research note. **Not a strategy, not wired into anything, no orders.** Data came only from public read-only
GETs (Gamma `/events?slug=`, CLOB `/prices-history`); no auth, no keys, no trading SDK, no `pip install`.
`config/risk.toml` untouched, `live_enabled=false`. Every number below is copied from one actual run of the script in
the appendix (run on the Grok Bot box, 2026-11-10 ~13:22–13:30 Asia/Shanghai).

## 中文摘要

- 声称：「BTC 5 分钟 / 15 分钟 Up/Down 市场，只在特定时间窗（如窗口末段）入场就能盈利」。
- 样本：15m 共 1335 个已结算市场（2026-10-27 12:00 至 11-10 11:45 北京时间开盘，14 天；另有 9 个查不到事件），
  5m 共 1152 个（2026-11-06 12:00 至 11-10 11:55 北京时间，4 天）。手续费按 Gamma `feeSchedule`（rate 0.07，仅吃单）
  与官方公式 `fee = C × 0.07 × p × (1−p)`；滑点按价格 +0/+0.01/+0.02 三档；每笔固定 10 USD。
- 结论：**没有复现出稳健的盈利。** 在 15m 第 12–14 分钟、5m 第 4 分钟买入领先方（0.80–0.99 区间）这类「末段入场」规则，
  滑点 0.01 时 15m 的 9 个格子里 4 个为正（最好 +33.22 USD/223 笔），5m 的 3 个格子全部为负；滑点 0.02 时仅 1 个格子为正。
  每行按平均收益选出的最好格子，t 值最高只有 +1.16（15m、买领先方、滑点 0）。前半段挑出的正收益格子（15m 39 个、5m 9 个）放到后半段，
  分别只有 5 个、2 个仍为正，合计后半段 PnL 为 −7535.72 / −778.74 USD。表现为多重比较噪声，而不是可交易优势。
- 主要局限：价格历史约 1 分钟一点，不是可成交的卖一价；没有历史盘口深度；样本只有 14 天 / 4 天；
  「收盘后（after-the-bell）入场」无法用这些数据诚实测试（见注意事项）。

## Claim tested

"BTC 5-minute / 15-minute Up/Down markets, entering only in certain time windows (e.g. late in the window /
after-the-bell), is profitable."

## Data

- Markets: event slugs `btc-updown-15m-<start_unix>` and `btc-updown-5m-<start_unix>` (Gamma series
  `btc-up-or-down-15m` / `btc-up-or-down-5m`), one per window, walked backwards from a fixed end
  `1794283200` (2026-11-10 04:00 UTC = 12:00 Asia/Shanghai): 1344 consecutive 15m windows and 1152 consecutive 5m windows.
- Outcome: Gamma `outcomePrices` of a `closed` market, `["1","0"]` = Up won, `["0","1"]` = Down won.
- Prices: CLOB `prices-history?market=<Up token>&startTs=<start-120>&endTs=<end+120>&fidelity=1`.
  Down is taken as `1 - Up` (no separate Down history fetched).
- Usable sample:
  - 15m: **1335** markets, window starts 2026-10-27 04:00 UTC to 2026-11-10 03:45 UTC (12:00 to 11:45 Asia/Shanghai);
    Up won 633. Excluded: 9 slugs with no Gamma event, a contiguous block 1793508300..1793515500
    (2026-11-01 04:45–06:45 UTC, which is around the US DST change; not investigated).
  - 5m: **1152** markets, 2026-11-06 04:00 UTC to 2026-11-10 03:55 UTC; Up won 565. None excluded.
- Price-point spacing inside the window (all usable markets): 15m n=19577 gaps, median 60 s, p90 72 s, max 548 s;
  5m n=5714 gaps, median 60 s, p90 72 s, max 210 s.
- Every usable market had the same Gamma `feeSchedule`: `{"exponent": 1, "rate": 0.07, "rebateRate": 0.2, "takerOnly": true}`.

## Method

- Decision time: window start + `k` minutes (15m: k = 1..14; 5m: k = 1..4). Price used = the last Up history point with
  timestamp `<=` decision time (no look-ahead) and at most 90 s old; otherwise no trade.
- Favourite side = the side with price `>= 0.5`; its price `pf` must fall in a band
  `[0.50,0.60) [0.60,0.70) [0.70,0.80) [0.80,0.90) [0.90,0.95) [0.95,0.99)`.
- `side=fav` buys the favourite at `pf`; `side=dog` buys the other side at `1 - pf`.
- Entry price `e = min(price + slip, 0.999)`, `slip` in {0, 0.01, 0.02} (stands in for half-spread + adverse
  selection; real historical spreads/depth are not available, see caveats).
- Stake 10 USD per trade, shares `= 10 / e`, held to resolution (payout 1 per winning share).
- Fee (taker, at entry only; settlement has no fee): `shares × 0.07 × e × (1 − e)`. Source: Polymarket docs
  "Fees" (https://docs.polymarket.com/trading/fees, fetched 2026-11-10: `fee = C × feeRate × p × (1 − p)`, Crypto
  taker rate 0.07, makers 0) and the per-market Gamma `feeSchedule.rate = 0.07` above. Taker rebates (tiered) and
  maker rebates are ignored; fee rounding to 5 decimals is ignored.
- Per cell (market × side × slip × k × band): trades n, win rate, average PnL per trade, cumulative PnL, max drawdown
  of the cumulative PnL in chronological order. One trade per market per cell.
- Robustness: (1) a summary across all cells with n >= 30; (2) an out-of-sample check: split each market type in time
  halves (15m at 1793682000 = 2026-11-03 05:00 UTC; 5m at 1794110400 = 2026-11-08 04:00 UTC), select every cell
  with n >= 30 and positive average PnL in the first half (slip 0.01), and score the same cells on the second half.

## Results

### Headline: "late in the window" rules (buy the favourite at 0.80–0.99)

| market | slip | minute k | band | n | win | avg$ | cum$ | maxDD$ |
|---|---|---|---|---|---|---|---|---|
| 15m | 0.0 | 12 | [0.80,0.90) | 223 | 88.3% | +0.262 | +58.49 | 38.18 |
| 15m | 0.0 | 12 | [0.90,0.95) | 164 | 89.6% | -0.393 | -64.43 | 71.31 |
| 15m | 0.0 | 12 | [0.95,0.99) | 265 | 98.9% | +0.139 | +36.86 | 10.03 |
| 15m | 0.0 | 13 | [0.80,0.90) | 165 | 83.6% | -0.339 | -56.00 | 101.41 |
| 15m | 0.0 | 13 | [0.90,0.95) | 148 | 92.6% | -0.061 | -8.96 | 32.74 |
| 15m | 0.0 | 13 | [0.95,0.99) | 278 | 98.2% | +0.070 | +19.57 | 30.62 |
| 15m | 0.0 | 14 | [0.80,0.90) | 133 | 85.0% | -0.185 | -24.62 | 66.37 |
| 15m | 0.0 | 14 | [0.90,0.95) | 123 | 95.1% | +0.202 | +24.79 | 35.77 |
| 15m | 0.0 | 14 | [0.95,0.99) | 216 | 98.6% | +0.109 | +23.54 | 18.93 |
| 15m | 0.01 | 12 | [0.80,0.90) | 223 | 88.3% | +0.149 | +33.22 | 41.35 |
| 15m | 0.01 | 12 | [0.90,0.95) | 164 | 89.6% | -0.489 | -80.17 | 84.39 |
| 15m | 0.01 | 12 | [0.95,0.99) | 265 | 98.9% | +0.043 | +11.36 | 10.02 |
| 15m | 0.01 | 13 | [0.80,0.90) | 165 | 83.6% | -0.445 | -73.45 | 115.15 |
| 15m | 0.01 | 13 | [0.90,0.95) | 148 | 92.6% | -0.160 | -23.71 | 42.53 |
| 15m | 0.01 | 13 | [0.95,0.99) | 278 | 98.2% | -0.025 | -6.99 | 34.19 |
| 15m | 0.01 | 14 | [0.80,0.90) | 133 | 85.0% | -0.293 | -38.91 | 71.44 |
| 15m | 0.01 | 14 | [0.90,0.95) | 123 | 95.1% | +0.099 | +12.20 | 37.88 |
| 15m | 0.01 | 14 | [0.95,0.99) | 216 | 98.6% | +0.013 | +2.84 | 22.47 |
| 15m | 0.02 | 12 | [0.80,0.90) | 223 | 88.3% | +0.038 | +8.56 | 45.83 |
| 15m | 0.02 | 12 | [0.90,0.95) | 164 | 89.6% | -0.583 | -95.55 | 97.76 |
| 15m | 0.02 | 12 | [0.95,0.99) | 265 | 98.9% | -0.032 | -8.43 | 15.58 |
| 15m | 0.02 | 13 | [0.80,0.90) | 165 | 83.6% | -0.548 | -90.47 | 128.56 |
| 15m | 0.02 | 13 | [0.90,0.95) | 148 | 92.6% | -0.258 | -38.14 | 52.30 |
| 15m | 0.02 | 13 | [0.95,0.99) | 278 | 98.2% | -0.098 | -27.27 | 37.02 |
| 15m | 0.02 | 14 | [0.80,0.90) | 133 | 85.0% | -0.397 | -52.86 | 76.39 |
| 15m | 0.02 | 14 | [0.90,0.95) | 123 | 95.1% | -0.001 | -0.11 | 41.45 |
| 15m | 0.02 | 14 | [0.95,0.99) | 216 | 98.6% | -0.061 | -13.09 | 25.94 |
| 5m | 0.0 | 4 | [0.80,0.90) | 227 | 85.5% | -0.057 | -12.99 | 78.97 |
| 5m | 0.0 | 4 | [0.90,0.95) | 174 | 90.8% | -0.247 | -42.97 | 78.24 |
| 5m | 0.0 | 4 | [0.95,0.99) | 269 | 97.4% | -0.003 | -0.71 | 57.03 |
| 5m | 0.01 | 4 | [0.80,0.90) | 227 | 85.5% | -0.167 | -37.93 | 92.51 |
| 5m | 0.01 | 4 | [0.90,0.95) | 174 | 90.8% | -0.345 | -59.98 | 88.74 |
| 5m | 0.01 | 4 | [0.95,0.99) | 269 | 97.4% | -0.098 | -26.24 | 61.95 |
| 5m | 0.02 | 4 | [0.80,0.90) | 227 | 85.5% | -0.274 | -62.25 | 106.18 |
| 5m | 0.02 | 4 | [0.90,0.95) | 174 | 90.8% | -0.440 | -76.60 | 99.00 |
| 5m | 0.02 | 4 | [0.95,0.99) | 269 | 97.4% | -0.170 | -45.69 | 65.80 |

At slip 0.01: 15m has 4 of 9 cells positive (best: k=12, [0.80,0.90), n=223, +33.22 USD total, +0.149 USD/trade);
5m has 0 of 3 positive. At slip 0.02, 1 of 12 late cells is positive (15m k=12 [0.80,0.90), +8.56 USD). High win
rates (83–99 %) are what the entry price already pays for; after fee and slip the average is within roughly ±0.5 USD per
10 USD trade.

### Summary over all cells (n >= 30)

| market | side | slip | cells n>=30 | cells avg>0 | best cell (k, band): n, win, avg$, cum$, maxDD$, t |
|---|---|---|---|---|---|
| 15m | fav | 0.0 | 71 | 21 | k=1 [0.60,0.70): 160, 69.4%, +0.670, +107.21, 121.31, t=+1.16 |
| 15m | fav | 0.01 | 71 | 14 | k=1 [0.60,0.70): 160, 69.4%, +0.507, +81.18, 129.61, t=+0.89 |
| 15m | fav | 0.02 | 71 | 7 | k=1 [0.60,0.70): 160, 69.4%, +0.350, +55.97, 137.65, t=+0.62 |
| 15m | dog | 0.0 | 71 | 23 | k=12 [0.90,0.95): 164, 10.4%, +3.367, +552.22, 266.24, t=+1.03 |
| 15m | dog | 0.01 | 71 | 13 | k=12 [0.90,0.95): 164, 10.4%, +1.665, +273.01, 320.00, t=+0.58 |
| 15m | dog | 0.02 | 71 | 7 | k=11 [0.60,0.70): 167, 40.7%, +0.592, +98.92, 162.34, t=+0.57 |
| 5m | fav | 0.0 | 20 | 9 | k=1 [0.60,0.70): 305, 68.5%, +0.398, +121.39, 71.88, t=+0.96 |
| 5m | fav | 0.01 | 20 | 6 | k=1 [0.60,0.70): 305, 68.5%, +0.242, +73.73, 75.32, t=+0.59 |
| 5m | fav | 0.02 | 20 | 4 | k=1 [0.60,0.70): 305, 68.5%, +0.090, +27.58, 82.63, t=+0.22 |
| 5m | dog | 0.0 | 20 | 4 | k=4 [0.90,0.95): 174, 9.2%, +2.561, +445.59, 447.22, t=+0.80 |
| 5m | dog | 0.01 | 20 | 1 | k=4 [0.90,0.95): 174, 9.2%, +0.864, +150.32, 446.93, t=+0.31 |
| 5m | dog | 0.02 | 20 | 0 | k=3 [0.50,0.60): 192, 47.4%, -0.348, -66.75, 176.87, t=-0.45 |

`t` = mean / (sample sd / sqrt(n)) of the per-trade PnL of the best cell (best by average PnL). With 71 (15m) or 20 (5m)
cells tested per row, these best-cell t values (highest +1.16) are in the range chance alone produces; t was only computed
for the best cell of each row, not for every cell.

### Out-of-sample check (picked on first half, scored on second half, slip 0.01)

| market | side | k | band | H1 n | H1 avg$ | H2 n | H2 win | H2 avg$ | H2 cum$ | H2 maxDD$ |
|---|---|---|---|---|---|---|---|---|---|---|
| 15m | fav | 1 | [0.50,0.60) | 559 | +0.045 | 569 | 53.8% | -0.508 | -288.82 | 393.13 |
| 15m | fav | 6 | [0.60,0.70) | 158 | +0.220 | 172 | 64.5% | -0.456 | -78.43 | 140.04 |
| 15m | fav | 6 | [0.90,0.95) | 48 | +0.251 | 36 | 97.2% | +0.371 | +13.36 | 10.05 |
| 15m | fav | 7 | [0.90,0.95) | 68 | +0.177 | 61 | 91.8% | -0.210 | -12.78 | 24.78 |
| 15m | fav | 8 | [0.90,0.95) | 85 | +0.013 | 71 | 90.1% | -0.422 | -29.93 | 45.45 |
| 15m | fav | 8 | [0.95,0.99) | 43 | +0.252 | 46 | 97.8% | +0.008 | +0.36 | 10.02 |
| 15m | fav | 9 | [0.80,0.90) | 142 | +0.049 | 158 | 81.6% | -0.647 | -102.20 | 105.48 |
| 15m | fav | 11 | [0.70,0.80) | 104 | +0.587 | 98 | 73.5% | -0.486 | -47.60 | 95.20 |
| 15m | fav | 12 | [0.80,0.90) | 116 | +0.342 | 107 | 86.9% | -0.061 | -6.51 | 40.90 |
| 15m | dog | 2 | [0.60,0.70) | 210 | +1.113 | 202 | 31.7% | -1.765 | -356.49 | 372.96 |
| 15m | dog | 2 | [0.70,0.80) | 65 | +2.072 | 67 | 19.4% | -3.250 | -217.75 | 237.71 |
| 15m | dog | 3 | [0.50,0.60) | 243 | +0.345 | 270 | 36.3% | -2.531 | -683.24 | 683.53 |
| 15m | dog | 3 | [0.70,0.80) | 125 | +0.159 | 126 | 21.4% | -2.558 | -322.26 | 402.99 |
| 15m | dog | 4 | [0.50,0.60) | 189 | +2.107 | 209 | 45.5% | -0.437 | -91.42 | 186.54 |
| 15m | dog | 4 | [0.60,0.70) | 194 | +0.119 | 204 | 36.3% | -0.429 | -87.58 | 291.03 |
| 15m | dog | 5 | [0.50,0.60) | 168 | +0.411 | 175 | 43.4% | -0.946 | -165.47 | 184.42 |
| 15m | dog | 5 | [0.60,0.70) | 180 | +2.346 | 183 | 28.4% | -2.653 | -485.47 | 494.97 |
| 15m | dog | 5 | [0.70,0.80) | 178 | +0.891 | 179 | 29.6% | +0.806 | +144.24 | 224.01 |
| 15m | dog | 6 | [0.70,0.80) | 162 | +4.452 | 154 | 18.8% | -3.335 | -513.54 | 571.42 |
| 15m | dog | 6 | [0.80,0.90) | 105 | +0.134 | 136 | 16.2% | -1.281 | -174.21 | 407.66 |
| 15m | dog | 7 | [0.80,0.90) | 143 | +3.594 | 135 | 11.1% | -3.822 | -516.02 | 662.32 |
| 15m | dog | 8 | [0.50,0.60) | 116 | +0.510 | 121 | 50.4% | +0.611 | +73.98 | 81.17 |
| 15m | dog | 8 | [0.80,0.90) | 133 | +2.505 | 149 | 9.4% | -4.957 | -738.62 | 848.73 |
| 15m | dog | 9 | [0.50,0.60) | 109 | +1.450 | 102 | 42.2% | -1.178 | -120.20 | 193.50 |
| 15m | dog | 9 | [0.70,0.80) | 127 | +1.114 | 125 | 24.0% | -1.090 | -136.26 | 253.48 |
| 15m | dog | 10 | [0.60,0.70) | 83 | +0.547 | 92 | 35.9% | -0.290 | -26.68 | 167.01 |
| 15m | dog | 10 | [0.70,0.80) | 126 | +0.365 | 116 | 24.1% | -1.247 | -144.62 | 259.18 |
| 15m | dog | 10 | [0.80,0.90) | 134 | +0.261 | 148 | 17.6% | -0.223 | -33.03 | 315.58 |
| 15m | dog | 10 | [0.90,0.95) | 94 | +3.317 | 88 | 8.0% | -1.916 | -168.63 | 574.60 |
| 15m | dog | 11 | [0.60,0.70) | 87 | +1.107 | 80 | 40.0% | +0.663 | +53.06 | 152.61 |
| 15m | dog | 11 | [0.80,0.90) | 119 | +0.651 | 120 | 10.8% | -3.797 | -455.59 | 689.39 |
| 15m | dog | 11 | [0.90,0.95) | 89 | +3.351 | 107 | 6.5% | -3.688 | -394.63 | 607.64 |
| 15m | dog | 12 | [0.70,0.80) | 72 | +0.115 | 84 | 22.6% | -2.111 | -177.29 | 278.49 |
| 15m | dog | 12 | [0.90,0.95) | 82 | +4.244 | 82 | 8.5% | -0.915 | -74.99 | 282.84 |
| 15m | dog | 13 | [0.80,0.90) | 83 | +1.497 | 82 | 14.6% | -1.586 | -130.02 | 281.43 |
| 15m | dog | 14 | [0.50,0.60) | 33 | +0.269 | 42 | 45.2% | -0.820 | -34.45 | 113.62 |
| 15m | dog | 14 | [0.70,0.80) | 39 | +3.294 | 48 | 16.7% | -4.067 | -195.20 | 210.53 |
| 15m | dog | 14 | [0.80,0.90) | 71 | +0.771 | 62 | 12.9% | -2.089 | -129.55 | 313.53 |
| 15m | dog | 14 | [0.90,0.95) | 59 | +1.056 | 64 | 0.0% | -10.644 | -681.22 | 681.22 |
| 5m | fav | 1 | [0.60,0.70) | 154 | +0.100 | 151 | 69.5% | +0.386 | +58.35 | 75.32 |
| 5m | fav | 2 | [0.70,0.80) | 116 | +0.290 | 139 | 69.8% | -1.040 | -144.53 | 163.72 |
| 5m | fav | 3 | [0.70,0.80) | 112 | +0.586 | 120 | 75.8% | -0.228 | -27.30 | 115.75 |
| 5m | fav | 3 | [0.95,0.99) | 48 | +0.196 | 53 | 98.1% | +0.009 | +0.46 | 10.01 |
| 5m | fav | 4 | [0.50,0.60) | 56 | +0.688 | 49 | 53.1% | -0.845 | -41.40 | 98.04 |
| 5m | fav | 4 | [0.95,0.99) | 122 | +0.169 | 147 | 95.2% | -0.319 | -46.89 | 61.95 |
| 5m | dog | 3 | [0.50,0.60) | 103 | +0.955 | 89 | 41.6% | -1.402 | -124.73 | 149.92 |
| 5m | dog | 4 | [0.80,0.90) | 108 | +1.018 | 119 | 12.6% | -2.281 | -271.50 | 372.69 |
| 5m | dog | 4 | [0.90,0.95) | 97 | +3.418 | 77 | 6.5% | -2.353 | -181.18 | 446.93 |

15m: 39 cells selected on H1; 5 stayed positive on H2; summed H2 PnL of all selected cells = -7535.72 USD

5m: 9 cells selected on H1; 2 stayed positive on H2; summed H2 PnL of all selected cells = -778.74 USD

### Full tables, slip 0.01 (stake 10 USD/trade; avg / cum / maxDD in USD)

#### 15m, buy favourite

| minute k | band | n | win | avg | cum | maxDD |
|---|---|---|---|---|---|---|
| 1 | [0.50,0.60) | 1128 | 55.3% | -0.234 | -263.71 | 463.60 |
| 1 | [0.60,0.70) | 160 | 69.4% | +0.507 | +81.18 | 129.61 |
| 1 | [0.70,0.80) | 9 | 66.7% | -1.128 | -10.15 | 20.33 |
| 1 | [0.80,0.90) | 0 | - | - | - | - |
| 1 | [0.90,0.95) | 0 | - | - | - | - |
| 1 | [0.95,0.99) | 0 | - | - | - | - |
| 2 | [0.50,0.60) | 651 | 53.6% | -0.670 | -435.89 | 494.41 |
| 2 | [0.60,0.70) | 412 | 62.9% | -0.711 | -293.03 | 401.98 |
| 2 | [0.70,0.80) | 132 | 72.7% | -0.423 | -55.87 | 111.74 |
| 2 | [0.80,0.90) | 27 | 85.2% | -0.008 | -0.21 | 21.63 |
| 2 | [0.90,0.95) | 1 | 100.0% | +0.869 | +0.87 | 0.00 |
| 2 | [0.95,0.99) | 0 | - | - | - | - |
| 3 | [0.50,0.60) | 513 | 57.5% | +0.012 | +6.17 | 342.33 |
| 3 | [0.60,0.70) | 424 | 66.3% | -0.174 | -73.73 | 247.60 |
| 3 | [0.70,0.80) | 251 | 75.3% | -0.198 | -49.67 | 127.16 |
| 3 | [0.80,0.90) | 97 | 83.5% | -0.274 | -26.57 | 50.59 |
| 3 | [0.90,0.95) | 6 | 100.0% | +0.671 | +4.02 | 0.00 |
| 3 | [0.95,0.99) | 0 | - | - | - | - |
| 4 | [0.50,0.60) | 398 | 49.0% | -1.549 | -616.62 | 659.43 |
| 4 | [0.60,0.70) | 398 | 62.8% | -0.697 | -277.23 | 308.23 |
| 4 | [0.70,0.80) | 329 | 76.6% | -0.035 | -11.49 | 131.82 |
| 4 | [0.80,0.90) | 167 | 86.2% | +0.035 | +5.77 | 69.56 |
| 4 | [0.90,0.95) | 18 | 94.4% | +0.129 | +2.32 | 10.05 |
| 4 | [0.95,0.99) | 3 | 100.0% | +0.273 | +0.82 | 0.00 |
| 5 | [0.50,0.60) | 343 | 53.6% | -0.752 | -257.84 | 380.52 |
| 5 | [0.60,0.70) | 363 | 63.1% | -0.656 | -238.26 | 371.40 |
| 5 | [0.70,0.80) | 357 | 70.3% | -0.865 | -308.94 | 356.11 |
| 5 | [0.80,0.90) | 209 | 87.6% | +0.154 | +32.17 | 70.52 |
| 5 | [0.90,0.95) | 40 | 92.5% | -0.122 | -4.88 | 20.08 |
| 5 | [0.95,0.99) | 10 | 100.0% | +0.289 | +2.89 | 0.00 |
| 6 | [0.50,0.60) | 322 | 53.4% | -0.750 | -241.53 | 277.49 |
| 6 | [0.60,0.70) | 330 | 66.7% | -0.132 | -43.65 | 157.53 |
| 6 | [0.70,0.80) | 316 | 71.5% | -0.778 | -245.96 | 357.33 |
| 6 | [0.80,0.90) | 241 | 83.4% | -0.344 | -82.90 | 147.41 |
| 6 | [0.90,0.95) | 84 | 96.4% | +0.302 | +25.40 | 10.05 |
| 6 | [0.95,0.99) | 21 | 100.0% | +0.263 | +5.52 | 0.00 |
| 7 | [0.50,0.60) | 268 | 54.5% | -0.546 | -146.27 | 289.17 |
| 7 | [0.60,0.70) | 282 | 63.5% | -0.613 | -172.85 | 236.73 |
| 7 | [0.70,0.80) | 300 | 72.0% | -0.728 | -218.46 | 264.26 |
| 7 | [0.80,0.90) | 278 | 82.7% | -0.461 | -128.27 | 167.94 |
| 7 | [0.90,0.95) | 129 | 93.8% | -0.006 | -0.75 | 24.78 |
| 7 | [0.95,0.99) | 40 | 100.0% | +0.237 | +9.48 | 0.00 |
| 8 | [0.50,0.60) | 237 | 49.8% | -1.403 | -332.55 | 403.96 |
| 8 | [0.60,0.70) | 233 | 61.4% | -0.928 | -216.24 | 261.47 |
| 8 | [0.70,0.80) | 270 | 73.0% | -0.601 | -162.25 | 175.65 |
| 8 | [0.80,0.90) | 282 | 85.1% | -0.205 | -57.93 | 143.20 |
| 8 | [0.90,0.95) | 156 | 92.3% | -0.185 | -28.84 | 45.45 |
| 8 | [0.95,0.99) | 89 | 98.9% | +0.126 | +11.19 | 10.02 |
| 9 | [0.50,0.60) | 211 | 51.2% | -1.118 | -235.95 | 293.44 |
| 9 | [0.60,0.70) | 209 | 62.2% | -0.780 | -162.97 | 224.80 |
| 9 | [0.70,0.80) | 252 | 73.0% | -0.569 | -143.50 | 185.98 |
| 9 | [0.80,0.90) | 300 | 84.3% | -0.317 | -95.19 | 112.01 |
| 9 | [0.90,0.95) | 178 | 93.3% | -0.081 | -14.45 | 31.78 |
| 9 | [0.95,0.99) | 154 | 96.1% | -0.187 | -28.81 | 32.93 |
| 10 | [0.50,0.60) | 201 | 55.7% | -0.443 | -89.01 | 160.91 |
| 10 | [0.60,0.70) | 175 | 61.7% | -0.907 | -158.76 | 218.61 |
| 10 | [0.70,0.80) | 242 | 74.0% | -0.503 | -121.80 | 178.12 |
| 10 | [0.80,0.90) | 282 | 82.6% | -0.527 | -148.64 | 154.19 |
| 10 | [0.90,0.95) | 182 | 90.1% | -0.426 | -77.56 | 114.35 |
| 10 | [0.95,0.99) | 221 | 98.2% | +0.005 | +1.12 | 22.52 |
| 11 | [0.50,0.60) | 156 | 53.8% | -0.734 | -114.51 | 174.53 |
| 11 | [0.60,0.70) | 167 | 59.3% | -1.226 | -204.69 | 255.03 |
| 11 | [0.70,0.80) | 202 | 77.7% | +0.067 | +13.45 | 95.20 |
| 11 | [0.80,0.90) | 239 | 85.8% | -0.126 | -30.17 | 83.99 |
| 11 | [0.90,0.95) | 196 | 91.3% | -0.295 | -57.73 | 85.94 |
| 11 | [0.95,0.99) | 231 | 97.0% | -0.142 | -32.85 | 41.27 |
| 12 | [0.50,0.60) | 142 | 55.6% | -0.361 | -51.32 | 110.42 |
| 12 | [0.60,0.70) | 136 | 66.2% | -0.261 | -35.47 | 134.01 |
| 12 | [0.70,0.80) | 156 | 75.0% | -0.323 | -50.35 | 134.77 |
| 12 | [0.80,0.90) | 223 | 88.3% | +0.149 | +33.22 | 41.35 |
| 12 | [0.90,0.95) | 164 | 89.6% | -0.489 | -80.17 | 84.39 |
| 12 | [0.95,0.99) | 265 | 98.9% | +0.043 | +11.36 | 10.02 |
| 13 | [0.50,0.60) | 112 | 57.1% | -0.019 | -2.15 | 69.93 |
| 13 | [0.60,0.70) | 118 | 66.1% | -0.218 | -25.71 | 90.90 |
| 13 | [0.70,0.80) | 138 | 76.8% | -0.043 | -5.98 | 72.26 |
| 13 | [0.80,0.90) | 165 | 83.6% | -0.445 | -73.45 | 115.15 |
| 13 | [0.90,0.95) | 148 | 92.6% | -0.160 | -23.71 | 42.53 |
| 13 | [0.95,0.99) | 278 | 98.2% | -0.025 | -6.99 | 34.19 |
| 14 | [0.50,0.60) | 75 | 53.3% | -0.921 | -69.10 | 130.09 |
| 14 | [0.60,0.70) | 93 | 68.8% | +0.233 | +21.66 | 98.62 |
| 14 | [0.70,0.80) | 87 | 74.7% | -0.364 | -31.69 | 82.99 |
| 14 | [0.80,0.90) | 133 | 85.0% | -0.293 | -38.91 | 71.44 |
| 14 | [0.90,0.95) | 123 | 95.1% | +0.099 | +12.20 | 37.88 |
| 14 | [0.95,0.99) | 216 | 98.6% | +0.013 | +2.84 | 22.47 |

#### 15m, buy underdog

| minute k | band | n | win | avg | cum | maxDD |
|---|---|---|---|---|---|---|
| 1 | [0.50,0.60) | 1128 | 44.7% | -0.903 | -1018.93 | 1063.90 |
| 1 | [0.60,0.70) | 160 | 30.6% | -2.433 | -389.30 | 389.30 |
| 1 | [0.70,0.80) | 9 | 33.3% | +1.907 | +17.16 | 42.02 |
| 1 | [0.80,0.90) | 0 | - | - | - | - |
| 1 | [0.90,0.95) | 0 | - | - | - | - |
| 1 | [0.95,0.99) | 0 | - | - | - | - |
| 2 | [0.50,0.60) | 651 | 46.4% | -0.313 | -203.77 | 349.72 |
| 2 | [0.60,0.70) | 412 | 37.1% | -0.298 | -122.76 | 414.99 |
| 2 | [0.70,0.80) | 132 | 27.3% | -0.629 | -83.05 | 258.71 |
| 2 | [0.80,0.90) | 27 | 14.8% | -1.274 | -34.39 | 95.18 |
| 2 | [0.90,0.95) | 1 | 0.0% | -10.627 | -10.63 | 10.63 |
| 2 | [0.95,0.99) | 0 | - | - | - | - |
| 3 | [0.50,0.60) | 513 | 42.5% | -1.168 | -599.42 | 767.20 |
| 3 | [0.60,0.70) | 424 | 33.7% | -1.151 | -487.90 | 606.25 |
| 3 | [0.70,0.80) | 251 | 24.7% | -1.205 | -302.41 | 402.99 |
| 3 | [0.80,0.90) | 97 | 16.5% | -0.981 | -95.12 | 278.33 |
| 3 | [0.90,0.95) | 6 | 0.0% | -10.639 | -63.84 | 63.84 |
| 3 | [0.95,0.99) | 0 | - | - | - | - |
| 4 | [0.50,0.60) | 398 | 51.0% | +0.771 | +306.82 | 186.54 |
| 4 | [0.60,0.70) | 398 | 37.2% | -0.162 | -64.56 | 493.69 |
| 4 | [0.70,0.80) | 329 | 23.4% | -1.567 | -515.62 | 592.62 |
| 4 | [0.80,0.90) | 167 | 13.8% | -2.195 | -366.53 | 470.48 |
| 4 | [0.90,0.95) | 18 | 5.6% | -4.100 | -73.80 | 159.52 |
| 4 | [0.95,0.99) | 3 | 0.0% | -10.666 | -32.00 | 32.00 |
| 5 | [0.50,0.60) | 343 | 46.4% | -0.281 | -96.40 | 209.00 |
| 5 | [0.60,0.70) | 363 | 36.9% | -0.174 | -63.23 | 494.97 |
| 5 | [0.70,0.80) | 357 | 29.7% | +0.849 | +302.92 | 224.01 |
| 5 | [0.80,0.90) | 209 | 12.4% | -3.487 | -728.80 | 791.99 |
| 5 | [0.90,0.95) | 40 | 7.5% | -0.828 | -33.12 | 185.85 |
| 5 | [0.95,0.99) | 10 | 0.0% | -10.665 | -106.65 | 106.65 |
| 6 | [0.50,0.60) | 322 | 46.6% | -0.224 | -72.22 | 298.73 |
| 6 | [0.60,0.70) | 330 | 33.3% | -1.167 | -385.04 | 422.50 |
| 6 | [0.70,0.80) | 316 | 28.5% | +0.657 | +207.65 | 571.42 |
| 6 | [0.80,0.90) | 241 | 16.6% | -0.664 | -160.13 | 407.66 |
| 6 | [0.90,0.95) | 84 | 3.6% | -6.437 | -540.68 | 551.98 |
| 6 | [0.95,0.99) | 21 | 0.0% | -10.667 | -224.00 | 224.00 |
| 7 | [0.50,0.60) | 268 | 45.5% | -0.553 | -148.30 | 214.30 |
| 7 | [0.60,0.70) | 282 | 36.5% | -0.190 | -53.55 | 236.89 |
| 7 | [0.70,0.80) | 300 | 28.0% | +0.205 | +61.63 | 285.58 |
| 7 | [0.80,0.90) | 278 | 17.3% | -0.008 | -2.10 | 677.09 |
| 7 | [0.90,0.95) | 129 | 6.2% | -4.153 | -535.71 | 555.88 |
| 7 | [0.95,0.99) | 40 | 0.0% | -10.669 | -426.75 | 426.75 |
| 8 | [0.50,0.60) | 237 | 50.2% | +0.562 | +133.15 | 130.86 |
| 8 | [0.60,0.70) | 233 | 38.6% | +0.312 | +72.78 | 172.77 |
| 8 | [0.70,0.80) | 270 | 27.0% | -0.244 | -65.76 | 212.69 |
| 8 | [0.80,0.90) | 282 | 14.9% | -1.438 | -405.44 | 848.73 |
| 8 | [0.90,0.95) | 156 | 7.7% | -1.467 | -228.85 | 682.23 |
| 8 | [0.95,0.99) | 89 | 1.1% | -8.625 | -767.67 | 767.67 |
| 9 | [0.50,0.60) | 211 | 48.8% | +0.180 | +37.89 | 233.21 |
| 9 | [0.60,0.70) | 209 | 37.8% | -0.035 | -7.24 | 138.65 |
| 9 | [0.70,0.80) | 252 | 27.0% | +0.021 | +5.22 | 253.48 |
| 9 | [0.80,0.90) | 300 | 15.7% | -0.749 | -224.61 | 433.54 |
| 9 | [0.90,0.95) | 178 | 6.7% | -2.671 | -475.42 | 560.10 |
| 9 | [0.95,0.99) | 154 | 3.9% | -3.062 | -471.50 | 662.48 |
| 10 | [0.50,0.60) | 201 | 44.3% | -0.766 | -153.96 | 178.65 |
| 10 | [0.60,0.70) | 175 | 38.3% | +0.107 | +18.68 | 167.01 |
| 10 | [0.70,0.80) | 242 | 26.0% | -0.408 | -98.63 | 259.18 |
| 10 | [0.80,0.90) | 282 | 17.4% | +0.007 | +1.89 | 319.36 |
| 10 | [0.90,0.95) | 182 | 9.9% | +0.786 | +143.12 | 574.60 |
| 10 | [0.95,0.99) | 221 | 1.8% | -6.394 | -1413.06 | 1600.39 |
| 11 | [0.50,0.60) | 156 | 46.2% | -0.324 | -50.52 | 126.80 |
| 11 | [0.60,0.70) | 167 | 40.7% | +0.894 | +149.34 | 152.61 |
| 11 | [0.70,0.80) | 202 | 22.3% | -1.848 | -373.23 | 446.91 |
| 11 | [0.80,0.90) | 239 | 14.2% | -1.582 | -378.18 | 779.43 |
| 11 | [0.90,0.95) | 196 | 8.7% | -0.492 | -96.38 | 607.64 |
| 11 | [0.95,0.99) | 231 | 3.0% | -4.015 | -927.37 | 1277.50 |
| 12 | [0.50,0.60) | 142 | 44.4% | -0.769 | -109.19 | 219.46 |
| 12 | [0.60,0.70) | 136 | 33.8% | -1.059 | -144.06 | 182.17 |
| 12 | [0.70,0.80) | 156 | 25.0% | -1.083 | -168.99 | 279.52 |
| 12 | [0.80,0.90) | 223 | 11.7% | -3.039 | -677.60 | 734.17 |
| 12 | [0.90,0.95) | 164 | 10.4% | +1.665 | +273.01 | 320.00 |
| 12 | [0.95,0.99) | 265 | 1.1% | -7.793 | -2065.07 | 2076.16 |
| 13 | [0.50,0.60) | 112 | 42.9% | -1.022 | -114.50 | 207.57 |
| 13 | [0.60,0.70) | 118 | 33.9% | -0.956 | -112.76 | 243.12 |
| 13 | [0.70,0.80) | 138 | 23.2% | -1.680 | -231.87 | 354.74 |
| 13 | [0.80,0.90) | 165 | 16.4% | -0.035 | -5.79 | 286.89 |
| 13 | [0.90,0.95) | 148 | 7.4% | -1.767 | -261.50 | 352.40 |
| 13 | [0.95,0.99) | 278 | 1.8% | -6.474 | -1799.78 | 2347.56 |
| 14 | [0.50,0.60) | 75 | 46.7% | -0.341 | -25.58 | 113.62 |
| 14 | [0.60,0.70) | 93 | 31.2% | -1.806 | -167.94 | 200.74 |
| 14 | [0.70,0.80) | 87 | 25.3% | -0.767 | -66.73 | 287.99 |
| 14 | [0.80,0.90) | 133 | 15.0% | -0.562 | -74.78 | 313.53 |
| 14 | [0.90,0.95) | 123 | 4.9% | -5.032 | -618.94 | 831.38 |
| 14 | [0.95,0.99) | 216 | 1.4% | -6.471 | -1397.80 | 1397.80 |

#### 5m, buy favourite

| minute k | band | n | win | avg | cum | maxDD |
|---|---|---|---|---|---|---|
| 1 | [0.50,0.60) | 674 | 58.5% | +0.179 | +120.48 | 319.12 |
| 1 | [0.60,0.70) | 305 | 68.5% | +0.242 | +73.73 | 75.32 |
| 1 | [0.70,0.80) | 124 | 75.0% | -0.224 | -27.77 | 66.33 |
| 1 | [0.80,0.90) | 21 | 81.0% | -0.484 | -10.17 | 16.51 |
| 1 | [0.90,0.95) | 1 | 100.0% | +0.758 | +0.76 | 0.00 |
| 1 | [0.95,0.99) | 0 | - | - | - | - |
| 2 | [0.50,0.60) | 308 | 54.5% | -0.505 | -155.58 | 305.67 |
| 2 | [0.60,0.70) | 305 | 66.9% | -0.089 | -27.15 | 128.33 |
| 2 | [0.70,0.80) | 255 | 74.1% | -0.435 | -110.88 | 163.72 |
| 2 | [0.80,0.90) | 183 | 86.3% | -0.009 | -1.60 | 57.86 |
| 2 | [0.90,0.95) | 48 | 93.8% | +0.042 | +2.01 | 19.12 |
| 2 | [0.95,0.99) | 10 | 100.0% | +0.221 | +2.21 | 0.00 |
| 3 | [0.50,0.60) | 192 | 52.6% | -0.905 | -173.68 | 217.13 |
| 3 | [0.60,0.70) | 214 | 68.2% | +0.093 | +19.95 | 73.92 |
| 3 | [0.70,0.80) | 232 | 78.9% | +0.165 | +38.29 | 115.75 |
| 3 | [0.80,0.90) | 227 | 85.0% | -0.224 | -50.96 | 96.52 |
| 3 | [0.90,0.95) | 152 | 92.1% | -0.169 | -25.75 | 57.92 |
| 3 | [0.95,0.99) | 101 | 99.0% | +0.098 | +9.86 | 10.01 |
| 4 | [0.50,0.60) | 105 | 57.1% | -0.027 | -2.85 | 121.79 |
| 4 | [0.60,0.70) | 111 | 65.8% | -0.275 | -30.53 | 108.85 |
| 4 | [0.70,0.80) | 139 | 74.8% | -0.350 | -48.58 | 106.74 |
| 4 | [0.80,0.90) | 227 | 85.5% | -0.167 | -37.93 | 92.51 |
| 4 | [0.90,0.95) | 174 | 90.8% | -0.345 | -59.98 | 88.74 |
| 4 | [0.95,0.99) | 269 | 97.4% | -0.098 | -26.24 | 61.95 |

#### 5m, buy underdog

| minute k | band | n | win | avg | cum | maxDD |
|---|---|---|---|---|---|---|
| 1 | [0.50,0.60) | 674 | 41.5% | -1.475 | -994.28 | 1174.82 |
| 1 | [0.60,0.70) | 305 | 31.5% | -1.973 | -601.76 | 659.99 |
| 1 | [0.70,0.80) | 124 | 25.0% | -1.335 | -165.60 | 299.24 |
| 1 | [0.80,0.90) | 21 | 19.0% | -0.349 | -7.33 | 95.27 |
| 1 | [0.90,0.95) | 1 | 0.0% | -10.633 | -10.63 | 10.63 |
| 1 | [0.95,0.99) | 0 | - | - | - | - |
| 2 | [0.50,0.60) | 308 | 45.5% | -0.475 | -146.16 | 188.67 |
| 2 | [0.60,0.70) | 305 | 33.1% | -1.263 | -385.29 | 527.76 |
| 2 | [0.70,0.80) | 255 | 25.9% | -0.663 | -169.15 | 562.64 |
| 2 | [0.80,0.90) | 183 | 13.7% | -2.665 | -487.75 | 543.53 |
| 2 | [0.90,0.95) | 48 | 6.2% | -4.008 | -192.39 | 320.18 |
| 2 | [0.95,0.99) | 10 | 0.0% | -10.670 | -106.70 | 106.70 |
| 3 | [0.50,0.60) | 192 | 47.4% | -0.138 | -26.40 | 161.23 |
| 3 | [0.60,0.70) | 214 | 31.8% | -1.655 | -354.24 | 432.31 |
| 3 | [0.70,0.80) | 232 | 21.1% | -2.678 | -621.29 | 660.66 |
| 3 | [0.80,0.90) | 227 | 15.0% | -1.547 | -351.28 | 406.59 |
| 3 | [0.90,0.95) | 152 | 7.9% | -1.962 | -298.24 | 383.58 |
| 3 | [0.95,0.99) | 101 | 1.0% | -7.843 | -792.11 | 792.11 |
| 4 | [0.50,0.60) | 105 | 42.9% | -1.157 | -121.50 | 134.94 |
| 4 | [0.60,0.70) | 111 | 34.2% | -1.013 | -112.45 | 118.61 |
| 4 | [0.70,0.80) | 139 | 25.2% | -0.844 | -117.33 | 188.01 |
| 4 | [0.80,0.90) | 227 | 14.5% | -0.712 | -161.57 | 472.16 |
| 4 | [0.90,0.95) | 174 | 9.2% | +0.864 | +150.32 | 446.93 |
| 4 | [0.95,0.99) | 269 | 2.6% | -3.071 | -826.14 | 2038.59 |

Tables for slip 0 and 0.02 and the split-half tables are produced by the same script (`analyze`); they are summarised
above and not repeated here.

## Conclusion

The claim is **not reproduced** on this sample. Late-window favourite entries win 83–99 % of the time but their
average PnL after the 0.07 taker fee and a 1-tick-class slip is around zero or negative, and the few positive cells do not
survive a time split. The underdog side shows large in-sample "best cells" (for example 15m k=12 [0.90,0.95) +273.01 USD
at slip 0.01) that come from a handful of long-shot wins and reverse in the second half. Nothing here supports turning
this into a paper strategy without a much larger sample and real order-book data.

## Caveats (data limits)

1. **Price is not an executable ask.** `prices-history` returns one price per ~minute; the API does not say whether it
   is a midpoint or last trade (the flat 0.505 before the start looks like a midpoint of a 0.50/0.51 book). Real entry
   would be at the ask, with depth limits; `slip` only approximates that. No historical order books were fetched.
2. **Granularity.** ~60 s points (p90 72 s, max 548 s); a "minute k" entry can use a price up to 90 s old. Fast moves in
   the last minute are not seen. Sub-minute "after-the-bell"/last-seconds tactics cannot be tested with this data.
3. **"After-the-bell" not tested.** After the window ends the outcome is (almost) known from the reference price and the
   history converges to ~0.9995; trading then is either not possible (market stops accepting orders) or pure look-ahead
   here. Testing it honestly needs tick-level book data and the order-acceptance timestamps.
4. **Short sample, one regime.** 14 days (15m) and 4 days (5m), all in Oct–Nov 2026. 5m cells have n <= 674.
5. **Survivorship / gaps.** 9 15m windows had no Gamma event and were dropped (contiguous block around the US DST change).
   Down prices are inferred as `1 - Up`.
6. **Multiple comparisons.** 6 bands × 14 (or 4) minutes × 2 sides × 3 slips; the best cell is selected after the fact.
7. **Fees.** Rebates are ignored (they would help a maker, but this test is taker-only). If Polymarket changes the crypto
   fee rate, results shift; the rate used here is what Gamma returned for every market in the sample.
8. **Reproducibility.** Gamma/CLOB history can change or expire; a re-run may differ slightly. Raw cache (~10 MB JSON) is
   not committed.

## Reproduce

Stdlib Python 3 only, from an empty scratch directory (not inside the repo):

```
python3 bt.py fetch 15m 1794283200 1344
python3 bt.py fetch 5m 1794283200 1152
python3 bt.py analyze > results.md
```

`fetch` writes `./data/<5m|15m>/<start_unix>.json` (4 threads, ~2.5 min each here). Script sha256 of the version used:
`4a23f5006799cc80a4841c79eaf4307e1cd3e8833f481c94a261b67a37323b51`. It is kept inside this doc on purpose so nothing
under `src/`, `scripts/` or `tests/` changes and it cannot be imported by the pod.

<details><summary>bt.py (as run)</summary>

```python
#!/usr/bin/env python3
"""Offline paper backtest: BTC Up/Down 5m/15m, enter by time window.
Stdlib only. Public read-only GETs: gamma-api.polymarket.com/events?slug=,
clob.polymarket.com/prices-history. No auth, no orders.
Usage: python3 bt.py fetch <5m|15m> <end_unix> <n_markets>   (caches into ./data)
       python3 bt.py analyze > results.txt
"""
import json, os, sys, time, urllib.request, concurrent.futures as cf
D = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
STEP = {"5m": 300, "15m": 900}
def get(u):
    for a in range(4):
        try:
            r = urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "readonly-research"}), timeout=30)
            return json.load(r)
        except Exception as e:
            if a == 3: raise
            time.sleep(1.5 * (a + 1))
def fetch_one(w, ts):
    fn = os.path.join(D, w, f"{ts}.json")
    if os.path.exists(fn): return "cached"
    ev = get(f"https://gamma-api.polymarket.com/events?slug=btc-updown-{w}-{ts}")
    if not ev: rec = {"ts": ts, "missing": "no_event"}
    else:
        m = ev[0]["markets"][0]
        tok = json.loads(m["clobTokenIds"]); outs = json.loads(m["outcomes"])
        rec = {"ts": ts, "slug": m["slug"], "outcomes": outs, "outcomePrices": json.loads(m.get("outcomePrices") or "null"),
               "closed": m.get("closed"), "uma": m.get("umaResolutionStatus"), "feeSchedule": m.get("feeSchedule"),
               "eventStartTime": m.get("eventStartTime"), "endDate": m.get("endDate"), "tokens": tok}
        s = STEP[w]
        h = get(f"https://clob.polymarket.com/prices-history?market={tok[0]}&startTs={ts-120}&endTs={ts+s+120}&fidelity=1")
        rec["hist_up"] = h.get("history", [])
    os.makedirs(os.path.dirname(fn), exist_ok=True)
    json.dump(rec, open(fn, "w"))
    return "ok"
def fetch(w, end, n):
    s = STEP[w]; end -= end % s
    tss = [end - s * i for i in range(1, n + 1)]
    with cf.ThreadPoolExecutor(4) as ex:
        res = list(ex.map(lambda t: (t, fetch_one(w, t)), tss))
    print(w, len(res), sum(1 for _, r in res if r == "ok"), "fetched")

FEE_RATE = 0.07  # Gamma feeSchedule.rate for these markets; docs.polymarket.com/trading/fees: fee = C*rate*p*(1-p)
STAKE = 10.0
def load(w):
    out = []; miss = {}
    for f in sorted(os.listdir(os.path.join(D, w))):
        r = json.load(open(os.path.join(D, w, f)))
        if r.get("missing"): miss[r["missing"]] = miss.get(r["missing"], 0) + 1; continue
        op = r.get("outcomePrices")
        if not (r.get("closed") and op in (["1", "0"], ["0", "1"]) and r["outcomes"] == ["Up", "Down"]):
            miss["unresolved_or_odd"] = miss.get("unresolved_or_odd", 0) + 1; continue
        if not r["hist_up"]:
            miss["no_history"] = miss.get("no_history", 0) + 1; continue
        r["up_won"] = op == ["1", "0"]; out.append(r)
    return out, miss
def price_at(r, t):
    """last Up price with timestamp <= t and >= t-90 (no look-ahead, stale guard)."""
    best = None
    for x in r["hist_up"]:
        if x["t"] <= t: best = x
        else: break
    if best is None or t - best["t"] > 90: return None
    return float(best["p"])
def trade(e, win):
    sh = STAKE / e; fee = sh * FEE_RATE * e * (1 - e)
    return (sh if win else 0.0) - STAKE - fee
def stats(pnls):
    n = len(pnls)
    if not n: return None
    cum = peak = mdd = 0.0
    for p in pnls:
        cum += p; peak = max(peak, cum); mdd = max(mdd, peak - cum)
    w = sum(1 for p in pnls if p > 0)
    return n, w / n, cum / n, cum, mdd
def run(mk, w, k, lo, hi, side, slip):
    return stats(pnl_list(mk, w, k, lo, hi, side, slip))
def pnl_list(mk, w, k, lo, hi, side, slip):
    pnls = []
    for r in mk:
        p = price_at(r, r["ts"] + 60 * k)
        if p is None: continue
        fav_up = p >= 0.5; pf = p if fav_up else 1 - p
        if not (lo <= pf < hi): continue
        if side == "fav": buy_up, mid = fav_up, pf
        else: buy_up, mid = (not fav_up), 1 - pf
        e = min(mid + slip, 0.999)
        pnls.append(trade(e, buy_up == r["up_won"]))
    return pnls
BANDS = [(0.50, 0.60), (0.60, 0.70), (0.70, 0.80), (0.80, 0.90), (0.90, 0.95), (0.95, 0.99)]
def fmt(st):
    if not st: return "| 0 | - | - | - | - |"
    n, wr, avg, cum, mdd = st
    return f"| {n} | {wr*100:.1f}% | {avg:+.3f} | {cum:+.2f} | {mdd:.2f} |"
def analyze():
    for w in ("15m", "5m"):
        mk, miss = load(w)
        mk.sort(key=lambda r: r["ts"])
        fs = {json.dumps(r.get("feeSchedule"), sort_keys=True) for r in mk}
        print(f"\n## {w}: usable={len(mk)} excluded={miss} first_ts={mk[0]['ts']} last_ts={mk[-1]['ts']} up_won={sum(r['up_won'] for r in mk)} feeSchedules={fs}")
        gaps = [b["t"] - a["t"] for r in mk for a, b in zip(r["hist_up"], r["hist_up"][1:]) if r["ts"] <= a["t"] < r["ts"] + STEP[w]]
        gaps.sort(); print(f"in-window point gaps s: n={len(gaps)} median={gaps[len(gaps)//2]} p90={gaps[int(len(gaps)*.9)]} max={gaps[-1]}")
        ks = range(1, STEP[w] // 60)
        for side in ("fav", "dog"):
            for slip in (0.0, 0.01, 0.02):
                print(f"\n### {w} side={side} slip={slip} (stake ${STAKE:.0f}/trade; columns n | win | avgPnL$ | cumPnL$ | maxDD$)")
                print("| minute k | band | n | win | avg | cum | maxDD |"); print("|---|---|---|---|---|---|---|")
                for k in ks:
                    for lo, hi in BANDS:
                        print(f"| {k} | [{lo:.2f},{hi:.2f}) " + fmt(run(mk, w, k, lo, hi, side, slip)))
        # split-half stability for fav, slip 0.01
        h = len(mk) // 2
        print(f"\n### {w} split-half (fav, slip 0.01): first half ts<{mk[h]['ts']} vs second half")
        print("| k | band | H1 n | H1 win | H1 avg | H1 cum | H1 maxDD | H2 n | H2 win | H2 avg | H2 cum | H2 maxDD |"); print("|---|---|---|---|---|---|---|---|---|---|---|---|")
        for k in ks:
            for lo, hi in BANDS:
                a = fmt(run(mk[:h], w, k, lo, hi, "fav", 0.01)); b = fmt(run(mk[h:], w, k, lo, hi, "fav", 0.01))
                print(f"| {k} | [{lo:.2f},{hi:.2f}) " + a + b[1:])
def summary():
    import math
    print("\n## Summary (cells = minute k x band; only cells with n>=30 counted)")
    print("| market | side | slip | cells n>=30 | cells avg>0 | best cell (k, band): n, win, avg$, cum$, maxDD$, t |")
    print("|---|---|---|---|---|---|")
    for w in ("15m", "5m"):
        mk, _ = load(w); mk.sort(key=lambda r: r["ts"])
        for side in ("fav", "dog"):
            for slip in (0.0, 0.01, 0.02):
                cells = []
                for k in range(1, STEP[w] // 60):
                    for lo, hi in BANDS:
                        p = pnl_list(mk, w, k, lo, hi, side, slip)
                        if len(p) >= 30: cells.append((k, lo, hi, p))
                pos = [c for c in cells if sum(c[3]) > 0]
                b = max(cells, key=lambda c: sum(c[3]) / len(c[3]))
                st = stats(b[3]); n = st[0]; m = st[2]
                sd = math.sqrt(sum((x - m) ** 2 for x in b[3]) / (n - 1))
                print(f"| {w} | {side} | {slip} | {len(cells)} | {len(pos)} | k={b[0]} [{b[1]:.2f},{b[2]:.2f}): {n}, {st[1]*100:.1f}%, {m:+.3f}, {st[3]:+.2f}, {st[4]:.2f}, t={m/(sd/math.sqrt(n)):+.2f} |")
    print("\n## Out-of-sample check: cells picked on first half (n>=30, avg>0, fav and dog, slip 0.01), then scored on second half")
    print("| market | side | k | band | H1 n | H1 avg$ | H2 n | H2 win | H2 avg$ | H2 cum$ | H2 maxDD$ |")
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    tot = {}
    for w in ("15m", "5m"):
        mk, _ = load(w); mk.sort(key=lambda r: r["ts"]); h = len(mk) // 2
        for side in ("fav", "dog"):
            for k in range(1, STEP[w] // 60):
                for lo, hi in BANDS:
                    a = pnl_list(mk[:h], w, k, lo, hi, side, 0.01)
                    if len(a) < 30 or sum(a) <= 0: continue
                    b = pnl_list(mk[h:], w, k, lo, hi, side, 0.01); st = stats(b)
                    t = tot.setdefault(w, [0, 0, 0.0]); t[0] += 1; t[1] += (sum(b) > 0); t[2] += sum(b)
                    sb = f"{st[0]} | {st[1]*100:.1f}% | {st[2]:+.3f} | {st[3]:+.2f} | {st[4]:.2f}" if st else "0 | - | - | - | -"
                    print(f"| {w} | {side} | {k} | [{lo:.2f},{hi:.2f}) | {len(a)} | {sum(a)/len(a):+.3f} | {sb} |")
    for w, t in tot.items():
        print(f"\n{w}: {t[0]} cells selected on H1; {t[1]} stayed positive on H2; summed H2 PnL of all selected cells = {t[2]:+.2f} USD")

if __name__ == "__main__":
    if sys.argv[1] == "fetch": fetch(sys.argv[2], int(sys.argv[3]), int(sys.argv[4]))
    else: analyze(); summary()
```

</details>
