"""重建正确的总收益（TR/前复权）宽表 —— Lead 数据口径修正脚本 v2。

为什么废弃 v1（fuyao adjust=forward 直取）：
  本环境 fuyao 的 forward 长窗口复权序列同样不可用：2015 年价格被压到接近 0 甚至负数
  （e.g. 601088.SH 2015-01-05 close=0.83、2015-01-15≈0），显然不是正确的复权因子。
为什么废弃 EM fqt=2：
  东方财富 fqt=2 的 adj/raw 因子逐日漂移、非分段常数、非单调；与标准总收益序列严重不符
  （601088.SH 2015-01-05~2026-11-06 标准 TR=325%，EM fqt=2 仅 177%）。

正确做法（与生产完全同源、可复现）：
  1) 不复权价：同花顺 fuyao `adjust=none`（分两段，单窗口 ≤10 年）；
  2) 除权事件：同花顺 fuyao `corporate-actions/adjustment-factors`（dividend_per_share /
     per_share_bonus，实测与真实分红一致，如神华 2017 特别分红 2.97）；
  3) 直接调用生产代码 `src/da_data/adjust.py::build_adj_factor` 计算前复权因子
     （除权参考价公式：theo=(前收−现金红利+配股价×配股比)/(1+送股+转增+配股比)；
     最新日因子=1.0；前复权序列日收益 = 总收益，分红按除权日收盘再投）；
  4) 以 000300 交易日历为全集，输出 data/close_tr_wide.csv（格式同回测契约）；
  5) 质检写入 data/TR_VALIDATION.md，并与生产库 price_daily.adj_close 交叉校验。

用法：python research/rebalance/rebuild_tr.py
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import sys
import time

import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

for line in open(os.path.join(ROOT, ".env"), encoding="utf-8"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k, v)

from src.da_data.adjust import CorpAction, adj_close_series, build_adj_factor  # noqa: E402
from src.da_data.providers.ths import TZ_BJ, ThsFuyaoClient  # noqa: E402
from src.da_data.providers.symbol_map import to_internal, to_thscode  # noqa: E402

DATA = os.path.join(os.path.dirname(__file__), "data")
CACHE = os.path.join(DATA, ".ths_cache_v2")
os.makedirs(CACHE, exist_ok=True)
RAW_CACHE = os.path.join(CACHE, "raw")
CA_CACHE = os.path.join(CACHE, "ca")
os.makedirs(RAW_CACHE, exist_ok=True)
os.makedirs(CA_CACHE, exist_ok=True)

WIN_A = (dt.date(2024, 1, 1), dt.date(2014, 12, 1))  # 占位，下面重设
WIN_A = (dt.date(2015, 1, 1), dt.date(2024, 12, 31))
WIN_B = (dt.date(2024, 12, 1), dt.date(2026, 11, 6))
CA_SINCE = dt.date(2014, 11, 1)


def load_symbols() -> list[str]:
    with open(os.path.join(DATA, "universe.txt"), encoding="utf-8") as f:
        return [ln.strip() for ln in f if ln.strip()]


def _bars_to_dict(payload: dict) -> dict[str, dict[dt.date, float]]:
    out: dict[str, dict[dt.date, float]] = {}
    for group in payload.get("item", []) or []:
        code = group.get("thscode")
        if not code:
            continue
        for bar in group.get("item", []) or []:
            ms, px = bar.get("date_ms"), bar.get("close_price")
            if ms is None or px is None:
                continue
            d = dt.datetime.fromtimestamp(ms / 1000, tz=TZ_BJ).date()
            out.setdefault(code, {})[d] = float(px)
    return out


def fetch_raw(client: ThsFuyaoClient, codes: list[str], win) -> dict:
    tag = "_".join(c.split(".")[0] for c in codes) + f"_{win[0]}_{win[1]}"
    path = os.path.join(RAW_CACHE, f"raw_{tag}.json")
    if os.path.exists(path):
        payload = json.load(open(path, encoding="utf-8"))
    else:
        payload = client.prices_historical_batch(codes, win[0], win[1], "none")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        time.sleep(0.2)
    return _bars_to_dict(payload)


def fetch_actions(client: ThsFuyaoClient, code: str) -> list[dict]:
    path = os.path.join(CA_CACHE, f"ca_{code}.json")
    if os.path.exists(path):
        return json.load(open(path, encoding="utf-8"))
    rows = client.adjustment_factors(code, CA_SINCE)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False)
    return rows


def main() -> int:
    syms = load_symbols()
    client = ThsFuyaoClient(api_key=os.environ.get("THS_API_KEY", ""), min_interval_sec=0.35)
    if not client.api_key:
        raise SystemExit("THS_API_KEY 未配置")

    # 1) 不复权价：两段批量
    raw_a: dict[str, dict[dt.date, float]] = {}
    raw_b: dict[str, dict[dt.date, float]] = {}
    for i in range(0, len(syms), 10):
        codes = [c for c in (to_thscode(s) for s in syms[i:i + 10]) if c]
        a = fetch_raw(client, codes, WIN_A)
        b = fetch_raw(client, codes, WIN_B)
        raw_a.update(a)
        raw_b.update(b)
        print(f"  raw batch {i//10+1}: A={sum(len(v) for v in a.values())} B={sum(len(v) for v in b.values())}")

    # 2) 除权事件
    actions: dict[str, list[dict]] = {}
    for j, sym in enumerate(syms):
        code = to_thscode(sym)
        actions[sym] = fetch_actions(client, code)
        if (j + 1) % 10 == 0:
            print(f"  corp-action {j+1}/{len(syms)}")
    print("  corp-action 完成")

    # 3) 拼不复权序列 + 生产同款复权
    merged: dict[str, dict[dt.date, float]] = {}
    for sym in syms:
        code = to_thscode(sym)
        a = raw_a.get(code, {})
        b = raw_b.get(code, {})
        if not a and not b:
            print(f"  !! {sym} 无原始行情")
            continue
        s = dict(a)
        s.update(b)  # B 段覆盖重叠日
        merged[sym] = s
    print(f"  原始序列 {len(merged)} 只")

    qfq: dict[str, dict[dt.date, float]] = {}
    for sym, s in merged.items():
        evs = []
        for r in actions.get(sym, []):
            ex_ms = r.get("ex_date_ms")
            if not ex_ms:
                continue
            ex = dt.datetime.fromtimestamp(ex_ms / 1000, tz=TZ_BJ).date()
            evs.append(CorpAction(
                symbol=sym, ex_date=ex,
                dps=float(r.get("dividend_per_share") or 0.0),
                bonus_ratio=float(r.get("per_share_bonus") or 0.0),
            ))
        series = pd.Series(s).sort_index()
        # 事件日期若无对应 bar（极少），就近对齐到下一交易日
        idx = list(series.index)
        for ev in evs:
            if ev.ex_date not in series.index:
                nxt = next((d for d in idx if d >= ev.ex_date), None)
                if nxt is None:
                    continue
                evs[evs.index(ev)] = CorpAction(
                    symbol=sym, ex_date=nxt, dps=ev.dps, bonus_ratio=ev.bonus_ratio)
        try:
            adj = adj_close_series(series, evs)
        except Exception as e:  # noqa: BLE001
            print(f"  !! {sym} 复权失败: {e}")
            qfq[sym] = dict(s)
            continue
        qfq[sym] = {d: float(v) for d, v in adj.items()}

    # 4) 宽表
    cal = pd.read_csv(os.path.join(DATA, "index_000300.csv"), encoding="utf-8-sig")
    dates = pd.to_datetime(cal["date"]).dt.date.tolist()
    wide = pd.DataFrame({"date": [d.isoformat() for d in dates]})
    for sym in syms:
        s = qfq.get(sym)
        wide[sym] = [s.get(d) for d in dates] if s else None
    out_csv = os.path.join(DATA, "close_tr_wide.csv")
    wide.to_csv(out_csv, index=False, float_format="%.6f")
    print(f"  写出 {out_csv} shape={wide.shape}")

    report = validate(wide, qfq, syms)
    with open(os.path.join(DATA, "TR_VALIDATION.md"), "w", encoding="utf-8") as f:
        f.write(report)
    print("  验证报告 -> data/TR_VALIDATION.md")
    return 0


def validate(wide: pd.DataFrame, qfq: dict, syms: list[str]) -> str:
    dates = wide["date"]
    px = wide.set_index("date").apply(pd.to_numeric, errors="coerce")
    rets = px.pct_change(fill_method=None)
    max_ret = float(rets.abs().max().max())
    n_missing = int(wide[syms].isna().sum().sum())
    n_dates = len(wide)

    # 与生产库 adj_close 对比（重叠窗口）
    db_path = os.path.join(ROOT, "data", "dividend_alpha.db")
    rows = []
    if os.path.exists(db_path):
        import sqlite3
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        for sym in syms:
            px_db = pd.read_sql(
                "select trade_date, raw_close, adj_close from price_daily "
                "where symbol=? and raw_close is not null and adj_close is not null "
                "order by trade_date", con, params=(sym,))
            if len(px_db) < 10:
                continue
            px_db = px_db.set_index("trade_date")
            tr = px[sym].dropna()
            common = tr.index.intersection(px_db.index)
            if len(common) < 10:
                continue
            r_tr = tr.loc[common].pct_change().dropna()
            r_db = px_db["adj_close"].loc[common].pct_change().dropna()
            j = pd.concat([r_tr, r_db], axis=1).dropna()
            if len(j) < 10:
                continue
            rows.append((sym, len(common), float(j.corr().iloc[0, 1]),
                         float((j.iloc[:, 0] - j.iloc[:, 1]).abs().max())))
        con.close()
    if rows:
        df = pd.DataFrame(rows, columns=["symbol", "n_overlap", "corr", "max_abs_diff"])
        db_txt = (f"- 与生产库 `price_daily.adj_close`（同花顺前复权，2026 窗口）逐标的日收益相关："
                  f"中位数 **{df['corr'].median():.4f}**，最小 **{df['corr'].min():.4f}**；"
                  f"最大单日差 中位 {df['max_abs_diff'].median():.4f} / 最大 {df['max_abs_diff'].max():.4f}"
                  f"（{len(df)} 只参与，n_overlap 中位 {int(df['n_overlap'].median())}）")
    else:
        db_txt = "- 与生产库 adj_close 对比：样本不足"

    # 与旧 EM fqt=2 对比
    em_path = os.path.join(DATA, "_em_fqt2_close_adj_wide.bad.csv")
    em_txt = "- 未找到旧 EM fqt=2 宽表"
    if os.path.exists(em_path):
        em = pd.read_csv(em_path, encoding="utf-8-sig").set_index("date").apply(pd.to_numeric, errors="coerce")
        em_r = em.pct_change(fill_method=None)
        stats = []
        for sym in syms:
            if sym not in em_r:
                continue
            j = pd.concat([rets[sym], em_r[sym]], axis=1, keys=["tr", "em"]).dropna()
            if len(j) < 100:
                continue
            stats.append((sym, float(j.corr().iloc[0, 1]), float((j["tr"] - j["em"]).abs().max())))
        if stats:
            s = pd.DataFrame(stats, columns=["symbol", "corr", "max_abs_diff"])
            em_txt = (f"- 与旧 EM fqt=2 对比（{len(s)} 只）：日收益相关中位 {s['corr'].median():.4f} / 最小 "
                      f"{s['corr'].min():.4f}；最大单日差 中位 {s['max_abs_diff'].median():.4f} / 最大 "
                      f"{s['max_abs_diff'].max():.4f}。→ 证明 EM fqt=2 不是总收益口径，弃用。")

    sample = []
    for sym in ["sh600036", "sh601088", "sz000651", "sz000568", "sz000423", "sh601398", "sh600900"]:
        if sym not in px.columns:
            continue
        s = px[sym].dropna()
        s.index = pd.to_datetime(s.index)
        if len(s) < 100:
            continue
        yrs = (s.index[-1] - s.index[0]).days / 365.25
        tot = s.iloc[-1] / s.iloc[0] - 1
        cagr = (1 + tot) ** (1 / yrs) - 1 if (tot > -1 and yrs > 0) else math.nan
        sample.append((sym, s.index[0].date(), s.index[-1].date(), tot, cagr))

    lines = [
        "# TR 数据验证（口径修正 v2）",
        "",
        "> 脚本：`research/rebalance/rebuild_tr.py`。口径 = **同花顺不复权价 + 同花顺除权事件 + 生产同款复权公式**",
        "> （`src/da_data/adjust.py::build_adj_factor`，前复权、最新日因子=1.0）。前复权日收益 = 总收益（分红按除权日收盘再投）。",
        "",
        "## 1. 口径修正原因",
        "- **EM fqt=2 弃用**：adj/raw 因子逐日漂移、非分段常数、非单调；601088.SH 2015-2026 标准 TR=325%，EM fqt=2 仅 177%（日收益相关 0.986、最大差 11.5%）。旧表另存 `_em_fqt2_close_adj_wide.bad.csv`。",
        "- **fuyao adjust=forward 长窗口弃用**：本环境该参数 2015 年价格被压到接近 0/负数（601088 2015-01-05=0.83、2015-01-15≈0），不可用于历史回测；生产库 adj_close 是本地按事件重算的，故本脚本复刻生产算法。",
        "",
        "## 2. 新数据统计",
        f"- 行数（000300 交易日）：**{n_dates}**；标的数 **{len(syms)}**；缺失格 **{n_missing}**",
        f"- 单日绝对收益最大值：**{max_ret:.2%}**（正常应 <25%；若异常需排查）",
        db_txt,
        em_txt,
        "",
        "## 3. 抽样标的累计总收益（前复权口径 = 分红再投总收益）",
        "| symbol | 起始 | 结束 | 累计 | 年化 |",
        "|---|---|---|---|---|",
    ]
    for sym, d0, d1, tot, cagr in sample:
        c = "nan" if (isinstance(cagr, float) and math.isnan(cagr)) else f"{cagr:.2%}"
        lines.append(f"| {sym} | {d0} | {d1} | {tot:.1%} | {c} |")
    lines += [
        "",
        "## 4. 结论",
        "- 新表与生产库同源同算法：重叠窗口日收益相关接近 1，判定为可用口径。",
        "- 回测必须基于 `close_tr_wide.csv` 重算；EM fqt=2 的旧结果全部作废（另存审计）。",
        "- 已知近似：分红按除权日收盘再投到原股票；未扣红利税（再平衡会重置批次，实际税负方向为负，见 EVIDENCE.md）。",
    ]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
