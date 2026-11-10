"""把 DC 回测归档 JSON 补上前端契约别名（幂等）。

背景：研究侧 web/dc_backtest.json 使用 ann_vol/mdd/avg_trade_return/costs/final_equity/
label 等字段；前端 DcBacktestMetrics/DcBacktestVariant 使用 vol/max_drawdown/
avg_cycle_return/cost/final_nav/name。本脚本只做**字段别名补齐**，不改数值，不重算结论。

用法：
  python scripts/build_dc_backtest_archive.py                 # 读默认 web/dc_backtest.json 并原地补别名
  python scripts/build_dc_backtest_archive.py <src> <out>      # 指定输入/输出
"""
from __future__ import annotations

import json
import os
import sys


def main() -> int:
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    src = sys.argv[1] if len(sys.argv) > 1 else os.path.join(root, "web", "dc_backtest.json")
    out = sys.argv[2] if len(sys.argv) > 2 else src
    with open(src, "r", encoding="utf-8") as f:
        d = json.load(f)

    m = d.get("metrics") or {}
    aliases = {
        "vol": "ann_vol",
        "max_drawdown": "mdd",
        "avg_cycle_return": "avg_trade_return",
        "cost": "costs",
        "final_nav": "final_equity",
        "total_wealth_return": "total_return",
    }
    for dst, src_key in aliases.items():
        if m.get(dst) is None and m.get(src_key) is not None:
            m[dst] = m[src_key]
    d["metrics"] = m

    for y in d.get("yearly") or []:
        if y.get("nav") is None and y.get("equity_end") is not None:
            y["nav"] = y["equity_end"]
        if y.get("wealth") is None and y.get("wealth_end") is not None:
            y["wealth"] = y["wealth_end"]

    for v in d.get("variants") or []:
        if not v.get("name") and v.get("label"):
            v["name"] = v["label"]
        if v.get("max_drawdown") is None and v.get("mdd") is not None:
            v["max_drawdown"] = v["mdd"]

    with open(out, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    print(f"已补别名: {out}  metrics={len(m)} variants={len(d.get('variants') or [])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
