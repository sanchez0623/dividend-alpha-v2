#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dividend-alpha-v2 / research/dividend_capture —— DC 分红捕获回测：point-in-time 主口径跑批

task-3：**用买入日当时总市值（当时总股本 × 当日不复权收盘）选池**，替代上一版
「当前市值≥500 亿」筛池（后者作为 `legacy_current_cap` 对照保留）。

用法：
    python research/dividend_capture/run_backtest.py            # 全量（PIT 主口径 + 旧口径对照 + 19 变体）
    python research/dividend_capture/run_backtest.py --quick    # 只跑 base/子样本/pool62（不跑变体）

产出（主口径 = point_in_time）：
    research/dividend_capture/results/{summary.json,trades.csv,nav.csv,yearly.csv,
                                       variants.csv,equity_curve.png}
    research/dividend_capture/results/legacy_current_cap/{summary.json,trades.csv,nav.csv,yearly.csv}
    web/dc_backtest.json（精简归档；含前端别名 + universe_mode + 旧口径对照）
"""
from __future__ import annotations

import argparse
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

import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import engine as E  # noqa: E402

RESULTS = E.RESULTS
LEGACY_DIR = RESULTS / "legacy_current_cap"
WEB_JSON = ROOT / "web" / "dc_backtest.json"

SUBSAMPLES = {"2018": date(2018, 1, 2), "2021": date(2021, 1, 4)}


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def first_trading_day(cal: list[date], d: date) -> date:
    for x in cal:
        if x >= d:
            return x
    raise ValueError(f"日历内无 >= {d} 的交易日")


def log_variant(res: dict) -> None:
    m = res["metrics"]
    log(f"  {m['label']:<30s} CAGR {m['cagr']:>8.2%}  total {m['total_return']:>9.2%}  "
        f"MDD {m['mdd']:>7.2%}  Sharpe {m['sharpe']:>5.2f}  captures {m['captures']:>3d}  "
        f"win {m['win_rate']:>5.1%}")


# ---------------------------------------------------------------------------
# 变体计划（一次只改一个参数）
# ---------------------------------------------------------------------------
def variant_plan() -> list[dict]:
    return [
        {"label": "yield_5%", "overrides": {"min_div_yield_ttm": 0.05}},
        {"label": "yield_7%", "overrides": {"min_div_yield_ttm": 0.07}},
        {"label": "cap_300亿", "overrides": {"min_market_cap": 3.0e10}},
        {"label": "cap_1000亿", "overrides": {"min_market_cap": 1.0e11}},
        {"label": "lead_3", "overrides": {"entry_lead_trading_days": 3}},
        {"label": "lead_7", "overrides": {"entry_lead_trading_days": 7}},
        {"label": "hold_0", "overrides": {"hold_days_after_ex": 0}},
        {"label": "hold_10", "overrides": {"hold_days_after_ex": 10}},
        {"label": "hold_20", "overrides": {"hold_days_after_ex": 20}},
        {"label": "hold_40", "overrides": {"hold_days_after_ex": 40}},
        {"label": "maxpos_2", "overrides": {"max_concurrent_positions": 2}},
        {"label": "maxpos_3", "overrides": {"max_concurrent_positions": 3}},
        {"label": "trend_none", "overrides": {"trend_enabled": False}},
        {"label": "trend_up", "overrides": {"trend_mode": "up"}},
        {"label": "trend_up_pull", "overrides": {"trend_mode": "up_pull"}},
        {"label": "tax_0%", "overrides": {"tax_rate_short": 0.0, "tax_rate_mid": 0.0,
                                          "tax_rate_long": 0.0}},
        {"label": "tax_10%", "overrides": {"tax_rate_short": 0.10, "tax_rate_mid": 0.10,
                                           "tax_rate_long": 0.10}},
        {"label": "tax_20%", "overrides": {"tax_rate_short": 0.20, "tax_rate_mid": 0.20,
                                           "tax_rate_long": 0.20}},
        {"label": "withdraw_0%", "overrides": {"withdraw_ratio": 0.0}},
    ]


def metric_row(m: dict) -> dict:
    return {
        "label": m["label"], "cagr": m["cagr"], "total_return": m["total_return"],
        "nav_total_return": m["nav_total_return"], "ann_vol": m["ann_vol"],
        "sharpe": m["sharpe"], "mdd": m["mdd"], "calmar": m["calmar"],
        "captures": m["captures"], "captures_per_year": m["captures_per_year"],
        "win_rate": m["win_rate"], "avg_trade_return": m["avg_trade_return"],
        "avg_holding_days": m["avg_holding_days"],
        "gross_dividend": m["gross_dividend"], "tax": m["tax"],
        "withdrawn": m["withdrawn"], "costs": m["costs"],
        "price_pnl": m["price_pnl"], "dividend_pnl": m["dividend_pnl"],
        "final_wealth": m["final_wealth"], "final_equity": m["final_equity"],
        "delayed_exits": m["delayed_exits"],
        "open_positions": len(m["open_positions"]), "skipped": m["skipped"],
    }


# ---------------------------------------------------------------------------
# 前端兼容别名
# ---------------------------------------------------------------------------
def _alias_metrics(d: dict) -> dict:
    out = dict(d)
    for alias, src in (("vol", "ann_vol"), ("max_drawdown", "mdd"),
                       ("avg_cycle_return", "avg_trade_return"), ("cost", "costs"),
                       ("final_nav", "final_equity"),
                       ("total_wealth_return", "total_return")):
        if src in out and alias not in out:
            out[alias] = out[src]
    return out


def _alias_yearly(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        row = dict(r)
        if "equity_end" in row and "nav" not in row:
            row["nav"] = row["equity_end"]
        out.append(row)
    return out


def _alias_variants(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        row = dict(r)
        if "label" in row and "name" not in row:
            row["name"] = row["label"]
        if "mdd" in row and "max_drawdown" not in row:
            row["max_drawdown"] = row["mdd"]
        out.append(row)
    return out


def _compact_metrics(m: dict, keys: list[str]) -> dict:
    return _alias_metrics({k: m.get(k) for k in keys if k in m})


# ---------------------------------------------------------------------------
# 绘图
# ---------------------------------------------------------------------------
def plot_equity(pit: dict, legacy: dict, out_png: Path) -> None:
    nav = pit["sim"]["nav"]
    dates = pd.to_datetime(nav["date"])
    wealth = nav["wealth"].astype(float).to_numpy()
    equity = nav["equity"].astype(float).to_numpy()
    init = float(pit["sim"]["init_cash"])
    w = np.concatenate([[init], wealth])
    peak = np.maximum.accumulate(w)
    dd = w / peak - 1.0

    fig, axes = plt.subplots(2, 1, figsize=(12, 8), sharex=True,
                             gridspec_kw={"height_ratios": [3, 1]})
    ax = axes[0]
    ax.plot(dates, wealth / init, label="PIT universe: total wealth (incl. withdrawals)",
            lw=1.6)
    ax.plot(dates, equity / init, label="PIT universe: account equity (excl. withdrawals)",
            lw=1.0, alpha=0.8)
    ln = legacy["sim"]["nav"]
    ld = pd.to_datetime(ln["date"])
    ax.plot(ld, ln["wealth"].astype(float) / init,
            label="Legacy universe (current-cap>=50bn): total wealth", lw=1.0, alpha=0.7,
            ls="--")
    b = pd.read_csv(E.CAL_SRC)
    b["date"] = pd.to_datetime(b["date"])
    b = b[(b["date"] >= dates.iloc[0]) & (b["date"] <= dates.iloc[-1])]
    if len(b) > 10:
        ax.plot(b["date"], b["close"] / b["close"].iloc[0],
                label="CSI300 (price index, no dividend)", lw=1.0, alpha=0.6)
    ax.axhline(1.0, color="gray", lw=0.6, ls="--")
    ax.set_ylabel("NAV (init = 1.0)")
    ax.set_title("Dividend Capture (DC) backtest — point-in-time market-cap universe, 2016-2026")
    ax.legend(loc="upper left", fontsize=8)
    ax.grid(alpha=0.3)

    ax2 = axes[1]
    ax2.fill_between(dates, dd[1:] * 100, 0, color="tab:red", alpha=0.5)
    ax2.set_ylabel("PIT drawdown (%)")
    ax2.grid(alpha=0.3)
    fig.tight_layout()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png, dpi=130)
    plt.close(fig)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="不跑 19 变体")
    args = ap.parse_args()
    RESULTS.mkdir(parents=True, exist_ok=True)
    LEGACY_DIR.mkdir(parents=True, exist_ok=True)

    cfg, dc = E.load_configs()
    base_rules = E.DcRules.from_strategy(dc)
    start, end = E.BACKTEST_START, E.BACKTEST_END
    pool62 = [ln.strip() for ln in (E.DATA / "pool62.txt").read_text(encoding="utf-8").splitlines()
              if ln.strip()]

    # ---------------- 旧口径（当前市值筛池）对照 ----------------
    log("加载旧口径数据集（当前市值≥500 亿筛池 + 当前股本反推，仅作对照）…")
    ds_legacy = E.load_legacy_dataset()
    log(f"  symbols={len(ds_legacy.symbols)}")
    legacy = {
        "full": E.run_case(ds_legacy, cfg, dc, None, start, end,
                           label="legacy_current_cap", rules=base_rules),
    }
    log_variant(legacy["full"])
    for tag, s in SUBSAMPLES.items():
        s0 = first_trading_day(ds_legacy.calendar, s)
        legacy[tag] = E.run_case(ds_legacy, cfg, dc, None, s0, end,
                                 label=f"legacy_{tag}", rules=base_rules)
        log_variant(legacy[tag])
    log("加载 pool62 数据集（旧口径估计市值）…")
    ds62 = E.load_pool62_dataset(pool62)
    legacy_p62 = E.run_case(ds62, cfg, dc, None, start, end, label="legacy_pool62",
                            rules=base_rules)
    log_variant(legacy_p62)
    s2018_62 = first_trading_day(ds62.calendar, SUBSAMPLES["2018"])
    legacy_p62_2018 = E.run_case(ds62, cfg, dc, None, s2018_62, end,
                                 label="legacy_pool62_2018", rules=base_rules)
    log_variant(legacy_p62_2018)

    # ---------------- 新主口径：point-in-time ----------------
    log("加载 point-in-time 数据集（全 A 候选 + 买入日当时总股本×收盘价）…")
    ds_pit = E.load_pit_dataset()
    log(f"  pit symbols={len(ds_pit.symbols)} shares_hist={len(ds_pit.shares_hist)}")
    pit = {
        "full": E.run_case(ds_pit, cfg, dc, None, start, end,
                           label="pit_full", rules=base_rules),
    }
    log_variant(pit["full"])
    for tag, s in SUBSAMPLES.items():
        s0 = first_trading_day(ds_pit.calendar, s)
        pit[tag] = E.run_case(ds_pit, cfg, dc, None, s0, end,
                              label=f"pit_{tag}", rules=base_rules)
        log_variant(pit[tag])

    log("pool62（point-in-time 口径，限定 62 只池）…")
    ds_pit62 = E.load_pit_dataset(symbols=pool62)
    log(f"  pit-pool62 coverage={len(ds_pit62.symbols)}/{len(pool62)}")
    pit_p62 = E.run_case(ds_pit62, cfg, dc, None, start, end, label="pit_pool62",
                         rules=base_rules)
    log_variant(pit_p62)
    s2018_p62 = first_trading_day(ds_pit62.calendar, SUBSAMPLES["2018"])
    pit_p62_2018 = E.run_case(ds_pit62, cfg, dc, None, s2018_p62, end,
                              label="pit_pool62_2018", rules=base_rules)
    log_variant(pit_p62_2018)

    # ---------------- 19 变体（PIT 主口径） ----------------
    variants: list[dict] = []
    variant_metrics: dict[str, dict] = {}
    if not args.quick:
        plan = variant_plan()
        log(f"敏感性变体 {len(plan)} 个（point-in-time 口径）…")
        for item in plan:
            r = E.run_case(ds_pit, cfg, dc, item["overrides"], start, end,
                           label=item["label"], rules=base_rules)
            variants.append(metric_row(r["metrics"]))
            variant_metrics[item["label"]] = r["metrics"]
            log_variant(r)

    # ---------------- 落盘 ----------------
    # 主口径
    pit["full"]["sim"]["trades"].to_csv(RESULTS / "trades.csv", index=False,
                                        encoding="utf-8")
    pit["full"]["sim"]["nav"].to_csv(RESULTS / "nav.csv", index=False, encoding="utf-8")
    pd.DataFrame(pit["full"]["metrics"]["yearly"]).to_csv(
        RESULTS / "yearly.csv", index=False, encoding="utf-8")
    if variants:
        pd.DataFrame(variants).to_csv(RESULTS / "variants.csv", index=False,
                                      encoding="utf-8")
    plot_equity(pit["full"], legacy["full"], RESULTS / "equity_curve.png")
    # 旧口径对照
    legacy["full"]["sim"]["trades"].to_csv(LEGACY_DIR / "trades.csv", index=False,
                                           encoding="utf-8")
    legacy["full"]["sim"]["nav"].to_csv(LEGACY_DIR / "nav.csv", index=False,
                                        encoding="utf-8")
    pd.DataFrame(legacy["full"]["metrics"]["yearly"]).to_csv(
        LEGACY_DIR / "yearly.csv", index=False, encoding="utf-8")
    (LEGACY_DIR / "summary.json").write_text(json.dumps(
        {"generated_at": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S%z"),
         "universe_mode": "current_cap",
         "base": legacy["full"]["metrics"],
         "subsamples": {k: legacy[k]["metrics"] for k in SUBSAMPLES},
         "pool62": {"full": legacy_p62["metrics"],
                    "sub_2018": legacy_p62_2018["metrics"]}},
        ensure_ascii=False, indent=2), encoding="utf-8")

    hashes = {
        "pit_full": E.snapshot_hashes(pit["full"]),
        "legacy_full": E.snapshot_hashes(legacy["full"]),
        "pit_pool62": E.snapshot_hashes(pit_p62),
    }

    # 选池数量随年份变化
    yearly_counts = []
    pit_uni_path = E.DATA / "pit_universe.csv"
    manifest_all = E.DATA / "manifest_all.json"
    if manifest_all.exists():
        mf = json.loads(manifest_all.read_text(encoding="utf-8"))
        yearly_counts = mf.get("yearly_cap500_count") or []

    summary = {
        "generated_at": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S%z"),
        "universe_mode": "point_in_time",
        "cap_rule": "cap_t = total_shares_at(entry_date) × raw_close(entry_date) >= 5e10",
        "dataset": {
            "spot_rows": int(len(pd.read_csv(E.SPOT_ALL))) if E.SPOT_ALL.exists() else None,
            "bars_symbols": int(len(ds_pit.bars)) if ds_pit.bars else None,
            "pit_symbols": int(len(ds_pit.symbols)),
            "pit_symbols_with_shares": int(len(ds_pit.shares_hist)),
            "events_symbols": int(len(ds_pit.events)),
            "calendar_start": ds_pit.calendar[0].isoformat(),
            "calendar_end": ds_pit.calendar[-1].isoformat(),
            "yearly_cap500_count": yearly_counts,
            "legacy_universe_symbols": len(ds_legacy.symbols),
            "data_manifest": "research/dividend_capture/data/manifest_all.json",
        },
        "base": pit["full"]["metrics"],
        "subsamples": {k: pit[k]["metrics"] for k in SUBSAMPLES},
        "pool62": {"full": pit_p62["metrics"], "sub_2018": pit_p62_2018["metrics"]},
        "legacy_current_cap": {
            "universe_mode": "current_cap",
            "base": legacy["full"]["metrics"],
            "subsamples": {k: legacy[k]["metrics"] for k in SUBSAMPLES},
            "pool62": {"full": legacy_p62["metrics"],
                       "sub_2018": legacy_p62_2018["metrics"]},
        },
        "variants": variants,
        "variant_metrics": variant_metrics,
        "reproducibility": hashes,
    }
    (RESULTS / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---------------- web 归档 JSON ----------------
    pm = pit["full"]["metrics"]
    lm = legacy["full"]["metrics"]
    spot_syms = set(pd.read_csv(E.SPOT_ALL)["symbol"].tolist())
    dl_set = (set(pd.read_csv(E.DELISTED_LIST)["symbol"])
              if E.DELISTED_LIST.exists() else set())
    delisted_included = int(len(set(ds_pit.symbols) & dl_set))
    all_a_bars_symbols = None
    if manifest_all.exists():
        all_a_bars_symbols = json.loads(manifest_all.read_text(
            encoding="utf-8")).get("bars_symbols")
    web = {
        "available": True,
        "data_class": "归档",
        "as_of": pm["end"],
        "generated_at": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S%z"),
        "strategy": "DC dividend capture",
        "universe_mode": "point_in_time",
        "universe": {
            "mode": "point_in_time",
            "cap_rule": "total_shares_at(entry_date) × raw_close(entry_date) >= 5e10",
            "candidate_pool": "all A shares from Eastmoney spot (5,557 rows; "
                              "ST/退 excluded → 5,091 attempted for THS bars)",
            "spot_rows": int(len(spot_syms)),
            "candidate_pool_bars_symbols": all_a_bars_symbols,
            "pit_symbols": int(len(ds_pit.symbols)),
            "symbols_with_share_history": int(len(ds_pit.shares_hist)),
            "delisted_symbols_included": delisted_included,
            "yearly_cap500_count": yearly_counts,
            "legacy_mode_symbols": int(len(ds_legacy.symbols)),
        },
        "start": pm["start"],
        "end": pm["end"],
        "params": pm["params"],
        "metrics": _alias_metrics({k: pm[k] for k in (
            "cagr", "total_return", "nav_total_return", "ann_vol", "sharpe", "mdd",
            "calmar", "captures", "captures_per_year", "win_rate", "avg_trade_return",
            "avg_holding_days", "gross_dividend", "tax", "withdrawn", "costs",
            "price_pnl", "dividend_pnl", "final_equity", "final_wealth",
            "init_cash", "years", "open_positions", "skipped", "candidate_stats",
            "cap_source_counts", "data_protection", "benchmark")}),
        "subsamples": {
            k: _compact_metrics(pit[k]["metrics"], [
                "start", "end", "cagr", "total_return", "nav_total_return", "ann_vol",
                "sharpe", "mdd", "calmar", "captures", "captures_per_year", "win_rate",
                "avg_trade_return", "tax", "costs", "withdrawn", "final_equity",
                "final_wealth", "gross_dividend", "price_pnl", "dividend_pnl"])
            for k in SUBSAMPLES
        },
        "pool62": _compact_metrics(pit_p62["metrics"], [
            "start", "end", "cagr", "total_return", "nav_total_return", "ann_vol",
            "sharpe", "mdd", "captures", "captures_per_year", "win_rate",
            "avg_trade_return", "tax", "costs", "withdrawn", "final_equity",
            "final_wealth"]),
        "yearly": _alias_yearly(pm["yearly"]),
        "variants": _alias_variants([{k: row[k] for k in (
            "label", "cagr", "total_return", "mdd", "sharpe", "captures", "win_rate",
            "tax", "costs", "withdrawn", "final_wealth")} for row in variants]),
        "legacy_current_cap": {
            "universe_mode": "current_cap",
            "note": "旧口径：按 2026-11-08 当前总市值≥500 亿筛 344 只池，历史市值用当前股本反推；"
                    "存在幸存者偏差，仅作对照。",
            "metrics": _compact_metrics(lm, [
                "cagr", "total_return", "nav_total_return", "ann_vol", "sharpe", "mdd",
                "calmar", "captures", "captures_per_year", "win_rate",
                "avg_trade_return", "gross_dividend", "tax", "withdrawn", "costs",
                "price_pnl", "dividend_pnl", "final_equity", "final_wealth"]),
            "subsamples": {
                k: _compact_metrics(legacy[k]["metrics"], [
                    "start", "end", "cagr", "total_return", "mdd", "sharpe",
                    "captures", "captures_per_year", "win_rate", "final_wealth"])
                for k in SUBSAMPLES
            },
            "pool62": _compact_metrics(legacy_p62["metrics"], [
                "cagr", "total_return", "mdd", "sharpe", "captures", "win_rate",
                "final_wealth"]),
        },
        "caveats": [
            "主口径（point_in_time）：候选为全 A 当前上市股票（约 5,563 只，排除 ST/退），"
            "买入日市值 = 当时总股本(akshare cninfo 历史总股本) × 当日不复权收盘，仅当 ≥500 亿才合格。",
            "退市股：同花顺不提供退市股行情/除权数据，改用东方财富行情 + F10 股本/分红尽力纳入；"
            "仍抓不到的退市股缺失，残余幸存者偏差在 REPORT 中量化披露。",
            "ST/退按当前状态排除（未做历史 ST 状态 point-in-time），残余偏差已披露。",
            "历史总股本缺失的标的回退‘当前股本/送股反推’估算（本次回退占比见 metrics.cap_source_counts）。",
            "不复权价成交，除权日以现金分红入账 + 送股扩股补偿；涨跌停无法成交不建模。",
            "红利税：除权日税前分红入现金，卖出时按实际持有自然日扣税（<=30天20%/31-365天10%/>365天0%），再提取净分红30%。",
            "持有到除权日+30自然日，单标的持仓约 37-45 天；『一年10次』在单标的+30天持有约束下实际约 4-6 次/年。",
            "回测≠未来：规则冻结自用户口径，未做参数拟合；历史结果不代表未来收益。",
            "数据防护：疑似数据错误 bar 命中买入/计划卖出日则跳过该笔；停牌复牌跳变按真实价格处理。",
            "旧口径（当前市值筛池）保留为对照字段 legacy_current_cap，不与主口径混用。",
        ],
        "disclaimer": (
            "本回测为历史模拟，基于不复权价与真实除权事件、按用户冻结规则（股息率TTM>=6%、"
            "买入日当时总市值>=500亿、非下跌趋势、除权前5个交易日买入、除权后30天卖出、单标的持有、"
            "红利税按持有天数、净分红取现30%）计算，未做参数优化。选池采用 point-in-time 市值，"
            "但仍存在退市股/历史 ST 状态等残余偏差。历史表现不代表未来收益，不构成投资建议。"
        ),
    }
    WEB_JSON.parent.mkdir(parents=True, exist_ok=True)
    WEB_JSON.write_text(json.dumps(web, ensure_ascii=False, indent=2), encoding="utf-8")

    log("—— 关键结果（PIT 主口径）——")
    for name, r in [("full", pit["full"]), ("sub_2018", pit["2018"]),
                    ("sub_2021", pit["2021"]), ("pool62", pit_p62)]:
        log_variant(r)
    log("—— 旧口径对照 ——")
    for name, r in [("full", legacy["full"]), ("sub_2018", legacy["2018"]),
                    ("sub_2021", legacy["2021"]), ("pool62", legacy_p62)]:
        log_variant(r)
    log(f"产出：{RESULTS} 与 {WEB_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
