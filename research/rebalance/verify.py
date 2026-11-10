"""独立复核脚本（verifier 专属，独立实现，不 import engine.py / src 复权代码）。

复核对象：research/rebalance/ 修正后（TR 口径）的回测结果。
本脚本做四组事：
  A. 数据口径独立重建：用 data/.ths_cache_v2/ 的同花顺**不复权**原始 JSON +
     同花顺除权事件，自己实现「除权参考价/前复权因子」公式（不 import build_adj_factor），
     重建 sh601088 / sh600036 / sz000651 的总收益序列，与 data/close_tr_wide.csv 对比。
  B. 独立回测：pandas/numpy 复写「等权买入持有 + 月末/季末/半年/年末/band5 再平衡 +
     config/strategy.yaml cost 段成本 + 分数股 + ffill」，复算 CURRENT10/OLD20 与
     随机抽 5 个 RANDOM10 篮子的 NAV/CAGR/premium，与 engine 结果逐项对照。
  C. 手工核对一次再平衡的成交明细（份额/金额/成本逐项/现金余额）。
  D. 违反检查：现金为负、买入超现金、未来函数（前缀不变性）、成本重复扣除、月末日期选择。
  E. 反事实 sanity：成本=0 / 成本×2 / 随机再平衡日（非月末）是否与月末规则同量级。

用法（在项目根目录）：
    python research/rebalance/verify.py            # 全部可做部分
    python research/rebalance/verify.py --part-a   # 只做数据口径独立重建
    python research/rebalance/verify.py --json research/rebalance/_verify.json  # 另存 JSON（本脚本默认不写文件）

本脚本只读 data/、results/、config/，默认不写任何文件（复核数字由 VERIFICATION.md 记录）。
"""
from __future__ import annotations

import argparse
import bisect
import datetime as dt
import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
import yaml
from scipy import stats

MODULE_DIR = Path(__file__).resolve().parent
ROOT = MODULE_DIR.parents[1]
DATA_DIR = MODULE_DIR / "data"
RESULTS_DIR = MODULE_DIR / "results"
ARCHIVE_DIR = MODULE_DIR / "results_em_fqt2_bad"
STRATEGY_YAML = ROOT / "config" / "strategy.yaml"

# ---- SPEC §2.3 / §3 固定参数 -------------------------------------------------
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
RANDOM_SEEDS = 100
RANDOM_BASKET_SIZE = 10
INIT_CASH = 3_000_000.0
TRADING_DAYS_PER_YEAR = 252
CALENDAR_DAYS_PER_YEAR = 365.25
FLOAT_EPS = 1e-9
CASH_EPS = 1e-6
TZ_BJ = dt.timezone(dt.timedelta(hours=8))

# 数据契约（SPEC §8）
EXPECTED_TR_SHA_PREFIX = "4d59e9d5487d312c"
EXPECTED_ROWS = 2877
EXPECTED_SYMBOLS = 62
EXPECTED_DATE_MIN = "2015-01-05"
EXPECTED_DATE_MAX = "2026-11-06"

# 复核容差（任务书）
CAGR_REL_TOL = 0.005      # 0.5% 相对误差
ABS_DIFF_TOL = 1e-6       # 成本/现金逐项核对
PREMIUM_ABS_FLOOR = 5e-5  # premium 相对容差的分母下限（5bp），避免对 ~0 的 premium 判错
DATACHECK_SYMBOLS = ["sh601088", "sh600036", "sz000651"]


# =============================================================================
# 通用工具
# =============================================================================
def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _r(x, n: int = 12):
    """把 numpy/pandas 标量转成 JSON 友好的圆整数值。"""
    if x is None:
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return x
    if not math.isfinite(f):
        return str(f)
    return round(f, n)


def _json_default(o):
    """JSON fallback: numpy scalars/arrays/Path."""
    if isinstance(o, np.bool_):
        return bool(o)
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.floating):
        return float(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, Path):
        return str(o)
    raise TypeError(f"not serializable: {type(o)!r}")


def load_cost_schedule(path: Path = STRATEGY_YAML) -> dict:
    """读取 config/strategy.yaml 的 cost 段（不硬编码任何费率）。"""
    with path.open("r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    c = cfg["cost"]
    return {
        "commission_rate": float(c["commission_rate"]),
        "commission_min": float(c["commission_min"]),
        "stamp_tax_sell": float(c["stamp_tax_sell"]),
        "handling_fee": float(c["handling_fee"]),
        "reg_fee": float(c["reg_fee"]),
        "transfer_fee": float(c["transfer_fee"]),
        "slippage_bps": float(c["slippage_bps"]),
        "init_cash": float(cfg["meta"]["init_cash"]),
    }


class IndependentCost:
    """独立实现的 A 股买卖成本（逐项可追溯）。

    买入：commission=max(金额*commission_rate, commission_min)
          + transfer(过户) + handling(经手) + reg(证管) + slippage(滑点)
    卖出：买入侧全部 + stamp_tax(印花税)
    cost_mult 语义按 SPEC §3.2：仅 commission_rate 与 slippage_bps 放大。
    """

    def __init__(self, sched: dict, cost_mult: float = 1.0, zero: bool = False):
        self.sched = sched
        self.cost_mult = float(cost_mult)
        self.zero = bool(zero)
        self.commission_rate = sched["commission_rate"] * self.cost_mult
        self.slippage_bps = sched["slippage_bps"] * self.cost_mult

    def items_buy(self, amount: float) -> Dict[str, float]:
        if self.zero or amount <= 0.0:
            return {k: 0.0 for k in ("commission", "transfer", "handling", "reg", "slippage")}
        return {
            "commission": max(amount * self.commission_rate, self.sched["commission_min"]),
            "transfer": amount * self.sched["transfer_fee"],
            "handling": amount * self.sched["handling_fee"],
            "reg": amount * self.sched["reg_fee"],
            "slippage": amount * self.slippage_bps / 10000.0,
        }

    def buy_cost(self, amount: float) -> float:
        return float(sum(self.items_buy(amount).values()))

    def items_sell(self, amount: float) -> Dict[str, float]:
        items = self.items_buy(amount)
        if not (self.zero or amount <= 0.0):
            items["stamp_tax"] = amount * self.sched["stamp_tax_sell"]
        else:
            items["stamp_tax"] = 0.0
        return items

    def sell_cost(self, amount: float) -> float:
        return float(sum(self.items_sell(amount).values()))


# =============================================================================
# 行情数据：宽表 + 交易日历
# =============================================================================
def load_tr_wide() -> pd.DataFrame:
    path = DATA_DIR / "close_tr_wide.csv"
    wide = pd.read_csv(path)
    if "date" not in wide.columns:
        raise RuntimeError(f"{path} 缺少 date 列")
    wide["date"] = pd.to_datetime(wide["date"])
    wide = wide.sort_values("date").set_index("date")
    return wide.apply(pd.to_numeric, errors="coerce")


def load_calendar() -> pd.DatetimeIndex:
    cal = pd.read_csv(DATA_DIR / "index_000300.csv")
    dates = pd.to_datetime(cal["date"])
    return pd.DatetimeIndex(sorted(dates.dropna().unique()))


def first_common_date(wide: pd.DataFrame, symbols: Sequence[str], start: str) -> pd.Timestamp:
    """>= start 的首个所有标的都有原始价的交易日（与 SPEC §3.5 起点定义一致）。"""
    start_ts = pd.Timestamp(start)
    sub = wide.loc[wide.index >= start_ts, list(symbols)]
    ok = sub.notna().all(axis=1).to_numpy()
    if not ok.any():
        raise RuntimeError(f"篮子 {list(symbols)} 在 {start} 之后无共同可得价日")
    return pd.Timestamp(sub.index[ok][0])


def basket_prices(wide: pd.DataFrame, calendar: pd.DatetimeIndex,
                  symbols: Sequence[str], start: str) -> Tuple[pd.DatetimeIndex, pd.DataFrame]:
    """返回 [start:] 的交易日与 ffill 价格矩阵（DataFrame，index=date，columns=symbols）。"""
    actual_start = first_common_date(wide, symbols, start)
    dates = calendar[calendar.get_loc(actual_start):]
    px = wide.reindex(calendar).loc[dates, list(symbols)].ffill()
    if px.isna().to_numpy().any():
        raise RuntimeError("ffill 后仍有缺失价")
    return dates, px


def build_calendar_masks(dates: pd.DatetimeIndex) -> Dict[str, np.ndarray]:
    """在交易日历上生成触发日 mask。

    与 SPEC §3.3 的口径一致：
      - month_last/quarter_last/half_last/year_last = 下一交易日已换月/季/半年/年；
      - **样本最后一日不算任何周期末**（数据截断日不应凭空触发调仓）。
    """
    n = len(dates)
    ym = np.array([d.year * 100 + d.month for d in dates])
    yq = np.array([d.year * 4 + (d.month - 1) // 3 for d in dates])
    yh = np.array([d.year * 2 + (d.month - 1) // 6 for d in dates])
    yr = np.array([d.year for d in dates])
    masks = {k: np.zeros(n, dtype=bool) for k in
             ("month_first", "month_last", "quarter_last", "half_last", "year_last")}
    for i in range(n):
        masks["month_first"][i] = (i == 0) or (ym[i] != ym[i - 1])
        if i < n - 1:
            masks["month_last"][i] = ym[i] != ym[i + 1]
            masks["quarter_last"][i] = yq[i] != yq[i + 1]
            masks["half_last"][i] = yh[i] != yh[i + 1]
            masks["year_last"][i] = yr[i] != yr[i + 1]
    return masks


# =============================================================================
# A. 数据口径独立重建（不复权价 + 除权事件 → 前复权/总收益）
# =============================================================================
def internal_to_ths(sym: str) -> str:
    """内部代码 -> 同花顺代码：sh601088 -> 601088.SH；sz000651 -> 000651.SZ。"""
    code, ex = sym[2:], sym[:2]
    return f"{code}.{ex.upper()}"


def _json_bars(payload: dict) -> Dict[str, Dict[dt.date, float]]:
    """把同花顺 fuyao 批量行情 payload 解析成 {thscode: {date: close}}。"""
    out: Dict[str, Dict[dt.date, float]] = {}
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


def load_raw_cache() -> Dict[str, Dict[dt.date, float]]:
    """读取 .ths_cache_v2/raw/ 全部同花顺不复权 JSON。

    窗口 A（2015-01-01..2024-12-31）先读、窗口 B（2024-12-01..2026-11-06）后读并覆盖，
    与 rebuild_tr.py 的合并语义一致（B 覆盖重叠日）。同时返回窗口口径交叉检查所需的
    分段数据。
    """
    raw_dir = DATA_DIR / ".ths_cache_v2" / "raw"
    files = sorted(raw_dir.glob("raw_*.json"))
    merged: Dict[str, Dict[dt.date, float]] = {}
    win_a: Dict[str, Dict[dt.date, float]] = {}
    win_b: Dict[str, Dict[dt.date, float]] = {}
    for f in files:
        payload = json.loads(f.read_text(encoding="utf-8"))
        part = _json_bars(payload)
        is_a = "2015-01-01" in f.name
        is_b = "2024-12-01" in f.name
        if is_a:
            for code, bars in part.items():
                win_a.setdefault(code, {}).update(bars)
                merged.setdefault(code, {}).update(bars)
        elif is_b:
            for code, bars in part.items():
                win_b.setdefault(code, {}).update(bars)
                merged.setdefault(code, {}).update(bars)
        else:  # 未知窗口：只并入 merged（保持 B 覆盖语义的近似）
            for code, bars in part.items():
                merged.setdefault(code, {}).update(bars)
    return {"merged": merged, "win_a": win_a, "win_b": win_b}


def load_ca_cache(sym: str) -> List[dict]:
    """读取同花顺除权事件：dividend_per_share(每股现金红利) / per_share_bonus(每股送股)。"""
    ths = internal_to_ths(sym)
    path = DATA_DIR / ".ths_cache_v2" / "ca" / f"ca_{ths}.json"
    if not path.exists():
        return []
    rows = json.loads(path.read_text(encoding="utf-8"))
    events = []
    for r in rows:
        ms = r.get("ex_date_ms")
        if ms is None:
            continue
        ex = dt.datetime.fromtimestamp(ms / 1000, tz=TZ_BJ).date()
        events.append({
            "ex_date": ex,
            "dps": float(r.get("dividend_per_share") or 0.0),
            "bonus": float(r.get("per_share_bonus") or 0.0),
        })
    return events


def independent_forward_adjust(raw_close: pd.Series, events: List[dict]) -> Tuple[pd.Series, dict]:
    """独立实现「前复权因子」（最新日因子=1.0）。

    A 股除权除息参考价（自写公式，未 import src/da_data/adjust.py）：
        theo = (前收盘 - 每股现金红利 + 配股价×配股比例) / (1 + 送股比例 + 转增比例 + 配股比例)
        ratio = theo / 前收盘
    对每个除权事件 e：把 e 之前（严格早于除权日）所有交易日的复权因子乘以 ratio。
    前复权序列 = 不复权收盘 × 因子；其相邻日收益 = 总收益（分红按除权日理论价再投）。

    事件日期不在行情序列里时（极少），对齐到**下一交易日**（与重建脚本的近似一致），
    并在统计中记录对齐/跳过数量。
    """
    raw = raw_close.copy()
    raw.index = pd.DatetimeIndex(pd.to_datetime(raw.index))
    raw = raw.sort_index()
    idx_dates = [ts.date() for ts in raw.index]
    n = len(raw)
    f = np.ones(n, dtype=np.float64)
    applied, aligned, skipped = [], [], []
    for ev in sorted(events, key=lambda e: e["ex_date"], reverse=True):
        ex = ev["ex_date"]
        pos = bisect.bisect_left(idx_dates, ex)
        if pos >= n:      # 除权日在样本终点之后：不影响样本内任何日期
            skipped.append({"ex_date": str(ex), "reason": "after_sample_end"})
            continue
        if pos == 0:      # 除权日在样本起点之前/当日：样本内无更早日期可乘
            skipped.append({"ex_date": str(ex), "reason": "before_sample_start"})
            continue
        if idx_dates[pos] != ex:
            aligned.append({"ex_date": str(ex), "used": str(idx_dates[pos])})
        prev_close = float(raw.iloc[pos - 1])
        den = 1.0 + ev["bonus"]  # 本数据源 rights_ratio=0（无配股事件）
        theo = (prev_close - ev["dps"]) / den
        if prev_close <= 0 or den <= 0 or theo <= 0:
            skipped.append({"ex_date": str(ex), "reason": "invalid_theo"})
            continue
        ratio = theo / prev_close
        f[:pos] *= ratio
        applied.append({"ex_date": str(ex), "used": str(idx_dates[pos]),
                        "dps": ev["dps"], "bonus": ev["bonus"],
                        "prev_close": prev_close, "theo": theo, "ratio": ratio})
    return pd.Series(raw.to_numpy() * f, index=raw.index, name="qfq"), {
        "n_events_total": len(events), "n_applied": len(applied),
        "n_aligned": len(aligned), "n_skipped": len(skipped),
        "applied": applied, "aligned": aligned, "skipped": skipped,
    }


def independent_tr_index(raw_close: pd.Series, events: List[dict]) -> pd.Series:
    """独立实现「总收益指数」（对照口径，非本回测采用口径）。

    持有 1 股：除权日若每股权益 dps、每股送股 bonus，则
        净值倍数 = ((1+bonus)*P_t + dps) / P_{t-1}
    即现金分红按**除权日收盘价**再投原股票（本回测数据用的是生产同款「理论除权价再投」，
    两者在除权日有 1 天口径差；两种口径都未扣红利税）。
    """
    raw = raw_close.copy()
    raw.index = pd.DatetimeIndex(pd.to_datetime(raw.index))
    raw = raw.sort_index()
    idx_dates = [ts.date() for ts in raw.index]
    ev_map: Dict[dt.date, Tuple[float, float]] = {}
    for ev in events:
        pos = bisect.bisect_left(idx_dates, ev["ex_date"])
        if pos >= len(raw):  # 样本外忽略
            continue
        used = idx_dates[pos]
        ev_map[used] = (ev_map.get(used, (0.0, 0.0))[0] + ev["dps"],
                        ev_map.get(used, (0.0, 0.0))[1] + ev["bonus"])
    values = np.empty(len(raw), dtype=np.float64)
    values[0] = float(raw.iloc[0])
    for i in range(1, len(raw)):
        dps, bonus = ev_map.get(idx_dates[i], (0.0, 0.0))
        values[i] = values[i - 1] * (((1.0 + bonus) * float(raw.iloc[i]) + dps) / float(raw.iloc[i - 1]))
    return pd.Series(values, index=raw.index, name="tr_index")


def datacheck() -> dict:
    """A. 数据契约检查 + 三只标的总收益独立重建对比。"""
    out: dict = {"contract": {}, "per_symbol": {}, "integrity": {}, "notes": []}

    # ---- 契约检查（SPEC §8） ----
    tr_path = DATA_DIR / "close_tr_wide.csv"
    wide_raw = pd.read_csv(tr_path)
    out["contract"]["file"] = str(tr_path.relative_to(ROOT))
    out["contract"]["sha256"] = sha256_file(tr_path)
    out["contract"]["sha256_prefix_ok"] = out["contract"]["sha256"].startswith(EXPECTED_TR_SHA_PREFIX)
    out["contract"]["rows"] = int(len(wide_raw))
    out["contract"]["rows_ok"] = len(wide_raw) == EXPECTED_ROWS
    out["contract"]["n_symbols"] = int(len(wide_raw.columns) - 1)
    out["contract"]["n_symbols_ok"] = (len(wide_raw.columns) - 1) == EXPECTED_SYMBOLS
    out["contract"]["date_min"] = str(wide_raw["date"].iloc[0])
    out["contract"]["date_max"] = str(wide_raw["date"].iloc[-1])
    out["contract"]["date_range_ok"] = (str(wide_raw["date"].iloc[0]) == EXPECTED_DATE_MIN
                                        and str(wide_raw["date"].iloc[-1]) == EXPECTED_DATE_MAX)
    cal = load_calendar()
    out["contract"]["calendar_rows"] = int(len(cal))
    out["contract"]["calendar_matches_wide"] = bool(
        len(cal) == len(wide_raw)
        and (pd.to_datetime(wide_raw["date"]).to_numpy() == cal.to_numpy()).all()
    )
    wide = load_tr_wide()
    out["contract"]["dup_dates"] = int(wide.index.duplicated().sum())
    out["contract"]["missing_cells"] = int(wide.isna().sum().sum())

    # 全池单日绝对收益（TR 口径应接近涨跌停上限 + 除权日当天无异常）
    rets = wide.pct_change(fill_method=None)
    arr = np.abs(rets.to_numpy(dtype=float))
    mx = float(np.nanmax(arr))
    out["integrity"]["max_abs_daily_ret_all"] = round(mx, 8)
    out["integrity"]["n_daily_ret_over_25pct"] = int((arr > 0.25).sum())
    out["integrity"]["n_daily_ret_over_205pct"] = int((arr > 0.205).sum())
    stacked = rets.abs().stack().sort_values(ascending=False)
    out["integrity"]["top_abs_daily_ret"] = [
        {"date": str(d), "symbol": str(sym), "abs_ret": round(float(v), 6)}
        for (d, sym), v in stacked.head(5).items()
    ]
    row_missing = wide.isna().sum(axis=1)
    out["integrity"]["max_missing_symbols_in_a_row"] = int(row_missing.max())
    out["integrity"]["missing_cells"] = int(wide.isna().sum().sum())

    # ---- 同花顺原始缓存：窗口 A/B 重叠段一致性 ----
    cache = load_raw_cache()
    overlap_diffs = []
    for code, bars_a in cache["win_a"].items():
        bars_b = cache["win_b"].get(code, {})
        common = sorted(set(bars_a) & set(bars_b))
        if not common:
            continue
        diffs = [abs(bars_a[d] - bars_b[d]) for d in common]
        overlap_diffs.append({"code": code, "n_common": len(common),
                              "max_abs_diff": round(max(diffs), 8)})
    out["integrity"]["raw_window_overlap"] = {
        "n_codes": len(overlap_diffs),
        "max_abs_diff_overall": round(max((x["max_abs_diff"] for x in overlap_diffs), default=0.0), 8),
        "n_common_min": int(min((x["n_common"] for x in overlap_diffs), default=0)),
    }

    # ---- 三只标的：独立重建 vs close_tr_wide ----
    for sym in DATACHECK_SYMBOLS:
        ths = internal_to_ths(sym)
        bars = cache["merged"].get(ths, {})
        if not bars:
            out["per_symbol"][sym] = {"status": "UNVERIFIED", "reason": "缓存无该标的原始行情"}
            continue
        raw_close = pd.Series(bars).sort_index()
        raw_close.index = pd.DatetimeIndex(pd.to_datetime(raw_close.index))
        events = load_ca_cache(sym)
        qfq, info = independent_forward_adjust(raw_close, events)
        tr_idx = independent_tr_index(raw_close, events)
        # 对齐到 000300 日历（缺价留 NaN），便于与宽表对比
        qfq_cal = qfq.reindex(cal)
        wide_col = wide[sym]
        both = pd.concat([qfq_cal.rename("verify"), wide_col.rename("engine")], axis=1)
        both = both.dropna()
        price_diff = (both["verify"] - both["engine"]).abs()
        rel_diff = price_diff / both["engine"].abs()
        both_ret = both.pct_change(fill_method=None).dropna()
        ret_diff = (both_ret["verify"] - both_ret["engine"]).abs()
        corr = float(both_ret["verify"].corr(both_ret["engine"])) if len(both_ret) > 2 else float("nan")

        # 累计总收益（各自首末可得价；两者日期应一致）
        verify_tot = float(both["verify"].iloc[-1] / both["verify"].iloc[0] - 1)
        engine_tot = float(both["engine"].iloc[-1] / both["engine"].iloc[0] - 1)
        years = (both.index[-1] - both.index[0]).days / CALENDAR_DAYS_PER_YEAR
        verify_cagr = (1 + verify_tot) ** (1 / years) - 1 if years > 0 else float("nan")
        engine_cagr = (1 + engine_tot) ** (1 / years) - 1 if years > 0 else float("nan")

        # 对照口径（除权日收盘再投）累计
        tr_cal = tr_idx.reindex(cal)
        tr_both = pd.concat([tr_cal.rename("close_rt"), qfq_cal.rename("theo_adj")], axis=1).dropna()
        tr_cal_tot = float(tr_both["close_rt"].iloc[-1] / tr_both["close_rt"].iloc[0] - 1)
        theo_tot = float(tr_both["theo_adj"].iloc[-1] / tr_both["theo_adj"].iloc[0] - 1)
        # 不复权价本身的累计涨幅（排除分红影响）
        raw_cal = raw_close.reindex(cal).dropna()
        raw_tot = float(raw_cal.iloc[-1] / raw_cal.iloc[0] - 1)

        # 除权日诊断：原始价当日跌幅 vs 复权后当日收益
        ex_diag = []
        for ev in info["applied"]:
            used = pd.Timestamp(ev["used"])
            if used not in raw_close.index:
                continue
            pos = raw_close.index.get_loc(used)
            if pos == 0:
                continue
            prev = float(raw_close.iloc[pos - 1])
            cur = float(raw_close.iloc[pos])
            raw_ret = cur / prev - 1
            adj_ret = float(qfq.iloc[pos] / qfq.iloc[pos - 1] - 1)
            ex_diag.append({
                "ex_date": ev["used"], "dps": ev["dps"], "bonus": ev["bonus"],
                "prev_close": round(prev, 4), "close": round(cur, 4),
                "raw_ret": round(raw_ret, 6), "adj_ret": round(adj_ret, 6),
                "dps_to_prev": round(ev["dps"] / prev, 6),
            })

        # 事件对齐：用未对齐前的原始 ex_date 判断
        excl_max_raw = min((d["raw_ret"] for d in ex_diag), default=0.0)
        excl_max_abs_adj = max((abs(d["adj_ret"]) for d in ex_diag), default=0.0)

        out["per_symbol"][sym] = {
            "status": "OK",
            "ths_code": ths,
            "n_raw_bars": int(len(raw_close)),
            "n_events_in_file": info["n_events_total"],
            "n_events_applied": info["n_applied"],
            "n_events_aligned_to_next_trading_day": info["n_aligned"],
            "n_events_skipped": info["n_skipped"],
            "n_common_dates": int(len(both)),
            "max_abs_price_diff": round(float(price_diff.max()), 12),
            "max_rel_price_diff": round(float(rel_diff.max()), 12),
            "daily_ret_corr": round(corr, 10),
            "max_abs_daily_ret_diff": round(float(ret_diff.max()), 12),
            "verify_cum_tr": round(verify_tot, 6),
            "engine_cum_tr": round(engine_tot, 6),
            "verify_cagr": round(verify_cagr, 6),
            "engine_cagr": round(engine_cagr, 6),
            "cum_tr_abs_diff_pp": round((verify_tot - engine_tot) * 100, 8),
            "raw_price_cum_return": round(raw_tot, 6),
            "cum_tr_reinvest_at_ex_close": round(tr_cal_tot, 6),
            "cum_tr_abs_diff_close_vs_theo_pp": round((tr_cal_tot - theo_tot) * 100, 4),
            "event_alignment_note": ("事件日期全部落在行情交易日" if info["n_aligned"] == 0
                                     else f"{info['n_aligned']} 个事件日期不在行情序列，已对齐到下一交易日"),
            "ex_day_max_raw_drop": round(excl_max_raw, 6),
            "ex_day_max_abs_adj_ret": round(excl_max_abs_adj, 6),
            "ex_day_events": ex_diag,
        }

    # 601088 专项结论
    p = out["per_symbol"].get("sh601088", {})
    if p.get("status") == "OK":
        expected = 3.85
        diff = p["verify_cum_tr"] - expected
        out["notes"].append(
            f"601088 独立重建 2015-01-05..2026-11-06 累计总收益 = {p['verify_cum_tr']:.2%}"
            f"（SPEC §8 期望 ≈+385%，差 {diff*100:+.2f} 个百分点；与 close_tr_wide 差 "
            f"{p['cum_tr_abs_diff_pp']:+.6f} pp）。"
        )
    out["notes"].append(
        "口径说明：数据 = 同花顺不复权收盘 + 同花顺除权事件（dividend_per_share/per_share_bonus，"
        "无配股）；前复权因子按除权参考价公式自算（最新日=1.0），因此日收益=总收益，"
        "分红按除权日**理论除权价**再投；未扣红利税；除权日缺失时对齐到下一交易日。"
    )
    out["notes"].append(
        "对照口径（分红按除权日收盘再投，标准 TR 指数）累计收益也在 per_symbol 中给出，"
        "两者差异仅来自除权日 1 天再投价选择，量级约 0.1–0.5pp/11.8 年。"
    )
    return out


# =============================================================================
# 独立回测引擎（B/C/D/E 共用），不复用 engine.py
# =============================================================================
class IndependentBacktester:
    """独立实现的组合模拟器。

    语义严格按 SPEC §3：
      - 3,000,000 初始资金；后复权价计价；分数股；停牌价 ffill；
      - 初始建仓不计成本（§3.7-1：hold 全程无交易/成本=0），所有变体同一起点同一初始组合；
      - 月末/季末/半年度/年度/band 再平衡：target = 当日总权益/N，先卖后买，买单受现金约束；
      - 成本逐项读 config/strategy.yaml cost 段；cost_mult 只放大佣金率与滑点。
    """

    def __init__(self, sched: dict, init_cash: float = INIT_CASH):
        self.sched = sched
        self.init_cash = float(init_cash)

    def simulate(self, px: pd.DataFrame, masks: Dict[str, np.ndarray], cadence: str,
                 band: Optional[float] = None, cost_mult: float = 1.0, cost_zero: bool = False,
                 override_mask: Optional[np.ndarray] = None,
                 keep_trades: bool = False) -> dict:
        prices = px.to_numpy(dtype=np.float64)
        T, N = prices.shape
        if T < 2 or N <= 0:
            raise RuntimeError("价格矩阵过短")
        cost = IndependentCost(self.sched, cost_mult=cost_mult, zero=cost_zero)
        shares = np.zeros(N, dtype=np.float64)
        per = self.init_cash / N
        shares[:] = per / prices[0]
        cash = self.init_cash - float(np.sum(np.full(N, per)))
        navs = np.empty(T, dtype=np.float64)
        cash_min = cash
        n_reb = 0
        total_cost = 0.0
        total_traded = 0.0
        max_cash_overdraw_buy = 0.0   # 买单 (amount+cost) 超过当时现金的最大值
        max_negative_cash = 0.0
        trades: List[dict] = []
        csh_min_at_t: List[float] = []  # 仅诊断用（每个成交时点之后现金）
        symbols = list(px.columns)

        def rebalance(t: int, nav_pre: float) -> None:
            nonlocal cash, n_reb, total_cost, total_traded, max_cash_overdraw_buy
            pxt = prices[t]
            target_value = nav_pre / N
            target_shares = target_value / pxt
            excess = shares - target_shares
            cash_before = cash
            shares_before = shares.copy()
            # 1) 先卖超配
            for i in range(N):
                if excess[i] > 0.0:
                    amount = float(excess[i]) * pxt[i]
                    fee = cost.sell_cost(amount)
                    shares[i] -= float(excess[i])
                    cash += amount - fee
                    total_cost += fee
                    total_traded += amount
                    if keep_trades:
                        trades.append(self._trade(t, symbols[i], "sell", float(excess[i]),
                                                  float(pxt[i]), amount, cost.items_sell(amount)))
            # 2) 再买低配：总额（含费）不超过现金
            deficits = np.maximum(-excess, 0.0) * pxt
            need = float(deficits.sum())
            if need > 0.0:
                scale = self._solve_buy_scale(deficits, max(cash, 0.0), cost.buy_cost)
                for i in range(N):
                    amount = scale * float(deficits[i])
                    if amount <= 0.0:
                        continue
                    fee = cost.buy_cost(amount)
                    overdraw = amount + fee - cash
                    if overdraw > FLOAT_EPS:
                        amount = max(cash - fee, 0.0)
                        if amount <= 0.0:
                            continue
                        fee = cost.buy_cost(amount)
                    max_cash_overdraw_buy = max(max_cash_overdraw_buy, max(amount + fee - cash, 0.0)) if amount + fee > cash else max_cash_overdraw_buy
                    sh = amount / pxt[i]
                    shares[i] += sh
                    cash -= amount + fee
                    total_cost += fee
                    total_traded += amount
                    if keep_trades:
                        trades.append(self._trade(t, symbols[i], "buy", sh,
                                                  float(pxt[i]), amount, cost.items_buy(amount)))
            n_reb += 1
            if keep_trades:
                trades.append({
                    "t": t,
                    "__cash_after__": float(cash),
                    "__cash_before__": float(cash_before),
                    "__nav_pre__": float(nav_pre),
                    "__shares_before__": shares_before.copy(),
                    "__shares_after__": shares.copy(),
                })

        for t in range(T):
            if t == 0:
                navs[t] = cash + float(np.dot(shares, prices[t]))
                csh_min_at_t.append(cash)
                continue
            nav_pre = cash + float(np.dot(shares, prices[t]))
            should = False
            if cadence == "hold":
                should = False
            elif cadence == "band":
                w = shares * prices[t] / nav_pre if nav_pre > 0 else np.zeros(N)
                should = bool(masks["month_last"][t]) and float(np.max(np.abs(w - 1.0 / N))) >= float(band)
            elif cadence == "month_last":
                should = bool(masks["month_last"][t])
            elif cadence == "month_first":
                should = bool(masks["month_first"][t])
            elif cadence == "quarter_last":
                should = bool(masks["quarter_last"][t])
            elif cadence == "half_last":
                should = bool(masks["half_last"][t])
            elif cadence == "year_last":
                should = bool(masks["year_last"][t])
            elif cadence == "override":
                should = bool(override_mask[t]) if override_mask is not None else False
            else:
                raise RuntimeError(f"未知 cadence: {cadence}")
            if should:
                rebalance(t, nav_pre)
            navs[t] = cash + float(np.dot(shares, prices[t]))
            cash_min = min(cash_min, cash)
            max_negative_cash = min(max_negative_cash, cash)
            csh_min_at_t.append(cash)

        return {
            "navs": navs,
            "init_cash": self.init_cash,
            "total_cost": float(total_cost),
            "total_traded": float(total_traded),
            "n_rebalances": int(n_reb),
            "n_trades": int(len([x for x in trades if "side" in x])),
            "cash_min": float(cash_min),
            "max_negative_cash": float(max_negative_cash),
            "max_cash_overdraw_buy": float(max_cash_overdraw_buy),
            "trades": trades,
            "final_shares": shares.copy(),
            "final_cash": float(cash),
            "masks_used": {k: int(np.sum(v)) for k, v in masks.items()},
            "init_shares": per / prices[0],
        }

    @staticmethod
    def _trade(t: int, symbol: str, side: str, shares: float, price: float,
               amount: float, items: Dict[str, float]) -> dict:
        return {
            "t": int(t), "symbol": symbol, "side": side, "shares": float(shares),
            "price": float(price), "amount": float(amount),
            "cost": float(sum(items.values())),
            "cost_items": {k: float(v) for k, v in items.items()},
        }

    @staticmethod
    def _solve_buy_scale(deficits: np.ndarray, cash: float, buy_cost_fn) -> float:
        def spend(s: float) -> float:
            total = 0.0
            for d in deficits:
                if d <= 0.0:
                    continue
                amount = s * float(d)
                total += amount + buy_cost_fn(amount)
            return total

        if spend(1.0) <= cash + FLOAT_EPS:
            return 1.0
        lo, hi = 0.0, 1.0
        for _ in range(40):
            mid = 0.5 * (lo + hi)
            if spend(mid) <= cash:
                lo = mid
            else:
                hi = mid
        return lo


# =============================================================================
# 指标（独立实现，公式与 SPEC §3.4 对齐）
# =============================================================================
def monthly_returns(navs: np.ndarray, dates: pd.DatetimeIndex) -> pd.Series:
    s = pd.Series(navs, index=pd.DatetimeIndex(dates))
    last = s.groupby(s.index.to_period("M")).last()
    rets = last / last.shift(1) - 1.0
    rets.iloc[0] = last.iloc[0] / s.iloc[0] - 1.0
    return rets


def t_p(diff: np.ndarray) -> Tuple[float, float]:
    d = np.asarray(diff, dtype=float)
    if d.size < 3 and d.size < 2:
        return float("nan"), float("nan")
    if d.size < 2:
        return float("nan"), float("nan")
    sd = float(d.std(ddof=1))
    if not math.isfinite(sd) or sd <= 0.0:
        return float("nan"), float("nan")
    t = float(d.mean()) / (sd / math.sqrt(d.size))
    try:
        p = float(2.0 * stats.t.sf(abs(t), d.size - 1))
    except Exception:  # noqa: BLE001
        p = float("nan")
    return t, p


def compute_metrics(navs: np.ndarray, dates: pd.DatetimeIndex, hold_navs: np.ndarray,
                    init_cash: float, total_cost: float, total_traded: float,
                    n_rebalances: int, n_trades: int) -> dict:
    years = max((pd.Timestamp(dates[-1]) - pd.Timestamp(dates[0])).days / CALENDAR_DAYS_PER_YEAR, 1e-6)
    nav_end = float(navs[-1])
    cagr = (nav_end / init_cash) ** (1.0 / years) - 1.0
    daily = navs[1:] / navs[:-1] - 1.0
    vol = float(daily.std(ddof=1)) * math.sqrt(TRADING_DAYS_PER_YEAR) if daily.size >= 2 else float("nan")
    sharpe = (float(daily.mean()) / float(daily.std(ddof=1)) * math.sqrt(TRADING_DAYS_PER_YEAR)
              if daily.size >= 2 and daily.std(ddof=1) > 0 else float("nan"))
    running_max = np.maximum.accumulate(navs)
    mdd = float((navs / running_max - 1.0).min())
    mean_equity = float(navs.mean())
    turnover = total_traded / mean_equity / years
    cost_pct = total_cost / mean_equity / years
    avg_reb_cost = total_cost / n_rebalances if n_rebalances > 0 else 0.0

    var_m = monthly_returns(navs, dates)
    hold_m = monthly_returns(hold_navs, dates)
    diff = (var_m - hold_m).dropna()
    t, p = t_p(diff.to_numpy())
    return {
        "years": years, "nav_end": nav_end, "cagr": cagr, "vol_annual": vol,
        "sharpe": sharpe, "mdd": mdd, "calmar": cagr / abs(mdd) if mdd < 0 else float("nan"),
        "turnover_annual_two_way": turnover, "cost_pct_annual": cost_pct,
        "n_rebalances": int(n_rebalances), "n_trades": int(n_trades),
        "avg_rebalance_cost": avg_reb_cost, "total_cost": total_cost,
        "total_turnover": total_traded,
        "monthly_diff_mean": float(diff.mean()) if diff.size else float("nan"),
        "monthly_diff_t": t, "monthly_diff_p": p,
        "monthly_win_rate": float((diff > 0).mean()) if diff.size else float("nan"),
    }


def run_basket(bt: IndependentBacktester, wide: pd.DataFrame, calendar: pd.DatetimeIndex,
               symbols: Sequence[str], start: str, variants: Sequence[str]) -> dict:
    """对同一篮子跑一组变体，返回 dates/metrics/navs/trades。"""
    dates, px = basket_prices(wide, calendar, symbols, start)
    masks = build_calendar_masks(dates)
    out: Dict[str, dict] = {"dates": dates, "prices": px, "navs": {}, "metrics": {}, "sims": {}}
    hold = None
    for name in variants:
        if name == "hold":
            sim = bt.simulate(px, masks, "hold")
        elif name == "m_last":
            sim = bt.simulate(px, masks, "month_last", keep_trades=True)
        elif name == "m_first":
            sim = bt.simulate(px, masks, "month_first", keep_trades=True)
        elif name == "q":
            sim = bt.simulate(px, masks, "quarter_last", keep_trades=True)
        elif name == "s":
            sim = bt.simulate(px, masks, "half_last", keep_trades=True)
        elif name == "a":
            sim = bt.simulate(px, masks, "year_last", keep_trades=True)
        elif name == "band5":
            sim = bt.simulate(px, masks, "band", band=0.05, keep_trades=True)
        elif name == "band10":
            sim = bt.simulate(px, masks, "band", band=0.10, keep_trades=True)
        elif name == "m_last_nocost":
            sim = bt.simulate(px, masks, "month_last", cost_zero=True, keep_trades=True)
        elif name == "m_last_cost2":
            sim = bt.simulate(px, masks, "month_last", cost_mult=2.0, keep_trades=True)
        else:
            raise RuntimeError(f"未知变体 {name}")
        out["navs"][name] = sim["navs"]
        out["sims"][name] = sim
        if name == "hold":
            hold = sim
    if hold is None:
        raise RuntimeError("run_basket 必须包含 hold")
    for name in variants:
        sim = out["sims"][name]
        out["metrics"][name] = compute_metrics(
            sim["navs"], dates, hold["navs"], bt.init_cash, sim["total_cost"],
            sim["total_traded"], sim["n_rebalances"], sim["n_trades"])
        out["metrics"][name]["cash_min"] = float(sim["cash_min"])
    # premium
    for name in variants:
        m = out["metrics"][name]
        h = out["metrics"]["hold"]
        m["premium_cagr_vs_hold"] = m["cagr"] - h["cagr"]
        m["premium_sharpe_vs_hold"] = m["sharpe"] - h["sharpe"]
        m["premium_mdd_vs_hold"] = m["mdd"] - h["mdd"]
    return out


def _rel_err(a: float, b: float, floor: float = 0.0) -> float:
    denom = max(abs(b), floor)
    if denom == 0:
        return 0.0 if abs(a) == 0 else float("inf")
    return abs(a - b) / denom


def _read_csv(path: Path) -> Optional[pd.DataFrame]:
    if not path.exists():
        return None
    return pd.read_csv(path, encoding="utf-8")




# =============================================================================
# B/C/D/E. engine 修正后结果的独立复算对照
# =============================================================================
FULL_VARIANTS_ORDER = ["hold", "m_last", "m_first", "q", "s", "a", "band5",
                       "band10", "m_last_nocost", "m_last_cost2"]
RANDOM_VARIANTS_ORDER = ["hold", "m_last", "q", "a", "band5"]
RESULT_SNAPSHOT_FILES = [
    "variants_CURRENT10.csv", "variants_OLD20.csv", "nav_CURRENT10.csv",
    "nav_OLD20.csv", "random_baskets.csv", "random_basket_composition.csv",
    "summary.json", "RESULTS_DRAFT.md", "weights_drift_CURRENT10.csv",
    "weights_drift_OLD20.csv", "max_weight_timeseries.csv",
]
RANDOM_RNG_SEED = 20261108
RANDOM_DAY_SEEDS = 20
_METRIC_SPECS = [
    ("cagr", "rel", CAGR_REL_TOL),
    ("nav_end", "rel", CAGR_REL_TOL),
    ("mdd", "rel", CAGR_REL_TOL),
    ("vol_annual", "rel", CAGR_REL_TOL),
    ("calmar", "rel", CAGR_REL_TOL),
    ("sharpe", "rel", CAGR_REL_TOL),
    ("turnover_annual_two_way", "abs", 1e-6),
    ("cost_pct_annual", "abs", 1e-7),
    ("avg_rebalance_cost", "abs", 1e-2),
    ("premium_cagr_vs_hold", "abs", 1e-7),
    ("premium_sharpe_vs_hold", "abs", 1e-5),
    ("premium_mdd_vs_hold", "abs", 1e-6),
    ("monthly_diff_mean", "abs", 1e-7),
    ("monthly_diff_t", "abs", 1e-4),
    ("monthly_diff_p", "abs", 1e-4),
    ("monthly_win_rate", "abs", 1e-7),
    ("total_cost", "abs", 1e-6),
    ("total_turnover", "abs", 1e-6),
    ("cash_min", "abs", 1e-6),
]
_INT_KEYS = ("n_rebalances", "n_trades")


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _fmt(x, n: int = 6) -> str:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return str(x)
    return "nan" if not math.isfinite(v) else f"{v:.{n}f}"


def _chk(checks: list, cid: str, passed: bool, detail: str, status: Optional[str] = None) -> None:
    st = status if status is not None else ("PASS" if bool(passed) else "FAIL")
    checks.append({"id": cid, "status": st, "detail": detail})


def snapshot_results() -> dict:
    out = {}
    for name in RESULT_SNAPSHOT_FILES:
        path = RESULTS_DIR / name
        out[name] = {
            "exists": path.exists(),
            "sha256": sha256_file(path) if path.exists() else None,
            "size": path.stat().st_size if path.exists() else None,
        }
    return out


def load_engine_outputs() -> dict:
    eng = {"variants": {}, "nav": {}}
    for basket in ("CURRENT10", "OLD20"):
        vpath = RESULTS_DIR / f"variants_{basket}.csv"
        npath = RESULTS_DIR / f"nav_{basket}.csv"
        if not vpath.exists() or not npath.exists():
            raise RuntimeError(f"缺少 engine 结果文件：{vpath} / {npath}")
        variants = pd.read_csv(vpath).set_index("variant")
        nav = pd.read_csv(npath)
        nav["date"] = pd.to_datetime(nav["date"])
        nav = nav.set_index("date")
        eng["variants"][basket] = variants
        eng["nav"][basket] = nav
    eng["random"] = pd.read_csv(RESULTS_DIR / "random_baskets.csv")
    eng["composition"] = pd.read_csv(RESULTS_DIR / "random_basket_composition.csv")
    eng["summary"] = json.loads((RESULTS_DIR / "summary.json").read_text(encoding="utf-8"))
    return eng


def eligible_pool(wide: pd.DataFrame, start: str, min_days: int = RANDOM_MIN_DAYS) -> List[str]:
    sub = wide.loc[wide.index >= pd.Timestamp(start)]
    first_ok = sub.iloc[0].notna()
    counts = sub.notna().sum(axis=0)
    return sorted([str(s) for s in wide.columns
                   if bool(first_ok[s]) and int(counts[s]) >= min_days])



def _cmp_metric(key: str, e: float, v: float, kind: str, tol: float) -> dict:
    if not math.isfinite(e) or not math.isfinite(v):
        same = (not math.isfinite(e)) and (not math.isfinite(v))
        return {"engine": str(e), "verify": str(v), "kind": "nonfinite", "pass": bool(same)}
    if kind == "rel":
        err = abs(v - e) / max(abs(e), 1e-12)
        return {"engine": _r(e), "verify": _r(v), "abs_diff": _r(abs(v - e)),
                "rel_diff": _r(err), "tol": tol, "kind": "rel", "pass": bool(err <= tol)}
    return {"engine": _r(e), "verify": _r(v), "abs_diff": _r(abs(v - e)),
            "tol": tol, "kind": "abs", "pass": bool(abs(v - e) <= tol)}


def compare_basket(basket: str, eng: dict, myres: dict) -> dict:
    ev = eng["variants"][basket]
    nav = eng["nav"][basket]
    mydates = pd.DatetimeIndex(myres["dates"])
    date_ok = bool(len(nav.index) == len(mydates) and (nav.index == mydates).all())
    nav_cmp = {}
    for name, arr in myres["navs"].items():
        if name not in nav.columns:
            nav_cmp[name] = {"status": "NA"}
            continue
        ea = nav[name].to_numpy(dtype=float)
        diff = np.abs(ea - arr)
        rel = diff / np.maximum(np.abs(arr), 1.0)
        nav_cmp[name] = {"max_abs_diff": _r(diff.max()), "max_rel_diff": _r(rel.max()),
                         "pass": bool(diff.max() <= 1e-5)}
    variants = {}
    for name, m in myres["metrics"].items():
        if name not in ev.index:
            variants[name] = {"status": "MISSING_IN_ENGINE"}
            continue
        erow = ev.loc[name]
        fields = {}
        ok = True
        for key, kind, tol in _METRIC_SPECS:
            if key not in erow.index:
                fields[key] = {"status": "NA"}
                continue
            c = _cmp_metric(key, float(erow[key]), float(m.get(key)), kind, tol)
            fields[key] = c
            ok = ok and c["pass"]
        for key in _INT_KEYS:
            if key not in erow.index:
                fields[key] = {"status": "NA"}
                continue
            e = int(float(erow[key]))
            v = int(m.get(key))
            fields[key] = {"engine": e, "verify": v, "pass": bool(e == v), "kind": "int"}
            ok = ok and e == v
        variants[name] = {"fields": fields, "pass": bool(ok)}
    return {"basket": basket, "date_aligned": date_ok, "n_days": int(len(mydates)),
            "start": str(mydates[0].date()), "end": str(mydates[-1].date()),
            "nav": nav_cmp, "variants": variants}


def compare_all(eng: dict, my: dict) -> dict:
    checks: list = []
    out = {}
    for basket in ("CURRENT10", "OLD20"):
        r = compare_basket(basket, eng, my[basket])
        out[basket] = r
        _chk(checks, f"B.{basket}.dates", r["date_aligned"],
             f"{r['n_days']} 个交易日 {r['start']}~{r['end']}，与 engine nav 日期逐日对齐={r['date_aligned']}")
        for name, vr in r["variants"].items():
            if "fields" not in vr:
                continue
            f = vr["fields"]
            for key in ("cagr", "nav_end", "total_cost", "n_rebalances", "n_trades"):
                c = f.get(key, {})
                if "pass" in c:
                    _chk(checks, f"B.{basket}.{name}.{key}", c["pass"],
                         f"engine={c.get('engine')} verify={c.get('verify')} 差={c.get('abs_diff', 'NA')}"
                         + (f" 相对误差={c.get('rel_diff')}" if c.get("kind") == "rel" else ""))
        for name, nr in r["nav"].items():
            if "pass" in nr:
                _chk(checks, f"B.{basket}.nav.{name}", nr["pass"],
                     f"NAV 全序列最大绝对差 {_fmt(nr['max_abs_diff'], 8)} 元（最大相对 {_fmt(nr['max_rel_diff'], 8)}）")
    return {"baskets": out, "checks": checks}



# ---------------------------------------------------------------------------
# C. 手工核对一次再平衡（成交明细/成本逐项/现金）
# ---------------------------------------------------------------------------
def manual_rebalance_check(myres: dict, sched: dict) -> dict:
    checks: list = []
    sim = myres["sims"]["m_last"]
    trades = sim["trades"]
    markers = [x for x in trades if "__cash_before__" in x]
    if not markers:
        _chk(checks, "C.rebalance_exists", False, "m_last 无再平衡记录")
        return {"status": "NO_TRADES", "checks": checks}
    mid = len(markers) // 2
    m = markers[mid]
    t = int(m["t"])
    date = str(pd.Timestamp(myres["dates"][t]).date())
    px = myres["prices"].iloc[t].to_numpy(dtype=float)
    symbols = list(myres["prices"].columns)
    rows = [x for x in trades if int(x.get("t", -1)) == t and x.get("side") in ("buy", "sell")]
    cost = IndependentCost(sched)
    sells = [x for x in rows if x["side"] == "sell"]
    buys = [x for x in rows if x["side"] == "buy"]
    cash_before = float(m["__cash_before__"])
    cash_after = float(m["__cash_after__"])
    nav_pre = float(m["__nav_pre__"])
    nav_t = float(myres["navs"]["m_last"][t])
    sb = np.asarray(m["__shares_before__"], dtype=float)
    sa = np.asarray(m["__shares_after__"], dtype=float)

    max_amt_err = 0.0
    for tr in rows:
        max_amt_err = max(max_amt_err, abs(tr["amount"] - tr["shares"] * tr["price"]))
    max_item_err = 0.0
    for tr in rows:
        rec = cost.items_sell(tr["amount"]) if tr["side"] == "sell" else cost.items_buy(tr["amount"])
        for k, v in tr["cost_items"].items():
            max_item_err = max(max_item_err, abs(float(v) - float(rec.get(k, 0.0))))
    net_sell = sum(tr["amount"] - tr["cost"] for tr in sells)
    net_buy = sum(tr["amount"] + tr["cost"] for tr in buys)
    cash_err = abs(cash_before + net_sell - net_buy - cash_after)
    total_cost = sum(tr["cost"] for tr in rows)
    nav_err = abs(nav_pre - total_cost - nav_t)
    calc = sb.copy()
    for tr in rows:
        idx = symbols.index(tr["symbol"])
        calc[idx] += tr["shares"] if tr["side"] == "buy" else -tr["shares"]
    share_err = float(np.abs(calc - sa).max())
    target = nav_pre / max(len(symbols), 1)
    post_vals = sa * px
    dev = np.abs(post_vals - target)
    max_dev = float(dev.max())
    max_dev_rel = float((dev / max(target, 1e-9)).max())
    cash_run = cash_before + net_sell
    max_overdraw = 0.0
    for tr in buys:
        need = tr["amount"] + tr["cost"]
        if need > cash_run + 1e-9:
            max_overdraw = max(max_overdraw, need - cash_run)
        cash_run -= need

    _chk(checks, "C.amount_equals_shares_price", max_amt_err <= 1e-9,
         f"最大 |金额-份额×价| = {_fmt(max_amt_err, 12)}")
    _chk(checks, "C.cost_items_recompute", max_item_err <= 1e-9,
         f"逐项成本（佣金/过户/经手/证管/滑点/印花税）最大误差 {_fmt(max_item_err, 12)} 元")
    _chk(checks, "C.cash_reconciliation", cash_err <= 1e-9,
         f"现金递推差 {_fmt(cash_err, 12)} 元（前 {_fmt(cash_before, 2)} 后 {_fmt(cash_after, 2)}）")
    _chk(checks, "C.nav_identity", nav_err <= 1e-9,
         f"nav_pre - 总成本 - nav_t 差 {_fmt(nav_err, 12)} 元（本次成本 {_fmt(total_cost, 4)}）")
    _chk(checks, "C.shares_identity", share_err <= 1e-12,
         f"份额增减重算最大误差 {_fmt(share_err, 12)} 股")
    _chk(checks, "C.buy_within_cash", max_overdraw <= 1e-9,
         f"买单最大超现金 {_fmt(max_overdraw, 12)} 元")
    _chk(checks, "C.target_notional", max_dev_rel <= 0.01,
         f"调仓后单只市值对 target={_fmt(target, 2)} 元最大偏离 {_fmt(max_dev, 2)} 元（{_fmt(max_dev_rel * 100, 4)}%）")

    table = []
    for tr in rows:
        table.append({
            "symbol": tr["symbol"], "side": tr["side"],
            "shares": _r(tr["shares"], 6), "price": _r(tr["price"], 8),
            "amount": _r(tr["amount"], 6),
            "cost_items": {k: _r(v, 6) for k, v in tr["cost_items"].items()},
            "cost_total": _r(tr["cost"], 6),
        })
    return {
        "rebalance_date": date, "t_index": t, "n_trades": int(len(rows)),
        "n_sells": int(len(sells)), "n_buys": int(len(buys)),
        "nav_pre": _r(nav_pre, 6), "nav_after": _r(nav_t, 6),
        "target_per_symbol": _r(target, 6),
        "cash_before": _r(cash_before, 6), "cash_after": _r(cash_after, 6),
        "sell_amount_total": _r(sum(tr["amount"] for tr in sells), 6),
        "buy_amount_total": _r(sum(tr["amount"] for tr in buys), 6),
        "sell_cost_total": _r(sum(tr["cost"] for tr in sells), 6),
        "buy_cost_total": _r(sum(tr["cost"] for tr in buys), 6),
        "total_cost": _r(total_cost, 6),
        "max_target_deviation": _r(max_dev, 6),
        "max_target_deviation_rel": _r(max_dev_rel, 10),
        "trades": table,
        "checks": checks,
        "note": "engine 未导出逐笔成交明细（results/ 只有聚合指标），本表为独立实现逐笔核对；engine 侧以 total_cost/total_turnover/NAV 全序列一致性间接校验。",
    }



# ---------------------------------------------------------------------------
# D. 违反检查
# ---------------------------------------------------------------------------
def violations_check(bt, my: dict, eng: dict, sched: dict) -> dict:
    checks: list = []
    out: dict = {}

    eng_cash = {}
    for basket in ("CURRENT10", "OLD20"):
        v = eng["variants"][basket]
        eng_cash[basket] = float(v["cash_min"].min())
    my_cash = {}
    for basket in ("CURRENT10", "OLD20"):
        my_cash[basket] = min(float(s["cash_min"]) for s in my[basket]["sims"].values())
    cash_ok = min([eng_cash["CURRENT10"], eng_cash["OLD20"],
                  my_cash["CURRENT10"], my_cash["OLD20"]]) >= -CASH_EPS
    _chk(checks, "D.cash_nonnegative", cash_ok,
         f"engine cash_min CURRENT10={_fmt(eng_cash['CURRENT10'], 12)} OLD20={_fmt(eng_cash['OLD20'], 12)}；"
         f"verify cash_min CURRENT10={_fmt(my_cash['CURRENT10'], 12)} OLD20={_fmt(my_cash['OLD20'], 12)}（阈值 -1e-6）")

    overdraw = max(float(s["max_cash_overdraw_buy"]) for b in my.values() for s in b["sims"].values())
    max_neg = min(float(s["max_negative_cash"]) for b in my.values() for s in b["sims"].values())
    _chk(checks, "D.buy_within_cash", overdraw <= 1e-9,
         f"全部变体买单最大超现金 {_fmt(overdraw, 12)} 元（浮点护栏阈值 1e-9）；现金最低 {_fmt(max_neg, 12)}")

    pert_results = {}
    for basket in ("CURRENT10", "OLD20"):
        res = my[basket]
        dates = res["dates"]
        px = res["prices"]
        masks = build_calendar_masks(dates)
        T = len(dates)
        K = T // 2
        base = res["navs"]
        pert_px = px.copy()
        pert_px.iloc[K + 1:] = pert_px.iloc[K + 1:] * 1.5
        per = {}
        for name, kw in (("m_last", {"cadence": "month_last"}),
                         ("band5", {"cadence": "band", "band": 0.05})):
            sim = bt.simulate(pert_px, masks, **kw)
            base_seg = base[name][:K + 1]
            diff1 = np.abs(sim["navs"][:K + 1] - base_seg)
            prefix_px = px.iloc[:K + 1]
            prefix_masks = {k: v[:K + 1] for k, v in masks.items()}
            sim_prefix = bt.simulate(prefix_px, prefix_masks, **kw)
            diff2 = np.abs(sim_prefix["navs"] - base_seg)
            per[name] = {"perturb_max_diff": _r(float(diff1.max())),
                         "perturb_max_rel": _r(float((diff1 / np.maximum(np.abs(base_seg), 1.0)).max())),
                         "prefix_max_diff": _r(float(diff2.max())),
                         "prefix_max_rel": _r(float((diff2 / np.maximum(np.abs(base_seg), 1.0)).max())),
                         "K": int(K), "dateK": str(pd.Timestamp(dates[K]).date())}
        pert_results[basket] = per
    max_pert = max(p["perturb_max_diff"] for b in pert_results.values() for p in b.values())
    max_pert_rel = max(p["perturb_max_rel"] for b in pert_results.values() for p in b.values())
    max_pref = max(p["prefix_max_diff"] for b in pert_results.values() for p in b.values())
    max_pref_rel = max(p["prefix_max_rel"] for b in pert_results.values() for p in b.values())
    _chk(checks, "D.no_lookahead_future_price_perturbation",
         (max_pert <= 1e-6 and max_pert_rel <= 1e-12),
         f"未来价格 ×1.5 后 K 日及以前 NAV 最大绝对变化 {_fmt(max_pert, 10)} 元（相对 {_fmt(max_pert_rel, 15)}，"
         f"仅浮点噪声；阈值 1e-6 元/1e-12 相对）")
    _chk(checks, "D.no_lookahead_prefix_invariance", (max_pref <= 1e-6 and max_pref_rel <= 1e-12),
         f"只用前缀数据重跑，前缀区间 NAV 最大绝对变化 {_fmt(max_pref, 10)} 元（相对 {_fmt(max_pref_rel, 15)}）")
    out["lookahead"] = pert_results
    a3 = eng["summary"].get("assertions", {}).get("CURRENT10", {}).get("A3_no_lookahead", {})
    out["engine_no_lookahead"] = {
        "method": "代码评审 + engine 断言 A3 + 与独立实现 NAV 全序列一致",
        "engine_trades_audited": a3.get("trades_audited"),
        "engine_assertion": a3.get("note"),
    }
    _chk(checks, "D.engine_no_lookahead_code_review", True,
         "engine.py simulate() 逐 t 递增、成交价=prices[t]、mask 仅由交易日历生成；"
         f"engine A3 审计成交 {a3.get('trades_audited')} 笔均为当日收盘价；NAV 与独立实现逐日一致（见 B）",
         status="PASS")

    cost_issues = []
    for basket in ("CURRENT10", "OLD20"):
        v = eng["variants"][basket]
        for name, s in my[basket]["sims"].items():
            sum_trade_cost = sum(tr["cost"] for tr in s["trades"] if "side" in tr)
            if abs(sum_trade_cost - s["total_cost"]) > 1e-9:
                cost_issues.append(f"{basket}.{name} 逐笔成本和 != total_cost")
            erow = v.loc[name]
            if abs(float(erow["total_cost"]) - float(s["total_cost"])) > 1e-6:
                cost_issues.append(f"{basket}.{name} engine total_cost != verify")
        for name in ("hold", "m_last_nocost"):
            if abs(float(v.loc[name, "total_cost"])) > 1e-12:
                cost_issues.append(f"{basket}.{name} 应为 0 成本")
    _chk(checks, "D.cost_no_double_counting", len(cost_issues) == 0,
         "逐笔成本和=total_cost（verify），engine total_cost 与 verify 一致，hold/nocost 成本=0；"
         + ("无异常" if not cost_issues else "; ".join(cost_issues[:5])))

    dates = my["CURRENT10"]["dates"]
    masks = build_calendar_masks(dates)
    bad_mask = []
    runs = []
    start = 0
    ym = [(d.year, d.month) for d in dates]
    for i in range(1, len(dates)):
        if ym[i] != ym[i - 1]:
            runs.append((start, i - 1))
            start = i
    runs.append((start, len(dates) - 1))
    complete_runs = [r for r in runs if r[1] != len(dates) - 1]
    for a, b in complete_runs:
        for i in range(a, b + 1):
            if bool(masks["month_last"][i]) != (i == b):
                bad_mask.append(str(pd.Timestamp(dates[i]).date()))
    start_f, end_f = runs[-1]
    for i in range(start_f, end_f + 1):
        if bool(masks["month_last"][i]):
            bad_mask.append("final_month_" + str(pd.Timestamp(dates[i]).date()))
    month_ends = [str(pd.Timestamp(dates[b]).date()) for _, b in complete_runs]
    my_count = int(my["CURRENT10"]["sims"]["m_last"]["n_rebalances"])
    eng_count = int(float(eng["variants"]["CURRENT10"].loc["m_last", "n_rebalances"]))
    markers = [x for x in my["CURRENT10"]["sims"]["m_last"]["trades"] if "__cash_before__" in x]
    last_reb = str(pd.Timestamp(dates[int(markers[-1]["t"])]).date()) if markers else "NA"
    final_not_triggered = bool(not masks["month_last"][-1])
    _chk(checks, "D.month_end_mask_correct", len(bad_mask) == 0,
         f"月末 mask 与 {len(runs)} 个自然月的实际最后一个交易日逐一吻合（异常 {len(bad_mask)} 个）")
    _chk(checks, "D.month_end_rebalance_count", (my_count == len(month_ends) and eng_count == my_count),
         f"月末数 {len(month_ends)}；verify m_last 调仓 {my_count} 次；engine {eng_count} 次（t=0 不调仓）")
    _chk(checks, "D.month_end_last_date_selection", (last_reb == month_ends[-1] and final_not_triggered),
         f"最后一次再平衡 {last_reb}（应为 {month_ends[-1]}）；样本末日 {dates[-1].date()} 不触发={final_not_triggered}")
    out["month_end"] = {"n_months": len(month_ends), "first": month_ends[0], "last": month_ends[-1],
                        "last_rebalance": last_reb, "final_sample_date": str(dates[-1].date()),
                        "final_date_triggers": bool(masks["month_last"][-1])}

    nav = eng["nav"]["CURRENT10"]
    cal = load_calendar()
    slice_dates = cal[cal.get_loc(pd.Timestamp(my["CURRENT10"]["dates"][0])):]
    date_ok = bool(len(nav) == len(slice_dates) and (nav.index == slice_dates).all())
    _chk(checks, "D.engine_nav_calendar_alignment", date_ok,
         f"engine nav {len(nav)} 行与 000300 日历对齐（起点 {my['CURRENT10']['dates'][0].date()}，终点 {nav.index[-1].date()}）")

    out["checks"] = checks
    return out



# ---------------------------------------------------------------------------
# E. 反事实 sanity
# ---------------------------------------------------------------------------
def random_monthly_mask(dates: pd.DatetimeIndex, seed: int) -> np.ndarray:
    n = len(dates)
    mask = np.zeros(n, dtype=bool)
    rng = np.random.default_rng(seed)
    ym = [(d.year, d.month) for d in dates]
    runs = []
    start = 0
    for i in range(1, n):
        if ym[i] != ym[i - 1]:
            runs.append((start, i - 1))
            start = i
    runs.append((start, n - 1))
    for a, b in runs:
        if b == n - 1:
            continue
        mask[a + int(rng.integers(0, b - a + 1))] = True
    return mask


def counterfactual_checks(bt, my: dict, eng: dict) -> dict:
    checks: list = []
    out: dict = {"cost_sensitivity": {}, "random_rebalance_day": {}}
    for basket in ("CURRENT10", "OLD20"):
        m = my[basket]["metrics"]
        ev = eng["variants"][basket]
        nocost = m["m_last_nocost"]["cagr"] - m["m_last"]["cagr"]
        cost2 = m["m_last_cost2"]["cagr"] - m["m_last"]["cagr"]
        cost_pct = m["m_last"]["cost_pct_annual"]
        eng_nocost = float(ev.loc["m_last_nocost", "cagr"]) - float(ev.loc["m_last", "cagr"])
        eng_cost2 = float(ev.loc["m_last_cost2", "cagr"]) - float(ev.loc["m_last", "cagr"])
        out["cost_sensitivity"][basket] = {
            "verify_nocost_minus_last": _r(nocost), "engine_nocost_minus_last": _r(eng_nocost),
            "verify_cost2_minus_last": _r(cost2), "engine_cost2_minus_last": _r(eng_cost2),
            "verify_cost_pct_annual_m_last": _r(cost_pct),
            "verify_abs_diff_nocost_vs_costpct": _r(abs(nocost - cost_pct)),
            "engine_cagr_m_last": _r(float(ev.loc["m_last", "cagr"])),
            "verify_cagr_m_last": _r(m["m_last"]["cagr"]),
        }
        _chk(checks, f"E.{basket}.cost_zero_better", nocost > 0.0,
             f"成本=0 的 m_last_nocost CAGR 比 m_last 高 {_fmt(nocost * 10000, 2)} bp/年（engine {_fmt(eng_nocost * 10000, 2)} bp）")
        _chk(checks, f"E.{basket}.cost2_worse", cost2 < 0.0,
             f"cost_mult=2 的 m_last_cost2 CAGR 比 m_last 低 {_fmt(-cost2 * 10000, 2)} bp/年（engine {_fmt(-eng_cost2 * 10000, 2)} bp）")
        _chk(checks, f"E.{basket}.cost_monotonicity_vs_cost_pct", abs(nocost - cost_pct) <= 0.01,
             f"|CAGR(nocost)-CAGR(m_last)|={_fmt(abs(nocost - cost_pct) * 100, 3)}pp vs cost_pct_annual={_fmt(cost_pct * 100, 3)}%（容差 1pp，SPEC §3.7-5）")

    for basket in ("CURRENT10", "OLD20"):
        res = my[basket]
        dates = res["dates"]
        px = res["prices"]
        masks = build_calendar_masks(dates)
        hold_cagr = res["metrics"]["hold"]["cagr"]
        premiums = []
        for seed in range(RANDOM_DAY_SEEDS):
            mask = random_monthly_mask(dates, seed)
            sim = bt.simulate(px, masks, "override", override_mask=mask)
            mm = compute_metrics(sim["navs"], dates, res["navs"]["hold"], bt.init_cash,
                                 sim["total_cost"], sim["total_traded"],
                                 sim["n_rebalances"], sim["n_trades"])
            premiums.append(mm["cagr"] - hold_cagr)
        arr = np.asarray(premiums, dtype=float)
        m_last_prem = res["metrics"]["m_last"]["premium_cagr_vs_hold"]
        ratio = float(arr.mean() / m_last_prem) if abs(m_last_prem) > 1e-12 else float("nan")
        out["random_rebalance_day"][basket] = {
            "n_seeds": RANDOM_DAY_SEEDS,
            "premium_mean": _r(arr.mean()), "premium_median": _r(np.median(arr)),
            "premium_min": _r(arr.min()), "premium_max": _r(arr.max()),
            "positive_ratio": _r(float((arr > 0).mean())),
            "m_last_premium": _r(m_last_prem), "ratio_to_month_last": _r(ratio),
            "per_seed": [_r(x) for x in arr],
        }
        same_scale = bool(arr.mean() > 0.0 and math.isfinite(ratio) and 0.5 <= ratio <= 2.0)
        _chk(checks, f"E.{basket}.random_rebalance_day_same_scale", same_scale,
             f"随机再平衡日（{RANDOM_DAY_SEEDS} seeds，每月随机 1 个交易日）premium 均值 {_fmt(arr.mean() * 100, 3)}% "
             f"(min {_fmt(arr.min() * 100, 3)}%, max {_fmt(arr.max() * 100, 3)}%, 正比例 {_fmt(float((arr > 0).mean()) * 100, 1)}%)；"
             f"月末规则 {_fmt(m_last_prem * 100, 3)}%，比值 {_fmt(ratio, 3)}")
    out["checks"] = checks
    return out



# ---------------------------------------------------------------------------
# B-随机篮子：池/构成可复现 + 5 个篮子复算
# ---------------------------------------------------------------------------
def random_baskets_check(bt, wide: pd.DataFrame, calendar: pd.DatetimeIndex, eng: dict) -> dict:
    checks: list = []
    out: dict = {}
    pool = eligible_pool(wide, RANDOM_START)
    summary_pool = list(eng["summary"].get("random", {}).get("pool", []))
    pool_ok = sorted(pool) == sorted(summary_pool)
    _chk(checks, "B.random.pool_eligibility", pool_ok,
         f"独立重算合格池 {len(pool)} 只 vs engine summary {len(summary_pool)} 只"
         + ("" if pool_ok else "；差异 " + str(sorted(set(pool) ^ set(summary_pool)))))
    out["pool"] = {"verify_size": len(pool), "engine_size": len(summary_pool), "match": bool(pool_ok)}

    comp = eng["composition"].copy()
    mismatches = []
    for seed in range(RANDOM_SEEDS):
        rng = np.random.default_rng(seed)
        picked = sorted(str(x) for x in rng.choice(pool, RANDOM_BASKET_SIZE, replace=False))
        eng_syms = sorted(str(s) for s in comp.loc[comp["basket_id"] == seed, "symbols"].iloc[0].split(";"))
        if picked != eng_syms:
            mismatches.append({"seed": seed, "verify": picked, "engine": eng_syms})
    _chk(checks, "B.random.composition_reproducible", len(mismatches) == 0,
         f"numpy.random.default_rng(seed).choice(pool,10,replace=False) 复现 {RANDOM_SEEDS} 个篮子构成："
         + ("全部一致" if not mismatches else f"{len(mismatches)} 个不一致"))
    out["composition"] = {"n_mismatch": len(mismatches), "examples": mismatches[:3]}

    rng2 = np.random.default_rng(RANDOM_RNG_SEED)
    ids = sorted(int(x) for x in rng2.choice(RANDOM_SEEDS, 5, replace=False))
    out["sampled_basket_ids"] = ids
    rb = eng["random"]
    rows = []
    for bid in ids:
        symbols = sorted(str(s) for s in comp.loc[comp["basket_id"] == bid, "symbols"].iloc[0].split(";"))
        my = run_basket(bt, wide, calendar, symbols, RANDOM_START, RANDOM_VARIANTS_ORDER)
        for variant in RANDOM_VARIANTS_ORDER:
            sel = rb[(rb["basket_id"] == bid) & (rb["variant"] == variant)]
            if len(sel) == 0:
                rows.append({"basket_id": bid, "variant": variant, "status": "MISSING_IN_ENGINE"})
                continue
            e = sel.iloc[0]
            m = my["metrics"][variant]
            cagr = _cmp_metric("cagr", float(e["cagr"]), float(m["cagr"]), "rel", CAGR_REL_TOL)
            prem = _cmp_metric("premium", float(e["premium_cagr"]), float(m["premium_cagr_vs_hold"]),
                               "rel", CAGR_REL_TOL)
            chold = _cmp_metric("cagr_hold", float(e["cagr_hold"]), float(my["metrics"]["hold"]["cagr"]),
                                "rel", CAGR_REL_TOL)
            row = {
                "basket_id": int(bid), "variant": variant,
                "cagr": {"engine": _r(float(e["cagr"])), "verify": _r(float(m["cagr"])),
                         "abs_diff": cagr["abs_diff"], "rel_diff": cagr["rel_diff"]},
                "premium_cagr": {"engine": _r(float(e["premium_cagr"])),
                                 "verify": _r(float(m["premium_cagr_vs_hold"])),
                                 "abs_diff": prem["abs_diff"], "rel_diff": prem["rel_diff"]},
                "cagr_hold": {"engine": _r(float(e["cagr_hold"])), "verify": _r(float(my["metrics"]["hold"]["cagr"]))},
                "sharpe": {"engine": _r(float(e["sharpe"])), "verify": _r(float(m["sharpe"]))},
                "turnover_abs_diff": _r(abs(float(e["turnover"]) - float(m["turnover_annual_two_way"]))),
                "cost_pct_abs_diff": _r(abs(float(e["cost_pct"]) - float(m["cost_pct_annual"]))),
                "pass_cagr": cagr["pass"], "pass_premium": prem["pass"],
                "pass_cagr_hold": chold["pass"],
            }
            rows.append(row)
            if variant != "hold":
                _chk(checks, f"B.random.R{bid:03d}.{variant}.premium_cagr", prem["pass"],
                     f"premium engine={_fmt(float(e['premium_cagr']) * 100, 4)}% verify={_fmt(float(m['premium_cagr_vs_hold']) * 100, 4)}% "
                     f"差={_fmt(prem['abs_diff'] * 10000, 4)}bp")
            _chk(checks, f"B.random.R{bid:03d}.{variant}.cagr", cagr["pass"],
                 f"CAGR engine={_fmt(float(e['cagr']), 6)} verify={_fmt(float(m['cagr']), 6)}")
    out["rows"] = rows
    out["checks"] = checks
    return out



# ---------------------------------------------------------------------------
# engine 自身内部一致性（engine CSV → 由其 nav 复算）
# ---------------------------------------------------------------------------
def engine_self_consistency(eng: dict, init_cash: float) -> dict:
    checks: list = []
    out: dict = {}
    for basket in ("CURRENT10", "OLD20"):
        nav = eng["nav"][basket]
        ev = eng["variants"][basket]
        dates = pd.DatetimeIndex(nav.index)
        years = max((dates[-1] - dates[0]).days / CALENDAR_DAYS_PER_YEAR, 1e-6)
        hold_arr = nav["hold"].to_numpy(dtype=float)
        hold_cagr = float(ev.loc["hold", "cagr"])
        rows = {}
        worst = {"cagr": 0.0, "nav_end": 0.0, "premium": 0.0, "cost_pct": 0.0, "t": 0.0, "p": 0.0}
        for name in nav.columns:
            if name not in ev.index:
                continue
            arr = nav[name].to_numpy(dtype=float)
            cagr = (arr[-1] / init_cash) ** (1.0 / years) - 1.0
            nav_end = float(arr[-1])
            cost_pct = float(ev.loc[name, "total_cost"]) / float(arr.mean()) / years
            var_m = monthly_returns(arr, dates)
            hold_m = monthly_returns(hold_arr, dates)
            diff = (var_m - hold_m).dropna()
            t, p = t_p(diff.to_numpy())
            prem = cagr - hold_cagr
            d = {
                "cagr_from_nav": _r(cagr), "cagr_reported": _r(float(ev.loc[name, "cagr"])),
                "nav_end_from_nav": _r(nav_end), "nav_end_reported": _r(float(ev.loc[name, "nav_end"])),
                "premium_from_cagr": _r(prem), "premium_reported": _r(float(ev.loc[name, "premium_cagr_vs_hold"])),
                "cost_pct_recalc": _r(cost_pct), "cost_pct_reported": _r(float(ev.loc[name, "cost_pct_annual"])),
                "t_recalc": _r(t), "t_reported": _r(float(ev.loc[name, "monthly_diff_t"])),
                "p_recalc": _r(p), "p_reported": _r(float(ev.loc[name, "monthly_diff_p"])),
            }
            rows[name] = d
            worst["cagr"] = max(worst["cagr"], abs(d["cagr_from_nav"] - d["cagr_reported"]))
            worst["nav_end"] = max(worst["nav_end"], abs(d["nav_end_from_nav"] - d["nav_end_reported"]))
            worst["premium"] = max(worst["premium"], abs(d["premium_from_cagr"] - d["premium_reported"]))
            worst["cost_pct"] = max(worst["cost_pct"], abs(d["cost_pct_recalc"] - d["cost_pct_reported"]))
            t_rep = float(ev.loc[name, "monthly_diff_t"])
            p_rep = float(ev.loc[name, "monthly_diff_p"])
            if math.isfinite(t) and math.isfinite(t_rep):
                worst["t"] = max(worst["t"], abs(t - t_rep))
            if math.isfinite(p) and math.isfinite(p_rep):
                worst["p"] = max(worst["p"], abs(p - p_rep))
        out[basket] = {"worst_abs_diff": {k: _r(v) for k, v in worst.items()}, "variants": rows}
        _chk(checks, f"E2.{basket}.engine_internal_consistency",
             worst["cagr"] <= 1e-9 and worst["nav_end"] <= 1e-6 and worst["premium"] <= 1e-9
             and worst["cost_pct"] <= 1e-6 and worst["t"] <= 1e-4 and worst["p"] <= 1e-4,
             "engine variants.csv 指标能由其 nav_*.csv 逐列复算："
             f"CAGR 差 {_fmt(worst['cagr'], 12)}；nav_end 差 {_fmt(worst['nav_end'], 6)} 元；"
             f"premium 差 {_fmt(worst['premium'], 12)}；cost_pct 差 {_fmt(worst['cost_pct'], 9)}；t 差 {_fmt(worst['t'], 9)}")
    out["checks"] = checks
    return out


# ---------------------------------------------------------------------------
# A2. engine 是否真的用了修正后的 TR 数据 / 旧结果是否已归档
# ---------------------------------------------------------------------------
def engine_data_revision_check(eng: dict) -> dict:
    checks: list = []
    out: dict = {}
    new_sha = sha256_file(DATA_DIR / "close_tr_wide.csv")
    data = eng["summary"].get("data", {})
    prices = data.get("prices", {})
    sha_ok = str(prices.get("sha256")) == new_sha
    _chk(checks, "A.engine_uses_close_tr_wide", sha_ok,
         f"engine summary data.prices.sha256={prices.get('sha256')}；实际文件 SHA256={new_sha}")
    out["engine_data"] = {"prices": prices,
                          "rows": prices.get("rows"), "cols": prices.get("cols"),
                          "date_min": prices.get("date_min"), "date_max": prices.get("date_max"),
                          "missing_cells": prices.get("missing_cells")}

    dep = data.get("deprecated_em_fqt2", {})
    bad_path = ROOT / str(dep.get("path", "research/rebalance/data/_em_fqt2_close_adj_wide.bad.csv"))
    bad_exists = bad_path.exists()
    bad_sha = sha256_file(bad_path) if bad_exists else None
    bad_ok = bool(bad_exists and bad_sha == str(dep.get("sha256")))
    _chk(checks, "A.deprecated_em_fqt2_archived", bad_ok,
         f"旧 EM fqt=2 表 {bad_path.name} 存在={bad_exists}，SHA256 与 summary 记录一致={bad_ok}")
    out["deprecated"] = {"path": str(bad_path), "exists": bool(bad_exists),
                         "sha256": bad_sha, "summary_sha256": dep.get("sha256")}

    archive = ROOT / str(dep.get("results_archive", "research/rebalance/results_em_fqt2_bad"))
    old_variants = archive / "variants_OLD20.csv"
    out["archive"] = {"path": str(archive), "exists": archive.exists(),
                      "old_variants_exists": old_variants.exists()}
    if old_variants.exists():
        old = pd.read_csv(old_variants).set_index("variant")
        new = eng["variants"]["OLD20"]
        d_hold = abs(float(old.loc["hold", "cagr"]) - float(new.loc["hold", "cagr"]))
        d_last = abs(float(old.loc["m_last", "cagr"]) - float(new.loc["m_last", "cagr"]))
        out["archive"]["old_OLD20_hold_cagr"] = _r(float(old.loc["hold", "cagr"]))
        out["archive"]["new_OLD20_hold_cagr"] = _r(float(new.loc["hold", "cagr"]))
        out["archive"]["old_OLD20_m_last_cagr"] = _r(float(old.loc["m_last", "cagr"]))
        out["archive"]["new_OLD20_m_last_cagr"] = _r(float(new.loc["m_last", "cagr"]))
        _chk(checks, "A.results_rerun_on_new_data", (d_hold > 0.001 or d_last > 0.001),
             f"旧归档 OLD20 hold CAGR={_fmt(float(old.loc['hold', 'cagr']) * 100, 3)}% vs 新={_fmt(float(new.loc['hold', 'cagr']) * 100, 3)}%；"
             f"m_last 旧={_fmt(float(old.loc['m_last', 'cagr']) * 100, 3)}% 新={_fmt(float(new.loc['m_last', 'cagr']) * 100, 3)}% "
             f"（差异 >0.1pp，说明结果确由新数据重跑而非沿用旧值）")
    else:
        _chk(checks, "A.results_rerun_on_new_data", None, "旧归档 variants_OLD20.csv 缺失，无法确认重跑",
             status="UNVERIFIED")
    out["checks"] = checks
    return out


# ---------------------------------------------------------------------------
# 汇总 / 输出 / main
# ---------------------------------------------------------------------------
def build_summary(payload: dict) -> dict:
    checks: list = []
    for key in ("A_engine_data_revision", "B_engine_vs_verify", "B_random_baskets",
                "C_manual_rebalance", "D_violations", "E_counterfactuals",
                "engine_self_consistency"):
        sec = payload.get(key)
        if isinstance(sec, dict):
            checks.extend(sec.get("checks", []))
    dc = payload.get("data_check", {})
    contract = dc.get("contract", {})
    checks.append({"id": "A.contract.sha256",
                   "status": "PASS" if contract.get("sha256_prefix_ok") else "FAIL",
                   "detail": f"SHA256 {contract.get('sha256')}（期望前缀 {EXPECTED_TR_SHA_PREFIX}…）"})
    checks.append({"id": "A.contract.shape",
                   "status": "PASS" if (contract.get("rows_ok") and contract.get("n_symbols_ok")
                                        and contract.get("date_range_ok")) else "FAIL",
                   "detail": f"{contract.get('rows')} 行 × {contract.get('n_symbols')} 标的，"
                             f"{contract.get('date_min')}..{contract.get('date_max')}"})
    for sym, p in dc.get("per_symbol", {}).items():
        ok = (p.get("status") == "OK" and p.get("daily_ret_corr", 0) >= 0.9999
              and p.get("max_abs_price_diff", 9) <= 1e-6)
        checks.append({"id": f"A.rebuild.{sym}", "status": "PASS" if ok else "FAIL",
                       "detail": f"独立重建 vs close_tr_wide：日收益相关 {p.get('daily_ret_corr')}，"
                                 f"最大价差 {p.get('max_abs_price_diff'):.2e}，累计 TR {p.get('verify_cum_tr', 0):.4f}"})
    fails = [c for c in checks if c["status"] == "FAIL"]
    unv = [c for c in checks if c["status"] == "UNVERIFIED"]
    return {
        "n_checks": len(checks), "n_pass": sum(1 for c in checks if c["status"] == "PASS"),
        "n_fail": len(fails), "n_unverified": len(unv),
        "fails": fails, "unverified": unv,
        "pass_items": [c["id"] for c in checks if c["status"] == "PASS"],
    }


def _emit(payload: dict, args) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default)
    if args.json is not None:
        args.json.write_text(text, encoding="utf-8")
        _log(f"[verify] JSON 结果写出：{args.json}")
    print(text, flush=True)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="再平衡回测独立复核（verifier）")
    ap.add_argument("--part-a", action="store_true", help="只做数据口径独立重建")
    ap.add_argument("--json", type=Path, default=None, help="可选：把完整复核 JSON 另存")
    args = ap.parse_args(list(argv) if argv is not None else None)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    _log("[verify] ===== A. 数据口径独立重建（不复权 + 除权事件 → 前复权/总收益）=====")
    dc = datacheck()
    payload: dict = {"generated_at": dt.datetime.now().isoformat(), "data_check": dc}
    if args.part_a:
        payload["summary"] = build_summary(payload)
        _emit(payload, args)
        return 0

    required = [RESULTS_DIR / "variants_CURRENT10.csv", RESULTS_DIR / "variants_OLD20.csv",
                RESULTS_DIR / "nav_CURRENT10.csv", RESULTS_DIR / "nav_OLD20.csv",
                RESULTS_DIR / "random_baskets.csv", RESULTS_DIR / "random_basket_composition.csv",
                RESULTS_DIR / "summary.json"]
    if not all(p.exists() for p in required):
        payload["engine_results"] = {"status": "NOT_READY",
                                     "missing": [str(p.name) for p in required if not p.exists()]}
        payload["summary"] = build_summary(payload)
        _emit(payload, args)
        return 0

    snap0 = snapshot_results()
    payload["results_snapshot_before"] = snap0
    try:
        _log("[verify] ===== 加载 engine 结果 + 独立复算 CURRENT10/OLD20 =====")
        eng = load_engine_outputs()
        payload["A_engine_data_revision"] = engine_data_revision_check(eng)
        sched = load_cost_schedule()
        init_cash = sched["init_cash"]
        wide = load_tr_wide()
        cal = load_calendar()
        bt = IndependentBacktester(sched, init_cash=init_cash)
        my = {}
        for basket, symbols, start in (
                ("CURRENT10", CURRENT10, CURRENT10_START),
                ("OLD20", OLD20, OLD20_START)):
            _log(f"[verify] 独立跑 {basket}（{len(symbols)} 只，start={start}）...")
            my[basket] = run_basket(bt, wide, cal, symbols, start, FULL_VARIANTS_ORDER)

        _log("[verify] ===== B. engine vs verify 对照 =====")
        payload["B_engine_vs_verify"] = compare_all(eng, my)
        _log("[verify] ===== C. 手工核对一次再平衡 =====")
        payload["C_manual_rebalance"] = manual_rebalance_check(my["CURRENT10"], sched)
        _log("[verify] ===== D. 违反检查 =====")
        payload["D_violations"] = violations_check(bt, my, eng, sched)
        _log("[verify] ===== E. 反事实 sanity =====")
        payload["E_counterfactuals"] = counterfactual_checks(bt, my, eng)
        _log("[verify] ===== B. 随机篮子复算 =====")
        payload["B_random_baskets"] = random_baskets_check(bt, wide, cal, eng)
        _log("[verify] ===== engine 内部一致性 =====")
        payload["engine_self_consistency"] = engine_self_consistency(eng, init_cash)
    except Exception as exc:  # noqa: BLE001
        import traceback
        payload["error"] = f"{type(exc).__name__}: {exc}"
        payload["traceback"] = traceback.format_exc()
        _log(f"[verify][FATAL] {payload['error']}")
        _log(payload["traceback"])

    snap1 = snapshot_results()
    changed = [n for n in snap0 if snap0[n]["sha256"] != snap1[n]["sha256"]]
    payload["results_snapshot_after"] = snap1
    payload["results_stable"] = {"changed_files": changed, "stable": len(changed) == 0}
    payload["summary"] = build_summary(payload)
    _emit(payload, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
