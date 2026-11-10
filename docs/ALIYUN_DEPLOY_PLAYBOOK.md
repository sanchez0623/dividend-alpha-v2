# 阿里云部署手册（可复用模板）

> 目的：把当前系统的阿里云部署方式沉淀成一份**可移植**的手册，让其它项目照着同样思路部署。
> 现状参考：本项目已用这套链路部署到阿里云 ECS（cn-shenzhen），公网 IP 直访、无域名、Docker Compose 单机部署。
> 阅读对象：要把类似「前端 SPA + 后端 API + 定时任务 + 数据库」单体服务部署到阿里云 ECS 的开发者。

---

## 0. 一页总览

| 项 | 当前选择 | 说明 |
|---|---|---|
| 云厂商 / 区域 | 阿里云 ECS · `cn-shenzhen` | 区域写在脚本常量里 |
| 访问方式 | **公网 IP 直访，无域名** | `http://<ECS公网IP>/` |
| 部署形态 | **Docker Compose 单机** | 强烈建议，避免依赖地狱 |
| 入口 | **nginx(80) 单入口** | 前端静态 + `/da/v1` 反代后端 |
| 后端 | FastAPI + uvicorn（容器内 8000，不对公网暴露） | |
| 前端 | React + Vite 构建产物，由 nginx 托管 | 构建时注入 API 前缀 |
| 数据库 | 两档：TimescaleDB(pg16) 全量 / **SQLite 轻量** | 低配机用 SQLite |
| 定时任务 | 宿主机 **crontab** → 容器内盘后脚本 | 轻量版无 worker 容器 |
| 代码上云 | **OSS 中转 + ECS 云助手远程执行** | 全程可不 SSH |
| HTTPS | 暂无（无域名），预留升级路径 | 见 §8 |

两套 compose 文件：
- `infra/docker-compose.yml`：**全量**（web / api / worker / db TimescaleDB / redis）
- `infra/docker-compose.lite.yml`：**轻量**（web / api + SQLite），适配 2核2G，实际一键脚本用的就是它

---

## 1. 架构

```
浏览器
  │  http://<公网IP>/
  ▼
┌─────────────────────────────────────────────┐
│  ECS (Ubuntu 22.04, 2核2G, Docker)          │
│                                             │
│  ┌───────────────────────────┐              │
│  │ web (nginx:alpine)  :80   │◀── 公网唯一入口
│  │  • /            → SPA 静态 │              │
│  │  • /da/v1/      → 反代 api │              │
│  │  • WS 升级头已配           │              │
│  └────────────┬──────────────┘              │
│               │ proxy_pass http://api:8000  │
│  ┌────────────▼──────────────┐              │
│  │ api (python:3.11-slim)    │  expose 8000 │
│  │  FastAPI + uvicorn        │  (仅内网)    │
│  │  SQLite: /app/data/*.db   │              │
│  └───────────────────────────┘              │
│   volume: appdata (SQLite 持久化)            │
│                                             │
│  crontab 16:17 工作日 → cron_worker_once.sh │
│      └─► docker compose exec api 盘后闭环    │
└─────────────────────────────────────────────┘
```

全量版额外包含：`worker`（常驻调度 `src.da_worker.scheduler`）、`db`（TimescaleDB，volume `pgdata`）、`redis`。

**关键安全设计**：只有 nginx 的 80 端口映射到公网（`ports: ["80:80"]`）；api(8000)、db(5432)、redis(6379) 一律用 `expose`（仅容器内网），绝不 `ports` 映射到宿主机公网。

---

## 2. 前置准备

### 2.1 买 ECS
- 镜像：**Ubuntu 22.04**（或 Alibaba Cloud Linux 3）
- 配置：起步 **2核2G** 可跑轻量版（脚本会自动配 2G swap 防 npm/vite 构建 OOM）；跑全量 TimescaleDB 建议 **2核4G 起，生产 4核8G**
- 系统盘：**40G+**（Docker 镜像 + 数据）
- 带宽：固定带宽 3–5 Mbps 起步

### 2.2 安全组（最容易踩坑的一步）
ECS 控制台 → 实例 → 安全组 → 配置规则 → **入方向**：

| 端口 | 协议 | 授权对象 | 用途 |
|---|---|---|---|
| **80** | TCP | `0.0.0.0/0` | HTTP 访问（必开） |
| 22 | TCP | 你的本机 IP | SSH（**别开 0.0.0.0/0**） |
| ~~8000 / 5432 / 6379~~ | — | — | **不要开**！只走容器内网 |

### 2.3 安装 Docker
```bash
# 官方脚本，国内用阿里云镜像源
curl -fsSL https://get.docker.com | bash -s docker --mirror Aliyun
sudo systemctl enable --now docker

# docker compose v2 插件
sudo apt-get install -y docker-compose-plugin
docker compose version    # 验证

# 镜像加速（拉镜像快）
sudo tee /etc/docker/daemon.json <<'EOF'
{ "registry-mirrors": ["https://docker.m.daocloud.io", "https://docker.1ms.run"] }
EOF
sudo systemctl restart docker
```

---

## 3. 标准部署（SSH 登录，一键脚本）

适合能 SSH 上服务器的场景，一条命令搞定。

### 3.1 上传代码
```bash
# 方式 A：git（前提：项目已托管到 git 仓库）
git clone <你的仓库> ~/app && cd ~/app
# 方式 B：scp（本项目当前目录不是 git 仓库，实际用这种方式或 §4 的 OSS 中转）
scp -r ./your-project root@<公网IP>:~/
```

### 3.2 配环境变量
```bash
cp .env.example .env
nano .env
```
必改项（以本项目为例，其它项目替换成自己的密钥）：
```ini
THS_API_KEY=sk-fuyao-你的真实key     # 数据源密钥，只放后端，不进前端
DA_API_PREFIX=/da/v1                 # 接口命名空间前缀，前后端+nginx 须一致
POSTGRES_PASSWORD=改成一个强密码      # 全量版用；轻量版 SQLite 可忽略
```
> `.env` 不入库、不进镜像，权限 `chmod 600`。

### 3.3 一键部署
```bash
bash infra/deploy_aliyun.sh
```
脚本幂等（可重复执行），内部依次做 7 件事：
1. 检查/安装 Docker + compose 插件
2. 不存在 `.env` 则从模板创建
3. 配 2G swap（防构建 OOM，幂等）
4. `docker compose -f infra/docker-compose.lite.yml up -d --build`
5. 轮询 `http://127.0.0.1/da/v1/health` 等 api 就绪
6. 初始化首日账本 `scripts/ledger_init.py`（幂等）
7. 安装 crontab：工作日 16:17 盘后闭环（幂等，按标记去重）

### 3.4 验证
```bash
curl http://127.0.0.1/da/v1/health    # 服务器本机自测
curl http://<公网IP>/da/v1/health     # 外网验证
```
浏览器打开 `http://<公网IP>/` 看前端页面。

---

## 4. 远程部署（免 SSH：OSS 中转 + ECS 云助手）

这是本项目实际用的「本地一键上云」链路，**全程不用手动 SSH**，适合 CI 或本地脚本自动化。四个脚本串起来：

```
pack_deploy.py   →  打出最小部署包 da-deploy.zip（剔除 .env/node_modules/data 等）
      │
oss_upload.py    →  上传 zip 到阿里云 OSS，生成 30 分钟有效的预签名下载 URL
      │
ecs_run.py run   →  通过 ECS 云助手 RunCommand 在服务器上执行：
      │             curl 预签名URL 下载 → 解压 → bash deploy_aliyun.sh
      ▼
verify_deploy.py →  从公网验证前端 bundle / API 是否正常
```

### 4.1 打包：`scripts/pack_deploy.py`
产出 `da-deploy.zip`，只保留运行所需目录（`src / config / infra / scripts / web / tests / requirements.txt / .env.example / Makefile` 等），剔除 `.git / node_modules / data / .env / docs / .pnpm-store` 等大体积或敏感内容。**`.env` 永不进包**。

### 4.2 上传 OSS：`scripts/oss_upload.py`
用 V1 签名（HMAC-SHA1）实现 PutBucket → PutObject → 生成预签名 GET URL（30 分钟）。AK/SK 复用 `ecs_run.load_ak()`。输出最后一行是预签名 URL，供服务器 `curl` 下载。

### 4.3 远程执行：`scripts/ecs_run.py`
阿里云 **ECS 云助手（Cloud Assistant）OpenAPI** 客户端（RPC 风格，HMAC-SHA1 签名）：
- `python scripts/ecs_run.py check`：DescribeInstances 查实例状态/IP
- `python scripts/ecs_run.py run "uname -a"`：下发 shell 命令并轮询 DescribeInvocationResults 取回 base64 输出
- `python scripts/ecs_run.py upload <本地> <远端>`：分块 base64 上传小文件（遇 `CmdContent.ExceedLimit` 自动减半块大小）

AK 从本地 workbench 配置读取（脚本里的 `AK_CONFIG` 路径），**secret 不回显日志**。区域 / 实例 ID 是脚本顶部常量：
```python
REGION   = "cn-shenzhen"
INSTANCE = "i-wz95p1n73tl4oraq1xmi"
```
> 移植到其它项目时，改这三样：`REGION`、`INSTANCE`、`AK_CONFIG`，以及 OSS 的 `BUCKET`。

### 4.4 公网验证：`scripts/verify_deploy.py`
拉首页 → 抠出 `/assets/*.js` bundle → 检查关键探活字符串、无双重前缀 `/da/v1/da/v1` 残留 → 打 `/da/v1/overview` 看状态码。

### 4.5 典型远程部署序列
```bash
python scripts/pack_deploy.py
python scripts/oss_upload.py da-deploy.zip da-deploy.zip      # 得到 PRESIGNED_URL
python scripts/ecs_run.py run "
  cd /root/app &&
  curl -fsSL '<PRESIGNED_URL>' -o da-deploy.zip &&
  unzip -oq da-deploy.zip &&
  bash infra/deploy_aliyun.sh
"
python scripts/verify_deploy.py
```

---

## 5. 定时任务（盘后闭环）

轻量版没有常驻 worker 容器，改用**宿主机 crontab** 调容器内脚本：
```
17 16 * * 1-5  cd /root/app && bash infra/cron_worker_once.sh >> logs/da_close.log 2>&1
```
- `cron_worker_once.sh` → `docker compose exec -T api python scripts/daily_close_entry.py`
- **交易日闸门放在容器内**（`daily_close_entry.py` 用 akshare 真实日历，失败退回周一至周五近似），避免在 shell heredoc 里做日期判断
- 幂等：非交易日直接 exit 0 不写数据；同日重跑安全

运维：
```bash
crontab -l                        # 查看
bash infra/cron_worker_once.sh    # 手动跑一次
tail -f logs/da_close.log         # 看日志
```

---

## 6. 常用运维命令

```bash
cd /root/app
docker compose -f infra/docker-compose.lite.yml ps            # 容器状态
docker compose -f infra/docker-compose.lite.yml logs -f api   # api 日志
docker compose -f infra/docker-compose.lite.yml restart api   # 重启某服务
docker compose -f infra/docker-compose.lite.yml down          # 停止（数据在 volume 不丢）
docker compose -f infra/docker-compose.lite.yml up -d --build # 改代码后重建
```

---

## 7. 数据持久化与备份

- 轻量版：SQLite 在 docker volume `appdata`（`/app/data/dividend_alpha.db`），`down` 不丢，`down -v` 才删。
- 全量版：PostgreSQL 在 volume `pgdata`。
- 备份：
```bash
# SQLite
docker compose -f infra/docker-compose.lite.yml exec -T api \
  sh -c 'cp /app/data/dividend_alpha.db /app/data/dividend_alpha.db.bak_$(date +%Y%m%d_%H%M%S)'
# PostgreSQL（全量版）
docker compose -f infra/docker-compose.yml exec db \
  pg_dump -U da dividend_alpha | gzip > ~/backup_$(date +%F).sql.gz
```
- 建议挂阿里云云盘做数据盘，或开自动快照。

---

## 8. 升级 HTTPS（买了域名后）

代码零改动（前端用相对前缀 `/da/v1`，自动跟随域名）：
1. 阿里云「云解析 DNS」加 A 记录：`你的域名 → ECS 公网IP`
2. 改 `infra/nginx.conf`：`server_name 你的域名;`
3. 加证书（certbot / 阿里云免费证书），nginx 80 强制跳 443
4. `docker compose up -d --build` 重建 web

---

## 9. 移植到其它项目的清单

把这套链路搬到新项目，按顺序改这些点：

**必改（脚本常量 / 配置）**
1. `scripts/ecs_run.py`：`REGION`、`INSTANCE`、`AK_CONFIG` 路径
2. `scripts/oss_upload.py`：`REGION`、`BUCKET`（bucket 名带你的账号 ID 便于区分）
3. `scripts/verify_deploy.py`：目标公网 IP `B`
4. `infra/deploy_aliyun.sh`：`COMPOSE` 指向你的 compose 文件、健康检查 URL、cron 时间与脚本名

**必改（应用层）**
5. Dockerfile：`Dockerfile.api` / `Dockerfile.web`（后端启动命令、前端构建命令）
6. `infra/nginx.conf`：反代路径 `/da/v1/` 换成你的 API 前缀
7. `.env.example`：换成你项目的密钥与连接串
8. compose：按需选全量（带 PG/Redis）或轻量（SQLite）

**可选**
9. 数据源密钥只放后端 `.env`，前端通过后端反代访问，**不要把 key 打进前端 bundle**
10. 用 API 前缀（如 `/da/v1`）做命名空间，避免同机多服务 `/api` 冲突

---

## 10. 安全清单（公网必做）

- [ ] 安全组只开 80 + 22（22 限自己 IP）
- [ ] `.env` 权限 `chmod 600`，**不提交 git、不进部署包**
- [ ] 数据库密码改强密码；5432/6379/8000 不暴露公网
- [ ] SSH 用密钥登录，禁 root 密码登录
- [ ] 后端密钥只在 `.env`，不进前端 bundle
- [ ] 装 fail2ban / 阿里云「云安全中心」
- [ ] AK/SK 存本地配置文件，脚本不回显 secret
- [ ] 有域名后立刻上 HTTPS

---

## 11. 常见问题

**Q：没域名能访问吗？**
能，`http://<公网IP>/`。代价是不能上 HTTPS、不能用 CDN，功能完全可用。

**Q：一定要 Docker 吗？**
强烈建议。裸机要手动装 Python/Node/PG/Redis/nginx + systemd，版本冲突和运维成本高。Docker 一键起、环境一致、升级回滚只换 tag。

**Q：2核2G 跑得动吗？**
跑轻量版（SQLite）可以，`deploy_aliyun.sh` 会自动配 2G swap 防构建 OOM，api 容器 `mem_limit: 1g`。跑全量 TimescaleDB 建议 4G 起。

**Q：为什么 API 走 `/da/v1` 这种带前缀的路径？**
避免和同机其它服务的 `/api` 冲突，且前后端 + nginx 三处保持一致即可，前端用相对前缀，换域名不用改代码。

**Q：改了代码怎么更新？**
标准方式：`docker compose up -d --build`。远程方式：重跑 §4.5 的打包→上传→远程执行序列。
