"""信号生成管道：因子 + 持仓 → run_plugins → 落库 signal_log（幂等）。

由盘后闭环（daily_close）在 compute_factors 之后调用。
单个插件失败只记日志，不阻断其他插件与主流程。
"""
from __future__ import annotations

import logging
from datetime import date

from sqlalchemy.orm import Session

from ..da_core.config import Strategy
from ..da_core.models import FactorDaily, PriceDaily, Position, SignalLog
from .base import SignalContext
from .registry import default_plugins, run_plugins

log = logging.getLogger("da_signal.pipeline")


def generate_and_store_signals(session: Session, cfg: Strategy,
                               symbols: list[str], asof: date) -> int:
    """生成当日信号并落库，返回写入条数（含更新）。"""
    factors: dict[str, FactorDaily] = {}
    for sym in symbols:
        f = session.query(FactorDaily).filter_by(symbol=sym, trade_date=asof).one_or_none()
        if f:
            factors[sym] = f
    if not factors:
        log.info("no factors for %s, skip signals", asof)
        return 0

    price_adj: dict[str, float] = {}
    for sym in factors:
        r = (session.query(PriceDaily).filter_by(symbol=sym)
             .order_by(PriceDaily.trade_date.desc()).first())
        if r and r.adj_close is not None:
            price_adj[sym] = float(r.adj_close)

    positions = {p.symbol: p for p in
                 session.query(Position).filter_by(account_id="PAPER").all()}

    ctx = SignalContext(asof=asof, cfg=cfg, factors=factors,
                        positions=positions, price_adj=price_adj)

    sigs = []
    for plugin in default_plugins():
        try:
            sigs.extend(plugin.generate(ctx))
        except Exception as e:  # noqa: BLE001
            log.warning("plugin %s failed: %s", getattr(plugin, "name", plugin), e)

    n = 0
    for s in sigs:
        row = session.get(SignalLog, {"trade_date": asof, "symbol": s.symbol,
                                      "kind": s.kind})
        if row is None:
            row = SignalLog(trade_date=asof, symbol=s.symbol, kind=s.kind)
            session.add(row)
        row.strength = float(s.strength) if s.strength is not None else None
        row.expected_net = float(s.expected_net) if s.expected_net is not None else None
        # 因子冗余列（供前端直接展示）
        f = factors.get(s.symbol)
        row.drop_20d = float(f.drop_20d) if f and f.drop_20d is not None else None
        row.rsi_14 = float(f.rsi_14) if f and f.rsi_14 is not None else None
        row.reason = s.reason
        row.cost_gate = "通过" if s.passes_cost_gate else "拦截"
        row.status = "已生成"
        row.price_basis = s.price_basis
        row.config_hash = s.config_hash
        n += 1
    session.flush()
    log.info("signals %s: %d 条", asof, n)
    return n
