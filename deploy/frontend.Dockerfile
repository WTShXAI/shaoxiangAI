# =============================================================================
# 哨响AI - 前端镜像 (Vite build -> nginx:alpine 托管 SPA)
# =============================================================================
# 用法:
#   docker build -f deploy/frontend.Dockerfile -t shaoxiang-ai-frontend:latest .
# 设计要点:
#   - Stage 1: node:20-slim 跑 npm ci + vite build -> frontend/dist
#   - Stage 2: nginx:alpine 托管 dist, 并反代 /api 与 /ws 到 backend:9000
#   - SPA history 回退 (try_files) 保证前端路由刷新不 404
#   - nginx.conf 见 deploy/nginx.conf
# 注意: 前端在构建期通过 VITE_API_BASE 注入后端地址 (默认同域 /api)
# =============================================================================
FROM node:20-slim AS frontend-build

WORKDIR /app/frontend

# 优先 npm ci (需 package-lock.json), 失败回退 npm install
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci 2>&1 || npm install 2>&1

# rollup 在 linux x64 需要原生二进制 (alpine/musl 镜像之外, slim/debian 用 gnu)
RUN npm install @rollup/rollup-linux-x64-gnu --no-save 2>&1 || echo "rollup-linux skip"

COPY frontend/ ./
RUN npm run build 2>&1 \
    && test -f dist/index.html || (echo "ERROR: frontend dist/index.html missing" && exit 1)

# ── Stage 2: nginx 托管 ──
FROM nginx:1.27-alpine

# 移除默认配置, 注入自定义 SPA + 反代配置
RUN rm -f /etc/nginx/conf.d/default.conf
COPY deploy/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=frontend-build /app/frontend/dist /usr/share/nginx/html

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -qO- http://localhost/ >/dev/null 2>&1 || exit 1

CMD ["nginx", "-g", "daemon off;"]
