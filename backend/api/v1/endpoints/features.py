"""
特征查询 API — 球队特征查看 / 对阵特征实时计算
"""
import logging
import sys
import os
import sqlite3
from typing import Optional
from fastapi import APIRouter, HTTPException, Query, Depends
from pydantic import BaseModel, Field

from api.deps import get_current_user

# 确保项目根目录 features/ 包可导入（backend/features/ 在 sys.path 中同名遮蔽）
# features.py → endpoints/ → v1/ → api/ → backend/ → footballAI/
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

logger = logging.getLogger(__name__)
router = APIRouter()

class TeamFeaturesResponse(BaseModel):
    team_name: str
    match_count: int
    features: dict = Field(description="聚合特征 (avg_* + 排名/形态)")

class MatchFeaturesResponse(BaseModel):
    home_team: str
    away_team: str
    computed_features: dict = Field(description="模型就绪的特征向量")
    data_quality: dict = Field(description="特征来源覆盖信息")

@router.get("/teams/{team_name}", response_model=TeamFeaturesResponse)
async def get_team_features(
    team_name: str,
    recent_days: int = Query(180, ge=30, le=730, description="回溯天数"),
    user: dict = Depends(get_current_user),
):
    """获取指定球队的特征聚合快照"""
    try:
        from database.db_manager import get_db
        db = get_db()
        features = db.get_team_features(team_name, recent_days=recent_days)
        return TeamFeaturesResponse(
            team_name=team_name,
            match_count=features.pop("match_count", 0),
            features=features,
        )
    except (sqlite3.Error, sqlalchemy.exc.SQLAlchemyError) as e:
        logger.error(f"获取球队特征失败 ({team_name}): {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"特征查询失败: {e}")

class ComputeFeaturesRequest(BaseModel):
    home_team: str = Field(..., min_length=1, max_length=100)
    away_team: str = Field(..., min_length=1, max_length=100)

@router.get("/compute")
async def compute_match_features(
    home_team: str = Query(..., min_length=1, max_length=100),
    away_team: str = Query(..., min_length=1, max_length=100),
    user: dict = Depends(get_current_user),
):
    """实时计算两支球队的对阵特征向量（模型输入格式）"""
    try:
        from features.feature_calculator import FeatureCalculator
        from database.db_manager import get_db

        db = get_db()
        calc = FeatureCalculator()

        home_data = db.get_team_features(home_team)
        away_data = db.get_team_features(away_team)

        home_data['h2h_advantage'] = db.get_h2h_advantage(home_team, away_team)
        away_data['h2h_advantage'] = -home_data['h2h_advantage']

        features = calc.calculate_match_features(home_team, away_team, home_data, away_data)

        # 质量评估
        provided = sum(1 for v in features.values() if v != 0.0 and v is not None)
        total = len(features)
        quality = {
            "features_computed": total,
            "non_zero_features": provided,
            "fill_ratio": round(provided / max(total, 1), 3),
            "home_matches": home_data.get("match_count", 0),
            "away_matches": away_data.get("match_count", 0),
            "reliability": "ok" if provided > total * 0.5 else "low",
        }

        return MatchFeaturesResponse(
            home_team=home_team,
            away_team=away_team,
            computed_features=features,
            data_quality=quality,
        )
    except (sqlite3.Error, sqlalchemy.exc.SQLAlchemyError) as e:
        logger.error(f"实时特征计算失败 ({home_team} vs {away_team}): {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"特征计算失败: {e}")
