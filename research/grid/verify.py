#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dividend-alpha-v2 / research/grid —— 网格回测验证

断言（全部通过才可引用结果）：
  A. 数据：10 只标的日线/除权事件齐备；输入文件哈希与 summary 一致；价格为正、事件去重有序。
  B. 成交：成交日=交易日且当日有 bar；成交价=当日不复权收盘；金额=价×股数；100 股整数手；
     费用= cost.py 逐笔复算；卖出税=按 FIFO 批次实际持有天数重算；T+1（卖出批次买入日<卖出日）；
     trailing 触发条件（buy: price <= ref_before×(1-step)；sell: price >= ref_before×(1+step)）。
  C. 独立重放：用「初始建仓 + 逐笔成交 + 除权事件（分红/送股）+ cost.py 费用 + FIFO 红利税」
     独立重算每只标的期末现金/持仓/权益，与引擎结果逐只一致；期末财富=初始+价格损益+分红−费用−税。
  D. 可复现：同输入两次回测 nav/trades 哈希一致，且等于 summary 记录值。
  E. 超额分解恒等式：excess(vs core70_cash30) == grid_net（分红+价格−成本−税，含递延）。
  F. 基准：hold100 / core70_cash30 无网格成交；hold100 期末财富独立重放一致。

产出：results/verification.json
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

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import engine as E  # noqa: E402
from src.da_core.cost import buy_cost, sell_cost  # noqa: E402

RESULTS = HERE / "results"
TOL = 0.02


# ---------------------------------------------------------------------------
# 独立重放
# ---------------------------------------------------------------------------
class ReplayLot:
    __slots__ = ("shares", "date", "price", "dps")

    def __init__(self, shares, d, price):
        self.shares = float(shares)
        self.date = d
        self.price = float(price)
        self.dps = 0.0


def replay_symbol(sym: str, data, init: dict, trades: pd.DataFrame, cfg,
                  start: date, end: date) -> dict:
    """从初始建仓出发，按交易日（事件优先）重放，独立计算现金/持仓/费用/税/分红。"""
    init_date = date.fromisoformat(init["date"])
    p0 = float(init["price"])
    core_lots = [ReplayLot(init["core_shares"], init_date, p0)] if init["core_shares"] > 0 else []
    _iu = int(init["init_units"])
    _per_unit = (init["grid_shares"] / _iu) if _iu > 0 else 0.0
    grid_lots = [ReplayLot(_per_unit, init_date, p0) for _ in range(_iu)] if _iu > 0 else []
    cash = E.INIT_CASH / E.N_STOCKS
    # 初始建仓现金流
    core_amount = init["core_shares"] * p0
    core_fee = float(buy_cost(cfg, core_amount).total) if core_amount > 0 else 0.0
    grid_amount = init["grid_shares"] * p0
    grid_fee = float(buy_cost(cfg, grid_amount).total) if grid_amount > 0 else 0.0
    cash -= core_amount + core_fee + grid_amount + grid_fee
    fees_grid = grid_fee
    fees_core = core_fee
    div_core = 0.0
    div_grid = 0.0
    tax_total = 0.0
    realized_pnl = 0.0
    assert cash >= -1e-6, f"{sym} 初始现金为负"

    events = sorted(data.events.get(sym) or [], key=lambda e: e["ex_date"])
    ev_i = 0

    def apply_events_until(d: date) -> None:
        nonlocal ev_i, cash, div_core, div_grid
        while ev_i < len(events) and events[ev_i]["ex_date"] <= d:
            ev = events[ev_i]
            ev_i += 1
            if ev["ex_date"] <= init_date:
                continue     # 建仓前（含建仓当日）的除权事件不享有
            dps = float(ev["dps"])
            bonus = float(ev["bonus"])
            if dps > 0:
                for lot in core_lots:
                    g = dps * lot.shares
                    div_core += g
                    cash += g
                for lot in grid_lots:
                    g = dps * lot.shares
                    lot.dps += g
                    div_grid += g
                    cash += g
            if bonus > 0:
                for lot in core_lots + grid_lots:
                    lot.shares *= (1.0 + bonus)
                    lot.price /= (1.0 + bonus)

    tr = trades.sort_values(["date", "side"], kind="stable") if len(trades) else trades
    for r in (tr.itertuples() if len(tr) else []):
        d = date.fromisoformat(str(r.date)[:10])
        if d < start or d > end:
            continue
        apply_events_until(d)
        price = float(r.price)
        shares = float(r.shares)
        amount = price * shares
        assert abs(amount - float(r.amount)) < 1e-6, f"{sym} {d} 金额不符"
        if r.side == "BUY":
            fee = float(buy_cost(cfg, amount).total)
            assert abs(fee - float(r.fee)) < TOL, f"{sym} {d} 买入费用不符"
            cash -= amount + fee
            fees_grid += fee
            grid_lots.append(ReplayLot(shares, d, price))
        else:
            fee = float(sell_cost(cfg, amount).total)
            assert abs(fee - float(r.fee)) < TOL, f"{sym} {d} 卖出费用不符"
            remaining = shares
            tax = 0.0
            while remaining > 1e-9:
                lot = grid_lots[0]
                assert lot.date < d, f"{sym} {d} 违反 T+1（批次买入日 {lot.date}）"
                take = min(remaining, lot.shares)
                frac = take / lot.shares
                dps_part = lot.dps * frac
                hd = (d - lot.date).days
                tax += dps_part * E.tax_rate(hd)
                realized_pnl += (price - lot.price) * take
                lot.dps -= dps_part
                lot.shares -= take
                remaining -= take
                if lot.shares <= 1e-9:
                    grid_lots.pop(0)
            assert abs(tax - float(r.tax)) < TOL, f"{sym} {d} 卖出税不符 {tax} vs {r.tax}"
            cash += amount - fee - tax
            fees_grid += fee
            tax_total += tax
    # 末端事件（最后一笔交易之后、期末之前）
    apply_events_until(end)
    last_close = float(data.last_close(sym, end))
    core_sh = sum(l.shares for l in core_lots)
    grid_sh = sum(l.shares for l in grid_lots)
    unrealized_pnl = sum((last_close - l.price) * l.shares for l in core_lots + grid_lots)
    price_pnl_total = realized_pnl + unrealized_pnl
    deferred = sum(l.dps * E.tax_rate((end - l.date).days) for l in grid_lots)
    final_value = cash + (core_sh + grid_sh) * last_close
    return {
        "cash": cash, "core_shares": core_sh, "grid_shares": grid_sh,
        "fees_grid": fees_grid, "fees_core": fees_core, "tax": tax_total,
        "div_core": div_core, "div_grid": div_grid,
        "final_value": final_value, "deferred_tax": deferred,
        "last_close": last_close, "price_pnl_total": price_pnl_total,
    }


def main() -> int:
    cfg = E.load_configs()
    S = json.loads((RESULTS / "summary.json").read_text(encoding="utf-8"))
    checks: list[dict] = []

    def check(section: str, name: str, ok: bool, detail: str = "") -> None:
        checks.append({"section": section, "name": name, "ok": bool(ok),
                       "detail": str(detail)})
        print(f"  [{'PASS' if ok else 'FAIL'}] {section} · {name}  ({detail})", flush=True)

    print("—— A. 数据 ——", flush=True)
    data = E.load_data()
    check("A", "10 只标的数据齐备",
          all(len(data.bars.get(s) or {}) > 2000 for s in E.SYMBOLS),
          f"bars={ {s: len(data.bars.get(s) or {}) for s in E.SYMBOLS} }")
    check("A", "除权事件齐备（每只 >10 条）",
          all(len(data.events.get(s) or []) > 10 for s in E.SYMBOLS),
          f"events={ {s: len(data.events.get(s) or []) for s in E.SYMBOLS} }")
    bad_price = sum(1 for s in E.SYMBOLS for v in data.bars[s].values() if v <= 0)
    check("A", "价格均为正", bad_price == 0, f"非法={bad_price}")
    ev_bad = 0
    for s in E.SYMBOLS:
        ds = [e["ex_date"] for e in data.events[s]]
        if ds != sorted(set(ds)):
            ev_bad += 1
    check("A", "事件按日期去重有序", ev_bad == 0, f"异常={ev_bad}")
    inputs_ok = True
    for name, h in S.get("inputs_sha256", {}).items():
        f = {"bars_all.parquet": E.BARS_FILE, "events_all.parquet": E.EVENTS_FILE,
             "calendar.csv": E.CALENDAR_FILE}.get(name)
        if f is None or E.sha256_of(f) != h:
            inputs_ok = False
    check("A", "输入数据 SHA256 与 summary 一致", inputs_ok, "bars/events/calendar")

    print("—— B. 成交逐笔（主变体 trailing）——", flush=True)
    main_res = E.run_variant(data, E.Variant("core70_grid30_sym_trailing"), cfg,
                             E.START, E.END)
    tr = main_res["trades"]
    check("B", "主变体有网格成交", len(tr) > 0, f"trades={len(tr)}")
    bad = 0
    max_fee_d = max_tax_d = 0.0
    for r in tr.itertuples():
        d = date.fromisoformat(str(r.date)[:10])
        close = data.close_on(r.symbol, d)
        if close is None:
            bad += 1
            continue
        if abs(close - float(r.price)) > 1e-9:
            bad += 1
        amount = float(r.price) * float(r.shares)
        if abs(amount - float(r.amount)) > 1e-6:
            bad += 1
        if int(round(float(r.shares))) % 100 != 0:
            bad += 1
        fee = float((buy_cost(cfg, amount) if r.side == "BUY"
                     else sell_cost(cfg, amount)).total)
        max_fee_d = max(max_fee_d, abs(fee - float(r.fee)))
        if abs(fee - float(r.fee)) > TOL:
            bad += 1
        if r.side == "SELL":
            tax = sum(float(dp) * E.tax_rate(int(hd)) for dp, hd in r.tax_detail)
            max_tax_d = max(max_tax_d, abs(tax - float(r.tax)))
            if abs(tax - float(r.tax)) > TOL:
                bad += 1
            for bd in r.matched_buy_dates:
                if date.fromisoformat(bd) >= d:
                    bad += 1
            if float(r.price) < float(r.ref_before) * (1 + 0.02) - 1e-9:
                bad += 1
        else:
            if float(r.price) > float(r.ref_before) * (1 - 0.02) + 1e-9:
                bad += 1
    check("B", "成交日/成交价=当日 close/金额/整手", bad == 0, f"异常={bad}")
    check("B", "买入/卖出费用逐笔复算", max_fee_d <= TOL, f"maxΔ={max_fee_d:.6f}")
    check("B", "卖出红利税按 FIFO 实际持有天数复算", max_tax_d <= TOL,
          f"maxΔ={max_tax_d:.6f}")
    t1_bad = sum(1 for r in tr.itertuples() if r.side == "SELL"
                 for bd in r.matched_buy_dates
                 if date.fromisoformat(bd) >= date.fromisoformat(str(r.date)[:10]))
    check("B", "T+1（卖出批次买入日<卖出日）", t1_bad == 0, f"异常={t1_bad}")
    check("B", "成交笔数=metrics", int(S["full"]["core70_grid30_sym_trailing"]["grid_trades"])
          == len(tr), f"{len(tr)}")

    print("—— C. 独立重放（初始建仓+成交+事件+费用+税）——", flush=True)
    init = main_res["initial"]
    ok_all = True
    detail = []
    total_replay = 0.0
    fees_rep = tax_rep = 0.0
    reps = {}
    for s in E.SYMBOLS:
        rep = replay_symbol(s, data, init[s], tr[tr["symbol"] == s], cfg, E.START, E.END)
        reps[s] = rep
        book = main_res["books"][s]
        eng_val = book.value(book.last_close)
        d_val = abs(rep["final_value"] - eng_val)
        d_fee = abs(rep["fees_grid"] - (book.buy_fees + book.sell_fees))
        d_tax = abs(rep["tax"] - book.div_tax_grid)
        if d_val > 0.05 or d_fee > 0.05 or d_tax > 0.05:
            ok_all = False
            detail.append(f"{s}: Δval={d_val:.4f} Δfee={d_fee:.4f} Δtax={d_tax:.4f}")
        total_replay += rep["final_value"]
        fees_rep += rep["fees_grid"]
        tax_rep += rep["tax"]
    check("C", "逐只期末权益重放一致", ok_all, "; ".join(detail) or "10/10 一致")
    check("C", "组合期末权益重放一致",
          abs(total_replay - S["full"]["core70_grid30_sym_trailing"]["final_equity"]) < 0.05,
          f"重放={total_replay:.2f} 引擎={S['full']['core70_grid30_sym_trailing']['final_equity']:.2f}")
    check("C", "网格费用合计重放一致",
          abs(fees_rep - S["full"]["core70_grid30_sym_trailing"]["grid_costs"]) < 0.1,
          f"重放={fees_rep:.2f}")
    check("C", "红利税合计重放一致",
          abs(tax_rep - S["full"]["core70_grid30_sym_trailing"]["grid_tax_realized"]) < 0.1,
          f"重放={tax_rep:.2f}")
    # nav 末日 = Σ 各标的权益（引擎账本自洽）
    nav_last = float(main_res["nav"]["equity"].iloc[-1])
    books_sum = sum(b.value(b.last_close) for b in main_res["books"].values())
    check("C", "nav 末日=Σ持仓权益", abs(nav_last - books_sum) < 0.05,
          f"{nav_last:.2f} vs {books_sum:.2f}")
    # 期末财富守恒：期末 = 初始 + 价格损益 + 分红 − 费用 − 税（独立重放口径）
    lhs = total_replay - E.INIT_CASH
    rhs = sum(r["price_pnl_total"] + r["div_core"] + r["div_grid"]
              - r["fees_grid"] - r["fees_core"] - r["tax"] for r in reps.values())
    check("C", "期末=初始+价格损益+分红−费用−税", abs(lhs - rhs) < 0.1,
          f"LHS={lhs:.2f} RHS={rhs:.2f}")
    # 期末财富（扣递延税）与重放一致
    check("C", "期末财富(净值)−递延税=重放净值",
          abs((total_replay - sum(r["deferred_tax"] for r in reps.values()))
              - S["full"]["core70_grid30_sym_trailing"]["final_wealth_net"]) < 0.1,
          f"重放净值={total_replay:.2f}")

    print("—— D. 可复现 ——", flush=True)
    rep = S["reproducibility"]["core70_grid30_sym_trailing"]
    h1 = E.snapshot_hashes(main_res)
    check("D", "重跑 nav 哈希=summary", h1["nav_sha256"] == rep["nav_sha256"],
          h1["nav_sha256"][:16])
    check("D", "重跑 trades 哈希=summary", h1["trades_sha256"] == rep["trades_sha256"],
          h1["trades_sha256"][:16])
    main_res2 = E.run_variant(data, E.Variant("core70_grid30_sym_trailing"), cfg,
                              E.START, E.END)
    check("D", "连续两次重跑哈希一致", E.snapshot_hashes(main_res2) == h1, "hash match")

    print("—— E. 超额分解恒等式 ——", flush=True)
    dec = json.loads((RESULTS / "decomposition.json").read_text(encoding="utf-8"))
    bad_id = [k for k, v in dec["full"].items()
              if abs(v["identity_check_vs_core70"]) > 0.05]
    check("E", "excess(vs core70) == grid_net", not bad_id, f"异常={bad_id}")
    bad_id2 = [k for k, v in dec["full"].items()
               if abs(v["excess_vs_hold100"]
                      - (v["grid_net_pnl"] + v["cash_drag_30pct_vs_hold100"])) > 0.05]
    check("E", "excess(vs hold100) == grid_net + 现金拖累", not bad_id2, f"异常={bad_id2}")

    print("—— F. 基准 ——", flush=True)
    for name in ["hold100", "core70_cash30"]:
        res = E.run_variant(data, [v for v in E.variant_list() if v.name == name][0],
                            cfg, E.START, E.END)
        check("F", f"{name} 无网格成交", len(res["trades"]) == 0,
              f"trades={len(res['trades'])}")
        # hold100 独立重放
        rep_h = replay_symbol("sh600028", data, res["initial"]["sh600028"],
                              res["trades"], cfg, E.START, E.END)
        book = res["books"]["sh600028"]
        check("F", f"{name} sh600028 权益重放一致",
              abs(rep_h["final_value"] - book.value(book.last_close)) < 0.05,
              f"{rep_h['final_value']:.2f} vs {book.value(book.last_close):.2f}")

    print("—— G. 2021 子样本一致性 ——", flush=True)
    sub = E.run_variant(data, E.Variant("core70_grid30_sym_trailing"), cfg,
                        date(2021, 1, 4), E.END)
    sm = S["subsample_2021"]["core70_grid30_sym_trailing"]
    check("G", "2021 子样本期末权益复算一致",
          abs(sub["final_equity"] - sm["final_equity"]) < 1.0,
          f"{sub['final_equity']:.2f} vs {sm['final_equity']:.2f}")

    n_ok = sum(1 for c in checks if c["ok"])
    out = {"generated_at": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S%z"),
           "checks": checks, "n_checks": len(checks), "n_pass": n_ok,
           "n_fail": len(checks) - n_ok, "all_ok": n_ok == len(checks)}
    (RESULTS / "verification.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n验证结果：{n_ok}/{len(checks)} 通过"
          f"{'（全部通过）' if n_ok == len(checks) else '（存在失败，见上）'}")
    return 0 if n_ok == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
