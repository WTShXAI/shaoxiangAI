"""
API v1 主路由聚合
"""
from fastapi import APIRouter
from api.v1.endpoints import predictions, models, monitor, training, data_quality, auth, ab_test, alerts, historical_data, evaluation, features, matches, admin

api_router = APIRouter()

# 各子路由
api_router.include_router(predictions.router, prefix="/predict", tags=["预测"])
api_router.include_router(models.router, prefix="/models", tags=["模型管理"])
api_router.include_router(monitor.router, prefix="/monitor", tags=["监控"])
api_router.include_router(training.router, prefix="/training", tags=["训练"])
api_router.include_router(data_quality.router, prefix="/data-quality", tags=["数据质量"])
api_router.include_router(auth.router, prefix="/auth", tags=["认证"])
api_router.include_router(ab_test.router, prefix="/ab-test", tags=["A/B测试"])
api_router.include_router(alerts.router, prefix="/alerts", tags=["告警"])
api_router.include_router(historical_data.router, prefix="/historical", tags=["历史数据"])
api_router.include_router(evaluation.router, prefix="/evaluation", tags=["评估"])
api_router.include_router(features.router, prefix="/features", tags=["特征"])
api_router.include_router(matches.router, prefix="/matches", tags=["比赛数据"])
api_router.include_router(admin.router, prefix="/admin", tags=["管理"])
