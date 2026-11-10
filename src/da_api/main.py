"""FastAPI 应用入口（make serve）。

★ 接口命名空间：所有后端路由挂载在 DA_API_PREFIX（默认 /da/v1）下，
避免与同机其他服务 / 其它应用的 /api 冲突。前端与 nginx 代理须使用同一前缀。
"""
from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .. import __version__
from .routers_app import router as app_router
from .routers_archive import router as archive_router
from .routers_benchmarks import router as bench_router
from .routers_dc import router as dc_router
from .ws import router as ws_router

# ★ 统一接口前缀（可配）；nginx / 前端 vite proxy 须对齐
API_PREFIX = os.environ.get("DA_API_PREFIX", "/da/v1").rstrip("/")

app = FastAPI(
    title="dividend-alpha v3.0 · 前瞻验证系统",
    version=__version__,
    description="A 股高股息组合 Forward-only 前瞻验证系统",
    docs_url=f"{API_PREFIX}/docs",
    openapi_url=f"{API_PREFIX}/openapi.json",
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("DA_CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get(f"{API_PREFIX}/health")
def health():
    return {"status": "ok", "version": __version__, "mode": "FORWARD_ONLY",
            "prefix": API_PREFIX}


@app.get("/")
def root():
    return {"name": "dividend-alpha", "version": __version__,
            "api_prefix": API_PREFIX, "docs": f"{API_PREFIX}/docs",
            "mode": "前瞻运行 · 起始 2026-11-05"}


# ---- 数据源真实探活（前端"数据源状态灯"真值来源） ----
def _probe_datasources() -> dict:
    """逐个源做廉价连通性探测，返回真实状态。

    connected  = 实测可取到数据
    configured = 已配 key/SDK 但未实测（或本次探测失败）
    missing    = 缺 key / 缺 SDK
    """
    import datetime as dt
    from ..da_data.providers.ths import ThsFuyaoClient
    from ..da_data.providers.symbol_map import to_thscode

    out = []
    # 同花顺 fuyao（主源）：真发一次批量 K 线请求
    key = os.environ.get("THS_API_KEY", "")
    if not key:
        out.append({"name": "ths_fuyao", "role": "主源", "status": "missing"})
    else:
        try:
            cli = ThsFuyaoClient(api_key=key)
            d = cli.prices_historical_batch(
                [to_thscode("sh601398")],
                dt.date.today() - dt.timedelta(days=10), dt.date.today())
            bars = len(d.get("item", [{}])[0].get("item", [])) if d.get("item") else 0
            out.append({"name": "ths_fuyao", "role": "主源",
                        "status": "connected" if bars > 0 else "configured",
                        "bars": bars})
        except Exception as e:  # noqa: BLE001
            out.append({"name": "ths_fuyao", "role": "主源",
                        "status": "failed", "error": type(e).__name__})

    # akshare（备源）：SDK 是否可导入 + 能否拉到日线
    try:
        import akshare as ak  # noqa: F401
        try:
            df = ak.stock_zh_a_hist(symbol="601398", period="daily",
                                    start_date="20261101", end_date="20261106",
                                    adjust="qfq")
            out.append({"name": "akshare", "role": "备源",
                        "status": "connected" if df is not None and len(df) else "configured"})
        except Exception:  # noqa: BLE001
            out.append({"name": "akshare", "role": "备源", "status": "configured"})
    except Exception:  # noqa: BLE001
        out.append({"name": "akshare", "role": "备源", "status": "missing"})

    # tushare / baostock：未装 SDK → missing
    for name, role, mod in [("tushare", "备源", "tushare"),
                            ("baostock", "备源(串行)", "baostock")]:
        try:
            __import__(mod)
            out.append({"name": name, "role": role, "status": "configured"})
        except Exception:  # noqa: BLE001
            out.append({"name": name, "role": role, "status": "missing"})

    return {"sources": out,
            "primary_connected": any(
                s["name"] == "ths_fuyao" and s["status"] == "connected"
                for s in out)}


@app.get(f"{API_PREFIX}/datasources")
def datasources():
    return _probe_datasources()


# 所有业务路由统一挂在 API_PREFIX 下（★ 隔离命名空间）
sub = FastAPI(title="dividend-alpha api", version=__version__)
sub.include_router(app_router)
sub.include_router(bench_router)
sub.include_router(archive_router)
sub.include_router(dc_router)
sub.include_router(ws_router)
app.mount(API_PREFIX, sub)
