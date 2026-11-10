"""增量 ingest（Forward-only）：落 price_daily / corp_action / symbol_status，并回写 adj_close。"""
from __future__ import annotations

import time
from datetime import date, timedelta

import pandas as pd
from sqlalchemy.orm import Session

from ..da_core.models import CorpAction as CorpActionORM
from ..da_core.models import IngestLog, PriceDaily, SymbolStatus
from .adjust import CorpAction, adj_close_series, build_adj_factor
from .fallback import FallbackChain


def ingest_daily(session: Session, chain: FallbackChain, symbols: list[str], start: date,
                 end: date) -> int:
    """抓日线 → 落库 raw_close/adj_factor/adj_close。返回落库行数。幂等：主键 upsert。"""
    t0 = time.monotonic()
    df = chain.fetch_daily(symbols, start, end)
    rows = 0
    if df is None or df.empty:
        _log(session, "synthetic", "prices", int((time.monotonic() - t0) * 1000), 0, "EMPTY")
        return 0
    if "source" not in df.columns:
        df["source"] = "chain"
    for sym, g in df.groupby("symbol"):
        g = g.sort_values("trade_date")
        # ★ 除权事件回看 6.8 年：universe 门槛"连续分红 ≥5 年"需要多年事件史
        events = chain.fetch_corp_actions(sym, start - timedelta(days=2500))
        if events:
            ingest_corp_action(session, events)
        # ★ 复权只消费 ex_date ≤ 窗口末端 的事件：fuyao 会返回已公告未除权的分红
        #   预案（ex_date 在未来），提前折进 adj_close 是前视偏差
        known = [e for e in events if e.ex_date <= end]
        factor = build_adj_factor(
            pd.Series(g["raw_close"].astype(float).values,
                      index=[d if isinstance(d, date) else pd.Timestamp(d).date()
                             for d in g["trade_date"]]),
            known,
        )
        adj = adj_close_series(
            pd.Series(g["raw_close"].astype(float).values,
                      index=[d if isinstance(d, date) else pd.Timestamp(d).date()
                             for d in g["trade_date"]]),
            known,
        )
        for i, (_, r) in enumerate(g.iterrows()):
            td = r["trade_date"]
            td = td if isinstance(td, date) else pd.Timestamp(td).date()
            upsert_price(session, symbol=sym, trade_date=td,
                         open_=_f(r.get("open")), high=_f(r.get("high")),
                         low=_f(r.get("low")), raw_close=float(r["raw_close"]),
                         adj_factor=float(factor.iloc[i]), adj_close=float(adj.iloc[i]),
                         amount=_f(r.get("amount")), source=str(r.get("source", "chain")))
            rows += 1
    session.flush()
    _log(session, "chain", "prices", int((time.monotonic() - t0) * 1000), rows, "OK")
    return rows


def upsert_price(session: Session, *, symbol: str, trade_date: date, open_, high, low,
                 raw_close: float, adj_factor: float, adj_close: float, amount, source: str
                 ) -> PriceDaily:
    row = session.get(PriceDaily, {"symbol": symbol, "trade_date": trade_date})
    if row is None:
        row = PriceDaily(symbol=symbol, trade_date=trade_date)
        session.add(row)
    row.open = open_
    row.high = high
    row.low = low
    row.raw_close = raw_close
    row.adj_factor = adj_factor
    row.adj_close = adj_close
    row.amount = amount
    row.source = source
    return row


def ingest_corp_action(session: Session, actions: list[CorpAction]) -> int:
    n = 0
    for ev in actions:
        row = session.get(CorpActionORM, {"symbol": ev.symbol, "ex_date": ev.ex_date})
        if row is not None:
            existing = row
        else:
            # ★ 防御：±1 天内同 symbol 同 dps/bonus 的事件视为同一次除权
            #   （时区标签切换期的重复污染源；PK (symbol, ex_date) 挡不住跨日重复）
            near = (
                session.query(CorpActionORM)
                .filter(
                    CorpActionORM.symbol == ev.symbol,
                    CorpActionORM.ex_date.between(
                        ev.ex_date - timedelta(days=1), ev.ex_date + timedelta(days=1)),
                )
                .all()
            )
            if any(
                float(x.dps or 0) == float(ev.dps or 0)
                and float(x.bonus_ratio or 0) == float(ev.bonus_ratio or 0)
                for x in near
            ):
                continue
            existing = CorpActionORM(symbol=ev.symbol, ex_date=ev.ex_date)
            session.add(existing)
            row = existing
        row.dps = ev.dps
        row.bonus_ratio = ev.bonus_ratio
        row.transfer_ratio = ev.transfer_ratio
        row.rights_ratio = ev.rights_ratio
        row.rights_price = ev.rights_price
        row.announce_date = ev.announce_date
        row.pay_date = ev.pay_date
        n += 1
    session.flush()
    return n


def ingest_symbol_status(session: Session, df: pd.DataFrame) -> int:
    if df is None or df.empty:
        return 0
    n = 0
    for _, r in df.iterrows():
        td = r["trade_date"]
        td = td if isinstance(td, date) else pd.Timestamp(td).date()
        key = {"symbol": r["symbol"], "trade_date": td}
        row = session.get(SymbolStatus, key)
        if row is None:
            row = SymbolStatus(**key)
            session.add(row)
        for col in ["is_st", "is_suspended", "limit_up", "limit_down", "list_date",
                    "delist_date", "market_cap", "industry_l1", "industry_l3", "bank_subtype"]:
            if col in r and not (isinstance(r[col], float) and pd.isna(r[col])):
                setattr(row, col, r[col])
        n += 1
    session.flush()
    return n


def recompute_adj_close(session: Session, symbol: str, window_days: int = 90) -> int:
    """按最新因子重算窗口内全部 adj_close，返回更新行数（新除权改写历史是正确行为）。"""
    rows = (
        session.query(PriceDaily)
        .filter(PriceDaily.symbol == symbol)
        .order_by(PriceDaily.trade_date.desc())
        .limit(window_days)
        .all()
    )
    if not rows:
        return 0
    rows = list(reversed(rows))
    asof = rows[-1].trade_date  # ★ 复权基准 = 窗口最新交易日
    events = (
        session.query(CorpActionORM)
        .filter(CorpActionORM.symbol == symbol)
        .all()
    )
    from .adjust import CorpAction as CA

    evs = [
        CA(symbol=e.symbol, ex_date=e.ex_date, dps=float(e.dps),
           bonus_ratio=float(e.bonus_ratio), transfer_ratio=float(e.transfer_ratio),
           rights_ratio=float(e.rights_ratio), rights_price=float(e.rights_price))
        for e in events if e.ex_date <= asof  # ★ 复权只消费已除权事件（防前视）
    ]
    idx = [r.trade_date for r in rows]
    raw = pd.Series([float(r.raw_close) for r in rows], index=idx)
    factor = build_adj_factor(raw, evs)
    adj = raw * factor
    n = 0
    for r, f, a in zip(rows, factor.values, adj.values):
        r.adj_factor = float(f)
        r.adj_close = float(a)
        n += 1
    session.flush()
    return n


def _f(v) -> float | None:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    return float(v)


def _log(session: Session, source: str, capability: str, latency_ms: int, rows: int,
         status: str) -> None:
    session.add(IngestLog(source=source, capability=capability, latency_ms=latency_ms,
                          rows=rows, status=status))
    session.flush()
