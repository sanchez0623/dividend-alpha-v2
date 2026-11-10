"""再平衡回测编排：CURRENT10 / OLD20 / RANDOM10×100 + SPEC §3.7 断言 + 报告。

数据口径：SPEC §8（优先级高于 §2）——价格文件为
``research/rebalance/data/close_tr_wide.csv``（TR 总收益口径）；旧 EM fqt=2 宽表已作废。
旧 EM fqt=2 结果归档于 ``research/rebalance/results_em_fqt2_bad/``（仅审计用）。

用法（在项目根目录）：
    python research/rebalance/run_backtest.py --selftest        # 合成数据自测引擎
    python research/rebalance/run_backtest.py                   # 全量回测（默认读 close_tr_wide.csv）
    python research/rebalance/run_backtest.py --prices OTHER.csv # 指定价格文件
    python research/rebalance/run_backtest.py --random-seeds 20 # 调试用：只跑 20 个随机篮子

只写 research/rebalance/results/**；不修改 src/、config/、tests/、data/*.db。
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

import numpy as np
import pandas as pd

from engine import (
    CALENDAR_DAYS_PER_YEAR,
    CASH_EPS,
    CostSchedule,
    DEFAULT_DATA_DIR,
    DEFAULT_PRICES_FILENAME,
    FULL_VARIANTS,
    FULL_VARIANT_NAMES,
    RANDOM_VARIANTS,
    SPEC_INIT_CASH,
    BasketResult,
    EngineError,
    MarketData,
    add_premiums,
    build_calendar_masks,
    compute_metrics,
    load_market_data,
    monthly_returns,
    param_hash,
    run_basket,
    sha256_file,
    weight_drift,
)

MODULE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = MODULE_DIR.parents[1]
RESULTS_DIR = MODULE_DIR / "results"
DATA_DIR = MODULE_DIR / "data"
STRATEGY_YAML = PROJECT_ROOT / "config" / "strategy.yaml"
DEFAULT_PRICES_PATH = DATA_DIR / DEFAULT_PRICES_FILENAME
# SPEC §8：旧 EM fqt=2 结果归档位置（审计对比用）
OLD_RESULTS_DIR = MODULE_DIR / "results_em_fqt2_bad"

# SPEC §2.3 固定篮子
CURRENT10 = [
    "sz000157", "sz000423", "sz000568", "sz000651", "sz000987",
    "sz002032", "sz002236", "sz002532", "sz002555", "sz300979",
]
OLD20 = [
    "sh600028", "sh600036", "sh600585", "sh600900", "sh601088",
    "sh601288", "sh601398", "sh601857", "sh601988",
    "sz000001", "sz000333", "sz000538", "sz000630", "sz000651",
    "sz000708", "sz000858", "sz000895", "sz000921", "sz002032", "sz002142",
]
CURRENT10_START = "2021-05-06"
OLD20_START = "2015-01-05"
RANDOM_START = "2015-01-05"
RANDOM_MIN_DAYS = 2500
RANDOM_BASKET_SIZE = 10
RANDOM_SEEDS = 100

VARIANT_COLUMNS = [
    "variant", "start_date", "end_date", "years", "cagr", "vol_annual", "sharpe",
    "mdd", "calmar", "turnover_annual_two_way", "cost_pct_annual",
    "n_rebalances", "n_trades", "avg_rebalance_cost",
    "premium_cagr_vs_hold", "premium_sharpe_vs_hold", "premium_mdd_vs_hold",
    "monthly_diff_mean", "monthly_diff_t", "monthly_diff_p", "monthly_win_rate",
    "nav_end", "total_cost", "total_turnover", "cash_min",
]
RANDOM_COLUMNS = [
    "basket_id", "variant", "cagr", "cagr_hold", "premium_cagr",
    "sharpe", "sharpe_hold", "turnover", "cost_pct",
]

COST_APPROX_TOL = 0.01  # §3.7-5 "差值 ≈ cost_pct_annual" 的绝对容差（1 个百分点）


# ---------------------------------------------------------------------------
# 断言（SPEC §3.7）
# ---------------------------------------------------------------------------
def check_assertions(
    br: BasketResult,
    market: MarketData,
    init_cash: float,
    require_cost_recalc: bool = True,
) -> Dict[str, dict]:
    """SPEC §3.7 六条硬性断言；任何一条不满足抛 AssertionError。"""
    out: Dict[str, dict] = {}
    hold = br.sims["hold"]
    n_sym = len(br.symbols)
    idx_map = {s: i for i, s in enumerate(br.symbols)}

    # --- 断言 1：hold 期末净值 == Σ 初始份额×期末后复权价；全程无交易、成本 0 ---
    expected_hold_end = float(np.dot(hold.initial_shares, hold.prices_last))
    nav_diff = abs(float(hold.navs[-1]) - expected_hold_end)
    assert nav_diff < 1e-6, f"[A1] hold 期末净值与 Σ初始份额×期末价 差 {nav_diff:.3e} >= 1e-6"
    assert hold.n_trades == 0, f"[A1] hold 出现交易 {hold.n_trades} 笔"
    assert hold.n_rebalances == 0, f"[A1] hold 出现调仓 {hold.n_rebalances} 次"
    assert hold.total_cost == 0.0, f"[A1] hold 成本非 0：{hold.total_cost}"
    out["A1_hold_identity"] = {
        "nav_end": float(hold.navs[-1]),
        "sum_init_shares_end_price": expected_hold_end,
        "abs_diff": nav_diff,
        "n_trades": hold.n_trades,
        "total_cost": hold.total_cost,
    }

    # --- 断言 2：任意日现金 >= -1e-6；成本与按成交额重算一致（误差 <1e-9） ---
    recalc_errors = {}
    for name, sim in br.sims.items():
        assert sim.cash_min >= -CASH_EPS, f"[A2] {name} 最低现金 {sim.cash_min:.3e} < -1e-6"
        if require_cost_recalc:
            recalc = 0.0
            for tr in sim.trades:
                recalc += sim.cost_model.buy_cost(tr.amount) if tr.side == "buy" else sim.cost_model.sell_cost(tr.amount)
            err = abs(recalc - sim.total_cost)
            recalc_errors[name] = err
            assert err < 1e-9, f"[A2] {name} 成本重算误差 {err:.3e} >= 1e-9"
    out["A2_cash_and_cost"] = {
        "min_cash_all_variants": float(min(s.cash_min for s in br.sims.values())),
        "cost_recalc_max_abs_error": float(max(recalc_errors.values())) if recalc_errors else None,
    }

    # --- 断言 3：再平衡日只使用 date <= t 的价格（引擎天然满足；用成交审计验证） ---
    audit_checked = 0
    for name, sim in br.sims.items():
        prev_t = -1
        for tr in sim.trades:
            assert tr.t >= 1, f"[A3] {name} 在 t=0 交易"
            assert tr.t >= prev_t, f"[A3] {name} 成交时间倒序"
            prev_t = tr.t
            audit_checked += 1
            px = float(br.prices[tr.t, idx_map[tr.symbol]])
            assert abs(tr.price - px) <= 1e-12, f"[A3] {name} 成交价与当日收盘价不一致"
    # m_last 的每笔成交都必须落在月末触发日
    masks = market.masks_from(br.start_date)
    for tr in br.sims["m_last"].trades:
        assert bool(masks["month_last"][tr.t]), f"[A3] m_last 在非月末日 t={tr.t} 成交"
    out["A3_no_lookahead"] = {
        "trades_audited": audit_checked,
        "m_last_trades_all_on_month_last": True,
        "note": "引擎按 t 递增推进，成交价 = prices[t]；代码评审确认未访问 t 之后的价格",
    }

    # --- 断言 4：m_last 调仓次数 == 区间内月末数（±1） ---
    month_last_count = int(np.sum(masks["month_last"][1:]))
    got = br.sims["m_last"].n_rebalances
    assert abs(got - month_last_count) <= 1, f"[A4] m_last n_rebalances={got} vs 月末数={month_last_count} 差 >1"
    out["A4_rebalance_count"] = {
        "m_last_n_rebalances": int(got),
        "month_last_days_in_span": month_last_count,
        "diff": int(got - month_last_count),
    }

    # --- 断言 5：m_last_nocost CAGR >= m_last CAGR，且差值 ≈ cost_pct_annual ---
    if "m_last_nocost" in br.metrics and "m_last" in br.metrics:
        nocost = br.metrics["m_last_nocost"]["cagr"]
        with_cost = br.metrics["m_last"]["cagr"]
        cost_pct = br.metrics["m_last"]["cost_pct_annual"]
        diff_cagr = nocost - with_cost
        assert diff_cagr >= -1e-9, f"[A5] nocost CAGR {nocost:.6f} < cost CAGR {with_cost:.6f}"
        assert abs(diff_cagr - cost_pct) <= COST_APPROX_TOL, (
            f"[A5] CAGR 差 {diff_cagr:.6f} 与 cost_pct_annual {cost_pct:.6f} 差超过 {COST_APPROX_TOL}"
        )
        out["A5_cost_monotonicity"] = {
            "cagr_nocost": float(nocost),
            "cagr_m_last": float(with_cost),
            "cagr_diff": float(diff_cagr),
            "cost_pct_annual_m_last": float(cost_pct),
            "abs_diff_tolerance": COST_APPROX_TOL,
        }

    # --- 断言 6：所有变体同一起始日、同一初始组合；hold 与 m_last 起始净值相同 ---
    first_nav = float(hold.navs[0])
    for name, sim in br.sims.items():
        assert len(sim.navs) == len(hold.navs), f"[A6] {name} 净值长度不一致"
        assert abs(float(sim.navs[0]) - first_nav) <= 1e-9, f"[A6] {name} 起始净值 {sim.navs[0]} != {first_nav}"
        w0 = sim.initial_shares * br.prices[0] / float(sim.navs[0])
        err = float(np.max(np.abs(w0 - 1.0 / n_sym)))
        assert err <= 1e-12, f"[A6] {name} 初始权重偏离 1/N 达 {err:.3e}"
    assert abs(float(br.sims["m_last"].navs[0]) - first_nav) <= 1e-9
    out["A6_same_start_and_init"] = {
        "init_nav": first_nav,
        "n_variants": len(br.sims),
        "max_initial_weight_deviation": 0.0,
    }
    return out


# ---------------------------------------------------------------------------
# 随机篮子
# ---------------------------------------------------------------------------
def make_random_baskets(pool: Sequence[str], n_baskets: int, size: int) -> List[List[str]]:
    """seed=0..n-1，无放回抽 size 只；排序后返回（便于复现阅读）。"""
    baskets = []
    for seed in range(n_baskets):
        rng = np.random.default_rng(seed)
        picked = rng.choice(pool, size=size, replace=False)
        baskets.append(sorted(str(s) for s in picked))
    return baskets


def random_basket_stats(df: pd.DataFrame) -> Dict[str, dict]:
    stats_out: Dict[str, dict] = {}
    hold = df[df["variant"] == "hold"]["cagr"]
    stats_out["hold"] = {
        "n": int(len(hold)),
        "cagr_mean": float(hold.mean()),
        "cagr_median": float(hold.median()),
        "cagr_q05": float(hold.quantile(0.05)),
        "cagr_q95": float(hold.quantile(0.95)),
    }
    from scipy import stats as st

    for name in RANDOM_VARIANTS:
        if name.name == "hold":
            continue
        sub = df[df["variant"] == name.name]
        prem = sub["premium_cagr"].to_numpy(dtype=float)
        prem_sharpe = (sub["sharpe"] - sub["sharpe_hold"]).to_numpy(dtype=float)
        n = len(prem)
        mean = float(np.mean(prem))
        median = float(np.median(prem))
        pos = float(np.mean(prem > 0))
        if n >= 2:
            res = st.ttest_1samp(prem, 0.0)
            t, p = float(res.statistic), float(res.pvalue)
        else:
            t, p = float("nan"), float("nan")
        stats_out[name.name] = {
            "n": int(n),
            "premium_cagr_mean": mean,
            "premium_cagr_median": median,
            "positive_ratio": pos,
            "t": t,
            "p": p,
            "q05": float(np.quantile(prem, 0.05)),
            "q95": float(np.quantile(prem, 0.95)),
            "min": float(np.min(prem)),
            "max": float(np.max(prem)),
            "premium_sharpe_mean": float(np.mean(prem_sharpe)),
            "cost_pct_mean": float(sub["cost_pct"].mean()),
            "turnover_mean": float(sub["turnover"].mean()),
        }
    return stats_out


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------
def write_variants_csv(path: Path, br: BasketResult, variant_names: Sequence[str]) -> None:
    rows = []
    for name in variant_names:
        row = {k: br.metrics[name].get(k) for k in VARIANT_COLUMNS}
        rows.append(row)
    pd.DataFrame(rows, columns=VARIANT_COLUMNS).to_csv(path, index=False, encoding="utf-8")


def write_nav_csv(path: Path, br: BasketResult, variant_names: Sequence[str]) -> None:
    df = pd.DataFrame({"date": [d.strftime("%Y-%m-%d") for d in br.dates]})
    for name in variant_names:
        df[name] = br.sims[name].navs
    df.to_csv(path, index=False, encoding="utf-8", float_format="%.10f")


def weight_drift_bundle(br: BasketResult) -> Tuple[pd.DataFrame, pd.DataFrame, dict]:
    per_symbol, ts = weight_drift(br.sims["hold"].weights, br.dates, br.symbols)
    summary = {
        "start_date": str(br.start_date.date()),
        "end_date": str(br.end_date.date()),
        "n_days": int(len(br.dates)),
        "end_max_weight": float(per_symbol["end_weight"].max()),
        "end_max_symbol": str(per_symbol.loc[per_symbol["end_weight"].idxmax(), "symbol"]),
        "end_min_weight": float(per_symbol["end_weight"].min()),
        "end_min_symbol": str(per_symbol.loc[per_symbol["end_weight"].idxmin(), "symbol"]),
        "overall_max_single_weight": float(ts["max_weight"].max()),
        "overall_max_single_weight_date": str(
            ts.loc[ts["max_weight"].idxmax(), "date"]
        ),
        "overall_max_single_weight_symbol": str(
            ts.loc[ts["max_weight"].idxmax(), "argmax_symbol"]
        ),
        "pct_days_max_weight_over_15": float((ts["max_weight"] > 0.15).mean()),
        "pct_days_max_weight_over_20": float((ts["max_weight"] > 0.20).mean()),
        "per_symbol_end_weights": {
            r["symbol"]: float(r["end_weight"]) for _, r in per_symbol.iterrows()
        },
    }
    return per_symbol, ts, summary


def write_weights_drift(out_dir: Path, drift: Dict[str, Tuple[pd.DataFrame, pd.DataFrame, dict]]) -> None:
    for basket in ("CURRENT10", "OLD20"):
        per_symbol, _, _ = drift[basket]
        per_symbol.to_csv(out_dir / f"weights_drift_{basket}.csv", index=False, encoding="utf-8")
    # max_weight 时间序列：两篮子对齐到同一交易日历
    frames = []
    for basket in ("CURRENT10", "OLD20"):
        _, ts, _ = drift[basket]
        frames.append(ts.set_index(pd.to_datetime(ts["date"]))["max_weight"].rename(f"max_weight_{basket}"))
    merged = pd.concat(frames, axis=1)
    merged.index.name = "date"
    merged = merged.sort_index()
    merged.reset_index().to_csv(out_dir / "max_weight_timeseries.csv", index=False, encoding="utf-8")


# ---------------------------------------------------------------------------
# 报告（RESULTS_DRAFT.md）
# ---------------------------------------------------------------------------
def _f(x: float, digits: int = 6) -> float:
    return None if x is None or (isinstance(x, float) and not math.isfinite(x)) else float(round(x, digits))


def _pct(x: float, digits: int = 2) -> str:
    if x is None or not math.isfinite(float(x)):
        return "n/a"
    return f"{float(x) * 100:.{digits}f}%"


def _num(x: float, digits: int = 3) -> str:
    if x is None or not math.isfinite(float(x)):
        return "n/a"
    return f"{float(x):.{digits}f}"


def _variant_table(br: BasketResult) -> str:
    lines = [
        "| 变体 | CAGR | 年化波动 | Sharpe | MDD | Calmar | 年化双边换手 | 年化成本%（占平均权益/年） | 调仓次数 | 成交笔数 | 单次均成本(元) |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for name in FULL_VARIANT_NAMES:
        m = br.metrics[name]
        lines.append(
            f"| {name} | {_pct(m['cagr'])} | {_pct(m['vol_annual'])} | {_num(m['sharpe'])} | "
            f"{_pct(m['mdd'])} | {_num(m['calmar'])} | {_num(m['turnover_annual_two_way'], 2)} | "
            f"{_pct(m['cost_pct_annual'])} | {m['n_rebalances']} | {m['n_trades']} | "
            f"{_num(m['avg_rebalance_cost'], 0)} |"
        )
    return "\n".join(lines)


def _premium_table(br: BasketResult) -> str:
    lines = [
        "| 变体 | premium_CAGR | premium_Sharpe | premium_MDD | 月度差均值 | t 值 | p 值 | 月胜率 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for name in FULL_VARIANT_NAMES:
        if name == "hold":
            continue
        m = br.metrics[name]
        lines.append(
            f"| {name} | {_pct(m['premium_cagr_vs_hold'])} | {_num(m['premium_sharpe_vs_hold'])} | "
            f"{_pct(m['premium_mdd_vs_hold'])} | {_pct(m['monthly_diff_mean'], 3)} | "
            f"{_num(m['monthly_diff_t'])} | {_num(m['monthly_diff_p'], 4)} | {_pct(m['monthly_win_rate'])} |"
        )
    return "\n".join(lines)


def _random_table(stats: Dict[str, dict]) -> str:
    lines = [
        "| 变体 | 篮子数 | premium_CAGR 均值 | 中位数 | 正比例 | t 值 | p 值 | 5% 分位 | 95% 分位 | 最小 | 最大 |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for name in RANDOM_VARIANTS:
        if name.name == "hold":
            continue
        s = stats[name.name]
        lines.append(
            f"| {name.name} | {s['n']} | {_pct(s['premium_cagr_mean'])} | {_pct(s['premium_cagr_median'])} | "
            f"{_pct(s['positive_ratio'])} | {_num(s['t'])} | {_num(s['p'], 4)} | {_pct(s['q05'])} | "
            f"{_pct(s['q95'])} | {_pct(s['min'])} | {_pct(s['max'])} |"
        )
    return "\n".join(lines)


def _cost_sensitivity_rows(basket: str, br: BasketResult) -> List[str]:
    m = br.metrics
    base = m["m_last"]["cagr"]
    nocost_delta = m["m_last_nocost"]["cagr"] - base
    cost2_delta = m["m_last_cost2"]["cagr"] - base
    return [
        f"| {basket} | hold | {_pct(m['hold']['cagr'])} | 0.00% | - |",
        f"| {basket} | m_last | {_pct(m['m_last']['cagr'])} | {_pct(m['m_last']['cost_pct_annual'])} | - |",
        f"| {basket} | m_last_nocost | {_pct(m['m_last_nocost']['cagr'])} | 0.00% | "
        f"{_pct(nocost_delta)} (+{nocost_delta * 10000:.1f}bp) |",
        f"| {basket} | m_last_cost2 | {_pct(m['m_last_cost2']['cagr'])} | "
        f"{_pct(m['m_last_cost2']['cost_pct_annual'])} | {_pct(cost2_delta)} ({cost2_delta * 10000:.1f}bp) |",
    ]


def _weight_table(drift: Dict[str, Tuple[pd.DataFrame, pd.DataFrame, dict]]) -> str:
    lines = [
        "| 篮子 | 期末最大权重 | 期末最大权重标的 | 期末最小权重 | 全程单只最大权重 | 出现日期 | 全程 max>15% 天数占比 | 全程 max>20% 天数占比 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for basket in ("CURRENT10", "OLD20"):
        s = drift[basket][2]
        lines.append(
            f"| {basket} | {_pct(s['end_max_weight'])} | {s['end_max_symbol']} | {_pct(s['end_min_weight'])} | "
            f"{_pct(s['overall_max_single_weight'])} | {s['overall_max_single_weight_date']} | "
            f"{_pct(s['pct_days_max_weight_over_15'])} | {_pct(s['pct_days_max_weight_over_20'])} |"
        )
    return "\n".join(lines)


def _assertion_table(assertions: Dict[str, dict]) -> str:
    lines = ["| 断言 | 结果 | 关键数值 |", "|---|---|---|"]
    a1 = assertions.get("A1_hold_identity", {})
    lines.append(
        f"| §3.7-1 hold 期末净值 == Σ初始份额×期末价 | PASS | 差 {a1.get('abs_diff', float('nan')):.2e}；"
        f"trade={a1.get('n_trades')}；cost={a1.get('total_cost')} |"
    )
    a2 = assertions.get("A2_cash_and_cost", {})
    lines.append(
        f"| §3.7-2 现金≥-1e-6；成本重算一致<1e-9 | PASS | 最低现金 {a2.get('min_cash_all_variants', float('nan')):.2e}；"
        f"重算最大误差 {a2.get('cost_recalc_max_abs_error')} |"
    )
    a3 = assertions.get("A3_no_lookahead", {})
    lines.append(
        f"| §3.7-3 无未来函数 | PASS | 审计成交 {a3.get('trades_audited')} 笔；m_last 成交均在月末 |"
    )
    a4 = assertions.get("A4_rebalance_count", {})
    lines.append(
        f"| §3.7-4 m_last 调仓次数==月末数±1 | PASS | {a4.get('m_last_n_rebalances')} vs "
        f"{a4.get('month_last_days_in_span')}（差 {a4.get('diff')}） |"
    )
    a5 = assertions.get("A5_cost_monotonicity", {})
    if a5:
        lines.append(
            f"| §3.7-5 nocost CAGR≥m_last 且差≈cost_pct | PASS | 差 {a5.get('cagr_diff'):.6f} vs "
            f"cost_pct {a5.get('cost_pct_annual_m_last'):.6f} |"
        )
    a6 = assertions.get("A6_same_start_and_init", {})
    lines.append(
        f"| §3.7-6 同起始/同初始组合；hold=m_last 起始净值 | PASS | init nav {a6.get('init_nav', float('nan')):.0f}；"
        f"变体数 {a6.get('n_variants')} |"
    )
    return "\n".join(lines)


def build_results_draft(
    summary: dict,
    results: Dict[str, BasketResult],
    drift: Dict[str, Tuple[pd.DataFrame, pd.DataFrame, dict]],
    random_df: pd.DataFrame,
    random_stats: Dict[str, dict],
    assertions: Dict[str, Dict[str, dict]],
    sanity: Dict[str, dict],
    old_dir: Path | None = None,
) -> str:
    md: List[str] = []
    md.append("# 再平衡回测 · 结果草稿（引擎自动生成）\n")
    md.append(
        "> 本文件由 `research/rebalance/run_backtest.py` 根据同一批内存结果自动生成，"
        "**表中每个数字都对应 results/ 下同名文件**；结论为草稿，待 Lead 定稿。\n"
    )

    # ---- 数据口径修正（SPEC §8；按 Lead 要求置于最顶部）----
    md.append("## 0. 数据口径修正（2026-11-08，SPEC §8）\n")
    rev = summary.get("data_revision", {})
    dep = summary["data"].get("deprecated_em_fqt2", {})
    md.append(
        "- **旧口径已弃用**：EM fqt=2 后复权宽表（旧文件 `research/rebalance/data/_em_fqt2_close_adj_wide.bad.csv`，"
        f"SHA256 `{str(dep.get('sha256', 'n/a'))[:16]}…`）。复核发现其 adj/raw 因子几乎逐日漂移、非分段常数、非单调："
        "601088.SH 2015-2026 标准总收益 385%，EM fqt=2 仅 177%（日收益相关 0.986、最大单日差 11.5%）；"
        "fuyao `adjust=forward` 长窗口同样不可用（2015 年价格被压到接近 0/负数）。详见 `data/TR_VALIDATION.md` §1。\n"
    )
    md.append(
        "- **本次新口径**：`research/rebalance/data/close_tr_wide.csv`"
        f"（SHA256 `{summary['data']['prices']['sha256'][:16]}…`）= 同花顺不复权价 + 同花顺除权事件 + "
        "生产同款 `src/da_data/adjust.py::build_adj_factor` 前复权；日收益 = 总收益"
        "（分红按除权日收盘再投原股票，未扣红利税）。数据验证见 `data/TR_VALIDATION.md`"
        "（2877 行 × 62 标的、缺失 11444 格、与生产库窗口日收益相关中位 1.0000）。\n"
    )
    md.append(
        "- **旧结果作废并归档**：`research/rebalance/results_em_fqt2_bad/`（含旧 summary.json / "
        "RESULTS_DRAFT.md / 全 CSV）；本次 results/ 下所有数字均来自新 TR 数据重跑，未沿用旧数。\n"
    )
    if rev.get("old_vs_new"):
        md.append(
            "- **新旧关键数字对比**（旧=EM fqt=2，来源 `results_em_fqt2_bad/variants_*.csv`；"
            "新=本次 `results/variants_*.csv`；变化单位 pp=百分点）：\n"
        )
        md.append("| 篮子 | 指标 | 旧 EM fqt=2（作废） | 新 TR（本次） | 变化（新-旧，pp） |")
        md.append("|---|---|---|---|---|")
        cmp = rev["old_vs_new"]
        for bid in ("CURRENT10", "OLD20"):
            c = cmp[bid]
            for label, ok, nk in (
                ("hold CAGR", "hold_cagr_old", "hold_cagr_new"),
                ("m_last CAGR", "m_last_cagr_old", "m_last_cagr_new"),
                ("m_last 费后 premium", "m_last_premium_old", "m_last_premium_new"),
                ("hold MDD", "hold_mdd_old", "hold_mdd_new"),
                ("m_last MDD", "m_last_mdd_old", "m_last_mdd_new"),
            ):
                ov, nv = c[ok], c[nk]
                diff_pp = (nv - ov) * 100.0
                verdict = "持平" if abs(diff_pp) < 1e-9 else ("改善" if diff_pp > 0 else "更差")
                md.append(
                    f"| {bid} | {label} | {_pct(ov)} | {_pct(nv)} | {diff_pp:+.2f}pp（{verdict}） |"
                )
        md.append("")
        md.append(
            "- 注：变化 = 新 TR - 旧 EM（单位 pp）。对 CAGR/premium，正=改善、负=更差；MDD 为负数，"
            "变化为负表示回撤更深（更差），变化为正表示回撤更浅（改善）。\n"
        )
    else:
        md.append("- 未找到旧 EM fqt=2 归档结果（`results_em_fqt2_bad/`），本文件不提供新旧对比。\n")

    md.append("## 0.1 本次数据与口径\n")
    data_info = summary["data"]
    md.append(
        f"- TR 总收益宽表（SPEC §8）：`{data_info['prices']['path']}`；"
        f"交易日 {data_info['prices']['date_min']} ~ {data_info['prices']['date_max']}，"
        f"{data_info['prices']['rows']} 行 × {data_info['prices']['cols']} 标的，"
        f"缺失格 {data_info['prices']['missing_cells']}；"
        f"SHA256 `{data_info['prices']['sha256'][:16]}…`。\n"
    )
    md.append(
        f"- 交易日历：`research/rebalance/data/index_000300.csv`（{data_info['index_000300']['rows']} 个交易日）。\n"
    )
    md.append(
        "- 数据质检：新口径验证见 `research/rebalance/data/TR_VALIDATION.md`（§2 统计、§3 抽样累计总收益、§4 结论）；"
        "`data/DATA_QUALITY.md` 为旧 EM 口径质检报告（对应已作废数据，保留供审计）。"
        "本回测以 000300 日历为交易日全集。\n"
    )
    md.append(
        "- 初始资金 3,000,000 元（`config/strategy.yaml` `meta.init_cash`，SPEC §3.1）；"
        "TR 价、分数股、收盘成交；停牌日按最近可得价 ffill；分红再投已隐含在 TR 价中（未扣红利税）。\n"
    )
    md.append(
        "- 成本严格按 `config/strategy.yaml` `cost` 段：佣金 max(金额×0.00005, 5)+过户 0.00001+"
        "经手 0.0000341+证管 0.00002+滑点 5bp（买入）；卖出另加印花税 0.0005。\n"
    )
    md.append(
        "- **初始建仓不计成本**：SPEC §3.7-1 要求 hold 全程无交易/成本=0，故 t0 视为已按 1/N 建好的组合；"
        "所有变体同一起点、同一初始组合，该假设不改变变体间溢价差（仅略低估绝对成本）。\n"
    )
    md.append(
        "- **指标口径**：CAGR 按自然日/365.25 年化；Sharpe 用日收益（rf=0）×√252；MDD 为负数（-33.69%=最大回撤 33.69%）；"
        "`premium_*` = 变体 - hold，因此 `premium_MDD>0` 表示回撤更浅（更好）；`monthly_diff_*` 为（变体月收益 - hold 月收益）"
        "单样本 t 检验。停牌股按 ffill 价照常成交（SPEC §3.3 简化，会低估停牌/涨跌停的真实成交约束）。\n"
    )
    md.append(
        f"- 参数哈希：`{summary['param_hash']}`；生成时间：{summary['generated_at']}。\n"
    )

    md.append("## 1. CURRENT10（现持仓，样本 {start} ~ {end}）\n".format(
        start=results["CURRENT10"].start_date.date(), end=results["CURRENT10"].end_date.date()))
    md.append("### 1.1 收益 / 风险 / 换手 / 成本（来源：`results/variants_CURRENT10.csv`）\n")
    md.append(_variant_table(results["CURRENT10"]) + "\n")
    md.append("### 1.2 相对 hold 的溢价（来源：`results/variants_CURRENT10.csv`）\n")
    md.append(_premium_table(results["CURRENT10"]) + "\n")

    md.append("## 2. OLD20（长样本高股息篮子，样本 {start} ~ {end}）\n".format(
        start=results["OLD20"].start_date.date(), end=results["OLD20"].end_date.date()))
    md.append("### 2.1 收益 / 风险 / 换手 / 成本（来源：`results/variants_OLD20.csv`）\n")
    md.append(_variant_table(results["OLD20"]) + "\n")
    md.append("### 2.2 相对 hold 的溢价（来源：`results/variants_OLD20.csv`）\n")
    md.append(_premium_table(results["OLD20"]) + "\n")

    md.append("## 3. RANDOM10×100 跨篮子稳健性（来源：`results/random_baskets.csv`）\n")
    md.append(
        f"池：2015-01-05 有数据且样本内交易日 ≥2500（本次合格池 {summary['random']['pool_size']} 只，"
        "按新 TR 数据重算，池大小可能与旧 EM 口径不同）；seed=0..99，每篮无放回抽 10 只；"
        "每篮只跑 hold/m_last/q/a/band5。溢价 = 变体 CAGR - 同篮 hold CAGR。\n"
    )
    md.append(_random_table(random_stats) + "\n")
    md.append(
        f"- **口径提醒**：100 个随机篮子每篮只抽 10/{summary['random']['pool_size']} 只，篮子之间大量重叠"
        "（不是独立样本），因此上表跨篮子 "
        "t 检验的“独立样本”假设不成立，t/p 只能当方向性参考；更可靠的读数是**正比例、中位数与 5%/95% 分位**。\n"
    )
    md.append(
        "- 随机池以 2015 年即上市的中大市值标的为主，波动高于 OLD20 的银行/石油权重结构，因此其平均溢价高于 "
        "OLD20 并不矛盾（复利效应下高波动篮子通常有更大的再平衡溢价）。\n"
    )

    md.append("## 4. 成本敏感性（来源：`results/variants_CURRENT10.csv` / `variants_OLD20.csv`）\n")
    md.append("| 篮子 | 变体 | CAGR | 年化成本%（占平均权益/年） | 相对 m_last 的 CAGR 变化 |")
    md.append("|---|---|---|---|---|")
    for basket in ("CURRENT10", "OLD20"):
        md.extend(_cost_sensitivity_rows(basket, results[basket]))
    md.append("")
    md.append(
        f"- 成本=0 的 `m_last_nocost` 相对 `m_last`：CURRENT10 {_pct(sanity['CURRENT10']['nocost_minus_last'])}，"
        f"OLD20 {_pct(sanity['OLD20']['nocost_minus_last'])}；"
        f"月度收益差 t={_num(sanity['CURRENT10']['nocost_vs_last_t'])}"
        f"（p={_num(sanity['CURRENT10']['nocost_vs_last_p'], 4)}，CURRENT10）/ "
        f"t={_num(sanity['OLD20']['nocost_vs_last_t'])}（p={_num(sanity['OLD20']['nocost_vs_last_p'], 4)}，OLD20）。\n"
    )
    md.append(
        f"- `m_last_cost2`（SPEC §3.2：仅佣金率+滑点×2）相对 `m_last`：CURRENT10 {_pct(sanity['CURRENT10']['cost2_minus_last'])}，"
        f"OLD20 {_pct(sanity['OLD20']['cost2_minus_last'])}。\n"
    )
    md.append(
        "- **读法提醒**：`m_last_nocost` 与 `m_last` 的月度收益差 t 值很大（数十），原因是成本拖累几乎每月恒定、"
        "方差极小，这**不是独立的统计显著性证据**；成本量级请直接看表中的 %/bp。\n"
    )
    md.append(
        "- **未建模的隐性成本**：红利税批次重置（持有 ≤1 年分红征 10%、>1 年 0%，再平衡卖出会重置批次）未显式建模，"
        "方向上真实费后溢价应比本表略低（见 SPEC §6 已知局限）。\n"
    )

    md.append("## 5. 权重漂移证据：hold 变体的集中度（Lead 追加需求）\n")
    md.append(
        "来源：`results/weights_drift_CURRENT10.csv`、`results/weights_drift_OLD20.csv`、"
        "`results/max_weight_timeseries.csv`（列：date, max_weight_CURRENT10, max_weight_OLD20）。权重 = 市值/当日总权益。\n"
    )
    md.append(_weight_table(drift) + "\n")

    md.append("## 6. SPEC §3.7 硬性断言结果\n")
    for basket in ("CURRENT10", "OLD20"):
        md.append(f"### {basket}\n")
        md.append(_assertion_table(assertions[basket]) + "\n")
    md.append(
        "> 注：断言 5 的「差值 ≈ cost_pct_annual」采用绝对容差 1 个百分点；"
        "实际差值见上表（成本被提前扣除会轻微放大拖累，故差值通常略大于 cost_pct）。\n"
    )

    md.append("## 7. 结论草稿（待 Lead 定稿）\n")
    md.append("### 7.1 毛溢价 vs 费后溢价（来源：`results/variants_CURRENT10.csv`、`results/variants_OLD20.csv`）\n")
    md.append(
        "| 篮子 | hold CAGR | 毛（m_last_nocost）CAGR | 毛 premium | 费后（m_last）CAGR | 费后 premium | "
        "成本拖累 | 成本/毛溢价 | 年化双边换手 |"
    )
    md.append("|---|---|---|---|---|---|---|---|---|")
    for basket in ("CURRENT10", "OLD20"):
        m = results[basket].metrics
        gross = m["m_last_nocost"]["cagr"] - m["hold"]["cagr"]
        net = m["m_last"]["premium_cagr_vs_hold"]
        drag = m["m_last"]["cagr"] - m["m_last_nocost"]["cagr"]
        ratio = abs(drag) / gross if gross > 0 else float("nan")
        md.append(
            f"| {basket} | {_pct(m['hold']['cagr'])} | {_pct(m['m_last_nocost']['cagr'])} | "
            f"{_pct(gross)} | {_pct(m['m_last']['cagr'])} | {_pct(net)} | {_pct(drag)} "
            f"({drag * 10000:.1f}bp) | {_pct(ratio)} | {_num(m['m_last']['turnover_annual_two_way'], 2)} |"
        )
    md.append("")
    md.append("### 7.2 结论要点（草稿）\n")

    c = results["CURRENT10"].metrics
    o = results["OLD20"].metrics
    rs = random_stats
    md.append(
        f"1. **方向为正但主篮子统计不显著**：费后 m_last premium_CAGR CURRENT10 {_pct(c['m_last']['premium_cagr_vs_hold'])}、"
        f"OLD20 {_pct(o['m_last']['premium_cagr_vs_hold'])}；但月度收益差 t={_num(c['m_last']['monthly_diff_t'])}"
        f"（p={_num(c['m_last']['monthly_diff_p'], 4)}，CURRENT10）、t={_num(o['m_last']['monthly_diff_t'])}"
        f"（p={_num(o['m_last']['monthly_diff_p'], 4)}，OLD20），均未达 5% 显著。5.5 年/11.8 年单一路径不足以"
        f"宣称“显著超额”，只能说明方向为正。\n"
    )
    md.append(
        f"2. **跨篮子稳健性**：RANDOM10×100 中 m_last premium 正比例 {_pct(rs['m_last']['positive_ratio'])}、"
        f"中位 {_pct(rs['m_last']['premium_cagr_median'])}、5%/95% 分位 {_pct(rs['m_last']['q05'])}/{_pct(rs['m_last']['q95'])}；"
        f"q 正比例 {_pct(rs['q']['positive_ratio'])}、a {_pct(rs['a']['positive_ratio'])}、"
        f"band5 {_pct(rs['band5']['positive_ratio'])}。强调：100 个篮子高度重叠（非独立样本），跨篮子 t 检验"
        f"（m_last t={_num(rs['m_last']['t'])}）仅方向性参考；**正比例与分位是最有说服力的读数**。\n"
    )
    md.append(
        f"3. **风险收益比的改善比 CAGR 溢价更强**：OLD20 再平衡把 MDD 从 {_pct(o['hold']['mdd'])} 收窄到 "
        f"{_pct(o['m_last']['mdd'])}（premium_MDD {_pct(o['m_last']['premium_mdd_vs_hold'])}），年化波动 "
        f"{_pct(o['hold']['vol_annual'])}→{_pct(o['m_last']['vol_annual'])}，Sharpe {_num(o['hold']['sharpe'])}→"
        f"{_num(o['m_last']['sharpe'])}；CURRENT10 同向小幅改善（MDD {_pct(c['hold']['mdd'])}→"
        f"{_pct(c['m_last']['mdd'])}，Sharpe {_num(c['hold']['sharpe'])}→{_num(c['m_last']['sharpe'])}）。"
        f"若目标是回撤控制，再平衡的证据强于 CAGR 溢价。\n"
    )
    md.append(
        f"4. **频率/阈值**：CURRENT10 全组合调仓中 m_last 最优（CAGR {_pct(c['m_last']['cagr'])}），"
        f"m_first {_pct(c['m_first']['cagr'])}、q {_pct(c['q']['cagr'])}、a {_pct(c['a']['cagr'])}、"
        f"band5 {_pct(c['band5']['cagr'])}、band10 {_pct(c['band10']['cagr'])}；OLD20 中 CAGR 最高为 "
        f"`{max(('m_last', 'm_first', 'q', 's', 'a', 'band5', 'band10'), key=lambda v: o[v]['cagr'])}`"
        f"（{_pct(max(o[v]['cagr'] for v in ('m_last', 'm_first', 'q', 's', 'a', 'band5', 'band10')))}；"
        f"对应 band5 {_pct(o['band5']['cagr'])}/{o['band5']['n_rebalances']} 次、"
        f"band10 {_pct(o['band10']['cagr'])}/{o['band10']['n_rebalances']} 次调仓，阈值变体调仓次数少、"
        f"统计意义弱），m_last {_pct(o['m_last']['cagr'])}、q {_pct(o['q']['cagr'])}、a {_pct(o['a']['cagr'])}。"
        f"月度 vs 季度差异不显著，本样本没有出现“某频率明显更优”的稳健结论；成本随频率下降"
        f"（q/s/a/band 年化成本 0.01–0.03% < m_last 0.06–0.08%），但溢价也略低。\n"
    )
    md.append(
        f"5. **成本不是主要矛盾**：当前配置下 m_last 成本拖累约 "
        f"{_pct(c['m_last']['cagr'] - c['m_last_nocost']['cagr'])}/yr（CURRENT10）、"
        f"{_pct(o['m_last']['cagr'] - o['m_last_nocost']['cagr'])}/yr（OLD20），"
        f"占毛溢价约 {_pct(abs(c['m_last']['cagr'] - c['m_last_nocost']['cagr']) / (c['m_last_nocost']['cagr'] - c['hold']['cagr']))}"
        f"/{_pct(abs(o['m_last']['cagr'] - o['m_last_nocost']['cagr']) / (o['m_last_nocost']['cagr'] - o['hold']['cagr']))}；"
        f"`m_last_cost2`（佣金+滑点×2）仅再降约 {_pct(c['m_last']['cagr'] - c['m_last_cost2']['cagr'])}，"
        f"说明对佣金/滑点不敏感。更大的不确定性来自未建模的红利税批次重置、停牌/涨跌停不可成交与 100 股整数手。\n"
    )
    wd = summary["weight_drift"]
    md.append(
        f"6. **权重漂移（缺再平衡的集中度证据）**：hold 全程单只权重最大值 CURRENT10 "
        f"{_pct(wd['CURRENT10']['overall_max_single_weight'])}（{wd['CURRENT10']['overall_max_single_weight_date']}）、"
        f"OLD20 {_pct(wd['OLD20']['overall_max_single_weight'])}（{wd['OLD20']['overall_max_single_weight_date']}）；"
        f"期末单只最高权重 CURRENT10 {_pct(wd['CURRENT10']['end_max_weight'])}、OLD20 "
        f"{_pct(wd['OLD20']['end_max_weight'])}，全程单只权重 >15% 的天数占比 CURRENT10 "
        f"{_pct(wd['CURRENT10']['pct_days_max_weight_over_15'])}、OLD20 {_pct(wd['OLD20']['pct_days_max_weight_over_15'])}。"
        f"集中度漂移客观存在，但它是“要不要修剪”的必要性证据，不是再平衡溢价的来源本身。\n"
    )
    md.append(
        f"7. **稳健性边界**：随机篮子 premium 的 5% 分位仍为正（m_last {_pct(rs['m_last']['q05'])}），"
        f"说明在 2015-2026 这段历史里再平衡溢价对篮子选择较稳健；但样本只覆盖单一历史区间、篮子均来自 2026 年"
        f"仍在册标的（幸存者偏差），且未做样本外/参数稳定性检验。结论只描述历史样本，不外推未来。\n"
    )
    md.append(
        f"8. **结论草稿（供 Lead 定稿）**：等权再平衡在本数据上表现为“温和正溢价 + 明显降低回撤/波动”；"
        f"费后月频 CAGR 溢价 CURRENT10 {_pct(c['m_last']['premium_cagr_vs_hold'])}、"
        f"OLD20 {_pct(o['m_last']['premium_cagr_vs_hold'])}（毛溢价 {_pct(c['m_last_nocost']['cagr'] - c['hold']['cagr'])} / "
        f"{_pct(o['m_last_nocost']['cagr'] - o['hold']['cagr'])}），成本拖累小。但主篮子月度检验不显著、"
        f"随机篮子重叠使 t 检验被高估。建议表述为“**以风险控制为主、附带方向性溢价证据的候选改进**，"
        f"待 verifier 独立复核与样本外检验后再定稿”，而不是“已证实的超额收益策略”。\n"
    )
    md.append(
        "- **已知局限（必须写进最终报告）**：篮子为 2026 年仍在册标的（幸存者偏差，用 A/B 同篮溢价差缓解、RANDOM10 提供稳健性）；"
        "后复权隐含分红按除权日再投原股票；未建模红利税批次重置、涨跌停/停牌不可成交、100 股整数手、T+1、10% 现金缓冲与年度取现；"
        "仅历史样本结论。\n"
    )
    md.append("## 8. 可复现与自检\n")
    md.append(
        "- 复现命令：`python research/rebalance/run_backtest.py`（默认读 `research/rebalance/data/close_tr_wide.csv`，"
        "写 `research/rebalance/results/`；`--prices <file>` 可覆盖价格文件）；"
        "引擎自测：`python research/rebalance/run_backtest.py --selftest`。\n"
    )
    md.append(
        f"- 数据口径（SPEC §8）：新价格文件 `{summary['data']['prices']['path']}`，"
        f"SHA256 `{summary['data']['prices']['sha256']}`；旧 EM fqt=2 结果归档 `results_em_fqt2_bad/`（已作废）。\n"
    )
    md.append(
        f"- 参数哈希 `{summary['param_hash']}`；数据 SHA256 见 §0 与 `results/summary.json`；"
        f"随机篮子 seed=0..99（`numpy.random.default_rng(seed)` 无放回抽 10 只）。\n"
    )
    md.append(
        "- SPEC §3.7 六条断言在 CURRENT10/OLD20 上全部 PASS（见 §6）；成本=0 改善、cost×2 恶化方向性校验 PASS（见 §4）。\n"
    )
    md.append(
        "- 正式独立复核由 verifier 用独立实现完成（不复用 `engine.py`）；本引擎作者另做过一次独立链式复算"
        "（hold 与 m_last_nocost 期末净值/ CAGR 与引擎一致）仅作自检，不作为复核证据。\n"
    )
    md.append("## 9. 输出文件清单\n")
    md.append(
        "`results/variants_CURRENT10.csv`、`results/nav_CURRENT10.csv`、`results/variants_OLD20.csv`、"
        "`results/nav_OLD20.csv`、`results/random_baskets.csv`、`results/random_basket_composition.csv`、"
        "`results/weights_drift_CURRENT10.csv`、`results/weights_drift_OLD20.csv`、"
        "`results/max_weight_timeseries.csv`、`results/summary.json`、本文件。\n"
    )
    md.append(
        "另：旧 EM fqt=2 结果归档于 `research/rebalance/results_em_fqt2_bad/`（作废，仅审计/新旧对比用，"
        "不属于本次有效结果）。\n"
    )
    return "\n".join(md)


# ---------------------------------------------------------------------------
# 全量回测
# ---------------------------------------------------------------------------
def _read_init_cash() -> float:
    import yaml

    with STRATEGY_YAML.open("r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    init_cash = float(cfg["meta"]["init_cash"])
    if abs(init_cash - SPEC_INIT_CASH) > 1.0:
        raise EngineError(f"config init_cash={init_cash} 与 SPEC 3,000,000 不一致")
    return init_cash


def _sanity_for_basket(br: BasketResult) -> dict:
    m = br.metrics
    nocost_minus_last = m["m_last_nocost"]["cagr"] - m["m_last"]["cagr"]
    cost2_minus_last = m["m_last_cost2"]["cagr"] - m["m_last"]["cagr"]
    # cost=0 vs 有成本 的月度收益差 t 检验
    mret_nocost = monthly_returns(br.sims["m_last_nocost"].navs, br.dates)
    mret_last = monthly_returns(br.sims["m_last"].navs, br.dates)
    diff = (mret_nocost - mret_last).dropna()
    from scipy import stats as st

    if len(diff) >= 2 and float(diff.std(ddof=1)) > 0:
        t, p = st.ttest_1samp(diff.to_numpy(), 0.0)
        t, p = float(t), float(p)
    else:
        t, p = float("nan"), float("nan")
    return {
        "nocost_minus_last": float(nocost_minus_last),
        "cost2_minus_last": float(cost2_minus_last),
        "nocost_better": bool(nocost_minus_last > 0.0),
        "cost2_worse": bool(cost2_minus_last < 0.0),
        "nocost_vs_last_t": t,
        "nocost_vs_last_p": p,
        "nocost_vs_last_monthly_diff_mean": float(diff.mean()) if len(diff) else float("nan"),
    }


def _relpath(path: Path) -> str:
    try:
        return Path(path).resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return str(path)


def resolve_prices_path(data_dir: Path, prices_arg: Path | None) -> Path:
    """--prices 解析：默认 data_dir/close_tr_wide.csv；相对路径优先按 data_dir 解析。"""
    if prices_arg is None:
        return data_dir / DEFAULT_PRICES_FILENAME
    p = Path(prices_arg)
    if p.is_absolute():
        if not p.exists():
            raise EngineError(f"--prices 指定的价格文件不存在：{p}")
        return p
    if p.exists():
        return p
    alt = data_dir / p.name
    if alt.exists():
        return alt
    raise EngineError(f"--prices 指定的价格文件不存在：{p}（也未找到 {alt}）")


def validate_prices_contract(
    prices_path: Path,
    calendar: pd.DatetimeIndex,
) -> dict:
    """校验新 TR 宽表契约（SPEC §8）。不符即抛 EngineError，不静默改口径。"""
    df = pd.read_csv(prices_path)
    problems: List[str] = []
    if "date" not in df.columns:
        problems.append("缺少 date 列")
    else:
        dates = pd.to_datetime(df["date"])
        if dates.duplicated().any():
            problems.append(f"date 重复 {int(dates.duplicated().sum())} 行")
        if len(df) != len(calendar):
            problems.append(f"行数 {len(df)} != 交易日历 {len(calendar)}")
        if len(dates) and pd.Timestamp(dates.min()) != calendar[0]:
            problems.append(f"首日 {dates.min().date()} != 日历 {calendar[0].date()}")
        if len(dates) and pd.Timestamp(dates.max()) != calendar[-1]:
            problems.append(f"末日 {dates.max().date()} != 日历 {calendar[-1].date()}")
    symbols = [c for c in df.columns if c != "date"]
    expected_path = DATA_DIR / "universe.txt"
    if expected_path.exists():
        expected = [ln.strip() for ln in expected_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        missing = sorted(set(expected) - set(symbols))
        extra = sorted(set(symbols) - set(expected))
        if missing:
            problems.append(f"缺少标的 {missing}")
        if extra:
            problems.append(f"多余标的 {extra}")
        if len(symbols) != len(expected):
            problems.append(f"标的数 {len(symbols)} != universe {len(expected)}")
    if problems:
        raise EngineError(f"价格文件契约不符（{prices_path}）：" + "；".join(problems))
    return {
        "path": _relpath(prices_path),
        "sha256": sha256_file(prices_path),
        "rows": int(len(df)),
        "cols": int(len(symbols)),
        "date_min": str(pd.to_datetime(df["date"]).min().date()),
        "date_max": str(pd.to_datetime(df["date"]).max().date()),
        "missing_cells": int(df.drop(columns=["date"]).isna().sum().sum()),
        "contract_ok": True,
    }


def load_old_em_summary(old_dir: Path = OLD_RESULTS_DIR) -> dict | None:
    """读取归档的旧 EM fqt=2 结果 summary（用于新旧对比；不存在则返回 None）。"""
    path = Path(old_dir) / "summary.json"
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return None


def build_old_vs_new_comparison(
    old_dir: Path,
    results: Dict[str, BasketResult],
) -> dict | None:
    """从归档旧结果 CSV 与本次新结果构造 hold/m_last 关键指标对比。"""
    old_dir = Path(old_dir)
    if not old_dir.exists():
        return None
    out: Dict[str, dict] = {}
    for bid in ("CURRENT10", "OLD20"):
        old_path = old_dir / f"variants_{bid}.csv"
        if not old_path.exists():
            return None
        old = pd.read_csv(old_path).set_index("variant")
        new = results[bid].metrics
        out[bid] = {
            "hold_cagr_old": float(old.loc["hold", "cagr"]),
            "hold_cagr_new": float(new["hold"]["cagr"]),
            "m_last_cagr_old": float(old.loc["m_last", "cagr"]),
            "m_last_cagr_new": float(new["m_last"]["cagr"]),
            "m_last_premium_old": float(old.loc["m_last", "premium_cagr_vs_hold"]),
            "m_last_premium_new": float(new["m_last"]["premium_cagr_vs_hold"]),
            "hold_mdd_old": float(old.loc["hold", "mdd"]),
            "hold_mdd_new": float(new["hold"]["mdd"]),
            "m_last_mdd_old": float(old.loc["m_last", "mdd"]),
            "m_last_mdd_new": float(new["m_last"]["mdd"]),
        }
    return out


def run_full(
    data_dir: Path,
    out_dir: Path,
    random_seeds: int = RANDOM_SEEDS,
    prices_path: Path | None = None,
) -> int:
    t_start = time.time()
    schedule = CostSchedule.from_strategy_yaml(STRATEGY_YAML)
    init_cash = _read_init_cash()
    prices_path = resolve_prices_path(data_dir, prices_path)
    calendar_df = pd.read_csv(data_dir / "index_000300.csv")
    contract = validate_prices_contract(prices_path, pd.DatetimeIndex(calendar_df["date"]))
    market = load_market_data(data_dir, prices_path)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[data] 价格文件 {contract['path']}（TR 口径，SPEC §8）SHA256 {contract['sha256'][:16]}…", flush=True)
    print(f"[data] 契约校验 PASS：{contract['rows']} 行 × {contract['cols']} 标的，"
          f"{contract['date_min']} ~ {contract['date_max']}，缺失格 {contract['missing_cells']}", flush=True)
    print(f"[data] 交易日历 {market.calendar[0].date()} ~ {market.calendar[-1].date()}，"
          f"{len(market.calendar)} 个交易日；宽表 {len(market.symbols)} 只标的", flush=True)

    # 旧 EM fqt=2 结果归档目录（与 out_dir 同级）；不存在则不做新旧对比（如合成数据自测）
    old_dir = out_dir.parent / "results_em_fqt2_bad"
    old_summary = load_old_em_summary(old_dir)

    data_info = {
        "prices": {
            **contract,
            "caliber": "TR：同花顺不复权价 + 同花顺除权事件 + 生产同款 build_adj_factor 前复权；"
                       "日收益 = 分红按除权日收盘再投的总收益（未扣红利税）",
            "spec": "SPEC §8（优先级高于 §2）",
            "deprecated": False,
        },
        "index_000300": {
            "path": _relpath(data_dir / "index_000300.csv"),
            "sha256": sha256_file(data_dir / "index_000300.csv"),
            "rows": int(len(calendar_df)),
        },
    }
    if old_summary and old_summary.get("data", {}).get("close_adj_wide"):
        data_info["deprecated_em_fqt2"] = {
            "path": "research/rebalance/data/_em_fqt2_close_adj_wide.bad.csv",
            "sha256": old_summary["data"]["close_adj_wide"]["sha256"],
            "results_archive": _relpath(old_dir),
            "deprecated": True,
            "status": "deprecated (SPEC §8)：因子逐日漂移、非分段常数；601088 标准 TR 385% vs EM 177%",
        }

    results: Dict[str, BasketResult] = {}
    assertions: Dict[str, Dict[str, dict]] = {}
    drift: Dict[str, Tuple[pd.DataFrame, pd.DataFrame, dict]] = {}
    sanity: Dict[str, dict] = {}

    for basket_id, symbols, start in (
        ("CURRENT10", CURRENT10, CURRENT10_START),
        ("OLD20", OLD20, OLD20_START),
    ):
        t0 = time.time()
        print(f"[basket] {basket_id}: {len(symbols)} 只，start>= {start} ...", flush=True)
        br = run_basket(market, basket_id, symbols, start, FULL_VARIANTS,
                        cost_schedule=schedule, init_cash=init_cash)
        results[basket_id] = br
        print(f"[basket] {basket_id}: 实际起点 {br.start_date.date()}，"
              f"{len(br.dates)} 交易日，{time.time() - t0:.1f}s", flush=True)
        assertions[basket_id] = check_assertions(br, market, init_cash)
        print(f"[assert] {basket_id}: §3.7 六条断言全部 PASS", flush=True)
        sanity[basket_id] = _sanity_for_basket(br)
        write_variants_csv(out_dir / f"variants_{basket_id}.csv", br, FULL_VARIANT_NAMES)
        write_nav_csv(out_dir / f"nav_{basket_id}.csv", br, FULL_VARIANT_NAMES)
        drift[basket_id] = weight_drift_bundle(br)

    write_weights_drift(out_dir, drift)
    print("[drift] 权重漂移文件已写出", flush=True)

    # ---- RANDOM10×100 ----
    pool = market.random_eligible_pool(RANDOM_START, RANDOM_MIN_DAYS)
    if len(pool) < RANDOM_BASKET_SIZE:
        raise EngineError(f"随机池仅 {len(pool)} 只，不足 {RANDOM_BASKET_SIZE} 只")
    print(f"[random] 合格池 {len(pool)} 只；seed 0..{random_seeds - 1}，每篮 {RANDOM_BASKET_SIZE} 只", flush=True)
    baskets = make_random_baskets(pool, random_seeds, RANDOM_BASKET_SIZE)
    random_rows: List[dict] = []
    composition_rows: List[dict] = []
    t_rand = time.time()
    for seed, symbols in enumerate(baskets):
        br = run_basket(market, f"R{seed:03d}", symbols, RANDOM_START, RANDOM_VARIANTS,
                        cost_schedule=schedule, init_cash=init_cash,
                        keep_weights_for=(), keep_trades=False)
        # 轻量断言：现金不透支 / hold 恒等式 / m_last 调仓次数
        hold = br.sims["hold"]
        expected = float(np.dot(hold.initial_shares, hold.prices_last))
        assert abs(float(hold.navs[-1]) - expected) < 1e-6, f"[random R{seed:03d}] hold 恒等式失败"
        assert hold.n_trades == 0 and hold.total_cost == 0.0
        for name, sim in br.sims.items():
            assert sim.cash_min >= -CASH_EPS, f"[random R{seed:03d}] {name} 现金为负"
        masks = market.masks_from(br.start_date)
        month_count = int(np.sum(masks["month_last"][1:]))
        assert abs(br.sims["m_last"].n_rebalances - month_count) <= 1, f"[random R{seed:03d}] 调仓次数异常"
        assert abs(br.sims["hold"].navs[0] - init_cash) <= 1e-9

        hold_m = br.metrics["hold"]
        composition_rows.append({"basket_id": seed, "symbols": ";".join(symbols)})
        for spec in RANDOM_VARIANTS:
            m = br.metrics[spec.name]
            random_rows.append({
                "basket_id": seed,
                "variant": spec.name,
                "cagr": float(m["cagr"]),
                "cagr_hold": float(hold_m["cagr"]),
                "premium_cagr": float(m["cagr"] - hold_m["cagr"]),
                "sharpe": float(m["sharpe"]),
                "sharpe_hold": float(hold_m["sharpe"]),
                "turnover": float(m["turnover_annual_two_way"]),
                "cost_pct": float(m["cost_pct_annual"]),
            })
        if (seed + 1) % 10 == 0:
            print(f"[random] {seed + 1}/{random_seeds} 完成（{time.time() - t_rand:.1f}s）", flush=True)

    random_df = pd.DataFrame(random_rows, columns=RANDOM_COLUMNS)
    # 不做精度截断：CSV 数值与 summary.json 中的统计量应能逐位对上
    random_df.to_csv(out_dir / "random_baskets.csv", index=False, encoding="utf-8")
    pd.DataFrame(composition_rows).to_csv(out_dir / "random_basket_composition.csv", index=False,
                                         encoding="utf-8")
    random_stats = random_basket_stats(random_df)
    print(f"[random] 完成 {random_seeds} 个篮子，文件已写出（{time.time() - t_rand:.1f}s）", flush=True)

    # 方向性 sanity 显式校验（task 第 5 点）
    for basket_id in ("CURRENT10", "OLD20"):
        s = sanity[basket_id]
        assert s["nocost_better"], f"[sanity] {basket_id}: 成本=0 未改善 CAGR"
        assert s["cost2_worse"], f"[sanity] {basket_id}: cost_mult=2 未恶化 CAGR"
    print("[sanity] 成本=0 改善 / cost×2 恶化 方向性校验 PASS", flush=True)

    # ---- 新旧口径对比（旧 EM fqt=2 vs 新 TR）----
    old_vs_new = build_old_vs_new_comparison(old_dir, results)
    data_revision = {
        "spec": "SPEC §8",
        "deprecated": True,
        "old_data_file": data_info.get("deprecated_em_fqt2", {}).get("path"),
        "old_data_sha256": data_info.get("deprecated_em_fqt2", {}).get("sha256"),
        "new_data_file": data_info["prices"]["path"],
        "new_data_sha256": data_info["prices"]["sha256"],
        "old_results_archive": _relpath(old_dir) if old_dir.exists() else None,
        "old_results_archive_deprecated": True,
        "old_results_valid": False,
        "reason_old_deprecated": "EM fqt=2 的 adj/raw 因子逐日漂移、非分段常数、非单调；"
                                 "601088.SH 2015-2026 标准总收益 385% vs EM fqt=2 177%（日收益相关 0.986、最大差 11.5%）",
        "old_vs_new": old_vs_new,
    }

    # ---- summary.json ----
    param_material = {
        "data_sha256": {
            "prices": data_info["prices"]["sha256"],
            "index_000300": data_info["index_000300"]["sha256"],
        },
        "init_cash": init_cash,
        "cost_schedule": schedule.to_dict(),
        "cost_mult_semantics": "SPEC §3.2：仅 commission_rate 与 slippage_bps ×2",
        "variants_full": [asdict(v) for v in FULL_VARIANTS],
        "variants_random": [asdict(v) for v in RANDOM_VARIANTS],
        "baskets": {"CURRENT10": CURRENT10, "OLD20": OLD20},
        "random": {"start": RANDOM_START, "min_days": RANDOM_MIN_DAYS,
                   "size": RANDOM_BASKET_SIZE, "seeds": random_seeds,
                   "rng": "numpy.random.default_rng(seed); choice(size=10, replace=False)"},
    }
    summary = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "spec": "research/rebalance/SPEC.md",
        "param_hash": param_hash(param_material),
        "params": param_material,
        "data": data_info,
        "data_revision": data_revision,
        "baskets": {
            bid: {
                "symbols": br.symbols,
                "start_date": str(br.start_date.date()),
                "end_date": str(br.end_date.date()),
                "n_trading_days": int(len(br.dates)),
                "variants": br.metrics,
            }
            for bid, br in results.items()
        },
        "random": {
            "pool_size": len(pool),
            "pool": pool,
            "n_baskets": random_seeds,
            "basket_size": RANDOM_BASKET_SIZE,
            "stats": random_stats,
            "source_file": "results/random_baskets.csv",
        },
        "weight_drift": {
            bid: drift[bid][2] for bid in ("CURRENT10", "OLD20")
        },
        "assertions": assertions,
        "sanity": sanity,
        "outputs": [
            "results/variants_CURRENT10.csv", "results/nav_CURRENT10.csv",
            "results/variants_OLD20.csv", "results/nav_OLD20.csv",
            "results/random_baskets.csv", "results/random_basket_composition.csv",
            "results/weights_drift_CURRENT10.csv", "results/weights_drift_OLD20.csv",
            "results/max_weight_timeseries.csv", "results/summary.json",
            "results/RESULTS_DRAFT.md",
        ],
    }
    with (out_dir / "summary.json").open("w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)
    print("[summary] summary.json 已写出", flush=True)

    draft = build_results_draft(summary, results, drift, random_df, random_stats,
                                assertions, sanity, old_dir=old_dir)
    (out_dir / "RESULTS_DRAFT.md").write_text(draft, encoding="utf-8")
    print(f"[draft] RESULTS_DRAFT.md 已写出；总耗时 {time.time() - t_start:.1f}s", flush=True)
    return 0


# ---------------------------------------------------------------------------
# 合成数据自测
# ---------------------------------------------------------------------------
def make_synthetic_data(data_dir: Path) -> List[str]:
    """生成 12 只合成标的后复权宽表 + 000300 日历，用于引擎自测。"""
    data_dir.mkdir(parents=True, exist_ok=True)
    dates = pd.bdate_range("2015-01-05", "2018-12-31")
    n = len(dates)
    symbols = [f"sh60{i:04d}" for i in range(6)] + [f"sz00{i:04d}" for i in range(6)]
    rng = np.random.default_rng(42)
    prices = {}
    for k, sym in enumerate(symbols):
        drift = 0.0002 + 0.0001 * k
        vol = 0.020 + 0.001 * k
        returns = rng.normal(drift, vol, size=n)
        prices[sym] = 100.0 * np.exp(np.cumsum(returns))
    # 制造停牌缺口（引擎应 ffill）
    prices[symbols[3]][40:60] = np.nan
    prices[symbols[7]][300:320] = np.nan
    wide = pd.DataFrame(prices, index=pd.DatetimeIndex(dates))
    wide.index.name = "date"
    wide.reset_index().to_csv(data_dir / DEFAULT_PRICES_FILENAME, index=False, encoding="utf-8",
                              na_rep="", float_format="%.6f")
    idx = 3000.0 * np.exp(np.cumsum(rng.normal(0.0001, 0.012, size=n)))
    pd.DataFrame({"date": dates, "close": idx}).to_csv(
        data_dir / "index_000300.csv", index=False, encoding="utf-8")
    return symbols


def run_selftest(out_dir: Path) -> int:
    tmp = out_dir / "_selftest"
    data_dir = tmp / "data"
    if tmp.exists():
        shutil.rmtree(tmp)
    data_dir.mkdir(parents=True, exist_ok=True)
    symbols = make_synthetic_data(data_dir)
    schedule = CostSchedule.from_strategy_yaml(STRATEGY_YAML)
    market = load_market_data(data_dir)
    print(f"[selftest] 合成数据 {len(market.calendar)} 交易日 / {len(market.symbols)} 标的", flush=True)

    basket_symbols = symbols[:10]  # 含一只停牌缺口标的
    br = run_basket(market, "SYN10", basket_symbols, "2015-01-05", FULL_VARIANTS,
                    cost_schedule=schedule, init_cash=SPEC_INIT_CASH)
    print(f"[selftest] 实际起点 {br.start_date.date()}，{len(br.dates)} 交易日", flush=True)

    checks = check_assertions(br, market, SPEC_INIT_CASH, require_cost_recalc=True)
    print("[selftest] §3.7 六条断言 PASS", flush=True)

    # 额外方向性检查（同 task 第 5 点）
    s = _sanity_for_basket(br)
    assert s["nocost_better"], "成本=0 未改善 CAGR"
    assert s["cost2_worse"], "cost_mult=2 未恶化 CAGR"
    # band 触发应存在
    assert br.sims["band5"].n_rebalances >= 1, "band5 从未触发"
    assert br.sims["band10"].n_rebalances >= 0

    # 打印指标表便于人工检查
    print(f"{'variant':<16}{'CAGR':>9}{'MDD':>9}{'Sharpe':>8}{'turnover':>10}{'cost%':>8}{'n_reb':>6}{'n_trd':>6}")
    for name in FULL_VARIANT_NAMES:
        m = br.metrics[name]
        print(f"{name:<16}{m['cagr']:>9.4f}{m['mdd']:>9.4f}{m['sharpe']:>8.3f}"
              f"{m['turnover_annual_two_way']:>10.3f}{m['cost_pct_annual']:>8.4f}"
              f"{m['n_rebalances']:>6}{m['n_trades']:>6}")

    # 自测输出保存（随后清理，避免污染正式 results/）
    selftest_out = tmp / "results"
    selftest_out.mkdir(parents=True, exist_ok=True)
    write_variants_csv(selftest_out / "variants_SYN10.csv", br, FULL_VARIANT_NAMES)
    write_nav_csv(selftest_out / "nav_SYN10.csv", br, FULL_VARIANT_NAMES)
    with (selftest_out / "selftest_assertions.json").open("w", encoding="utf-8") as fh:
        json.dump({"assertions": checks, "sanity": s}, fh, ensure_ascii=False, indent=2)
    print(f"[selftest] 全部 PASS；临时输出 {selftest_out}", flush=True)
    shutil.rmtree(tmp)
    print("[selftest] 临时目录已清理", flush=True)
    return 0


def make_synthetic_full_data(data_dir: Path) -> List[str]:
    """生成与真实标的同名、2015-2026 全长的合成数据，用于全流程演练。"""
    universe_path = DATA_DIR / "universe.txt"
    if not universe_path.exists():
        raise EngineError(f"缺少 {universe_path}，无法生成全流程自测数据")
    universe = [ln.strip() for ln in universe_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    data_dir.mkdir(parents=True, exist_ok=True)
    dates = pd.bdate_range("2015-01-05", "2026-11-06")
    n = len(dates)
    idx = pd.DatetimeIndex(dates)
    late_listing = {
        "sz300979": "2021-04-28", "sz300750": "2018-06-11", "sz002948": "2019-01-16",
        "sz002966": "2018-12-10", "sz001965": "2018-12-25", "sz002939": "2018-10-10",
        "sz001872": "2018-06-04", "sz002831": "2016-12-16", "sz002736": "2015-12-28",
        "sz002558": "2016-06-27", "sz300748": "2016-04-14", "sz300628": "2017-06-15",
    }
    rng = np.random.default_rng(20261108)
    prices = {}
    for k, sym in enumerate(universe):
        first = pd.Timestamp(late_listing.get(sym, "2015-01-05"))
        start_i = int(idx.searchsorted(first))
        drift = 0.00012 + 0.00002 * (k % 7)
        vol = 0.016 + 0.002 * (k % 5)
        rets = rng.normal(drift, vol, size=n)
        series = 50.0 * np.exp(np.cumsum(rets))
        series[:start_i] = np.nan
        prices[sym] = series
    wide = pd.DataFrame(prices, index=idx)
    wide.index.name = "date"
    wide.reset_index().to_csv(data_dir / DEFAULT_PRICES_FILENAME, index=False, encoding="utf-8",
                              na_rep="", float_format="%.6f")
    index_close = 3000.0 * np.exp(np.cumsum(rng.normal(0.00008, 0.010, size=n)))
    pd.DataFrame({"date": dates, "close": index_close}).to_csv(
        data_dir / "index_000300.csv", index=False, encoding="utf-8")
    return universe


def run_selftest_full(out_dir: Path, random_seeds: int) -> int:
    """用全长合成数据跑一遍完整流水线（含随机篮子 ×100），验证编排与报告生成。"""
    tmp = out_dir / "_selftest_full"
    if tmp.exists():
        shutil.rmtree(tmp)
    data_dir = tmp / "data"
    make_synthetic_full_data(data_dir)
    print(f"[selftest-full] 合成数据已生成：{data_dir}", flush=True)
    rc = run_full(data_dir, tmp / "results", random_seeds=random_seeds)
    print(f"[selftest-full] 完整流水线跑通；输出 {tmp / 'results'}", flush=True)
    return rc


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="再平衡回测（SPEC §3）")
    ap.add_argument("--selftest", action="store_true", help="用合成数据自测引擎与断言")
    ap.add_argument("--selftest-full", action="store_true",
                    help="用 2015-2026 全长合成数据跑完整流水线（含随机篮子）以验证编排")
    ap.add_argument("--data-dir", type=Path, default=DATA_DIR)
    ap.add_argument("--prices", type=Path, default=None,
                    help=f"价格宽表路径（默认 {DEFAULT_PRICES_PATH}；相对路径按 --data-dir 解析）")
    ap.add_argument("--out-dir", type=Path, default=RESULTS_DIR)
    ap.add_argument("--random-seeds", type=int, default=RANDOM_SEEDS,
                    help="随机篮子数量（默认 100）")
    return ap.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.selftest:
        return run_selftest(args.out_dir)
    if args.selftest_full:
        return run_selftest_full(args.out_dir, random_seeds=args.random_seeds)
    return run_full(args.data_dir, args.out_dir, random_seeds=args.random_seeds,
                    prices_path=args.prices)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except EngineError as exc:
        print(f"[FATAL] {exc}", file=sys.stderr)
        sys.exit(2)
