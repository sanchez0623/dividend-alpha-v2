# 00 · 文档索引与真值声明

> **dividend-alpha v3.1 · 待开发版本（Forward-only）**
> 整理日期：2026-11-05 ｜ 目标执行方：DeepSeek Harness（可编码 agent）
> **状态：设计冻结，等待开发。生产代码尚未存在。**

---

## 0. 唯一真值声明（读之前先看）

1. 本目录 `docs/00~05` 是**唯一权威规格**。任何与之冲突的内容一律作废。
2. `docs/legacy/` 内为历史研究文档（`DESIGN.md` / `architecture.md` / `rotation-engine.md` /
   `strategy-and-backtest.md` / `data-and-api.md` / `IMPLEMENTATION.md` v2.0 / `REPORT1~11.md`），
   **仅作证据归档，不得作为开发依据**。其中大量数字（9.71% / 11.15% / N=20 / IN_POSITION_ONLY /
   锁仓吃息 / 轮动引擎）已被推翻或降级。
3. `research/` 内全部为一次性研究脚本与缓存快照，**不进生产镜像、不进 git 主分支代码路径**。
4. `web/prototype.html` 是**UI 行为基准**（8 页签、交互、数据标注），开发产物必须与之逐页对齐。
5. **`config/strategy.yaml` 是唯一参数真值**，代码不得硬编码策略参数。
6. **四源链**（v3.1）：同花顺 fuyao（主）→ AKShare 1.20.35（备）→ baostock（串行）→ 东财 push2his（最后兜底）。
   **仅 `tushare` / `TUSHARE_TOKEN` 移除**；**baostock 与东财保留**，缺失即回归缺陷。

---

## 1. 文档清单与阅读顺序

| 顺序 | 文件 | 用途 | 谁必须读 |
|---|---|---|---|
| 1 | `00_INDEX.md` | 真值声明、硬约束、交付清单 | 全部 |
| 2 | `01_PRD.md` | 产品需求：8 个页签、业务规则、验收 | 全部 |
| 3 | `02_UI_SPEC.md` | UI 规格：设计系统、逐页元素、双模式状态机 | 前端 |
| 4 | `03_ARCHITECTURE.md` | 架构：分层、调度、正确性设计、部署 | 后端 / 架构 |
| 5 | `04_DATA_SOURCES.md` | 数据源（**v3.1 仅同花顺 + AKShare**）：缺口矩阵、**RSI 复权日线补齐方案**、官方总览 10 条核对表、`ak.search()` 接口发现规则 | 数据 / 后端 |
| 6 | `05_DEVELOPMENT.md` | 开发手册：目录、DDL、接口签名、**TASK T00–T13**、REST 契约、文件清单、配置映射、前端规格、验收脚本、**§15 harness 启动指令** | 执行方 |
| — | **`06_HANDOFF.md`** | **面向发包方**：目标模式怎么写、T00 凭据二选一、可直接复制的完整指令、分阶段吩咐、收货核对 | **你**（不交给 harness） |

> `00~05` 是给 harness 的施工依据；`06` 是给你下任务用的元指令，**不要整份丢给 harness**
> （里面包含「它可能会跑偏」的预判与退回条件，交给它反而会诱导自证）。
> 只把 `06 §4` 那段贴进任务目标即可。

配套资产（只读参考）：

| 路径 | 内容 |
|---|---|
| `web/prototype.html` | 可交互 8 页原型（行为基准） |
| `web/ui_design/01~08_*.png` | 8 张 AI 视觉稿（配色/密度基准，数字为占位） |
| `web/ui_screens/*.png` | 8 张原型真实渲染截图 |
| `web/data.json` | 历史回测导出数据（**仅供「历史归档」页读取**） |
| `config/strategy.yaml` | 策略参数真值 |
| `config/providers.yaml` | 数据源与限流配置 |
| `docs/legacy/` | 历史研究归档 |

---

## 2. 本版本（v3.1）相对 v2.0 的变更

| # | 变更 | 落点 |
|---|---|---|
| **C1** | 补齐 `RSI(14) + 20日跌幅` 所需的 **~60 交易日前复权日线** | `04_DATA_SOURCES.md §4`、`05_DEVELOPMENT.md T02` |
| **C2** | 基准页：样本 <3 年**不展示年化超额**，改为「运行天数 / 累计净值差 / 样本数」进度条，**可切换** | `02_UI_SPEC.md §5.5`、`web/prototype.html` |
| **C3** | `10.61%` 降级为「历史样本里的一个数」；所有已回测内容集中到**独立的「历史归档」Tab**（第 8 页），生产页不把它当现行指标 | `01_PRD.md §3.8`、`02_UI_SPEC.md §5.8`、`web/prototype.html` |
| **C4（v3.1）** | **tushare 移除**；主备改为同花顺 fuyao + AKShare 1.20.35；**baostock 与东财保留为兜底**（用户明确要求）。AKShare 接口名由 T00 用 `ak.search()` 实测 | `04_DATA_SOURCES.md §2.3 / §6`、`config/providers.yaml`、`05_DEVELOPMENT.md §15` |

同时确立 **Forward-only 运行模式**：生产系统自 `2026-11-05` 起只向前记录，不复现 2015-2026 历史。

> **v3.1 数据源决策说明**：原链为 `tushare(主) → 同花顺 → akshare → baostock`。
> 按用户决策 **只移除 tushare**，主源改为同花顺、akshare 升为备源。
> **baostock 与东财 push2his 保留**（用户明确「不能清除」）：前者串行兜底、后者最后兜底，
> 不得作主源。完整链见 `04_DATA_SOURCES.md §6`。

---

## 3. 给执行方的硬约束（不可协商）

### 必须做

- **执行方直接以 `05_DEVELOPMENT.md §15` 的启动指令作为任务开头**，按 T00→T13 顺序施工；
  §10–§14 是可直接落地的 REST 契约、文件清单、配置映射、前端规格与验收命令。
- 先写**账户账本**（`da_portfolio` + 每日净值表），再写数据抓取。Forward-only 下每个数据点不可再生，晚一天开跑就少一天。
- 所有策略参数从 `config/strategy.yaml` 读取，禁止硬编码。
- 任何写库任务必须**幂等**（尤其年度取现：`(year, account_id)` 唯一键）。
- 所有价格计算使用**前复权价**；股息率计算使用**不复权价**（分子分母口径见 `04 §4.4`）。
- UI 上每个数字必须带数据标注：`实时 / 演示 / 估算 / 归档` 四选一，见 `02 §3`。
- 同花顺客户端必须**同时检查 HTTP 状态码与响应信封 `code`**；`429` / `code=4001` → 降并发 + 退避，禁止立即连续重试。

### 禁止做（已证伪，见 `01_PRD.md §6`）

- ❌ 股息率分位轮动、条件换仓、强制换仓、定向挪仓、分批挪仓
- ❌ MACD 金叉、趋势保护、市场温度计减仓、红利税择时、ETF 系统性替代、年内预留取现
- ❌ 按股息率**分位**排序换入（必须按绝对值）
- ❌ 修改 `research/` 内任何文件；修改 `web/data.json`
- ❌ 在样本 <3 年时展示「年化超额」
- ❌ 把 `10.61%` 写成"预期收益 / 已验证年化"

---

## 4. 一句话产品定义

> 一套 A 股高股息组合的**前瞻验证系统**：从 2026-11-05 起，按锁定参数持有 10 只跨行业
> 大市值高股息股，年内分红全额再投、年底取当年分红 30%，并用 RSI 超跌信号做实验性增强；
> 同时并行跑 B1–B6 六条基准，**持续回答"我这套操作到底有没有多赚钱"**。
> 2015-2026 的回测结果只作为只读存档，单独一个 Tab 展示。

---

## 5. 交付验收总检查

执行方完成后，逐条给出证据（命令输出 / 截图 / 测试报告）：

- [ ] `make test` 全绿，含 `test_adjust_price`、`test_dividend_annual`、`test_broker_t1`、`test_benchmark_guard`
- [ ] `make fetch-smoke` 能从主源或任一兜底取到近 60 交易日**前复权**日线，除权日无假跳变
- [ ] **T00 已产出 `docs/CONNECTIVITY.md`**：同花顺确切 endpoint / 前复权公式，以及 AKShare 每个用途经 `ak.search()` 实测的真实函数名与返回字段；`providers.yaml` 的 `resolved_interfaces` 已回填
- [ ] **v3.1 四源约束**：无 `tushare` / `TUSHARE_TOKEN`；`requirements.txt` 含 `akshare==1.20.35`；**`config/providers.yaml` 与 `src/da_data/providers/` 中 baostock、eastmoney 均存在**，且 baostock `max_concurrency: 1`
- [ ] `make ledger-init` 生成首日净值与 6 条基准起点
- [ ] `make serve` 后 8 个页签逐页可达，与 `web/ui_screens/` 截图一致；P7 数据源状态卡为 4 行（主/备/两兜底）
- [ ] 基准页默认显示进度条模式；切换「年化超额」时因样本不足被拦截并给出提示
- [ ] 「历史归档」页所有数字带「只读存档 / 不代表前瞻业绩」标注
- [ ] 生产代码中不存在 `rotation.enabled: true`
- [ ] `docs/legacy/` 与 `research/` 未被修改
