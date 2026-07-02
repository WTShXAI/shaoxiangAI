# 哨响AI — 生产部署指南 v4.0

## 架构概览

```
┌─────────────────────────────────────────────────────────────────┐
│                       Nginx (反向代理)                          │
│                    端口: 80 / 443                               │
├──────────┬──────────┬──────────┬──────────┬────────────────────┤
│ FastAPI  │ Celery   │ MLflow   │ Grafana  │ Prometheus         │
│ 后端 API │ Worker   │ 追踪     │ 监控面板 │ 指标采集           │
│ :8000    │          │ :5001    │ :3001    │ :9090              │
├──────────┴──────────┴──────────┴──────────┴────────────────────┤
│                    Redis (缓存/消息队列)                        │
│                    PostgreSQL (可选)                            │
│                    SQLite (开发环境)                            │
└─────────────────────────────────────────────────────────────────┘
```

## 快速部署 (Docker Compose)

### 1. 准备环境

```bash
# 克隆项目
git clone <repo-url> footballAI
cd footballAI

# 复制环境变量
cp deployment/.env.example .env
# 编辑 .env 填入 API Key 等敏感信息
```

### 2. 启动核心服务

```bash
# 启动全部服务 (API + Celery + Redis + MLflow)
docker compose -f deployment/docker-compose.yml --profile full up -d

# 仅启动 API (最小化部署)
docker compose -f deployment/docker-compose.yml --profile api up -d

# 启动含监控 (Prometheus + Grafana)
docker compose -f deployment/docker-compose.yml --profile monitoring up -d
```

### 3. 验证服务

```bash
# 健康检查
curl http://localhost:8000/api/v1/monitor/health

# API 文档
open http://localhost:8000/api/v1/docs

# MLflow 面板
open http://localhost:5001

# Grafana (admin/admin)
open http://localhost:3001
```

## 手动部署 (无 Docker)

### 后端

```bash
cd backend
pip install -r requirements.txt
python main.py
# 或: uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

### Celery Worker

```bash
# 需要先启动 Redis
cd backend
celery -A tasks.celery_app worker --loglevel=info --concurrency=2
```

### MLflow

```bash
cd ml
pip install mlflow
mlflow server --backend-store-uri sqlite:///mlflow.db --default-artifact-root ./artifacts --host 0.0.0.0 --port 5001
```

## 生产环境推荐

### 使用 Docker Swarm

```bash
# 初始化 Swarm
docker swarm init

# 部署
docker stack deploy -c deployment/docker-compose.yml football

# 查看服务
docker stack services football
```

### 使用 Kubernetes (k8s)

```yaml
# deployment/k8s/api-deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: football-api
spec:
  replicas: 3
  selector:
    matchLabels:
      app: football-api
  template:
    metadata:
      labels:
        app: football-api
    spec:
      containers:
      - name: api
        image: football-backend:latest
        ports:
        - containerPort: 8000
        env:
        - name: DATABASE_URL
          valueFrom:
            secretKeyRef:
              name: football-secrets
              key: database-url
        resources:
          limits:
            memory: "2Gi"
            cpu: "2"
          requests:
            memory: "512Mi"
            cpu: "0.5"
---
apiVersion: v1
kind: Service
metadata:
  name: football-api
spec:
  selector:
    app: football-api
  ports:
  - port: 8000
    targetPort: 8000
  type: LoadBalancer
```

## 环境变量

| 变量 | 必需 | 默认值 | 说明 |
|------|------|--------|------|
| `SECRET_KEY` | ✅ | `change-me` | JWT 签名密钥 |
| `FOOTBALL_DATA_API_KEY` | 推荐 | — | football-data.org API Key |
| `RAPIDAPI_KEY` | 推荐 | — | API-Football Key (RapidAPI) |
| `REDIS_URL` | 推荐 | `redis://localhost:6379/0` | Redis 地址 |
| `DATABASE_URL` | — | `sqlite:///...` | 数据库连接 |
| `MLFLOW_TRACKING_URI` | — | `http://localhost:5001` | MLflow 地址 |
| `LOG_LEVEL` | — | `INFO` | 日志级别 |
| `GPU_MODE` | — | `auto` | GPU 模式 |

## 安全加固清单

- [ ] 修改默认 SECRET_KEY 和密码
- [ ] 配置 HTTPS (Nginx + Let's Encrypt)
- [ ] 限制 CORS 来源
- [ ] 启用 API 限流 (rate limiting)
- [ ] 配置防火墙规则
- [ ] 设置日志审计
- [ ] 定期更新依赖

## 备份策略

```bash
# 数据库备份
cp data/football_data.db backups/football_data_$(date +%Y%m%d).db

# 模型备份
tar -czf saved_models_backup_$(date +%Y%m%d).tar.gz saved_models/*.joblib

# 注册表备份
cp saved_models/model_registry.json backups/model_registry_$(date +%Y%m%d).json
```

## 常见问题

**Q: 端口冲突？**
A: 修改 `.env` 中的 `API_PORT` 等。

**Q: 模型加载失败？**
A: 确保 `saved_models/` 目录存在 `.joblib` 文件。或运行训练流水线生成。

**Q: Redis 连接失败？**
A: 开发环境可通过 `export REDIS_URL=` (空值) 降级到内存缓存。
