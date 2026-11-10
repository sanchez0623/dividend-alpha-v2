"""分红捕获（DC）每日闭环：与 S* 并行，独立账户/净值/流水。

流程：
  1) 周度（或缓存为空）刷新全市场市值≥500 亿池 + 除权事件；
  2) 取交易日历；只对「今天就是买点」的候选 + 当前持仓抓近 180 天日线；
  3) 除权日税前分红入账（税递延到卖出）；送股扩股；
  4) 到除权+30 天 → 卖出，按持有天数扣红利税 + 提取净分红 30%；
  5) 空仓且今天有买点 → 排序买一只；
  6) 写 DC 净值 + ExecutionLog（幂等）。
"""
from __future__ import annotations

import logging
import os
from datetime import date, timedelta

from sqlalchemy.orm import Session

from ..da_core.config import Strategy
from ..da_core.dc_config import DcStrategy
from ..da_core.models import (
    CorpAction as CorpActionORM,
)
from ..da_core.models import (
    DailyNav,
    DcCycle,
    DividendEvent,
    ExecutionLog,
    PriceDaily,
    SymbolStatus,
)
from ..da_data.dc_screen import build_candidates
from ..da_data.dc_universe import (
    fetch_dc_prices,
    load_dc_universe,
    refresh_dc_universe,
)
from ..da_data.fallback import FallbackChain, build_chain
from ..da_data.providers.ths import ThsFuyaoClient
from ..da_portfolio.dc_ledger import DcLedger
from ..da_strategy.dividend_capture import (
    DcRules,
    entry_date_for,
    exit_date_for,
    rank_candidates,
)

log = logging.getLogger("da_worker.dividend_capture_close")


def _approx_calendar(center: date, before: int = 90, after: int = 120) -> list[date]:
    from ..da_core.calendar import is_trading_day

    days = []
    d = center - timedelta(days=before)
    end = center + timedelta(days=after)
    while d <= end:
        if is_trading_day(d):
            days.append(d)
        d += timedelta(days=1)
    return days


def _get_calendar(asof: date, fuyao: ThsFuyaoClient | None,
                  provided: list[date] | None) -> tuple[list[date], bool]:
    if provided:
        return sorted(provided), True
    if fuyao is not None:
        try:
            days = fuyao.fetch_trade_calendar(asof - timedelta(days=90),
                                              asof + timedelta(days=120))
            if days:
                return sorted(days), True
        except Exception as e:  # noqa: BLE001
            log.warning("DC 交易日历获取失败，回退近似日历: %s", e)
    return _approx_calendar(asof), False


def _log(session: Session, key: str, status: str, payload: dict) -> None:
    row = session.get(ExecutionLog, {"task": "dc_close", "idempotency_key": key})
    if row is not None:
        row.status = status
        row.payload = payload
        session.flush()
        return
    session.add(ExecutionLog(task="dc_close", idempotency_key=key,
                             status=status, payload=payload))


def _latest_nav(session: Session, dc: DcStrategy, asof: date) -> float | None:
    row = session.get(DailyNav, {"trade_date": asof, "strategy_id": dc.strategy_id,
                                 "account_id": dc.account_id})
    return float(row.nav) if row is not None else None


def _reconcile_position(session: Session, ledger: DcLedger, dc: DcStrategy,
                        asof: date, calendar: list[date]) -> DcCycle | None:
    """持仓存在但周期缺失（崩溃恢复）：按持仓重建一个 HOLDING 周期。"""
    pos = ledger.position()
    if pos is None:
        return None
    entry = pos.buy_date or asof
    ev = (
        session.query(CorpActionORM)
        .filter(CorpActionORM.symbol == pos.symbol,
                CorpActionORM.ex_date >= entry)
        .order_by(CorpActionORM.ex_date.asc())
        .first()
    )
    ex = ev.ex_date if ev is not None else asof
    dps = float(ev.dps or 0) if ev is not None else 0.0
    rules = DcRules.from_strategy(dc)
    st = (
        session.query(SymbolStatus)
        .filter(SymbolStatus.symbol == pos.symbol)
        .order_by(SymbolStatus.trade_date.desc())
        .first()
    )
    exit_ = exit_date_for(ex, calendar, rules.hold_days_after_ex) or ex
    cycle = DcCycle(
        cycle_id=f"dc:{dc.account_id}:{pos.symbol}:{ex.isoformat()}",
        account_id=dc.account_id,
        symbol=pos.symbol,
        name=st.name if st else None,
        ex_date=ex,
        planned_entry_date=entry,
        planned_exit_date=exit_,
        entry_date=entry,
        shares=float(pos.shares_total),
        buy_price=float(pos.cost_price),
        dps=dps,
        buy_cost=0.0,
        status="HOLDING",
        reason="reconciled",
    )
    session.add(cycle)
    session.flush()
    log.warning("DC 检测到无周期持仓 %s，已重建周期 %s（请核对）",
                pos.symbol, cycle.cycle_id)
    return cycle


def _credit_due_dividends(session: Session, ledger: DcLedger,
                          cycle: DcCycle, asof: date) -> list[dict]:
    """除权日（含漏跑补记）税前分红 + 送股扩股；按 dividend_event 幂等。"""
    if cycle is None:
        return []
    events = (
        session.query(CorpActionORM)
        .filter(CorpActionORM.symbol == cycle.symbol,
                CorpActionORM.ex_date >= (cycle.entry_date or cycle.ex_date),
                CorpActionORM.ex_date <= asof)
        .order_by(CorpActionORM.ex_date.asc())
        .all()
    )
    out: list[dict] = []
    for ev in events:
        existing = (
            session.query(DividendEvent)
            .filter_by(account_id=ledger.account_id, symbol=cycle.symbol,
                       ex_date=ev.ex_date)
            .first()
        )
        if existing is not None:
            continue
        dps = float(ev.dps or 0)
        bonus = float(ev.bonus_ratio or 0)
        if dps <= 0 and bonus <= 0:
            continue
        gross = ledger.credit_dividend(cycle, ev.ex_date, dps, bonus)
        out.append({"kind": "DIVIDEND", "symbol": cycle.symbol,
                    "ex_date": ev.ex_date.isoformat(),
                    "gross": gross, "bonus_ratio": bonus})
    return out


def run_dividend_capture_close(
    session: Session,
    dc: DcStrategy,
    cfg: Strategy,
    asof: date | None = None,
    chain: FallbackChain | None = None,
    fuyao: ThsFuyaoClient | None = None,
    calendar_days: list[date] | None = None,
) -> dict:
    asof = asof or date.today()
    key = f"{asof.isoformat()}:{dc.account_id}"
    existing = session.get(ExecutionLog, {"task": "dc_close", "idempotency_key": key})
    if existing is not None and existing.status == "OK":
        return {"trade_date": asof.isoformat(), "strategy_id": dc.strategy_id,
                "idempotent": True, "nav": _latest_nav(session, dc, asof),
                "actions": []}

    ledger = DcLedger(session, cfg, dc)
    ledger.ensure_account()
    ledger.broker.settle_eod()          # 盘后：清掉历史 T+1 锁定，保证已持仓可卖
    rules = DcRules.from_strategy(dc)
    actions: list[dict] = []

    fuyao = fuyao or ThsFuyaoClient(api_key=os.environ.get("THS_API_KEY", ""))
    calendar, cal_ok = _get_calendar(asof, fuyao, calendar_days)

    # ---- 1) 全市场池 ----
    cache = load_dc_universe(session)
    need_refresh = (asof.weekday() == int(dc.universe.get("refresh_weekday", 0))
                    or cache is None)
    symbols: list[str] | None = cache[0] if cache is not None else None
    cache_date: date | None = cache[1] if cache is not None else None
    if need_refresh:
        try:
            fresh = refresh_dc_universe(session, dc, asof, fuyao=fuyao)
        except Exception as e:  # noqa: BLE001
            log.warning("DC 全市场池刷新异常: %s", e)
            fresh = None
        if fresh is not None:
            symbols = fresh
            cache_date = asof
    if symbols is None:
        nav = ledger.write_nav(asof)
        _log(session, key, "OK", {"nav": nav, "reason": "no_universe"})
        return {"trade_date": asof.isoformat(), "strategy_id": dc.strategy_id,
                "nav": nav, "idempotent": False, "actions": actions,
                "reason": "no_universe"}

    age = (asof - cache_date).days if cache_date is not None else 9999
    max_age = int(dc.universe.get("max_cache_age_days", 14))
    allow_new = bool(cal_ok and age <= max_age)

    # ---- 2) 持仓/周期 ----
    cycle = ledger.holding_cycle()
    pos = ledger.position()
    if pos is not None and cycle is None:
        cycle = _reconcile_position(session, ledger, dc, asof, calendar)
    if pos is None and cycle is not None:
        cycle.status = "CLOSED"
        cycle.reason = cycle.reason or "position_missing"
        session.flush()
        cycle = None

    # ---- 3) 今天买点/持仓/候选日线 ----
    lookahead = int(dc.universe.get("lookahead_calendar_days", 10))
    cand_days = int(dc.universe.get("candidates_lookahead_days", 60))
    end = asof + timedelta(days=cand_days)
    future_events = (
        session.query(CorpActionORM)
        .filter(CorpActionORM.symbol.in_(symbols),
                CorpActionORM.ex_date >= asof,
                CorpActionORM.ex_date <= end)
        .all()
    )
    entry_syms: set[str] = set()
    event_syms: set[str] = set()
    for ev in future_events:
        if float(ev.dps or 0) <= 0 and float(ev.bonus_ratio or 0) <= 0:
            continue
        event_syms.add(ev.symbol)
        ent = entry_date_for(ev.ex_date, calendar, rules.entry_lead_trading_days)
        if ent == asof:
            entry_syms.add(ev.symbol)
    # ★ 预抓未来 60 天候选的日线：候选页要展示 TTM 股息率/趋势，且买点日直接可用
    to_fetch = set(event_syms)
    if pos is not None:
        to_fetch.add(pos.symbol)
    if to_fetch:
        chain = chain or FallbackChain(build_chain(cfg.data.get("providers", {})))
        fetch_dc_prices(session, chain, sorted(to_fetch), asof, lookback_days=180)

    # ---- 4) 分红/送股 ----
    if cycle is not None and pos is not None:
        actions.extend(_credit_due_dividends(session, ledger, cycle, asof))

    # ---- 5) 卖出 ----
    if cycle is not None and pos is not None and cycle.planned_exit_date:
        if asof >= cycle.planned_exit_date:
            bar = (
                session.query(PriceDaily)
                .filter_by(symbol=pos.symbol, trade_date=asof)
                .one_or_none()
            )
            price = float(bar.raw_close) if bar is not None and bar.raw_close else None
            if price is not None and price > 0:
                closed = ledger.sell(cycle, asof, price)
                actions.append({
                    "kind": "SELL", "symbol": closed.symbol,
                    "exit_date": asof.isoformat(), "price": price,
                    "tax": float(closed.tax or 0),
                    "withdrawn": float(closed.withdrawn or 0),
                    "net_pnl": float(closed.net_pnl or 0),
                    "return_pct": float(closed.return_pct or 0),
                })
                cycle = None
                pos = None
            else:
                log.info("DC %s 卖出日 %s 无当日 bar（停牌?），顺延", pos.symbol, asof)

    # ---- 6) 买入 ----
    if pos is None and allow_new and entry_syms:
        cands = build_candidates(session, dc, symbols, asof, calendar,
                                 days=lookahead, entry_only=True)
        ranked = rank_candidates(cands)
        if ranked:
            cand = ranked[0]
            bar = (
                session.query(PriceDaily)
                .filter_by(symbol=cand.symbol, trade_date=asof)
                .one_or_none()
            )
            price = float(bar.raw_close) if bar is not None and bar.raw_close else None
            if price is not None and price > 0:
                bought = ledger.buy(cand, asof, price)
                if bought is not None:
                    actions.append({
                        "kind": "BUY", "symbol": bought.symbol,
                        "entry_date": asof.isoformat(), "price": price,
                        "shares": float(bought.shares),
                        "div_yield": cand.div_yield,
                        "market_cap": cand.market_cap,
                        "trend": cand.trend.label,
                    })
        else:
            # 记录“今天有买点但全部不合格”的候选，便于排障
            if cands:
                actions.append({"kind": "SKIP", "reason": "no_eligible",
                                "candidates": [c.symbol for c in cands]})

    # ---- 7) 净值 + 幂等日志 ----
    ledger.broker.settle_eod()          # 当日买入 T+1 锁定 → 收盘解锁
    nav = ledger.write_nav(asof)
    payload = {"nav": nav, "universe": len(symbols), "allow_new": allow_new,
               "calendar_ok": cal_ok, "cache_age_days": age, "actions": actions}
    _log(session, key, "OK", payload)
    session.flush()
    log.info("DC close %s: nav=%.2f universe=%d actions=%s",
             asof, nav, len(symbols), [a.get("kind") for a in actions])
    return {"trade_date": asof.isoformat(), "strategy_id": dc.strategy_id,
            "nav": nav, "idempotent": False, "actions": actions,
            "universe": len(symbols), "allow_new": allow_new}
