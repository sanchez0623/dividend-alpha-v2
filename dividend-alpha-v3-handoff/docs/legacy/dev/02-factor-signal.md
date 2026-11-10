# 02 · 因子、轮动引擎与增强层开发规格

> 本章是系统的核心。所有参数均带 `【实证】` 标注，来自两轮检验，未经批准不得修改。

---

## 1. 因子层 `da_factor`

### 1.1 股息率 TTM

```python
def div_yield_ttm(DIV, SF, CLOSE, t):
    num = sum(DIV[:, u] * (SF[:, u] / SF[:, t]) for u in range(t-11, t+1))
    return num / CLOSE[:, t]
```

### 1.2 ★ 股息率历史分位（轮动主因子）

```python
def div_yield_pctile(YLD, t, window=60, minobs=48):
    """分位越高 = 股息率处自身历史高位 = 股价相对自己越便宜"""
    w = YLD[:, max(0, t-window+1) : t+1]
    fin = np.isfinite(w); c = fin.sum(1); cur = YLD[:, t]
    ok = np.isfinite(cur) & (c >= minobs)
    le = ((w <= cur[:, None]) & fin).sum(1)
    pct = np.full(S, np.nan); pct[ok] = le[ok] / c[ok]
    return pct
```

**【实证】IC 表现**（越高越好）：

| 周期 | IC 均值 | ICIR | IC>0 占比 |
|---|---|---|---|
| 3 月 | +0.025 | 0.22 | 57.4% |
| 6 月 | +0.034 | 0.34 | 68.6% |
| 12 月 | +0.055 | 0.56 | 72.5% |
| **24 月** | **+0.096** | **0.95** | **82.3%** |

→ **因子主要作用在 24 个月周期**，这决定了持有期应以"月-年"计，不是做 T 尺度。

### 1.3 季节分位（可选增强，除权免疫）

```python
def pctile_seasonal(YLD, t, lags=(12,24,36,48,60), minobs=4):
    """只与去年/前年同期比，天然规避除权日跳变"""
```
【实证】略优于滚动分位（12m IC +0.060、24m +0.098），可作为 `conflict_policy` 的第二票。

### 1.4 连续分红年数 ★（轮动开关）

```python
def consec_div_years(divyear: set[int], year: int, n=5) -> bool:
    return all((year - k) in divyear for k in range(1, n+1))
```

【实证】**这是轮动生效的开关**：

| | 无此过滤 | 有此过滤 |
|---|---|---|
| B7 轮动 | 12.21% | **14.02%** |
| B2 持有 | 12.99% | 10.97% |
| **轮动 − 持有** | **−0.77%** | **+3.05%** |

### 1.5 多窗口冲突处理

同一标的窗口不同结论会完全相反（银行：10年 26-45%、5年 72%、3年 80%、2年 91%）。

```yaml
div_yield_pctile:
  windows: [3, 5, 10]
  primary: 5y                    # 【实证】主用 5 年
  conflict_policy: conservative  # 取最保守判定，宁可不换
```

---

## 2. 选股漏斗 `da_portfolio.select`

```
全 A 股 ~5222 只（剔除北交所）
   ↓ ① 排雷
剔除 ST/退市整理；上市 <3 年；近6月中位月成交额 < 3000万；股价 < 2元
   ↓ ② 质量 ★ 轮动开关
连续分红年数 ≥ 5            【实证 H1，不可降级】
   ↓ ③ 股息（绝对底线）
股息率 TTM ≥ 2.5%           【实证 H4：提高到3.5%会掉到 10.75%】
   ↓ ④ 相对高低位 ★ 主信号
股息率 5 年分位 ≥ 60         【实证 H2】
   ↓ 候选池（2026-10：分位>70 的有 405 只，横跨消费/医药/材料/工业/券商）
   ↓ 排序取 Top N=20（等权）
```

**候选池不设行业偏好**【实证 H9】。当前实测构成（分位>70 的 405 只样本）：
珀莱雅、东鹏饮料、燕京啤酒、金徽酒、大参林、丽珠集团、健康元、华东医药、巨化股份、南山铝业、北新建材、柳工、中际联合、申万宏源、华泰证券……

**优质股入选率**：美的 55.0%、五粮液 36.2%、格力 36.5%、海尔 32.5%、茅台 24.4%。

### 打分（仅用于排序，不用于过滤）

```python
total_score = 0.40*div_score + 0.35*quality_score + 0.25*value_score
# 惩罚：股息率>8% 且 PE分位>60% → ×0.6（价值陷阱）
#       近1年股息率骤升>2x     → ×0.8
```

> 注：实证中轮动组合按**分位降序**取 Top20 效果最好（14.02%）；综合打分版本未单独验证，作为 A/B 选项保留。

---

## 3. 轮动引擎 `da_signal.rotation`

### 3.1 状态机

```python
class RotationEngine:
    """月频触发，阈值驱动（非日历强制换仓）"""

    def on_month_end(self, t, positions: dict[int, int]) -> list[OrderIntent]:
        # 1) 计算当期候选池
        pool = self.universe(t)                       # 见 §2 漏斗
        # 2) 换出判定
        sell = []
        for j, buy_t in positions.items():
            if t - buy_t < self.cfg.min_hold_months:  # 6
                continue
            p = PCT[j, t]
            if not np.isfinite(p) or p * 100 < self.cfg.exit_pctile:   # 30
                sell.append(j)
        # 3) 换手预算：单次最多换 max_change 只【实证：6 只 / N=20】
        if len(sell) > self.cfg.max_change:
            sell = sorted(sell, key=lambda j: PCT[j, t])[:self.cfg.max_change]
        # 4) 换入：按分位降序补满 N
        buy_from = [j for j in pool if j not in positions]
        order = sorted(buy_from, key=lambda j: -PCT[j, t])
        ...
```

### 3.2 参数（默认 = 实证最优）

| 参数 | 值 | 依据 |
|---|---|---|
| `N` | 20 | 实证基准 |
| `weighting` | equal | |
| `entry_pctile_5y` | 60 | H2 |
| `exit_pctile_5y` | 30 | H3 |
| `min_hold_months` | 6 | 【实证】设 12 月则 11.83%（低于 14.02%） |
| `max_change_per_event` | 6 | 实证默认 |
| `rebalance` | **monthly** | 【实证】月频 14.02% > 季频 9.72% > 半年频 10.42% |
| `min_div_years` | 5 | H1 开关 |
| `min_div_yield` | 0.025 | H4 |
| `min_price` | 2.0 | |
| `min_adv_6m` | 3e7 | |

### 3.3 换出规则（★ 不带任何"保护"条款）

```yaml
rotation:
  exit:
    trigger_pctile_5y_below: 30.0
    confirm_months: 0            # 【实证】不加确认期
    action: full_sell            # 【实证】清仓，不是减半
    hold_overrides: []           # ★★ 空！trend_guard 已证伪，禁止添加
```

**【实证 X1】趋势保护有害**：加入 `close>MA60 且 MA60 上倾 → 只减半` 后，年化 **14.02% → 10.74%**，最大回撤 **−24.5% → −33.3%**。
结论：高股息股涨贵了就该卖，"让利润奔跑"在此不成立。

### 3.4 无股可换（保留但降级）

【实证】合格标的**中位数 153 只，最少 10 只**（2015-04/05），<15 只的月份仅 **2/165（1.2%）**。

```yaml
underfill_policy:
  min_qualified_candidates: 10
  action_sequence: [hold_current, add_etf, hold_cash]
  etf_fallback: ["510880.SH", "515180.SH", "512890.SH"]
  max_cash_ratio: 0.30
```

### 3.5 已证伪、禁止实现

```yaml
market_timer:      {enabled: false}   # 【X3】减仓使年化 13.11%->11.11%，回撤无改善
dividend_tax_optimization:
  full_year_bonus: {enabled: false}   # 【X2】年税仅0.35%，推迟卖出反使年化 -0.5%
```

---

## 4. 增强层：超跌反弹事件 `da_signal.overlay`

### 4.1 定义与证据

**【实证 H7】** 600 只大市值股、177 万日收益样本；事件 = 过去 N 日累计跌幅超阈值，持有 M 日：

| 事件（质量池内） | 样本 | 毛超额 | 净超额(扣0.30%) | t | 胜率 | 年机会(20只) |
|---|---|---|---|---|---|---|
| **20日跌>20% 持20日** | 2,890 | **+4.03%** | **+3.73%** | 17.8 | **58.4%** | **7.9 次** |
| 20日跌>25% 持10日 | 51,990 | +3.63%* | +3.33% | 26.5 | 59.4% | 5.7 次 |
| 20日跌>20% 持10日 | 4,094 | +2.23% | +1.93% | — | 57.4% | 11.2 次 |
| 10日跌>15% 持10日 | 4,956 | +1.83% | +1.53% | — | 55.0% | 13.5 次 |

\* 未去重叠口径；去重叠后见"年机会"列。

**关键**：**质量过滤让信号更强**（全样本 2.95% → 质量池 3.84%，胜率 57.7% → 60.5%）。
能连续 5 年分红的股票跌下来，更可能是**情绪性超跌**而非基本面崩塌 —— 这正是"错误定价"。

### 4.2 实现

```python
class OversoldReboundGenerator:
    """事件驱动：等极端超跌，而非每日找机会"""

    def scan(self, daily_bars, universe_mask, positions, cfg) -> list[Signal]:
        out = []
        for sym in universe_mask & quality_pool:          # ★ H8：只在质量池内
            if sym in cooldown: continue                  # 同标的 20 日内不重复
            ret20 = close[-1] / close[-21] - 1            # 20 日累计跌幅
            if ret20 <= -cfg.drop_threshold:              # 0.20
                expected_gross = cfg.base_excess           # 4.03%
                expected_cost  = 0.0030                    # 往返
                expected_net   = expected_gross - expected_cost   # 3.73%
                if expected_net >= cfg.min_expected_net:   # 1.0%
                    out.append(Signal(kind='OVERLAY', ...))
        # 按 expected_net 降序，最多 max_concurrent 笔
        return sorted(out, key=lambda s: -s.expected_net)[:cfg.max_concurrent]
```

### 4.3 参数

```yaml
overlay:
  enabled: true
  kind: oversold_rebound
  trigger:      {lookback_days: 20, drop_threshold: 0.20}
  holding_days: 20
  position_per_trade: 0.20     # 单笔占用总资产 20%
  max_concurrent: 3            # 最多同时 3 笔（总敞口 ≤60%）
  require_in_core_universe: true   # ★ H8 只在连续分红5年池内
  min_expected_net_pct: 1.0
  cooldown_same_symbol_days: 20    # 去重叠
  # 预期：7.9 次/年 × 3.73% × 20% ≈ +5.9%/年
```

### 4.4 仓位与记账

增强层与轮动仓**物理隔离**（`tier='TACTICAL'`），独立 FIFO 批次，独立归因。
这样可一键关闭增强层做 A/B（B7 vs B7+overlay）。

### 4.5 已关闭的旧信号族

```yaml
legacy_t_trade: {enabled: false}   # RSI/布林/量价背离/支撑压力
```
【实证 X4】证据远弱于超跌反弹。代码保留但默认关闭，供后续 A/B 对比。

---

## 5. 风控 `da_risk`

```yaml
risk:
  max_single_weight: 0.08
  max_industry_l1_weight: 0.25      # ⚠️ 待行业数据补齐后生效
  max_positions: 25
  min_positions: 15
  max_annual_turnover: 2.0          # 【实证】实际仅 0.54
  drawdown_halt: 0.18               # 回撤 18% 停止增强层（轮动继续）
  single_trade_max_pct_nav: 0.03
```

**卖出硬规则**（立即换仓，不等月末）：
- 连续分红中断（当年不分红）
- 分红率 > 100%
- 扣非净利连续 2 年下滑 > 20%
- 出现重大违规 / 退市风险

---

## 6. 信号对象

```python
@dataclass
class Signal:
    ts: datetime; symbol: str
    kind: Literal['ROTATION', 'OVERLAY', 'RISK']
    direction: Literal['BUY', 'SELL']
    horizon: str                     # 'MONTHLY' | '20D'
    strength: float
    expected_gross: float            # %
    expected_cost: float             # %  ★ 必须填，不得为 0
    expected_net: float              # %  ★ = gross - cost
    entry_price: Decimal; target_price: Decimal; stop_price: Decimal
    reasons: dict                    # 触发明细，用于复盘
    config_hash: str                 # ★ 可追溯

    @property
    def is_actionable(self) -> bool:
        return self.expected_net >= self.threshold and self.risk_reward >= 1.5
```

**成本闸**：任何 `expected_cost = 0` 的信号直接拒绝入库（防止回测虚高）。
