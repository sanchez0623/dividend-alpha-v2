# 04 · 数据源与数据规格

> **dividend-alpha v3.1 · Forward-only**
> 原则：**只向前记录，不回填历史**。历史回测数据在 `research/` 只读归档，不进生产管道。

---

## 1. 现有资产清单（研究期一次性快照，只读）

| 文件 | 内容 | 覆盖 | 生产用途 |
|---|---|---|---|
| `research/cache/daily.json` | 1402 只 × `[date, 不复权 close, amount]` | ~2005 ~ 2026-10-30 | **仅归档**，不作生产源 |
| `research/cache/prices.json` | 5222 只月度收盘 | 2005-12 起 | 归档 |
| `research/cache/dividends.json` | 49,720 条 × `[code, ex, dps, bonus]` | 2008-02-27 ~ 2026-11-09 | 归档；可作自算复权的**验证集** |
| `research/cache/shares.json` | 325,163 条股本变动 `[code, END_DATE, TOTAL_SHARES]` | 1951 ~ 2026-11-11 | 归档 |
| `research/cache/industry.json` | 当前行业快照，含 `BOARD_NAME_LEVEL` 四级（银行细分） | 当前 | **可作生产初始快照** |
| `research/cache/industry_map.json` | EM2016 / BOARD 映射 | 当前 | 可作初始快照 |
| `research/cache/stock_list.json` | 5,576 只 × `[symbol, code, name]` | 当前 | 归档 |
| `research/cache/panel.npz` | 月度 CLOSE/AMT/DIV/SF/YLD/RET1，226 月 | 2008-01 ~ 2026-10 | 归档（`SF` 是送转因子，非价格复权因子） |
| `research/cache/bench_index.json` | 9 个**价格**指数 | 2014 起（中证红利仅到 2019） | 归档 |
| `web/data.json` | 历史回测导出（净值/分年度/取现/KPI） | 2015-01 ~ 2026-10 | **P8 历史归档页唯一数据源，只读** |

> ⚠️ `daily.json` 经池化回归验证为**不复权原价**。除权日实测机械跳变：
> 工商银行 2025-12-15 −2.01%、招商银行 2025-07-11 −5.39%、贵州茅台 2026-06-26 −3.59%。
> **直接拿它算 RSI(14)/20日跌幅会触发假的超跌买入信号。**

---

## 2. 同花顺金融数据 API（用户已提供 key）

### 2.1 已核对的公开 capability（依据 `接口总览`）

| capability | REST 路径 | 覆盖我们的缺口 |
|---|---|---|
| `prices` | `/api/a-share/prices` | 行情快照与历史 K 线 |
| `market-dumps` | `/dump/market-dumps/...` | **全市场 10 年日 K + 复权因子 Parquet** ★ |
| `corporate-actions` | `/api/a-share/corporate-actions` | 分红 / 送股 / 配股复权因子事件流 ★ |
| `a-share-index` | `/api/a-share-index` | 指数列表与成分股（含沪深300） |
| `financials/indicators` | `/api/a-share/financials/indicators` | 成长/盈利/偿债/营运/现金流五类 |

公开 REST 与 MCP Tools 共享同一后端 capability，数据语义一致。
清单中**未见**：行业分类（尤其银行四级）、退市名单、分红 `pay_date`、精确全收益指数、交易日历、60min 线、账户流水。

### 2.2 调用规范（硬约束，逐字对齐官方总览）

```
1. 业务响应（含业务错误）通常返回 HTTP 200，业务结果经响应信封 code 字段表达
   → 客户端必须【同时检查 HTTP 状态码 和 响应信封 code】，只看 HTTP 200 会漏掉业务失败
2. HTTP 429 表示已被限流，无论响应体是否包含标准信封，都按限流场景处理
3. code=4001 同样表示限流
4. 限流时：降低并发量 + 降低请求频率 + 稍后重试
   → 禁止立即连续重试；采用指数退避（建议 base 2s，factor 2，max 60s，jitter）
5. 服务不限制累计调用次数，但会依据实时负载动态调整限流策略
   → 客户端主动限流：默认并发 ≤2、单 capability 间隔 ≥0.5s
6. 响应记录中的 thscode 无法确定时可为 null → 解析必须容忍 null，不得假设非空
7. 同花顺代码与本系统 symbol 需做映射表（sh600036 ↔ 同花顺 thscode），映射失败落 error 表
```

```python
# da_data/providers/ths.py（伪代码，必须实现）
resp = http.get(url, headers={"Authorization": f"Bearer {THS_API_KEY}"})
if resp.status_code == 429:
    raise RateLimited(retry_after=backoff.next())
resp.raise_for_status()
env = resp.json()                      # ApiResponse 信封
if env.get("code") not in (0, "0", None):
    if env.get("code") == 4001: raise RateLimited(retry_after=backoff.next())
    raise BizError(code=env["code"], msg=env.get("message"))
return env["data"]
```

### 2.2.1 官方《接口总览》逐条核对表

> 来源：`https://fuyao.aicubes.cn/docs/api-reference/overview/`（用户提供原文，2026-11-06 复核）。
> 下表是**规范原文 → 本项目落点**的一一映射，T00/T02 实现时不得遗漏任何一行。

| # | 官方总览原文要点 | 本项目落点 | 实现位置 |
|---|---|---|---|
| 1 | 通过 **REST** 提供能力；业务响应（**含业务错误**）通常返回 **HTTP 200**，业务结果经响应信封 **`code`** 字段表达 | 客户端必须**同时检查** HTTP 状态码与 `code`；只看 200 会漏掉业务失败 | `providers/ths.py` 的 `parse_envelope()` |
| 2 | 客户端应**同时检查** HTTP 状态码和信封 `code`；触发限流时也可能返回 **HTTP 429**，**无论响应体是否包含标准信封都应按限流场景处理** | `429` 先于 `.json()` 判断，直接抛 `RateLimited`，不尝试解析信封 | 同上 |
| 3 | **`code=4001`** 与 `429` 均表示触发限流 | 与 `429` 走同一退避分支 | 同上 |
| 4 | 限流时请**降低并发量和请求频率，避免立即连续重试，并在稍后重新发起** | `retry_immediately: false`；指数退避 base 2s / factor 2 / max 60s + jitter；并发 ≤2、间隔 ≥0.5s | `config/providers.yaml` 的 `ths.backoff`、`fallback.py` |
| 5 | 服务**不限制累计调用次数**；但应**避免短时间内集中或高并发调用**；会依据实时负载**动态调整**限流策略 | 不按总次数配额设计；客户端主动限流，容忍策略动态收紧 | `providers.yaml` 注释 + `ingest_log` 记录 `429/4001` |
| 6 | 响应记录中的同花顺代码（如 **`thscode`）无法确定时可为 `null`** | 解析必须容忍 `null`，禁止假设非空；`null` 落 `symbol_map_error` 表并跳过该行 | `providers/ths.py` 的 `resolve_symbol()` |
| 7 | 正常进入业务处理的请求返回统一 **`ApiResponse`** 信封 | 信封解析器统一封装，所有 capability 复用 | `providers/ths.py` |
| 8 | 已注册公开 REST capability **同时暴露为 MCP Tools**，共享同一后端 capability、数据语义完全一致 | 本项目只用 REST；若 harness 改用 MCP Tool，字段口径不变，无需改 `factors.py` | 架构说明，不产生代码差异 |
| 9 | 工具命名、参数与返回值见 **MCP 工具概览** | 若走 MCP，需另读 MCP 工具概览确认参数名；REST 路径以 §2.1 表为准 | T00 实测记录 |
| 10 | **部分数据与分析能力已接入同花顺 AI 客户端**，请前往使用 | 与本项目无关，不产生实现；仅记录，避免误以为所有能力都有 REST | 无 |

> ⚠️ **第 1、2 条是最容易写错的地方**：很多客户端只判 `resp.raise_for_status()`，在同花顺这里会把
> `HTTP 200 + code=4001/业务错误` 当成成功数据写库，导致因子静默错误。

### 2.3 AKShare（备源 / 交易日历 / 盘中）

> 来源：`https://akshare.akfamily.xyz/data/stock/stock.html`，**版本锁定 `1.20.35`**（用户提供，2026-11-06）。
> ⚠️ 这份是**入口页**，原文只说明「股票数据按主题分为以下几个页面；查找具体接口时，
> 可以使用页面顶部的搜索(Ctrl+K)或 `ak.search()`，详见 AKShare 接口检索」。
> **它没有给出任何具体接口名、参数或返回字段。**

因此定下三条硬规则：

1. **禁止臆测接口名**。本文及 `config/providers.yaml` 中不得出现未经实测的 `ak.*` 函数名；
   历史文档里出现过的 `tool_trade_date_hist_sina` 等一律降级为「候选，待核实」。
2. **T00 必须用 `ak.search()` 逐项定位**下列用途的实际函数，并把
   `{用途 → func 名 / 参数 / 返回字段 / 频率限制}` 写入 `docs/CONNECTIVITY.md`，
   再回填 `config/providers.yaml` 的 `resolved_interfaces` / `calendar.resolved_func`：

   | 用途 | 需要的字段 | 备注 |
   |---|---|---|
   | 日线 OHLC + 成交额（**不复权**） | date/open/high/low/close/amount | 同花顺未通时的主行情来源 |
   | 前复权日线（≥90 交易日） | 复权价或复权因子 | 若 akshare 能直接给，可省 §4.4 自算 |
   | 交易日历（**未来**） | trade_date | 判断 12 月最后交易日 |
   | 股票状态（ST/停牌/涨跌停/list_date） | — | 执行约束 |
   | 总市值 | total_mv / 报表值 | 300 亿门槛 |
   | 分红事件 | ex_date / dps / bonus / pay_date | 同花顺 `corporate-actions` 的备源 |
   | 指数价格与成分股 | — | B3/B4 起点 |
   | 60min 线 | — | 可延后 |

3. **版本锁定**：`requirements.txt` 写 `akshare==1.20.35`，升级需重跑 T00 实测。
   AKShare 接口随版本改名/下线较频繁，不锁版本会让管道静默失效。

> 这条「入口页没有具体接口」正是 `06_HANDOFF.md §4` 上报协议要覆盖的未定项之一：
> harness 遇到时**停下来上报，不要猜一个函数名写死**。

### 2.4 连通性状态

| 项 | 状态 |
|---|---|
| 同花顺文档站 | ✅ 可访问 |
| 同花顺 API 网关（沙盒） | ❌ `policy_default_denied` —— 出口 IP 不在白名单 |
| 同花顺生产 | ⏳ **需加白名单并实测**（T00 任务） |
| AKShare 1.20.35 | ⏳ 需 T00 用 `ak.search()` 实测接口名与可用性 |
| **baostock（保留）** | ✅ 无需 key；串行；T00 实测后记录 |
| **东财 `push2his`（保留）** | ⚠️ 曾返回 502；定位为最后兜底，T00 需记录 502 复现情况 |

> **v3.1 数据源决策（用户明确口径）**：
> - **移除**：`tushare` / `TUSHARE_TOKEN` —— 用户明确说「不用」。仅这一项禁止在代码中出现。
> - **主备**：同花顺 fuyao（主）+ AKShare 1.20.35（备）—— 用户点名「改用」的两个。
> - **保留兜底**：`baostock` 与东财 `push2his` —— **用户明确要求不得清除**。
>   baostock 串行、东财已知 502，仅在前面全失败时启用，不得作主源。

---

## 3. Forward-only 缺口矩阵

**立刻消失的 5 项考古工作**：历史行业分类（前视偏差）、退市股回填、2015-2018 市值推算、
精确全收益指数历史序列、10 年日 K 全量下载。

| # | 数据域 | 支撑页 | 紧迫度 | 来源 | 状态 |
|---|---|---|---|---|---|
| 1 | **账户流水层**（持仓/现金/成交/分红到账/取现日志/每日净值/逐笔做T配对） 🆕 | P1 P2 P3 P4 P5 P6 | ★★★ | **系统自产**（非外部源） | 完全空白，**最先写** |
| 2 | **前复权日线（≥60 交易日）** 🆕 | P3 | ★★★ | 同花顺 `market-dumps` + `corporate-actions` | 见 §4 |
| 3 | 日线 OHLC + 成交额（每日增量） | P1 P2 P3 | ★★★ | **同花顺 `prices`** → akshare → baostock → 东财 | 待接 |
| 4 | 分红事件（含 `announce_date` / `ex_date` / `pay_date` / `dps` / `bonus`） | P6 P4 | ★★★ | **同花顺 `corporate-actions`** → akshare → baostock | 待接 |
| 5 | **未来**交易日历 | 全系统调度 | ★★ | akshare（`ak.search()` 定位）→ baostock | 待接 |
| 6 | 股票状态（ST / 停牌 / 涨跌停 / 退市整理 / list_date） | P2 P3 | ★★ | akshare 股票列表（`ak.search()` 定位） | 待接 |
| 7 | 行业分类（当前快照 + 增量变更） | P2 | ★★ | 现有 `industry.json` 起步 + 增量 | ✅ 初始可用 |
| 8 | 总市值（当前报表值） | P2 | ★★ | 同花顺 / akshare 报表值（`ak.search()` 定位） | ✅ 初始可用 |
| 9 | 指数价格 + 成分股（B1/B3/B4 起点） | P5 | ★★ | **同花顺 `a-share-index`** → akshare 备 | 部分可用 |
| 10 | 60min 线 / 盘中快照 | P1 P3 | ★ | akshare（`ak.search()` 定位）→ 东财 | 可延后 |
| 11 | 精确全收益指数 H00300 / H00922 | P5 | ✩ | 中证官网 / 付费源，或从今天自攒 | 可延后 |
| 12 | 财报五类指标 | P7（配置展示） | ✩ | 同花顺 `financials/indicators` | **策略未用，勿误启用** |

> **v3.1 主备与兜底**：**同花顺 fuyao（主）→ AKShare 1.20.35（备）→ baostock（串行兜底）→ 东财 push2his（最后兜底）**。
> `tushare` 已移除且不再有 `TUSHARE_TOKEN`；**baostock 与东财保留**（见 §2.4、§6）。
> 上表所有 akshare 项的具体函数名均为**待 T00 用 `ak.search()` 核实**，禁止臆测。

> ⚠️ **配置一致性**：`config/strategy.yaml` 中**不得**保留 `min_roe_ttm` / `max_profit_decline_yoy` /
> `min_ocf_to_profit` / `max_pe_pctile_5y` 等未被回测引擎使用的财报字段。
> 锁定漏斗只有三条：连续分红≥5年、市值≥300亿、股息率≥2.5%、排除 ST。

---

## 4. ★ 变更 C1：RSI(14) + 20日跌幅 的前复权日线补齐方案

### 4.1 为什么需要

| 指标 | 口径 | 若用不复权价的后果 |
|---|---|---|
| `RSI(14)` | 前复权 close | 除权日机械大跌 → RSI 假性超卖 → **假买入信号** |
| `20日跌幅` | 前复权 close | 除权日 −2%~−5% 被计入跌幅 → 触发 `≥12%` 阈值误判 |
| `股息率 TTM` | **不复权** close | 若误用复权价，分子分红与分母股价口径错位 → 股息率失真 |

**结论：两列必须并存** `raw_close` / `adj_close`，禁止只存其一。

### 4.2 最小窗口

| 用途 | 需要 |
|---|---|
| RSI(14) 稳定 | ≥ 14 + 预热，建议 30 |
| 20 日跌幅 | 20 |
| 除权跳变校验 | 前后各 1 日 |
| 缓冲（停牌、节假日、Wilder 平滑预热） | +30 |
| **建议落库窗口** | **≥90 个交易日**（最低 60，90 为推荐值） |

> 起始日 2026-11-05 只需回看约 4 个月，是轻量数据，不是考古负担。

### 4.3 路径 A（生产首选）：同花顺官方复权因子

```
① GET /dump/market-dumps/...  → 下载「最近 10 交易日日K + 复权因子」Parquet（增量）
                              或「全市场 10 年日K + 复权因子」（首次冷启动）
   ⚠️ 确切 endpoint / 参数 / 返回字段由 T00 实测后写入 docs/CONNECTIVITY.md
② GET /api/a-share/corporate-actions → 分红/送股/配股事件流，用于每日增量修正
③ 落库 price_daily(symbol, trade_date, open, high, low, raw_close, amount,
                   adj_factor, adj_close)
④ adj_close = raw_close × adj_factor（或按厂商定义的前复权公式，需核对文档）
⑤ 校验：对同一 symbol，除权日前后 adj_close 的跳变应 ≈ 真实市场波动，
        不得出现 ≈ −dps/raw_close 的机械下跌（见 §4.5）
```

**每日增量**：只需取当日 `raw_close` + 最新 `adj_factor`；因子变化时（新除权）
**按该 symbol 全窗口重算 `adj_close`**（前复权因子变化会改写历史值，这是正确行为）。

### 4.3.1 路径 A′（同花顺未通）：AKShare 接管

> 同花顺未通时，先由 akshare 接管；akshare 也不足时再退到 baostock / 东财（§6）。
> T00 用 `ak.search()` 定位后二选一：

| 情形 | 处理 |
|---|---|
| akshare 能直接给**前复权日线** | 直接落 `adj_close`，`adj_factor` 记 1 并标注 `source=akshare_adj`，**跳过 §4.4 自算** |
| akshare 只给不复权价 | 落 `raw_close`，走 §4.4 路径 B 自算；公司行动从 akshare 分红接口补 |
| 两者都拿不到 | **停下来上报**，不得伪造 `adj_close`；账本 T01 照常先跑 |

无论哪条路径，`test_adjust_price`（§4.5）必须全绿才允许进入 T03。

### 4.4 路径 B（降级 / 冷启动 / 单元测试）：自算复权

> 用于 T00 白名单未通、且 akshare 只给不复权价时先跑通链路，
> 或作为官方因子的交叉校验。**生产不得长期依赖**。

现有 `dividends.json` 有 `dps`（每股现金分红）与 `bonus`（送转，实测样本中多为 0），
`panel.npz` 的 `SF` 为送转因子（39% 非 1）。可用事件流反推：

```python
# da_data/adjust.py（参考实现）
def build_adj_factor(raw: pd.Series, events: list[CorpAction]) -> pd.Series:
    """
    raw: 以 trade_date 为索引的不复权收盘价
    events: [{ex_date, dps, bonus_ratio, transfer_ratio, rights_ratio, rights_price}]
    返回前复权因子（最新日 = 1.0）
    """
    f = pd.Series(1.0, index=raw.index)
    # 从最新往最旧遍历除权事件
    for ev in sorted(events, key=lambda e: e.ex_date, reverse=True):
        if ev.ex_date not in raw.index: continue
        i = raw.index.get_loc(ev.ex_date)
        if i == 0: continue
        prev_close = raw.iloc[i-1]
        # A股除权除息参考价 = (前收 − 现金红利 + 配股款) / (1 + 送股 + 转增 + 配股)
        num = prev_close - ev.dps + ev.rights_ratio * ev.rights_price
        den = 1.0 + ev.bonus_ratio + ev.transfer_ratio + ev.rights_ratio
        theo = num / den
        ratio = theo / prev_close          # 该事件导致的价格比例
        f.iloc[:i] *= ratio                # 之前的所有价格按此比例前复权
    return f

adj_close = raw_close * build_adj_factor(raw_close, events)
```

**口径说明**：
- `bonus`（送股）与转增需从 `SF` / 公告补齐；`dividends.json` 的 `bonus` 实测多为 0，
  **仅用 dps 会漏掉送转除权**，这是路径 B 不能长期用的原因
- 配股需 `rights_ratio` / `rights_price`，公告源缺失时按 `corporate-actions` 补
- 前复权因子**会随新除权事件改写历史值**，故每日需重算窗口内全部 `adj_close`

### 4.5 验收断言（T02 必须通过）

```python
def test_adjust_price():
    # A1 除权日平滑：对每个已知除权事件
    for ev in KNOWN_EVENTS:                # 工行/招行/茅台/长电/神华近 3 年
        raw_jump = raw[ev.ex] / raw[prev] - 1
        adj_jump = adj[ev.ex] / adj[prev] - 1
        assert abs(raw_jump - (-ev.dps / raw[prev])) < 0.01     # 确认原价确有机械跳变
        assert abs(adj_jump) < abs(raw_jump) * 0.35              # 复权后跳变显著收敛

    # A2 技术指标不产生假信号
    rsi = compute_rsi(adj_close, 14)
    drop20 = adj_close / adj_close.shift(20) - 1
    for ev in KNOWN_EVENTS:
        assert not (rsi[ev.ex] < 25 and drop20[ev.ex] < -0.12)   # 除权日不得触发 combo

    # A3 股息率口径独立
    yld = div_ttm / raw_close
    assert yld.between(0, 0.30).all()                             # 不得因复权失真

    # A4 窗口完整性
    assert len(adj_close) >= 60
    assert adj_close.notna().all()
```

### 4.6 因子计算规范（落库字段）

| 字段 | 公式 | 价格口径 |
|---|---|---|
| `div_yield_ttm` | 近 12 个月 `dps` 之和 ÷ `raw_close` | 不复权 |
| `consecutive_div_years` | 近 5 年每年至少 1 次现金分红 | — |
| `market_cap` | 报表总市值（或 `total_shares × raw_close`） | 不复权 |
| `rsi_14` | Wilder RSI on `adj_close` | **前复权** |
| `drop_20d` | `adj_close / adj_close.shift(20) − 1` | **前复权** |
| `signal_strength` | `f(|drop_20d|, 25 − rsi_14)` 归一化 | 前复权 |

---

## 5. 账本自产数据（非外部源，最先写）

| 表 | 关键字段 | 产生时点 |
|---|---|---|
| `account` | cash, total_equity, updated_at | 初始化 + 每笔成交 |
| `position` | symbol, shares_total, shares_locked, shares_available, cost, buy_date | buy/sell/settle |
| `lot` | buy_date, shares, price（FIFO 红利税） | buy |
| `order` / `fill` | symbol, side, shares, price, commission, stamp_tax, slippage | 订单确认 |
| `dividend_event` | symbol, ex_date, **pay_date**, dps_gross, tax_withheld, net_cash | 分红到账 |
| `withdraw_log` | year, want, got, rate, **idempotency_key** | 12月最后交易日 |
| `daily_nav` | trade_date, s_nav, b1..b6_nav, running_days, paired_trade_count | 每日 16:50 |
| `signal_pair` | trade_date, symbol, real_ret, random_ret, config_hash | 每笔做T成交 |
| `execution_log` | task, idempotency_key, status, payload | 每个幂等任务 |

> `daily_nav` 是 P5 基准页双模式守门的**唯一数据来源**；`signal_pair` 是 B6 样本 `m` 的唯一来源。
> 这两张表第一天就要开始写，否则永远攒不够 1095 天 / 300 笔。

---

## 6. 四源 fallback 链（v3.1）

> 用户口径：**tushare 移除**；主备为同花顺 + AKShare；**baostock 与东财保留为兜底**。

```
                    ┌─ 429 / code=4001 → 降并发 + 指数退避，【不切源、禁止立即连续重试】
① 主 同花顺 fuyao ─┤
                    └─ 退避耗尽 / 业务错误 ──> ② 备 AKShare 1.20.35
                                                    │
                                                    └─ 失败 ──> ③ baostock【串行，并发=1】
                                                                    │
                                                                    └─ 失败 ──> ④ 东财 push2his【最后兜底，已知 502】
                                                                                      │
                                                                                      └─ 全失败 → ingest_log(FAIL) + 告警，
                                                                                         当日不推进因子；【账本 T01 照常记 D+n】
```

| 序 | 源 | 角色 | 并发 | 备注 |
|---|---|---|---|---|
| ① | **同花顺 fuyao** | 主 | ≤2，间隔 ≥0.5s | 需白名单；HTTP 状态码与信封 `code` **同时检查**；`thscode` 容忍 null |
| ② | **AKShare 1.20.35** | 备 | ≤4 | 接口名由 T00 `ak.search()` 实测；版本锁定，升级重测 |
| ③ | **baostock**（保留） | 串行兜底 | **1，禁止并发** | 无需 key；历史日线稳定；仅在前两源失败时启用 |
| ④ | **东财 push2his**（保留） | 最后兜底 | ≤2 | 曾返回 502；**不得作主源**；T00 在 CONNECTIVITY.md 记录实测状态 |

**分工原则**（避免多源混用造成口径漂移）：

| 数据域 | 主责 | 降级顺序 |
|---|---|---|
| 日线 / 复权因子 / 公司行动 / 指数 | 同花顺 | akshare → baostock → 东财 |
| 交易日历 / 股票状态 / 60min | **akshare 直接主责**（官方总览未列） | baostock → 东财 |
| 行业分类 / 初始市值 | 现有快照起步 | 同花顺 → akshare |
| 财报指标（策略未用） | 同花顺 `financials/indicators` | 不降级，缺则留空 |

**三条限流/降级纪律**：

1. 同花顺 `429/4001` 时**先退避重试，不立即切源**——否则一次限流就把全市场切走，反而放大请求量。只有退避耗尽（max 60s）或业务错误才降级。
2. **baostock 必须串行**（`max_concurrency: 1`），并发会直接报错；切到它时抓取耗时会明显变长，属预期行为。
3. 东财只作最后兜底与交叉校验；若返回 502 记 `ingest_log(FAIL)`，**不要反复重试拖垮盘后任务**。

每次抓取写 `ingest_log(source, capability, latency, rows, status)`，供 P7 数据源状态卡展示；
状态卡显示 **4 行**（主 / 备 / 两个兜底），见 `02_UI_SPEC.md §5.7` 与 `web/prototype.html` 配置页。

> 全库搜索 `tushare` / `TUSHARE_TOKEN` 应无命中（除 `docs/legacy/` 归档与决策说明）；
> **baostock / eastmoney 必须存在**，缺失即回归缺陷。

---

## 7. 数据标注映射（对应 `02_UI_SPEC.md §3`）

| 数据来源 | `data_class` |
|---|---|
| 账本 / 每日净值 / 实时信号 | `实时` |
| 初期占位（P2 持仓、P3 信号流水） | `演示` |
| 全收益指数估算、超额数字 | `估算` |
| `web/data.json` 全部内容 | `归档` |

后端每个数值响应必须带 `data_class`，前端统一渲染，禁止前端硬编码判断。
