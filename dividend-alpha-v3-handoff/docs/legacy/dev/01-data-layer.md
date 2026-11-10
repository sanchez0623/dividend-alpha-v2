# 01 · 数据层开发规格

> **最重要的一条**：新浪日线经"剔除市场涨跌后的池化回归"验证为**不复权原价**（斜率 1.143 ± 0.084，n=328）。
> 股息率必须基于此计算，禁止再叠加任何复权处理，否则会与分红数据重复计算。

---

## 1. 数据源清单（已验证可用）

| 数据 | 接口 | 验证状态 | 关键注意 |
|---|---|---|---|
| 股票列表 | 新浪 `Market_Center.getHQNodeData?node=hs_a&num=100` | ✅ 5576 只 | 分页上限 100，需 60 页 |
| 日线 | 新浪 `CN_MarketData.getKLineData?scale=240&datalen=5000` | ✅ **不复权** | 单只约 5000 根；并发 ≤12 |
| 分红除权 | 东财 `RPT_SHAREBONUS_DET` | ✅ 49720 条 | 需 `ASSIGN_PROGRESS='实施分配'`；含 `EX_DIVIDEND_DATE`、`PRETAX_BONUS_RMB`(每10股)、`BONUS_IT_RATIO` |
| 指数 | 新浪日线（借同一接口） | ✅ | 000922 / H30269 / 000300 |
| 行业分类 | **未获取** ⚠️ | ❌ | 当前用名称关键词兜底，P3 前必须补齐 |
| 退市名单 | **未获取** ⚠️ | ❌ | 见 §6 偏差处理 |

### 请求规范

```python
HEADERS = {"User-Agent": "Mozilla/5.0 ...", "Referer": "https://finance.sina.com.cn"}
# 东财需 Referer: https://data.eastmoney.com/
# 退避：0.6*(k+1) + random()*0.4，重试 3 次
# 并发：列表/分红串行；行情 12-24 线程；批次写入（每 200 只落盘一次，原子写 os.replace）
```

---

## 2. 抓取模块接口

```python
# da_data/providers/sina.py
def fetch_stock_list(cache=True) -> list[StockRef]:
    """-> [{'symbol':'sh600036','code':'600036','name':'招商银行'}, ...]"""

def fetch_kline(symbol: str, datalen: int = 5000) -> list[Bar]:
    """-> [{'day':'2026-10-30','open':..,'high':..,'low':..,'close':..,'volume':..}, ...]
       注意：close 为不复权原价"""

# da_data/providers/eastmoney.py
def fetch_dividends(since="2008-01-01") -> list[Dividend]:
    """-> [{'code':'600036','ex':'2026-07-10','dps':1.003,'bonus':0.0}, ...]
       dps = PRETAX_BONUS_RMB / 10  (每股派息,元)
       bonus = (BONUS_RATIO + IT_RATIO) / 10  (每股送转股数)"""

# da_data/etl/panel.py
def build_monthly_panel() -> Panel   # 见 §3
```

**落盘**：`research/cache/*.json`，键为 symbol；**必须原子写**（`.tmp` + `os.replace`），否则并发会写坏 JSON（已踩坑）。

---

## 3. 月频面板（Panel）—— 研究态核心数据结构

回测选股用月频即可（实证均基于月频），日线仅用于增强层与实盘。

```python
@dataclass
class Panel:
    months: list[str]          # ['2008-01', ..., '2026-10']，长度 T
    syms:   list[str]          # 长度 S
    CLOSE:  np.ndarray (S,T)   # 月末收盘（不复权）
    AMT:    np.ndarray (S,T)   # 月成交金额 = volume * close
    DIV:    np.ndarray (S,T)   # 该月除权的每股派息(税前)
    SF:     np.ndarray (S,T)   # 送转累积因子，1股初始 -> SF 股
    YLD:    np.ndarray (S,T)   # 股息率 TTM
    RET1:   np.ndarray (S,T)   # 月度总回报（含现金分红，不 reinvest）
```

### 计算规则（严格照此实现）

```python
# 送转累积
SF[j,t] = prod_{u<=t} (1 + BONUS[j,u])

# 股息率 TTM（过去12个月派息，折算到 t 时股数）
num[j,t] = sum_{u=t-11..t} DIV[j,u] * (SF[j,u] / SF[j,t])
YLD[j,t] = num[j,t] / CLOSE[j,t]

# 月度总回报（持有 sf0 份 -> t+1 月变 sf1 份；t+1 月派息按 sf1 计）
val1 = CLOSE[j,t+1] * (SF[j,t+1]/SF[j,t]) + DIV[j,t+1] * (SF[j,t+1]/SF[j,t])
RET1[j,t] = val1 / CLOSE[j,t] - 1
```

**校验基准**（必须作为单元测试）：
- 招商银行 2013-06 股息率 **5.43%**、分位 **100%**；2019-06 **2.33%**、分位 **3.4%**；2023-06 **4.65%**、分位 **96.7%**
- 招行 2013-01 → 2026-10 总回报 **481%**，年化 **13.60%**

---

## 4. 数据库 Schema（生产态）

```sql
CREATE EXTENSION IF NOT EXISTS timescaledb;

CREATE TABLE security (
    symbol      TEXT PRIMARY KEY,          -- 'sh600036'
    code        TEXT NOT NULL,
    name        TEXT NOT NULL,
    exchange    TEXT NOT NULL,             -- SSE | SZSE | BSE
    list_date   DATE, delist_date DATE,    -- 退市日，NULL=在市
    is_active   BOOLEAN DEFAULT TRUE,
    updated_at  TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE security_industry (           -- ⚠️ 待补齐数据源
    symbol TEXT, standard TEXT, level SMALLINT,
    code TEXT, name TEXT, valid_from DATE, valid_to DATE,
    PRIMARY KEY (symbol, standard, level, valid_from)
);

CREATE TABLE daily_bar (
    symbol TEXT, trade_date DATE,
    open NUMERIC(12,4), high NUMERIC(12,4), low NUMERIC(12,4), close NUMERIC(12,4),
    volume BIGINT, amount NUMERIC(20,2),
    limit_up BOOLEAN DEFAULT FALSE, limit_down BOOLEAN DEFAULT FALSE,
    is_suspended BOOLEAN DEFAULT FALSE,
    PRIMARY KEY (symbol, trade_date)
);                                          -- ★ close 为不复权原价
SELECT create_hypertable('daily_bar','trade_date', chunk_time_interval=>INTERVAL '1 year');

CREATE TABLE intraday_bar (                 -- 60min，增强层盘中用
    symbol TEXT, bar_time TIMESTAMPTZ, interval TEXT DEFAULT '60m',
    open NUMERIC(12,4), high NUMERIC(12,4), low NUMERIC(12,4), close NUMERIC(12,4),
    volume BIGINT, amount NUMERIC(20,2),
    PRIMARY KEY (symbol, interval, bar_time)
);

CREATE TABLE dividend (
    id BIGSERIAL PRIMARY KEY, symbol TEXT, code TEXT,
    announce_date DATE, ex_date DATE NOT NULL, record_date DATE, pay_date DATE,
    cash_per_share NUMERIC(12,6),           -- 每股派息(税前)
    share_bonus NUMERIC(12,6),              -- 每股送转
    report_year INT,
    UNIQUE(symbol, ex_date, report_year)
);

CREATE TABLE factor_daily (
    symbol TEXT, trade_date DATE,
    div_yield_ttm NUMERIC(8,4),
    div_yield_pctile_3y NUMERIC(6,2), div_yield_pctile_5y NUMERIC(6,2),
    div_yield_pctile_10y NUMERIC(6,2),      -- ★ 轮动主因子
    div_yield_pctile_seasonal NUMERIC(6,2), -- 同期季节分位（除权免疫，可选启用）
    div_years SMALLINT,                     -- 连续分红年数 ★
    payout_ratio NUMERIC(8,4),
    pe_ttm NUMERIC(12,4), pb NUMERIC(12,4),
    adv_20d NUMERIC(20,2), atr20 NUMERIC(12,4), close NUMERIC(12,4),
    total_score NUMERIC(6,3),
    PRIMARY KEY (symbol, trade_date)
);

CREATE TABLE portfolio (
    id SERIAL PRIMARY KEY, name TEXT,
    kind TEXT,                              -- LIVE | SIM | BENCHMARK
    bench_code TEXT, init_cash NUMERIC(20,2)
);

CREATE TABLE position (
    portfolio_id INT, symbol TEXT,
    tier TEXT DEFAULT 'CORE',               -- CORE | TACTICAL(增强层独立记账)
    shares_total BIGINT, shares_locked BIGINT,   -- ★ T+1
    entry_date DATE, cost_basis NUMERIC(12,4),
    PRIMARY KEY (portfolio_id, symbol, tier)
);

CREATE TABLE trade_lot (                    -- FIFO，用于成本与红利税
    id BIGSERIAL PRIMARY KEY, portfolio_id INT, symbol TEXT,
    buy_date DATE, buy_price NUMERIC(12,4), shares BIGINT,
    remain_shares BIGINT, tier TEXT
);

CREATE TABLE trade (
    id BIGSERIAL PRIMARY KEY, portfolio_id INT, symbol TEXT,
    trade_time TIMESTAMPTZ, side TEXT, shares BIGINT, price NUMERIC(12,4),
    tier TEXT, signal_id BIGINT,
    commission NUMERIC(12,2), stamp_tax NUMERIC(12,2),
    transfer_fee NUMERIC(12,2), slippage_cost NUMERIC(12,2),
    dividend_tax NUMERIC(12,2), realized_pnl NUMERIC(14,2),
    holding_days INT, reason TEXT
);

CREATE TABLE signal (
    id BIGSERIAL PRIMARY KEY, ts TIMESTAMPTZ, symbol TEXT,
    kind TEXT,                              -- ROTATION | OVERLAY | RISK
    direction TEXT, horizon TEXT,
    strength NUMERIC(5,3),
    expected_gross NUMERIC(7,4), expected_cost NUMERIC(7,4), expected_net NUMERIC(7,4),
    entry_price NUMERIC(12,4), target_price NUMERIC(12,4), stop_price NUMERIC(12,4),
    reasons JSONB, strategy_version TEXT, config_hash TEXT,
    status TEXT DEFAULT 'ACTIVE', expires_at TIMESTAMPTZ
);

CREATE TABLE nav_daily (portfolio_id INT, trade_date DATE, nav NUMERIC(16,6),
                        cash NUMERIC(20,2), market_value NUMERIC(20,2),
                        dividend_cash NUMERIC(20,2), PRIMARY KEY(portfolio_id, trade_date));

CREATE TABLE backtest_run (id BIGSERIAL PRIMARY KEY, name TEXT,
                           start_date DATE, end_date DATE,
                           strategy_config JSONB, config_hash TEXT UNIQUE,
                           metrics JSONB, attribution JSONB, created_at TIMESTAMPTZ);
```

---

## 5. 数据质量校验（每日 15:05 自动执行，任一失败即告警）

| # | 校验 | 阈值 |
|---|---|---|
| 1 | 当日日线覆盖率 | ≥99%（剔除停牌） |
| 2 | 股息率异常 | `YLD > 15%` 的标的进人工复核（大概率数据错误） |
| 3 | 复权一致性 | 除权日 `close` 跌幅应 ≈ −dps（不复权口径），偏差 >50% 告警 |
| 4 | 分红记录完整性 | 连续分红年数计算依赖 `ex_date`，缺失即告警 |
| 5 | 面板新鲜度 | 最新月份 = 当前月，否则阻断信号生成 |
| 6 | 价格跳变 | 单日涨跌 >15% 且非除权日 → 标记疑似复权/拆并股问题 |

---

## 6. 幸存者偏差处理

实证结论：**偏差量由 1-3% 下调为 0.1-0.5%/年**（筛选后前瞻12月跌>50% 概率 1.85%→1.12%；候选池股价<3元占比 2.12%）。

**但仍需做**：
1. `security.delist_date` 字段预留，退市股历史数据**永久保留**，不得物理删除
2. 回测股票池：`list_date <= D <= (delist_date or ∞)`
3. **P3 前必须补齐退市名单**（东财/交易所公告），否则在报告中标注"未修正偏差 ≤0.5%/年"

⚠️ **真实风险提示**：B7 最坏的几次亏损全在 2013-01 买入的周期股（盘江 −64%、兖州煤业 −62%、三一 −50%）。它们当时"分位最高=最便宜"，结果又跌两年。**"便宜可以继续更便宜"** —— 这是价值陷阱成本，与幸存者偏差无关，需在前端显著提示。

---

## 7. 调度时序（交易日）

| 时间 | 任务 | 频率 |
|---|---|---|
| 08:30 | 交易日历 + 停牌/涨跌停状态 | 每日 |
| 09:30–14:30 | 拉 60min bar | 每 30 分钟 |
| 09:30–14:30 | **扫描超跌反弹事件**（仅质量池内持仓+候选） | 每 30 分钟 |
| 15:05 | 拉当日日线 | 每日 |
| 15:30 | 增量更新因子（分位、连续分红） | 每日 |
| **每月最后一个交易日 16:00** | **轮动再平衡，生成订单意图** | 月频 ★ |
| 17:00 | 净值结算 + 归因 + 日报推送 | 每日 |
| 每周六 02:00 | 全量因子重算 | 每周 |
