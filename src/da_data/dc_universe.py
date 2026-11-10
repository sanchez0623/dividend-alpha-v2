"""DC 数据层：周度全市场池刷新 + 候选近期不复权日线。

设计：
  - 周度：东财全市场快照 → 市值≥500 亿/非 ST → 同花顺除权事件逐只落库；
    失败率超阈值熔断（返回 None，保留上次 DcUniverseCache）。
  - 每日：只对「除权日落在近 10 天」的候选抓近 180 天不复权日线，按生产
    同款 build_adj_factor 重算 adj_close（与 S* 的技术指标口径一致）。
  - 不向合成源借数据；corp_action 只信真实源。
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import date, timedelta

import pandas as pd
from sqlalchemy.orm import Session

from ..da_core.dc_config import DcStrategy
from ..da_core.models import CorpAction as CorpActionORM
from ..da_core.models import DcUniverseCache, SymbolStatus
from ..da_data.adjust import CorpAction, adj_close_series, build_adj_factor
from ..da_data.ingest import ingest_corp_action, upsert_price
from ..da_data.providers.base import BizError, RateLimited
from ..da_data.providers.eastmoney import fetch_all_a_spot
from ..da_data.providers.symbol_map import to_thscode
from ..da_data.providers.ths import ThsFuyaoClient

log = logging.getLogger("da_data.dc_universe")


def _d(v) -> date:
    return v if isinstance(v, date) else pd.Timestamp(v).date()


def _f(v) -> float | None:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    return float(v)


def _retry_call(fn, *, label: str, attempts: int, backoff: float):
    """返回 (result, err)。限流/网络错误指数退避；1002 永久错误不重试。"""
    last = None
    for attempt in range(1, max(int(attempts), 1) + 1):
        try:
            return fn(), None
        except RateLimited as e:
            last = {"type": "rate_limited", "code": None, "detail": str(e)[:160]}
            delay = max(float(e.retry_after or 0.0), backoff * (2 ** (attempt - 1)))
            log.warning("DC %s 第%d/%d次失败(限流): %s → %.1fs",
                        label, attempt, attempts, e, delay)
            time.sleep(delay)
        except BizError as e:
            last = {"type": "biz_error", "code": str(e.code), "detail": str(e)[:160]}
            if str(e.code) in ("1002", "3002"):
                log.info("DC %s 确定性业务错误(不重试): %s", label, e)
                return None, last
            log.warning("DC %s 第%d/%d次失败(code=%s): %s",
                        label, attempt, attempts, e.code, e)
            time.sleep(backoff * (2 ** (attempt - 1)))
        except Exception as e:  # noqa: BLE001
            last = {"type": type(e).__name__, "code": None, "detail": str(e)[:160]}
            log.warning("DC %s 第%d/%d次失败(%s): %s",
                        label, attempt, attempts, type(e).__name__, e)
            time.sleep(backoff * (2 ** (attempt - 1)))
    return None, last


def refresh_dc_universe(session: Session, dc: DcStrategy, asof: date,
                        fuyao: ThsFuyaoClient | None = None,
                        force: bool = False) -> list[str] | None:
    """全市场市值≥500 亿池刷新；成功写 DcUniverseCache，失败/熔断返回 None。"""
    u = dc.universe
    min_cap = float(u["min_market_cap"])
    max_fail = float(u.get("max_fail_ratio", 0.05))
    attempts = int(u.get("retry_attempts", 3))
    backoff = float(u.get("retry_backoff_sec", 2))
    since_days = int(u.get("refresh_since_days", 800))

    prior = session.get(DcUniverseCache, asof)
    if prior is not None and prior.symbols_json and not force:
        return json.loads(prior.symbols_json)

    spot = fetch_all_a_spot()
    if spot is None or spot.empty:
        log.warning("DC universe: 全市场快照为空")
        return None
    spot["market_cap"] = pd.to_numeric(spot["market_cap"], errors="coerce")
    spot["last"] = pd.to_numeric(spot["last"], errors="coerce")
    spot = spot.dropna(subset=["market_cap", "last"])
    if dc.universe.get("exclude_st", True):
        spot = spot[~spot["name"].fillna("").str.contains("ST|退", na=False)]
    spot = spot[spot["market_cap"] >= min_cap]
    cands = {
        r["symbol"]: {"name": (r["name"] if isinstance(r["name"], str) else None),
                      "last": float(r["last"]), "cap": float(r["market_cap"])}
        for _, r in spot.iterrows()
    }
    log.info("DC universe S1: 市值≥%.0f亿 且非ST → %d 只", min_cap / 1e8, len(cands))
    if not cands:
        return None

    fuyao = fuyao or ThsFuyaoClient(api_key=os.environ.get("THS_API_KEY", ""))
    since = asof - timedelta(days=since_days)
    failures: list[dict] = []
    survivors: dict[str, dict] = {}
    scanned = 0
    for sym in sorted(cands):
        scanned += 1
        ths = to_thscode(sym)
        if not ths:
            continue
        rows, err = _retry_call(
            lambda ths=ths: fuyao.adjustment_factors(ths, since),
            label=f"DC {sym}", attempts=attempts, backoff=backoff)
        if err:
            if str(err.get("code")) == "3002":
                events = []          # 无除权事件：确定性空事件流
            else:
                failures.append({"symbol": sym, **err})
                continue
        else:
            events = fuyao.to_corp_actions(sym, rows)
        try:
            ingest_corp_action(session, events)
        except Exception as e:  # noqa: BLE001
            log.warning("DC %s 除权事件落库失败(不影响池): %s", sym, e)
        st = session.get(SymbolStatus, {"symbol": sym, "trade_date": asof})
        if st is None:
            st = SymbolStatus(symbol=sym, trade_date=asof)
            session.add(st)
        st.name = cands[sym]["name"]
        st.market_cap = cands[sym]["cap"]
        st.is_st = False
        survivors[sym] = cands[sym]
        if scanned % 50 == 0:
            log.info("DC universe: %d/%d 已扫，存活 %d，失败 %d",
                     scanned, len(cands), len(survivors), len(failures))
            session.flush()

    fail_ratio = len(failures) / scanned if scanned else 0.0
    if fail_ratio > max_fail:
        log.error("DC universe 失败率 %.1f%% 超阈值 %.1f%% → 熔断，保留上次缓存",
                  fail_ratio * 100, max_fail * 100)
        return None
    if not survivors:
        return None

    snapshot = [{"symbol": k, **v} for k, v in sorted(survivors.items())]
    row = session.get(DcUniverseCache, asof)
    if row is None:
        row = DcUniverseCache(trade_date=asof)
        session.add(row)
    row.symbols_json = json.dumps(sorted(survivors))
    row.snapshot_json = json.dumps(snapshot, ensure_ascii=False)
    row.n_candidates = len(survivors)
    session.flush()
    log.info("DC universe: 写入 %s，%d 只（失败 %d）", asof, len(survivors), len(failures))
    return sorted(survivors)


def load_dc_universe(session: Session) -> tuple[list[str], date] | None:
    row = (
        session.query(DcUniverseCache)
        .order_by(DcUniverseCache.trade_date.desc())
        .first()
    )
    if row is None or not row.symbols_json:
        return None
    try:
        return json.loads(row.symbols_json), row.trade_date
    except Exception:  # noqa: BLE001
        return None


def _corp_events(session: Session, symbol: str, asof: date) -> list[CorpAction]:
    rows = (
        session.query(CorpActionORM)
        .filter(CorpActionORM.symbol == symbol,
                CorpActionORM.ex_date <= asof)
        .order_by(CorpActionORM.ex_date.asc())
        .all()
    )
    return [
        CorpAction(symbol=r.symbol, ex_date=r.ex_date, dps=float(r.dps or 0),
                   bonus_ratio=float(r.bonus_ratio or 0),
                   transfer_ratio=float(r.transfer_ratio or 0),
                   rights_ratio=float(r.rights_ratio or 0),
                   rights_price=float(r.rights_price or 0))
        for r in rows
    ]


def fetch_dc_prices(session: Session, chain, symbols: list[str], asof: date,
                    lookback_days: int = 180) -> int:
    """抓候选近 lookback_days 交易日不复权日线，按 DB 除权事件重算 adj_close。"""
    if not symbols:
        return 0
    start = asof - timedelta(days=int(lookback_days * 2.0))
    try:
        df = chain.fetch_daily(symbols, start, asof)
    except Exception as e:  # noqa: BLE001
        log.warning("DC 日线抓取失败: %s", e)
        return 0
    if df is None or df.empty:
        return 0
    rows = 0
    for sym, g in df.groupby("symbol"):
        g = g.sort_values("trade_date")
        if "raw_close" not in g.columns:
            continue
        idx = [_d(x) for x in g["trade_date"]]
        raw = pd.Series(g["raw_close"].astype(float).values, index=idx)
        events = _corp_events(session, sym, asof)
        known = [e for e in events if e.ex_date <= asof]
        try:
            factor = build_adj_factor(raw, known)
            adj = adj_close_series(raw, known)
        except Exception as e:  # noqa: BLE001
            log.warning("DC %s 复权失败: %s", sym, e)
            factor = pd.Series(1.0, index=raw.index)
            adj = raw
        for i, (_, r) in enumerate(g.iterrows()):
            td = _d(r["trade_date"])
            upsert_price(
                session, symbol=sym, trade_date=td,
                open_=_f(r.get("open")), high=_f(r.get("high")), low=_f(r.get("low")),
                raw_close=float(r["raw_close"]), adj_factor=float(factor.iloc[i]),
                adj_close=float(adj.iloc[i]), amount=_f(r.get("amount")),
                source=str(r.get("source", "dc")),
            )
            rows += 1
    session.flush()
    log.info("DC 日线: %d 只 → %d 行", len(symbols), rows)
    return rows
