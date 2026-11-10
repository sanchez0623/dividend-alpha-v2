"""实验性 RSI 超跌叠加层（★ 前复权，experimental，不得当作收益来源）。

trigger: 20日跌幅 ≥ drop_threshold 且 RSI(14) < rsi_threshold（全池 POOL_WIDE）。
"""
from __future__ import annotations

from datetime import date, datetime, time as dtime

from ..da_core.cost import round_trip_cost_ratio
from ..da_core.models import FactorDaily
from .base import Signal, SignalContext, SignalPlugin, enforce_cost_gate


def signal_strength(drop: float, rsi: float, rsi_threshold: float) -> float:
    """f(|drop|, rsi_threshold − rsi) 归一化到 [0,1]。"""
    a = min(abs(drop) / 0.30, 1.0)
    b = max(0.0, min((rsi_threshold - rsi) / 25.0, 1.0))
    return round(0.6 * a + 0.4 * b, 4)


class OverlayRSIPlugin(SignalPlugin):
    name = "overlay_rsi"

    def generate(self, ctx: SignalContext) -> list[Signal]:
        o = ctx.cfg.overlay
        if not o.get("enabled", False):
            return []
        trig = o["trigger"]
        lookback = int(trig["lookback_days"])
        drop_thr = float(trig["drop_threshold"])
        rsi_thr = float(trig["rsi_threshold"])
        min_expected = float(o["min_expected_net"])
        ch = ctx.cfg.config_hash()

        out: list[Signal] = []
        for sym, f in ctx.factors.items():
            if not isinstance(f, FactorDaily):
                continue
            if f.rsi_14 is None or f.drop_20d is None:
                continue
            rsi = float(f.rsi_14)
            drop = float(f.drop_20d)
            if drop >= -drop_thr or rsi >= rsi_thr:
                continue
            strength = signal_strength(drop, rsi, rsi_thr)
            # 估算预期净收益：跌得越深、RSI 越低，预期反弹越高（保守估计）
            expected_net = round(max(0.0, -drop) * 0.15 + max(0.0, rsi_thr - rsi) * 0.004, 6)
            price = ctx.price_adj.get(sym)
            amount = (price or 0) * 100
            # 冷静期/并发/资金由 portfolio 层再校验；此处仅成本闸
            if not enforce_cost_gate(ctx.cfg, Signal(
                    symbol=sym, kind="BUY", strength=strength, expected_net=expected_net,
                    reason="combo", asof=datetime.now(), config_hash=ch), max(amount, 1.0)):
                continue
            out.append(Signal(
                symbol=sym, kind="BUY", strength=strength, expected_net=expected_net,
                reason=f"drop20={drop:.2%}, rsi14={rsi:.1f}",
                asof=datetime.combine(ctx.asof, dtime(16, 30)), config_hash=ch,
                price_basis="ADJ",
            ))
        # rank_by_strength 降序，取 max_concurrent
        out.sort(key=lambda s: s.strength, reverse=True)
        return out[: int(o.get("max_concurrent", 12))]


def within_cooldown(last_buy: date | None, asof: date, cooldown_days: int) -> bool:
    if last_buy is None:
        return False
    return (asof - last_buy).days < cooldown_days
