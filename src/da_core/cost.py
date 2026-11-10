"""成本模型与成本闸（不可绕过）。

单笔往返成本 ≈ 佣金双边(min5) + 卖出印花 + 过户 + 经手费 + 证管费 + 滑点双边。
费率真值：config/strategy.yaml cost 段（2026-11-07 按用户券商实际费率更新）。
信号生成条件：expected_net >= min_expected_net 且 >= cost_multiple × 往返成本。
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import Strategy


@dataclass(frozen=True)
class TradeCost:
    commission: float
    stamp_tax: float
    transfer_fee: float
    slippage: float
    handling_fee: float = 0.0   # 经手费（万0.341，双边）
    reg_fee: float = 0.0        # 证管费（万0.2，双边）

    @property
    def total(self) -> float:
        return (self.commission + self.stamp_tax + self.transfer_fee + self.slippage
                + self.handling_fee + self.reg_fee)


def _fixed_fees(cfg: Strategy, amount: float) -> tuple[float, float, float]:
    """过户费、经手费、证管费（均双边，分开记账）。"""
    c = cfg.cost
    transfer = amount * c["transfer_fee"]
    handling = amount * c.get("handling_fee", 0.0)
    reg = amount * c.get("reg_fee", 0.0)
    return transfer, handling, reg


def buy_cost(cfg: Strategy, amount: float) -> TradeCost:
    c = cfg.cost
    commission = max(amount * c["commission_rate"], c["commission_min"])
    transfer, handling, reg = _fixed_fees(cfg, amount)
    return TradeCost(
        commission=commission,
        stamp_tax=0.0,
        transfer_fee=transfer,
        slippage=amount * c["slippage_bps"] / 10000.0,
        handling_fee=handling,
        reg_fee=reg,
    )


def sell_cost(cfg: Strategy, amount: float) -> TradeCost:
    c = cfg.cost
    commission = max(amount * c["commission_rate"], c["commission_min"])
    transfer, handling, reg = _fixed_fees(cfg, amount)
    return TradeCost(
        commission=commission,
        stamp_tax=amount * c["stamp_tax_sell"],
        transfer_fee=transfer,
        slippage=amount * c["slippage_bps"] / 10000.0,
        handling_fee=handling,
        reg_fee=reg,
    )


def round_trip_cost_ratio(cfg: Strategy, amount: float) -> float:
    """往返成本占金额比例。amount 越小，min 佣金摊薄越差。"""
    if amount <= 0:
        return 1.0
    return (buy_cost(cfg, amount).total + sell_cost(cfg, amount).total) / amount


def cost_gate(cfg: Strategy, expected_net: float, amount: float) -> bool:
    """成本闸：expected_net >= min_expected_net 且 >= cost_multiple × 往返成本。"""
    o = cfg.overlay
    if expected_net < o["min_expected_net"]:
        return False
    rt = round_trip_cost_ratio(cfg, amount)
    if expected_net < o["cost_multiple"] * rt:
        return False
    return True


def dividend_tax(holding_days: int, gross: float) -> float:
    """红利税 FIFO 档位：<=1月 20% / 1月~1年 10% / >1年 0。"""
    if holding_days <= 30:
        rate = 0.20
    elif holding_days <= 365:
        rate = 0.10
    else:
        rate = 0.0
    return gross * rate
