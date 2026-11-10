"""分红捕获（DC）只读 API：与 S* 并行展示，不改动既有 /overview 等接口。"""
from __future__ import annotations

import json
import os
from datetime import date, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..da_core.dc_config import DcStrategy
from ..da_core.models import (
    Account,
    DailyNav,
    DcCycle,
    DividendEvent,
    Position,
    PriceDaily,
)
from ..da_data.dc_screen import build_candidates
from ..da_data.dc_universe import load_dc_universe
from ..da_strategy.dividend_capture import rank_candidates
from .deps import get_cfg, get_dc_cfg, get_session
from .schemas import (
    DcBacktestResponse,
    DcCandidateRow,
    DcCycleRow,
    DcNavResponse,
    DcOverviewResponse,
    NumberWithClass,
)

router = APIRouter(prefix="/dc", tags=["dividend_capture"])

_DC_DISCLAIMER = (
    "历史回测为 2015-2026 样本口径，含幸存者偏差、股本近似与执行假设；"
    "不等于未来收益，不构成投资建议。"
)


def _live(v: float | None, unit: str | None = None) -> NumberWithClass:
    return NumberWithClass(value=v, data_class="实时", unit=unit)


def _latest_raw(session: Session, symbol: str) -> float | None:
    row = (
        session.query(PriceDaily)
        .filter(PriceDaily.symbol == symbol)
        .order_by(PriceDaily.trade_date.desc())
        .first()
    )
    return float(row.raw_close) if row is not None and row.raw_close is not None else None


def _approx_calendar(center: date, before: int = 45, after: int = 120) -> list[date]:
    from ..da_core.calendar import is_trading_day

    out: list[date] = []
    d = center - timedelta(days=before)
    end = center + timedelta(days=after)
    while d <= end:
        if is_trading_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out


def _backtest_path() -> Path:
    return Path(os.environ.get("DA_DC_BACKTEST_JSON", "./web/dc_backtest.json"))


def _holding_dict(session: Session, cycle: DcCycle) -> dict:
    price = _latest_raw(session, cycle.symbol) or float(cycle.buy_price or 0)
    shares = float(cycle.shares or 0)
    return {
        "symbol": cycle.symbol,
        "name": cycle.name,
        "ex_date": cycle.ex_date.isoformat(),
        "entry_date": cycle.entry_date.isoformat() if cycle.entry_date else None,
        "planned_exit_date": (cycle.planned_exit_date.isoformat()
                              if cycle.planned_exit_date else None),
        "shares": shares,
        "buy_price": float(cycle.buy_price or 0),
        "market_value": shares * price,
        "gross_dividend": float(cycle.gross_dividend or 0),
        "tax": float(cycle.tax or 0),
        "withdrawn": float(cycle.withdrawn or 0),
        "status": cycle.status,
        "dps": float(cycle.dps or 0),
    }


@router.get("/overview", response_model=DcOverviewResponse)
def overview(session: Session = Depends(get_session), cfg=Depends(get_cfg),
             dc: DcStrategy = Depends(get_dc_cfg)) -> DcOverviewResponse:
    acc = session.get(Account, dc.account_id)
    # 首次运行前账户可能尚未创建：展示初始资金，避免页面显示 0
    cash = float(acc.cash) if acc is not None else float(dc.init_cash)
    positions = session.query(Position).filter_by(account_id=dc.account_id).all()
    fv = 0.0
    for p in positions:
        price = _latest_raw(session, p.symbol) or float(p.cost_price or 0)
        fv += float(p.shares_total) * price

    nav_rows = (
        session.query(DailyNav)
        .filter_by(account_id=dc.account_id, strategy_id=dc.strategy_id)
        .order_by(DailyNav.trade_date.desc())
        .limit(2)
        .all()
    )
    data_date = (nav_rows[0].trade_date.isoformat() if nav_rows
                 else date.today().isoformat())
    daily_pnl = (float(nav_rows[0].nav) - float(nav_rows[1].nav)
                 if len(nav_rows) == 2 else 0.0)
    running_days = (
        session.query(DailyNav)
        .filter_by(account_id=dc.account_id, strategy_id=dc.strategy_id)
        .count()
    )
    div_gross = float(
        session.query(func.coalesce(func.sum(DividendEvent.dps_gross), 0.0))
        .filter(DividendEvent.account_id == dc.account_id).scalar() or 0.0)
    div_net = float(
        session.query(func.coalesce(func.sum(DividendEvent.net_cash), 0.0))
        .filter(DividendEvent.account_id == dc.account_id).scalar() or 0.0)
    withdrawn = float(
        session.query(func.coalesce(func.sum(DcCycle.withdrawn), 0.0))
        .filter(DcCycle.account_id == dc.account_id).scalar() or 0.0)
    captures = int(
        session.query(DcCycle)
        .filter(DcCycle.account_id == dc.account_id,
                DcCycle.status == "CLOSED",
                DcCycle.gross_dividend > 0)
        .count())

    holding_cycle = (
        session.query(DcCycle)
        .filter(DcCycle.account_id == dc.account_id, DcCycle.status == "HOLDING")
        .order_by(DcCycle.entry_date.desc())
        .first()
    )
    holding = _holding_dict(session, holding_cycle) if holding_cycle else None
    next_action = None
    if holding_cycle is not None:
        next_action = {"kind": "SELL", "symbol": holding_cycle.symbol,
                       "date": (holding_cycle.planned_exit_date.isoformat()
                                if holding_cycle.planned_exit_date else None),
                       "reason": "除权后 30 天到期卖出"}
    else:
        universe = load_dc_universe(session)
        if universe is not None:
            today = date.today()
            cands = build_candidates(session, dc, universe[0], today,
                                     _approx_calendar(today), days=90,
                                     entry_only=False)
            ranked = rank_candidates(cands)
            if ranked:
                c = ranked[0]
                next_action = {"kind": "BUY", "symbol": c.symbol, "name": c.name,
                               "date": c.entry_date.isoformat(),
                               "ex_date": c.ex_date.isoformat(),
                               "div_yield": c.div_yield,
                               "market_cap": c.market_cap,
                               "trend": c.trend.label}
    total = cash + fv
    return DcOverviewResponse(
        data_date=data_date,
        strategy_id=dc.strategy_id,
        account_id=dc.account_id,
        init_cash=_live(dc.init_cash),
        total_equity=_live(total),
        cash=_live(cash),
        positions_value=_live(fv),
        daily_pnl=_live(daily_pnl),
        position_count=_live(float(len(positions))),
        running_days=_live(float(running_days)),
        dividend_gross=_live(div_gross),
        dividend_net=_live(div_net),
        withdrawn=_live(withdrawn),
        captures=_live(float(captures)),
        holding=holding,
        next_action=next_action,
        backtest_available=_backtest_path().exists(),
    )


@router.get("/cycles", response_model=list[DcCycleRow])
def cycles(limit: int = Query(100, ge=1, le=500),
           session: Session = Depends(get_session),
           dc: DcStrategy = Depends(get_dc_cfg)) -> list[DcCycleRow]:
    rows = (
        session.query(DcCycle)
        .filter(DcCycle.account_id == dc.account_id)
        .order_by(DcCycle.entry_date.desc())
        .limit(limit)
        .all()
    )
    out: list[DcCycleRow] = []
    for r in rows:
        out.append(DcCycleRow(
            cycle_id=r.cycle_id, symbol=r.symbol, name=r.name,
            ex_date=r.ex_date.isoformat(), status=r.status,
            planned_entry_date=(r.planned_entry_date.isoformat()
                                if r.planned_entry_date else None),
            planned_exit_date=(r.planned_exit_date.isoformat()
                               if r.planned_exit_date else None),
            entry_date=r.entry_date.isoformat() if r.entry_date else None,
            exit_date=r.exit_date.isoformat() if r.exit_date else None,
            shares=float(r.shares or 0), buy_price=float(r.buy_price or 0),
            sell_price=float(r.sell_price) if r.sell_price is not None else None,
            dps=float(r.dps or 0), gross_dividend=float(r.gross_dividend or 0),
            tax=float(r.tax or 0), withdrawn=float(r.withdrawn or 0),
            net_dividend=float(r.net_dividend or 0),
            net_pnl=float(r.net_pnl) if r.net_pnl is not None else None,
            return_pct=float(r.return_pct) if r.return_pct is not None else None,
            reason=r.reason,
        ))
    return out


@router.get("/candidates", response_model=list[DcCandidateRow])
def candidates(days: int = Query(60, ge=5, le=180),
               session: Session = Depends(get_session),
               dc: DcStrategy = Depends(get_dc_cfg)) -> list[DcCandidateRow]:
    universe = load_dc_universe(session)
    if universe is None:
        return []
    today = date.today()
    raw = build_candidates(session, dc, universe[0], today,
                           _approx_calendar(today), days=days, entry_only=False)
    rank_map: dict[tuple[str, str], int] = {
        (c.symbol, c.ex_date.isoformat()): c.rank for c in rank_candidates(raw)}
    out: list[DcCandidateRow] = []
    for c in sorted(raw, key=lambda x: (not x.eligible, x.ex_date, x.symbol)):
        key = (c.symbol, c.ex_date.isoformat())
        out.append(DcCandidateRow(
            symbol=c.symbol, name=c.name,
            ex_date=c.ex_date.isoformat(),
            entry_date=c.entry_date.isoformat(),
            exit_date=c.exit_date.isoformat(),
            dps=c.dps, ttm_dps=c.ttm_dps, price=c.price,
            div_yield=_live(c.div_yield, "%"),
            market_cap=_live(c.market_cap),
            trend=c.trend.label, eligible=c.eligible,
            reason=c.reason, rank=rank_map.get(key, 0),
        ))
    return out


@router.get("/nav", response_model=DcNavResponse)
def nav(session: Session = Depends(get_session)) -> DcNavResponse:
    from ..da_portfolio.ledger import STRATEGY_ID

    rows = (
        session.query(DailyNav)
        .filter(DailyNav.account_id.in_(["PAPER", "DC"]))
        .all()
    )
    series: dict[str, dict[date, float]] = {"S*": {}, "DC": {}, "B2": {}}
    for r in rows:
        d = r.trade_date
        if r.account_id == "PAPER" and r.strategy_id == STRATEGY_ID:
            series["S*"][d] = float(r.nav)
        elif r.account_id == "PAPER" and r.strategy_id == "B2":
            series["B2"][d] = float(r.nav)
        elif r.account_id == "DC" and r.strategy_id == "DC":
            series["DC"][d] = float(r.nav)
    dates = sorted(set().union(*[set(v) for v in series.values()]))
    return DcNavResponse(
        dates=[d.isoformat() for d in dates],
        s=[series["S*"].get(d) for d in dates],
        dc=[series["DC"].get(d) for d in dates],
        b2=[series["B2"].get(d) for d in dates],
    )


@router.get("/config")
def config(dc: DcStrategy = Depends(get_dc_cfg)) -> dict:
    import yaml

    text = yaml.safe_dump(dc.raw, allow_unicode=True, sort_keys=False)
    return {"strategy_yaml": text, "config_hash": dc.config_hash(),
            "strategy_id": dc.strategy_id, "account_id": dc.account_id,
            "init_cash": dc.init_cash, "start_date": dc.start_date}


@router.get("/backtest", response_model=DcBacktestResponse)
def backtest() -> DcBacktestResponse:
    path = _backtest_path()
    if not path.exists():
        return DcBacktestResponse(available=False, disclaimer=_DC_DISCLAIMER)
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return DcBacktestResponse(available=False, disclaimer=_DC_DISCLAIMER)
    # 归档里的 universe 可能是对象（旧 kind/filter/symbols 或新 mode/cap_rule/pit_symbols）；
    # API 统一转成可渲染字符串。
    universe = d.get("universe")
    if isinstance(universe, dict):
        mode = str(universe.get("mode") or universe.get("kind") or "")
        n = (universe.get("pit_symbols") or universe.get("symbols")
             or universe.get("candidate_pool_bars_symbols"))
        if mode == "point_in_time":
            universe = (f"point_in_time · PIT 候选 {n} 只 · "
                        f"买入日当时总股本×当日收盘 ≥500亿")
        else:
            rule = str(universe.get("cap_rule") or universe.get("filter") or "")
            universe = " · ".join(
                x for x in [mode, f"{n} 只" if n else "", rule] if x)
    return DcBacktestResponse(
        available=bool(d.get("available", True)),
        data_class="归档",
        as_of=d.get("as_of"),
        universe=universe,
        start=d.get("start"),
        end=d.get("end"),
        params=d.get("params"),
        metrics=d.get("metrics"),
        yearly=d.get("yearly") or [],
        variants=d.get("variants") or [],
        caveats=d.get("caveats") or [],
        disclaimer=d.get("disclaimer") or _DC_DISCLAIMER,
        raw=d,
    )
