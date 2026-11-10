"""信号插件注册表。"""
from __future__ import annotations

from .base import SignalContext, SignalPlugin
from .core_selection import CoreSelectionPlugin
from .overlay_rsi import OverlayRSIPlugin
from .sell_rules import SellRulesPlugin


def default_plugins() -> list[SignalPlugin]:
    return [SellRulesPlugin(), OverlayRSIPlugin(), CoreSelectionPlugin()]


def run_plugins(ctx: SignalContext, plugins: list[SignalPlugin] | None = None):
    plugins = plugins or default_plugins()
    out = []
    for p in plugins:
        out.extend(p.generate(ctx))
    return out
