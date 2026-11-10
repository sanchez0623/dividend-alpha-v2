"""月频再平衡（仅补仓到 N，不主动换股）入口。"""
from __future__ import annotations

import logging
from datetime import date

from sqlalchemy.orm import Session

from ..da_core.config import Strategy
from ..da_core.broker import T1Broker
from ..da_core.models import FactorDaily, Order, Position, SymbolStatus
from ..da_signal.core_selection import build_targets
from .manager import PortfolioManager, TargetWeight

log = logging.getLogger("da_portfolio.rebalance")


def monthly_rebalance(session: Session, account_id: str, cfg: Strategy,
                      targets: list[TargetWeight], price_map: dict[str, float],
                      trade_date: date) -> list[tuple[str, str, int]]:
    """月频补仓。禁止换股；换股只能由卖出规则触发清仓后经选股补入。"""
    if cfg.rotation_enabled:
        raise RuntimeError("rotation.enabled=true 已证伪，禁止开启")
    mgr = PortfolioManager(session, account_id, cfg)
    return mgr.apply_targets(targets, price_map, trade_date)


def rebalance_due(session: Session, account_id: str, cfg: Strategy, asof: date) -> bool:
    """调仓触发判定：
    1) 初始建仓——策略起始后持仓仍为空 → 立即建仓；
    2) 月度调仓——selection.rebalance=monthly 且本月尚无 BUY 订单 → 本月首个执行日触发。
    """
    n_pos = session.query(Position).filter_by(account_id=account_id).filter(
        Position.shares_total > 0).count()
    if n_pos == 0 and asof.isoformat() >= cfg.start_date:
        return True
    if str(cfg.portfolio.get("rebalance", "monthly")).lower() != "monthly":
        return False
    month_start = asof.replace(day=1)
    has_buy = (
        session.query(Order)
        .filter(Order.account_id == account_id, Order.side == "BUY",
                Order.trade_date >= month_start, Order.trade_date <= asof)
        .first()
    )
    return has_buy is None


def run_rebalance(session: Session, account_id: str, cfg: Strategy,
                  symbols: list[str], asof: date) -> list[tuple[str, str, int]]:
    """选核心仓（build_targets）→ broker 补仓到目标权重。返回成交列表。"""
    factors: dict[str, FactorDaily] = {}
    status: dict[str, SymbolStatus] = {}
    for sym in symbols:
        f = session.query(FactorDaily).filter_by(symbol=sym, trade_date=asof).one_or_none()
        if f:
            factors[sym] = f
        st = (session.query(SymbolStatus).filter(SymbolStatus.symbol == sym,
                                                 SymbolStatus.trade_date <= asof)
              .order_by(SymbolStatus.trade_date.desc()).first())
        if st:
            status[sym] = st
    if not factors:
        log.warning("rebalance %s: 无因子，跳过", asof)
        return []

    targets = [
        TargetWeight(symbol=t["symbol"], weight=float(t["weight"]),
                     industry_l1=t.get("industry_l1"),
                     bank_subtype=t.get("bank_subtype"))
        for t in build_targets(cfg, factors, status)
    ]
    if not targets:
        log.warning("rebalance %s: 漏斗无合格标的（连续分红/市值/股息率门槛）", asof)
        return []

    from ..da_core.models import PriceDaily

    price_map: dict[str, float] = {}
    for t in targets:
        r = (session.query(PriceDaily).filter(PriceDaily.symbol == t.symbol,
                                              PriceDaily.trade_date <= asof)
             .order_by(PriceDaily.trade_date.desc()).first())
        if r and r.adj_close:
            price_map[t.symbol] = float(r.adj_close)

    trades = monthly_rebalance(session, account_id, cfg, targets, price_map, asof)
    # 盘后批次：当日买入的 T+1 锁定即时结算（模拟盘盘后闭环）
    T1Broker(session, account_id, cfg).settle_eod()
    log.info("rebalance %s: 目标 %d 只，成交 %d 笔", asof, len(targets), len(trades))
    return trades
