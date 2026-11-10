#!/usr/bin/env bash
# dividend-alpha 盘后定时任务（cron 调用）
# 设计：
#   1) 用 fuyao 探测今天是否有新日线——是交易日且 fuyao 可达才跑 worker；
#   2) 节假日 / fuyao 故障 → 跳过（绝不把合成源假数据写进前瞻账本；
#      数据窗口 180 天幂等，漏跑一天次日自动补齐）；
#   3) 全程日志落 /root/da_cron.log。
set -u
cd /root/dividend-alpha
LOG=/root/da_cron.log
echo "=== $(date '+%F %T') cron tick ===" >> "$LOG"

VERDICT=$(docker compose -f infra/docker-compose.lite.yml exec -T api python - <<'PYEOF'
import datetime as dt
import os
from src.da_data.providers.ths import ThsFuyaoClient

today = dt.date.today()
try:
    c = ThsFuyaoClient(api_key=os.environ.get("THS_API_KEY", ""))
    # ★ 显式交易日历判定（/api/a-share/calendar/trading-days，语义干净）
    cal = c.fetch_trade_calendar(today - dt.timedelta(days=10), today)
    print("TRADING" if today in cal else "HOLIDAY")
except Exception:
    print("SKIP")
PYEOF
)
echo "verdict: $VERDICT" >> "$LOG"

if [ "$VERDICT" != "TRADING" ]; then
  echo "跳过（非交易日或 fuyao 不可达）" >> "$LOG"
  exit 0
fi

docker compose -f infra/docker-compose.lite.yml exec -T api \
  python scripts/worker_once.py >> "$LOG" 2>&1
echo "=== done rc=$? ===" >> "$LOG"
