#!/usr/bin/env bash
# dividend-alpha · 阿里云 ECS 一键部署（轻量版：SQLite + api + web，适配 2核2G）
# 用法：
#   1) 代码放服务器（git clone 或 Workbench 上传）
#   2) cd dividend-alpha-v2 && bash infra/deploy_aliyun.sh
# 幂等：重复执行安全。
set -euo pipefail
COMPOSE="infra/docker-compose.lite.yml"

echo "==> [1/6] 检查 Docker"
if ! command -v docker >/dev/null 2>&1; then
  echo "    未安装，正在安装（阿里云镜像源）..."
  curl -fsSL https://get.docker.com | bash -s docker --mirror Aliyun
  systemctl enable --now docker
fi
docker --version
if ! docker compose version >/dev/null 2>&1; then
  echo "    安装 docker compose 插件..."
  (apt-get update && apt-get install -y docker-compose-plugin) || \
  (yum install -y docker-compose-plugin) || true
fi

echo "==> [2/6] 配置 .env（不存在则从模板创建）"
if [ ! -f .env ]; then
  cp .env.example .env
  # 轻量版用 SQLite，把 DSN 覆盖为容器内路径（compose 里已强制，此处仅提示）
  echo "    已创建 .env —— 请编辑填入 THS_API_KEY：nano .env"
fi

echo "==> [2b/6] 配置 2G swap（防 npm/vite 构建 OOM，幂等）"
if [ ! -f /swapfile ]; then
  fallocate -l 2G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=2048
  chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  echo "    swap 已启用"
else
  swapon /swapfile 2>/dev/null || true
  echo "    swap 已存在"
fi
free -h | grep -i swap || true

echo "==> [3/6] 构建并启动容器（web:80 + api，SQLite）"
docker compose -f "$COMPOSE" up -d --build

echo "==> [4/6] 等待 api 就绪"
for i in $(seq 1 30); do
  if curl -fsS http://127.0.0.1/da/v1/health >/dev/null 2>&1; then break; fi
  sleep 3
done

echo "==> [5/6] 初始化首日账本（幂等）"
docker compose -f "$COMPOSE" exec -T api python scripts/ledger_init.py || \
  echo "    (账本已存在，跳过)"

echo "==> [6/6] 健康检查"
if curl -fsS http://127.0.0.1/da/v1/health >/dev/null 2>&1; then
  echo "✅ 后端健康"
else
  echo "⚠️  后端未就绪，查看日志: docker compose -f $COMPOSE logs -f api"
fi

echo "==> [7/7] 安装盘后闭环定时任务（幂等）"
# 轻量版无 worker 容器，用宿主机 crontab 在每个交易日收盘后跑 worker_once.py。
# 幂等：先删掉带标记的旧条目再重插，重复部署不会堆叠。
CRON_MARK="dividend-alpha-daily-close"
CRON_JOB="17 16 * * 1-5 cd $(pwd) && bash infra/cron_worker_once.sh >> logs/da_close.log 2>&1 # ${CRON_MARK}"
( crontab -l 2>/dev/null | grep -v "# ${CRON_MARK}" || true
  echo "${CRON_JOB}"
) | crontab -
chmod 755 infra/cron_worker_once.sh
mkdir -p logs
echo "    已安装 crontab：周一至周五 16:17 自动盘后闭环（交易日闸门见 cron_worker_once.sh）。"
echo "    查看：crontab -l   |   日志：logs/da_close.log   |   手动跑一次：bash infra/cron_worker_once.sh"

echo ""
echo "完成。浏览器访问: http://<本机公网IP>/"
echo "数据源状态: curl http://127.0.0.1/da/v1/datasources"
echo "定时任务:   crontab -l   |   手动跑一次: bash infra/cron_worker_once.sh"
