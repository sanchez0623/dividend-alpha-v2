"""盘后闭环：ingest → adj_close → factors → signals → 订单 → 净值 → B1–B6 推进。"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from sqlalchemy.orm import Session

from ..da_benchmark.b1_b6 import advance, init_states
from ..da_core.config import Strategy
from ..da_core.models import Account, DailyNav
from ..da_data.factors import compute_factors, sync_market_cap, sync_symbol_meta
from ..da_data.fallback import FallbackChain, build_chain
from ..da_data.ingest import ingest_daily
from ..da_data.universe import build_candidates, load_last_candidates
from ..da_portfolio.ledger import Ledger, STRATEGY_ID, BENCHMARK_IDS
from ..da_portfolio.rebalance import rebalance_due, run_rebalance
from ..da_signal.pipeline import generate_and_store_signals

log = logging.getLogger("da_worker.daily_close")


def run_daily_close(session: Session, cfg: Strategy, account_id: str = "PAPER",
                    asof: date | None = None, chain: FallbackChain | None = None) -> dict:
    asof = asof or date.today()
    chain = chain or FallbackChain(build_chain(cfg.data.get("providers", {})))
    held = [p.symbol for p in _held_symbols(session, account_id)]
    rebalance = rebalance_due(session, account_id, cfg, asof)

    # ★ 候选池：调仓日重建全市场漏斗；其余日用缓存短名单；失败回退演示池
    candidates: list[str] | None = None
    if rebalance:
        candidates = build_candidates(session, chain, cfg, asof)
    if candidates is None:
        candidates = load_last_candidates(session) or []
    if not candidates:
        candidates = held
    symbols = sorted(set(held) | set(candidates)) or _demo_symbols(chain)
    log.info("run_daily_close %s: 候选 %d + 持仓 %d → 处理 %d 只",
             asof, len(candidates), len(held), len(symbols))
    start = asof - timedelta(days=int(cfg.data.get("adj_window_trading_days", 90)) * 2)

    rows = ingest_daily(session, chain, symbols, start, asof)
    sync_market_cap(session, symbols, asof)  # 腾讯免费快照补 market_cap
    sync_symbol_meta(session, symbols, asof)  # ★ 名称/行业L1/银行子类（东财+QQ）
    compute_factors(session, cfg, symbols, asof)
    n_signals = generate_and_store_signals(session, cfg, symbols, asof)  # ★ 信号流水落库

    # ★ 建仓执行链：调仓日已确认 → 选核心仓 → broker 补仓（候选=全市场漏斗短名单）
    trades: list[tuple[str, str, int]] = []
    if rebalance:
        trades = run_rebalance(session, account_id, cfg, symbols, asof)

    ledger = Ledger(session, account_id, cfg)
    # 策略净值：现金 + 持仓按 asof 当日收盘估值（★ 禁用未来价格）
    acc = session.get(Account, account_id)
    cash = float(acc.cash) if acc else float(cfg.init_cash)
    s_nav = cash
    for p in _positions_value(session, account_id, asof=asof):
        s_nav += p
    navs = {STRATEGY_ID: s_nav}
    # B1–B6 推进（真实指数收益：fuyao → QQ 免费 → 链上容错降级）
    states = _load_states(session, account_id, cfg.init_cash, asof=asof)
    rets = {b: _bench_ret(chain, b, asof) for b in BENCHMARK_IDS}
    states = advance(states, rets)
    navs.update(states)

    ledger.write_daily_nav(asof, strategy_navs=navs)
    session.commit()
    return {"trade_date": asof.isoformat(), "ingest_rows": rows,
            "signals": n_signals, "trades": [{"symbol": s, "side": sd, "shares": n}
                                             for s, sd, n in trades],
            "navs": navs}


def _held_symbols(session: Session, account_id: str) -> list:
    from ..da_core.models import Position

    return (session.query(Position)
            .filter_by(account_id=account_id)
            .filter(Position.shares_total > 0).all())


def _demo_symbols(chain) -> list[str]:
    return [m["symbol"] for m in _universe_symbols(chain)]


def _universe_symbols(chain) -> list[dict]:
    for src in chain.sources:
        if hasattr(src, "universe"):
            return src.universe()
    return []


def _positions_value(session: Session, account_id: str,
                     asof: date | None = None) -> list[float]:
    """持仓估值（★ asof 过滤：重放时禁止使用未来价格）。"""
    from ..da_core.models import Position, PriceDaily

    vals = []
    for p in session.query(Position).filter_by(account_id=account_id).all():
        q = session.query(PriceDaily).filter(PriceDaily.symbol == p.symbol)
        if asof is not None:
            q = q.filter(PriceDaily.trade_date <= asof)
        r = q.order_by(PriceDaily.trade_date.desc()).first()
        price = float(r.adj_close) if r else float(p.cost_price)
        vals.append(float(p.shares_total) * price)
    return vals


def _load_states(session: Session, account_id: str, init_cash: float,
                 asof: date | None = None) -> dict[str, float]:
    states = init_states(init_cash)
    q = session.query(DailyNav).filter_by(account_id=account_id)
    if asof is not None:
        q = q.filter(DailyNav.trade_date < asof)  # ★ 只取 asof 之前的净值（重放正确性）
    rows = q.all()
    latest: dict[str, float] = {}
    for r in rows:
        if r.strategy_id in BENCHMARK_IDS:
            latest[r.strategy_id] = float(r.nav)
    states.update(latest)
    return states


def _bench_ret(chain, bench: str, asof: date) -> float:
    """基准日收益率：真实指数（fuyao → QQ 免费 → 其余链上源容错降级），
    不再取合成源近似。B2（不复投对照）与 B6（安慰剂）不走指数，恒 0。"""
    try:
        code = {"B1": "000300", "B3": "000922", "B4": "000300", "B5": "000922"}.get(bench)
        if not code:
            return 0.0
        df = chain.fetch_index(code, asof - timedelta(days=7), asof)
        if df is None or len(df) < 2:
            log.warning("bench %s: 指数数据不足(<2根)，当日记 0", bench)
            return 0.0
        c = df["close"].astype(float).reset_index(drop=True)
        return float(c.iloc[-1] / c.iloc[-2] - 1.0)
    except Exception:  # noqa: BLE001
        return 0.0
