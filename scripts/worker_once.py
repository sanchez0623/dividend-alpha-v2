"""make worker-once：手动跑一次盘后闭环（S* + DC 并行，幂等）。

用法：
  python scripts/worker_once.py                 # 今天
  python scripts/worker_once.py 2026-11-06      # 指定交易日（回填/重放）
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import date

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# ★ 漏斗进度/失败明细走 INFO/WARNING 日志；cron 重定向时必须可见（可复现性证据链）
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")

from src.da_core.config import load_strategy  # noqa: E402
from src.da_core.db import init_db, make_engine  # noqa: E402
from src.da_core.dc_config import load_dividend_capture  # noqa: E402
from src.da_core.models import DailyNav  # noqa: E402
from src.da_portfolio.ledger import Ledger  # noqa: E402
from src.da_worker.daily_close import run_daily_close  # noqa: E402
from src.da_worker.dividend_capture_close import run_dividend_capture_close  # noqa: E402


def main() -> int:
    cfg = load_strategy()
    dc = load_dividend_capture()
    engine = make_engine(os.environ.get("POSTGRES_DSN", "sqlite:///./data/dividend_alpha.db"))
    init_db(engine)
    from sqlalchemy.orm import sessionmaker

    session = sessionmaker(bind=engine, expire_on_commit=False)()
    # 可选日期参数：python scripts/worker_once.py [YYYY-MM-DD]（回填重放历史交易日）
    asof = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date.today()

    # ---- S*（保持原行为不变） ----
    Ledger(session, "PAPER", cfg).init_day_one(date.fromisoformat(cfg.start_date), cfg.init_cash)
    result = run_daily_close(session, cfg, "PAPER", asof=asof)
    n = session.query(DailyNav).filter_by(trade_date=asof, account_id="PAPER").count()
    session.commit()
    print("盘后闭环:", result)
    print(f"S* daily_nav 当日 = {n} 行（应为 7）")

    # ---- DC（并行；失败只影响自身，不破坏已提交的 S*） ----
    dc_ok = True
    dc_result = None
    try:
        dc_result = run_dividend_capture_close(session, dc, cfg, asof=asof)
        session.commit()
    except Exception as e:  # noqa: BLE001
        session.rollback()
        dc_ok = False
        print(f"DC 闭环失败（S* 已提交，不受影响）: {type(e).__name__}: {e}")
    dc_n = session.query(DailyNav).filter_by(trade_date=asof, account_id=dc.account_id).count()
    if dc_result is not None:
        print("DC 闭环:", dc_result)
    print(f"DC daily_nav 当日 = {dc_n} 行（应为 1）")

    return 0 if (n == 7 and dc_ok and dc_n == 1) else 1


if __name__ == "__main__":
    raise SystemExit(main())
