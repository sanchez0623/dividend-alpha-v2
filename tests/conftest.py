"""测试公共夹具：内存 SQLite + 冻结参数真值。"""
from __future__ import annotations

import os
import sys
from datetime import date

import pytest
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.da_core.config import load_strategy  # noqa: E402
from src.da_core.db import init_db, make_engine  # noqa: E402


@pytest.fixture()
def cfg():
    return load_strategy()


@pytest.fixture()
def session(cfg):
    engine = make_engine("sqlite://")
    init_db(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False)()
    yield s
    s.close()


@pytest.fixture()
def day_one(cfg):
    return date.fromisoformat(cfg.start_date)
