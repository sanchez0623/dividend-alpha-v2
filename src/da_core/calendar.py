"""交易日历。生产接 akshare tool_trade_date_hist_sina；本地/测试用内置近似日历。

硬约束：只向前使用；不得回补 2015-2026 历史。
"""
from __future__ import annotations

from datetime import date, timedelta


def is_trading_day(d: date) -> bool:
    """近似判断 A 股交易日：周一至周五且非已知节假日。生产应替换为真实日历源。"""
    if d.weekday() >= 5:
        return False
    # 年末 12 月最后交易日用于取现；此处不做节假日精确剔除，
    # 生产由 da_data/providers/akshare.py 的日历源覆盖。
    return True


def last_trading_day_of_december(year: int) -> date:
    """返回该年 12 月最后一个工作日（近似；生产由日历源校正）。"""
    d = date(year, 12, 31)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def next_trading_day(d: date) -> date:
    nd = d + timedelta(days=1)
    while not is_trading_day(nd):
        nd += timedelta(days=1)
    return nd


def prev_trading_day(d: date) -> date:
    pd_ = d - timedelta(days=1)
    while not is_trading_day(pd_):
        pd_ -= timedelta(days=1)
    return pd_


def trading_days_between(start: date, end: date) -> int:
    """(start, end] 之间的近似交易日数。"""
    if end <= start:
        return 0
    n = 0
    cur = start
    while cur < end:
        cur += timedelta(days=1)
        if is_trading_day(cur):
            n += 1
    return n
