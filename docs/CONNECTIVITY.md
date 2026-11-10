# CONNECTIVITY.md · 数据源连通性实测记录（T00）

> 实测日期：2026-11-06（Asia/Shanghai）｜ 执行：`scripts/connectivity_check.py`
> 指定数据源：**AKShare**（https://akshare.akfamily.xyz/data/stock/stock.html）
> 与 **同花顺 fuyao**（https://fuyao.aicubes.cn/docs/api-reference/overview/）

## 1. 结论

| 源 | 状态 | 用途 |
|---|---|---|
| **同花顺 fuyao** | ✅ **连通，code=0** | **主源**：历史 K 线 + 复权因子事件流 |
| **AKShare** | ✅ **可用**（已装 1.18.60） | **备源**：前复权/不复权日 K（`stock_zh_a_hist`） |
| 腾讯财经免费行情 | ✅ **连通**（免 Key） | **补充**：实时快照含总市值/流通市值（fuyao prices 不返回市值） |
| Tushare | ⛔ 未安装 SDK，未配 token | 不在指定源内，保留为代码占位 |
| BaoStock | ⛔ 未安装 SDK | 同上 |

**fallback 链（实测顺序）**：`ths_fuyao → akshare → tushare → baostock → synthetic(兜底)`。
主源 fuyao 已连通，生产数据来自 fuyao；synthetic 仅离线冒烟兜底。

## 2. 同花顺 fuyao 实测（API Key 已配）

- 认证：请求头 `X-api-key`（**非 Bearer**，官方契约；缺失/无效 → `code=2001`）
- 标的：完整 `thscode`（`601398.SH`），内部 `sh601398` 经 `symbol_map` 双向映射
- 限流：HTTP 429 / `code=4001` → 降并发 + 退避，禁止立即连续重试

| capability | 端点 | 实测 |
|---|---|---|
| 历史批量 K 线 | `GET /api/a-share/prices/historical-batch` | ✅ `code=0`，10 代码/次，`adjust=forward\|none` |
| 复权因子事件流 | `GET /api/a-share/corporate-actions/adjustment-factors` | ✅ `code=0`，返回 `dividend_per_share`/`per_share_bonus` |

**实测样本（sh601398，2026-11-05）**：raw_close=8.3500，adj_close=8.3500，factor=1.0000（最新日）。
**★ C1 前复权验证**：2026-10-20 除息（dps=0.1511）前一日 `factor=0.98212`，除权日起 `factor=1.0`
→ 除权机械跳变已从 `adj_close` 中剔除，不会触发假超跌信号。

**落库（200 自然日窗口，10 只演示池）**：`price_daily=1340` 行，`corp_action=37` 行（含 2025-07-13 / 2025-12-14 / 2026-05-12 / 2026-10-20 除权事件）。
**因子**：`div_yield_ttm(sh601398)=5.53%`（不复权口径），RSI(14)=49.1，`consecutive_div_years=2`（受 200 日回看窗口限制，生产按全量事件流累积）。

## 3. AKShare 实测（备源）

`stock_zh_a_hist(adjust="qfq")` + `adjust=""` 分别取前复权/不复权 close，两列并存（★ C1）。
实测 sh601398 2026-11-05 close=8.35，与 fuyao **一致**（交叉校验通过）。

## 4. 腾讯财经免费源（补充 market_cap）

- 实时快照 `https://qt.gtimg.cn/q=sh601398,...`：GBK 解码、`~` 分隔；
  `[3]`现价 `[5]`今开 `[44]`流通市值 `[45]`总市值（**单位：亿**）。实测 sh601398 总市值 29759.92 亿。
- 前复权日 K `https://ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh601398,day,,,320,qfq`：
  **选复权后键名为 `qfqday`**（按 `day` 取会拿到空），实测最后 10 根正常。
- 用途：`sync_market_cap()` 填充 `symbol_status.market_cap`（universe 门槛 ≥300 亿依赖它）。

## 5. 全链路验收

`make worker-once`（接真实 fuyao 数据）→ 盘后闭环 `daily_nav` 当日 **7 行**（S* + B1–B6）。
`pytest -q` → **23 passed**（数据层改动无回归）。

> 满足 `00_INDEX.md` 验收项：`make fetch-smoke` 从主源取到近 60+ 交易日前复权日线、除权日无假跳变。
