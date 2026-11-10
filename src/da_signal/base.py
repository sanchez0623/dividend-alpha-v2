"""信号基类与协议。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal, Protocol, runtime_checkable

from ..da_core.config import Strategy
from ..da_core.cost import cost_gate
from ..da_core.models import FactorDaily, Position


@dataclass(frozen=True)
class Signal:
    symbol: str
    kind: Literal["BUY", "SELL", "TRIM"]
    strength: float
    expected_net: float
    reason: str
    asof: datetime
    config_hash: str
    price_basis: Literal["ADJ"] = "ADJ"  # ★ 技术指标必须前复权

    @property
    def passes_cost_gate(self) -> bool:
        # 估算下单金额用 expected_net 与 overlay position_per_trade 校验
        return self.expected_net >= 0.0  # 实际闸由 overlay 配置在 registry 判定


@dataclass
class SignalContext:
    asof: date
    cfg: Strategy
    factors: dict[str, FactorDaily]
    positions: dict[str, Position]
    price_adj: dict[str, float] = field(default_factory=dict)


@runtime_checkable
class SignalPlugin(Protocol):
    name: str

    def generate(self, ctx: SignalContext) -> list[Signal]: ...


def enforce_cost_gate(cfg: Strategy, sig: Signal, amount: float) -> bool:
    """成本闸：expected_net >= min_expected_net 且 >= cost_multiple × 往返成本。"""
    return cost_gate(cfg, sig.expected_net, amount)
