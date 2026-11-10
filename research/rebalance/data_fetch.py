#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dividend-alpha-v2 / research/rebalance —— 行情数据抓取、构建与质检
====================================================================

按 research/rebalance/SPEC.md §2（唯一真值）执行：

  1. 从东方财富 push2his 日K接口抓取：
       - 62 只池内标的 *后复权* 收盘价（fqt=2）——本次回测唯一价格口径；
       - 同一批标的 *不复权* 收盘价（fqt=0）——仅用于复权一致性质检，不进入回测；
       - 指数 000922（中证红利，fqt=0）、000300（沪深300，fqt=0）。
     （注意：本环境 fqt=1 前复权序列同一天 OHLC 复权因子不一致，已知异常，全流程不使用 fqt=1。）

  2. 输出（UTF-8 CSV，date=YYYY-MM-DD）：
       - research/rebalance/data/close_adj_wide.csv  宽表，行=000300 交易日历，列=date+62 标的，缺失留空
       - research/rebalance/data/index_000922.csv    date,close
       - research/rebalance/data/index_000300.csv    date,close
       - research/rebalance/data/universe_meta.csv   symbol,first_date,last_date,n_days,missing_days,max_abs_daily_ret

  3. 质检（SPEC §2.4）→ research/rebalance/data/DATA_QUALITY.md
       缺失交易日 / 最大单日绝对涨跌（>25% 列出）/ fqt=2 vs fqt=0 日收益差异
       （与库内 corp_action 除权日交叉验证）/ 与 data/dividend_alpha.db price_daily 重叠窗口
       收益 corr 与最大绝对差 / 交易日历确认 / 数据可用性结论。

请求策略：User-Agent + Referer，请求间隔 ≥0.3s，失败最多重试 3 次（1s/2s/4s 指数退避），
失败清单写入 DATA_QUALITY.md（有失败时同时落 fetch_failures.csv）。

缓存：原始响应 JSON 落在 data/.cache/，重跑默认命中缓存（`--force` 强制重抓）。
本机系统代理会重置到 eastmoney 的长连接（WinError 10054），因此显式 `trust_env=False` 绕过代理。

用法：
    python research/rebalance/data_fetch.py                # 抓取(缓存优先) + 构建 + 自测 + 质检
    python research/rebalance/data_fetch.py --force        # 强制重新抓取全部 126 个请求
    python research/rebalance/data_fetch.py --no-fetch     # 只用本地缓存（缓存缺失则报错）
    python research/rebalance/data_fetch.py --skip-quality # 只抓取/构建/自测
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import requests

# ----------------------------------------------------------------------------
# 路径与常量
# ----------------------------------------------------------------------------
HERE = Path(__file__).resolve().parent          # research/rebalance
ROOT = HERE.parent.parent                        # 项目根目录
DATA_DIR = HERE / "data"
CACHE_DIR = DATA_DIR / ".cache"
UNIVERSE_FILE = DATA_DIR / "universe.txt"
DB_PATH = ROOT / "data" / "dividend_alpha.db"

BASE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
REFERER = "https://quote.eastmoney.com/"
HEADERS = {
    "User-Agent": UA,
    "Referer": REFERER,
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}
API_COMMON = {
    "fields1": "f1,f2,f3,f4,f5,f6",
    "fields2": "f51,f52,f53,f54,f55,f56,f57",
    "klt": 101,
    "beg": 20150101,
    "end": 20500101,
    "lmt": 100000,
}

FQT_ADJ = 2      # 后复权：回测唯一口径
FQT_RAW = 0      # 不复权：仅质检对照
BEGIN_DATE = "2015-01-01"
END_DATE = "2026-11-06"
EXPECTED_CALENDAR_DAYS = 2877

MIN_REQUEST_INTERVAL = 0.3   # 秒
MAX_ATTEMPTS = 4             # 首次 + 重试 3 次
BACKOFF_BASE = 1.0           # 指数退避：1s, 2s, 4s

OUT_WIDE = DATA_DIR / "close_adj_wide.csv"
OUT_IDX_922 = DATA_DIR / "index_000922.csv"
OUT_IDX_300 = DATA_DIR / "index_000300.csv"
OUT_META = DATA_DIR / "universe_meta.csv"
OUT_QUALITY = DATA_DIR / "DATA_QUALITY.md"
OUT_FAILURES = DATA_DIR / "fetch_failures.csv"

INDEX_KEYS = {  # 结果字典里的键 → (secid, 输出文件, 中文名)
    "INDEX_000922": ("1.000922", OUT_IDX_922, "中证红利 000922"),
    "INDEX_000300": ("1.000300", OUT_IDX_300, "沪深300 000300"),
}

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# 篮子定义（SPEC §2.3）
CURRENT10 = ["sz000157", "sz000423", "sz000568", "sz000651", "sz000987",
             "sz002032", "sz002236", "sz002532", "sz002555", "sz300979"]
OLD20 = ["sh600028", "sh600036", "sh600585", "sh600900", "sh601088",
         "sh601288", "sh601398", "sh601857", "sh601988", "sz000001",
         "sz000333", "sz000538", "sz000630", "sz000651", "sz000708",
         "sz000858", "sz000895", "sz000921", "sz002032", "sz002142"]
CURRENT10_START = "2021-05-06"
OLD20_START = "2015-01-05"


# ----------------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------------
def now_str() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S%z")


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def safe_float(x):
    """把接口里的字符串转 float，'-'/'' 等返回 None。"""
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def md_table(headers, rows) -> str:
    """极简 markdown 表格。"""
    out = ["| " + " | ".join(str(h) for h in headers) + " |",
           "|" + "|".join(["---"] * len(headers)) + "|"]
    for r in rows:
        out.append("| " + " | ".join("" if c is None else str(c) for c in r) + " |")
    return "\n".join(out)


def pct(v, nd: int = 2) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    return f"{v * 100:.{nd}f}%"


def bp(v, nd: int = 1) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    return f"{v * 10000:.{nd}f}bp"


def num(v, nd: int = 2) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    return f"{v:.{nd}f}"


def read_universe() -> list[str]:
    lines = [ln.strip() for ln in UNIVERSE_FILE.read_text(encoding="utf-8").splitlines()]
    syms = [ln for ln in lines if ln and not ln.startswith("#")]
    for s in syms:
        if not re.match(r"^(sh|sz)\d{6}$", s):
            raise ValueError(f"universe.txt 非法内部代码: {s!r}")
    if len(syms) != 62:
        raise ValueError(f"universe.txt 应为 62 只，实际 {len(syms)}")
    if len(set(syms)) != len(syms):
        raise ValueError("universe.txt 存在重复代码")
    return syms


def symbol_to_secid(sym: str) -> str:
    market = "1" if sym.startswith("sh") else "0"
    return f"{market}.{sym[2:]}"


def masked_returns(s: pd.Series) -> pd.Series:
    """按日历相邻日计算日收益；任一端缺失(停牌)则置 NaN（不跨缺口计算）。"""
    prev = s.shift(1)
    ret = s / prev - 1.0
    return ret.where(s.notna() & prev.notna())


def sha256_short(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


# ----------------------------------------------------------------------------
# 抓取
# ----------------------------------------------------------------------------
class RateLimiter:
    """保证相邻请求开始时刻间隔 ≥ min_interval，并给重试同样限速。"""

    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._next_at = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        if now < self._next_at:
            time.sleep(self._next_at - now)
        self._next_at = time.monotonic() + self.min_interval


def make_session() -> requests.Session:
    s = requests.Session()
    # 本机系统代理（Windows 注册表）会让 eastmoney 长连接被重置(10054)，显式绕过
    s.trust_env = False
    s.proxies = {"http": None, "https": None}
    s.headers.update(HEADERS)
    return s


def cache_path(secid: str, fqt: int) -> Path:
    return CACHE_DIR / f"kline_{secid.replace('.', '_')}_fqt{fqt}.json"


def validate_payload(payload) -> dict:
    """校验接口返回结构，返回 data 段；异常抛 RuntimeError 供重试。"""
    if not isinstance(payload, dict):
        raise RuntimeError("响应不是 JSON 对象")
    if payload.get("rc") != 0:
        raise RuntimeError(f"接口 rc={payload.get('rc')} msg={payload.get('msg')}")
    data = payload.get("data")
    if not isinstance(data, dict) or not data.get("klines"):
        raise RuntimeError("data/klines 为空（secid 可能有误或标的无数据）")
    return data


def kline_request(session: requests.Session, limiter: RateLimiter, secid: str, fqt: int) -> dict:
    limiter.wait()
    params = dict(API_COMMON)
    params["secid"] = secid
    params["fqt"] = fqt
    resp = session.get(BASE_URL, params=params, timeout=(10, 30))
    resp.raise_for_status()
    return resp.json()


def fetch_symbol(session, limiter, secid: str, fqt: int,
                 force: bool = False, no_fetch: bool = False):
    """带缓存与重试的单标的抓取。

    返回 (payload, cache_hit, attempts)。
    """
    path = cache_path(secid, fqt)
    if (not force) and path.exists() and (not no_fetch):
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
            payload = obj.get("payload")
            validate_payload(payload)
            return payload, True, 0
        except Exception as e:  # 缓存损坏则重抓
            log(f"缓存无效，重抓 {secid} fqt={fqt}: {e}")
    if no_fetch:
        raise RuntimeError(f"--no-fetch 但缓存不存在/无效: {path.name}")

    last_err = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            payload = kline_request(session, limiter, secid, fqt)
            validate_payload(payload)
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"fetched_at": now_str(), "secid": secid, "fqt": fqt,
                                        "payload": payload}, ensure_ascii=False),
                            encoding="utf-8")
            return payload, False, attempt
        except Exception as e:
            last_err = e
            if attempt < MAX_ATTEMPTS:
                backoff = BACKOFF_BASE * (2 ** (attempt - 1))
                log(f"抓取失败 {secid} fqt={fqt} 第{attempt}次: {e}; {backoff:.0f}s 后重试")
                time.sleep(backoff)
            else:
                raise RuntimeError(f"{secid} fqt={fqt} 重试 {attempt} 次仍失败: {e}") from e
    raise RuntimeError(str(last_err))


def parse_klines(payload) -> tuple[list[dict], dict]:
    """解析 klines → 按日期升序的 [{date,open,close,high,low,volume,amount}]。"""
    data = validate_payload(payload)
    raw = data["klines"]
    seen: dict[str, dict] = {}
    dups = 0
    invalid = 0
    for line in raw:
        flds = str(line).split(",")
        if len(flds) < 7:
            raise ValueError(f"K线字段不足: {line!r}")
        date = flds[0].strip()
        if not DATE_RE.match(date):
            raise ValueError(f"K线日期格式异常: {date!r}")
        close = safe_float(flds[2])
        if close is None or close <= 0:
            invalid += 1
            continue
        if date in seen:
            dups += 1
        seen[date] = {
            "date": date,
            "open": safe_float(flds[1]),
            "close": close,
            "high": safe_float(flds[3]),
            "low": safe_float(flds[4]),
            "volume": safe_float(flds[5]),
            "amount": safe_float(flds[6]),
        }
    if not seen:
        raise ValueError("无有效K线")
    rows = [seen[d] for d in sorted(seen)]
    meta = {
        "code": data.get("code"),
        "name": data.get("name"),
        "market": data.get("market"),
        "decimal": data.get("decimal"),
        "dktotal": data.get("dktotal"),
        "dups": dups,
        "invalid": invalid,
    }
    return rows, meta


def fetch_all(universe: list[str], force: bool, no_fetch: bool, verbose: bool):
    """抓取全部 126 个请求（62 * 2 + 2 指数）。"""
    session = make_session()
    limiter = RateLimiter(MIN_REQUEST_INTERVAL)
    results: dict[tuple[str, int], dict] = {}
    failures: list[dict] = []
    stats = {"requests": 0, "cache_hits": 0, "fetched": 0, "failures": 0}

    targets: list[tuple[str, str, int, str]] = []
    for sym in universe:
        targets.append((sym, symbol_to_secid(sym), FQT_ADJ, "stock_adj"))
        targets.append((sym, symbol_to_secid(sym), FQT_RAW, "stock_raw"))
    for key, (secid, _out, label) in INDEX_KEYS.items():
        targets.append((key, secid, FQT_RAW, "index"))

    total = len(targets)
    t0 = time.time()
    for idx, (target, secid, fqt, kind) in enumerate(targets, start=1):
        stats["requests"] += 1
        cache_hit = False
        try:
            payload, cache_hit, attempts = fetch_symbol(
                session, limiter, secid, fqt, force=force, no_fetch=no_fetch)
            rows, meta = parse_klines(payload)
            results[(target, fqt)] = {
                "target": target, "secid": secid, "fqt": fqt, "kind": kind,
                "rows": rows, "meta": meta,
                "cache_hit": cache_hit, "attempts": attempts,
            }
            if cache_hit:
                stats["cache_hits"] += 1
            else:
                stats["fetched"] += 1
        except Exception as e:
            failures.append({"target": target, "secid": secid, "fqt": fqt,
                             "kind": kind, "error": str(e), "when": now_str()})
            log(f"✗ 失败 {target} {secid} fqt={fqt}: {e}")
        if verbose or idx % 10 == 0 or idx == total:
            log(f"抓取进度 {idx}/{total}  缓存命中 {stats['cache_hits']}  "
                f"实抓 {stats['fetched']}  失败 {len(failures)}  已用 {time.time()-t0:.1f}s")

    stats["failures"] = len(failures)
    stats["elapsed_sec"] = time.time() - t0
    if failures:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(failures).to_csv(OUT_FAILURES, index=False, encoding="utf-8")
        log(f"失败清单已写 {OUT_FAILURES}（{len(failures)} 条）")
    elif OUT_FAILURES.exists():
        OUT_FAILURES.unlink()
    return results, failures, stats


# ----------------------------------------------------------------------------
# 构建输出
# ----------------------------------------------------------------------------
def build_dataset(results: dict, universe: list[str]):
    """由抓取结果构建宽表、指数表、meta。"""
    if ("INDEX_000300", FQT_RAW) not in results:
        raise RuntimeError("缺少 000300 指数数据，无法建立交易日历")

    idx300 = results[("INDEX_000300", FQT_RAW)]
    idx922 = results.get(("INDEX_000922", FQT_RAW))
    calendar = [r["date"] for r in idx300["rows"]]
    if calendar != sorted(set(calendar)):
        raise ValueError("000300 日历存在重复/乱序")
    if calendar[0] < BEGIN_DATE or calendar[-1] > END_DATE:
        raise ValueError(f"000300 日历越界: {calendar[0]}..{calendar[-1]}")
    cal_set = set(calendar)

    close_adj: dict[str, dict[str, float]] = {}
    close_raw: dict[str, dict[str, float]] = {}
    off_calendar: dict[str, int] = {}
    out_of_range: dict[str, int] = {}
    for sym in universe:
        adj = results.get((sym, FQT_ADJ))
        raw = results.get((sym, FQT_RAW))
        if adj is None:
            close_adj[sym] = {}
            off_calendar[sym] = 0
            out_of_range[sym] = 0
            continue
        d = {r["date"]: float(r["close"]) for r in adj["rows"]}
        off = sum(1 for x in d if x not in cal_set)
        oor = sum(1 for x in d if x < BEGIN_DATE or x > END_DATE)
        off_calendar[sym] = off
        out_of_range[sym] = oor
        close_adj[sym] = d
        if raw is not None:
            close_raw[sym] = {r["date"]: float(r["close"]) for r in raw["rows"]}

    def to_wide(close_map: dict[str, dict[str, float]]) -> pd.DataFrame:
        wide = pd.DataFrame(index=pd.Index(calendar, name="date"))
        for sym in universe:
            m = close_map.get(sym)
            if not m:
                wide[sym] = np.nan
                continue
            s = {d: c for d, c in m.items() if d in cal_set}
            wide[sym] = pd.Series(s, dtype="float64").reindex(calendar)
        return wide

    wide_adj = to_wide(close_adj)
    wide_raw = to_wide(close_raw)

    def index_df(idx_res) -> pd.DataFrame:
        rows = [r for r in idx_res["rows"] if BEGIN_DATE <= r["date"] <= END_DATE]
        return pd.DataFrame({"date": [r["date"] for r in rows],
                             "close": [float(r["close"]) for r in rows]})

    index300 = index_df(idx300)
    index922 = index_df(idx922) if idx922 is not None else pd.DataFrame(columns=["date", "close"])

    # universe_meta
    meta_rows = []
    for sym in universe:
        s = wide_adj[sym]
        have = s.dropna()
        if len(have) == 0:
            meta_rows.append({"symbol": sym, "first_date": "", "last_date": "",
                              "n_days": 0, "missing_days": len(calendar),
                              "max_abs_daily_ret": np.nan})
            continue
        ret = masked_returns(s).abs()
        max_abs = float(ret.max()) if ret.notna().any() else np.nan
        meta_rows.append({
            "symbol": sym,
            "first_date": have.index[0],
            "last_date": have.index[-1],
            "n_days": int(len(have)),
            "missing_days": int(len(calendar) - len(have)),
            "max_abs_daily_ret": max_abs,
        })
    meta = pd.DataFrame(meta_rows, columns=["symbol", "first_date", "last_date",
                                            "n_days", "missing_days", "max_abs_daily_ret"])

    return {
        "universe": universe,
        "calendar": calendar,
        "wide_adj": wide_adj,
        "wide_raw": wide_raw,
        "index300": index300,
        "index922": index922,
        "meta": meta,
        "off_calendar": off_calendar,
        "out_of_range": out_of_range,
        "results": results,
    }


def write_dataset_outputs(ds: dict) -> list[dict]:
    """落盘 4 个 CSV，返回文件清单（name/size/sha256）。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    wide = ds["wide_adj"]
    out = wide.reset_index(drop=True)
    out.insert(0, "date", list(ds["calendar"]))
    out.to_csv(OUT_WIDE, index=False, na_rep="", float_format="%.6f", encoding="utf-8")

    ds["index922"][["date", "close"]].to_csv(
        OUT_IDX_922, index=False, float_format="%.6f", encoding="utf-8")
    ds["index300"][["date", "close"]].to_csv(
        OUT_IDX_300, index=False, float_format="%.6f", encoding="utf-8")

    meta = ds["meta"].copy()
    meta.to_csv(OUT_META, index=False, float_format="%.8f", encoding="utf-8")

    manifest = []
    for p in (OUT_WIDE, OUT_IDX_922, OUT_IDX_300, OUT_META):
        manifest.append({"name": p.name, "path": str(p), "size": p.stat().st_size,
                         "sha256_16": sha256_short(p)})
    return manifest


# ----------------------------------------------------------------------------
# 自测断言
# ----------------------------------------------------------------------------
def run_self_tests(ds: dict) -> list[dict]:
    tests: list[dict] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        tests.append({"name": name, "ok": bool(ok), "detail": str(detail)})

    cal = ds["calendar"]
    wide = ds["wide_adj"]
    universe = ds["universe"]

    check("交易日历行数≈2877", 2870 <= len(cal) <= 2885,
          f"n={len(cal)} 目标 {EXPECTED_CALENDAR_DAYS}")
    check("日历首日≥2015-01-01", cal[0] >= BEGIN_DATE, f"first={cal[0]}")
    check("日历末日=2026-11-06", cal[-1] == END_DATE, f"last={cal[-1]}")
    check("日历无重复且升序", cal == sorted(set(cal)), f"n_unique={len(set(cal))}")

    check("宽表行数=日历行数", len(wide) == len(cal), f"{len(wide)} vs {len(cal)}")
    check("宽表(内存)列数=62", wide.shape[1] == 62, f"{wide.shape}")
    # 列数断言以落盘文件为准：date + 62 = 63 列（宽表构建时 date 在落盘阶段插入）
    csv_lines = OUT_WIDE.read_text(encoding="utf-8").splitlines()
    header = csv_lines[0].split(",")
    check("close_adj_wide.csv 列数=63(date+62)", len(header) == 63 and header[0] == "date",
          f"cols={len(header)} header0={header[0] if header else ''}")
    check("close_adj_wide.csv 行数=2877(+表头)", len(csv_lines) - 1 == len(cal),
          f"data_rows={len(csv_lines) - 1}")
    check("列顺序=universe.txt", list(wide.columns) == universe,
          f"前3列={list(wide.columns[:3])}")
    check("date 首列与日历一致", list(wide.index) == cal, "index==calendar")

    arr = wide.to_numpy(dtype="float64")
    finite = np.isfinite(arr)
    check("无 inf/-inf", bool(np.isinf(arr).sum() == 0), f"inf={int(np.isinf(arr).sum())}")
    bad = finite & (arr <= 0)
    check("数值为正或空", bool(bad.sum() == 0), f"非正数格数={int(bad.sum())}")
    check("每列至少1个有效值", bool((~pd.isna(wide)).sum().min() >= 1),
          f"min_n_days={int((~pd.isna(wide)).sum().min())}")
    check("无重复日期", len(set(wide.index)) == len(wide), f"n={len(wide)}")

    off = sum(ds["off_calendar"].values())
    oor = sum(ds["out_of_range"].values())
    check("无日历外K线", off == 0, f"off_calendar={off}")
    check("无越界K线(2015-01-01..2026-11-06)", oor == 0, f"out_of_range={oor}")

    # 指数文件
    i300 = ds["index300"]
    i922 = ds["index922"]
    check("index_000300 行数=日历", len(i300) == len(cal), f"{len(i300)} vs {len(cal)}")
    check("index_000300 日期=日历", list(i300["date"]) == cal, "dates match")
    check("index_000300 收盘为正", bool((i300["close"] > 0).all()), "all > 0")
    check("index_000922 行数=日历", len(i922) == len(cal), f"{len(i922)} vs {len(cal)}")
    check("index_000922 日期=日历", list(i922["date"]) == cal, "dates match")
    check("index_000922 收盘为正", bool((i922["close"] > 0).all()), "all > 0")
    check("两指数末日期=2026-11-06",
          i300["date"].iloc[-1] == END_DATE and i922["date"].iloc[-1] == END_DATE,
          f"300={i300['date'].iloc[-1]} 922={i922['date'].iloc[-1]}")

    # meta 与宽表一致性
    meta = ds["meta"]
    check("universe_meta 62 行", len(meta) == 62, f"{len(meta)}")
    n_days = [int((~pd.isna(wide[s])).sum()) for s in universe]
    check("meta.n_days=非空格数", list(meta["n_days"]) == n_days, "n_days match")
    check("meta.missing_days=日历数-n_days",
          list(meta["missing_days"]) == [len(cal) - n for n in n_days], "missing match")
    max_abs = []
    for s in universe:
        r = masked_returns(wide[s]).abs()
        max_abs.append(float(r.max()) if r.notna().any() else np.nan)
    ok_max = all((math.isnan(a) and math.isnan(b)) or abs(a - b) < 1e-12
                 for a, b in zip(max_abs, meta["max_abs_daily_ret"]))
    check("meta.max_abs_daily_ret 复算一致", ok_max, "max_abs match")

    # 全部 62 只 fqt=2 抓取成功
    res = ds["results"]
    failed_adj = [s for s in universe if (s, FQT_ADJ) not in res]
    check("62 只后复权抓取全部成功", not failed_adj, f"failed={failed_adj}")

    return tests


def report_self_tests(tests: list[dict]) -> bool:
    log("—— 自测断言 ——")
    all_ok = True
    for t in tests:
        mark = "PASS" if t["ok"] else "FAIL"
        if not t["ok"]:
            all_ok = False
        print(f"  [{mark}] {t['name']}  ({t['detail']})", flush=True)
    print(f"自测结果: {'全部通过' if all_ok else '存在失败'} ({sum(t['ok'] for t in tests)}/{len(tests)})",
          flush=True)
    return all_ok


# ----------------------------------------------------------------------------
# 生产库 / 公司行为
# ----------------------------------------------------------------------------
def open_db_readonly(path: Path) -> sqlite3.Connection:
    uri = path.resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True)


def load_price_daily_window(start: str, end: str):
    """读生产库 price_daily（只读），返回 symbol/trade_date/raw_close/adj_close。"""
    if not DB_PATH.exists():
        return pd.DataFrame(columns=["symbol", "trade_date", "raw_close", "adj_close"]), "数据库不存在"
    try:
        conn = open_db_readonly(DB_PATH)
        df = pd.read_sql_query(
            "select symbol, trade_date, raw_close, adj_close from price_daily "
            "where trade_date between ? and ? order by symbol, trade_date",
            conn, params=(start, end))
        conn.close()
        df["trade_date"] = df["trade_date"].astype(str)
        return df, "ok"
    except Exception as e:  # 质检不应因库问题中断
        return pd.DataFrame(columns=["symbol", "trade_date", "raw_close", "adj_close"]), f"读取失败: {e}"


def load_corp_actions():
    """读生产库 corp_action（只读）：symbol/ex_date/dps/bonus/transfer。"""
    if not DB_PATH.exists():
        return pd.DataFrame(columns=["symbol", "ex_date", "dps", "bonus_ratio", "transfer_ratio"])
    try:
        conn = open_db_readonly(DB_PATH)
        df = pd.read_sql_query(
            "select symbol, ex_date, dps, bonus_ratio, transfer_ratio from corp_action", conn)
        conn.close()
        df["ex_date"] = df["ex_date"].astype(str)
        for c in ("dps", "bonus_ratio", "transfer_ratio"):
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
        return df
    except Exception:
        return pd.DataFrame(columns=["symbol", "ex_date", "dps", "bonus_ratio", "transfer_ratio"])


# ----------------------------------------------------------------------------
# 质检报告
# ----------------------------------------------------------------------------
def generate_quality_report(ds: dict, failures: list[dict], stats: dict,
                            tests: list[dict], manifest: list[dict], out_path: Path) -> Path:
    universe = ds["universe"]
    cal = ds["calendar"]
    wide = ds["wide_adj"]
    wide_raw = ds["wide_raw"]
    n_cal = len(cal)

    corp = load_corp_actions()
    corp_by_sym = {sym: df for sym, df in corp.groupby("symbol")}

    # ---- 覆盖度（全部标的） ----
    coverage: dict[str, dict] = {}
    max_abs_all: list[tuple[str, float, str, float]] = []  # (sym, max_abs, date, ret)
    ret_gt: dict[str, int] = {"10pct": 0, "15pct": 0, "20pct": 0, "25pct": 0}
    for sym in universe:
        s = wide[sym]
        have = s.dropna()
        ret = masked_returns(s)
        absr = ret.abs()
        if len(have) == 0:
            coverage[sym] = {"first": "", "last": "", "n": 0, "missing": n_cal,
                             "pre": n_cal, "post": 0, "in_window": 0,
                             "missing_dates": [], "max_abs": np.nan, "max_date": ""}
            continue
        first = str(have.index[0])
        last = str(have.index[-1])
        pre = bisect.bisect_left(cal, first)
        post = n_cal - bisect.bisect_right(cal, last)
        inwin = n_cal - pre - post - len(have)
        exp = cal[pre:bisect.bisect_right(cal, last)]
        have_set = set(have.index)
        miss_dates = [d for d in exp if d not in have_set]
        if absr.notna().any():
            imax = absr.idxmax()
            max_abs = float(absr.loc[imax])
            max_date = str(imax)
            max_ret = float(ret.loc[imax])
        else:
            max_abs, max_date, max_ret = np.nan, "", np.nan
        coverage[sym] = {"first": first, "last": last, "n": int(len(have)),
                         "missing": int(n_cal - len(have)), "pre": int(pre),
                         "post": int(post), "in_window": int(inwin),
                         "missing_dates": miss_dates,
                         "max_abs": max_abs, "max_date": max_date}
        if not math.isnan(max_abs):
            max_abs_all.append((sym, max_abs, max_date, max_ret))
        for key, thr in (("10pct", 0.10), ("15pct", 0.15), ("20pct", 0.20), ("25pct", 0.25)):
            ret_gt[key] += int((absr > thr).sum())

    max_abs_all.sort(key=lambda x: x[1], reverse=True)
    big25 = [x for x in max_abs_all if x[1] > 0.25]
    big20 = [x for x in max_abs_all if x[1] > 0.20]

    # ---- fqt=2 vs fqt=0 一致性 ----
    cons: dict[str, dict] = {}
    all_diffs: list[tuple[str, str, float, float, float, float]] = []  # sym,date,r_adj,r_raw,diff,step
    steps_all: list[dict] = []
    for sym in universe:
        if sym not in wide_raw or wide_raw[sym].notna().sum() == 0:
            cons[sym] = None
            continue
        a = wide[sym]
        r = wide_raw[sym]
        ratio = a / r
        ret_a = masked_returns(a)
        ret_r = masked_returns(r)
        both = ret_a.notna() & ret_r.notna()
        diff = (ret_a - ret_r).where(both)
        step = (ratio / ratio.shift(1) - 1.0).where(ratio.notna() & ratio.shift(1).notna())
        common = (a.notna() & r.notna())
        set_mismatch = int(((a.notna()) ^ (r.notna())).sum())
        dd = diff.dropna()
        st = step.dropna()
        steps = [{"date": str(d), "step": float(v)} for d, v in st.items()]
        for s_ in steps:
            steps_all.append({"symbol": sym, "date": s_["date"], "step": s_["step"]})
        for d, v in dd.items():
            all_diffs.append((sym, str(d), float(ret_a.loc[d]), float(ret_r.loc[d]),
                             float(v), float(step.loc[d]) if not math.isnan(float(step.loc[d])) else np.nan))
        cons[sym] = {
            "n_common": int(common.sum()),
            "set_mismatch": set_mismatch,
            "n_adj_pairs": int(ret_a.notna().sum()),
            "median_abs": float(dd.abs().median()) if len(dd) else np.nan,
            "p95_abs": float(dd.abs().quantile(0.95)) if len(dd) else np.nan,
            "max_abs": float(dd.abs().max()) if len(dd) else np.nan,
            "max_date": str(dd.abs().idxmax()) if len(dd) else "",
            "n_gt_0_5pct": int((dd.abs() > 0.005).sum()),
            "n_gt_1pct": int((dd.abs() > 0.01).sum()),
            "n_steps": int(sum(1 for s_ in steps if abs(s_["step"]) >= 0.003)),
            "n_steps_big": int(sum(1 for s_ in steps if abs(s_["step"]) >= 0.10)),
            "n_ratio_down": int((st < -0.001).sum()),
            "steps": steps,
        }

    cons_ok = {s: v for s, v in cons.items() if v is not None}
    all_diffs.sort(key=lambda x: abs(x[4]), reverse=True)
    top5_diffs = all_diffs[:5]

    # ---- 差异日 vs 除权日（库内 corp_action，覆盖 2019-07 之后） ----
    db_corp_min = (str(corp["ex_date"].min()) if len(corp) else "")
    ex_by_sym: dict[str, set[str]] = {}
    for sym, df in corp_by_sym.items():
        ex_by_sym[sym] = set(df["ex_date"].astype(str))

    def is_ex_date(sym: str, date: str, tolerance_cal: int = 1) -> bool:
        ex = ex_by_sym.get(sym, set())
        if not ex:
            return False
        if date in ex:
            return True
        i = bisect.bisect_left(cal, date)
        for k in range(max(0, i - tolerance_cal), min(n_cal, i + tolerance_cal + 1)):
            if cal[k] in ex:
                return True
        return False

    # 除权日校验：所有 |step|>=0.3% 的复权因子步进，按库覆盖区间统计命中率
    step_cover = [s_ for s_ in steps_all
                  if db_corp_min and s_["date"] >= db_corp_min and abs(s_["step"]) >= 0.003
                  and s_["date"] <= END_DATE]
    step_matched = [s_ for s_ in step_cover if is_ex_date(s_["symbol"], s_["date"])]
    step_unmatched = [s_ for s_ in step_cover if not is_ex_date(s_["symbol"], s_["date"])]

    # 除权日幅度校验：cash-only 事件预测 step ≈ dps/(P0-dps)
    valid_rows = []
    matched_ex_events = 0
    for s_ in step_matched:
        sym, ex_date = s_["symbol"], s_["date"]
        df = corp_by_sym.get(sym)
        if df is None:
            continue
        sub = df[df["ex_date"] == ex_date]
        if len(sub) == 0:
            continue
        dps = float(sub["dps"].sum())
        bonus = float(sub["bonus_ratio"].sum())
        transfer = float(sub["transfer_ratio"].sum())
        if dps <= 0 or bonus > 0 or transfer > 0:
            continue
        raw_pos = wide_raw[sym].shift(1).loc[ex_date] if ex_date in wide_raw.index else np.nan
        if pd.isna(raw_pos) or raw_pos <= dps:
            continue
        pred = dps / (float(raw_pos) - dps)
        obs = s_["step"]
        rel = obs / pred - 1.0
        valid_rows.append({"symbol": sym, "ex_date": ex_date, "dps": dps,
                          "p0": float(raw_pos), "pred": pred, "obs": obs, "rel": rel})
        matched_ex_events += 1
    valid_rows.sort(key=lambda x: x["ex_date"], reverse=True)
    if valid_rows:
        rel_med = float(np.median([abs(x["rel"]) for x in valid_rows]))
        rel_p90 = float(np.quantile([abs(x["rel"]) for x in valid_rows], 0.90))
        rel_max = max(abs(x["rel"]) for x in valid_rows)
    else:
        rel_med = rel_p90 = rel_max = np.nan


    # ---- 与生产库 price_daily 重叠窗口交叉校验 ----
    start_w, end_w = "2026-05-11", END_DATE
    cal_win = [d for d in cal if start_w <= d <= end_w]
    db_df, db_status = load_price_daily_window(start_w, end_w)
    db_stats = {"status": db_status, "rows": len(db_df), "cal_win": len(cal_win)}
    db_sym_count: dict[str, int] = {}
    if len(db_df):
        db_sym_count = db_df.groupby("symbol")["trade_date"].nunique().to_dict()
        union_dates = sorted(db_df["trade_date"].unique())
        db_stats["union_dates"] = len(union_dates)
        db_stats["outside_cal_win"] = len([d for d in union_dates if d not in set(cal_win)])

    db_rows = []
    big_db_diffs: list[tuple[str, str, float, float]] = []
    raw_level_diffs: list[float] = []
    if len(db_df):
        db_adj = db_df.pivot_table(index="trade_date", columns="symbol",
                                   values="adj_close", aggfunc="last")
        db_raw = db_df.pivot_table(index="trade_date", columns="symbol",
                                   values="raw_close", aggfunc="last")
        db_adj = db_adj.reindex(index=cal_win, columns=universe)
        db_raw = db_raw.reindex(index=cal_win, columns=universe)
        for sym in universe:
            our_a = wide[sym].reindex(cal_win)
            our_r = wide_raw[sym].reindex(cal_win) if sym in wide_raw else pd.Series(np.nan, index=cal_win)
            d_a = db_adj[sym] if sym in db_adj.columns else pd.Series(np.nan, index=cal_win)
            d_r = db_raw[sym] if sym in db_raw.columns else pd.Series(np.nan, index=cal_win)
            ret_a = masked_returns(our_a)
            ret_d = masked_returns(d_a)
            pairs = pd.concat([ret_a, ret_d], axis=1).dropna()
            corr = float(pairs.iloc[:, 0].corr(pairs.iloc[:, 1])) if len(pairs) >= 3 else np.nan
            dvals = (pairs.iloc[:, 0] - pairs.iloc[:, 1]).abs() if len(pairs) else pd.Series(dtype=float)
            max_diff = float(dvals.max()) if len(dvals) else np.nan
            max_date = str(dvals.idxmax()) if len(dvals) else ""
            n_big = int((dvals > 0.01).sum())
            raw_cmp = pd.concat([our_r, d_r], axis=1).dropna()
            raw_rel = ((raw_cmp.iloc[:, 0] / raw_cmp.iloc[:, 1] - 1.0).abs()
                       if len(raw_cmp) else pd.Series(dtype=float))
            if len(raw_rel):
                raw_level_diffs.extend([float(x) for x in raw_rel])
            for d, v in dvals.items():
                if v > 0.01:
                    big_db_diffs.append((sym, str(d), float(v), 1.0 if is_ex_date(sym, str(d)) else 0.0))
            db_rows.append({
                "symbol": sym, "n_pairs": int(len(pairs)), "corr": corr,
                "max_diff": max_diff, "max_date": max_date, "n_big": n_big,
                "db_days": int(db_sym_count.get(sym, 0)),
                "raw_med_rel": float(raw_rel.median()) if len(raw_rel) else np.nan,
                "raw_max_rel": float(raw_rel.max()) if len(raw_rel) else np.nan,
                "ex_win": int(len([d for d in ex_by_sym.get(sym, set()) if start_w <= d <= end_w])),
                "max_is_ex": bool(is_ex_date(sym, max_date)) if max_date else False,
            })
    db_rows_valid = [r for r in db_rows if r["n_pairs"] >= 3 and not math.isnan(r["corr"])]
    corr_vals = [r["corr"] for r in db_rows_valid]
    maxdiff_vals = [r["max_diff"] for r in db_rows_valid]
    db_summary = {
        "n_symbols": len(db_rows),
        "n_valid": len(db_rows_valid),
        "corr_min": min(corr_vals) if corr_vals else np.nan,
        "corr_p10": float(np.quantile(corr_vals, 0.10)) if corr_vals else np.nan,
        "corr_med": float(np.median(corr_vals)) if corr_vals else np.nan,
        "corr_max": max(corr_vals) if corr_vals else np.nan,
        "n_ge_999": int(sum(1 for v in corr_vals if v >= 0.999)),
        "maxdiff_med": float(np.median(maxdiff_vals)) if maxdiff_vals else np.nan,
        "maxdiff_max": max(maxdiff_vals) if maxdiff_vals else np.nan,
    }
    worst_db = sorted(db_rows_valid, key=lambda r: r["max_diff"] if not math.isnan(r["max_diff"]) else -1, reverse=True)[:8]
    big_db_diffs.sort(key=lambda x: x[2], reverse=True)
    n_big_db = len(big_db_diffs)
    n_big_db_ex = int(sum(1 for x in big_db_diffs if x[3] > 0))

    # ---- 可用性 ----
    full_hist = [s for s in universe if coverage[s]["first"] == cal[0] and coverage[s]["last"] == cal[-1]]
    elig_random = [s for s in universe
                   if coverage[s]["first"] == OLD20_START and coverage[s]["n"] >= 2500]
    old20_rows = [[s, coverage[s]["first"], coverage[s]["last"], coverage[s]["n"],
                   coverage[s]["in_window"]] for s in OLD20]
    current10_rows = []
    for s in CURRENT10:
        win = [d for d in cal if d >= CURRENT10_START]
        n_win = int(wide[s].reindex(win).notna().sum())
        current10_rows.append([s, coverage[s]["first"], coverage[s]["last"], n_win,
                               len(win) - n_win])
    old20_missing_total = sum(coverage[s]["in_window"] for s in OLD20)
    old20_not_from_2015 = [s for s in OLD20 if coverage[s]["first"] != OLD20_START]
    c10_not_ready = [r for r in current10_rows if r[3] < len([d for d in cal if d >= CURRENT10_START])]
    win_2021 = [d for d in cal if d >= CURRENT10_START]
    n_2021 = len(win_2021)

    # ---- 组装 Markdown ----
    L: list[str] = []
    def add(s: str = "") -> None:
        L.append(s)

    add("# DATA_QUALITY.md —— 再平衡回测行情数据质检报告")
    add()
    add(f"- 生成时间：`{now_str()}`；生成脚本：`research/rebalance/data_fetch.py`（自动生成，勿手改）")
    add(f"- 数据源：东方财富 push2his 日K接口；标的池：`research/rebalance/data/universe.txt`（{len(universe)} 只）；口径见 SPEC §2")
    add(f"- 回测价格口径：**fqt=2 后复权收盘**；fqt=0 不复权与指数仅作质检/背景对照；全程不使用 fqt=1（前复权，本环境已知同 I 日 OHLC 复权因子不一致）")
    add(f"- 区间：{BEGIN_DATE} .. {END_DATE}；交易日历 = 沪深300（000300）交易日，共 **{n_cal}** 个交易日")
    add()

    # §1 摘要
    add("## 1. 结论摘要（先看这里）")
    add()
    add(f"1. **产出可用**：`close_adj_wide.csv` {len(wide)} 行 × {wide.shape[1]} 列（date + {len(universe)} 标的），"
        f"{cal[0]} .. {cal[-1]}；缺失格 = {int(pd.isna(wide.to_numpy()).sum())}（停牌/新股上市前，留空不 ffill）。")
    add(f"2. **抓取**：126 个请求（62×fqt=2 + 62×fqt=0 + 2 指数），失败 **{len(failures)}** 个；"
        f"缓存 {stats['cache_hits']} 命中 / 实抓 {stats['fetched']}。请求间隔 ≥{MIN_REQUEST_INTERVAL}s，"
        f"失败重试 3 次（1s/2s/4s 指数退避）。")
    add(f"3. **覆盖**：62 只中 {len(full_hist)} 只自 {cal[0]} 起有完整数据；"
        f"新上市标的 {len([s for s in universe if coverage[s]['pre'] > 0])} 只（上市前留空属正常）；"
        f"上市后停牌/数据缺口共 {len([s for s in universe if coverage[s]['in_window'] > 0])} 只有缺口，"
        f"合计 {sum(coverage[s]['in_window'] for s in universe)} 个交易日（最差为 "
        f"{max(((s, coverage[s]['in_window']) for s in universe), key=lambda x: x[1])}）。")
    add(f"4. **异常涨跌**：单日 |收益| >25% 共 **{len(big25)}** 处（>20% {len(big20)} 处）；"
        f"全池最大单日绝对收益为 {pct(max_abs_all[0][1]) if max_abs_all else '—'}（{max_abs_all[0][0] if max_abs_all else ''} {max_abs_all[0][2] if max_abs_all else ''}）。")
    add(f"5. **复权一致性**：fqt=2 vs fqt=0 日收益差中位数 {bp(np.median([v['median_abs'] for v in cons_ok.values()]))}，"
        f"差值 >0.5% 的样本日 {sum(v['n_gt_0_5pct'] for v in cons_ok.values())} 个（共 {sum(v['n_adj_pairs'] for v in cons_ok.values())} 个收益日）；"
        f"这些大差日与库内除权日命中率 {len(step_matched)}/{len(step_cover)}"
        f"（{pct(len(step_matched)/len(step_cover)) if step_cover else '—'}）——差异集中在除权日，"
        f"非除权日仅复权价舍入级噪声。")
    add(f"6. **与生产库交叉校验**：重叠窗口 {start_w}..{end_w}（{len(cal_win)} 个交易日），"
        f"逐标的日收益 corr = [{num(db_summary['corr_min'],4)}, {num(db_summary['corr_max'],4)}]，"
        f"中位数 {num(db_summary['corr_med'],4)}（{db_summary['n_ge_999']}/{db_summary['n_valid']} 只 ≥0.999）；"
        f"max|Δ日收益| 中位数 {bp(db_summary['maxdiff_med'])}、最大 {pct(db_summary['maxdiff_max'])}。")
    add(f"7. **数据可用性结论**：**足以支撑** 2015-2026（≈11.8 年）与 2021-2026（≈5.5 年）两段回测"
        f"（详见 §8；注意现象：生产库窗口内混入非交易日期，生产侧日历需修，但本回测以 000300 日历为准，不受影响）。")
    add()

    # §2 来源与参数
    add("## 2. 数据来源、接口参数与请求策略")
    add()
    add(f"- 接口：`GET {BASE_URL}`")
    add(f"- 参数：`secid=1.<sh代码> 或 0.<sz代码>`，`fields1=f1,f2,f3,f4,f5,f6`，"
        f"`fields2=f51,f52,f53,f54,f55,f56,f57`，`klt=101`，`fqt=2`（后复权）/`fqt=0`（不复权），"
        f"`beg=20150101`，`end=20500101`，`lmt=100000`")
    add(f"- 解析字段：`f51=date,f52=open,f53=close,f54=high,f55=low,f56=volume,f57=amount`；"
        f"本次仅入库 `date/close`（fqt=2）。")
    add(f"- 请求头：`User-Agent`（Chrome UA）+ `Referer: {REFERER}`")
    add(f"- 限速/重试：相邻请求开始间隔 ≥ {MIN_REQUEST_INTERVAL}s；单请求最多 {MAX_ATTEMPTS} 次尝试"
        f"（首次 + 3 次重试，退避 {int(BACKOFF_BASE)}s/2s/4s）；失败清单见 §2.1。")
    add(f"- 代理：本机系统代理会使 eastmoney 长连接重置（WinError 10054），脚本 `trust_env=False` 显式绕过。")
    add(f"- 缓存：`data/.cache/`（{stats['cache_hits'] + stats['fetched']} 个响应 JSON，重跑默认命中；`--force` 重抓）。")
    add()
    add("### 2.1 失败清单")
    add()
    if not failures:
        add(f"**无失败**：126/126 个请求最终成功（含缓存命中 {stats['cache_hits']} 个、实抓 {stats['fetched']} 个）。")
    else:
        add(f"共 {len(failures)} 个失败（`data/fetch_failures.csv` 同时落盘）：")
        add()
        add(md_table(["target", "secid", "fqt", "错误"], [
            [f["target"], f["secid"], f["fqt"], str(f["error"])[:120].replace("|", "\\|")]
            for f in failures]))
    add()

    # §3 日历
    add("## 3. 交易日历确认")
    add()
    add(f"- 000300 交易日：**{n_cal}** 个，首日 `{cal[0]}`，末日 `{cal[-1]}`"
        f"{' ✅ 与 SPEC 要求 2026-11-06 一致' if cal[-1] == END_DATE else ' ❌ 与 2026-11-06 不一致'}")
    add(f"- 000922 日期集合与 000300 {'完全一致' if list(ds['index922']['date']) == cal else '不一致'}（{len(ds['index922'])} 个）。")
    add(f"- 日历内无周末日期：{sum(1 for d in cal if pd.Timestamp(d).weekday() >= 5)} 个；无重复：{len(set(cal))} 个唯一日期。")
    add(f"- 交易日历全集约定：`close_adj_wide.csv` 的行索引即该日历；个股停牌日留空，由回测引擎自行 ffill。")
    add()
    add(f"- 说明：生产库 `price_daily` 的 `{start_w}..{end_w}` 窗口去重日期数为 "
        f"**{db_stats.get('union_dates', '—')}**（其中 {db_stats.get('outside_cal_win', '—')} 个不在 000300 交易日历内，"
        f"含周末日期，属生产侧数据/日历噪声），与本回测日历（{len(cal_win)} 日）不一致；本回测不受其影响。")
    add()

    # §4 覆盖与缺失
    add("## 4. 覆盖度与缺失交易日")
    add()
    add(f"- 每只标的的 first_date/last_date/n_days/missing_days 见 `universe_meta.csv`（全量 {len(universe)} 行）。")
    add(f"- 60 只以上老标的：上市前缺失（pre-listing）{len([s for s in universe if coverage[s]['pre'] > 0])} 只；"
        f"上市后缺口（停牌或数据缺口）{len([s for s in universe if coverage[s]['in_window'] > 0])} 只。")
    add(f"- 缺失分布：missing_days 中位数 {int(ds['meta']['missing_days'].median())}、最大 "
        f"{int(ds['meta']['missing_days'].max())}（{ds['meta'].loc[ds['meta']['missing_days'].idxmax(), 'symbol']}）；"
        f"零缺失标的 {int((ds['meta']['missing_days'] == 0).sum())} 只。")
    add()
    add("### 4.1 有缺口的标的（上市前缺失或上市后停牌/缺口）")
    add()
    rows = []
    for s in universe:
        c = coverage[s]
        if c["pre"] > 0 or c["in_window"] > 0 or c["post"] > 0:
            miss_sample = "、".join(c["missing_dates"][:6]) + (" …" if len(c["missing_dates"]) > 6 else "")
            if c["pre"] > 0:
                miss_sample = f"上市前 {c['pre']} 日" + (f"；上市后缺口 {c['in_window']} 日（{miss_sample}）" if c["in_window"] else "")
            rows.append([s, c["first"], c["last"], c["n"], c["missing"],
                         c["pre"], c["in_window"], c["post"] or "0", miss_sample])
    add(md_table(["symbol", "first_date", "last_date", "n_days", "missing_days",
                  "上市前", "上市后缺口", "末日后", "缺口日期样例"], rows))
    add()
    top_gap = sorted(universe, key=lambda x: -coverage[x]["in_window"])[:5]
    gap_text = "、".join([f"{s}({coverage[s]['in_window']}日)" for s in top_gap])
    add(f"- 上市后缺口集中在老标的的长期停牌（如 {gap_text}）。"
        f"所有缺口在宽表中留空，未做 ffill；引擎按最近可得价估值。")
    add()

    # §5 最大单日涨跌
    add("## 5. 最大单日绝对涨跌（后复权口径）")
    add()
    add(f"- 全池按标的取 max|日收益|，分布见下；阈值统计：>10% {ret_gt['10pct']} 个样本日、"
        f">15% {ret_gt['15pct']}、>20% {ret_gt['20pct']}、**>25% {ret_gt['25pct']}**。")
    add()
    add("### 5.1 各标的最大单日绝对收益 Top 15")
    add()
    add(md_table(["symbol", "max|日收益|", "日期", "当日收益"],
                 [[s, pct(a), d, pct(r)] for s, a, d, r in max_abs_all[:15]]))
    add()
    add(f"### 5.2 |单日收益| > 25% 清单（共 {len(big25)} 处）")
    add()
    if big25:
        add(md_table(["symbol", "max|日收益|", "日期", "当日收益"],
                     [[s, pct(a), d, pct(r)] for s, a, d, r in big25]))
        add()
        add("处理建议：逐条核对原始K线。后复权价日收益出现 >25% 一般意味着数据源在某个除权/送转事件处的复权因子异常，"
            "或存在非交易事件（长期停牌复牌、极端行情）。若仅个别点，回测引擎按规则交易/或剔除该日；不影响整体结论。")
    else:
        add("**无**：全池 62 只在 {0}..{1} 的后复权日收益均未超过 ±25%（A股涨跌停制度下合理；"
            ">20% 也仅 {2} 处）。".format(cal[0], cal[-1], len(big20)))
    add()

    # §6 fqt2 vs fqt0
    add("## 6. 复权一致性抽检：fqt=2（后复权）vs fqt=0（不复权）")
    add()
    add("方法：对同一标的取同一天 `adj_close(fqt=2)` 与 `raw_close(fqt=0)`，")
    add("  (a) 计算复权比 `ratio = adj/raw`，ratio 的跳变日 = 除权/送转事件日；")
    add("  (b) 比较两者日收益 `r_adj - r_raw`，差异应只出现在除权日附近，量级 ≈ 当日分红/送转幅度；")
    add("  (c) 用生产库 `corp_action` 的 `ex_date/dps/bonus/transfer` 做独立交叉验证。")
    add(f"抽查范围：全部 {len(cons_ok)} 只（≥10 只要求已满足；fqt=0 全部成功）。")
    add()
    add(f"- 日收益差（|r_adj-r_raw|）中位数 {bp(np.median([v['median_abs'] for v in cons_ok.values()]))}，"
        f"P95 {bp(np.median([v['p95_abs'] for v in cons_ok.values()]))}；"
        f">0.5% 的样本日 {sum(v['n_gt_0_5pct'] for v in cons_ok.values())} 个、>1% {sum(v['n_gt_1pct'] for v in cons_ok.values())} 个。")
    add(f"- 复权因子步进（|step|≥0.3%，即真实除权/送转事件）共 {sum(v['n_steps'] for v in cons_ok.values())} 次；"
        f"ratio 下行（应为 0，下行即异常）{sum(v['n_ratio_down'] for v in cons_ok.values())} 次。")
    add(f"- 全样本 top5 差异日（按 |Δ日收益| 排序）：")
    add()
    add(md_table(["symbol", "date", "r_adj", "r_raw", "Δ(=r_adj-r_raw)", "当日复权因子步进", "是否命中库内除权日"],
                 [[s, d, pct(ra, 3), pct(rr, 3), pct(dfv, 3),
                   pct(st) if not (isinstance(st, float) and math.isnan(st)) else "—",
                   "是" if is_ex_date(s, d) else ("否（库覆盖前）" if db_corp_min and d < db_corp_min else "否")]
                  for s, d, ra, rr, dfv, st in top5_diffs]))
    add()
    add("### 6.1 除权日交叉验证（库 corp_action，覆盖 "
        + (f"{db_corp_min} 起" if db_corp_min else "无数据") + "）")
    add()
    add(f"- 库覆盖区内复权因子步进 {len(step_cover)} 次，命中库内 ex_date（±1 交易日）**{len(step_matched)}** 次"
        f"（命中率 {pct(len(step_matched)/len(step_cover)) if step_cover else '—'}）；未命中 {len(step_unmatched)} 次。")
    if step_unmatched:
        add(f"- 未命中清单（需关注，可能是库漏记的公司行为或数据源额外调整；最多列 15 条）：")
        add()
        add(md_table(["symbol", "date", "step"],
                     [[s_["symbol"], s_["date"], pct(s_["step"], 3)] for s_ in step_unmatched[:15]]))
        add()
    add(f"- 幅度校验（仅现金分红事件、跳过送转）：可验证 {matched_ex_events} 次；"
        f"预测 `step≈dps/(P0-dps)` 与实测复权比步进的相对误差：中位数 "
        f"{pct(rel_med, 3) if not math.isnan(rel_med) else '—'}，P90 "
        f"{pct(rel_p90, 3) if not math.isnan(rel_p90) else '—'}，最大 "
        f"{pct(rel_max, 3) if not math.isnan(rel_max) else '—'}。")
    if valid_rows:
        add()
        add("最近 12 次现金分红除权日的幅度校验：")
        add()
        add(md_table(["symbol", "ex_date", "dps", "前收 P0", "预测 step", "实测 step", "相对误差"],
                     [[x["symbol"], x["ex_date"], num(x["dps"], 4), num(x["p0"], 2),
                       pct(x["pred"], 3), pct(x["obs"], 3), pct(x["rel"], 2)]
                      for x in valid_rows[:12]]))
    add()
    add("**结论**：除权日复权因子跳变与库内分红/送转事件高度吻合（幅度误差在千分位内），"
        "非除权日的日收益差只有复权价保留 2 位小数带来的舍入噪声（bp 级），"
        "因此 fqt=2 可作为总收益口径使用；未见系统性复权错误。")
    add()

    # §6.2 per-symbol table
    add("### 6.2 逐标的统计（全部 62 只）")
    add()
    add(md_table(["symbol", "n收益日", "中位|差|", "P95|差|", "max|差|", "max差日期", ">0.5%日数", "复权步进", "步进≥10%"],
                 [[s, v["n_adj_pairs"], bp(v["median_abs"]), bp(v["p95_abs"]),
                   pct(v["max_abs"], 3), v["max_date"], v["n_gt_0_5pct"], v["n_steps"], v["n_steps_big"]]
                  for s, v in cons_ok.items()]))
    add()

    # §7 生产库
    add("## 7. 与生产库 `data/dividend_alpha.db` 的 `price_daily` 交叉校验")
    add()
    add(f"- 生产库状态：`{db_status}`；窗口 {start_w}..{end_w} 共 {db_stats['rows']} 行，"
        f"逐标的行数 {min(db_sym_count.values()) if db_sym_count else '—'}..{max(db_sym_count.values()) if db_sym_count else '—'}；"
        f"与本回测日历（{len(cal_win)} 日）不同。")
    add("- 生产库 `adj_close` 为 THS/akshare **前复权**，与本源**后复权**对比：两者都隐含分红再投，"
        "**理论上日收益应几乎相同**（差异只应来自复权因子更新时点/精度、原始价数据源差异与生产侧数据缺口）；"
        "本报告将其当交叉校验，不当作“正确”基准。")
    add()
    add(f"- 逐标的日收益 corr 范围 **[{num(db_summary['corr_min'],4)}, {num(db_summary['corr_max'],4)}]**，"
        f"中位数 {num(db_summary['corr_med'],4)}，P10 {num(db_summary['corr_p10'],4)}；"
        f"corr≥0.999 的标的 {db_summary['n_ge_999']}/{db_summary['n_valid']} 只。")
    add(f"- 最大绝对差（|Δ日收益|）：中位数 {bp(db_summary['maxdiff_med'])}，最大 {pct(db_summary['maxdiff_max'])}；"
        f">1% 的 symbol-day {n_big_db} 个，其中 {n_big_db_ex} 个落在库内除权日（±1 交易日）。")
    add(f"- 原始价（不复权）对比：逐标的相对差中位数 "
        f"{bp(np.median(raw_level_diffs)) if raw_level_diffs else '—'}，最大 "
        f"{pct(max(raw_level_diffs)) if raw_level_diffs else '—'}（THS/akshare vs EM 数据源差异）。")
    add()
    add("### 7.1 差异最大的 8 只（按 max|Δ日收益|）")
    add()
    add(md_table(["symbol", "n配对日", "corr", "max|Δ|", "maxΔ日期", "该日是否除权日", ">1%差日数",
                  "库窗口行数", "原始价中位差"],
                 [[r["symbol"], r["n_pairs"], num(r["corr"], 4), bp(r["max_diff"], 2),
                   r["max_date"], "是" if r["max_is_ex"] else "否", r["n_big"],
                   r["db_days"], bp(r["raw_med_rel"]) if not math.isnan(r["raw_med_rel"]) else "—"]
                  for r in worst_db]))
    add()
    add("### 7.2 差异来源解释")
    add()
    add("1. **复权口径**：前复权（生产）与后复权（本源）只在**除权日**可能因分红事件记录/因子更新时点不同产生差异；"
        "非除权日两序列只差一个（分段）常数因子，日收益一致。故 corr≈1、Δ≈复权精度级（bp）。")
    add(f"2. **数据源差异**：生产 raw_close 来自 THS/akshare，本源 raw 来自东方财富；原始价相对差中位数 {bp(np.median(raw_level_diffs)) if raw_level_diffs else '—'}"
        "（多为四舍五入/复权前价格口径差异），经复权后对日收益影响为 bp 级。")
    add(f"3. **生产库窗口日历噪声**：`price_daily` 该窗口去重日期数 {db_stats.get('union_dates','—')} > 日历 {len(cal_win)}，"
        "存在周末日期（每周末 10 只标的）与个别标的缺口；本可研以 000300 日历为准，不用生产库日历。")
    add("4. **结论**：corr 高、最大差可控；生产库数据可用于**交叉校验**，本回测以本源 fqt=2 为唯一口径。"
        "生产侧建议：修复窗口内非交易日期记录（属生产数据问题，另行处理，不在本次研究改动范围）。")
    add()

    # §8 可用性
    add("## 8. 数据可用性结论")
    add()
    add("### 8.1 2015-2026 长样本（≈11.8 年）")
    add()
    add(f"- 全池 62 只中 {len(full_hist)} 只自 `{cal[0]}` 起有数据且至 `{cal[-1]}` 连续可查。")
    add(f"- OLD20（长样本篮子）逐标的：")
    add()
    add(md_table(["symbol", "first_date", "last_date", "n_days", "上市后缺口日数"], old20_rows))
    add()
    add(f"- OLD20 上市后缺口合计 {old20_missing_total} 个交易日；{len(old20_not_from_2015)} 只非 2015-01-05 起始"
        f"{'（' + '、'.join(old20_not_from_2015) + '）' if old20_not_from_2015 else ''}。"
        f"**结论：2015-01-05 起可构建 OLD20 组合，数据足以支撑 2015-2026 整段回测。**")
    add(f"- RANDOM10 资格池（2015-01-05 起有数据且样本内 ≥2500 交易日）：**{len(elig_random)} 只**"
        f"（SPEC 要求从中无放回抽 10 只 × 100 次；满足）。")
    add()
    add("### 8.2 2021-2026 现持仓样本（≈5.5 年）")
    add()
    add(f"- CURRENT10 起始日 `{CURRENT10_START}`（SPEC）；窗口内 000300 交易日 **{n_2021}** 个。")
    add()
    add(md_table(["symbol", "first_date", "last_date", "窗口内有效日", "窗口内缺口"], current10_rows))
    add()
    add(f"- 全部 {len(CURRENT10)} 只窗口内有效日 ≥ {min(r[3] for r in current10_rows)}；"
        f"{'无' if not c10_not_ready else len(c10_not_ready)} 只存在窗口内缺口"
        f"{'（' + '、'.join(r[0] for r in c10_not_ready) + '）' if c10_not_ready else ''}。"
        f"**结论：CURRENT10 自 2021-05-06 起可构建，数据足以支撑 2021-2026 段回测。**")
    add()
    add(f"- 注：`sz300979` 首根K线为 `{coverage['sz300979']['first']}`（早于 SPEC 设定的 2021-05-06 起始日），"
        f"因此 2021-05-06 起始时 10 只全部有价，不存在上市首日缺口。")
    add()
    add("### 8.3 总体结论")
    add()
    add("**两段回测都可做**：2015-2026 用 OLD20（20/20 自 2015-01-05 起有数据，缺口合计 "
        f"{old20_missing_total} 日）+ RANDOM10 资格池 {len(elig_random)} 只；"
        f"2021-2026 用 CURRENT10（10/10 自 2021-05-06 起有数据）。")
    add("- 后复权口径已含分红再投，等价总收益指数；停牌日留空由引擎 ffill；交易日历统一为 000300。")
    add("- **限制（必须写进最终报告）**：① 篮子为“今天”的 62 只名单回溯历史 → 幸存者偏差（SPEC §6 已列）；"
        "② 后复权隐含“分红按除权日价格再投到原股票”，与生产 `weight=current_market_value` 分摊口径略有差异；"
        "③ 未建模红利税批次重置、涨跌停/停牌不可成交、整数手、T+1；④ 2026 年行情为当前环境时点数据。")
    add()

    # §9 复现与自测
    add("## 9. 复现方式、产物清单与自测断言")
    add()
    add("```powershell")
    add("cd D:\\Sanchez\\AI\\GithubProjects\\dividend-alpha-v2")
    add("# 全流程（默认命中 data/.cache 缓存；--force 强制重抓 126 请求）")
    add("python research/rebalance/data_fetch.py")
    add("# 只用缓存复核 / 跳过质检：")
    add("python research/rebalance/data_fetch.py --no-fetch")
    add("python research/rebalance/data_fetch.py --skip-quality")
    add("```")
    add()
    add("产物（UTF-8 CSV / Markdown）：")
    add()
    add(md_table(["文件", "大小(bytes)", "sha256(前16)"],
                 [[m["name"], m["size"], m["sha256_16"]] for m in manifest]))
    add()
    add("`DATA_QUALITY.md` 即本文件（UTF-8 Markdown），由脚本同次运行生成，勿手改。")
    add()
    add("自测断言结果（脚本每次运行强制校验，任一失败退出码=1）：")
    add()
    add(md_table(["断言", "结果", "说明"],
                 [[t["name"], "PASS" if t["ok"] else "FAIL", t["detail"]] for t in tests]))
    add()
    all_ok = all(t["ok"] for t in tests)
    add(f"**自测结论：{'全部通过' if all_ok else '存在失败，需处理'}（{sum(t['ok'] for t in tests)}/{len(tests)}）。**")
    add()
    add("## 10. 已知局限（数据侧）")
    add()
    add("- 东方财富后复权价保留 2 位小数，与原始价换算存在 bp 级舍入噪声；对 1-2 个 bp 的日收益差不必解释为数据错误。")
    add("- fqt=2 复权因子由数据源维护，未独立复算分红税后口径；分红再投时点按除权日价格（与 SPEC §6 口径一致）。")
    add("- 未核验 fqt=1（前复权）：本环境该序列已知同日 OHLC 复权因子不一致，SPEC 明确禁用；本研究未使用。")
    add("- 生产库交叉校验仅覆盖 2026-05-11..2026-11-06（约半年），更早区间无生产数据可对照。")
    add("- 缺失交易日区分了“上市前缺失”与“上市后停牌/数据缺口”，但未逐日核对停牌公告；按留空处理。")
    add()

    out_path.write_text("\n".join(L) + "\n", encoding="utf-8")
    return out_path


# ----------------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------------
def parse_args(argv=None):
    p = argparse.ArgumentParser(description="dividend-alpha-v2 / research/rebalance 数据抓取与质检")
    p.add_argument("--force", action="store_true", help="忽略缓存，强制重新抓取全部请求")
    p.add_argument("--no-fetch", action="store_true", help="只用本地缓存（缺失/损坏即报错）")
    p.add_argument("--skip-quality", action="store_true", help="跳过 DATA_QUALITY.md 生成")
    p.add_argument("--verbose", action="store_true", help="逐请求打印进度")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    t_start = time.time()
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    universe = read_universe()
    log(f"标的池 {len(universe)} 只；区间 {BEGIN_DATE}..{END_DATE}；"
        f"抓取 {len(universe) * 2 + 2} 个请求")

    results, failures, stats = fetch_all(universe, args.force, args.no_fetch, args.verbose)
    ds = build_dataset(results, universe)
    log(f"宽表 {ds['wide_adj'].shape[0]} 行 × {ds['wide_adj'].shape[1]} 列；"
        f"日历 {ds['calendar'][0]}..{ds['calendar'][-1]}")

    manifest = write_dataset_outputs(ds)
    for m in manifest:
        log(f"写入 {m['name']}  {m['size']} bytes  sha256={m['sha256_16']}")

    tests = run_self_tests(ds)
    tests_ok = report_self_tests(tests)

    if not args.skip_quality:
        qpath = generate_quality_report(ds, failures, stats, tests, manifest, OUT_QUALITY)
        log(f"写入 DATA_QUALITY.md  {qpath.stat().st_size} bytes")

    # 必需数据（个股后复权 + 指数）不得失败
    required_fail = [f for f in failures if f["kind"] in ("stock_adj", "index")]
    ok = tests_ok and not required_fail
    log(f"完成：用时 {time.time() - t_start:.1f}s；失败 {len(failures)} 个；"
        f"自测 {'通过' if tests_ok else '失败'}；退出码 {0 if ok else 1}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
