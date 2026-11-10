# 06 · 如何把任务交给 harness（目标模式吩咐）

> 面向：**你**（发包方）。`00~05` 是给 harness 读的施工依据，本文是教你怎么下目标。
> 版本：**v3.1**（数据源收敛为同花顺 + AKShare）｜ 2026-11-06

---

## 0. 先澄清一个词

我不清楚 DeepSeek Harness 产品里是否有名为「目标模式」的具体开关，**不臆测它的界面**。
下面说的「目标模式」指的是：**把任务目标写成一份可验收的契约**，而不是一句愿望。
无论它是 goal-driven、plan/act 还是普通 agent 模式，把 §4 那段贴进「任务目标 / Goal」即可。

---

## 1. 最大的坑：别把目标写成「做一个系统」

| ❌ 别这么写 | 为什么 | ✅ 改成 |
|---|---|---|
| 「做一个可以真实跟踪的策略系统」 | "真实"会被理解成接你的资金账户；A股没有个人交易 API，它会猜 | 「交付**模拟盘（PAPER）**前瞻记账系统，从首次 `ledger-init` 日起逐交易日推进」 |
| 「实现这套策略，年化 15%」 | 15% 已被实证下修，且回测结论不许当承诺 | 「按 `config/strategy.yaml` 执行；**不承诺收益**、不证明有效性」 |
| 「把界面做成实时数据」 | 它会去改 `web/data.json` 或伪造数字 | 「生产 7 页读 API；P8 读只读归档；样本不足处渲染占位与进度条」 |
| 「顺便优化一下策略」 | 会触发已证伪清单（轮动/条件换仓/MACD） | 「**禁止改参数、禁止新增策略逻辑**，参数唯一真值是 `strategy.yaml`」 |
| 「跑通历史 2015 数据」 | 与 Forward-only 冲突 | 「不回填历史；`research/` 只读」 |

**核心原则：目标 = 可运行的工程 + 可复现的证据；非目标要写得比目标还清楚。**

---

## 2. 目标契约的四要素（缺一不可）

| 要素 | 本项目的取值 |
|---|---|
| **① 交付定义 (DoD)** | `src/` + `web/src/` + `infra/` + `tests/` 可运行；`make test` 全绿；8 页截图；首日 `daily_nav` 7 行 |
| **② 运行模式** | `FORWARD_ONLY` / `PAPER` / 不下单 / 起始日见 §3.2 |
| **③ 非目标 (Non-goals)** ★ | 不接实盘资金、不自动下单、不证明策略有效、不展示 <3 年年化超额、不补历史、不改归档 |
| **④ 验收权** | 以 `05_DEVELOPMENT.md §14` 的脚本输出为准，**harness 自证，不采信自述** |

> 第 ③ 项最容易被省略。agent 天然会"补全"它以为你想要的——不写非目标，它就会替你接实盘、算年化、开轮动。

---

## 3. 动工前你必须先拍板的两件事

### 3.1 T00 数据源凭据（二选一，直接决定它会不会卡住）

> **v3.1 四源链**：同花顺 fuyao（主）→ AKShare 1.20.35（备）→ **baostock（串行兜底）** → **东财 push2his（最后兜底）**。
> **只移除 `tushare` / `TUSHARE_TOKEN`**（你明确说「不用」）。
> **baostock 与东财保留**——你明确说过「不能清除」，上一版误删了，这版已恢复。

| | 选项 A：同花顺已通 | 选项 B：同花顺未通，授权逐级降级 |
|---|---|---|
| 你提供 | `THS_API_KEY` + 生产出口 IP 已加白名单 | 只说「逐级降级」（**无需任何 key**，baostock/东财也无 key） |
| harness 行为 | T00 实测 `market-dumps` / `corporate-actions` / `prices`，拿官方复权因子；用 `ak.search()` 定位 akshare 接口；**并实测 baostock 串行可用性与东财 502 是否复现** | 先走 AKShare 1.20.35；不足时退 baostock（串行）→ 东财；前复权按 `04 §4.3.1` 二选一 |
| 必须写进目标 | 「T00 **四源逐条**实测并产出 `CONNECTIVITY.md`，回填 `resolved_interfaces`」 | 「**降级也要产出四源逐条的 `CONNECTIVITY.md`**；不得因此阻塞 T01」 |
| 风险 | 白名单没加会返回 `policy_default_denied` | 若 akshare 只给不复权价，路径 B 自算缺送转/配股数据，需标红；**baostock 串行会拖慢盘后任务，东财可能 502** |

> ⚠️ 三处文档未定项，无论选 A/B 都要靠**上报协议**（§4 指令）兜住：
> ① 同花顺 `market-dumps` 确切路径带 `...`、厂商前复权公式「需核对文档」、MCP 参数名未给；
> ② **AKShare 官方入口页只说了「用 `ak.search()` 检索」，没有任何具体接口名/参数**——
>    这份文档我读过了，它确实没给函数签名，所以**禁止让 harness 猜一个 `ak.*` 写死**；
> ③ 交割单导入接口未定义（与本次数据源调整无关，但仍未定）。

### 3.2 起始日口径（很容易差一天）

配置里锁的是 `meta.start_date: 2026-11-05`，但你实际发包可能更晚。**二选一写清楚**：

- **建议**：「以首次 `make ledger-init` 执行日记为 **D+1**，不回填中间空白日；`meta.start_date` 保持原值仅作档案」
- 或：「由我把 `meta.start_date` 改成实际开工日后再动工」——**别让 harness 自己改配置**

---

## 4. 可直接复制的完整指令

> 把下面整段贴进 harness 的任务目标。**主要只需删掉【A】或【B】其中一段**；其余已按 v3.1 写死。

```text
【任务目标】按 docs/00_INDEX.md 的唯一真值声明，实现 dividend-alpha v3.1 的可运行工程，
并自证验收通过。这是编码任务，不是研究任务，也不是设计任务。

【数据源 — v3.1 硬约束】四源链：同花顺 fuyao（主）→ AKShare 1.20.35（备）
→ baostock（串行兜底，并发必须=1）→ 东财 push2his（最后兜底，已知 502）。
【只】禁止 tushare / TUSHARE_TOKEN（用户明确不用）。
baostock 与 eastmoney 必须在 providers.yaml 和 src/da_data/providers/ 中存在，缺失即验收失败。
requirements.txt 必须锁 akshare==1.20.35。

【唯一入口】先完整读 docs/00_INDEX.md，按其中顺序读 01_PRD / 02_UI_SPEC /
03_ARCHITECTURE / 04_DATA_SOURCES / 05_DEVELOPMENT。执行顺序与全部契约以
05_DEVELOPMENT.md 的 TASK T00→T13、§10 REST 契约、§11 文件清单、§12 配置映射、
§13 前端规格为准。以 §15 启动指令作为开工约束。

【完成定义 DoD — 全部满足才算完成】
1. src/、web/src/、infra/、tests/ 存在且可运行；make init / fetch-smoke /
   ledger-init / worker-once / serve / test 全部执行成功。
2. make test 全绿（8 个测试文件），逐条给出 §14 验收脚本的实际输出。
3. 首次 ledger-init 后 daily_nav 有 7 行（S* + B1~B6），running_days 从 1 起算。
4. 8 个页签逐页可达，截图与 web/ui_screens/ 基准一致（含 benchmarks_locked 拦截态）。
5. 用 §14 的 curl 证明：mode=annual 返回 400 SAMPLE_INSUFFICIENT，且 progress / 400
   两种响应体都不含 annual_excess_* 字段。
6. 产出 docs/CONNECTIVITY.md（见下方数据源条款）。

【运行模式 — 不可协商】
FORWARD_ONLY + PAPER 模拟盘。不下单、不接券商实盘、不导入真实资金账户。
不复现 2015-2026 历史；生产系统只从开工日起向前记录。

【非目标 — 明确禁止，比目标更重要】
× 不证明策略有效、不承诺任何年化收益、不把 10.61% 写成预期收益
× 样本 <1095 天或 <300 笔时，不计算、不返回、不渲染年化超额
× 不回填历史、不改 research/ 与 docs/legacy/、不改 web/data.json /
  web/prototype.html / web/ui_design/ / web/ui_screens/
× 不改 config/strategy.yaml 任何参数、不新增策略逻辑、不开启 rotation
  （启动自检必须断言 rotation.enabled is False）
× 不实现 01_PRD.md §6 的 16 条已证伪清单
× 除 T00 的 CONNECTIVITY.md 外，不写新的设计文档

【数据源条款 — 二选一，删掉另一条】
【A 同花顺已通】我已提供 THS_API_KEY 且生产出口 IP 已加白名单。T00 须实测
  market-dumps / corporate-actions / prices，把确切 endpoint、参数、返回字段样例、
  厂商前复权公式定义写入 docs/CONNECTIVITY.md；同时用 ak.search() 定位并实测
  AKShare 1.20.35 的交易日历/股票状态/60min/分红/市值/指数接口，回填
  config/providers.yaml 的 resolved_interfaces 与 calendar.resolved_func。
【B 同花顺未通，逐级降级】不要等待白名单。按 akshare → baostock（串行）→ 东财
  逐级取数，前复权按 04_DATA_SOURCES.md §4.3.1 二选一；仍须用 ak.search() 实测
  每个 akshare 接口，并【实测 baostock 与东财 push2his】（含 502 是否复现），
  四源逐条写入 docs/CONNECTIVITY.md，降级项标红。
  不得因数据源未通而阻塞 T01 账本先跑。

【起始日】以首次 make ledger-init 执行日为 D+1，不回填中间空白日；
meta.start_date 保持原值仅作档案，不得自行修改。

【遇到文档未定项的处理协议 — 关键】
① 同花顺确切 endpoint 带省略号、厂商前复权公式口径未定、MCP 工具参数名未给；
② AKShare 官方入口页（akshare.akfamily.xyz/data/stock/stock.html）只说明
   「可用页面搜索或 ak.search() 检索」，【没有给出任何具体接口名/参数/返回字段】；
③ 交割单导入接口未定义。
遇到这些：**停下来，列出「缺什么 / 有哪几个候选 / 建议选哪个」上报，
不要猜、不要写死一个 ak.* 函数名、不要自己补设计文档**。等确认后继续；其余部分可并行推进。

【验收权】以 05_DEVELOPMENT.md §14 的命令输出与 git diff 为准。
交付时逐条给 00_INDEX.md §5 的证据；不采信「已完成」这类自述。
最后报告必须包含：改动文件清单、make test 输出、§14 脚本输出、
CONNECTIVITY.md 内容、research/ 与归档目录 git diff 为空的证明。
```

---

## 5. 建议分两阶段吩咐（防长任务漂移）

一次性丢 T00–T13，后段最容易开始"自由发挥"。建议：

**阶段一（先跑通地基）**
> 只做 T00（或降级）+ **T01 账本与 DDL** + T02 前复权日线 + T03 因子 + 对应测试。
> 交付证据：`daily_nav` 首日 7 行、`test_adjust_price` 通过、CONNECTIVITY.md。
> 完成后**停下来等我确认**，不要继续写信号/前端。

**阶段二（确认后再放）**
> 基于阶段一已落库的数据，做 T04–T13，按 §4 完整 DoD 验收。

账本先行是硬要求（`03_ARCHITECTURE.md §3.3`）：Forward-only 下晚一天开跑就少一天样本，
阶段一哪怕行情源还没通，也应该先把 D+1 记上。

---

## 6. 你这边怎么收货（不用读代码）

| 看什么 | 期望 |
|---|---|
| `make test` 输出 | 8 个文件全绿，无 skip |
| `docs/CONNECTIVITY.md` | 存在；**四源逐条实测记录**（AKShare 接口名 + 参数 + 返回字段；baostock 串行状态；东财 502 复现情况）；降级项标红 |
| `config/providers.yaml` | `resolved_interfaces` 与 `calendar.resolved_func` **已回填**；`fallback` 含 `baostock, eastmoney`；baostock `max_concurrency: 1` |
| `daily_nav` 导出 | 首日 7 行，`running_days=1` |
| `curl .../benchmarks?mode=annual` | **400** + `SAMPLE_INSUFFICIENT`，无 `annual_excess_` |
| 9 张新截图 | 与 `web/ui_screens/` 对照；`benchmarks_locked` 必须在；**P7 数据源卡为 4 行** |
| `git diff` | `research/`、`docs/legacy/`、`web/data.json`、`web/prototype.html` **为空** |
| 全库搜索 | `rotation.enabled: true` 无命中；**`tushare` / `TUSHARE_TOKEN` 无命中**；**`baostock` / `eastmoney` 必须有命中** |
| `requirements.txt` | 含 `akshare==1.20.35`，无 tushare 依赖 |
| topbar | 显示 `Forward-only`、`D+1`，不是 `2015-01 ~ 2026-10` |

**如果它交回来的是一份新设计文档、或者把界面数字填成 10.61% / 年化超额，那就是目标没立住，退回重跑，别逐行 review 代码。**

---

## 7. 一句话记住

> **吩咐它「交付可运行、可自证、有边界的工程」，而不是「交付一个能赚钱的真实系统」。**
> 前者是它能做到的；后者里的"真实"和"赚钱"，分别卡在 A 股无个人交易 API、以及 Forward-only 才刚 D+1。
