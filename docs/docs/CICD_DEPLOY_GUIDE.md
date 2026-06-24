# footballAI CI/CD & Docker 部署指南 v3.0
## 对应流程图: 代码推送 → CI/CD流水线 → 测试 → 构建Docker → 推送Registry → 滚动更新 → 健康检查 → 部署/回滚

---

### 目录
1. [架构概览](#1-架构概览)
2. [快速开始](#2-快速开始)
3. [Docker 镜像说明](#3-docker-镜像说明)
4. [CI/CD 流水线](#4-cicd-流水线)
5. [滚动更新与自动回滚](#5-滚动更新与自动回滚)
6. [监控与健康检查](#6-监控与健康检查)
7. [生产环境配置清单](#7-生产环境配置清单)
8. [常见问题](#8-常见问题)

---

## 1. 架构概览

```
┌─────────────────────────────────────────────────────────────┐
│                    GitHub Repository                         │
│                                                             │
│  push to main ──→ .github/workflows/ci-cd.yml               │
│       │                                                      │
│       ▼                                                      │
│  ┌─────────┐   ┌──────────┐   ┌──────────┐   ┌─────────┐   │
│  │  测试    │ → │  构建     │ → │  推送     │ → │  部署    │   │
│  │ pytest  │   │ Docker   │   │ GHCR     │   │ SSH+Compose│ │
│  │ bandit  │   │ multi-arch│   │          │   │          │   │
│  └─────────┘   └──────────┘   └──────────┘   └─────────┘   │
└─────────────────────────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│                   生产服务器                                  │
│                                                             │
│  ┌──────────┐    ┌──────────┐    ┌──────────────────┐      │
│  │   API    │◄──►│  Redis   │    │  Data Collector   │      │
│  │ :8080    │    │ :6379    │    │  (定时任务)        │      │
│  │ Flask    │    │ 缓存层   │    │  增量数据同步      │      │
│  └──────────┘    └──────────┘    └──────────────────┘      │
│       │                                                   │
│       ▼                                                    │
│  /api/monitor/health  ← 健康检查                            │
│  /api/monitor/report ← 四维监控                             │
└─────────────────────────────────────────────────────────────┘
```

**容器组成**:
| 容器 | 端口 | 用途 |
|------|------|------|
| `api` | 8080 | Flask REST API + 预测引擎 + 监控端点 |
| `redis` | 6379 | 共享缓存层（L2缓存） |
| `collector` | — | 定时数据采集（增量同步） |

---

## 2. 快速开始

### 本地开发（无需 Docker）

```bash
cd footballAI
pip install -r requirements.txt
python api/prediction_service.py
# 访问 http://localhost:8080/api/monitor/health
```

### Docker Compose 一键启动

```bash
# 1. 配置环境变量
cp .env.example .env
# 编辑 .env 填入你的 API Keys

# 2. 启动全部服务
docker compose up -d

# 3. 检查状态
docker compose ps
curl localhost:8080/api/monitor/health

# 4. 查看日志
docker compose logs -f api
```

---

## 3. Docker 镜像说明

### 多阶段构建策略

| 阶段 | 基础镜像 | 内容 | 大小 |
|------|----------|------|------|
| `builder` | python:3.10-slim | 编译所有依赖（含 PyTorch CPU） | ~2GB |
| `runtime` | python:3.10-slim | 仅复制编译结果 + 应用代码 | ~800MB |

### 关键优化

- **分层缓存**: `requirements.txt` 变更才重建依赖层
- **非 root 用户**: `appuser`(UID 1000)，满足最小权限原则
- **健康检查**: HTTP 方式调用 `/api/monitor/health`
- **多架构**: 支持 `linux/amd64` + `linux/arm64`

### 手动构建命令

```bash
# 开发版
docker build -t football-ai:dev .

# 生产版（指定版本标签）
docker build --target runtime -t football-ai:v3.0.1 .

# 多架构构建（需 buildx）
docker buildx build --platform linux/amd64,linux/arm64 -t ghcr.io/owner/football-ai:v3.0.1 --push .
```

---

## 4. CI/CD 流水线

### 工作流文件

`.github/workflows/ci-cd.yml`

### 触发条件

| 事件 | 行为 |
|------|------|
| `push` 到 `main` | 完整流程: 测试→构建→推送→部署 |
| `PR` 到 `main` | 仅测试 + 构建（不部署） |
| `workflow_dispatch` | 手动触发，可选择版本和目标环境 |

### Job 流程图

```
test (并行)
  ├── pytest 集成测试 (Redis service)
  ├── bandit 安全扫描
  └── safety 依赖漏洞检查
      │
      ▼ (全部通过)
build-and-push
  ├── docker buildx (multi-platform)
  └── push to ghcr.io
      │
      ▼
deploy (environment: production)
  ├── SSH 连接服务器
  ├── docker pull 新镜像
  ├── docker compose 滚动更新
  ├── 健康检查 (最长180s)
  │   ├─ 通过 → ✅ 部署成功
  │   └─ 超时 → ⚠️ 自动回滚
  └── 通知 (Webhook/Slack/企微)
```

### 必须配置的 GitHub Secrets

在仓库 **Settings → Secrets and variables → Actions** 中添加:

| Secret 名称 | 说明 | 示例 |
|-------------|------|------|
| `DEPLOY_HOST` | 部署服务器 IP 或域名 | `192.168.1.100` |
| `DEPLOY_USER` | SSH 用户名 | `ubuntu` |
| `SSH_PRIVATE_KEY` | SSH 私钥内容 | `-----BEGIN ...` |
| `FOOTBALL_DATA_API_KEY` |足球数据 API Key | `xxx...` |
| `FLASK_SECRET_KEY` | Flask 密钥 | 随机字符串 |
| `API_AUTH_TOKEN` | API 认证 Token | 随机字符串 |
| `NOTIFY_WEBHOOK_URL` | 通知 Webhook URL | 可选 |
| `THE_ODDS_API_KEY` | 赔率 API Key | 可选 |
| `RAPIDAPI_KEY` | RapidAPI Key | 可选 |

> **注意**: `GITHUB_TOKEN` 由 Actions 自动提供，用于登录 GHCR。

---

## 5. 滚动更新与自动回滚

### 滚动更新机制

```bash
# 标准部署（零停机）
docker compose up -d --no-deps api
# 1. 拉取新镜像
# 2. 创建新容器
# 3. 健康检查通过后切换流量
# 4. 停止旧容器
```

### 自动回滚逻辑

当以下任一条件发生时，自动回滚到上一个可用版本:

1. **健康检查超时** (>180s `/api/monitor/health` 未返回 ok)
2. **容器启动失败** (exit code != 0)
3. **手动触发** (`docker compose rollback api`)

回滚步骤:
```bash
# 1. 从 backup 恢复 docker-compose.yml
cp docker-compose.yml.backup docker-compose.yml
# 2. 重启服务（使用旧镜像）
docker compose up -d
# 3. 发送告警通知
```

### 回滚脚本

```bash
#!/bin/bash
# scripts/rollback.sh — 手动一键回滚
set -e
BACKUP="docker-compose.yml.backup"
if [ -f "$BACKUP" ]; then
    cp "$BACKUP" docker-compose.yml
    docker compose up -d
    echo "✅ 已回滚"
else
    echo "❌ 无备份文件"
fi
```

---

## 6. 监控与健康检查

### 健康检查端点

```bash
# 轻量级检查（适合 uptime 监控 + Kubernetes liveness probe）
GET /api/monitor/health
# → {"status": "ok", "database": "ok", "model": "ok", ...}

# 四维完整报告
GET /api/monitor/report?window=24
# → {business: {...}, system: {...}, data_quality: {...}, errors: {...}}

# 缓存统计
GET /api/cache/stats
# → {"cache": {l1_hits: 123, l2_hits: 45, hit_rate: 0.78}}

# 安全审计（需要 Bearer Token）
GET /api/monitor/security
Authorization: Bearer <API_AUTH_TOKEN>
```

### Docker 层面健康检查

```yaml
# docker-compose.yml 中配置
healthcheck:
  test: ["CMD", "curl", "-f", "http://localhost:8080/api/monitor/health"]
  interval: 30s      # 每30秒检查
  timeout: 10s       # 单次超时10秒
  retries: 3         # 连续失败3次标记为 unhealthy
  start_period: 90s  # 启动后90秒才开始检查
```

### 推荐外部监控方案

| 监控项 | 工具 | 配置 |
|--------|------|------|
| Uptime | UptimeRobot / 阿里云云监控 | GET `/api/monitor/health` (每60s) |
| 日志 | ELK / Loki + Grafana | `docker logs football-api` |
| 指标 | Prometheus + Grafana | 自定义 exporter 读取 `/api/monitor/report` |
| 告警 | AlertManager / PagerDuty | health check 失败 → 通知 |

---

## 7. 生产环境配置清单

### 服务器最低要求

| 资源 | 最低配置 | 推荐 |
|------|----------|------|
| CPU | 2 cores | 4 cores |
| 内存 | 4 GB | 8 GB |
| 磁盘 | 20 GB SSD | 50 GB SSD |
| 系统 | Ubuntu 22.04 LTS | Ubuntu 22.04 LTS |
| Docker | >= 24.0 | 最新稳定版 |
| Docker Compose | v2.x | v2.x |

### `.env.production` 模板

```bash
# ===== 版本 =====
VERSION=v3.0.1
API_PORT=8080

# ===== 安全（必须修改！）=====
FLASK_SECRET_KEY=<随机64字符>
API_AUTH_TOKEN=<随机32字符>

# ===== 数据源 =====
FOOTBALL_DATA_API_KEY=<从 football-data.org 获取>
THE_ODDS_API_KEY=<可选>
RAPIDAPI_KEY=<可选>

# ===== Redis 缓存 =====
REDIS_MAX_MEMORY=256mb

# ===== 资源限制 =====
API_MEMORY_LIMIT=2G
LOG_LEVEL=INFO
GPU_MODE=auto

# ===== 采集器 =====
COLLECTOR_INTERVAL=1800  # 30分钟

# ===== 通知（可选）=====
NOTIFY_WEBHOOK_URL=<企业微信/Slack/DingTalk webhook>
```

### Nginx 反向代理配置（推荐）

```nginx
server {
    listen 443 ssl http2;
    server_name ai.yourdomain.com;

    ssl_certificate /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # WebSocket 支持（如需要）
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";

        # 超时设置
        proxy_read_timeout 120s;
        proxy_send_timeout 120s;
    }

    # 健康检查端点不需要认证
    location /api/monitor/ {
        proxy_pass http://127.0.0.1:8080;
        allow all;
    }

    # 其他 API 需要 Token
    location /api/predict {
        proxy_pass http://127.0.0.1:8080;
        # 可添加 rate limiting
    }
}
```

---

## 8. 常见问题

**Q: 健康检查一直 failing？**
A: 检查 `docker logs football-api`，常见原因：
  - 模型未加载（需要先训练或复制 saved_models）
  - 数据库为空（需要先运行数据导入）
  - 端口冲突（检查 8080 是否被占用）

**Q: 如何查看部署历史？**
A: `docker ps -a | grep football-api` 查看容器列表；`git log --oneline -10` 查看代码提交。

**Q: 如何扩容？**
A: 修改 `docker-compose.yml` 的 `deploy.replicas` 或迁移到 Kubernetes。

**Q: Redis 连不上怎么办？**
A: 系统会自动降级到纯内存缓存。检查 `depends_on.condition: service_healthy` 和网络配置。
