"""SQLAlchemy ORM 模型（对齐 05_DEVELOPMENT.md §2 DDL）。

生产为 PostgreSQL 16 + TimescaleDB；本地/测试可用 SQLite。两列口径并存：
raw_close（不复权，股息率用）/ adj_close（前复权，RSI/跌幅用）。
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class PriceDaily(Base):
    __tablename__ = "price_daily"
    symbol: Mapped[str] = mapped_column(String, primary_key=True)
    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    open: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    high: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    low: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    raw_close: Mapped[float] = mapped_column(Numeric, nullable=False)
    adj_factor: Mapped[float] = mapped_column(Numeric, nullable=False, default=1)
    adj_close: Mapped[float] = mapped_column(Numeric, nullable=False)
    amount: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    source: Mapped[str | None] = mapped_column(String, nullable=True)


class CorpAction(Base):
    __tablename__ = "corp_action"
    symbol: Mapped[str] = mapped_column(String, primary_key=True)
    ex_date: Mapped[date] = mapped_column(Date, primary_key=True)
    announce_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    pay_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    dps: Mapped[float] = mapped_column(Numeric, nullable=False, default=0)
    bonus_ratio: Mapped[float] = mapped_column(Numeric, nullable=False, default=0)
    transfer_ratio: Mapped[float] = mapped_column(Numeric, nullable=False, default=0)
    rights_ratio: Mapped[float] = mapped_column(Numeric, nullable=False, default=0)
    rights_price: Mapped[float] = mapped_column(Numeric, nullable=False, default=0)


class SymbolStatus(Base):
    __tablename__ = "symbol_status"
    symbol: Mapped[str] = mapped_column(String, primary_key=True)
    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    name: Mapped[str | None] = mapped_column(String, nullable=True)  # 证券简称（东财）
    is_st: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_suspended: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    limit_up: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    limit_down: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    list_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    delist_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    market_cap: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    industry_l1: Mapped[str | None] = mapped_column(String, nullable=True)
    industry_l3: Mapped[str | None] = mapped_column(String, nullable=True)
    bank_subtype: Mapped[str | None] = mapped_column(String, nullable=True)


class FactorDaily(Base):
    __tablename__ = "factor_daily"
    symbol: Mapped[str] = mapped_column(String, primary_key=True)
    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    div_yield_ttm: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    consecutive_div_years: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rsi_14: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    drop_20d: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    config_hash: Mapped[str | None] = mapped_column(String, nullable=True)


class Account(Base):
    __tablename__ = "account"
    account_id: Mapped[str] = mapped_column(String, primary_key=True)
    cash: Mapped[float] = mapped_column(Numeric, nullable=False)
    total_equity: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    mode: Mapped[str] = mapped_column(String, nullable=False, default="PAPER")
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Position(Base):
    __tablename__ = "position"
    account_id: Mapped[str] = mapped_column(String, primary_key=True)
    symbol: Mapped[str] = mapped_column(String, primary_key=True)
    shares_total: Mapped[float] = mapped_column(Numeric, nullable=False, default=0)
    shares_locked: Mapped[float] = mapped_column(Numeric, nullable=False, default=0)
    shares_available: Mapped[float] = mapped_column(Numeric, nullable=False, default=0)
    cost_price: Mapped[float] = mapped_column(Numeric, nullable=False, default=0)
    buy_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    industry_l1: Mapped[str | None] = mapped_column(String, nullable=True)


class Lot(Base):
    __tablename__ = "lot"
    lot_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(String, nullable=False)
    symbol: Mapped[str] = mapped_column(String, nullable=False)
    buy_date: Mapped[date] = mapped_column(Date, nullable=False)
    shares: Mapped[float] = mapped_column(Numeric, nullable=False)
    price: Mapped[float] = mapped_column(Numeric, nullable=False)


class Order(Base):
    __tablename__ = "orders"
    order_id: Mapped[str] = mapped_column(String, primary_key=True)
    account_id: Mapped[str] = mapped_column(String, nullable=False)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    symbol: Mapped[str] = mapped_column(String, nullable=False)
    side: Mapped[str] = mapped_column(String, nullable=False)
    shares: Mapped[float] = mapped_column(Numeric, nullable=False)
    price: Mapped[float] = mapped_column(Numeric, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="NEW")


class Fill(Base):
    __tablename__ = "fills"
    fill_id: Mapped[str] = mapped_column(String, primary_key=True)
    order_id: Mapped[str] = mapped_column(String, nullable=False)
    account_id: Mapped[str] = mapped_column(String, nullable=False)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    symbol: Mapped[str] = mapped_column(String, nullable=False)
    side: Mapped[str] = mapped_column(String, nullable=False)
    shares: Mapped[float] = mapped_column(Numeric, nullable=False)
    price: Mapped[float] = mapped_column(Numeric, nullable=False)
    commission: Mapped[float] = mapped_column(Numeric, nullable=False, default=0)
    stamp_tax: Mapped[float] = mapped_column(Numeric, nullable=False, default=0)
    transfer_fee: Mapped[float] = mapped_column(Numeric, nullable=False, default=0)
    slippage: Mapped[float] = mapped_column(Numeric, nullable=False, default=0)
    handling_fee: Mapped[float] = mapped_column(Numeric, nullable=False, default=0)
    reg_fee: Mapped[float] = mapped_column(Numeric, nullable=False, default=0)


class DividendEvent(Base):
    __tablename__ = "dividend_event"
    event_id: Mapped[str] = mapped_column(String, primary_key=True)
    account_id: Mapped[str] = mapped_column(String, nullable=False)
    symbol: Mapped[str] = mapped_column(String, nullable=False)
    ex_date: Mapped[date] = mapped_column(Date, nullable=False)
    pay_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    dps_gross: Mapped[float] = mapped_column(Numeric, nullable=False)
    tax_withheld: Mapped[float] = mapped_column(Numeric, nullable=False, default=0)
    net_cash: Mapped[float] = mapped_column(Numeric, nullable=False, default=0)


class WithdrawLog(Base):
    __tablename__ = "withdraw_log"
    year: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[str] = mapped_column(String, primary_key=True)
    want: Mapped[float] = mapped_column(Numeric, nullable=False)
    got: Mapped[float] = mapped_column(Numeric, nullable=False)
    rate: Mapped[float] = mapped_column(Numeric, nullable=False)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False)


class DailyNav(Base):
    __tablename__ = "daily_nav"
    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    strategy_id: Mapped[str] = mapped_column(String, primary_key=True)
    account_id: Mapped[str] = mapped_column(String, primary_key=True)
    nav: Mapped[float] = mapped_column(Numeric, nullable=False)
    running_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    paired_trade_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class UniverseCache(Base):
    """全市场漏斗短名单缓存（月度重建；每日 factor 覆盖用它）。"""
    __tablename__ = "universe_cache"
    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    symbols_json: Mapped[str] = mapped_column(String, nullable=False)


class FunnelAudit(Base):
    """漏斗审计：每次 build_candidates 的阶段结果与失败明细（可复现性证据链）。

    status=ok：snapshot_json 存有 S1 快照，同 asof 重跑直接复用 → 可复现；
    status=aborted：失败率超阈值熔断，不落候选缓存、不存快照（下次重取实时）。
    """
    __tablename__ = "funnel_audit"
    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="ok")
    s1_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    s2_scanned: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    s2_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    s2_failures_json: Mapped[str | None] = mapped_column(String, nullable=True)
    symbols_json: Mapped[str | None] = mapped_column(String, nullable=True)
    snapshot_json: Mapped[str | None] = mapped_column(String, nullable=True)
    ts: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=datetime.utcnow
    )


class SignalPair(Base):
    __tablename__ = "signal_pair"
    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    symbol: Mapped[str] = mapped_column(String, primary_key=True)
    real_ret: Mapped[float] = mapped_column(Numeric, nullable=False)
    random_ret: Mapped[float] = mapped_column(Numeric, nullable=False)
    config_hash: Mapped[str] = mapped_column(String, primary_key=True)


class SignalLog(Base):
    """信号流水（盘后管道生成；幂等键 trade_date+symbol+kind）。"""
    __tablename__ = "signal_log"
    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    symbol: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String, primary_key=True)  # BUY/SELL/TRIM
    strength: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    expected_net: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    drop_20d: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    rsi_14: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    reason: Mapped[str | None] = mapped_column(String, nullable=True)
    cost_gate: Mapped[str] = mapped_column(String, nullable=False, default="通过")
    status: Mapped[str] = mapped_column(String, nullable=False, default="已生成")
    price_basis: Mapped[str] = mapped_column(String, nullable=False, default="ADJ")
    config_hash: Mapped[str | None] = mapped_column(String, nullable=True)


class ExecutionLog(Base):
    __tablename__ = "execution_log"
    task: Mapped[str] = mapped_column(String, primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String, primary_key=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict | None] = mapped_column(Integer, nullable=True)  # JSON via type below
    executed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=datetime.utcnow
    )


class IngestLog(Base):
    __tablename__ = "ingest_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String, nullable=False)
    capability: Mapped[str] = mapped_column(String, nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String, nullable=False)
    ts: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=datetime.utcnow
    )


# payload JSONB：SQLite 用 JSON 文本，Postgres 用 JSONB。统一用 SQLAlchemy JSON 类型。
from sqlalchemy import JSON  # noqa: E402

ExecutionLog.__table__.c.payload.type = JSON()


class DcCycle(Base):
    """分红捕获（DC）单次周期：计划/持仓/已平仓 + 分红税与取现明细。

    ★ 与 S* 隔离：account_id 恒为 DC（config/strategy_dividend_capture.yaml）。
    同一 (account_id, symbol, ex_date) 只允许一个周期，cycle_id 用确定性字符串。
    """
    __tablename__ = "dc_cycle"
    cycle_id: Mapped[str] = mapped_column(String, primary_key=True)
    account_id: Mapped[str] = mapped_column(String, nullable=False, default="DC")
    symbol: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str | None] = mapped_column(String, nullable=True)
    ex_date: Mapped[date] = mapped_column(Date, nullable=False)
    planned_entry_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    planned_exit_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    entry_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    exit_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    shares: Mapped[float] = mapped_column(Numeric, nullable=False, default=0)
    buy_price: Mapped[float] = mapped_column(Numeric, nullable=False, default=0)
    sell_price: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    dps: Mapped[float] = mapped_column(Numeric, nullable=False, default=0)
    gross_dividend: Mapped[float] = mapped_column(Numeric, nullable=False, default=0)
    tax: Mapped[float] = mapped_column(Numeric, nullable=False, default=0)
    withdrawn: Mapped[float] = mapped_column(Numeric, nullable=False, default=0)
    net_dividend: Mapped[float] = mapped_column(Numeric, nullable=False, default=0)
    buy_cost: Mapped[float] = mapped_column(Numeric, nullable=False, default=0)
    sell_cost: Mapped[float] = mapped_column(Numeric, nullable=False, default=0)
    net_pnl: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    return_pct: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="HOLDING")
    dividend_event_id: Mapped[str | None] = mapped_column(String, nullable=True)
    reason: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=datetime.utcnow
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=datetime.utcnow
    )


class DcUniverseCache(Base):
    """DC 周度全市场池快照（可复现：同 trade_date 复用，失败不落缓存）。"""
    __tablename__ = "dc_universe_cache"
    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    symbols_json: Mapped[str] = mapped_column(String, nullable=False)
    snapshot_json: Mapped[str | None] = mapped_column(String, nullable=True)
    n_candidates: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=datetime.utcnow
    )
