#!/usr/bin/env bash
# dividend-alpha · 上 HTTPS（无域名，用公网 IP 证书）
# 证书：Let's Encrypt「shortlived」profile，支持 IP 标识（RFC 8738），有效期约 6 天，
#       由本脚本安装的 cron 每 12 小时自动续期（certbot 只在临期才真正续）。
# 流程：nginx 先以 HTTP-only 启动（https.conf 为空）→ certbot webroot 挑战签发
#       → 用模板填充 https.conf（启用 443）→ reload → 装 cron 自动续期。
# 路径：compose 挂载的是 infra/certs 与 infra/letsencrypt（相对 compose 文件所在 infra/），
#       certbot 也必须读写同一目录，否则挑战文件对不上。
# 用法：cd dividend-alpha-v2 && bash infra/enable_https_ip.sh <公网IP>
# 幂等：重复执行安全。
set -euo pipefail

IP="${1:-}"
if [ -z "$IP" ]; then
  IP=$(curl -fsS --max-time 10 ifconfig.me || true)
fi
if ! [[ "$IP" =~ ^[0-9]+(\.[0-9]+){3}$ ]]; then
  echo "用法：bash infra/enable_https_ip.sh <公网IPv4>"; exit 1
fi
echo "==> 为公网 IP $IP 申请 Let's Encrypt 证书"

PROJ_DIR="$(pwd)"
COMPOSE="infra/docker-compose.lite.yml"
INFRA="$PROJ_DIR/infra"
HTTPS_CONF="$INFRA/https.conf"
CERTS_DIR="$INFRA/certs"
LE_DIR="$INFRA/letsencrypt"
# HTTPS 开关标记：证书就绪后创建（nginx 据此决定 HTTP 是否跳 https）。位于 conf.d，worker 必可读。
MARKER_CONF="$INFRA/https.enabled"
CONTAINER_MARKER="/etc/nginx/conf.d/https.enabled"

# 1) 准备目录（与 compose 挂载一致）；https.conf 为空 => nginx HTTP-only 启动
mkdir -p "$CERTS_DIR" "$LE_DIR" "$PROJ_DIR/logs"
chmod 755 "$CERTS_DIR" "$LE_DIR"
[ -f "$HTTPS_CONF" ] || : > "$HTTPS_CONF"
# 安全兜底：本轮签发成功前先清掉旧标记，避免 certbot 失败时 HTTP 仍跳到起不来的 443
rm -f "$MARKER_CONF"
docker compose -f "$COMPOSE" up -d --build web
sleep 2

# 2) certbot 走 webroot 挑战签发 IP 证书（--ip-address + shortlived profile）
docker run --rm \
  -v "$LE_DIR:/etc/letsencrypt" \
  -v "$CERTS_DIR:/var/www/certbot" \
  certbot/certbot:latest certonly \
    --non-interactive --agree-tos --no-eff-email \
    --email "${CERTBOT_EMAIL:-23837880@qq.com}" \
    --webroot -w /var/www/certbot \
    --ip-address "$IP" \
    --preferred-profile shortlived

# 3) 证书就绪：用模板渲染证书路径（容器内 letsencrypt 挂载于 /etc/letsencrypt），启用 443，reload
LE_LIVE="/etc/letsencrypt/live/$IP"
sed -e "s|__CERT__|${LE_LIVE}/fullchain.pem|g" \
    -e "s|__KEY__|${LE_LIVE}/privkey.pem|g" \
    infra/nginx.https.conf.template > "$HTTPS_CONF"
docker compose -f "$COMPOSE" exec -T web nginx -t
docker compose -f "$COMPOSE" exec -T web nginx -s reload
# 证书就绪 → 创建开关标记，nginx 的 HTTP server 据此启用 跳转
touch "$MARKER_CONF"
chmod 644 "$MARKER_CONF"
echo "==> HTTPS 已启用（标记 $CONTAINER_MARKER 已创建）"

# 4) 安装续期 cron（每 12 小时；certbot 自判临期才续，幂等不堆叠）
CRON_MARK="dividend-alpha-certbot-renew"
CRON_JOB="17 */12 * * * cd ${PROJ_DIR} && docker run --rm -v ${INFRA}/letsencrypt:/etc/letsencrypt -v ${INFRA}/certs:/var/www/certbot certbot/certbot:latest renew --webroot -w /var/www/certbot --preferred-profile shortlived >> logs/certbot_renew.log 2>&1 && sed -e \"s|__CERT__|${LE_LIVE}/fullchain.pem|g\" -e \"s|__KEY__|${LE_LIVE}/privkey.pem|g\" infra/nginx.https.conf.template > infra/https.conf && touch infra/https.enabled && docker compose -f ${COMPOSE} exec -T web nginx -s reload # ${CRON_MARK}"
( crontab -l 2>/dev/null | grep -v "# ${CRON_MARK}" || true
  echo "${CRON_JOB}"
) | crontab -
echo "    已安装续期 cron：每 12 小时检查（仅临期才续），日志 logs/certbot_renew.log"

# 5) 验证
sleep 2
if curl -fsS --max-time 10 "https://${IP}/da/v1/health" >/dev/null 2>&1; then
  echo "✅ HTTPS 可用：https://${IP}/"
else
  echo "⚠️  HTTPS 未就绪，查看：docker compose -f $COMPOSE logs -f web"
fi
