"""DC 选股/候选构建（worker 与 API 共用）。

输入是 DB 里已落库的 corp_action / price_daily / symbol_status，
不直接访问网络；网络刷新在 da_data.dc_universe。
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

import pandas as pd
from sqlalchemy.orm import Session

from ..da_core.dc_config import DcStrategy
from ..da_core.models import CorpAction as CorpActionORM
from ..da_core.models import PriceDaily, SymbolStatus
from ..da_strategy.dividend_capture import (
    DcCandidate,
    DcRules,
    evaluate_candidate,
    ttm_dividend,
)


def load_adj_series(session: Session, symbol: str, asof: date,
                    bars: int = 250) -> pd.Series | None:
    rows = (
        session.query(PriceDaily.trade_date, PriceDaily.adj_close)
        .filter(PriceDaily.symbol == symbol, PriceDaily.trade_date <= asof)
        .order_by(PriceDaily.trade_date.desc())
        .limit(bars)
        .all()
    )
    if not rows:
        return None
    rows = list(reversed(rows))
    s = pd.Series([float(r[1]) for r in rows],
                  index=[pd.Timestamp(r[0]) for r in rows])
    return s


def load_raw_price(session: Session, symbol: str, d: date) -> float | None:
    row = (
        session.query(PriceDaily)
        .filter(PriceDaily.symbol == symbol, PriceDaily.trade_date <= d)
        .order_by(PriceDaily.trade_date.desc())
        .first()
    )
    if row is None:
        return None
    return float(row.raw_close) if row.raw_close is not None else None


def latest_symbol_status(session: Session, symbol: str,
                          asof: date) -> SymbolStatus | None:
    return (
        session.query(SymbolStatus)
        .filter(SymbolStatus.symbol == symbol, SymbolStatus.trade_date <= asof)
        .order_by(SymbolStatus.trade_date.desc())
        .first()
    )


def build_candidates(
    session: Session,
    dc: DcStrategy,
    symbols: list[str],
    asof: date,
    calendar: list[date],
    days: int = 10,
    *,
    entry_only: bool = False,
) -> list[DcCandidate]:
    """从 DB 构建候选列表。

    entry_only=True：只返回“今天就是买点”的候选（worker 用）；
    False：返回 [asof, asof+days] 内所有事件（API 预览用，未来买点用当前价估算）。
    """
    if not symbols:
        return []
    rules = DcRules.from_strategy(dc)
    end = asof + timedelta(days=days)
    rows = (
        session.query(CorpActionORM)
        .filter(CorpActionORM.symbol.in_(symbols),
                CorpActionORM.ex_date >= asof,
                CorpActionORM.ex_date <= end)
        .order_by(CorpActionORM.ex_date.asc(), CorpActionORM.symbol.asc())
        .all()
    )
    if not rows:
        return []
    # 每只标的历史事件（含未来到 end，用于 TTM；TTM 只取 <= 当前事件日）
    hist = (
        session.query(CorpActionORM)
        .filter(CorpActionORM.symbol.in_(symbols),
                CorpActionORM.ex_date >= asof - timedelta(days=370),
                CorpActionORM.ex_date <= end)
        .all()
    )
    by_sym: dict[str, list[tuple[date, float]]] = defaultdict(list)
    for h in hist:
        by_sym[h.symbol].append((h.ex_date, float(h.dps or 0)))

    adj_cache: dict[str, pd.Series | None] = {}
    out: list[DcCandidate] = []
    seen: set[tuple[str, date]] = set()
    for ev in rows:
        sym = ev.symbol
        if (sym, ev.ex_date) in seen:
            continue
        seen.add((sym, ev.ex_date))
        status = latest_symbol_status(session, sym, asof)
        cap = float(status.market_cap) if status and status.market_cap else 0.0
        name = status.name if status else None
        ttm = ttm_dividend(by_sym.get(sym, []), ev.ex_date)
        if sym not in adj_cache:
            adj_cache[sym] = load_adj_series(session, sym, asof)
        # 预览：未来买点用 asof 最新价；worker 的 entry_only 会在下面用当日价重算
        price = load_raw_price(session, sym, min(asof, ev.ex_date))
        cand = evaluate_candidate(
            symbol=sym, name=name, ex_date=ev.ex_date,
            dps=float(ev.dps or 0), ttm_dps=ttm, market_cap=cap,
            calendar=calendar, raw_price=price,
            adj_series=adj_cache[sym], asof=asof, rules=rules,
        )
        if cand is None:
            continue
        if entry_only and cand.entry_date != asof:
            continue
        out.append(cand)
    return out
