"""baostock 数据源（备源，明确串行不支持并发）。"""
from __future__ import annotations

from datetime import date

import pandas as pd

from ..adjust import CorpAction
from .base import DataSource


class BaostockSource(DataSource):
    name = "baostock"

    def _bs(self):
        try:
            import baostock as bs  # type: ignore
        except Exception as e:  # pragma: no cover
            raise NotImplementedError("baostock SDK 未安装") from e
        return bs

    def fetch_daily(self, symbols: list[str], start: date, end: date) -> pd.DataFrame:
        bs = self._bs()
        lg = bs.login()
        if lg.error_code != "0":  # pragma: no cover
            raise RuntimeError("baostock login failed")
        frames = []
        try:
            for sym in symbols:
                code = self._to_bs_code(sym)
                rs = bs.query_history_k_data_plus(
                    code, "date,open,high,low,close,amount",
                    start_date=start.strftime("%Y-%m-%d"), end_date=end.strftime("%Y-%m-%d"),
                    frequency="d", adjustflag="2")  # 前复权
                rows = []
                while rs.next():
                    rows.append(rs.get_row_data())
                df = pd.DataFrame(rows, columns=["trade_date", "open", "high", "low",
                                                 "raw_close", "amount"])
                df["symbol"] = sym
                frames.append(df)
        finally:
            bs.logout()
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    def fetch_corp_actions(self, symbol: str, since: date) -> list[CorpAction]:
        return []

    def fetch_adj_factors(self, symbols: list[str], asof: date) -> pd.DataFrame:
        return pd.DataFrame()

    def fetch_symbol_status(self, asof: date) -> pd.DataFrame:
        return pd.DataFrame()

    def fetch_index(self, code: str, start: date, end: date) -> pd.DataFrame:
        return pd.DataFrame()

    @staticmethod
    def _to_bs_code(sym: str) -> str:
        s = sym.lower()
        if s.startswith("sh"):
            return "sh." + s[2:]
        if s.startswith("sz"):
            return "sz." + s[2:]
        return s
