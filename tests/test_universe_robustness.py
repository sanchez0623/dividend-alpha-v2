"""漏斗可复现性加固测试：重试 / 失败记录 / 确定性错误 / 熔断 / 同日幂等复用。

背景：旧实现 S2/S3 对单只查询失败 except: continue 静默丢弃，网络抖动导致
两次运行候选池不同 → 选股不可复现。加固后：
  - 限流/网络错误按配置重试；1002/3002 确定性业务错误不重试；
  - 所有最终失败计入 funnel_audit（WARNING 日志 + s2_failures_json）；
  - 失败率超 max_fail_ratio 熔断本次漏斗（返回 None，不落缓存）；
  - 同 asof 重跑直接复用已验证结果（幂等）。
"""
from __future__ import annotations

import json
from datetime import date, datetime

import pandas as pd

from src.da_core.models import FunnelAudit, UniverseCache
from src.da_data import universe
from src.da_data.providers.base import BizError, RateLimited
from src.da_data.providers.ths import TZ_BJ, ThsFuyaoClient

ASOF = date(2026, 11, 5)
SYMS = ["sh600036", "sz000651", "sz000001"]


def _evt(year: int, dps: float = 0.3) -> dict:
    ms = int(datetime(year, 7, 1, tzinfo=TZ_BJ).timestamp() * 1000)
    return {"ex_date_ms": ms, "dividend_per_share": dps, "per_share_bonus": 0}


def _ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, 15, tzinfo=TZ_BJ).timestamp() * 1000)


class FakeFuyao:
    """behavior: 内部 symbol → 异常序列；第 n 次调用抛 seq[n-1]，序列耗尽后成功。"""

    to_corp_actions = staticmethod(ThsFuyaoClient.to_corp_actions)

    def __init__(self, behavior: dict | None = None):
        self.behavior = behavior or {}
        self.calls: dict[str, int] = {}

    def adjustment_factors(self, thscode: str, since):
        sym = thscode[-2:].lower() + thscode[:6]  # "600036.SH" → "sh600036"
        self.calls[sym] = self.calls.get(sym, 0) + 1
        seq = self.behavior.get(sym, [])
        if self.calls[sym] <= len(seq):
            exc = seq[self.calls[sym] - 1]
            if exc is not None:
                raise exc
        return [_evt(y) for y in range(2022, 2027)]  # 连续 5 年分红

    def prices_historical_batch(self, thscodes, start, end, adjust="none"):
        return {"item": [{"thscode": t,
                          "item": [{"date_ms": _ms(ASOF), "close_price": 10.0}]}
                         for t in thscodes]}


def _patch_spot(monkeypatch) -> None:
    df = pd.DataFrame([
        {"symbol": s, "name": f"股{s}", "last": 10.0, "market_cap": 5e10 + i}
        for i, s in enumerate(SYMS)])
    monkeypatch.setattr(universe, "fetch_all_a_spot", lambda: df)


def _patch_info(monkeypatch) -> None:
    def info_fn(symbols):
        return pd.DataFrame([
            {"symbol": s, "name": f"股{s}", "industry_l1": f"行业{s}",
             "market_cap": 5e10} for s in symbols])
    monkeypatch.setattr(universe, "fetch_individual_info", info_fn)


def _quiet_cfg(cfg, **over) -> None:
    cfg.data["funnel"] = {"retry_attempts": 3, "retry_backoff_sec": 0,
                          "max_fail_ratio": 0.05, **over}


def test_success_audit_and_same_day_reuse(session, cfg, monkeypatch):
    """全成功 → 审计落库 + 快照存证；同 asof 重跑零请求、结果逐字一致。"""
    _quiet_cfg(cfg)
    _patch_spot(monkeypatch)
    _patch_info(monkeypatch)
    f = FakeFuyao()
    out = universe.build_candidates(session, None, cfg, ASOF, fuyao=f)
    assert out == sorted(SYMS)

    audit = session.get(FunnelAudit, ASOF)
    assert audit is not None and audit.status == "ok"
    assert audit.s1_count == 3 and audit.s2_scanned == 3 and audit.s2_failed == 0
    snap = json.loads(audit.snapshot_json)
    assert [r["symbol"] for r in snap] == sorted(SYMS)
    assert json.loads(session.get(UniverseCache, ASOF).symbols_json) == sorted(SYMS)

    class Boom:
        def adjustment_factors(self, *a, **k):
            raise AssertionError("同日重跑不应再请求上游")

    out2 = universe.build_candidates(session, None, cfg, ASOF, fuyao=Boom())
    assert out2 == out


def test_permanent_unknown_thscode_recorded(session, cfg, monkeypatch):
    """1002 Unknown thscode：确定性错误不重试、计失败、不熔断（阈值放宽）。"""
    _quiet_cfg(cfg, max_fail_ratio=0.9)
    _patch_spot(monkeypatch)
    _patch_info(monkeypatch)
    f = FakeFuyao(behavior={
        "sz000001": [BizError(1002, "Unknown thscode: 000001.SZ", "ths_fuyao")]})
    out = universe.build_candidates(session, None, cfg, ASOF, fuyao=f)
    assert out == ["sh600036", "sz000651"]

    audit = session.get(FunnelAudit, ASOF)
    assert audit.status == "ok" and audit.s2_failed == 1
    fails = json.loads(audit.s2_failures_json)
    assert fails[0]["symbol"] == "sz000001" and fails[0]["code"] == "1002"
    assert f.calls["sz000001"] == 1


def test_no_events_3002_treated_as_empty(session, cfg, monkeypatch):
    """3002 无除权事件：语义为空事件流（确定性数据事实），不计失败、不重试，
    标的因连续分红 0 年被年限筛自然剔除。"""
    _quiet_cfg(cfg)
    _patch_spot(monkeypatch)
    _patch_info(monkeypatch)
    f = FakeFuyao(behavior={
        "sz000001": [BizError(3002, "No adjustment events for thscode=000001.SZ",
                              "ths_fuyao")]})
    out = universe.build_candidates(session, None, cfg, ASOF, fuyao=f)
    assert out == ["sh600036", "sz000651"]

    audit = session.get(FunnelAudit, ASOF)
    assert audit.status == "ok" and audit.s2_failed == 0
    assert f.calls["sz000001"] == 1


def test_abort_when_fail_ratio_exceeded(session, cfg, monkeypatch):
    """失败率 2/3 超阈值 → 熔断返回 None：不落缓存、不存快照，审计标记 aborted。"""
    _quiet_cfg(cfg, retry_attempts=2)
    _patch_spot(monkeypatch)
    _patch_info(monkeypatch)
    rl = RateLimited(retry_after=0.0, source="ths_fuyao", code=429)
    f = FakeFuyao(behavior={"sh600036": [rl, rl, rl], "sz000651": [rl, rl, rl]})
    out = universe.build_candidates(session, None, cfg, ASOF, fuyao=f)
    assert out is None

    audit = session.get(FunnelAudit, ASOF)
    assert audit.status == "aborted" and audit.s2_failed == 2
    assert audit.snapshot_json is None
    assert session.get(UniverseCache, ASOF) is None
    assert f.calls["sh600036"] == 2  # retry_attempts=2，重试确实发生


def test_transient_failure_retried_then_survives(session, cfg, monkeypatch):
    """限流+超时各一次 → 重试后成功 → 标的存活且不计失败。"""
    _quiet_cfg(cfg)
    _patch_spot(monkeypatch)
    _patch_info(monkeypatch)
    rl = RateLimited(retry_after=0.0, source="ths_fuyao", code=429)
    f = FakeFuyao(behavior={"sh600036": [rl, Exception("read timeout")]})
    out = universe.build_candidates(session, None, cfg, ASOF, fuyao=f)
    assert out == sorted(SYMS)
    assert f.calls["sh600036"] == 3

    audit = session.get(FunnelAudit, ASOF)
    assert audit.status == "ok" and audit.s2_failed == 0
