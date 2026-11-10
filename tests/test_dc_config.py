"""DC 配置加载：独立文件、hash 稳定、规则映射正确。"""
from __future__ import annotations

import pytest

from src.da_core.dc_config import load_dividend_capture, reset_cache
from src.da_strategy.dividend_capture import DcRules


def test_load_dc_config_and_rules():
    dc = load_dividend_capture()
    assert dc.strategy_id == "DC"
    assert dc.account_id == "DC"
    assert dc.init_cash == 3_000_000
    assert len(dc.config_hash()) == 16

    r = DcRules.from_strategy(dc)
    assert r.min_market_cap == 5.0e10
    assert r.min_div_yield_ttm == 0.06
    assert r.entry_lead_trading_days == 5
    assert r.hold_days_after_ex == 30
    assert r.max_concurrent_positions == 1
    assert r.trend_mode == "nondown"
    assert r.withdraw_ratio == 0.30
    assert r.withdraw_base == "net"
    assert r.lot_size == 100


def test_missing_dc_config_raises(tmp_path):
    reset_cache()
    try:
        with pytest.raises(FileNotFoundError):
            load_dividend_capture(str(tmp_path / "nope.yaml"))
    finally:
        reset_cache()


def test_dc_config_hash_stable():
    a = load_dividend_capture().config_hash()
    reset_cache()
    b = load_dividend_capture().config_hash()
    assert a == b
