"""同花顺 fuyao 数据源适配（实现 DataSource 协议）。

★ C1 关键：一次取回 adjust=forward（前复权 close）与 adjust=none（不复权 raw_close），
两列并存落库；adj_factor = adj_close / raw_close。官方已预计算前复权价，
corporate-actions 事件流用于因子交叉校验与分红到账。
"""
from __future__ import annotations

from datetime import date, datetime

import pandas as pd

from ..adjust import CorpAction
from .base import DataSource
from .symbol_map import to_internal, to_thscode
from .ths import TZ_BJ, ThsFuyaoClient

_COLS = ["symbol", "trade_date", "open", "high", "low", "raw_close", "adj_close", "amount"]


class ThsFuyaoSource(DataSource):
    name = "ths_fuyao"

    def __init__(self, client: ThsFuyaoClient | None = None):
        import os

        self.client = client or ThsFuyaoClient(
            api_key=os.environ.get("THS_API_KEY", ""),
        )

    def fetch_daily(self, symbols: list[str], start: date, end: date) -> pd.DataFrame:
        frames = []
        codes = [s for s in (to_thscode(x) for x in symbols) if s]
        # 批量接口每次最多 10 个
        for i in range(0, len(codes), 10):
            batch = codes[i:i + 10]
            adj = self._bars(batch, start, end, "forward")
            raw = self._bars(batch, start, end, "none")
            merged = adj.merge(raw, on=["symbol", "trade_date"], how="outer",
                               suffixes=("", "_raw"))
            frames.append(merged)
        if not frames:
            return pd.DataFrame(columns=_COLS)
        df = pd.concat(frames, ignore_index=True)
        df["adj_factor"] = df.apply(
            lambda r: (r["close"] / r["close_raw"])
            if r.get("close_raw") else 1.0, axis=1)
        df["raw_close"] = df["close_raw"]
        df["adj_close"] = df["close"]
        df["amount"] = df.get("turnover")
        return df[_COLS]

    def _bars(self, batch: list[str], start: date, end: date, adjust: str) -> pd.DataFrame:
        data = self.client.prices_historical_batch(batch, start, end, adjust)
        rows = []
        for group in data.get("item", []):
            internal = to_internal(group.get("thscode") or "")
            if not internal:
                continue
            for b in group.get("item", []):
                # ★ fuyao date_ms 为北京时间当日零点，按东八区取日期（UTC 截断会偏一天）
                rows.append({
                    "symbol": internal,
                    "trade_date": datetime.fromtimestamp(
                        b["date_ms"] / 1000, tz=TZ_BJ).date(),
                    "open": b.get("open_price"),
                    "high": b.get("high_price"),
                    "low": b.get("low_price"),
                    "close": b.get("close_price"),
                    "turnover": b.get("turnover"),
                })
        return pd.DataFrame(rows)

    def fetch_corp_actions(self, symbol: str, since: date) -> list[CorpAction]:
        ths = to_thscode(symbol)
        if not ths:
            return []
        rows = self.client.adjustment_factors(ths, since)
        return self.client.to_corp_actions(symbol, rows)

    def fetch_adj_factors(self, symbols: list[str], asof: date) -> pd.DataFrame:
        # 官方已预计算前复权价，因子由 fetch_daily 内 adj/raw 比值得到
        return pd.DataFrame(columns=["symbol", "trade_date", "adj_factor"])

    def fetch_symbol_status(self, asof: date) -> pd.DataFrame:
        return pd.DataFrame()

    def fetch_index(self, code: str, start: date, end: date) -> pd.DataFrame:
        ths = to_thscode(code) or code
        rows = self.client.index_historical(ths, start, end)
        return pd.DataFrame([{
            "trade_date": datetime.fromtimestamp(r["date_ms"] / 1000, tz=TZ_BJ).date(),
            "close": r.get("close_price"),
        } for r in rows])
