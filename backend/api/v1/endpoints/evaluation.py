"""
哨响AI — 评估管道 API
======================
GET  /api/v1/evaluation/latest     → 最近一次评估结果
GET  /api/v1/evaluation/history    → 评估历史列表
POST /api/v1/evaluation/run        → 手动触发全链路评估
GET  /api/v1/evaluation/status     → 评估系统健康状态
"""

import os
import sys
import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

router = APIRouter()

# project root
_PROJECT_ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..")

def _db_path() -> str:
    return os.path.join(_PROJECT_ROOT, "data", "football_data.db")

def _get_eval_pipeline():
    """延迟导入 EvaluationPipeline，避免首次加载时 db_path 缺失"""
    sys.path.insert(0, _PROJECT_ROOT)
    from agents.evaluator.evaluation_pipeline import EvaluationPipeline
    return EvaluationPipeline(db_path=_db_path())


# ══════════════════════════════════════════════════════
# GET /evaluation/latest
# ══════════════════════════════════════════════════════

@router.get("/latest")
async def get_latest_evaluation():
    """获取最近一次评估报告"""
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM evaluation_runs ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    conn.close()

    if not row:
        return {"available": False, "message": "尚无评估记录，请运行 POST /evaluation/run"}

    d = dict(row)
    # 反序列化 JSON 字段
    for key in list(d.keys()):
        if key.endswith("_full_json"):
            try:
                d[key] = json.loads(d[key]) if d[key] else None
            except (json.JSONDecodeError, TypeError):
                pass
    try:
        d["summary_json"] = json.loads(d["summary_json"]) if d.get("summary_json") else None
    except (json.JSONDecodeError, TypeError):
        pass

    return d


# ══════════════════════════════════════════════════════
# GET /evaluation/history
# ══════════════════════════════════════════════════════

@router.get("/history")
async def get_evaluation_history(limit: int = Query(20, ge=1, le=100)):
    """获取评估历史"""
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT run_id, run_timestamp, trigger_type, experts_run, sample_size,
                  overall_score, overall_rating, urgency
           FROM evaluation_runs ORDER BY run_id DESC LIMIT ?""",
        (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ══════════════════════════════════════════════════════
# POST /evaluation/run
# ══════════════════════════════════════════════════════

@router.post("/run")
async def run_evaluation(
    experts: Optional[str] = Query(None, description="逗号分隔，默认E1-E7全部"),
    force: bool = Query(False, description="强制运行，即使无新数据"),
):
    """手动触发全链路评估"""
    try:
        pipeline = _get_eval_pipeline()
        expert_list = experts.split(",") if experts else None
        result = pipeline.run(
            experts=expert_list,
            trigger_type="manual",
            skip_if_unchanged=not force,
        )
        return result
    except (ValueError, KeyError, FileNotFoundError) as e:
        raise HTTPException(status_code=500, detail=f"评估执行失败: {e}")


# ══════════════════════════════════════════════════════
# GET /evaluation/status
# ══════════════════════════════════════════════════════

@router.get("/status")
async def get_evaluation_status():
    """评估系统健康状态"""
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row

    last = conn.execute(
        "SELECT run_timestamp, overall_score, overall_rating, urgency FROM evaluation_runs ORDER BY run_id DESC LIMIT 1"
    ).fetchone()

    total = conn.execute("SELECT COUNT(*) as cnt FROM evaluation_runs").fetchone()["cnt"]

    # 检查预测表有多少笔可评估数据
    pred_count = conn.execute(
        "SELECT COUNT(*) as cnt FROM predictions WHERE actual_result IS NOT NULL AND is_correct IS NOT NULL"
    ).fetchone()["cnt"]

    conn.close()

    return {
        "available": total > 0,
        "total_runs": total,
        "last_run": dict(last) if last else None,
        "evaluable_predictions": pred_count,
    }
