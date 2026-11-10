# 05 · 开发详细文档（执行手册）

> **dividend-alpha v3.1 · Forward-only 待开发版本**
> 目标执行方：DeepSeek Harness ｜ 按 `TASK` 编号顺序执行，每个 TASK 必须给出验收证据
> 前置阅读：`00_INDEX.md` → `01_PRD.md` → 本文对应章节

---

## 1. 工程目录（最终形态）

```
dividend-alpha/
├── README.md                         # 入口，指向 docs/00_INDEX.md
├── pyproject.toml                    # Python 3.11+, uv/poetry
├── requirements.txt                  # ★ 锁 akshare==1.20.35；无 tushare 依赖
├── Makefile                          # init/fetch-smoke/ledger-init/serve/test/lint
├── .env.example                      # THS_API_KEY / POSTGRES_DSN / REDIS_URL
├── docs/
│   ├── 00_INDEX.md                   # ★ 真值声明
│   ├── 01_PRD.md
│   ├── 02_UI_SPEC.md
│   ├── 03_ARCHITECTURE.md
│   ├── 04_DATA_SOURCES.md
│   ├── 05_DEVELOPMENT.md             # 本文
│   └── legacy/                       # 历史研究归档（只读，禁止修改）
├── config/
│   ├── strategy.yaml                 # ★ 唯一参数真值
│   └── providers.yaml                # 数据源 + 限流
├── infra/{Dockerfile,docker-compose.yml}
├── src/
│   ├── da_core/{models,broker,cost,calendar}.py
│   ├── da_data/
│   │   ├── providers/{base,ths,akshare,baostock,eastmoney}.py  # ★ 主+备+2兜底
│   │   ├── {fallback,adjust,ingest,factors,store}.py     # ★ adjust.py
│   ├── da_signal/{base,core_selection,sell_rules,overlay_rsi,registry}.py
│   ├── da_portfolio/{manager,dividend,rebalance,ledger}.py # ★ ledger.py
│   ├── da_benchmark/{b1_b6,placebo,guard}.py              # ★ guard.py
│   ├── da_archive/reader.py                                # 只读 web/data.json
│   ├── da_api/{main,routers,ws,schemas}.py
│   └── da_worker/{daily_close,intraday,annual_withdraw}.py
├── web/
│   ├── prototype.html                # ★ UI 行为基准（8 页）
│   ├── data.json                      # ★ 归档只读数据
│   ├── ui_design/01~08_*.png
│   ├── ui_screens/*.png
│   ├── src/{pages,components,hooks,styles}
│   └── vite.config.ts
├── tests/
│   ├── test_adjust_price.py          # ★ C1
│   ├── test_benchmark_guard.py       # ★ C2（API 层不返回年化超额）
│   ├── test_archive_readonly.py      # ★ C3
│   ├── test_broker_t1.py
│   ├── test_dividend_annual.py
│   ├── test_cost_gate.py
│   ├── test_industry_constraint.py
│   └── test_placebo.py
└── research/                         # ★ 只读归档，禁止修改，不进镜像
```

---

## 2. 数据库 DDL（PostgreSQL 16 + TimescaleDB）

```sql
-- ---------- 行情（hypertable） ----------
CREATE TABLE price_daily (
  symbol        text        NOT NULL,
  trade_date    date        NOT NULL,
  open          numeric, high numeric, low numeric,
  raw_close     numeric     NOT NULL,      -- 不复权（股息率用）
  adj_factor    numeric     NOT NULL DEFAULT 1,
  adj_close     numeric     NOT NULL,      -- 前复权（RSI/跌幅用）★C1
  amount        numeric,
  source        text,
  PRIMARY KEY (symbol, trade_date)
);
SELECT create_hypertable('price_daily','trade_date');

CREATE TABLE corp_action (
  symbol text, ex_date date, announce_date date, pay_date date,
  dps numeric, bonus_ratio numeric DEFAULT 0, transfer_ratio numeric DEFAULT 0,
  rights_ratio numeric DEFAULT 0, rights_price numeric DEFAULT 0,
  PRIMARY KEY (symbol, ex_date)
);

CREATE TABLE symbol_status (
  symbol text, trade_date date, is_st bool, is_suspended bool,
  limit_up bool, limit_down bool, list_date date, delist_date date,
  market_cap numeric, industry_l1 text, industry_l3 text, bank_subtype text,
  PRIMARY KEY (symbol, trade_date)
);

CREATE TABLE factor_daily (
  symbol text, trade_date date,
  div_yield_ttm numeric, consecutive_div_years int,
  rsi_14 numeric, drop_20d numeric,        -- ★ 基于 adj_close
  config_hash text,
  PRIMARY KEY (symbol, trade_date)
);

-- ---------- 账本（关系表） ----------
CREATE TABLE account (
  account_id text PRIMARY KEY, cash numeric NOT NULL,
  total_equity numeric, mode text DEFAULT 'PAPER', updated_at timestamptz
);
CREATE TABLE position (
  account_id text, symbol text,
  shares_total numeric, shares_locked numeric, shares_available numeric,
  cost_price numeric, buy_date date, industry_l1 text,
  PRIMARY KEY (account_id, symbol)
);
CREATE TABLE lot (
  lot_id bigserial PRIMARY KEY, account_id text, symbol text,
  buy_date date, shares numeric, price numeric     -- FIFO 红利税
);
CREATE TABLE orders  (order_id text PRIMARY KEY, account_id text, trade_date date,
                      symbol text, side text, shares numeric, price numeric, status text);
CREATE TABLE fills   (fill_id text PRIMARY KEY, order_id text, account_id text,
                      trade_date date, symbol text, side text, shares numeric,
                      price numeric, commission numeric, stamp_tax numeric, slippage numeric);
CREATE TABLE dividend_event (
  event_id text PRIMARY KEY, account_id text, symbol text,
  ex_date date, pay_date date, dps_gross numeric, tax_withheld numeric, net_cash numeric
);
CREATE TABLE withdraw_log (
  year int, account_id text, want numeric, got numeric, rate numeric,
  executed_at timestamptz, status text,
  PRIMARY KEY (year, account_id)                 -- ★ 幂等键
);
CREATE TABLE daily_nav (
  trade_date date, strategy_id text, account_id text,
  nav numeric, running_days int, paired_trade_count int,
  PRIMARY KEY (trade_date, strategy_id, account_id)
);
CREATE TABLE signal_pair (
  trade_date date, symbol text, real_ret numeric, random_ret numeric,
  config_hash text, PRIMARY KEY (trade_date, symbol, config_hash)
);
CREATE TABLE execution_log (
  task text, idempotency_key text, status text, payload jsonb,
  executed_at timestamptz DEFAULT now(),
  PRIMARY KEY (task, idempotency_key)
);
CREATE TABLE ingest_log (
  id bigserial PRIMARY KEY, source text, capability text,
  latency_ms int, rows int, status text, ts timestamptz DEFAULT now()
);
```

**索引**：`price_daily(symbol, trade_date DESC)`、`factor_daily(trade_date, div_yield_ttm DESC)`、
`signal_pair(trade_date)`、`daily_nav(trade_date, strategy_id)`。

---

## 3. 核心接口签名（固定，不得改名）

```python
# ---------- da_data/providers/base.py ----------
class DataSource(Protocol):
    name: str
    def fetch_daily(self, symbols: list[str], start: date, end: date) -> pl.DataFrame: ...
    def fetch_corp_actions(self, symbol: str, since: date) -> list[CorpAction]: ...
    def fetch_adj_factors(self, symbols: list[str], asof: date) -> pl.DataFrame: ...
    def fetch_symbol_status(self, asof: date) -> pl.DataFrame: ...
    def fetch_index(self, code: str, start: date, end: date) -> pl.DataFrame: ...

class RateLimited(Exception):
    retry_after: float

# ---------- da_data/adjust.py ★ C1 ----------
def build_adj_factor(raw: pd.Series, events: list[CorpAction]) -> pd.Series: ...
def recompute_adj_close(symbol: str, window_days: int = 90) -> int:
    """按最新因子重算窗口内全部 adj_close，返回更新行数。"""

# ---------- da_core/broker.py ----------
class T1Broker:
    def buy(self, symbol: str, shares: int, price: float) -> Fill: ...
    def sell(self, symbol: str, shares: int, price: float) -> Fill: ...
        # assert shares <= position.shares_available
    def on_dividend(self, e: DividendEvent) -> None: ...
    def settle_eod(self) -> None: ...

# ---------- da_signal/base.py ----------
@dataclass(frozen=True)
class Signal:
    symbol: str
    kind: Literal["BUY","SELL","TRIM"]
    strength: float
    expected_net: float            # 成本闸：>=0.008 且 >=3×往返成本
    reason: str
    asof: datetime
    config_hash: str
    price_basis: Literal["ADJ"] = "ADJ"   # ★ 技术指标必须前复权

class SignalPlugin(Protocol):
    def generate(self, ctx: SignalContext) -> list[Signal]: ...

# ---------- da_portfolio/dividend.py ----------
class AnnualWithdraw:
    def on_dividend(self, amount: float) -> None: ...   # ytd_gross += amount
    def run_year_end(self, ctx) -> WithdrawResult: ...  # 幂等 (year, account_id)

# ---------- da_portfolio/ledger.py ★ 最先实现 ----------
class Ledger:
    def init_day_one(self, asof: date, cash: float) -> None: ...
    def write_daily_nav(self, trade_date: date) -> None: ...  # 7 条：S*, B1..B6
    def record_signal_pair(self, real_ret: float, random_ret: float) -> None: ...
    def running_days(self) -> int: ...
    def paired_trade_count(self) -> int: ...

# ---------- da_benchmark/guard.py ★ C2 ----------
MIN_DAYS = 1095
MIN_TRADES = 300

class SampleInsufficient(Exception):
    def __init__(self, n: int, m: int):
        self.need_days = max(0, MIN_DAYS - n)
        self.need_trades = max(0, MIN_TRADES - m)

def get_benchmark_view(mode: str, ledger: Ledger) -> BenchmarkView:
    n, m = ledger.running_days(), ledger.paired_trade_count()
    if mode == "annual" and (n < MIN_DAYS or m < MIN_TRADES):
        raise SampleInsufficient(n, m)
    # 样本不足时响应【不得包含】annual_excess_* 字段（不是前端隐藏）

# ---------- da_archive/reader.py ★ C3 ----------
class ArchiveReader:
    """只读 web/data.json；不提供任何写方法。"""
    def snapshot(self) -> ArchiveSnapshot: ...   # 全部字段带 data_class="归档"
    def report_index(self) -> list[ReportMeta]: ...

# ---------- da_api/schemas.py ----------
class NumberWithClass(BaseModel):
    value: float | None
    data_class: Literal["实时","演示","估算","归档"]
```

---

## 4. TASK 列表（按序执行）

| ID | 任务 | 输入 | 输出 | 依赖 | 验收命令 |
|---|---|---|---|---|---|
| **T00** | **四源连通性实测**：① 同花顺加白名单，实测 `market-dumps` / `corporate-actions` / `prices` 的确切 endpoint、参数、返回字段、**厂商前复权公式**；② 用 `ak.search()` 定位 AKShare 1.20.35 全部所需接口（见 `04 §2.3` 表）；③ 实测 baostock 串行可用性；④ 实测东财 `push2his`，**记录 502 是否复现** | `THS_API_KEY`、出口 IP、`akshare==1.20.35` | `docs/CONNECTIVITY.md`（4 源逐条）+ 回填 `providers.yaml` 的 `resolved_interfaces` | — | `make fetch-smoke` 返回 ≥1 源成功；CONNECTIVITY.md 四源均有实测记录 |
| **T01** | **账本与 DDL**（★ 最先）：建 §2 全部表 + `Ledger.init_day_one` / `write_daily_nav` | DDL | 首日 7 条净值（S\*, B1–B6） | T00 可不阻塞 | `make ledger-init` + 查 `daily_nav` = 7 行 |
| **T02** | **★ C1 前复权日线**：`providers/*` + `fallback` + `adjust.py` + 增量 ingest，落 ≥90 交易日 `raw_close/adj_factor/adj_close` | 同花顺 `market-dumps` + `corporate-actions`；路径 B 降级 | `price_daily` 全 A 覆盖近 90 日 | T00 | `pytest tests/test_adjust_price.py -q` |
| **T03** | 因子层：股息率TTM（raw）、RSI(14)/drop_20d（adj）、连续分红年数、市值、行业 | T02 | `factor_daily` | T02 | `make factors-smoke` 抽查 5 只与手工一致 |
| **T04** | `T1Broker` + 成本模型 + 成本闸 | `config/strategy.yaml` | `da_core` | T01 | `pytest tests/test_broker_t1.py tests/test_cost_gate.py -q` |
| **T05** | 信号插件：`core_selection` / `sell_rules` / `overlay_rsi` / `registry` | T03/T04 | `Signal(expected_net, price_basis="ADJ")` | T03,T04 | `pytest tests/test_industry_constraint.py -q` |
| **T06** | 组合层：目标持仓→订单、月频补仓、分红再投、`AnnualWithdraw`（幂等） | T04/T05 | `da_portfolio` | T04,T05 | `pytest tests/test_dividend_annual.py -q`；重复触发 `withdraw_log` 仅 1 行 |
| **T07** | 基准层：B1–B6 并行、配对安慰剂、B6 配对记录、`signal_pair` | T01/T06 | `da_benchmark` | T01,T06 | `pytest tests/test_placebo.py -q` |
| **T08** | **★ C2 样本守门**：`guard.py` + REST `/api/benchmarks?mode=annual` 抛 `SampleInsufficient` | T07 | API 守门 | T07 | `pytest tests/test_benchmark_guard.py -q`（断言响应无 `annual_excess_*`） |
| **T09** | **★ C3 归档只读**：`ArchiveReader` + `/api/archive` | `web/data.json` | 只读 API | T01 | `pytest tests/test_archive_readonly.py -q`（断言无写方法、字段 `data_class=归档`） |
| **T10** | worker：`daily_close` / `annual_withdraw` / 可选 `intraday`，全部幂等 | T02–T09 | 调度闭环 | 全部 | `make worker-once` 后 `daily_nav` 当日 7 行 |
| **T11** | REST + WS + `data_class` 统一标注 | T08/T09 | `da_api` | T08,T09 | `make serve` + 接口冒烟 |
| **T12** | **前端 8 页**：按 `02_UI_SPEC.md` 实现，逐页对齐 `web/prototype.html` | T11 | React 应用 | T11 | 8 页截图对比 `web/ui_screens/`；A2/A3/A7 人工核验 |
| **T13** | Docker Compose + Makefile + CI | 全部 | 一键起 | 全部 | `make test && make serve` 全绿 |

### 4.1 关键依赖顺序图

```
T00 ──> T02 ──> T03 ──> T05 ──> T06 ──> T07 ──> T08 ──> T11 ──> T12 ──> T13
 │                                                        ▲
 └──> T01 ──> T04 ───────────────────────────────────────┤
              └──> T09 ──────────────────────────────────┘
                            T10（汇合全部）
```

> **T01 与 T02 可并行**，但 T01 优先。理由见 `03_ARCHITECTURE.md §3.3`：
> Forward-only 下账户账本先跑，数据点才不丢。

---

## 5. 测试用例规格

| 测试 | 断言 |
|---|---|
| `test_adjust_price` | §4.5 的 A1–A4 全部通过 |
| `test_benchmark_guard` | `n=1,m=0` 时 `mode=annual` 抛异常；`get_benchmark_view("progress")` 响应**不含** `annual_excess_*` 键；`n=1095,m=300` 时解锁 |
| `test_archive_readonly` | `ArchiveReader` 无 `save/update/delete` 方法；`snapshot()` 每个数值 `data_class=="归档"`；写 `web/data.json` 的尝试被拒 |
| `test_broker_t1` | 当日买入 `available=0`；`sell > available` 抛异常；`settle_eod` 后解锁；FIFO 税率 20%/10%/0% |
| `test_dividend_annual` | 年内分红全额再投；12 月最后交易日取 `ytd×0.30`；重复执行不重复取；`skip_incomplete_year` |
| `test_cost_gate` | `expected_net < 0.008` 或 `< 3×cost` 的信号不进入订单 |
| `test_industry_constraint` | 同 L1 ≤1；银行 4 子类区分；每子类 ≤2 |
| `test_placebo` | 每笔做T成交写 1 行 `signal_pair`（真实 + 同日随机） |

---

## 6. 配置样例（`config/strategy.yaml` 唯一真值）

```yaml
meta:
  version: "3.1"
  mode: FORWARD_ONLY
  start_date: "2026-11-05"
  init_cash: 3000000

universe:
  min_consecutive_dividend_years: 5
  min_market_cap: 3.0e10
  min_div_yield_ttm: 0.025
  exclude_st: true
  exclude_delisting: true
  # 注意：不设流动性门槛；不含 min_roe_ttm / max_profit_decline_yoy 等未验证字段

portfolio:
  target_positions: 10
  weighting: equal
  core_ratio: 0.90
  industry_constraint:
    level: L1
    max_per_industry: 1
    bank_subtype: {treat_as_distinct: true, max_per_subtype: 2}

selection:
  rank_by: div_yield_ttm            # 绝对值降序，禁止分位
  rebalance: monthly                # 仅补仓，不主动换股

sell_rules:
  hard:
    - {cond: "div_yield_ttm < 0.015", action: sell_all}
    - {cond: "dividend_interrupted", action: sell_all}
    - {cond: "holding_months >= 60", action: sell_all}
  overheat: {cond: "unrealized_gain >= 0.50", action: sell_all}

rotation: {enabled: false}          # ★ 已证伪，禁止开启

dividend_reinvest:
  enabled: true
  trigger: on_dividend
  ratio: 1.00
  weight: current_market_value
  lot_size: 100
  min_cash_ratio: 0.02

dividend_withdraw:
  enabled: true
  mode: annual
  timing: last_trading_day_of_December
  base: ytd_cash_dividend_gross
  ratio: 0.30
  shortfall_policy: cash_only       # cash_only | sell_to_cover
  idempotency_key: "(year, account_id)"
  skip_incomplete_year: true

overlay:
  enabled: true
  experimental: true
  scope: POOL_WIDE
  price_basis: ADJ                  # ★ C1
  trigger: {kind: combo, lookback_days: 20, drop_threshold: 0.12,
            rsi_period: 14, rsi_threshold: 25}
  holding_days: 30
  rank_by_strength: true
  max_concurrent: 12
  position_per_trade: 0.03
  cooldown_days: 5
  capital_guard: "amt + cost <= cash * 0.98"
  min_expected_net: 0.008
  cost_multiple: 3

cost:
  commission_rate: 0.00025
  commission_min: 5
  stamp_tax_sell: 0.0005
  transfer_fee: 0.00001
  slippage_bps: 10
  cash_interest_annual: 0.02

benchmarks:
  enabled: [B1, B2, B3, B4, B5, B6]
  annual_view_guard: {min_days: 1095, min_trades: 300}   # ★ C2

data:
  adj_window_trading_days: 90       # ★ C1，最低 60
  providers: {primary: ths_fuyao, fallback: [akshare, baostock, eastmoney]}  # ★ 主+备+2兜底
  akshare_version: "1.20.35"        # 锁版本；接口名由 T00 用 ak.search() 实测
  ths: {max_concurrency: 2, min_interval_sec: 0.5,
        backoff_base_sec: 2, backoff_max_sec: 60,
        check_envelope_code: true, retry_immediately: false}
```

---

## 7. Makefile 目标

```makefile
init:          ## 建库 + 迁移 + 写入 config
fetch-smoke:   ## T00/T02：抓近 90 日前复权日线，打印覆盖率
factors-smoke: ## T03：抽查 5 只因子
ledger-init:   ## T01：写入首日 7 条净值
worker-once:   ## T10：手动跑一次盘后闭环
serve:         ## api + web
test:          ## pytest -q（含 8 个测试文件）
lint:          ## ruff + mypy
```

---

## 8. 验收总检查（交付前逐条给证据）

- [ ] `make test` 全绿（8 个测试文件）
- [ ] `make fetch-smoke`：近 90 交易日前复权覆盖率 100%，除权日无假跳变
- [ ] `make ledger-init`：`daily_nav` 首日 7 行（S\* + B1~B6）
- [ ] `make serve`：8 页可达，与 `web/ui_screens/` 一致
- [ ] **A2**：生产 7 页无裸 `10.61%`；仅 P8 出现且带琥珀警示 + 「只读」
- [ ] **A3**：`GET /api/benchmarks?mode=annual` 在 `n=1` 返回 4xx 且响应体**无** `annual_excess_*`
- [ ] **A4**：`test_adjust_price` 通过；P3 表头标注「前复权」
- [ ] **A7**：所有数值响应带 `data_class`
- [ ] 全库搜索 `rotation.enabled: true` 无命中
- [ ] `docs/legacy/`、`research/`、`web/data.json` 未被修改（git diff 为空）

---

## 9. 交付物清单

| 交付 | 路径 |
|---|---|
| 可运行后端 + 前端 + worker | `src/`、`web/src/`、`infra/` |
| 8 页 UI（含 P8 归档、P5 双模式进度条） | `web/src/pages/` |
| 测试报告 | `pytest -q` 输出 + CI 日志 |
| 连通性实测记录 | `docs/CONNECTIVITY.md`（T00 产出） |
| 首日账本快照 | `daily_nav` 导出 CSV |
| 8 页新截图 | `web/ui_screens/`（与基准对照） |

---

> **§10–§15 是「可直接交给 harness 执行」的补充契约。** 若 §1–§9 与本节冲突，以本节为准。

## 10. REST API 契约（T11/T12 的唯一依据）

### 10.1 统一响应信封

所有数值必须包一层 `NumberWithClass`（见 §3），**禁止裸 `number`**：

```jsonc
{
  "asof": "2026-11-05",
  "mode": "FORWARD_ONLY",
  "data_class": "实时",          // 页面级默认值；单个数值可覆盖
  "config_hash": "sha256:...",
  "data": { /* 见下表 */ }
}
```

```python
class NumberWithClass(BaseModel):
    value: float | None
    unit: Literal["CNY","PCT","COUNT","DAY","RATIO"]
    data_class: Literal["实时","演示","估算","归档"]
    note: str | None = None

# 值为 None 时前端统一渲染 "—"，禁止 NaN / undefined
```

### 10.2 路由表（固定，不得改名）

| 方法 | 路径 | 支撑页 | 响应 `data` 主要字段 | 数据类 |
|---|---|---|---|---|
| `GET` | `/api/health` | topbar | `db_ok, redis_ok, last_trade_date, ingest_status` | 实时 |
| `GET` | `/api/overview` | P1 | `account{...}, progress{n,m,diff_pct}, today_signals[], positions_top5[], forward_note` | 实时 |
| `GET` | `/api/positions` | P2 | `rows[]`（含 `shares_available`）、`industry_chart[]`、`sell_rules` | 实时 / 演示 |
| `GET` | `/api/signals?from=&to=&status=` | P3 | `rows[]`（`price_basis:"ADJ"`）、`params`、`empirical_status` | 演示 → 实时 |
| `GET` | `/api/attribution` | P4 | `module_bars[]`（样本不足为 `待积累`）、`cashflow[]`、`yearly_guard` | 实时 |
| `GET` | `/api/benchmarks?mode=progress\|annual` | P5 | 见 §10.3 | 实时 / 估算 |
| `GET` | `/api/withdraw` | P6 | `ytd_gross, want, countdown_days, execution_log[], rules` | 实时 |
| `GET` | `/api/config` | P7 | `strategy_yaml`(文本)、`limits[]`、`sources[]`、`baseline_check` | 实时 |
| `GET` | `/api/archive` | P8 | **完整** `web/data.json` 快照，每个数值 `data_class="归档"` | 归档 |
| `GET` | `/api/archive/reports` | P8 | `REPORT1~11` 索引 `[{no,title,status}]` | 归档 |
| `POST` | `/api/paper/orders/confirm` | P2/P3 | 仅 `PAPER` 模式；`{order_id, fill_id}` | 实时 |
| `POST` | `/api/admin/worker-once` | 调试 | 手动跑一次 T10 闭环（内部令牌） | 实时 |

**写接口只有最后两个**；`/api/archive*` 代码层不提供任何写方法（验收 `test_archive_readonly`）。

### 10.3 `/api/benchmarks` 双模式契约 ★ C2

```python
# mode=progress（默认，任意样本）
{
  "mode": "progress",
  "progress": {
    "running_days":      NumberWithClass(value=1,    unit="DAY",   target=1095),
    "nav_diff_pct":      NumberWithClass(value=0.0,  unit="PCT"),
    "paired_trades":     NumberWithClass(value=0,    unit="COUNT", target=300)
  },
  "baselines": [{"id":"B1","nav":NumberWithClass(...),"status":"启用"}, ...],
  "guard": {"unlocked": false, "need_days":1094, "need_trades":300}
  # ★ 响应中不得出现任何 annual_excess_* 键
}

# mode=annual 且 n<1095 或 m<300 → HTTP 400
{
  "error": {
    "code": "SAMPLE_INSUFFICIENT",
    "message": "样本不足，暂不展示年化超额",
    "need_days": 1094, "need_trades": 300,
    "archive_link": "/archive"
  }
  # ★ 同样不得出现 annual_excess_* 键
}

# mode=annual 且达标 → 200，才返回 annual_excess_hs300_tr / annual_excess_div_tr
```

> **守门在 API 层**：`guard.py` 抛 `SampleInsufficient` → 全局异常处理器转 400。
> 前端 `.seg` 按钮灰显只是辅助，**不得仅靠前端隐藏**（验收 A3 用 §14 的 curl 验证）。

### 10.4 错误码表

| HTTP | `error.code` | 触发 | 前端行为 |
|---|---|---|---|
| 400 | `SAMPLE_INSUFFICIENT` | `mode=annual` 样本不足 | 渲染 `LockedCard` + 归档跳转 |
| 400 | `INVALID_MODE` | `mode` 非法 | 回落 progress |
| 404 | `NO_DATA` | 该交易日无净值 | 骨架卡 + 「数据暂不可用」 |
| 409 | `IDEMPOTENT_CONFLICT` | `execution_log` 唯一键冲突 | 视为成功，不重复写 |
| 422 | `COST_GATE_REJECTED` | 订单未过成本闸 | 信号行标「拦截」 |
| 503 | `DATA_SOURCE_DOWN` | 主源+备源全失败 | topbar 状态灯转红 + 重试 |
| 500 | `LEDGER_INCONSISTENT` | 账本连续性校验失败 | 硬告警，停止当日任务 |

### 10.5 WebSocket 事件（可选但推荐）

| 事件 | 载荷 | 用途 |
|---|---|---|
| `nav.updated` | `{trade_date, s_nav, b2_nav, running_days, m}` | 进度条实时前移 |
| `signal.generated` | `{signal_id, symbol, cost_gate_status}` | P3 流水追加 |
| `task.failed` | `{task, idempotency_key, error}` | P7 状态灯 |

重连策略：指数退避 + 全量 `GET` 补齐，禁止只依赖增量导致丢点。

---

## 11. 逐 TASK 文件产出清单（harness 按此建文件）

| TASK | 必须创建的文件 | 关键类 / 函数 |
|---|---|---|
| T00 | `docs/CONNECTIVITY.md`（运行时产出） | — |
| T01 | `src/da_core/models.py`、`src/da_portfolio/ledger.py`、`infra/migrations/001_init.sql` | `Ledger.init_day_one / write_daily_nav / running_days / paired_trade_count` |
| T02 | `src/da_data/providers/{base,ths,akshare,baostock,eastmoney}.py`、`{fallback,adjust,ingest,store}.py` | `parse_envelope()`（HTTP+信封 code）、`resolve_symbol()`（thscode 容忍 null）、`build_adj_factor()`、`recompute_adj_close()`；baostock 串行、东财仅兜底 |
| T03 | `src/da_data/factors.py` | `compute_div_yield_ttm(raw)`、`compute_rsi14(adj)`、`compute_drop20d(adj)` |
| T04 | `src/da_core/{broker,cost,calendar}.py` | `T1Broker.buy/sell/settle_eod`、`CostModel.round_trip()` |
| T05 | `src/da_signal/{base,core_selection,sell_rules,overlay_rsi,registry}.py` | `Signal`、`SignalPlugin.generate()` |
| T06 | `src/da_portfolio/{manager,dividend,rebalance}.py` | `AnnualWithdraw.run_year_end()`、`DividendReinvest.on_dividend()` |
| T07 | `src/da_benchmark/{b1_b6,placebo}.py` | `advance_all()`、`record_pair()` |
| T08 | `src/da_benchmark/guard.py`、`src/da_api/errors.py` | `SampleInsufficient` → 400 处理器 |
| T09 | `src/da_archive/reader.py`、`src/da_api/routers/archive.py` | `ArchiveReader.snapshot/report_index`（**无写方法**） |
| T10 | `src/da_worker/{daily_close,intraday,annual_withdraw}.py` | 见 `03_ARCHITECTURE.md §4.1` 时间线 |
| T11 | `src/da_api/{main,routers/*,ws,schemas}.py` | §10 全部路由 + `NumberWithClass` |
| T12 | `web/src/{pages/*,components/*,hooks/*,styles/*}`、`vite.config.ts` | 见 §13 |
| T13 | `infra/{Dockerfile,docker-compose.yml}`、`Makefile`、`.github/workflows/ci.yml`、`.env.example` | §7 全部目标 |

**每个 TASK 完成后必须提交**：文件路径清单 + 对应 `pytest`/`make` 输出，作为验收证据。

---

## 12. 环境变量与配置映射

### 12.1 `.env.example`（照抄）

```dotenv
# 数据源
THS_API_KEY=                  # 同花顺 fuyao 主源，需出口 IP 加白名单
# AKShare 1.20.35 无需密钥；版本锁在 requirements.txt
# 存储
POSTGRES_DSN=postgresql://da:da@localhost:5432/dividend_alpha
REDIS_URL=redis://localhost:6379/0
# 运行
TZ=Asia/Shanghai
RUN_MODE=PAPER                # PAPER | LIVE_IMPORT（禁止自动下单）
INTERNAL_TOKEN=               # /api/admin/* 用
LOG_LEVEL=INFO
```

### 12.2 `config/strategy.yaml` → 读取模块映射（禁止硬编码）

| 配置路径 | 读取方 | 用途 |
|---|---|---|
| `meta.*` | `da_api`, `da_worker` | 版本号 / 起始日 / 本金 |
| `universe.*` | `da_signal/core_selection.py` | 漏斗三条 + ST 排除 |
| `portfolio.*` | `da_portfolio/manager.py` | N=10 / 等权 / core_ratio / 行业约束 |
| `selection.*` | `da_signal/core_selection.py` | 排序键 / 补仓频率 |
| `sell_rules.*` | `da_signal/sell_rules.py` | 硬规则 + 超涨 |
| `rotation.enabled` | **启动自检断言 `is False`** | 若为 true 直接退出进程 |
| `dividend_reinvest.*` | `da_portfolio/dividend.py` | 年内全额再投 |
| `dividend_withdraw.*` | `da_portfolio/dividend.py` | 年度取现 + 幂等键 |
| `overlay.*` | `da_signal/overlay_rsi.py` | 阈值 / RSI / 并发 / 成本闸 |
| `cost.*` | `da_core/cost.py` | 佣金 / 印花 / 滑点 / 利息 |
| `benchmarks.annual_view_guard` | `da_benchmark/guard.py` | 1095 / 300 |
| `data.*` | `da_data/*` | 90 日窗口 / **ths_fuyao → akshare → baostock → eastmoney** / akshare 版本 / 限流 |

启动自检（建议放 `da_api/main.py` 的 lifespan）：

```python
cfg = load_strategy()
assert cfg["rotation"]["enabled"] is False, "轮动已证伪，禁止开启"
assert cfg["overlay"]["price_basis"] == "ADJ"
assert cfg["portfolio"]["target_positions"] == 10
log.info("config_hash=%s", sha256_of(cfg))
```

### 12.3 外部规范来源与落点

| 规范 | 落点 |
|---|---|
| 同花顺《接口总览》原文 10 条要点 | `04_DATA_SOURCES.md §2.2 + §2.2.1` 核对表 |
| AKShare 1.20.35 入口页（仅说明 `ak.search()` 检索，无具体接口名） | `04_DATA_SOURCES.md §2.3`；T00 实测后回填 `CONNECTIVITY.md` |
| `web/prototype.html` 8 页行为 | `02_UI_SPEC.md` + 本文 §13 |
| `web/data.json` 字段 | `da_archive/reader.py` 只读映射，禁止改文件 |

---

## 13. 前端工程规格（T12）

### 13.1 路由表

| 路由 | 页 | 主 API |
|---|---|---|
| `/` `/overview` | P1 总览 | `/api/overview`, `/api/health` |
| `/positions` | P2 持仓 | `/api/positions` |
| `/signals` | P3 信号 | `/api/signals` |
| `/attribution` | P4 归因 | `/api/attribution` |
| `/benchmarks` | P5 基准 | `/api/benchmarks?mode=` |
| `/withdraw` | P6 取现 | `/api/withdraw` |
| `/config` | P7 配置 | `/api/config` |
| `/archive` | P8 归档 | `/api/archive`, `/api/archive/reports` |

侧栏 8 项、分组「监控 / 验证 / 系统 / 归档」与 `prototype.html` 完全一致。

### 13.2 组件树

```
<App>
 ├ <Sidebar>           8 项、选中高亮、归档项带「只读」徽章
 ├ <TopBar>            数据日期 / Forward-only / D+n / 本金 / 状态灯
 ├ <DataClassBadge>    ★ 统一渲染 实时|演示|估算|归档
 ├ <ProgressBar>       label,value,target,fmt,neutral,neg（§02 5.5.1）
 ├ <NavChart>          S*/B2/HS300，区间切换，悬停 tooltip（复用 drawNav 逻辑）
 ├ <ModuleBars> / <YearBars> / <PlaceboHist>
 ├ <DataTable>         斑马纹、.num 右对齐、状态徽章
 ├ <CodeBlock>         YAML 只读渲染（P3/P6/P7）
 ├ <Callout> / <ArchiveBanner> / <LockedCard>
 └ <Footer>
```

### 13.3 Hook → API 映射

| Hook | 端点 | 刷新 |
|---|---|---|
| `useOverview` | `/api/overview` | WS `nav.updated` + 盘后 |
| `useBenchmark(mode)` | `/api/benchmarks?mode=` | 切换时；捕获 400 `SAMPLE_INSUFFICIENT` 渲染 `LockedCard` |
| `useArchive` | `/api/archive` | 仅进入 `/archive` 时拉一次，缓存不失效 |
| `useSignals` | `/api/signals` | WS `signal.generated` |
| `useConfig` | `/api/config` | 启动时一次 |

### 13.4 与原型逐页对照（截图基准）

`web/ui_screens/{overview,positions,signals,attribution,benchmarks,benchmarks_locked,withdraw,config,archive}.png`
共 9 张；`benchmarks_locked.png` 是「年化超额」被拦截态，**必须单独实现并截图**。
`web/ui_design/01~08_*.png` 只作配色/密度参照，其中数字是占位，禁止抄进代码。

---

## 14. 可复制验收脚本

```bash
# 前置
make init && make ledger-init && make serve &

# A1 八页可达（前端 e2e 或人工）
# A3 ★ 年化超额守门：必须 400，且响应体不含 annual_excess_
curl -s -o /tmp/bm.json -w "%{http_code}\n" \
  "http://localhost:8000/api/benchmarks?mode=annual"      # 期望 400
grep -q "annual_excess_" /tmp/bm.json && echo "❌ A3 FAIL" || echo "✅ A3 PASS"
python3 -c "import json;d=json.load(open('/tmp/bm.json'));assert d['error']['code']=='SAMPLE_INSUFFICIENT';print('✅ error.code OK')"

# progress 模式同样不得含年化超额字段
curl -s "http://localhost:8000/api/benchmarks?mode=progress" > /tmp/p.json
grep -q "annual_excess_" /tmp/p.json && echo "❌ progress FAIL" || echo "✅ progress PASS"

# A2 归档隔离：P8 有警示，其他页无裸 10.61%
curl -s http://localhost:8000/api/archive | grep -q "2015-01" && echo "✅ archive OK"
# 前端检查：除 /archive 外，DOM 中不出现 "10.61%"

# A7 数值标注
python3 - <<'PY'
import json,urllib.request
d=json.load(urllib.request.urlopen('http://localhost:8000/api/overview'))
def walk(x):
    if isinstance(x,dict):
        if 'value' in x and isinstance(x['value'],(int,float)):
            assert 'data_class' in x, f"缺 data_class: {x}"
        for v in x.values(): walk(v)
    elif isinstance(x,list):
        for v in x: walk(v)
walk(d); print("✅ A7 PASS")
PY

# A4 前复权
pytest tests/test_adjust_price.py -q

# A8 无禁用逻辑
! grep -R "rotation.*enabled.*true" src/ config/ && echo "✅ A8 PASS"

# A9 ★ v3.1 数据源约束：仅 tushare 移除；主备 + 两个兜底必须齐备
! grep -RiE "tushare|TUSHARE_TOKEN" src/ config/ infra/ requirements.txt \
  && echo "✅ A9 tushare 已清除" || echo "❌ A9 FAIL: tushare 残留"
grep -q "akshare==1.20.35" requirements.txt && echo "✅ akshare 版本锁定 OK"
grep -rq "baostock" src/da_data/providers/ config/ && echo "✅ baostock 兜底已保留" || echo "❌ A9 FAIL: baostock 缺失"
grep -rq "eastmoney" src/da_data/providers/ config/ && echo "✅ 东财兜底已保留" || echo "❌ A9 FAIL: 东财缺失"
python3 -c "import yaml;p=yaml.safe_load(open('config/providers.yaml'));\nassert p['providers']['market']['fallback']==['akshare','baostock','eastmoney'];\nassert p['providers']['market']['baostock']['max_concurrency']==1;print('✅ 降级链与串行约束 OK')"

# A10 T00 已把 AKShare 接口名实测回填
python3 - <<'PY'
import yaml
p=yaml.safe_load(open('config/providers.yaml'))
ak=p['providers']['market']['akshare']
assert ak['version']=='1.20.35'
assert ak['resolved_interfaces'], 'T00 未回填 ak.search() 实测接口'
assert p['providers']['market']['calendar']['resolved_func'], '交易日历接口未核实'
print("✅ A10 CONNECTIVITY 回填 OK")
PY

# 归档只读
pytest tests/test_archive_readonly.py -q
```

---

## 15. 给 harness 的启动指令（复制这段作为任务开头）

> 你是 `dividend-alpha v3.1` 的实现方。先完整阅读 `docs/00_INDEX.md`，按其顺序读
> `01_PRD.md` → `02_UI_SPEC.md` → `03_ARCHITECTURE.md` → `04_DATA_SOURCES.md` → 本文。
> 按 `05_DEVELOPMENT.md §4` 的 **TASK T00→T13** 顺序实现，每个 TASK 按 `§11` 建文件、
> 按 `§10` 实现 API、按 `§13` 实现前端，完成后用 `§14` 命令给出验收证据。
>
> **不可协商**：
> 1. 运行模式 `FORWARD_ONLY`，起始 `2026-11-05`；不复现 2015-2026 历史。
> 2. 先写 `T01` 账本，再写 `T02` 抓取；`daily_nav` 与 `signal_pair` 从第一天起记录。
> 3. 所有参数从 `config/strategy.yaml` 读，禁止硬编码；`rotation.enabled` 必须为 false。
> 4. 技术指标用**前复权** `adj_close`，股息率用**不复权** `raw_close`，两列并存。
> 5. 样本 <1095 天或 <300 笔时，**API 层不返回** `annual_excess_*`。
> 6. **四源链**：同花顺 fuyao（主）→ AKShare 1.20.35（备）→ baostock（串行兜底）→ 东财 push2his（最后兜底）。
>    **仅 `tushare` / `TUSHARE_TOKEN` 禁止出现**（用户明确不用）；
>    **baostock 与东财必须保留**，用户明确要求不得清除。baostock 并发必须 =1。
>    `requirements.txt` 锁 `akshare==1.20.35`。
> 7. 同花顺客户端同时检查 HTTP 状态码与信封 `code`；`429`/`code=4001` 降并发退避，
>    禁止立即连续重试，**且退避耗尽前不切备源**；`thscode` 容忍 `null`
>    （见 `04_DATA_SOURCES.md §2.2.1` 全部 10 条）。
> 8. **AKShare 具体接口名一律由 T00 用 `ak.search()` 实测后写入 `CONNECTIVITY.md`，
>    禁止臆测或硬编码未核实的 `ak.*` 函数名**；官方入口页没有给出任何接口签名。
> 9. 年度取现幂等键 `(year, account_id)`；所有写任务先查 `execution_log`。
>
> **禁止修改**：`research/`（全部）、`docs/legacy/`（全部）、`web/data.json`、
> `web/prototype.html`、`web/ui_design/`、`web/ui_screens/`。这些是真值或归档。
> **禁止实现**：`01_PRD.md §6` 的 16 条已证伪清单。
>
> 交付时逐条给出 `00_INDEX.md §5` 的证据。**本任务只写代码与测试，不写新的设计文档**
> （唯一例外是 T00 产出的 `docs/CONNECTIVITY.md` 实测记录）。

