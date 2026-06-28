"""
告警 API 端点
"""
import logging
from typing import Optional, Dict, List
from fastapi import APIRouter, HTTPException, Query, Depends
from pydantic import BaseModel

from core.security import require_admin, require_operator
from services.alert_service import get_alert_service, AlertRule, AlertLevel

logger = logging.getLogger(__name__)
router = APIRouter()

class CheckMetricsRequest(BaseModel):
    metrics: Dict[str, float]

@router.get("/alerts")
async def get_alerts(
    limit: int = Query(50, ge=1, le=200),
    level: Optional[str] = Query(None),
    _: None = Depends(require_operator),
):
    """获取最近告警"""
    try:
        svc = get_alert_service()
        alerts = svc.get_recent_alerts(limit=limit, level=level)
        return {"alerts": alerts, "total": len(alerts)}
    except ValueError as e:
        logger.error(f"参数错误: {e}")
        raise HTTPException(status_code=400, detail=f"参数错误: {str(e)}")
    except KeyError as e:
        logger.error(f"数据格式错误: {e}")
        raise HTTPException(status_code=500, detail="数据格式错误")
    except (ValueError, KeyError, FileNotFoundError) as e:
        logger.error(f"获取告警失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="获取告警失败")

@router.get("/rules")
async def get_rules():
    """获取告警规则"""
    try:
        svc = get_alert_service()
        return {"rules": svc.get_rules()}
    except HTTPException:
        raise
    except (ValueError, KeyError, FileNotFoundError) as e:
        logger.error(f"获取告警规则失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="获取规则失败")

@router.post("/check")
async def check_metrics(req: CheckMetricsRequest):
    """检查指标是否触发告警"""
    try:
        svc = get_alert_service()
        triggered = svc.check_metrics(req.metrics)
        return {
            "triggered": triggered,
            "count": len(triggered),
            "has_alerts": len(triggered) > 0,
        }
    except HTTPException:
        raise
    except (ValueError, KeyError, FileNotFoundError) as e:
        logger.error(f"指标检查失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="指标检查失败")

@router.post("/rules")
async def add_rule(
    name: str,
    metric: str,
    condition: str,
    threshold: float,
    level: str = "warning",
    cooldown_minutes: int = 30,
    description: str = "",
    _: None = Depends(require_admin),
):
    """添加告警规则"""
    if not name.strip():
        raise HTTPException(status_code=400, detail="规则名不能为空")
    if not metric.strip():
        raise HTTPException(status_code=400, detail="指标名不能为空")
    if condition not in (">", "<", ">=", "<=", "==", "!="):
        raise HTTPException(status_code=400, detail=f"无效条件: {condition}，可选: >, <, >=, <=, ==, !=")

    try:
        alert_level = AlertLevel(level)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"无效告警级别: {level}，可选: info/warning/error/critical")

    try:
        rule = AlertRule(
            name=name.strip(),
            metric=metric.strip(),
            condition=condition,
            threshold=threshold,
            level=alert_level,
            cooldown_minutes=cooldown_minutes,
            description=description,
        )
        svc = get_alert_service()
        svc.add_rule(rule)
        return {"status": "ok", "rule": name}
    except HTTPException:
        raise
    except (ValueError, KeyError, FileNotFoundError) as e:
        logger.error(f"添加告警规则失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="添加规则失败")

@router.delete("/alerts")
async def clear_alerts(_: None = Depends(require_admin)):
    """清除所有告警"""
    try:
        svc = get_alert_service()
        svc.clear_alerts()
        return {"status": "cleared"}
    except HTTPException:
        raise
    except (ValueError, KeyError, FileNotFoundError) as e:
        logger.error(f"清除告警失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="清除告警失败")
