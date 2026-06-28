"""
训练 API — 触发训练/训练状态/训练历史
"""
import asyncio
import logging
import sqlite3
import sqlalchemy
from typing import Optional, Dict, Any
from fastapi import APIRouter, HTTPException, Query, Depends, BackgroundTasks
from pydantic import BaseModel, Field

from api.deps import get_admin_user

logger = logging.getLogger(__name__)
router = APIRouter()

class TrainingRequest(BaseModel):
    data_source: str = Field("latest", description="latest/db/all")
    n_estimators: int = Field(1000, ge=100, le=5000)
    force_retrain: bool = False
    description: Optional[str] = None

class TrainingStatus(BaseModel):
    task_id: Optional[str] = None
    status: str = Field(default="idle", description="idle/running/completed/failed")
    progress: float = Field(0.0, ge=0.0, le=1.0)
    message: Optional[str] = None
    metrics: Optional[Dict[str, Any]] = None

# 全局状态 + 并发锁（防止竞态条件）
_training_status = TrainingStatus(status="idle")
_training_lock = asyncio.Lock()

@router.post("/start")
async def start_training(
    req: TrainingRequest,
    background_tasks: BackgroundTasks,
    user: dict = Depends(get_admin_user),
):
    """触发模型训练（后台异步）"""
    global _training_status
    async with _training_lock:
        if _training_status.status == "running":
            raise HTTPException(status_code=409, detail="训练已在运行中")
        _training_status = TrainingStatus(status="running", progress=0.0, message="正在初始化...")

    background_tasks.add_task(_run_training, req)
    return {"status": "accepted", "message": "训练已启动"}

@router.get("/status", response_model=TrainingStatus)
async def get_training_status(
    user: dict = Depends(get_admin_user),
):
    """获取训练状态"""
    return _training_status

@router.get("/history")
async def get_training_history(
    limit: int = Query(20, ge=1, le=100),
    user: dict = Depends(get_admin_user),
):
    """获取训练历史"""
    try:
        from services.model_service import ModelService
        svc = ModelService()
        return svc.get_training_history(limit)
    except HTTPException:
        raise
    except (ValueError, KeyError, FileNotFoundError) as e:
        logger.error(f"获取训练历史失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/celery")
async def trigger_celery_training(
    req: TrainingRequest,
    user: dict = Depends(get_admin_user),
):
    """通过 Celery 异步训练"""
    try:
        from tasks.training_tasks import train_model_task
        task = train_model_task.delay(
            data_source=req.data_source,
            n_estimators=req.n_estimators,
            force_retrain=req.force_retrain,
            description=req.description,
        )
        return {"status": "accepted", "task_id": task.id}
    except ImportError:
        raise HTTPException(status_code=501, detail="Celery 未配置")
    except (ValueError, KeyError, FileNotFoundError) as e:
        raise HTTPException(status_code=500, detail=str(e))

async def _run_training(req: TrainingRequest):
    """后台训练执行器"""
    global _training_status
    try:
        _training_status = TrainingStatus(status="running", progress=0.1, message="加载数据...")
        import sys, os
        from core.config import settings

        _training_status = TrainingStatus(status="running", progress=0.3, message="训练中...")

        # 调用现有训练流水线
        from training.training_pipeline import TrainingPipeline
        from optimize.model_registry import ModelRegistry

        registry = ModelRegistry()
        pipeline = TrainingPipeline(registry)
        result = pipeline.run_training_pipeline(
            data_source=req.data_source,
            n_estimators=req.n_estimators,
            description=req.description,
        )
        if not result:
            raise RuntimeError("训练流水线返回空结果")

        async with _training_lock:
            _training_status = TrainingStatus(
                status="completed",
                progress=1.0,
                message=f"训练完成 v{result.get('version', '?')}",
                metrics=result.get("metrics"),
            )
    except (sqlite3.Error, sqlalchemy.exc.SQLAlchemyError) as e:
        async with _training_lock:
            _training_status = TrainingStatus(
                status="failed",
                progress=0.0,
                message=str(e),
            )
    except Exception as e:
        async with _training_lock:
            _training_status = TrainingStatus(
                status="failed",
                progress=0.0,
                message=f"训练失败: {e}",
            )
