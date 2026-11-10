"""DC 选股/趋势/排序/日期/结算纯逻辑测试（无网络）。"""
from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import numpy as np
import pandas as pd

from src.da_core.dc_config import load_dividend_capture
from src.da_strategy.dividend_capture import (
    DcCandidate,
    DcRules,
    TrendState,
    compute_trend,
    entry_date_for,
    evaluate_candidate,
    exit_date_for,
    rank_candidates,
    settle_cycle,
    trend_eligible,
    ttm_dividend,
)


def _rules() -> DcRules:
    return DcRules.from_strategy(load_dividend_capture())


def _series(prices: list[float], end: date = date(2026, 6, 1)) -> pd.Series:
    idx = pd.bdate_range(end=pd.Timestamp(end), periods=len(prices))
    return pd.Series(prices, index=idx)


def _calendar(start: date, end: date) -> list[date]:
    return [d.date() for d in pd.bdate_range(start=pd.Timestamp(start), end=pd.Timestamp(end))]


def test_trend_labels():
    r = _rules()
    up = _series(list(np.linspace(10, 20, 220)))
    down = _series(list(np.linspace(20, 10, 220)))
    assert compute_trend(up, up.index[-1].date(), r).label in ("up", "up_pull")
    assert compute_trend(down, down.index[-1].date(), r).label == "down"
    assert compute_trend(_series([10.0] * 10), date(2026, 6, 1), r).label == "insufficient"


def test_trend_eligibility_modes():
    r = _rules()
    up = TrendState("up")
    down = TrendState("down")
    pull = TrendState("up_pull")
    nondown = TrendState("nondown")
    assert trend_eligible(up, replace(r, trend_mode="nondown")) is True
    assert trend_eligible(down, replace(r, trend_mode="nondown")) is False
    assert trend_eligible(nondown, replace(r, trend_mode="up")) is False
    assert trend_eligible(pull, replace(r, trend_mode="up_pull")) is True


def test_entry_exit_dates():
    cal = _calendar(date(2026, 1, 1), date(2026, 12, 31))
    ex = date(2026, 7, 1)
    entry = entry_date_for(ex, cal, 5)
    exit_ = exit_date_for(ex, cal, 30)
    assert entry is not None and entry < ex
    assert exit_ is not None and exit_ >= ex + timedelta(days=30)
    assert entry_date_for(cal[0], cal, 5) is None


def test_evaluate_candidate_filters():
    r = _rules()
    cal = _calendar(date(2025, 1, 1), date(2026, 12, 31))
    ex = date(2026, 7, 1)
    entry = entry_date_for(ex, cal, 5)
    up = _series(list(np.linspace(10, 20, 220)), end=entry)
    down = _series(list(np.linspace(20, 10, 220)), end=entry)

    def ev(**kw):
        base = dict(symbol="sh600028", name="中国石化", ex_date=ex, dps=0.8,
                    ttm_dps=0.8, market_cap=6.0e10, calendar=cal,
                    raw_price=10.0, adj_series=up, asof=entry, rules=r)
        base.update(kw)
        return evaluate_candidate(**base)

    ok = ev()
    assert ok is not None and ok.eligible and ok.div_yield == 0.08
    low_yield = ev(ttm_dps=0.5)
    assert low_yield is not None and not low_yield.eligible
    assert "yield" in low_yield.reason
    small_cap = ev(market_cap=3.0e10)
    assert small_cap is not None and not small_cap.eligible
    assert "cap" in small_cap.reason
    downtrend = ev(adj_series=down)
    assert downtrend is not None and not downtrend.eligible
    assert "trend" in downtrend.reason


def test_rank_prefers_trend_then_yield():
    r = _rules()
    ex = date(2026, 7, 1)
    entry = date(2026, 6, 24)
    exit_ = date(2026, 7, 31)

    def cand(sym: str, trend: str, y: float) -> DcCandidate:
        return DcCandidate(symbol=sym, name=None, ex_date=ex, entry_date=entry,
                           exit_date=exit_, dps=0.8, ttm_dps=0.8, price=10.0,
                           div_yield=y, market_cap=6e10, trend=TrendState(trend),
                           eligible=True)

    ranked = rank_candidates([
        cand("sz000001", "up", 0.09),
        cand("sh600028", "up_pull", 0.07),
        cand("sh601398", "nondown", 0.10),
    ])
    assert [c.symbol for c in ranked] == ["sh600028", "sz000001", "sh601398"]
    assert [c.rank for c in ranked] == [1, 2, 3]


def test_ttm_dividend_window():
    asof = date(2026, 7, 1)
    events = [(asof - timedelta(days=400), 1.0),
              (asof - timedelta(days=300), 0.3),
              (asof, 0.5)]
    assert ttm_dividend(events, asof) == 0.8


def test_settle_cycle_math():
    r = _rules()
    st = settle_cycle(entry_date=date(2026, 1, 2), exit_date=date(2026, 2, 10),
                      shares=1000, buy_price=10, buy_cost=50,
                      sell_price=11, sell_cost=60, gross_dividend=800, rules=r)
    assert st.holding_days == 39
    assert st.tax == 80.0
    assert st.net_dividend == 720.0
    assert st.withdrawn == 216.0
    assert st.net_pnl == 1394.0
    assert abs(st.return_pct - 0.1394) < 1e-9


def test_settle_cycle_tax_brackets():
    r = _rules()
    from src.da_strategy.dividend_capture import dividend_tax_for

    assert dividend_tax_for(20, 1000, r) == 200.0
    assert dividend_tax_for(40, 1000, r) == 100.0
    assert dividend_tax_for(400, 1000, r) == 0.0
