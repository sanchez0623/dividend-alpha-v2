"""基准路由（★ C2 守门）。样本不足时 mode=annual 返回 409 且响应体无 annual_excess_*。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..da_benchmark.b1_b6 import BENCH_IDS, BENCH_META
from ..da_benchmark.guard import SampleInsufficient, get_benchmark_view
from ..da_core.models import DailyNav
from ..da_portfolio.ledger import STRATEGY_ID
from .deps import get_cfg, get_session
from .schemas import BenchmarksResponse, ProgressItem

router = APIRouter(prefix="/benchmarks", tags=["benchmarks"])


def _progress_items(view) -> list[ProgressItem]:
    items = [
        ProgressItem(label="运行天数", value=float(view.running_days), target=1095.0,
                     fmt=f"{view.running_days} / 1095 天"),
        ProgressItem(label="做T配对样本", value=float(view.paired_trade_count), target=300.0,
                     fmt=f"{view.paired_trade_count} / 300 笔"),
    ]
    return items


@router.get("", response_model=BenchmarksResponse)
def benchmarks(mode: str = "progress", session: Session = Depends(get_session),
               cfg=Depends(get_cfg)) -> BenchmarksResponse:
    from ..da_portfolio.ledger import Ledger

    ledger = Ledger(session, "PAPER", cfg)
    n = ledger.running_days()
    m = ledger.paired_trade_count()
    try:
        view = get_benchmark_view(mode, n, m)
    except SampleInsufficient as e:
        # 409 + 拦截信息；响应体【不含】annual_excess_* 字段
        raise HTTPException(status_code=409, detail={
            "code": "SAMPLE_INSUFFICIENT",
            "message": "样本不足，暂不展示年化超额",
            "need_days": e.need_days,
            "need_trades": e.need_trades,
            "running_days": e.n,
            "paired_trade_count": e.m,
        })
    rows = (
        session.query(DailyNav)
        .filter(DailyNav.account_id == "PAPER", DailyNav.trade_date.isnot(None))
        .all()
    )
    latest: dict[str, float] = {}
    for r in rows:
        if r.strategy_id in BENCH_IDS:
            latest[r.strategy_id] = float(r.nav)
    bench_list = []
    for bid in BENCH_IDS:
        meta = BENCH_META[bid]
        bench_list.append({
            "id": bid,
            "name": meta["name"],
            "data_class": meta["cls"],
            "nav": latest.get(bid),
            "status": "未通过" if bid == "B6" else "运行中",
            "paired": m if bid == "B6" else None,
        })
    return BenchmarksResponse(
        mode=view.mode,
        running_days=n,
        paired_trade_count=m,
        need_days=view.need_days,
        need_trades=view.need_trades,
        annual_unlocked=view.annual_unlocked,
        progress=_progress_items(view),
        benchmarks=bench_list,
    )
