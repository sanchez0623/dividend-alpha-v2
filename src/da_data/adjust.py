"""前复权（★ 变更 C1 的架构落点）。

技术指标（RSI14 / 20日跌幅）用 adj_close；股息率 TTM 用 raw_close。两列并存。
build_adj_factor 按除权事件反推前复权因子（最新日 = 1.0），新除权会改写历史 adj_close。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class CorpAction:
    symbol: str
    ex_date: date
    dps: float = 0.0
    bonus_ratio: float = 0.0
    transfer_ratio: float = 0.0
    rights_ratio: float = 0.0
    rights_price: float = 0.0
    announce_date: date | None = None
    pay_date: date | None = None


def build_adj_factor(raw: pd.Series, events: list[CorpAction]) -> pd.Series:
    """
    raw: 以 trade_date 为索引的不复权收盘价（升序）。
    返回前复权因子（最新日 = 1.0）。

    A 股除权除息参考价 = (前收 − 现金红利 + 配股价×配股比例) / (1 + 送股 + 转增 + 配股)
    """
    raw = raw.sort_index()
    f = pd.Series(1.0, index=raw.index)
    for ev in sorted(events, key=lambda e: e.ex_date, reverse=True):
        if ev.ex_date not in raw.index:
            continue
        i = raw.index.get_loc(ev.ex_date)
        if i == 0:
            continue
        prev_close = float(raw.iloc[i - 1])
        num = prev_close - ev.dps + ev.rights_ratio * ev.rights_price
        den = 1.0 + ev.bonus_ratio + ev.transfer_ratio + ev.rights_ratio
        if den <= 0 or prev_close <= 0:
            continue
        theo = num / den
        ratio = theo / prev_close
        f.iloc[:i] *= ratio
    return f


def adj_close_series(raw: pd.Series, events: list[CorpAction]) -> pd.Series:
    f = build_adj_factor(raw, events)
    return raw * f


def compute_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder RSI。"""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi


def compute_drop(close: pd.Series, lookback: int = 20) -> pd.Series:
    return close / close.shift(lookback) - 1.0
