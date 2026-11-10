"""DC 账本：买入/分红入账/送股/卖出税与取现/净值/幂等。"""
from __future__ import annotations

from datetime import date

import pytest

from src.da_core.dc_config import load_dividend_capture
from src.da_core.models import DividendEvent, PriceDaily
from src.da_portfolio.dc_ledger import DcLedger
from src.da_strategy.dividend_capture import DcCandidate, TrendState


def _seed_price(session, symbol: str, rows: list[tuple[date, float]]) -> None:
    for d, p in rows:
        session.add(PriceDaily(symbol=symbol, trade_date=d, open=p, high=p,
                               low=p, raw_close=p, adj_factor=1.0, adj_close=p))
    session.flush()


def _cand(entry: date, ex: date, exit_: date) -> DcCandidate:
    return DcCandidate(
        symbol="sh600028", name="中国石化", ex_date=ex, entry_date=entry,
        exit_date=exit_, dps=0.8, ttm_dps=0.8, price=10.0, div_yield=0.08,
        market_cap=6.0e10, trend=TrendState("up"), eligible=True,
    )


def test_buy_dividend_sell_full_cycle(session, cfg):
    dc = load_dividend_capture()
    ledger = DcLedger(session, cfg, dc)
    acc = ledger.ensure_account()
    assert acc.cash == pytest.approx(3_000_000)

    entry, ex, exit_ = date(2026, 6, 25), date(2026, 7, 2), date(2026, 8, 3)
    _seed_price(session, "sh600028", [(entry, 10.0), (ex, 10.0), (exit_, 11.0)])

    cycle = ledger.buy(_cand(entry, ex, exit_), entry, 10.0)
    assert cycle is not None and cycle.status == "HOLDING"
    pos = ledger.position()
    assert pos is not None
    shares = float(pos.shares_total)
    assert shares == 298_500

    ledger.broker.settle_eod()
    gross = ledger.credit_dividend(cycle, ex, 0.8)
    assert gross == pytest.approx(shares * 0.8)
    ev = session.query(DividendEvent).filter_by(account_id="DC").one()
    # 税前先入账，税递延到卖出
    assert float(ev.net_cash) == pytest.approx(gross)
    assert float(ev.tax_withheld) == 0.0

    closed = ledger.sell(cycle, exit_, 11.0)
    assert closed.status == "CLOSED"
    assert closed.tax == pytest.approx(gross * 0.10)
    assert closed.withdrawn == pytest.approx((gross - closed.tax) * 0.30)
    assert closed.net_pnl is not None and closed.net_pnl > 0
    assert closed.return_pct is not None and closed.return_pct > 0

    session.refresh(ev)
    assert float(ev.tax_withheld) == pytest.approx(closed.tax)
    assert float(ev.net_cash) == pytest.approx(gross - closed.tax)

    assert ledger.position() is None
    assert ledger.nav(exit_) == pytest.approx(float(acc.cash))

    # 同 symbol+ex_date 不允许重复建仓
    assert ledger.buy(_cand(entry, ex, exit_), entry, 10.0) is None


def test_bonus_shares_increase_and_cost_adjust(session, cfg):
    dc = load_dividend_capture()
    ledger = DcLedger(session, cfg, dc)
    ledger.ensure_account()
    entry, ex, exit_ = date(2026, 6, 25), date(2026, 7, 2), date(2026, 8, 3)
    _seed_price(session, "sh600028", [(entry, 10.0), (ex, 10.0), (exit_, 10.0)])
    cycle = ledger.buy(_cand(entry, ex, exit_), entry, 10.0)
    assert cycle is not None
    ledger.broker.settle_eod()
    before = float(ledger.position().shares_total)
    cost_before = float(ledger.position().cost_price)

    ledger.credit_dividend(cycle, ex, 0.0, bonus_ratio=0.7)

    pos = ledger.position()
    assert float(pos.shares_total) == pytest.approx(before * 1.7)
    assert float(pos.cost_price) == pytest.approx(cost_before / 1.7)
    assert ledger.sell(cycle, exit_, 10.0).status == "CLOSED"


def test_dc_nav_without_position_is_cash(session, cfg):
    dc = load_dividend_capture()
    ledger = DcLedger(session, cfg, dc)
    ledger.ensure_account()
    assert ledger.nav(date(2026, 6, 25)) == pytest.approx(3_000_000)
