"""
JEPA v5.0 预测端点 — 路由归一至 api/v1/endpoints/ (2026-06-28)
============================================
迁移说明: 原 backend/routers/ → 统一至 api/v1/endpoints/
"""import logging
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)
router = APIRouter(tags=["jepa"])

@router.post("/v5/predict")
async def v5_predict(request: Request):
    """v5.0 JEPA World Model 预测"""
    try:
        body = await request.json()
        home_odds = float(body.get('home_odds', 2.0))
        draw_odds = float(body.get('draw_odds', 3.5))
        away_odds = float(body.get('away_odds', 3.0))
        home_team = body.get('home_team', '')
        away_team = body.get('away_team', '')
        from predictors.jepa_predictor import quick_predict
        result = quick_predict(home_team, away_team, '', home_odds, draw_odds, away_odds)
        return JSONResponse({
            'success': True, 'version': 'v5.0-alpha',
            'prediction': result['prediction'],
            'probabilities': {'home': round(float(result['probabilities'][0]),4),
                              'draw': round(float(result['probabilities'][1]),4),
                              'away': round(float(result['probabilities'][2]),4)},
            'confidence': round(result['confidence'],4), 'source': result['source'],
            'draw_signal': result['draw_signal'],
        })
    except Exception as e:
        return JSONResponse({'success': False, 'error': str(e)}, status_code=500)

@router.get("/v5/health")
async def v5_health():
    """v5.0 健康检查"""
    try:
        from models.jepa import FootballJEPA
        m = FootballJEPA()
        return {"status": "ok", "version": "v5.0-alpha",
                "params": sum(p.numel() for p in m.parameters())}
    except Exception as e:
        return {"status": "degraded", "error": str(e)}
