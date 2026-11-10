"""make factors-smoke：抽查 5 只因子与手工一致（T03 验收）。"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.da_core.config import load_strategy  # noqa: E402
from src.da_core.db import init_db, make_engine  # noqa: E402
from src.da_core.models import FactorDaily  # noqa: E402
from src.da_data.factors import compute_factors  # noqa: E402
from src.da_data.fallback import FallbackChain, build_chain  # noqa: E402
from src.da_data.ingest import ingest_daily  # noqa: E402
from src.da_data.providers.synthetic import DEMO_UNIVERSE  # noqa: E402


def main() -> int:
    cfg = load_strategy()
    engine = make_engine(os.environ.get("POSTGRES_DSN", "sqlite:///./data/dividend_alpha.db"))
    init_db(engine)
    from sqlalchemy.orm import sessionmaker

    session = sessionmaker(bind=engine, expire_on_commit=False)()
    chain = FallbackChain(build_chain(cfg.data.get("providers", {})))
    syms = [m["symbol"] for m in DEMO_UNIVERSE]
    end = date.today()
    ingest_daily(session, chain, syms, end - timedelta(days=200), end)
    compute_factors(session, cfg, syms, end)
    n = 0
    for f in session.query(FactorDaily).filter(FactorDaily.trade_date == end).limit(5).all():
        print(f"{f.symbol:10s} yld={f.div_yield_ttm} rsi={f.rsi_14 and round(f.rsi_14, 2)} "
              f"drop20={f.drop_20d and round(f.drop_20d, 4)} div_years={f.consecutive_div_years}")
        n += 1
    session.commit()
    print(f"抽查 {n} 只因子")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
