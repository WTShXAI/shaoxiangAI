# 哨响AI — 部署套件 (Docker + Windows 原生守护) 

让单机 FastAPI + React 系统稳定、可托管上线的交付物。所有文件位于 `deploy/`，**不修改任何现有业务代码**，天然满足「不破坏预测能力 + 可回滚」。

---

## 1. 架构

```
                 ┌─────────────────────────────────────────────┐
   浏览器 ──────► │  frontend (nginx:alpine) :8080              │
                 │   - 托管 Vite SPA (frontend/dist)            │
                 │   - 反代 /api,/ws -> backend:9000            │
                 └───────────────┬─────────────────────────────┘
                                 │  /api /ws (proxy)
                 ┌───────────────▼─────────────────────────────┐
                 │  backend (python:3.12-slim) :9000           │
                 │   bridge_service.py                         │
                 │   - API 路由 + 内嵌 SPA 回退(/)              │
                 │   - 预测引擎 (torch 反序列化 joblib)         │
                 │   - /health 健康检查                         │
                 └───────┬───────────────────────┬─────────────┘
                         │ 读/写 SQLite           │ 实时拉取乐鱼
                 ┌───────▼─────────┐      ┌───────▼──────────────┐
                 │ data/           │      │ collector            │
                 │  GQ.db(8GB)     │      │ gq/auto_collector.py │
                 │  football_data   │      │ (前台, Docker 托管)  │
                 │  .db(661MB)      │      └─────────────────────┘
                 └─────────────────┘

Windows 原生模式(无 Docker):
   deploy/windows/daemon_guard.py 常驻 -> Popen DETACHED 自举
   bridge_service.py + gq/auto_collector.py (双进程守护 + 冻结自动重启)
   注册为 Task Scheduler "ShaoXiangAIGuard" 随系统启动
```

---

## 2. 文件清单

| 文件                                   | 作用                                                   |
| ------------------------------------ | ---------------------------------------------------- |
| `deploy/requirements.prod.txt`       | 生产依赖（剔除 PySide6 桌面 GUI，保留 torch 预测能力）                |
| `deploy/backend.Dockerfile`          | 后端镜像：python:3.12-slim，仅拷贝存在的运行时目录                    |
| `deploy/frontend.Dockerfile`         | 前端镜像：node:20 构建 → nginx:alpine 托管 SPA + 反代           |
| `deploy/nginx.conf`                  | 前端 nginx：SPA 回退 + `/api`、`/ws` 反代到 backend:9000      |
| `deploy/docker-compose.yml`          | 三服务编排（backend/frontend/collector）+ 数据卷 + 日志轮转 + 健康检查 |
| `deploy/collector_healthcheck.py`    | 采集器存活探针（GQ.db 写新鲜度，供 compose healthcheck 复用）         |
| `deploy/windows/daemon_guard.py`     | Windows 进程守护：Popen DETACHED 自举 + /health 冻结检测 + 日志轮转 |
| `deploy/windows/install_guard.bat`   | 注册 Task Scheduler 任务（系统启动自动运行，管理员运行）                 |
| `deploy/windows/uninstall_guard.bat` | 卸载任务并精准停止守护及其托管进程                                    |
| `deploy/.env.example`                | 环境变量模板（无真实密钥；复制为 `deploy/.env`）                      |
| `deploy/ci.yml`                      | CI/CD 草稿（替换 `.github/workflows/ci.yml`，新增部署冒烟测试）     |

> 注：根目录已有的 `Dockerfile` / `docker-compose.yml` / `.github/workflows/ci.yml` 维持原状；本套件是更完整的独立交付，二者可并存。根 `Dockerfile` 已改为引用 `deploy/requirements.prod.txt`（剔除 PySide6），使原有单镜像构建与 CI `build` 作业恢复正常。

---

## 3. Docker 部署

### 3.1 准备

```bash
cp deploy/.env.example deploy/.env
# 编辑 deploy/.env: 填入 SECRET_KEY / ADMIN_PASSWORD / GQ_REQUEST_ID
```

### 3.2 构建并启动

```bash
# 在仓库根目录执行
docker compose -f deploy/docker-compose.yml up -d --build
```

- 后端 API：`http://<host>:9000/health`
- 前端 SPA：`http://<host>:8080/`

### 3.3 验证

```bash
curl -sf http://localhost:9000/health && echo "backend OK"
curl -sf http://localhost:8080/        >/dev/null && echo "frontend OK"
docker compose -f deploy/docker-compose.yml ps   # 三服务 healthy
```

### 3.4 日志轮转

compose 各服务 `logging.max-size=50m, max-file=10`，由 Docker 守护进程原生轮转，无需额外配置。

---

## 4. Windows 原生部署（无 Docker）

适合本机单机常驻，复用现成的 `.venv` shim（系统 Python312 无 fastapi，靠 venv 注入 site-packages）。

### 4.1 准备 venv

```bat
python -m venv .venv
.venv\Scripts\pip install -r deploy\requirements.prod.txt
```

### 4.2 安装守护（管理员运行）

```bat
deploy\windows\install_guard.bat
```

- 注册 Task Scheduler 任务 `ShaoXiangAIGuard`，系统启动自动运行
- 守护自动 Popen DETACHED 拉起 `bridge_service.py` 与 `gq/auto_collector.py`
- 每 15s 巡检：`/health` 超时(8s)连续 2 次 → 判冻结重启；`GQ.db` 10 分钟未更新 → 判采集器假死重启
- 子进程 stdout/stderr 经管道泵入按大小轮转日志（默认 20MB × 5），**根治历史 logs 膨胀 1.2GB**

### 4.3 停止 / 卸载

```bat
deploy\windows\uninstall_guard.bat
```

### 4.4 日志位置

`logs/daemon_guard.log`（守护自身，RotatingFileHandler）、`logs/bridge.out.log`、`logs/bridge.err.log`、`logs/auto_collector.out.log` 等（均按大小轮转）。

---

## 5. 回滚

所有改动要么是新文件（`deploy/`），要么是根 `Dockerfile` 一处安全修订（已 git 跟踪）。回滚方式：

```bash
git checkout -- Dockerfile            # 若需还原根 Dockerfile
git clean -fd deploy                  # 移除整套 deploy 交付物
```

Docker 镜像按 tag 管理，回滚只需 `docker compose up -d` 指定上一版 tag。

---

## 6. 已知约束 / 后续

- 当前为 SQLite 单文件，宿主挂载 `./data`；中期可平滑迁移 Postgres（根 `docker-compose.yml` 已预留注释模板）。
- 镜像体积：默认 `torch` 走 PyPI CUDA 构建（CPU 推理）。如需更小镜像，可在 `backend.Dockerfile` 先 `pip install torch --index-url https://download.pytorch.org/whl/cpu` 再装其余依赖。
- 采集器依赖 `GQ_REQUEST_ID`（乐鱼令牌）；令牌过期需更新 `deploy/.env` 并重启 collector 容器 / 守护。
