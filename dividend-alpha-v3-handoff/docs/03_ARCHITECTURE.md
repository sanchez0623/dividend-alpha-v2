# 03 · 架构设计文档

> **dividend-alpha v3.1 · Forward-only**
> 技术栈：Python 3.11+ / FastAPI / PostgreSQL 16 + TimescaleDB / Redis / React 18 + TS + Vite
> 调度：APScheduler（单实例起步，可平滑换 Celery）

---

## 1. 架构总图

```
┌───────────────────────────────────────────────────────────────────────────┐
│  Web（React+TS，8 页签）  ← REST / WebSocket ←  da_api (FastAPI)          │
│   P1 总览  P2 持仓  P3 信号  P4 归因  P5 基准  P6 取现  P7 配置  P8 归档  │
└───────────────────────────────────┬───────────────────────────────────────┘
                                    │
┌───────────────────────────────────▼───────────────────────────────────────┐
│  应用层 da_app                                                              │
│   PortfolioService  SignalService  BenchmarkService  ArchiveService(只读)    │
│   ★ SampleGuard：n<1095 或 m<300 时禁止返回 annual_excess_*                 │
└───────┬───────────────────┬───────────────────┬───────────────────────────┘
        │                   │                   │
┌───────▼────────┐ ┌────────▼────────┐ ┌───────▼──────────────────────────┐
│ da_portfolio   │ │ da_signal       │ │ da_benchmark / da_archive         │
│ ★ 账本（核心） │ │ 插件式信号      │ │ B1–B6 并行 · 安慰剂 · 只读归档    │
│ T1Broker       │ │ core_selection  │ │                                   │
│ AnnualWithdraw │ │ sell_rules      │ │ da_backtest（仅研究态，不进生产）  │
│ DividendReinvest│ │ overlay_rsi ★  │ │                                   │
│ DailyNAV       │ │ cost_gate       │ │                                   │
└───────┬────────┘ └────────┬────────┘ └───────┬──────────────────────────┘
        │                   │                   │
┌───────▼───────────────────▼───────────────────▼──────────────────────────┐
│  da_data · 增量数据管道（Forward-only，只向前，不回填历史）                 │
│  providers: ths(主)→akshare(备)→baostock(串行)→eastmoney(兜底)  ★4源     │
│  fallback + 限流退避 │ adjust.py ★前复权 │ ingest │ factors │ store         │
└───────┬───────────────────────────────────────────────────────────────────┘
        │
┌───────▼───────────────────────────────────────────────────────────────────┐
│  存储                                                                       │
│  PostgreSQL 16 + TimescaleDB（行情/净值 hypertable、账本关系表）             │
│  Redis（限流计数、任务锁、热缓存）  │  Parquet 冷备  │  web/data.json（只读）│
└───────────────────────────────────────────────────────────────────────────┘
```

---

## 2. 分层职责矩阵

| 层 | 模块 | 职责 | 禁止 |
|---|---|---|---|
| `da_core` | models / broker / cost / calendar | 领域模型、T+1 账户、成本闸、交易日历 | 含任何业务策略参数 |
| `da_data` | providers / fallback / adjust / ingest / factors / store | 增量抓取、**前复权**、因子落库 | 回补 2015-2026 历史；改 `research/` |
| `da_signal` | core_selection / sell_rules / overlay_rsi / registry | 生成 Signal（含 `expected_net`） | 直接下单；绕过成本闸 |
| `da_portfolio` | manager / dividend / rebalance / **ledger** | 目标持仓→订单、分红再投、年度取现、**每日净值** | 允许非幂等写入 |
| `da_benchmark` | b1_b6 / placebo / guard | 6 条基准并行、配对样本、样本守门 | 样本不足时返回年化超额 |
| `da_archive` | reader | 只读 `web/data.json` + 报告索引 | 任何写操作 |
| `da_worker` | daily_close / intraday / annual_withdraw | 定时任务编排 | 无幂等键的任务 |
| `da_api` | routers / ws / schemas | REST + 推送 + `data_class` 标注 | 硬编码前端判断 |
| `web` | 8 页 | 展示与交互 | 自行计算年化超额 |

---

## 3. ★ 正确性地基（四个不可省的设计）

### 3.1 T1Broker（T+1 + FIFO 红利税）

```
Position { shares_total, shares_locked, shares_available, lots[FIFO] }
Lot      { buy_date, shares, price }

buy()  → shares_total += n; shares_locked += n; append Lot
sell() → assert n <= shares_available; 按 FIFO 出 Lot；按持股期定税率
settle_eod() → shares_locked = 0
on_dividend() → cash += dps * shares_total；记录 taxable 事件
```

> 绝大多数"做T很赚钱"的回测失真都源于没做这一条。

### 3.2 前复权（★ 本轮变更 C1 的架构落点）

- 所有**技术指标**（RSI(14)、20日跌幅、信号强度）必须基于 `adj_close`（前复权）
- **股息率 TTM** 必须用 `raw_close`（不复权）——分子每股分红与分母股价必须同口径
- 存储时两列并存：`raw_close` / `adj_close` / `adj_factor`，禁止只存其一
- 详见 `04_DATA_SOURCES.md §4`

### 3.3 账本优先（Forward-only 的第一原则）

> **先写账本，再写抓取。** 从 2026-11-05 起每个数据点不可再生，晚一天开跑就少一天。

`ledger` 最小表集：`account、position、lot、order、fill、dividend_event、withdraw_log、daily_nav、signal_pair`。
`daily_nav` 每天一条，含 `s_nav / b1..b6_nav / running_days / paired_trade_count`，
是 P5 双模式守门的唯一数据来源。

### 3.4 幂等

| 任务 | 幂等键 |
|---|---|
| 年度取现 | `(year, account_id)` |
| 每日净值 | `(trade_date, account_id, strategy_id)` |
| 盘后因子 | `(trade_date, symbol, factor_name)` |
| 信号 | `(trade_date, symbol, signal_kind, config_hash)` |

所有写任务先查 `execution_log` 再执行；重复调度不得产生第二行。

---

## 4. 运行模式与调度（Asia/Shanghai）

### 4.1 交易日时间线

| 时点 | 任务 | 依赖 | 幂等键 |
|---|---|---|---|
| 09:00 | 交易日历检查、股票状态同步 | 未来日历 | `(trade_date)` |
| 15:30 | 日线/分红/股本/行业增量落库 | 主源或备源 | `(trade_date, symbol)` |
| 15:45 | **前复权因子重算 + adj_close 回写** 🆕 | 落库完成 | `(trade_date, symbol)` |
| 16:00 | 因子：股息率TTM / 连续分红年数 / 市值 / RSI(14) / 20日跌幅 | adj_close | `(trade_date, symbol)` |
| 16:15 | 选股漏斗 + 行业约束 → 目标持仓 | 因子 | `(trade_date)` |
| 16:30 | 卖出规则 + overlay 信号扫描（全池） | 目标持仓 | `(trade_date, symbol, kind)` |
| 16:45 | 成本闸校验 → 订单建议 → 告警推送 | 全部 | `(trade_date, order_id)` |
| 16:50 | **每日净值落库 + B1–B6 并行推进** ★ | 账本 | `(trade_date, strategy_id)` |
| **12月最后交易日 16:55** | **年度取现（幂等）** | 当日分红已入账 | `(year, account_id)` |
| 次日 09:00 | 订单确认 / 模拟盘记账 | 人工或导入 | `(fill_id)` |

### 4.2 三种运行模式

| 模式 | 说明 |
|---|---|
| `PAPER`（默认） | 模拟盘账本，全部自造数据，不需券商接口 |
| `LIVE_IMPORT` | 导入券商交割单生成 `fill`，系统只做监控与建议 |
| `RESEARCH`（研究态） | 仅本地跑 `research/` 脚本，**不进生产镜像** |

> A 股无面向个人的官方交易 API，实盘下单只能人工或券商客户端，本系统**不下单**。

### 4.3 盘中（可选）

60min K 线轮询（10:30 / 11:30 / 14:00 / 15:00 四根；半日市 2 根），
仅用于「准实时信号提示」，**不触发自动订单**。

---

## 5. 数据流（Forward-only 每日闭环）

```
[数据源] ──增量──> raw_price / dividend_event / share_event / industry / calendar
                        │
                        ├─> adjust.py ──> adj_close (前复权) ──┐
                        │                                      ├─> factors
                        └─> raw_close ──> 股息率TTM ────────────┘   (RSI/跌幅/分位)
                                                                     │
                        ┌──────────────────────────────────────────────┘
                        ▼
              da_signal → Signal(expected_net) → cost_gate → OrderSuggestion
                        │
                        ▼
              da_portfolio (T1Broker) → Fill → ledger
                        │
                        ▼
              daily_nav (S*, B1..B6) ──> SampleGuard ──> da_api ──> Web 8 页
                        │
                        └─> signal_pair（真实 vs 同日随机）→ B6 配对样本 m
```

**B6 配对记录必须从第一天就写**：每笔做T成交时，同时记录「同日期、同笔数、标的随机」的对照收益，
否则以后永远攒不到 300 笔样本。

---

## 6. 部署拓扑

```yaml
# infra/docker-compose.yml（节选）
services:
  api:      { image: da-api,      ports: ["8000:8000"], depends_on: [db, redis] }
  worker:   { image: da-worker,   depends_on: [db, redis] }   # APScheduler 单实例
  web:      { image: da-web,      ports: ["3000:80"] }         # nginx 静态
  db:       { image: timescale/timescaledb-ha:pg16 }
  redis:    { image: redis:7-alpine }
volumes: [pgdata, parquet_cold]
```

- 单实例起步足够（每日一次批量，负载极低）
- `config/strategy.yaml` 以只读卷挂载；修改需重启 worker
- `web/data.json` 以只读卷挂载给 `da_archive`
- 日志结构化 JSON；关键事件（取现、基线校验失败、限流退避）单独告警

---

## 7. 可观测性

| 指标 | 告警条件 |
|---|---|
| 数据源成功率 | 主源连续 3 日失败且备源也失败 |
| 前复权覆盖率 | `adj_factor` 缺失 symbol 数 >0 |
| 账本连续性 | `daily_nav` 缺交易日 |
| 基准并行 | B1–B6 任一当日未推进 |
| 限流 | `429` / `code=4001` 触发退避 |
| 幂等冲突 | `execution_log` 唯一键冲突（应为 0） |
| 基线校验 | `test_dividend_annual` 失败 |

---

## 8. 安全与配置

- 所有密钥走环境变量：`THS_API_KEY` / `POSTGRES_DSN` / `REDIS_URL`（AKShare 无需密钥）
- `requirements.txt` 锁 `akshare==1.20.35`；**仅 tushare 移除**；**baostock（并发=1）与东财 push2his 必须保留为兜底**，见 `04_DATA_SOURCES.md §6`
- `.env` 不入库不入镜像；`config/providers.yaml` 只放 `${VAR}` 引用
- 同花顺 `THS_API_KEY` 需在生产出口 IP 加白名单（沙盒曾返回 `policy_default_denied`）
- API 只读为主；写操作仅限 `paper` 账本任务，带内部服务令牌
- `da_archive` 路由强制只读，代码层不提供写方法

---

## 9. 与 `research/` 的隔离

| | 生产 | 研究归档 |
|---|---|---|
| 路径 | `src/`、`config/`、`web/` | `research/`、`docs/legacy/` |
| 数据 | 每日增量 PostgreSQL | 一次性 JSON/NPZ 快照 |
| 时间 | 2026-11-05 起 | 2015-01 ~ 2026-10 |
| 引擎 | 事件驱动账本 | 研究脚本（不再运行） |
| 镜像 | ✅ 进 | ❌ 不进 |
| 修改 | 允许 | **禁止** |
