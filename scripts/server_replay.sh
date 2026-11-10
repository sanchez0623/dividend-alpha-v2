#!/usr/bin/env bash
# 三段重放：11-05 建仓（起始日收盘）→ 11-06 → 11-07
set -e
cd /root/dividend-alpha
for D in 2026-11-05 2026-11-06 2026-11-07; do
  echo "=== replay $D ==="
  docker compose -f infra/docker-compose.lite.yml exec -T api python scripts/worker_once.py "$D" 2>&1 | tail -1
done
