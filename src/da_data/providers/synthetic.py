"""确定性合成源（PAPER 模式 / 离线冒烟）。

Forward-only 生产优先官方源；当所有外部源不可用（T00 白名单未通 / token 未配置）时，
用确定性合成数据跑通数据→因子→信号→账本闭环，保证 fetch-smoke / ledger-init / worker-once 可验证。
合成数据带明确 source='synthetic'，UI 标注为「演示」，不参与实盘决策。
"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd

from ..adjust import CorpAction
from .base import DataSource

# 10 只跨行业大市值高股息标的（演示池）
DEMO_UNIVERSE: list[dict] = [
    {"symbol": "sh601398", "name": "工商银行", "l1": "银行", "sub": "国有大型", "yld": 0.055},
    {"symbol": "sh600036", "name": "招商银行", "l1": "银行", "sub": "股份制", "yld": 0.048},
    {"symbol": "sh601288", "name": "农业银行", "l1": "银行", "sub": "国有大型", "yld": 0.052},
    {"symbol": "sh601988", "name": "中国银行", "l1": "银行", "sub": "国有大型", "yld": 0.050},
    {"symbol": "sh601088", "name": "中国神华", "l1": "煤炭", "sub": None, "yld": 0.060},
    {"symbol": "sh600900", "name": "长江电力", "l1": "公用事业", "sub": None, "yld": 0.032},
    {"symbol": "sh600028", "name": "中国石化", "l1": "石油石化", "sub": None, "yld": 0.058},
    {"symbol": "sh601857", "name": "中国石油", "l1": "石油石化", "sub": None, "yld": 0.045},
    {"symbol": "sz000651", "name": "格力电器", "l1": "家用电器", "sub": None, "yld": 0.052},
    {"symbol": "sh600585", "name": "海螺水泥", "l1": "建材", "sub": None, "yld": 0.040},
]


def trading_dates(end: date, n: int) -> list[date]:
    """生成截至 end 的 n 个近似交易日（周一~周五）。"""
    out: list[date] = []
    d = end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return list(reversed(out))


class SyntheticSource(DataSource):
    name = "synthetic"

    def __init__(self, seed: int = 42, end: date | None = None, window: int = 90):
        self.seed = seed
        self.end = end or date.today()
        self.window = window

    def universe(self) -> list[dict]:
        return DEMO_UNIVERSE

    def fetch_daily(self, symbols: list[str], start: date, end: date) -> pd.DataFrame:
        rng = np.random.default_rng(self.seed)
        dates = trading_dates(end, self.window)
        frames = []
        for i, meta in enumerate(DEMO_UNIVERSE):
            if symbols and meta["symbol"] not in symbols:
                continue
            base = 5.0 + i * 2.0
            ret = rng.normal(0.0003, 0.012, size=len(dates))
            close = base * np.cumprod(1 + ret)
            df = pd.DataFrame({
                "symbol": meta["symbol"],
                "trade_date": dates,
                "open": close * (1 + rng.normal(0, 0.004, len(dates))),
                "high": close * (1 + rng.normal(0.003, 0.004, len(dates))),
                "low": close * (1 + rng.normal(-0.003, 0.004, len(dates))),
                "raw_close": close,
                "amount": rng.uniform(2e8, 8e8, len(dates)),
                "source": self.name,
            })
            frames.append(df)
        if not frames:
            return pd.DataFrame()
        out = pd.concat(frames, ignore_index=True)
        return out[(out["trade_date"] >= start) & (out["trade_date"] <= end)]

    def fetch_corp_actions(self, symbol: str, since: date) -> list[CorpAction]:
        meta = next((m for m in DEMO_UNIVERSE if m["symbol"] == symbol), None)
        if not meta:
            return []
        base = 5.0 + DEMO_UNIVERSE.index(meta) * 2.0
        dps = round(meta["yld"] * base, 4)
        out: list[CorpAction] = []
        # 近 6 年每年一次现金分红（保证 consecutive_div_years≥5、TTM 有值）
        for y in range(since.year, self.end.year + 1):
            ex = date(y, 7, 15)
            if ex < since or ex > self.end:
                continue
            out.append(CorpAction(symbol=symbol, ex_date=ex, dps=dps))
        return out

    def fetch_adj_factors(self, symbols: list[str], asof: date) -> pd.DataFrame:
        return pd.DataFrame(columns=["symbol", "trade_date", "adj_factor"])

    def fetch_symbol_status(self, asof: date) -> pd.DataFrame:
        rows = []
        for m in DEMO_UNIVERSE:
            rows.append({
                "symbol": m["symbol"], "trade_date": asof, "is_st": False,
                "is_suspended": False, "limit_up": False, "limit_down": False,
                "list_date": date(2000, 1, 1), "delist_date": None,
                "market_cap": 3.0e10 * (2 + DEMO_UNIVERSE.index(m)),
                "industry_l1": m["l1"], "industry_l3": m["l1"], "bank_subtype": m["sub"],
            })
        return pd.DataFrame(rows)

    def fetch_index(self, code: str, start: date, end: date) -> pd.DataFrame:
        rng = np.random.default_rng(self.seed + 1)
        dates = trading_dates(end, self.window)
        ret = rng.normal(0.0002, 0.010, size=len(dates))
        close = 3000 * np.cumprod(1 + ret)
        return pd.DataFrame({"trade_date": dates, "close": close, "code": code})
