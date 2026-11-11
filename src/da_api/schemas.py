"""API Schema：数值统一带 data_class（实时/演示/估算/归档）。"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

DataClass = Literal["实时", "演示", "估算", "归档"]


class NumberWithClass(BaseModel):
    value: float | None
    data_class: DataClass
    unit: str | None = None


class ProgressItem(BaseModel):
    label: str
    value: float
    target: float | None
    fmt: str | None = None


class OverviewResponse(BaseModel):
    data_date: str
    mode: str
    init_cash: NumberWithClass
    total_equity: NumberWithClass
    cash: NumberWithClass
    positions_value: NumberWithClass
    daily_pnl: NumberWithClass          # 当日盈亏 = S* 最新净值 − 上一交易日
    unrealized_total: NumberWithClass   # 持仓浮盈合计（元）
    position_count: NumberWithClass     # 持仓只数（真实持仓表计数）
    running_days: NumberWithClass
    progress: list[ProgressItem]
    benchmark_hint: str


class PositionRow(BaseModel):
    symbol: str
    name: str | None
    industry_l1: str | None
    bank_subtype: str | None
    div_yield_ttm: NumberWithClass
    div_yield_prev: NumberWithClass = NumberWithClass(value=None, data_class="实时", unit="%")  # 上一交易日股息率（环比箭头用）
    market_value: NumberWithClass
    cost_price: NumberWithClass
    unrealized_gain: NumberWithClass
    unrealized_pnl: NumberWithClass          # 浮盈金额（元）
    holding_days: NumberWithClass
    shares_available: NumberWithClass
    status: str


class SignalRow(BaseModel):
    trade_date: str
    symbol: str
    drop_20d: NumberWithClass | None
    rsi_14: NumberWithClass | None
    strength: NumberWithClass | None
    expected_net: NumberWithClass | None
    cost_gate: str
    status: str
    price_basis: str = "ADJ"


class AttributionResponse(BaseModel):
    running_days: NumberWithClass
    modules: list[dict]
    cashflow: dict[str, NumberWithClass]


class BenchmarksResponse(BaseModel):
    mode: str
    running_days: int
    paired_trade_count: int
    need_days: int
    need_trades: int
    annual_unlocked: bool
    progress: list[ProgressItem]
    benchmarks: list[dict]
    # 年化超额字段仅在解锁时由服务层填充；锁定时严格不出现


class WithdrawResponse(BaseModel):
    year: int
    ytd_gross: NumberWithClass
    planned_withdraw: NumberWithClass
    next_withdraw_date: str
    rule: dict
    history: list[dict]


class ConfigResponse(BaseModel):
    strategy_yaml: str
    config_hash: str
    data_sources: list[dict]
    limitations: list[str]
    baseline_check: str


class ArchiveResponse(BaseModel):
    kpi: dict[str, NumberWithClass]
    months: list[str]
    s: list[float]
    b2: list[float]
    hs300: list[float]
    yearly: list[dict]
    modules: list[dict]
    withdraw: list[dict]
    data_class: DataClass = "归档"
    disclaimer: str


# ---------- 分红捕获（DC） ----------
class DcOverviewResponse(BaseModel):
    data_date: str
    strategy_id: str
    account_id: str
    init_cash: NumberWithClass
    total_equity: NumberWithClass
    cash: NumberWithClass
    positions_value: NumberWithClass
    daily_pnl: NumberWithClass
    position_count: NumberWithClass
    running_days: NumberWithClass
    dividend_gross: NumberWithClass
    dividend_net: NumberWithClass
    withdrawn: NumberWithClass
    captures: NumberWithClass
    holding: dict | None = None
    next_action: dict | None = None
    backtest_available: bool = False


class DcCycleRow(BaseModel):
    cycle_id: str
    symbol: str
    name: str | None = None
    ex_date: str
    status: str
    planned_entry_date: str | None = None
    planned_exit_date: str | None = None
    entry_date: str | None = None
    exit_date: str | None = None
    shares: float
    buy_price: float
    sell_price: float | None = None
    dps: float
    gross_dividend: float
    tax: float
    withdrawn: float
    net_dividend: float
    net_pnl: float | None = None
    return_pct: float | None = None
    reason: str | None = None


class DcCandidateRow(BaseModel):
    symbol: str
    name: str | None = None
    ex_date: str
    entry_date: str
    exit_date: str
    dps: float
    ttm_dps: float
    price: float
    div_yield: NumberWithClass
    market_cap: NumberWithClass
    trend: str
    eligible: bool
    reason: str
    rank: int = 0


class DcNavResponse(BaseModel):
    dates: list[str]
    s: list[float | None]
    dc: list[float | None]
    b2: list[float | None]


class DcBacktestResponse(BaseModel):
    available: bool
    data_class: DataClass = "归档"
    as_of: str | None = None
    universe: str | None = None
    start: str | None = None
    end: str | None = None
    params: dict | None = None
    metrics: dict | None = None
    yearly: list[dict] = []
    variants: list[dict] = []
    caveats: list[str] = []
    disclaimer: str = ""
    raw: dict | None = None
