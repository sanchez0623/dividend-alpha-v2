"""★ C3 归档只读：无写方法、字段 data_class=归档、写 data.json 被拒。"""
from __future__ import annotations

import json
import os

import pytest

from src.da_archive.reader import ARCHIVE_CLASS, ArchiveReadOnlyError, ArchiveReader

_HANDOFF_JSON = os.path.join(
    os.path.dirname(__file__), "..", "dividend-alpha-v3-handoff", "web", "data.json"
)


def test_no_write_methods():
    r = ArchiveReader()
    assert not hasattr(r, "save") or _raises(r.save)
    assert _raises(getattr(r, "update"))
    assert _raises(getattr(r, "delete"))


def _raises(fn):
    try:
        fn()
        return False
    except ArchiveReadOnlyError:
        return True


def test_snapshot_all_archived():
    r = ArchiveReader(_HANDOFF_JSON)
    snap = r.snapshot()
    assert snap.data_class == ARCHIVE_CLASS
    assert snap.kpi["ann"] == 10.61
    for _k, v in snap.kpi_classed().items():
        assert v["data_class"] == ARCHIVE_CLASS


def test_write_data_json_rejected(tmp_path, monkeypatch):
    p = tmp_path / "data.json"
    p.write_text(json.dumps({"kpi": {"ann": 10.61}}), encoding="utf-8")
    r = ArchiveReader(str(p))
    snap = r.snapshot()
    assert snap.kpi["ann"] == 10.61
    # 归档读取器不提供任何写路径
    assert _raises(lambda: r.save({"kpi": {}}))
    with pytest.raises(ArchiveReadOnlyError):
        r.path = p  # 禁止改指向
