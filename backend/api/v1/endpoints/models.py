"""
模型管理 API — 版本管理/部署/对比/回滚
"""
import logging
import requests
from typing import List, Optional, Dict, Any
from fastapi import APIRouter, HTTPException, Query, Depends
from pydantic import BaseModel, Field

from api.deps import get_admin_user
from services.model_service import ModelService

logger = logging.getLogger(__name__)
router = APIRouter()

# ── 响应模型 ──────────────────────────────

class ModelInfo(BaseModel):
    model_id: str
    model_type: str
    version: Optional[str] = None
    model_hash: Optional[str] = None
    status: str
    metrics: Dict[str, Any]
    registered_at: Optional[str] = None
    is_production: bool = False
    description: Optional[str] = None

class VersionComparison(BaseModel):
    model_a: str
    model_b: str
    semver_a: Optional[str]
    semver_b: Optional[str]
    diff: Dict[str, Any]
    verdict: str
    hash_changed: bool
    training_data_changed: Optional[bool]

class ModelListResponse(BaseModel):
    models: List[ModelInfo]
    total: int
    current_production: Optional[str]

class DeploymentRequest(BaseModel):
    model_id: str
    reason: Optional[str] = None

class RegistrationRequest(BaseModel):
    model_path: str
    semver: Optional[str] = None
    model_type: str = "ensemble"
    description: Optional[str] = None
    tags: Optional[List[str]] = None

# ── 端点 ──────────────────────────────────

@router.get("/versions", response_model=ModelListResponse)
async def list_models(
    status: Optional[str] = Query(None, description="active/production/deprecated"),
    model_type: Optional[str] = None,
    limit: int = Query(20, ge=1, le=100),
):
    """列出所有模型版本"""
    try:
        svc = ModelService()
        models = svc.list_models(status=status, model_type=model_type, limit=limit)
        return models
    except ValueError as e:
        logger.error(f"参数错误: {e}")
        raise HTTPException(status_code=400, detail=f"参数错误: {str(e)}")
    except FileNotFoundError as e:
        logger.error(f"模型文件不存在: {e}")
        raise HTTPException(status_code=404, detail="模型文件不存在")
    except KeyError as e:
        logger.error(f"模型数据格式错误: {e}")
        raise HTTPException(status_code=500, detail="模型数据格式错误")
    except (ValueError, KeyError, FileNotFoundError) as e:
        logger.error(f"列出模型失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="获取模型列表失败")

@router.get("/versions/{model_id}", response_model=ModelInfo)
async def get_model(model_id: str):
    """获取模型详情"""
    try:
        svc = ModelService()
        model = svc.get_model(model_id)
        if model is None:
            raise HTTPException(status_code=404, detail=f"模型 {model_id} 不存在")
        return model
    except ValueError as e:
        logger.error(f"参数错误: {e}")
        raise HTTPException(status_code=400, detail=f"参数错误: {str(e)}")
    except FileNotFoundError as e:
        logger.error(f"模型文件不存在: {e}")
        raise HTTPException(status_code=404, detail="模型文件不存在")
    except KeyError as e:
        logger.error(f"模型数据格式错误: {e}")
        raise HTTPException(status_code=500, detail="模型数据格式错误")
    except (requests.exceptions.RequestException, ConnectionError, TimeoutError) as e:
        logger.error(f"获取模型详情失败 {model_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="获取模型详情失败")

@router.post("/deploy")
async def deploy_model(
    req: DeploymentRequest,
    user: dict = Depends(get_admin_user),
):
    """部署模型到生产环境"""
    try:
        svc = ModelService()
        success = svc.deploy(req.model_id)
        if not success:
            raise HTTPException(status_code=400, detail=f"部署失败: {req.model_id}")
        return {"status": "ok", "production_model": req.model_id}
    except ValueError as e:
        logger.error(f"参数错误: {e}")
        raise HTTPException(status_code=400, detail=f"参数错误: {str(e)}")
    except FileNotFoundError as e:
        logger.error(f"模型文件不存在: {e}")
        raise HTTPException(status_code=404, detail="模型文件不存在")
    except KeyError as e:
        logger.error(f"模型数据格式错误: {e}")
        raise HTTPException(status_code=500, detail="模型数据格式错误")
    except (requests.exceptions.RequestException, ConnectionError, TimeoutError) as e:
        logger.error(f"部署模型失败 {req.model_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="部署模型失败")

@router.post("/rollback")
async def rollback_model(
    target_version: Optional[str] = Query(None),
    user: dict = Depends(get_admin_user),
):
    """回滚到指定版本"""
    try:
        svc = ModelService()
        result = svc.rollback(target_version)
        if not result:
            raise HTTPException(status_code=400, detail="回滚失败")
        return {"status": "ok", "rolled_back_to": result}
    except ValueError as e:
        logger.error(f"参数错误: {e}")
        raise HTTPException(status_code=400, detail=f"参数错误: {str(e)}")
    except FileNotFoundError as e:
        logger.error(f"模型文件不存在: {e}")
        raise HTTPException(status_code=404, detail="目标模型不存在")
    except KeyError as e:
        logger.error(f"模型数据格式错误: {e}")
        raise HTTPException(status_code=500, detail="模型数据格式错误")
    except (requests.exceptions.RequestException, ConnectionError, TimeoutError) as e:
        logger.error(f"回滚失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="回滚失败")

@router.get("/compare", response_model=VersionComparison)
async def compare_models(
    model_id_a: str = Query(..., description="模型A ID"),
    model_id_b: str = Query(..., description="模型B ID"),
):
    """对比两个模型版本"""
    if model_id_a == model_id_b:
        raise HTTPException(status_code=400, detail="不能对比相同的模型")
    try:
        svc = ModelService()
        comp = svc.compare_versions(model_id_a, model_id_b)
        if "error" in comp:
            raise HTTPException(status_code=400, detail=comp["error"])
        return comp
    except ValueError as e:
        logger.error(f"参数错误: {e}")
        raise HTTPException(status_code=400, detail=f"参数错误: {str(e)}")
    except FileNotFoundError as e:
        logger.error(f"模型文件不存在: {e}")
        raise HTTPException(status_code=404, detail="一个或多个模型文件不存在")
    except KeyError as e:
        logger.error(f"模型数据格式错误: {e}")
        raise HTTPException(status_code=500, detail="模型数据格式错误")
    except (requests.exceptions.RequestException, ConnectionError, TimeoutError) as e:
        logger.error(f"模型对比失败 {model_id_a} vs {model_id_b}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="模型对比失败")

@router.post("/register")
async def register_model(
    req: RegistrationRequest,
    user: dict = Depends(get_admin_user),
):
    """注册新模型"""
    try:
        svc = ModelService()
        model_id = svc.register(
            model_path=req.model_path,
            semver=req.semver,
            model_type=req.model_type,
            description=req.description,
            tags=req.tags,
        )
        return {"status": "ok", "model_id": model_id}
    except ValueError as e:
        logger.error(f"参数错误: {e}")
        raise HTTPException(status_code=400, detail=f"参数错误: {str(e)}")
    except FileNotFoundError as e:
        logger.error(f"模型路径不存在: {e}")
        raise HTTPException(status_code=404, detail="模型文件路径不存在")
    except KeyError as e:
        logger.error(f"模型数据格式错误: {e}")
        raise HTTPException(status_code=500, detail="模型数据格式错误")
    except (ValueError, KeyError, FileNotFoundError) as e:
        logger.error(f"注册模型失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="注册模型失败")

@router.get("/best")
async def get_best_model(
    metric: str = Query("accuracy", description="accuracy/draw_f1/brier/mcc"),
):
    """获取最优模型"""
    try:
        svc = ModelService()
        best = svc.get_best(metric)
        if not best:
            raise HTTPException(status_code=404, detail="无可用模型")
        return best
    except ValueError as e:
        logger.error(f"参数错误: {e}")
        raise HTTPException(status_code=400, detail=f"参数错误: {str(e)}")
    except KeyError as e:
        logger.error(f"模型数据格式错误: {e}")
        raise HTTPException(status_code=500, detail="模型数据格式错误")
    except (requests.exceptions.RequestException, ConnectionError, TimeoutError) as e:
        logger.error(f"获取最优模型失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="获取最优模型失败")

@router.get("/info")
async def get_current_model_info():
    """获取当前生产模型信息"""
    try:
        svc = ModelService()
        return svc.get_current_info()
    except ValueError as e:
        logger.error(f"参数错误: {e}")
        raise HTTPException(status_code=400, detail=f"参数错误: {str(e)}")
    except FileNotFoundError as e:
        logger.error(f"模型文件不存在: {e}")
        raise HTTPException(status_code=404, detail="当前无生产模型")
    except KeyError as e:
        logger.error(f"模型数据格式错误: {e}")
        raise HTTPException(status_code=500, detail="模型数据格式错误")
    except (ValueError, KeyError, FileNotFoundError) as e:
        logger.error(f"获取当前模型信息失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="获取模型信息失败")

@router.post("/auto-promote")
async def auto_promote(
    min_gain: float = Query(0.5, ge=0.0, le=10.0, description="最小准确率增益(pp)"),
    user: dict = Depends(get_admin_user),
):
    """自动晋升最优模型"""
    try:
        svc = ModelService()
        result = svc.auto_promote(min_gain)
        return {"status": "ok", "promoted_to": result}
    except ValueError as e:
        logger.error(f"参数错误: {e}")
        raise HTTPException(status_code=400, detail=f"参数错误: {str(e)}")
    except FileNotFoundError as e:
        logger.error(f"模型文件不存在: {e}")
        raise HTTPException(status_code=404, detail="无可用模型文件")
    except KeyError as e:
        logger.error(f"模型数据格式错误: {e}")
        raise HTTPException(status_code=500, detail="模型数据格式错误")
    except (ValueError, KeyError, FileNotFoundError) as e:
        logger.error(f"自动晋升失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="自动晋升失败")
