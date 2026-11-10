# 阿里云 ECS 部署指南（无域名）

> ⚠️ **读我先**：一键脚本 `infra/deploy_aliyun.sh` 实际部署的是**轻量版** `infra/docker-compose.lite.yml`（web + api + SQLite，适配 2核2G），**不是**下文 §3.3–§6 示例里的全量 `docker-compose.yml`。
> - 照一键脚本部署（推荐）：把下文命令里的 `infra/docker-compose.yml` 换成 `infra/docker-compose.lite.yml`；初始化账本/盘后闭环用 `scripts/ledger_init.py` / `scripts/daily_close_entry.py`（cron 已自动装）；备份用 §4.1 的 SQLite `cp` 方式（lite 无 `db` 容器，`pg_dump` 不可用）。
> - **最新、与脚本完全对齐的可复用手册**：见 [`docs/ALIYUN_DEPLOY_PLAYBOOK.md`](./ALIYUN_DEPLOY_PLAYBOOK.md)。
>
> 适用：把 dividend-alpha 部署到阿里云 ECS，**没有域名**，用公网 IP 直接访问。
> 架构：单入口 nginx(80) = 前端静态 + `/da/v1` 反代 FastAPI；PostgreSQL/Redis 内网容器（全量版）。

---

## 1. 先回答你的两个问题

### Q：没有域名，怎么访问？
**用 ECS 公网 IP 直接访问**：浏览器打开 `http://<你的公网IP>/`。
- 前端页面：`http://<公网IP>/`
- 后端接口：`http://<公网IP>/da/v1/...`（例如健康检查 `http://<公网IP>/da/v1/health`）
- API 文档：`http://<公网IP>/da/v1/docs`

无域名不影响访问，只是：
- ❌ 不能上 **HTTPS**（证书要绑域名）→ 公网明文传输，**不要把真实 API key 放前端**（本项目 key 只在后端环境变量，OK）
- ❌ 无法用 CDN / 套 Cloudflare
- ✅ 功能完全可用；以后买了域名，改 nginx `server_name` + 挂证书即可，代码不用动

> 想现在就上 HTTPS 又没域名：可用阿里云「**云解析**」临时解析一个自有域名，或用 Caddy + 自签（仅测试）。

### Q：是否需要 Docker？
**强烈建议用 Docker**，理由：
| | 裸机部署 | Docker（推荐） |
|---|---|---|
| 依赖冲突 | Python/Node/PG/TimescaleDB 版本地狱 | 镜像隔离，一键起 |
| 环境一致性 | 易「本地能跑线上不能」 | 与开发环境一致 |
| 运维 | 手工 systemd + 编译 | `docker compose up -d` |
| 升级回滚 | 痛苦 | 换镜像 tag |

本项目已备好 `infra/docker-compose.yml`（api / worker / web / db / redis 五容器）。

---

## 2. 服务器准备（阿里云 ECS）

### 2.1 买 ECS
- 镜像：**Ubuntu 22.04** 或 Alibaba Cloud Linux 3
- 配置：起步 **2核4G**（TimescaleDB 吃内存，4G 是底线；生产建议 4核8G）
- **系统盘 40G+**（Docker 镜像 + 数据）
- **带宽**：按固定带宽 3–5 Mbps 起步（个人用够）

### 2.2 安全组（关键，否则访问不通）
ECS 控制台 → 实例 → 安全组 → 配置规则 → **入方向**添加：

| 端口 | 协议 | 授权对象 | 用途 |
|---|---|---|---|
| **80** | TCP | `0.0.0.0/0` | HTTP 访问（必开） |
| 22 | TCP | 你的本机 IP | SSH（别开 0.0.0.0/0） |
| ~~8000 / 5432 / 6379~~ | — | — | **不要开**！只走容器内网，暴露公网有安全风险 |

### 2.3 安装 Docker
SSH 登录后：
```bash
# 官方脚本（国内建议用阿里云镜像加速）
curl -fsSL https://get.docker.com | bash -s docker --mirror Aliyun
sudo systemctl enable --now docker

# docker compose 插件（v2）
sudo apt-get install -y docker-compose-plugin   # Ubuntu
docker compose version                          # 验证

# 配置镜像加速（拉镜像快）
sudo tee /etc/docker/daemon.json <<'EOF'
{
  "registry-mirrors": [
    "https://docker.m.daocloud.io",
    "https://docker.1ms.run"
  ]
}
EOF
sudo systemctl restart docker
```

---

## 3. 部署步骤

### 3.0 一键部署（推荐）
代码上服务器后，**一条命令**完成装 Docker → 构建镜像 → 起容器 → 初始化账本 → 健康检查：
```bash
cd ~/dividend-alpha
cp .env.example .env && nano .env   # 填 THS_API_KEY、改 POSTGRES_PASSWORD
bash infra/deploy_aliyun.sh
```
脚本幂等可重复执行。3.1–3.5 是它内部做的事（手动逐步时参考）。

### 3.1 上传代码
```bash
# 方式 A：git（推荐）
sudo apt-get install -y git
git clone <你的仓库> ~/dividend-alpha && cd ~/dividend-alpha

# 方式 B：本机 scp 上传
# scp -r ./dividend-alpha root@<公网IP>:~/
```

### 3.2 配置环境变量
```bash
cd ~/dividend-alpha
cp .env.example .env
nano .env
```
**必改项**：
```ini
THS_API_KEY=sk-fuyao-你的真实key
DA_API_PREFIX=/da/v1
# 数据库密码改掉（compose 里 POSTGRES_PASSWORD 引用它）
POSTGRES_PASSWORD=改成一个强密码
```

### 3.3 一键启动
```bash
docker compose -f infra/docker-compose.yml up -d --build
```
首次构建约 3–5 分钟（拉镜像 + npm/pip 安装）。

### 3.4 初始化数据（首日账本 + 建表）
```bash
docker compose -f infra/docker-compose.yml exec api \
  python scripts/ledger_init.py
```

### 3.5 验证
浏览器打开：
- `http://<公网IP>/` → 前端 8 页
- `http://<公网IP>/da/v1/health` → `{"status":"ok",...}`

或命令行：
```bash
curl http://127.0.0.1/da/v1/health       # 服务器本机自测
curl http://<公网IP>/da/v1/health        # 外网验证
```

---

## 4. 常用运维命令

```bash
cd ~/dividend-alpha
docker compose -f infra/docker-compose.yml ps          # 看容器状态
docker compose -f infra/docker-compose.yml logs -f api # 看 api 日志
docker compose -f infra/docker-compose.yml restart api # 重启某服务
docker compose -f infra/docker-compose.yml down        # 停止（数据在 volume 不丢）
docker compose -f infra/docker-compose.yml up -d --build  # 改代码后重建
```
跑盘后任务（手动）：
```bash
docker compose -f infra/docker-compose.yml exec api python scripts/worker_once.py
```

---

## 4.1 分红捕获（DC）上线检查

DC 与 S\* 并行，**沿用现有 cron**（工作日 17:00 调 `scripts/worker_once.py`），无需新增 crontab。

```bash
cd ~/dividend-alpha

# 1) 确认新配置/新代码已随部署包解压
ls config/strategy_dividend_capture.yaml
ls web/dc_backtest.json

# 2) 重建 api + web（web 含新页面）
docker compose -f infra/docker-compose.lite.yml up -d --build

# 3) 初始化/回填 DC 账户与净值（幂等；首次会刷新全市场≥500亿池，约 3-5 分钟）
docker compose -f infra/docker-compose.lite.yml exec -T api \
  python scripts/dc_backfill.py 2026-11-05 2026-11-07

# 4) 同日双策略闭环（幂等）
docker compose -f infra/docker-compose.lite.yml exec -T api \
  python scripts/worker_once.py 2026-11-06

# 5) 验证
curl -s http://127.0.0.1/da/v1/health
curl -s http://127.0.0.1/da/v1/dc/overview
curl -s http://127.0.0.1/da/v1/dc/cycles
curl -s http://127.0.0.1/da/v1/dc/backtest
curl -s http://<公网IP>/da/v1/dc/overview
```

浏览器打开 `http://<公网IP>/`，侧边栏应出现「策略 → 分红捕获」。
数据库应有 `dc_cycle` / `dc_universe_cache` 表，`daily_nav` 有 `strategy_id=DC, account_id=DC` 行；
S\* 的 `daily_nav` 行数不变。

**备份与回滚**：

```bash
# 部署前备份 SQLite（数据在 appdata 卷）
docker compose -f infra/docker-compose.lite.yml exec -T api \
  sh -c 'cp /app/data/dividend_alpha.db /app/data/dividend_alpha.db.bak_$(date +%Y%m%d_%H%M%S)'
cp da-deploy.zip da-deploy.prev.zip

# 回滚：恢复上一版代码 + 重建 + 用备份覆盖 DB
```

**注意**：DC 的 30% 取现是**逐次**（每笔分红卖出后）提取，与 S\* 的年度取现（12 月最后交易日）
是两套规则；DC 不写 `withdraw_log`，取现明细在 `dc_cycle.withdrawn`。

---

## 5. 数据持久化与备份

- PostgreSQL 数据在 docker volume `pgdata`，`down` 不丢，`down -v` 才删。
- 定期备份：
```bash
docker compose -f infra/docker-compose.yml exec db \
  pg_dump -U da dividend_alpha | gzip > ~/backup_$(date +%F).sql.gz
```
- 建议挂**阿里云云盘**做数据盘，或开「自动快照」。

---

## 6. 安全清单（公网必做）

- [ ] 安全组只开 80 + 22（22 限自己 IP）
- [ ] `.env` 权限 `chmod 600`，**不要提交 git**
- [ ] `POSTGRES_PASSWORD` 改强密码；5432/6379/8000 不暴露公网
- [ ] SSH 用密钥登录，禁 root 密码登录
- [ ] 后端 key（THS_API_KEY）只在 `.env`，不进前端
- [ ] 装 fail2ban / 阿里云「云安全中心」
- [ ] 有域名后立刻上 HTTPS（nginx 挂 Let's Encrypt 证书）

---

## 7. 没有域名 → 以后买域名怎么升级
1. 阿里云「云解析 DNS」加 A 记录：`你的域名 → ECS 公网IP`
2. 改 `infra/nginx.conf`：`server_name 你的域名;`
3. 加 HTTPS（certbot 或阿里云免费证书），nginx 80 强制跳 443
4. `docker compose up -d --build` 重建 web
5. **代码零改动**（前端用相对前缀 `/da/v1`，自动跟随域名）

---

## 7b. 无域名上 HTTPS（公网 IP 证书，已实测可用）

> 适用于**没有域名**、想直接用 `https://<公网IP>` 访问的场景。
> 利用 Let's Encrypt 新支持的 **IP 标识证书**（RFC 8738），走 `shortlived` profile。

### 原理
- Let's Encrypt `shortlived` profile 是目前**唯一**支持给裸 IP（`iPAddress` SAN）签发证书的 profile，
  有效期约 **6 天**，因此**必须自动续期**。
- 用官方 `certbot/certbot` Docker 镜像（宿主机 Python 3.6 太老，装不了现代 certbot）。
- 挑战方式：`webroot`，复用 nginx 80 端口；关键参数是 **`--ip-address`**（不是 `-d`）
  和 **`--preferred-profile shortlived`**。

### 涉及文件
| 文件 | 作用 |
|---|---|
| `infra/nginx.conf` | 80 端口：ACME 挑战路径 + 其余 301 跳 HTTPS |
| `infra/nginx.https.conf.template` | 443 站点模板（含 `__CERT__`/`__KEY__` 占位符） |
| `infra/https.conf` | 由脚本渲染生成（空 = 只跑 HTTP；有内容 = 启用 443） |
| `infra/enable_https_ip.sh` | 一键签发 + 启用 HTTPS + 装 cron |
| `infra/install_certbot_cron.sh` | 单独装/刷新续期 cron |
| `infra/docker-compose.lite.yml` | web 增开 443、挂载 `certs/`、`letsencrypt/`、`https.conf` |

### 一键启用
```bash
cd ~/dividend-alpha
# 前置：安全组入方向放行 TCP 443（0.0.0.0/0）
bash infra/enable_https_ip.sh <公网IP>      # 例：bash infra/enable_https_ip.sh 47.119.50.139
```
脚本幂等，重复执行安全。它会：
1. 以 HTTP-only 模式启动 nginx（`https.conf` 为空，避免证书缺失导致 nginx 起不来）；
2. `docker run certbot/certbot` 走 webroot 挑战，用 `--ip-address` + `shortlived` 签发证书到 `infra/letsencrypt/live/<IP>/`；
3. 渲染 `infra/https.conf`（填入**容器内**证书路径 `/etc/letsencrypt/live/<IP>/...`），reload nginx；
4. 安装每 12 小时的续期 cron（certbot 只在临期才真正续）。

### 验证
```bash
curl -s https://<公网IP>/da/v1/health      # {"status":"ok",...}
curl -sI http://<公网IP>/                  # 301 -> https://<公网IP>/
# 证书链（应 verify return code: 0，链到 ISRG Root X2）
echo | openssl s_client -connect <公网IP>:443 -verify_return_error | openssl x509 -noout -issuer -dates -ext subjectAltName
```

### 续期与运维
- 续期 cron 日志：`logs/certbot_renew.log`；手动跑一次：
  ```bash
  docker run --rm -v ~/dividend-alpha/infra/letsencrypt:/etc/letsencrypt \
    -v ~/dividend-alpha/infra/certs:/var/www/certbot certbot/certbot:latest renew \
    --webroot -w /var/www/certbot --preferred-profile shortlived
  ```
- 续期演练（不实际签发）：上面命令加 `--dry-run`。
- ⚠️ **证书仅 6 天有效期**，务必保证 cron 正常（`crontab -l` 应有 `dividend-alpha-certbot-renew` 条目）。
- **续期节奏**：cron **每天 04:17 检查一次**，renewal conf 里设了 `renew_before_expiry = 4 days`，
  即证书**剩余 ≤4 天**才真正续 → 实际约 **3 天续一次**，始终保有 3~4 天余量。
  > 别把 cron 真改成「每 3 天跑一次」：6 天期证书 certbot 默认剩 <50%(3天) 才续，
  > 三天一跑容易轮到时已临期/过期。正确做法就是「每日检查 + 抬高阈值」。
- **注册邮箱**：`certbot register/update_account` 用 `--email 23837880@qq.com`（过期提醒用）；
  `infra/enable_https_ip.sh` 已默认带此邮箱，可用环境变量 `CERTBOT_EMAIL` 覆盖。
  > 注：certbot 5.9.0 的 `show_account` 可能显示 `Email contact: none`，那是它不把 contact
  > 回写本地 `regr.json` 的显示 quirk，服务端其实已记录（抓 ACME 日志可见 `contact` 字段）。
- 买域名后建议改回经典 90 天证书：`certbot certonly -d 你的域名 ...`（去掉 `--ip-address` 与 `shortlived`）。

---

## 8. 不用 Docker 的替代方案（不推荐）
若坚持裸机：需手动装 Python 3.11 / Node 20 / PostgreSQL+TimescaleDB / Redis / nginx，
配 systemd 四个服务 + nginx 反代，运维成本高、易版本冲突。**除非有特殊理由，用 Docker。**
