"""卖出规则：硬规则（股息率<1.5%/分红中断/持有满5年）+ 超涨浮盈≥50% 一次性清仓。

禁止轮动 / 条件换仓 / 强制换仓 / 趋势保护。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ..da_core.config import Strategy
from ..da_core.models import CorpAction as CorpActionORM
from ..da_core.models import FactorDaily, Position
from .base import Signal, SignalContext, SignalPlugin


@dataclass
class SellDecision:
    symbol: str
    rule: str
    action: str = "sell_all"


def evaluate_sell(cfg: Strategy, sym: str, pos: Position, factor: FactorDaily | None,
                  price: float, asof: date, dividend_interrupted: bool = False) -> SellDecision | None:
    if pos.shares_total <= 0:
        return None
    if factor is not None and factor.div_yield_ttm is not None:
        if float(factor.div_yield_ttm) < 0.015:
            return SellDecision(sym, "div_yield_ttm < 0.015")
    if dividend_interrupted:
        return SellDecision(sym, "dividend_interrupted")
    if pos.buy_date:
        months = (asof - pos.buy_date).days / 30.44
        if months >= 60:
            return SellDecision(sym, "holding_months >= 60")
    if pos.cost_price and price and float(pos.cost_price) > 0:
        gain = price / float(pos.cost_price) - 1.0
        if gain >= 0.50:
            return SellDecision(sym, "unrealized_gain >= 0.50")
    return None


def check_dividend_interrupted(session, sym: str, asof: date) -> bool:
    """近 12 个月无现金分红 → 分红中断。"""
    from datetime import timedelta

    start = asof - timedelta(days=400)
    n = (
        session.query(CorpActionORM)
        .filter(CorpActionORM.symbol == sym, CorpActionORM.ex_date > start,
                CorpActionORM.ex_date <= asof, CorpActionORM.dps > 0)
        .count()
    )
    return n == 0


class SellRulesPlugin(SignalPlugin):
    name = "sell_rules"

    def generate(self, ctx: SignalContext) -> list[Signal]:
        out: list[Signal] = []
        for sym, pos in ctx.positions.items():
            f = ctx.factors.get(sym)
            price = ctx.price_adj.get(sym)
            d = evaluate_sell(ctx.cfg, sym, pos, f, price, ctx.asof)
            if d is not None:
                out.append(Signal(
                    symbol=sym, kind="SELL", strength=1.0, expected_net=0.0,
                    reason=d.rule, asof=_dt(ctx.asof), config_hash=ctx.cfg.config_hash(),
                    price_basis="ADJ",
                ))
        return out


def _dt(d: date) -> "datetime":
    from datetime import datetime, time as t
    return datetime.combine(d, t(16, 0))
