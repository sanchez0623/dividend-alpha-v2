# 再平衡缺失诊断（代码审计 · 只读）

> 审计对象：`D:\Sanchez\AI\GithubProjects\dividend-alpha-v2`（v3.0 生产代码 + `data/dividend_alpha.db`）
> 审计方式：只读静态审计 + SQLite `mode=ro` 只读复算；**未修改任何 src/、config/、tests/、data/*.db**
> 结论服务 SPEC §1 第 1 问：**生产代码里到底有没有再平衡？缺的是哪一类（补仓 / 修剪 / 换股）？**

**数据快照时点**：2026-11-08 15:39（Asia/Shanghai）。本报告全部库内数值均取自该时点快照。
审计期间（15:41:16 起）检测到**另一进程**对 `data/dividend_alpha.db` 执行了 `scripts/reset_paper.py` 式的清表（account/position/orders/daily_nav 等被清空；非本审计所为，本审计全程只读）。若需复现，请先按 `scripts/server_replay.sh` 重放 2026-11-05~11-07 再对照本文。快照数值与任务书给出的基准（cash=312,140.3984、10 只持仓成本≈268 万、总权益≈300 万）完全一致。

---

## 0. 结论摘要（TL;DR）

**有"再平衡"函数，没有"再平衡"行为。** 生产里唯一被调用的调仓入口 `run_rebalance()`，其语义被两处置死为"**只补仓、不修剪**"：

1. `PortfolioManager.apply_targets()` 只处理 `delta > 0` 的买单（`src/da_portfolio/manager.py:51-53`），`delta < 0`（超配）直接 `continue`，**不存在任何 trim/卖出分支**；全项目 `src/` 内**没有任何一处调用 `T1Broker.sell()`**（`broker.py:120` 的定义只有 `tests/test_broker_t1.py:32,43` 调用）。
2. 目标金额用 `investable = cash * core_ratio`（`manager.py:36`），而**不是总权益**。初始建仓花掉 90% 现金后，`cash≈312,140`，此后每月"每只目标金额"只有 **28,092.64 元**，而现持仓每只 **264,384~275,400 元** → 10 只 `delta` 全为负、合计 **−2,403,507.64 元**，**月度再平衡从第二个月起 100% 空转、0 成交**。

所以缺的是 **"修剪（trim / 回归目标权重）"这一类**，而不是"补仓"（补仓代码存在，但预算口径错误导致建仓后失效），也不是"换股轮动"（`rotation.enabled=false` 已被显式禁止，`src/da_portfolio/rebalance.py:22-23` 有运行时断言）。

附带独立的三个"执行层断裂"：

- **卖出规则从未执行**：`sell_rules` 只把 SELL 写进 `signal_log`，`signal_log` 全项目**没有交易执行消费者**（只有只读展示 API `routers_app.py:144-159` 与测试脚本会读取）；`dividend_interrupted` 规则还因参数从不传入而**永不触发**。
- **分红再投从未接入**：`DividendReinvest.on_dividend/reinvest` 没有任何生产调用方（唯一包装 `manager.reinvest_dividends` 也是死代码）；`corp_action` 有 1,807 行除权事件、**`dividend_event` 表 0 行**，分红现金 0 入账、0 再投（库内 TTM 口径年化约 **16.6 万元**被漏掉，见 §2c）。
- **年度取现不在闭环内**：`AnnualWithdraw` 只在 APScheduler 12 月任务里跑（`scheduler.py:32-40`），`daily_close.py` 不调用；且其基数来自恒空的 `dividend_event` → `want=0`，**30% 年度取现实际也永不发生**（`withdraw_log` 0 行）。

---

## 1. 每日闭环 `daily_close.py` 实际执行链路

入口 `run_daily_close()`（`src/da_worker/daily_close.py:23-73`），实际顺序与动作如下：

| # | 动作 | 执行？ | 证据 |
|---|---|---|---|
| 1 | 读持仓、判定 `rebalance_due` | ✅ | `daily_close.py:27-28` |
| 2 | 调仓日重建全市场漏斗候选（非调仓日读缓存） | ✅（仅当 rebalance=True） | `daily_close.py:32-38` → `da_data/universe.py:40-151` |
| 3 | `ingest_daily` 行情/除权入库 | ✅ | `daily_close.py:43` → `da_data/ingest.py:16-59` |
| 4 | `sync_market_cap`（QQ 快照补市值） | ✅ | `daily_close.py:44` |
| 5 | `sync_symbol_meta`（东财名称/行业/银行子类） | ✅ | `daily_close.py:45` → `da_data/factors.py:20-69` |
| 6 | `compute_factors` 因子落库 | ✅ | `daily_close.py:46` |
| 7 | `generate_and_store_signals` 信号落 `signal_log` | ✅（**只落日志**） | `daily_close.py:47` → `da_signal/pipeline.py:53-72` |
| 8 | `run_rebalance`：选核心仓 + `apply_targets` **补仓** | ✅（仅调仓日；只买不卖） | `daily_close.py:51-52` → `rebalance.py:49-91` → `manager.py:30-63` |
| 9 | `settle_eod` 解锁 | ⚠️ 只在 `run_rebalance` 内部调用 | `rebalance.py:89`（非调仓日不解锁） |
| 10 | 现金 + 持仓（adj_close）估值 → S* 净值 | ✅ | `daily_close.py:54-61,95-108` |
| 11 | B1–B6 基准推进（真实指数） | ✅ | `daily_close.py:63-66,126-140` |
| 12 | `write_daily_nav` 写 7 行净值 | ✅ | `daily_close.py:68` → `ledger.py:58-84` |
| 13 | **卖出/修剪执行（`broker.sell`）** | ❌ 无调用 | 全项目仅 `tests/test_broker_t1.py:32,43` 调用 `.sell(` |
| 14 | **分红到账/红利税/分红再投** | ❌ 未 import、未调用 | `daily_close.py` 无 `dividend.py`/`DividendReinvest` 引用 |
| 15 | **年度取现（12 月最后交易日）** | ❌ 不在闭环内 | 只在 `scheduler.py:32-40` → `annual_withdraw.py:13-22` |
| 16 | **消费 `signal_log`（SELL/BUY/TRIM）→ 订单** | ❌ 全项目无交易消费方 | `grep SignalLog` 仅写（`pipeline.py:55-71`）与只读展示（`routers_app.py:147`） |

运行期 DB 证据（2026-11-08 15:39 快照）：

| 表 | 快照值 | 含义 |
|---|---|---|
| `account` | 1 行，cash=**312,140.398439**，total_equity=3,000,000（**陈旧，从未更新**） | 建仓后现金缓冲 |
| `position` | 10 行，buy_date 全为 2026-11-05，shares_available 全为 0（仅展示字段） | 10 只等权核心仓 |
| `orders` | **10 行，全部为 2026-11-05 的 BUY** | 建仓后 **3 天内 0 笔交易**（11-06/11-07 无任何订单） |
| `fills` / `lot` | 10 / 10 | 同上 |
| `dividend_event` | **0 行** | 分红现金从未入账 |
| `withdraw_log` | **0 行** | 年度取现从未发生 |
| `signal_log` | **0 行** | 3 日用卖出规则未产生任何 SELL 信号（即便产生也无人执行） |
| `daily_nav` | 21 行 = 3 日 × 7 条（S* + B1..B6） | 闭环每天在跑 |
| `factor_daily` | 156 行 = 3 日 × 52 只 | 因子在算 |
| `execution_log` | 4 行（day_one / daily_nav ×3，其中 11-05 被重放覆盖 1 次） | 仅账本幂等日志，**无 rebalance 日志** |

`daily_nav` 的 S*：2026-11-05 = **2,998,350.3984**；11-06 = **2,996,574.3984**；11-07 = **2,996,574.3984**（与 11-06 完全相同 —— 因为 11-07 是周六、库里没有 11-07 的任何价格行，估值自动退化为 11-06 收盘价；`price_daily` 最大日期 = 2026-11-06）。所以"2026-11-07 收盘权重" = **11-06 收盘权重**（下文同口径）。

**旁证（审计期间观察到的另一次 11-05→11-06 重放，篮子不同）**：该次重放 11-05 建仓 10 笔 BUY（现金 310,310.28），11-06 闭环写完 7 行 NAV（S\* = 3,007,824.28，**订单表仍只有 11-05 的 10 笔，0 新增**）——与"建仓后第二次闭环不再交易"的代码结论一致（11-06 `rebalance_due=False`：同月已有 BUY）。

---

## 2. 逐项核查

### (a) `rebalance_due` / `run_rebalance` / `apply_targets`：只补仓、不修剪；目标按"现金"而非"总权益" → 建仓后空转

**代码事实**

1. `monthly_rebalance()` 的注释就写明了语义：`# 月频补仓。禁止换股；换股只能由卖出规则触发清仓后经选股补入。`（`src/da_portfolio/rebalance.py:18-25`）。
2. `PortfolioManager.apply_targets()`（`src/da_portfolio/manager.py:30-63`）：
   - `core_ratio = 0.90`（L34），`investable = float(acc.cash) * core_ratio`（**L35-36**）——预算基数 = **现金**；
   - `target_mv = investable * t.weight`（**L50**）；
   - `delta = target_mv - cur_mv`（L51）；**`if delta <= 0: continue`（L52-53）**——超配/持平一律跳过，**没有卖出分支**；
   - 注释 `# 先卖出不在目标内的持仓（由卖出规则触发的清仓由 caller 处理），此处仅补仓。`（**L39**）——"由 caller 处理"的 caller 并不存在（见 (b)）；
   - `PortfolioManager.total_equity()`（L65-74）存在但**全项目无调用**（grep `total_equity` 仅定义与 API 展示），`account.total_equity` 建仓后恒为 3,000,000。
3. 目标权重来自 `build_targets()`：等权 `w = 1/len(sel)`（`src/da_signal/core_selection.py:93-106`，L96）。`run_rebalance()` 把 `(symbol, weight)` 传给 `apply_targets`（`rebalance.py:67-72,87`）。

**用库内真实数值复算（快照 2026-11-08，10 只持仓 + cash=312,140.3984）**

建仓日（2026-11-05 收盘，adj_close=raw_close）：

- 建仓预算 `investable = 3,000,000 × 0.9 = 2,700,000` → 每只目标 **270,000**；因 100 股整手取整，实际买入 266,940~269,966 元/只，合计买入额 **2,686,210**，交易成本 **≈1,649.6**（6.1bp），现金余额 **312,140.3984**，总权益 **2,998,350.3984**（NAV 一致）。

**月度再平衡（"今天"用 11-06 收盘价复算，即 `run_rebalance` 若此刻执行）**：

| 项 | 数值 |
|---|---|
| `investable = cash × 0.9` | **280,926.36** |
| 每只目标 `investable × 1/10` | **28,092.64** |
| 目标总额 / 持仓市值 | 280,926.36 / 2,684,434 = **10.47%** |
| 目标总额 / 总权益 | 280,926.36 / 2,996,574.40 = **9.375%** |
| 每只当前市值 | 264,384 ~ 275,400（≈0.94% 权益目标的 **9.5 倍**） |
| 每只 delta = 28,092.64 − 当前市值 | **−237,217 ~ −247,307**（10/10 全为负） |
| Σdelta | **−2,403,507.64** |

逐只明细（11-06 adj_close；`delta<=0` 全部被 L52-53 跳过）：

```
sz000157 cur=265,310.00 target=28,092.64 delta=-237,217.36
sz000423 cur=269,505.00 target=28,092.64 delta=-241,412.36
sz000568 cur=267,861.00 target=28,092.64 delta=-239,768.36
sz000651 cur=269,238.00 target=28,092.64 delta=-241,145.36
sz000987 cur=268,410.00 target=28,092.64 delta=-240,317.36
sz002032 cur=272,160.00 target=28,092.64 delta=-244,067.36
sz002236 cur=266,082.00 target=28,092.64 delta=-237,989.36
sz002532 cur=266,084.00 target=28,092.64 delta=-237,991.36
sz002555 cur=264,384.00 target=28,092.64 delta=-236,291.36
sz300979 cur=275,400.00 target=28,092.64 delta=-247,307.36
Σdelta = -2,403,507.64 → 实际成交 0 笔
```

**量化结论**

- 初始建仓后 `cash` 只剩 10.4% 权益，而"再平衡预算 = 现金 × 0.9"只在**建仓那一刻**等于 90% 权益；之后预算缩到 **9.375% 权益**（每只 0.94% 权益）。**只要持仓不动、现金不增长，`delta` 恒为负，月度再平衡永久空转。**
- 每个月的最大可投放资金 = **280,926 元 = 9.4% 权益**；**89.6% 权益（2,684,434 元）没有任何修剪机制**。
- 若改为"按总权益"目标（SPEC §3.3 口径 `target = total_equity/N`，即 `0.9 × 2,996,574.40 / 10 = 269,691.70`），同一天就会产生：**卖 2 笔 8,176.61 元（sz002032、sz300979）＋ 买 8 笔 20,659.57 元，合计换手 28,836.18 元 = 0.96% 权益**（仅 1 个交易日的漂移；成本约 20~30 元）。这就是"修剪路径存在与否"的直接差别。
- 反事实：若下月漏斗换入一只**新**标的，`cur_mv=0` → 会买入约 **28,092 元（0.94% 权益）** 的新仓，而 10 只旧仓 267 万**一股不卖** → 组合变成 11 只（甚至更多），行业约束在持仓端被绕过；现金被小仓位慢慢漏掉，且"目标 = 剩余现金 × 0.9"会让下月预算进一步缩小。

### (b) 卖出规则只在 pipeline 里落 `signal_log`，全项目从不调用 `T1Broker.sell`

**代码事实**

- 规则判定：`evaluate_sell()`（`src/da_signal/sell_rules.py:23-40`）实现 4 条硬规则（股息率<1.5% / 分红中断 / 持有≥60 月 / 浮盈≥50%）。
- 插件只产出信号：`SellRulesPlugin.generate()` 返回 `Signal(kind="SELL")`（`sell_rules.py:57-72`，L67-71）。
- 信号只写库：`generate_and_store_signals()` 把插件输出写进 `signal_log`（`src/da_signal/pipeline.py:46-72`），**没有后续消费**。
- 全项目 `signal_log` 的读取方只有只读展示 API（`src/da_api/routers_app.py:144-159`）与测试脚本（`scripts/test_signal_pipeline.py:45`），**没有任何"交易执行"消费者**：没有代码按 SELL 信号调用 `broker.sell`。
- `T1Broker.sell()` 定义在 `src/da_core/broker.py:120-155`；`grep ".sell("` 全库仅 **`tests/test_broker_t1.py:32,43`** 两处调用，`src/` 内 **0 次**。
- 另外两条"静默失效"：
  - `evaluate_sell(..., dividend_interrupted=False)` 的入参在唯一调用点 `sell_rules.py:65` **没有传入**，而 `check_dividend_interrupted()`（`sell_rules.py:43-54`）全项目**无调用方** → "分红中断"规则**永远不可能触发**；
  - `Signal.kind` 支持 `"TRIM"`（`src/da_signal/base.py:16`、`models.py:198` 注释），但全项目没有任何地方产生 `TRIM`。

**结论**：卖出规则链 = **规则→信号→日志→（无人读）**，是"纯展示/观测"功能，**没有执行闭环**。前端也只是静态文案：`web/src/pages/Positions.tsx:47-49`（`Card title="卖出规则" tag="锁定"`）。

**量化**：快照 `signal_log` 0 行 → 3 个交易日连 SELL 信号都未产生（可解释：建仓 1 天、持有月数远小于 60、浮盈未达 50%、股息率未跌破 1.5%）；但即使产生也不会有任何成交，因为不存在 `broker.sell` 调用点。

### (c) 分红再投：`DividendReinvest.on_dividend/reinvest` 未被 daily_close/scheduler 调用；`dividend_event` 表为空

**代码事实**

- `DividendReinvest.on_dividend()`（`src/da_portfolio/dividend.py:28-35`）→ 调 `T1Broker.on_dividend()`（`src/da_core/broker.py:158-188`，按 FIFO 批次扣红利税、净额入现金、写 `DividendEvent`）。
- `DividendReinvest.reinvest()`（`dividend.py:37-66`）按持仓市值权重把"可投现金"再投（`broker.buy`）。
- **调用方**：仅 `PortfolioManager.reinvest_dividends()`（`manager.py:76-78`）包装 `reinvest()`；而 `reinvest_dividends` **全项目无调用**（grep 仅定义 1 处）。`DividendReinvest(` 实例化也只在 `manager.py:77`。
- `daily_close.py` 未 import `dividend.py`；`scheduler.py` 未调度分红任务。
- 数据面：`ingest_daily()` **确实**每天把除权事件写进 `corp_action`（`src/da_data/ingest.py:30-32`），但**没有任何代码把已除权的 `corp_action` 转成现金入账**（`DividendEvent(` 全项目仅 `broker.py:177` 与测试使用）→ 快照 `corp_action` 1,807 行、`dividend_event` **0 行**。

**结论**：分红再投是**死代码**；分红现金流 0 入账、0 再投。

**量化（库内 TTM 口径，需注意本库为演示/半合成数据，见 §5 局限）**：

- 首个必然漏掉的分红：`sz002555` 除权日 **2026-11-09**、dps=0.1，持仓 16,200 股 → 税前 **1,620 元**，无任何代码会处理。
- 按"近 12 个月 dps×当前持仓"估算年化现金分红（`corp_action` 2025-11-06~2026-11-07 的 dps 之和）：

| 标的 | TTM dps/股 | 持仓 | 年化分红(元) |
|---|---|---|---|
| sz000157 | 0.40 | 43,000 | 17,200 |
| sz000423 | 2.7803 | 5,300 | 14,735.6 |
| sz000568 | 5.7751 | 3,300 | 19,057.9 |
| sz000651 | 4.092 | 6,900 | 28,234.8 |
| sz000987 | 0.40 | 38,900 | 15,560 |
| sz002032 | 2.63 | 6,000 | 15,780 |
| sz002236 | 0.861 | 18,300 | 15,756.3 |
| sz002532 | 0.65 | 22,100 | 14,365 |
| sz002555 | 0.71 | 16,200 | 11,502 |
| sz300979 | 2.10 | 6,800 | 14,280 |
| **合计** | | | **166,471.60** |

  = 持仓市值的 **6.20%**、总权益的 **5.56%**。按配置本应：年度取现 30% ≈ **49,941 元**（`dividend_withdraw.ratio=0.30`，`strategy.yaml:52`）＋ 分红再投 **100%**（`dividend_reinvest.ratio=1.00`，`strategy.yaml:42`）——**两者都没发生**。
  - 另注：配置本身存在**口径重叠**——"收到分红 100% 再投"与"按税前分红 30% 年度取现"合计相当于 130% 分红，现金流上必须澄清先后顺序（现实做法通常是先取现 30% 再投剩余 70%）；`cash_only` 短差策略会让超出部分从现金缓冲扣（`dividend.py:146-150`）。这是"让分红链路真正跑起来"之前必须先定好的设计点。
- 另注意：若把 `reinvest()` 直接接上，其现金约束是 `cash_avail = cash - min_cash_ratio(2%)×(mv+cash)`（`dividend.py:48`），会把现金从当前 10.4% 权益一次性打到约 2%（约 60,000 元）——这与 `core_ratio=0.9`（10% 现金缓冲）**冲突**；接入时不能简单照搬。

### (d) 年度取现：只在 scheduler 12 月执行，daily_close 不调用

- `AnnualWithdraw.run_year_end()`（`src/da_portfolio/dividend.py:121-167`）：`want = ytd_gross(year) × ratio(0.30)`（L134,143），`cash_only` 时 `got = min(want, cash)`（L148-149），写 `WithdrawLog`。
- `ytd_gross()` 来自 `dividend_event` 汇总（`dividend.py:97-119`）。
- 唯一生产调用链：`scheduler.py:32-40`（`cron ... month="12" hour=16 minute=55`）→ `annual_withdraw.py:13-22`（`run_annual_withdraw`，内部再判 `date.today() < 12月最后交易日 → NOT_DUE`）。
- `daily_close.py` 无 `AnnualWithdraw` import；`scripts/worker_once.py` 只调 `run_daily_close` → **回放/手动闭环路径上年度取现不会执行**。
- 数据面：`dividend_event=0` → `ytd_gross=0` → `want=0` → 即便 12 月任务触发也**取不出钱**；`withdraw_log` 快照 **0 行**。

**结论**：年度取现"有调度、无现金流"；在闭环（`run_daily_close`）里完全缺位。附带一致性风险：`skip_incomplete_year` 用 `date.today()` 而非 `asof`（`dividend.py:137-141`），**历史回放**时会按"当前年份"判断，回填旧年份会直接执行（幂等键兜底，但语义与 asof 脱节）。

### (e) `rebalance_due` 读 `cfg.portfolio.get("rebalance")`，配置却在 `selection.rebalance`（键位错位）

**代码事实**

- 配置真值：`config/strategy.yaml:26-28`：
  ```
  selection:
    rank_by: div_yield_ttm
    rebalance: monthly              # 仅补仓到 N，不主动换股
  ```
- 读取点：`src/da_portfolio/rebalance.py:37` `if str(cfg.portfolio.get("rebalance", "monthly")).lower() != "monthly": return False`。
- `Strategy.portfolio` 只返回 `raw["portfolio"]`（`src/da_core/config.py:37-38`），`portfolio` 段（`strategy.yaml:17-24`）只有 `target_positions/weighting/core_ratio/industry_constraint`，**没有 `rebalance` 键** → 表达式**永远取默认值 `"monthly"`**；`cfg.selection`（`config.py:41-42`）存在但从未被读取。

**后果**

1. **配置"唯一真值"失效**：修改 `selection.rebalance`（例如改 `quarterly`/`none`）**不会改变任何行为**——这是与 `config.py:1-5` 声明的"唯一参数真值、代码禁止硬编码"直接冲突的硬伤。
2. 当前 `selection.rebalance=monthly` 与默认值恰好相同，所以**行为上暂时被"掩盖"**，属潜伏 bug：一旦有人按文档改配置就会踩坑（README/配置注释会让人以为生效）。
3. 真正导致**触发频次异常**的不是这个键，而是紧随其后的 `has_buy` 判定（`rebalance.py:40-46`）：**只要本月没有 BUY 订单就返回 True**。而 (a) 已证明建仓后 `apply_targets` 不再产生任何 BUY → 从 **2026-12-01 起每个交易日** `rebalance_due=True`（"月度"退化成"每日尝试"）。
   - 每日 True 的连锁成本：`daily_close.py:32-33` 会每天执行 `build_candidates()` 全市场漏斗（`universe.py:40-151`：S1 全市场快照 + S2 逐票串行抓除权、`ths.max_concurrency=2 / min_interval_sec=0.5`，`config/strategy.yaml` data 段）——外部 API 压力与耗时都显著放大，而 `run_rebalance` 依然 0 成交（只有 warning 日志 `rebalance.py:73-75`）。
   - 快照可推：11-06/11-07 `rebalance_due=False`（11-05 有 BUY，落在同月），11-05 因 `n_pos==0` 走初始建仓分支（`rebalance.py:33-36`）。

### (f) 有没有别处（API/scripts/web）会触发卖出/再平衡/分红再投？

**结论：没有。唯一交易入口就是 `daily_close` 的 `run_rebalance`。**

- **API 全部只读 GET**：`src/da_api/routers_app.py:48/103/144/162/179/197`（overview/positions/signals/attribution/withdraw/config）、`routers_archive.py:18`、`routers_benchmarks.py:27`；`main.py:39/45/112`。全项目无 `@router.post/put/delete`。`/withdraw` 只是**读** `ytd_gross` 与计划取现金额（`routers_app.py:179-194`），不执行 `run_year_end`。
- **scripts**：`worker_once.py:14,27` → `run_daily_close`（同一闭环）；`server_replay.sh` 逐日调 `worker_once.py 2026-11-05/06/07`；`reset_paper.py` 只清表；`test_signal_pipeline.py` 只测信号落库。**没有脚本直接调用 `broker.sell` / `reinvest`，也没有绕过 `daily_close` 的调仓入口。**
- **web**：`web/src/api.ts:79` 只有 `fetch(GET)`；页面全是展示（`Positions.tsx:47-49` 卖出规则是静态文案，`Withdraw.tsx` 只展示计划）。无下单按钮/接口。
- **盘中**：`src/da_worker/intraday.py:13-19` 明确"仅提示，不下单"。
- **测试**：`tests/` 里没有 `apply_targets`/`rebalance_due`/`run_rebalance`/`DividendReinvest` 的集成用例（只有 `test_broker_t1.py` 直接测 broker.sell、`test_dividend_annual.py` 用注入的 `DividendEvent` 测 `AnnualWithdraw`）→ 上述断链**没有任何测试守护**。

---

## 3. "缺什么"清单（含当前权重漂移量化）

### 3.1 缺失清单

| # | 缺失项 | 代码证据 | 影响 | 量级（库内快照） |
|---|---|---|---|---|
| 1 | **修剪（trim）**：超配不卖、权重不回目标 | `manager.py:51-53`（`delta<=0: continue`）、`manager.py:39`；全项目无 sell 分支 | 权重单向漂移，永不回归；行业约束在持仓端失效；组合可膨胀到 >10 只 | 89.6% 权益（2,684,434 元）永久冻结 |
| 2 | **卖出执行**：SELL 信号只落 `signal_log` | `sell_rules.py:57-72`、`pipeline.py:53-72`；`broker.sell` 仅测试调用；`signal_log` 0 行 | 4 条硬卖出规则（含浮盈≥50%、持有≥60 月）全部只展示不执行 | 3 日 0 条 SELL 信号；0 笔卖单 |
| 3 | **分红再投接入** | `dividend.py:28-66` 无调用方；`manager.py:76-78` 死代码；`ingest.py:30-32` 只入 `corp_action` | 分红 0 现金入账、0 再投；红利税/批次逻辑永不运行 | `dividend_event` 0 行；年化漏记 ≈16.6 万元（TTM 口径，≈5.56% 权益） |
| 4 | **年度取现接入闭环** | `scheduler.py:32-40` 独有；`daily_close.py` 不调用；基数依赖空表 | 12 月任务 want=0 → 30% 取现永不发生；现金缓冲永不变化 | `withdraw_log` 0 行；计划取现 ≈4.99 万元/年 |
| 5 | **月度补仓空转**（目标按 cash×core_ratio 算） | `manager.py:35-36,50-53` | 建仓后每只目标 28,092 vs 持仓 ≈267k，10/10 delta<0 → 0 成交 | Σdelta = **−2,403,507.64**；月度可投上限只剩 9.375% 权益 |
| 6 | **配置键错位** | `rebalance.py:37` vs `strategy.yaml:28` | `selection.rebalance` 改了不生效（唯一真值失效） | 当前被默认值掩盖 |
| 7 | **触发频次异常**（has_buy 判定） | `rebalance.py:40-46` | 无 BUY 的月份 → 每天 rebalance_due=True → 每天全市场漏斗重建，仍 0 成交 | 12-01 起每个交易日触发 |
| 8 | **`account.total_equity` 陈旧** | `ledger.py:31` 初始化 3,000,000；`manager.py:65-74` 的刷新函数无调用 | 任何未来修复若读 `acc.total_equity` 会拿到建仓前数值 | 快照值仍为 3,000,000，而实际权益 2,996,574 |
| 9 | 附带：T+1 解锁只在调仓日内 | `rebalance.py:89`（`settle_eod` 唯一生产调用点） | 非调仓日不解锁；且 `settle_eod` 连当日买入一起解锁（`broker.py:191-199`），T+1 语义在"盘后即收盘"模式下实际等于当日可卖 | 持仓 `shares_available` 字段恒 0（不维护） |
| 10 | 附带：`signal_log` 无消费者（BUY/overlay 同样） | `pipeline.py`、`routers_app.py:144` | overlay 实验层的 BUY 信号同样从不执行 | 3 日 0 行 |

### 3.2 当前权重漂移（2026-11-05 建仓等权 vs 2026-11-06/07 收盘）

口径：权重 = 个股市值（`price_daily.adj_close` × shares）/ 总权益（现金 + Σ市值）；目标 = `core_ratio/N = 9.0%`（**注意：这是"目标可投资产权重"，现金另占 10%**）。

| 标的 | 11-05 建仓市值 | 11-05 权重 | 11-06 收盘市值 | 11-06 权重 | 偏离 9% |
|---|---|---|---|---|---|
| sz000157 | 269,610 | 8.9919% | 265,310 | 8.8538% | −0.1462pp |
| sz000423 | 267,809 | 8.9319% | 269,505 | 8.9938% | −0.0062pp |
| sz000568 | 267,630 | 8.9259% | 267,861 | 8.9389% | −0.0611pp |
| sz000651 | 267,030 | 8.9059% | 269,238 | 8.9849% | −0.0151pp |
| sz000987 | 269,966 | 9.0038% | 268,410 | 8.9572% | −0.0428pp |
| sz002032 | 266,940 | 8.9029% | 272,160 | 9.0824% | +0.0824pp |
| sz002236 | 269,742 | 8.9963% | 266,082 | 8.8795% | −0.1205pp |
| sz002532 | 269,399 | 8.9849% | 266,084 | 8.8796% | −0.1204pp |
| sz002555 | 269,892 | 9.0013% | 264,384 | 8.8229% | −0.1771pp |
| sz300979 | 268,192 | 8.9447% | 275,400 | 9.1905% | +0.1905pp |
| 现金 | 312,140 | 10.410% | 312,140 | 10.417% | — |
| **合计** | 2,998,350 | 100% | 2,996,574 | 100% | — |

- **11-05 建仓时**：等权目标 9.0%，因 100 股整手取整，实际落在 **8.903%~9.004%**（偏离 −0.097~+0.004pp），已投入 89.590% 权益、现金 10.410%（设计 90%/10%）。
- **11-06 收盘（唯一有行情的第二个交易日）**：最大绝对偏离 **0.191pp**（sz300979 +0.191 / sz002555 −0.177），均值 ≈0.09pp。漂移很小——因为只过了 **1 个交易日**。
- **11-07（周六，无行情）**：`price_daily` 无 11-07 行，S* 净值与 11-06 完全相同（2,996,574.3984）→ 权重与 11-06 相同；`run_intraday` 也不会产生订单。
- **漂移趋势的反事实估计**（解释"缺修剪"的长期后果，用库内 2026-04-20→11-06 共 160 个日期的行情）：假设 2026-04-20 以 9%/只 等权建仓并买入持有到 11-06（约 6.5 个月），权重会漂到 **7.459%~11.316%**，最大绝对偏离 **2.32pp**（sz000651 11.316% vs 9%；sz002532 7.459%），均值 1.25pp、现金 10.93%。→ **漂移是数月量级持续累积的**；生产没有任何机制把它拉回，一年以上偏离会更大（本库只有 160 天历史，无法给多年数字，此处仅作量级证据）。
- 结论：**"当前持仓缺少再平衡"成立**——不是因为今天偏离很大（1 天只有 0.19pp），而是**因为修复机制不存在**：无论漂移多大，`apply_targets` 都只会跳过（delta<0），永远不会 trim。

---

## 4. 最小修复建议（只描述，不改代码）

> 目标：让"再平衡"真正生效 = **目标口径 + 修剪路径 + 卖出执行 + 分红现金流接入 + 触发频次修正**，同时保持"禁止轮动/换股"的已证伪约束。

**P0（让月度调仓语义成立，最小改动）**

1. **目标金额改为按总权益计算**：`apply_targets` 内用 `equity = cash + Σ(shares_total × adj_close)`（优先复用 `PortfolioManager.total_equity()`，`manager.py:65-74`；**不要读 `account.total_equity`，快照里它已陈旧为 3,000,000**），`target_mv = equity * core_ratio * weight`。这样目标总和 = 90% 权益，与 `core_ratio` 的"10% 现金缓冲"语义一致。
2. **加入 trim（卖出）分支**：`delta < 0` 且超过阈值时调 `broker.sell()`（先卖后买，卖出时校验 `shares_total - shares_locked`；注意先处理 T+1 解锁顺序）。为避免频繁小额交易，加"最小交易额/权重偏离带（band）"与成本闸（可用 `cost.round_trip_cost_ratio()`）；未超过 band 不动。
3. **卖出规则接入执行**：在 `daily_close` 中，把 `generate_and_store_signals` 之后新增一步"SELL 信号 → 清仓清单"（或直接调用 `evaluate_sell`），由执行器调 `broker.sell` 全清；同时修复 `dividend_interrupted`：调用 `evaluate_sell` 时传入 `check_dividend_interrupted(session, sym, asof)` 的结果（`sell_rules.py:65` 现在漏传，规则永不触发）。
4. **分红现金入账 + 再投**：`daily_close` 在 `ingest_daily` 之后，对持仓标的取 `corp_action.ex_date == asof`（或 `<= asof` 且未在 `dividend_event` 中登记）的事件，调 `DividendReinvest.on_dividend(...)`（`broker.on_dividend` 会写 `DividendEvent`、按 FIFO 扣红利税、净额进现金）；再调用 `reinvest()`。**接入时必须区分"分红现金"与"基础 10% 现金缓冲"**：现行 `reinvest` 的 `min_cash_ratio=2%`（`dividend.py:41,48`）会把缓冲一起投出去，与 `core_ratio=0.9` 冲突 → 建议只再投"本次分红净额"或把保留下限改为 `core_ratio` 对应的缓冲。
5. **年度取现接入闭环**：在 `daily_close` 内判"是否为 12 月最后一个交易日"（交易日历 `da_core/calendar.last_trading_day_of_december`）→ 调 `run_annual_withdraw(session, cfg, asof)`；它依赖第 4 步的 `dividend_event`，否则 `want` 恒 0。
6. **触发频次与配置键修正**：`rebalance_due` 改读 `cfg.selection["rebalance"]`（建议在 `config.py` 加 `Strategy.rebalance_mode` property，替代 `rebalance.py:37` 的 `cfg.portfolio.get(...)`），并支持 `monthly/off`；把"本月是否已执行"从 `has_buy` 改为**独立的幂等标记**（例如 `ExecutionLog(task="rebalance", idempotency_key=f"{YYYY-MM}:{account_id}")`），保证每月只触发一次（而不是"没成交就天天触发"）。
7. **可观测性**：每次 rebalance 写一行 `ExecutionLog`（记录 targets/deltas/trades 数），让"空转"在日志里可见；`account.total_equity` 在建仓/每日收盘后刷新。
8. **测试守护**（现有 `tests/` 完全没有覆盖）：① 建仓后次月 `run_rebalance` 必须产生 trim/买卖（用假价格把某只抬高后断言 sell）；② 注入 `DividendEvent` 后 `daily_close` 现金增加且再投；③ 无 BUY 的月份 `rebalance_due` 只 True 一次；④ `selection.rebalance` 改值生效；⑤ `dividend_interrupted` 能触发 SELL。

**若只想做"研究口径"的最小对齐**：把 `investable = cash * core_ratio` 改成 `total_equity * core_ratio` 即可让月度再平衡不再是 0 成交（但**没有 trim 时仍只有"补仓/小仓换入"半套语义**）；完整"等权重再平衡"必须同时有 P0-1 + P0-2。

---

## 5. 复现方法与局限

**复现（只读）**

```sql
-- 账户/持仓/订单
SELECT * FROM account; SELECT * FROM position; SELECT * FROM orders;
SELECT side, trade_date, count(*) FROM orders GROUP BY side, trade_date;
-- 关键空表
SELECT count(*) FROM dividend_event;   -- 0
SELECT count(*) FROM withdraw_log;     -- 0
SELECT count(*) FROM signal_log;       -- 0
-- 净值与行情
SELECT trade_date, strategy_id, nav FROM daily_nav WHERE strategy_id='S*' ORDER BY trade_date;
SELECT min(trade_date), max(trade_date), count(*) FROM price_daily;  -- 2026-04-20 / 2026-11-06 / 7995
```

```python
# 月度再平衡 delta 复算（关键一步）
investable = 312140.3984390001 * 0.9          # manager.py:36
per_target = investable / 10                   # manager.py:50, core_selection.py:96
# per_target = 28092.6359；10 只 cur_mv ≈ 264,384~275,400 → 全部 delta < 0 → 0 成交
```

**局限/注意**

1. 本报告 DB 数值来自 **2026-11-08 15:39 快照**；15:41 起另有进程清表并重放（非本审计），且该次重放因漏斗数据波动选出了**另一组 10 只**核心仓（cash≈310,310.28、持仓成本合计≈2,688,039、S\* NAV≈2,998,349.28）。结构结论与篮子无关：`0.9×cash/10 ≈ 2.79 万/只` vs 持仓 `≈26.9 万/只` → delta 仍全为负、月度再平衡仍 0 成交。复现前请先按 `scripts/server_replay.sh` 重放 2026-11-05~11-07 再对照本文。
2. `price_daily` 含 **24 个周末日期（均标 `ths_fuyao`、各 10 只）**，且库内 TTM 股息率偏高（约 6.2% 持仓市值）→ 数据带**演示/半合成成分**。本文的"结构性结论"（无 sell 调用、无分红接入、target 按 cash 计算等）是**代码层事实，不受数据质量影响**；分红金额、漂移 pp 数仅用于量级参考。
3. 11-07 无行情 → "11-07 收盘权重"按 11-06 收盘口径；11-07 的 NAV 是周末重放写下的冻结值。
4. 本库只有 2026-04-20~11-06 共 160 个交易日，无法给出"多年不修剪"的漂移数字；§3.2 的 6.5 个月反事实仅作趋势量级证据。

---

## 6. 一句话回答 SPEC §1 第 1 问

**生产代码里有"再平衡"的函数名和调用点，但没有再平衡的行为。缺的是"修剪（把超配持仓卖回目标权重）"这一环**：`apply_targets` 只补仓不修剪（`manager.py:51-53`），目标金额又按 `cash*core_ratio` 而非总权益（`manager.py:35-36`），初始建仓耗尽 90% 现金后 `delta` 恒为负 → 月度再平衡**永久空转**；配套的卖出执行、分红再投、年度取现三条现金流/执行链也全部断裂；"换股轮动"一类则早已被 `rotation.enabled=false` 明确禁止（不在缺失清单内）。
