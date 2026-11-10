#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dividend-alpha-v2 / research/grid —— 70/30 核心+网格 回测引擎

口径（用户冻结，见 SPEC.md）：
  - 标的：S* 当前 10 只持仓（每只 30 万，等权，初始 300 万）
  - 每只 70% 核心（买入持有）+ 30% 网格
  - 主口径 symmetric + trailing：grid sleeve 初始 50% 股票/50% 现金，每格 10% sleeve，
    100 股整数手；参考价=上次成交价，±step（默认 2%）触发一格；每日收盘触发/成交；每只每日最多一格；T+1
  - 变体：sym_fixed（固定档位，锚=首日收盘）、sym_recenter（每月首个交易日重置参考价）、dip（30% 全现金）、
    hold100 / core70_cash30 基准、step 1.5%/2.5%、core/grid 80/20 与 50/50
  - 账户：cost.py 成本（config/strategy.yaml cost 段）；core 红利 0% 税（持有>1年）；
    grid 红利按 FIFO 批次实际持有天数 10%/20%；送股扩股；现金不计息；T+1

不使用任何未来数据：每日只用当日收盘及之前的价格/事件。
"""
from __future__ import annotations

import bisect
import json
import os
import sys
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("DA_CONFIG_DIR", str(ROOT / "config"))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

DATA = ROOT / "research" / "dividend_capture" / "data"
RESULTS = HERE / "results"
BARS_FILE = DATA / "bars_all.parquet"
EVENTS_FILE = DATA / "events_all.parquet"
CALENDAR_FILE = DATA / "calendar.csv"

START = date(2016, 1, 4)
END = date(2026, 11, 6)
INIT_CASH = 3_000_000.0
N_STOCKS = 10
PER_STOCK = INIT_CASH / N_STOCKS
SYMBOLS = ["sh600028", "sh600219", "sh601288", "sh601988", "sh600329",
           "sz000651", "sh600585", "sh601216", "sh601088", "sz000568"]

from src.da_core.cost import buy_cost, sell_cost  # noqa: E402

TAX_SHORT = 0.20    # <=30 天
TAX_MID = 0.10      # 31~365 天
TAX_LONG = 0.0      # >365 天


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def tax_rate(holding_days: int) -> float:
    if holding_days <= 30:
        return TAX_SHORT
    if holding_days <= 365:
        return TAX_MID
    return TAX_LONG


def load_configs():
    from src.da_core.config import load_strategy

    return load_strategy()


def sha256_of(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# 数据
# ---------------------------------------------------------------------------
@dataclass
class GridData:
    calendar: list[date]
    bars: dict[str, dict[date, float]]              # 不复权收盘（与生产/DC 同源）
    events: dict[str, list[dict]]                   # [{ex_date, dps, bonus}]
    names: dict[str, str]
    _bars_dates: dict[str, list[date]] = field(default_factory=dict, repr=False)

    def finalize(self) -> "GridData":
        for s in self.bars:
            self._bars_dates[s] = sorted(self.bars[s])
        return self

    def close_on(self, sym: str, d: date) -> float | None:
        return self.bars.get(sym, {}).get(d)

    def last_close(self, sym: str, d: date) -> float | None:
        ds = self._bars_dates.get(sym) or []
        i = bisect.bisect_right(ds, d) - 1
        return float(self.bars[sym][ds[i]]) if i >= 0 else None

    def first_close_on_or_after(self, sym: str, d: date) -> tuple[date, float] | None:
        ds = self._bars_dates.get(sym) or []
        i = bisect.bisect_left(ds, d)
        if i >= len(ds):
            return None
        return ds[i], float(self.bars[sym][ds[i]])


def load_data() -> GridData:
    cal_df = pd.read_csv(CALENDAR_FILE)
    calendar = [datetime.strptime(str(x)[:10], "%Y-%m-%d").date() for x in cal_df["date"]]
    calendar = [d for d in calendar if date(2015, 1, 5) <= d <= END]
    bars_df = pd.read_parquet(BARS_FILE)
    bars_df = bars_df[bars_df["symbol"].isin(SYMBOLS)]
    bars: dict[str, dict[date, float]] = {s: {} for s in SYMBOLS}
    for r in bars_df.itertuples():
        d = r.date if isinstance(r.date, date) else datetime.strptime(str(r.date)[:10],
                                                                     "%Y-%m-%d").date()
        bars[r.symbol][d] = float(r.close)
    ev_df = pd.read_parquet(EVENTS_FILE)
    ev_df = ev_df[ev_df["symbol"].isin(SYMBOLS)]
    raw: dict[str, list[dict]] = {s: [] for s in SYMBOLS}
    for r in ev_df.itertuples():
        d = r.ex_date if isinstance(r.ex_date, date) else datetime.strptime(
            str(r.ex_date)[:10], "%Y-%m-%d").date()
        raw[r.symbol].append({"ex_date": d, "dps": float(r.dps), "bonus": float(r.bonus)})
    events: dict[str, list[dict]] = {}
    for s in SYMBOLS:
        by: dict[date, dict] = {}
        for e in raw.get(s, []):
            cur = by.setdefault(e["ex_date"], {"ex_date": e["ex_date"], "dps": 0.0,
                                               "bonus": 0.0})
            cur["dps"] += e["dps"]
            cur["bonus"] += e["bonus"]
        events[s] = [by[d] for d in sorted(by)]
    from src.da_data.providers.eastmoney import fetch_individual_info
    try:
        info = fetch_individual_info(SYMBOLS)
    except Exception:  # noqa: BLE001
        info = pd.DataFrame()
    names = {}
    for s in SYMBOLS:
        nm = None
        if len(info):
            row = info[info["symbol"] == s]
            if len(row):
                nm = row["name"].iloc[0]
        names[s] = nm or s
    return GridData(calendar=calendar, bars=bars, events=events, names=names).finalize()


# ---------------------------------------------------------------------------
# 持仓批次 / 单标的账户
# ---------------------------------------------------------------------------
@dataclass
class Lot:
    shares: float
    buy_date: date
    price: float           # 每股成本价（送股后已除权调整）
    dps_accrued: float = 0.0   # 持有期间收到的税前分红（用于卖出时计税）
    fee_buy: float = 0.0       # 买入成本（按批次分摊）
    sell_level: int = 0        # 固定档位模式的卖出触发档（相对锚价的网格序号）


@dataclass
class Variant:
    name: str
    core_ratio: float = 0.70
    grid_mode: str = "trailing"       # none | trailing | fixed | recenter | dip
    step: float = 0.02
    grid_init_stock_frac: float = 0.50
    unit_frac: float = 0.10           # 每格占 grid sleeve 比例
    max_units: int = 10
    init_units: int = 5
    desc: str = ""


def variant_list() -> list[Variant]:
    return [
        Variant("hold100", core_ratio=1.0, grid_mode="none",
                desc="100% 买入持有（基准）"),
        Variant("core70_cash30", core_ratio=0.70, grid_mode="none",
                desc="70% 核心 + 30% 现金（无网格，现金拖累基准）"),
        Variant("core70_grid30_sym_trailing", core_ratio=0.70, grid_mode="trailing",
                desc="主口径：70/30 + 动态参考价（上次成交价）±2% 网格"),
        Variant("core70_grid30_sym_fixed", core_ratio=0.70, grid_mode="fixed",
                desc="固定档位：以 2016 首个交易日收盘为锚，±2%/±4%/... 穿越成交"),
        Variant("core70_grid30_sym_recenter", core_ratio=0.70, grid_mode="recenter",
                desc="每月首个交易日把参考价重置为当日收盘"),
        Variant("core70_grid30_dip", core_ratio=0.70, grid_mode="dip",
                grid_init_stock_frac=0.0, init_units=0,
                desc="30% 全现金，只做跌 2% 买 / 涨 2% 卖"),
        Variant("core70_grid30_step1.5", core_ratio=0.70, grid_mode="trailing",
                step=0.015, desc="敏感性：网格 1.5%"),
        Variant("core70_grid30_step2.5", core_ratio=0.70, grid_mode="trailing",
                step=0.025, desc="敏感性：网格 2.5%"),
        Variant("core80_grid20_sym", core_ratio=0.80, grid_mode="trailing",
                desc="敏感性：core/grid = 80/20"),
        Variant("core50_grid50_sym", core_ratio=0.50, grid_mode="trailing",
                desc="敏感性：core/grid = 50/50"),
    ]


@dataclass
class StockBook:
    symbol: str
    name: str
    core_lots: list[Lot] = field(default_factory=list)
    grid_lots: list[Lot] = field(default_factory=list)
    core_cash: float = 0.0
    grid_cash: float = 0.0
    grid_ref: float = 0.0
    anchor: float = 0.0
    last_close: float = 0.0
    last_date: date | None = None
    fixed_prev_level: int = 0
    init_grid_units: int = 0
    unit_shares: int = 0
    grid_sleeve: float = 0.0
    grid_units: int = 0
    initialized: bool = False
    fixed_pending_buys: set = field(default_factory=set)
    fixed_prev_close: float | None = None
    init_date: date | None = None
    init_price: float = 0.0
    init_core_shares: float = 0.0
    init_grid_shares: float = 0.0
    # 统计
    n_buys: int = 0
    n_sells: int = 0
    buy_amount: float = 0.0
    sell_amount: float = 0.0
    buy_fees: float = 0.0
    sell_fees: float = 0.0
    div_gross_grid: float = 0.0
    div_gross_core: float = 0.0
    div_tax_grid: float = 0.0
    realized_price_pnl: float = 0.0
    initial_grid_stock_cost: float = 0.0
    max_grid_units: int = 0
    round_trips: int = 0
    win_round_trips: int = 0
    trade_details: list[dict] = field(default_factory=list)
    min_cash: float = 0.0

    # ---- 估值 ----
    def core_shares(self) -> float:
        return sum(l.shares for l in self.core_lots)

    def grid_shares(self) -> float:
        return sum(l.shares for l in self.grid_lots)

    def value(self, close: float) -> float:
        return (self.core_shares() + self.grid_shares()) * close + self.core_cash + self.grid_cash

    def grid_value(self, close: float) -> float:
        return self.grid_shares() * close + self.grid_cash


# ---------------------------------------------------------------------------
# 初始化
# ---------------------------------------------------------------------------
def init_book(sym: str, data: GridData, va: Variant, cfg) -> StockBook:
    """建账：只分配现金，等首个有 bar 的交易日再建仓（避免用未来价格建仓）。"""
    book = StockBook(symbol=sym, name=data.names.get(sym, sym))
    book.core_cash = PER_STOCK * va.core_ratio
    book.grid_sleeve = PER_STOCK * (1.0 - va.core_ratio)
    book.grid_cash = book.grid_sleeve
    return book


def init_positions(book: StockBook, va: Variant, d: date, p0: float, cfg) -> None:
    """首个有 bar 的交易日：核心买入并持有 + 网格初始仓位（symmetric 50%）。"""
    book.anchor = p0
    book.grid_ref = p0
    book.last_close = p0
    book.last_date = d
    book.last_close = p0
    book.init_date = d
    book.init_price = p0
    # 核心
    core_budget = book.core_cash
    core_shares = int(core_budget / p0 // 100) * 100
    book.init_core_shares = float(core_shares)
    if core_shares > 0:
        amount = core_shares * p0
        fee = float(buy_cost(cfg, amount).total)
        book.core_lots.append(Lot(shares=float(core_shares), buy_date=d, price=p0,
                                  fee_buy=fee))
        book.core_cash = core_budget - amount - fee
    # 网格单元
    book.unit_shares = int(va.unit_frac * book.grid_sleeve / p0 // 100) * 100
    if va.grid_mode == "none" or book.unit_shares <= 0:
        book.initialized = True
        book.grid_units = 0
        book.min_cash = min(book.core_cash, book.grid_cash)
        return
    init_units = va.init_units
    init_shares = init_units * book.unit_shares
    amount = init_shares * p0
    fee = float(buy_cost(cfg, amount).total) if init_shares > 0 else 0.0
    if amount + fee > book.grid_cash + 1e-9:
        init_units = int((book.grid_cash / p0) // book.unit_shares) if book.unit_shares else 0
        init_units = min(init_units, va.init_units)
        init_shares = init_units * book.unit_shares
        amount = init_shares * p0
        fee = float(buy_cost(cfg, amount).total) if init_shares > 0 else 0.0
    if init_shares > 0:
        # 按「每格 1 个批次」建仓，便于 FIFO 计税与固定档位配对
        per_unit_fee = fee / max(init_units, 1)
        for i in range(init_units):
            lot = Lot(shares=float(book.unit_shares), buy_date=d, price=p0,
                      fee_buy=per_unit_fee)
            if va.grid_mode == "fixed":
                lot.sell_level = i + 1      # 初始在锚价(0 档)，上卖档 +1..+5
            book.grid_lots.append(lot)
        book.grid_cash -= amount + fee
        book.initial_grid_stock_cost = amount
        book.buy_fees += fee
        book.buy_amount += amount
        # 初始建仓不计入「网格交易笔数」，但买入金额/费用计入账户成本
    book.init_grid_units = init_units
    book.grid_units = init_units
    book.init_grid_shares = float(init_units * book.unit_shares)
    book.max_grid_units = init_units
    if va.grid_mode == "fixed":
        book.fixed_pending_buys = {k for k in range(-10, 0)}   # -1..-10 档可买
        book.fixed_prev_close = p0
    book.initialized = True
    book.min_cash = min(book.core_cash, book.grid_cash)


# ---------------------------------------------------------------------------
# 事件（除权日分红/送股）
# ---------------------------------------------------------------------------
def apply_event(book: StockBook, ev: dict) -> None:
    dps = float(ev["dps"])
    bonus = float(ev["bonus"])
    factor = 1.0 + bonus
    if dps > 0:
        # core：持有至 >1 年 → 0% 红利税（Lead 冻结口径），现金入 core_cash
        core_sh = book.core_shares()
        if core_sh > 0:
            gross = dps * core_sh
            book.div_gross_core += gross
            book.core_cash += gross
        # grid：税前入 grid_cash，税在卖出时按 FIFO 批次实际持有天数扣
        for lot in book.grid_lots:
            g = dps * lot.shares
            lot.dps_accrued += g
            book.div_gross_grid += g
            book.grid_cash += g
    if bonus > 0:
        for lot in book.core_lots + book.grid_lots:
            lot.shares *= factor
            lot.price /= factor
        # 网格单元按送股比例扩大股数，保持单元金额稳定
        book.unit_shares = max(100, int(round(book.unit_shares * factor / 100.0)) * 100)
    # 除权除息价调整：参考价/锚价按 (ref - dps) / (1+bonus) 调整，避免除权跳空被当成 2% 价格波动
    if dps > 0 or bonus > 0:
        if book.initialized:
            book.grid_ref = max((book.grid_ref - dps) / factor, 1e-6)
            book.anchor = max((book.anchor - dps) / factor, 1e-6)
            if book.fixed_prev_close is not None:
                book.fixed_prev_close = (book.fixed_prev_close - dps) / factor


# ---------------------------------------------------------------------------
# 网格成交
# ---------------------------------------------------------------------------
def grid_sellable_units(book: StockBook, d: date) -> int:
    """T+1：只统计买入日 < 今天的网格批次。"""
    return sum(1 for lot in book.grid_lots if lot.buy_date < d and lot.shares > 0)


def do_grid_buy(book: StockBook, d: date, price: float, unit: int, cfg,
                reason: str, sell_level: int = 0, ref_before: float | None = None,
                prev_close: float | None = None) -> bool:
    amount = unit * price
    fee = float(buy_cost(cfg, amount).total)
    if amount + fee > book.grid_cash + 1e-9:
        return False
    book.grid_cash -= amount + fee
    book.grid_lots.append(Lot(shares=float(unit), buy_date=d, price=price,
                              fee_buy=fee, sell_level=sell_level))
    book.grid_units += 1
    book.n_buys += 1
    book.buy_amount += amount
    book.buy_fees += fee
    book.max_grid_units = max(book.max_grid_units, book.grid_units)
    book.trade_details.append({"date": d.isoformat(), "symbol": book.symbol,
                               "side": "BUY", "price": price, "shares": unit,
                               "amount": amount, "fee": fee, "tax": 0.0,
                               "units_after": book.grid_units, "reason": reason,
                               "ref_before": ref_before, "prev_close": prev_close})
    return True


def do_grid_sell(book: StockBook, d: date, price: float, unit: int, cfg,
                 reason: str, ref_before: float | None = None) -> bool:
    """FIFO 卖出 unit 股；按批次实际持有天数扣红利税。"""
    if sum(l.shares for l in book.grid_lots if l.buy_date < d) < unit - 1e-9:
        return False
    remaining = unit
    sold_parts = []   # (buy_date, buy_price, take, dps_part, holding_days)
    while remaining > 0:
        lot = book.grid_lots[0]
        take = min(remaining, lot.shares)
        if take <= 0:
            book.grid_lots.pop(0)
            continue
        frac = take / lot.shares
        dps_part = lot.dps_accrued * frac
        hd = (d - lot.buy_date).days
        sold_parts.append((lot.buy_date, lot.price, take, dps_part, hd))
        lot.dps_accrued -= dps_part
        lot.shares -= take
        remaining -= take
        if lot.shares <= 1e-9:
            book.grid_lots.pop(0)
    amount = unit * price
    fee = float(sell_cost(cfg, amount).total)
    tax = 0.0
    for buy_date, buy_price, take, dps_part, hd in sold_parts:
        tax += dps_part * tax_rate(hd)
        book.realized_price_pnl += (price - buy_price) * take
        book.round_trips += 1
        if price > buy_price:
            book.win_round_trips += 1
    book.grid_cash += amount - fee - tax
    book.n_sells += 1
    book.grid_units -= 1
    book.sell_amount += amount
    book.sell_fees += fee
    book.div_tax_grid += tax
    book.trade_details.append({"date": d.isoformat(), "symbol": book.symbol,
                               "side": "SELL", "price": price, "shares": unit,
                               "amount": amount, "fee": fee, "tax": tax,
                               "units_after": book.grid_units, "reason": reason,
                               "ref_before": ref_before,
                               "tax_detail": [[round(dp, 6), hd]
                                              for _, _, _, dp, hd in sold_parts],
                               "matched_buy_dates": [bd.isoformat()
                                                     for bd, *_ in sold_parts]})
    assert book.grid_cash >= -1e-6, f"{book.symbol} 卖出后现金为负"
    return True


def do_grid_sell_lot(book: StockBook, lot: Lot, d: date, price: float, cfg,
                     reason: str) -> bool:
    """固定档位：卖出指定的单批（1 格）。"""
    if (d - lot.buy_date).days <= 0:
        return False        # T+1
    take = lot.shares
    dps_part = lot.dps_accrued
    hd = (d - lot.buy_date).days
    tax = dps_part * tax_rate(hd)
    amount = take * price
    fee = float(sell_cost(cfg, amount).total)
    book.grid_lots.remove(lot)
    book.grid_cash += amount - fee - tax
    book.n_sells += 1
    book.grid_units -= 1
    book.sell_amount += amount
    book.sell_fees += fee
    book.div_tax_grid += tax
    book.realized_price_pnl += (price - lot.price) * take
    book.round_trips += 1
    if price > lot.price:
        book.win_round_trips += 1
    book.trade_details.append({"date": d.isoformat(), "symbol": book.symbol,
                               "side": "SELL", "price": price, "shares": take,
                               "amount": amount, "fee": fee, "tax": tax,
                               "units_after": book.grid_units, "reason": reason,
                               "tax_detail": [[round(dps_part, 6), hd]],
                               "matched_buy_dates": [lot.buy_date.isoformat()]})
    assert book.grid_cash >= -1e-6, f"{book.symbol} 卖出后现金为负"
    return True


def grid_trigger(book: StockBook, va: Variant, d: date, close: float, cfg,
                 first_of_month: bool) -> None:
    """每日收盘检查一次，最多成交一格。"""
    if va.grid_mode == "none" or book.unit_shares <= 0:
        return
    unit = book.unit_shares
    if va.grid_mode == "fixed":
        # 固定档位：以锚价为 0 档，L(k)=anchor*(1+k*step)。
        # 向下穿越 k 档（k 为未消耗买档）→ 买入；该批次上卖档 = k+1；
        # 向上穿越某批次的卖档 → 卖出该批；卖出后重新挂回下一档买单。
        prev = book.fixed_prev_close
        if prev is None:
            book.fixed_prev_close = close
            return
        lv = lambda k: book.anchor * (1.0 + k * va.step)
        if close < prev:
            for k in sorted([k for k in range(-10, 1) if prev > lv(k) >= close],
                            reverse=True):
                if k in book.fixed_pending_buys and book.grid_units < va.max_units:
                    if do_grid_buy(book, d, close, unit, cfg, f"fixed_buy_{k}",
                                   sell_level=k + 1, prev_close=prev):
                        book.fixed_pending_buys.discard(k)
                        break
        elif close > prev:
            for k in sorted([k for k in range(-9, 11) if prev < lv(k) <= close]):
                cand = [lot for lot in book.grid_lots
                        if lot.sell_level == k and lot.buy_date < d]
                if cand:
                    if do_grid_sell_lot(book, cand[0], d, close, cfg, f"fixed_sell_{k}"):
                        book.fixed_pending_buys.add(k - 1)
                        break
        book.fixed_prev_close = close
        return
    if va.grid_mode == "recenter" and first_of_month:
        book.grid_ref = close
        return
    # trailing / dip / recenter（非重置日）
    upper = book.grid_ref * (1.0 + va.step)
    lower = book.grid_ref * (1.0 - va.step)
    ref_before = book.grid_ref
    if close <= lower:
        if book.grid_units < va.max_units:
            if do_grid_buy(book, d, close, unit, cfg, f"trail_buy_{va.step:.3f}",
                           ref_before=ref_before):
                book.grid_ref = close
    elif close >= upper:
        if do_grid_sell(book, d, close, unit, cfg, f"trail_sell_{va.step:.3f}",
                        ref_before=ref_before):
            book.grid_ref = close


# ---------------------------------------------------------------------------
# 组合模拟
# ---------------------------------------------------------------------------
def run_variant(data: GridData, va: Variant, cfg,
                start: date = START, end: date = END) -> dict:
    books: dict[str, StockBook] = {}
    ev_ptrs: dict[str, int] = {}
    for s in SYMBOLS:
        books[s] = init_book(s, data, va, cfg)
        ev_ptrs[s] = 0
    seen_month = {s: set() for s in SYMBOLS}
    nav_rows = []
    cal = [d for d in data.calendar if start <= d <= end]
    days_with_bar = {s: 0 for s in SYMBOLS}
    exposure_sum = 0.0
    exposure_n = 0
    for d in cal:
        for s in SYMBOLS:
            # 1) 除权事件（按首个 >= ex_date 的交易日处理；停牌顺延）
            evs = data.events.get(s) or []
            while ev_ptrs[s] < len(evs) and evs[ev_ptrs[s]]["ex_date"] <= d:
                apply_event(books[s], evs[ev_ptrs[s]])
                ev_ptrs[s] += 1
            close = data.close_on(s, d)
            if close is None or close <= 0:
                continue          # 停牌：不交易，估值用最近可信价
            if not books[s].initialized:
                init_positions(books[s], va, d, close, cfg)
            days_with_bar[s] += 1
            books[s].last_close = close
            books[s].last_date = d
            # 每月首个有 bar 的交易日
            key = (d.year, d.month)
            first_of_month = key not in seen_month[s]
            if first_of_month:
                seen_month[s].add(key)
            # 2) 网格（当日收盘触发）
            grid_trigger(books[s], va, d, close, cfg, first_of_month)
            books[s].min_cash = min(books[s].min_cash, books[s].core_cash,
                                    books[s].grid_cash)
        total = 0.0
        stock_val = 0.0
        cash_val = 0.0
        for s in SYMBOLS:
            b = books[s]
            total += b.value(b.last_close)
            stock_val += (b.core_shares() + b.grid_shares()) * b.last_close
            cash_val += b.core_cash + b.grid_cash
        if total > 0:
            exposure_sum += stock_val / total
            exposure_n += 1
        nav_rows.append({"date": d.isoformat(), "equity": total,
                         "nav": total / INIT_CASH, "stock_value": stock_val,
                         "cash": cash_val})
    nav_df = pd.DataFrame(nav_rows)

    # 期末：递延红利税（未卖出的 grid 批次，按截至末日的持有天数）
    deferred_tax = 0.0
    grid_rows = []
    for s in SYMBOLS:
        b = books[s]
        for lot in b.grid_lots:
            deferred_tax += lot.dps_accrued * tax_rate((end - lot.buy_date).days)
        close = b.last_close
        grid_rows.append({
            "symbol": s, "name": b.name,
            "core_shares": round(b.core_shares(), 2),
            "grid_shares": round(b.grid_shares(), 2),
            "grid_units": len(b.grid_lots),
            "init_grid_units": b.init_grid_units,
            "core_cash": round(b.core_cash, 2),
            "grid_cash": round(b.grid_cash, 2),
            "last_close": round(close, 4),
            "grid_div_gross": round(b.div_gross_grid, 2),
            "grid_div_tax": round(b.div_tax_grid, 2),
            "n_buys": b.n_buys, "n_sells": b.n_sells,
            "buy_amount": round(b.buy_amount, 2), "sell_amount": round(b.sell_amount, 2),
            "buy_fees": round(b.buy_fees, 2), "sell_fees": round(b.sell_fees, 2),
            "realized_price_pnl": round(b.realized_price_pnl, 2),
            "max_grid_units": b.max_grid_units,
            "round_trips": b.round_trips, "win_round_trips": b.win_round_trips,
            "initial_grid_stock_cost": round(b.initial_grid_stock_cost, 2),
            "grid_final_value": round(b.grid_value(close), 2),
            "grid_sleeve": round(b.grid_sleeve, 2),
            "grid_excess_gross": round(b.grid_value(close) - b.grid_sleeve, 2),
        })
    final_equity = nav_df["equity"].iloc[-1] if len(nav_df) else INIT_CASH
    trades = []
    for s in SYMBOLS:
        trades.extend(books[s].trade_details)
    trades_df = pd.DataFrame(trades)
    return {
        "variant": va,
        "nav": nav_df,
        "trades": trades_df,
        "books": books,
        "grid_rows": pd.DataFrame(grid_rows),
        "grid_excess_gross": float(sum(r["grid_excess_gross"] for r in grid_rows)),
        "deferred_tax": float(deferred_tax),
        "final_equity": float(final_equity),
        "final_wealth_net": float(final_equity - deferred_tax),
        "n_buys": int(sum(b.n_buys for b in books.values())),
        "n_sells": int(sum(b.n_sells for b in books.values())),
        "buy_fees": float(sum(b.buy_fees for b in books.values())),
        "sell_fees": float(sum(b.sell_fees for b in books.values())),
        "grid_div_gross": float(sum(b.div_gross_grid for b in books.values())),
        "core_div_gross": float(sum(b.div_gross_core for b in books.values())),
        "grid_tax": float(sum(b.div_tax_grid for b in books.values())),
        "realized_price_pnl": float(sum(b.realized_price_pnl for b in books.values())),
        "round_trips": int(sum(b.round_trips for b in books.values())),
        "win_round_trips": int(sum(b.win_round_trips for b in books.values())),
        "max_grid_units": int(max((b.max_grid_units for b in books.values()), default=0)),
        "days_with_bar": days_with_bar,
        "exposure": float(exposure_sum / exposure_n) if exposure_n else 0.0,
        "min_cash": float(min((b.min_cash for b in books.values()), default=0.0)),
        "initial": {s: {"date": books[s].init_date.isoformat() if books[s].init_date else None,
                        "price": books[s].init_price,
                        "core_shares": books[s].init_core_shares,
                        "grid_shares": books[s].init_grid_shares,
                        "init_units": books[s].init_grid_units,
                        "unit_shares": books[s].unit_shares,
                        "grid_sleeve": books[s].grid_sleeve,
                        "core_ratio": va.core_ratio}
                    for s in SYMBOLS},
        "last_close": {s: books[s].last_close for s in SYMBOLS},
    }


# ---------------------------------------------------------------------------
# 指标
# ---------------------------------------------------------------------------
def compute_metrics(res: dict, start: date = START, end: date = END) -> dict:
    nav = res["nav"]
    eq = nav["equity"].astype(float).to_numpy()
    years = (end - start).days / 365.25
    final = float(eq[-1]) if len(eq) else INIT_CASH
    w = np.concatenate([[INIT_CASH], eq])
    rets = w[1:] / w[:-1] - 1.0
    vol = float(np.std(rets, ddof=1) * np.sqrt(252)) if len(rets) > 2 else float("nan")
    sharpe = (float(np.mean(rets) / np.std(rets, ddof=1) * np.sqrt(252))
              if len(rets) > 2 and np.std(rets, ddof=1) > 0 else float("nan"))
    peak = np.maximum.accumulate(w)
    dd = w / peak - 1.0
    mdd = float(dd.min())
    mdd_idx = int(np.argmin(dd))
    mdd_date = (["(init)"] + list(nav["date"]))[mdd_idx]
    peak_idx = int(np.argmax(w[:mdd_idx + 1]))
    peak_date = (["(init)"] + list(nav["date"]))[peak_idx]
    cagr = (final / INIT_CASH) ** (1.0 / years) - 1.0 if years > 0 else float("nan")
    # 逐年
    nav = nav.copy()
    nav["year"] = nav["date"].str[:4].astype(int)
    yearly = []
    prev = INIT_CASH
    for y in sorted(nav["year"].unique()):
        sub = nav[nav["year"] == y]
        w_end = float(sub["equity"].iloc[-1])
        yearly.append({"year": int(y), "return": round(w_end / prev - 1.0, 6),
                       "equity_end": round(w_end, 2)})
        prev = w_end
    # 暴露：股票市值/总权益 的日均
    # （主变体再算；这里用 grid_rows 的最终值不能反映日均，另由 run_backtest 补充）
    trades = res["trades"]
    n_tr = len(trades)
    years_t = years
    metrics = {
        "variant": res["variant"].name,
        "desc": res["variant"].desc,
        "params": {
            "core_ratio": res["variant"].core_ratio,
            "grid_ratio": round(1.0 - res["variant"].core_ratio, 6),
            "grid_mode": res["variant"].grid_mode,
            "step": res["variant"].step,
            "grid_init_stock_frac": res["variant"].grid_init_stock_frac,
            "unit_frac": res["variant"].unit_frac,
            "max_units": res["variant"].max_units,
            "init_units": res["variant"].init_units,
        },
        "start": start.isoformat(), "end": end.isoformat(),
        "years": round(years, 4),
        "init_cash": INIT_CASH,
        "final_equity": round(final, 2),
        "deferred_tax": round(res["deferred_tax"], 2),
        "final_wealth_net": round(res["final_wealth_net"], 2),
        "total_return": round(final / INIT_CASH - 1.0, 6),
        "cagr": round(float(cagr), 6),
        "ann_vol": round(vol, 6),
        "sharpe": round(float(sharpe), 4),
        "mdd": round(mdd, 6),
        "mdd_peak_date": str(peak_date), "mdd_trough_date": str(mdd_date),
        "calmar": round(float(cagr / abs(mdd)), 4) if mdd < 0 else None,
        "grid_trades": int(n_tr),
        "grid_trades_per_year": round(n_tr / years_t, 3),
        "grid_buys": int(res["n_buys"]),
        "grid_sells": int(res["n_sells"]),
        "grid_buy_fees": round(res["buy_fees"], 2),
        "grid_sell_fees": round(res["sell_fees"], 2),
        "grid_costs": round(res["buy_fees"] + res["sell_fees"], 2),
        "grid_div_gross": round(res["grid_div_gross"], 2),
        "grid_tax_realized": round(res["grid_tax"], 2),
        "core_div_gross": round(res["core_div_gross"], 2),
        "grid_realized_price_pnl": round(res["realized_price_pnl"], 2),
        "grid_excess_gross": round(res["grid_excess_gross"], 2),
        "grid_round_trips": int(res["round_trips"]),
        "grid_win_rate": (round(res["win_round_trips"] / res["round_trips"], 6)
                          if res["round_trips"] else 0.0),
        "max_grid_units": int(res["max_grid_units"]),
        "exposure_ratio": round(float(res["exposure"]), 6),
        "min_cash": round(float(res["min_cash"]), 2),
        "yearly": yearly,
    }
    # 换手 / 单笔均值 / 逐年网格笔数（仅真实网格成交，不含初始建仓）
    trade_notional = float(trades["amount"].sum()) if n_tr else 0.0
    avg_equity = float(nav["equity"].mean()) if len(nav) else INIT_CASH
    metrics["grid_turnover"] = round(trade_notional / avg_equity / years_t, 4) if years_t else 0.0
    metrics["avg_grid_trade_notional"] = (round(trade_notional / n_tr, 2)
                                          if n_tr else 0.0)
    metrics["grid_trade_notional"] = round(trade_notional, 2)
    if len(trades) and "date" in trades.columns:
        trades2 = trades.assign(year=trades["date"].str[:4].astype(int))
        by_year = trades2.groupby("year").size().to_dict()
        for row in metrics["yearly"]:
            row["grid_trades"] = int(by_year.get(row["year"], 0))
    metrics["grid_by_stock"] = res["grid_rows"].to_dict(orient="records")
    return metrics


def snapshot_hashes(res: dict) -> dict:
    import hashlib

    nav_csv = res["nav"].to_csv(index=False, float_format="%.2f")
    tr_csv = (res["trades"].to_csv(index=False, float_format="%.6f")
              if len(res["trades"]) else "")
    return {"nav_sha256": hashlib.sha256(nav_csv.encode()).hexdigest(),
            "trades_sha256": hashlib.sha256(tr_csv.encode()).hexdigest(),
            "final_equity": round(float(res["final_equity"]), 2),
            "grid_trades": int(len(res["trades"]))}
