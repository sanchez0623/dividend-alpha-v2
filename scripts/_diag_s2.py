"""S2 失败类型实证诊断：扫描 S1 全池，逐只调 fuyao，分类记录所有异常。

只读诊断：不写任何 DB 表。输出按错误类型分桶 + 失败清单。
"""
from __future__ import annotations

import json
import os
import sys
import time
from collections import Counter
from datetime import date, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import httpx  # noqa: E402

from src.da_data.providers.base import BizError, RateLimited  # noqa: E402
from src.da_data.providers.eastmoney import fetch_all_a_spot  # noqa: E402
from src.da_data.providers.symbol_map import to_thscode  # noqa: E402
from src.da_data.providers.ths import ThsFuyaoClient  # noqa: E402

ASOF = date(2026, 11, 5)
MIN_CAP = 3.0e10

spot = fetch_all_a_spot()
spot["market_cap"] = __import__("pandas").to_numeric(spot["market_cap"], errors="coerce")
spot["last"] = __import__("pandas").to_numeric(spot["last"], errors="coerce")
spot = spot.dropna(subset=["market_cap", "last"])
spot = spot[spot["market_cap"] >= MIN_CAP]
spot = spot[~spot["name"].fillna("").str.contains("ST|退", na=False)]
cands = sorted(spot["symbol"])
print(f"S1 池: {len(cands)} 只，开始逐只扫描 fuyao adjustment_factors ...", flush=True)

key = os.environ.get("THS_API_KEY", "")
client = ThsFuyaoClient(api_key=key)

failures: list[dict] = []
buckets: Counter = Counter()
t0 = time.time()
for i, sym in enumerate(cands, 1):
    ths = to_thscode(sym)
    try:
        client.adjustment_factors(ths, ASOF - timedelta(days=2500))
        buckets["成功"] += 1
    except RateLimited as e:
        buckets[f"限流(429/4001)"] += 1
        failures.append({"symbol": sym, "type": "rate_limited", "detail": str(e)})
    except BizError as e:
        buckets[f"业务错误 code={e.code}"] += 1
        failures.append({"symbol": sym, "type": "biz_error", "code": getattr(e, "code", None),
                         "detail": str(e)[:120]})
    except httpx.HTTPError as e:
        buckets[f"网络错误({type(e).__name__})"] += 1
        failures.append({"symbol": sym, "type": "network", "detail": str(e)[:120]})
    except Exception as e:  # noqa: BLE001
        buckets[f"其他({type(e).__name__})"] += 1
        failures.append({"symbol": sym, "type": "other", "detail": str(e)[:120]})
    if i % 100 == 0:
        el = time.time() - t0
        print(f"  进度 {i}/{len(cands)}  已用时 {el:.0f}s  失败 {sum(1 for _ in failures)}", flush=True)

el = time.time() - t0
print(f"\n===== 诊断结果（asof={ASOF}, 用时 {el:.0f}s, {len(cands)} 只）=====")
for k, v in buckets.most_common():
    print(f"  {k}: {v}")
print(f"\n失败清单（前 50 条）:")
for f in failures[:50]:
    print(" ", json.dumps(f, ensure_ascii=False))
with open(os.path.join("data", "_diag_s2_failures.json"), "w", encoding="utf-8") as fp:
    json.dump({"asof": ASOF.isoformat(), "scanned": len(cands), "buckets": dict(buckets),
               "failures": failures}, fp, ensure_ascii=False, indent=1)
print("\n明细已写 data/_diag_s2_failures.json")
