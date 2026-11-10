"""行业约束：同 L1 ≤1；银行子类区分、每子类 ≤2，且银行全组合 ≤ max_bank_total。"""
from __future__ import annotations

from src.da_signal.core_selection import passes_industry_constraint


def _chosen(*items):
    return list(items)


def test_same_l1_max_one(cfg):
    chosen = _chosen(("sh601398", "银行", "国有大型"))
    # 另一银行不同子类 → 允许（子类区分）
    assert passes_industry_constraint(cfg, chosen, "银行", "股份制") is True
    # 同 L1 非银行 → 拒绝
    chosen2 = _chosen(("sh600900", "公用事业", None))
    assert passes_industry_constraint(cfg, chosen2, "公用事业", None) is False


def test_bank_subtype_max_two(cfg):
    chosen = _chosen(
        ("sh601398", "银行", "国有大型"),
        ("sh601288", "银行", "国有大型"),
    )
    # 第 3 只同子类 → 拒绝
    assert passes_industry_constraint(cfg, chosen, "银行", "国有大型") is False
    # 换一个子类 → 允许
    assert passes_industry_constraint(cfg, chosen, "银行", "城商行") is True


def test_bank_total_max_four(cfg):
    """★ 2026-11-08 新增：银行全组合 ≤4（max_bank_total），子类区分不变。"""
    chosen = _chosen(
        ("sh601398", "银行", "国有大行"),
        ("sh601288", "银行", "国有大行"),
        ("sh600015", "银行", "股份行"),
        ("sh600926", "银行", "城农商行"),
    )
    # 第 5 家银行（子类还有空位）→ 仍拒绝
    assert passes_industry_constraint(cfg, chosen, "银行", "股份行") is False
    assert passes_industry_constraint(cfg, chosen, "银行", "城农商行") is False
    # 第 4 家之后非银行不受影响
    assert passes_industry_constraint(cfg, chosen, "白酒", None) is True


def test_bank_total_cap_configurable(cfg):
    """max_bank_total=0/缺省 → 不限制（向后兼容）。"""
    chosen = _chosen(
        ("sh601398", "银行", "国有大行"),
        ("sh601288", "银行", "国有大行"),
        ("sh600015", "银行", "股份行"),
        ("sh600926", "银行", "城农商行"),
        ("sh601838", "银行", "城农商行"),
        ("sh601169", "银行", "城农商行"),
    )
    # 临时把帽子摘掉：6 家银行 + 空位子类 → 允许
    cfg.portfolio["industry_constraint"]["max_bank_total"] = 0
    assert passes_industry_constraint(cfg, chosen, "银行", "股份行") is True
    assert passes_industry_constraint(cfg, chosen, "银行", "国有大行") is False  # 子类帽仍生效
