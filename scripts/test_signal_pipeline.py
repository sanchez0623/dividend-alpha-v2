"""端到端验证信号管道：构造触发条件的因子 → 插件 → 落库 → 读取。"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy.orm import sessionmaker  # noqa: E402

from src.da_core.config import load_strategy  # noqa: E402
from src.da_core.db import init_db, make_engine  # noqa: E402
from src.da_core.models import FactorDaily, PriceDaily, SignalLog  # noqa: E402
from src.da_signal.base import SignalContext  # noqa: E402
from src.da_signal.overlay_rsi import OverlayRSIPlugin  # noqa: E402
from src.da_signal.pipeline import generate_and_store_signals  # noqa: E402

DSN = "sqlite:///./data/sig_test.db"

eng = make_engine(DSN)
init_db(eng)
s = sessionmaker(bind=eng, expire_on_commit=False)()
cfg = load_strategy()
d = date.today()

# 1) 构造触发条件的因子（20日跌幅 20% ≥ 12%，RSI 15 < 25；
#    expected_net = 0.20*0.15 + 10*0.004 = 0.07 > 3×往返成本 0.0435 → 成本闸通过）
s.merge(FactorDaily(symbol="sh601398", trade_date=d, rsi_14=15.0, drop_20d=-0.20,
                    div_yield_ttm=0.055, consecutive_div_years=6))
s.merge(PriceDaily(symbol="sh601398", trade_date=d, raw_close=8.35,
                   adj_close=8.35, adj_factor=1.0))
s.flush()

# 2) 直调插件
f = s.query(FactorDaily).filter_by(symbol="sh601398", trade_date=d).one()
ctx = SignalContext(asof=d, cfg=cfg, factors={"sh601398": f},
                    positions={}, price_adj={"sh601398": 8.35})
out = OverlayRSIPlugin().generate(ctx)
print(f"overlay 插件产出: {len(out)} 条")
for x in out:
    print(f"  {x.kind} {x.symbol} strength={x.strength} exp={x.expected_net} {x.reason}")

# 3) 全管道（含落库）
n = generate_and_store_signals(s, cfg, ["sh601398"], d)
s.commit()
rows = s.query(SignalLog).all()
print(f"管道写入: {n} 条; signal_log 表内 {len(rows)} 条")
for r in rows:
    print(f"  LOG {r.kind} {r.symbol} strength={float(r.strength):.3f} "
          f"exp={float(r.expected_net):.4f} gate={r.cost_gate} {r.reason}")

assert len(rows) == len(out) and len(rows) > 0, "信号未落库！"
print("OK 信号管道端到端通过")
