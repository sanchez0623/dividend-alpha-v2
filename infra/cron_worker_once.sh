#!/usr/bin/env bash
# dividend-alpha · 盘后闭环 cron 包装（轻量版 SQLite 部署用）
#
# 由 crontab 在每个交易日收盘后调用。交易日判断在容器内做（scripts/daily_close_entry.py），
# 不再用 shell heredoc，避免非交互环境下的 docker/转义问题。
#
# 幂等：非交易日容器内直接 exit 0 不写数据；worker 逻辑对
# (trade_date, strategy_id, account_id) 幂等，同日重跑安全。
#
# 手动调用（日志直接打到终端，便于排查）：
#   bash infra/cron_worker_once.sh
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_DIR}" || exit 1

COMPOSE_FILE="${COMPOSE_FILE:-infra/docker-compose.lite.yml}"
API_SERVICE="${API_SERVICE:-api}"

echo "==== dividend-alpha 盘后闭环 $(date '+%Y-%m-%d %H:%M:%S') ===="
exec docker compose -f "${COMPOSE_FILE}" exec -T "${API_SERVICE}" \
  python scripts/daily_close_entry.py
