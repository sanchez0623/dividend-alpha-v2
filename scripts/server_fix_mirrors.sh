#!/usr/bin/env bash
# 修复 docker 镜像加速 + 重启 docker + 重新启动部署
set -e
printf '%s' '{ "registry-mirrors": ["https://docker.1ms.run", "https://docker.m.daocloud.io", "https://docker.1panel.live", "https://hub.rat.dev"] }' > /etc/docker/daemon.json
cat /etc/docker/daemon.json
systemctl restart docker
sleep 3
docker info 2>/dev/null | grep -A5 'Registry Mirrors' || true
cd /root/dividend-alpha
setsid nohup bash infra/deploy_aliyun.sh > /root/da_deploy.log 2>&1 < /dev/null &
echo RELAUNCHED
