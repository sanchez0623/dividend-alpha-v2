"""分红捕获策略（DC）纯逻辑 —— 回测与生产共用，禁止在两边各写一套。

用户规则（冻结）：
  - 股息率 TTM >= 6%；总市值 >= 500 亿；非下跌趋势（优先上涨+回调）
  - 除权前 5 个交易日买入；持有到除权后 30 天卖出；同一时间只持有一只
  - 除权日拿现金分红；卖出时按持有天数扣红利税；再提取净分红的 30%；
    剩余资金滚入下一只股票

本模块只做「评估/排序/日期/结算」的纯计算，不碰数据库、网络、账户。
"""
from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import Iterable, Sequence

import pandas as pd

from ..da_core.dc_config import DcStrategy
from ..da_data.adjust import compute_drop, compute_rsi

# 趋势优先级：上涨+回调 > 上涨 > 非下跌 > 下跌 > 数据不足
TREND_PRIORITY: dict[str, int] = {
    "up_pull": 3,
    "up": 2,
    "nondown": 1,
    "down": 0,
    "insufficient": -1,
}


@dataclass(frozen=True)
class DcRules:
    """从 DcStrategy 解出的、可直接传给纯函数的参数。"""

    min_market_cap: float
    min_div_yield_ttm: float
    entry_lead_trading_days: int
    hold_days_after_ex: int
    max_concurrent_positions: int
    trend_enabled: bool
    trend_mode: str
    ma_short: int
    ma_mid: int
    ma_long: int
    rsi_period: int
    rsi_min: float
    rsi_max: float
    drop_20d_min: float
    drop_20d_max: float
    tax_rate_short: float
    tax_rate_mid: float
    tax_rate_long: float
    withdraw_ratio: float
    withdraw_base: str
    lot_size: int
    cash_buffer: float

    @classmethod
    def from_strategy(cls, dc: DcStrategy) -> "DcRules":
        u, s, t, d, e = (dc.universe, dc.schedule, dc.trend,
                         dc.dividend, dc.execution)
        return cls(
            min_market_cap=float(u["min_market_cap"]),
            min_div_yield_ttm=float(u["min_div_yield_ttm"]),
            entry_lead_trading_days=int(s["entry_lead_trading_days"]),
            hold_days_after_ex=int(s["hold_days_after_ex"]),
            max_concurrent_positions=int(s["max_concurrent_positions"]),
            trend_enabled=bool(t["enabled"]),
            trend_mode=str(t["mode"]),
            ma_short=int(t["ma_short"]),
            ma_mid=int(t["ma_mid"]),
            ma_long=int(t["ma_long"]),
            rsi_period=int(t["rsi_period"]),
            rsi_min=float(t["rsi_min"]),
            rsi_max=float(t["rsi_max"]),
            drop_20d_min=float(t["drop_20d_min"]),
            drop_20d_max=float(t["drop_20d_max"]),
            tax_rate_short=float(d.get("tax_rate_short", 0.20)),
            tax_rate_mid=float(d.get("tax_rate_mid", 0.10)),
            tax_rate_long=float(d.get("tax_rate_long", 0.0)),
            withdraw_ratio=float(d.get("withdraw_ratio", 0.30)),
            withdraw_base=str(d.get("withdraw_base", "net")),
            lot_size=int(e.get("lot_size", 100)),
            cash_buffer=float(e.get("cash_buffer", 0.005)),
        )


@dataclass(frozen=True)
class TrendState:
    label: str                     # up_pull | up | nondown | down | insufficient
    ma_short: float | None = None
    ma_mid: float | None = None
    ma_long: float | None = None
    rsi: float | None = None
    drop_20d: float | None = None


@dataclass(frozen=True)
class DcCandidate:
    symbol: str
    name: str | None
    ex_date: date
    entry_date: date
    exit_date: date
    dps: float
    ttm_dps: float
    price: float
    div_yield: float
    market_cap: float
    trend: TrendState
    eligible: bool
    reason: str = ""
    rank: int = 0


@dataclass(frozen=True)
class CycleSettlement:
    holding_days: int
    gross_dividend: float
    tax: float
    net_dividend: float
    withdrawn: float
    net_pnl: float
    return_pct: float


# ---------- 日期 ----------
def entry_date_for(ex_date: date, calendar: Sequence[date], lead: int) -> date | None:
    """除权日前第 lead 个交易日；不足 lead 天返回 None。"""
    if not calendar or lead < 0:
        return None
    idx = bisect_left(calendar, ex_date)
    if idx >= len(calendar):
        return None
    j = idx - lead
    if j < 0:
        return None
    return calendar[j]


def exit_date_for(ex_date: date, calendar: Sequence[date], hold_days: int) -> date | None:
    """除权日 + hold_days 自然日后的首个交易日。"""
    if not calendar:
        return None
    target = ex_date + timedelta(days=int(hold_days))
    idx = bisect_left(calendar, target)
    if idx >= len(calendar):
        return None
    return calendar[idx]


def trading_day_on_or_after(calendar: Sequence[date], d: date) -> date | None:
    if not calendar:
        return None
    idx = bisect_left(calendar, d)
    return calendar[idx] if idx < len(calendar) else None


# ---------- 趋势 ----------
def compute_trend(adj_series: pd.Series | None, asof: date,
                  rules: DcRules) -> TrendState:
    """用前复权序列计算 MA/RSI/20 日跌幅；不足 ma_long 根返回 insufficient。"""
    if adj_series is None or len(adj_series) == 0:
        return TrendState("insufficient")
    s = adj_series.copy()
    if not isinstance(s.index, pd.DatetimeIndex):
        s.index = pd.to_datetime(s.index)
    s = s[s.index <= pd.Timestamp(asof)].dropna().sort_index()
    n = len(s)
    if n < rules.ma_long:
        return TrendState("insufficient")
    last = float(s.iloc[-1])
    ma_s = float(s.iloc[-rules.ma_short:].mean())
    ma_m = float(s.iloc[-rules.ma_mid:].mean())
    ma_l = float(s.iloc[-rules.ma_long:].mean())
    rsi_v = compute_rsi(s, rules.rsi_period).iloc[-1]
    drop_v = compute_drop(s, 20).iloc[-1]
    rsi = None if rsi_v is None or pd.isna(rsi_v) else float(rsi_v)
    drop = None if drop_v is None or pd.isna(drop_v) else float(drop_v)

    if last <= ma_l:
        label = "down"
    elif last > ma_m and ma_m > ma_l:
        if (rsi is not None and rules.rsi_min <= rsi <= rules.rsi_max
                and drop is not None
                and rules.drop_20d_min <= drop <= rules.drop_20d_max):
            label = "up_pull"
        else:
            label = "up"
    else:
        label = "nondown"
    return TrendState(label, ma_s, ma_m, ma_l, rsi, drop)


def trend_eligible(state: TrendState, rules: DcRules) -> bool:
    if not rules.trend_enabled:
        return True
    mode = rules.trend_mode
    if mode == "nondown":
        return state.label in ("nondown", "up", "up_pull")
    if mode == "up":
        return state.label in ("up", "up_pull")
    if mode == "up_pull":
        return state.label == "up_pull"
    return False


# ---------- 候选 ----------
def ttm_dividend(events: Iterable[tuple[date, float]], asof: date) -> float:
    """[asof-365, asof] 内每股现金分红合计（含 asof 当天事件）。"""
    start = asof - timedelta(days=365)
    return float(sum(dps for ex, dps in events if start < ex <= asof))


def evaluate_candidate(
    *,
    symbol: str,
    name: str | None,
    ex_date: date,
    dps: float,
    ttm_dps: float,
    market_cap: float,
    calendar: Sequence[date],
    raw_price: float | None,
    adj_series: pd.Series | None,
    asof: date,
    rules: DcRules,
) -> DcCandidate | None:
    """评估单只候选；买入日/卖出日/价格任一缺失返回 None。"""
    entry = entry_date_for(ex_date, calendar, rules.entry_lead_trading_days)
    exit_ = exit_date_for(ex_date, calendar, rules.hold_days_after_ex)
    if entry is None or exit_ is None or raw_price is None or raw_price <= 0:
        return None
    trend_asof = asof if asof <= entry else entry
    trend = compute_trend(adj_series, trend_asof, rules)
    y = float(ttm_dps) / float(raw_price)
    ok_y = y >= rules.min_div_yield_ttm
    ok_cap = float(market_cap) >= rules.min_market_cap
    ok_trend = trend_eligible(trend, rules)
    reason = "ok"
    if not ok_y:
        reason = f"yield {y:.2%} < {rules.min_div_yield_ttm:.0%}"
    elif not ok_cap:
        reason = f"cap {market_cap / 1e8:.0f}亿 < {rules.min_market_cap / 1e8:.0f}亿"
    elif not ok_trend:
        reason = f"trend {trend.label}"
    return DcCandidate(
        symbol=symbol,
        name=name,
        ex_date=ex_date,
        entry_date=entry,
        exit_date=exit_,
        dps=float(dps),
        ttm_dps=float(ttm_dps),
        price=float(raw_price),
        div_yield=y,
        market_cap=float(market_cap),
        trend=trend,
        eligible=ok_y and ok_cap and ok_trend,
        reason=reason,
    )


def rank_candidates(cands: Iterable[DcCandidate]) -> list[DcCandidate]:
    """只保留合格候选，按 趋势优先级 → 股息率降序 → 市值降序 → 代码 排序。"""
    out = [c for c in cands if c.eligible]
    out.sort(key=lambda c: (
        -TREND_PRIORITY.get(c.trend.label, -1),
        -c.div_yield,
        -c.market_cap,
        c.symbol,
    ))
    return [replace(c, rank=i + 1) for i, c in enumerate(out)]


# ---------- 结算 ----------
def dividend_tax_for(holding_days: int, gross: float, rules: DcRules) -> float:
    """A 股红利税档位：<=30 天 20% / 31~365 天 10% / >365 天 0%。"""
    if holding_days <= 30:
        rate = rules.tax_rate_short
    elif holding_days <= 365:
        rate = rules.tax_rate_mid
    else:
        rate = rules.tax_rate_long
    return float(gross) * float(rate)


def settle_cycle(
    *,
    entry_date: date,
    exit_date: date,
    shares: float,
    buy_price: float,
    buy_cost: float,
    sell_price: float,
    sell_cost: float,
    gross_dividend: float,
    rules: DcRules,
) -> CycleSettlement:
    """卖出时结算：持有天数、红利税、30% 取现、净盈亏/收益率。"""
    days = (exit_date - entry_date).days
    tax = dividend_tax_for(days, gross_dividend, rules)
    net_div = float(gross_dividend) - tax
    base = net_div if rules.withdraw_base == "net" else float(gross_dividend)
    withdrawn = max(base, 0.0) * rules.withdraw_ratio
    buy_amount = float(shares) * float(buy_price)
    sell_amount = float(shares) * float(sell_price)
    net_pnl = (sell_amount - float(sell_cost) + float(gross_dividend)
               - tax - withdrawn - buy_amount - float(buy_cost))
    return CycleSettlement(
        holding_days=days,
        gross_dividend=float(gross_dividend),
        tax=tax,
        net_dividend=net_div,
        withdrawn=withdrawn,
        net_pnl=net_pnl,
        return_pct=(net_pnl / buy_amount) if buy_amount > 0 else 0.0,
    )



