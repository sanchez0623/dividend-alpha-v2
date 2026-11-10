"""make ledger-init：写入首日 7 条净值（S* + B1~B6），T01 验收。"""
from __future__ import annotations

import csv
import os
import sys
from datetime import date

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.da_core.config import load_strategy  # noqa: E402
from src.da_core.db import init_db, make_engine  # noqa: E402
from src.da_core.models import DailyNav  # noqa: E402
from src.da_portfolio.ledger import Ledger  # noqa: E402


def main() -> int:
    cfg = load_strategy()
    engine = make_engine(os.environ.get("POSTGRES_DSN", "sqlite:///./data/dividend_alpha.db"))
    init_db(engine)
    from sqlalchemy.orm import sessionmaker

    session = sessionmaker(bind=engine, expire_on_commit=False)()
    start = date.fromisoformat(cfg.start_date)
    ledger = Ledger(session, "PAPER", cfg)
    ledger.init_day_one(start, cfg.init_cash)
    session.commit()
    rows = session.query(DailyNav).filter_by(trade_date=start, account_id="PAPER").all()
    os.makedirs("data", exist_ok=True)
    with open("data/daily_nav_day_one.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["trade_date", "strategy_id", "nav", "running_days"])
        for r in sorted(rows, key=lambda r: r.strategy_id):
            w.writerow([r.trade_date, r.strategy_id, float(r.nav), r.running_days])
            print(f"{r.strategy_id:3s} nav={float(r.nav):,.0f} day={r.running_days}")
    print(f"\ndaily_nav 首日 = {len(rows)} 行（应为 7）")
    return 0 if len(rows) == 7 else 1


if __name__ == "__main__":
    raise SystemExit(main())
