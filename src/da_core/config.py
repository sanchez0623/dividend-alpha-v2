"""唯一参数真值加载器。

config/strategy.yaml 是唯一参数真值，代码禁止硬编码任何策略参数。
所有模块必须通过 load_strategy() 读取。
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml


def _config_dir() -> Path:
    return Path(os.environ.get("DA_CONFIG_DIR", "./config")).resolve()


@dataclass(frozen=True)
class Strategy:
    """只读策略参数容器。raw 保留原始 dict，供 P7 配置页展示。"""

    raw: dict[str, Any]

    @property
    def meta(self) -> dict[str, Any]:
        return self.raw["meta"]

    @property
    def universe(self) -> dict[str, Any]:
        return self.raw["universe"]

    @property
    def portfolio(self) -> dict[str, Any]:
        return self.raw["portfolio"]

    @property
    def selection(self) -> dict[str, Any]:
        return self.raw["selection"]

    @property
    def sell_rules(self) -> dict[str, Any]:
        return self.raw["sell_rules"]

    @property
    def rotation_enabled(self) -> bool:
        # 已证伪，永远应为 False。此处强制读取以便测试断言。
        return bool(self.raw.get("rotation", {}).get("enabled", False))

    @property
    def dividend_reinvest(self) -> dict[str, Any]:
        return self.raw["dividend_reinvest"]

    @property
    def dividend_withdraw(self) -> dict[str, Any]:
        return self.raw["dividend_withdraw"]

    @property
    def overlay(self) -> dict[str, Any]:
        return self.raw["overlay"]

    @property
    def cost(self) -> dict[str, Any]:
        return self.raw["cost"]

    @property
    def benchmarks(self) -> dict[str, Any]:
        return self.raw["benchmarks"]

    @property
    def data(self) -> dict[str, Any]:
        return self.raw["data"]

    @property
    def start_date(self) -> str:
        return str(self.meta["start_date"])

    @property
    def init_cash(self) -> float:
        return float(self.meta["init_cash"])

    @property
    def target_positions(self) -> int:
        return int(self.portfolio["target_positions"])

    def config_hash(self) -> str:
        """对 raw 做 canonical 序列化取哈希，用于幂等键与因子落库。"""
        import json

        blob = json.dumps(self.raw, sort_keys=True, ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()[:16]


@lru_cache(maxsize=1)
def load_strategy(path: str | None = None) -> Strategy:
    p = Path(path) if path else _config_dir() / "strategy.yaml"
    with open(p, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return Strategy(raw=raw)


@lru_cache(maxsize=1)
def load_providers(path: str | None = None) -> dict[str, Any]:
    p = Path(path) if path else _config_dir() / "providers.yaml"
    with open(p, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def reset_cache() -> None:
    load_strategy.cache_clear()
    load_providers.cache_clear()
