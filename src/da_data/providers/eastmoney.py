"""东方财富个股信息直连（免 SDK、免 Key）。

用途：证券简称 + 行业（industry_l1 口径）+ 精确总市值。
服务器无需安装 akshare，直接 HTTP；字段实测（2026-11-07）：
  f57=代码 f58=简称 f127=行业 f116=总市值（元）
"""
from __future__ import annotations

from datetime import date

import httpx
import pandas as pd

from ..adjust import CorpAction
from .base import DataSource
from .symbol_map import to_internal

_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}


def _secid(symbol: str) -> str | None:
    """内部 sh600036/sz000651 → 东财 secid（1.=沪 0.=深）。"""
    s = symbol.strip().lower()
    if len(s) == 8 and s[:2] in ("sh", "sz"):
        exch, code = s[:2], s[2:]
        return f"1.{code}" if exch == "sh" else f"0.{code}"
    return None  # bj 暂不支持


def fetch_individual_info(symbols: list[str]) -> pd.DataFrame:
    """批量取个股信息。返回 symbol/name/industry_l1/market_cap(元)。"""
    rows = []
    with httpx.Client(headers=_HEADERS, timeout=15) as c:
        for sym in symbols:
            secid = _secid(sym)
            if not secid:
                continue
            try:
                r = c.get("https://push2.eastmoney.com/api/qt/stock/get",
                          params={"secid": secid, "fields": "f57,f58,f127,f116"})
                d = (r.json() or {}).get("data") or {}
            except Exception:  # noqa: BLE001
                continue
            if not d.get("f58"):
                continue
            rows.append({
                "symbol": sym,
                "name": d.get("f58"),
                "industry_l1": d.get("f127"),
                "market_cap": float(d["f116"]) if d.get("f116") else None,
            })
    return pd.DataFrame(rows)


def _exchange_for(code: str, f13: object) -> str:
    """交易所判定：优先 f13（东财 clist 接口必须显式请求才返回该字段），
    缺失时按代码段兜底：6 开头=沪（主板/科创板），0/3=深（主板/创业板）。

    历史 bug：fields 不含 f13 时 x.get("f13") 恒为 None，全市场被误标 sz，
    沪市股票带着 sz600xxx 假代码在 S2 查同花顺失败被静默丢弃 → 候选池 0 沪市。
    """
    if f13 in (1, "1"):
        return "sh"
    if f13 in (0, "0"):
        return "sz"
    return "sh" if code.startswith("6") else "sz"


def fetch_all_a_spot() -> pd.DataFrame:
    """全 A 股快照（固定主域名 push2.eastmoney.com，不分片；分页拉取）。

    返回 symbol(内部格式)/name/last/close/market_cap(元)。
    fs 覆盖：沪深主板/创业板/科创板（不含北交所）。
    """
    rows = []
    with httpx.Client(headers=_HEADERS, timeout=20) as c:
        pn, total = 1, None
        while True:
            r = c.get("https://push2.eastmoney.com/api/qt/clist/get", params={
                "pn": pn, "pz": 500, "po": 1, "np": 1, "fltt": 2, "invt": 2,
                "fid": "f20",  # 按总市值排序
                "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
                # ★ f13（交易所）必须显式请求才返回；漏掉会导致沪市全部误判为深市
                "fields": "f12,f13,f14,f2,f20",
            })
            d = (r.json() or {}).get("data") or {}
            diff = d.get("diff") or []
            if total is None:
                total = int(d.get("total") or 0)
            for x in diff:
                code = str(x.get("f12", ""))
                if not code:
                    continue
                exch = _exchange_for(code, x.get("f13"))
                rows.append({
                    "symbol": exch + code,
                    "name": x.get("f14"),
                    "last": x.get("f2"),
                    "market_cap": x.get("f20"),
                })
            pn += 1
            if not diff or len(rows) >= total:
                break
    return pd.DataFrame(rows)


# ---------- 银行子类（领域常量：银行股分类规则，非可调参数） ----------
_STATE_BIG4 = {"工商银行", "建设银行", "农业银行", "中国银行", "交通银行", "邮储银行"}
_JOINT_STOCK = {"招商银行", "平安银行", "兴业银行", "浦发银行", "中信银行",
                "民生银行", "光大银行", "华夏银行", "浙商银行", "渤海银行", "恒丰银行"}


def bank_subtype_for(name: str | None, industry: str | None) -> str | None:
    """银行股子类：国有大行 / 股份行 / 城农商行；非银行返回 None。"""
    if not name or not industry or "银行" not in industry:
        return None
    if name in _STATE_BIG4:
        return "国有大行"
    if name in _JOINT_STOCK:
        return "股份行"
    return "城农商行"


class EastmoneyInfoSource(DataSource):
    """DataSource 协议占位实现（个股信息为主用途，K 线不在此源）。"""

    name = "eastmoney_info"

    def fetch_symbol_status(self, asof: date) -> pd.DataFrame:
        return fetch_individual_info([])

    def fetch_daily(self, symbols: list[str], start: date, end: date) -> pd.DataFrame:
        return pd.DataFrame()

    def fetch_corp_actions(self, symbol: str, since: date) -> list[CorpAction]:
        return []

    def fetch_adj_factors(self, symbols: list[str], asof: date) -> pd.DataFrame:
        return pd.DataFrame(columns=["symbol", "trade_date", "adj_factor"])

    def fetch_index(self, code: str, start: date, end: date) -> pd.DataFrame:
        return pd.DataFrame(columns=["trade_date", "close"])
