# 02 · 数据模型与 API 设计

> ⚠️ **部分过时**：「核心仓锁仓吃息、禁止做T」已被 [`IMPLEMENTATION.md`](./IMPLEMENTATION.md) 取代。
> 12 张表 SQL、领域模型、Signal 插件接口、REST API 清单仍然有效。


## 1. 数据源清单

| 数据 | 字段要点 | 推荐来源 | 频率 | 关键陷阱 |
|---|---|---|---|---|
| 交易日历 | trade_date, is_open | akshare | 年 | 节假日调休 |
| 股票列表 | symbol, name, list_date, delist_date, is_st | akshare / tushare | 日 | **退市股不可删**，防幸存者偏差 |
| 日线行情 | open/high/low/close/volume/amount/turnover | tushare / baostock | 日 | 必须取**复权因子**自行复权 |
| 60min 线 | 同上（4 根/日：10:30/11:30/14:00/15:00） | akshare | 30分钟 | 半日市/节假日只有 2 根 |
| 复权因子 | adj_factor, split, bonus | tushare | 日 | 除权日突变 |
| 分红送配 | announce_date, ex_date, record_date, pay_date, cash_per_share, share_bonus, trans_bonus | tushare | 日 | **除权除息日**与**公告日**要分开存 |
| 财务报表 | report_period, **announce_date**, revenue, net_profit, roe, eps, debt_ratio, ocf | tushare | 季 | **必须用 announce_date 对齐，否则未来函数** |
| 行业分类 | symbol, sw_l1/l2/l3, citic_l1/l2 | tushare | 月 | 分类会调整，需存生效日期 |
| 指数行情 | 000922 / H30269 / 000300 收盘 | akshare | 日 | 基准同步 |
| 指数成分 | index_code, symbol, in_date, out_date | tushare | 月 | 历史成分用于对照 |
| 股东/股本 | total_share, float_share | tushare | 季 | 市值计算 |

> **免费额度提示**：tushare 需要积分，akshare 免费但不稳定。建议做 **Provider 抽象 + 多源 fallback + 本地缓存**，任一源挂掉自动切换。

---

## 2. 数据库 Schema（PostgreSQL 16 + TimescaleDB）

```sql
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ========== 标的 ==========
CREATE TABLE security (
    symbol          TEXT PRIMARY KEY,          -- '600036.SH'
    name            TEXT NOT NULL,
    exchange        TEXT NOT NULL,             -- 'SSE' | 'SZSE' | 'BSE'
    asset_type      TEXT NOT NULL DEFAULT 'STOCK',  -- STOCK | ETF | INDEX
    list_date       DATE,
    delist_date     DATE,                      -- 退市日，NULL 表示仍上市
    is_active       BOOLEAN DEFAULT TRUE,
    updated_at      TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_security_active ON security(is_active) WHERE is_active;

-- 行业分类（含生效区间，防未来函数）
CREATE TABLE security_industry (
    symbol      TEXT NOT NULL,
    standard    TEXT NOT NULL,                 -- 'SW2021' | 'CITIC'
    level       SMALLINT NOT NULL,             -- 1 | 2 | 3
    code        TEXT NOT NULL,
    name        TEXT NOT NULL,
    valid_from  DATE NOT NULL,
    valid_to    DATE,                          -- NULL = 当前有效
    PRIMARY KEY (symbol, standard, level, valid_from)
);

-- ========== 行情（TimescaleDB 超表）==========
CREATE TABLE daily_bar (
    symbol        TEXT NOT NULL,
    trade_date    DATE NOT NULL,
    open          NUMERIC(12,4), high  NUMERIC(12,4),
    low           NUMERIC(12,4), close NUMERIC(12,4),
    volume        BIGINT, amount NUMERIC(20,2),
    turnover_rate NUMERIC(8,4),
    adj_factor    NUMERIC(16,6),
    adj_close     NUMERIC(12,4),               -- 前复权收盘（回测用）
    is_suspended  BOOLEAN DEFAULT FALSE,
    limit_up      BOOLEAN DEFAULT FALSE,       -- 涨停（影响可成交性）
    limit_down    BOOLEAN DEFAULT FALSE,
    PRIMARY KEY (symbol, trade_date)
);
SELECT create_hypertable('daily_bar','trade_date', chunk_time_interval => INTERVAL '1 year');
CREATE INDEX idx_daily_symbol ON daily_bar(symbol, trade_date DESC);

CREATE TABLE intraday_bar (                    -- 60min，可扩展 30/15min
    symbol      TEXT NOT NULL,
    bar_time    TIMESTAMPTZ NOT NULL,          -- bar 结束时间
    interval    TEXT NOT NULL DEFAULT '60m',
    open NUMERIC(12,4), high NUMERIC(12,4),
    low  NUMERIC(12,4), close NUMERIC(12,4),
    volume BIGINT, amount NUMERIC(20,2),
    PRIMARY KEY (symbol, interval, bar_time)
);
SELECT create_hypertable('intraday_bar','bar_time', chunk_time_interval => INTERVAL '6 months');

-- ========== 分红送配 ==========
CREATE TABLE dividend (
    id              BIGSERIAL PRIMARY KEY,
    symbol          TEXT NOT NULL,
    announce_date   DATE,                      -- 公告日（可得性起点）
    ex_date         DATE NOT NULL,             -- 除权除息日（价格跳空日）
    record_date     DATE,
    pay_date        DATE,
    cash_per_share  NUMERIC(12,6),             -- 每股派息（税前）
    share_bonus     NUMERIC(12,6),             -- 每 10 股送转股数（已换算每股）
    year            INT,                       -- 分红所属年度
    UNIQUE (symbol, ex_date, year)
);
CREATE INDEX idx_div_symbol_ex ON dividend(symbol, ex_date);

-- ========== 财务（按披露日对齐）==========
CREATE TABLE financial (
    symbol          TEXT NOT NULL,
    report_period   DATE NOT NULL,             -- '2025-03-31'
    announce_date   DATE NOT NULL,             -- 实际披露日 ★
    revenue         NUMERIC(20,2),
    net_profit      NUMERIC(20,2),
    net_profit_ded  NUMERIC(20,2),             -- 扣非
    roe             NUMERIC(8,4),
    eps             NUMERIC(10,4),
    debt_ratio      NUMERIC(8,4),
    ocf             NUMERIC(20,2),             -- 经营现金流
    total_share     NUMERIC(20,2),
    PRIMARY KEY (symbol, report_period)
);
CREATE INDEX idx_fin_announce ON financial(symbol, announce_date);

-- ========== 因子（日频快照）==========
CREATE TABLE factor_daily (
    symbol          TEXT NOT NULL,
    trade_date      DATE NOT NULL,
    -- 股息维度
    div_yield_ttm   NUMERIC(8,4),              -- 近12个月股息率 %
    div_yield_5y    NUMERIC(8,4),              -- 5年平均股息率 %
    div_yield_fwd   NUMERIC(8,4),              -- 预期股息率（按最新分红年化）
    div_years       SMALLINT,                  -- 连续分红年数
    payout_ratio    NUMERIC(8,4),              -- 分红率 %
    -- 质量维度
    roe_ttm         NUMERIC(8,4),
    profit_growth   NUMERIC(8,4),              -- 净利同比 %
    ocf_to_profit   NUMERIC(8,4),              -- 现金流/净利
    debt_ratio      NUMERIC(8,4),
    -- 估值维度
    pe_ttm          NUMERIC(12,4),
    pb              NUMERIC(12,4),
    pe_pctile_5y    NUMERIC(6,2),              -- 5年历史分位 0-100
    pb_pctile_5y    NUMERIC(6,2),
    -- 技术与流动性
    close           NUMERIC(12,4),
    ma20 NUMERIC(12,4), ma60 NUMERIC(12,4), ma250 NUMERIC(12,4),
    atr20           NUMERIC(12,4),             -- 做T空间预估
    vol_20d         NUMERIC(10,6),             -- 20日波动率
    adv_20d         NUMERIC(20,2),             -- 20日日均成交额（容量约束）
    -- 综合打分
    div_score       NUMERIC(6,3), quality_score NUMERIC(6,3),
    value_score     NUMERIC(6,3), total_score   NUMERIC(6,3),
    rank_in_universe INT,
    PRIMARY KEY (symbol, trade_date)
);
CREATE INDEX idx_factor_date_score ON factor_daily(trade_date, total_score DESC);

-- ========== 信号 ==========
CREATE TABLE signal (
    id                BIGSERIAL PRIMARY KEY,
    ts                TIMESTAMPTZ NOT NULL,    -- 信号生成时间
    symbol            TEXT NOT NULL,
    kind              TEXT NOT NULL,           -- 'T_TRADE' | 'SWING' | 'SELECT' | 'RISK'
    direction         TEXT NOT NULL,           -- 'BUY' | 'SELL' | 'HOLD'
    horizon           TEXT NOT NULL,           -- '3-7D' | '<1M' | 'LONG'
    strength          NUMERIC(5,3),            -- 0-1 信号强度
    expected_gross    NUMERIC(7,4),            -- 预期毛收益 %
    expected_cost     NUMERIC(7,4),            -- 预期成本 % ★（含双边费+滑点）
    expected_net      NUMERIC(7,4),            -- 预期净收益 % ★
    entry_price       NUMERIC(12,4),
    target_price      NUMERIC(12,4),
    stop_price        NUMERIC(12,4),
    confidence        NUMERIC(5,3),
    reasons           JSONB,                   -- 触发因子明细，用于复盘
    strategy_version  TEXT NOT NULL,
    config_hash       TEXT NOT NULL,           -- 可追溯 ★
    status            TEXT DEFAULT 'ACTIVE',   -- ACTIVE|EXECUTED|EXPIRED|INVALIDATED
    expires_at        TIMESTAMPTZ,
    created_at        TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_signal_active ON signal(status, ts DESC) WHERE status='ACTIVE';
CREATE INDEX idx_signal_symbol ON signal(symbol, ts DESC);

-- ========== 组合与成交 ==========
CREATE TABLE portfolio (
    id           SERIAL PRIMARY KEY,
    name         TEXT NOT NULL,
    kind         TEXT NOT NULL,                -- 'LIVE' | 'SIM' | 'BENCHMARK'
    bench_code   TEXT,                         -- 基准定义（见 03 文档）
    init_cash    NUMERIC(20,2),
    created_at   TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE position (
    portfolio_id  INT REFERENCES portfolio(id),
    symbol        TEXT NOT NULL,
    tier          TEXT NOT NULL DEFAULT 'CORE', -- 'CORE' 核心仓 | 'TACTICAL' 机动仓
    shares_total  BIGINT NOT NULL DEFAULT 0,
    shares_locked BIGINT NOT NULL DEFAULT 0,    -- ★ T+1：当日买入锁定
    cost_basis    NUMERIC(12,4),
    PRIMARY KEY (portfolio_id, symbol, tier)
);

CREATE TABLE trade_lot (                       -- ★ FIFO 批次，用于红利税与成本
    id            BIGSERIAL PRIMARY KEY,
    portfolio_id  INT NOT NULL,
    symbol        TEXT NOT NULL,
    buy_date      DATE NOT NULL,
    buy_price     NUMERIC(12,4) NOT NULL,
    shares        BIGINT NOT NULL,
    remain_shares BIGINT NOT NULL,             -- 未卖出余额
    tier          TEXT NOT NULL,
    PRIMARY KEY (id)
);
CREATE INDEX idx_lot_open ON trade_lot(portfolio_id, symbol, buy_date) WHERE remain_shares > 0;

CREATE TABLE trade (
    id            BIGSERIAL PRIMARY KEY,
    portfolio_id  INT NOT NULL,
    symbol        TEXT NOT NULL,
    trade_time    TIMESTAMPTZ NOT NULL,
    side          TEXT NOT NULL,               -- BUY | SELL
    shares        BIGINT NOT NULL,
    price         NUMERIC(12,4) NOT NULL,
    tier          TEXT NOT NULL,
    signal_id     BIGINT,                      -- 关联信号（做T/波段才有）
    commission    NUMERIC(12,2), stamp_tax     NUMERIC(12,2),
    transfer_fee  NUMERIC(12,2), slippage_cost NUMERIC(12,2),
    dividend_tax  NUMERIC(12,2),               -- ★ 红利税（卖出时结算）
    realized_pnl  NUMERIC(14,2),
    holding_days  INT,                         -- ★ 用于红利税档位
    reason        TEXT
);

-- ========== 净值与归因 ==========
CREATE TABLE nav_daily (
    portfolio_id  INT NOT NULL,
    trade_date    DATE NOT NULL,
    nav           NUMERIC(16,6),
    cash          NUMERIC(20,2),
    market_value  NUMERIC(20,2),
    dividend_cash NUMERIC(20,2),               -- 当日到账股息
    PRIMARY KEY (portfolio_id, trade_date)
);

CREATE TABLE backtest_run (
    id              BIGSERIAL PRIMARY KEY,
    name            TEXT,
    start_date      DATE, end_date DATE,
    strategy_config JSONB NOT NULL,
    config_hash     TEXT NOT NULL UNIQUE,
    metrics         JSONB,                     -- 年化/夏普/回撤/换手...
    attribution     JSONB,                     -- 股息/估值/增强/成本
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE backtest_trade (
    run_id BIGINT REFERENCES backtest_run(id),
    LIKE trade INCLUDING ALL
);
CREATE INDEX idx_bt_trade ON backtest_trade(run_id, trade_time);
```

### TimescaleDB 压缩策略

```sql
ALTER TABLE daily_bar SET (
  timescaledb.compress,
  timescaledb.compress_segmentby = 'symbol',
  timescaledb.compress_orderby = 'trade_date DESC'
);
SELECT add_compression_policy('daily_bar', INTERVAL '3 years');
SELECT add_compression_policy('intraday_bar', INTERVAL '2 years');
-- 连续聚合：周线视图加速长周期回测
CREATE MATERIALIZED VIEW weekly_bar
  WITH (timescaledb.continuous) AS
  SELECT symbol, time_bucket('7 day', trade_date) AS week,
         first(open, trade_date) AS open, max(high) AS high,
         min(low) AS low, last(close, trade_date) AS close, sum(volume) AS volume
  FROM daily_bar GROUP BY symbol, week;
```

---

## 3. 核心领域模型（Python）

```python
# da_core/models.py
from dataclasses import dataclass, field
from datetime import datetime, date
from decimal import Decimal
from enum import Enum

class Tier(str, Enum):
    CORE = "CORE"        # 核心仓：锁仓吃息，禁止做T
    TACTICAL = "TACTICAL"  # 机动仓：做T/波段专用

class SignalKind(str, Enum):
    T_TRADE = "T_TRADE"   # 做T（3-7日）
    SWING   = "SWING"     # 波段（<1月）
    SELECT  = "SELECT"    # 选股/调仓
    RISK    = "RISK"      # 风控信号

@dataclass
class Signal:
    ts: datetime
    symbol: str
    kind: SignalKind
    direction: str                   # BUY | SELL
    horizon: str                     # '3-7D' | '<1M' | 'LONG'
    strength: float                  # 0-1
    expected_gross: float            # % 毛收益
    expected_cost: float             # % 往返成本（含滑点）
    expected_net: float              # % 净收益 = gross - cost ★
    entry_price: Decimal
    target_price: Decimal
    stop_price: Decimal
    confidence: float
    reasons: dict = field(default_factory=dict)
    expires_at: datetime | None = None

    @property
    def is_actionable(self) -> bool:
        """成本闸：净收益必须 > 成本闸阈值，且赔率达标"""
        return (
            self.expected_net >= self.threshold
            and self.risk_reward_ratio >= 1.5
        )

    @property
    def risk_reward_ratio(self) -> float:
        risk = abs(float(self.entry_price) - float(self.stop_price))
        reward = abs(float(self.target_price) - float(self.entry_price))
        return reward / risk if risk > 0 else 0.0

@dataclass
class PositionSnapshot:
    symbol: str
    tier: Tier
    shares_total: int
    shares_locked: int    # T+1 当日买入锁定
    cost_basis: Decimal

    @property
    def shares_available(self) -> int:
        """★ A股 T+1：可卖数量 = 总持仓 - 当日买入锁定"""
        return self.shares_total - self.shares_locked

@dataclass
class OrderIntent:
    symbol: str
    side: str
    shares: int
    tier: Tier
    reason: str
    signal_id: int | None = None
    limit_price: Decimal | None = None
    valid_until: date | None = None
```

---

## 4. Signal 引擎接口（插件式）

```python
# da_signal/base.py
from abc import ABC, abstractmethod
import polars as pl

class SignalGenerator(ABC):
    """所有信号生成器统一接口，便于插拔与 A/B 测试"""
    kind: SignalKind
    version: str = "v1"

    @abstractmethod
    def scan(
        self,
        bars: pl.DataFrame,      # 含 symbol, ts, o/h/l/c/v
        factors: pl.DataFrame,   # 当日因子
        positions: dict[str, PositionSnapshot],
        cfg: dict,
    ) -> list[Signal]: ...

    def cost_gate(self, sig: Signal, cfg: dict) -> bool:
        """统一成本闸：防止无意义交易"""
        min_net = cfg["signal"]["min_expected_net_pct"]    # 默认 0.8
        cost_mult = cfg["signal"]["cost_multiple"]         # 默认 3.0
        return (
            sig.expected_net >= min_net
            and sig.expected_net >= cost_mult * sig.expected_cost
        )
```

注册方式（配置驱动，不改代码）：

```yaml
signal_generators:
  - name: mean_reversion_t
    module: da_signal.t_trade.mean_reversion
    kind: T_TRADE
    enabled: true
    weight: 0.4
  - name: bollinger_extreme
    module: da_signal.t_trade.bollinger
    kind: T_TRADE
    enabled: true
    weight: 0.3
  - name: swing_momentum
    module: da_signal.swing.momentum
    kind: SWING
    enabled: true
    weight: 0.5
```

---

## 5. REST API 清单

基础路径 `/api/v1`，JSON，JWT 鉴权。

### 标的与因子

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/universe?date=&filters=` | 可选股池（带行业/股息率过滤） |
| GET | `/securities/{symbol}` | 标的基础信息 |
| GET | `/factors?date=&symbols=&sort=total_score` | 因子快照 |
| GET | `/factors/{symbol}/history?start=&end=` | 单标的因子时间序列 |
| GET | `/dividends/{symbol}` | 分红历史与未来除权日 |
| GET | `/bars/{symbol}?interval=1d\|60m&start=&end=` | K线（含复权选项） |

### 信号

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/signals?status=ACTIVE&kind=&min_net=` | 信号列表 |
| GET | `/signals/{id}` | 信号详情（含 reasons 明细） |
| POST | `/signals/{id}/execute` | 标记为已执行（联动下单） |
| POST | `/signals/{id}/dismiss` | 忽略（记录原因，用于复盘） |
| GET | `/signals/stats?start=&end=` | 信号胜率、平均净收益统计 |
| WS | `/ws/signals` | **实时推送新信号** |

### 组合与交易

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/portfolios` | 组合列表（含基准组合） |
| GET | `/portfolios/{id}/positions` | 持仓（含 CORE/TACTICAL 分层） |
| GET | `/portfolios/{id}/nav?start=&end=` | 净值曲线 |
| GET | `/portfolios/{id}/trades?start=&end=` | 成交明细 |
| POST | `/portfolios/{id}/rebalance` | 触发再平衡（返回订单意图） |
| POST | `/portfolios/{id}/orders` | 提交订单意图（需风控校验） |
| GET | `/portfolios/{id}/risk` | 当前风险敞口（行业/单票/回撤） |

### 回测与归因

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/backtests` | 提交回测任务（异步，返回 run_id） |
| GET | `/backtests/{id}` | 回测结果（指标 + 归因） |
| GET | `/backtests/{id}/trades` | 回测成交明细 |
| GET | `/benchmarks/compare?start=&end=&bench=B1,B2,B3,B4` | **多基准对比曲线** |
| GET | `/attribution/{portfolio_id}?start=&end=` | 收益归因（股息/估值/增强/成本） |
| GET | `/attribution/significance?run_id=` | 增强层显著性检验结果 |

### 配置与系统

| 方法 | 路径 | 说明 |
|---|---|---|
| GET/PUT | `/config/strategy` | 策略参数读写（带版本） |
| GET | `/config/versions` | 配置历史与 config_hash |
| GET | `/health` / `/health/data` | 健康检查 / 数据新鲜度 |
| POST | `/admin/jobs/{name}/trigger` | 手动触发任务 |

### 关键响应示例

```jsonc
// GET /api/v1/signals?status=ACTIVE
{
  "total": 3,
  "items": [
    {
      "id": 10241,
      "ts": "2026-10-30T10:32:00+08:00",
      "symbol": "600036.SH",
      "name": "招商银行",
      "kind": "T_TRADE",
      "direction": "SELL",
      "horizon": "3-7D",
      "strength": 0.72,
      "expected_gross": 2.10,
      "expected_cost": 0.31,
      "expected_net": 1.79,          // ★ 净收益，前端高亮
      "entry_price": 38.42,
      "target_price": 37.60,
      "stop_price": 39.10,
      "confidence": 0.68,
      "suggested_shares": 2000,
      "tier": "TACTICAL",
      "reasons": {
        "rsi_6": 78.4,
        "dev_from_ma20": 4.2,
        "volume_ratio": 1.6,
        "atr20": 0.62
      }
    }
  ]
}
```

```jsonc
// GET /api/v1/benchmarks/compare
{
  "start": "2018-01-01", "end": "2026-10-01",
  "series": {
    "S0_strategy":  { "annual": 14.2, "mdd": -21.3, "sharpe": 1.05, "turnover": 1.8 },
    "B1_concentrated": { "annual": 9.8,  "mdd": -34.1, "sharpe": 0.55, "turnover": 0.2 },
    "B2_diversified":  { "annual": 10.6, "mdd": -26.7, "sharpe": 0.71, "turnover": 0.2 },
    "B3_div_index":    { "annual": 8.4,  "mdd": -28.9, "sharpe": 0.52, "turnover": 0.1 },
    "B4_hs300":        { "annual": 5.1,  "mdd": -39.2, "sharpe": 0.31, "turnover": 0.1 },
    "B6_random_t":     { "annual": 9.1,  "mdd": -27.0, "sharpe": 0.60, "turnover": 1.8 }  // ★ placebo
  },
  "enhancement_vs_B2": 3.6,      // 增强层年化贡献
  "enhancement_vs_B6": 5.1,      // ★ 相对安慰剂，才是真 alpha
  "significance": { "t_stat": 2.31, "p_value": 0.021, "n_trades": 247 }
}
```

---

## 6. 数据一致性关键规则

1. **可得性对齐**：任何基本面字段在计算 `date = D` 的因子时，只能使用 `announce_date <= D` 的数据。
2. **分红可得性**：计算股息率时，只能用 `ex_date <= D` 的已实施分红；预测股息率需标注为 `fwd` 且与 `ttm` 分开。
3. **退市股保留**：`is_active=false` 但历史数据完整保留，回测股票池包含所有 `list_date <= D <= delist_date` 的标的。
4. **复权统一**：回测统一使用**前复权**价格序列；实盘价格用**不复权**价格 + 除权日仓位调整。两套逻辑必须分开，不能混用。
5. **停牌处理**：停牌日不产生信号，`shares_available` 不变，成本按暂停计算（不计入换手）。
6. **ST 过滤**：默认剔除 ST/*ST/退市整理期标的（可配置）。
