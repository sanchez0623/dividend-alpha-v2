"""归档路由（★ C3 只读）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..da_archive.reader import ArchiveReader
from .deps import get_cfg
from .schemas import ArchiveResponse, NumberWithClass

router = APIRouter(prefix="/archive", tags=["archive"])

DISCLAIMER = (
    "以下数字来自 2015-01 ~ 2026-10 历史样本，不是已验证的将来收益；"
    "生产系统自 2026-11-05 起前瞻运行。只读存档 / 不代表前瞻业绩。"
)


@router.get("", response_model=ArchiveResponse)
def archive(cfg=Depends(get_cfg)) -> ArchiveResponse:
    reader = ArchiveReader()
    try:
        snap = reader.snapshot()
    except FileNotFoundError as e:
        raise HTTPException(status_code=503, detail=str(e))
    kpi = {k: NumberWithClass(value=float(v), data_class="归档") for k, v in snap.kpi.items()}
    return ArchiveResponse(
        kpi=kpi,
        months=snap.months,
        s=snap.s,
        b2=snap.b2,
        hs300=snap.hs300,
        yearly=snap.yearly,
        modules=snap.modules,
        withdraw=snap.withdraw,
        data_class="归档",
        disclaimer=DISCLAIMER,
    )
