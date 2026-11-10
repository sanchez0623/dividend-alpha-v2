"""因子层：股息率TTM(raw)、RSI14/drop_20d(adj)、连续分红年数、市值、行业。

口径硬约束见 04 §4.6。所有因子幂等落库 factor_daily。
"""
from __future__ import annotations

from datetime import date

import pandas as pd
from sqlalchemy.orm import Session

from ..da_core.config import Strategy
from ..da_core.models import CorpAction as CorpActionORM
from ..da_core.models import FactorDaily, Position, PriceDaily, SymbolStatus
from .adjust import compute_drop, compute_rsi
from .providers.eastmoney import bank_subtype_for, fetch_individual_info
from .providers.qq import QQRealtimeSource


def sync_symbol_meta(session: Session, symbols: list[str], asof: date) -> int:
    """证券简称 + 行业L1 + 银行子类 + 精确市值（东财直连；QQ 快照兜底名称）。

    - 名称/行业：东财 stock/get（f58 简称 / f127 行业）
    - 银行子类：spec 维度，仅银行股有值（国有大行/股份行/城农商行），非银行 None
    - 顺带回填 PAPER 持仓的 industry_l1（初始建仓早于行业数据时为 None）
    """
    meta: dict[str, dict] = {}
    try:
        df = fetch_individual_info(symbols)
        for _, r in df.iterrows():
            meta[r["symbol"]] = {"name": r.get("name"),
                                 "industry_l1": r.get("industry_l1"),
                                 "market_cap": r.get("market_cap")}
    except Exception:  # noqa: BLE001
        pass
    try:  # QQ 快照兜底名称（东财失败时不至于全空）
        snap = QQRealtimeSource().fetch_snapshot(symbols)
        for _, r in snap.iterrows():
            m = meta.setdefault(r["symbol"], {})
            if not m.get("name"):
                m["name"] = r.get("name")
    except Exception:  # noqa: BLE001
        pass

    n = 0
    for sym, m in meta.items():
        row = (
            session.query(SymbolStatus)
            .filter_by(symbol=sym, trade_date=asof)
            .one_or_none()
        )
        if row is None:
            row = SymbolStatus(symbol=sym, trade_date=asof)
            session.add(row)
        if m.get("name"):
            row.name = m["name"]
        if m.get("industry_l1"):
            row.industry_l1 = m["industry_l1"]
        if m.get("market_cap"):
            row.market_cap = float(m["market_cap"])
        row.bank_subtype = bank_subtype_for(row.name, row.industry_l1)
        n += 1

    # 回填已持仓行的行业（仅填空，不覆盖）
    for p in session.query(Position).filter_by(account_id="PAPER").all():
        if not p.industry_l1 and p.symbol in meta:
            p.industry_l1 = meta[p.symbol].get("industry_l1")
    session.flush()
    return n


def sync_market_cap(session: Session, symbols: list[str], asof: date) -> int:
    """用腾讯免费快照补 market_cap（fuyao prices 不返回市值）。

    universe 门槛（总市值 ≥300 亿）与 factor market_cap 依赖它。幂等 upsert。
    """
    if not symbols:
        return 0
    try:
        snap = QQRealtimeSource().fetch_snapshot(symbols)
    except Exception:
        return 0
    if snap is None or snap.empty:
        return 0
    n = 0
    for _, r in snap.iterrows():
        mcap = r.get("market_cap")
        if mcap is None:
            continue
        mcap = float(mcap) * 1e8  # 快照单位为「亿」→ 元
        row = (
            session.query(SymbolStatus)
            .filter_by(symbol=r["symbol"], trade_date=asof)
            .one_or_none()
        )
        if row is None:
            row = SymbolStatus(symbol=r["symbol"], trade_date=asof)
            session.add(row)
        row.market_cap = mcap
        n += 1
    session.flush()
    return n


def div_yield_ttm(session: Session, symbol: str, asof: date) -> float | None:
    """近 12 个月每股现金分红之和 ÷ raw_close（不复权）。"""
    price = (
        session.query(PriceDaily)
        .filter(PriceDaily.symbol == symbol, PriceDaily.trade_date <= asof)
        .order_by(PriceDaily.trade_date.desc())
        .first()
    )
    if price is None:
        return None
    raw_close = float(price.raw_close)
    if raw_close <= 0:
        return None
    window_start = date(asof.year - 1, asof.month, asof.day)
    rows = (
        session.query(CorpActionORM)
        .filter(CorpActionORM.symbol == symbol,
                CorpActionORM.ex_date > window_start,
                CorpActionORM.ex_date <= asof)
        .all()
    )
    dps_sum = sum(float(r.dps) for r in rows)
    return dps_sum / raw_close


def consecutive_div_years(session: Session, symbol: str, asof: date, lookback: int = 5) -> int:
    """连续现金分红年数（近 N 年每年至少一次）。"""
    rows = (
        session.query(CorpActionORM)
        .filter(CorpActionORM.symbol == symbol, CorpActionORM.ex_date <= asof)
        .all()
    )
    years = {r.ex_date.year for r in rows if float(r.dps) > 0}
    n = 0
    y = asof.year
    while y in years and n < 30:
        n += 1
        y -= 1
    return n


def compute_factors(session: Session, cfg: Strategy, symbols: list[str], asof: date) -> int:
    """计算并幂等落库当日因子。返回更新 symbol 数。"""
    ch = cfg.config_hash()
    n = 0
    for sym in symbols:
        rows = (
            session.query(PriceDaily)
            .filter(PriceDaily.symbol == sym, PriceDaily.trade_date <= asof)
            .order_by(PriceDaily.trade_date.asc())
            .all()
        )
        if len(rows) < 2:
            continue
        idx = [r.trade_date for r in rows]
        adj = pd.Series([float(r.adj_close) for r in rows], index=idx)
        rsi = compute_rsi(adj, int(cfg.overlay["trigger"]["rsi_period"]))
        drop = compute_drop(adj, int(cfg.overlay["trigger"]["lookback_days"]))
        rsi_v = rsi.iloc[-1]
        drop_v = drop.iloc[-1]
        st = (
            session.query(SymbolStatus)
            .filter(SymbolStatus.symbol == sym, SymbolStatus.trade_date <= asof)
            .order_by(SymbolStatus.trade_date.desc())
            .first()
        )
        mcap = float(st.market_cap) if st and st.market_cap else None
        row = (
            session.query(FactorDaily)
            .filter_by(symbol=sym, trade_date=asof)
            .one_or_none()
        )
        if row is None:
            row = FactorDaily(symbol=sym, trade_date=asof)
            session.add(row)
        row.div_yield_ttm = div_yield_ttm(session, sym, asof)
        row.consecutive_div_years = consecutive_div_years(session, sym, asof)
        row.rsi_14 = None if pd.isna(rsi_v) else float(rsi_v)
        row.drop_20d = None if pd.isna(drop_v) else float(drop_v)
        row.config_hash = ch
        n += 1
    session.flush()
    return n
