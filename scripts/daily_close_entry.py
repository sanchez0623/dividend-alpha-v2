"""容器内盘后入口：交易日闸门 + worker_once（供宿主机 cron 调用）。

为什么存在：宿主机 cron 直接跑 `docker compose exec api python scripts/worker_once.py`
时，交易日判断若用 shell heredoc 在非交互环境下易出错。这里把闸门放进容器，
用项目自带的交易日历（akshare 真实日历，失败退回周一至周五近似），更稳。

用法（幂等）：
    docker compose -f infra/docker-compose.lite.yml exec -T api python scripts/daily_close_entry.py
非交易日打印一行说明并 exit 0，不写任何数据。
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import date

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.da_core.calendar import is_trading_day  # noqa: E402


def _akshare_trading_day(today: date) -> bool | None:
    """用 akshare 真实交易日历判断；取不到返回 None（让调用方退回近似）。"""
    try:
        import akshare as ak

        df = ak.tool_trade_date_hist_sina()
        col = "trade_date" if "trade_date" in df.columns else df.columns[0]
        days = {str(x) for x in df[col].tolist()}
        return today.isoformat() in days
    except Exception:
        return None


def main() -> int:
    today = date.today()
    real = _akshare_trading_day(today)
    if real is None:
        trading = is_trading_day(today)  # 近似：周一至周五
        src = "approx(周一至周五)"
    else:
        trading = real
        src = "akshare"
    if not trading:
        print(f"[daily_close_entry] {today} 非 A 股交易日（{src}），跳过，不写数据。")
        return 0
    print(f"[daily_close_entry] {today} 交易日（{src}），开始盘后闭环...")

    from src.da_core.config import load_strategy
    from src.da_core.dc_config import load_dividend_capture
    from src.da_core.db import init_db, make_engine
    from src.da_core.models import DailyNav
    from src.da_portfolio.ledger import Ledger
    from src.da_worker.daily_close import run_daily_close
    from src.da_worker.dividend_capture_close import run_dividend_capture_close
    from sqlalchemy.orm import sessionmaker

    logging.basicConfig(level=logging.INFO)
    cfg = load_strategy()
    dc = load_dividend_capture()
    engine = make_engine(os.environ.get("POSTGRES_DSN", "sqlite:////app/data/dividend_alpha.db"))
    init_db(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()

    Ledger(session, "PAPER", cfg).init_day_one(date.fromisoformat(cfg.start_date), cfg.init_cash)
    result = run_daily_close(session, cfg, "PAPER", asof=today)
    n = session.query(DailyNav).filter_by(trade_date=today, account_id="PAPER").count()
    session.commit()
    print("盘后闭环:", result)
    print(f"S* daily_nav 当日 = {n} 行（应为 7）")

    dc_ok = True
    try:
        dc_result = run_dividend_capture_close(session, dc, cfg, asof=today)
        session.commit()
    except Exception as e:  # noqa: BLE001
        session.rollback()
        dc_ok = False
        print(f"DC 闭环失败（S* 已提交，不受影响）: {type(e).__name__}: {e}")
    dc_n = session.query(DailyNav).filter_by(trade_date=today, account_id=dc.account_id).count()
    if dc_ok:
        print("DC 闭环完成")
    print(f"DC daily_nav 当日 = {dc_n} 行（应为 1）")
    return 0 if (n == 7 and dc_ok and dc_n == 1) else 1


if __name__ == "__main__":
    raise SystemExit(main())
