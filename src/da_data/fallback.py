"""多源 fallback：主源失败/限流时按序降级，429/4001 退避不切源。"""
from __future__ import annotations

import logging
from datetime import date

import pandas as pd

from .adjust import CorpAction
from .providers.akshare import AkshareSource
from .providers.baostock import BaostockSource
from .providers.base import DataSource, RateLimited
from .providers.qq import QQRealtimeSource
from .providers.synthetic import SyntheticSource
from .providers.ths_fuyao import ThsFuyaoSource
from .providers.tushare import TushareSource

log = logging.getLogger("da_data.fallback")


def build_chain(cfg: dict) -> list[DataSource]:
    """构造 fallback 链：用户指定的两个数据源优先（fuyao → akshare），
    再叠加其余备源；QQ 免费源（指数日线/快照）在合成兜底之前；
    末尾追加合成源（仅本地离线冒烟兜底）。"""
    chain: list[DataSource] = [ThsFuyaoSource(), AkshareSource(),
                               TushareSource(), BaostockSource(),
                               QQRealtimeSource()]
    return chain + [SyntheticSource()]


class FallbackChain:
    def __init__(self, sources: list[DataSource]):
        self.sources = sources
        self.last_source: str | None = None      # ★ 实际生效数据源（可观测性）
        self.last_errors: list[str] = []

    def fetch_daily(self, symbols: list[str], start: date, end: date) -> pd.DataFrame:
        errors: list[str] = []
        for src in self.sources:
            try:
                df = src.fetch_daily(symbols, start, end)
                if df is not None and not df.empty:
                    # ★ 记录实际生效源，供 ingest/前端展示真实 provenance
                    self.last_source = src.name
                    self.last_errors = errors
                    if "source" not in df.columns:
                        df["source"] = src.name
                    return df
                errors.append(f"{src.name}:empty")
            except RateLimited as e:
                # 限流：退避不切源、禁止立即连续重试 —— 记录后尝试下一源（生产应 sleep）
                log.warning("rate limited by %s retry_after=%.1fs", e.source, e.retry_after)
                errors.append(f"{src.name}:rate_limited")
            except NotImplementedError as e:
                errors.append(f"{src.name}:not_ready({e})")
            except Exception as e:  # noqa: BLE001
                errors.append(f"{src.name}:{type(e).__name__}")
        self.last_errors = errors
        raise RuntimeError("所有数据源失败: " + "; ".join(errors))

    def fetch_corp_actions(self, symbol: str, since: date) -> list[CorpAction]:
        """除权事件按链序取用。★ 合成源禁止供出除权事件（2026-11-08 加固）：
        演示分红写进 corp_action 会永久污染股息率因子（本地库曾中招：
        中石化被合成出每年 07-15 dps=0.986 的事件，TTM 虚增到 21%）。
        演示闭环（fetch-smoke/worker-once）不依赖合成除权，仅演示价格即可。"""
        errors: list[str] = []
        for src in self.sources:
            if src.name == "synthetic":
                log.warning("corp_action 拒绝合成源（防演示分红污染因子账本）")
                errors.append("synthetic:refused")
                continue
            try:
                actions = src.fetch_corp_actions(symbol, since)
                if actions:
                    self.last_source = f"{src.name}:corp_action"
                    self.last_errors = errors
                    return actions
                errors.append(f"{src.name}:empty")
            except NotImplementedError:
                errors.append(f"{src.name}:not_ready")
                continue
            except RateLimited as e:
                # 限流：退避不切源、禁止立即连续重试 —— 记录后尝试下一源
                log.warning("rate limited by %s retry_after=%.1fs (corp_action %s)",
                            e.source, e.retry_after, symbol)
                errors.append(f"{src.name}:rate_limited")
            except Exception as e:  # noqa: BLE001
                log.warning("corp_action %s via %s failed: %s", symbol, src.name, e)
                errors.append(f"{src.name}:{type(e).__name__}")
        self.last_errors = errors
        log.warning("corp_action %s 全部真实源失败: %s", symbol, "; ".join(errors))
        return []

    def fetch_index(self, code: str, start: date, end: date) -> pd.DataFrame:
        """指数日线（code 传裸 6 位码，如 000300）。按链序容错降级，
        每源做代码格式适配（QQ 要 sh 前缀 / fuyao 要 .SH 后缀）。"""
        for src in self.sources:
            try:
                src_code = code
                if src.name == "qq_free":
                    src_code = f"sh{code}"
                elif src.name == "ths_fuyao":
                    src_code = f"{code}.SH"
                df = src.fetch_index(src_code, start, end)
                if df is not None and not df.empty:
                    self.last_source = f"{src.name}:index"
                    return df
            except NotImplementedError:
                continue
            except RateLimited:
                continue
            except Exception as e:  # noqa: BLE001
                log.warning("index %s via %s failed: %s", code, src.name, e)
                continue
        return pd.DataFrame(columns=["trade_date", "close"])
