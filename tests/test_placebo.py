"""B6 安慰剂配对：每笔做T成交写 1 行 signal_pair（真实 + 同日随机）。"""
from __future__ import annotations

import random
from datetime import date

import pytest

from src.da_benchmark.placebo import Placebo
from src.da_portfolio.ledger import Ledger
from src.da_core.models import SignalPair


def test_pair_written_per_trade(session, cfg, day_one):
    ledger = Ledger(session, "PAPER", cfg)
    ledger.init_day_one(day_one, cfg.init_cash)
    p = Placebo(session, ledger)
    rng = random.Random(7)
    universe = [0.01, -0.02, 0.015, -0.005]
    p.record_trade(day_one, "sh601398", 0.012, universe, rng)
    p.record_trade(day_one, "sh600036", -0.008, universe, rng)
    rows = session.query(SignalPair).filter_by(trade_date=day_one).all()
    assert len(rows) == 2
    for r in rows:
        assert r.real_ret is not None and r.random_ret is not None
        assert r.config_hash == cfg.config_hash()
    assert ledger.paired_trade_count() == 2


def test_pair_idempotent(session, cfg, day_one):
    ledger = Ledger(session, "PAPER", cfg)
    p = Placebo(session, ledger)
    p.record_trade(day_one, "sh601398", 0.01, [0.02], random.Random(1))
    p.record_trade(day_one, "sh601398", 0.03, [0.02], random.Random(1))
    rows = session.query(SignalPair).filter_by(trade_date=day_one, symbol="sh601398").all()
    assert len(rows) == 1
    assert float(rows[0].real_ret) == pytest.approx(0.03)
