"""样本守门（★ 变更 C2）。

n<MIN_DAYS 或 m<MIN_TRADES 时：mode=annual 抛 SampleInsufficient；
progress 模式响应不得包含 annual_excess_* 字段（不是前端隐藏）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

MIN_DAYS = 1095
MIN_TRADES = 300


class SampleInsufficient(Exception):
    def __init__(self, n: int, m: int):
        self.n = n
        self.m = m
        self.need_days = max(0, MIN_DAYS - n)
        self.need_trades = max(0, MIN_TRADES - m)
        super().__init__(
            f"样本不足: n={n}(<{MIN_DAYS} 需 {self.need_days} 天), m={m}(<{MIN_TRADES} "
            f"需 {self.need_trades} 笔)"
        )


@dataclass
class BenchmarkView:
    mode: str  # progress | annual
    running_days: int
    paired_trade_count: int
    need_days: int
    need_trades: int
    annual_unlocked: bool
    benchmarks: list[dict] = field(default_factory=list)
    # annual 字段仅在解锁时存在；锁定时严格不设这些键
    annual_excess_vs_hs300_tr: dict | None = None
    annual_excess_vs_dividend_tr: dict | None = None
    b6_placebo: dict | None = None


def get_benchmark_view(mode: str, running_days: int, paired_trade_count: int,
                       benchmarks: list[dict] | None = None) -> BenchmarkView:
    n, m = running_days, paired_trade_count
    unlocked = n >= MIN_DAYS and m >= MIN_TRADES
    if mode == "annual" and not unlocked:
        raise SampleInsufficient(n, m)
    view = BenchmarkView(
        mode=mode,
        running_days=n,
        paired_trade_count=m,
        need_days=max(0, MIN_DAYS - n),
        need_trades=max(0, MIN_TRADES - m),
        annual_unlocked=unlocked,
        benchmarks=benchmarks or [],
    )
    if mode == "annual" and unlocked:
        # 仅在解锁时由调用方填充真实年化超额
        pass
    return view
