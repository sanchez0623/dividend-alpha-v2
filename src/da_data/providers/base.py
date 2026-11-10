"""数据源协议 + 限流异常。"""
from __future__ import annotations

from datetime import date
from typing import Protocol, runtime_checkable

import pandas as pd

from ..adjust import CorpAction


class RateLimited(Exception):
    def __init__(self, retry_after: float, source: str = "", code: int | str | None = None):
        self.retry_after = retry_after
        self.source = source
        self.code = code
        super().__init__(f"rate limited by {source} code={code} retry_after={retry_after}")


class BizError(Exception):
    def __init__(self, code, msg: str, source: str = ""):
        self.code = code
        self.msg = msg
        self.source = source
        super().__init__(f"[{source}] biz error code={code}: {msg}")


@runtime_checkable
class DataSource(Protocol):
    name: str

    def fetch_daily(self, symbols: list[str], start: date, end: date) -> pd.DataFrame: ...
    def fetch_corp_actions(self, symbol: str, since: date) -> list[CorpAction]: ...
    def fetch_adj_factors(self, symbols: list[str], asof: date) -> pd.DataFrame: ...
    def fetch_symbol_status(self, asof: date) -> pd.DataFrame: ...
    def fetch_index(self, code: str, start: date, end: date) -> pd.DataFrame: ...
