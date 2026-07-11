"""
哨响AI 独立桥接服务 (FootballAI Bridge)
=========================================
哨响AI v7.1 — 优化版规则流水线预测引擎 (DrawExpert + 17报告决策树)

架构:
  bailongma 容器 ──HTTP──> :8000/predict ──> v7_rule_pipeline.predict()

启动:
  "D:\\Architecture\\.venv\\Scripts\\python.exe" bridge_service.py
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
from datetime import datetime, timezone

# ── 项目根入 sys.path，确保 pipeline 包可导入 ──
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from fastapi import FastAPI, HTTPException, Request
from starlette.websockets import WebSocket
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

logger = logging.getLogger("football_bridge")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)

# ── 加载核心引擎 (v7.1 双引擎: wc/league) ──
_DEFAULT_ENGINE = os.getenv("ENGINE", "wc")
ENGINE = None
_ENGINE_REGISTRY: Dict[str, Any] = {}
MatchInput = None
_ENGINE_LOAD_OK = False

try:
    from pipeline.engine import create_engine, _ENGINE_REGISTRY as _reg
    from pipeline.predictors.data_classes import MatchInput as _MatchInput
    _ENGINE_REGISTRY = _reg
    MatchInput = _MatchInput
    ENGINE = create_engine(_DEFAULT_ENGINE)
    _ENGINE_LOAD_OK = True
    logger.info(f"默认引擎加载成功: {ENGINE.description}")
except Exception as e:
    logger.error(f"引擎加载失败: {e}", exc_info=True)

# 动态引擎缓存 (按 competition 路由)
_ENGINE_CACHE: Dict[str, Any] = {}
if ENGINE is not None:
    _ENGINE_CACHE[_DEFAULT_ENGINE] = ENGINE


def _get_engine(competition: str = "wc"):
    """按赛事类型获取引擎实例 (惰性加载+缓存)"""
    comp = competition.lower()
    if comp not in _ENGINE_REGISTRY:
        comp = _DEFAULT_ENGINE  # 未知赛事回退默认
    if comp not in _ENGINE_CACHE:
        _ENGINE_CACHE[comp] = create_engine(comp)
        logger.info(f"引擎加载: {_ENGINE_CACHE[comp].description}")
    return _ENGINE_CACHE[comp]


# ═══ Pydantic 输入模型 ═══
class PredictRequest(BaseModel):
    """全链路预测请求 — 对应 MatchInput 字段"""
    home: str = Field(..., description="主队名")
    away: str = Field(..., description="客队名")
    odds_h: float = Field(..., gt=0, description="主胜赔率(>0)")
    odds_d: float = Field(..., gt=0, description="平局赔率(>0)")
    odds_a: float = Field(..., gt=0, description="客胜赔率(>0)")
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
    competition: str = "wc"  # wc=世界杯, league=五大联赛


class SinglePredictRequest(BaseModel):
    """前端兼容请求 — 球队名 + 可选赔率 (不传赔率时查数据库)"""
    home_team: Optional[str] = None
    homeTeam: Optional[str] = None
    away_team: Optional[str] = None
    awayTeam: Optional[str] = None
    league: Optional[str] = None
    # 可选赔率 (前端有则传，无则查库)
    odds_h: Optional[float] = None
    odds_d: Optional[float] = None
    odds_a: Optional[float] = None
    hcp: Optional[float] = None
    ou_line: Optional[float] = None
    stage: str = "knockout"
    competition: str = "wc"  # wc=世界杯, league=五大联赛


def _lookup_odds_from_db(home: str, away: str) -> Optional[Dict[str, float]]:
    """查赔率: DB → QF预测JSON"""
    import sqlite3
    # Step 1: DB lookup
    db_path = os.path.join(PROJECT_ROOT, "data", "football_data.db")
    if os.path.exists(db_path):
        try:
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute(
                "SELECT odds_h, odds_d, odds_a, ah_line, ou_line FROM world_cup_2026_predictions WHERE home_team=? AND away_team=?",
                (home, away),
            )
            row = cur.fetchone()
            if row and row[0]:
                conn.close()
                return {"odds_h": row[0], "odds_d": row[1], "odds_a": row[2], "hcp": row[3] or 0.0, "ou_line": row[4] or 2.5}
            conn.close()
        except Exception as e:
            logger.warning(f"DB查赔率失败: {e}")

    # Step 2: QF predictions JSON 兜底
    qf_path = os.path.join(PROJECT_ROOT, "data", "qf_predictions_repredict.json")
    try:
        with open(qf_path, encoding='utf-8') as f:
            qf_data = json.load(f)
        for m in qf_data:
            if m.get("home") == home and m.get("away") == away:
                odds_str = m.get("odds", "0/0/0")
                p = odds_str.split("/")
                return {
                    "odds_h": float(p[0]), "odds_d": float(p[1]), "odds_a": float(p[2]),
                    "hcp": float(m.get("hcp", 0)), "ou_line": float(m.get("ou", 2.5)),
                }
    except Exception as e:
        logger.warning(f"QF JSON查赔率失败: {e}")

    return None


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

# ═══ API兼容中间件 — 拦截 /api/v1/* 返回空数据防前端崩溃 ═══
#  注意: 使用纯 ASGI 中间件, 避免 BaseHTTPMiddleware 破坏 WebSocket 连接
from starlette.responses import JSONResponse
from datetime import datetime, timezone as tz


def _wrap_data(data) -> dict:
    """包装为前端 ApiResponse<T> 格式"""
    return {
        "success": True,
        "data": data,
        "timestamp": datetime.now(tz.utc).isoformat(),
    }


# ── 从 QF 预测数据加载真实赛程 ──
def _load_qf_fixtures():
    """加载8强赛程数据, 转为前端 Fixture/Match 格式"""
    qf_path = os.path.join(PROJECT_ROOT, "data", "qf_predictions_repredict.json")
    try:
        with open(qf_path, encoding='utf-8') as f:
            qf_data = json.load(f)
    except Exception:
        return [], []

    fixtures = []
    matches = []
    for m in qf_data:
        if m.get("error"):
            continue
        idx = m.get("idx", 0)
        home = m.get("home", "")
        away = m.get("away", "")
        odds_str = m.get("odds", "0/0/0")
        odds_parts = odds_str.split("/")
        odds_h = float(odds_parts[0]) if len(odds_parts) > 0 else 0
        odds_d = float(odds_parts[1]) if len(odds_parts) > 1 else 0
        odds_a = float(odds_parts[2]) if len(odds_parts) > 2 else 0

        # 计算隐含概率 (去 overround)
        implied_sum = 1/odds_h + 1/odds_d + 1/odds_a
        imp_h = (1/odds_h) / implied_sum if implied_sum > 0 else 0.33
        imp_d = (1/odds_d) / implied_sum if implied_sum > 0 else 0.33
        imp_a = (1/odds_a) / implied_sum if implied_sum > 0 else 0.33

        # 构建 top_scores 格式
        best_score = m.get("best_score", "")
        alt_scores = m.get("alt_scores", [])
        top_scores = [{"score": best_score, "prob": 0.30}] if best_score else []
        for s in alt_scores[:2]:
            top_scores.append({"score": s, "prob": 0.15})

        # Fixture 格式
        fixtures.append({
            "id": idx,
            "home": home,
            "away": away,
            "time": f"2026-07-{5+idx:02d}T00:00:00Z",
            "time_local": f"{5+idx:02d}:00",
            "date_local": f"07-{5+idx:02d}",
            "day_of_week": "一",
            "group": "",
            "stage": "quarterfinal",
            "status": "TIMED",
            "score_home": None,
            "score_away": None,
            "is_finished": False,
            "prediction": {
                "verdict": m.get("verdict", ""),
                "best_score": best_score,
                "top_scores": top_scores,
                "confidence": m.get("confidence", 0),
                "rec_type": m.get("rec_type", ""),
                "probabilities": {"H": round(imp_h, 3), "D": round(imp_d, 3), "A": round(imp_a, 3)},
            } if m.get("verdict") else None,
        })

        # Match 格式
        matches.append({
            "id": str(idx),
            "homeTeam": {"id": str(idx), "name": home, "shortName": home[:3]},
            "awayTeam": {"id": str(idx), "name": away, "shortName": away[:3]},
            "league": {"code": "WC26", "name": "世界杯 2026", "country": "国际"},
            "kickoff": f"2026-07-{5+idx:02d}T00:00:00Z",
            "status": "upcoming",
            "homeOdds": odds_h,
            "drawOdds": odds_d,
            "awayOdds": odds_a,
            "prediction": m.get("verdict", ""),
            "confidence": m.get("confidence", 0),
        })

    return fixtures, matches


_QF_FIXTURES, _QF_MATCHES = _load_qf_fixtures()

_API_V1_STUBS = {
    "monitor/health": _wrap_data({
        "status": "healthy",
        "uptime": 0,
        "apiLatency": 0,
        "predictionLatency": 0,
        "modelHealth": "healthy",
        "databaseHealth": "healthy",
        "memoryUsage": 0,
        "cpuUsage": 0,
    }),
    "monitor/metrics/summary": _wrap_data({
        "apiRequestsPerMin": 0,
        "avgResponseTime": 0,
        "predictionRequestsPerMin": 0,
        "errorRate": 0,
        "activeUsers": 0,
    }),
    "alerts/alerts": _wrap_data([]),
    "fixtures/upcoming": _wrap_data({
        "matches": _QF_FIXTURES,
        "days": 7,
        "upcoming_count": len(_QF_FIXTURES),
        "finished_count": 0,
        "cutoff": "2026-07-15",
        "today": _QF_FIXTURES[:3],
        "tomorrow": _QF_FIXTURES[3:],
    }),
    "matches/list": _wrap_data({"matches": _QF_MATCHES, "total": len(_QF_MATCHES)}),
    "predict/stats": _wrap_data({
        "total": len(_QF_MATCHES),
        "todayAccuracy": 0,
        "overallAccuracy": 0,
        "totalPredictions": len(_QF_MATCHES),
        "hotLeagues": [{"league": "世界杯 2026", "count": len(_QF_MATCHES)}],
    }),
    "predict/history": _wrap_data([]),
    "models/versions": _wrap_data([]),
    "historical/leagues": _wrap_data([{"code": "WC26", "name": "世界杯 2026", "country": "国际"}]),
    "data-quality/reports": _wrap_data([]),
}

class APIV1CompatMiddleware:
    """纯 ASGI 中间件 — 不破坏 WebSocket 连接"""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith("/api/v1/"):
            sub = scope["path"][len("/api/v1/"):]
            for key, stub in _API_V1_STUBS.items():
                if sub == key or sub.startswith(key):
                    body = json.dumps(stub, ensure_ascii=False).encode("utf-8")
                    await send({
                        "type": "http.response.start",
                        "status": 200,
                        "headers": [
                            (b"content-type", b"application/json; charset=utf-8"),
                            (b"content-length", str(len(body)).encode()),
                        ],
                    })
                    await send({"type": "http.response.body", "body": body})
                    return
            # 未匹配: 去掉 /api/v1 前缀, 放行到真实端点
            scope["path"] = "/" + sub
        await self.app(scope, receive, send)

app.add_middleware(APIV1CompatMiddleware)

# ── 前端静态文件 (SPA路由回退) — 必须在CORS和API中间件之后 ──
#  注意: 前端 dist 不存在时跳过, 服务退化为纯 API 模式
from fastapi.staticfiles import StaticFiles

FRONTEND_DIR = os.path.join(PROJECT_ROOT, "frontend", "dist")
ASSETS_DIR = os.path.join(FRONTEND_DIR, "assets")
if os.path.exists(FRONTEND_DIR) and os.path.isdir(ASSETS_DIR):
    app.mount("/assets", StaticFiles(directory=ASSETS_DIR), name="assets")
    logger.info(f"[Bridge] 前端静态文件: {FRONTEND_DIR}")


def _run_predict(match: MatchInput, competition: str = "wc") -> Dict[str, Any]:
    """执行预测并返回兼容格式 — 按赛事路由引擎"""
    engine = _get_engine(competition)
    if engine is None:
        raise HTTPException(status_code=503, detail="预测引擎未加载")
    # G10: 计算跨庄 soft-line 调整(与 _odds_intel 同源), 护栏OFF时不传入 predict(保持argmax兜底)
    sl = _compute_softline(match, getattr(match, 'match_id', None))
    try:
        result = engine.predict(match, softline=sl if ENABLE_SOFTLINE_DECISION else None)
    except Exception as e:
        logger.error(f"预测执行失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"预测失败: {e}")

    # 构建兼容 v6 _format_prediction 的 raw dict
    return {
        "final_verdict": {
            "primary": {"H": "主胜", "D": "平局", "A": "客胜"}.get(result.prediction, "?"),
            "secondary": "",
            "best_score": result.best_score,
            "alt_scores": result.alt_scores,
            "confidence": result.confidence,
        },
        "ou_link": {
            "recommend": result.ou_recommend.get("recommend") if result.ou_recommend else None,
            "line": result.ou_recommend.get("line") if result.ou_recommend else None,
            "expected_total": result.ou_recommend.get("expected_total") if result.ou_recommend else None,
            "confidence": result.ou_recommend.get("confidence") if result.ou_recommend else None,
            "wc_calibrated": bool(result.ou_recommend and result.ou_recommend.get("wc_calibrated")),
        },
        "chains": {
            "v7_rule": {
                "verdict": result.prediction,
                "draw_prob": result.market_probs.get("D", 0.30),
                "confidence": result.confidence,
                "confidence_level": result.confidence_level,
                "market_baseline": result.market_baseline,
                "mid_range_filtered": result.mid_range_filtered,
                "mispricing_overlay": result.mispricing_overlay,
                "massacre_triggered": result.massacre_triggered,
                "survival_clash": result.survival_clash,
                "rationale": result.rationale,
            }
        },
        "v7_raw": {
            "prediction": result.prediction,
            "confidence": result.confidence,
            "best_score": result.best_score,
            "alt_scores": result.alt_scores,
            "market_probs": result.market_probs,
            "market_baseline": result.market_baseline,
            "confidence_level": result.confidence_level,
            "mid_range_filtered": result.mid_range_filtered,
            "mispricing_overlay": result.mispricing_overlay,
            "massacre_triggered": result.massacre_triggered,
            "survival_clash": result.survival_clash,
            "rationale": result.rationale,
            "ou_recommend": result.ou_recommend,
            "hcp_recommend": result.hcp_recommend,
        },
        # G10: 跨庄 soft-line 展示 (灰度期供人工复核; 护栏ON且disagreement时已被predict覆盖)
        "softline": sl,
    }


@app.get("/")
async def root():
    """首页 — 返回前端 SPA (如未构建则返回API信息)"""
    index_path = os.path.join(FRONTEND_DIR, "index.html")
    if os.path.exists(index_path):
        from fastapi.responses import FileResponse
        return FileResponse(index_path)
    return {
        "service": "FootballAI Bridge",
        "version": "7.0.0",
        "engine": ENGINE.description if ENGINE else "未加载",
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
    ok = ENGINE is not None
    return {
        "ok": ok,
        "status": "healthy" if ok else "degraded",
        "engine": ENGINE.description if ENGINE else "未加载",
    }


@app.get("/ready")
async def ready():
    """K8s readiness probe: 引擎+DB+赔率库全部就绪才200"""
    if ENGINE is None:
        raise HTTPException(status_code=503, detail="引擎未加载")
    return {"ok": True, "engine": ENGINE.description}

# ── WebSocket 实时更新 ──
@app.websocket("/ws/realtime")
async def ws_realtime(ws):
    """WebSocket 实时推送 — 心跳保持连接"""
    try:
        await ws.accept()
        logger.info("[WS] 客户端已连接")
        while True:
            data = await ws.receive_text()
            if data == "ping":
                await ws.send_text(json.dumps({
                    "type": "pong",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }))
    except Exception as e:
        logger.warning(f"[WS] 连接异常: {e}")


@app.post("/predict")
async def predict(req: PredictRequest):
    """全链路预测 — 7层联动 (Chain -1,0,0.5,1,2,3,4)"""
    if MatchInput is None or ENGINE is None:
        raise HTTPException(status_code=503, detail="预测引擎未就绪")
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
    return _run_predict(match, competition=req.competition)


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
    return _run_predict(match, competition="wc")  # simple端点默认世界杯


@app.post("/predict/single")
async def predict_single(req: SinglePredictRequest):
    """前端兼容端点 — 接收球队名(+可选赔率)，返回 ApiResponse 格式"""
    home = req.home_team or req.homeTeam or ""
    away = req.away_team or req.awayTeam or ""
    if not home or not away:
        raise HTTPException(status_code=422, detail="需要提供 home_team 和 away_team")

    # 赔率来源优先级: 显式传入 > 数据库查询
    odds_h = req.odds_h
    odds_d = req.odds_d
    odds_a = req.odds_a
    hcp = req.hcp
    ou_line = req.ou_line

    if odds_h is None or odds_d is None or odds_a is None:
        db_odds = _lookup_odds_from_db(home, away)
        if db_odds:
            odds_h = odds_h or db_odds["odds_h"]
            odds_d = odds_d or db_odds["odds_d"]
            odds_a = odds_a or db_odds["odds_a"]
            hcp = hcp if hcp is not None else db_odds["hcp"]
            ou_line = ou_line or db_odds["ou_line"]

    if odds_h is None or odds_d is None or odds_a is None:
        raise HTTPException(
            status_code=404,
            detail=f"数据库无 {home} vs {away} 赔率记录，请通过 /predict 端点显式传入赔率",
        )

    match = MatchInput(
        home=home, away=away,
        odds_h=odds_h, odds_d=odds_d, odds_a=odds_a,
        hcp=hcp or 0.0, ou_line=ou_line or 2.5,
        stage=req.stage,
    )
    raw = _run_predict(match, competition=req.competition)

    # 直接返回预测数据 (与后端 /predict/single 格式一致, 不包 ApiResponse 壳)
    fv = raw.get("final_verdict", {})
    ou_link = raw.get("ou_link", {})
    primary = fv.get("primary", "")
    secondary = fv.get("secondary", "")
    # 推导 result: 让胜/主胜→H, 让负/客胜→A, 平→D
    if "客" in primary or "负" in primary:
        pred_code = "A"
    elif "平" in primary:
        pred_code = "D"
    else:
        pred_code = "H"
    # 推导模型概率 (pH, pD, pA) — v7.1-opt 使用 v7_rule 链
    v7_chain = raw.get("chains", {}).get("v7_rule", {})
    model_verdict = v7_chain.get("verdict", raw.get("v7_raw", {}).get("prediction", "?"))
    draw_prob = raw.get("v7_raw", {}).get("market_probs", {}).get("D", 0.30)
    # 从赔率推导隐含概率 (去 overround)
    implied_sum = 1/match.odds_h + 1/match.odds_d + 1/match.odds_a
    imp_h = (1/match.odds_h) / implied_sum if implied_sum else 0
    imp_d = (1/match.odds_d) / implied_sum if implied_sum else 0
    imp_a = (1/match.odds_a) / implied_sum if implied_sum else 0

    if model_verdict == "D":
        pH = (1 - draw_prob) * imp_h / (imp_h + imp_a) if (imp_h + imp_a) > 0 else 0.325
        pD = draw_prob
        pA = (1 - draw_prob) * imp_a / (imp_h + imp_a) if (imp_h + imp_a) > 0 else 0.325
    elif model_verdict == "H":
        pD = draw_prob
        pH = max(1 - draw_prob - 0.15, 0.40)
        pA = 1 - pH - pD
    elif model_verdict == "A":
        pD = draw_prob
        pA = max(1 - draw_prob - 0.15, 0.40)
        pH = 1 - pA - pD
    else:  # 未知: 使用赔率隐含概率
        pH, pD, pA = imp_h, imp_d, imp_a

    return {
        "prediction": pred_code,
        "result": pred_code,
        "probabilities": {
            "H": round(pH, 4),
            "D": round(pD, 4),
            "A": round(pA, 4),
            "home": round(pH, 4),
            "draw": round(pD, 4),
            "away": round(pA, 4),
        },
        # 市场基线 (收盘赔率argmax — 永远正确的参照系)
        "market_baseline": {
            "H": round(imp_h, 4),
            "D": round(imp_d, 4),
            "A": round(imp_a, 4),
            "prediction": "H" if imp_h > imp_d and imp_h > imp_a else ("D" if imp_d > imp_h and imp_d > imp_a else "A"),
        },
        "score": {
            "home": int(fv.get("best_score", "0-0").split("-")[0]) if fv.get("best_score") else 0,
            "away": int(fv.get("best_score", "0-0").split("-")[1]) if fv.get("best_score") else 0,
        },
        "score_prediction": {
            "primary": fv.get("best_score", "0-0"),
            "top_scores": [{"score": fv.get("best_score", "0-0"), "prob": 0.3, "outcome": pred_code}] +
                          [{"score": s, "prob": 0.15, "outcome": pred_code} for s in fv.get("alt_scores", [])],
        },
        "confidence": fv.get("confidence", 0),
        "prediction_mode": "哨响AI-v7.1-opt+DrawExpert",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "analysis": f"{primary}+{secondary}" if secondary else primary,
        # ── P0修复新增字段 ──
        "consistency": fv.get("consistency"),
        "hcp2_law_applied": ou_link.get("hcp2_law_applied"),
        "short_circuit": fv.get("short_circuit"),
        "p0_triggers": fv.get("p0_triggers", []),
        "best_score": fv.get("best_score"),
        "alt_scores": fv.get("alt_scores", []),
        "dgate_result": raw.get("dgate_result"),
        "ou_linkage": ou_link,
        "taoge_strategy": raw.get("taoge_strategy"),
        # ── WC校准 OU/让球建议 (v7.1 rules-layer 新增) ──
        "ou_recommend": raw.get("v7_raw", {}).get("ou_recommend"),
        "hcp_recommend": raw.get("v7_raw", {}).get("hcp_recommend"),
        # Phase A: ReverseOddsEngine 赔率逆向分析
        "odds_intel": _odds_intel(match, raw, getattr(match, 'match_id', None)),
    }

# ── G4: 真 bet-split 源 (替代 rlm_proxy 代理); 无 key/id/异常→None 自动降级 ──
def _resolve_rlm_real(match_id: Optional[str]) -> Optional[object]:
    """按内部 match_id(=The Odds API event id)拉真投注分布; 无 key/id/异常→None.

    上层 analyze_multi 收到 None → 自动用 rlm_proxy 代理(行为不变).
    仅当环境变量 THEODDS_API_KEY 设置且 match_id 有效时才发起外部调用,
    不消耗 quota / 不引入延迟 (无 key 时直接返回 None).
    """
    if not match_id:
        return None
    try:
        from pipeline.bet_split_source import TheOddsApiBetSplit
        if not os.environ.get('THEODDS_API_KEY'):
            return None
        src = TheOddsApiBetSplit(api_key=os.environ['THEODDS_API_KEY'])
        return src.fetch(str(match_id))
    except Exception:
        return None


# ── G10: 跨庄 soft-line 抽取 (供预测层 predict() 第7步回灌) ──
def _compute_softline(match: MatchInput, match_id: Optional[str] = None) -> Optional[dict]:
    """抽取跨庄 soft-line 调整(与 _odds_intel 同源逻辑), 供预测层 predict() 回灌。

    仅当查到 >=2 庄(WH+IW)且 analyze_multi 产出 softline_adjusted_probs 时返回 dict,
    否则返回 None (predict 退化纯 argmax)。异常安全: 任何 DB/解析错误返回 None。
    """
    try:
        from pipeline.reverse_odds_engine import ReverseOddsEngine
        engine = ReverseOddsEngine()
        books = engine.query_odds_multi(match.home, match.away)
        if len(books) >= 2:
            rlm_real = _resolve_rlm_real(match_id)
            r = engine.analyze_multi(books, rlm_real=rlm_real)
            if r.softline_adjusted_probs is not None:
                return {
                    "softline_adjusted_probs": [float(x) for x in r.softline_adjusted_probs],
                    "disagreement_detected": bool(r.disagreement_detected),
                    "softline_fade_applied": bool(r.softline_fade_applied),
                }
    except Exception:
        return None
    return None


# ── Phase A: ReverseOddsEngine 赔率逆向分析 ──
def _odds_intel(match: MatchInput, raw: dict, match_id: Optional[str] = None) -> Optional[dict]:
    """调用 ReverseOddsEngine 分析赔率意图(多机构优先, 单机构兜底), 失败时返回 None。

    操盘手框架: 多机构同步异动=真信号; 单机构独调=平衡动作(非陷阱)。
    多机构时额外回传 cross_book_sync / confirmed / clv_beat(soft line edge) / rlm_proxy。
    """
    try:
        from pipeline.reverse_odds_engine import ReverseOddsEngine, OddsInput
        engine = ReverseOddsEngine()

        # 多机构优先: 跨机构同步判定(真信号) + CLV(soft line edge)
        books = []
        try:
            books = engine.query_odds_multi(match.home, match.away)
        except Exception:
            books = []
        if len(books) >= 2:
            rlm_real = _resolve_rlm_real(match_id)
            result = engine.analyze_multi(books, rlm_real=rlm_real)
        else:
            # 单机构(或无DB记录): 当前/查询赔率做单快照分析
            odds_record = engine.query_odds_by_teams(match.home, match.away)
            if odds_record:
                # G6 修复: query_odds_by_teams 返回 OddsInput 对象(非dict), 用属性访问
                odds_input = OddsInput(
                    open_h=odds_record.open_h, open_d=odds_record.open_d, open_a=odds_record.open_a,
                    close_h=odds_record.close_h, close_d=odds_record.close_d, close_a=odds_record.close_a,
                )
                had_open = True  # 初盘数据可用 → drift 可算, honest_def 可触发
            else:
                # 无初盘数据: open=close 兜底, 显式标注 drift 不可用
                # (操盘手铁律: 不可把"无数据"当成"无陷阱")
                odds_input = OddsInput(
                    open_h=match.odds_h, open_d=match.odds_d, open_a=match.odds_a,
                    close_h=match.odds_h, close_d=match.odds_d, close_a=match.odds_a,
                )
                had_open = False
            result = engine.analyze(odds_input)

        return {
            "intent": result.intent.value if hasattr(result.intent, 'value') else str(result.intent),
            "intent_confidence": round(result.intent_confidence, 3),
            "drift_pattern": result.drift_pattern,
            "mispricing_score": round(result.mispricing_score, 3),
            "expected_edge": round(result.expected_edge, 3),
            "kelly_fraction": round(result.kelly_fraction, 3),
            "recommended_bet": result.recommended_bet,
            "verdict": result.verdict,
            # 操盘手框架扩展字段
            "n_books": result.n_books,
            "cross_book_sync": result.cross_book_sync,
            "confirmed": result.confirmed,
            "clv_beat": result.clv_beat,
            "rlm_proxy": result.rlm_proxy,
            "rlm_real": result.rlm_real,   # G4: 真 bet-split (None=用代理)
            "single_book_only": result.single_book_only,
            # 跨庄分歧 soft-line 概率调整 (OOS验证: 分歧→淡共识热门)
            "softline_adjusted_probs": result.softline_adjusted_probs,
            "disagreement_detected": result.disagreement_detected,
            "softline_fade_applied": result.softline_fade_applied,
            # honest_def 低权重次级修正 (仅DB路径有drift时激活)
            "honest_def_target": result.honest_def_target,
            "honest_def_applied": result.honest_def_applied,
            "honest_def_weight": result.honest_def_weight,
            # G6: drift 可用性显式标注 — True=初盘命中(可算drift/honest_def), False=无初盘(不可误判为"无陷阱")
            "drift_available": had_open,
        }
    except Exception as e:
        logger.warning(f"ReverseOddsEngine 分析失败 (降级): {e}")
        return None

# ═══ 实时 OIP 预测端点 (v6.0 锁定架构: 市场argmax方向 + OIP比分/OU + 平局信号) ═══
#  这些端点直接复用 pipeline.score_model + pipeline.draw_signal, 独立于旧 v7 引擎。
#  懒加载 pipeline, 任何导入异常只影响本组端点, 不破坏 bridge 启动。
DRAW_ALERT = 0.26
HIGH_VIG = 0.12
# P0-1 soft-line 决策闭环开关: False=灰度(soft-line仅展示, 主决策仍信共识argmax);
# True=开启后, 跨庄方向性分歧触发淡化的概率回灌 compute_value_layer 驱动主 BET 决策.
ENABLE_SOFTLINE_DECISION = False
_LIVE_DIRECTION = ["主胜", "平局", "客胜"]


def _compute_trap_detector(oh, od, oa, ph, pd, pa, market_conf, direction,
                           hcp_line, hcp_home_odds, hcp_away_odds,
                           ou_line, over_water, under_water, league,
                           lambda_h, lambda_a) -> Dict[str, Any]:
    """初盘陷阱识别 (Trap Detector) — 透明规则引擎。
    返回 trap_score(0-100), traps_fired[], trap_verdict。
    L1 深盘穿盘缺口 | L2 胜赔-让球背离 | L3 赛事先验(大巴) | L4 滚球漂移(赛前提示) | L5 大小球诱盘。
    仅对"有数据的层"计分; 无对应输入则跳过该层。"""
    import math
    traps: list = []
    score = 0

    # ---- L1 深盘穿盘缺口 ----
    abs_line = abs(hcp_line) if hcp_line is not None else 0.0
    if abs_line >= 1.25:
        if abs_line >= 2.0:
            fair_cover = 0.30
        elif abs_line >= 1.5:
            fair_cover = 0.40
        else:
            fair_cover = 0.47
        # 市场隐含"大胜"期望 ≈ 主胜隐含概率(深盘即逼你信大胜); 若有亚盘赔率则用其反推
        if hcp_home_odds and hcp_away_odds and hcp_home_odds > 0 and hcp_away_odds > 0:
            s = 1.0 / hcp_home_odds + 1.0 / hcp_away_odds
            fav_is_home = hcp_home_odds < hcp_away_odds
            implied_cover = (1.0 / hcp_home_odds) / s if fav_is_home else (1.0 / hcp_away_odds) / s
            gap = implied_cover - fair_cover
            gap_src = "亚盘反推"
        else:
            gap = market_conf - fair_cover
            gap_src = "主胜隐含"
        if gap >= 0.30:
            pts, sev = 35, "high"
        elif gap >= 0.20:
            pts, sev = 25, "mid"
        elif gap >= 0.10:
            pts, sev = 15, "low"
        else:
            pts, sev = 0, "low"
        if pts > 0:
            score += pts
            traps.append({"layer": "L1", "label": "深盘穿盘缺口",
                          "detail": f"主胜隐含{round(market_conf*100)}%, 但深盘(|{abs_line}|)历史穿盘仅{round(fair_cover*100)}% → 缺口{round(gap*100)}pp ({gap_src})",
                          "severity": sev})

    # ---- L2 胜赔/让球背离度 ----
    if hcp_line is not None and abs_line >= 0.25:
        L = abs_line
        # 赢盘(非走盘)所需净胜球: 整数盘(如-1.0)需+1, 非整数盘(如-2.25)取上整
        win_margin = (int(L) + 1) if L == math.floor(L) else math.ceil(L)
        exp_margin = (lambda_h or 0) - (lambda_a or 0)
        divergence = win_margin - exp_margin
        if divergence >= 1.5:
            pts, sev = 20, "high"
        elif divergence >= 0.75:
            pts, sev = 12, "mid"
        else:
            pts, sev = 0, "low"
        if pts > 0:
            score += pts
            traps.append({"layer": "L2", "label": "让球过深背离",
                          "detail": f"盘口需净胜{win_margin}球才赢盘, OIP期望净胜{round(exp_margin,2)}球 → 背离{round(divergence,2)}球",
                          "severity": sev})

    # ---- L3 赛事先验 (大巴战术) ----
    if league:
        lg = str(league).lower()
        bus_kw = ['qualifier', '资格赛', '杯', 'cup', 'uefa', 'champions', '欧冠', '欧战',
                  'fa-', 'copa', 'afc', 'concacaf', 'nations', '两回合', 'knockout', '淘汰', 'playoff']
        if any(k in lg for k in bus_kw):
            score += 15
            traps.append({"layer": "L3", "label": "赛事先验:大巴战术",
                          "detail": f"赛事'{league}'属杯赛/资格赛 → 弱队死守, 强队难穿盘/难大球",
                          "severity": "mid"})

    # ---- L5 大小球诱盘 (大球线高估) ----
    if ou_line is not None:
        exp_total = (lambda_h or 0) + (lambda_a or 0)
        over_trap = False
        detail = ""
        if ou_line >= 3.0 and exp_total < ou_line - 0.5:
            over_trap = True
            detail = f"大{ou_line}需≥{int(math.ceil(ou_line))}球, OIP期望总进球{round(exp_total,2)} → 大球被高估"
        elif abs_line >= 1.5 and ou_line >= 3.0:
            over_trap = True
            detail = f"深盘(|{abs_line}|)+大{ou_line}组合=屠杀局包装, 弱队死守实际难大球"
        if over_trap:
            score += 20
            traps.append({"layer": "L5", "label": "大小球诱盘",
                          "detail": detail, "severity": "high"})

    # ---- R5 一边倒强队折扣 (降低误报: 极强热门深盘穿盘概率更高) ----
    if market_conf >= 0.62:
        raw = score
        score = round(score * 0.85)
        traps.append({"layer": "R5", "label": "一边倒强队折扣",
                      "detail": f"主胜隐含{round(market_conf*100)}%≥62% → 强队深盘穿盘概率上调, 陷阱分×0.85 ({raw}→{score})",
                      "severity": "low", "exempt": True})

    # ---- L4 滚球漂移 (赛前提示, 仅当已有陷阱信号时提示回溯) ----
    if score >= 40:
        traps.append({"layer": "L4", "label": "滚球漂移监控",
                      "detail": "开赛45分钟内主胜跳升>25%且平赔腰斩→回溯确认本陷阱盘",
                      "severity": "low", "monitor": True})

    score = min(100, score)
    if score >= 70:
        verdict = f"⚠️ 初盘深让+大球双重陷阱(评分{score}): 主胜方向可信, 但深盘与大球均为诱盘, 勿碰深盘/大球"
    elif score >= 40:
        verdict = f"谨慎: 检出初盘诱盘信号(评分{score})"
    else:
        verdict = f"未检出明显初盘陷阱(评分{score})"
    return {"trap_score": score, "traps_fired": traps, "trap_verdict": verdict}


def _build_cs_score_odds(books):
    """[score_str, odds] 或 [book, score_str, odds] 列表 → {(i,j): 跨庄最优十进制赔率}。
    取同一比分跨庄的最高赔率(最优价)。无有效项返回 {}。"""
    if not books:
        return {}
    best = {}
    for entry in books:
        try:
            if len(entry) >= 3:
                s, o = entry[1], float(entry[2])
            elif len(entry) == 2:
                s, o = entry[0], float(entry[1])
            else:
                continue
            if not isinstance(s, str) or "-" not in s:
                continue
            i_s, j_s = s.split("-")
            i, j, o = int(i_s), int(j_s), float(o)
            if o <= 1:
                continue
            key = (i, j)
            if key not in best or o > best[key]:
                best[key] = o
        except (ValueError, TypeError, AttributeError, IndexError):
            continue
    return best


# WC 波胆命中率校准 (canon源: wc_all_matches 313场, 2014-2026, 20×70/30 OOS):
# 调参仅在train/eval仅在test → goal_scale=1.35 使 top3 命中率 29.7%→34.4%(+4.7pp),
# 优于旧值1.199(31.5%)。仅WC生效; 经验收缩α/Dixon-Colesρ会拉低top3, 不采用。
WC_OIP_GOAL_SCALE = 1.35

# WC 波胆过自信修正 (来源: data/wc_calibration.json overconfidence.ratio_x, 基于运行时goal_scale=1.35重测):
# 重测(2026-07-11): 模型TOP1均概率0.1306 vs 真实命中0.1136 → 把握被高估~1.15倍。
# (旧1.93是在goal_scale=1.0低估总进球、概率堆在少数比分上造成的假象, 已废弃。)
# 仅WC生效: 传给 correct_score_value 做温度收缩(p_eff=p/overconf)后再算EV,
# 把"小edge假价值"压成负EV→PASS, 避免WC上"EV>0即BET"亏钱。非WC联赛=None(不收缩)。
WC_CS_OVERCONF = 1.15

# ═══ 34 联赛赛程目录 (The Odds API sport_key → 中文名+分类) ═══
LEAGUE_CATALOG: Dict[str, Dict[str, str]] = {
    # 五大联赛 (核心)
    "soccer_epl":                     {"name": "英超",       "category": "五大联赛"},
    "soccer_spain_la_liga":           {"name": "西甲",       "category": "五大联赛"},
    "soccer_italy_serie_a":           {"name": "意甲",       "category": "五大联赛"},
    "soccer_germany_bundesliga":      {"name": "德甲",       "category": "五大联赛"},
    "soccer_france_ligue_one":        {"name": "法甲",       "category": "五大联赛"},
    # 英格兰联赛
    "soccer_efl_champ":               {"name": "英冠",       "category": "英格兰联赛"},
    "soccer_england_league1":         {"name": "英甲",       "category": "英格兰联赛"},
    "soccer_england_league2":         {"name": "英乙",       "category": "英格兰联赛"},
    "soccer_england_efl_cup":         {"name": "联赛杯",     "category": "英格兰联赛"},
    # 德国联赛
    "soccer_germany_bundesliga2":     {"name": "德乙",       "category": "德国联赛"},
    "soccer_germany_liga3":           {"name": "德丙",       "category": "德国联赛"},
    "soccer_germany_dfb_pokal":       {"name": "德国杯",     "category": "德国联赛"},
    # 北欧
    "soccer_sweden_allsvenskan":      {"name": "瑞典超",     "category": "北欧"},
    "soccer_sweden_superettan":       {"name": "瑞典甲",     "category": "北欧"},
    "soccer_norway_eliteserien":      {"name": "挪威超",     "category": "北欧"},
    "soccer_denmark_superliga":       {"name": "丹麦超",     "category": "北欧"},
    "soccer_finland_veikkausliiga":   {"name": "芬兰超",     "category": "北欧"},
    # 美洲
    "soccer_brazil_serie_a":          {"name": "巴甲",       "category": "美洲"},
    "soccer_brazil_serie_b":          {"name": "巴乙",       "category": "美洲"},
    "soccer_argentina_primera_division": {"name": "阿根廷",  "category": "美洲"},
    "soccer_mexico_ligamx":           {"name": "墨西哥",     "category": "美洲"},
    "soccer_usa_mls":                 {"name": "MLS",        "category": "美洲"},
    "soccer_conmebol_copa_libertadores":  {"name": "解放者杯", "category": "美洲"},
    "soccer_conmebol_copa_sudamericana":  {"name": "南美杯",   "category": "美洲"},
    # 亚洲/其他
    "soccer_china_superleague":       {"name": "中超",       "category": "亚洲/其他"},
    "soccer_korea_kleague1":          {"name": "韩K联",      "category": "亚洲/其他"},
    "soccer_ireland_premier":         {"name": "爱尔兰超",   "category": "亚洲/其他"},
    "soccer_japan_j1_league":         {"name": "日职联",     "category": "亚洲/其他"},
    # 杯赛/国际
    "soccer_fifa_world_cup":          {"name": "世界杯",     "category": "杯赛/国际"},
    "soccer_uefa_europa_league":      {"name": "欧联杯",     "category": "杯赛/国际"},
    "soccer_uefa_champs_league":      {"name": "欧冠",       "category": "杯赛/国际"},
    "soccer_scotland_premiership":    {"name": "苏格兰超",   "category": "杯赛/国际"},
    "soccer_switzerland_superleague": {"name": "瑞士超",     "category": "杯赛/国际"},
    "soccer_austria_bundesliga":      {"name": "奥地利超",   "category": "杯赛/国际"},
}

# 联赛赛程缓存 (sport_key → {fetched_at, fixtures}), 1小时过期
_LEAGUE_FIXTURE_CACHE: Dict[str, Dict] = {}


def _live_predict(home, away, oh, od, oa,
                  home_norm=None, away_norm=None, date=None, league=None,
                  extra_bookmakers=None, correct_score_books=None,
                  hcp_line=None, hcp_home_odds=None, hcp_away_odds=None,
                  ou_line=None, over_water=None, under_water=None) -> Dict[str, Any]:
    """真实1X2赔率 -> 全链路预测 (与 scripts/predict_live.py 同构)。返回结构化 dict。"""
    from pipeline.score_model import predict_score, deoverround
    from pipeline.draw_signal import market_draw_prob, consensus_draw_signal, draw_alert_with_booster
    if extra_bookmakers:
        from pipeline.draw_signal import multi_bookmaker_consensus
    import numpy as np
    from pipeline.deep_report import (compute_value_layer, consensus_probs,
                                      ou_value, draw_consensus_value,
                                      correct_score_value)
    oh = float(oh); od = float(od); oa = float(oa)
    ph, pd, pa = deoverround(oh, od, oa)
    # 抽水(overround)必须用原始赔率倒数和算, deoverround 已去抽水(和为1)不能复用
    overround = (1.0 / oh + 1.0 / od + 1.0 / oa) - 1.0

    # ① 市场隐含概率 + 抽水
    # ② 1X2 方向 = 市场 argmax (生产默认 ENABLE_ML_MARKET_OVERRIDE=OFF)
    best = max((ph, 0), (pd, 1), (pa, 2))
    direction = _LIVE_DIRECTION[best[1]]
    market_conf = best[0]

    # ③ OIP 比分 / 大小球
    # WC 比赛应用校准后的 goal_scale 修正OIP低估总进球; 非WC不受影响
    is_wc = bool(league and "WC" in str(league).upper())
    r = predict_score(home_norm or home, away_norm or away, oh, od, oa,
                      goal_scale=WC_OIP_GOAL_SCALE if is_wc else 1.0)
    M = r["matrix"]; mg = M.shape[0] - 1
    ov25 = float(sum(M[i, j] for i in range(mg + 1) for j in range(mg + 1) if i + j >= 3))
    ov15 = float(sum(M[i, j] for i in range(mg + 1) for j in range(mg + 1) if i + j >= 2))
    ov35 = float(sum(M[i, j] for i in range(mg + 1) for j in range(mg + 1) if i + j >= 4))
    flat = M.flatten()
    order = np.argsort(-flat)[:3]
    top3 = [tuple(int(x) for x in divmod(int(k), mg + 1)) for k in order]
    top3_prob = [float(flat[k]) for k in order]

    # ⑩ 价值层 (L0 深度决策): 跨庄共识概率 vs 跨庄最优价 → edge/EV/凯利/情景PnL
    # 诚实约束(v6铁律): 模型对1X2无超额信息优势 → "模型概率"取跨庄共识隐含概率;
    # 真实 edge 仅来自跨庄价差(soft line)。单庄时共识=该庄 → edge≈0 → 强制PASS。
    price_books = [[oh, od, oa]]
    if extra_bookmakers:
        for bk in extra_bookmakers:
            if len(bk) >= 4:
                try:
                    hh, dd, aa = float(bk[1]), float(bk[2]), float(bk[3])
                    inv = 1.0 / hh + 1.0 / dd + 1.0 / aa
                    if 1.0 < inv < 1.30:    # 过滤混入的让球盘(负抽水), 仅留合法 1X2 价
                        price_books.append([hh, dd, aa])
                except (ValueError, TypeError):
                    pass
    best_odds = [max(p[0] for p in price_books),
                 max(p[1] for p in price_books),
                 max(p[2] for p in price_books)]
    cons = consensus_probs(price_books)   # 跨庄共识隐含概率(诚实估计)

    # ⑥.5 操盘手 soft-line 分歧检测 (前置: 结果同时驱动决策回灌与展示)
    # 专测"跨庄对谁热门看法不一致" → 触发概率淡化(edge来自不平衡, OOS验证0.41).
    # 开关ON且触发淡化 → cons 被 adjusted_probs 覆盖, 下方 compute_value_layer 用淡后概率(P0-1闭环);
    # 无论开关, 始终挂 value_layer["softline"] 展示供人工复核(灰度期开关默认OFF).
    _sl_fade = False
    _sl_adj = None
    _sl_display = None
    if extra_bookmakers and len(extra_bookmakers) >= 2:
        try:
            from pipeline.reverse_odds_engine import ReverseOddsEngine as _ROE, OddsInput as _ROI
            _eng = _ROE()
            _books = []
            for _bk in extra_bookmakers:
                if len(_bk) >= 4:
                    try:
                        _hh, _dd, _aa = float(_bk[1]), float(_bk[2]), float(_bk[3])
                        _inv = 1.0 / _hh + 1.0 / _dd + 1.0 / _aa
                        if 1.0 < _inv < 1.30:   # 仅合法 1X2 盘, 过滤让球/变盘线
                            _books.append(_ROI(open_h=_hh, open_d=_dd, open_a=_aa,
                                               close_h=_hh, close_d=_dd, close_a=_aa))
                    except (ValueError, TypeError, ZeroDivisionError):
                        pass
            if len(_books) >= 2:
                _res = _eng.analyze_multi(_books)
                _sl_fade = _res.softline_fade_applied
                _sl_adj = _res.softline_adjusted_probs
                _sl_display = {
                    "n_books": _res.n_books,
                    "disagreement_detected": _res.disagreement_detected,
                    "softline_fade_applied": _res.softline_fade_applied,
                    "consensus_probs": [round(float(x), 4) for x in _res.implied_probs],
                    "adjusted_probs": ([round(float(x), 4) for x in _res.softline_adjusted_probs]
                                       if _res.softline_adjusted_probs else None),
                    "clv_beat": _res.clv_beat,
                    "honest_def_target": _res.honest_def_target,
                    "honest_def_applied": _res.honest_def_applied,
                    "honest_def_weight": _res.honest_def_weight,
                    "verdict": _res.verdict,
                }
        except Exception as _e:
            logger.debug(f"soft-line 检测失败(非致命): {_e}")

    # P0-1 闭环: 跨庄分歧触发淡化时, 用 soft-line 调整后概率覆盖共识, 驱动主 BET 决策
    if ENABLE_SOFTLINE_DECISION and _sl_fade and _sl_adj:
        cons = list(_sl_adj)

    single_book = len(price_books) <= 1
    value_layer = compute_value_layer(
        odds=best_odds,
        model_probs=cons,
        overround=overround,
    )
    value_layer["best_odds"] = [round(x, 3) for x in best_odds]
    value_layer["books_count"] = len(price_books)
    # soft-line 展示字段 (始终挂, 供人工复核; 决策是否采用由开关控制)
    value_layer["softline"] = _sl_display
    if single_book:
        # 单庄: 共识=该庄, edge 不可证伪 → 仅展示, 不下注结论
        value_layer["decision"] = "PASS"
        value_layer["best_direction"] = "PASS"
        value_layer["single_book"] = True
        value_layer["decision_text"] = "PASS · 单庄无独立定价验证，edge 不可证伪→不接盘"
        value_layer["scenario"] = {"direction": None,
                                   "note": "单庄模式: 价值层仅展示 edge/EV, 不下注结论"}

    # ④ 平局信号 (操盘手一手定价)
    m_pd = market_draw_prob(oh, od, oa)
    draw_alert = m_pd >= DRAW_ALERT

    # ⑤ 跨庄家共识 (优先: extra_bookmakers > WH×IW > 回退市场P平)
    consensus = None
    if extra_bookmakers:
        try:
            consensus = multi_bookmaker_consensus(extra_bookmakers)
            consensus["source"] = "multi_bookmaker"
        except Exception:
            consensus = None
    if not consensus and home_norm and away_norm and date and league:
        try:
            consensus = consensus_draw_signal(home_norm, away_norm, oh, od, oa, date, league)
            consensus["source"] = "WH×IW"
        except Exception:
            consensus = None

    # G5 · consensus booster: 双庄共识 strong → 平局预警阈值 0.26→0.24 (设计见 draw_bookmaker_validation.md)
    # consensus 不可用(单庄/WC无IW→available=False/strong=False)时回退纯市场 P 平
    draw_alert = draw_alert_with_booster(m_pd, consensus)

    # ⑥ 风控护栏
    high_vig = overround > HIGH_VIG

    # ⑦ 让球盘口分析 (可选增强)
    handicap = None
    if hcp_line is not None and hcp_home_odds is not None and hcp_away_odds is not None:
        try:
            line = float(hcp_line)
            h_odds = float(hcp_home_odds)
            a_odds = float(hcp_away_odds)
            # 亚盘隐含概率 (去抽水)
            hcp_sum_inv = (1.0 / h_odds + 1.0 / a_odds) if h_odds > 0 and a_odds > 0 else 1
            if hcp_sum_inv > 0:
                hcp_ph_raw = (1.0 / h_odds) / hcp_sum_inv
                hcp_pa_raw = (1.0 / a_odds) / hcp_sum_inv
            else:
                hcp_ph_raw, hcp_pa_raw = 0.5, 0.5

            # 深浅让判定
            abs_line = abs(line)
            if abs_line >= 1.25:
                depth_label = "深让"
                depth_color = "deep"
            elif abs_line >= 0.75:
                depth_label = "中深"
                depth_color = "medium"
            elif abs_line >= 0.25:
                depth_label = "浅让"
                depth_color = "shallow"
            else:
                depth_label = "平手盘"
                depth_color = "level"

            # 方向判定: 主让(负线)=主队减N球; 客让/主受让(正线)=客队减N球
            # 亚盘方向=赔率较低的一方(庄家看好的一方)
            if h_odds < a_odds:
                hcp_dir = "主让赢" if line < 0 else "受让赢"
                hcp_fav = "home"
            else:
                hcp_dir = "客让赢" if line > 0 else "受让赢"
                hcp_fav = "away"

            # 与1X2方向一致性检查
            dir_map = {"主胜": "home", "平局": "draw", "客胜": "away"}
            x12_fav = dir_map.get(direction, "")
            consistent = (hcp_fav == x12_fav or direction in ("主胜", "客胜") and (
                (line < 0 and hcp_fav == "home" and direction == "主胜") or
                (line < 0 and hcp_fav == "away" and direction == "客胜")
            ))

            # TaoGe 策略标签 (四维铁律)
            tao_ge = []
            if abs_line >= 1.0:
                tao_ge.append("深让: 胜+平")
            elif abs_line >= 0.25:
                tao_ge.append("浅让: 胜+平")
            if direction == "客胜":
                tao_ge.append("⚠️ 永不让负")

            # 让球overround
            hcp_overround = max(0, (hcp_sum_inv - 1.0)) * 100

            if consistent:
                advice = "亚盘与1X2同向, 可作置信增强"
            else:
                advice = ("亚盘与1X2反向: 历史验证显示分歧时1X2命中68%、亚盘仅10%, "
                          "亚盘反向多为噪声 → 请以1X2为准")

            handicap = {
                "line": round(line, 2),
                "line_str": f"{line:+g}" if line != 0 else "0",
                "home_odds": round(h_odds, 2),
                "away_odds": round(a_odds, 2),
                "depth_label": depth_label,
                "depth_color": depth_color,
                "abs_line": round(abs_line, 2),
                "direction": hcp_dir,
                "fav_side": hcp_fav,
                "implied_p_home": round(hcp_ph_raw, 4),
                "implied_p_away": round(hcp_pa_raw, 4),
                "consistent_with_x12": bool(consistent),
                "x12_direction": direction,
                "advice": advice,
                "tao_ge_tags": tao_ge,
                "hcp_overround_pct": round(hcp_overround, 2),
                "note": f"{'✅' if consistent else '⚠️'} 1X2({direction})与亚盘({hcp_dir}){'一致' if consistent else '分歧→信1X2'}",
            }
        except Exception as he:
            handicap = {"error": str(he), "note": "让球数据解析失败, 不影响1X2预测"}

    # ⑧ 操盘手视角 (playbook v2 固化: 7条落地规则, 来自 WC2026 88场逐场回测)
    op_rules = [{
        "id": "R1", "label": "一级信号=市场argmax",
        "detail": f"方向={direction} (置信 {round(market_conf*100,1)}%)",
        "rule": "反抽水取赔率argmax为一级信号", "color": "blue"
    }]
    hcp_ok = bool(handicap) and not handicap.get("error")
    if draw_alert:
        op_rules.append({"id":"R2","label":"防平预警","detail":f"P(平)={round(m_pd*100,1)}% ≥ 26% → 需防平局","rule":"P(平)≥26%触发防平","color":"amber"})
    if hcp_ok and handicap.get("consistent_with_x12") is False:
        op_rules.append({"id":"R3","label":"分歧盘:信1X2弃亚盘","detail":"亚盘与1X2反向, 历史验证1X2命中68%/亚盘10% → 亚盘当噪声","rule":"分歧盘一律信1X2","color":"amber"})
    if hcp_ok and handicap.get("depth_color")=="deep":
        op_rules.append({"id":"R4","label":"深盘:信赢球避穿盘","detail":"深盘favorite穿盘率仅47%, 但赢球率高 → 赌赢球别追穿","rule":"深盘难穿,AH=Margin非Winner","color":"blue"})
    if market_conf >= 0.62:
        op_rules.append({"id":"R5","label":"一边倒强队","detail":f"fav概率{round(market_conf*100,1)}% ≥ 62% → 正路稳, 可重仓","rule":"一边倒强队可重仓","color":"green"})
    if high_vig:
        op_rules.append({"id":"R6","label":"高抽水降权","detail":f"抽水{round(overround*100,1)}% > 12% → 信息质量差, 降权","rule":"高水降权","color":"red"})
    if hcp_ok and handicap.get("consistent_with_x12") and handicap.get("depth_color")!="deep":
        op_rules.append({"id":"R7","label":"亚盘增强维度","detail":"亚盘与1X2同向, 可作Margin置信增强","rule":"亚盘仅作增强维度","color":"blue"})

    stake = "标准"
    if market_conf >= 0.62 and not (hcp_ok and handicap.get("consistent_with_x12") is False):
        stake = "重仓"
    if high_vig:
        stake = "谨慎"
    verdict = [f"主信号: {direction}"]
    if draw_alert: verdict.append("配防平")
    if hcp_ok and handicap.get("consistent_with_x12") is False: verdict.append("弃亚盘信1X2")
    if market_conf >= 0.62: verdict.append("强队正路")

    # ⑨ 初盘陷阱识别 (Trap Detector) — 透明规则引擎 L1-L5
    trap = _compute_trap_detector(
        oh=oh, od=od, oa=oa, ph=ph, pd=pd, pa=pa, market_conf=market_conf,
        direction=direction, hcp_line=hcp_line, hcp_home_odds=hcp_home_odds,
        hcp_away_odds=hcp_away_odds, ou_line=ou_line, over_water=over_water,
        under_water=under_water, league=league,
        lambda_h=r.get("lh"), lambda_a=r.get("la"))
    trap_score = trap["trap_score"]
    if trap_score >= 70:
        stake = "回避"
        op_rules.append({"id": "R8", "label": "初盘深让陷阱",
                         "detail": trap["trap_verdict"],
                         "rule": "深盘+大球组合诱盘", "color": "red"})
        verdict.append("初盘陷阱→回避")
    elif trap_score >= 40 and stake == "重仓":
        stake = "谨慎"
        verdict.append("陷阱信号→重仓降谨慎")

    operator_view = {
        "rules_fired": op_rules,
        "primary_signal": direction,
        "confidence_pct": round(market_conf*100,1),
        "verdict": " · ".join(verdict),
        "stake_hint": stake,
        "rule_count": len(op_rules),
        "trap_score": trap_score,
        "trap_verdict": trap["trap_verdict"],
        "traps_fired": trap["traps_fired"],
    }

    # ⑪ 子市场价值层 (P1): 大小球(跨市场不一致) / 平局共识(跨庄溢价) / 波胆(模型扫描)
    # 诚实约束: 子市场 edge 只来自跨盘/跨庄价差, 绝不"模型 vs 同源盘"。
    sub_markets = {}
    # 大小球: 需 OU 盘口 + 大/小水位
    if ou_line is not None and over_water and under_water:
        try:
            sub_markets["ou"] = ou_value(
                oh, od, oa, float(ou_line), float(over_water), float(under_water),
                model_m=M.tolist())
        except Exception:
            pass
    # 平局共识: 需跨庄/WH×IW 共识 P(平) (单庄时 consensus=None → 跳过, 不可证伪)
    cons_pd = None
    cons_strong = False
    if consensus:
        if consensus.get("source") == "multi_bookmaker":
            cons_pd = consensus.get("mean_pd")
            cons_strong = bool(consensus.get("strong"))
        else:  # WH×IW
            cons_pd = consensus.get("consensus") or consensus.get("mean_pd")
            cons_strong = bool(consensus.get("strong"))
    best_draw = min((p[1] for p in price_books), default=od)
    if cons_pd is not None:
        try:
            sub_markets["draw"] = draw_consensus_value(
                oh, od, oa, consensus_pd=cons_pd, strong=cons_strong,
                best_draw_odds=best_draw)
        except Exception:
            pass
    # 波胆价值层/扫描: 统一入口 correct_score_value。
    # 有跨庄CS盘→真实edge(按EV排序); 无CS盘→诚实概率扫描(decision=SCAN, 不伪称edge)。
    try:
        cs_score_odds = _build_cs_score_odds(correct_score_books)
        sub_markets["correct_score"] = correct_score_value(
            M.tolist(), score_odds=cs_score_odds if cs_score_odds else None, top_n=3,
            overconf=WC_CS_OVERCONF if is_wc else None)
    except Exception:
        pass

    return {
        "home": home, "away": away,
        "odds": {"oh": oh, "od": od, "oa": oa},
        "market_prob": {"h": round(ph, 4), "d": round(pd, 4), "a": round(pa, 4)},
        "overround": round(overround, 4),
        "direction": direction,
        "market_conf": round(market_conf, 4),
        "oip": {
            "lambda_h": r["lh"], "lambda_a": r["la"],
            "top3_scores": [f"{h}-{a}" for (h, a) in top3],
            "top3_prob": [round(p, 4) for p in top3_prob],
            "over15": round(ov15, 4), "over25": round(ov25, 4), "over35": round(ov35, 4),
        },
        "draw_signal": {"market_pdraw": round(m_pd, 4), "draw_alert": draw_alert},
        "consensus": consensus,
        "risk": {"high_vig": high_vig},
        "handicap": handicap,
        "operator_view": operator_view,
        "value_layer": value_layer,
        "sub_markets": sub_markets,
    }


def _persist_bet_record(home, away, value_layer, oh, od, oa,
                        league=None, match_date=None, source="prediction",
                        sub_markets=None) -> Optional[int]:
    """决策闭环: 将单场价值层结论落库 bet_records (主市场1X2), 并将子市场 BET 决策
    落库 submarket_bets (P1, 专用表, 不污染1X2列)。PASS 也记录以便回补 ROI。
    非致命, 失败仅告警。返回主市场 bet_id; 失败返回 None。"""
    import sqlite3
    try:
        db_path = os.path.join(PROJECT_ROOT, "data", "football_data.db")
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        rows = value_layer.get("rows", [])
        mod = value_layer.get("model_prob", [0.0, 0.0, 0.0])
        best = value_layer.get("best_direction", "PASS")
        best_edge = value_layer.get("best_edge_pct", 0.0)
        # 取最优方向的凯利半仓比与 EV
        kelly_half = ev = 0.0
        for r in rows:
            if r["outcome"] == best:
                kelly_half = r.get("kelly_half", 0.0)
                ev = r.get("ev", 0.0)
                break
        predicted = best if best != "PASS" else None
        confidence = max(mod) if mod else 0.0
        cur.execute(
            """INSERT INTO bet_records
               (match_id, home_team, away_team, league, match_date, bet_type, source,
                predicted_result, verdict_text, confidence,
                home_prob, draw_prob, away_prob,
                home_odds, draw_odds, away_odds,
                value_gap, kelly, expected_value, notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (None, home, away, league, match_date, "recommendation", source,
             predicted, value_layer.get("decision_text", ""), confidence,
             mod[0], mod[1], mod[2],
             oh, od, oa,
             round(best_edge, 2), round(kelly_half, 4), round(ev, 4),
             f"edge={best_edge:.2f}%, decision={value_layer.get('decision')}"),
        )
        bet_id = cur.lastrowid

        # ── P1: 子市场闭环落库 ──
        if sub_markets:
            cur.execute(
                """CREATE TABLE IF NOT EXISTS submarket_bets (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       home_team TEXT, away_team TEXT, league TEXT, match_date TEXT,
                       market TEXT, selection TEXT, model_prob REAL, best_odds REAL,
                       value_gap REAL, kelly REAL, expected_value REAL,
                       decision TEXT, decision_text TEXT,
                       actual_result TEXT, is_correct INTEGER,
                       actual_score TEXT, resolved_at TEXT, notes TEXT,
                       created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
            _persist_submarkets(cur, home, away, league, match_date, sub_markets)

        conn.commit()
        conn.close()
        return bet_id
    except Exception as e:
        logger.warning(f"bet_records 落库失败(非致命): {e}")
        return None


def _persist_submarkets(cur, home, away, league, match_date, sub_markets):
    """将子市场 BET 决策写入 submarket_bets (仅 BET, PASS 不落, 减少噪音)。"""
    def insert(market, selection, model_prob, best_odds, ev, kelly, decision, text):
        if decision != "BET":
            return
        cur.execute(
            """INSERT INTO submarket_bets
               (home_team, away_team, league, match_date, market, selection,
                model_prob, best_odds, value_gap, kelly, expected_value,
                decision, decision_text)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (home, away, league, match_date, market, selection,
             model_prob, best_odds, round((ev or 0), 4), round((kelly or 0), 4),
             round((ev or 0), 4), decision, text))
    # 大小球
    ou = sub_markets.get("ou")
    if ou and ou.get("decision") == "BET":
        sc = ou.get("scenario", {})
        side = sc.get("side")
        if side:
            odds = ou.get("over_odds") if side == "over" else ou.get("under_odds")
            insert("OU", f"{side}_{ou.get('ou_line')}",
                   ou.get("model_p_over") if side == "over" else ou.get("model_p_under"),
                   odds, ou.get("ev_over_pct") if side == "over" else ou.get("ev_under_pct"),
                   None, "BET", ou.get("decision_text", ""))
    # 平局共识
    dr = sub_markets.get("draw")
    if dr and dr.get("decision") == "BET":
        insert("DRAW_CONSENSUS", "D", dr.get("consensus_pd"), dr.get("best_odds"),
               dr.get("ev_pct"), None, "BET", dr.get("decision_text", ""))
    # 波胆 (仅当 correct_score_value 给了 BET; 扫描模式无 decision 键则跳过)
    cs = sub_markets.get("correct_score")
    if isinstance(cs, dict) and cs.get("decision") == "BET":
        for r in cs.get("rows", [])[:1]:
            insert("CS", r.get("score"), r.get("prob"), r.get("odds"),
                   r.get("ev_pct"), r.get("kelly_half"), "BET", cs.get("decision_text", ""))


class LivePredictRequest(BaseModel):
    """单场真实赔率预测请求"""
    home: str
    away: str
    oh: float
    od: float
    oa: float
    home_norm: Optional[str] = None
    away_norm: Optional[str] = None
    date: Optional[str] = None
    league: Optional[str] = None
    # 多庄家共识 (可选): 每项 [name, oh, od, oa]
    # 传此字段时自动调用 multi_bookmaker_consensus 替代/补充 IW 共识
    extra_bookmakers: Optional[list[list]] = None

    # 让球盘口 (可选, 亚盘分析增强)
    hcp_line: Optional[float] = None       # 让球数: 负=主让(-0.5/-1), 正=客让/主受让(+0.5/+1)
    hcp_home_odds: Optional[float] = None   # 主让赔率
    hcp_away_odds: Optional[float] = None   # 客让(受让)赔率

    # 大小球 (可选, 陷阱扫描 L5 用)
    ou_line: Optional[float] = None         # 大小球线: 2.5/3.0/3.5
    over_water: Optional[float] = None      # 大球赔率
    under_water: Optional[float] = None     # 小球赔率

    # 跨庄波胆价 (可选): 每项 [score_str, odds] 或 [book, score_str, odds]
    # 提供时 correct_score_value 走真实 edge(BET/PASS); 缺失→诚实 SCAN(仅fair value)
    correct_score_books: Optional[list] = None

    # 决策闭环 (P0): record=True 时将本次价值层结论落库 bet_records, 供后续 ROI 回补
    record: bool = False


@app.post("/api/predict/live")
async def predict_live_api(req: LivePredictRequest):
    """单场真实1X2赔率 -> 锁定架构全链路预测 (方向=市场argmax, OIP比分/OU, 平局信号, 让球分析)"""
    try:
        out = _live_predict(req.home, req.away, req.oh, req.od, req.oa,
                            home_norm=req.home_norm, away_norm=req.away_norm,
                            date=req.date, league=req.league,
                            extra_bookmakers=req.extra_bookmakers,
                            correct_score_books=req.correct_score_books,
                            hcp_line=req.hcp_line, hcp_home_odds=req.hcp_home_odds,
                            hcp_away_odds=req.hcp_away_odds,
                            ou_line=req.ou_line, over_water=req.over_water,
                            under_water=req.under_water)
        # 决策闭环: record=True 时落库 bet_records, 返回 bet_id 供后续 ROI 回补
        if req.record:
            bet_id = _persist_bet_record(
                req.home, req.away, out.get("value_layer", {}),
                req.oh, req.od, req.oa,
                league=req.league, match_date=req.date, source="prediction",
                sub_markets=out.get("sub_markets", {}))
            if bet_id is not None:
                out["bet_recorded"] = True
                out["bet_id"] = bet_id
        return _wrap_data(out)
    except Exception as e:
        logger.error(f"实时预测失败: {e}", exc_info=True)
        return _wrap_data({"error": f"预测失败: {e}"})


@app.get("/api/live/wc")
async def live_wc_api():
    """实时拉取在跑世界杯比赛赔率并预测 (经 The Odds API)。key 失效优雅报错。"""
    try:
        from pipeline.collectors.sp_odds_api import SPOddsAPI
    except Exception as e:
        return _wrap_data({"error": f"采集器加载失败: {e}"})
    try:
        api = SPOddsAPI()
        matches = api.get_odds("soccer_fifa_world_cup")
    except Exception as e:
        return _wrap_data({"error": f"实时拉取失败(可能key过期/无额度): {type(e).__name__}: {e}",
                           "hint": "在 pipeline/collectors/config.ini 填有效 The Odds API key"})
    if not matches:
        return _wrap_data({"matches": [], "note": "该赛事当前无在跑比赛或返回0场"})
    results = []
    for m in matches:
        h2h = m.get("best_h2h") or {}
        if not h2h:
            continue
        # 真实多庄明细 → extra_bookmakers (触发 cross-book 共识 + soft-line 分歧检测)
        bm = m.get("bookmakers_detail") or []
        extra = [[bk["name"], bk["h"], bk["d"], bk["a"]]
                 for bk in bm
                 if all(k in bk for k in ("name", "h", "d", "a"))]
        try:
            o = _live_predict(m.get("home_team"), m.get("away_team"),
                              h2h.get("home"), h2h.get("draw"), h2h.get("away"),
                              home_norm=m.get("home_team"), away_norm=m.get("away_team"),
                              date=m.get("commence_time"), league=None,
                              extra_bookmakers=extra if len(extra) >= 2 else None)
            try:
                api.save_to_db(m)
            except Exception:
                pass
            o["fixture"] = {"home": m.get("home_team"), "away": m.get("away_team"),
                            "commence_time": m.get("commence_time"), "sport_key": "soccer_fifa_world_cup"}
            results.append(o)
        except Exception as e:
            logger.warning(f"WC单场预测跳过 {m.get('home_team')}: {e}")
    return _wrap_data({"matches": results,
                       "captured_at": datetime.now(timezone.utc).isoformat()})


class ReplayRequest(BaseModel):
    """库内 football-data.co.uk 真实赔率回放 (验证模式, 显示真实赛果)"""
    edition: int = 2026
    limit: int = 10


@app.post("/api/predict/live/replay")
async def replay_api(req: ReplayRequest):
    """从 wc_xlsx_matches 读真实赔率回放, 显示方向命中(市场argmax基线)。"""
    import sqlite3
    db_path = os.path.join(PROJECT_ROOT, "data", "football_data.db")
    try:
        con = sqlite3.connect(db_path); cur = con.cursor()
        cur.execute(
            """SELECT home_norm, away_norm, date, oh, od, oa, hg, ag, stage
               FROM wc_xlsx_matches WHERE edition=? AND oh IS NOT NULL ORDER BY date LIMIT ?""",
            (req.edition, req.limit))
        rows = cur.fetchall(); con.close()
    except Exception as e:
        return _wrap_data({"error": f"库内回放失败: {e}"})
    results = []; hits = 0; known = 0
    for (h, a, d, oh, od, oa, hg, ag, stage) in rows:
        try:
            o = _live_predict(h, a, oh, od, oa, home_norm=h, away_norm=a, date=d, league=None)
        except Exception as e:
            logger.warning(f"回放单场跳过 {h}: {e}"); continue
        actual = f"{hg}-{ag}" if hg is not None and ag is not None else "未知"
        correct = (o["direction"] == "主胜" and hg > ag) or \
                  (o["direction"] == "平局" and hg == ag) or \
                  (o["direction"] == "客胜" and hg < ag)
        if hg is not None and ag is not None:
            known += 1
            hits += 1 if correct else 0
        o["actual"] = actual
        o["direction_correct"] = correct
        results.append(o)
    acc = hits / known if known else 0
    return _wrap_data({"edition": req.edition, "n": len(results),
                       "direction_acc": round(acc, 4), "matches": results})


@app.get("/api/backtest")
async def backtest_api():
    """返回 WC2026 全量逐场回测明细 (odds_db/operator_backtest_full.json)。"""
    path = os.path.join(PROJECT_ROOT, "odds_db", "operator_backtest_full.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return _wrap_data(data)
    except Exception as e:
        return _wrap_data({"error": f"读取回测数据失败: {e}"})


# ═══ 联赛赛程 ═══
@app.get("/api/leagues")
async def leagues_api():
    """返回 34 联赛目录 (按分类分组, 含各联赛可赛程数)。"""
    from pipeline.collectors.sp_odds_api import SPOddsAPI
    try:
        api = SPOddsAPI()
        available = set()
        if api.get_remaining_requests() > 0:
            try:
                sports = api.get_sports()
                available = {s["key"] for s in sports if s.get("group") == "Soccer"}
            except Exception:
                logger.warning("联赛列表拉取失败, 用全量目录兜底")
        else:
            logger.warning("API 额度不足, 联赛可用性标记全部为未知")
    except Exception:
        available = set()

    categories: Dict[str, list] = {}
    for sk, info in LEAGUE_CATALOG.items():
        cat = info["category"]
        entry = {"sport_key": sk, "name": info["name"],
                 "available": sk in available if available else True,
                 "fixture_count": len(_LEAGUE_FIXTURE_CACHE.get(sk, {}).get("fixtures", []))}
        categories.setdefault(cat, []).append(entry)

    cat_order = ["五大联赛", "英格兰联赛", "德国联赛", "北欧", "美洲", "亚洲/其他", "杯赛/国际"]
    result = [{"category": c, "leagues": categories.get(c, [])} for c in cat_order if c in categories]
    return _wrap_data({"categories": result, "total_leagues": len(LEAGUE_CATALOG)})


@app.get("/api/leagues/{sport_key}/fixtures")
async def league_fixtures_api(sport_key: str):
    """获取指定联赛未来赛程 (带 1 小时缓存, 来源 The Odds API)。"""
    sk = sport_key
    info = LEAGUE_CATALOG.get(sk)
    if not info:
        return _wrap_data({"error": f"未知联赛: {sk}", "fixtures": []})

    # 缓存检查
    cache = _LEAGUE_FIXTURE_CACHE.get(sk)
    if cache:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(cache["fetched_at"])).total_seconds()
        if age < 3600:
            return _wrap_data({"sport_key": sk, "name": info["name"], "category": info["category"],
                               "fixtures": cache["fixtures"], "cached": True, "cache_age_s": int(age)})

    # 实时拉取
    from pipeline.collectors.sp_odds_api import SPOddsAPI
    try:
        api = SPOddsAPI()
        matches = api.get_odds(sk)
    except Exception as e:
        # 缓存兜底 (即使过期)
        if cache:
            return _wrap_data({"sport_key": sk, "name": info["name"], "category": info["category"],
                               "fixtures": cache["fixtures"], "cached": True,
                               "stale": True, "note": f"实时拉取失败({e}), 返回缓存"})
        return _wrap_data({"error": f"获取失败: {e}", "fixtures": []})

    fixtures = []
    for m in matches:
        h2h = m.get("best_h2h", {})
        fixtures.append({
            "id": m.get("id", ""),
            "home": m.get("home_team", ""),
            "away": m.get("away_team", ""),
            "commence_time": m.get("commence_time", ""),
            "odds_h": h2h.get("home"),
            "odds_d": h2h.get("draw"),
            "odds_a": h2h.get("away"),
            "bookmakers_count": len(m.get("bookmakers_raw", [])),
        })

    _LEAGUE_FIXTURE_CACHE[sk] = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "fixtures": fixtures,
    }
    return _wrap_data({"sport_key": sk, "name": info["name"], "category": info["category"],
                       "fixtures": fixtures, "cached": False})


# ═══ 赔率 Widget URL (服务端注入 API Key, 前端只拿 URL 嵌 iframe) ═══
@app.get("/api/widget-url")
async def widget_url_api(
    sport_key: str,
    bookmaker_keys: str = "pinnacle",
    odds_format: str = "decimal",
    markets: str = "h2h,spreads,totals",
):
    """返回 The Odds API 赔率 widget URL。
    API key 由服务端注入, 不暴露给前端 bundle。
    query params 提供默认值, 前端可按需覆盖。
    """
    # 优先用 widget 专用 key, 回退到通用 THE_ODDS_API_KEY
    access_key = os.getenv("WIDGET_ACCESS_KEY") or os.getenv("THE_ODDS_API_KEY")
    if not access_key:
        return _wrap_data({"error": "未配置 THE_ODDS_API_KEY 或 WIDGET_ACCESS_KEY 环境变量"})

    widget_url = (
        f"https://widget.the-odds-api.com/v1/sports/{sport_key}/events/"
        f"?accessKey={access_key}"
        f"&bookmakerKeys={bookmaker_keys}"
        f"&oddsFormat={odds_format}"
        f"&markets={markets}"
    )
    return _wrap_data({
        "widget_url": widget_url,
        "sport_key": sport_key,
        "bookmaker_keys": bookmaker_keys,
        "odds_format": odds_format,
        "markets": markets,
    })


# ═══ 赔率实时匹配 (单场预测用, 非历史回测) ═══
@app.get("/api/match-odds")
async def match_odds_api(home: str, away: str):
    """按主客队名匹配**实时赔率**(不是历史数据)。
    优先级: live_odds_raw(实时采集) → The Odds API(实时拉取) → 提示手动录入。
    铁律: 不查 odds_features 历史库 (那是库内回放/逐场回测用的)。
    """
    import sqlite3 as _sq
    import json as _json
    db_path = os.path.join(PROJECT_ROOT, "data", "football_data.db")

    # ── 1) live_odds_raw 实时采集表 ──
    try:
        con = _sq.connect(db_path); cur = con.cursor()
        cur.execute(
            """SELECT home_team, away_team, best_h2h, commence_time, sport_key
               FROM live_odds_raw
               WHERE (home_team LIKE ? OR home_team_en LIKE ?)
                 AND (away_team LIKE ? OR away_team_en LIKE ?)
               ORDER BY captured_at DESC LIMIT 1""",
            (f"%{home}%", f"%{home}%", f"%{away}%", f"%{away}%"))
        row = cur.fetchone()
        con.close()
        if row:
            h2h = _json.loads(row[2] or "{}")
            if h2h.get("home"):
                return _wrap_data({
                    "matched": True, "source": "live",
                    "home": row[0], "away": row[1],
                    "open_h": h2h["home"], "open_d": h2h["draw"], "open_a": h2h["away"],
                    "commence_time": row[3], "league": row[4],
                    "note": f"实时采集 {row[3]} ({row[0]} vs {row[1]})",
                })
    except Exception:
        pass

    # ── 2) The Odds API 实时拉取 ──
    try:
        from pipeline.collectors.sp_odds_api import SPOddsAPI
        api = SPOddsAPI()
        if api.get_remaining_requests() > 0:
            for sk in ["soccer_fifa_world_cup", "soccer_epl", "soccer_spain_la_liga",
                       "soccer_italy_serie_a", "soccer_germany_bundesliga", "soccer_france_ligue_one",
                       "soccer_uefa_champs_league", "soccer_uefa_europa_league"]:
                try:
                    for m in api.get_odds(sk):
                        h = m.get("home_team", ""); a = m.get("away_team", "")
                        h_en = m.get("home_team_en", ""); a_en = m.get("away_team_en", "")
                        if ((home.lower() in h.lower() or home.lower() in h_en.lower()) and
                            (away.lower() in a.lower() or away.lower() in a_en.lower())):
                            h2h = m.get("best_h2h", {})
                            return _wrap_data({
                                "matched": True, "source": "api",
                                "home": h, "away": a,
                                "open_h": h2h.get("home"), "open_d": h2h.get("draw"), "open_a": h2h.get("away"),
                                "commence_time": m.get("commence_time", ""), "league": sk,
                                "note": f"实时API {m.get('commence_time','')} ({h} vs {a})",
                            })
                except Exception:
                    continue
    except Exception:
        pass

    # ── 3) 无实时赔率 ──
    return _wrap_data({"matched": False, "note": "无实时赔率, 请手动录入当日报价"})


# ═══ 模拟投注 (paper betting) — 赛程页内嵌下注闭环 ═══
@app.get("/api/bets")
async def bets_list_api(limit: int = 100, offset: int = 0, status: str = ""):
    """查询模拟投注记录 (bet_records 表)。
    Args:
        limit/offset: 分页
        status: "resolved"(已结算) / "pending"(未结算) / ""(全部)
    """
    import sqlite3
    try:
        db_path = os.path.join(PROJECT_ROOT, "data", "football_data.db")
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        where = ""
        params: list = []
        if status == "resolved":
            where = "WHERE actual_result IS NOT NULL"
        elif status == "pending":
            where = "WHERE actual_result IS NULL"
        rows = conn.execute(
            f"""SELECT bet_id, match_id, home_team, away_team, league, match_date,
                      bet_type, source, predicted_result, confidence,
                      home_odds, draw_odds, away_odds, kelly, expected_value,
                      actual_result, is_correct, actual_score, resolved_at, created_at
               FROM bet_records {where}
               ORDER BY bet_id DESC LIMIT ? OFFSET ?""",
            (limit, offset),
        ).fetchall()
        total = conn.execute(f"SELECT COUNT(*) FROM bet_records {where}").fetchone()[0]
        conn.close()
        bets = [dict(r) for r in rows]
        return _wrap_data({"bets": bets, "total": total, "limit": limit, "offset": offset})
    except Exception as e:
        return _wrap_data({"error": f"查询失败: {e}", "bets": [], "total": 0})


@app.post("/api/bets")
async def bets_place_api(request: Request):
    """手动模拟下注 (赛程页内嵌触发)。
    请求体 JSON: {home_team, away_team, league, home_odds, draw_odds, away_odds,
             bet_side('H'/'D'/'A'), stake_amount, confidence?}
    写入 bet_records (source='manual', bet_type='paper_bet')。
    """
    import sqlite3
    try:
        req = await request.json()
    except Exception:
        req = {}
    home = req.get("home_team", "")
    away = req.get("away_team", "")
    league = req.get("league", "")
    oh = float(req.get("home_odds", 0) or 0)
    od = float(req.get("draw_odds", 0) or 0)
    oa = float(req.get("away_odds", 0) or 0)
    side = req.get("bet_side", "")
    stake = float(req.get("stake_amount", 0) or 0)
    confidence = float(req.get("confidence", 0) or 0)

    if not home or not away or side not in ("H", "D", "A"):
        return _wrap_data({"error": "参数缺失: 需 home_team, away_team, bet_side(H/D/A)"})
    if oh <= 1 or od <= 1 or oa <= 1:
        return _wrap_data({"error": "赔率无效: 须 > 1.0"})

    # 隐含概率 (去 overround)
    inv = 1/oh + 1/od + 1/oa
    ph, pd, pa = (1/oh)/inv, (1/od)/inv, (1/oa)/inv
    probs = {"H": ph, "D": pd, "A": pa}
    p_true = probs[side]
    odds_map = {"H": oh, "D": od, "A": oa}
    o_side = odds_map[side]

    # 基础凯利 (半凯利, 封顶10%), 与 bet_core 一致
    b = o_side - 1
    kelly_full = (b * p_true - (1 - p_true)) / b if b > 0 else 0
    kelly_half = max(0, kelly_full * 0.5)
    # 若前端未传 stake, 用默认本金3000的半凯利建议
    if stake <= 0:
        stake = round(3000 * min(kelly_half, 0.10), 1)

    try:
        db_path = os.path.join(PROJECT_ROOT, "data", "football_data.db")
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO bet_records
               (match_id, home_team, away_team, league, bet_type, source,
                predicted_result, confidence, home_prob, draw_prob, away_prob,
                home_odds, draw_odds, away_odds, kelly, expected_value, notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (None, home, away, league, "executed", "manual",
             side, confidence, ph, pd, pa,
             oh, od, oa, round(kelly_half, 4), round(kelly_full, 4),
             f"手动模拟下注 {side} @{o_side}, 注码¥{stake}"),
        )
        bet_id = cur.lastrowid
        conn.commit()
        conn.close()
        return _wrap_data({
            "bet_id": bet_id, "home_team": home, "away_team": away, "league": league,
            "bet_side": side, "odds": o_side, "stake_amount": stake,
            "kelly_half": round(kelly_half, 4), "implied_prob": round(p_true, 4),
            "message": f"已记录模拟下注: {home} vs {away} → {side} @{o_side} ¥{stake}",
        })
    except Exception as e:
        return _wrap_data({"error": f"下注失败: {e}"})


# ═══ SPA fallback — 必须注册在所有显式路由之后 ═══
if os.path.exists(FRONTEND_DIR):
    @app.get("/{full_path:path}")
    async def serve_frontend(full_path: str = ""):
        """SPA fallback — 捕获所有未匹配路径, 返回 index.html"""
        index_path = os.path.join(FRONTEND_DIR, "index.html")
        if os.path.exists(index_path):
            from fastapi.responses import FileResponse
            return FileResponse(index_path)
        return {"error": "frontend not built"}


if __name__ == "__main__":
    import uvicorn
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 9000
    host = os.getenv("API_HOST", "0.0.0.0")
    logger.info(f"启动 FootballAI Bridge @ {host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="info")
