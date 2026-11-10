#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dividend-alpha-v2 / research/dividend_capture —— point-in-time 回测验证（task-3）

验证：
  A. 数据可复现：bars_all / events_all / shares_history 与原始缓存独立重算一致；
     全 A 快照、ST/退剔除、退市股纳入情况。
  B. **point-in-time 市值正确性**：每个候选 cap = shares_at(买入日) × 当日不复权收盘
     （历史总股本缺失时回退估算并计数）；cap<500 亿的候选不得标记为合格；日期规则/无未来数据。
  C. 账户守恒：现金>=0、税/取现/成本逐笔复算、期末权益=期初+Σ净盈亏+未平仓市值。
  D. 可复现：同输入两次回测 trades/nav 哈希一致，且等于 summary.json 记录值。
  E. 旧口径对照：重算旧口径（当前市值筛池）与 results/legacy_current_cap/summary.json 一致。
  F. pool62（PIT 口径）基本校验。

产出：results/verification.json + 控制台 PASS/FAIL。
用法：python research/dividend_capture/verify.py [--skip-rerun]
"""
from __future__ import annotations

import argparse
import bisect
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
os.environ.setdefault("DA_CONFIG_DIR", str(ROOT / "config"))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import data_fetch_all as DF  # noqa: E402
import engine as E  # noqa: E402
from src.da_core.cost import buy_cost, sell_cost  # noqa: E402
from src.da_strategy.dividend_capture import (  # noqa: E402
    dividend_tax_for, entry_date_for, exit_date_for, ttm_dividend,
)

RESULTS = E.RESULTS
LEGACY_DIR = RESULTS / "legacy_current_cap"
TOL = 0.02


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-rerun", action="store_true")
    args = ap.parse_args()

    cfg, dc = E.load_configs()
    rules = E.DcRules.from_strategy(dc)
    E._install_trend_memo()   # 保留原始 compute_trend 引用供无未来数据校验
    summary = json.loads((RESULTS / "summary.json").read_text(encoding="utf-8"))
    legacy_summary = json.loads((LEGACY_DIR / "summary.json").read_text(encoding="utf-8"))
    checks: list[dict] = []

    def check(section: str, name: str, ok: bool, detail: str = "") -> None:
        checks.append({"section": section, "name": name, "ok": bool(ok),
                       "detail": str(detail)})
        print(f"  [{'PASS' if ok else 'FAIL'}] {section} · {name}  ({detail})", flush=True)

    start, end = E.BACKTEST_START, E.BACKTEST_END

    # ================= A. 数据 =================
    print("—— A. 数据（全 A + 退市 + 历史股本）——", flush=True)
    cal = E.read_calendar()
    check("A", "日历 2877 行", len(cal) == 2877, f"n={len(cal)}")
    spot = pd.read_csv(E.SPOT_ALL)
    check("A", "全 A 快照 >5000 行", len(spot) > 5000, f"n={len(spot)}")
    check("A", "ST/退剔除后候选 ~5000+",
          int((~spot["is_st_delist"]).sum()) > 5000,
          f"n={int((~spot['is_st_delist']).sum())}")

    bars_frames = [pd.read_parquet(E.PIT_BARS)]
    if E.PIT_DELISTED_BARS.exists():
        bars_frames.append(pd.read_parquet(E.PIT_DELISTED_BARS))
    bars_df = pd.concat(bars_frames, ignore_index=True)
    ev_df = pd.read_parquet(E.PIT_EVENTS)
    sh_df = pd.read_parquet(E.PIT_SHARES)
    pit_uni = pd.read_csv(E.PIT_UNIVERSE)
    dl = pd.read_csv(E.DELISTED_LIST) if E.DELISTED_LIST.exists() else pd.DataFrame(
        columns=["symbol"])
    delisted_syms = set(dl["symbol"])

    # A1 日线：raw_all 缓存（非退市）独立重算
    exp_bars: dict[str, dict[date, float]] = {}
    raw_files = sorted((E.DATA / "cache" / "raw_all").glob("raw_*.json"))
    for p in raw_files:
        obj = json.loads(p.read_text(encoding="utf-8"))
        for sym, d in DF.parse_raw(obj["payload"]).items():
            exp_bars.setdefault(sym, {}).update(d)
    parq_bars = {s: dict(zip(g["date"], g["close"])) for s, g in bars_df.groupby("symbol")}
    n_exp = 0
    bad = 0
    max_d = 0.0
    for s, dm in exp_bars.items():
        pm = parq_bars.get(s) or {}
        for d, c in dm.items():
            n_exp += 1
            pc = pm.get(d)
            if pc is None or abs(float(pc) - float(c)) > 1e-6:
                bad += 1
                max_d = max(max_d, abs(float(pc) - float(c)) if pc else 0.0)
    check("A", "日线=raw_all 缓存重算", bad == 0 and n_exp > 0,
          f"cache行={n_exp} 不一致={bad} maxΔ={max_d}")
    # 退市股日线来自 delisted 缓存
    dl_dir = E.DATA / "cache" / "delisted"
    dl_bad = 0
    if dl_dir.exists():
        for p in sorted(dl_dir.glob("dl_*.json")):
            obj = json.loads(p.read_text(encoding="utf-8"))
            pm = parq_bars.get(obj["symbol"]) or {}
            for b in obj["bars"]:
                d = datetime.strptime(b["date"], "%Y-%m-%d").date()
                if abs(float(pm.get(d, -1)) - float(b["close"])) > 1e-6:
                    dl_bad += 1
    check("A", "退市股日线=缓存", dl_bad == 0,
          f"退市股数={len(delisted_syms & set(parq_bars))} 不一致={dl_bad}")

    # A2 除权事件（只比对数据集覆盖的标的：精筛标的 + 退市股；粗筛未精筛标的不进数据集）
    data_syms = set(ev_df["symbol"])
    exp_ev: dict[tuple[str, date], float] = {}
    for p in sorted((E.DATA / "cache" / "ca_all").glob("ca_*.json")):
        obj = json.loads(p.read_text(encoding="utf-8"))
        ths = obj["thscode"]
        sym = ths.split(".")[1].lower() + ths.split(".")[0]
        if sym not in data_syms:
            continue
        for e in DF.parse_ca(obj["payload"]):
            if e["ex_date"] <= E.BACKTEST_END:
                k = (sym, e["ex_date"])
                exp_ev[k] = exp_ev.get(k, 0.0) + float(e["dps"])
    if dl_dir.exists():
        for p in sorted(dl_dir.glob("dl_*.json")):
            obj = json.loads(p.read_text(encoding="utf-8"))
            for e in obj["events"]:
                d = datetime.strptime(e["ex_date"], "%Y-%m-%d").date()
                k = (obj["symbol"], d)
                exp_ev[k] = exp_ev.get(k, 0.0) + float(e["dps"])
    parq_ev = {(r.symbol, r.ex_date): float(r.dps) for r in ev_df.itertuples()}
    ev_bad = sum(1 for k, v in exp_ev.items()
                 if abs(parq_ev.get(k, -1e9) - v) > 1e-6)
    ev_missing = len(set(parq_ev) - set(exp_ev))
    check("A", "除权事件=缓存重算", ev_bad == 0 and ev_missing == 0,
          f"事件数={len(parq_ev)} 不一致={ev_bad} 缓存缺失={ev_missing}")

    # A3 历史总股本
    exp_sh: dict[tuple[str, date], float] = {}
    for p in sorted((E.DATA / "cache" / "shares").glob("shares_*.json")):
        obj = json.loads(p.read_text(encoding="utf-8"))
        for rec in obj.get("payload") or []:
            exp_sh[(obj["symbol"], datetime.strptime(rec["date"], "%Y-%m-%d").date())] = \
                float(rec["total_shares"])
    if dl_dir.exists():
        for p in sorted(dl_dir.glob("dl_*.json")):
            obj = json.loads(p.read_text(encoding="utf-8"))
            for rec in obj["shares"]:
                exp_sh[(obj["symbol"], datetime.strptime(rec["date"], "%Y-%m-%d").date())] = \
                    float(rec["total_shares"])
    parq_sh = {(r.symbol, r.change_date): float(r.total_shares) for r in sh_df.itertuples()}
    sh_bad = sum(1 for k, v in exp_sh.items() if abs(parq_sh.get(k, -1) - v) > 1.0)
    check("A", "历史总股本=缓存重算", sh_bad == 0,
          f"记录={len(parq_sh)} 不一致={sh_bad}")

    # ================= B. point-in-time 市值 =================
    print("—— B. point-in-time 市值与无未来数据 ——", flush=True)
    ds = E.load_pit_dataset()
    check("B", "PIT 候选集 > 500 只", len(ds.symbols) > 500, f"n={len(ds.symbols)}")
    check("B", "有历史股本标的 >= 500", len(ds.shares_hist) >= 500,
          f"n={len(ds.shares_hist)}")

    cands, cstats = E.build_candidates(ds, rules, start, end)
    check("B", "候选数 > 5000", len(cands) > 5000, f"n={len(cands)}")
    fallback = cstats.get("cap_source_est_current_shares", 0)
    pit_n = cstats.get("cap_source_pit_shares", 0)
    check("B", "历史股本口径占比 >95%", pit_n > 0 and pit_n / max(pit_n + fallback, 1) > 0.95,
          f"pit={pit_n} fallback={fallback}")

    bad_cap = bad_elig_cap = bad_yield = bad_trend = bad_ttm = bad_date = 0
    for c in cands:
        price = ds.price_on(c.symbol, c.entry_date)
        if price is None:
            bad_cap += 1
            continue
        sh = ds.shares_at(c.symbol, c.entry_date)
        exp_cap = (sh * price) if sh is not None else ds.cap_on(c.symbol, c.entry_date, price)
        if abs(exp_cap - c.market_cap) > max(1.0, 1e-9 * abs(exp_cap)):
            bad_cap += 1
        if c.eligible and c.market_cap < rules.min_market_cap:
            bad_elig_cap += 1
        if (not c.eligible) and c.reason.startswith("cap") and c.market_cap >= rules.min_market_cap:
            bad_elig_cap += 1
        # 日期
        ent = entry_date_for(c.ex_date, cal, rules.entry_lead_trading_days)
        ex_ = exit_date_for(c.ex_date, cal, rules.hold_days_after_ex)
        if ent != c.entry_date or ex_ != c.exit_date or c.entry_date >= c.ex_date:
            bad_date += 1
        if (c.exit_date - c.ex_date).days < rules.hold_days_after_ex:
            bad_date += 1
        # 趋势只用 <= entry 数据
        adj = ds.adj.get(c.symbol)
        if adj is not None and len(adj):
            masked = adj.copy()
            if not isinstance(masked.index, pd.DatetimeIndex):
                masked.index = pd.to_datetime(masked.index)
            masked = masked[masked.index <= pd.Timestamp(c.entry_date)]
            fn = E._ORIG_COMPUTE_TREND or (lambda a, b, r: None)
            if fn is not None:
                st = fn(masked, c.entry_date, rules)
                if st is not None and st.label != c.trend.label:
                    bad_trend += 1
        evs = ds.events.get(c.symbol) or []
        ttm = ttm_dividend([(e["date"], e["dps"]) for e in evs], c.ex_date)
        if abs(ttm - c.ttm_dps) > 1e-9:
            bad_ttm += 1
        if abs(c.ttm_dps / c.price - c.div_yield) > 1e-12:
            bad_yield += 1
    check("B", "cap = 当时总股本×当日收盘（逐候选复算）", bad_cap == 0, f"异常={bad_cap}")
    check("B", "合格⇔cap>=500亿（双向）", bad_elig_cap == 0, f"异常={bad_elig_cap}")
    check("B", "entry/exit 日期规则", bad_date == 0, f"异常={bad_date}")
    check("B", "趋势只用<=entry数据", bad_trend == 0, f"异常={bad_trend}")
    check("B", "TTM 只用<=ex_date事件", bad_ttm == 0, f"异常={bad_ttm}")
    check("B", "股息率=TTM/买入价", bad_yield == 0, f"异常={bad_yield}")

    # ================= C. 交易与账户守恒 =================
    print("—— C. 交易机制与账户守恒（PIT 主口径）——", flush=True)
    tr = pd.read_csv(RESULTS / "trades.csv")
    nav = pd.read_csv(RESULTS / "nav.csv")
    base = summary["base"]
    init = float(base["init_cash"])

    def dt(x) -> date:
        return date.fromisoformat(str(x)[:10])

    check("C", "捕获次数=交易行数", int(base["captures"]) == len(tr),
          f"{base['captures']} vs {len(tr)}")
    check("C", "现金每日 >= 0", bool((nav["cash"] >= -1e-6).all()),
          f"min={nav['cash'].min():.2f}")
    bad = 0
    for r in tr.itertuples():
        entry, exit_ = dt(r.entry_date), dt(r.exit_date)
        if not (entry < dt(r.ex_date)):
            bad += 1
        if (exit_ - dt(r.ex_date)).days < rules.hold_days_after_ex:
            bad += 1
        if int(r.holding_days) != (exit_ - entry).days:
            bad += 1
    check("C", "逐笔 entry<ex & exit>=ex+30 & 持有天数", bad == 0, f"异常={bad}")

    bad_cost = bad_tax = bad_wd = bad_pnl = 0
    max_dcost = max_dtax = max_dwd = max_dpnl = 0.0
    for r in tr.itertuples():
        bc = float(buy_cost(cfg, float(r.buy_amount)).total)
        sc = float(sell_cost(cfg, float(r.sell_amount)).total)
        max_dcost = max(max_dcost, abs(bc - float(r.buy_cost)), abs(sc - float(r.sell_cost)))
        if abs(bc - float(r.buy_cost)) > TOL or abs(sc - float(r.sell_cost)) > TOL:
            bad_cost += 1
        tax = float(dividend_tax_for(int(r.holding_days), float(r.gross_dividend), rules))
        max_dtax = max(max_dtax, abs(tax - float(r.tax)))
        if abs(tax - float(r.tax)) > TOL:
            bad_tax += 1
        wd = max(float(r.gross_dividend) - tax, 0.0) * rules.withdraw_ratio
        max_dwd = max(max_dwd, abs(wd - float(r.withdrawn)))
        if abs(wd - float(r.withdrawn)) > TOL:
            bad_wd += 1
        pnl = (float(r.sell_amount) - float(r.sell_cost) + float(r.gross_dividend)
               - float(r.tax) - float(r.withdrawn) - float(r.buy_amount) - float(r.buy_cost))
        max_dpnl = max(max_dpnl, abs(pnl - float(r.net_pnl)))
        if abs(pnl - float(r.net_pnl)) > TOL:
            bad_pnl += 1
        if abs(float(r.price_pnl) + float(r.dividend_pnl) - float(r.net_pnl)) > TOL:
            bad_pnl += 1
    check("C", "买卖成本逐笔复算", bad_cost == 0, f"异常={bad_cost} maxΔ={max_dcost:.4f}")
    check("C", "红利税逐笔复算", bad_tax == 0, f"异常={bad_tax} maxΔ={max_dtax:.4f}")
    check("C", "30% 取现逐笔复算", bad_wd == 0, f"异常={bad_wd} maxΔ={max_dwd:.4f}")
    check("C", "净盈亏=价格+分红损益", bad_pnl == 0, f"异常={bad_pnl} maxΔ={max_dpnl:.4f}")
    open_mv = sum(float(p["market_value"]) for p in base["open_positions"])
    recon = init + float(tr["net_pnl"].sum()) + open_mv
    check("C", "期末权益=期初+Σ净盈亏+未平仓市值",
          abs(recon - float(base["final_equity"])) < 1.0,
          f"复算={recon:.2f} 报告={base['final_equity']:.2f}")
    check("C", "nav 末日 wealth=报告期末总财富",
          abs(float(nav["wealth"].iloc[-1]) - float(base["final_wealth"])) < 1.0,
          f"{nav['wealth'].iloc[-1]:.2f} vs {base['final_wealth']:.2f}")
    check("C", "nav 末日 equity=报告期末权益",
          abs(float(nav["equity"].iloc[-1]) - float(base["final_equity"])) < 1.0,
          f"{nav['equity'].iloc[-1]:.2f} vs {base['final_equity']:.2f}")
    check("C", "CAGR 与期末总财富一致",
          abs((float(base["final_wealth"]) / init) ** (1.0 / (end - start).days * 365.25)
              - 1.0 - float(base["cagr"])) < 1e-6,
          f"cagr={base['cagr']:.6f}")

    # ================= D. 可复现 =================
    print("—— D. 可复现（同输入同输出）——", flush=True)
    rep = summary["reproducibility"]
    h1 = E.snapshot_hashes(E.run_case(ds, cfg, dc, None, start, end, label="verify1",
                                      rules=rules))
    check("D", "重跑 trades 哈希=summary",
          h1["trades_sha256"] == rep["pit_full"]["trades_sha256"],
          h1["trades_sha256"][:16])
    check("D", "重跑 nav 哈希=summary",
          h1["nav_sha256"] == rep["pit_full"]["nav_sha256"], h1["nav_sha256"][:16])
    if not args.skip_rerun:
        h2 = E.snapshot_hashes(E.run_case(ds, cfg, dc, None, start, end, label="verify2",
                                          rules=rules))
        check("D", "连续两次重跑哈希一致", h1 == h2,
              f"{h1['trades_sha256'][:12]} / {h2['trades_sha256'][:12]}")

    # ================= E. 旧口径对照 =================
    print("—— E. 旧口径（当前市值筛池）复算 ——", flush=True)
    ds_legacy = E.load_legacy_dataset()
    lg = E.run_case(ds_legacy, cfg, dc, None, start, end, label="verify_legacy",
                    rules=rules)
    lgm = lg["metrics"]
    lrep = legacy_summary["base"]
    check("E", "旧口径 CAGR 复算一致",
          abs(lgm["cagr"] - lrep["cagr"]) < 1e-9,
          f"{lgm['cagr']:.6f} vs {lrep['cagr']:.6f}")
    check("E", "旧口径期末总财富复算一致",
          abs(lgm["final_wealth"] - lrep["final_wealth"]) < 1.0,
          f"{lgm['final_wealth']:.2f} vs {lrep['final_wealth']:.2f}")
    check("E", "旧口径哈希=summary",
          E.snapshot_hashes(lg) == rep["legacy_full"], "hash match")

    # ================= F. pool62（PIT） =================
    print("—— F. pool62（PIT 口径）——", flush=True)
    pool62 = [ln.strip() for ln in (E.DATA / "pool62.txt").read_text(
        encoding="utf-8").splitlines() if ln.strip()]
    ds62 = E.load_pit_dataset(symbols=pool62)
    check("F", "pool62 PIT 覆盖 >= 55", len(ds62.symbols) >= 55, f"n={len(ds62.symbols)}")
    r62 = E.run_case(ds62, cfg, dc, None, start, end, label="verify_pool62", rules=rules)
    tr62 = r62["sim"]["trades"]
    check("F", "pool62 捕获次数>0", len(tr62) > 0, f"n={len(tr62)}")
    bad62 = 0
    for r in tr62.itertuples():
        entry, exit_ = dt(r.entry_date), dt(r.exit_date)
        if not (entry < dt(r.ex_date)) or (exit_ - dt(r.ex_date)).days < rules.hold_days_after_ex:
            bad62 += 1
    check("F", "pool62 日期规则", bad62 == 0, f"异常={bad62}")
    check("F", "pool62 现金>=0", bool((r62["sim"]["nav"]["cash"] >= -1e-6).all()),
          f"min={r62['sim']['nav']['cash'].min():.2f}")
    check("F", "pool62 可复现", E.snapshot_hashes(r62) == rep["pit_pool62"], "hash match")

    # ================= G. 优化方向（条件卖出/选池） =================
    print("—— G. 优化方向（条件卖出/选池）——", flush=True)
    import run_optimizations as RO

    opt_path = RESULTS / "optimizations" / "summary.json"
    if opt_path.exists():
        opt = json.loads(opt_path.read_text(encoding="utf-8"))
        rec = opt.get("recommended")
        check("G", "optimizations 结果存在", rec is not None,
              f"rows={len(opt.get('exit_policies', [])) + len(opt.get('selection', [])) + len(opt.get('combos', [])) + 1}")
        if rec is not None:
            spec = RO.SELECTORS[rec["selector"]]
            pol = RO.EXIT_POLICIES[rec["exit_policy"]][0]
            r_opt = E.run_case(ds, cfg, dc, spec["overrides"], start, end,
                               label="verify_opt_recommended", rules=rules,
                               rank_mode=spec.get("rank_mode", "pure"),
                               score_cfg=(RO.SCORE_CFG
                                          if spec.get("rank_mode") == "score" else None),
                               exit_policy=pol)
            mo = r_opt["metrics"]
            check("G", "推荐组合 CAGR 复算一致",
                  abs(mo["cagr"] - float(rec["cagr"])) < 1e-9,
                  f"{mo['cagr']:.6f} vs {float(rec['cagr']):.6f}")
            check("G", "推荐组合期末总财富复算一致",
                  abs(mo["final_wealth"] - float(rec["final_wealth"])) < 1.0,
                  f"{mo['final_wealth']:.2f} vs {float(rec['final_wealth']):.2f}")
            tr_opt = r_opt["sim"]["trades"]
            nav_opt = r_opt["sim"]["nav"]
            check("G", "推荐组合现金>=0",
                  bool((nav_opt["cash"] >= -1e-6).all()),
                  f"min={nav_opt['cash'].min():.2f}")
            bad_o = 0
            bad_cost_o = bad_tax_o = bad_wd_o = 0
            for r in tr_opt.itertuples():
                entry, exit_ = dt(r.entry_date), dt(r.exit_date)
                if not (entry < dt(r.ex_date)):
                    bad_o += 1
                if (exit_ - dt(r.ex_date)).days < 0:
                    bad_o += 1
                if int(r.holding_days) != (exit_ - entry).days:
                    bad_o += 1
                if abs(float(buy_cost(cfg, float(r.buy_amount)).total)
                       - float(r.buy_cost)) > TOL:
                    bad_cost_o += 1
                if abs(float(sell_cost(cfg, float(r.sell_amount)).total)
                       - float(r.sell_cost)) > TOL:
                    bad_cost_o += 1
                if abs(float(dividend_tax_for(int(r.holding_days),
                                              float(r.gross_dividend), rules))
                       - float(r.tax)) > TOL:
                    bad_tax_o += 1
                wd = max(float(r.gross_dividend) - float(r.tax), 0.0) * rules.withdraw_ratio
                if abs(wd - float(r.withdrawn)) > TOL:
                    bad_wd_o += 1
            check("G", "推荐组合逐笔日期/持有天数", bad_o == 0, f"异常={bad_o}")
            check("G", "推荐组合成本复算", bad_cost_o == 0, f"异常={bad_cost_o}")
            check("G", "推荐组合红利税按实际持有天数复算（早卖=20%档）",
                  bad_tax_o == 0, f"异常={bad_tax_o}")
            check("G", "推荐组合取现复算", bad_wd_o == 0, f"异常={bad_wd_o}")
            early = tr_opt[tr_opt["holding_days"] <= 30] if len(tr_opt) else tr_opt
            check("G", "早卖(<=30天)税档=20%",
                  bool(len(early) == 0 or
                       (abs(early["tax"] / early["gross_dividend"]
                            - rules.tax_rate_short) < 1e-6).all()),
                  f"早卖笔数={len(early)}")
    else:
        check("G", "optimizations 结果存在", False, "未找到 results/optimizations/summary.json")

    # ================= 汇总 =================
    n_ok = sum(1 for c in checks if c["ok"])
    out = {
        "generated_at": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S%z"),
        "universe_mode": "point_in_time",
        "checks": checks, "n_checks": len(checks), "n_pass": n_ok,
        "n_fail": len(checks) - n_ok, "all_ok": n_ok == len(checks),
        "pit_cap_sources": {"pit_shares": pit_n, "fallback_estimated": fallback},
        "data_counts": {
            "spot_rows": int(len(spot)),
            "pit_symbols": int(len(ds.symbols)),
            "shares_history_records": int(len(sh_df)),
            "events": int(len(ev_df)),
            "delisted_included": int(len(delisted_syms & set(bars_df["symbol"]))),
        },
        "reproducibility": rep,
    }
    (RESULTS / "verification.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n验证结果：{n_ok}/{len(checks)} 通过"
          f"{'（全部通过）' if n_ok == len(checks) else '（存在失败，见上）'}")
    return 0 if n_ok == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
