"""数据库会话与初始化。生产 PostgreSQL / 本地 SQLite 双支持。"""
from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base


def database_dsn() -> str:
    dsn = os.environ.get("POSTGRES_DSN", "sqlite:///./data/dividend_alpha.db")
    # 允许纯 postgres:// 简写
    if dsn.startswith("postgres://"):
        dsn = dsn.replace("postgres://", "postgresql+psycopg://", 1)
    elif dsn.startswith("postgresql://"):
        dsn = dsn.replace("postgresql://", "postgresql+psycopg://", 1)
    return dsn


def make_engine(dsn: str | None = None) -> Engine:
    dsn = dsn or database_dsn()
    if dsn.startswith("sqlite"):
        Path("./data").mkdir(parents=True, exist_ok=True)
        engine = create_engine(dsn, connect_args={"check_same_thread": False})
    else:
        engine = create_engine(dsn, pool_pre_ping=True)
    return engine


def init_db(engine: Engine) -> None:
    """建全部表（幂等）。TimescaleDB hypertable 由 infra/migrations.sql 处理。"""
    if engine.dialect.name == "sqlite":
        # SQLite 默认外键/并发足够测试使用
        @event.listens_for(engine, "connect")
        def _fk(dbapi_conn, _):  # pragma: no cover
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    Base.metadata.create_all(engine)
    _migrate_columns(engine)


# 轻量列迁移：create_all 不给已有表加列，这里按模型演进补齐（幂等）。
_COLUMN_MIGRATIONS: list[tuple[str, str, str]] = [
    # (table, column, ddl type)
    ("symbol_status", "name", "VARCHAR(64)"),
    ("fills", "transfer_fee", "NUMERIC"),
    ("fills", "handling_fee", "NUMERIC"),
    ("fills", "reg_fee", "NUMERIC"),
]


def _migrate_columns(engine: Engine) -> None:
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    for table, column, ddl in _COLUMN_MIGRATIONS:
        if table not in insp.get_table_names():
            continue
        cols = [c["name"] for c in insp.get_columns(table)]
        if column in cols:
            continue
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))


def make_session(engine: Engine) -> Session:
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    return SessionLocal()
