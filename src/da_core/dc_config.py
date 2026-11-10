"""分红捕获策略（DC）参数加载器。

★ 与 S* 隔离：DC 参数在 config/strategy_dividend_capture.yaml，独立于
config/strategy.yaml。这样新增 DC 不会改变 S* 的 config_hash（signal_pair /
factor_daily 的幂等键），不会扰动既有前瞻账本。
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml


def _config_dir() -> Path:
    return Path(os.environ.get("DA_CONFIG_DIR", "./config")).resolve()


@dataclass(frozen=True)
class DcStrategy:
    """只读 DC 参数容器。raw 保留原始 dict 供 API/配置页展示。"""

    raw: dict[str, Any]

    @property
    def meta(self) -> dict[str, Any]:
        return self.raw["meta"]

    @property
    def universe(self) -> dict[str, Any]:
        return self.raw["universe"]

    @property
    def schedule(self) -> dict[str, Any]:
        return self.raw["schedule"]

    @property
    def trend(self) -> dict[str, Any]:
        return self.raw["trend"]

    @property
    def dividend(self) -> dict[str, Any]:
        return self.raw["dividend"]

    @property
    def execution(self) -> dict[str, Any]:
        return self.raw["execution"]

    @property
    def strategy_id(self) -> str:
        return str(self.meta["strategy_id"])

    @property
    def account_id(self) -> str:
        return str(self.meta["account_id"])

    @property
    def start_date(self) -> str:
        return str(self.meta["start_date"])

    @property
    def init_cash(self) -> float:
        return float(self.meta["init_cash"])

    def config_hash(self) -> str:
        blob = json.dumps(self.raw, sort_keys=True, ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()[:16]


@lru_cache(maxsize=1)
def load_dividend_capture(path: str | None = None) -> DcStrategy:
    p = Path(path) if path else _config_dir() / "strategy_dividend_capture.yaml"
    with open(p, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    if not isinstance(raw, dict):
        raise ValueError(f"DC 配置格式错误: {p}")
    return DcStrategy(raw=raw)


def reset_cache() -> None:
    load_dividend_capture.cache_clear()
