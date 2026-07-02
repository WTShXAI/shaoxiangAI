"""
监控 API — 健康检查/系统状态/Prometheus指标
"""
import sys
import os
import time
from datetime import datetime, timezone
from typing import Dict, Any
from fastapi import APIRouter, Response
from backend.core.config import settings

router = APIRouter()

START_TIME = time.time()

@router.get("/health")
async def health_check():
    """基础健康检查（含前端 SystemHealth 兼容字段）"""
    uptime = time.time() - START_TIME

    # 数据库检查
    db_health = "healthy"
    try:
        from core.database import engine
        engine.connect().close()
    except Exception:
        db_health = "down"

    # 模型检查
    model_health = "healthy"
    try:
        from services.model_service import ModelService
        svc = ModelService()
        if not svc.get_current_model_path():
            model_health = "degraded"
    except Exception:
        model_health = "down"

    # CPU / 内存
    cpu_pct = 0.0
    mem_pct = 0.0
    try:
        import psutil
        cpu_pct = round(psutil.cpu_percent(interval=0.1), 1)
        mem_pct = round(psutil.virtual_memory().percent, 1)
    except Exception:
        pass

    overall = "healthy"
    if db_health == "down" or model_health == "down":
        overall = "down"
    elif model_health == "degraded":
        overall = "degraded"

    return {
        # 原始字段
        "status": overall,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "uptime_seconds": round(uptime, 1),
        "version": settings.APP_VERSION,
        # 前端 SystemHealth 兼容字段
        "uptime": round(uptime),
        "apiLatency": 0,
        "predictionLatency": 0,
        "modelHealth": model_health,
        "databaseHealth": db_health,
        "memoryUsage": mem_pct,
        "cpuUsage": cpu_pct,
    }

@router.get("/health/ready")
async def readiness_check():
    """就绪检查（含数据库）"""
    checks = {
        "api": True,
        "database": False,
        "model": False,
    }
    try:
        from core.database import engine
        engine.connect().close()
        checks["database"] = True
    except (OSError, ValueError, KeyError) as e:
        logger.debug(f"操作失败: {e}")

    try:
        from services.model_service import ModelService
        svc = ModelService()
        if svc.get_current_model_path():
            checks["model"] = True
    except (OSError, ValueError, KeyError) as e:
        logger.debug(f"操作失败: {e}")

    all_ready = all(checks.values())
    status_code = 200 if all_ready else 503
    return Response(
        content=str({"status": "ready" if all_ready else "not_ready", "checks": checks}),
        status_code=status_code,
        media_type="application/json",
    )

@router.get("/health/live")
async def liveness_check():
    """存活检查（轻量级）"""
    return {"status": "alive", "timestamp": datetime.now(timezone.utc).isoformat()}

@router.get("/model-health")
async def model_health():
    """模型健康检查"""
    try:
        from services.model_service import ModelService
        svc = ModelService()
        info = svc.get_current_info()
        return {
            "status": "ok" if info else "no_model",
            "model_info": info,
        }
    except (ValueError, KeyError, FileNotFoundError) as e:
        return {"status": "error", "error": str(e)}

@router.get("/system")
async def system_info():
    """系统资源信息"""
    try:
        import psutil
        mem = psutil.virtual_memory()
        cpu = psutil.cpu_percent(interval=0.5)
        disk = psutil.disk_usage("/" if os.name != "nt" else "C:\\")
        return {
            "cpu_percent": cpu,
            "memory": {
                "total_gb": round(mem.total / (1024**3), 2),
                "available_gb": round(mem.available / (1024**3), 2),
                "percent": mem.percent,
            },
            "disk": {
                "total_gb": round(disk.total / (1024**3), 2),
                "free_gb": round(disk.free / (1024**3), 2),
                "percent": disk.percent,
            },
            "python_version": sys.version,
        }
    except ImportError:
        return {"status": "psutil not installed"}

@router.get("/metrics/summary")
async def metrics_summary():
    """监控指标摘要（Prometheus 网关数据）"""
    try:
        from utils.metrics_exporter import get_metrics_exporter
        exporter = get_metrics_exporter()
        if hasattr(exporter, 'get_summary'):
            return exporter.get_summary()
        return {"predictions_total": 0, "accuracy": 0, "uptime_hours": 0, "status": "metrics_collector_not_initialized"}
    except (ImportError, Exception):
        return {"predictions_total": 0, "accuracy": 0, "uptime_hours": 0, "status": "metrics_unavailable"}
