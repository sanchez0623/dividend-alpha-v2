"""全市场选股漏斗（月度重建）。

分阶段（每阶段打印存活数）：
  S1 东财全市场快照 → 市值≥300亿 + 剔除 ST/退市（akshare 同源数据，固定主域名直连）
  S2 fuyao 除权事件（回看 6.8 年）→ 连续现金分红 ≥5 年
  S3 股息率TTM = 近365天每股分红 ÷ 最新价 → ≥2.5%
  S4 东财行业/银行子类 → select_core 行业约束 → 前 10

★ 可复现性（2026-11-08 加固）：
  - 单只/单批查询失败：限流(429/4001)与网络错误按指数退避重试（data.funnel.retry_attempts）；
    BizError(code=1002, Unknown thscode) 为永久错误不重试；任何最终失败都落 WARNING 日志
    并计入漏斗审计（funnel_audit 表），不再静默丢弃。
  - 熔断：S2/S3 失败率 > data.funnel.max_fail_ratio → 判定数据源异常，本次漏斗作废
    （返回 None，不写候选缓存，保留上次结果），杜绝"半残漏斗"静默进入组合。
  - 同日幂等：成功漏斗的结果与 S1 快照存入 funnel_audit；同 asof 再次调用直接返回
    已验证短名单，同日重跑/重放逐字一致。

失败降级：任何阶段异常 → 返回 None（调用方回退上次缓存/演示池）。
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import date, datetime, timedelta

import pandas as pd
from sqlalchemy.orm import Session

from ..da_core.config import Strategy
from ..da_core.models import CorpAction as CorpActionORM
from ..da_core.models import FunnelAudit
from ..da_core.models import UniverseCache
from ..da_data.ingest import ingest_corp_action
from ..da_data.providers.base import BizError, RateLimited
from ..da_data.providers.eastmoney import bank_subtype_for, fetch_all_a_spot, fetch_individual_info
from ..da_data.providers.symbol_map import to_internal, to_thscode
from ..da_data.providers.ths import TZ_BJ, ThsFuyaoClient

log = logging.getLogger("da_data.universe")


def _consecutive_years(events: list, asof: date) -> int:
    years = {e.ex_date.year for e in events if float(e.dps or 0) > 0}
    n, y = 0, asof.year
    while y in years and n < 30:
        n += 1
        y -= 1
    return n


def _with_retry(fn, *, label: str, retry_attempts: int, backoff_base: float):
    """执行 fn()：限流(429/4001)与网络类瞬时失败按指数退避重试；
    BizError(code=1002 Unknown thscode / 3002 No adjustment events) 为确定性响应不重试；
    每次尝试都落日志。返回 (result, None) 或最终失败 (None, failure_dict)。
    """
    attempts = max(int(retry_attempts), 1)
    permanent = {"1002", "3002"}
    last: dict | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fn(), None
        except RateLimited as e:
            last = {"type": "rate_limited", "code": None, "detail": str(e)[:160]}
            delay = max(float(e.retry_after or 0.0), backoff_base * (2 ** (attempt - 1)))
            log.warning("漏斗 %s 第%d/%d次失败(限流): %s → %.1fs 后重试",
                        label, attempt, attempts, e, delay)
            time.sleep(delay)
        except BizError as e:
            last = {"type": "biz_error", "code": str(e.code), "detail": str(e)[:160]}
            if str(e.code) in permanent:
                log.warning("漏斗 %s 确定性业务错误(不重试): %s", label, e)
                return None, last
            log.warning("漏斗 %s 第%d/%d次失败(业务code=%s): %s",
                        label, attempt, attempts, e.code, e)
            time.sleep(backoff_base * (2 ** (attempt - 1)))
        except Exception as e:  # noqa: BLE001 —— httpx 网络错误及其它瞬时异常
            last = {"type": type(e).__name__, "code": None, "detail": str(e)[:160]}
            log.warning("漏斗 %s 第%d/%d次失败(%s): %s",
                        label, attempt, attempts, type(e).__name__, e)
            time.sleep(backoff_base * (2 ** (attempt - 1)))
    return None, last


def _write_audit(session: Session, asof: date, status: str, cands: dict,
                 s2_scanned: int, s2_failures: list[dict],
                 shortlist: list[str] | None, snapshot: list | None) -> None:
    """写/更新漏斗审计行（同 asof 幂等覆盖）。"""
    row = session.get(FunnelAudit, asof)
    if row is None:
        row = FunnelAudit(trade_date=asof)
        session.add(row)
    row.status = status
    row.s1_count = len(cands)
    row.s2_scanned = int(s2_scanned)
    row.s2_failed = len(s2_failures)
    row.s2_failures_json = json.dumps(s2_failures, ensure_ascii=False) if s2_failures else None
    row.symbols_json = json.dumps(shortlist) if shortlist is not None else None
    row.snapshot_json = json.dumps(snapshot, ensure_ascii=False) if snapshot else None
    session.flush()


def build_candidates(session: Session, chain, cfg: Strategy, asof: date,
                     fuyao: ThsFuyaoClient | None = None) -> list[str] | None:
    """全市场漏斗 → 合格候选（股息率达标短名单）。失败/熔断返回 None。"""
    try:
        u = cfg.universe
        min_cap = float(u["min_market_cap"])
        min_years = int(u["min_consecutive_dividend_years"])
        min_yld = float(u["min_div_yield_ttm"])
        fcfg = cfg.data.get("funnel") or {}
        retry_attempts = int(fcfg.get("retry_attempts", 3))
        backoff_base = float(fcfg.get("retry_backoff_sec", 2))
        max_fail_ratio = float(fcfg.get("max_fail_ratio", 0.05))

        # ---- ★ 同 asof 幂等复用：已有成功审计 → 直接返回已验证短名单 ----
        prior = session.get(FunnelAudit, asof)
        if prior is not None and prior.status == "ok" and prior.symbols_json:
            try:
                cached = json.loads(prior.symbols_json)
            except Exception:  # noqa: BLE001
                cached = None
            if cached:
                log.info("universe: 复用 %s 已验证漏斗结果 → %d 只（同日重跑完全一致）",
                         asof, len(cached))
                return cached

        # ---- S1 全市场快照 + 门槛 ----
        spot = fetch_all_a_spot()
        if spot is None or spot.empty:
            log.warning("universe S1: 全市场快照为空")
            return None
        # ★ 停牌/无数据行的市值/价格是字符串"-"，强转数值后丢弃
        spot["market_cap"] = pd.to_numeric(spot["market_cap"], errors="coerce")
        spot["last"] = pd.to_numeric(spot["last"], errors="coerce")
        spot = spot.dropna(subset=["market_cap", "last"])
        spot = spot[spot["market_cap"] >= min_cap]
        spot = spot[~spot["name"].fillna("").str.contains("ST|退", na=False)]
        cands = {r["symbol"]: {"name": r["name"], "last": float(r["last"]),
                               "cap": float(r["market_cap"])}
                 for _, r in spot.iterrows()}
        log.info("universe S1: 市值≥%.0f亿 且非ST → %d 只", min_cap / 1e8, len(cands))
        if not cands:
            _write_audit(session, asof, "aborted", {}, 0, [], None, None)
            return None

        # ---- S2 除权事件 → 连续分红年数（★ 重试 + 全量失败记录，杜绝静默丢弃） ----
        fuyao = fuyao or ThsFuyaoClient(api_key=os.environ.get("THS_API_KEY", ""))
        since = asof - timedelta(days=2500)
        survivors: dict[str, list] = {}
        s2_failures: list[dict] = []
        s2_no_events = 0
        scanned = 0
        for sym in sorted(cands):  # ★ 固定按代码序扫描，快照行序不影响结果
            scanned += 1
            ths = to_thscode(sym)
            if not ths:
                continue
            rows, err = _with_retry(
                lambda ths=ths: fuyao.adjustment_factors(ths, since),
                label=f"S2 {sym}", retry_attempts=retry_attempts,
                backoff_base=backoff_base)
            if err:
                if str(err.get("code")) == "3002":
                    # 3002 = 该股无任何除权事件（次新/从未分红送转）→ 语义为空事件流，
                    # 确定性数据事实而非失败；自然过不了连续分红年限筛
                    s2_no_events += 1
                    events = []
                else:
                    s2_failures.append({"symbol": sym, **err})
                    continue
            else:
                events = fuyao.to_corp_actions(sym, rows)
            try:
                ingest_corp_action(session, events)
            except Exception as e:  # noqa: BLE001
                log.warning("S2 %s 除权事件落库失败(不影响本轮漏斗): %s", sym, e)
            years = _consecutive_years(events, asof)
            if years >= min_years:
                survivors[sym] = events
            if scanned % 50 == 0:
                log.info("universe S2: %d/%d 已扫，存活 %d，失败 %d",
                         scanned, len(cands), len(survivors), len(s2_failures))
                session.flush()
        log.info("universe S2: 连续分红≥%d年 → %d 只；失败 %d 只；无除权事件(3002) %d 只",
                 min_years, len(survivors), len(s2_failures), s2_no_events)
        fail_ratio = len(s2_failures) / scanned if scanned else 0.0
        if survivors and fail_ratio > max_fail_ratio:
            log.error("universe S2 失败率 %.1f%% 超阈值 %.1f%% → 判定数据源异常，熔断本次漏斗"
                      "（不写候选缓存，保留上次结果）", fail_ratio * 100, max_fail_ratio * 100)
            _write_audit(session, asof, "aborted", cands, scanned, s2_failures, None, None)
            return None
        if not survivors:
            log.warning("universe S2: 无存活标的")
            _write_audit(session, asof, "aborted", cands, scanned, s2_failures, None, None)
            return None

        # ---- S3 股息率TTM（★ 用 asof 当日不复权收盘价——历史重放口径一致，
        #      不用实时快照价，否则重放 11-05 会按 11-07 的价格排序）----
        surv_syms = sorted(survivors)
        batches = [[to_thscode(x) for x in surv_syms[i:i + 10] if to_thscode(x)]
                   for i in range(0, len(surv_syms), 10)]
        batches = [b for b in batches if b]
        closes: dict[str, float] = {}
        s3_failed_syms: list[str] = []
        for bi, batch in enumerate(batches, 1):
            raw, err = _with_retry(
                lambda b=batch: fuyao.prices_historical_batch(
                    b, asof - timedelta(days=30), asof, "none"),
                label=f"S3 批次{bi}({batch[0]}…)", retry_attempts=retry_attempts,
                backoff_base=backoff_base)
            if err:
                log.warning("universe S3 批次 %d 最终失败，影响 %d 只: %s",
                            bi, len(batch), batch)
                s3_failed_syms.extend(batch)
                continue
            try:
                for group in raw.get("item", []):
                    sym = to_internal(group.get("thscode") or "")
                    bars = group.get("item") or []
                    if not sym or not bars:
                        continue
                    last_bar = max(bars, key=lambda b: b["date_ms"])
                    bd = datetime.fromtimestamp(last_bar["date_ms"] / 1000, tz=TZ_BJ).date()
                    if last_bar.get("close_price") and bd <= asof:
                        closes[sym] = float(last_bar["close_price"])
            except Exception as e:  # noqa: BLE001
                log.warning("universe S3 批次 %d 解析失败，影响 %d 只: %s", bi, len(batch), e)
                s3_failed_syms.extend(batch)
        s3_scanned = sum(len(b) for b in batches)
        if s3_scanned and len(s3_failed_syms) / s3_scanned > max_fail_ratio:
            log.error("universe S3 失败率 %.1f%% 超阈值 %.1f%% → 熔断本次漏斗"
                      "（不写候选缓存，保留上次结果）",
                      len(s3_failed_syms) / s3_scanned * 100, max_fail_ratio * 100)
            _write_audit(session, asof, "aborted", cands, scanned, s2_failures, None, None)
            return None
        shortlist: dict[str, float] = {}
        for sym, events in survivors.items():
            close = closes.get(sym)
            if not close:
                if sym in s3_failed_syms:
                    log.warning("universe S3 %s 因批次失败无收盘价，本轮剔除", sym)
                continue
            ws = date(asof.year - 1, asof.month, asof.day)
            dps_sum = sum(float(e.dps or 0) for e in events if ws < e.ex_date <= asof)
            if dps_sum <= 0:
                continue
            yld = dps_sum / close
            if yld >= min_yld:
                shortlist[sym] = yld
        log.info("universe S3: 股息率≥%.1f%% → %d 只；批次失败影响 %d 只",
                 min_yld * 100, len(shortlist), len(s3_failed_syms))
        if not shortlist:
            return None

        # ---- S4 行业/银行子类 → 行业约束（select_core 消费） ----
        info = fetch_individual_info(list(shortlist))
        from ..da_core.models import SymbolStatus

        for _, r in info.iterrows():
            row = session.query(SymbolStatus).filter_by(
                symbol=r["symbol"], trade_date=asof).one_or_none()
            if row is None:
                row = SymbolStatus(symbol=r["symbol"], trade_date=asof)
                session.add(row)
            if r.get("name"):
                row.name = r["name"]
            if r.get("industry_l1"):
                row.industry_l1 = r["industry_l1"]
            row.bank_subtype = bank_subtype_for(r.get("name"), r.get("industry_l1"))
        session.flush()
        log.info("universe S4: 行业/子类已同步 %d 只", len(info))

        snapshot = [{"symbol": k, "name": v["name"], "last": v["last"], "cap": v["cap"]}
                    for k, v in sorted(cands.items())]
        _write_audit(session, asof, "ok", cands, scanned, s2_failures,
                     sorted(shortlist), snapshot)
        save_cache(session, asof, sorted(shortlist))
        return sorted(shortlist)
    except Exception as e:  # noqa: BLE001
        log.warning("universe 构建失败，回退演示池: %s", e)
        return None


def save_cache(session: Session, asof: date, symbols: list[str]) -> None:
    row = session.get(UniverseCache, asof)
    if row is None:
        row = UniverseCache(trade_date=asof)
        session.add(row)
    row.symbols_json = json.dumps(symbols)
    session.flush()


def load_last_candidates(session: Session) -> list[str] | None:
    row = (session.query(UniverseCache)
           .order_by(UniverseCache.trade_date.desc()).first())
    if row and row.symbols_json:
        try:
            return json.loads(row.symbols_json)
        except Exception:  # noqa: BLE001
            return None
    return None
