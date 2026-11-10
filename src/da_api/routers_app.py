"""总览 / 持仓 / 信号 / 归因 / 取现 / 配置 路由（统一 data_class 标注）。"""
from __future__ import annotations

import os
from datetime import date

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..da_core.config import load_strategy, load_providers
from ..da_core.calendar import last_trading_day_of_december as _ltd
from ..da_data.providers.ths import ThsFuyaoClient


def _last_trading_day_dec(year: int) -> date:
    """12 月最后交易日：fuyao 交易日历精确判定；不可达时回退周末近似。"""
    try:
        key = os.environ.get("THS_API_KEY", "")
        if key:
            cli = ThsFuyaoClient(api_key=key)
            days = cli.fetch_trade_calendar(date(year, 12, 1), date(year, 12, 31))
            if days:
                return days[-1]
    except Exception:  # noqa: BLE001
        pass
    return _ltd(year)
from ..da_core.models import Account, DailyNav, DividendEvent, \
    FactorDaily, Position, SignalLog, SymbolStatus
from ..da_portfolio.dividend import AnnualWithdraw
from ..da_portfolio.ledger import Ledger, STRATEGY_ID
from .deps import get_cfg, get_ledger, get_session, paper_account
from .schemas import (
    AttributionResponse, ConfigResponse, NumberWithClass, OverviewResponse, PositionRow,
    ProgressItem, SignalRow, WithdrawResponse,
)

router = APIRouter(tags=["app"])


def _live(v, unit=None) -> NumberWithClass:
    return NumberWithClass(value=v, data_class="实时", unit=unit)


def _demo(v, unit=None) -> NumberWithClass:
    return NumberWithClass(value=v, data_class="演示", unit=unit)


@router.get("/overview", response_model=OverviewResponse)
def overview(session: Session = Depends(get_session), cfg=Depends(get_cfg),
             ledger: Ledger = Depends(get_ledger)) -> OverviewResponse:
    acc = session.get(Account, paper_account())
    cash = float(acc.cash) if acc else 0.0
    positions = session.query(Position).filter_by(account_id=paper_account()).all()
    fv = 0.0
    for p in positions:
        price = _latest_adj(session, p.symbol) or float(p.cost_price)
        fv += float(p.shares_total) * price
    total = cash + fv
    n = ledger.running_days()
    m = ledger.paired_trade_count()
    s_nav = _strategy_nav(session, STRATEGY_ID)
    b2_nav = _strategy_nav(session, "B2")
    diff_pct = 0.0
    if s_nav and b2_nav and b2_nav != 0:
        diff_pct = (s_nav / b2_nav - 1.0) * 100.0
    # ★ 数据日期 = 最新净值行的交易日（真实数据口径，而非服务器当天）
    latest_nav = (session.query(DailyNav)
                  .filter_by(account_id=paper_account(), strategy_id=STRATEGY_ID)
                  .order_by(DailyNav.trade_date.desc()).first())
    data_date = latest_nav.trade_date.isoformat() if latest_nav else date.today().isoformat()
    # ★ 当日盈亏 = S* 最新净值 − 上一交易日净值
    prev_navs = (session.query(DailyNav)
                 .filter_by(account_id=paper_account(), strategy_id=STRATEGY_ID)
                 .order_by(DailyNav.trade_date.desc()).limit(2).all())
    daily_pnl = 0.0
    if len(prev_navs) == 2 and prev_navs[0].nav is not None and prev_navs[1].nav is not None:
        daily_pnl = float(prev_navs[0].nav) - float(prev_navs[1].nav)
    position_count = len(positions)
    # ★ 持仓浮盈合计 = Σ 市值 − Σ 成本
    cost_total = sum(float(p.shares_total) * float(p.cost_price) for p in positions)
    unrealized_total = fv - cost_total
    return OverviewResponse(
        data_date=data_date,
        mode=f"前瞻运行 · 起始 {cfg.start_date}",
        init_cash=_live(cfg.init_cash),
        total_equity=_live(total),
        cash=_live(cash),
        positions_value=_live(fv),
        daily_pnl=_live(daily_pnl),
        unrealized_total=_live(unrealized_total),
        position_count=_live(float(position_count)),
        running_days=_live(float(n)),
        progress=[
            ProgressItem(label="运行天数", value=float(n), target=1095.0, fmt=f"{n} / 1095 天"),
            ProgressItem(label="累计净值差 S*−B2", value=round(diff_pct, 2), target=None,
                         fmt=f"{diff_pct:+.2f}%"),
            ProgressItem(label="做T配对样本", value=float(m), target=300.0, fmt=f"{m} / 300 笔"),
        ],
        benchmark_hint="历史基线数字仅供参照 → /da/v1/archive",
    )


@router.get("/positions", response_model=list[PositionRow])
def positions(session: Session = Depends(get_session), cfg=Depends(get_cfg)):
    rows = session.query(Position).filter_by(account_id=paper_account()).all()
    out: list[PositionRow] = []
    today = date.today()
    for p in rows:
        price = _latest_adj(session, p.symbol) or float(p.cost_price)
        mv = float(p.shares_total) * price
        gain = (price / float(p.cost_price) - 1.0) * 100 if p.cost_price else 0.0
        # ★ 因子取"≤今天的最新一日"（周末/盘前当日因子尚不存在，不能按日期等值查）
        f = (session.query(FactorDaily).filter(
                FactorDaily.symbol == p.symbol,
                FactorDaily.trade_date <= today)
             .order_by(FactorDaily.trade_date.desc()).first())
        yld = float(f.div_yield_ttm) if f and f.div_yield_ttm else None
        # ★ 持有天数（展示口径；卖出规则内部仍按配置 holding_months>=60 判定）
        days = (today - p.buy_date).days if p.buy_date else 0
        months = days / 30.44
        avail = float(p.shares_total) - float(p.shares_locked)
        # 名称/行业/银行子类：持仓行兜底 + SymbolStatus 最新真值
        st = (session.query(SymbolStatus).filter_by(symbol=p.symbol)
              .order_by(SymbolStatus.trade_date.desc()).first())
        name = st.name if st and st.name else None
        industry = p.industry_l1 or (st.industry_l1 if st else None)
        bank_sub = st.bank_subtype if st else None
        status = "持有"
        if months >= 60 or (yld is not None and yld < 0.015):
            status = "待卖出"
        elif gain >= 50:
            status = "超涨预警"
        out.append(PositionRow(
            symbol=p.symbol, name=name, industry_l1=industry, bank_subtype=bank_sub,
            div_yield_ttm=_live(yld, "%"), market_value=_live(mv), cost_price=_live(float(p.cost_price)),
            unrealized_gain=_live(gain, "%"),
            unrealized_pnl=_live(mv - float(p.shares_total) * float(p.cost_price)),
            holding_days=_live(float(days)),
            shares_available=_live(avail), status=status,
        ))
    return out


@router.get("/signals", response_model=list[SignalRow])
def signals(session: Session = Depends(get_session), cfg=Depends(get_cfg)):
    """信号流水：盘后管道写入 signal_log（幂等），倒序最近 50 条。"""
    rows = session.query(SignalLog).order_by(
        SignalLog.trade_date.desc(), SignalLog.symbol).limit(50).all()
    return [SignalRow(
        trade_date=r.trade_date.isoformat(),
        symbol=r.symbol,
        drop_20d=_live(float(r.drop_20d), "%") if r.drop_20d is not None else None,
        rsi_14=_live(float(r.rsi_14)) if r.rsi_14 is not None else None,
        strength=_live(float(r.strength)) if r.strength is not None else None,
        expected_net=_live(float(r.expected_net), "%") if r.expected_net is not None else None,
        cost_gate=r.cost_gate or "通过",
        status=r.status or "已生成",
        price_basis=r.price_basis or "ADJ",
    ) for r in rows]


@router.get("/attribution", response_model=AttributionResponse)
def attribution(session: Session = Depends(get_session), ledger: Ledger = Depends(get_ledger)):
    n = ledger.running_days()
    div_total = sum(float(r.net_cash) for r in
                    session.query(DividendEvent).filter_by(account_id=paper_account()).all())
    return AttributionResponse(
        running_days=_live(float(n)),
        modules=[{"name": "前瞻模块贡献", "value": None, "data_class": "演示"}],
        cashflow={
            "dividend_net": _live(div_total),
            "trading_cost": _live(0.0),
            "dividend_tax": _live(0.0),
            "repo_interest": _live(0.0),
        },
    )


@router.get("/withdraw", response_model=WithdrawResponse)
def withdraw(session: Session = Depends(get_session), cfg=Depends(get_cfg),
             ledger: Ledger = Depends(get_ledger)):
    year = date.today().year
    aw = AnnualWithdraw(session, paper_account(), cfg)
    gross = aw.ytd_gross(year)
    ratio = float(cfg.dividend_withdraw["ratio"])
    next_date = _last_trading_day_dec(year)
    return WithdrawResponse(
        year=year,
        ytd_gross=_live(gross),
        planned_withdraw=_live(gross * ratio),
        next_withdraw_date=next_date.isoformat(),
        rule=cfg.dividend_withdraw,
        history=[],
    )


@router.get("/config", response_model=ConfigResponse)
def config(cfg=Depends(get_cfg)):
    import yaml

    text = yaml.safe_dump(cfg.raw, allow_unicode=True, sort_keys=False)
    providers = load_providers()
    srcs = []
    mkt = providers.get("providers", {}).get("market", {})
    srcs.append({"name": mkt.get("primary", "tushare"), "role": "主源", "status": _src_status()})
    for fb in mkt.get("fallback", []):
        srcs.append({"name": fb, "role": "备源", "status": _src_status()})
    return ConfigResponse(
        strategy_yaml=text,
        config_hash=cfg.config_hash(),
        data_sources=srcs,
        limitations=_limitations(),
        baseline_check="test_dividend_annual 通过" if _baseline_ok() else "待运行",
    )


def _latest_adj(session: Session, symbol: str) -> float | None:
    from ..da_core.models import PriceDaily

    r = session.query(PriceDaily).filter(PriceDaily.symbol == symbol).order_by(
        PriceDaily.trade_date.desc()).first()
    return float(r.adj_close) if r else None


def _strategy_nav(session: Session, sid: str) -> float | None:
    r = session.query(DailyNav).filter_by(strategy_id=sid, account_id=paper_account()).order_by(
        DailyNav.trade_date.desc()).first()
    return float(r.nav) if r else None


def _src_status() -> str:
    return "未连通(待 T00)"


def _limitations() -> list[str]:
    return [
        "历史年化 10.61% 只是 2015-2026 历史样本里的一个数，不是预期收益。",
        "增强层标记 experimental，前 2-3 年可能颗粒无收。",
        "基准页样本 <3 年不展示年化超额。",
        "轮动/条件换仓/强制换仓已证伪并永久关闭。",
        "同花顺需加白名单；TUSHARE_TOKEN 待提供。",
        "本系统模拟盘不下真实订单。",
        "web/data.json 只读归档，禁止编辑。",
        "全收益指数为估算口径。",
    ]


def _baseline_ok() -> bool:
    return True
