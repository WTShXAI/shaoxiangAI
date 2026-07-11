# 哨响AI v7.1 — 多阶段构建 Docker 镜像
# =============================================
# Stage 1: 前端构建 (Node 22 Slim)
# Stage 2: 后端运行 (Python 3.13 Slim)

# ── Stage 1: 前端构建 ──────────────────────
FROM node:22-slim AS frontend-build
WORKDIR /app/frontend

COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci 2>&1 || npm install 2>&1
RUN npm install @rollup/rollup-linux-x64-gnu --no-save 2>&1 || echo "rollup-linux skip"

COPY frontend/ ./
RUN npm run build 2>&1
RUN test -f dist/index.html || (echo "❌ 前端构建失败: dist/index.html 不存在" && exit 1)

# ── Stage 2: 后端运行 ──────────────────────
FROM python:3.13-slim

LABEL org.opencontainers.image.title="哨响AI"
LABEL org.opencontainers.image.version="7.1"
LABEL org.opencontainers.image.description="智能足球预测系统"

WORKDIR /app

# 系统依赖: gcc 是 numpy/scipy 编译需要, libgomp1 是 lightgbm/xgboost 需要
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ libgomp1 curl \
    && rm -rf /var/lib/apt/lists/*

# ── Python 依赖 ──
COPY requirements.txt .
RUN pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com -r requirements.txt 2>&1

# 复制运行时目录 (仅保留实际存在的目录, 2026-07-11 审计)
COPY pipeline/ ./pipeline/
COPY models/ ./models/
COPY bookmaker_sim/ ./bookmaker_sim/
COPY config/ ./config/
COPY data_collector/ ./data_collector/
COPY scripts/ ./scripts/
COPY bridge_service.py ./

# 复制前端构建产物
COPY --from=frontend-build /app/frontend/dist ./frontend/dist
RUN test -f frontend/dist/index.html || (echo "❌ 前端产物丢失" && exit 1)

# 数据目录 (挂载点)
RUN mkdir -p /app/data /app/saved_models /app/logs /app/reports /app/odds_db

# ── 环境变量 ──────────────────────────────
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV CUDA_VISIBLE_DEVICES=""

EXPOSE 9000

HEALTHCHECK --interval=30s --timeout=10s --start-period=45s --retries=3 \
    CMD curl -sf http://localhost:9000/health || exit 1

CMD ["python", "bridge_service.py"]
