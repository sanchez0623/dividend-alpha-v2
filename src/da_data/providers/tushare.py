"""Tushare 数据源（主源）。缺 SDK 或 token 时抛 NotImplementedError 由 fallback 接管。"""
from __future__ import annotations

import os
from datetime import date

import pandas as pd

from ..adjust import CorpAction
from .base import DataSource, RateLimited


class TushareSource(DataSource):
    name = "tushare"

    def __init__(self, token: str | None = None):
        self.token = token or os.environ.get("TUSHARE_TOKEN", "")

    def _api(self):
        if not self.token:
            raise NotImplementedError("TUSHARE_TOKEN 未配置")
        try:
            import tushare as ts  # type: ignore
        except Exception as e:  # pragma: no cover
            raise NotImplementedError("tushare SDK 未安装") from e
        ts.set_token(self.token)
        return ts.pro_api()

    def fetch_daily(self, symbols: list[str], start: date, end: date) -> pd.DataFrame:
        pro = self._api()
        frames = []
        for sym in symbols:
            ts_code = self._to_ts_code(sym)
            try:
                df = pro.daily(ts_code=ts_code, start_date=start.strftime("%Y%m%d"),
                               end_date=end.strftime("%Y%m%d"))
            except Exception as e:
                msg = str(e)
                if "每分钟" in msg or "限流" in msg or "-2001" in msg:
                    raise RateLimited(retry_after=15, source="tushare") from e
                raise
            if df is None or df.empty:
                continue
            df = df.rename(columns={"vol": "volume"})
            df["symbol"] = sym
            frames.append(df)
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, ignore_index=True)

    def fetch_corp_actions(self, symbol: str, since: date) -> list[CorpAction]:
        pro = self._api()
        df = pro.dividend(ts_code=self._to_ts_code(symbol))
        out: list[CorpAction] = []
        for _, r in df.iterrows():
            ex = r.get("ex_date")
            if not ex or pd.isna(ex):
                continue
            exd = pd.Timestamp(str(ex)).date()
            if exd < since:
                continue
            out.append(CorpAction(symbol=symbol, ex_date=exd,
                                  dps=float(r.get("cash_div") or 0)))
        return out

    def fetch_adj_factors(self, symbols: list[str], asof: date) -> pd.DataFrame:
        pro = self._api()
        frames = []
        for sym in symbols:
            df = pro.adj_factor(ts_code=self._to_ts_code(sym))
            if df is None or df.empty:
                continue
            df["symbol"] = sym
            frames.append(df)
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    def fetch_symbol_status(self, asof: date) -> pd.DataFrame:
        return pd.DataFrame()

    def fetch_index(self, code: str, start: date, end: date) -> pd.DataFrame:
        pro = self._api()
        return pro.index_daily(ts_code=code, start_date=start.strftime("%Y%m%d"),
                               end_date=end.strftime("%Y%m%d"))

    @staticmethod
    def _to_ts_code(sym: str) -> str:
        sym = sym.lower()
        if sym.startswith("sh") or sym.startswith("sz"):
            return sym[2:] + "." + sym[:2].upper()
        return sym
