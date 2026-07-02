"""
杂项 HTTP 端点 — 路由归一至 api/v1/endpoints/ (2026-06-28)
========================================
迁移说明: 原 backend/routers/ → 统一至 api/v1/endpoints/
"""
import logging
from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse
import os

logger = logging.getLogger(__name__)
router = APIRouter(tags=["misc"])

@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def dashboard():
    """哨响AI v5.0 — AI 对话界面"""
    _chat_html = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        'static', 'conversation.html'
    )
    if os.path.exists(_chat_html):
        with open(_chat_html, 'r', encoding='utf-8') as f:
            return f.read()
    return "<h2>哨响AI v5.0</h2><p>对话界面文件未找到</p>"

@router.get("/generate.html", include_in_schema=False)
async def generate_legacy():
    """旧 generate.html — 前端已删除"""
    return {"detail": "前端已删除，请使用 /docs API文档"}

@router.get("/api/monitor/health")
async def health_legacy():
    """兼容旧版 Flask API 的健康检查端点"""
    from api.v1.endpoints.monitor import health_check
    return await health_check()
