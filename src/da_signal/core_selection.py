"""核心选股：股息率TTM 绝对值降序取前 N，施加行业约束（禁止分位排序 / 禁止换股）。"""
from __future__ import annotations

from collections import defaultdict

from ..da_core.config import Strategy
from ..da_core.models import FactorDaily, SymbolStatus
from .base import Signal, SignalContext, SignalPlugin


def passes_industry_constraint(cfg: Strategy, chosen: list[tuple[str, str | None, str | None]],
                               cand_l1: str | None, cand_sub: str | None) -> bool:
    """行业约束：同 L1 ≤1；银行按子类区分（视为不同类），每子类 ≤2，且银行全组合 ≤ max_bank_total。

    chosen: [(symbol, industry_l1, bank_subtype)]
    ★ 行业名匹配用包含判断（东财口径为"银行Ⅱ"，等值比较永远不命中）
    ★ max_bank_total 缺省/0 = 不限制（向后兼容旧配置）
    """
    ic = cfg.portfolio["industry_constraint"]
    max_per_ind = int(ic["max_per_industry"])
    bank = ic["bank_subtype"]
    treat_distinct = bool(bank.get("treat_as_distinct", True))
    max_per_sub = int(bank.get("max_per_subtype", 2))
    max_bank_total = int(ic.get("max_bank_total") or 0)

    def is_bank(l1: str | None) -> bool:
        return bool(l1) and "银行" in l1

    l1_count = defaultdict(int)
    sub_count = defaultdict(int)
    bank_total = 0
    for _sym, l1, sub in chosen:
        if l1:
            l1_count[l1] += 1
        if is_bank(l1):
            bank_total += 1
            if treat_distinct and sub:
                sub_count[sub] += 1
    if treat_distinct and is_bank(cand_l1):
        # 银行总量帽：优先于子类约束
        if max_bank_total and bank_total >= max_bank_total:
            return False
        # 银行子类不同则不受 L1≤1 限制，但受子类 ≤2
        if cand_sub:
            return sub_count[cand_sub] < max_per_sub
        # 无子类信息按 L1 约束
        return l1_count[cand_l1] < max_per_ind
    return l1_count[cand_l1 or ""] < max_per_ind


def select_core(cfg: Strategy, factors: dict[str, FactorDaily],
                status: dict[str, SymbolStatus]) -> list[tuple[str, FactorDaily]]:
    """漏斗：连续分红≥N年、市值≥M、股息率≥Y、排除 ST/退市 → 股息率绝对值降序 + 行业约束。"""
    u = cfg.universe
    min_years = int(u["min_consecutive_dividend_years"])
    min_cap = float(u["min_market_cap"])
    min_yld = float(u["min_div_yield_ttm"])
    target = int(cfg.portfolio["target_positions"])

    eligible: list[tuple[str, float]] = []
    for sym, f in factors.items():
        st = status.get(sym)
        if cfg.universe.get("exclude_st") and st and st.is_st:
            continue
        if cfg.universe.get("exclude_delisting") and st and st.delist_date:
            continue
        if f.consecutive_div_years is None or f.consecutive_div_years < min_years:
            continue
        if st and st.market_cap and float(st.market_cap) < min_cap:
            continue
        if f.div_yield_ttm is None or float(f.div_yield_ttm) < min_yld:
            continue
        eligible.append((sym, float(f.div_yield_ttm)))
    # 绝对值降序（禁止分位）
    eligible.sort(key=lambda kv: kv[1], reverse=True)

    chosen: list[tuple[str, str | None, str | None]] = []
    result: list[tuple[str, FactorDaily]] = []
    for sym, _y in eligible:
        if len(result) >= target:
            break
        st = status.get(sym)
        l1 = st.industry_l1 if st else None
        sub = st.bank_subtype if st else None
        if not passes_industry_constraint(cfg, chosen, l1, sub):
            continue
        chosen.append((sym, l1, sub))
        result.append((sym, factors[sym]))
    return result


class CoreSelectionPlugin(SignalPlugin):
    name = "core_selection"

    def generate(self, ctx: SignalContext) -> list[Signal]:
        # 选股不直接产出买卖信号；由 portfolio 层消费目标持仓。这里产出「BUY 候选」信息信号。
        status: dict[str, SymbolStatus] = {}
        return []


def build_targets(cfg: Strategy, factors: dict[str, FactorDaily],
                  status: dict[str, SymbolStatus]) -> list[dict]:
    sel = select_core(cfg, factors, status)
    w = 1.0 / max(len(sel), 1)
    targets = []
    for sym, _f in sel:
        st = status.get(sym)
        targets.append({
            "symbol": sym,
            "weight": w,
            "industry_l1": st.industry_l1 if st else None,
            "bank_subtype": st.bank_subtype if st else None,
        })
    return targets
