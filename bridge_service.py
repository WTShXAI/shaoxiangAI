"""
哨响AI 独立桥接服务 (FootballAI Bridge)
=========================================
绕开损坏的 backend/main.py，直接调用核心预测引擎 FullLinkagePipeline。

架构:
  bailongma 容器 ──HTTP──> :8000/predict ──> FullLinkagePipeline.predict()
                                       └─> D-Gate / OU联动 / 模型 / TaoGe策略

启动:
  "D:\\Architecture v4.0\\.venv\\Scripts\\python.exe" bridge_service.py
  或: python bridge_service.py --port 8000

端点:
  GET  /            服务信息
  GET  /health      健康检查
  POST /predict     核心预测 (接收 MatchInput 字段)
  POST /predict/simple  简化输入 (赔率字符串格式)
"""
from __future__ import annotations
import os
import sys
import json
import logging
from typing import Any, Dict, Optional

# ── 项目根入 sys.path，确保 pipeline 包可导入 ──
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

logger = logging.getLogger("football_bridge")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)

# ── 加载核心引擎 (启动时一次性初始化) ──
try:
    from pipeline.predictors.data_classes import MatchInput
    from pipeline.predictors.pipeline import FullLinkagePipeline
    ENGINE = FullLinkagePipeline()
    logger.info("FullLinkagePipeline 引擎加载成功")
except Exception as e:
    logger.error(f"引擎加载失败: {e}", exc_info=True)
    ENGINE = None


# ═══ Pydantic 输入模型 ═══
class PredictRequest(BaseModel):
    """全链路预测请求 — 对应 MatchInput 字段"""
    home: str = Field(..., description="主队名")
    away: str = Field(..., description="客队名")
    odds_h: float = Field(..., description="主胜赔率")
    odds_d: float = Field(..., description="平局赔率")
    odds_a: float = Field(..., description="客胜赔率")
    hcp: float = Field(..., description="让球(外围初盘, -1=主让1球, +0.5=主受让0.5)")
    ou_line: float = Field(..., description="大小球盘口(2.0/2.25/2.5/2.75/3.0)")
    over_water: float = 1.90
    under_water: float = 1.92
    matchday: int = 3
    r3_rotation: bool = False
    stage: str = "group"
    home_formation: str = ""
    away_formation: str = ""
    home_full_strength: bool = True
    away_full_strength: bool = True
    home_missing_stars: str = ""
    away_missing_stars: str = ""
    sporttery_hcp: float = 0.0


class SimplePredictRequest(BaseModel):
    """简化请求 — 赔率字符串格式"""
    home: str
    away: str
    odds_1x2: str = Field(..., description="格式: '4.05,3.55,1.80'")
    hcp: str = Field(..., description="格式: '+0.5' 或 '-1.25'")
    ou: str = Field(..., description="格式: '2.5'")
    ou_odds: str = "1.90/1.92"
    r3: bool = False


# ═══ FastAPI 应用 ═══
app = FastAPI(
    title="FootballAI Bridge",
    description="哨响AI 核心预测引擎 HTTP 桥接 (绕开损坏的 backend/main.py)",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


def _run_predict(match: MatchInput) -> Dict[str, Any]:
    """执行预测并清洗不可序列化字段"""
    if ENGINE is None:
        raise HTTPException(status_code=503, detail="预测引擎未加载")
    try:
        result = ENGINE.predict(match)
    except Exception as e:
        logger.error(f"预测执行失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"预测失败: {e}")

    # 去掉 lambda (不可 JSON 序列化)
    result.pop("_half_time_adjust", None)
    # 兜底: 任何残留不可序列化对象转字符串
    return json.loads(json.dumps(result, ensure_ascii=False, default=str))


@app.get("/")
async def root():
    return {
        "service": "FootballAI Bridge",
        "version": "1.0.0",
        "engine": "FullLinkagePipeline (D-Gate v5.3 + OU联动 + UnifiedPredictor + TaoGe策略)",
        "engine_loaded": ENGINE is not None,
        "endpoints": {
            "predict": "POST /predict",
            "predict_simple": "POST /predict/simple",
            "health": "GET /health",
            "docs": "GET /docs",
        },
    }


@app.get("/health")
async def health():
    return {"ok": ENGINE is not None, "engine": "FullLinkagePipeline"}


@app.post("/predict")
async def predict(req: PredictRequest):
    """全链路预测 — 7层联动 (Chain -1,0,0.5,1,2,3,4)"""
    match = MatchInput(
        home=req.home, away=req.away,
        odds_h=req.odds_h, odds_d=req.odds_d, odds_a=req.odds_a,
        hcp=req.hcp, ou_line=req.ou_line,
        over_water=req.over_water, under_water=req.under_water,
        matchday=req.matchday, r3_rotation=req.r3_rotation,
        stage=req.stage,
        home_formation=req.home_formation, away_formation=req.away_formation,
        home_full_strength=req.home_full_strength, away_full_strength=req.away_full_strength,
        home_missing_stars=req.home_missing_stars, away_missing_stars=req.away_missing_stars,
        sporttery_hcp=req.sporttery_hcp,
    )
    return _run_predict(match)


@app.post("/predict/simple")
async def predict_simple(req: SimplePredictRequest):
    """简化预测 — 赔率字符串快速构造"""
    try:
        match = MatchInput.from_odds_snapshot(
            home=req.home, away=req.away,
            odds_1x2=req.odds_1x2, hcp_str=req.hcp, ou_str=req.ou,
            ou_odds=req.ou_odds, r3=req.r3,
        )
    except (ValueError, TypeError) as e:
        raise HTTPException(status_code=422, detail=f"参数格式错误: {e}")
    return _run_predict(match)


if __name__ == "__main__":
    import uvicorn
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 9000
    host = os.getenv("API_HOST", "0.0.0.0")
    logger.info(f"启动 FootballAI Bridge @ {host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="info")
