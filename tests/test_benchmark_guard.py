"""★ C2 样本守门：n<1095 或 m<300 时禁止年化超额（API 层不返回字段）。"""
from __future__ import annotations

import pytest

from src.da_benchmark.guard import MIN_DAYS, MIN_TRADES, SampleInsufficient, \
    get_benchmark_view


def test_annual_raises_when_insufficient():
    with pytest.raises(SampleInsufficient):
        get_benchmark_view("annual", running_days=1, paired_trade_count=0)


def test_progress_view_has_no_annual_excess_keys():
    view = get_benchmark_view("progress", running_days=1, paired_trade_count=0)
    payload = view.__dict__
    assert not any(k.startswith("annual_excess") and v is not None
                   for k, v in payload.items())
    assert view.annual_unlocked is False
    assert view.need_days == MIN_DAYS - 1
    assert view.need_trades == MIN_TRADES


def test_unlock_at_threshold():
    view = get_benchmark_view("annual", running_days=MIN_DAYS, paired_trade_count=MIN_TRADES)
    assert view.annual_unlocked is True
    assert view.need_days == 0 and view.need_trades == 0
