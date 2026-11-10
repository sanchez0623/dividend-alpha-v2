.PHONY: init fetch-smoke factors-smoke ledger-init worker-once serve test lint clean

PY ?= python
export PYTHONPATH := $(CURDIR)/src

init:            ## 建库 + 迁移 + 写入 config
	$(PY) -c "from src.da_core.db import make_engine, init_db; init_db(make_engine()); print('db initialized')"

fetch-smoke:     ## T00/T02：抓近 90 日前复权日线，打印覆盖率
	$(PY) scripts/fetch_smoke.py

factors-smoke:   ## T03：抽查 5 只因子
	$(PY) scripts/factors_smoke.py

ledger-init:     ## T01：写入首日 7 条净值
	$(PY) scripts/ledger_init.py

worker-once:     ## T10：手动跑一次盘后闭环
	$(PY) scripts/worker_once.py

serve:           ## api + web
	$(PY) -m uvicorn src.da_api.main:app --host $${DA_API_HOST:-127.0.0.1} --port $${DA_API_PORT:-8100}

web-install:
	cd web && npm install

web-build:
	cd web && npm run build

test:            ## pytest -q（含 8 个测试文件）
	$(PY) -m pytest -q

lint:            ## ruff + mypy
	$(PY) -m ruff check src tests || true
	$(PY) -m mypy src || true

clean:
	rm -rf data/*.db data/parquet web/dist .pytest_cache
