"""
Celery 配置 — 异步任务队列
"""
import sys
import os
from celery import Celery
from core.config import settings

# 确保项目根目录可导入

celery_app = Celery(
    "footballAI",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=[
        "tasks.training_tasks",
        "tasks.sync_tasks",  # ⚡ P1优化: 添加后台同步任务
    ],
)

# Celery 配置
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Asia/Shanghai",
    enable_utc=True,
    task_track_started=True,
    task_time_limit=3600,       # 1小时超时
    task_soft_time_limit=3000,  # 50分钟软超时
    worker_max_tasks_per_child=10,
    worker_prefetch_multiplier=1,
)
