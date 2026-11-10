"""目标持仓 → 订单（月频补仓到 N，不主动换股）。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session

from ..da_core.broker import T1Broker
from ..da_core.config import Strategy
from ..da_core.models import Account, Position
from .dividend import DividendReinvest


@dataclass
class TargetWeight:
    symbol: str
    weight: float
    industry_l1: str | None = None
    bank_subtype: str | None = None


class PortfolioManager:
    def __init__(self, session: Session, account_id: str, cfg: Strategy):
        self.s = session
        self.account_id = account_id
        self.cfg = cfg
        self.broker = T1Broker(session, account_id, cfg)

    def apply_targets(self, targets: list[TargetWeight], price_map: dict[str, float],
                      trade_date: date) -> list[tuple[str, str, int]]:
        """向目标权重靠拢（仅补仓 / 清仓已触发卖出规则的标的）。返回 (symbol, side, shares)。"""
        cfg = self.cfg.portfolio
        core_ratio = float(cfg.get("core_ratio", 0.90))
        acc = self.s.get(Account, self.account_id)
        investable = float(acc.cash) * core_ratio if acc else 0.0

        executed: list[tuple[str, str, int]] = []
        # 先卖出不在目标内的持仓（由卖出规则触发的清仓由 caller 处理），此处仅补仓。
        cur = {p.symbol: p for p in self.s.query(Position).filter_by(
            account_id=self.account_id).all()}
        lot = int(self.cfg.dividend_reinvest.get("lot_size", 100))
        for t in targets:
            price = price_map.get(t.symbol)
            if not price or price <= 0:
                continue
            pos = cur.get(t.symbol)
            cur_shares = float(pos.shares_total) if pos else 0.0
            cur_mv = cur_shares * price
            target_mv = investable * t.weight
            delta = target_mv - cur_mv
            if delta <= 0:
                continue
            shares = int(delta / price // lot) * lot
            if shares <= 0:
                continue
            try:
                self.broker.buy(t.symbol, shares, price, trade_date=trade_date,
                                industry_l1=t.industry_l1)
                executed.append((t.symbol, "BUY", shares))
            except ValueError:
                continue
        return executed

    def total_equity(self, price_map: dict[str, float]) -> float:
        acc = self.s.get(Account, self.account_id)
        cash = float(acc.cash) if acc else 0.0
        mv = 0.0
        for p in self.s.query(Position).filter_by(account_id=self.account_id).all():
            price = price_map.get(p.symbol, float(p.cost_price))
            mv += float(p.shares_total) * price
        if acc is not None:
            acc.total_equity = cash + mv
        return cash + mv

    def reinvest_dividends(self, price_map: dict[str, float], trade_date: date):
        return DividendReinvest(self.s, self.account_id, self.cfg).reinvest(
            price_map, trade_date)
