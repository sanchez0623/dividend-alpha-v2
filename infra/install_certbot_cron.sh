#!/usr/bin/env bash
# 续期策略改为：每天检查 + renew_before_expiry=4 days（剩 ≤4 天才续，实际约 3 天一续）。
# 注：certbot 5.9.0 的 renew 无 --days 参数；阈值由 renewal conf 的 renew_before_expiry 控制。
set -euo pipefail
PROJ_DIR=/root/dividend-alpha
INFRA=$PROJ_DIR/infra
COMPOSE=infra/docker-compose.lite.yml
LE_LIVE=/etc/letsencrypt/live
CONF=$INFRA/letsencrypt/renewal/47.119.50.139.conf
MARK=dividend-alpha-certbot-renew

# 1) 在 renewal conf 里显式设置续期阈值 = 剩余 4 天
if grep -q '^renew_before_expiry' "$CONF"; then
  sed -i 's|^renew_before_expiry.*|renew_before_expiry = 4 days|' "$CONF"
else
  sed -i '/^\[renewalparams\]/i renew_before_expiry = 4 days' "$CONF"
fi
echo "=== renewal conf 阈值 ==="
grep -E '^renew_before_expiry' "$CONF"

# 2) cron 改回每日检查（去掉无效的 --days）
CRON_JOB="17 4 * * * cd ${PROJ_DIR} && docker run --rm -v ${INFRA}/letsencrypt:/etc/letsencrypt -v ${INFRA}/certs:/var/www/certbot certbot/certbot:latest renew --webroot -w /var/www/certbot --preferred-profile shortlived >> logs/certbot_renew.log 2>&1 && for d in ${LE_LIVE}/*/; do ip=\$(basename \"\$d\"); sed -e \"s|__CERT__|${LE_LIVE}/\${ip}/fullchain.pem|g\" -e \"s|__KEY__|${LE_LIVE}/\${ip}/privkey.pem|g\" infra/nginx.https.conf.template > infra/https.conf; done && docker compose -f ${COMPOSE} exec -T web nginx -s reload # ${MARK}"
( crontab -l 2>/dev/null | grep -v "# ${MARK}" || true
  echo "${CRON_JOB}"
) | crontab -

echo "=== 更新后的 crontab ==="
crontab -l | grep -E 'certbot|close'
