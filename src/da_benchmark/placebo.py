"""B6 配对安慰剂记录。每笔做T成交写 1 行 signal_pair（真实 + 同日随机对照）。"""
from __future__ import annotations

import random
from datetime import date

from sqlalchemy.orm import Session

from ..da_portfolio.ledger import Ledger


class Placebo:
    def __init__(self, session: Session, ledger: Ledger):
        self.s = session
        self.ledger = ledger

    def record_trade(self, trade_date: date, symbol: str, real_ret: float,
                     universe_returns: list[float], rng: random.Random | None = None) -> None:
        rng = rng or random.Random()
        random_ret = float(rng.choice(universe_returns)) if universe_returns else 0.0
        self.ledger.record_signal_pair(trade_date, symbol, real_ret, random_ret)
