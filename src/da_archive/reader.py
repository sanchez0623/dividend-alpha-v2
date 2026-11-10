"""只读归档读取器（★ 变更 C3）。

只读 web/data.json，不提供任何写方法；snapshot() 全部字段带 data_class="归档"。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ARCHIVE_CLASS = "归档"


class ArchiveReadOnlyError(Exception):
    pass


@dataclass
class ReportMeta:
    name: str
    topic: str
    status: str


@dataclass
class ArchiveSnapshot:
    months: list[str]
    s: list[float]
    b2: list[float]
    hs300: list[float]
    yearly: list[dict]
    kpi: dict[str, Any]
    modules: list[dict]
    withdraw: list[dict]
    data_class: str = ARCHIVE_CLASS

    def kpi_classed(self) -> dict[str, dict]:
        return {k: {"value": v, "data_class": ARCHIVE_CLASS} for k, v in self.kpi.items()}


class ArchiveReader:
    """只读 web/data.json；不提供任何写方法。"""

    def __init__(self, path: str | None = None):
        default = os.environ.get("DA_ARCHIVE_JSON")
        if not default:
            # 生产挂载 ./web/data.json；仓库内回退到 handoff 归档
            local = Path("./web/data.json")
            handoff = Path(__file__).resolve().parents[2] / "dividend-alpha-v3-handoff" / "web" / "data.json"
            default = str(local if local.exists() else handoff)
        self.path = Path(path or default)

    def _load(self) -> dict:
        if not self.path.exists():
            raise FileNotFoundError(f"归档数据不存在: {self.path}")
        with open(self.path, "r", encoding="utf-8") as f:
            return json.load(f)

    def snapshot(self) -> ArchiveSnapshot:
        d = self._load()
        return ArchiveSnapshot(
            months=d.get("months", []),
            s=[float(x) for x in d.get("s", [])],
            b2=[float(x) for x in d.get("b2", [])],
            hs300=[float(x) for x in d.get("hs300", [])],
            yearly=d.get("yearly", []),
            kpi=d.get("kpi", {}),
            modules=d.get("modules", []),
            withdraw=d.get("withdraw", []),
            data_class=ARCHIVE_CLASS,
        )

    def report_index(self) -> list[ReportMeta]:
        d = self._load()
        out: list[ReportMeta] = []
        for r in d.get("reports", []):
            out.append(ReportMeta(name=r.get("name", ""), topic=r.get("topic", ""),
                                  status=r.get("status", "归档")))
        return out

    # 显式禁止写：任何写方法调用即抛错（供 test_archive_readonly 断言）
    def __setattr__(self, key: str, value: Any) -> None:
        if key in ("path",) and key in self.__dict__:
            raise ArchiveReadOnlyError("ArchiveReader 只读，禁止修改")
        object.__setattr__(self, key, value)

    def save(self, *_a, **_k):
        raise ArchiveReadOnlyError("ArchiveReader 只读，禁止写 web/data.json")

    update = save
    delete = save
