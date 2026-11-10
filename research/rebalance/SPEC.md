# 再平衡回测 · 规格（Lead 冻结，唯一真值）

> 目标问题：**当前持仓体系缺少"再平衡"（只补仓、不修剪），这部分能否产生超额收益？能不能回测？**
> 本目录是一次性研究（research），不进入生产镜像、不改动 `src/`、`config/`、`dividend-alpha-v3-handoff/`。
> 结论必须可复现：数据 CSV + 引擎 + 结果 + 独立复核。

---

## 1. 必须回答的三问

1. **现状**：生产代码里到底有没有再平衡？缺的是哪一类（补仓 / 修剪 / 换股）？（由 Lead 用代码证据回答）
2. **超额**：把"等权重再平衡"叠加到同一篮子高股息股票上，相对"买入持有"，**费后**年化超额是多少？是否统计显著？
3. **可回测**：用真实历史行情（后复权，含分红再投）能否复现，数据与成本假设是否站得住？

---

## 2. 数据契约（data-fetch 负责）

### 2.1 来源
- 东方财富 `push2his.eastmoney.com` 日 K：
  `GET /api/qt/stock/kline/get?secid=<1.600036|0.000651>&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57&klt=101&fqt=2&beg=20150101&end=20500101&lmt=100000`
  - `fqt=2` = **后复权**（已含分红/送转再投，等价于总收益指数）。这是本次回测的**唯一价格口径**。
  - 字段：`f51=date,f52=open,f53=close,f54=high,f55=low,f56=volume,f57=amount`。
  - 请求间隔 ≥0.3s，失败重试 3 次（指数退避），记录失败清单。
- 指数：`secid=1.000922`（中证红利，fqt=0）、`1.000300`（沪深300，fqt=0），仅作背景对照，不参与组合模拟。
- 注意：**不要用 fqt=1（前复权）**——本环境该接口的前复权序列有已知异常（同一日期 OHLC 复权因子不一致）。已实测 fqt=2 为一致口径。

### 2.2 输出文件（全部 UTF-8，逗号分隔，date 为 `YYYY-MM-DD`）
| 文件 | 内容 |
|---|---|
| `research/rebalance/data/close_adj_wide.csv` | 宽表：第一列 `date`，其余每列一个内部代码（如 `sh600036`），值=后复权收盘价（float）。索引为交易日全集（用 000300 的交易日历），个股停牌/缺失处**留空**（不要前向填充到文件里，引擎自己 ffill）。 |
| `research/rebalance/data/index_000922.csv` | `date,close`（不复权指数点位） |
| `research/rebalance/data/index_000300.csv` | `date,close` |
| `research/rebalance/data/universe_meta.csv` | `symbol,first_date,last_date,n_days,missing_days,max_abs_daily_ret` |
| `research/rebalance/data/DATA_QUALITY.md` | 质量报告（见 2.4） |

- 内部代码→secid：`sh`/`sz` + 6 位数字；`sh`→`1.xxxxxx`，`sz`→`0.xxxxxx`。
- 抓取范围：2015-01-01 至 2026-11-06（含两端）。

### 2.3 标的池（固定，共 62 只；见 `data/universe.txt`，已由 Lead 落盘）
即 `data/dividend_alpha.db` 中 `price_daily` ∪ `universe_cache` 的 62 只演示池标的（52 只候选 + 10 只现持仓/额外标的）。其中：
- **CURRENT10（现持仓）**：`sz000157, sz000423, sz000568, sz000651, sz000987, sz002032, sz002236, sz002532, sz002555, sz300979`
- **OLD20（2015 年起有完整数据的长样本篮子）**：`sh600028, sh600036, sh600585, sh600900, sh601088, sh601288, sh601398, sh601857, sh601988, sz000001, sz000333, sz000538, sz000630, sz000651, sz000708, sz000858, sz000895, sz000921, sz002032, sz002142`

### 2.4 必须做的数据质检（写进 DATA_QUALITY.md，附数字）
1. 每只标的：首末日期、缺失交易日数、最大单日绝对涨跌幅（后复权口径，A股涨跌停 10%/20%，>25% 记为可疑并列出）。
2. **一致性抽检**：对 ≥10 只标的，取 `fqt=0` 不复权收盘价，计算 `adj/raw` 因子；后复权日收益与不复权日收益的差，应**只出现在除权日附近**（差值与当日分红/送转幅度同量级），列出最大的 5 个差异日及差幅。
3. **与生产库交叉校验**：`data/dividend_alpha.db` 的 `price_daily`（THS/akshare 前复权，窗口 2026-05-11~2026-11-06）与该源后复权日收益做相关：逐标的 corr 与最大绝对差，说明差异来源（复权口径/数据源差异），不得当作"数据错误"直接丢弃。
4. 交易日历：以 000300 交易日为全集；确认 2026-11-06 是最后一日。
5. 数据可用性结论：**是否足以支撑 2015-2026（≈11.8 年）与 2021-2026（≈5.5 年）两段回测**。

---

## 3. 模拟契约（backtest-engine 负责）

### 3.1 组合与账户
- 初始资金 3,000,000 元（与 `config/strategy.yaml` `init_cash` 一致）。
- **总收益空间、分数股**：以 `close_adj_wide.csv` 的后复权价为计价单位，持股数量允许小数（不模拟 100 股整数手；理由：再平衡目标金额≈27 万/只，整数手误差 <0.1%，对溢价估计无实质影响；在报告中注明）。停牌日按"最近可得后复权价" ffill 估值。
- 交易日按 `index_000300.csv` 日历；所有卖出/买入按**当日收盘后复权价**成交（与生产"盘后闭环"一致）。
- 分红再投已隐含在后复权价中（不做二次分红现金流）；现金不计息（rf 另设）。

### 3.2 成本（必须与生产 `config/strategy.yaml` cost 段一致，读取 YAML 而不是硬编码）
买入：`commission=max(金额*0.00005, 5)` + `transfer=金额*0.00001` + `handling=金额*0.0000341` + `reg=金额*0.00002` + `slippage=金额*5/10000`
卖出：同上 + `stamp_tax=金额*0.0005`
成本从现金中扣除；买单金额不得超过可用现金（先卖后买）。
另跑一套 `cost_mult=2`（滑点+佣金×2，代表真实冲击/费率上浮）作敏感性。

### 3.3 变体（每个篮子 × 每个变体独立跑，起始日相同、初始权重相同=1/N）
| 变体 | 触发 | 动作 |
|---|---|---|
| `hold` | 无 | 买入后永不交易（权重自然漂移） |
| `m_last` | 每月最后一个交易日 | 全组合调回 1/N 等权 |
| `m_first` | 每月第一个交易日 | 同上（对齐生产"本月首个执行日"） |
| `q` | 每季末最后交易日 | 同上 |
| `s` | 每半年末 | 同上 |
| `a` | 每年末 | 同上 |
| `band5` | 每月末检查：max_i abs(w_i - 1/N) ≥ 0.05 | 触发则全组合调回 1/N |
| `band10` | 同上，阈值 0.10 | 同上 |
| `m_last_nocost` | 同 `m_last` 但成本=0 | 用于分解毛溢价/成本拖累 |
| `m_last_cost2` | 同 `m_last`，cost_mult=2 | 敏感性 |

- 再平衡执行规则：计算目标金额 `target = total_equity/N`；先卖出超配部分（shares×P 减少），再买入低配部分；买入量受现金约束（卖后现金 + 现有现金），不得透支。被 ffill 的停牌股照常按 ffill 价交易（简化，注明）。
- 除权/分红：由后复权价自动处理，不得额外加现金分红。

### 3.4 指标（每个变体输出一行）
`cagr, vol_annual, sharpe(rf=0), mdd, calmar, turnover_annual_two_way, cost_pct_annual(成本/平均权益/年), n_rebalances, n_trades, avg_rebalance_cost, premium_cagr_vs_hold, premium_sharpe_vs_hold, premium_mdd_vs_hold, monthly_diff_mean, monthly_diff_t, monthly_diff_p, monthly_win_rate`

- `monthly_diff_*`：变体与 `hold` 的**月度收益差**序列（变体月收益 - hold 月收益）的均值、t 统计量、p 值（单样本 t 检验，H0: 均值=0）、胜率。
- 另输出：日度净值宽表（每个变体一列）用于画曲线。

### 3.5 篮子
| 篮子 | 起始日 | 说明 |
|---|---|---|
| `CURRENT10` | 2021-05-06（300979 上市后首个共同交易日） | 现持仓，含 5.5 年样本 |
| `OLD20` | 2015-01-05 | 长样本高股息/蓝筹 20 只 |
| `RANDOM10×100` | 2015-01-05 | 从"2015-01-05 起有数据、样本内交易日 ≥ 2500"的池中随机（无放回）抽 10 只，seed=0..99；只跑 `hold / m_last / q / a / band5` 五个变体，用于检验**溢价是否对篮子选择稳健** |

- RANDOM10 统计：每个变体溢价的均值、中位数、正比例、跨篮子 t 检验（H0=0）、5%/95% 分位。这是回答"是不是普遍有效，还是单一篮子的运气"的关键证据。

### 3.6 输出文件（UTF-8）
| 文件 | 内容 |
|---|---|
| `research/rebalance/results/variants_CURRENT10.csv` | 每变体一行指标 |
| `research/rebalance/results/nav_CURRENT10.csv` | `date,<各变体净值>` |
| `research/rebalance/results/variants_OLD20.csv` | 同上 |
| `research/rebalance/results/nav_OLD20.csv` | 同上 |
| `research/rebalance/results/random_baskets.csv` | `basket_id,variant,cagr,cagr_hold,premium_cagr,sharpe,sharpe_hold,turnover,cost_pct` |
| `research/rebalance/results/summary.json` | 机器可读汇总（关键数字 + 数据区间 + 参数哈希） |
| `research/rebalance/results/RESULTS_DRAFT.md` | 引擎作者结论草稿（含表格、图可省略） |

### 3.7 引擎硬性校验（跑完必须断言，失败即中止）
1. `hold` 期末净值 == Σ 初始份额×期末后复权价（误差 <1e-6），且全程无交易、成本=0。
2. 任意日现金 ≥ -1e-6；`cost_pct_annual` 与按成交额重算的成本一致（误差 <1e-9）。
3. 再平衡日只使用 `date <= t` 的价格（引擎天然满足，代码评审确认）。
4. `m_last` 的 `n_rebalances` 等于区间内月末数（±1）。
5. `m_last_nocost` 的 CAGR ≥ `m_last` 的 CAGR（成本单调性），且差值 ≈ `cost_pct_annual`。
6. 所有变体同一起始日、同一初始组合；`hold` 与 `m_last` 起始净值相同。

---

## 4. 证据与解释（evidence 负责，写 `research/rebalance/EVIDENCE.md`）

1. 读 `dividend-alpha-v3-handoff/docs/`（重点是 `01_PRD.md §6 已证伪清单`、`legacy/rotation-engine.md`、`legacy/dev/03-backtest.md`、`legacy/reports/REPORT*.md`），回答：
   - v2.0/legacy 里"月频 14.02% > 季频 9.72% > 半年 10.42%"到底是什么实验？**是"换股轮动"还是"等权重再平衡"**？二者不能混为一谈。
   - v3.1 为什么把 `rotation.enabled` 冻结为 false？证伪证据是什么（B7 vs B2/B8）？
2. 理论：再平衡溢价（volatility harvesting / 波动率收割）的来源、成立条件（收益相近、不完全相关、均值回复），以及 A 股高股息情形的特殊性（银行/煤炭/公用事业行业、红利税、动量效应、集中度约束）。
3. 文献/外部证据：检索公开研究（中英文均可）关于"等权重组合再平衡溢价 / rebalancing premium"的量级与风险，给 2-4 条可引用链接与结论（注明外部来源、非本地数据）。
4. 成本细节：A 股卖出印花税 0.05%、佣金双边、滑点、以及**红利税对再平衡的隐性成本**（持有 ≤1 年分红征 10%，>1 年 0%；再平衡卖出会重置批次）——给出每次/每年量级估计，说明回测未显式建模，需在结论中打折的方向。
5. 结论草案：先验预期（再平衡溢价毛收益量级、费后是否可能为负、适合频率），等待引擎数字后由 Lead 定稿。

---

## 5. 独立复核（verifier 负责，写 `VERIFICATION.md` + `verify.py`）

- 用**独立实现**（不得复用 engine.py 的关键函数）从原始 CSV 复算：
  1. `CURRENT10` 的 `hold` 与 `m_last` 的期末净值、CAGR；
  2. `m_last` 一次典型再平衡的成交明细（份额/金额/成本/现金），手工核对成本公式；
  3. 随机抽 5 个 RANDOM10 篮子复算 `premium_cagr`；
  4. 违反检查：现金为负、买入超现金、使用未来价格、成本重复扣除。
- 差异容差：CAGR 相对误差 < 0.5%（应接近 1e-6），否则判失败并回报。
- 另做 2 个"反事实" sanity check：
  - 成本设为 0 时 `m_last` 应显著改善；成本 ×2 时应显著变差（方向性检查）。
  - 用"随机再平衡日"（每月随机日）跑 `m_last`，溢价应与月末规则同量级，说明不是月末日历效应。

---

## 6. 已知局限（必须写进最终报告，不许隐藏）
- 篮子选择有**幸存者偏差**（61 只均为 2026 年仍在册的演示池）；CURRENT10/OLD20 用"今天"的名单回溯历史。缓解：A/B 两臂同一篮子，**溢价差**对选股偏差不敏感；RANDOM10 给出跨篮子稳健性。
- 后复权隐含"分红按除权日价格再投到原股票"，与生产 `dividend_reinvest.weight=current_market_value`（按当前市值比例分摊）略有差异。
- 未显式建模红利税批次重置、涨跌停/停牌不可成交、100 股整数手、T+1 锁定。
- 未建模生产 10% 现金缓冲与年度取现 30% 的现金流效应（对"再平衡溢价"估计为一阶近似）。
- 2026 年行情为环境当前时点数据；未来不可知，结论只描述历史样本。

---

## 7. 交付与写作用域（避免并发覆盖）
| 角色 | 可写文件 |
|---|---|
| data-fetch | `research/rebalance/data/**`, `research/rebalance/data_fetch.py`, `research/rebalance/data/DATA_QUALITY.md` |
| backtest-engine | `research/rebalance/engine.py`, `research/rebalance/run_backtest.py`, `research/rebalance/results/**` |
| evidence | `research/rebalance/EVIDENCE.md` |
| verifier | `research/rebalance/verify.py`, `research/rebalance/VERIFICATION.md` |
| Lead | `research/rebalance/SPEC.md`, `research/rebalance/REPORT.md`, 最终回答 |

**禁止**修改 `src/`、`config/`、`tests/`、`web/`、`dividend-alpha-v3-handoff/`、`data/*.db`。

---

## 8. 数据口径修订（Lead，2026-11-08 16:00，优先级高于 §2）

复核发现 §2.1 的「fqt=2 = 后复权」在本环境**不成立**：
- EM fqt=2 的 adj/raw 因子几乎逐日漂移（非分段常数、非单调），601088.SH 2015-2026 标准总收益 = 385%，EM fqt=2 仅 177%；日收益相关 0.986、最大单日差 11.5%。
- fuyao `adjust=forward` 长窗口同样不可用（2015 年价格被压到接近 0/负数）。

**修订后的数据契约（唯一有效）**：
- 价格文件改为 `research/rebalance/data/close_tr_wide.csv`（SHA256 前缀 `4d59e9d5487d312c…`）：
  - 口径 = 同花顺 fuyao 不复权价 + 同花顺除权事件 + 生产同款 `src/da_data/adjust.py::build_adj_factor` 前复权；
  - 日收益 = 总收益（分红按除权参考价 theo 再投，与生产 build_adj_factor 同口径；未扣红利税）；
  - 2877 行 × 62 标的，2015-01-05..2026-11-06，缺失留空。
- `data/close_adj_wide.csv`（EM fqt=2）**作废**，另存 `data/_em_fqt2_close_adj_wide.bad.csv`，仅供审计。
- 交易日历继续用 `data/index_000300.csv`。
- 引擎必须支持 `--prices` 指向新文件；其余模拟契约（§3.2 成本、§3.3 变体、§3.4 指标、§3.5 篮子、§3.7 断言）不变。
- 报告必须新增「数据口径修正」小节：旧（EM fqt=2）新（TR）关键数字对比，并声明旧结果作废、归档到 `results_em_fqt2_bad/`。
