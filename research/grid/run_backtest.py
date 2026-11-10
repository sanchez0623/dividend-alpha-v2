#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dividend-alpha-v2 / research/grid —— 70/30 核心+网格 回测跑批与产出

产出：
  research/grid/results/{summary.json,trades.csv,nav.csv,yearly.csv,variants.csv,
                         equity_curve.png,decomposition.json}
用法：
  python research/grid/run_backtest.py
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
for _p in (str(ROOT), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
os.environ.setdefault("DA_CONFIG_DIR", str(ROOT / "config"))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import engine as E  # noqa: E402

RESULTS = HERE / "results"
SUB_START_2021 = date(2021, 1, 4)
BENCH = ["hold100", "core70_cash30"]
MAIN = "core70_grid30_sym_trailing"


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def decomposition(res: dict, bench_core: dict, bench_hold: dict) -> dict:
    """分解超额：网格 P&L（分红+价格-成本-税）与现金拖累。"""
    gross = res["grid_excess_gross"]          # grid_final - grid_sleeve
    div = res["grid_div_gross"]
    costs = res["buy_fees"] + res["sell_fees"]
    tax_real = res["grid_tax"]
    deferred = res["deferred_tax"]
    price_pnl_total = gross - div + costs + tax_real   # 价格 P&L（已实现+未实现）
    grid_net = div + price_pnl_total - costs - tax_real - deferred
    w_var = res["final_wealth_net"]
    w_core = bench_core["final_wealth_net"]
    w_hold = bench_hold["final_wealth_net"]
    excess_core = w_var - w_core
    excess_hold = w_var - w_hold
    cash_drag = w_core - w_hold               # 30% 现金相对满仓的拖累
    return {
        "final_wealth_net": round(w_var, 2),
        "bench_core70_cash30_wealth": round(w_core, 2),
        "bench_hold100_wealth": round(w_hold, 2),
        "excess_vs_core70_cash30": round(excess_core, 2),
        "excess_vs_hold100": round(excess_hold, 2),
        "grid_price_pnl_total": round(price_pnl_total, 2),
        "grid_div_gross": round(div, 2),
        "grid_costs": round(costs, 2),
        "grid_tax_realized": round(tax_real, 2),
        "grid_tax_deferred": round(deferred, 2),
        "grid_net_pnl": round(grid_net, 2),
        "cash_drag_30pct_vs_hold100": round(cash_drag, 2),
        "identity_check_vs_core70": round(excess_core - grid_net, 6),
        "identity_check_vs_hold100": round(excess_hold - (grid_net + cash_drag), 6),
        "components_note": ("超额(vs core70_cash30) = 网格分红 + 价格 P&L − 成本 − 税(含递延)；"
                            "vs hold100 再减 30% 现金拖累"),
    }


def run_all(data, cfg, start: date, end: date) -> tuple[dict, dict[str, dict]]:
    results = {}
    metrics = {}
    for va in E.variant_list():
        res = E.run_variant(data, va, cfg, start, end)
        results[va.name] = res
        metrics[va.name] = E.compute_metrics(res, start, end)
        m = metrics[va.name]
        log(f"  {va.name:<32s} CAGR {m['cagr']:>8.2%} total {m['total_return']:>9.2%} "
            f"MDD {m['mdd']:>7.2%} trades {m['grid_trades']:>4d} "
            f"win {m['grid_win_rate']:>5.1%}")
    return results, metrics


def plot_curves(results: dict, out_png: Path) -> None:
    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
    fig, axes = plt.subplots(2, 1, figsize=(12, 8), sharex=True,
                             gridspec_kw={"height_ratios": [3, 1]})
    ax = axes[0]
    styles = {
        "hold100": ("hold100 (100% buy&hold)", "gray", "--"),
        "core70_cash30": ("core70_cash30 (70% core + 30% cash)", "tab:orange", "--"),
        "core70_grid30_sym_trailing": ("main: 70/30 + 2% trailing grid", "tab:blue", "-"),
        "core70_grid30_sym_fixed": ("70/30 + 2% fixed grid", "tab:green", "-"),
        "core70_grid30_dip": ("70/30 + dip grid (30% cash)", "tab:red", "-"),
    }
    for name, (label, color, ls) in styles.items():
        if name not in results:
            continue
        nav = results[name]["nav"]
        dates = pd.to_datetime(nav["date"])
        ax.plot(dates, nav["nav"].astype(float), label=label, color=color, ls=ls,
                lw=1.4 if name.startswith("core70_grid30_sym_trailing") else 1.0)
    ax.axhline(1.0, color="gray", lw=0.6, ls=":")
    ax.set_ylabel("NAV (init=1.0)")
    ax.set_title("S* 10 holdings: 70% core + 30% grid (2% step, daily close), 2016-2026")
    ax.legend(loc="upper left", fontsize=8)
    ax.grid(alpha=0.3)
    # drawdown of main
    nav = results["core70_grid30_sym_trailing"]["nav"]
    dates = pd.to_datetime(nav["date"])
    eq = nav["equity"].astype(float).to_numpy()
    w = np.concatenate([[E.INIT_CASH], eq])
    peak = np.maximum.accumulate(w)
    dd = w / peak - 1.0
    ax2 = axes[1]
    ax2.fill_between(dates, dd[1:] * 100, 0, color="tab:blue", alpha=0.4)
    ax2.set_ylabel("main drawdown (%)")
    ax2.grid(alpha=0.3)
    fig.tight_layout()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png, dpi=130)
    plt.close(fig)


def main() -> int:
    RESULTS.mkdir(parents=True, exist_ok=True)
    cfg = E.load_configs()
    log("加载 10 只标的日线/除权事件（research/dividend_capture/data，同源）…")
    data = E.load_data()
    for s in E.SYMBOLS:
        b = data.bars.get(s) or {}
        e = data.events.get(s) or []
        log(f"  {s} {data.names.get(s, ''):<8s} bars={len(b)} events={len(e)} "
            f"first={min(b) if b else None} last={max(b) if b else None}")
    # 输入数据指纹
    inputs = {p.name: E.sha256_of(p) for p in
              [E.BARS_FILE, E.EVENTS_FILE, E.CALENDAR_FILE] if p.exists()}

    log("全样本 2016-01-04 ~ 2026-11-06 跑全部变体…")
    full_results, full_metrics = run_all(data, cfg, E.START, E.END)
    log("2021-01-04 起子样本跑全部变体…")
    sub_results, sub_metrics = run_all(data, cfg, SUB_START_2021, E.END)

    # 超额分解（70/30 网格变体 vs 两个基准；基准用全样本结果）
    dec = {}
    for va in E.variant_list():
        if va.grid_mode == "none" or abs(va.core_ratio - 0.70) > 1e-9:
            continue
        dec[va.name] = decomposition(full_results[va.name],
                                     full_results["core70_cash30"],
                                     full_results["hold100"])
    dec_sub = {}
    for name in [MAIN, "core70_grid30_sym_fixed", "core70_grid30_dip"]:
        dec_sub[name] = decomposition(sub_results[name],
                                      sub_results["core70_cash30"],
                                      sub_results["hold100"])

    # ---- 落盘 ----
    main_res = full_results[MAIN]
    main_res["trades"].to_csv(RESULTS / "trades.csv", index=False, encoding="utf-8")
    # NAV 宽表（全部变体）
    nav_wide = None
    for name, res in full_results.items():
        nav = res["nav"][["date", "nav"]].rename(columns={"nav": name})
        nav_wide = nav if nav_wide is None else nav_wide.merge(nav, on="date", how="outer")
    nav_wide["date"] = pd.to_datetime(nav_wide["date"]).dt.strftime("%Y-%m-%d")
    nav_wide.to_csv(RESULTS / "nav.csv", index=False, encoding="utf-8",
                    float_format="%.6f")
    # 年度表（全部变体）
    yearly_rows = []
    for name, m in full_metrics.items():
        for y in m["yearly"]:
            yearly_rows.append({"variant": name, **y})
    pd.DataFrame(yearly_rows).to_csv(RESULTS / "yearly.csv", index=False,
                                     encoding="utf-8")
    # 变体汇总表
    cols = ["variant", "desc", "cagr", "total_return", "ann_vol", "sharpe", "mdd",
            "calmar", "final_equity", "final_wealth_net", "deferred_tax",
            "grid_trades", "grid_trades_per_year", "grid_buys", "grid_sells",
            "grid_costs", "grid_tax_realized", "grid_div_gross",
            "grid_realized_price_pnl", "grid_win_rate", "grid_round_trips",
            "max_grid_units", "exposure_ratio", "grid_turnover",
            "avg_grid_trade_notional", "core_div_gross", "grid_excess_gross"]
    variants_df = pd.DataFrame([{k: m.get(k) for k in cols}
                                for m in full_metrics.values()])
    variants_df.to_csv(RESULTS / "variants.csv", index=False, encoding="utf-8")
    plot_curves(full_results, RESULTS / "equity_curve.png")

    hashes = {name: E.snapshot_hashes(res) for name, res in full_results.items()
              if name in BENCH or name == MAIN}
    summary = {
        "generated_at": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S%z"),
        "title": "S* 10 holdings: 70/30 core+grid (2% step, daily close) backtest",
        "selection_bias_note": (
            "这 10 只是 2026 年选出的 S* 当前持仓；用它们回溯 2016 存在选择/幸存者偏差。"
            "本回测回答『如果这 10 只从 2016 就这么网格』，不是 S* 历史策略。"),
        "symbols": E.SYMBOLS,
        "names": data.names,
        "start": E.START.isoformat(), "end": E.END.isoformat(),
        "init_cash": E.INIT_CASH, "per_stock": E.PER_STOCK,
        "inputs_sha256": inputs,
        "accounting": {
            "costs": "src/da_core/cost.py buy_cost/sell_cost；config/strategy.yaml cost 段",
            "core_dividend_tax": "0%（持有>1年；Lead 冻结口径）",
            "grid_dividend_tax": "FIFO 批次实际持有天数：<=30天 20% / 31~365天 10% / >365天 0%",
            "bonus": "per_share_bonus 扩股、成本价除权",
            "cash_interest": 0.0,
            "lot_size": 100,
            "T_plus_1": "卖出仅限买入日 < 卖出日的网格批次",
        },
        "full": {name: m for name, m in full_metrics.items()},
        "subsample_2021": {name: m for name, m in sub_metrics.items()},
        "decomposition": dec,
        "decomposition_subsample_2021": dec_sub,
        "reproducibility": hashes,
        "data_quality": {
            "bars_symbols": len(data.bars),
            "events_symbols": sum(1 for s in E.SYMBOLS if data.events.get(s)),
            "suspension_note": ("停牌无 bar 跳过交易、用最近可信收盘估值；"
                                "格力/神华/君正/南山/泸州老窖均有停牌段（见 bars 缺口）"),
        },
        "caveats": [
            "选择/幸存者偏差：10 只为 2026 年当前持仓，回溯 2016 会高估表现。",
            "2% 网格参数未做样本外优化；step 1.5%/2.5% 为敏感性而非最优参数。",
            "每日收盘单点成交，未建模盘中触发/滑点差异（成本已含 5bp 滑点）。",
            "T+1 只对卖出批次做约束；买入当日不可卖。",
            "涨跌停无法成交、停牌、流动性冲击未建模。",
            "core 红利按 0% 税（持有>1年）；若按实际持有天数，仅 2016 年首年分红应缴 10%（影响 <0.1pp/年）。",
            "回测≠未来；历史结果不代表未来收益。",
        ],
    }
    (RESULTS / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                          encoding="utf-8")
    (RESULTS / "decomposition.json").write_text(
        json.dumps({"full": dec, "subsample_2021": dec_sub}, ensure_ascii=False,
                   indent=2), encoding="utf-8")

    log("—— 关键结果（全样本）——")
    for name in ["hold100", "core70_cash30", MAIN, "core70_grid30_sym_fixed",
                 "core70_grid30_dip"]:
        m = full_metrics[name]
        d = dec.get(name, {})
        log(f"  {name:<32s} CAGR {m['cagr']:>8.2%} wealth {m['final_wealth_net']:>12,.0f} "
            f"MDD {m['mdd']:>7.2%} trades {m['grid_trades']:>4d} "
            f"excess_vs_core70 {d.get('excess_vs_core70_cash30', 0):>12,.0f} "
            f"excess_vs_hold100 {d.get('excess_vs_hold100', 0):>12,.0f}")
    log("—— 关键结果（2021 起）——")
    for name in ["hold100", "core70_cash30", MAIN]:
        m = sub_metrics[name]
        d = dec_sub.get(name, {})
        log(f"  {name:<32s} CAGR {m['cagr']:>8.2%} wealth {m['final_wealth_net']:>12,.0f} "
            f"MDD {m['mdd']:>7.2%} excess_vs_core70 "
            f"{d.get('excess_vs_core70_cash30', 0):>12,.0f}")
    log(f"产出：{RESULTS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
