"""B1–B6 基准定义 + 每日推进。

B1 沪深300价格指数 · B2 等权红利组合(不复投对照) · B3 中证红利价格 ·
B4 沪深300全收益(估算) · B5 红利全收益(估算) · B6 安慰剂(做T随机对照)。
"""
from __future__ import annotations

from dataclasses import dataclass

BENCH_IDS = ["B1", "B2", "B3", "B4", "B5", "B6"]

BENCH_META = {
    "B1": {"name": "沪深300价格指数", "cls": "实时"},
    "B2": {"name": "等权高股息组合(不复投对照)", "cls": "实时"},
    "B3": {"name": "中证红利价格指数", "cls": "实时"},
    "B4": {"name": "沪深300全收益(估算)", "cls": "估算"},
    "B5": {"name": "红利全收益(估算)", "cls": "估算"},
    "B6": {"name": "做T安慰剂(随机对照)", "cls": "估算"},
}


@dataclass
class BenchState:
    nav: dict[str, float]


def init_states(init_cash: float) -> dict[str, float]:
    return {bid: init_cash for bid in BENCH_IDS}


def advance(states: dict[str, float], rets: dict[str, float]) -> dict[str, float]:
    """按当日各基准收益率推进净值。rets 缺失的基准保持不变。"""
    out = dict(states)
    for bid in BENCH_IDS:
        r = rets.get(bid)
        if r is not None:
            out[bid] = out.get(bid, 0.0) * (1.0 + r)
    return out


def paired_random_return(rng, trade_returns: list[float]) -> float:
    """B6 安慰剂：同日、同笔数、标的随机的对照收益。"""
    if not trade_returns:
        return 0.0
    import random

    return float(rng.choice(trade_returns))
