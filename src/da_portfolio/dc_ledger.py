"""分红捕获（DC）专用账本：独立账户 + 分红/红利税/30% 取现/周期流水。

设计要点：
  - 复用 T1Broker 的买卖成本/T+1/批次模型，但**不改变** T1Broker 的现有行为；
  - 除权日先按税前分红入现金并写 dividend_event（tax=0），卖出时按实际持有
    天数结算红利税、再提取净分红的 30%，并回写 dividend_event 的 tax/net；
  - account_id 恒为 DC（配置），与 S* 的 PAPER 账本完全隔离。
"""
from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..da_core.config import Strategy
from ..da_core.dc_config import DcStrategy
from ..da_core.models import (
    Account,
    DcCycle,
    DividendEvent,
    Lot,
    Position,
    PriceDaily,
)
from ..da_strategy.dividend_capture import (
    DcCandidate,
    DcRules,
    settle_cycle,
)
from .ledger import Ledger
from ..da_core.broker import T1Broker


class DcLedger:
    def __init__(self, session: Session, cfg: Strategy, dc: DcStrategy,
                 account_id: str | None = None):
        self.s = session
        self.cfg = cfg
        self.dc = dc
        self.account_id = account_id or dc.account_id
        self.rules: DcRules = DcRules.from_strategy(dc)
        self.broker = T1Broker(session, self.account_id, cfg)

    # ---------- 账户 ----------
    def ensure_account(self, cash: float | None = None) -> Account:
        acc = self.s.get(Account, self.account_id)
        if acc is None:
            init = float(cash if cash is not None else self.dc.init_cash)
            acc = Account(account_id=self.account_id, cash=init,
                          total_equity=init, mode="PAPER")
            self.s.add(acc)
            self.s.flush()
        return acc

    def cash(self) -> float:
        acc = self.ensure_account()
        return float(acc.cash)

    # ---------- 查询 ----------
    def latest_price(self, symbol: str, asof: date) -> float | None:
        row = (
            self.s.query(PriceDaily)
            .filter(PriceDaily.symbol == symbol, PriceDaily.trade_date <= asof)
            .order_by(PriceDaily.trade_date.desc())
            .first()
        )
        if row is None:
            return None
        if row.raw_close is not None:
            return float(row.raw_close)
        return float(row.adj_close) if row.adj_close is not None else None

    def position(self) -> Position | None:
        return (
            self.s.query(Position)
            .filter(Position.account_id == self.account_id,
                    Position.shares_total > 0)
            .order_by(Position.buy_date.asc())
            .first()
        )

    def holding_cycle(self) -> DcCycle | None:
        return (
            self.s.query(DcCycle)
            .filter(DcCycle.account_id == self.account_id,
                    DcCycle.status == "HOLDING")
            .order_by(DcCycle.entry_date.desc())
            .first()
        )

    # ---------- 买入 ----------
    def buy(self, cand: DcCandidate, asof: date, price: float) -> DcCycle | None:
        if price is None or price <= 0:
            return None
        if self.position() is not None:
            return None
        cycle_id = self._cycle_id(cand.symbol, cand.ex_date)
        if self.s.get(DcCycle, cycle_id) is not None:
            return None
        acc = self.ensure_account()
        budget = float(acc.cash) * (1.0 - self.rules.cash_buffer)
        lot = self.rules.lot_size
        shares = int(budget / price // lot) * lot
        if shares <= 0:
            return None
        try:
            fill = self.broker.buy(
                cand.symbol, shares, price, trade_date=asof,
                industry_l1=self._industry(cand.symbol),
            )
        except ValueError:
            return None
        cycle = DcCycle(
            cycle_id=cycle_id,
            account_id=self.account_id,
            symbol=cand.symbol,
            name=cand.name,
            ex_date=cand.ex_date,
            planned_entry_date=cand.entry_date,
            planned_exit_date=cand.exit_date,
            entry_date=asof,
            shares=shares,
            buy_price=price,
            dps=cand.dps,
            buy_cost=float(fill.cost.total),
            status="HOLDING",
            reason="ok",
        )
        self.s.add(cycle)
        self.s.flush()
        return cycle

    # ---------- 分红（除权日） ----------
    def credit_dividend(self, cycle: DcCycle, ex_date: date, dps: float,
                        bonus_ratio: float = 0.0) -> float:
        pos = self.position()
        if pos is None:
            return 0.0
        shares = float(pos.shares_total)
        gross = float(dps) * shares
        event_id = None
        if gross > 0:
            event_id = uuid.uuid4().hex
            acc = self.ensure_account()
            acc.cash = float(acc.cash) + gross
            self.s.add(DividendEvent(
                event_id=event_id,
                account_id=self.account_id,
                symbol=cycle.symbol,
                ex_date=ex_date,
                pay_date=ex_date,
                dps_gross=gross,
                tax_withheld=0.0,
                net_cash=gross,
            ))
            cycle.gross_dividend = float(cycle.gross_dividend or 0) + gross
            cycle.dividend_event_id = event_id
        if bonus_ratio and bonus_ratio > 0:
            self.apply_bonus(cycle.symbol, float(bonus_ratio))
        self.s.flush()
        return gross

    def apply_bonus(self, symbol: str, bonus_ratio: float) -> None:
        """送股/转增：股数×(1+bonus)，成本价÷(1+bonus)，Lot 同步。"""
        pos = (
            self.s.query(Position)
            .filter_by(account_id=self.account_id, symbol=symbol)
            .one_or_none()
        )
        if pos is None or bonus_ratio <= 0:
            return
        factor = 1.0 + bonus_ratio
        pos.shares_total = float(pos.shares_total) * factor
        pos.cost_price = float(pos.cost_price) / factor
        pos.shares_available = float(pos.shares_total) - float(pos.shares_locked)
        for lot in (
            self.s.query(Lot)
            .filter_by(account_id=self.account_id, symbol=symbol)
            .all()
        ):
            lot.shares = float(lot.shares) * factor
            lot.price = float(lot.price) / factor
        self.s.flush()

    # ---------- 卖出 + 税/取现 ----------
    def sell(self, cycle: DcCycle, asof: date, price: float) -> DcCycle:
        pos = self.position()
        if pos is None or price is None or price <= 0:
            return cycle
        shares = float(pos.shares_total)
        if shares <= 0:
            return cycle
        fill = self.broker.sell(cycle.symbol, shares, price, trade_date=asof)
        buy_price = float(cycle.buy_price)
        buy_cost = float(cycle.buy_cost or 0)
        gross = float(cycle.gross_dividend or 0)
        st = settle_cycle(
            entry_date=cycle.entry_date or asof,
            exit_date=asof,
            shares=shares,
            buy_price=buy_price,
            buy_cost=buy_cost,
            sell_price=price,
            sell_cost=float(fill.cost.total),
            gross_dividend=gross,
            rules=self.rules,
        )
        acc = self.ensure_account()
        acc.cash = float(acc.cash) - st.tax - st.withdrawn
        self._update_dividend_events(cycle, asof, st.tax, gross)
        cycle.exit_date = asof
        cycle.sell_price = price
        cycle.sell_cost = float(fill.cost.total)
        cycle.tax = st.tax
        cycle.withdrawn = st.withdrawn
        cycle.net_dividend = st.net_dividend
        cycle.net_pnl = st.net_pnl
        cycle.return_pct = st.return_pct
        cycle.status = "CLOSED"
        self.s.flush()
        return cycle

    def _update_dividend_events(self, cycle: DcCycle, exit_date: date,
                                tax_total: float, gross_total: float) -> None:
        if tax_total <= 0 or gross_total <= 0:
            return
        rows = (
            self.s.query(DividendEvent)
            .filter(
                DividendEvent.account_id == self.account_id,
                DividendEvent.symbol == cycle.symbol,
                DividendEvent.ex_date >= (cycle.entry_date or cycle.ex_date),
                DividendEvent.ex_date <= exit_date,
            )
            .all()
        )
        if not rows:
            return
        rate = tax_total / gross_total
        for ev in rows:
            g = float(ev.dps_gross or 0)
            t = g * rate
            ev.tax_withheld = t
            ev.net_cash = g - t

    # ---------- 估值/净值 ----------
    def nav(self, asof: date) -> float:
        total = self.cash()
        pos = self.position()
        if pos is not None:
            price = self.latest_price(pos.symbol, asof)
            if price is not None:
                total += float(pos.shares_total) * price
        return total

    def write_nav(self, asof: date) -> float:
        nav = self.nav(asof)
        Ledger(self.s, self.account_id, self.cfg).upsert_nav(
            asof, self.dc.strategy_id, nav)
        return nav

    # ---------- 内部 ----------
    def _cycle_id(self, symbol: str, ex_date: date) -> str:
        return f"dc:{self.account_id}:{symbol}:{ex_date.isoformat()}"

    def _industry(self, symbol: str) -> str | None:
        from ..da_core.models import SymbolStatus

        row = (
            self.s.query(SymbolStatus)
            .filter(SymbolStatus.symbol == symbol)
            .order_by(SymbolStatus.trade_date.desc())
            .first()
        )
        return row.industry_l1 if row else None

    def dividend_net_total(self) -> float:
        total = (
            self.s.query(func.coalesce(func.sum(DividendEvent.net_cash), 0.0))
            .filter(DividendEvent.account_id == self.account_id)
            .scalar()
        )
        return float(total or 0.0)

    def withdrawn_total(self) -> float:
        total = (
            self.s.query(func.coalesce(func.sum(DcCycle.withdrawn), 0.0))
            .filter(DcCycle.account_id == self.account_id)
            .scalar()
        )
        return float(total or 0.0)
