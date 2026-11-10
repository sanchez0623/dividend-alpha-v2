# SPEC.md —— DC 分红捕获策略历史回测规格（冻结 · point-in-time 主口径）

> 项目：dividend-alpha-v2 / `research/dividend_capture`
> 生成：2026-11-09（task-3 重做：**买入日当时市值筛池**，替换 v1 的「当前市值筛池」）
> 规则来源：用户口述口径 + `config/strategy_dividend_capture.yaml` + `src/da_strategy/dividend_capture.py`
> 本文件是本次回测的唯一口径真值。任何指标只有在符合本规格的前提下才有意义。

## 1. 目标

用**真实历史行情、真实除权事件与真实历史总股本**，检验「除权前买入 → 拿现金分红 → 除权后 30 天卖出 →
取现 30%」这套 DC 分红捕获规则在 2016-01-04 ~ 2026-11-06 的收益、风险与现金流特征。

**v1 的致命口径问题**：v1 用「2026-11-08 当前总市值 ≥500 亿」筛出 344 只池，再回测历史——
历史上市值 ≥500 亿、后来跌下来的公司（包括退市股）完全没进样本，存在严重幸存者偏差。
**v2（本规格）改为**：候选 = 全 A 当前上市股票（约 5,563 只，排除 ST/退）；每个候选按**买入日当时
总股本 × 当日不复权收盘**计算当时总市值，仅当 **≥500 亿**才合格。旧口径结果保留在
`results/legacy_current_cap/` 作为对照。

**不做的事**：不为让结果好看而改规则/挑样本；不向合成数据源借数据；不拟合参数。

## 2. 数据

| 项 | 口径 |
|---|---|
| 全 A 快照 | 东方财富 `fetch_all_a_spot()`（2026-11-09，5,563 行）→ 只排除 **ST/退** 与无效代码，**不再按当前市值筛选** |
| 日线 | 同花顺 fuyao `prices/historical-batch`，`adjust="none"`（**不复权**），批量 10 只/次；两段窗口 2015-01-01..2024-12-31 + 2024-12-01..2026-11-06 |
| 除权事件 | 同花顺 `corporate-actions/adjustment-factors`，`since=2014-11-01`；同 (symbol, ex_date) 多行按 dps/bonus 求和聚合 |
| **历史总股本** | **akshare `stock_share_change_cninfo(symbol, start_date=20140101, end_date=20261106)`**（巨潮资讯，单位万股 ×1e4）；解析成 (symbol, change_date, total_shares) step function：`total_shares_at(t) = 最近一条 change_date ≤ t 的记录` |
| 退市股 | akshare `stock_info_sh_delist()` / `stock_info_sz_delist()` 取 ≥2015 年退市的 A 股；同花顺无退市股数据（实测 3001/1002），改用**东方财富 push2his 行情 + F10 股本变动（lngbbd）/分红方案（fhyx）**尽力纳入 |
| 交易日历 | 复用 `research/rebalance/data/index_000300.csv`，2015-01-05..2026-11-06 共 2,877 个交易日 |
| 预热 | 2015 全年用于 MA120 预热；回测从 2016-01-04 起 |
| 缓存/质检 | `data/cache/{raw_all,ca_all,shares,delisted}/`；`data/manifest_all.json`（SHA256）；`data/DATA_QUALITY_ALL.md`（失败清单/覆盖度/残余偏差） |

### 2.1 粗筛 → 精筛（控制请求量）

1. **粗筛**：`shares_est = 当前总市值 / 最新不复权价`；`max 粗市值 = shares_est × 历史最高收盘`；
   保留 `max 粗市值 ≥ 200 亿` 的标的抓除权事件与历史总股本（200 亿 = 500 亿的 40%，留安全边际）。
2. **精筛**：用除权送股事件反推估算股本，重算 `max 精市值`，保留 ≥200 亿的标的抓历史总股本。
3. 阈值不足导致的漏筛风险在 REPORT 中披露；退市股不参与粗筛，直接尽力抓全部。

## 3. 策略规则（冻结，来自生产纯逻辑）

复用的生产纯逻辑（`src/da_strategy/dividend_capture.py`）：
`DcRules.from_strategy` / `evaluate_candidate` / `rank_candidates` /
`entry_date_for` / `exit_date_for` / `ttm_dividend` / `settle_cycle` / `dividend_tax_for`。
回测不重写这些规则，只提供数据与账户模拟。

### 3.1 候选（每个除权事件一条候选）

- 事件：`dps > 0` 的现金分红除权事件（送股/转增也计入；dps=0 的纯送转不单独成为候选）。
- `ex_date` = 除权日；`entry_date` = 除权日前第 **5 个交易日**；`exit_date` = 除权日 + **30 个自然日**后的首个交易日。
- **股息率 TTM** = Σ(近 365 天内每股现金分红，含当前事件) / **买入日不复权收盘价** ≥ **6%**。
- **买入日当时总市值** = `total_shares_at(entry_date) × raw_close(entry_date)` ≥ **500 亿元**
  （历史总股本缺失时回退「当前股本/送股反推」估算，回退笔数在 summary 中披露）。
- **趋势过滤**（默认 `nondown`）：用前复权序列（由不复权价 + 除权事件反推，最新日=1.0）
  计算 MA20/60/120、RSI14、20 日跌幅；`last_close > MA120` 才算不下跌；`up` = 站上 MA60 且 MA60>MA120；
  `up_pull` = up 且 RSI∈[35,70] 且 20 日跌幅∈[-12%,+3%]。趋势判定只用 **entry 日及之前**的数据。
- 排序：趋势优先级（up_pull > up > nondown > down）→ 股息率降序 → 市值降序 → 代码。

### 3.2 交易与账户

- 初始资金：配置 `meta.init_cash` = **3,000,000 元**；现金不计利息。
- **一次只持有一只**（`max_concurrent_positions=1`；变体测试 2/3）。
- 买入：entry 日收盘价，100 股整数手；预算 = 可用现金 × (1 − 0.5% 现金缓冲)；成本用
  `src/da_core/cost.py::buy_cost`（参数读 `config/strategy.yaml` cost 段）。
- 除权日：`税前分红 = dps × 持仓股数` 记入现金；`per_share_bonus>0` 时股数 ×(1+bonus)、成本价 ÷(1+bonus)。
- 卖出：`exit_date`（或其后首个有 bar 的交易日）收盘价；成本用 `sell_cost`。
- 卖出时结算（复用 `settle_cycle`）：红利税按实际持有自然日（≤30 天 20% / 31~365 天 10% / >365 天 0%）；
  取现 = **净分红 × 30%** 转出账户；剩余资金滚入下一只。
- 停牌：买入日无 bar → **跳过该候选**；卖出日无 bar → **顺延**至下一有 bar 交易日。
- **数据防护**：疑似数据错误 bar（非除权事件日 + 相邻 bar ≤10 自然日 + |单日收益|>30%）命中买入日/计划卖出日
  → 跳过该笔（计 `skipped_data`）；持仓估值与延迟卖出用最近可信价。停牌复牌跳变按真实价格处理。
- 涨跌停：**不建模**（按收盘价成交）。
- 期末未平仓：按最后收盘估值（不强制平仓，不计入捕获次数）。

### 3.3 历史市值口径（本次核心修正）

```
旧口径（legacy，仅对照）：
    shares_est_now = 当前总市值 / 最新不复权价
    shares_t       = shares_est_now / Π(1 + per_share_bonus, 除权日 > t)
    cap_t          = shares_t × raw_close_t
新口径（point-in-time，主口径）：
    total_shares_t = 最近一条 change_date ≤ t 的历史总股本记录（akshare cninfo，万股×1e4）
    cap_t          = total_shares_t × raw_close_t                （t = 买入日）
    仅当 cap_t ≥ 5e10 合格；缺失历史股本时回退旧口径估算（并计数/披露）
```

## 4. 回测口径

- 主区间：**2016-01-04 ~ 2026-11-06**（约 10.84 年）。
- 子样本：**2018-01-02 起**、**2021-01-04 起**（到 2026-11-06）。
- pool62 对照：同一引擎与规则，股票池限定为 `research/rebalance/data/universe.txt` 的 62 只
  （PIT 口径：这 62 只同样用历史总股本 × 当日收盘算市值；旧口径 pool62 数字保留在 legacy 对照中）。
- 旧口径对照：v1 的 344 只（当前市值≥500 亿）池 + 当前股本反推市值，保留在 `results/legacy_current_cap/`。

## 5. 指标定义

| 指标 | 定义 |
|---|---|
| 总收益 | `期末总财富 / 初始资金 − 1`，总财富 = 账户权益 + 累计取现 |
| 期末净值（NAV） | `期末账户权益 / 初始资金`（不含取出；取现会机械性降低它） |
| CAGR | `(期末总财富/初始资金)^(1/年数) − 1`，年数 = 日历天数/365.25 |
| 年化波动 | 日总财富收益标准差 × √252 |
| Sharpe | 日收益均值/标准差 × √252（rf=0） |
| 最大回撤 | **总财富曲线**上的最大峰谷跌幅 |
| Calmar | CAGR / |MDD| |
| 捕获次数 | 完成的「买入→卖出」交易数（不含期末未平仓） |
| 捕获/年 | 捕获次数 / 年数 |
| 胜率 | `net_pnl > 0` 的交易占比 |
| 平均单次收益 | 交易 `net_pnl / buy_amount` 的均值 |
| 价格损益 | Σ(卖出金额 − 卖出成本 − 买入金额 − 买入成本) |
| 分红损益 | Σ(税前分红 − 红利税 − 取现) |
| 成本 | Σ(买入成本 + 卖出成本) |
| 放弃候选 | yield/cap/trend 拒绝、持仓重叠（skipped_overlap）、现金不足、无 bar、数据防护跳过等计数 |

## 6. 必做变体（一次只改一个参数；PIT 口径）

yield 5/6/7%；cap 300/500/1000 亿；lead 3/5/7 个交易日；hold 0/10/20/30/40 自然日；
max_positions 1/2/3；trend none/nondown/up/up_pull；tax 0/10/20%（平税率档）；withdraw 0/30%。

### 6.1 优化方向实验（task-3 追加；**不改主口径 base**）

- **条件卖出**（替代固定 ex+30 自然日；max_hold=30 自然日兜底）：
  `fill`（填权即卖，close≥除权前最后交易日收盘）/ `no_fill_3d`（除权后第 3 个交易日未填权即卖）/
  `stop_buy_3pct`（**主口径**：买入价 -3% 止损，除权日 close 跌破也砍）/ `stop_ex_3pct`（次口径：除权日收盘为基准）/
  `cover_tax`（收益覆盖红利税即卖，按**实际卖出日适用税率**计算目标价）/ 三个组合；
  触发全部按**当日 close** 成交；同一天 stop 与 fill 同时满足时 **stop 优先**（风控优先）；
  除权日触发 stop 时当日仍先拿税前分红，再按实际持有天数扣税（≤30 天为 20% 档）。
- **选池打分**（yield≥6% 仍是硬条件）：`cap300`（hard 300 亿 + trend nondown 硬筛）、
  `cap300_score` / `cap500_score` / `cap_any_score`（hard cap + trend 只加分，score 排序）：
  `score = yield + 0.5%×(cap≥500亿) + 0.5%×trend_score`（up_pull=1 / up=0.5 / nondown=0 / down=-0.5）。
- 结果见 `results/optimizations/` 与 `REPORT.md` §6-§8。

## 7. 验收断言（`verify.py`）

- 数据：`bars_all/events_all/shares_history` 与原始缓存独立重算一致；全 A 快照/ST 剔除/退市纳入计数。
- **PIT 市值**：每个候选 `cap = total_shares_at(entry) × raw_close(entry)` 逐笔复算；
  `cap < 500 亿的候选不得标记为合格`（双向）；历史股本缺失回退笔数披露；日期规则/趋势/TTM 无未来数据。
- 账户：现金任何时点 ≥ 0；`期末权益 = 期初资金 + Σ净盈亏 + 未平仓市值`；税/取现/成本逐笔复算一致。
- 可复现：同一输入跑两次 trades/nav 哈希一致，且等于 summary 记录值。
- 旧口径回归：重算旧口径与 `results/legacy_current_cap/summary.json` 完全一致。

## 8. 已知局限（必须与结果一起阅读）

1. **退市股残余偏差**：同花顺不提供退市股数据；本次用东财行情 + F10 股本/分红尽力补。
   F10 分红方案仅保留最近 10 条公告（约 5 年），较早除权事件可能缺失；仍抓不到的退市股不在样本内。
2. **ST/退按当前状态剔除**：未做历史 ST 状态 point-in-time；当前 ST/退股票的历史非 ST 期不在样本内。
3. **历史总股本缺失回退**：少数标的回退当前股本/送股反推估算（披露回退笔数）。
4. **粗筛阈值漏筛**：max 估算市值 <200 亿的标的不抓除权事件，理论上存在漏网（误差需 >2.5 倍）。
5. **北交所不在池内**（东财全 A 快照 fs 覆盖沪深主板/创业板/科创板；北交所无 ≥500 亿标的）。
6. **红利税/取现口径**：除权日税前分红入现金、卖出时收税；取现按净分红 30%；与生产账本一致。
7. **频率现实**：单标的 + 除权后 30 天持有 + 一年分红 1-2 次 → 实际约 **4-6 次/年**，非「一年 10 次」。
8. **回测≠未来**：历史结果不代表未来收益；参数未做任何拟合/优化。
9. 涨跌停无法成交、流动性冲击、盘后公告导致的价格漂移均未建模。
