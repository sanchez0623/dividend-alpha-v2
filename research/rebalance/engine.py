"""dividend-alpha-v2 再平衡回测引擎（research only，不进生产）。

唯一契约来源：``research/rebalance/SPEC.md`` §3。本模块只负责计算：
数据加载 / 停牌价 ffill / 变体模拟 / 成本 / 指标 / 权重漂移。
IO、断言编排与报告在 ``run_backtest.py``。

与 SPEC 对齐的关键约定
-----------------------
1. 总收益收盘价（TR 口径，SPEC §8：同花顺不复权+除权事件+生产同款前复权，
   日收益=分红按除权日收盘再投的总收益）是唯一计价单位；现金不计息；
   持股数量为分数股（SPEC §3.1 允许，并在报告中注明整数手误差 <0.1%）。
   （注：旧 EM fqt=2 后复权宽表已作废，见 SPEC §8 与 TR_VALIDATION.md。）
2. 交易日历 = ``index_000300.csv``；所有成交按**当日**收盘价。
3. 停牌/缺失价：在日历上做 ffill（只用过去值，无未来信息）。
4. 初始建仓（t0）**不计成本**：SPEC §3.7 断言 1 要求 ``hold`` 期末净值
   == Σ 初始份额×期末价，且全程无交易/成本=0；因此初始组合视为
   "已按 1/N 建好、成本已沉没"，所有变体用同一初始组合（对溢价差无影响）。
   仅再平衡产生的交易计成本。本假设在 RESULTS_DRAFT.md 中注明。
5. 再平衡：target = 当日总权益/N；先卖后买，买入总额（含费）受现金约束，
   现金不得为负；cost 公式严格读 ``config/strategy.yaml`` 的 ``cost`` 段。
6. ``m_last_cost2``：SPEC §3.2 明确 "cost_mult=2（滑点+佣金×2）"，因此
   ``cost_mult`` 只放大 commission_rate 与 slippage_bps（佣金下限 commission_min
   不变），其余税费不动；见 ``CostModel.describe()``。
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
import yaml
from scipy import stats

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MODULE_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = MODULE_DIR / "data"
DEFAULT_STRATEGY_YAML = PROJECT_ROOT / "config" / "strategy.yaml"
# SPEC §8：EM fqt=2 已弃用；唯一有效价格文件为 TR 宽表
DEFAULT_PRICES_FILENAME = "close_tr_wide.csv"
CALENDAR_FILENAME = "index_000300.csv"

SPEC_INIT_CASH = 3_000_000.0
TRADING_DAYS_PER_YEAR = 252
CALENDAR_DAYS_PER_YEAR = 365.25

# 现金/浮点安全阈值（SPEC §3.7 断言 2）
CASH_EPS = 1e-6
FLOAT_EPS = 1e-9


class EngineError(RuntimeError):
    """引擎契约被违反时抛出的错误。"""


# ---------------------------------------------------------------------------
# 成本
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CostSchedule:
    """config/strategy.yaml ``cost`` 段的只读快照（不得硬编码）。"""

    commission_rate: float
    commission_min: float
    stamp_tax_sell: float
    handling_fee: float
    reg_fee: float
    transfer_fee: float
    slippage_bps: float

    @classmethod
    def from_strategy_yaml(cls, path: Path | str = DEFAULT_STRATEGY_YAML) -> "CostSchedule":
        path = Path(path)
        if not path.exists():
            raise EngineError(f"找不到策略配置：{path}")
        with path.open("r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh)
        try:
            cost = cfg["cost"]
            return cls(
                commission_rate=float(cost["commission_rate"]),
                commission_min=float(cost["commission_min"]),
                stamp_tax_sell=float(cost["stamp_tax_sell"]),
                handling_fee=float(cost["handling_fee"]),
                reg_fee=float(cost["reg_fee"]),
                transfer_fee=float(cost["transfer_fee"]),
                slippage_bps=float(cost["slippage_bps"]),
            )
        except (KeyError, TypeError) as exc:  # pragma: no cover - 配置损坏才会触发
            raise EngineError(f"strategy.yaml cost 段缺少字段：{exc}") from exc

    def to_dict(self) -> dict:
        return {
            "commission_rate": self.commission_rate,
            "commission_min": self.commission_min,
            "stamp_tax_sell": self.stamp_tax_sell,
            "handling_fee": self.handling_fee,
            "reg_fee": self.reg_fee,
            "transfer_fee": self.transfer_fee,
            "slippage_bps": self.slippage_bps,
        }


class CostModel:
    """SPEC §3.2 的买卖成本。

    买入：commission=max(amount*rate, min) + transfer + handling + reg + slippage
    卖出：买入侧全部项 + stamp_tax
    ``cost_mult`` 依 SPEC 只作用于 commission_rate 与 slippage_bps（"滑点+佣金×2"）。
    """

    def __init__(
        self,
        schedule: CostSchedule,
        cost_mult: float = 1.0,
        zero: bool = False,
        label: str = "cost",
    ) -> None:
        self.schedule = schedule
        self.cost_mult = float(cost_mult)
        self.zero = bool(zero)
        self.label = label
        # 只有佣金率与滑点被 cost_mult 放大；税费/经手费/过户费保持配置值
        self.commission_rate = schedule.commission_rate * self.cost_mult
        self.slippage_bps = schedule.slippage_bps * self.cost_mult

    def buy_cost(self, amount: float) -> float:
        if self.zero or amount <= 0.0:
            return 0.0
        commission = max(amount * self.commission_rate, self.schedule.commission_min)
        other_rate = (
            self.schedule.transfer_fee
            + self.schedule.handling_fee
            + self.schedule.reg_fee
            + self.slippage_bps / 10000.0
        )
        return commission + amount * other_rate

    def sell_cost(self, amount: float) -> float:
        if self.zero or amount <= 0.0:
            return 0.0
        return self.buy_cost(amount) + amount * self.schedule.stamp_tax_sell

    def describe(self) -> dict:
        return {
            "label": self.label,
            "zero": self.zero,
            "cost_mult": self.cost_mult,
            "cost_mult_semantics": "SPEC §3.2：仅 commission_rate 与 slippage_bps ×cost_mult",
            "effective_commission_rate": self.commission_rate,
            "commission_min": self.schedule.commission_min,
            "slippage_bps": self.slippage_bps,
            "stamp_tax_sell": self.schedule.stamp_tax_sell,
            "handling_fee": self.schedule.handling_fee,
            "reg_fee": self.schedule.reg_fee,
            "transfer_fee": self.schedule.transfer_fee,
        }


# ---------------------------------------------------------------------------
# 变体定义（SPEC §3.3）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class VariantSpec:
    """单个回测变体。

    cadence:
      hold / month_last / month_first / quarter_last / half_last / year_last / band
    cost_mode: normal | zero | mult2
    """

    name: str
    cadence: str
    band: Optional[float] = None
    cost_mode: str = "normal"

    def cost_model(self, schedule: CostSchedule) -> CostModel:
        mult = 2.0 if self.cost_mode == "mult2" else 1.0
        return CostModel(
            schedule,
            cost_mult=mult,
            zero=(self.cost_mode == "zero"),
            label=f"{self.name}:{self.cost_mode}",
        )


FULL_VARIANTS: Tuple[VariantSpec, ...] = (
    VariantSpec("hold", "hold"),
    VariantSpec("m_last", "month_last"),
    VariantSpec("m_first", "month_first"),
    VariantSpec("q", "quarter_last"),
    VariantSpec("s", "half_last"),
    VariantSpec("a", "year_last"),
    VariantSpec("band5", "band", band=0.05),
    VariantSpec("band10", "band", band=0.10),
    VariantSpec("m_last_nocost", "month_last", cost_mode="zero"),
    VariantSpec("m_last_cost2", "month_last", cost_mode="mult2"),
)

RANDOM_VARIANTS: Tuple[VariantSpec, ...] = (
    VariantSpec("hold", "hold"),
    VariantSpec("m_last", "month_last"),
    VariantSpec("q", "quarter_last"),
    VariantSpec("a", "year_last"),
    VariantSpec("band5", "band", band=0.05),
)

FULL_VARIANT_NAMES: Tuple[str, ...] = tuple(v.name for v in FULL_VARIANTS)
RANDOM_VARIANT_NAMES: Tuple[str, ...] = tuple(v.name for v in RANDOM_VARIANTS)


# ---------------------------------------------------------------------------
# 数据
# ---------------------------------------------------------------------------
def load_market_data(
    data_dir: Path | str = DEFAULT_DATA_DIR,
    prices_path: Path | str | None = None,
) -> "MarketData":
    """读取 data-fetch 产出的 CSV 并返回 MarketData。

    价格文件（SPEC §8）：默认 ``data_dir/close_tr_wide.csv``（TR 口径，SHA256 前缀
    ``4d59e9d5487d312c…``）；可用 ``prices_path`` 指向其他文件（相对路径按
    ``data_dir`` 解析）。交易日历固定为 ``data_dir/index_000300.csv``。
    """
    data_dir = Path(data_dir)
    cal_path = data_dir / CALENDAR_FILENAME
    if prices_path is None:
        wide_path = data_dir / DEFAULT_PRICES_FILENAME
    else:
        wide_path = Path(prices_path)
        if not wide_path.is_absolute():
            wide_path = data_dir / wide_path
    for path in (wide_path, cal_path):
        if not path.exists():
            raise EngineError(f"数据文件尚未就绪：{path}")

    wide = pd.read_csv(wide_path)
    if "date" not in wide.columns:
        raise EngineError(f"{wide_path} 缺少 date 列")
    wide["date"] = pd.to_datetime(wide["date"])
    if wide["date"].duplicated().any():
        raise EngineError(f"{wide_path} 存在重复 date")
    wide = wide.sort_values("date").set_index("date")
    wide = wide.apply(pd.to_numeric, errors="coerce")

    cal = pd.read_csv(cal_path)
    if "date" not in cal.columns:
        raise EngineError(f"{cal_path} 缺少 date 列")
    cal["date"] = pd.to_datetime(cal["date"])
    calendar = pd.DatetimeIndex(sorted(cal["date"].dropna().unique()))
    if len(calendar) < 2:
        raise EngineError(f"{cal_path} 交易日历过短")

    # 以 000300 日历为全集对齐；宽表中多余的日期丢弃，缺失日期留 NaN
    raw = wide.reindex(calendar)
    raw.index.name = "date"
    return MarketData(calendar=calendar, raw=raw, source_paths=[wide_path, cal_path])


@dataclass
class MarketData:
    """宽表原始（未 ffill）+ 交易日历。"""

    calendar: pd.DatetimeIndex
    raw: pd.DataFrame  # index=calendar, columns=内部代码, value=TR 收盘价（NaN=停牌/缺失）
    source_paths: List[Path] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._masks: Dict[str, np.ndarray] = build_calendar_masks(self.calendar)

    @property
    def symbols(self) -> List[str]:
        return list(self.raw.columns)

    # -- 篮子取样 ---------------------------------------------------------
    def first_common_date(self, symbols: Sequence[str], start_date: str | pd.Timestamp) -> pd.Timestamp:
        """返回 >= start_date 的首个"所有 symbol 都有原始价"的交易日。"""
        missing = [s for s in symbols if s not in self.raw.columns]
        if missing:
            raise EngineError(f"宽表缺少标的：{missing}")
        start = pd.Timestamp(start_date)
        sub = self.raw.loc[self.raw.index >= start, list(symbols)]
        if len(sub) == 0:
            raise EngineError(f"起点 {start_date} 之后没有交易日")
        ok = sub.notna().all(axis=1).to_numpy()
        if not ok.any():
            raise EngineError(f"篮子 {list(symbols)} 在 {start_date} 之后无共同可得价日")
        return pd.Timestamp(sub.index[ok][0])

    def slice_prices(
        self, symbols: Sequence[str], start_date: str | pd.Timestamp
    ) -> Tuple[pd.DatetimeIndex, np.ndarray]:
        """返回 [start:] 的日期与 ffill 后的价格矩阵（T×N，float64）。"""
        start = self.first_common_date(symbols, start_date)
        i0 = int(self.calendar.get_loc(start))
        dates = self.calendar[i0:]
        mat = self.raw.loc[dates, list(symbols)].ffill()
        prices = mat.to_numpy(dtype=np.float64)
        if prices.shape[0] < 2:
            raise EngineError("样本期过短（<2 个交易日）")
        if np.isnan(prices[0]).any():
            raise EngineError("起始日仍存在缺失价（不应发生：first_common_date 已校验）")
        if np.isnan(prices).any():
            raise EngineError("ffill 后仍存在缺失价，请检查数据契约")
        return dates, prices

    def masks_from(self, start_date: str | pd.Timestamp) -> Dict[str, np.ndarray]:
        """把全历法 trigger mask 裁到 [start:] 区间。"""
        start = pd.Timestamp(start_date)
        i0 = int(self.calendar.get_loc(start))
        return {k: v[i0:] for k, v in self._masks.items()}

    def price_availability(self, symbols: Sequence[str], start_date: str | pd.Timestamp) -> Dict[str, int]:
        """样本内每只标的原始非空价天数（用于随机池资格判断）。"""
        start = pd.Timestamp(start_date)
        sub = self.raw.loc[self.raw.index >= start, list(symbols)]
        return {s: int(sub[s].notna().sum()) for s in symbols}

    def random_eligible_pool(self, start_date: str, min_days: int = 2500) -> List[str]:
        """SPEC §3.5：起点有数据且样本内交易日 >= min_days 的池。"""
        start = pd.Timestamp(start_date)
        sub = self.raw.loc[self.raw.index >= start]
        if len(sub) == 0:
            raise EngineError(f"随机池起点 {start_date} 之后没有交易日")
        first_ok = sub.iloc[0].notna()
        counts = sub.notna().sum(axis=0)
        pool = [s for s in self.raw.columns if bool(first_ok[s]) and int(counts[s]) >= min_days]
        return sorted(pool)


def build_calendar_masks(calendar: pd.DatetimeIndex) -> Dict[str, np.ndarray]:
    """在交易日历上预计算各调仓频率的触发日。

    注意：**样本最后一日不视为月末/季末/年末**——样本终点是任意截断日（数据到
    2026-11-06），不应凭空触发一次调仓。因此 "月末" 定义为"下一交易日已换月"。

    返回 mask：month_first / month_last / quarter_last / half_last / year_last。
    """
    n = len(calendar)
    ym = calendar.year.to_numpy() * 100 + calendar.month.to_numpy()
    yq = calendar.year.to_numpy() * 4 + (calendar.month.to_numpy() - 1) // 3
    yh = calendar.year.to_numpy() * 2 + (calendar.month.to_numpy() - 1) // 6
    yr = calendar.year.to_numpy()

    masks = {k: np.zeros(n, dtype=bool) for k in (
        "month_first", "month_last", "quarter_last", "half_last", "year_last"
    )}
    for i in range(n):
        first = (i == 0) or (ym[i] != ym[i - 1])
        masks["month_first"][i] = first
        if i < n - 1:  # 样本最后一日不触发
            masks["month_last"][i] = ym[i] != ym[i + 1]
            masks["quarter_last"][i] = yq[i] != yq[i + 1]
            masks["half_last"][i] = yh[i] != yh[i + 1]
            masks["year_last"][i] = yr[i] != yr[i + 1]
    return masks


# ---------------------------------------------------------------------------
# 模拟
# ---------------------------------------------------------------------------
@dataclass
class Trade:
    t: int
    date: pd.Timestamp
    symbol: str
    side: str  # buy / sell
    shares: float
    price: float
    amount: float
    cost: float


@dataclass
class SimResult:
    variant: str
    dates: pd.DatetimeIndex
    navs: np.ndarray
    cash_min: float
    n_rebalances: int
    total_cost: float
    total_traded: float
    trades: List[Trade]
    initial_shares: np.ndarray
    final_shares: np.ndarray
    prices_last: np.ndarray
    cost_model: CostModel
    weights: Optional[np.ndarray] = None  # T×N，仅 keep_weights=True 时保存

    @property
    def n_trades(self) -> int:
        return len(self.trades)


def simulate(
    prices: np.ndarray,
    dates: pd.DatetimeIndex,
    variant: VariantSpec,
    cost_model: CostModel,
    init_cash: float,
    masks: Dict[str, np.ndarray],
    keep_weights: bool = False,
    keep_trades: bool = True,
) -> SimResult:
    """按 SPEC §3 推进单个变体的组合。

    严格按日历顺序推进；第 t 步只使用 dates[t] 及以前的收盘价（无未来函数）。
    """
    T, N = prices.shape
    if N <= 0 or T < 2:
        raise EngineError("价格矩阵形状非法")
    shares = np.zeros(N, dtype=np.float64)
    cash = float(init_cash)
    navs = np.empty(T, dtype=np.float64)
    cash_min = cash
    trades: List[Trade] = []
    n_rebalances = 0
    total_cost = 0.0
    total_traded = 0.0
    weights = np.empty((T, N), dtype=np.float64) if keep_weights else None
    initial_shares = np.zeros(N, dtype=np.float64)

    def _rebalance(t: int, nav: float) -> None:
        nonlocal cash, n_rebalances, total_cost, total_traded
        px = prices[t]
        target_value = nav / N
        target_shares = target_value / px
        excess = shares - target_shares  # >0 超配(卖)，<0 低配(买)

        # 1) 先卖：把超配份额卖到 target
        for i in range(N):
            if excess[i] > 0.0:
                sh = float(excess[i])
                amount = sh * px[i]
                cost = cost_model.sell_cost(amount)
                shares[i] -= sh
                cash += amount - cost
                total_cost += cost
                total_traded += amount
                if keep_trades:
                    trades.append(Trade(t, pd.Timestamp(dates[t]), str(prices_symbols[i]), "sell",
                                        sh, float(px[i]), float(amount), float(cost)))
        # 2) 再买：按目标缺口买入，总额（含费）不超过当前现金
        deficits = np.maximum(-excess, 0.0) * px
        need = float(deficits.sum())
        if need > 0.0:
            scale = _solve_buy_scale(deficits, max(cash, 0.0), cost_model.buy_cost)
            for i in range(N):
                amount = scale * deficits[i]
                if amount <= 0.0:
                    continue
                cost = cost_model.buy_cost(amount)
                if amount + cost > cash + FLOAT_EPS:
                    # 浮点护栏：按现金上限截断（正常不会触发）
                    amount = max(cash - cost, 0.0)
                    if amount <= 0.0:
                        continue
                    cost = cost_model.buy_cost(amount)
                sh = amount / px[i]
                shares[i] += sh
                cash -= amount + cost
                total_cost += cost
                total_traded += amount
                if keep_trades:
                    trades.append(Trade(t, pd.Timestamp(dates[t]), str(prices_symbols[i]), "buy",
                                        float(sh), float(px[i]), float(amount), float(cost)))
        n_rebalances += 1

    prices_symbols = list(getattr(prices, "symbols", []))
    if not prices_symbols:
        # simulate 也可以直接收到裸 ndarray；用占位名（调用方会传入带 symbols 的数组）
        prices_symbols = [f"col{j}" for j in range(N)]

    for t in range(T):
        if t == 0:
            # 初始建仓：1/N 等权、成本为 0（SPEC §3.7 断言 1）
            per = init_cash / N
            shares[:] = per / prices[0]
            cash = init_cash - float(np.sum(np.full(N, per)))
            initial_shares = shares.copy()
        else:
            nav_pre = cash + float(np.dot(shares, prices[t]))
            should = False
            if variant.cadence == "hold":
                should = False
            elif variant.cadence == "band":
                w = shares * prices[t] / nav_pre if nav_pre > 0 else np.zeros(N)
                should = bool(masks["month_last"][t]) and float(np.max(np.abs(w - 1.0 / N))) >= float(variant.band)
            elif variant.cadence == "month_last":
                should = bool(masks["month_last"][t])
            elif variant.cadence == "month_first":
                should = bool(masks["month_first"][t])
            elif variant.cadence == "quarter_last":
                should = bool(masks["quarter_last"][t])
            elif variant.cadence == "half_last":
                should = bool(masks["half_last"][t])
            elif variant.cadence == "year_last":
                should = bool(masks["year_last"][t])
            else:  # pragma: no cover
                raise EngineError(f"未知 cadence: {variant.cadence}")
            if should:
                _rebalance(t, nav_pre)
        nav = cash + float(np.dot(shares, prices[t]))
        navs[t] = nav
        if cash < cash_min:
            cash_min = cash
        if weights is not None:
            weights[t] = shares * prices[t] / nav if nav > 0 else 0.0

    return SimResult(
        variant=variant.name,
        dates=dates,
        navs=navs,
        cash_min=float(cash_min),
        n_rebalances=int(n_rebalances),
        total_cost=float(total_cost),
        total_traded=float(total_traded),
        trades=trades,
        initial_shares=initial_shares,
        final_shares=shares.copy(),
        prices_last=prices[T - 1].copy(),
        cost_model=cost_model,
        weights=weights,
    )


def _solve_buy_scale(deficits: np.ndarray, cash: float, buy_cost) -> float:
    """求 s ∈ [0,1] 使 Σ_i(s*d_i + buy_cost(s*d_i)) <= cash，二分求解。"""
    def spend(s: float) -> float:
        total = 0.0
        for d in deficits:
            if d <= 0.0:
                continue
            amount = s * float(d)
            total += amount + buy_cost(amount)
        return total

    if spend(1.0) <= cash + FLOAT_EPS:
        return 1.0
    lo, hi = 0.0, 1.0
    for _ in range(40):
        mid = 0.5 * (lo + hi)
        if spend(mid) <= cash:
            lo = mid
        else:
            hi = mid
    return lo


# ---------------------------------------------------------------------------
# 指标（SPEC §3.4）
# ---------------------------------------------------------------------------
def monthly_returns(navs: np.ndarray, dates: pd.DatetimeIndex) -> pd.Series:
    """自然月收益：月末净值 / 上月末净值 - 1；首月用 t0 净值作基期。"""
    series = pd.Series(navs, index=pd.DatetimeIndex(dates))
    month_last = series.groupby(series.index.to_period("M")).last()
    rets = month_last / month_last.shift(1) - 1.0
    rets.iloc[0] = month_last.iloc[0] / series.iloc[0] - 1.0
    return rets


def _t_p(diff: np.ndarray) -> Tuple[float, float]:
    """单样本 t 检验 H0: mean=0。返回 (t, p)；样本不足或零方差返回 NaN。"""
    d = np.asarray(diff, dtype=float)
    n = d.size
    if n < 2:
        return float("nan"), float("nan")
    mean = float(d.mean())
    sd = float(d.std(ddof=1))
    if not math.isfinite(sd) or sd <= 0.0:
        return float("nan"), float("nan")
    t = mean / (sd / math.sqrt(n))
    p = float(2.0 * stats.t.sf(abs(t), n - 1))
    return float(t), float(p)


def compute_metrics(
    sim: SimResult,
    hold_sim: SimResult,
    init_cash: float,
) -> Dict[str, float]:
    """单个变体的 SPEC §3.4 指标。"""
    navs = np.asarray(sim.navs, dtype=float)
    dates = pd.DatetimeIndex(sim.dates)
    if len(navs) < 2:
        raise EngineError("净值序列过短")
    years = (dates[-1] - dates[0]).days / CALENDAR_DAYS_PER_YEAR
    years = max(years, 1e-6)
    nav_end = float(navs[-1])
    cagr = (nav_end / float(init_cash)) ** (1.0 / years) - 1.0

    daily = navs[1:] / navs[:-1] - 1.0
    vol = float(daily.std(ddof=1)) * math.sqrt(TRADING_DAYS_PER_YEAR) if daily.size >= 2 else float("nan")
    sharpe = (
        float(daily.mean()) / float(daily.std(ddof=1)) * math.sqrt(TRADING_DAYS_PER_YEAR)
        if daily.size >= 2 and daily.std(ddof=1) > 0
        else float("nan")
    )

    running_max = np.maximum.accumulate(navs)
    drawdown = navs / running_max - 1.0
    mdd = float(drawdown.min())  # 负数：-0.30 表示最大回撤 30%
    calmar = cagr / abs(mdd) if mdd < 0 else float("nan")

    mean_equity = float(navs.mean())
    turnover = sim.total_traded / mean_equity / years
    cost_pct = sim.total_cost / mean_equity / years
    avg_reb_cost = sim.total_cost / sim.n_rebalances if sim.n_rebalances > 0 else 0.0
    n_trades = len(sim.trades)
    n_rebalances = sim.n_rebalances

    # 与 hold 的月度收益差
    var_m = monthly_returns(navs, dates)
    hold_m = monthly_returns(np.asarray(hold_sim.navs, dtype=float), pd.DatetimeIndex(hold_sim.dates))
    hold_m = hold_m.reindex(var_m.index)
    diff = (var_m - hold_m).dropna()
    diff_mean = float(diff.mean()) if diff.size else float("nan")
    t, p = _t_p(diff.to_numpy())
    win_rate = float((diff > 0).mean()) if diff.size else float("nan")

    return {
        "variant": sim.variant,
        "start_date": str(dates[0].date()),
        "end_date": str(dates[-1].date()),
        "years": float(years),
        "nav_end": float(nav_end),
        "cagr": float(cagr),
        "vol_annual": float(vol),
        "sharpe": float(sharpe),
        "mdd": float(mdd),
        "calmar": float(calmar),
        "turnover_annual_two_way": float(turnover),
        "cost_pct_annual": float(cost_pct),
        "n_rebalances": int(n_rebalances),
        "n_trades": int(n_trades),
        "avg_rebalance_cost": float(avg_reb_cost),
        "monthly_diff_mean": float(diff_mean),
        "monthly_diff_t": float(t),
        "monthly_diff_p": float(p),
        "monthly_win_rate": float(win_rate),
        # 便于复核的附加列
        "total_cost": float(sim.total_cost),
        "total_turnover": float(sim.total_traded),
        "cash_min": float(sim.cash_min),
    }


def add_premiums(metrics: Dict[str, Dict[str, float]]) -> Dict[str, Dict[str, float]]:
    """给每个变体补 premium_*_vs_hold（变体 - hold；mdd 为负数，>0=回撤更浅=更好）。"""
    hold = metrics["hold"]
    for name, row in metrics.items():
        row["premium_cagr_vs_hold"] = row["cagr"] - hold["cagr"]
        row["premium_sharpe_vs_hold"] = row["sharpe"] - hold["sharpe"]
        row["premium_mdd_vs_hold"] = row["mdd"] - hold["mdd"]
    return metrics


# ---------------------------------------------------------------------------
# 篮子
# ---------------------------------------------------------------------------
@dataclass
class BasketResult:
    basket_id: str
    symbols: List[str]
    start_date: pd.Timestamp
    end_date: pd.Timestamp
    dates: pd.DatetimeIndex
    sims: Dict[str, SimResult]
    metrics: Dict[str, Dict[str, float]]
    prices: np.ndarray


def run_basket(
    market: MarketData,
    basket_id: str,
    symbols: Sequence[str],
    start_date: str,
    variants: Sequence[VariantSpec] = FULL_VARIANTS,
    cost_schedule: Optional[CostSchedule] = None,
    init_cash: float = SPEC_INIT_CASH,
    keep_weights_for: Sequence[str] = ("hold",),
    keep_trades: bool = True,
) -> BasketResult:
    """跑一个篮子 × 全部变体（同一 start_date、同一初始 1/N 组合）。"""
    if cost_schedule is None:
        cost_schedule = CostSchedule.from_strategy_yaml()
    symbols = list(symbols)
    dates, prices = market.slice_prices(symbols, start_date)
    actual_start = dates[0]
    masks = market.masks_from(actual_start)
    # 给裸 ndarray 挂上列名（Trade.symbol 需要）
    prices_named = _NamedPrices(prices, symbols)

    sims: Dict[str, SimResult] = {}
    for spec in variants:
        model = spec.cost_model(cost_schedule)
        sims[spec.name] = simulate(
            prices_named,
            dates,
            spec,
            model,
            init_cash,
            masks,
            keep_weights=(spec.name in keep_weights_for),
            keep_trades=keep_trades,
        )

    metrics = {name: compute_metrics(sim, sims["hold"], init_cash) for name, sim in sims.items()}
    metrics = add_premiums(metrics)

    return BasketResult(
        basket_id=basket_id,
        symbols=symbols,
        start_date=pd.Timestamp(actual_start),
        end_date=pd.Timestamp(dates[-1]),
        dates=dates,
        sims=sims,
        metrics=metrics,
        prices=prices,
    )


class _NamedPrices(np.ndarray):
    """ndarray 子类，携带 symbols 属性供 Trade 记录标的代码。"""

    def __new__(cls, data: np.ndarray, symbols: Sequence[str]):
        obj = np.asarray(data, dtype=np.float64).view(cls)
        obj.symbols = list(symbols)
        return obj

    def __array_finalize__(self, obj) -> None:
        if obj is None:
            return
        self.symbols = getattr(obj, "symbols", None)


# ---------------------------------------------------------------------------
# 权重漂移（Lead 追加需求）
# ---------------------------------------------------------------------------
def weight_drift(
    weights: np.ndarray,
    dates: pd.DatetimeIndex,
    symbols: Sequence[str],
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """hold 变体的权重漂移证据。

    返回：
      per_symbol: symbol,start_weight,end_weight,max_weight,max_weight_date,
                  min_weight,pct_days_over_15,pct_days_over_20
      timeseries: date,max_weight,min_weight,argmax_symbol（组合内单只权重极值）
    """
    w = np.asarray(weights, dtype=float)
    T, N = w.shape
    if T < 2:
        raise EngineError("权重序列过短")
    per_rows = []
    for i, sym in enumerate(symbols):
        wi = w[:, i]
        j = int(np.argmax(wi))
        per_rows.append(
            {
                "symbol": sym,
                "start_weight": float(wi[0]),
                "end_weight": float(wi[-1]),
                "max_weight": float(wi[j]),
                "max_weight_date": str(pd.Timestamp(dates[j]).date()),
                "min_weight": float(wi.min()),
                "pct_days_over_15": float(np.mean(wi > 0.15)),
                "pct_days_over_20": float(np.mean(wi > 0.20)),
            }
        )
    per_symbol = pd.DataFrame(per_rows)

    max_w = w.max(axis=1)
    min_w = w.min(axis=1)
    argmax = np.array(symbols, dtype=object)[w.argmax(axis=1)]
    ts = pd.DataFrame(
        {
            "date": [str(pd.Timestamp(d).date()) for d in dates],
            "max_weight": max_w,
            "min_weight": min_w,
            "argmax_symbol": argmax,
        }
    )
    return per_symbol, ts


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def sha256_file(path: Path | str) -> str:
    path = Path(path)
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def param_hash(material: dict) -> str:
    blob = json.dumps(material, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
