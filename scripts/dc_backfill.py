"""回填 DC 账户与净值（默认从 2026-11-05 起，按 S* 已写净值的日期逐日重放）。

用法：
  python scripts/dc_backfill.py                 # start=DC 配置 start_date, end=今天
  python scripts/dc_backfill.py 2026-11-05 2026-11-07
幂等：同 asof 重跑命中 execution_log 的 dc_close 键，直接返回。
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")

from src.da_core.config import load_strategy  # noqa: E402
from src.da_core.db import init_db, make_engine  # noqa: E402
from src.da_core.dc_config import load_dividend_capture  # noqa: E402
from src.da_core.models import DailyNav  # noqa: E402
from src.da_portfolio.dc_ledger import DcLedger  # noqa: E402
from src.da_worker.dividend_capture_close import run_dividend_capture_close  # noqa: E402


def main() -> int:
    cfg = load_strategy()
    dc = load_dividend_capture()
    engine = make_engine(os.environ.get("POSTGRES_DSN", "sqlite:///./data/dividend_alpha.db"))
    init_db(engine)
    from sqlalchemy.orm import sessionmaker

    session = sessionmaker(bind=engine, expire_on_commit=False)()
    start = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date.fromisoformat(dc.start_date)
    end = date.fromisoformat(sys.argv[2]) if len(sys.argv) > 2 else date.today()
    if end < start:
        end = start

    # 优先按 S* 已存在的净值日期回填（保证两条策略同日可比）；
    # 若 S* 暂无，则至少写 start 一天。
    rows = (
        session.query(DailyNav.trade_date)
        .filter(DailyNav.account_id == "PAPER",
                DailyNav.trade_date >= start,
                DailyNav.trade_date <= end)
        .distinct()
        .order_by(DailyNav.trade_date.asc())
        .all()
    )
    dates = [r[0] for r in rows]
    if not dates:
        dates = [start]
        d = start
        while d < end:
            d += timedelta(days=1)
            dates.append(d)

    DcLedger(session, cfg, dc).ensure_account()
    for d in dates:
        try:
            res = run_dividend_capture_close(session, dc, cfg, asof=d)
            session.commit()
            print(d.isoformat(), res)
        except Exception as e:  # noqa: BLE001
            session.rollback()
            print(f"{d.isoformat()} DC 回填失败: {type(e).__name__}: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
