"""★ C1 前复权正确性（04 §4.5 的 A1–A4）。"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from src.da_data.adjust import CorpAction, adj_close_series, build_adj_factor, compute_drop, \
    compute_rsi


def _raw_series(events, start=date(2025, 6, 1), end=date(2026, 6, 30)):
    idx = pd.date_range(start, end, freq="B")
    raw = pd.Series(20.0, index=[d.date() for d in idx])
    return raw


def test_a1_ex_date_smoothing():
    raw = _raw_series(None)
    dps = 0.5
    ev = CorpAction(symbol="sh601398", ex_date=date(2026, 1, 5), dps=dps)
    adj = adj_close_series(raw, [ev])
    ex = date(2026, 1, 5)
    i = raw.index.get_loc(ex)
    raw_jump = raw.iloc[i] / raw.iloc[i - 1] - 1
    adj_jump = adj.iloc[i] / adj.iloc[i - 1] - 1
    # 原价除权日无跳变（合成数据恒为 20），复权后除权日前的历史价被下调
    assert abs(raw_jump) < 1e-9
    # 除权前因子 < 1（前复权历史价更低），最新日 = 1.0
    assert adj.iloc[i - 1] < adj.iloc[-1]
    factor = build_adj_factor(raw, [ev])
    assert abs(factor.iloc[-1] - 1.0) < 1e-9
    assert factor.iloc[i - 1] < 1.0


def test_a2_no_false_signal_on_ex_date():
    raw = _raw_series(None)
    ev = CorpAction(symbol="sh601900", ex_date=raw.index[40], dps=0.6)
    adj = adj_close_series(raw, [ev])
    rsi = compute_rsi(adj, 14)
    drop20 = compute_drop(adj, 20)
    ex = ev.ex_date
    # 复权后除权日不触发 combo（跌幅 ≥12% 且 RSI<25）
    assert not (rsi.loc[ex] < 25 and drop20.loc[ex] < -0.12)


def test_a3_yield_independent_of_adjust():
    raw = _raw_series(None)
    ev = CorpAction(symbol="x", ex_date=raw.index[30], dps=1.0)
    adj = adj_close_series(raw, [ev])
    div_ttm = 1.0
    yld_raw = div_ttm / raw
    yld_adj = div_ttm / adj
    assert (yld_raw.between(0, 0.30)).all()
    # 口径不同：不复权股息率 ≠ 复权股息率
    assert not yld_raw.equals(yld_adj)


def test_a4_window_completeness():
    raw = _raw_series(None, start=date(2026, 1, 1), end=date(2026, 6, 30))
    assert len(raw) >= 60
    ev = CorpAction(symbol="x", ex_date=raw.index[30], dps=0.4)
    adj = adj_close_series(raw, [ev])
    assert adj.notna().all()
