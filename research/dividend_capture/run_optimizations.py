#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dividend-alpha-v2 / research/dividend_capture —— DC 优化方向回测（task-3 追加）

在 **point-in-time 主口径**（买入日当时总股本 × 当日收盘 ≥500 亿）基础上，测：
  A. 条件卖出（替代固定 ex+30 自然日；max_hold=30 自然日兜底）
     - fixed_22td（固定除权后 22 个交易日，参照）
     - fill（填权达标就走：除权后收盘 >= 除权前最后交易日收盘）
     - no_fill_3d（除权后第 3 个交易日收盘仍未填权 → 走）
     - stop_buy_3pct（**主口径**：买入价 -3% 止损，除权日 close 跌破也砍）
     - stop_ex_3pct（次口径敏感性：以除权日收盘为基准，避免除权缺口机械触发）
     - cover_tax（收益覆盖红利税：按**实际卖出日税率**，price >= buy_price - dps + rate*dps）
     - combo_primary（fill|no_fill_3d|stop_buy|cover_tax）
     - combo_nostop（fill|no_fill_3d|cover_tax）
     - combo_primary_ex（fill|no_fill_3d|stop_ex|cover_tax 敏感性）
  B. 选池（yield>=6% 仍是硬条件）
     - cap300（hard >=300 亿，trend nondown 硬筛）
     - cap300_score / cap500_score / cap_any_score（hard cap + trend 只加分排序）
       score = yield + 0.5%*(cap>=500亿) + 0.5%*trend_score(up_pull=1/up=.5/nondown=0/down=-.5)
  C. 组合：自动取 exit / selection 各自 CAGR Top2 交叉组合。

注意（必须写进报告）：
  - 早卖（<30 自然日）适用 **20% 红利税档**，fill/no_fill_3d/stop 等变体按实际持有天数结算；
    `cover_tax` 的目标价也按实际卖出日税率计算，不固定 10%。
  - 除权日 close 触发 stop_buy 时，当日仍先拿税前分红，再按实际持有天数扣税；成交价=当日 close。
  - 同一天 stop 与 fill 同时满足时 **stop 优先**（风控优先）。

产出：
  results/optimizations/{summary.json,optimizations.csv}
  results/summary.json 增加 optimizations
  web/dc_backtest.json 增加 optimizations
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

import pandas as pd  # noqa: E402

import engine as E  # noqa: E402

RESULTS = E.RESULTS
OPT_DIR = RESULTS / "optimizations"
WEB_JSON = ROOT / "web" / "dc_backtest.json"
START, END = E.BACKTEST_START, E.BACKTEST_END


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# 实验定义
# ---------------------------------------------------------------------------
EXIT_POLICIES = {
    "fixed_30d": (None, "固定除权+30自然日（base 参照）"),
    "fixed_22td": ({"fixed_trading_days": 22}, "固定除权后22个交易日"),
    "fill": ({"fill": True}, "填权达标即卖（close>=除权前最后收盘）"),
    "no_fill_3d": ({"no_fill_days": 3}, "除权后第3个交易日未填权即卖"),
    "stop_buy_3pct": ({"stop_ref": "buy", "stop_pct": 0.03},
                      "-3%止损（基准=买入价，主口径；除权日close跌破也砍）"),
    "stop_ex_3pct": ({"stop_ref": "ex", "stop_pct": 0.03},
                     "-3%止损（基准=除权日收盘，次口径敏感性）"),
    "cover_tax": ({"cover_tax": True},
                  "收益覆盖红利税即卖（按实际卖出日税率）"),
    "combo_primary": ({"fill": True, "no_fill_days": 3, "stop_ref": "buy",
                       "stop_pct": 0.03, "cover_tax": True},
                      "组合：fill|no_fill_3d|stop_buy|cover_tax"),
    "combo_nostop": ({"fill": True, "no_fill_days": 3, "cover_tax": True},
                     "组合：fill|no_fill_3d|cover_tax（不带止损）"),
    "combo_primary_ex": ({"fill": True, "no_fill_days": 3, "stop_ref": "ex",
                          "stop_pct": 0.03, "cover_tax": True},
                         "组合：fill|no_fill_3d|stop_ex|cover_tax（敏感性）"),
}

SCORE_CFG = {"cap_bonus": 0.005, "trend_bonus": 0.005, "cap_ref": 5e10}

SELECTORS = {
    "base_cap500_hard": {
        "overrides": {},
        "rank_mode": "pure",
        "desc": "hard cap>=500亿 + trend nondown 硬筛（base）",
    },
    "cap300": {
        "overrides": {"min_market_cap": 3e10},
        "rank_mode": "pure",
        "desc": "hard cap>=300亿 + trend nondown 硬筛",
    },
    "cap300_score": {
        "overrides": {"min_market_cap": 3e10, "trend_enabled": False},
        "rank_mode": "score",
        "desc": "hard cap>=300亿；trend 只加分，score 排序",
    },
    "cap500_score": {
        "overrides": {"min_market_cap": 5e10, "trend_enabled": False},
        "rank_mode": "score",
        "desc": "hard cap>=500亿；trend 只加分，score 排序",
    },
    "cap_any_score": {
        "overrides": {"min_market_cap": 1e10, "trend_enabled": False},
        "rank_mode": "score",
        "desc": "hard cap>=100亿防微盘；cap/trend 只加分，score 排序",
    },
}


def _row(res: dict, group: str, selector: str, exit_name: str, stop_ref: str | None,
         desc: str, hard_cap: float | None = None) -> dict:
    m = res["metrics"]
    tr = res["sim"]["trades"]
    row = {
        "name": f"{selector}__{exit_name}",
        "label": m["label"],
        "group": group,
        "selector": selector,
        "exit_policy": exit_name,
        "stop_ref": stop_ref,
        "desc": desc,
        "hard_cap_yi": (hard_cap / 1e8) if hard_cap else None,
        "cagr": m["cagr"],
        "total_return": m["total_return"],
        "nav_total_return": m["nav_total_return"],
        "ann_vol": m["ann_vol"],
        "sharpe": m["sharpe"],
        "mdd": m["mdd"],
        "calmar": m["calmar"],
        "captures": m["captures"],
        "captures_per_year": m["captures_per_year"],
        "win_rate": m["win_rate"],
        "avg_trade_return": m["avg_trade_return"],
        "avg_holding_days": m["avg_holding_days"],
        "exposure_ratio": m.get("exposure_ratio", 0.0),
        "gross_dividend": m["gross_dividend"],
        "tax": m["tax"],
        "tax_ratio_of_gross": m.get("tax_ratio_of_gross", 0.0),
        "costs": m["costs"],
        "withdrawn": m["withdrawn"],
        "price_pnl": m["price_pnl"],
        "dividend_pnl": m["dividend_pnl"],
        "final_equity": m["final_equity"],
        "final_wealth": m["final_wealth"],
        "eligible_candidates": m.get("eligible_candidates", 0),
        "eligible_symbols": m.get("eligible_symbols", 0),
        "early_exits_le30d": m.get("early_exits_le30d", 0),
        "early_exit_tax": m.get("early_exit_tax", 0.0),
        "exit_reason_counts": m.get("exit_reason_counts", {}),
        "n_trades": int(len(tr)),
    }
    if hard_cap is not None:
        cands = res["candidates"]
        row["hard_cap_symbols"] = len({c.symbol for c in cands
                                       if c.market_cap >= hard_cap})
    return row


def _poly_for(name: str):
    return EXIT_POLICIES[name][0]


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> int:
    OPT_DIR.mkdir(parents=True, exist_ok=True)
    cfg, dc = E.load_configs()
    base_rules = E.DcRules.from_strategy(dc)
    log("加载 point-in-time 数据集…")
    ds = E.load_pit_dataset()
    log(f"  pit symbols={len(ds.symbols)} shares_hist={len(ds.shares_hist)}")

    # ---- 基准候选（选池 base）----
    log("构建 base 候选（cap500 hard + trend nondown）…")
    base_res = E.run_case(ds, cfg, dc, None, START, END, label="opt_base_fixed_30d",
                          rules=base_rules)
    base_cands = base_res["candidates"]
    base_stats = base_res["candidate_stats"]
    rows: list[dict] = []
    rows.append(_row(base_res, "exit", "base_cap500_hard", "fixed_30d", None,
                     EXIT_POLICIES["fixed_30d"][1], hard_cap=5e10))
    log(f"  base：CAGR {base_res['metrics']['cagr']:.2%} "
        f"captures {base_res['metrics']['captures']}")

    # ---- A. 条件卖出（复用 base 候选，不重建）----
    log(f"条件卖出变体 {len(EXIT_POLICIES) - 1} 个（base 候选复用）…")
    exit_rows: list[dict] = []
    for name, (policy, desc) in EXIT_POLICIES.items():
        if name == "fixed_30d":
            continue
        r = E.run_case(ds, cfg, dc, None, START, END, label=f"exit_{name}",
                       rules=base_rules, exit_policy=policy,
                       candidates=base_cands, candidate_stats=base_stats)
        row = _row(r, "exit", "base_cap500_hard", name,
                   (policy or {}).get("stop_ref"), desc, hard_cap=5e10)
        rows.append(row)
        exit_rows.append(row)
        log(f"  exit_{name:<18s} CAGR {row['cagr']:>7.2%} total {row['total_return']:>8.2%} "
            f"MDD {row['mdd']:>7.2%} caps {row['captures']:>3d} win {row['win_rate']:>5.1%} "
            f"avgHold {row['avg_holding_days']:>5.1f}d")

    # ---- B. 选池变体（base 卖出）----
    log(f"选池变体 {len(SELECTORS) - 1} 个…")
    sel_rows: list[dict] = []
    sel_cands: dict[str, tuple] = {}
    for name, spec in SELECTORS.items():
        if name == "base_cap500_hard":
            continue
        r = E.run_case(ds, cfg, dc, spec["overrides"], START, END,
                       label=f"sel_{name}", rules=base_rules,
                       rank_mode=spec.get("rank_mode", "pure"),
                       score_cfg=SCORE_CFG if spec.get("rank_mode") == "score" else None)
        hard = spec["overrides"].get("min_market_cap", 5e10)
        row = _row(r, "selection", name, "fixed_30d", None, spec["desc"], hard_cap=hard)
        rows.append(row)
        sel_rows.append(row)
        sel_cands[name] = (r["candidates"], r["candidate_stats"])
        log(f"  sel_{name:<16s} CAGR {row['cagr']:>7.2%} total {row['total_return']:>8.2%} "
            f"MDD {row['mdd']:>7.2%} caps {row['captures']:>3d} win {row['win_rate']:>5.1%} "
            f"selSymbols {row['eligible_symbols']:>4d}")

    # ---- C. 组合：exit / selection 各 Top2 交叉 ----
    def _key(r: dict) -> tuple:
        return (r["cagr"], r["total_return"])

    exits_sorted = sorted(exit_rows, key=_key, reverse=True)
    sels_sorted = sorted(sel_rows, key=_key, reverse=True)
    log(f"组合测试：exit Top {[r['exit_policy'] for r in exits_sorted[:2]]} × "
        f"sel Top {[r['selector'] for r in sels_sorted[:2]]}")
    combo_rows: list[dict] = []
    for ex in exits_sorted[:2]:
        for se in sels_sorted[:2]:
            spec = SELECTORS[se["selector"]]
            cds, cstats = sel_cands[se["selector"]]
            r = E.run_case(ds, cfg, dc, spec["overrides"], START, END,
                           label=f"combo_{ex['exit_policy']}_{se['selector']}",
                           rules=base_rules,
                           rank_mode=spec.get("rank_mode", "pure"),
                           score_cfg=SCORE_CFG if spec.get("rank_mode") == "score" else None,
                           exit_policy=_poly_for(ex["exit_policy"]),
                           candidates=cds, candidate_stats=cstats)
            hard = spec["overrides"].get("min_market_cap", 5e10)
            row = _row(r, "combo", se["selector"], ex["exit_policy"],
                       ex["stop_ref"], f"{se['desc']} + {ex['desc']}", hard_cap=hard)
            rows.append(row)
            combo_rows.append(row)
            log(f"  combo {ex['exit_policy']}×{se['selector']:<14s} "
                f"CAGR {row['cagr']:>7.2%} total {row['total_return']:>8.2%} "
                f"MDD {row['mdd']:>7.2%} caps {row['captures']:>3d}")

    df = pd.DataFrame(rows)
    df.to_csv(OPT_DIR / "optimizations.csv", index=False, encoding="utf-8")

    # 推荐：CAGR 最高且 MDD 可接受（|MDD|<=30%）的组合；否则按 CAGR
    cand_all = [r for r in rows if r["group"] in ("exit", "selection", "combo")]
    ok = [r for r in cand_all if abs(r["mdd"]) <= 0.30]
    ranked = sorted(ok or cand_all, key=_key, reverse=True)
    top3 = ranked[:3]
    summary = {
        "generated_at": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S%z"),
        "universe_mode": "point_in_time",
        "base": rows[0],
        "exit_policies": exit_rows,
        "selection": sel_rows,
        "combos": combo_rows,
        "top3": top3,
        "recommended": top3[0] if top3 else None,
        "notes": [
            "主 base：point-in-time 市值（买入日当时总股本×当日不复权收盘>=500亿），"
            "固定 ex+30 自然日卖出。",
            "条件卖出均保留 max_hold=30 自然日兜底（fixed_22td 变体按 22 个交易日）。",
            "stop_ref=buy_price（主口径）与 stop_ref=ex_close（次口径）都测；同日 stop 与 fill 同时满足时 stop 优先。",
            "除权日 close 触发 stop_buy 时：当日仍拿税前分红，再按实际持有天数扣税（<=30天为20%档）。",
            "cover_tax 目标价按实际卖出日适用税率计算（不固定10%）。",
            "选池 score = yield + 0.5%*(cap>=500亿) + 0.5%*trend_score（up_pull=1/up=.5/nondown=0/down=-.5）。",
        ],
        "caveats": [
            "条件卖出为回测内规则实验，未做样本外/多市场验证，也未做参数搜索（3% 止损、3 日不填权为固定值）。",
            "早卖触发 20% 红利税档，税负高于基准；结果已按实际持有天数结算。",
            "全样本 2016-2026 含 2016-2017 强 beta 段；2018/2021 子样本收益更差，优化结论不能外推。",
        ],
    }
    (OPT_DIR / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- 回写 results/summary.json ----
    s_path = RESULTS / "summary.json"
    if s_path.exists():
        s = json.loads(s_path.read_text(encoding="utf-8"))
        s["optimizations"] = {
            "generated_at": summary["generated_at"],
            "base": rows[0],
            "rows": rows,
            "top3": top3,
            "recommended": summary["recommended"],
            "notes": summary["notes"],
            "caveats": summary["caveats"],
        }
        s_path.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- 回写 web/dc_backtest.json ----
    opt_web = []
    for r in rows:
        opt_web.append({
            "name": r["name"],
            "label": r["label"],
            "group": r["group"],
            "cagr": r["cagr"],
            "total_return": r["total_return"],
            "nav_total_return": r["nav_total_return"],
            "mdd": r["mdd"],
            "max_drawdown": r["mdd"],
            "captures": r["captures"],
            "captures_per_year": r["captures_per_year"],
            "win_rate": r["win_rate"],
            "avg_holding_days": r["avg_holding_days"],
            "exposure_ratio": r["exposure_ratio"],
            "tax": r["tax"],
            "costs": r["costs"],
            "withdrawn": r["withdrawn"],
            "price_pnl": r["price_pnl"],
            "dividend_pnl": r["dividend_pnl"],
            "eligible_symbols": r["eligible_symbols"],
            "hard_cap_symbols": r.get("hard_cap_symbols"),
            "early_exits_le30d": r["early_exits_le30d"],
            "early_exit_tax": r["early_exit_tax"],
            "exit_reason_counts": r["exit_reason_counts"],
            "stop_ref": r["stop_ref"],
            "params": {
                "selector": r["selector"],
                "exit_policy": r["exit_policy"],
                "stop_ref": r["stop_ref"],
                "hard_cap_yi": r["hard_cap_yi"],
                "desc": r["desc"],
            },
        })
    if WEB_JSON.exists():
        web = json.loads(WEB_JSON.read_text(encoding="utf-8"))
    else:
        web = {"available": True, "universe_mode": "point_in_time"}
    web["optimizations"] = opt_web
    web["optimization_notes"] = summary["notes"]
    web["optimization_recommended"] = {
        "name": summary["recommended"]["name"],
        "cagr": summary["recommended"]["cagr"],
        "total_return": summary["recommended"]["total_return"],
        "mdd": summary["recommended"]["mdd"],
        "captures": summary["recommended"]["captures"],
        "win_rate": summary["recommended"]["win_rate"],
        "params": {
            "selector": summary["recommended"]["selector"],
            "exit_policy": summary["recommended"]["exit_policy"],
            "stop_ref": summary["recommended"]["stop_ref"],
        },
    } if summary["recommended"] else None
    WEB_JSON.write_text(json.dumps(web, ensure_ascii=False, indent=2), encoding="utf-8")

    log("—— Top 3（CAGR 排序，|MDD|<=30% 优先）——")
    for r in top3:
        log(f"  {r['name']:<40s} CAGR {r['cagr']:>7.2%} total {r['total_return']:>8.2%} "
            f"MDD {r['mdd']:>7.2%} caps {r['captures']:>3d} win {r['win_rate']:>5.1%}")
    log(f"产出：{OPT_DIR} / web/dc_backtest.json / results/summary.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
