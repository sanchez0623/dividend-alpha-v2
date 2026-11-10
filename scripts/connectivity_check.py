"""数据源连通性实测记录（T00）。

实测 akshare + 同花顺 fuyao 两个指定数据源，记录到 docs/CONNECTIVITY.md。
"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy.orm import sessionmaker  # noqa: E402

from src.da_core.config import load_strategy  # noqa: E402
from src.da_core.db import init_db, make_engine  # noqa: E402
from src.da_core.models import CorpAction as CA  # noqa: E402
from src.da_core.models import PriceDaily  # noqa: E402
from src.da_data.factors import compute_factors, consecutive_div_years, div_yield_ttm  # noqa: E402
from src.da_data.factors import sync_market_cap  # noqa: E402
from src.da_data.fallback import FallbackChain, build_chain  # noqa: E402
from src.da_data.ingest import ingest_daily, recompute_adj_close  # noqa: E402
from src.da_data.providers.synthetic import DEMO_UNIVERSE  # noqa: E402


def main() -> int:
    cfg = load_strategy()
    dsn = os.environ.get("POSTGRES_DSN", "sqlite:///./data/live.db")
    eng = make_engine(dsn)
    init_db(eng)
    s = sessionmaker(bind=eng, expire_on_commit=False)()
    chain = FallbackChain(build_chain(cfg.data.get("providers", {})))
    print("fallback 链:", [x.name for x in chain.sources])
    syms = [m["symbol"] for m in DEMO_UNIVERSE]
    end = date.today()
    start = end - timedelta(days=200)

    rows = ingest_daily(s, chain, syms, start, end)
    for sym in syms:
        recompute_adj_close(s, sym, window_days=90)
    s.commit()

    n = s.query(PriceDaily).count()
    ca = s.query(CA).count()
    print(f"ingest 行数={rows}  price_daily={n}  corp_action={ca}")
    used = getattr(chain, "_last_source", None)
    for sym in syms[:3]:
        r = s.query(PriceDaily).filter_by(symbol=sym).order_by(
            PriceDaily.trade_date.desc()).first()
        if r:
            print(f"  {sym} {r.trade_date} raw={float(r.raw_close):.4f} "
                  f"adj={float(r.adj_close):.4f} factor={float(r.adj_factor):.4f} "
                  f"src={r.source}")
    compute_factors(s, cfg, syms, end)
    s.commit()
    print(f"股息率TTM(不复权) sh601398 = {div_yield_ttm(s, 'sh601398', end)}")
    print(f"连续分红年数 sh601398 = {consecutive_div_years(s, 'sh601398', end)}")
    # 腾讯免费快照（fuyao prices 不返回市值，用 QQ 补 market_cap）
    try:
        from src.da_data.providers.qq import QQRealtimeSource

        snap = QQRealtimeSource().fetch_snapshot(syms)
        print("腾讯实时快照 market_cap(亿):",
              {r.symbol: r.market_cap for _, r in snap.head(3).iterrows()})
    except Exception as e:  # noqa: BLE001
        print("腾讯快照不可用:", type(e).__name__)
    return 0 if n > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
