#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dividend-alpha-v2 / research/dividend_capture —— point-in-time 全 A 数据抓取

task-3：用**买入日当时总市值**选池，替代上一版「当前市值≥500亿」筛池。

阶段（`--phase`）：
  bars      全 A 两段不复权日线（同花顺 fuyao，10 只/批，0.35s 间隔）→ data/cache/raw_all/
  assemble  解析缓存 → data/bars_all.parquet + 粗筛清单（max 粗市值≥200 亿）
  ca        粗筛标的除权事件（同花顺 adjustment_factors）→ data/cache/ca_all/
  shares    精筛标的 akshare stock_share_change_cninfo 历史总股本 → data/cache/shares/
  delisted  退市股（akshare 退市清单 + 东财 F10/行情）→ 追加缓存
  build     组装 data/{events_all,shares_history}.parquet、universe_all.csv、pit_universe.csv、
            DATA_QUALITY_ALL.md、manifest_all.json

缓存优先：任何阶段重跑命中缓存；`--force` 重抓。失败清单落 data/fetch_failures_all.csv。

全 A 快照：东方财富 fetch_all_a_spot()（约 5,563 只）——不再按当前市值筛选，只排 ST/退。
历史总股本：akshare stock_share_change_cninfo（万股 ×1e4）。
退市股：同花顺已无数据（实测 3001/1002），改用东财 push2his 行情 + 东财 F10 股本/分红；
       仍抓不到的明确披露。
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

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

DATA = HERE / "data"
CACHE = DATA / "cache"
RAW_ALL = CACHE / "raw_all"
CA_ALL = CACHE / "ca_all"
SHARES_DIR = CACHE / "shares"
RESULTS = HERE / "results"

WINDOWS = [(date(2015, 1, 1), date(2024, 12, 31)),
           (date(2024, 12, 1), date(2026, 11, 6))]
CA_SINCE = date(2014, 11, 1)
TAIL_DATE = date(2026, 11, 6)
BATCH_SIZE = 10
MIN_INTERVAL = 0.35
BACKOFF_BASE = 2.0
MAX_ATTEMPTS = 4
COARSE_CAP = 2.0e10       # 粗筛：max 历史（估算）市值 ≥200 亿
SHARES_START = "20140101"
SHARES_END = "20261106"
DELIST_MIN_DATE = date(2015, 1, 1)

OUT_SPOT = DATA / "spot_all.csv"
OUT_BARS = DATA / "bars_all.parquet"
OUT_DELISTED_BARS = DATA / "bars_delisted.parquet"
OUT_EVENTS = DATA / "events_all.parquet"
OUT_SHARES = DATA / "shares_history.parquet"
OUT_COARSE = DATA / "coarse_list.csv"
OUT_SHARES_LIST = DATA / "shares_list.csv"
OUT_UNIVERSE = DATA / "universe_all.csv"
OUT_PIT = DATA / "pit_universe.csv"
OUT_QUALITY = DATA / "DATA_QUALITY_ALL.md"
OUT_MANIFEST = DATA / "manifest_all.json"
OUT_FAILURES = DATA / "fetch_failures_all.csv"
OUT_DELISTED = DATA / "delisted_list.csv"


def now_str() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S%z")


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def load_env() -> None:
    p = ROOT / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def ms_to_date(ms: int) -> date:
    return (datetime.utcfromtimestamp(int(ms) / 1000) + timedelta(hours=8)).date()


def iso(d) -> str:
    if isinstance(d, date):
        return d.strftime("%Y-%m-%d")
    return str(d)[:10]


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def chunked(seq: list, n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def thscode(sym: str) -> str:
    return sym[2:] + "." + sym[:2].upper()


# ---------------------------------------------------------------------------
# 快照 / 池
# ---------------------------------------------------------------------------
def load_spot(force: bool) -> pd.DataFrame:
    path = CACHE / "spot_all.json"
    if path.exists() and not force:
        obj = json.loads(path.read_text(encoding="utf-8"))
        log(f"全 A 快照命中缓存（{obj.get('fetched_at')}，{len(obj['rows'])} 行）")
        return pd.DataFrame(obj["rows"])
    from src.da_data.providers.eastmoney import fetch_all_a_spot

    last_err = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            df = fetch_all_a_spot()
            if df is None or df.empty:
                raise RuntimeError("快照为空")
            CACHE.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"fetched_at": now_str(),
                                        "rows": df.to_dict(orient="records")},
                                       ensure_ascii=False), encoding="utf-8")
            log(f"全 A 快照抓取成功：{len(df)} 行")
            return df
        except Exception as e:  # noqa: BLE001
            last_err = e
            log(f"快照第 {attempt} 次失败: {type(e).__name__}: {e}")
            time.sleep(BACKOFF_BASE * (2 ** (attempt - 1)))
    if path.exists():
        obj = json.loads(path.read_text(encoding="utf-8"))
        log(f"⚠ 快照抓取失败，回退旧缓存 {obj.get('fetched_at')}: {last_err}")
        return pd.DataFrame(obj["rows"])
    raise RuntimeError(f"全 A 快照失败且无缓存: {last_err}")


def build_spot_table(spot: pd.DataFrame) -> pd.DataFrame:
    df = spot.copy()
    df["market_cap"] = pd.to_numeric(df["market_cap"], errors="coerce")
    df["last"] = pd.to_numeric(df["last"], errors="coerce")
    df["name"] = df["name"].fillna("")
    df["is_st_delist"] = df["name"].str.contains("ST|退", na=False)
    df["valid_code"] = df["symbol"].str.match(r"^(sh6|sz0|sz3)\d{5}$")
    df = df.drop_duplicates(subset=["symbol"]).sort_values("symbol").reset_index(drop=True)
    return df


# ---------------------------------------------------------------------------
# 同花顺抓取器
# ---------------------------------------------------------------------------
class ThsFetcher:
    def __init__(self, force: bool):
        from src.da_data.providers.ths import ThsFuyaoClient

        key = os.environ.get("THS_API_KEY", "")
        if not key:
            raise RuntimeError("THS_API_KEY 未配置")
        self.c = ThsFuyaoClient(api_key=key, min_interval_sec=MIN_INTERVAL)
        self.force = force
        self.stats = {"requests": 0, "cache_hits": 0, "fetched": 0, "retries": 0}
        self.failures: list[dict] = []

    def _call(self, fn, label: str):
        from src.da_data.providers.base import BizError, RateLimited

        last = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                return fn(), None
            except RateLimited as e:
                last = {"type": "rate_limited", "code": str(e.code), "detail": str(e)[:160]}
                delay = max(float(e.retry_after or 0.0), BACKOFF_BASE * (2 ** (attempt - 1)))
                self.stats["retries"] += 1
                time.sleep(delay)
            except BizError as e:
                last = {"type": "biz_error", "code": str(e.code), "detail": str(e)[:160]}
                if str(e.code) in ("1002", "3002", "3001"):
                    return None, last      # 确定性错误：不重试
                self.stats["retries"] += 1
                time.sleep(BACKOFF_BASE * (2 ** (attempt - 1)))
            except Exception as e:  # noqa: BLE001
                last = {"type": type(e).__name__, "code": None, "detail": str(e)[:160]}
                self.stats["retries"] += 1
                time.sleep(BACKOFF_BASE * (2 ** (attempt - 1)))
        return None, last

    def raw_path(self, codes: list[str], w0: date, w1: date) -> Path:
        tag = f"{iso(w0)}_{iso(w1)}"
        return RAW_ALL / f"raw_{'_'.join(c.replace('.', '_') for c in codes)}_{tag}.json"

    def fetch_raw_batch(self, codes: list[str], w0: date, w1: date) -> dict | None:
        path = self.raw_path(codes, w0, w1)
        if path.exists() and not self.force:
            try:
                obj = json.loads(path.read_text(encoding="utf-8"))
                data = obj["payload"]
                if isinstance(data.get("item"), list) and len(data["item"]) > 0:
                    self.stats["cache_hits"] += 1
                    return data
            except Exception as e:  # noqa: BLE001
                log(f"缓存损坏 {path.name}: {e}")
        self.stats["requests"] += 1
        data, err = self._call(
            lambda: self.c.prices_historical_batch(codes, w0, w1, adjust="none"),
            label=f"raw[{len(codes)}]")
        if err is not None or data is None:
            merged = {"interval": "1d", "adjust": "none", "item": []}
            for t in codes:
                self.stats["requests"] += 1
                d1, e1 = self._call(
                    lambda t=t: self.c.prices_historical_batch([t], w0, w1, adjust="none"),
                    label=f"raw[{t}]")
                if e1 is not None or d1 is None:
                    self.failures.append({"kind": "raw", "target": t,
                                          "window": f"{iso(w0)}_{iso(w1)}",
                                          **e1, "when": now_str()})
                else:
                    merged["item"].extend(d1.get("item") or [])
            if not merged["item"]:
                return None
            data = merged
        self.stats["fetched"] += 1
        RAW_ALL.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"fetched_at": now_str(), "thscodes": codes,
                                    "start": iso(w0), "end": iso(w1), "payload": data},
                                   ensure_ascii=False), encoding="utf-8")
        return data

    def fetch_ca(self, ths: str) -> list[dict] | None:
        path = CA_ALL / f"ca_{ths}.json"
        if path.exists() and not self.force:
            try:
                obj = json.loads(path.read_text(encoding="utf-8"))
                rows = obj["payload"]
                if isinstance(rows, list):
                    self.stats["cache_hits"] += 1
                    return rows
            except Exception as e:  # noqa: BLE001
                log(f"CA 缓存损坏 {path.name}: {e}")
        self.stats["requests"] += 1
        rows, err = self._call(lambda: self.c.adjustment_factors(ths, CA_SINCE),
                               label=f"ca[{ths}]")
        if err is not None:
            if str(err.get("code")) == "3002":
                rows = []
            else:
                return None
        self.stats["fetched"] += 1
        CA_ALL.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"fetched_at": now_str(), "thscode": ths,
                                    "since": iso(CA_SINCE), "payload": rows},
                                   ensure_ascii=False), encoding="utf-8")
        return rows


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------
def parse_raw(payload: dict) -> dict[str, dict[date, float]]:
    out: dict[str, dict[date, float]] = {}
    for it in payload.get("item") or []:
        ths = str(it.get("thscode") or "")
        if "." not in ths:
            continue
        code, exch = ths.split(".", 1)
        sym = exch.lower() + code
        d: dict[date, float] = {}
        for b in it.get("item") or []:
            ms, close = b.get("date_ms"), b.get("close_price")
            if ms is None or close is None:
                continue
            try:
                c = float(close)
            except (TypeError, ValueError):
                continue
            if c > 0:
                d[ms_to_date(int(ms))] = c
        out[sym] = d
    return out


def parse_ca(rows: list[dict] | None) -> list[dict]:
    out = []
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


def load_calendar() -> list[date]:
    p = ROOT / "research" / "rebalance" / "data" / "index_000300.csv"
    df = pd.read_csv(p)
    lo, hi = date(2015, 1, 5), TAIL_DATE
    out = [datetime.strptime(str(x)[:10], "%Y-%m-%d").date() for x in df["date"]]
    out = [d for d in out if lo <= d <= hi]
    if out[-1] != hi:
        raise RuntimeError(f"日历末日 {out[-1]} != {hi}")
    return out


# ---------------------------------------------------------------------------
# 阶段实现
# ---------------------------------------------------------------------------
def phase_bars(args) -> None:
    spot = load_spot(args.force)
    tab = build_spot_table(spot)
    tab.to_csv(OUT_SPOT, index=False, encoding="utf-8", float_format="%.6f")
    symbols = list(tab[tab["valid_code"] & ~tab["is_st_delist"]]["symbol"])
    log(f"全 A {len(tab)} 行；剔除 ST/退 {int(tab['is_st_delist'].sum())} 与无效代码后 "
        f"→ {len(symbols)} 只参与日线抓取")
    codes = [thscode(s) for s in symbols]
    batches = list(chunked(codes, BATCH_SIZE))
    fetcher = ThsFetcher(args.force)
    t0 = time.time()
    for wi, (w0, w1) in enumerate(WINDOWS, start=1):
        log(f"窗口 {wi}/{len(WINDOWS)}: {iso(w0)} .. {iso(w1)}（{len(batches)} 批）")
        for bi, batch in enumerate(batches, start=1):
            fetcher.fetch_raw_batch(batch, w0, w1)
            if bi % 25 == 0 or bi == len(batches):
                elapsed = time.time() - t0
                done = (wi - 1) * len(batches) + bi
                total = len(WINDOWS) * len(batches)
                eta = elapsed / done * (total - done)
                log(f"  {done}/{total} 批  实抓 {fetcher.stats['fetched']} "
                    f"缓存 {fetcher.stats['cache_hits']} 失败 {len(fetcher.failures)} "
                    f"已用 {elapsed / 60:.1f}min ETA {eta / 60:.1f}min")
    if fetcher.failures:
        pd.DataFrame(fetcher.failures).to_csv(OUT_FAILURES, index=False, encoding="utf-8")
    log(f"bars 阶段完成：{fetcher.stats} 失败 {len(fetcher.failures)} "
        f"用时 {(time.time() - t0) / 60:.1f}min")
    log("下一步：python research/dividend_capture/data_fetch_all.py --phase assemble")


def phase_assemble(args) -> None:
    """解析全 A 日线缓存 → bars_all.parquet；粗筛 max 历史市值≥200 亿。"""
    spot = pd.read_csv(OUT_SPOT)
    spot["market_cap"] = pd.to_numeric(spot["market_cap"], errors="coerce")
    spot["last"] = pd.to_numeric(spot["last"], errors="coerce")
    spot_by = {r.symbol: r for r in spot.itertuples()}
    bars: dict[str, dict[date, float]] = {}
    files = sorted(RAW_ALL.glob("raw_*.json"))
    log(f"解析 {len(files)} 个日线缓存文件…")
    for i, p in enumerate(files, start=1):
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
            for sym, d in parse_raw(obj["payload"]).items():
                bars.setdefault(sym, {}).update(d)
        except Exception as e:  # noqa: BLE001
            log(f"✗ 解析失败 {p.name}: {e}")
        if i % 200 == 0:
            log(f"  {i}/{len(files)}")
    rows = [(s, d, float(c)) for s, dm in bars.items() for d, c in dm.items()]
    bars_df = pd.DataFrame(rows, columns=["symbol", "date", "close"])
    bars_df = bars_df.sort_values(["symbol", "date"]).reset_index(drop=True)
    bars_df.to_parquet(OUT_BARS, index=False)
    log(f"bars_all: {len(bars_df)} 行 / {bars_df['symbol'].nunique()} 只")

    # 粗筛：shares_est = 当前市值 / 最新价；max 粗市值 = shares_est × max(历史 close)
    recs = []
    for s, dm in bars.items():
        if not dm:
            continue
        r = spot_by.get(s)
        if r is None or not np.isfinite(float(r.market_cap)):
            continue
        price_now = float(r.last) if np.isfinite(float(r.last)) else float(dm[max(dm)])
        if price_now <= 0:
            continue
        shares_est = float(r.market_cap) / price_now
        max_close = max(float(c) for c in dm.values())
        max_cap_rough = shares_est * max_close
        recs.append({"symbol": s, "name": r.name, "market_cap_now": float(r.market_cap),
                     "price_now": price_now, "shares_est": shares_est,
                     "max_close": max_close, "max_cap_rough": max_cap_rough,
                     "n_bars": len(dm), "first_date": iso(min(dm)), "last_date": iso(max(dm))})
    uni = pd.DataFrame(recs).sort_values("max_cap_rough", ascending=False).reset_index(drop=True)
    uni.to_csv(OUT_UNIVERSE, index=False, encoding="utf-8", float_format="%.6f")
    coarse = uni[uni["max_cap_rough"] >= COARSE_CAP].reset_index(drop=True)
    coarse.to_csv(OUT_COARSE, index=False, encoding="utf-8", float_format="%.6f")
    log(f"粗筛（max 估算市值≥{COARSE_CAP / 1e8:.0f}亿）→ {len(coarse)} 只（全池 {len(uni)} 只）")
    log("下一步：--phase ca")


def phase_ca(args) -> None:
    coarse = pd.read_csv(OUT_COARSE)
    symbols = list(coarse["symbol"])
    fetcher = ThsFetcher(args.force)
    t0 = time.time()
    events: dict[str, list[dict]] = {}
    for i, s in enumerate(symbols, start=1):
        rows = fetcher.fetch_ca(thscode(s))
        if rows is None:
            fetcher.failures.append({"kind": "ca", "target": s, "window": "",
                                     "type": "fail", "code": "",
                                     "detail": "adjustment_factors 失败", "when": now_str()})
            events[s] = []
        else:
            events[s] = parse_ca(rows)
        if i % 100 == 0 or i == len(symbols):
            log(f"  CA {i}/{len(symbols)} 失败 {len(fetcher.failures)} "
                f"已用 {(time.time() - t0) / 60:.1f}min")
    # 精筛：用送股反推估算股本，max 精市值 = shares_est_t × close_t
    bars_df = pd.read_parquet(OUT_BARS)
    by_sym = {s: dict(zip(g["date"], g["close"]))
              for s, g in bars_df.groupby("symbol")}
    refined = []
    for s in symbols:
        dm = by_sym.get(s) or {}
        if not dm:
            continue
        row = coarse[coarse["symbol"] == s].iloc[0]
        shares_now = float(row["shares_est"])
        evs = sorted([(e["ex_date"], 1.0 + e["bonus"]) for e in events.get(s, [])
                      if e["bonus"] > 0])
        # 后缀乘积：Π(1+bonus, ex_date > t)
        dates = [e[0] for e in evs]
        suffix = [1.0] * (len(evs) + 1)
        for k in range(len(evs) - 1, -1, -1):
            suffix[k] = suffix[k + 1] * evs[k][1]
        import bisect

        max_cap = 0.0
        for d, c in dm.items():
            i_ = bisect.bisect_right(dates, d)
            mult = suffix[i_] if i_ < len(suffix) else 1.0
            cap = shares_now * float(c) / mult
            if cap > max_cap:
                max_cap = cap
        refined.append({"symbol": s, "name": row["name"], "shares_now_est": shares_now,
                        "max_cap_refined": max_cap, "n_events": len(events.get(s, [])),
                        "n_cash_events": sum(1 for e in events.get(s, []) if e["dps"] > 0)})
    ref = pd.DataFrame(refined).sort_values("max_cap_refined", ascending=False).reset_index(drop=True)
    keep = ref[ref["max_cap_refined"] >= COARSE_CAP]
    keep.to_csv(OUT_SHARES_LIST, index=False, encoding="utf-8", float_format="%.6f")
    ev_rows = []
    for s in keep["symbol"]:
        for e in events.get(s, []):
            if e["ex_date"] <= TAIL_DATE:
                ev_rows.append((s, e["ex_date"], float(e["dps"]), float(e["bonus"])))
    ev_df = pd.DataFrame(ev_rows, columns=["symbol", "ex_date", "dps", "bonus"])
    if len(ev_df):
        ev_df = (ev_df.groupby(["symbol", "ex_date"], as_index=False)
                 .agg(dps=("dps", "sum"), bonus=("bonus", "sum")))
    ev_df = ev_df.sort_values(["symbol", "ex_date"]).reset_index(drop=True)
    ev_df.to_parquet(OUT_EVENTS, index=False)
    if fetcher.failures:
        pd.DataFrame(fetcher.failures).to_csv(OUT_FAILURES, index=False, encoding="utf-8")
    log(f"ca 阶段完成：粗筛 {len(symbols)} 只 → 精筛 {len(keep)} 只，"
        f"事件 {len(ev_df)} 条，失败 {len(fetcher.failures)}，"
        f"用时 {(time.time() - t0) / 60:.1f}min")
    log("下一步：--phase shares")


# ---------------------------------------------------------------------------
# akshare 历史总股本
# ---------------------------------------------------------------------------
def akshare_shares(symbol6: str) -> pd.DataFrame | None:
    import akshare as ak

    try:
        df = ak.stock_share_change_cninfo(symbol=symbol6, start_date=SHARES_START,
                                          end_date=SHARES_END)
    except Exception:  # noqa: BLE001
        return None
    if df is None or len(df) == 0:
        return None
    return df


def phase_shares(args) -> None:
    sl = pd.read_csv(OUT_SHARES_LIST)
    symbols = list(sl["symbol"])
    log(f"历史总股本抓取：{len(symbols)} 只")
    SHARES_DIR.mkdir(parents=True, exist_ok=True)
    failures = []
    t0 = time.time()
    for i, s in enumerate(symbols, start=1):
        code = s[2:]
        path = SHARES_DIR / f"shares_{s}.json"
        if path.exists() and not args.force:
            continue
        df = None
        last = None
        for attempt in range(1, 4):
            try:
                df = akshare_shares(code)
                break
            except Exception as e:  # noqa: BLE001
                last = e
                time.sleep(1.0 * attempt)
        if df is None:
            failures.append({"symbol": s, "error": str(last)[:160] if last else "empty"})
            path.write_text(json.dumps({"fetched_at": now_str(), "symbol": s,
                                        "payload": None, "error": "empty"}, ensure_ascii=False),
                            encoding="utf-8")
        else:
            cols = df.columns.tolist()
            # 变动日期、总股本（万股）；列名依赖 akshare 版本，做容错
            dcol = next((c for c in cols if "变动日期" in str(c)), None)
            tcol = next((c for c in cols if str(c) == "总股本"), None)
            recs = []
            if dcol is not None and tcol is not None:
                for r in df[[dcol, tcol]].itertuples(index=False):
                    try:
                        d = r[0]
                        if not isinstance(d, date):
                            d = datetime.strptime(str(d)[:10], "%Y-%m-%d").date()
                        v = float(r[1])
                        if v > 0:
                            recs.append({"date": iso(d), "total_shares": v * 1e4})
                    except Exception:  # noqa: BLE001
                        continue
            recs.sort(key=lambda x: x["date"])
            path.write_text(json.dumps({"fetched_at": now_str(), "symbol": s,
                                        "payload": recs}, ensure_ascii=False),
                            encoding="utf-8")
        if i % 100 == 0 or i == len(symbols):
            log(f"  shares {i}/{len(symbols)} 失败 {len(failures)} "
                f"已用 {(time.time() - t0) / 60:.1f}min")
    if failures:
        pd.DataFrame(failures).to_csv(OUT_FAILURES, index=False, encoding="utf-8")
    log(f"shares 阶段完成：失败 {len(failures)} / {len(symbols)}，"
        f"用时 {(time.time() - t0) / 60:.1f}min")
    log("下一步：--phase delisted")


# ---------------------------------------------------------------------------
# 退市股（东财替代源）
# ---------------------------------------------------------------------------
EM_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://emweb.securities.eastmoney.com/"}
UA_QUOTE = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}


def _em_session():
    import requests

    s = requests.Session()
    s.trust_env = False
    s.proxies = {"http": None, "https": None}
    s.headers.update(UA_QUOTE)
    return s


def em_kline(sess, secid: str, w0: date, w1: date) -> dict[date, float]:
    r = sess.get("https://push2his.eastmoney.com/api/qt/stock/kline/get", params={
        "secid": secid, "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57", "klt": 101, "fqt": 0,
        "beg": int(w0.strftime("%Y%m%d")), "end": int(w1.strftime("%Y%m%d")),
        "lmt": 100000}, timeout=20)
    data = (r.json() or {}).get("data") or {}
    out = {}
    for line in data.get("klines") or []:
        f = str(line).split(",")
        try:
            d = datetime.strptime(f[0], "%Y-%m-%d").date()
            c = float(f[2])
        except (ValueError, IndexError):
            continue
        if c > 0:
            out[d] = c
    return out


def em_f10(code_with_ex: str) -> tuple[list[dict], list[dict]]:
    """返回 (股本变动 lngbbd, 分红方案 fhyx)。code_with_ex 形如 SH600401。"""
    sess = _em_session()
    sess.headers.update(EM_HEADERS)
    cap = sess.get("https://emweb.securities.eastmoney.com/PC_HSF10/CapitalStockStructure/PageAjax",
                   params={"code": code_with_ex}, timeout=20).json()
    bonus = sess.get("https://emweb.securities.eastmoney.com/PC_HSF10/BonusFinancing/PageAjax",
                     params={"code": code_with_ex}, timeout=20).json()
    return cap.get("lngbbd") or [], bonus.get("fhyx") or []


def parse_em_plan(plan: str) -> tuple[float, float]:
    """解析东财分红方案文本：'10派4.1元' / '10转10派0.28元' / '10送2转3派1元(含税)'。"""
    import re

    if not plan:
        return 0.0, 0.0
    dps = bonus = 0.0
    m = re.search(r"10\s*派\s*([0-9.]+)", plan)
    if m:
        dps = float(m.group(1)) / 10.0
    m = re.search(r"10\s*送\s*([0-9.]+)", plan)
    if m:
        bonus += float(m.group(1)) / 10.0
    m = re.search(r"10\s*转\s*([0-9.]+)", plan)
    if m:
        bonus += float(m.group(1)) / 10.0
    return dps, bonus


def delisted_symbols() -> pd.DataFrame:
    import akshare as ak

    rows = []
    try:
        sh = ak.stock_info_sh_delist()
        for r in sh.itertuples():
            code = str(r[1]).zfill(6)
            if not code.startswith("6"):
                continue
            rows.append({"symbol": "sh" + code, "name": str(r[2]),
                         "delist_date": iso(r[4])})
    except Exception as e:  # noqa: BLE001
        log(f"⚠ 上交所退市清单失败: {e}")
    try:
        sz = ak.stock_info_sz_delist()
        for r in sz.itertuples(index=False):
            code = str(r[0]).zfill(6)
            if not (code.startswith("0") or code.startswith("3")):
                continue
            rows.append({"symbol": "sz" + code, "name": str(r[1]),
                         "delist_date": iso(r[3])})
    except Exception as e:  # noqa: BLE001
        log(f"⚠ 深交所退市清单失败: {e}")
    df = pd.DataFrame(rows).drop_duplicates("symbol").reset_index(drop=True)
    df["delist_date"] = pd.to_datetime(df["delist_date"], errors="coerce").dt.date
    df = df[df["delist_date"] >= DELIST_MIN_DATE].reset_index(drop=True)
    return df


def phase_delisted(args) -> None:
    dl = delisted_symbols()
    dl.to_csv(OUT_DELISTED, index=False, encoding="utf-8")
    log(f"退市清单（≥2015 年退市、A 股）{len(dl)} 只；尝试东财行情 + F10 股本/分红")
    sess = _em_session()
    events: dict[str, list[dict]] = {}
    shares: dict[str, list[dict]] = {}
    bars: dict[str, dict[date, float]] = {}
    failures = []
    t0 = time.time()
    for i, r in enumerate(dl.itertuples(), start=1):
        s = r.symbol
        ex = s[:2].upper()
        secid = ("1." if s.startswith("sh") else "0.") + s[2:]
        code_ex = ex + s[2:]
        cache_path = CACHE / "delisted" / f"dl_{s}.json"
        try:
            dm: dict[date, float] = {}
            if cache_path.exists():
                obj = json.loads(cache_path.read_text(encoding="utf-8"))
                dm = {datetime.strptime(b["date"], "%Y-%m-%d").date(): float(b["close"])
                      for b in obj["bars"]}
                sh_recs = obj["shares"]
                ev_recs = [{**e, "ex_date": datetime.strptime(e["ex_date"], "%Y-%m-%d").date()}
                           for e in obj["events"]]
                bars[s] = dm
                shares[s] = sh_recs
                events[s] = ev_recs
                continue
            for w0, w1 in WINDOWS:
                dm.update(em_kline(sess, secid, w0, w1))
            if not dm:
                failures.append({"symbol": s, "error": "no_kline"})
                continue
            cap_rows, bonus_rows = em_f10(code_ex)
            sh_recs = []
            for x in cap_rows:
                try:
                    d = datetime.strptime(str(x.get("END_DATE"))[:10], "%Y-%m-%d").date()
                    v = float(x.get("TOTAL_SHARES") or 0)
                except (TypeError, ValueError):
                    continue
                if v > 0:
                    sh_recs.append({"date": iso(d), "total_shares": v})
            sh_recs.sort(key=lambda x: x["date"])
            ev_recs = []
            for x in bonus_rows:
                exd = x.get("EX_DIVIDEND_DATE")
                plan = str(x.get("IMPL_PLAN_PROFILE") or "")
                if not exd:
                    continue
                try:
                    ed = datetime.strptime(str(exd)[:10], "%Y-%m-%d").date()
                except ValueError:
                    continue
                dps, bonus = parse_em_plan(plan)
                if dps > 0 or bonus > 0:
                    ev_recs.append({"ex_date": ed, "dps": dps, "bonus": bonus})
            ev_recs.sort(key=lambda x: x["ex_date"])
            if not sh_recs:
                failures.append({"symbol": s, "error": "no_share_history"})
                continue
            bars[s] = dm
            shares[s] = sh_recs
            events[s] = ev_recs
            # 缓存（与同花顺阶段分开，便于审计）
            (RAW_ALL).mkdir(parents=True, exist_ok=True)
            (SHARES_DIR).mkdir(parents=True, exist_ok=True)
            (CACHE / "delisted").mkdir(parents=True, exist_ok=True)
            payload = {"fetched_at": now_str(), "symbol": s,
                       "bars": [{"date": iso(d), "close": float(c)} for d, c in sorted(dm.items())],
                       "events": [{"ex_date": iso(e["ex_date"]), "dps": e["dps"],
                                   "bonus": e["bonus"]} for e in ev_recs],
                       "shares": sh_recs}
            (CACHE / "delisted" / f"dl_{s}.json").write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            failures.append({"symbol": s, "error": f"{type(e).__name__}: {e}"[:160]})
        if i % 50 == 0 or i == len(dl):
            log(f"  退市股 {i}/{len(dl)} 成功 {len(bars)} 失败 {len(failures)} "
                f"已用 {(time.time() - t0) / 60:.1f}min")
    if failures:
        pd.DataFrame(failures).to_csv(DATA / "delisted_failures.csv", index=False,
                                      encoding="utf-8")
    log(f"delisted 阶段完成：成功 {len(bars)} / {len(dl)}，失败 {len(failures)}，"
        f"用时 {(time.time() - t0) / 60:.1f}min")
    log("下一步：--phase build")


def phase_build(args) -> None:
    """组装最终数据集：events_all（含退市）、shares_history、bars_delisted、pit_universe。

    内存优化：bars_all（10.6M 行）不重写，只按 PIT 候选集过滤后读入；退市股日线单独成 parquet。
    """
    log("组装最终数据集…")
    dl_dir = CACHE / "delisted"
    dl_syms: list[str] = []
    dl_bar_rows: list[tuple] = []
    if dl_dir.exists():
        for p in sorted(dl_dir.glob("dl_*.json")):
            obj = json.loads(p.read_text(encoding="utf-8"))
            s = obj["symbol"]
            dl_syms.append(s)
            for b in obj["bars"]:
                dl_bar_rows.append((s, datetime.strptime(b["date"], "%Y-%m-%d").date(),
                                    float(b["close"])))
    if dl_bar_rows:
        pd.DataFrame(dl_bar_rows, columns=["symbol", "date", "close"]).to_parquet(
            OUT_DELISTED_BARS, index=False)
    log(f"退市股日线：{len(dl_syms)} 只 / {len(dl_bar_rows)} 行 → {OUT_DELISTED_BARS.name}")

    # events：同花顺精筛 + 退市（幂等：先剔除输入里已有的退市股事件，避免重复累加）
    ev_df = pd.read_parquet(OUT_EVENTS) if OUT_EVENTS.exists() else pd.DataFrame(
        columns=["symbol", "ex_date", "dps", "bonus"])
    if len(ev_df) and dl_syms:
        ev_df = ev_df[~ev_df["symbol"].isin(set(dl_syms))].reset_index(drop=True)
    ev_rows = list(ev_df.itertuples(index=False, name=None))
    for s in dl_syms:
        obj = json.loads((dl_dir / f"dl_{s}.json").read_text(encoding="utf-8"))
        for e in obj["events"]:
            ev_rows.append((s, datetime.strptime(e["ex_date"], "%Y-%m-%d").date(),
                            float(e["dps"]), float(e["bonus"])))
    ev_df = pd.DataFrame(ev_rows, columns=["symbol", "ex_date", "dps", "bonus"])
    if len(ev_df):
        ev_df = (ev_df.groupby(["symbol", "ex_date"], as_index=False)
                 .agg(dps=("dps", "sum"), bonus=("bonus", "sum")))
    ev_df = ev_df.sort_values(["symbol", "ex_date"]).reset_index(drop=True)
    ev_df.to_parquet(OUT_EVENTS, index=False)
    log(f"events_all：{len(ev_df)} 条 / {ev_df['symbol'].nunique()} 只")

    # shares_history：akshare（或临时粗筛缓存）+ 退市（东财 F10）
    sh_rows = []
    for p in sorted(SHARES_DIR.glob("shares_*.json")):
        obj = json.loads(p.read_text(encoding="utf-8"))
        for rec in obj.get("payload") or []:
            sh_rows.append((obj["symbol"], datetime.strptime(rec["date"], "%Y-%m-%d").date(),
                            float(rec["total_shares"]), "akshare_cninfo"))
    for s in dl_syms:
        obj = json.loads((dl_dir / f"dl_{s}.json").read_text(encoding="utf-8"))
        for rec in obj["shares"]:
            sh_rows.append((s, datetime.strptime(rec["date"], "%Y-%m-%d").date(),
                            float(rec["total_shares"]), "eastmoney_f10"))
    sh_df = pd.DataFrame(sh_rows, columns=["symbol", "change_date", "total_shares", "source"])
    sh_df = sh_df.drop_duplicates(["symbol", "change_date"], keep="last").sort_values(
        ["symbol", "change_date"]).reset_index(drop=True)
    sh_df.to_parquet(OUT_SHARES, index=False)
    log(f"shares_history：{len(sh_df)} 条 / {sh_df['symbol'].nunique()} 只 "
        f"(akshare {sh_df[sh_df.source == 'akshare_cninfo'].symbol.nunique()} 只, "
        f"eastmoney {sh_df[sh_df.source == 'eastmoney_f10'].symbol.nunique()} 只)")

    # pit_universe：有股本历史且有事件的标的（最终候选集）
    syms = sorted(set(sh_df["symbol"]) & set(ev_df["symbol"]))
    log(f"PIT 候选集（有历史股本 ∩ 有除权事件）：{len(syms)} 只")

    # 只加载候选集日线（全 A parquet 过滤后读入，避免 10.6M 行的 python dict）
    bars_df = pd.read_parquet(OUT_BARS)
    bars_df = bars_df[bars_df["symbol"].isin(set(syms))]
    bars = {s: dict(zip(g["date"], g["close"])) for s, g in bars_df.groupby("symbol")}
    if dl_bar_rows:
        dl_bars_df = pd.DataFrame(dl_bar_rows, columns=["symbol", "date", "close"])
        for s, g in dl_bars_df.groupby("symbol"):
            bars.setdefault(s, {}).update(dict(zip(g["date"], g["close"])))
    log(f"候选集日线：{sum(len(v) for v in bars.values())} 行 / {len(bars)} 只")

    # 选池数量随年份变化（cap≥500 亿的标的数，按每年首个交易日估算）
    cal = load_calendar()
    sh_by = {s: g for s, g in sh_df.groupby("symbol")}
    import bisect

    def shares_at(s: str, d: date) -> float | None:
        g = sh_by.get(s)
        if g is None or not len(g):
            return None
        dates = list(g["change_date"])
        i = bisect.bisect_right(dates, d) - 1
        if i < 0:
            return 0.0
        return float(g["total_shares"].iloc[i])

    # --- 向量化：每只标的的 (bar日期序数, close) 与 (股本变动日期序数, total_shares)
    bar_arrays: dict[str, tuple] = {}
    for s in syms:
        dm = bars.get(s) or {}
        if not dm:
            continue
        ds_ = sorted(dm)
        ords = np.array([d.toordinal() for d in ds_], dtype=np.int64)
        closes = np.array([float(dm[d]) for d in ds_], dtype=np.float64)
        g = sh_by.get(s)
        if g is None or not len(g):
            continue
        sh_dates = [x if isinstance(x, date) else datetime.strptime(str(x)[:10], "%Y-%m-%d").date()
                    for x in g["change_date"]]
        sh_ords = np.array([d.toordinal() for d in sh_dates], dtype=np.int64)
        sh_vals = np.asarray(g["total_shares"], dtype=np.float64)
        bar_arrays[s] = (ords, closes, sh_ords, sh_vals)

    def cap_series_at(s: str, d: date) -> float | None:
        arr = bar_arrays.get(s)
        if arr is None:
            return None
        ords, closes, sh_ords, sh_vals = arr
        ib = int(np.searchsorted(ords, d.toordinal(), side="right")) - 1
        if ib < 0:
            return None
        iv = int(np.searchsorted(sh_ords, ords[ib], side="right")) - 1
        if iv < 0:
            return None
        return float(sh_vals[iv] * closes[ib])

    yearly = []
    for y in range(2015, 2027):
        d = next((x for x in cal if x.year == y), None)
        if d is None:
            continue
        n = 0
        for s in syms:
            arr = bar_arrays.get(s)
            if arr is None:
                continue
            ords, closes, sh_ords, sh_vals = arr
            ib = int(np.searchsorted(ords, d.toordinal(), side="right")) - 1
            if ib < 0:
                continue
            iv = int(np.searchsorted(sh_ords, ords[ib], side="right")) - 1
            if iv < 0:
                continue
            if float(sh_vals[iv] * closes[ib]) >= 5e10:
                n += 1
        yearly.append({"year": y, "date": iso(d), "n_listed_cap_ge_500e": n})
    yearly = pd.DataFrame(yearly)
    log("按年份的「当时市值≥500亿」标的数：\n" + yearly.to_string(index=False))

    # 最大 PIT 市值 + 事件数（向量化）
    ev_counts = ev_df.groupby("symbol").size().to_dict() if len(ev_df) else {}
    recs = []
    for s in syms:
        arr = bar_arrays.get(s)
        if arr is None:
            continue
        ords, closes, sh_ords, sh_vals = arr
        idx = np.searchsorted(sh_ords, ords, side="right") - 1
        valid = idx >= 0
        if not valid.any():
            continue
        caps = sh_vals[idx[valid]] * closes[valid]
        recs.append({"symbol": s, "max_cap_pit": float(caps.max()),
                     "n_bars": int(len(ords)),
                     "first_date": iso(date.fromordinal(int(ords[0]))),
                     "last_date": iso(date.fromordinal(int(ords[-1]))),
                     "n_events": int(ev_counts.get(s, 0)),
                     "n_shares_records": int(len(sh_ords))})
    rec = pd.DataFrame(recs).sort_values("max_cap_pit", ascending=False).reset_index(drop=True)
    rec.to_csv(OUT_PIT, index=False, encoding="utf-8", float_format="%.2f")
    log(f"pit_universe.csv：{len(rec)} 只（max PIT 市值≥500亿 "
        f"{int((rec['max_cap_pit'] >= 5e10).sum())} 只，≥200亿 "
        f"{int((rec['max_cap_pit'] >= 2e10).sum())} 只）")

    # manifest
    files = []
    for p in sorted(RAW_ALL.glob("*.json")):
        files.append({"kind": "raw_all", "path": str(p.relative_to(ROOT)),
                      "bytes": p.stat().st_size, "sha256": sha256_of(p)})
    for p in sorted(CA_ALL.glob("*.json")):
        files.append({"kind": "ca_all", "path": str(p.relative_to(ROOT)),
                      "bytes": p.stat().st_size, "sha256": sha256_of(p)})
    for p in sorted(SHARES_DIR.glob("shares_*.json")):
        files.append({"kind": "shares", "path": str(p.relative_to(ROOT)),
                      "bytes": p.stat().st_size, "sha256": sha256_of(p)})
    for p in sorted(dl_dir.glob("dl_*.json")) if dl_dir.exists() else []:
        files.append({"kind": "delisted", "path": str(p.relative_to(ROOT)),
                      "bytes": p.stat().st_size, "sha256": sha256_of(p)})
    for p in [OUT_BARS, OUT_DELISTED_BARS, OUT_EVENTS, OUT_SHARES, OUT_PIT, OUT_SPOT]:
        files.append({"kind": "output", "path": str(p.relative_to(ROOT)),
                      "bytes": p.stat().st_size, "sha256": sha256_of(p)})
    manifest = {"generated_at": now_str(),
                "source": "THS fuyao bars/adjustment-factors (adjust=none); akshare "
                          "stock_share_change_cninfo (万股×1e4); delisted: EM push2his+F10",
                "universe_mode": "point_in_time",
                "spot_rows": int(len(pd.read_csv(OUT_SPOT))),
                "bars_symbols": int(pd.read_parquet(OUT_BARS, columns=["symbol"])
                                    ["symbol"].nunique()),
                "delisted_symbols": len(dl_syms),
                "delisted_with_bars": len(dl_bar_rows and set(x[0] for x in dl_bar_rows)),
                "pit_symbols": int(len(rec)),
                "yearly_cap500_count": yearly.to_dict(orient="records"),
                "failures": str(OUT_FAILURES.relative_to(ROOT)),
                "files": files}
    OUT_MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                            encoding="utf-8")
    log(f"manifest_all.json：{len(files)} 个文件")
    spot_tab = pd.read_csv(OUT_SPOT)
    dl_tab = (pd.read_csv(OUT_DELISTED) if OUT_DELISTED.exists()
              else pd.DataFrame(columns=["symbol", "name", "delist_date"]))
    write_quality(rec, bars_df, ev_df, sh_df, spot_tab, yearly, dl_tab, pit_uni=rec)
    log("build 阶段完成。下一步：engine 更新 + run_backtest")


# ---------------------------------------------------------------------------
# 数据质量报告
# ---------------------------------------------------------------------------
PHASES = {
    "bars": phase_bars,
    "assemble": phase_assemble,
    "ca": phase_ca,
    "shares": phase_shares,
    "delisted": phase_delisted,
    "build": phase_build,
}


def write_quality(rec: pd.DataFrame, bars_df: pd.DataFrame, ev_df: pd.DataFrame,
                  sh_df: pd.DataFrame, spot: pd.DataFrame, yearly: pd.DataFrame,
                  dl: pd.DataFrame, pit_uni: pd.DataFrame) -> None:
    import numpy as np

    L: list[str] = []

    def add(s: str = "") -> None:
        L.append(s)

    n_bars_sym = int(pd.read_parquet(OUT_BARS, columns=["symbol"])["symbol"].nunique())
    try:
        import pyarrow.parquet as pq

        n_bars_rows = int(pq.ParquetFile(OUT_BARS).metadata.num_rows)
    except Exception:  # noqa: BLE001
        n_bars_rows = -1
    n_events_sym = int(ev_df["symbol"].nunique())
    n_shares_sym = int(sh_df["symbol"].nunique())
    n_pit = int(len(rec))
    fallback_n = int((rec["max_cap_pit"] <= 0).sum())
    spot_rows = len(spot)
    st_n = int(spot["is_st_delist"].sum()) if "is_st_delist" in spot else 0
    add("# DATA_QUALITY_ALL.md —— point-in-time 全 A 数据质检报告（task-3）")
    add()
    add(f"- 生成时间：`{now_str()}`；脚本：`research/dividend_capture/data_fetch_all.py`（自动生成，勿手改）")
    add("- **选池口径**：全 A 当前上市股票全部作为候选，**买入日当时总市值 = 当时总股本 × 当日不复权收盘**，"
        "仅当 ≥500 亿才合格（不再用当前市值筛历史池）。")
    add("- 数据源：同花顺 fuyao（不复权日线 `adjust=none` + 除权事件）、"
        "akshare `stock_share_change_cninfo`（历史总股本，万股×1e4）、"
        "退市股：东方财富 push2his 行情 + F10 股本/分红（同花顺不提供退市股数据）")
    add(f"- 交易日历：`research/rebalance/data/index_000300.csv`（2877 个交易日，2015-01-05..2026-11-06）")
    add()
    add("## 1. 结论摘要")
    add()
    add(f"1. **全 A 快照**：{spot_rows} 行；剔除 ST/退 {st_n} 只；参与日线抓取 "
        f"{spot_rows - st_n - int((~spot['valid_code']).sum())} 只（另剔除无效代码）。")
    add(f"2. **日线**：{n_bars_sym} 只有日线，共 {n_bars_rows} 行（`bars_all.parquet`）+ "
        f"退市股日线 {len(pd.read_parquet(OUT_DELISTED_BARS)) if OUT_DELISTED_BARS.exists() else 0} 行"
        f"（`bars_delisted.parquet`）。")
    add(f"3. **除权事件**：{len(ev_df)} 条 / {n_events_sym} 只（同花顺精筛 + 退市股东财 F10）。")
    add(f"4. **历史总股本**：{n_shares_sym} 只 / {len(sh_df)} 条；"
        f"其中 akshare cninfo {int(sh_df[sh_df.source == 'akshare_cninfo'].symbol.nunique())} 只、"
        f"东财 F10（退市股）{int(sh_df[sh_df.source == 'eastmoney_f10'].symbol.nunique())} 只。")
    add(f"5. **PIT 候选集**：{n_pit} 只（有历史总股本 ∩ 有除权事件）；"
        f"最大 PIT 市值 ≥500 亿 {int((rec['max_cap_pit'] >= 5e10).sum())} 只。")
    add(f"6. **退市股**：退市清单（≥2015 年退市、A 股）{len(dl)} 只；"
        f"成功纳入行情+股本+分红 {int(len(dl) - len(dl) + len(set(rec['symbol']) & set(dl['symbol'])))} 只"
        f"（见 data/delisted_list.csv 与 data/delisted_failures.csv）。")
    add("7. **按年份的「当时市值≥500 亿」标的数**（PIT 口径，每年首个交易日）：")
    add()
    add("| 年份 | 日期 | 当时市值≥500亿 标的数 |")
    add("|---|---|---:|")
    for r in yearly.itertuples():
        add(f"| {r.year} | {r.date} | {r.n_listed_cap_ge_500e} |")
    add()
    add("## 2. 粗筛 → 精筛")
    add()
    add("- 粗筛：`shares_est = 当前总市值 / 最新不复权价`，`max 粗市值 = shares_est × 历史最高收盘`；"
        f"保留 max 粗市值 ≥200 亿 → {len(pd.read_csv(OUT_COARSE))} 只（全池 "
        f"{len(pd.read_csv(OUT_UNIVERSE))} 只）。")
    add("- 精筛：用除权送股事件反推估算股本后重算 `max 精市值`，保留 ≥200 亿 → "
        f"{len(pd.read_csv(OUT_SHARES_LIST))} 只，抓除权事件 + 历史总股本。")
    add("- 阈值 200 亿 < 500 亿，留 2.5 倍安全边际，避免因股本估算误差漏掉历史上曾 ≥500 亿的标的；"
        "残余漏筛风险见 §4。")
    add()
    add("## 3. 数据防护")
    add()
    add("- 疑似数据错误 bar（非除权事件日 + 相邻 bar≤10 自然日 + |单日收益|>30%）由引擎跳过/降级；"
        "停牌复牌跳变按真实价格处理。")
    add("- 买入日停牌无 bar 的候选跳过；卖出日无 bar 顺延。")
    add()
    add("## 4. 残余偏差（必须与回测结果一起读）")
    add()
    add("1. **退市股**：同花顺不提供退市股行情/除权数据；本次用东财行情 + F10 尽力补，"
        "但 F10 分红方案只保留最近 10 条公告（约 5 年），较早的除权事件可能缺失；"
        "股本 F10 对退市股完整。仍未抓到的退市股（见 delisted_failures.csv）不在样本内 → 残余幸存者偏差。")
    add("2. **ST/退按当前状态剔除**：历史上曾 ST 但现已摘帽的股票**保留**（这是 point-in-time 的近似处理）；"
        "当前 ST/退的股票被整体剔除，其历史（非 ST 期）不在样本内。")
    add("3. **历史总股本缺失回退**：akshare/东财股本缺失的标的回退「当前股本/送股反推」估算，"
        "回退笔数见回测 summary 的 `cap_source_counts`。")
    add("4. **coarse 阈值漏筛**：max 估算市值 <200 亿的标的不抓除权事件，"
        "理论上存在「历史市值 ≥500 亿但估算 <200 亿」的漏网（误差需 >2.5 倍），概率低但非零。")
    add("5. **北交所**：东财全 A 快照 fs 不含北交所（m:0/1 沪深主板/创业板/科创板）；"
        "北交所无 ≥500 亿标的，对结果无影响（披露）。")
    add()
    add("## 5. 失败清单")
    add()
    if OUT_FAILURES.exists():
        fdf = pd.read_csv(OUT_FAILURES)
        add(f"共 {len(fdf)} 条（`data/fetch_failures_all.csv`）：")
        add()
        add("| kind | target | window | type | code | detail |")
        add("|---|---|---|---|---|---|")
        for r in fdf.head(60).itertuples():
            add(f"| {getattr(r, 'kind', '')} | {getattr(r, 'target', '')} | "
                f"{getattr(r, 'window', '')} | {getattr(r, 'type', '')} | "
                f"{getattr(r, 'code', '')} | {str(getattr(r, 'detail', ''))[:80]} |")
    else:
        add("**无失败**。")
    add()
    add("## 6. 文件")
    add()
    add("| 文件 | 说明 |")
    add("|---|---|")
    add("| `data/spot_all.csv` | 全 A 当前快照（含 ST/退标记） |")
    add("| `data/bars_all.parquet` | 不复权收盘长表（全 A + 退市股） |")
    add("| `data/events_all.parquet` | 除权事件（同花顺精筛 + 退市东财） |")
    add("| `data/shares_history.parquet` | 历史总股本 step function（symbol/change_date/total_shares/source） |")
    add("| `data/pit_universe.csv` | PIT 候选集（有股本 ∩ 有事件） |")
    add("| `data/universe_all.csv` | 全部有日线标的 + 粗筛市值 |")
    add("| `data/coarse_list.csv` / `data/shares_list.csv` | 粗筛/精筛清单 |")
    add("| `data/delisted_list.csv` / `data/delisted_failures.csv` | 退市股清单/失败 |")
    add("| `data/manifest_all.json` | 抓取时点 + 全部缓存/产出 SHA256 |")
    add("| `data/cache/raw_all/`、`ca_all/`、`shares/`、`delisted/` | 原始缓存 |")
    add()
    add("> 回测口径、断言与新旧口径对比见 `SPEC.md` 与 `REPORT.md`；"
        "历史结果不代表未来收益。")
    OUT_QUALITY.write_text("\n".join(L) + "\n", encoding="utf-8")
    log(f"DATA_QUALITY_ALL.md 写入完成")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=list(PHASES) + ["all"], default="all")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    load_env()
    for d in (CACHE, RAW_ALL, CA_ALL, SHARES_DIR):
        d.mkdir(parents=True, exist_ok=True)
    order = ["bars", "assemble", "ca", "shares", "delisted", "build"]
    phases = order if args.phase == "all" else [args.phase]
    for ph in phases:
        log(f"===== 阶段 {ph} =====")
        PHASES[ph](args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
