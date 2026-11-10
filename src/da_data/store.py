"""Parquet 冷备 + 只读 store 辅助。"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
from sqlalchemy.orm import Session

from ..da_core.models import FactorDaily, PriceDaily


def export_prices_parquet(session: Session, symbols: list[str], out_dir: str = "./data/parquet"
                          ) -> Path:
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    rows = (
        session.query(PriceDaily)
        .filter(PriceDaily.symbol.in_(symbols))
        .order_by(PriceDaily.symbol, PriceDaily.trade_date)
        .all()
    )
    df = pd.DataFrame([{
        "symbol": r.symbol, "trade_date": r.trade_date, "raw_close": float(r.raw_close),
        "adj_factor": float(r.adj_factor), "adj_close": float(r.adj_close),
    } for r in rows])
    p = Path(out_dir) / f"price_daily_{date.today().isoformat()}.parquet"
    df.to_parquet(p, index=False)
    return p


def load_factors(session: Session, asof: date) -> pd.DataFrame:
    rows = session.query(FactorDaily).filter(FactorDaily.trade_date == asof).all()
    return pd.DataFrame([{
        "symbol": r.symbol,
        "div_yield_ttm": float(r.div_yield_ttm) if r.div_yield_ttm is not None else None,
        "consecutive_div_years": r.consecutive_div_years,
        "rsi_14": float(r.rsi_14) if r.rsi_14 is not None else None,
        "drop_20d": float(r.drop_20d) if r.drop_20d is not None else None,
    } for r in rows])
