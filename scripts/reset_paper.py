"""清空模拟盘交易状态，重新来过（保留行情/除权/因子/标的信息等数据表）。

清除：account / position / lot / order / fills / dividend_event /
      withdraw_log / daily_nav / signal_log / factor_daily /
      universe_cache / funnel_audit（★ 派生缓存一并清空，否则重放会复用旧漏斗结果）
保留：price_daily / corp_action / symbol_status / ingest_log（数据资产）
用法：python scripts/reset_paper.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import text  # noqa: E402

from src.da_core.db import make_engine  # noqa: E402

TABLES = ["account", "position", "lot", "orders", "fills", "dividend_event",
          "withdraw_log", "daily_nav", "signal_log", "factor_daily",
          "universe_cache", "funnel_audit"]


def main() -> int:
    engine = make_engine(os.environ.get("POSTGRES_DSN", "sqlite:///./data/dividend_alpha.db"))
    with engine.begin() as conn:
        for t in TABLES:
            conn.execute(text(f'DELETE FROM "{t}"'))  # ★ order 是保留字，统一加引号
            print(f"  cleared {t}")
    print("模拟盘已清空（行情/除权/标的信息保留）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
