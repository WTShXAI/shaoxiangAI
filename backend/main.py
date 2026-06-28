"""
哨响AI (ShXAI) - FastAPI 主应用入口 v5.0
==========================================
2026-06-28: God File 拆分 — 路由/服务移至 routers/ + services/

功能概述:
  - D-Gate v5.0 多维度平局检测系统 (四模式: A/B/C/D)
  - 操盘手模拟集成 (BookmakerTrapDetector 16引擎)
  - 世界杯/杯赛高平局率自适应阈值
  - 统一后端架构: FastAPI (原生路由) + Flask (WSGI 兼容层)
  - 微服务组件: SQLAlchemy + Celery + MLflow + Prometheus

Runtime 修复: RTX 5070 Ti CUDA sm_120 不兼容 → 强制 CPU 模式
"""
import os as _os
_os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")
import sys
import os
import json as _json_module
from datetime import datetime, timezone
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import time
import logging
import uuid
import contextvars
from logging.handlers import RotatingFileHandler
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import HTMLResponse
from starlette.websockets import WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, FileResponse
from a2wsgi import WSGIMiddleware

from core.config import settings
from core.database import engine, Base

# ── request_id 上下文 ─────────────────────
_request_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")

def _get_request_id() -> str:
    return _request_id_ctx.get()

class JsonFormatter(logging.Formatter):
    """JSON 结构化日志格式化器"""
    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "name": record.name,
            "message": record.getMessage(),
            "request_id": _get_request_id(),
        }
        if record.exc_info and record.exc_info[0]:
            log_entry["exception"] = self.formatException(record.exc_info)
        return _json_module.dumps(log_entry, ensure_ascii=False)

class RequestIdFilter(logging.Filter):
    """将 request_id 注入日志记录 (兼容非 JSON handler)"""
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _get_request_id() or "-"
        return True

# ── 日志 ──────────────────────────────────
_log_dir = os.path.join(_project_root, "logs")
os.makedirs(_log_dir, exist_ok=True)

# 文件 handler — JSON 格式
_file_handler = RotatingFileHandler(
    filename=os.path.join(_log_dir, "app.log"),
    maxBytes=10 * 1024 * 1024,  # 10MB
    backupCount=5,
    encoding="utf-8",
)
_file_handler.setFormatter(JsonFormatter())

# 控制台 handler — 文本格式
_console_handler = logging.StreamHandler()
_console_handler.setFormatter(
    logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] [%(request_id)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
)

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL),
    handlers=[_file_handler, _console_handler],
)
_root_logger = logging.getLogger()
_root_logger.addFilter(RequestIdFilter())

logger = logging.getLogger(__name__)
logger.info(f"结构化日志已配置: {os.path.join(_log_dir, 'app.log')} (JSON/10MB轮转/保留5份)")

# ── P1-2: FIFA 排名数据加载 ──────────────
_FIFA_RANKINGS = {}
try:
    import json as _json_rank
    _rank_path = os.path.join(_project_root, 'config', 'fifa_rankings_2026.json')
    if os.path.exists(_rank_path):
        with open(_rank_path, 'r', encoding='utf-8') as _rf:
            _rank_data = _json_rank.load(_rf)
            _FIFA_RANKINGS = {k: v for k, v in _rank_data.items() if not k.startswith('_')}
        logger.info(f"[P1-2] FIFA排名加载: {len(_FIFA_RANKINGS)}支球队")
except Exception as _re:
    logger.warning(f"[P1-2] FIFA排名加载失败: {_re}")

def _get_fifa_rank_diff(home: str, away: str) -> int:
    """查询两队FIFA排名差 (abs值), 找不到返回None"""
    if not _FIFA_RANKINGS or not home or not away:
        return None
    r_h = _FIFA_RANKINGS.get(home) or _FIFA_RANKINGS.get(home.strip())
    r_a = _FIFA_RANKINGS.get(away) or _FIFA_RANKINGS.get(away.strip())
    if r_h is None or r_a is None:
        return None
    return abs(r_h - r_a)

# ── P3: D-Gate 统一引擎 ──────────────────
from rules.d_gate_engine import apply_dgate, detect_match_type

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期 — 统一 FastAPI + Flask legacy 启动"""
    logger.info(f"[启动] {settings.APP_NAME} v{settings.APP_VERSION} 正在初始化...")
    logger.info(f"   数据库: {settings.DATABASE_URL}")
    logger.info(f"   模型目录: {settings.MODEL_DIR}")
    try:
        from core.model_registry_helper import get_active_model_version
        logger.info(f"   活跃模型版本: {get_active_model_version()}")
    except (ImportError, AttributeError):
        pass
    try:
        from core.security import _init_default_user
        _init_default_user()
        logger.info("   用户模块初始化完成")
    except (ValueError, KeyError, FileNotFoundError) as e:
        logger.warning(f"   用户初始化失败: {e}")
    try:
        from utils.metrics_exporter import get_metrics_exporter
        get_metrics_exporter().start()
        logger.info("   Prometheus 指标导出已启动")
    except (ValueError, KeyError, FileNotFoundError) as e:
        logger.warning(f"   Prometheus 指标导出失败: {e}")
    import threading
    flask_init = threading.Thread(target=_init_flask_startup, daemon=True)
    flask_init.start()
    yield
    logger.info("👋 应用关闭中...")
    from core.database import engine
    engine.dispose()

def _init_flask_startup():
    try:
        from flask_bridge import run_flask_startup
        run_flask_startup()
    except (ValueError, KeyError, FileNotFoundError) as e:
        logger.error(f"Flask startup 失败: {e}")

# ── 创建应用 ──────────────────────────────
app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="智能足球预测决策系统 — 微服务架构",
    docs_url=f"{settings.API_V1_PREFIX}/docs",
    redoc_url=f"{settings.API_V1_PREFIX}/redoc",
    openapi_url=f"{settings.API_V1_PREFIX}/openapi.json",
    lifespan=lifespan,
)

# ── 中间件 ────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-Request-ID"],
)
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=["*"] if settings.DEBUG else ["localhost", "127.0.0.1"],
)

@app.middleware("http")
async def add_request_id_and_process_time(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
    _request_id_ctx.set(request_id)
    start_time = time.time()
    response = await call_next(request)
    process_time = time.time() - start_time
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Process-Time"] = str(round(process_time, 4))
    return response

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"未处理异常: {exc}", exc_info=True)
    if settings.DEBUG:
        return JSONResponse(status_code=500, content={"detail": str(exc), "path": str(request.url)})
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})

# ── 路由注册 ──────────────────────────────
from api.v1.router import api_router
app.include_router(api_router, prefix=settings.API_V1_PREFIX)

# OCR 端点 (动态加载)
try:
    import importlib.util
    _ocr_spec = importlib.util.spec_from_file_location(
        'api_ocr', os.path.join(_project_root, 'api', 'ocr.py'))
    _ocr_mod = importlib.util.module_from_spec(_ocr_spec)
    sys.modules['api_ocr'] = _ocr_mod
    _ocr_spec.loader.exec_module(_ocr_mod)
    app.include_router(_ocr_mod.ocr_router)
    logger.info("OCR routes registered: POST /api/v1/ocr/upload")
except Exception as e:
    logger.warning(f"OCR routes not available: {e}")

# ── 聊天/赛程/图片/JEPA/杂项 端点 (从 routers/ 拆出) ──
import json as _json, asyncio as _asyncio, re as _re
from backend.routers.chat import router as chat_router
from backend.routers.fixtures import router as fixtures_router
from backend.routers.predict_image import router as predict_image_router
from backend.routers.jepa import router as jepa_router
from backend.routers.misc import router as misc_router
app.include_router(chat_router, prefix="/api/v1")
app.include_router(fixtures_router, prefix="/api/v1")
app.include_router(predict_image_router, prefix="/api/v1")
app.include_router(jepa_router, prefix="/api/v1")
app.include_router(misc_router)

logger.info("Chat routes: POST /api/v1/chat, GET /api/v1/chat/health")

# ── 静态文件挂载 ──────────────────────────
_static_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'static')
if not os.path.exists(_static_dir):
    os.makedirs(_static_dir, exist_ok=True)
try:
    from fastapi.staticfiles import StaticFiles
    app.mount("/static", StaticFiles(directory=_static_dir), name="static")
    @app.get("/chat")
    async def chat_page():
        from fastapi.responses import FileResponse
        chat_html = os.path.join(_static_dir, 'conversation.html')
        if os.path.exists(chat_html):
            return FileResponse(chat_html)
        return {"message": "conversation.html not found in static/"}
    logger.info(f"Static files mounted: /static -> {_static_dir}")
except ImportError:
    logger.info("StaticFiles not available")

# ── WebSocket 健康推送 ───────────────────
@app.websocket("/ws/health")
async def websocket_health(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            try:
                from modules.auto_optimizer import get_optimizer
                opt = get_optimizer()
                status = opt.status_summary()
                await websocket.send_text(_json.dumps({
                    "type": "health_update", "timestamp": datetime.now(timezone.utc).isoformat(),
                    "health": status["health"], "performance": status["performance"]["current"],
                    "trend": status["performance"]["trend"]["direction"], "advice": status["health_advice"],
                }))
            except Exception as e:
                await websocket.send_text(_json.dumps({"type":"health_update","health":"unknown","error":str(e)}))
            await _asyncio.sleep(30)
    except WebSocketDisconnect:
        pass

# ── Prometheus 指标端点 ──────────────────
@app.get("/metrics")
async def metrics():
    try:
        from utils.metrics_exporter import get_metrics_exporter
        exporter = get_metrics_exporter()
        return exporter.render()
    except ImportError:
        return JSONResponse(status_code=501, content={"error": "metrics_exporter not installed"})

# ── Flask Legacy WSGI 挂载 ────────────────
try:
    from flask_bridge import get_flask_app
    flask_wsgi = get_flask_app()
    app.mount("/", WSGIMiddleware(flask_wsgi))
    logger.info("[挂载] Flask legacy API 已成功挂载 (WSGI 兼容层)")
except ImportError as e:
    logger.warning(f"[警告] Flask legacy API 未挂载: {e}")
except (ValueError, KeyError, FileNotFoundError) as e:
    logger.error(f"[错误] Flask WSGI 挂载失败: {e}")

# ── 启动 ──────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.DEBUG,
        log_level=settings.LOG_LEVEL.lower(),
    )
