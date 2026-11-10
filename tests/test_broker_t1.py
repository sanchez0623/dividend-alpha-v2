"""T+1 账户模型：当日买入 available=0、超卖抛错、settle 解锁、FIFO 税率档位。"""
from __future__ import annotations

import pytest

from src.da_core.broker import InsufficientShares, T1Broker
from src.da_core.cost import dividend_tax
from src.da_portfolio.ledger import Ledger


def _init(session, cfg, day_one):
    Ledger(session, "PAPER", cfg).init_day_one(day_one, cfg.init_cash)
    return T1Broker(session, "PAPER", cfg)


def test_t1_same_day_buy_locked(session, cfg, day_one):
    b = _init(session, cfg, day_one)
    b.buy("sh601398", 1000, 5.0, trade_date=day_one, industry_l1="银行")
    session.flush()
    from src.da_core.models import Position

    pos = session.query(Position).filter_by(account_id="PAPER", symbol="sh601398").one()
    assert float(pos.shares_total) == 1000
    assert float(pos.shares_locked) == 1000
    assert float(pos.shares_total) - float(pos.shares_locked) == 0


def test_sell_more_than_available_raises(session, cfg, day_one):
    b = _init(session, cfg, day_one)
    b.buy("sh600036", 1000, 30.0, trade_date=day_one)
    with pytest.raises(InsufficientShares):
        b.sell("sh600036", 1000, 31.0, trade_date=day_one)


def test_settle_eod_unlocks(session, cfg, day_one):
    from src.da_core.calendar import next_trading_day

    b = _init(session, cfg, day_one)
    b.buy("sh600900", 500, 20.0, trade_date=day_one)
    nd = next_trading_day(day_one)
    b.settle_eod()
    session.flush()
    b.sell("sh600900", 500, 21.0, trade_date=nd)  # 解锁后可卖
    from src.da_core.models import Position

    pos = session.query(Position).filter_by(account_id="PAPER", symbol="sh600900").one()
    assert float(pos.shares_total) == 0


def test_fifo_tax_tiers():
    assert dividend_tax(10, 100.0) == pytest.approx(20.0)   # ≤1月 20%
    assert dividend_tax(100, 100.0) == pytest.approx(10.0)  # 1月~1年 10%
    assert dividend_tax(400, 100.0) == pytest.approx(0.0)   # >1年 0%
