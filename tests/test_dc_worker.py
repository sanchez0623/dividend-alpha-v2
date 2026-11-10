"""DC 每日闭环集成测试：假数据源，不联网，覆盖刷新→买入→分红→卖出→幂等。"""
from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import pytest

from src.da_core.dc_config import load_dividend_capture
from src.da_data import dc_universe
from src.da_data.providers.ths import TZ_BJ, ThsFuyaoClient
from src.da_worker.dividend_capture_close import run_dividend_capture_close
from src.da_strategy.dividend_capture import entry_date_for, exit_date_for

SYM = "sh600028"
EX = date(2026, 7, 1)
CAL = [d.date() for d in pd.bdate_range("2026-01-01", "2026-12-31")]
ENTRY = entry_date_for(EX, CAL, 5)
EXIT = exit_date_for(EX, CAL, 30)


def _ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=TZ_BJ).timestamp() * 1000)


class FakeFuyao:
    to_corp_actions = staticmethod(ThsFuyaoClient.to_corp_actions)

    def adjustment_factors(self, thscode, since):
        return [
            {"ex_date_ms": _ms(date(2025, 7, 1)), "dividend_per_share": 0.7,
             "per_share_bonus": 0},
            {"ex_date_ms": _ms(EX), "dividend_per_share": 0.8,
             "per_share_bonus": 0},
        ]

    def fetch_trade_calendar(self, start, end):
        return [d for d in CAL if start <= d <= end]


class FakeChain:
    def __init__(self):
        self.calls = 0

    def fetch_daily(self, symbols, start, end):
        self.calls += 1
        rows = []
        for i, d in enumerate(CAL):
            if d < start or d > end:
                continue
            px = 8.0 + 4.0 * (i / max(len(CAL) - 1, 1))
            rows.append({"symbol": SYM, "trade_date": d, "open": px, "high": px,
                         "low": px, "raw_close": px, "amount": 1e8,
                         "source": "fake"})
        return pd.DataFrame(rows)


def _patch_spot(monkeypatch):
    df = pd.DataFrame([{"symbol": SYM, "name": "中国石化", "last": 10.0,
                        "market_cap": 6.0e10}])
    monkeypatch.setattr(dc_universe, "fetch_all_a_spot", lambda: df)


def test_worker_full_cycle(session, cfg, monkeypatch):
    _patch_spot(monkeypatch)
    dc = load_dividend_capture()
    chain = FakeChain()
    fuyao = FakeFuyao()

    # 1) 买点日：刷新池 → 选股 → 买入
    r1 = run_dividend_capture_close(session, dc, cfg, asof=ENTRY, chain=chain,
                                    fuyao=fuyao, calendar_days=CAL)
    assert r1["idempotent"] is False
    assert any(a["kind"] == "BUY" for a in r1["actions"]), r1
    from src.da_core.models import DcCycle, Position
    cycle = session.query(DcCycle).filter_by(account_id="DC", status="HOLDING").one()
    assert cycle.symbol == SYM
    pos = session.query(Position).filter_by(account_id="DC", symbol=SYM).one()
    shares = float(pos.shares_total)
    assert shares > 0

    # 2) 除权日：税前分红入账
    r2 = run_dividend_capture_close(session, dc, cfg, asof=EX, chain=chain,
                                    fuyao=fuyao, calendar_days=CAL)
    assert any(a["kind"] == "DIVIDEND" for a in r2["actions"]), r2
    session.refresh(cycle)
    assert float(cycle.gross_dividend) > 0
    gross = float(cycle.gross_dividend)
    assert gross > 0

    # 3) 除权+30 天：卖出，扣 10% 税 + 30% 净分红取现
    r3 = run_dividend_capture_close(session, dc, cfg, asof=EXIT, chain=chain,
                                    fuyao=fuyao, calendar_days=CAL)
    assert any(a["kind"] == "SELL" for a in r3["actions"]), r3
    session.refresh(cycle)
    assert cycle.status == "CLOSED"
    assert float(cycle.tax) > 0
    assert float(cycle.withdrawn) > 0
    # 持有约 37 天 → 10% 档
    assert float(cycle.tax) / gross == pytest.approx(0.10, abs=1e-9)

    # 4) 幂等：同日重跑不再交易
    r4 = run_dividend_capture_close(session, dc, cfg, asof=EXIT, chain=chain,
                                    fuyao=fuyao, calendar_days=CAL)
    assert r4["idempotent"] is True
    assert r4["actions"] == []
