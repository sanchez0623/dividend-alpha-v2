#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dividend-alpha-v2 / research/dividend_capture —— DC 分红捕获回测引擎

设计原则：
  1. **复用生产纯逻辑**：`src/da_strategy/dividend_capture.py` 的
     `DcRules / evaluate_candidate / rank_candidates / entry_date_for / exit_date_for /
      ttm_dividend / settle_cycle / dividend_tax_for` 全部直接调用，回测与生产同一套规则；
     成本用 `src/da_core/cost.py::buy_cost/sell_cost`（参数读 config/strategy.yaml cost 段）。
  2. **不复权价成交 + 显式分红**：用不复权收盘价买入/卖出/估值；除权日按
     `dps × 股数` 把税前分红记入现金、按 `per_share_bonus` 扩股；卖出时按实际持有自然日
     扣红利税（≤30天 20% / 31~365天 10% / >365天 0%），再提取净分红的 30%。
  3. **无未来数据**：候选只用 entry 日及之前的不复权价、前复权序列（前复权由除权事件
     反推，不含未来事件）与 entry 日市值估算；买入=entry 日收盘，卖出=exit 日收盘。
  4. **离线可复现**：输入只有 data/ 下缓存产出的 parquet/csv + config，输出确定性。

关键词口径：
  - 一次只持有一只（max_concurrent_positions 可调），持仓重叠的候选计入 skipped_overlap。
  - 买入日无 bar（停牌）→ 跳过该候选；卖出日无 bar → 顺延到下一有 bar 的交易日。
  - 涨跌停不建模（按收盘价成交），报告披露。
"""
from __future__ import annotations

import bisect
import json
import math
import os
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
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

DATA = HERE / "data"
RESULTS = HERE / "results"
BACKTEST_START = date(2016, 1, 4)
BACKTEST_END = date(2026, 11, 6)
CAL_SRC = ROOT / "research" / "rebalance" / "data" / "index_000300.csv"
POOL62_CACHE = ROOT / "research" / "rebalance" / "data" / ".ths_cache_v2"

# ---- point-in-time（新主口径，task-3）----
PIT_BARS = DATA / "bars_all.parquet"
PIT_DELISTED_BARS = DATA / "bars_delisted.parquet"
PIT_EVENTS = DATA / "events_all.parquet"
PIT_SHARES = DATA / "shares_history.parquet"
PIT_UNIVERSE = DATA / "pit_universe.csv"
SPOT_ALL = DATA / "spot_all.csv"
DELISTED_LIST = DATA / "delisted_list.csv"
# ---- legacy（旧口径：当前市值≥500 亿筛池 + 当前股本反推，仅作对照）----
LEGACY_BARS = DATA / "bars_raw.parquet"
LEGACY_EVENTS = DATA / "events.parquet"
LEGACY_UNIVERSE = DATA / "universe.csv"
LEGACY_SPOT = DATA / "spot_snapshot.csv"

# 数据防护：疑似数据错误 bar 的判定（见 DATA_QUALITY.md §5.1）
#   非除权事件日 + 相邻 bar 间隔 <= 10 自然日（非停牌复牌）+ |单日收益| > 30%
# → 视为数据源异常，候选买入/计划卖出落在该 bar 上则跳过；持仓估值/延迟卖出用最近可信价。
ANOMALY_RET_THRESHOLD = 0.30
ANOMALY_MAX_GAP_DAYS = 10
RESUMPTION_GAP_DAYS = 30       # 相邻 bar 间隔 > 30 天视为长期停牌后的复牌跳变（真实）

from src.da_core.cost import buy_cost, sell_cost  # noqa: E402
from src.da_strategy.dividend_capture import (  # noqa: E402
    DcCandidate,
    DcRules,
    TREND_PRIORITY,
    entry_date_for,
    evaluate_candidate,
    exit_date_for,
    rank_candidates,
    settle_cycle,
    ttm_dividend,
)


def detect_anomalies(bars: dict[date, float], events: list[dict]) -> set[date]:
    """返回疑似数据错误 bar 日期集合（规则见上方常量注释）。"""
    ds = sorted(bars)
    if len(ds) < 2:
        return set()
    ev_dates = {e["date"] for e in events}
    out: set[date] = set()
    for i in range(1, len(ds)):
        d0, d1 = ds[i - 1], ds[i]
        c0, c1 = float(bars[d0]), float(bars[d1])
        if c0 <= 0 or c1 <= 0:
            continue
        gap = (d1 - d0).days
        ret = c1 / c0 - 1.0
        if abs(ret) > ANOMALY_RET_THRESHOLD and gap <= ANOMALY_MAX_GAP_DAYS and d1 not in ev_dates:
            out.add(d1)
    return out


# ---------------------------------------------------------------------------
# 趋势记忆化（性能优化：趋势只依赖 (symbol, entry_date)，ma/rsi 参数跨变体固定）
# 只在本进程 build_candidates 期间启用；启用时行为与生产 compute_trend 完全一致。
# ---------------------------------------------------------------------------
_TREND_CACHE: dict = {}
_TREND_CACHE_ON = False
_TREND_CACHE_DS = None
_ORIG_COMPUTE_TREND = None


def _install_trend_memo() -> None:
    global _ORIG_COMPUTE_TREND
    import src.da_strategy.dividend_capture as m

    if getattr(m, "_dc_trend_memo_installed", False):
        _ORIG_COMPUTE_TREND = m._dc_trend_orig
        return
    orig = m.compute_trend
    _ORIG_COMPUTE_TREND = orig

    def memo(adj_series, asof, rules):
        if not _TREND_CACHE_ON:
            return orig(adj_series, asof, rules)
        name = getattr(adj_series, "name", None) if adj_series is not None else None
        if name is None:
            return orig(adj_series, asof, rules)
        key = (name, asof)
        v = _TREND_CACHE.get(key)
        if v is None:
            v = orig(adj_series, asof, rules)
            _TREND_CACHE[key] = v
        return v

    m._dc_trend_orig = orig
    m._dc_trend_memo_installed = True
    m.compute_trend = memo


def _enable_trend_cache(ds_id: int) -> None:
    global _TREND_CACHE_ON, _TREND_CACHE_DS
    if _TREND_CACHE_DS != ds_id:
        _TREND_CACHE.clear()
        _TREND_CACHE_DS = ds_id
    _TREND_CACHE_ON = True


def _disable_trend_cache() -> None:
    global _TREND_CACHE_ON
    _TREND_CACHE_ON = False



# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def load_configs():
    from src.da_core.config import load_strategy
    from src.da_core.dc_config import load_dividend_capture

    return load_strategy(), load_dividend_capture()


def sha256_of(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_date(x) -> date:
    if isinstance(x, date):
        return x
    return datetime.strptime(str(x)[:10], "%Y-%m-%d").date()


def read_calendar() -> list[date]:
    df = pd.read_csv(DATA / "calendar.csv")
    out = [parse_date(x) for x in df["date"]]
    if out != sorted(set(out)):
        raise ValueError("calendar.csv 重复/乱序")
    return out


# ---------------------------------------------------------------------------
# 数据集
# ---------------------------------------------------------------------------
@dataclass
class Dataset:
    kind: str
    symbols: list[str]
    calendar: list[date]
    bars: dict[str, dict[date, float]]              # 不复权收盘（成交/估值用实际值）
    events: dict[str, list[dict]]                   # 按 ex_date 聚合: {date,dps,bonus}
    names: dict[str, str]
    cap_now: dict[str, float]                       # 当前总市值（元；退市股缺失）
    price_now: dict[str, float]                     # 估算用"最新不复权价"
    shares_now: dict[str, float]                    # 估算当前总股本（fallback 用）
    cap_mode: str = "estimated"                     # pit | estimated
    shares_hist: dict[str, tuple[list[date], list[float]]] = field(default_factory=dict)
    adj: dict[str, pd.Series] = field(default_factory=dict)      # 前复权（清洗后价格）
    clean_bars: dict[str, dict[date, float]] = field(default_factory=dict)  # 异常 bar → 最近可信价
    anomaly: dict[str, set[date]] = field(default_factory=dict)  # 疑似数据错误 bar
    _dates: dict[str, list[date]] = field(default_factory=dict, repr=False)
    _closes: dict[str, list[float]] = field(default_factory=dict, repr=False)
    _bonus_dates: dict[str, list[date]] = field(default_factory=dict, repr=False)
    _bonus_suffix: dict[str, list[float]] = field(default_factory=dict, repr=False)

    def finalize(self) -> "Dataset":
        for s in self.symbols:
            d = self.bars.get(s) or {}
            events = self.events.get(s) or []
            anom = detect_anomalies(d, events)
            clean: dict[date, float] = {}
            last_trusted: float | None = None
            for x in sorted(d):
                if x in anom:
                    clean[x] = last_trusted if last_trusted is not None else float(d[x])
                else:
                    clean[x] = float(d[x])
                    last_trusted = float(d[x])
            self.anomaly[s] = anom
            self.clean_bars[s] = clean
            self.adj[s] = _build_adj(s, clean, events)
            ds = sorted(clean)
            self._dates[s] = ds
            self._closes[s] = [float(clean[x]) for x in ds]
            evs = sorted([(e["date"], 1.0 + float(e["bonus"]))
                          for e in events if float(e["bonus"]) > 0])
            dates = [e[0] for e in evs]
            suffix = [1.0] * (len(evs) + 1)
            for i in range(len(evs) - 1, -1, -1):
                suffix[i] = suffix[i + 1] * evs[i][1]
            self._bonus_dates[s] = dates
            self._bonus_suffix[s] = suffix
        return self

    # ---- 历史总股本（point-in-time step function）----
    def shares_at(self, symbol: str, d: date) -> float | None:
        pair = self.shares_hist.get(symbol)
        if not pair:
            return None
        dates, vals = pair
        i = bisect.bisect_right(dates, d) - 1
        if i < 0:
            return None
        v = float(vals[i])
        return v if v > 0 else None

    def cap_source(self, symbol: str, d: date) -> str:
        if self.cap_mode == "pit" and self.shares_at(symbol, d) is not None:
            return "pit_shares"
        if self.shares_now.get(symbol) and np.isfinite(self.shares_now[symbol]):
            return "est_current_shares"
        return "none"

    # 精确某日不复权收盘（无 bar 返回 None）—— 成交价的真实值
    def price_on(self, symbol: str, d: date) -> float | None:
        return (self.bars.get(symbol) or {}).get(d)

    # 估值/延迟卖出用：异常 bar 返回最近可信价（已清洗）
    def trusted_price_on(self, symbol: str, d: date) -> float | None:
        return (self.clean_bars.get(symbol) or {}).get(d)

    def is_anomaly(self, symbol: str, d: date) -> bool:
        return d in (self.anomaly.get(symbol) or set())

    # <= d 的最近一根可信收盘（停牌期间估值用；异常 bar 已清洗）
    def last_price(self, symbol: str, d: date) -> float | None:
        ds = self._dates.get(symbol) or []
        if not ds:
            return None
        i = bisect.bisect_right(ds, d) - 1
        if i < 0:
            return None
        return self._closes[symbol][i]

    # 买入日总市值：
    #   pit 模式：优先 total_shares_at(t) × raw_close_t（akshare/东财历史总股本）
    #   estimated 模式（旧口径/缺失回退）：当前总股本估算 / Π(1+送股, ex_date>t) × price_t
    def cap_on(self, symbol: str, d: date, price: float) -> float:
        if self.cap_mode == "pit":
            sh = self.shares_at(symbol, d)
            if sh is not None:
                return float(sh) * float(price)
        shares_now = self.shares_now.get(symbol)
        if not shares_now or not np.isfinite(shares_now):
            return 0.0
        dates = self._bonus_dates.get(symbol) or []
        suffix = self._bonus_suffix.get(symbol) or [1.0]
        i = bisect.bisect_right(dates, d)
        mult = suffix[i] if i < len(suffix) else 1.0
        return float(shares_now) * float(price) / float(mult)


def _aggregate_events(raw_events: list[dict]) -> list[dict]:
    """同一 (symbol, ex_date) 的多行（如分红+送股分行）合并：dps/bonus 各自求和。"""
    by: dict[date, dict] = {}
    for e in raw_events:
        d = e["date"]
        cur = by.setdefault(d, {"date": d, "dps": 0.0, "bonus": 0.0})
        cur["dps"] += float(e["dps"])
        cur["bonus"] += float(e["bonus"])
    return [by[d] for d in sorted(by)]


def _build_adj(symbol: str, bars: dict[date, float], events: list[dict]) -> pd.Series:
    """用生产同款 build_adj_factor/adj_close_series 构造前复权序列（date 索引）。"""
    from src.da_data.adjust import CorpAction, adj_close_series

    if not bars:
        return pd.Series(dtype="float64")
    ds = sorted(bars)
    raw = pd.Series([float(bars[d]) for d in ds], index=ds, dtype="float64")
    acts = [CorpAction(symbol=symbol, ex_date=e["date"], dps=float(e["dps"]),
                       bonus_ratio=float(e["bonus"])) for e in events]
    try:
        adj = adj_close_series(raw, acts)
        adj.name = symbol
        return adj
    except Exception as e:  # noqa: BLE001
        log(f"⚠ {symbol} 前复权失败，退化为不复权: {e}")
        raw.name = symbol
        return raw


def load_legacy_dataset() -> Dataset:
    """旧口径数据集（当前市值≥500 亿筛池 + 当前股本反推市值），仅作新旧对照。"""
    uni = pd.read_csv(DATA / "universe.csv")
    bars_df = pd.read_parquet(DATA / "bars_raw.parquet")
    ev_df = pd.read_parquet(DATA / "events.parquet")
    calendar = read_calendar()
    symbols = list(uni["symbol"])
    names = {r.symbol: (r.name if isinstance(r.name, str) else "")
             for r in uni.itertuples()}
    cap_now = {r.symbol: float(r.market_cap_now) for r in uni.itertuples()}

    bars: dict[str, dict[date, float]] = defaultdict(dict)
    for r in bars_df.itertuples():
        d = r.date if isinstance(r.date, date) else parse_date(r.date)
        bars[r.symbol][d] = float(r.close)

    raw_ev: dict[str, list[dict]] = defaultdict(list)
    for r in ev_df.itertuples():
        d = r.ex_date if isinstance(r.ex_date, date) else parse_date(r.ex_date)
        raw_ev[r.symbol].append({"date": d, "dps": float(r.dps), "bonus": float(r.bonus)})
    events = {s: _aggregate_events(raw_ev.get(s, [])) for s in symbols}

    price_now, shares_now = {}, {}
    for s in symbols:
        d = bars.get(s) or {}
        p = float(d[max(d)]) if d else float("nan")
        price_now[s] = p
        shares_now[s] = (cap_now[s] / p) if p and p > 0 else float("nan")

    return Dataset(kind="legacy_current_cap", symbols=symbols, calendar=calendar,
                   bars=dict(bars), events=events, names=names, cap_now=cap_now,
                   price_now=price_now, shares_now=shares_now,
                   cap_mode="estimated").finalize()


def load_pit_dataset(symbols: list[str] | None = None) -> Dataset:
    """point-in-time 数据集（新主口径，task-3）。

    候选 = pit_universe.csv（有历史总股本 ∩ 有除权事件）中的全部标的；
    市值 = total_shares_at(买入日) × 不复权收盘；缺失时回退旧估算（并计数）。
    """
    pit = pd.read_csv(PIT_UNIVERSE)
    all_syms = list(pit["symbol"])
    want = set(all_syms) if symbols is None else (set(symbols) & set(all_syms))
    symbols = [s for s in all_syms if s in want]
    calendar = read_calendar()
    cal_set = set(calendar)

    bars_df = pd.read_parquet(PIT_BARS)
    bars_df = bars_df[bars_df["symbol"].isin(symbols)]
    bars: dict[str, dict[date, float]] = defaultdict(dict)
    for r in bars_df.itertuples():
        d = r.date if isinstance(r.date, date) else parse_date(r.date)
        if d in cal_set:
            bars[r.symbol][d] = float(r.close)
    if PIT_DELISTED_BARS.exists():
        dlb = pd.read_parquet(PIT_DELISTED_BARS)
        dlb = dlb[dlb["symbol"].isin(symbols)]
        for r in dlb.itertuples():
            d = r.date if isinstance(r.date, date) else parse_date(r.date)
            if d in cal_set:
                bars[r.symbol][d] = float(r.close)

    ev_df = pd.read_parquet(PIT_EVENTS)
    ev_df = ev_df[ev_df["symbol"].isin(symbols)]
    raw_ev: dict[str, list[dict]] = defaultdict(list)
    for r in ev_df.itertuples():
        d = r.ex_date if isinstance(r.ex_date, date) else parse_date(r.ex_date)
        raw_ev[r.symbol].append({"date": d, "dps": float(r.dps), "bonus": float(r.bonus)})
    events = {s: _aggregate_events(raw_ev.get(s, [])) for s in symbols}

    sh_df = pd.read_parquet(PIT_SHARES)
    sh_df = sh_df[sh_df["symbol"].isin(symbols)].sort_values(["symbol", "change_date"])
    shares_hist: dict[str, tuple[list[date], list[float]]] = {}
    for s, g in sh_df.groupby("symbol"):
        dates = [r if isinstance(r, date) else parse_date(r) for r in g["change_date"]]
        vals = [float(v) for v in g["total_shares"]]
        # 同日去重（保留最后一条）
        dedup: dict[date, float] = {}
        for d, v in zip(dates, vals):
            dedup[d] = v
        dd = sorted(dedup)
        shares_hist[s] = (dd, [dedup[x] for x in dd])

    spot = pd.read_csv(SPOT_ALL)
    spot["market_cap"] = pd.to_numeric(spot["market_cap"], errors="coerce")
    spot["last"] = pd.to_numeric(spot["last"], errors="coerce")
    spot_by = {r.symbol: r for r in spot.itertuples()}
    names, cap_now, price_now, shares_now = {}, {}, {}, {}
    for s in symbols:
        d = bars.get(s) or {}
        p = float(d[max(d)]) if d else float("nan")
        price_now[s] = p
        row = spot_by.get(s)
        nm = getattr(row, "name", None) if row is not None else None
        names[s] = str(nm) if isinstance(nm, str) else ""
        cap = float(row.market_cap) if row is not None and np.isfinite(float(row.market_cap)) else float("nan")
        cap_now[s] = cap
        shares_now[s] = (cap / p) if p and p > 0 else float("nan")
    # 退市股名称
    if DELISTED_LIST.exists():
        dl = pd.read_csv(DELISTED_LIST)
        for r in dl.itertuples():
            if r.symbol in names and not names.get(r.symbol):
                names[r.symbol] = str(r.name)

    return Dataset(kind="pit_all", symbols=symbols, calendar=calendar,
                   bars=dict(bars), events=events, names=names, cap_now=cap_now,
                   price_now=price_now, shares_now=shares_now, cap_mode="pit",
                   shares_hist=shares_hist).finalize()


# 兼容旧调用名
load_full_dataset = load_legacy_dataset


def load_pool62_dataset(pool62: list[str]) -> Dataset:
    """从 research/rebalance 的 .ths_cache_v2 缓存构建 62 只池数据集（只读复用）。"""
    import data_fetch as dfetch  # 同目录解析器复用

    spot = pd.read_csv(DATA / "spot_snapshot.csv")
    spot["market_cap"] = pd.to_numeric(spot["market_cap"], errors="coerce")
    spot["last"] = pd.to_numeric(spot["last"], errors="coerce")
    spot_by_sym = {r.symbol: r for r in spot.itertuples()}

    raw_bars: dict[str, dict[date, float]] = defaultdict(dict)
    for p in sorted((POOL62_CACHE / "raw").glob("raw_*.json")):
        obj = json.loads(p.read_text(encoding="utf-8"))
        # rebalance 缓存：顶层就是 {interval, adjust, item}；data_fetch 缓存多一层 payload
        payload = obj.get("payload") if isinstance(obj, dict) and "payload" in obj else obj
        for sym, d in dfetch.parse_raw(payload).items():
            raw_bars[sym].update(d)
    raw_ev: dict[str, list[dict]] = defaultdict(list)
    for p in sorted((POOL62_CACHE / "ca").glob("ca_*.json")):
        obj = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(obj, list):
            rows = obj
            ths = p.stem.replace("ca_", "").replace("_", ".")
        else:
            rows = obj.get("payload") or []
            ths = obj.get("thscode") or p.stem.replace("ca_", "").replace("_", ".")
        sym = None
        if "." in ths:
            code, exch = ths.split(".", 1)
            sym = exch.lower() + code
        if sym is None:
            continue
        raw_ev[sym].extend({"date": e["ex_date"], "dps": e["dps"], "bonus": e["bonus"]}
                           for e in dfetch.parse_ca(rows))

    calendar = read_calendar()
    cal_set = set(calendar)
    available = [s for s in pool62 if s in raw_bars and len(raw_bars[s]) > 0]
    missing = [s for s in pool62 if s not in available]
    if missing:
        log(f"pool62：{len(missing)} 只无缓存数据 → 排除 {missing}")
    symbols = available
    names, cap_now, price_now, shares_now = {}, {}, {}, {}
    for s in symbols:
        d = {k: v for k, v in raw_bars[s].items() if k in cal_set}
        raw_bars[s] = d
        p = float(d[max(d)]) if d else float("nan")
        price_now[s] = p
        row = spot_by_sym.get(s)
        if row is None or not np.isfinite(float(row.market_cap)):
            cap_now[s] = float("nan")
            shares_now[s] = float("nan")
            names[s] = ""
        else:
            cap_now[s] = float(row.market_cap)
            shares_now[s] = cap_now[s] / p if p and p > 0 else float("nan")
            names[s] = str(row.name) if isinstance(row.name, str) else ""
    events = {s: _aggregate_events(raw_ev.get(s, [])) for s in symbols}
    bars = {s: raw_bars[s] for s in symbols}
    return Dataset(kind="pool62", symbols=symbols, calendar=calendar, bars=bars,
                   events=events, names=names, cap_now=cap_now,
                   price_now=price_now, shares_now=shares_now).finalize()


# ---------------------------------------------------------------------------
# 候选构建
# ---------------------------------------------------------------------------
REASON_CATEGORY = {"yield": "yield", "cap": "cap", "trend": "trend"}


def reason_category(reason: str) -> str:
    for k in REASON_CATEGORY:
        if reason.startswith(k):
            return REASON_CATEGORY[k]
    return "other"


def build_candidates(ds: Dataset, rules: DcRules, start: date = BACKTEST_START,
                     end: date = BACKTEST_END) -> tuple[list[DcCandidate], dict]:
    """逐除权事件评估候选（entry/exit/市值/趋势/TTM 股息率）。"""
    cal = ds.calendar
    cands: list[DcCandidate] = []
    stats = Counter()
    for sym in ds.symbols:
        events = ds.events.get(sym) or []
        cash_events = [e for e in events if e["dps"] > 0]
        if not cash_events:
            continue
        ttm_series = [(e["date"], float(e["dps"])) for e in events]
        adj = ds.adj.get(sym)
        for ev in cash_events:
            ex = ev["date"]
            entry = entry_date_for(ex, cal, rules.entry_lead_trading_days)
            exit_ = exit_date_for(ex, cal, rules.hold_days_after_ex)
            if entry is None or exit_ is None:
                stats["rejected_no_entry_or_exit"] += 1
                continue
            if entry < start or entry > end:
                stats["out_of_window"] += 1
                continue
            # 数据防护：买入日/计划卖出日落在疑似数据错误 bar 上 → 跳过该笔
            if ds.is_anomaly(sym, entry):
                stats["skipped_data_entry_bar"] += 1
                continue
            if ds.is_anomaly(sym, exit_):
                stats["skipped_data_exit_bar"] += 1
                continue
            price = ds.price_on(sym, entry)
            if price is None or price <= 0:
                stats["rejected_no_entry_bar"] += 1
                continue
            ttm = ttm_dividend(ttm_series, ex)
            cap = ds.cap_on(sym, entry, price)
            if ds.cap_mode == "pit":
                stats["cap_source_" + ds.cap_source(sym, entry)] += 1
            cand = evaluate_candidate(
                symbol=sym, name=ds.names.get(sym), ex_date=ex,
                dps=float(ev["dps"]), ttm_dps=ttm, market_cap=cap,
                calendar=cal, raw_price=price, adj_series=adj, asof=entry,
                rules=rules,
            )
            if cand is None:
                stats["rejected_eval_none"] += 1
                continue
            cands.append(cand)
            stats["candidate_events"] += 1
            if not cand.eligible:
                stats["rejected_" + reason_category(cand.reason)] += 1
    return cands, dict(stats)


# ---------------------------------------------------------------------------
# 组合模拟
# ---------------------------------------------------------------------------
@dataclass
class Position:
    symbol: str
    name: str
    shares: float
    entry_date: date
    ex_date: date
    planned_exit: date
    buy_price: float
    buy_amount: float
    buy_cost_total: float
    dps: float
    bonus: float
    div_yield: float
    market_cap: float
    trend: str
    rank: int
    gross_div: float = 0.0
    div_credited: bool = False
    last_price: float = 0.0
    pre_ex_close: float | None = None     # 除权日前最后一个交易日收盘（填权基准）
    ex_close: float | None = None         # 除权日收盘（止损基准）
    ex_idx: int = -1                      # 除权日在日历中的下标
    exit_reason: str = ""


# 打分选池（optimization）：yield + 0.5%*(cap>=500亿) + 0.5%*trend_score
TREND_SCORE = {"up_pull": 1.0, "up": 0.5, "nondown": 0.0, "down": -0.5,
               "insufficient": -1.0}


def candidate_score(c: DcCandidate, score_cfg: dict | None = None) -> float:
    cfg = score_cfg or {}
    cap_bonus = float(cfg.get("cap_bonus", 0.005))
    trend_bonus = float(cfg.get("trend_bonus", 0.005))
    cap_ref = float(cfg.get("cap_ref", 5e10))
    s = float(c.div_yield)
    if c.market_cap >= cap_ref:
        s += cap_bonus
    s += trend_bonus * TREND_SCORE.get(c.trend.label, -1.0)
    return s


def simulate(ds: Dataset, cfg, rules: DcRules, cands: list[DcCandidate],
             start: date = BACKTEST_START, end: date = BACKTEST_END,
             init_cash: float | None = None,
             exit_policy: dict | None = None, rank_mode: str = "pure",
             score_cfg: dict | None = None) -> dict:
    """日频组合模拟：分红入账 → 到期/条件卖出 → 空位买入 → 记净值。

    exit_policy:
      None / {}                         固定除权+30自然日（base）
      {"fixed_trading_days": 22}        固定除权后 22 个交易日
      {"fill": True, ...}               条件卖出（fill/no_fill_days/stop_ref/cover_tax）
    rank_mode: "pure"（趋势→yield→cap→code，生产口径）| "score"（打分排序）
    """
    init_cash = float(init_cash if init_cash is not None else 3_000_000.0)
    policy = exit_policy or {}
    fixed_td = policy.get("fixed_trading_days")
    p_fill = bool(policy.get("fill", False))
    p_no_fill_days = policy.get("no_fill_days")
    p_stop_ref = policy.get("stop_ref")        # "buy" | "ex" | None
    p_stop_pct = float(policy.get("stop_pct", 0.03))
    p_cover_tax = bool(policy.get("cover_tax", False))
    conditional = any([p_fill, p_no_fill_days is not None, p_stop_ref, p_cover_tax])
    full_cal = ds.calendar
    idx_of = {d: i for i, d in enumerate(full_cal)}

    ranked = rank_candidates(cands)
    rank_of = {(c.symbol, c.ex_date): c.rank for c in ranked}
    eligible_by_entry: dict[date, list[DcCandidate]] = defaultdict(list)
    ineligible_by_entry: dict[date, list[DcCandidate]] = defaultdict(list)
    for c in cands:
        (eligible_by_entry if c.eligible else ineligible_by_entry)[c.entry_date].append(c)
    for d in eligible_by_entry:
        if rank_mode == "score":
            eligible_by_entry[d].sort(key=lambda c: (
                -candidate_score(c, score_cfg), -c.div_yield, -c.market_cap, c.symbol))
        else:
            eligible_by_entry[d].sort(key=lambda c: (
                rank_of.get((c.symbol, c.ex_date), 10**9), c.symbol))
    for d in ineligible_by_entry:
        ineligible_by_entry[d].sort(key=lambda c: c.symbol)

    cash = init_cash
    cum_withdrawn = 0.0
    positions: list[Position] = []
    trades: list[dict] = []
    nav_rows: list[dict] = []
    outcomes: Counter = Counter()
    buys_blocked: list[dict] = []
    min_cash = init_cash
    cal = [d for d in ds.calendar if start <= d <= end]

    def _buy_one(c: DcCandidate, d: date) -> bool:
        nonlocal cash
        slots = rules.max_concurrent_positions - len(positions)
        if slots <= 0:
            outcomes["skipped_overlap"] += 1
            return False
        price = ds.price_on(c.symbol, d)
        if price is None or price <= 0:
            outcomes["skipped_no_bar_at_entry"] += 1
            return False
        if ds.is_anomaly(c.symbol, d):
            outcomes["skipped_data_entry_bar"] += 1
            return False
        budget = cash / slots * (1.0 - rules.cash_buffer)
        lot = int(rules.lot_size)
        shares = int(budget / price // lot) * lot
        bc = 0.0
        while shares > 0:
            amount = shares * price
            bc = float(buy_cost(cfg, amount).total)
            if amount + bc <= cash + 1e-9:
                break
            shares -= lot
        if shares <= 0:
            outcomes["skipped_insufficient_cash"] += 1
            buys_blocked.append({"date": d.isoformat(), "symbol": c.symbol,
                                 "reason": "insufficient_cash", "cash": round(cash, 2),
                                 "price": price})
            return False
        amount = shares * price
        cash -= amount + bc
        assert cash >= -1e-6, f"买入后现金为负: {cash}"
        # 除权日下标 / 填权基准（除权前最后交易日收盘）/ 除权日收盘
        ex_i = idx_of.get(c.ex_date, -1)
        pre_close = None
        if ex_i > 0:
            prev_day = full_cal[ex_i - 1]
            pre_close = ds.price_on(c.symbol, prev_day) or ds.last_price(c.symbol, prev_day)
        ex_close = None
        if ex_i >= 0:
            ex_close = ds.price_on(c.symbol, c.ex_date) or ds.last_price(c.symbol, c.ex_date)
        planned = c.exit_date
        if fixed_td is not None:
            ex_i2 = idx_of.get(c.ex_date, -1)
            k = ex_i2 + int(fixed_td)
            if 0 <= k < len(full_cal):
                planned = full_cal[k]
        positions.append(Position(
            symbol=c.symbol, name=c.name or "", shares=float(shares), entry_date=d,
            ex_date=c.ex_date, planned_exit=planned, buy_price=float(price),
            buy_amount=float(amount), buy_cost_total=float(bc), dps=float(c.dps),
            bonus=0.0, div_yield=float(c.div_yield), market_cap=float(c.market_cap),
            trend=c.trend.label, rank=int(rank_of.get((c.symbol, c.ex_date), 0)),
            last_price=float(price), pre_ex_close=pre_close, ex_close=ex_close, ex_idx=ex_i,
        ))
        # 送股比例从事件表补齐（dps 与 bonus 在同一除权事件上）
        for ev in ds.events.get(c.symbol, []):
            if ev["date"] == c.ex_date:
                positions[-1].bonus = float(ev["bonus"])
                break
        outcomes["executed"] += 1
        return True

    def _exit_check(pos: Position, d: date) -> tuple[bool, str]:
        """返回 (是否卖出, 卖出原因)。固定政策只看 planned_exit；条件政策逐日检查。"""
        if fixed_td is not None:
            return (d >= pos.planned_exit), "fixed_td"
        if p_stop_ref == "buy" and d > pos.entry_date:
            p = ds.price_on(pos.symbol, d)
            if p is not None and p > 0 and p <= pos.buy_price * (1.0 - p_stop_pct):
                return True, "stop_buy"
        if d <= pos.ex_date:
            return (d >= pos.planned_exit), "max_hold"
        p = ds.price_on(pos.symbol, d)
        if p is None or p <= 0:
            return (d >= pos.planned_exit), "max_hold"
        tdays = idx_of.get(d, pos.ex_idx) - pos.ex_idx
        if p_stop_ref == "ex" and pos.ex_close and p <= pos.ex_close * (1.0 - p_stop_pct):
            return True, "stop_ex"
        if p_fill and pos.pre_ex_close and p >= pos.pre_ex_close:
            return True, "fill"
        if p_no_fill_days is not None and tdays >= int(p_no_fill_days):
            return True, "no_fill_3d"
        if p_cover_tax:
            # cover_tax：按**实际卖出日的适用税率**（持有天数决定）判断，
            # (price - buy_price) + dps*(1-rate) >= 0
            hd = (d - pos.entry_date).days
            if hd <= 30:
                rate = rules.tax_rate_short
            elif hd <= 365:
                rate = rules.tax_rate_mid
            else:
                rate = rules.tax_rate_long
            if (p - pos.buy_price) + pos.dps * (1.0 - rate) >= 0:
                return True, "cover_tax"
        if d >= pos.planned_exit:
            return True, "max_hold"
        return False, ""

    for d in cal:
        # 1) 除权日：税前分红入现金 + 送股扩股（每个持仓事件只记一次）
        for pos in positions:
            if not pos.div_credited and d >= pos.ex_date:
                gross = pos.dps * pos.shares
                pos.gross_div += gross
                cash += gross
                if pos.bonus > 0:
                    pos.shares *= (1.0 + pos.bonus)
                pos.div_credited = True
        # 2) 卖出：条件卖出 / 固定到期；无 bar（停牌）则顺延
        for pos in list(positions):
            due, reason = _exit_check(pos, d)
            if not due:
                continue
            price = ds.price_on(pos.symbol, d)
            if price is None or price <= 0:
                continue
            pos.exit_reason = reason
            # 数据防护：卖出日 bar 若疑似数据错误，用最近可信价成交并标记
            data_adjusted = False
            if ds.is_anomaly(pos.symbol, d):
                tp = ds.trusted_price_on(pos.symbol, d)
                if tp is not None and tp > 0:
                    price = tp
                    data_adjusted = True
                    outcomes["price_adjusted_anomaly_bar"] += 1
            sell_amount = pos.shares * price
            sc = float(sell_cost(cfg, sell_amount).total)
            buy_price_eff = pos.buy_amount / pos.shares if pos.shares > 0 else pos.buy_price
            st = settle_cycle(
                entry_date=pos.entry_date, exit_date=d, shares=pos.shares,
                buy_price=buy_price_eff, buy_cost=pos.buy_cost_total,
                sell_price=float(price), sell_cost=sc,
                gross_dividend=pos.gross_div, rules=rules)
            cash += sell_amount - sc - st.tax - st.withdrawn
            cum_withdrawn += st.withdrawn
            assert cash >= -1e-6, f"卖出后现金为负: {cash}"
            price_pnl = sell_amount - sc - pos.buy_amount - pos.buy_cost_total
            div_pnl = st.gross_dividend - st.tax - st.withdrawn
            trades.append({
                "symbol": pos.symbol, "name": pos.name,
                "ex_date": pos.ex_date.isoformat(),
                "entry_date": pos.entry_date.isoformat(),
                "exit_date": d.isoformat(),
                "planned_exit_date": pos.planned_exit.isoformat(),
                "delayed_exit": bool(d != pos.planned_exit),
                "exit_reason": pos.exit_reason or "max_hold",
                "holding_days": int(st.holding_days),
                "buy_price": round(pos.buy_price, 6),
                "sell_price": round(float(price), 6),
                "shares_buy": round(pos.shares / (1.0 + pos.bonus), 6) if pos.bonus > 0 else round(pos.shares, 6),
                "shares_sell": round(pos.shares, 6),
                "bonus_ratio": round(pos.bonus, 6),
                "buy_amount": round(pos.buy_amount, 2),
                "sell_amount": round(sell_amount, 2),
                "buy_cost": round(pos.buy_cost_total, 2),
                "sell_cost": round(sc, 2),
                "gross_dividend": round(st.gross_dividend, 2),
                "tax": round(st.tax, 2),
                "withdrawn": round(st.withdrawn, 2),
                "net_dividend": round(st.net_dividend, 2),
                "price_pnl": round(price_pnl, 2),
                "dividend_pnl": round(div_pnl, 2),
                "net_pnl": round(st.net_pnl, 2),
                "return_pct": round(st.return_pct, 6),
                "data_adjusted": bool(data_adjusted),
                "div_yield": round(pos.div_yield, 6),
                "market_cap": round(pos.market_cap, 2),
                "trend": pos.trend,
                "rank": pos.rank,
            })
            positions.remove(pos)
            outcomes["captured"] += 1
        # 3) 买入：今天有买点的候选（先记不合格），有空位才买
        for c in ineligible_by_entry.get(d, []):
            outcomes["rejected_" + reason_category(c.reason)] += 1
        for c in eligible_by_entry.get(d, []):
            if len(positions) >= rules.max_concurrent_positions:
                outcomes["skipped_overlap"] += 1
                continue
            _buy_one(c, d)
        # 4) 估值
        pv = 0.0
        for pos in positions:
            lp = ds.last_price(pos.symbol, d)
            if lp is not None:
                pos.last_price = float(lp)
            pv += pos.shares * pos.last_price
        equity = cash + pv
        nav_rows.append({
            "date": d.isoformat(), "cash": round(cash, 2),
            "position_value": round(pv, 2), "equity": round(equity, 2),
            "n_positions": len(positions),
            "cum_withdrawn": round(cum_withdrawn, 2),
            "wealth": round(equity + cum_withdrawn, 2),
        })
        min_cash = min(min_cash, cash)

    open_positions = []
    for pos in positions:
        open_positions.append({
            "symbol": pos.symbol, "entry_date": pos.entry_date.isoformat(),
            "ex_date": pos.ex_date.isoformat(), "planned_exit": pos.planned_exit.isoformat(),
            "shares": round(pos.shares, 6), "buy_amount": round(pos.buy_amount, 2),
            "last_price": round(pos.last_price, 6),
            "market_value": round(pos.shares * pos.last_price, 2),
            "gross_dividend": round(pos.gross_div, 2),
        })

    nav_df = pd.DataFrame(nav_rows)
    trades_df = pd.DataFrame(trades)
    return {
        "nav": nav_df, "trades": trades_df, "init_cash": init_cash,
        "cash_end": cash, "cum_withdrawn": cum_withdrawn,
        "open_positions": open_positions, "outcomes": dict(outcomes),
        "min_cash": min_cash, "buys_blocked": buys_blocked,
        "calendar": cal,
    }


# ---------------------------------------------------------------------------
# 指标
# ---------------------------------------------------------------------------
def compute_metrics(ds: Dataset, sim: dict, start: date, end: date,
                    params: dict, label: str = "base") -> dict:
    nav = sim["nav"]
    trades = sim["trades"]
    init = float(sim["init_cash"])
    eq = nav["equity"].astype(float).to_numpy()
    wealth = nav["wealth"].astype(float).to_numpy()
    years = (end - start).days / 365.25
    w_end = float(wealth[-1])
    e_end = float(eq[-1])

    w = np.concatenate([[init], wealth])
    rets = w[1:] / w[:-1] - 1.0
    ann_vol = float(np.std(rets, ddof=1) * math.sqrt(252)) if len(rets) > 2 else float("nan")
    sharpe = (float(np.mean(rets) / np.std(rets, ddof=1) * math.sqrt(252))
              if len(rets) > 2 and np.std(rets, ddof=1) > 0 else float("nan"))
    peak = np.maximum.accumulate(np.concatenate([[init], wealth]))
    dd = np.concatenate([[init], wealth]) / peak - 1.0
    mdd = float(dd.min())
    mdd_idx = int(np.argmin(dd))
    mdd_date = (["(init)"] + list(nav["date"]))[mdd_idx]
    peak_idx = int(np.argmax(np.concatenate([[init], wealth])[:mdd_idx + 1]))
    peak_date = (["(init)"] + list(nav["date"]))[peak_idx]
    cagr = (w_end / init) ** (1.0 / years) - 1.0 if years > 0 and w_end > 0 else float("nan")
    calmar = cagr / abs(mdd) if mdd < 0 else float("nan")

    n_tr = int(len(trades))
    if n_tr:
        win_rate = float((trades["net_pnl"] > 0).mean())
        avg_ret = float(trades["return_pct"].mean())
        avg_hold = float(trades["holding_days"].mean())
        gross_div = float(trades["gross_dividend"].sum())
        tax = float(trades["tax"].sum())
        withdrawn = float(trades["withdrawn"].sum())
        costs = float((trades["buy_cost"] + trades["sell_cost"]).sum())
        price_pnl = float(trades["price_pnl"].sum())
        div_pnl = float(trades["dividend_pnl"].sum())
        net_pnl = float(trades["net_pnl"].sum())
        delayed = int(trades["delayed_exit"].sum())
    else:
        win_rate = avg_ret = avg_hold = 0.0
        gross_div = tax = withdrawn = costs = price_pnl = div_pnl = net_pnl = 0.0
        delayed = 0

    # 分年度
    nav = nav.copy()
    nav["year"] = nav["date"].str[:4].astype(int)
    yearly = []
    prev_wealth = init
    tr_by_year = (trades.assign(year=trades["exit_date"].str[:4].astype(int))
                  .groupby("year").size().to_dict()) if n_tr else {}
    for y in sorted(nav["year"].unique()):
        sub = nav[nav["year"] == y]
        w_end_y = float(sub["wealth"].iloc[-1])
        yearly.append({
            "year": int(y),
            "return": round(w_end_y / prev_wealth - 1.0, 6),
            "wealth_end": round(w_end_y, 2),
            "equity_end": round(float(sub["equity"].iloc[-1]), 2),
            "cum_withdrawn": round(float(sub["cum_withdrawn"].iloc[-1]), 2),
            "captures": int(tr_by_year.get(int(y), 0)),
        })
        prev_wealth = w_end_y

    # benchmark：沪深300（价格指数，不含分红），同区间
    bench = None
    try:
        b = pd.read_csv(CAL_SRC)
        b["date"] = pd.to_datetime(b["date"]).dt.date
        b = b[(b["date"] >= start) & (b["date"] <= end)]
        if len(b) > 10:
            bv = b["close"].astype(float).to_numpy()
            b_cagr = (bv[-1] / bv[0]) ** (1.0 / years) - 1.0
            b_peak = np.maximum.accumulate(bv)
            b_mdd = float((bv / b_peak - 1.0).min())
            bench = {"name": "沪深300(价格指数,不含分红)",
                     "total_return": round(float(bv[-1] / bv[0] - 1.0), 6),
                     "cagr": round(float(b_cagr), 6), "mdd": round(b_mdd, 6)}
    except Exception:  # noqa: BLE001
        bench = None

    out = {
        "label": label,
        "start": start.isoformat(), "end": end.isoformat(),
        "years": round(years, 4),
        "params": params,
        "init_cash": round(init, 2),
        "final_equity": round(e_end, 2),
        "final_wealth": round(w_end, 2),
        "total_return": round(w_end / init - 1.0, 6),
        "nav_total_return": round(e_end / init - 1.0, 6),
        "cagr": round(float(cagr), 6),
        "ann_vol": round(ann_vol, 6),
        "sharpe": round(float(sharpe), 4),
        "mdd": round(mdd, 6),
        "mdd_peak_date": str(peak_date), "mdd_trough_date": str(mdd_date),
        "calmar": round(float(calmar), 4),
        "captures": n_tr,
        "captures_per_year": round(n_tr / years, 3),
        "win_rate": round(win_rate, 6),
        "avg_trade_return": round(avg_ret, 6),
        "avg_holding_days": round(avg_hold, 2),
        "gross_dividend": round(gross_div, 2),
        "tax": round(tax, 2),
        "withdrawn": round(withdrawn, 2),
        "costs": round(costs, 2),
        "price_pnl": round(price_pnl, 2),
        "dividend_pnl": round(div_pnl, 2),
        "net_pnl": round(net_pnl, 2),
        "tax_ratio_of_gross": round(tax / gross_div, 6) if gross_div > 0 else 0.0,
        "delayed_exits": delayed,
        "exposure_days": int(trades["holding_days"].sum()) if n_tr else 0,
        "exposure_ratio": round(float(trades["holding_days"].sum())
                                / max((end - start).days, 1), 6) if n_tr else 0.0,
        "open_positions": sim["open_positions"],
        "skipped": sim["outcomes"],
        "buys_blocked": sim["buys_blocked"][:20],
        "min_cash": round(float(sim["min_cash"]), 2),
        "benchmark": bench,
        "yearly": yearly,
    }
    return out


# ---------------------------------------------------------------------------
# 单次回测
# ---------------------------------------------------------------------------
VARIANT_KEYS = (
    "min_div_yield_ttm", "min_market_cap", "entry_lead_trading_days",
    "hold_days_after_ex", "max_concurrent_positions", "trend_enabled", "trend_mode",
    "tax_rate_short", "tax_rate_mid", "tax_rate_long", "withdraw_ratio",
)


def run_case(ds: Dataset, cfg, dc, overrides: dict | None = None,
             start: date = BACKTEST_START, end: date = BACKTEST_END,
             label: str = "base", rules: DcRules | None = None, *,
             exit_policy: dict | None = None, rank_mode: str = "pure",
             score_cfg: dict | None = None, candidates: list | None = None,
             candidate_stats: dict | None = None) -> dict:
    overrides = overrides or {}
    bad = [k for k in overrides if k not in VARIANT_KEYS]
    if bad:
        raise ValueError(f"未知参数覆盖: {bad}")
    base = rules if rules is not None else DcRules.from_strategy(dc)
    rules = replace(base, **overrides) if overrides else base
    _install_trend_memo()
    if candidates is None:
        _enable_trend_cache(id(ds))
        try:
            cands, cstats = build_candidates(ds, rules, start, end)
        finally:
            _disable_trend_cache()
    else:
        cands, cstats = candidates, (candidate_stats or {})
    sim = simulate(ds, cfg, rules, cands, start, end, init_cash=dc.init_cash,
                   exit_policy=exit_policy, rank_mode=rank_mode, score_cfg=score_cfg)
    params = {
        "min_div_yield_ttm": rules.min_div_yield_ttm,
        "min_market_cap": rules.min_market_cap,
        "entry_lead_trading_days": rules.entry_lead_trading_days,
        "hold_days_after_ex": rules.hold_days_after_ex,
        "max_concurrent_positions": rules.max_concurrent_positions,
        "trend_enabled": rules.trend_enabled, "trend_mode": rules.trend_mode,
        "tax_rates": [rules.tax_rate_short, rules.tax_rate_mid, rules.tax_rate_long],
        "withdraw_ratio": rules.withdraw_ratio, "withdraw_base": rules.withdraw_base,
        "lot_size": rules.lot_size, "cash_buffer": rules.cash_buffer,
        "cap_mode": ds.cap_mode, "universe": ds.kind,
        "exit_policy": (exit_policy or {"type": "fixed_30d"}),
        "rank_mode": rank_mode, "score_cfg": score_cfg,
    }
    metrics = compute_metrics(ds, sim, start, end, params, label)
    metrics["candidate_stats"] = cstats
    metrics["cap_mode"] = ds.cap_mode
    metrics["cap_source_counts"] = {k: v for k, v in cstats.items()
                                    if k.startswith("cap_source_")}
    metrics["shares_history_symbols"] = len(ds.shares_hist)
    elig = [c for c in cands if c.eligible]
    metrics["eligible_candidates"] = len(elig)
    metrics["eligible_symbols"] = len({c.symbol for c in elig})
    if len(sim["trades"]):
        reasons = sim["trades"]["exit_reason"].value_counts().to_dict()
        metrics["exit_reason_counts"] = {str(k): int(v) for k, v in reasons.items()}
        tr_ = sim["trades"]
        early = tr_[tr_["holding_days"] <= 30]
        metrics["early_exits_le30d"] = int(len(early))
        metrics["early_exit_tax"] = round(float(early["tax"].sum()), 2) if len(early) else 0.0
        metrics["early_exit_gross_div"] = (round(float(early["gross_dividend"].sum()), 2)
                                           if len(early) else 0.0)
        metrics["avg_holding_days_early"] = (round(float(early["holding_days"].mean()), 2)
                                             if len(early) else None)
    else:
        metrics["exit_reason_counts"] = {}
        metrics["early_exits_le30d"] = 0
        metrics["early_exit_tax"] = 0.0
        metrics["early_exit_gross_div"] = 0.0
        metrics["avg_holding_days_early"] = None
    metrics["data_protection"] = {
        "rule": ("疑似数据错误 bar：非除权事件日 + 相邻bar间隔<=10自然日 + |单日收益|>30%；"
                 "买入日/计划卖出日命中则跳过该笔，持仓估值与延迟卖出用最近可信价"),
        "anomaly_bars": int(sum(len(v) for v in ds.anomaly.values())),
        "anomaly_symbols": int(sum(1 for v in ds.anomaly.values() if v)),
        "skipped_data_entry_bar": int(cstats.get("skipped_data_entry_bar", 0)),
        "skipped_data_exit_bar": int(cstats.get("skipped_data_exit_bar", 0)),
        "price_adjusted_trades": int(sim["outcomes"].get("price_adjusted_anomaly_bar", 0)),
        "resumption_gap_rule": (f"相邻bar间隔>{RESUMPTION_GAP_DAYS}自然日的复牌跳变按真实价格处理，"
                                "仅披露，不做修正"),
    }
    metrics["universe_count"] = len(ds.symbols)
    metrics["dataset_kind"] = ds.kind
    return {"metrics": metrics, "sim": sim, "rules": rules,
            "candidates": cands, "candidate_stats": cstats}


def snapshot_hashes(result: dict) -> dict:
    """结果快照哈希（可复现性校验：同输入应产生同哈希）。"""
    import hashlib

    tr = result["sim"]["trades"]
    nav = result["sim"]["nav"]
    tr_csv = tr.to_csv(index=False, float_format="%.6f") if len(tr) else ""
    nav_csv = nav.to_csv(index=False, float_format="%.2f")
    return {
        "trades_sha256": hashlib.sha256(tr_csv.encode()).hexdigest(),
        "nav_sha256": hashlib.sha256(nav_csv.encode()).hexdigest(),
        "captures": int(len(tr)),
        "final_wealth": float(nav["wealth"].iloc[-1]),
    }
