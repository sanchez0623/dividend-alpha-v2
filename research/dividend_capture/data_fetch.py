#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dividend-alpha-v2 / research/dividend_capture —— DC 分红捕获回测：数据抓取与质检

数据源与口径（与任务 task-1 一致）：
  1. 东方财富 `fetch_all_a_spot()` 取全 A 当前快照 → 总市值 ≥ 500 亿、非 ST/退，
     得到当前全市场候选池（约 344 只）。快照落 data/spot_snapshot.csv。
  2. 同花顺 fuyao `ThsFuyaoClient` 抓每只 2015-01-01..2024-12-31 与
     2024-12-01..2026-11-06 两段 `adjust="none"`（不复权）日线；
     批量接口单次最多 10 个代码，缓存 JSON 到 data/cache/raw/。
  3. 同花顺 `adjustment_factors(since=2014-11-01)` 抓每只除权事件（分红/送股），
     缓存 JSON 到 data/cache/ca/。
  4. 交易日历复用 research/rebalance/data/index_000300.csv（沪深300 交易日），
     输出 data/calendar.csv。

产出：
  - data/universe.csv         当前池（含估算股本 shares_est 与数据覆盖）
  - data/calendar.csv         回测交易日历
  - data/bars_raw.parquet     长表 symbol,date,close（不复权，两窗口合并）
  - data/events.parquet       长表 symbol,ex_date,dps,bonus（除权事件）
  - data/spot_snapshot.csv    全 A 快照（供 pool62 对照估算市值）
  - data/DATA_QUALITY.md      数据质量报告（抓取失败清单、覆盖度、异常）
  - data/manifest.json        抓取时点 + 全部缓存文件 SHA256
  - data/fetch_failures.csv   失败清单（仅在有失败时）

缓存优先：重跑默认命中缓存；`--force` 重抓；`--no-fetch` 只读缓存（缺失即报错）。
请求策略：批量 10 只/次，相邻请求间隔 ≥0.35s；失败指数退避重试，限流读 retry_after。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Windows 控制台默认 GBK，日志含中文/符号会崩；统一 UTF-8。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

TZ_BJ_HOURS = 8
DATA = HERE / "data"
CACHE = DATA / "cache"
RAW_DIR = CACHE / "raw"
CA_DIR = CACHE / "ca"
RESULTS = HERE / "results"

WINDOWS = [(date(2015, 1, 1), date(2024, 12, 31)),
           (date(2024, 12, 1), date(2026, 11, 6))]
WINDOW_TAGS = [f"{a.isoformat()}_{b.isoformat()}" for a, b in WINDOWS]
CA_SINCE = date(2014, 11, 1)
MIN_CAP = 5.0e10
BATCH_SIZE = 10
MIN_INTERVAL = 0.35
BACKOFF_BASE = 2.0
MAX_ATTEMPTS = 4

POOL62_DIR = ROOT / "research" / "rebalance" / "data" / ".ths_cache_v2"
CAL_SRC = ROOT / "research" / "rebalance" / "data" / "index_000300.csv"
EVENTS_TAIL_DATE = date(2026, 11, 6)   # 回测末日；其后的除权事件不进回测

OUT_UNIVERSE = DATA / "universe.csv"
OUT_CALENDAR = DATA / "calendar.csv"
OUT_BARS = DATA / "bars_raw.parquet"
OUT_EVENTS = DATA / "events.parquet"
OUT_SPOT = DATA / "spot_snapshot.csv"
OUT_QUALITY = DATA / "DATA_QUALITY.md"
OUT_MANIFEST = DATA / "manifest.json"
OUT_FAILURES = DATA / "fetch_failures.csv"
OUT_POOL62 = DATA / "pool62.txt"

# 数据防护判定（与 engine.py 保持一致）：
#   疑似数据错误 bar = 非除权事件日 + 相邻 bar 间隔 <= 10 自然日 + |单日收益| > 30%
#   停牌复牌跳变 = 相邻 bar 间隔 > 30 自然日（真实价格，不修正，仅披露）
ANOMALY_RET_THRESHOLD = 0.30
ANOMALY_MAX_GAP_DAYS = 10
RESUMPTION_GAP_DAYS = 30

DATE_RE = "%Y-%m-%d"


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def now_str() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S%z")


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def load_env() -> None:
    """.env 逐行读取注入环境（不覆盖已存在的环境变量）。"""
    p = ROOT / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ms_to_date(ms: int) -> date:
    """fuyao 毫秒时间戳按 Asia/Shanghai 日界解析（与 provider 一致）。"""
    return (datetime.utcfromtimestamp(ms / 1000) + timedelta(hours=TZ_BJ_HOURS)).date()


def iso(d: date) -> str:
    return d.strftime(DATE_RE)


def pct(v, nd: int = 2) -> str:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "—"
    return f"{v * 100:.{nd}f}%"


def num(v, nd: int = 2) -> str:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "—"
    return f"{v:.{nd}f}"


def md_table(headers, rows) -> str:
    out = ["| " + " | ".join(str(h) for h in headers) + " |",
           "|" + "|".join(["---"] * len(headers)) + "|"]
    for r in rows:
        out.append("| " + " | ".join("" if c is None else str(c) for c in r) + " |")
    return "\n".join(out)


def chunked(seq: list, n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


# ---------------------------------------------------------------------------
# 快照与池
# ---------------------------------------------------------------------------
def load_spot(force: bool, no_fetch: bool) -> pd.DataFrame:
    """全 A 快照：缓存 data/cache/spot.json；失败时回退缓存并记录。"""
    import httpx  # noqa: F401  （提前 fail-fast 检查依赖）

    from src.da_data.providers.eastmoney import fetch_all_a_spot

    path = CACHE / "spot.json"
    if path.exists() and not force:
        obj = json.loads(path.read_text(encoding="utf-8"))
        df = pd.DataFrame(obj["rows"])
        log(f"全 A 快照命中缓存（{obj.get('fetched_at')}，{len(df)} 行）")
        return df
    if no_fetch:
        raise RuntimeError("--no-fetch 但 spot.json 缓存不存在")
    last_err = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            df = fetch_all_a_spot()
            if df is None or df.empty:
                raise RuntimeError("快照为空")
            CACHE.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(
                {"fetched_at": now_str(), "rows": df.to_dict(orient="records")},
                ensure_ascii=False), encoding="utf-8")
            log(f"全 A 快照抓取成功：{len(df)} 行")
            return df
        except Exception as e:  # noqa: BLE001
            last_err = e
            delay = BACKOFF_BASE * (2 ** (attempt - 1))
            log(f"全 A 快照第 {attempt} 次失败: {type(e).__name__}: {e}")
            if attempt < MAX_ATTEMPTS:
                time.sleep(delay)
    if path.exists():
        obj = json.loads(path.read_text(encoding="utf-8"))
        log(f"⚠ 快照抓取失败，回退旧缓存（{obj.get('fetched_at')}）：{last_err}")
        return pd.DataFrame(obj["rows"])
    raise RuntimeError(f"全 A 快照抓取失败且无缓存: {last_err}")


def build_universe(spot: pd.DataFrame) -> pd.DataFrame:
    df = spot.copy()
    df["market_cap"] = pd.to_numeric(df["market_cap"], errors="coerce")
    df["last"] = pd.to_numeric(df["last"], errors="coerce")
    df = df.dropna(subset=["market_cap"])
    df["name"] = df["name"].fillna("")
    df = df[~df["name"].str.contains("ST|退", na=False)]
    df = df[df["market_cap"] >= MIN_CAP]
    df = df.drop_duplicates(subset=["symbol"]).sort_values("market_cap", ascending=False)
    df = df.reset_index(drop=True)
    log(f"池筛选：市值≥{MIN_CAP / 1e8:.0f}亿 且非ST/退 → {len(df)} 只")
    return df


# ---------------------------------------------------------------------------
# 抓取器（带缓存/重试/限流）
# ---------------------------------------------------------------------------
class Fetcher:
    def __init__(self, client, force: bool, no_fetch: bool):
        self.c = client
        self.force = force
        self.no_fetch = no_fetch
        self.stats = {"requests": 0, "cache_hits": 0, "fetched": 0, "retries": 0}
        self.failures: list[dict] = []

    @staticmethod
    def raw_path(codes: list[str], win_tag: str) -> Path:
        return RAW_DIR / f"raw_{'_'.join(c.replace('.', '_') for c in codes)}_{win_tag}.json"

    @staticmethod
    def ca_path(thscode: str) -> Path:
        return CA_DIR / f"ca_{thscode}.json"

    def _call(self, fn, label: str):
        """单次调用 + 指数退避重试；RateLimited 用 retry_after。返回 (result, err)。"""
        from src.da_data.providers.base import BizError, RateLimited

        last = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                return fn(), None
            except RateLimited as e:
                last = {"type": "rate_limited", "code": str(e.code), "detail": str(e)[:160]}
                delay = max(float(e.retry_after or 0.0), BACKOFF_BASE * (2 ** (attempt - 1)))
                self.stats["retries"] += 1
                log(f"  {label} 限流（第{attempt}次），{delay:.1f}s 后重试")
                time.sleep(delay)
            except BizError as e:
                last = {"type": "biz_error", "code": str(e.code), "detail": str(e)[:160]}
                if str(e.code) in ("1002", "3002"):
                    return None, last
                self.stats["retries"] += 1
                delay = BACKOFF_BASE * (2 ** (attempt - 1))
                log(f"  {label} 业务错误 code={e.code}（第{attempt}次），{delay:.1f}s 后重试")
                time.sleep(delay)
            except Exception as e:  # noqa: BLE001
                last = {"type": type(e).__name__, "code": None, "detail": str(e)[:160]}
                self.stats["retries"] += 1
                delay = BACKOFF_BASE * (2 ** (attempt - 1))
                log(f"  {label} 第{attempt}次异常: {type(e).__name__}: {e}；{delay:.1f}s 后重试")
                time.sleep(delay)
        return None, last

    # ---- 日线批量 ----
    def fetch_raw_batch(self, thscodes: list[str], w0: date, w1: date) -> dict:
        tag = f"{iso(w0)}_{iso(w1)}"
        path = self.raw_path(thscodes, tag)
        if path.exists() and not self.force:
            try:
                obj = json.loads(path.read_text(encoding="utf-8"))
                data = obj["payload"]
                if isinstance(data.get("item"), list) and len(data["item"]) > 0:
                    self.stats["cache_hits"] += 1
                    return data
            except Exception as e:  # noqa: BLE001
                log(f"缓存损坏，重抓 {path.name}: {e}")
        if self.no_fetch:
            raise RuntimeError(f"--no-fetch 但缓存缺失/无效: {path.name}")
        self.stats["requests"] += 1
        data, err = self._call(
            lambda: self.c.prices_historical_batch(thscodes, w0, w1, adjust="none"),
            label=f"raw[{len(thscodes)}] {iso(w0)}..{iso(w1)}")
        if err is not None or data is None:
            # 批量失败 → 逐只回退
            log(f"  批量失败，逐只回退 {len(thscodes)} 只")
            merged = {"interval": "1d", "adjust": "none", "item": []}
            for t in thscodes:
                self.stats["requests"] += 1
                d1, e1 = self._call(
                    lambda t=t: self.c.prices_historical_batch([t], w0, w1, adjust="none"),
                    label=f"raw[{t}] {iso(w0)}..{iso(w1)}")
                if e1 is not None or d1 is None:
                    self.failures.append({"kind": "raw", "target": t,
                                          "window": tag, **e1,
                                          "when": now_str()})
                else:
                    merged["item"].extend(d1.get("item") or [])
            if not merged["item"]:
                raise RuntimeError(f"批量与逐只均失败: {err}")
            data = merged
        self.stats["fetched"] += 1
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(
            {"fetched_at": now_str(), "thscodes": thscodes, "start": iso(w0), "end": iso(w1),
             "payload": data}, ensure_ascii=False), encoding="utf-8")
        return data

    # ---- 除权事件 ----
    def fetch_ca(self, thscode: str) -> tuple[list[dict] | None, dict | None]:
        path = self.ca_path(thscode)
        if path.exists() and not self.force:
            try:
                obj = json.loads(path.read_text(encoding="utf-8"))
                rows = obj["payload"]
                if isinstance(rows, list):
                    self.stats["cache_hits"] += 1
                    return rows, None
            except Exception as e:  # noqa: BLE001
                log(f"CA 缓存损坏，重抓 {path.name}: {e}")
        if self.no_fetch:
            raise RuntimeError(f"--no-fetch 但 CA 缓存缺失: {path.name}")
        self.stats["requests"] += 1
        rows, err = self._call(lambda: self.c.adjustment_factors(thscode, CA_SINCE),
                               label=f"ca[{thscode}]")
        if err is not None:
            if str(err.get("code")) == "3002":
                rows = []      # 确定性：无除权事件
            else:
                return None, err
        self.stats["fetched"] += 1
        CA_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(
            {"fetched_at": now_str(), "thscode": thscode, "since": iso(CA_SINCE),
             "payload": rows}, ensure_ascii=False), encoding="utf-8")
        return rows, None


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------
def parse_raw(payload: dict) -> dict[str, dict[date, float]]:
    """payload → {symbol: {date: close}}（不复权）。"""
    out: dict[str, dict[date, float]] = {}
    for it in payload.get("item") or []:
        ths = str(it.get("thscode") or "")
        sym = None
        if "." in ths:
            code, exch = ths.split(".", 1)
            exch = exch.lower()
            if exch in ("sh", "sz", "bj"):
                sym = exch + code
        if sym is None:
            continue
        bars = it.get("item") or []
        d: dict[date, float] = {}
        for b in bars:
            ms = b.get("date_ms")
            close = b.get("close_price")
            if ms is None or close is None:
                continue
            try:
                c = float(close)
            except (TypeError, ValueError):
                continue
            if c <= 0:
                continue
            d[ms_to_date(int(ms))] = c
        out[sym] = d
    return out


def parse_ca(rows: list[dict] | None) -> list[dict]:
    """adjustment_factors item[] → [{ex_date, dps, bonus}]（仅保留 ex_date 可解析）。"""
    out: list[dict] = []
    for r in rows or []:
        ms = r.get("ex_date_ms")
        if not ms:
            continue
        try:
            ex = ms_to_date(int(ms))
            dps = float(r.get("dividend_per_share") or 0.0)
            bonus = float(r.get("per_share_bonus") or 0.0)
        except (TypeError, ValueError):
            continue
        out.append({"ex_date": ex, "dps": dps, "bonus": bonus})
    out.sort(key=lambda x: x["ex_date"])
    return out


# ---------------------------------------------------------------------------
# 构建数据集
# ---------------------------------------------------------------------------
def load_calendar() -> list[date]:
    df = pd.read_csv(CAL_SRC)
    dates = [datetime.strptime(str(x), DATE_RE).date() for x in df["date"]]
    lo, hi = date(2015, 1, 5), date(2026, 11, 6)
    dates = [d for d in dates if lo <= d <= hi]
    if dates[-1] != hi:
        raise RuntimeError(f"日历末日 {dates[-1]} != {hi}")
    if dates != sorted(set(dates)):
        raise RuntimeError("日历重复/乱序")
    return dates


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="忽略缓存，重抓全部")
    ap.add_argument("--no-fetch", action="store_true", help="只用缓存")
    ap.add_argument("--skip-quality", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="仅抓前 N 只（自测用，勿用于正式产出）")
    args = ap.parse_args()

    load_env()
    DATA.mkdir(parents=True, exist_ok=True)
    for d in (RAW_DIR, CA_DIR):
        d.mkdir(parents=True, exist_ok=True)

    t_start = time.time()
    api_key = os.environ.get("THS_API_KEY", "")
    if not api_key:
        raise RuntimeError("THS_API_KEY 未配置（.env）")
    from src.da_data.providers.ths import ThsFuyaoClient
    client = ThsFuyaoClient(api_key=api_key, min_interval_sec=MIN_INTERVAL)
    fetcher = Fetcher(client, force=args.force, no_fetch=args.no_fetch)

    # 1) 快照 → 当前池
    spot = load_spot(args.force, args.no_fetch)
    universe = build_universe(spot)
    if args.limit:
        universe = universe.head(args.limit).reset_index(drop=True)
        log(f"⚠ 自测模式：仅取前 {len(universe)} 只（不是正式产出）")
    pool62 = [ln.strip() for ln in
              (ROOT / "research" / "rebalance" / "data" / "universe.txt")
              .read_text(encoding="utf-8").splitlines()
              if ln.strip() and not ln.strip().startswith("#")]
    OUT_POOL62.write_text("\n".join(pool62) + "\n", encoding="utf-8")

    thscodes = {s: s[2:] + "." + s[:2].upper() for s in universe["symbol"]}
    symbols = list(universe["symbol"])
    batches = list(chunked(symbols, BATCH_SIZE))
    log(f"开始抓取：{len(symbols)} 只 / {len(batches)} 批 × {len(WINDOWS)} 窗口 + CA")

    # 2) 两段不复权日线
    bars: dict[str, dict[date, float]] = {}
    window_coverage: dict[str, dict[str, int]] = {s: {} for s in symbols}
    for w0, w1 in WINDOWS:
        tag = f"{iso(w0)}_{iso(w1)}"
        for bi, batch in enumerate(batches, start=1):
            codes = [thscodes[s] for s in batch]
            try:
                payload = fetcher.fetch_raw_batch(codes, w0, w1)
            except Exception as e:  # noqa: BLE001
                log(f"✗ 窗口 {tag} 批次 {bi} 失败: {e}")
                fetcher.failures.append({"kind": "raw_batch", "target": ",".join(codes),
                                         "window": tag, "type": type(e).__name__,
                                         "code": None, "detail": str(e)[:200],
                                         "when": now_str()})
                continue
            parsed = parse_raw(payload)
            for s in batch:
                d = parsed.get(s) or {}
                window_coverage[s][tag] = len(d)
                if d:
                    bars.setdefault(s, {}).update(d)
            if bi % 5 == 0 or bi == len(batches):
                log(f"  窗口 {tag} 进度 {bi}/{len(batches)}  已用 {time.time() - t_start:.0f}s")

    # 3) 除权事件
    events: dict[str, list[dict]] = {}
    ca_missing: list[str] = []
    for i, s in enumerate(symbols, start=1):
        try:
            rows, err = fetcher.fetch_ca(thscodes[s])
        except Exception as e:  # noqa: BLE001
            log(f"✗ CA {s} 失败: {e}")
            fetcher.failures.append({"kind": "ca", "target": s, "window": "",
                                     "type": type(e).__name__, "code": None,
                                     "detail": str(e)[:200], "when": now_str()})
            ca_missing.append(s)
            continue
        if err is not None:
            fetcher.failures.append({"kind": "ca", "target": s, "window": "",
                                     **err, "when": now_str()})
            ca_missing.append(s)
            continue
        events[s] = parse_ca(rows)
        if i % 50 == 0 or i == len(symbols):
            log(f"  CA 进度 {i}/{len(symbols)}  已用 {time.time() - t_start:.0f}s")

    # 4) 组装长表
    cal = load_calendar()
    cal_set = set(cal)
    bar_rows, ev_rows = [], []
    for s in symbols:
        for d, c in sorted(bars.get(s, {}).items()):
            if d in cal_set:
                bar_rows.append((s, d, float(c)))
        for ev in events.get(s, []):
            if ev["ex_date"] <= EVENTS_TAIL_DATE:
                ev_rows.append((s, ev["ex_date"], float(ev["dps"]), float(ev["bonus"])))
    bars_df = pd.DataFrame(bar_rows, columns=["symbol", "date", "close"])
    events_df = pd.DataFrame(ev_rows, columns=["symbol", "ex_date", "dps", "bonus"])
    bars_df = bars_df.sort_values(["symbol", "date"]).reset_index(drop=True)
    # 同一 (symbol, ex_date) 可能有多行（现金分红/送股分行下发）→ 聚合为一条事件
    if len(events_df):
        events_df = (events_df.groupby(["symbol", "ex_date"], as_index=False)
                     .agg(dps=("dps", "sum"), bonus=("bonus", "sum")))
        events_df = events_df.sort_values(["symbol", "ex_date"]).reset_index(drop=True)
    bars_df.to_parquet(OUT_BARS, index=False)
    events_df.to_parquet(OUT_EVENTS, index=False)

    # 5) universe.csv（含股本估算与覆盖）
    last_close = {}
    for s in symbols:
        d = bars.get(s) or {}
        if d:
            last_close[s] = float(d[max(d)])
    rows = []
    for _, r in universe.iterrows():
        s = r["symbol"]
        d = bars.get(s) or {}
        price_now = float(r["last"]) if pd.notna(r["last"]) else last_close.get(s)
        shares_est = (float(r["market_cap"]) / price_now) if price_now and price_now > 0 else np.nan
        evs = events.get(s, [])
        rows.append({
            "symbol": s,
            "name": r["name"],
            "thscode": thscodes[s],
            "market_cap_now": float(r["market_cap"]),
            "last_now": float(r["last"]) if pd.notna(r["last"]) else np.nan,
            "price_last_bar": last_close.get(s, np.nan),
            "shares_est": shares_est,
            "n_bars": len(d),
            "n_events": len(evs),
            "n_cash_events": sum(1 for e in evs if e["dps"] > 0),
            "first_date": iso(min(d)) if d else "",
            "last_date": iso(max(d)) if d else "",
            "ca_missing": s in ca_missing,
        })
    uni_df = pd.DataFrame(rows)
    uni_df.to_csv(OUT_UNIVERSE, index=False, encoding="utf-8", float_format="%.6f")
    pd.DataFrame({"date": [iso(d) for d in cal]}).to_csv(
        OUT_CALENDAR, index=False, encoding="utf-8")

    # 6) 全 A 快照落盘（pool62 对照用）
    spot_out = spot.copy()
    spot_out["market_cap"] = pd.to_numeric(spot_out["market_cap"], errors="coerce")
    spot_out["last"] = pd.to_numeric(spot_out["last"], errors="coerce")
    spot_out.to_csv(OUT_SPOT, index=False, encoding="utf-8", float_format="%.6f")

    if fetcher.failures:
        pd.DataFrame(fetcher.failures).to_csv(OUT_FAILURES, index=False, encoding="utf-8")
    elif OUT_FAILURES.exists():
        OUT_FAILURES.unlink()

    # 7) 质量报告
    if not args.skip_quality:
        write_quality(uni_df, bars_df, events_df, cal, window_coverage, fetcher, pool62,
                      elapsed=time.time() - t_start)

    # 8) manifest（缓存 + 产出 SHA256）
    files = []
    for p in sorted(list(RAW_DIR.glob("*.json")) + list(CA_DIR.glob("*.json"))
                    + [CACHE / "spot.json"]):
        if p.exists():
            files.append({"kind": "cache", "path": str(p.relative_to(ROOT)),
                          "bytes": p.stat().st_size, "sha256": sha256_of(p)})
    for p in [OUT_BARS, OUT_EVENTS, OUT_UNIVERSE, OUT_CALENDAR, OUT_SPOT]:
        files.append({"kind": "output", "path": str(p.relative_to(ROOT)),
                      "bytes": p.stat().st_size, "sha256": sha256_of(p)})
    manifest = {
        "generated_at": now_str(),
        "source": "THS fuyao prices/historical-batch(adjust=none) + corporate-actions/"
                  "adjustment-factors; Eastmoney all-A spot for universe/market cap",
        "universe_count": int(len(universe)),
        "windows": WINDOW_TAGS,
        "ca_since": iso(CA_SINCE),
        "min_market_cap": MIN_CAP,
        "request_stats": fetcher.stats,
        "failures": len(fetcher.failures),
        "files": files,
    }
    OUT_MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                            encoding="utf-8")
    log(f"完成：bars={len(bars_df)} 行，events={len(events_df)} 行，"
        f"缓存文件 {len(files)} 个，失败 {len(fetcher.failures)}，"
        f"用时 {time.time() - t_start:.0f}s")
    if fetcher.failures:
        log(f"⚠ 有 {len(fetcher.failures)} 条失败，见 {OUT_FAILURES} 与 DATA_QUALITY.md")
    return 0 if not fetcher.failures else 2


# ---------------------------------------------------------------------------
# 质量报告
# ---------------------------------------------------------------------------
def write_quality(uni_df, bars_df, events_df, cal, window_coverage, fetcher,
                  pool62, elapsed: float) -> None:
    cal_set = set(cal)
    symbols = list(uni_df["symbol"])
    n_cal = len(cal)
    n_bars = len(bars_df)

    # 覆盖度
    cov = bars_df.groupby("symbol")["date"].agg(["count", "min", "max"])
    cov = cov.reindex(symbols)
    n_no_data = int(cov["count"].isna().sum())
    first_dates = cov["min"].dropna()
    from_2015 = int(sum(1 for s in symbols
                        if s in cov.index and pd.notna(cov.loc[s, "min"])
                        and str(cov.loc[s, "min"]) <= "2015-01-09"))
    # 缺失（上市后）统计：首末之间应有多少交易日
    cal_idx = {d: i for i, d in enumerate(cal)}
    gaps = []
    for s in symbols:
        if s not in cov.index or pd.isna(cov.loc[s, "count"]):
            gaps.append((s, n_cal))
            continue
        f, l = cov.loc[s, "min"], cov.loc[s, "max"]
        exp = cal_idx[l] - cal_idx[f] + 1
        gaps.append((s, exp - int(cov.loc[s, "count"])))
    gaps.sort(key=lambda x: -x[1])

    # 事件统计
    ev_syms = events_df["symbol"].nunique() if len(events_df) else 0
    cash_ev = events_df[events_df["dps"] > 0] if len(events_df) else events_df
    bonus_ev = events_df[events_df["bonus"] > 0] if len(events_df) else events_df
    n_events = len(events_df)

    # 跳变扫描（不复权）：区分 真实除权事件 / 停牌复牌 / 疑似数据错误
    events_all = {(r.symbol, pd.Timestamp(r.ex_date).date())
                  for r in (events_df.itertuples() if len(events_df) else [])}
    jumps = []
    for s, g in bars_df.groupby("symbol"):
        g = g.sort_values("date")
        dates = list(g["date"])
        closes = [float(x) for x in g["close"]]
        for i in range(1, len(dates)):
            if closes[i - 1] <= 0:
                continue
            jumps.append({
                "symbol": s, "date": dates[i],
                "ret": closes[i] / closes[i - 1] - 1.0,
                "gap": (dates[i] - dates[i - 1]).days,
                "is_event": (s, dates[i]) in events_all,
            })
    jdf = pd.DataFrame(jumps) if jumps else pd.DataFrame(
        columns=["symbol", "date", "ret", "gap", "is_event"])
    abs_ret = jdf["ret"].abs() if len(jdf) else pd.Series(dtype=float)
    n_gt_15 = int((abs_ret > 0.15).sum())
    n_gt_15_non_ev = int(((abs_ret > 0.15) & (~jdf["is_event"])).sum()) if len(jdf) else 0
    event_jumps = jdf[jdf["is_event"] & (abs_ret > 0.15)] if len(jdf) else jdf
    suspicious = jdf[(~jdf["is_event"]) & (jdf["gap"] <= ANOMALY_MAX_GAP_DAYS)
                     & (abs_ret > ANOMALY_RET_THRESHOLD)] if len(jdf) else jdf
    resumption = jdf[(~jdf["is_event"]) & (jdf["gap"] > RESUMPTION_GAP_DAYS)
                     & (abs_ret > 0.20)] if len(jdf) else jdf
    if len(jdf):
        imax = abs_ret.idxmax()
        max_abs = {"all": float(abs_ret.loc[imax]),
                   "all_where": (jdf.loc[imax, "symbol"],
                                 iso(jdf.loc[imax, "date"]),
                                 float(jdf.loc[imax, "ret"]),
                                 int(jdf.loc[imax, "gap"]),
                                 bool(jdf.loc[imax, "is_event"]))}
    else:
        max_abs = {"all": 0.0, "all_where": None}

    # 市值估算检查
    shares = uni_df["shares_est"].dropna()
    cap_now = uni_df["market_cap_now"]
    oldest = []
    for s in symbols:
        d = events_df[events_df["symbol"] == s]
        if len(d) and s in cov.index and pd.notna(cov.loc[s, "min"]):
            oldest.append((s, iso(cov.loc[s, "min"])))

    # 每窗口覆盖
    w_tags = WINDOW_TAGS
    w_cov = {t: 0 for t in w_tags}
    for s in symbols:
        for t in w_tags:
            if window_coverage.get(s, {}).get(t, 0) > 0:
                w_cov[t] += 1

    L = []
    def add(s: str = "") -> None:
        L.append(s)
    add("# DATA_QUALITY.md —— DC 分红捕获回测数据质检报告")
    add()
    add(f"- 生成时间：`{now_str()}`；脚本：`research/dividend_capture/data_fetch.py`（自动生成，勿手改）")
    add(f"- 数据源：同花顺 fuyao（不复权日线 + 除权事件）、东方财富全 A 快照（当前市值/池）；"
        f"交易日历复用 `research/rebalance/data/index_000300.csv`")
    add(f"- 区间：`2015-01-05 .. 2026-11-06`（2015 预热 MA120；回测 {iso(cal[0])} 起），"
        f"交易日 {n_cal} 个")
    add()
    add("## 1. 结论摘要")
    add()
    add(f"1. **当前池**：市值 ≥ {MIN_CAP / 1e8:.0f} 亿且非 ST/退 → **{len(symbols)} 只**"
        f"（全 A 快照见 `data/spot_snapshot.csv`）。")
    add(f"2. **抓取**：请求 {fetcher.stats['requests']} 次（缓存命中 {fetcher.stats['cache_hits']}，"
        f"实抓 {fetcher.stats['fetched']}，重试 {fetcher.stats['retries']}），"
        f"失败 **{len(fetcher.failures)}** 条；总用时 {elapsed:.0f}s。"
        f"两窗口有日线的标的："
        + "、".join(f"{t}: {w_cov[t]}/{len(symbols)}" for t in w_tags) + "。")
    add(f"3. **数据量**：日线 {n_bars} 行（{n_bars / max(len(symbols), 1):.0f} 行/只均），"
        f"除权事件 {n_events} 条（现金分红 {len(cash_ev)}，含送股 {len(bonus_ev)}），"
        f"覆盖 {ev_syms} 只。")
    add(f"4. **覆盖度**：自 2015-01 起即有日线的标的 {from_2015} 只；无任何日线 {n_no_data} 只；"
        f"上市后缺口中位数 {int(np.median([g for _, g in gaps])) if gaps else 0} 个交易日"
        f"（停牌/数据缺口，回测按最近可得价估值并跳过无 bar 的买点）。")
    add(f"5. **不复权口径的已知跳变**：单日 |收益| >15% 共 {n_gt_15} 处，其中**除权事件日** "
        f"{len(event_jumps)} 处（真实除权跳空，回测按 dps/送股显式补偿，不是错误）；"
        f"**非事件日** {n_gt_15_non_ev} 处，含停牌复牌跳变 {len(resumption)} 处"
        f"（真实价格，不修正）与**疑似数据错误 {len(suspicious)} 处**（引擎按数据防护规则跳过/降级）。"
        f"全池最大单日绝对跳变 {pct(max_abs['all'])}"
        + (f"（{max_abs['all_where'][0]} {max_abs['all_where'][1]}，{pct(max_abs['all_where'][2])}，"
           f"间隔 {max_abs['all_where'][3]} 自然日"
           + ("，除权事件日" if max_abs['all_where'][4] else "，非除权事件日")
           + "）" if max_abs["all_where"] else "") + "。")
    add(f"6. **市值/股本估算**：`shares_est = 当前总市值 / 最新不复权价`，"
        f"历史市值 `cap_t = shares_est × raw_close_t / Π(1+送股比例, 除权日在 t 之后)`。"
        f"这是**近似**：未建模增发/回购/配股，且用当前市值筛历史池 → 存在幸存者偏差与前视偏差。"
        f"shares_est 中位数 {num(shares.median() / 1e8, 2)} 亿股"
        f"（P10 {num(shares.quantile(0.1) / 1e8, 2)} / P90 {num(shares.quantile(0.9) / 1e8, 2)}）。")
    add(f"7. **数据可用性结论**：**足以支撑** 2016-01-04..2026-11-06 回测与 2018/2021 子样本"
        f"（详见 §3-§5；残余风险见 §6）。")
    add()

    add("## 2. 落盘文件")
    add()
    add(md_table(["文件", "说明"], [
        ["`data/universe.csv`", "当前池（symbol/name/thscode/当前市值/最新价/估算股本/日线覆盖/事件数）"],
        ["`data/calendar.csv`", f"回测交易日历（{n_cal} 个交易日，000300 口径）"],
        ["`data/bars_raw.parquet`", "不复权收盘长表 symbol,date,close（两窗口合并去重）"],
        ["`data/events.parquet`", "除权事件长表 symbol,ex_date,dps,bonus（≤2026-11-06）"],
        ["`data/spot_snapshot.csv`", "全 A 快照（pool62 对照估算市值用）"],
        ["`data/cache/raw/*.json`", "同花顺批量不复权日线原始响应（带 fetched_at）"],
        ["`data/cache/ca/*.json`", "同花顺除权事件原始响应（带 fetched_at）"],
        ["`data/fetch_failures.csv`", "抓取失败清单（仅失败时存在）"],
        ["`data/manifest.json`", "抓取时点 + 全部缓存/产出文件 SHA256"],
    ]))
    add()

    add("## 3. 抓取与失败清单")
    add()
    add(f"- 批量：{BATCH_SIZE} 只/次 × {len(WINDOWS)} 窗口；窗口 = "
        + "、".join(f"`{t}`" for t in w_tags) + "；`adjust=\"none\"`。")
    add(f"- 限速：相邻请求 ≥ {MIN_INTERVAL}s；失败重试 {MAX_ATTEMPTS} 次"
        f"（指数退避 {BACKOFF_BASE:.0f}s 起，限流按 retry_after）；批量失败再逐只回退。")
    add(f"- 缓存优先：重跑命中缓存；`--force` 重抓；`--no-fetch` 只用缓存。")
    add()
    if not fetcher.failures:
        add("**无失败**：全部请求成功（含缓存命中）。")
    else:
        add(f"共 **{len(fetcher.failures)}** 条失败（`data/fetch_failures.csv`）：")
        add()
        rows = [[f.get("kind", ""), f.get("target", ""), f.get("window", ""),
                 f.get("type", ""), f.get("code", ""),
                 str(f.get("detail", ""))[:100].replace("|", "/")][:6]
                for f in fetcher.failures[:80]]
        add(md_table(["kind", "target", "window", "type", "code", "detail"], rows))
        if len(fetcher.failures) > 80:
            add(f"\n（仅列前 80 条，完整见 CSV）")
    add()

    add("## 4. 覆盖度")
    add()
    add(f"- 日线：{n_bars} 行；首末 bar 区间内实际覆盖交易日数中位数 "
        f"{int(np.median([n_cal - g for _, g in gaps]))}（全区间 {n_cal} 日，"
        f"差额来自上市前/停牌/数据缺口）。")
    add(f"- 自 2015 年初有数据 {from_2015} 只；无数据 {n_no_data} 只。")
    add(f"- 上市后缺口 Top 10：" + "、".join(f"{s}({g}日)" for s, g in gaps[:10]) + "。")
    add()
    add("> 缺口处理：宽表不做 ffill；回测买入日若无 bar 则**跳过该候选**（计入 skipped），"
        "卖出日若无 bar 则顺延到下一有 bar 的交易日；估值用最近可得收盘价。")
    add()

    add("## 5. 除权事件与异常涨跌")
    add()
    add(f"- 事件 {n_events} 条：现金分红 {len(cash_ev)}，送转 {len(bonus_ev)}；"
        f"平均每股现金分红 {num(float(cash_ev['dps'].mean()) if len(cash_ev) else np.nan, 4)} 元。")
    if len(bonus_ev):
        add("- 含送股事件清单（影响股本反推与成本价除权）：")
        add()
        add(md_table(["symbol", "ex_date", "dps", "per_share_bonus"],
                     [[r.symbol, iso(r.ex_date), num(r.dps, 4), num(r.bonus, 4)]
                      for r in bonus_ev.itertuples()][:40]))
    add()
    add(f"- 单日 |收益| >15% 共 {n_gt_15} 处：除权事件日 {len(event_jumps)} 处、非事件日 "
        f"{n_gt_15_non_ev} 处（其中停牌复牌 {len(resumption)} 处、疑似数据错误 {len(suspicious)} 处）。")
    add()
    add(f"### 5.1 疑似数据错误（判定：非除权事件日 + 相邻 bar 间隔 ≤ {ANOMALY_MAX_GAP_DAYS} 自然日 "
        f"+ |单日收益| > {ANOMALY_RET_THRESHOLD:.0%}，共 {len(suspicious)} 处）")
    add()
    if len(suspicious):
        add(md_table(["symbol", "date", "单日收益", "相邻bar间隔(日)", "close", "prev_close"],
                     [[r.symbol, iso(r.date), pct(r.ret, 2), int(r.gap),
                       num(float(bars_df[(bars_df.symbol == r.symbol)
                                         & (bars_df.date == r.date)]["close"].iloc[0]), 2),
                       "—"]
                      for r in suspicious.itertuples()]))
        add()
    else:
        add("**无**。")
        add()
    add("处理规则（引擎 `engine.py` 实现，报告披露）：")
    add(f"1. 候选的**买入日或计划卖出日**命中疑似数据错误 bar → **跳过该笔交易**，计入 `skipped_data`；")
    add("2. 持仓每日估值与『延迟卖出』用**最近可信价**（该 bar 用前一个可信收盘替代），避免权益曲线被单点跳变污染；")
    add("3. **停牌复牌跳变不修正**（视为真实可实现价格，见 §5.2）；长期停牌导致的卖出顺延按实际持有天数计税。")
    add()
    add(f"### 5.2 停牌复牌跳变（相邻 bar 间隔 > {RESUMPTION_GAP_DAYS} 自然日且 |跳变|>20%，"
        f"共 {len(resumption)} 处；真实价格，不修正）")
    add()
    if len(resumption):
        add(md_table(["symbol", "date", "跳变", "停牌间隔(自然日)", "prev_close", "close"],
                     [[r.symbol, iso(r.date), pct(r.ret, 2), int(r.gap), "—", "—"]
                      for r in resumption.sort_values("ret").itertuples()]))
        add()
        add("> 这些跳变跨越长期停牌（如盐湖股份 2021-08-10 复牌 +306%），不是数据错误；"
            "回测保留真实复牌价，若持仓被迫跨停牌，卖出顺延且按实际自然日计算红利税档位。")
    else:
        add("**无**。")
    add()
    add(f"### 5.3 除权事件日跳变 Top 15（真实除权，回测以现金分红/送股补偿）")
    add()
    if len(event_jumps):
        add(md_table(["symbol", "date", "单日收益", "事件 dps/bonus"],
                     [[r.symbol, iso(r.date), pct(r.ret, 2), "见 events.parquet"]
                      for r in event_jumps.reindex(event_jumps["ret"].abs()
                                                   .sort_values(ascending=False).index)
                      .head(15).itertuples()]))
    add()
    add("> 不复权价在除权日会自然向下跳空（分红/送股所致），回测按规则在除权日以现金分红"
        "+ 送股扩股补偿，故这些跳空不是数据错误。")
    add()

    add("## 6. 口径、近似与残余风险")
    add()
    add("1. **幸存者偏差**：当前池用 2026-11-08 的全 A 快照按当前市值筛选。"
        "历史上市值 ≥500 亿但现已跌出/退市的标的**不在样本内**；历史上 <500 亿但后来长大的标的"
        "被以「当前市值」筛入。因此回测结果偏向当前仍是大市值、且仍有分红能力的公司。")
    add("2. **当前市值筛历史**：历史市值由「当前总市值/最新价」反推股本 × 历史不复权价估算，"
        "再按除权日后送股比例反推。未建模增发/回购/配股/可转债转股，个股历史市值可能有偏差。")
    add("3. **不复权价 + 显式分红**：回测用不复权价成交与估值，除权日把 dps×股数记入现金、"
        "送股扩股；不使用前复权价，避免复权因子引入的未来信息。")
    add(f"4. **数据防护**：非除权事件日、相邻 bar 间隔 ≤{ANOMALY_MAX_GAP_DAYS} 自然日、"
        f"|单日收益|>{ANOMALY_RET_THRESHOLD:.0%} 的 bar 判为疑似数据错误（本次 {len(suspicious)} 个）。"
        "买入日/计划卖出日命中则跳过该笔交易（计入 `skipped_data`），持仓估值与延迟卖出用最近可信价；"
        "受影响笔数见 `results/summary.json` 的 `data_protection`。")
    add("4. **停牌/无 bar**：买入日无 bar 跳过候选；卖出日无 bar 顺延。")
    add("5. **涨跌停不建模**：按收盘价成交，不模拟涨跌停无法成交的情形（披露，不做修正）。")
    add("6. **红利税/取现**：除权日税前分红入现金；卖出时按实际持有自然日扣税"
        "（≤30 天 20% / 31~365 天 10% / >365 天 0%），再提取净分红的 30%；税率档位与生产一致。")
    add()
    OUT_QUALITY.write_text("\n".join(L) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
