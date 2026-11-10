"""APScheduler 单实例调度（Asia/Shanghai）。"""
from __future__ import annotations

import logging

from apscheduler.schedulers.blocking import BlockingScheduler

from ..da_core.db import init_db, make_engine
from ..da_core.config import load_strategy
from .annual_withdraw import run_annual_withdraw
from .daily_close import run_daily_close

log = logging.getLogger("da_worker.scheduler")


def build_scheduler() -> BlockingScheduler:
    sched = BlockingScheduler(timezone="Asia/Shanghai")
    engine = make_engine()
    init_db(engine)
    cfg = load_strategy()

    @sched.scheduled_job("cron", day_of_week="mon-fri", hour=16, minute=50)
    def _daily_close():  # pragma: no cover
        from sqlalchemy.orm import sessionmaker

        s = sessionmaker(bind=engine, expire_on_commit=False)()
        try:
            run_daily_close(s, cfg)
        finally:
            s.close()

    @sched.scheduled_job("cron", day_of_week="mon-fri", hour=16, minute=55, month="12")
    def _annual():  # pragma: no cover
        from sqlalchemy.orm import sessionmaker

        s = sessionmaker(bind=engine, expire_on_commit=False)()
        try:
            run_annual_withdraw(s, cfg)
        finally:
            s.close()

    return sched


def main():  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    build_scheduler().start()


if __name__ == "__main__":
    main()
