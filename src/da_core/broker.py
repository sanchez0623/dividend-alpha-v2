"""T+1 账户模型 + FIFO 红利税（正确性地基，见 03_ARCHITECTURE.md §3.1）。

Position 三字段：shares_total / shares_locked / shares_available。
当日买入标记 locked，收盘 settle_eod() 解锁。sell 必须校验 n <= available。
FIFO 批次用于红利税档位（<=1月 20% / 1月~1年 10% / >1年 0）。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session

from ..da_core.config import Strategy
from ..da_core.cost import TradeCost, buy_cost, dividend_tax, sell_cost
from ..da_core.models import Account, Fill as FillORM, Lot, Order, Position


class InsufficientShares(Exception):
    pass


@dataclass
class FillResult:
    fill_id: str
    order_id: str
    symbol: str
    side: str
    shares: float
    price: float
    cost: TradeCost


def _available(pos: Position) -> float:
    return float(pos.shares_total) - float(pos.shares_locked)


class T1Broker:
    """模拟盘账本经纪（PAPER 模式，不下真实订单）。"""

    def __init__(self, session: Session, account_id: str, cfg: Strategy):
        self.s = session
        self.account_id = account_id
        self.cfg = cfg

    # ---------- 内部 ----------
    def _account(self) -> Account:
        acc = self.s.get(Account, self.account_id)
        if acc is None:
            raise ValueError(f"account {self.account_id} 不存在，先 init_day_one")
        return acc

    def _position(self, symbol: str) -> Position:
        pos = (
            self.s.query(Position)
            .filter_by(account_id=self.account_id, symbol=symbol)
            .one_or_none()
        )
        if pos is None:
            pos = Position(
                account_id=self.account_id,
                symbol=symbol,
                shares_total=0,
                shares_locked=0,
                shares_available=0,
                cost_price=0,
            )
            self.s.add(pos)
            self.s.flush()
        return pos

    def _lots(self, symbol: str) -> list[Lot]:
        return (
            self.s.query(Lot)
            .filter_by(account_id=self.account_id, symbol=symbol)
            .order_by(Lot.buy_date.asc(), Lot.lot_id.asc())
            .all()
        )

    # ---------- 买入 ----------
    def buy(self, symbol: str, shares: int, price: float, trade_date: date | None = None,
            industry_l1: str | None = None) -> FillResult:
        if shares <= 0:
            raise ValueError("shares must be > 0")
        trade_date = trade_date or date.today()
        amount = shares * price
        acc = self._account()
        cost = buy_cost(self.cfg, amount)
        total = amount + cost.total
        if float(acc.cash) + 1e-6 < total:
            raise ValueError(f"现金不足: cash={acc.cash} need={total}")
        acc.cash = float(acc.cash) - total

        pos = self._position(symbol)
        prev_total = float(pos.shares_total)
        new_total = prev_total + shares
        # 加权成本
        pos.cost_price = (
            (prev_total * float(pos.cost_price) + amount) / new_total if new_total else 0
        )
        pos.shares_total = new_total
        pos.shares_locked = float(pos.shares_locked) + shares  # T+1 当日锁定
        pos.buy_date = pos.buy_date or trade_date
        if industry_l1:
            pos.industry_l1 = industry_l1

        self.s.add(
            Lot(
                account_id=self.account_id,
                symbol=symbol,
                buy_date=trade_date,
                shares=shares,
                price=price,
            )
        )
        return self._record(trade_date, symbol, "BUY", shares, price, cost)

    # ---------- 卖出 ----------
    def sell(self, symbol: str, shares: int, price: float, trade_date: date | None = None) -> FillResult:
        if shares <= 0:
            raise ValueError("shares must be > 0")
        trade_date = trade_date or date.today()
        pos = self._position(symbol)
        avail = _available(pos)
        if shares > avail + 1e-6:
            raise InsufficientShares(
                f"sell {shares} > available {avail} (T+1: total={pos.shares_total}, "
                f"locked={pos.shares_locked})"
            )
        amount = shares * price
        cost = sell_cost(self.cfg, amount)
        acc = self._account()
        acc.cash = float(acc.cash) + (amount - cost.total)

        # FIFO 出批次并结算红利税（此处仅记录卖出，红利税在 on_dividend 结算）
        remaining = shares
        for lot in self._lots(symbol):
            if remaining <= 0:
                break
            take = min(remaining, float(lot.shares))
            lot.shares = float(lot.shares) - take
            remaining -= take
        self.s.query(Lot).filter(
            Lot.account_id == self.account_id, Lot.symbol == symbol, Lot.shares <= 0
        ).delete(synchronize_session=False)

        pos.shares_total = float(pos.shares_total) - shares
        if pos.shares_total <= 0:
            pos.shares_total = 0
            pos.shares_locked = 0
            pos.cost_price = 0
            pos.buy_date = None
            pos.industry_l1 = None
        return self._record(trade_date, symbol, "SELL", shares, price, cost)

    # ---------- 分红 ----------
    def on_dividend(self, symbol: str, dps_gross: float, ex_date: date,
                    pay_date: date | None = None) -> float:
        """分红到账：按 FIFO 批次持股天数扣红利税，净额入现金。返回净现金。"""
        from ..da_core.models import DividendEvent

        pos = self._position(symbol)
        shares_total = float(pos.shares_total)
        gross = dps_gross * shares_total
        # 按批次加权税（简化：按剩余批次平均持股天数）
        tax = 0.0
        lots = [l for l in self._lots(symbol) if float(l.shares) > 0]
        for lot in lots:
            days = (ex_date - lot.buy_date).days
            lot_gross = dps_gross * float(lot.shares)
            tax += dividend_tax(days, lot_gross)
        net = gross - tax
        acc = self._account()
        acc.cash = float(acc.cash) + net
        self.s.add(
            DividendEvent(
                event_id=uuid.uuid4().hex,
                account_id=self.account_id,
                symbol=symbol,
                ex_date=ex_date,
                pay_date=pay_date or ex_date,
                dps_gross=dps_gross,
                tax_withheld=tax,
                net_cash=net,
            )
        )
        return net

    # ---------- 收盘解锁 ----------
    def settle_eod(self) -> int:
        """收盘解锁当日买入锁定的股份。返回解锁股数。"""
        positions = self.s.query(Position).filter_by(account_id=self.account_id).all()
        unlocked = 0
        for pos in positions:
            if float(pos.shares_locked) > 0:
                unlocked += float(pos.shares_locked)
                pos.shares_locked = 0
        return unlocked

    # ---------- 记账 ----------
    def _record(self, trade_date: date, symbol: str, side: str, shares: float, price: float,
                cost: TradeCost) -> FillResult:
        order_id = uuid.uuid4().hex
        fill_id = uuid.uuid4().hex
        self.s.add(
            Order(
                order_id=order_id,
                account_id=self.account_id,
                trade_date=trade_date,
                symbol=symbol,
                side=side,
                shares=shares,
                price=price,
                status="FILLED",
            )
        )
        self.s.add(
            FillORM(
                fill_id=fill_id,
                order_id=order_id,
                account_id=self.account_id,
                trade_date=trade_date,
                symbol=symbol,
                side=side,
                shares=shares,
                price=price,
                commission=cost.commission,
                stamp_tax=cost.stamp_tax,
                transfer_fee=cost.transfer_fee,
                slippage=cost.slippage,
                handling_fee=cost.handling_fee,
                reg_fee=cost.reg_fee,
            )
        )
        self.s.flush()
        return FillResult(
            fill_id=fill_id,
            order_id=order_id,
            symbol=symbol,
            side=side,
            shares=shares,
            price=price,
            cost=cost,
        )
