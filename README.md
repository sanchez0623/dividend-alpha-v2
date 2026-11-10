# dividend-alpha v3.0 · 生产实现

> A 股高股息组合 · 前瞻验证系统（Forward-only）。规格真值见 `dividend-alpha-v3-handoff/docs/00_INDEX.md`。

## 快速开始

```bash
cp dividend-alpha-v3-handoff/config/*.yaml config/   # 唯一参数真值
cp .env.example .env
make init          # 建库（SQLite 本地 / PostgreSQL 生产）
make ledger-init   # 首日 7 条净值（S* + B1~B6）
make fetch-smoke   # 近 90 交易日前复权日线（合成源兜底）
make worker-once   # 盘后闭环一次，daily_nav=7
make test          # 8 个验收测试
make serve         # FastAPI :8100（/da/v1），8 页前端 web/preview.html
```

## 双策略（v3.1）

| 策略 | 账户/策略 ID | 参数真值 | 说明 |
|---|---|---|---|
| S\* 高股息组合 | `PAPER` / `S*` | `config/strategy.yaml` | 原有 10 只高股息组合，Forward-only |
| DC 分红捕获 | `DC` / `DC` | `config/strategy_dividend_capture.yaml` | 股息率≥6%、市值≥500 亿、除权前 5 日买、除权后 30 天卖、单标的、扣红利税、提取净分红 30% |

- 两条策略**并行**：`make worker-once` / `scripts/worker_once.py` 先跑 S\* 再跑 DC，DC 失败不影响 S\*。
- 页面：`http://<host>/` → 侧边栏「策略 → 分红捕获」；接口前缀 `/da/v1/dc/*`。
- 回测与结论：`docs/DC_STRATEGY.md`、`research/dividend_capture/REPORT.md`。
- DC 回测数据只读归档：`web/dc_backtest.json`（站点上标记「归档 · 只读 · 不代表未来」）。

```bash
python scripts/dc_backfill.py 2026-11-05 2026-11-07   # 初始化/回填 DC 净值
python scripts/worker_once.py 2026-11-06              # S* + DC 同日跑
```

## 结构

| 路径 | 内容 |
|---|---|
| `src/da_core` | 模型 / T+1 账户 / 成本闸 / 交易日历 / 唯一参数加载 |
| `src/da_data` | providers(tushare/ths/akshare/baostock/synthetic) + fallback + **前复权** + 因子 |
| `src/da_signal` | core_selection / sell_rules / overlay_rsi / registry |
| `src/da_strategy` | **DC 分红捕获纯逻辑**（回测与生产共用） |
| `src/da_portfolio` | **账本** / 分红再投 / 年度取现(幂等) / 再平衡 / **DC 账本** |
| `src/da_benchmark` | B1–B6 / 安慰剂配对 / **样本守门** |
| `src/da_archive` | **只读** web/data.json |
| `src/da_api` | REST + WS + `data_class` 统一标注 |
| `src/da_worker` | daily_close / annual_withdraw / intraday / scheduler |
| `web/` | React 18 + TS + Vite 8 页（`preview.html` 为零构建预览） |
| `tests/` | 8 个验收测试 |
| `infra/` | Dockerfile ×3 + docker-compose + TimescaleDB 迁移 |

## 硬约束（已落实）

- **接口命名空间 `/da/v1`**（`DA_API_PREFIX` 可配），前后端 + nginx 一致，避免与同机其它服务 `/api` 冲突。见 `docs/DEPLOY_ALIYUN.md`。
- 参数唯一真值 `config/strategy.yaml`，代码零硬编码；`rotation.enabled: false`。
- `raw_close`（股息率）/ `adj_close`（RSI、跌幅）两列并存。
- 年度取现幂等 `(year, account_id)`；每日净值幂等 `(trade_date, strategy_id, account_id)`。
- 样本 <1095 天 / <300 笔时 API **不返回** `annual_excess_*`（409）。
- `da_archive` 无写方法，`web/data.json` 只读。
- 每个数值响应带 `data_class`（实时/演示/估算/归档）。
- `docs/legacy/` 与 `research/` 只读、不进镜像。
