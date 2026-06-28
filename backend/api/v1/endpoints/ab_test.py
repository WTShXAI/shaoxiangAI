"""
A/B测试 API 端点
"""
from typing import Optional, Dict, List
from fastapi import APIRouter, HTTPException, Query, Depends
from pydantic import BaseModel

from core.security import require_admin
from services.ab_test_service import get_ab_test_service, ABTestConfig

router = APIRouter()

class CreateTestRequest(BaseModel):
    name: str
    variants: Dict[str, str]  # {variant_name: model_id}
    traffic_split: Optional[Dict[str, float]] = None
    metrics: Optional[List[str]] = None

class RecordResultRequest(BaseModel):
    test_name: str
    variant: str
    is_correct: bool
    confidence: float = 0.0

@router.post("/tests")
async def create_test(req: CreateTestRequest, _: None = Depends(require_admin)):
    """创建A/B测试"""
    svc = get_ab_test_service()
    cfg = svc.create_test(req.name, req.variants, req.traffic_split, req.metrics)
    return {
        "name": cfg.name,
        "variants": cfg.variants,
        "traffic_split": cfg.traffic_split,
        "status": cfg.status,
    }

@router.get("/tests")
async def list_tests():
    """列出所有A/B测试"""
    svc = get_ab_test_service()
    return {"tests": svc.list_tests()}

@router.get("/tests/{test_name}")
async def get_test_results(test_name: str):
    """获取A/B测试结果"""
    svc = get_ab_test_service()
    result = svc.get_results(test_name)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result

@router.post("/tests/{test_name}/stop")
async def stop_test(test_name: str, _: None = Depends(require_admin)):
    """停止A/B测试"""
    svc = get_ab_test_service()
    result = svc.stop_test(test_name)
    return {"status": "stopped", "final_results": result}

@router.post("/record")
async def record_result(req: RecordResultRequest):
    """记录一次预测结果（用于A/B测试统计）"""
    svc = get_ab_test_service()
    svc.record_result(req.test_name, req.variant, req.is_correct, req.confidence)
    return {"status": "ok"}

@router.get("/variant")
async def get_variant(test_name: str, user_id: str = Query(...)):
    """获取用户的实验变体"""
    svc = get_ab_test_service()
    variant = svc.get_variant(test_name, user_id)
    if variant is None:
        raise HTTPException(status_code=404, detail="测试不存在或已停止")
    return {"test_name": test_name, "variant": variant}
