# =============================================================================
# 哨响AI - 后端镜像 (单镜像, 仅 API, 不内嵌前端)
# =============================================================================
# 用法:
#   docker build -f deploy/backend.Dockerfile -t shaoxiang-ai-backend:latest .
# 设计要点:
#   - python:3.12-slim (与本地 .venv 3.12 对齐)
#   - 安装 deploy/requirements.prod.txt (已剔除 PySide6, 保留 torch)
#   - 仅拷贝实际存在的运行时目录 (pipeline/models/gq/scripts/...)
#   - libgomp1 为 lightgbm/xgboost 的 OpenMP 运行库必需
#   - HEALTHCHECK 命中 /health (与 bridge_service.py:1033 契约一致)
#   - 前端由独立 deploy/frontend.Dockerfile + nginx 提供
# =============================================================================
FROM python:3.12-slim

LABEL org.opencontainers.image.title="哨响AI-backend"
LABEL org.opencontainers.image.description="足球赔率破解/量化预测 API 服务"

# 系统依赖: gcc/g++ 编译 numpy/scipy 轮子, libgomp1 为 lightgbm/xgboost OpenMP 必需,
#           curl 供 HEALTHCHECK 使用
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ libgomp1 curl \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CUDA_VISIBLE_DEVICES="" \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# ── 依赖 (先拷需求文件, 利用层缓存) ──
COPY deploy/requirements.prod.txt ./requirements.prod.txt
RUN pip install --upgrade pip \
    && pip install -r requirements.prod.txt

# ── 运行时代码 (仅拷贝实际存在的目录, 防止 COPY 不存在目录导致构建失败) ──
COPY bridge_service.py ./bridge_service.py
COPY pipeline/ ./pipeline/
COPY models/ ./models/
COPY bookmaker_sim/ ./bookmaker_sim/
COPY config/ ./config/
COPY data_collector/ ./data_collector/
COPY scripts/ ./scripts/
COPY gq/ ./gq/

# 运行期挂载点 (SQLite 数据库/日志/模型/报告)
RUN mkdir -p /app/data /app/saved_models /app/logs /app/reports /app/odds_db

EXPOSE 9000

HEALTHCHECK --interval=30s --timeout=10s --start-period=45s --retries=3 \
    CMD curl -sf http://localhost:9000/health || exit 1

# bridge_service.py 读取 API_HOST 环境变量 (默认 0.0.0.0), 端口 9000
CMD ["python", "bridge_service.py"]
