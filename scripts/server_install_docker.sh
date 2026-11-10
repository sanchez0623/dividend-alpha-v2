#!/usr/bin/env bash
# 阿里云 ECS 国内源安装 Docker（Alibaba Cloud Linux 3 / RHEL8 系）
set -x
curl -fsSL https://mirrors.aliyun.com/docker-ce/linux/centos/docker-ce.repo \
  -o /etc/yum.repos.d/docker-ce.repo || exit 1
sed -i 's|download.docker.com|mirrors.aliyun.com/docker-ce|g' /etc/yum.repos.d/docker-ce.repo
sed -i 's|\$releasever|8|g' /etc/yum.repos.d/docker-ce.repo
yum makecache -q 2>&1 | tail -2
yum install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin 2>&1 | tail -3
mkdir -p /etc/docker
cat > /etc/docker/daemon.json <<'EOF'
{ "registry-mirrors": ["https://docker.m.daocloud.io", "https://docker.1ms.run"] }
EOF
systemctl enable --now docker
sleep 2
docker --version && docker compose version && echo DOCKER-OK
