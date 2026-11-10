"""DC API：router 级 TestClient，覆盖 overview/cycles/nav/config/backtest。"""
from __future__ import annotations

import json
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.da_api.deps import get_cfg, get_dc_cfg, get_session
from src.da_api.routers_dc import router
from src.da_core.dc_config import load_dividend_capture
from src.da_core.models import (
    Account,
    DailyNav,
    DcCycle,
    DividendEvent,
    PriceDaily,
)


@pytest.fixture()
def client(session, cfg):
    dc = load_dividend_capture()
    # 账户 + 两条 DC 净值（用于 daily_pnl）
    session.add(Account(account_id="DC", cash=3_100_000, total_equity=3_100_000,
                        mode="PAPER"))
    session.add(DailyNav(trade_date=date(2026, 11, 6), strategy_id="DC",
                         account_id="DC", nav=3_000_000, running_days=1))
    session.add(DailyNav(trade_date=date(2026, 11, 7), strategy_id="DC",
                         account_id="DC", nav=3_100_000, running_days=2))
    session.add(DailyNav(trade_date=date(2026, 11, 7), strategy_id="S*",
                         account_id="PAPER", nav=3_050_000, running_days=2))
    session.add(DailyNav(trade_date=date(2026, 11, 7), strategy_id="B2",
                         account_id="PAPER", nav=3_000_000, running_days=2))
    # 已平仓周期 + 持仓周期 + 分红
    session.add(DcCycle(
        cycle_id="c1", account_id="DC", symbol="sh600028", name="中国石化",
        ex_date=date(2026, 7, 2), entry_date=date(2026, 6, 25),
        exit_date=date(2026, 8, 3), shares=1000, buy_price=10, sell_price=11,
        dps=0.8, gross_dividend=800, tax=80, withdrawn=216, net_dividend=720,
        net_pnl=1394, return_pct=0.1394, status="CLOSED"))
    session.add(DcCycle(
        cycle_id="c2", account_id="DC", symbol="sh601398", name="工商银行",
        ex_date=date(2026, 12, 1), entry_date=date(2026, 11, 24),
        planned_exit_date=date(2027, 1, 1), shares=1000, buy_price=7,
        dps=0.3, gross_dividend=0, status="HOLDING"))
    session.add(DividendEvent(event_id="e1", account_id="DC", symbol="sh600028",
                              ex_date=date(2026, 7, 2), dps_gross=800,
                              tax_withheld=80, net_cash=720))
    session.add(PriceDaily(symbol="sh601398", trade_date=date(2026, 11, 7),
                           raw_close=7.2, adj_factor=1.0, adj_close=7.2))
    session.flush()

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_cfg] = lambda: cfg
    app.dependency_overrides[get_dc_cfg] = lambda: dc
    with TestClient(app) as c:
        yield c


def test_overview(client):
    r = client.get("/dc/overview")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["strategy_id"] == "DC"
    assert d["data_date"] == "2026-11-07"
    assert d["daily_pnl"]["value"] == pytest.approx(100_000)
    assert d["dividend_gross"]["value"] == pytest.approx(800)
    assert d["dividend_net"]["value"] == pytest.approx(720)
    assert d["withdrawn"]["value"] == pytest.approx(216)
    assert d["captures"]["value"] == pytest.approx(1)
    assert d["holding"]["symbol"] == "sh601398"
    assert d["holding"]["market_value"] == pytest.approx(7200)
    assert d["next_action"]["kind"] == "SELL"


def test_cycles(client):
    r = client.get("/dc/cycles")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 2
    closed = next(x for x in rows if x["status"] == "CLOSED")
    assert closed["symbol"] == "sh600028"
    assert closed["tax"] == pytest.approx(80)
    assert closed["withdrawn"] == pytest.approx(216)


def test_nav(client):
    r = client.get("/dc/nav")
    assert r.status_code == 200
    d = r.json()
    assert d["dates"] == ["2026-11-06", "2026-11-07"]
    assert d["dc"] == [3_000_000, 3_100_000]
    assert d["s"] == [None, 3_050_000]
    assert d["b2"] == [None, 3_000_000]


def test_config(client):
    r = client.get("/dc/config")
    assert r.status_code == 200
    d = r.json()
    assert d["strategy_id"] == "DC"
    assert "min_div_yield_ttm" in d["strategy_yaml"]


def test_candidates_empty_without_universe(client):
    r = client.get("/dc/candidates")
    assert r.status_code == 200
    assert r.json() == []


def test_backtest_missing_then_present(client, tmp_path, monkeypatch):
    p = tmp_path / "dc_backtest.json"
    monkeypatch.setenv("DA_DC_BACKTEST_JSON", str(p))
    r = client.get("/dc/backtest")
    assert r.status_code == 200
    assert r.json()["available"] is False

    p.write_text(json.dumps({
        "available": True, "as_of": "2026-11-06",
        "universe": {"kind": "full_market", "filter": "cap>=5e10", "symbols": 344},
        "start": "2016-01-04", "end": "2026-11-06",
        "metrics": {"cagr": -0.05, "captures": 39},
        "yearly": [{"year": 2026, "return": -0.1, "nav": 1.0}],
        "variants": [{"name": "base", "cagr": -0.05, "max_drawdown": -0.2}],
        "caveats": ["幸存者偏差"],
    }, ensure_ascii=False), encoding="utf-8")
    d = client.get("/dc/backtest").json()
    assert d["available"] is True
    assert d["data_class"] == "归档"
    assert d["metrics"]["cagr"] == pytest.approx(-0.05)
    assert isinstance(d["universe"], str) and "344" in d["universe"]
    assert d["caveats"] == ["幸存者偏差"]
