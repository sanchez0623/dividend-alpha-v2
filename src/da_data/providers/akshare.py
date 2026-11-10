"""akshare 数据源（备源）+ 交易日历。缺 SDK 时抛 NotImplementedError。"""
from __future__ import annotations

from datetime import date

import pandas as pd

from ..adjust import CorpAction
from .base import DataSource


def fetch_trade_calendar() -> list[date]:
    """交易日历：akshare tool_trade_date_hist_sina。缺 SDK 返回空（回退近似日历）。"""
    try:
        import akshare as ak  # type: ignore
    except Exception:
        return []
    df = ak.tool_trade_date_hist_sina()
    return [pd.Timestamp(d).date() for d in df["trade_date"].tolist()]


class AkshareSource(DataSource):
    name = "akshare"

    def _ak(self):
        try:
            import akshare as ak  # type: ignore
        except Exception as e:  # pragma: no cover
            raise NotImplementedError("akshare SDK 未安装") from e
        return ak

    def fetch_daily(self, symbols: list[str], start: date, end: date) -> pd.DataFrame:
        ak = self._ak()
        frames = []
        for sym in symbols:
            code = sym[2:] if sym[:2].lower() in ("sh", "sz") else sym
            raw = self._hist(ak, code, start, end, "qfq")
            raw_raw = self._hist(ak, code, start, end, "")
            if raw is None or raw.empty:
                continue
            df = raw_raw if raw_raw is not None and not raw_raw.empty else raw
            df = df.rename(columns={
                "日期": "trade_date", "开盘": "open", "最高": "high", "最低": "low",
                "收盘": "raw_close", "成交额": "amount",
            })
            df["symbol"] = sym
            if "trade_date" in df:
                df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
            # 前复权 close 与不复权 close 两列并存（★ C1）
            if "close" in raw.columns and "trade_date" in df.columns:
                adj_map = dict(zip(
                    pd.to_datetime(raw["日期"]).dt.date, raw["close"]))
                df["adj_close"] = df["trade_date"].map(adj_map)
            else:
                df["adj_close"] = df["raw_close"]
            df["adj_factor"] = df.apply(
                lambda r: (r["adj_close"] / r["raw_close"])
                if r.get("raw_close") else 1.0, axis=1)
            frames.append(df)
        if not frames:
            return pd.DataFrame()
        out = pd.concat(frames, ignore_index=True)
        keep = [c for c in ["symbol", "trade_date", "open", "high", "low", "raw_close",
                            "adj_close", "adj_factor", "amount"] if c in out.columns]
        return out[keep]

    @staticmethod
    def _hist(ak, code, start, end, adjust):
        try:
            return ak.stock_zh_a_hist(symbol=code, period="daily",
                                      start_date=start.strftime("%Y%m%d"),
                                      end_date=end.strftime("%Y%m%d"),
                                      adjust=adjust)
        except Exception:
            return None

    def fetch_corp_actions(self, symbol: str, since: date) -> list[CorpAction]:
        return []

    def fetch_adj_factors(self, symbols: list[str], asof: date) -> pd.DataFrame:
        return pd.DataFrame()

    def fetch_symbol_status(self, asof: date) -> pd.DataFrame:
        return pd.DataFrame()

    def fetch_index(self, code: str, start: date, end: date) -> pd.DataFrame:
        return pd.DataFrame()
