"""独立运行全市场漏斗（诊断/校验用）：打印候选数与清单哈希，不产生订单。

用法：
  python scripts/funnel_run.py [YYYY-MM-DD]      # 默认今天
行为：
  - 该日期首次执行 → 全量扫描（S1 快照 → S2 重试扫描 → S3 批量价格 → S4 行业）
  - 该日期已有成功审计 → 同日幂等复用，直接返回已验证短名单（零上游请求）
  - 需要强制全新扫描：先清空 funnel_audit / universe_cache（或 reset_paper.py）
输出：
  FUNNEL_RESULT: asof=... count=... sha=... elapsed=...s
  [候选清单 JSON]
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import time
from datetime import date

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")

from sqlalchemy.orm import sessionmaker  # noqa: E402

from src.da_core.config import load_strategy  # noqa: E402
from src.da_core.db import init_db, make_engine  # noqa: E402
from src.da_data.fallback import build_chain as build_fb_chain  # noqa: E402
from src.da_data.fallback import FallbackChain  # noqa: E402
from src.da_data.universe import build_candidates  # noqa: E402


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    cfg = load_strategy()
    engine = make_engine(os.environ.get("POSTGRES_DSN", "sqlite:///./data/dividend_alpha.db"))
    init_db(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    chain = FallbackChain(build_fb_chain(cfg.data.get("providers", {})))
    asof = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date.today()

    t0 = time.time()
    syms = build_candidates(session, chain, cfg, asof)
    session.commit()  # ★ 落审计与缓存（漏斗本身只 flush，提交由调用方负责）
    elapsed = time.time() - t0
    if syms is None:
        print("FUNNEL_RESULT: null（熔断/失败，见上方 WARNING 日志与 funnel_audit 表）")
        return 1
    sha = hashlib.sha256(json.dumps(syms).encode()).hexdigest()[:16]
    print(f"FUNNEL_RESULT: asof={asof} count={len(syms)} sha={sha} elapsed={elapsed:.0f}s")
    print(json.dumps(syms, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
