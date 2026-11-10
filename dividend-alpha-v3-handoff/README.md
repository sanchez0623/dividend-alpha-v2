# dividend-alpha

> **A 股高股息组合 · 前瞻验证系统**
> **v3.1 待开发版本**（同花顺主 / AKShare 备 / baostock + 东财兜底；tushare 已移除）｜ 2026-11-06 ｜ 目标执行方：DeepSeek Harness

---

## 开工请先读

**👉 [`docs/00_INDEX.md`](docs/00_INDEX.md)** —— 唯一真值声明、文档阅读顺序、硬约束、验收总检查。

| 文档 | 用途 |
|---|---|
| [`docs/01_PRD.md`](docs/01_PRD.md) | 产品需求：8 个页签、业务规则、已证伪清单 |
| [`docs/02_UI_SPEC.md`](docs/02_UI_SPEC.md) | UI 规格：设计系统、基准页双模式、历史归档页 |
| [`docs/03_ARCHITECTURE.md`](docs/03_ARCHITECTURE.md) | 架构：Forward-only 分层、调度、正确性地基 |
| [`docs/04_DATA_SOURCES.md`](docs/04_DATA_SOURCES.md) | 数据源：**RSI 前复权日线补齐方案**、限流规范 |
| [`docs/05_DEVELOPMENT.md`](docs/05_DEVELOPMENT.md) | 执行手册：目录、DDL、接口签名、**TASK T00–T13**、REST 契约、文件清单、配置映射、前端规格、验收脚本、**§15 harness 启动指令** |
| [`docs/06_HANDOFF.md`](docs/06_HANDOFF.md) | **发包方用**：怎么把任务目标写给 harness（可直接复制的指令 + 凭据二选一 + 收货核对） |

**行为基准**：[`web/prototype.html`](web/prototype.html)（8 页签可交互原型）
**参数真值**：[`config/strategy.yaml`](config/strategy.yaml)

> 交给 harness：从 [`docs/00_INDEX.md`](docs/00_INDEX.md) 读起。
> 你自己下任务：直接复制 [`docs/06_HANDOFF.md`](docs/06_HANDOFF.md) §4。

---

## 这是什么

从 **2026-11-05** 起，按锁定参数持有并动态管理 10 只跨行业大市值高股息股，
年内分红全额再投、年底取当年分红 30%，用 RSI 超跌信号做实验性增强；
同时并行跑 B1–B6 六条基准，持续回答「我这套操作到底有没有多赚钱」。

**运行模式：Forward-only** —— 只向前记录，不复现历史。

---

## 三个必须知道的定性

1. **历史年化 10.61% 不是"已验证"，只是 2015-2026 历史样本里的一个数。**
   所有回测内容集中在第 8 页「历史归档」（只读存档），生产页不把它当现行指标。
2. **样本 <3 年时，基准页不展示年化超额**，改为「运行天数 / 累计净值差 / 做T配对样本」进度条；
   后端 `SampleGuard` 同步拦截，不只是前端隐藏。
3. **增强层标记 `experimental`**：B6 安慰剂分位 75%，组合层不显著。前 2-3 年可能颗粒无收。

---

## 状态

| 层 | 状态 |
|---|---|
| 策略参数与实证验证 | ✅ 冻结（10 轮研究，见 `docs/legacy/reports/`） |
| 产品 / UI / 架构 / 数据源 / 开发文档 | ✅ **本版本交付** |
| 可交互 UI 原型（8 页） | ✅ 交付 |
| 生产代码（`src/`、`web/src/`） | ⬜ **待开发**，按 `05_DEVELOPMENT.md` TASK 执行 |
| 数据管道连通性 | ⏳ 同花顺需加白名单实测；AKShare 1.20.35 接口待 `ak.search()` 核实；baostock / 东财兜底保留 |

> `docs/legacy/` 与 `research/` 是历史研究归档，**只读、不进生产镜像、禁止修改**。

---

## 最快开工路径

```
docs/00_INDEX.md → 01_PRD.md → 05_DEVELOPMENT.md
T00 连通性实测 → T01 账本与 DDL（最先）→ T02 前复权日线 → ... → T13
```
