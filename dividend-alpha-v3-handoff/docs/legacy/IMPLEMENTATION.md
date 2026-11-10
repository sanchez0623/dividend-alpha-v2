# IMPLEMENTATION.md · 唯一权威实现规格

> 版本 v2.0 ｜ 2026-10-31 ｜ **状态：可开工**
>
> ⚠️ **本文件是唯一真值。** `DESIGN.md`、`architecture.md`、`rotation-engine.md`、
> `strategy-and-backtest.md` 均为历史档案与推导过程，其中与本文冲突处**一律以本文为准**。
> 实证证据见 `research/REPORT1~11.md`，回写前不得修改本文参数。

---

## 0. 决策已冻结

| # | 决策 | 结论 | 依据 |
|---|---|---|---|
| D1 | 市场 | A股，总市值 ≥ **300 亿** | 用户指定 |
| D2 | 持仓数 | **N=10，等权**，同 L1 行业 ≤1，银行按 国有/股份/城商/农商 细分且每子类 ≤2 | REPORT7/9 |
| D3 | 卖出 | 硬规则（股息率<1.5% / 分红中断 / 满5年）+ **浮盈>50% 一次性清仓** | REPORT6/9 |
| D4 | 换仓 | **不做**任何形式的主动轮动/条件换仓/强制换仓 | REPORT3/5/6/9 |
| D5 | 分红 | 年内 **100% 再投**；**每年 12 月最后交易日取当年税前现金分红 × 30%** | REPORT11 |
| D6 | 增强层 | 全合格池暴跌日加仓，`20日跌≥12% 且 RSI(14)<25`，持 30 日，**12×3%**，标记「实验性」 | REPORT8/11 |
| D7 | 本金 | 300 万 | 用户指定 |

**收益基线（2015-01~2026-10，含全部成本）**：

| | 年化 | 样本外(22-26) | 波动 | 夏普 | 最大回撤 | 期末 |
|---|---|---|---|---|---|---|
| **S\*（D1-D7 全开）** | **10.61%** | **15.61%** | 22.17% | 0.39 | −42.5% | **931.9 万** |
| B2 纯持有（关增强层） | 10.49% | 16.33% | — | 0.35 | −42.7% | 920.8 万 |

⚠️ 原定 15% 已实证下修。**相对沪深300全收益超额约 +7.3%/年，相对红利基准全收益约 +4.6%/年**（估算口径，见 §8）。

---

## 1. 系统一句话

以「连续 5 年分红 + 市值≥300亿 + 股息率≥2.5%」为池，按**股息率绝对值降序**持有 10 只；
只在超涨（浮盈>50%）或基本面破坏时换出，年内把分红全部再投，年底取走当年分红的 30%；
另用 RSI+超跌信号在暴跌日做小仓位增强；全程用 B1-B6 基准与安慰剂检验持续证伪自己。

**Web 形态**：日线级准实时监控看板（盘后批量 + 盘中可选轮询），输出持仓/信号/归因/多基准曲线。

---

## 2. 最终参数（唯一真值，直接映射 `config/strategy.yaml`）

```yaml
universe:
  min_consecutive_dividend_years: 5
  min_market_cap: 3.0e10          # 300亿
  min_div_yield_ttm: 0.025
  exclude_st: true
  include_delisted: true          # 防幸存者偏差（REPORT2：偏差仅 0.1-0.5%/年）
  min_adv_6m: null                # ★ 不设流动性门槛（用户裁定，300万吃得下）

portfolio:
  target_positions: 10
  weighting: equal                # 等权优于股息率平方加权
  core_ratio: 0.90
  industry_constraint:
    level: L1
    max_per_industry: 1
    bank_subtype: {treat_as_distinct: true, max_per_subtype: 2}

selection:
  rank_by: div_yield_ttm          # ★ 绝对值降序，禁止用分位排序（REPORT9：3.21% vs 9.76%）
  rebalance: monthly              # 仅补仓到 N，不主动换股

sell_rules:
  hard:
    - {cond: "div_yield_ttm < 0.015", action: sell_all}
    - {cond: "dividend_interrupted", action: sell_all}
    - {cond: "holding_months >= 60", action: sell_all}
  overheat:
    cond: "unrealized_gain >= 0.50"   # 相对成本浮盈
    action: sell_all                  # ★ 分批挪仓实测更差，已关闭

dividend:
  reinvest:
    enabled: true
    trigger: on_dividend
    ratio: 1.00                     # 年内全额再投
    weight: current_market_value
    lot_size: 100
  withdraw:
    enabled: true
    mode: annual                    # ★ 每年一次
    timing: "last_trading_day_of_December"
    base: "ytd_cash_dividend_gross"  # 当年累计税前现金分红（核心仓+机动仓）
    ratio: 0.30
    shortfall_policy: cash_only     # cash_only(默认,兑现83.8%) | sell_to_cover(100%兑现)
    reset_counter_after: true
    skip_incomplete_year: true      # 进行中的年份不取

overlay:                            # 实验性，可一键关闭
  enabled: true
  experimental: true
  scope: POOL_WIDE                  # ★ 全合格池，不是仅持仓（T+1 不约束 30 日持仓）
  trigger: {kind: combo, lookback_days: 20, drop_threshold: 0.12,
            rsi_period: 14, rsi_threshold: 25}
  holding_days: 30
  rank_by_strength: true
  max_concurrent: 12
  position_per_trade: 0.03          # 机动仓总暴露 ≈30%
  cooldown_days: 5
  capital_guard: "amt + cost <= cash * 0.98"

cost_model:
  commission: 0.00025;  commission_min: 5
  stamp_tax_sell: 0.0005
  transfer_fee: 0.00001
  slippage: 0.0010
  dividend_tax: {lt_1m: 0.20, 1m_to_1y: 0.10, gt_1y: 0.00}
  cash_interest_annual: 0.02        # 国债逆回购

benchmarks:
  enabled: [B1, B2, B3, B4, B5, B6]
  b6_placebo: {enabled: true, n_sim: 200, min_samples: 300}   # 持续积累，勿提前下结论
```

---

## 3. 工程目录（P4 起按此建）

```
dividend-alpha/
├── IMPLEMENTATION.md          ← 本文件（唯一真值）
├── README.md
├── pyproject.toml             # poetry / uv，Python 3.11+
├── requirements.txt
├── .env.example               # TUSHARE_TOKEN / THS_API_KEY / POSTGRES_DSN / REDIS_URL
├── Makefile                   # init / fetch / backtest / serve / test / lint
├── config/
│   ├── strategy.yaml          # ← 由 §2 生成（strategy.example.yaml 为模板）
│   └── providers.yaml         # 多源 fallback + 限流
├── infra/
│   ├── Dockerfile
│   └── docker-compose.yml     # api + worker + postgres(+timescale) + redis + web
├── src/da_core/               # 领域模型（对齐 docs/data-and-api.md §3）
│   ├── models.py              # Symbol / Position / Lot / Signal / Order / DividendEvent
│   ├── broker.py              # ★ T1Broker：shares_total/locked/available + FIFO 红利税
│   ├── cost.py                # 成本闸（不可绕过）
│   └── calendar.py
├── src/da_data/               # 数据管道（唯一生产链路）
│   ├── providers/{tushare,akshare,baostock,ths}.py   # 统一 DataSource 协议
│   ├── fallback.py            # 主源失败自动降级 + 限流退避（HTTP429/code4001）
│   ├── ingest.py              # 日线/分红/股本事件流/行业分类/交易日历
│   ├── mcap.py                # ★ 总股本 × 不复权收盘价 → 市值（覆盖 2015 起）
│   ├── factors.py             # 股息率TTM / 5年分位 / 连续分红年数
│   └── store.py               # PostgreSQL + TimescaleDB，Parquet 冷备
├── src/da_signal/
│   ├── base.py                # Signal 插件协议（generate(ctx) -> list[Signal]）
│   ├── core_selection.py      # 漏斗 + 行业约束 + 排序
│   ├── sell_rules.py          # 硬规则 + 浮盈清仓
│   ├── overlay_rsi.py         # 超跌 + RSI 组合
│   └── registry.py
├── src/da_portfolio/
│   ├── manager.py             # 目标持仓 → 订单（含涨停/跌停/整手校验）
│   ├── dividend.py            # ★ 年内再投 + 年底取现调度（§2 dividend）
│   └── rebalance.py           # 月频补仓 + 事件驱动换出
├── src/da_backtest/
│   ├── event_engine.py        # 事件驱动（唯一权威引擎）
│   ├── vector_engine.py       # 因子级快速验证
│   ├── benchmarks.py          # B1-B6 + 配对安慰剂
│   └── attribution.py         # 模块贡献 / 收益拆解 / 分年度
├── src/da_api/                # FastAPI
│   ├── main.py
│   ├── routers/{portfolio,signals,backtest,factors,system}.py
│   ├── ws.py                  # 信号推送
│   └── schemas.py
├── src/da_worker/             # APScheduler / Celery
│   ├── daily_close.py         # 盘后：行情→因子→信号→告警
│   ├── intraday.py            # 可选：60min 轮询
│   └── annual_withdraw.py     # ★ 12月最后交易日（幂等，带 execution_log）
├── web/                       # React + TS + ECharts + lightweight-charts
│   ├── pages/{Overview,Positions,Signals,Attribution,Benchmarks,Config}.tsx
│   └── components/{NavChart,KLineWithSignals,BenchmarkCompare,WithdrawLog}.tsx
├── tests/
│   ├── test_broker_t1.py      # ★ T+1 / FIFO 红利税 / 除权跳变
│   ├── test_dividend_annual.py# ★ 基线：payout=0 必须 = 11.15%
│   ├── test_cost_gate.py
│   ├── test_industry_constraint.py
│   └── test_placebo.py
└── research/                  # 历史研究脚本与报告（只读归档，不进生产镜像）
    ├── REPORT1~11.md
    └── *.py
```

---

## 4. 关键模块接口（必须实现，签名固定）

```python
# da_data/providers/base.py
class DataSource(Protocol):
    name: str
    def fetch_daily(self, symbols: list[str], start: date, end: date) -> pl.DataFrame: ...
    def fetch_dividends(self, symbol: str) -> list[DividendEvent]: ...
    def fetch_share_events(self, symbol: str) -> pl.DataFrame: ...   # 股本变动，用于市值
    def fetch_industry(self, symbol: str) -> IndustryPath: ...        # 含银行四级子类

# da_signal/base.py
@dataclass(frozen=True)
class Signal:
    symbol: str; kind: Literal["BUY","SELL","TRIM"]; strength: float
    expected_net: float          # 成本闸：net >= 0.008 且 net >= 3×往返成本
    reason: str; asof: datetime; config_hash: str

class SignalPlugin(Protocol):
    def generate(self, ctx: SignalContext) -> list[Signal]: ...

# da_core/broker.py  ★ 正确性地基
class T1Broker:
    def buy(self, symbol, shares, price) -> Fill: ...   # T+1 标记 locked
    def sell(self, symbol, shares, price) -> Fill: ...  # 必须 n <= available，否则 reject
    def on_dividend(self, event: DividendEvent) -> None: ...  # 现金入账 + FIFO 税档
    def settle_eod(self) -> None: ...                    # 解锁

# da_portfolio/dividend.py  ★ 本轮新增
class AnnualWithdraw:
    def on_dividend(self, amount: float) -> None: ...   # ytd_gross += amount；年内全额再投
    def run_year_end(self, ctx) -> WithdrawResult: ...  # 取 ytd_gross*0.30，幂等，写 execution_log
```

**幂等要求**：`run_year_end` 必须按 `(year, account_id)` 唯一键写 `execution_log`，
重复调度不得重复取现。这是生产事故高发点。

---

## 5. 调度时序（Asia/Shanghai）

| 时点 | 任务 | 依赖 |
|---|---|---|
| 15:30 | 日线/分红/股本/行业增量落库 | 数据源 fallback |
| 16:00 | 因子重算（股息率TTM、5年分位、连续分红年数） | 落库完成 |
| 16:15 | 选股漏斗 + 行业约束 → 目标持仓 | 因子 |
| 16:30 | 卖出规则判定 + overlay 信号扫描（全池） | 目标持仓 |
| 16:45 | 生成订单建议 + 成本闸校验 + 告警推送 | 全部 |
| **12月最后交易日 16:50** | **年度取现（幂等）** | 当日分红已入账 |
| 次日 09:00 | 订单确认 / 模拟盘记账 | 人工或券商 |

- 分红到账日即时按市值权重再投（`min_cash_ratio: 0.02` 防碎片交易）
- **除权日前后 3 天冻结分位信号**（防机械跳变，见 rotation-engine.md §2.3，该结论仍有效）

---

## 6. 数据管道（开工前必须先跑通，最高优先级）

> ⚠️ **当前唯一未验证的生产环节。** 研究期用的是一次性抓取脚本，不是可复用管道。

1. **主源**：tushare（需 `TUSHARE_TOKEN`）→ 日线（带复权因子）、分红送配、股本变动
2. **备源**：akshare / baostock（注意并发，baostock 串行）
3. **同花顺 fuyao**（用户已给 key `sk-fuyao-…`）：
   - 响应统一 `ApiResponse` 信封，**必须同时检查 HTTP 状态码与 `code`**
   - `HTTP 429` 或 `code=4001` = 限流 → **降低并发并退避重试，禁止立即连续重试**
   - 沙盒环境曾返回 `policy_default_denied`（出口 IP 不在白名单）；生产部署前需在同花顺侧加白名单
   - 已注册能力同时暴露 MCP Tools，语义与 REST 一致
4. **市值**：`总股本 × 不复权收盘价`（股本事件流覆盖 2002 起，绕过东财报表 2018 限制，一致率 98%+）
5. **索引基准**：精确全收益指数（H00300 / H00922）当前接口取不到，先用「价格指数 + 典型股息率」估算，
   对外表述必须标注"估算"。**上线前需接付费源补齐。**

**验收标准**：任选 2015-01~2026-10 任一日，管道能重算出与 `research/` 一致的
股息率面板、市值面板、连续分红年数，且 `tests/test_dividend_annual.py` 通过。

### 6.1 数据源缺口矩阵（2026-11-05 盘点，对齐 `web/` 七页原型）

**研究期实际资产**（`research/cache/`）：`daily.json` 1402 只 × `[date, 不复权close, amount]`；
`dividends.json` 49,720 条 × `[code, ex, dps, bonus]`；`shares.json` 32.5 万条股本事件（1951 起）；
`industry.json` 当前行业快照（含银行四级子类）；`stock_list.json` 5,576 只 × `[symbol, code, name]`；
`panel.npz` 月度 CLOSE/AMT/DIV/SF/YLD/RET1；`bench_index.json` 9 个**价格**指数。
**全部是一次性快照，不是可复用管道。**

| 数据域 | 支撑的 UI 页 | 现状 | 缺口 | 补齐来源 |
|---|---|---|---|---|
| 日线 **OHLC** + 成交额 | 信号 / 持仓 / 总览 | 仅 close + amount，1402 只 | open/high/low、全A覆盖、每日增量、K线图 | 同花顺 `prices` / `market-dumps` |
| **复权因子** ★ | 信号（RSI/跌幅） | **完全没有**（`panel.SF` 是送转因子，非价格复权） | 前复权价序列；否则除权日机械下跌会触发**假超跌信号** | 同花顺 `market-dumps`（10年日K+复权因子 Parquet）、`corporate-actions` |
| 分红事件 | 取现 / 持仓 | 仅 `ex` 除权日 | `announce_date` 预案、`pay_date` 到账日、分红率；取现基数需到账口径 | 同花顺 `corporate-actions` + 补公告源 |
| 股本事件 | 持仓 / 选股 | ✅ 32.5万条，2002 起覆盖 | 仅需增量更新 | 已有 / 同花顺 corporate-actions |
| **行业分类（历史）** ★ | 持仓 / 归因 | 仅**当前**快照 | 历史行业变更；用 2026 分类回测 2015 有**前视偏差**，L1≤1 的 +1.81% 可能虚高 | 需确认同花顺清单未列；改用东财历史板块或申万历史分类 |
| 股票状态 / 退市 | 选股 / 回测 | 仅 symbol/code/name | `list_date`、`delist_date`、`is_st`、停牌；`exclude_st` / `include_delisted` 目前无法正确实现 | 待补（akshare 股票列表 / 交易所公告） |
| **交易日历** | 全系统调度 | 自建月度序列 | 未来日历：判断「12月最后交易日」「月频补仓」、节假日调休 | akshare `tool_trade_date_hist_sina` |
| 财报指标 | 配置页 / 选股 | **无**；回测引擎实际未用 | ⚠️ `config/core_selection` 仍留 `min_roe_ttm / max_profit_decline_yoy / min_ocf_to_profit / max_pe_pctile_5y`，与锁定参数**不一致**，要么补数据要么删字段 | 同花顺 `financials/indicators`（成长/盈利/偿债/营运/现金流五类） |
| **精确全收益指数** ★ | 基准页 | 9 个**价格**指数（中证红利仅到 2019） | H00300 / H00922；基准页三个超额数字目前标「估算」，用价格指数会虚高 2.5-4 个点 | 中证指数官网 / 付费源（Wind、聚宽、米筐）；同花顺 `a-share-index` 可补成分股 |
| 指数成分股 | B1 / B3 构造 | 无 | 历史成分股（防前视偏差） | 同花顺 `a-share-index` |
| 60min 线 / 盘中快照 | 准实时监控 | **无** | 60min K 线、实时现价、涨停/跌停/停牌状态（执行约束必需） | akshare 60min / 行情快照源 |
| **账户与成交流水** ★ | 持仓 / 信号 / 取现 / 总览 | **完全空白**（UI 这三页均为演示数据） | 持仓快照(成本价/买入日/available-locked)、现金余额、订单与成交流水(实收佣金印花滑点)、分红到账流水+红利税扣缴、取现 `execution_log`、每日净值、逐笔做T配对记录 | **自家系统产生，非外部数据源**：P6 模拟盘自造；实盘只能导入券商交割单（A股无面向个人的官方交易 API） |

**同花顺能力清单核对**（依据接口总览）：`prices` 行情快照/历史K线、`market-dumps` 全市场10年日K+复权因子、
`corporate-actions` 分红/送股/配股复权事件流、`a-share-index` 指数与成分股、`financials/indicators` 五类财务指标。
→ **可覆盖行情底座的大半缺口**；清单中**未见**行业分类、退市名单、分红预案/pay_date、精确全收益指数、
交易日历、60min 线、账户流水，这些需另找源或由系统自产。

⚠️ **连通性未验证**：文档站可访问，但 API 网关在沙盒返回 `policy_default_denied`（出口 IP 不在白名单）。
生产部署前须加白名单并实测；`TUSHARE_TOKEN` 仍未提供，东财 `push2his` 曾返回 502。
所有源必须按 `HTTP 429` / `code=4001` 降并发退避，禁止立即连续重试。

**三个影响正确性（而非仅"有没有"）的缺口**：
1. **复权因子** — 直接污染 `RSI(14)` 与 20 日跌幅，除权日会发出假信号。
2. **历史行业分类** — 前视偏差可能使行业约束的贡献虚高。
3. **账户流水层** — 没有它，「今日信号」「持仓」「取现」三页永远只能是演示数据，B6 也攒不到 300 笔实盘样本。

**P4.1 最小可开工集**：复权日线 + 分红事件流 + 股本事件 + 交易日历 + 股票状态 + 历史行业分类。
精确全收益指数与实盘账户流水可延后，但基准页与持仓页在此之前必须标注「估算 / 演示」。

### 6.2 Forward-only 模式（★ 推荐开工路径：不补历史，从最近开始）

> 决策选项：**放弃复现 2015-2026 历史回测，只从当前（`2026-11-05`）起向前运行**。
> 历史回测保留在 `research/` 作为研究归档，不进生产管道；生产系统只记录「今天之后」的事实。

#### 立刻消失的 5 项考古工作

| 原缺口 | 为什么不再需要 |
|---|---|
| **历史行业分类**（前视偏差） | 用**当前快照**从今天起记录，完全正确；未来变更增量更新即可。+1.81% 的虚高风险自动消失 |
| **退市股回填**（幸存者偏差） | 从今天起记录，未来退市自然入库；不需要补齐历史退市名单 |
| **2015-2018 市值推算**（股本事件流回溯） | 直接用**当前报表市值**；`shares.json` 32.5 万条不再用于回推 |
| **精确全收益指数的历史序列** | 从今天起并行记录价格指数 + 股息率即可自行累积；短期无需付费源 |
| **10 年日 K 全量下载** | 只需最近窗口（下表） |

#### 但「不要历史」≠「零历史」：最小历史窗口（5 项，同花顺 2~3 个接口一次覆盖）

| 因子 | 必需窗口 | 现有资产 | 补齐 |
|---|---|---|---|
| RSI(14) + 20日跌幅 | **~60 交易日 前复权日线** ★ | ❌ 无（只有不复权 close） | `market-dumps` / `corporate-actions` 复权因子 |
| 股息率 TTM | 近 **12 个月**分红 | ✅ 4,963 条（2025-11 起） | 增量更新 |
| 连续分红 ≥5 年 | 近 **5 年**分红 | ✅ `dividends.json` 4.97 万条 | 增量更新 |
| 市值 ≥300 亿 | 当前快照 1 天 | ✅ `shares.json` 到 2026-11-11 | 无需 |
| 行业 L1≤1（银行四级） | 当前快照 1 天 | ✅ `industry.json` | 无需 |

> **关键洞察**：真正省不掉的历史只有「60 日复权价 + 5 年分红文本」，而这两样恰好都是
> 同花顺 `market-dumps` + `corporate-actions` 一次能拿全的轻量数据，**不构成考古负担**。
> 现有缓存已覆盖到 2026-10-30（1400 只），距今天仅差 4 个交易日。

#### 剩下的缺口（性质从「考古」变成「实时」）

| # | 缺口 | 紧迫度 | 说明 |
|---|---|---|---|
| 1 | **账户流水层**（持仓/现金/成交/分红到账/取现日志/每日净值/逐笔做T配对） | ★★★ | **完全不变，且更紧迫**。没有它「持仓/信号/取现」三页永远是演示数据；且从今天起才有样本可攒 |
| 2 | **复权因子** | ★★★ | 除权日机械下跌会触发假超跌信号；现在就开始记录才不会污染未来样本 |
| 3 | 盘中/准实时层（60min 线、实时现价、涨跌停/停牌/ST 状态） | ★★ | 执行约束与「准实时监控」的核心，历史模式反而用不上 |
| 4 | 交易日历（**未来**日历） | ★★ | 判断「12月最后交易日」「月频补仓」；只要未来，不要过去 |
| 5 | B1-B6 基准 **从第一天起并行记录** | ★★ | 见下方警告 |
| 6 | 精确全收益指数 | ✩ 可延后 | 从今天起自攒即可，短期标「估算」 |
| 7 | 网关白名单 / `TUSHARE_TOKEN` 实测 | ★★★ | 与历史无关，**仍是开工第一道门** |

#### ⚠️ 三条必须接受的代价

**① 10.61% 不再是「已验证」，而是「历史样本里的一个数」。**
所有实证结论来自 2015-2026，其中含 2015 股灾、2021/2024 危机 alpha。从 2026-11 起，
系统变成**边跑边验证**：`S*−B2` 与 B6 安慰剂必须从今天起重新攒样本（门槛 300 笔，
按年均 15 笔做T估算需要 **~20 年**；这也是 overlay 标 `experimental` 的原因）。

**② 增强层是「危机 alpha」，前 2-3 年可能颗粒无收。**
2020/2025 等平稳年份基本白干甚至小亏，年度胜率仅 3/12。从最近开始，很可能先经历
一段连续跑输 B2 的枯燥期。**B2 纯持有基准必须从第一天就开跑**，否则以后无法判断。

**③ 基准页短期无统计意义。**
「相对沪深300全收益 +7.3%」这类数字在样本 <3 年时不可展示。UI 应改为
「运行天数 / 累计净值差 / 样本数」进度条，而非年化超额。

#### 推荐落地顺序（Forward-only）

```
D0  加白名单 + 实测同花顺 market-dumps / corporate-actions  ← 开工第一道门
D1  建 da_data 增量管道：复权日线(近60日+) / 分红 / 股本 / 行业 / 日历 / 股票状态
D2  建账户流水层（模拟盘账本）→ 持仓/信号/取现三页当天即可脱离演示数据
D3  B1-B6 六条基准并行开跑（第一天就有净值）
D4  信号引擎 + 成本闸 + 告警；每日落一条 S* vs B2 vs B6 配对记录
D5  跑满 6-12 个月再回看有效性；期间不调参
```

**结论**：从最近开始，**数据源缺口从 13 项收敛到 7 项，且 5 项考古工作全部消失**。
但系统性质从「已验证的历史策略」变成「从零积累的前瞻实验」——
**最该先写的不是数据抓取，是账户账本**（`da_portfolio/dividend.py` + 每日净值表），
因为从今天起每一个数据点都是不可再生的样本。

---

## 7. 验证与基准（系统的核心诉求）

| 基准 | 定义 | 状态 |
|---|---|---|
| B1 集中 | 股息率 Top10、无行业约束、不动 | 启用 |
| B2 分散 | 同漏斗、N=10+行业约束、不动 | ★ 最关键对照 |
| B3 红利指数 | 上证红利 / 红利ETF 510880（全收益口径） | 启用 |
| B4 沪深300 | 全收益口径 | 启用 |
| B5 零成本克隆 | 同 S\* 但成本=0 | 启用 |
| B6 随机安慰剂 | 同日期、同笔数、标的随机 ×200 | ✅ 已跑，分位 75%，**组合层不显著** |

**判定树**：`S* − B2 > 成本` 且 `显著 > B6 (p<0.05, 样本≥300)` 才算增强层有效。
当前 S\*−B2 = **+0.11%/年**（取现方案下），**未通过**，故 overlay 标记 `experimental: true`。

**已证伪清单见 DESIGN.md §10，不要重复实现**（轮动/条件换仓/强制换仓/定向挪仓/
ETF 替代/市场温度计/红利税择时/趋势保护/MACD 金叉/分位排序换入）。

---

## 8. 已知限制（写进 README，不要隐藏）

1. **最大回撤 −42.5%**，年度胜率低，超额集中在波动年份（危机 alpha）
2. **取现兑现率 83.8%**（默认 cash_only）；如需 100% 用 `sell_to_cover`，年化仅差 0.04%
3. **精确全收益指数缺失**，超额数字为估算，可能虚高 2.5-4 个点（若误用价格指数）
4. **幸存者偏差** 0.1-0.5%/年（已量化，未完全消除）
5. **总市值 2015-2018 依赖股本事件流推算**，非报表原值（一致率 98%+）
6. **策略容量有限**：收益部分来自小盘流动性溢价，资金放大到千万级会衰减
7. **B6 未通过**：增强层统计上不显著，实盘需继续积累样本
8. 2026 数据止于 10 月，该年未取现（符合"每年年底"语义）

---

## 9. 开发任务拆分

| 阶段 | 交付 | 验收 |
|---|---|---|
| **P4.1 数据管道** | `da_data/` + fallback + 限流 + 落库 | §6 验收标准通过 |
| **P4.2 核心引擎** | `da_core/broker.py` + `da_portfolio/dividend.py` | `test_broker_t1` / `test_dividend_annual` 通过 |
| **P4.3 信号与 API** | `da_signal/` + `da_api/` + worker | 盘后任务端到端产出订单建议 |
| **P5 Web** | 6 个页面 + 多基准曲线 | 可查看 S\*/B1-B6 与取现流水 |
| **P6 模拟盘** | 6-12 个月纸面运行 | 记录信号 vs 同日随机配对收益，回看 B6 |

**开工顺序严格按 P4.1 → P4.2**，数据管道与 T1Broker 是正确性地基，先写这两块。

---

## 10. 历史文档状态

| 文件 | 状态 | 说明 |
|---|---|---|
| `DESIGN.md` | 历史档案 | 十轮实证推导过程；§0、§7 的旧数字已被本文覆盖 |
| `architecture.md` | 部分有效 | 分层/选型/部署仍有效；头部 9.71% + `IN_POSITION_ONLY` 已过时 |
| `data-and-api.md` | 部分有效 | 12 张表 SQL / 领域模型 / REST 清单有效；"核心仓锁仓禁止做T"已过时 |
| `strategy-and-backtest.md` | 历史档案 | Core-Satellite 原设计，双引擎与成本模型仍可参考 |
| `rotation-engine.md` | **已证伪** | 轮动范式整体关闭；仅"除权跳变修正"结论保留 |
| `research/REPORT1~11.md` | 证据 | 全部实测，REPORT10/11 口径最新 |
