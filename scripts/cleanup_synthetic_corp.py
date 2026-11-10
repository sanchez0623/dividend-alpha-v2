"""清理本地 corp_action 中的合成源演示分红（07-15 固定模式），并从 fuyao 补回真实事件。

背景：早期离线冒烟时 fallback 链走到 SyntheticSource，把 10 只演示标的的
演示分红写进了 corp_action（如中石化每年 07-15 dps=0.986），虚增股息率 TTM。
ingest 本身幂等（无重复行），此脚本一次性清除历史污染并重建真实事件。

用法：python scripts/_cleanup_synthetic_corp.py            # 清理 + 重建 + 重算复权
      python scripts/_cleanup_synthetic_corp.py --check    # 仅统计污染行
"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from src.da_core.config import load_strategy  # noqa: E402
from src.da_core.db import init_db, make_engine  # noqa: E402
from src.da_data.adjust import CorpAction  # noqa: E402
from src.da_data.ingest import ingest_corp_action, recompute_adj_close  # noqa: E402
from src.da_data.providers.synthetic import DEMO_UNIVERSE  # noqa: E402
from src.da_data.providers.ths import ThsFuyaoClient  # noqa: E402

DEMO_SYMS = [m["symbol"] for m in DEMO_UNIVERSE]
SYN_PATTERN = {"sh601398": 0.275, "sh600036": 0.336, "sh601288": 0.468,
               "sh601988": 0.55, "sh601088": 0.78, "sh600900": 0.48,
               "sh600028": 0.986, "sh601857": 0.855, "sz000651": 1.092,
               "sh600585": 0.92}


def main() -> int:
    check_only = "--check" in sys.argv
    engine = make_engine(os.environ.get("POSTGRES_DSN", "sqlite:///./data/dividend_alpha.db"))
    init_db(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    conn = session.connection()

    poisoned = 0
    for sym, dps in SYN_PATTERN.items():
        n = conn.execute(
            text("select count(*) from corp_action where symbol=:sym "
                 "and strftime('%m-%d', ex_date)='07-15' and abs(dps-:dps)<1e-6"),
            {"sym": sym, "dps": dps}).scalar()
        if n:
            print(f"  {sym}: {n} 条合成模式行 (07-15, dps={dps})")
            poisoned += n
    print(f"合成模式污染行合计: {poisoned}")
    if check_only:
        return 0
    if not poisoned:
        print("无需清理")
        return 0

    cfg = load_strategy()
    client = ThsFuyaoClient(api_key=os.environ.get("THS_API_KEY", ""))
    today = date.today()
    for sym in DEMO_SYMS:
        before = conn.execute(text("select count(*) from corp_action where symbol=:s"),
                              {"s": sym}).scalar()
        conn.execute(text("delete from corp_action where symbol=:s"), {"s": sym})
        rows = client.adjustment_factors(
            sym.replace("sz", "").replace("sh", "") + ("." + sym[:2].upper()),
            today - timedelta(days=2500))
        events: list[CorpAction] = ThsFuyaoClient.to_corp_actions(sym, rows)
        ingest_corp_action(session, events)
        recompute_adj_close(session, sym, window_days=180)
        after = conn.execute(text("select count(*) from corp_action where symbol=:s"),
                             {"s": sym}).scalar()
        print(f"  {sym}: {before} → {after} 行（真实事件已重建）")
    session.commit()
    print("清理完成；本地因子将在下次盘后闭环时按真实事件重算")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
