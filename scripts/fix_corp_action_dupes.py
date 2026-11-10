"""一次性清理 corp_action 时区污染重复行。

背景：UTC/北京两套解析并存时期，同一除权事件被写入相邻两日各一行
（主键 (symbol, ex_date) 不同即共存），导致 TTM 分红合计翻倍。
规则：同 symbol + 同 dps，ex_date 相邻（差 1 天）的成对行 → 保留最早一行删另一行。
"""
from __future__ import annotations

import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy.orm import sessionmaker  # noqa: E402

from src.da_core.db import init_db, make_engine  # noqa: E402
from src.da_core.models import CorpAction  # noqa: E402


def main() -> int:
    engine = make_engine(os.environ.get("POSTGRES_DSN", "sqlite:///./data/dividend_alpha.db"))
    init_db(engine)
    s = sessionmaker(bind=engine)()
    removed = 0
    symbols = [r[0] for r in s.query(CorpAction.symbol).distinct().all()]
    for sym in symbols:
        rows = (s.query(CorpAction).filter_by(symbol=sym)
                .order_by(CorpAction.ex_date).all())
        # 按 (dps, bonus) 分组，组内相邻日去重
        groups: dict[tuple, list[CorpAction]] = {}
        for r in rows:
            key = (round(float(r.dps or 0), 6), round(float(r.bonus_ratio or 0), 6))
            groups.setdefault(key, []).append(r)
        for key, grp in groups.items():
            if len(grp) < 2:
                continue
            grp.sort(key=lambda r: r.ex_date)
            keep = grp[0]
            for r in grp[1:]:
                if (r.ex_date - keep.ex_date).days <= 1:
                    s.delete(r)
                    removed += 1
                    print(f"  删 {sym} {r.ex_date} dps={key[0]}（与 {keep.ex_date} 重复）")
                else:
                    keep = r
    s.commit()
    print(f"清理完成：删除 {removed} 行重复除权事件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
