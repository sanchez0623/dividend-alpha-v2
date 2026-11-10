"""成本闸：expected_net < min 或 < 3×往返成本 的信号不进入订单。"""
from __future__ import annotations

from datetime import date

from src.da_core.cost import cost_gate, round_trip_cost_ratio
from src.da_signal.overlay_rsi import OverlayRSIPlugin
from src.da_signal.base import SignalContext


def test_cost_gate_min_threshold(cfg):
    o = cfg.overlay
    min_net = float(o["min_expected_net"])
    assert cost_gate(cfg, min_net - 0.0001, 100000.0) is False
    assert cost_gate(cfg, min_net + 0.01, 100000.0) is True


def test_cost_gate_small_amount_blocked(cfg):
    # 极小金额：min 佣金摊薄，往返成本高，3× 成本闸拦截
    amount = 1000.0
    rt = round_trip_cost_ratio(cfg, amount)
    o = cfg.overlay
    need = float(o["cost_multiple"]) * rt
    assert cost_gate(cfg, need * 0.5, amount) is False
    assert cost_gate(cfg, need * 2.0, amount) is True


def test_overlay_filters_by_cost_gate(cfg, session):
    # 构造一个预期净收益极低的超跌信号，应被成本闸过滤
    from src.da_core.models import FactorDaily

    session.add(FactorDaily(symbol="sh601398", trade_date=date(2027, 1, 4),
                            div_yield_ttm=0.05, consecutive_div_years=6,
                            rsi_14=20.0, drop_20d=-0.13, config_hash=cfg.config_hash()))
    session.flush()
    from src.da_core.models import Position

    ctx = SignalContext(asof=date(2027, 1, 4), cfg=cfg,
                        factors={},
                        positions={},
                        price_adj={})
    # 直接调用插件并检查成本闸逻辑：expected_net 低则不产出
    f = FactorDaily(symbol="sh601398", trade_date=date(2027, 1, 4), div_yield_ttm=0.05,
                    consecutive_div_years=6, rsi_14=20.0, drop_20d=-0.13)
    ctx.factors = {"sh601398": f}
    plugin = OverlayRSIPlugin()
    sigs = plugin.generate(ctx)
    # 若产出，必须全部通过成本闸
    from src.da_core.cost import cost_gate

    for s in sigs:
        assert cost_gate(cfg, s.expected_net, 100000.0) is True
