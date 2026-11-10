"""共享依赖：会话、策略、账本。"""
from __future__ import annotations

import functools
from datetime import date

from fastapi import Depends
from sqlalchemy.orm import Session

from ..da_core.config import Strategy, load_strategy
from ..da_core.db import init_db, make_engine
from ..da_core.dc_config import DcStrategy, load_dividend_capture
from ..da_portfolio.ledger import Ledger

_ENGINE = None
_PAPER_ACCOUNT = "PAPER"


def get_engine():
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = make_engine()
        init_db(_ENGINE)
    return _ENGINE


def get_session():
    from sqlalchemy.orm import sessionmaker

    engine = get_engine()
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def get_cfg() -> Strategy:
    return load_strategy()


def get_dc_cfg() -> DcStrategy:
    return load_dividend_capture()


def get_ledger(session: Session = Depends(get_session)) -> Ledger:
    return Ledger(session, _PAPER_ACCOUNT, load_strategy())


def paper_account() -> str:
    return _PAPER_ACCOUNT
