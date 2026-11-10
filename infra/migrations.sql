-- dividend-alpha v3.0 · PostgreSQL 16 + TimescaleDB 迁移（生产）
-- 本地/测试使用 SQLite（由 SQLAlchemy ORM 自动建表），生产执行本文件启用 hypertable。

-- ---------- 行情（hypertable） ----------
SELECT create_hypertable('price_daily', 'trade_date', if_not_exists => TRUE);

-- ---------- 索引（05 §2） ----------
CREATE INDEX IF NOT EXISTS idx_price_symbol_date ON price_daily (symbol, trade_date DESC);
CREATE INDEX IF NOT EXISTS idx_factor_date_yld ON factor_daily (trade_date, div_yield_ttm DESC);
CREATE INDEX IF NOT EXISTS idx_signal_pair_date ON signal_pair (trade_date);
CREATE INDEX IF NOT EXISTS idx_daily_nav_date ON daily_nav (trade_date, strategy_id);

-- ---------- 分红捕获（DC） ----------
CREATE INDEX IF NOT EXISTS idx_dc_cycle_account_status ON dc_cycle (account_id, status);
CREATE INDEX IF NOT EXISTS idx_dc_cycle_ex_date ON dc_cycle (ex_date);
