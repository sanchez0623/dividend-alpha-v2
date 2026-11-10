"""账本（★ 最先实现，Forward-only 的第一原则）。

daily_nav 是 P5 基准页双模式守门的唯一数据来源；signal_pair 是 B6 样本 m 的唯一来源。
写库任务全部幂等：(trade_date, account_id, strategy_id) / (year, account_id)。
"""
from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy.orm import Session

from ..da_core.models import Account, DailyNav, ExecutionLog, SignalPair
from ..da_core.config import Strategy

STRATEGY_ID = "S*"
BENCHMARK_IDS = ["B1", "B2", "B3", "B4", "B5", "B6"]


class Ledger:
    def __init__(self, session: Session, account_id: str, cfg: Strategy):
        self.s = session
        self.account_id = account_id
        self.cfg = cfg

    # ---------- T01：首日初始化 ----------
    def init_day_one(self, asof: date, cash: float) -> list[DailyNav]:
        """建账户 + 写入首日 7 条净值（S* + B1..B6），幂等。"""
        acc = self.s.get(Account, self.account_id)
        if acc is None:
            acc = Account(account_id=self.account_id, cash=cash, total_equity=cash, mode="PAPER")
            self.s.add(acc)
            self.s.flush()

        rows: list[DailyNav] = []
        ids = [STRATEGY_ID] + BENCHMARK_IDS
        for sid in ids:
            nav = cash
            existing = self._get_nav(asof, sid)
            if existing is not None:
                rows.append(existing)
                continue
            row = DailyNav(
                trade_date=asof,
                strategy_id=sid,
                account_id=self.account_id,
                nav=nav,
                running_days=1,
                paired_trade_count=0,
            )
            self.s.add(row)
            rows.append(row)
        self._log("ledger_init_day_one", f"{asof}:{self.account_id}", {"cash": cash})
        self.s.flush()
        return rows

    # ---------- 每日净值 ----------
    def write_daily_nav(self, trade_date: date, strategy_navs: dict[str, float] | None = None
                        ) -> list[DailyNav]:
        """写当日 7 条净值，幂等。strategy_navs 提供则更新对应行 nav。"""
        rows: list[DailyNav] = []
        ids = [STRATEGY_ID] + BENCHMARK_IDS
        n = self.running_days(asof=trade_date)
        m = self.paired_trade_count()
        for sid in ids:
            row = self._get_nav(trade_date, sid)
            if row is None:
                row = DailyNav(
                    trade_date=trade_date,
                    strategy_id=sid,
                    account_id=self.account_id,
                    nav=0.0,
                    running_days=n,
                    paired_trade_count=m,
                )
                self.s.add(row)
            row.running_days = n
            row.paired_trade_count = m
            if strategy_navs and sid in strategy_navs:
                row.nav = float(strategy_navs[sid])
            rows.append(row)
        self._log("ledger_daily_nav", f"{trade_date}:{self.account_id}", {"n": n, "m": m})
        self.s.flush()
        return rows

    def upsert_nav(self, trade_date: date, strategy_id: str, nav: float) -> DailyNav:
        row = self._get_nav(trade_date, strategy_id)
        if row is None:
            row = DailyNav(
                trade_date=trade_date,
                strategy_id=strategy_id,
                account_id=self.account_id,
                nav=nav,
                running_days=self.running_days(asof=trade_date),
                paired_trade_count=self.paired_trade_count(),
            )
            self.s.add(row)
        else:
            row.nav = float(nav)
        self.s.flush()
        return row

    # ---------- B6 配对样本 ----------
    def record_signal_pair(self, trade_date: date, symbol: str, real_ret: float,
                           random_ret: float) -> SignalPair:
        ch = self.cfg.config_hash()
        row = (
            self.s.query(SignalPair)
            .filter_by(trade_date=trade_date, symbol=symbol, config_hash=ch)
            .one_or_none()
        )
        if row is not None:
            row.real_ret = float(real_ret)
            row.random_ret = float(random_ret)
            return row
        row = SignalPair(
            trade_date=trade_date,
            symbol=symbol,
            real_ret=float(real_ret),
            random_ret=float(random_ret),
            config_hash=ch,
        )
        self.s.add(row)
        self.s.flush()
        return row

    # ---------- 查询 ----------
    def running_days(self, asof: date | None = None) -> int:
        q = self.s.query(DailyNav).filter_by(
            strategy_id=STRATEGY_ID, account_id=self.account_id
        )
        if asof is not None:
            q = q.filter(DailyNav.trade_date <= asof)
        return q.count()

    def paired_trade_count(self) -> int:
        return self.s.query(SignalPair).count()

    def nav_series(self, strategy_id: str = STRATEGY_ID) -> list[tuple[date, float]]:
        rows = (
            self.s.query(DailyNav)
            .filter_by(strategy_id=strategy_id, account_id=self.account_id)
            .order_by(DailyNav.trade_date.asc())
            .all()
        )
        return [(r.trade_date, float(r.nav)) for r in rows]

    # ---------- 内部 ----------
    def _get_nav(self, trade_date: date, strategy_id: str) -> DailyNav | None:
        return (
            self.s.query(DailyNav)
            .filter_by(
                trade_date=trade_date,
                strategy_id=strategy_id,
                account_id=self.account_id,
            )
            .one_or_none()
        )

    def _log(self, task: str, key: str, payload: dict) -> None:
        exists = (
            self.s.query(ExecutionLog)
            .filter_by(task=task, idempotency_key=key)
            .one_or_none()
        )
        if exists is not None:
            return
        self.s.add(
            ExecutionLog(task=task, idempotency_key=key, status="OK", payload=payload)
        )


def new_id() -> str:
    return uuid.uuid4().hex
