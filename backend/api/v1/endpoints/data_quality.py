"""
数据质量 API — 质量报告/漂移检测
"""
from typing import List, Optional, Dict, Any
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from datetime import datetime

router = APIRouter()

class QualityReport(BaseModel):
    timestamp: str
    data_source: str
    quality_score: float
    checks_passed: int
    checks_failed: int
    violations: List[Dict[str, Any]]
    metrics: Dict[str, Any]

class DriftReport(BaseModel):
    timestamp: str
    drift_detected: bool
    drift_metrics: Dict[str, Any]

@router.get("/reports", response_model=List[QualityReport])
async def get_quality_reports(
    source: Optional[str] = None,
    limit: int = Query(100, ge=1, le=500),
):
    """获取数据质量报告"""
    try:
        from utils.data_quality_checker import DataQualityChecker
        checker = DataQualityChecker()
        reports = checker.get_reports(source=source, limit=limit)
        return reports
    except ImportError:
        raise HTTPException(status_code=501, detail="data_quality_checker 不可用")
    except (ValueError, KeyError, FileNotFoundError) as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/check")
async def check_data_quality():
    """运行数据质量检查"""
    try:
        import sys
        from core.config import settings

        from utils.data_quality_checker import DataQualityChecker
        checker = DataQualityChecker()
        report = checker.check_all()
        return report
    except ImportError:
        raise HTTPException(status_code=501, detail="data_quality_checker 不可用")
    except (ValueError, KeyError, FileNotFoundError) as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/drift-detection", response_model=DriftReport)
async def detect_data_drift(
    window_days: int = Query(30, ge=7, le=365),
    significance_level: float = Query(0.05, ge=0.01, le=0.10),
):
    """检测数据漂移"""
    try:
        import sys
        from core.config import settings

        from utils.drift_detector import DataDriftDetector
        detector = DataDriftDetector()
        report = detector.check_recent_drift(
            window_days=window_days,
            significance_level=significance_level,
        )
        return report
    except ImportError:
        raise HTTPException(status_code=501, detail="drift_detector 不可用")
    except (ValueError, KeyError, FileNotFoundError) as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/freshness")
async def check_data_freshness():
    """检查数据新鲜度"""
    try:
        import sys
        from core.config import settings

        from utils.data_quality_checker import DataQualityChecker
        checker = DataQualityChecker()
        freshness = checker.check_freshness()
        return freshness
    except ImportError:
        raise HTTPException(status_code=501, detail="data_quality_checker 不可用")
    except (ValueError, KeyError, FileNotFoundError) as e:
        raise HTTPException(status_code=500, detail=str(e))
