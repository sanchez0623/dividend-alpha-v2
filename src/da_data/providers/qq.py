"""腾讯财经免费行情源（qt.gtimg.cn / ifzq.gtimg.cn）。

免 Key。两点价值：
1) 实时快照含【总市值 / 流通市值 / PE】—— fuyao prices 不返回市值，
   而 spec 的 universe 门槛（总市值 ≥300 亿）与 factor market_cap 需要它；
2) fqkline 提供 qfq 前复权日 K，可作 fuyao 的交叉校验 / 降级备源。

编码：返回 GBK，需解码。字段用 `~` 分隔。
代码前缀：sh 沪 / sz 深 / hk 港 / us 美。
"""
from __future__ import annotations

from datetime import date

import httpx
import pandas as pd

from ..adjust import CorpAction
from .base import DataSource
from .symbol_map import to_internal

_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}


class QQRealtimeSource(DataSource):
    name = "qq_free"

    def __init__(self, timeout: float = 20.0):
        self._client = httpx.Client(headers=_HEADERS, timeout=timeout)

    # ---------- 实时快照（含市值） ----------
    def fetch_snapshot(self, symbols: list[str]) -> pd.DataFrame:
        """返回 symbol/trade_date/last/open/high/low/prev_close/market_cap/float_cap/pe。"""
        codes = ",".join(symbols)
        r = self._client.get(f"https://qt.gtimg.cn/q={codes}")
        r.raise_for_status()
        text = r.content.decode("gbk", errors="replace")
        rows = []
        for line in text.strip().split("\n"):
            if "=" not in line:
                continue
            lhs = line.split("=", 1)[0].strip()      # e.g. v_sh601398
            body = line.split("=", 1)[1].strip().strip('";')
            f = body.split("~")
            if len(f) < 46:
                continue
            # ★ 从 key (v_sh601398) 取带交易所前缀的代码；QQ 的 ticker 字段无交易所后缀
            raw_code = lhs[2:] if lhs.lower().startswith("v_") else f[2]
            sym = to_internal(raw_code) or raw_code
            rows.append({
                "symbol": sym,
                "name": f[1],
                "last": _num(f[3]),
                "prev_close": _num(f[4]),
                "open": _num(f[5]),
                "high": _num(f[33]),
                "low": _num(f[34]),
                "volume": _num(f[36]),
                "pe": _num(f[39]),
                "float_cap": _num(f[44]),   # 流通市值（亿）
                "market_cap": _num(f[45]),  # 总市值（亿）
            })
        return pd.DataFrame(rows)

    # ---------- 前复权日 K（交叉校验 / 备源） ----------
    def fetch_qfq_daily(self, symbol: str, count: int = 320) -> pd.DataFrame:
        r = self._client.get(
            "https://ifzq.gtimg.cn/appstock/app/fqkline/get",
            params={"param": f"{symbol},day,,,{count},qfq"},
        )
        r.raise_for_status()
        data = r.json().get("data", {}).get(symbol, {})
        key = "qfqday" if "qfqday" in data else "day"  # ★ 选复权后键名变 qfqday
        bars = data.get(key, [])
        rows = []
        for b in bars:
            rows.append({
                "trade_date": pd.Timestamp(b[0]).date(),
                "open": float(b[1]), "close": float(b[2]),
                "high": float(b[3]), "low": float(b[4]),
                "volume": float(b[5]) if len(b) > 5 else None,
            })
        df = pd.DataFrame(rows)
        if not df.empty:
            df["symbol"] = to_internal(symbol) or symbol
        return df

    # ---------- DataSource 协议（快照填充市值状态） ----------
    def fetch_daily(self, symbols: list[str], start: date, end: date) -> pd.DataFrame:
        frames = []
        for sym in symbols:
            df = self.fetch_qfq_daily(sym, count=800)
            if df.empty:
                continue
            df = df[(df["trade_date"] >= start) & (df["trade_date"] <= end)]
            df = df.rename(columns={"close": "adj_close", "volume": "amount"})
            frames.append(df)
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    def fetch_corp_actions(self, symbol: str, since: date) -> list[CorpAction]:
        return []

    def fetch_adj_factors(self, symbols: list[str], asof: date) -> pd.DataFrame:
        return pd.DataFrame(columns=["symbol", "trade_date", "adj_factor"])

    def fetch_symbol_status(self, asof: date) -> pd.DataFrame:
        # 单独用 fetch_snapshot 填充市值；此处留空
        return pd.DataFrame()

    def fetch_index(self, code: str, start: date, end: date) -> pd.DataFrame:
        df = self.fetch_qfq_daily(code, count=800)
        return df[(df["trade_date"] >= start) & (df["trade_date"] <= end)] \
            if not df.empty else df


def _num(x):
    try:
        v = float(x)
        return v if v != 0 else None
    except (TypeError, ValueError):
        return None
