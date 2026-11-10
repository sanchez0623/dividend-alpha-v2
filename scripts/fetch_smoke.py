"""make fetch-smoke：抓近 90 交易日前复权日线，打印覆盖率（T00/T02 验收）。"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.da_core.config import load_strategy  # noqa: E402
from src.da_core.db import init_db, make_engine  # noqa: E402
from src.da_core.models import PriceDaily  # noqa: E402
from src.da_data.fallback import FallbackChain, build_chain  # noqa: E402
from src.da_data.ingest import ingest_daily, recompute_adj_close  # noqa: E402
from src.da_data.providers.synthetic import DEMO_UNIVERSE  # noqa: E402


def main() -> int:
    cfg = load_strategy()
    engine = make_engine(os.environ.get("POSTGRES_DSN", "sqlite:///./data/dividend_alpha.db"))
    init_db(engine)
    from sqlalchemy.orm import sessionmaker

    session = sessionmaker(bind=engine, expire_on_commit=False)()
    chain = FallbackChain(build_chain(cfg.data.get("providers", {})))
    symbols = [m["symbol"] for m in DEMO_UNIVERSE]
    end = date.today()
    start = end - timedelta(days=int(cfg.data.get("adj_window_trading_days", 90)) * 2)
    rows = ingest_daily(session, chain, symbols, start, end)
    covered = 0
    for sym in symbols:
        recompute_adj_close(session, sym, window_days=90)
        n = session.query(PriceDaily).filter(PriceDaily.symbol == sym).count()
        if n >= 60:
            covered += 1
        print(f"{sym:10s} rows={n:3d} adj_window={'OK' if n >= 60 else 'LOW'}")
    session.commit()
    print(f"\n覆盖率: {covered}/{len(symbols)} 标的 ≥60 交易日；总落库 {rows} 行 "
          f"(实际生效数据源={chain.last_source or '无'})")
    if chain.last_errors:
        print("降级记录:", ", ".join(chain.last_errors))
    return 0 if covered == len(symbols) else 1


if __name__ == "__main__":
    raise SystemExit(main())
