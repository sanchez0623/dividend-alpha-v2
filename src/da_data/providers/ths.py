"""同花顺 fuyao 客户端（数据源之一，已实测连通）。

契约（https://fuyao.aicubes.cn/docs/api-reference/overview/，2026-11-06 实测对齐）：
- Base URL: https://fuyao.aicubes.cn
- 认证：请求头 `X-api-key: <key>`（缺失/无效 → code=2001）
- 股票/指数用完整 thscode（如 600519.SH），不接受纯 ticker
- 业务结果经响应信封 code 表达；HTTP 429 或 code=4001 表示限流 → 降并发 + 退避，禁止立即连续重试
- thscode 可为 null，解析需容忍
"""
from __future__ import annotations

import random
import time
from datetime import date, datetime, timedelta, timezone

import httpx
import pandas as pd

from ..adjust import CorpAction
from .base import BizError, RateLimited

TZ_BJ = timezone(timedelta(hours=8))  # ★ fuyao 毫秒戳按 Asia/Shanghai 日界


class Backoff:
    def __init__(self, base_sec: float = 2, factor: float = 2, max_sec: float = 60,
                 jitter: bool = True):
        self.base = base_sec
        self.factor = factor
        self.max = max_sec
        self.jitter = jitter
        self._attempt = 0

    def next(self) -> float:
        delay = min(self.base * (self.factor ** self._attempt), self.max)
        self._attempt += 1
        if self.jitter:
            delay *= 0.5 + random.random() * 0.5
        return delay

    def reset(self) -> None:
        self._attempt = 0


def to_ms(d: date) -> int:
    """日期 → 毫秒时间戳（★ Asia/Shanghai 当日零点）。
    fuyao 日线按北京日界返回；若按 UTC 零点取整，end 会停在北京 08:00，
    拿不到当天收盘 bar。"""
    return int(datetime(d.year, d.month, d.day, tzinfo=TZ_BJ).timestamp() * 1000)


class ThsFuyaoClient:
    def __init__(self, api_key: str, base_url: str = "https://fuyao.aicubes.cn",
                 max_concurrency: int = 2, min_interval_sec: float = 0.5,
                 backoff: Backoff | None = None, transport: httpx.BaseTransport | None = None):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.max_concurrency = max_concurrency
        self.min_interval = min_interval_sec
        self.backoff = backoff or Backoff()
        self._last_call = 0.0
        self._transport = transport
        self._client = None
        if api_key:
            self._client = httpx.Client(
                base_url=self.base_url,
                headers={"X-api-key": api_key},  # ★ 官方契约：X-api-key，非 Bearer
                transport=transport,
                timeout=30.0,
            )

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            raise NotImplementedError("THS_API_KEY 未配置")
        return self._client

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_call
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_call = time.monotonic()

    def get(self, path: str, params: dict | None = None) -> dict:
        """同时检查 HTTP 状态码与响应信封 code（官方硬约束）。"""
        self._throttle()
        resp = self.client.get(path, params=params or {})
        if resp.status_code == 429:
            raise RateLimited(retry_after=self.backoff.next(), source="ths_fuyao", code=429)
        resp.raise_for_status()
        env = resp.json()
        code = env.get("code")
        if code not in (0, "0", None):
            if code == 4001:
                raise RateLimited(retry_after=self.backoff.next(), source="ths_fuyao", code=4001)
            raise BizError(code=code, msg=env.get("message", ""), source="ths_fuyao")
        self.backoff.reset()
        return env.get("data") or {}

    # ---------- capabilities（实测可用） ----------
    def prices_historical_batch(self, thscodes: list[str], start: date, end: date,
                                adjust: str = "forward") -> dict:
        """批量历史 K 线：最多 10 个代码，共用窗口。返回 data。"""
        if len(thscodes) > 10:
            raise ValueError("fuyao 批量历史 K 线单次最多 10 个代码")
        return self.get("/api/a-share/prices/historical-batch", {
            "thscodes": ",".join(thscodes), "interval": "1d",
            "start": to_ms(start), "end": to_ms(end), "adjust": adjust,
        })

    def adjustment_factors(self, thscode: str, since: date) -> list[dict]:
        """复权因子事件流（分红/送股）。返回原始 item 列表。"""
        data = self.get("/api/a-share/corporate-actions/adjustment-factors", {
            "thscode": thscode,
            "from": (since - pd.Timedelta(days=0)).isoformat(),
        })
        return data.get("item", []) or []

    def index_historical(self, thscode: str, start: date, end: date) -> list[dict]:
        """指数历史 K 线。★ 正确路径含 /prices 段（/api/a-share-index/prices/historical），
        单 thscode、仅 1d、近 5 自然年；item[] 直接是 PriceBarItem（date_ms/close_price…）。"""
        data = self.get("/api/a-share-index/prices/historical", {
            "thscode": thscode, "interval": "1d", "start": to_ms(start), "end": to_ms(end),
        })
        return data.get("item", []) or []

    def fetch_trade_calendar(self, start: date, end: date) -> list[date]:
        """A 股交易日序列（闭区间，Asia/Shanghai）。用于交易日判定 / 月末取现日。"""
        data = self.get("/api/a-share/calendar/trading-days", {
            "start_date": start.strftime("%Y%m%d"), "end_date": end.strftime("%Y%m%d"),
        })
        out = []
        for x in data.get("item", []) or []:
            ds = str(x.get("date") or "")
            if len(ds) == 8:
                out.append(datetime.strptime(ds, "%Y%m%d").date())
        return out

    # ---------- 解析 ----------
    @staticmethod
    def to_corp_actions(internal_symbol: str, rows: list[dict]) -> list[CorpAction]:
        out: list[CorpAction] = []
        for r in rows:
            ex_ms = r.get("ex_date_ms")
            if not ex_ms:
                continue
            ex = datetime.fromtimestamp(ex_ms / 1000, tz=TZ_BJ).date()
            out.append(CorpAction(
                symbol=internal_symbol,
                ex_date=ex,
                dps=float(r.get("dividend_per_share") or 0),
                bonus_ratio=float(r.get("per_share_bonus") or 0),
            ))
        return out
