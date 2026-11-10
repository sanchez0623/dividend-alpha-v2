"""回归：东财全市场快照交易所判定（沪市误标 sz 修复）。

bug 背景：clist 接口的 fields 不含 f13 时，x.get("f13") 恒为 None，
全市场被误标 sz → 沪市股票带 sz600xxx 假代码在 universe S2 查同花顺
失败被静默丢弃 → 候选池 0 沪市 → 持仓全为深市。
"""
from __future__ import annotations

from src.da_data.providers.eastmoney import _exchange_for


def test_f13_present() -> None:
    # f13 显式请求时正常返回 1/0
    assert _exchange_for("601398", 1) == "sh"
    assert _exchange_for("600519", "1") == "sh"
    assert _exchange_for("000651", 0) == "sz"
    assert _exchange_for("300750", "0") == "sz"


def test_f13_missing_fallback_by_prefix() -> None:
    # f13 缺失（未请求）时代码段兜底：6 开头=沪
    assert _exchange_for("601398", None) == "sh"
    assert _exchange_for("600519", None) == "sh"
    assert _exchange_for("688825", None) == "sh"   # 科创板
    assert _exchange_for("605111", None) == "sh"   # 沪主板
    assert _exchange_for("000651", None) == "sz"
    assert _exchange_for("002032", None) == "sz"
    assert _exchange_for("300750", None) == "sz"   # 创业板
    assert _exchange_for("001965", None) == "sz"
