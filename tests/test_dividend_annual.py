"""年度分红取现：年内全额再投、12月最后交易日取 30%、幂等、skip_incomplete_year。"""
from __future__ import annotations

from datetime import date

import pytest

from src.da_core.models import Account, DividendEvent, WithdrawLog
from src.da_portfolio.dividend import AnnualWithdraw
from src.da_portfolio.ledger import Ledger


def _seed_dividends(session, account_id, year, gross):
    acc = session.get(Account, account_id)
    acc.cash = float(acc.cash) + gross
    session.add(DividendEvent(
        event_id=f"e{year}", account_id=account_id, symbol="sh601398",
        ex_date=date(year, 7, 1), pay_date=date(year, 7, 5),
        dps_gross=gross, tax_withheld=0, net_cash=gross))


def test_annual_withdraw_ratio_and_idempotent(session, cfg):
    year = 2027  # 未来完整年份，绕过 skip_incomplete_year
    Ledger(session, "PAPER", cfg).init_day_one(date(year, 1, 2), cfg.init_cash)
    _seed_dividends(session, "PAPER", year, 100000.0)
    aw = AnnualWithdraw(session, "PAPER", cfg)
    res = aw.run_year_end(year)
    assert res.want == pytest.approx(30000.0)
    assert res.got == pytest.approx(30000.0)
    assert res.rate == pytest.approx(100.0)
    # 重复触发不重复取
    res2 = aw.run_year_end(year)
    assert res2.idempotent is True
    n = session.query(WithdrawLog).filter_by(year=year, account_id="PAPER").count()
    assert n == 1
    from src.da_core.models import ExecutionLog

    logs = session.query(ExecutionLog).filter_by(
        task="annual_withdraw", idempotency_key=f"{year}:PAPER").count()
    assert logs == 1


def test_skip_incomplete_year(session, cfg):
    year = date.today().year
    Ledger(session, "PAPER", cfg).init_day_one(date(year, 1, 2), cfg.init_cash)
    _seed_dividends(session, "PAPER", year, 50000.0)
    # 若今天尚未到年末最后交易日，则进行中年份不取
    from src.da_core.calendar import last_trading_day_of_december

    if date.today() < last_trading_day_of_december(year):
        res = AnnualWithdraw(session, "PAPER", cfg).run_year_end(year)
        assert res.status == "SKIPPED_INCOMPLETE"
        assert session.query(WithdrawLog).filter_by(year=year, account_id="PAPER").count() == 0
