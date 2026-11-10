"""年度取现 worker（幂等，12月最后交易日盘后）。"""
from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from ..da_core.config import Strategy
from ..da_core.calendar import last_trading_day_of_december
from ..da_portfolio.dividend import AnnualWithdraw


def run_annual_withdraw(session: Session, cfg: Strategy, account_id: str = "PAPER",
                        year: int | None = None) -> dict:
    year = year or date.today().year
    ld = last_trading_day_of_december(year)
    if date.today() < ld:
        return {"status": "NOT_DUE", "next": ld.isoformat()}
    res = AnnualWithdraw(session, account_id, cfg).run_year_end(year)
    session.commit()
    return {"status": res.status, "year": res.year, "want": res.want, "got": res.got,
            "rate": res.rate, "idempotent": res.idempotent}
