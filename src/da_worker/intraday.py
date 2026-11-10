"""盘中（可选）：60min K 线轮询，仅准实时提示，不触发自动订单。"""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy.orm import Session

from ..da_core.config import Strategy
from ..da_data.fallback import FallbackChain, build_chain
from ..da_data.factors import compute_factors


def run_intraday(session: Session, cfg: Strategy, asof: date | None = None) -> dict:
    """盘中仅刷新因子提示（不落订单）。生产用 akshare 60min。"""
    asof = asof or date.today()
    chain = FallbackChain(build_chain(cfg.data.get("providers", {})))
    symbols = [m["symbol"] for m in getattr(chain.sources[-1], "universe", list)()]
    n = compute_factors(session, cfg, symbols, asof)
    return {"status": "OK", "asof": asof.isoformat(), "symbols": n, "note": "仅提示，不下单"}
