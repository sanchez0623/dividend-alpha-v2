"""分红再投 + 年度取现（幂等）。

年内到账分红 100% 按持仓市值权重再投；12 月最后交易日取当年【税前】分红 30%，
幂等键 (year, account_id)，skip_incomplete_year=true。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..da_core.broker import T1Broker
from ..da_core.config import Strategy
from ..da_core.models import Account, DividendEvent, ExecutionLog, Position, WithdrawLog
from .ledger import Ledger, new_id


class DividendReinvest:
    """年内分红全额再投（按当前市值权重，lot 100，min_cash_ratio 保护）。"""

    def __init__(self, session: Session, account_id: str, cfg: Strategy):
        self.s = session
        self.account_id = account_id
        self.cfg = cfg

    def on_dividend(self, symbol: str, dps_gross: float, ex_date: date) -> float:
        """收到分红 → 计入 ytd；再投由 rebalance 触发。这里只登记并立即按现价再投。"""
        cfg = self.cfg.dividend_reinvest
        if not cfg.get("enabled", True):
            return 0.0
        broker = T1Broker(self.s, self.account_id, self.cfg)
        net = broker.on_dividend(symbol, dps_gross, ex_date)
        return net

    def reinvest(self, price_map: dict[str, float], trade_date: date) -> list[tuple[str, int]]:
        """把可投现金按持仓市值权重再投（仅补仓，不换股）。返回 (symbol, shares)。"""
        cfg = self.cfg.dividend_reinvest
        lot = int(cfg.get("lot_size", 100))
        min_cash_ratio = float(cfg.get("min_cash_ratio", 0.02))
        acc = self.s.get(Account, self.account_id)
        positions = self.s.query(Position).filter_by(account_id=self.account_id).all()
        mvs = {p.symbol: float(p.shares_total) * price_map.get(p.symbol, 0) for p in positions}
        total_mv = sum(mvs.values())
        if total_mv <= 0:
            return []
        cash_avail = float(acc.cash) - min_cash_ratio * (total_mv + float(acc.cash))
        broker = T1Broker(self.s, self.account_id, self.cfg)
        executed: list[tuple[str, int]] = []
        for sym, mv in sorted(mvs.items(), key=lambda kv: -kv[1]):
            price = price_map.get(sym)
            if not price or price <= 0:
                continue
            weight = mv / total_mv
            alloc = cash_avail * weight
            shares = int(alloc / price // lot) * lot
            if shares <= 0:
                continue
            try:
                broker.buy(sym, shares, price, trade_date=trade_date,
                           industry_l1=self._industry(sym))
                executed.append((sym, shares))
            except ValueError:
                continue
        return executed

    def _industry(self, symbol: str) -> str | None:
        p = self.s.query(Position).filter_by(
            account_id=self.account_id, symbol=symbol).one_or_none()
        return p.industry_l1 if p else None


@dataclass
class WithdrawResult:
    year: int
    want: float
    got: float
    rate: float
    status: str
    idempotent: bool = False


class AnnualWithdraw:
    """年度取现：base=当年税前现金分红，ratio=0.30，cash_only，幂等 (year, account_id)。"""

    def __init__(self, session: Session, account_id: str, cfg: Strategy):
        self.s = session
        self.account_id = account_id
        self.cfg = cfg

    def on_dividend(self, amount: float) -> None:
        """分红到账时累计 ytd_gross（税前）。此处仅记账，实际累计从 dividend_event 汇总。"""
        # 保留接口；真实累计见 ytd_gross()
        return None

    def ytd_gross(self, year: int) -> float:
        """当年累计税前现金分红（核心仓 + 机动仓），来自 dividend_event。"""
        total = (
            self.s.query(func.coalesce(func.sum(DividendEvent.dps_gross), 0.0))
            .filter(
                DividendEvent.account_id == self.account_id,
                func.strftime("%Y", DividendEvent.ex_date) == str(year),
            )
            .scalar()
        )
        if total is None:
            # 非 SQLite（Postgres）用 extract
            from sqlalchemy import extract

            total = (
                self.s.query(func.coalesce(func.sum(DividendEvent.dps_gross), 0.0))
                .filter(
                    DividendEvent.account_id == self.account_id,
                    extract("year", DividendEvent.ex_date) == year,
                )
                .scalar()
            )
        return float(total or 0.0)

    def run_year_end(self, year: int, ctx_cash: float | None = None) -> WithdrawResult:
        """12 月最后交易日盘后执行；幂等。skip_incomplete_year 时进行中年份不取。"""
        cfgw = self.cfg.dividend_withdraw
        idem_key = f"{year}:{self.account_id}"
        existing = (
            self.s.query(WithdrawLog)
            .filter_by(year=year, account_id=self.account_id)
            .one_or_none()
        )
        if existing is not None:
            return WithdrawResult(year, float(existing.want), float(existing.got),
                                  float(existing.rate), existing.status, idempotent=True)

        gross = self.ytd_gross(year)
        # skip_incomplete_year：仅当 year == 当前年且尚未到年末最后交易日时不取；
        # 历史完整年份（year < 当前年）与未来已排期年份照常按幂等键执行。
        if cfgw.get("skip_incomplete_year", True):
            today = date.today()
            if year == today.year and today < date(year, 12, 31):
                self._log(idem_key, "SKIPPED_INCOMPLETE", year, 0.0, 0.0, 0.0)
                return WithdrawResult(year, 0.0, 0.0, 0.0, "SKIPPED_INCOMPLETE")

        want = gross * float(cfgw.get("ratio", 0.30))
        acc = self.s.get(Account, self.account_id)
        cash = float(acc.cash) if acc else 0.0
        policy = cfgw.get("shortfall_policy", "cash_only")
        got = want
        if policy == "cash_only":
            got = min(want, cash)
        # sell_to_cover 这里不实现（已证伪仅作占位，cash_only 差 0.04%/年）
        rate = (got / want * 100.0) if want > 0 else 100.0
        status = "OK" if got >= want - 1e-6 else "PARTIAL"

        acc.cash = float(acc.cash) - got
        self.s.add(
            WithdrawLog(
                year=year,
                account_id=self.account_id,
                want=want,
                got=got,
                rate=rate,
                status=status,
            )
        )
        self._log(idem_key, status, year, want, got, rate)
        self.s.flush()
        return WithdrawResult(year, want, got, rate, status)

    def _log(self, idem_key: str, status: str, year: int, want: float, got: float,
             rate: float) -> None:
        self.s.add(
            ExecutionLog(
                task="annual_withdraw",
                idempotency_key=idem_key,
                status=status,
                payload={"year": year, "want": want, "got": got, "rate": rate},
            )
        )
