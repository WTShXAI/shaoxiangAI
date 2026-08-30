"""
哨响AI 独立桥接服务 (FootballAI Bridge)
=========================================
哨响AI v7.4 — 优化版规则流水线预测引擎 (DrawExpert + 17报告决策树)

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
import time
import logging
import threading
import sqlite3
import asyncio
from typing import Any, Dict, List, Optional, Union
from datetime import datetime, timedelta, timezone

# ── 加载 .env (使 THEODDS_API_KEY 等环境变量可用) ──
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# ── 项目根入 sys.path，确保 pipeline 包可导入 ──
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from fastapi import FastAPI, HTTPException, Request
from starlette.websockets import WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger("football_bridge")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)

# ── V7.1 复盘链路: gq.db 分析缓存函数 (加载失败→复盘链路停用, 主预测不受影响) ──
try:
    from gq.db import (save_analysis, correct_analysis, backfill_all,
                       query_analysis_cache, ensure_analysis_cache)
    _GQ_DB_OK = True
except Exception as _gq_e:
    logger.warning(f"[analysis_cache] gq.db 加载失败, 复盘链路停用: {_gq_e}")
    _GQ_DB_OK = False

# ── 分析中心扫描引擎 (比赛分析页面主数据源) ──
try:
    from pipeline.analysis_center import run_scan as _analysis_scan
    _ANALYSIS_CENTER_OK = True
except Exception as _ace:
    logger.warning(f"[analysis_center] 加载失败: {_ace}")
    _ANALYSIS_CENTER_OK = False

# ── 冷门波胆检测模型 (赛前CS网格 + 正路盘口交叉验证闭环) ──
try:
    from analysis.cold_door_model import predict_for_grid as _cold_predict
    from pipeline.analysis_center import cs_top3_from_market as _regular_cs_top3
    _COLD_OK = True
except Exception as _cle:
    logger.warning(f"[cold_door] 加载失败: {_cle}")
    _COLD_OK = False

# ── 滚球破蛋概率仪 (Live Goal Probe) ──
try:
    from analysis.live_goal_probe import probe_match as _live_goal_probe, list_live_matches as _live_goal_matches, analyze_three_market as _analyze_three_market, list_three_market_candidates as _list_three_market_candidates
    from analysis.backtest_live_goal_probe import main as _backtest_live_goal_probe
    _LIVE_GOAL_OK = True
except Exception as _lge:
    logger.warning(f"[live_goal_probe] 加载失败: {_lge}")
    _LIVE_GOAL_OK = False

# ── 分钟级数据流 (Minute-level stream: 盘口+比分时间线/进球事件/逐分钟破蛋曲线) ──
try:
    from analysis.minute_level_stream import get_match_minute_stream as _get_match_minute_stream
    _MINUTE_STREAM_OK = True
except Exception as _mse:
    logger.warning(f"[minute_level_stream] 加载失败: {_mse}")
    _MINUTE_STREAM_OK = False

# ── 模板偏差实时扫描 ──
try:
    from pipeline.template_deviation_api import scan_live_matches as _template_scan
    _TEMPLATE_DEV_OK = True
except Exception as _tde:
    logger.warning(f"[template_deviation] 加载失败: {_tde}")
    _TEMPLATE_DEV_OK = False

# ── 加载核心引擎 (v7.4 双引擎: wc/league) ──
_DEFAULT_ENGINE = os.getenv("ENGINE", "wc")
ENGINE = None
_ENGINE_REGISTRY: Dict[str, Any] = {}
MatchInput = None
_ENGINE_LOAD_OK = False

# ── 赔率初始快照 (Req2: 每场比赛首次出现时记录开盘赔率, 用于初始vs实时对比) ──
# key = f"{home}|{away}|{commence_time}", value = {odds_h, odds_d, odds_a, ah_*, ou_*, snapshot_at}
_INITIAL_ODDS_SNAPSHOT: Dict[str, Any] = {}

# ── 自动赛果记录 (Req3: feed 检测到 match_state<0 时自动记录) ──
# key = f"{home}|{away}|{date}", value = {result, score, opening_odds, closing_odds, recorded_at}
_AUTO_RESULTS: Dict[str, Any] = {}


# ── 运动类型过滤(2026-08-27): 只对外暴露足球, 排除篮球/板球/棒球/排球/网球/电竞/虚拟等 ──
# 关键词黑名单(覆盖 BSKT/IPBL/WNBA/IPL板球/篮球/棒球/排球/网球/UFC/NFL/F1/电竞/虚拟足球等)
# league 文本含任一关键词 → 视为非足球, 响应层排除。改此清单改一处即生效。
NON_FOOTBALL_LEAGUE_KEYWORDS = [
    # 篮球 (含单字"篮"覆盖 澳篮联/篮球/女篮/男篮 等所有篮球联赛命名)
    "篮球", "篮", "BSKT", "WNBA", "NBA", "MPBL", "PBA", "NBL", "ABL", "B联赛", "CBA",
    # 板球/棒球/排球/网球/高尔夫/手球/曲棍球
    "板球", "棒球", "排球", "网球", "高尔夫", "手球", "曲棍球", "马球", "羽毛球", "乒乓球", "壁球",
    # 格斗/美式足球
    "拳击", "UFC", "MMA", "美式足球", "NFL", "澳式足球", "AFL", "击剑",
    # 赛车
    "F1", "F2", "F3", "MotoGP",
    # 冰球
    "冰球", "NHL",
    # 电竞/虚拟体育(明确非足球, 包括电子足球)
    "电竞", "电子竞技", "FIFAe", "eSports", "Esports",
    # 乐鱼虚拟电子足球(8分钟电子赛, 与"梦幻对垒"虚拟)
    "VS-", "瓦尔哈拉", "瓦尔基里", "梦幻对垒", "8分钟",
]


def is_football(league: Optional[str]) -> bool:
    """判断 league 名是否属于足球(是→True, 否→False)。空值视为足球兜底。"""
    if not league:
        return True
    return not any(kw in league for kw in NON_FOOTBALL_LEAGUE_KEYWORDS)


def _filter_football_matches(matches: list, league_key: str = "league") -> list:
    """对比赛字典列表做运动类型过滤, 非足球被丢弃。league_key 默认'league', 可改 sport_key。"""
    if not matches:
        return matches
    return [m for m in matches if is_football(m.get(league_key) or m.get("sport_key") or "")]


def _resolve_live_ou_anchor(match_key: str, minute: int = 0):
    """滚球锚 (2026-08-27 用户: 锚点用滚球数据, 不用固定数值).

    从 odds_snapshots 自动取该场【当前】滚球 OU 主盘线 + 去水隐含总球,
    取代固定 line=2.5 / 开盘锚 opening_total。复用 _current_inplay_odds(live 快照)。
    返回 (line, implied_total) 或 (None, None) — 无滚球数据(如未开赛)时诚实降级。
    """
    try:
        import sqlite3
        from analysis.live_goal_probe import _current_inplay_odds
        db = os.path.join(PROJECT_ROOT, "data", "events.db")
        con = sqlite3.connect(db, timeout=10)
        try:
            cur = _current_inplay_odds(con, match_key, minute or 0)
        finally:
            con.close()
        if not cur or not cur.get("ou"):
            return None, None
        ou_line, ou_over, ou_under = cur["ou"]
        if ou_line is None or ou_over is None or ou_under is None:
            return (float(ou_line), None) if ou_line else (None, None)
        try:
            po = (1.0 / float(ou_over)) / (1.0 / float(ou_over) + 1.0 / float(ou_under))
        except (ZeroDivisionError, TypeError, ValueError):
            po = 0.5
        # OU 隐含总球: total = line + 2*(p_over - 0.5) (1球=100%概率差)
        implied_total = round(float(ou_line) + 2.0 * (po - 0.5), 2)
        return float(ou_line), implied_total
    except Exception:
        return None, None


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


# ── ReverseOddsEngine 单例 (P2-3: 收敛 3 处独立实例化) ──
_REVERSE_ENGINE = None


def _get_reverse_engine():
    """懒加载 ReverseOddsEngine 单例。"""
    global _REVERSE_ENGINE
    if _REVERSE_ENGINE is None:
        from pipeline.reverse_odds_engine import ReverseOddsEngine
        _REVERSE_ENGINE = ReverseOddsEngine()
        logger.info("ReverseOddsEngine 单例初始化完成")
    return _REVERSE_ENGINE


# ── GQ 今日比赛时间轴客户端 (乐鱼体育, 纯 stdlib, 不依赖采集器) ──
try:
    import pipeline.gq_timeline as gqt
    _GQT_OK = True
except Exception as _e:
    gqt = None
    _GQT_OK = False
    logger.warning("pipeline.gq_timeline 导入失败(时间轴API将不可用): %s", _e)


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

# H7(2026-07-30): CORS 白名单而非 "*" — 防任意站点带凭证跨域请求.
# 默认仅放行同源前端 (从 :9000 加载). 跨机部署设 CORS_ORIGINS 环境变量.
_CORS_ORIGINS = [o.strip() for o in os.getenv(
    "CORS_ORIGINS", "http://localhost:9000,http://127.0.0.1:9000").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_credentials=(_CORS_ORIGINS != ["*"]),
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# ── 事故③ 根治: 注册统一错误信封 — 任何未捕获异常都返回 {"ok":false,"error":{code,message}},
#    message 恒为 str, 杜绝前端白屏 (core 缺失时降级跳过, 不阻断桥启动) ──
def _coerce_message_fallback(value):
    """core 不可用时的兜底: 保证 detail 恒为 str, 绝不透传裸对象(事故③同源坏点)。"""
    try:
        return str(value)
    except Exception:
        return "unspecified error"


# 先挂兜底, core 可用时再覆盖为 error_envelope.coerce_message (对象型 message→安全字符串).
coerce_message = _coerce_message_fallback

try:
    from core.error_envelope import (
        register_exception_handlers as _register_exception_handlers,
        coerce_message,  # 覆盖兜底: 任意对象(含 list[dict])→str, 杜绝前端白屏
    )
    _register_exception_handlers(app)
except Exception:
    # 极端情况 (core 不可 import): 用兜底 coerce_message, detail 仍为 str (降级路径)
    pass

# ═══ 速率限制中间件 (ECC security-review: 所有端点限流) ═══
# 进程内固定窗口: 按 (客户端IP, 路径前缀) 计数, 默认 120 次/分钟
# 仅作用于 /api/* 与 /predict/*; /health /ws 等健康与长连接豁免
_rate_lock = threading.Lock()
_rate_buckets: Dict[str, Any] = {}


def _rate_check(path: str, client_ip: str, limit_per_min: int) -> bool:
    """True=放行, False=限流"""
    if not (path.startswith("/api") or path.startswith("/predict")):
        return True
    now = time.time()
    # live-goal-probe/focus 是滚球神器高频轮询端点, 用独立桶(否则与其它 /api 共享 120/min 桶,
    # 多标签页 5s 轮询瞬间 429 风暴 → 前端"崩"). 其余 /api 仍共享默认桶.
    if path.startswith("/api/live-goal-probe") or path.startswith("/api/focus"):
        seg = "api_probe"
    else:
        seg = path.split('/')[1]
    key = f"{client_ip}|{seg}"
    with _rate_lock:
        bucket = _rate_buckets.get(key)
        if bucket is None or now - bucket[0] >= 60:
            _rate_buckets[key] = [now, 1]
            return True
        if bucket[1] >= limit_per_min:
            return False
        bucket[1] += 1
        return True


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api") or path.startswith("/predict"):
        client_ip = request.client.host if request.client else "unknown"
        # live-goal-probe 是滚球神器核心端点, 前端每 5s 轮询列表+详情+焦点(多标签页会成倍),
        # 与其他 /api 共享 120/min 桶会瞬间 429 风暴 → 前端"崩"(拿不到数据/反复错误)。
        # live-goal-probe/focus 是本地滚球神器高频轮询端点, 用户会开大量标签页。多次上调后
        # 4000/min 仍被瞬时峰值顶穿; 作为单机私有部署, 直接给到 8000/min 硬件上限, 并保留
        # 前端 Page Visibility 降低后台轮询。其余 /api 仍 120/min。
        if path.startswith("/api/live-goal-probe") or path.startswith("/api/focus"):
            limit = int(os.getenv("RATE_LIMIT_PROBE_PER_MIN", "8000"))
        else:
            limit = int(os.getenv("RATE_LIMIT_PER_MIN", "120"))
        if not _rate_check(path, client_ip, limit):
            # 429 短路绕过 CORSMiddleware → 必须手工补 CORS 头,
            # 否则浏览器看到 "Access-Control-Allow-Origin 缺失" 会把 preflight 通过后的真实请求当成失败。
            origin = request.headers.get("origin", "")
            echo_origin = origin if origin in _CORS_ORIGINS else (
                _CORS_ORIGINS[0] if _CORS_ORIGINS and _CORS_ORIGINS != ["*"] else "*")
            req_headers = request.headers.get("access-control-request-headers", "")
            return JSONResponse(
                status_code=429,
                content={"success": False, "error": {"code": "rate_limit_exceeded", "message": "请求过于频繁, 请稍后再试"}},
                headers={
                    "Access-Control-Allow-Origin": echo_origin,
                    "Access-Control-Allow-Credentials": "true" if _CORS_ORIGINS != ["*"] else "false",
                    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                    "Access-Control-Allow-Headers": req_headers or "*",
                    "Vary": "Origin",
                },
            )
    return await call_next(request)


# ═══ API Key 鉴权中间件 (H4 2026-07-30) ═══
# 仅当配置了 API_KEY 环境变量时才启用 (默认关闭, 保持单机私有部署兼容).
# 写操作 (POST/PUT/DELETE/PATCH 到 /api/*) 必须带 X-API-Key 头且匹配, 否则 401.
# 前端 api.ts 已支持从 VITE_API_KEY 注入该头. 生产部署设 API_KEY + 前端 VITE_API_KEY 即可收紧.
@app.middleware("http")
async def api_key_middleware(request: Request, call_next):
    api_key = os.getenv("API_KEY", "")
    if api_key:
        path = request.url.path
        is_write = request.method in ("POST", "PUT", "DELETE", "PATCH")
        if path.startswith("/api") and is_write:
            provided = request.headers.get("X-API-Key", "")
            if provided != api_key:
                origin = request.headers.get("origin", "")
                echo_origin = origin if origin in _CORS_ORIGINS else (
                    _CORS_ORIGINS[0] if _CORS_ORIGINS and _CORS_ORIGINS != ["*"] else "*")
                return JSONResponse(
                    status_code=401,
                    content={"success": False, "error": {"code": "unauthorized", "message": "缺少有效的 X-API-Key"}},
                    headers={
                        "Access-Control-Allow-Origin": echo_origin,
                        "Access-Control-Allow-Credentials": "true" if _CORS_ORIGINS != ["*"] else "false",
                        "Vary": "Origin",
                    },
                )
    return await call_next(request)


# ═══ API兼容中间件 — 拦截 /api/v1/* 返回空数据防前端崩溃 ═══
#  注意: 使用纯 ASGI 中间件, 避免 BaseHTTPMiddleware 破坏 WebSocket 连接
from starlette.responses import JSONResponse
from datetime import datetime, timezone as tz
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.exceptions import RequestValidationError

# ── 全局异常处理器：统一返回 JSON 信封, 杜绝后端任何异常→前端白屏 ──
@app.exception_handler(Exception)
async def _unhandled_exc_handler(request: Request, exc: Exception):
    logger.error(f"未捕获异常 [{request.url.path}]: {exc}", exc_info=True)
    return JSONResponse(status_code=500, content={"success": False, "data": None, "error": f"服务器内部错误: {type(exc).__name__}: {exc}"})

@app.exception_handler(ValueError)
async def _value_error_handler(request: Request, exc: ValueError):
    # 修H1(2026-07-30): 删除 numpy 特例 — 它把任何含"numpy"的真实业务 ValueError 吞成 200 success:None,
    # 导致前端静默失败. numpy 序列化降级应在序列化层(NumPyJSONResponse)处理, 不应在异常处理器.
    return await _unhandled_exc_handler(request, exc)

@app.exception_handler(StarletteHTTPException)
async def _http_exc_handler(request: Request, exc: StarletteHTTPException):
    return JSONResponse(status_code=exc.status_code, content={"success": False, "data": None, "error": exc.detail})

@app.exception_handler(RequestValidationError)
async def _validation_exc_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=422, content={"success": False, "data": None, "error": "参数校验失败", "detail": coerce_message(exc.errors())})


def _json_safe(obj):
    """终极 numpy→Python: json 往返消除一切 numpy 痕迹."""
    import json, numpy as np, logging
    _log = logging.getLogger("football_bridge")
    class NpEncoder(json.JSONEncoder):
        def default(self, o):
            if isinstance(o, (np.integer,)): return int(o)
            if isinstance(o, (np.floating,)): return float(o)
            if isinstance(o, (np.bool_,)): return bool(o)
            if isinstance(o, np.ndarray): return o.tolist()
            try:
                return super().default(o)
            except TypeError:
                _log.warning(f"[json_safe] 不可序列化类型: {type(o)}, 转str")
                return str(o)
    try:
        return json.loads(json.dumps(obj, cls=NpEncoder))
    except Exception as e:
        import traceback
        _log.error(f"[json_safe] 序列化失败: {e}")
        # 逐字段排查
        if isinstance(obj, dict):
            for k, v in obj.items():
                try: json.dumps({k: v}, cls=NpEncoder)
                except Exception as e2:
                    _log.error(f"[json_safe] 问题字段: {k} type={type(v)} err={e2}")
        return obj  # 回退原始对象, 让调用方处理

# ── Monkey-patch FastAPI encoder (必须放在 _json_safe 定义之后) ──
from fastapi import encoders as _enc
_orig_encode = _enc.jsonable_encoder
def _safe_encode(obj, *a, **kw):
    return _orig_encode(_json_safe(obj), *a, **kw)
_enc.jsonable_encoder = _safe_encode


def _wrap_data(data) -> dict:
    """包装为前端 ApiResponse<T> 格式"""
    return {
        "success": True,
        "data": data,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _resolve_mk(match_key: str) -> str:
    """2026-08-28: match_key 规范化 (IR-16 队名透传).
    GQ H5 实时队名 vs DB 历史队名存在音译差异(如 奎瓦特→塞瓦特 / 河床FC→河王FC),
    前端选中比赛后各端点按 match_key 精确查 DB 落空 → 全部 not found。
    这里统一"精确→模糊"两级解析, 失败原样返回(宁缺勿错)。"""
    if not match_key:
        return match_key
    try:
        from analysis.match_key_resolver import resolve_match_key
        import analysis.live_goal_probe as _lgp
        con = _lgp._open_gq()
        try:
            resolved = resolve_match_key(con, str(match_key))
        finally:
            try:
                con.close()
            except Exception:
                pass
        return resolved or str(match_key)
    except Exception:
        return str(match_key)


def _wrap_error(code: str, message: str, details=None, status: int = 400) -> JSONResponse:
    """统一错误信封 (ECC api-design: 语义化状态码 + 结构化错误体)"""
    return JSONResponse(
        status_code=status,
        content={"success": False, "error": {"code": code, "message": message, "details": details}},
    )


# ── 从实时赔率库加载赛程数据（优先） ──
def _resolve_team_cn(name: str) -> str:
    """将英文/混合队名解析为中文 canonical 名, 查 team_canonical 表"""
    if not name:
        return name
    # 已经是中文(含中文字符) 则直接返回
    if any('\u4e00' <= c <= '\u9fff' for c in name):
        return name
    try:
        db_path = os.path.join(PROJECT_ROOT, "data", "football_data.db")
        if not os.path.exists(db_path):
            return name
        import sqlite3
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        # 正则提取拉丁字母
        import re
        latin = ''.join(re.findall(r'[A-Za-z]+', name)).lower()
        if not latin:
            conn.close()
            return name
        rows = cur.execute(
            "SELECT canonical, aliases_json FROM team_canonical"
        ).fetchall()
        conn.close()
        best = name  # 默认返回原值
        for canon, aj in rows:
            aliases = json.loads(aj) if aj else []
            for a in aliases:
                if ''.join(re.findall(r'[A-Za-z]+', a)).lower() == latin:
                    # 优先取中文 canonical
                    if any('\u4e00' <= c <= '\u9fff' for c in canon):
                        return canon
                    if best == name:
                        best = canon
                    break
            if ''.join(re.findall(r'[A-Za-z]+', canon)).lower() == latin:
                if any('\u4e00' <= c <= '\u9fff' for c in canon):
                    return canon
                if best == name:
                    best = canon
        return best
    except Exception:
        pass
    return name


def _load_real_match_data(db_path: Optional[str] = None, days: int = 7):
    """优先读取 live_odds_raw 的最新赛事, 失败时回退 QF JSON。"""
    fixtures = []
    matches = []
    leagues = [{"code": "WC26", "name": "世界杯 2026", "country": "国际"}]

    db_path = db_path or os.path.join(PROJECT_ROOT, "data", "football_data.db")
    if os.path.exists(db_path):
        try:
            import sqlite3
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, sport_key, home_team, away_team, home_team_en, away_team_en,
                       commence_time, best_h2h, bookmakers_detail, captured_at
                FROM live_odds_raw
                WHERE commence_time IS NOT NULL
                AND id IN (
                    SELECT MAX(id) FROM live_odds_raw
                    WHERE commence_time IS NOT NULL
                    GROUP BY home_team, away_team, commence_time
                )
                ORDER BY commence_time ASC
                """
            )
            rows = cur.fetchall()
            conn.close()
        except Exception as e:
            logger.warning(f"读取 live_odds_raw 失败: {e}")
            rows = []
    else:
        rows = []

    now = datetime.now(timezone.utc)
    window_end = now + timedelta(days=days)
    for row in rows:
        try:
            commence_dt = datetime.fromisoformat((row["commence_time"] or "").replace("Z", "+00:00"))
        except Exception:
            continue
        if commence_dt < now - timedelta(days=1):
            continue
        if commence_dt > window_end:
            continue

        h2h = {}
        try:
            h2h = json.loads(row["best_h2h"] or "{}")
        except Exception:
            h2h = {}
        home = row["home_team"] or row["home_team_en"] or ""
        away = row["away_team"] or row["away_team_en"] or ""
        # 解析为中文队名
        home = _resolve_team_cn(home)
        away = _resolve_team_cn(away)
        fixture_id = row["id"]
        odds_h = h2h.get("home")
        odds_d = h2h.get("draw")
        odds_a = h2h.get("away")

        fixtures.append({
            "id": fixture_id,
            "home": home,
            "away": away,
            "time": row["commence_time"],
            "time_local": commence_dt.strftime("%H:%M"),
            "date_local": commence_dt.strftime("%m-%d"),
            "day_of_week": ["一", "二", "三", "四", "五", "六", "日"][commence_dt.weekday()],
            "group": "",
            "stage": row["sport_key"] or "world_cup",
            "status": "FINISHED" if commence_dt < now else "TIMED",
            "score_home": None,
            "score_away": None,
            "is_finished": commence_dt < now,
            "prediction": None,
            "odds_h": odds_h,
            "odds_d": odds_d,
            "odds_a": odds_a,
            "bookmakers_count": len(json.loads(row["bookmakers_detail"] or "[]")) if row["bookmakers_detail"] else 0,
        })

        matches.append({
            "id": str(fixture_id),
            "homeTeam": {"id": str(fixture_id), "name": home, "shortName": home[:3]},
            "awayTeam": {"id": str(fixture_id), "name": away, "shortName": away[:3]},
            "league": {"code": "WC26", "name": "世界杯 2026", "country": "国际"},
            "kickoff": row["commence_time"],
            "status": "finished" if commence_dt < now else "upcoming",
            "homeOdds": odds_h,
            "drawOdds": odds_d,
            "awayOdds": odds_a,
            "prediction": "",
            "confidence": 0,
        })

    if fixtures or matches:
        return fixtures, matches, leagues

    # 回退: QF 预测数据
    qf_path = os.path.join(PROJECT_ROOT, "data", "qf_predictions_repredict.json")
    try:
        with open(qf_path, encoding='utf-8') as f:
            qf_data = json.load(f)
    except Exception:
        return [], [], leagues

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
        fixtures.append({
            "id": idx,
            "home": home,
            "away": away,
            "time": f"2026-07-{5 + idx:02d}T00:00:00Z",
            "time_local": f"{5 + idx:02d}:00",
            "date_local": f"07-{5 + idx:02d}",
            "day_of_week": "一",
            "group": "",
            "stage": "quarterfinal",
            "status": "TIMED",
            "score_home": None,
            "score_away": None,
            "is_finished": False,
            "prediction": None,
            "odds_h": odds_h,
            "odds_d": odds_d,
            "odds_a": odds_a,
            "bookmakers_count": 0,
        })
        matches.append({
            "id": str(idx),
            "homeTeam": {"id": str(idx), "name": home, "shortName": home[:3]},
            "awayTeam": {"id": str(idx), "name": away, "shortName": away[:3]},
            "league": {"code": "WC26", "name": "世界杯 2026", "country": "国际"},
            "kickoff": f"2026-07-{5 + idx:02d}T00:00:00Z",
            "status": "upcoming",
            "homeOdds": odds_h,
            "drawOdds": odds_d,
            "awayOdds": odds_a,
            "prediction": m.get("verdict", ""),
            "confidence": m.get("confidence", 0),
        })

    return fixtures, matches, leagues


def _build_api_v1_stub(sub: str):
    """为 /api/v1/* 端点生成动态内容，优先用实时数据库数据。"""
    if sub in {"fixtures/upcoming", "fixtures/upcoming/"}:
        fixtures, matches, _ = _load_real_match_data(days=7)
        today = [f for f in fixtures if f.get("date_local") == datetime.now(timezone.utc).strftime("%m-%d")]
        tomorrow = [f for f in fixtures if f.get("date_local") != datetime.now(timezone.utc).strftime("%m-%d")][:4]
        return _wrap_data({
            "matches": fixtures,
            "days": 7,
            "upcoming_count": len(fixtures),
            "finished_count": sum(1 for f in fixtures if f.get("is_finished")),
            "cutoff": (datetime.now(timezone.utc) + timedelta(days=7)).strftime("%Y-%m-%d"),
            "today": today or fixtures[:3],
            "tomorrow": tomorrow or fixtures[3:6],
        })
    if sub in {"matches/list", "matches/list/"}:
        _, matches, _ = _load_real_match_data(days=7)
        return _wrap_data({"matches": matches, "total": len(matches)})
    if sub in {"matches/scores", "matches/scores/"}:
        _, matches, _ = _load_real_match_data(days=7)
        return _wrap_data(matches)
    if sub in {"historical/leagues", "historical/leagues/"}:
        _, _, leagues = _load_real_match_data(days=7)
        return _wrap_data(leagues)
    if sub in {"predict/stats", "predict/stats/"}:
        _, matches, _ = _load_real_match_data(days=7)
        return _wrap_data({
            "total": len(matches),
            "todayAccuracy": 0,
            "overallAccuracy": 0,
            "totalPredictions": len(matches),
            "hotLeagues": [{"league": "世界杯 2026", "count": len(matches)}],
        })
    if sub in {"predict/history", "predict/history/"}:
        return _wrap_data([])
    if sub in {"models/versions", "models/versions/"}:
        return _wrap_data([])
    if sub in {"data-quality/reports", "data-quality/reports/"}:
        return _wrap_data([])
    if sub in {"monitor/health", "monitor/health/"}:
        return _wrap_data({
            "status": "healthy",
            "uptime": 0,
            "apiLatency": 0,
            "predictionLatency": 0,
            "modelHealth": "healthy",
            "databaseHealth": "healthy",
            "memoryUsage": 0,
            "cpuUsage": 0,
        })
    if sub in {"monitor/metrics/summary", "monitor/metrics/summary/"}:
        return _wrap_data({
            "apiRequestsPerMin": 0,
            "avgResponseTime": 0,
            "predictionRequestsPerMin": 0,
            "errorRate": 0,
            "activeUsers": 0,
        })
    if sub in {"alerts/alerts", "alerts/alerts/"}:
        return _wrap_data([])
    return None

class APIV1CompatMiddleware:
    """纯 ASGI 中间件 — 不破坏 WebSocket 连接"""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith("/api/v1/"):
            sub = scope["path"][len("/api/v1/"):]
            stub = _build_api_v1_stub(sub)
            if stub is not None:
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
from fastapi import Request
from fastapi.staticfiles import StaticFiles


# SPA 缓存策略: 带 content-hash 的构建产物(/assets) → 永久缓存(文件名变即新文件);
# index.html 不缓存(no-cache), 保证每次导航都向服务器验证, 避免旧版SPA残留导致的"前端信息滞后"。
@app.middleware("http")
async def _static_cache_policy(request: Request, call_next):
    resp = await call_next(request)
    if request.url.path.startswith("/assets/"):
        resp.headers.setdefault("Cache-Control", "public, max-age=31536000, immutable")
    return resp


FRONTEND_DIR = os.path.join(PROJECT_ROOT, "frontend", "dist")
ASSETS_DIR = os.path.join(FRONTEND_DIR, "assets")
if os.path.exists(FRONTEND_DIR) and os.path.isdir(ASSETS_DIR):
    app.mount("/assets", StaticFiles(directory=ASSETS_DIR), name="assets")
    logger.info(f"[Bridge] 前端静态文件: {FRONTEND_DIR}")


# ═══ 交易 API 路由 (B-2): 信号列表 / 下单 / 结算 / 持仓 ═══
try:
    from pipeline.trading_api import trading_router
    app.include_router(trading_router)
    logger.info("[Bridge] 交易API路由已注册: /api/trading/*")
except Exception as _trade_e:
    logger.warning(f"[Bridge] 交易API路由加载失败 (Trading页面将不可用): {_trade_e}")


# ═══════════════════════════════════════════════════════
# WebSocket ConnectionManager (实时推送管理)
# ═══════════════════════════════════════════════════════
import asyncio as _asyncio

class ConnectionManager:
    """管理所有 WebSocket 连接, 支持 broadcast 到所有终端客户端"""
    def __init__(self):
        self._connections: list = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self._connections.append(ws)
        logger.info(f"[WS] 新连接, 当前 {len(self._connections)} 个")

    def disconnect(self, ws: WebSocket):
        if ws in self._connections:
            self._connections.remove(ws)
            logger.info(f"[WS] 断开, 剩余 {len(self._connections)} 个")

    async def broadcast(self, msg: dict):
        """向所有已连接客户端广播消息"""
        payload = json.dumps(msg, ensure_ascii=False)
        gone = []
        for ws in self._connections:
            try:
                await ws.send_text(payload)
            except Exception:
                gone.append(ws)
        for ws in gone:
            self.disconnect(ws)

ws_manager = ConnectionManager()

# ── 实时赔率摄入缓存: {match_key: [book_data]} ──
_ODDS_INGEST_CACHE: Dict[str, list] = {}


# ═══════════════════════════════════════════════════════
# 后台数据飞轮 (按方案第3章)
# ═══════════════════════════════════════════════════════
async def _daily_odds_loop():
    """每日00:05 + 启动时立即执行: 智能拉取活跃联赛赔率"""
    import asyncio
    while True:
        try:
            # 预算前置检查: 今日配额耗尽则跳过本次拉取
            from pipeline.collectors.api_budget import get_guard
            guard = get_guard()
            if not guard.can_spend(1):
                logger.warning(f"[飞轮] 日配额耗尽({guard.daily_used()}/{guard.daily_cap}), "
                               f"跳过本次赔率拉取")
            else:
                from pipeline.collectors.daily_collector import DailyCollector
                dc = DailyCollector()
                # to_thread 包裹同步阻塞采集, 不卡事件循环(否则 health 一直连不上)
                stats = await _asyncio.to_thread(dc.collect_daily_odds)
                logger.info(f"[飞轮] 每日赔率拉取: 采集{stats.get('collected',0)}场 "
                            f"活跃{len(stats.get('active_leagues',[]))}联赛 "
                            f"剩余配额{stats.get('remaining_quota','?')}")
                if stats.get("remaining_quota", 999) < 50:
                    logger.warning(f"[飞轮] ⚠️ API配额低: {stats['remaining_quota']}")
        except Exception as e:
            logger.error(f"[飞轮] 每日赔率拉取失败(非致命): {e}")
        # 等到明天凌晨00:05 (用 timedelta 跨月/跨年安全, 避免 day=now.day+1 在月末越界)
        now = datetime.now()
        target = now.replace(hour=0, minute=5, second=0, microsecond=0)
        if target <= now:
            from datetime import timedelta
            target = (now + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
        wait_sec = (target - now).total_seconds()
        logger.info(f"[飞轮] 下次赔率拉取: {target.isoformat()} (~{wait_sec//3600:.0f}h)")
        await asyncio.sleep(wait_sec)


async def _result_backfill_loop():
    """每6小时扫描回填赛果"""
    import asyncio
    while True:
        try:
            from pipeline.collectors.daily_collector import DailyCollector
            dc = DailyCollector()
            stats = await _asyncio.to_thread(dc.backfill_results)
            logger.info(f"[飞轮] 赛果回填: 扫描{stats.get('scanned',0)} "
                        f"回填{stats.get('backfilled',0)} 待手动{stats.get('pending',0)}")
        except Exception as e:
            logger.error(f"[飞轮] 赛果回填失败(非致命): {e}")
        await asyncio.sleep(6 * 3600)  # 6小时


async def _odds_features_sync_loop():
    """每24小时同步 odds_features 训练数据"""
    import asyncio
    while True:
        try:
            from pipeline.collectors.daily_collector import DailyCollector
            dc = DailyCollector()
            stats = await _asyncio.to_thread(dc.sync_to_odds_features)
            logger.info(f"[飞轮] odds_features同步: {stats.get('synced',0)}条 "
                        f"总行数{stats.get('total_odds_features',0)}")
        except Exception as e:
            logger.error(f"[飞轮] odds_features同步失败(非致命): {e}")
        await asyncio.sleep(24 * 3600)  # 24小时


async def _startup_probe():
    """首次启动探测活跃联赛 — 真正非阻塞(后台线程跑, 不卡 startup, health 立即可用)"""
    try:
        from pipeline.collectors.daily_collector import DailyCollector
        dc = DailyCollector()
        # 丢到后台线程执行, startup 不 await, health 立即响应
        stats = await _asyncio.to_thread(dc.collect_daily_odds, True)
        logger.info(f"[飞轮] 启动探测完成: 采集{stats.get('collected',0)}场 "
                    f"活跃{len(stats.get('active_leagues',[]))}联赛")
    except Exception as e:
        logger.warning(f"[飞轮] 启动探测失败(后台继续): {e}")


@app.on_event("startup")
async def _start_background_loops():
    """启动3个后台飞轮循环 + 恢复活跃联赛探测"""
    logger.info("[飞轮] 启动后台数据飞轮 (3后台循环)...")
    # 首次探测丢到后台任务, 不阻塞 startup 完成(否则 force_full 探测33联赛
    # 会卡住 uvicorn, health 一直连不上)
    _asyncio.create_task(_startup_probe())
    # 启动后台异步循环
    _asyncio.create_task(_daily_odds_loop())
    _asyncio.create_task(_result_backfill_loop())
    _asyncio.create_task(_odds_features_sync_loop())
    # 每10分钟采集一次 live_odds_raw (轻度循环, 与赔率主循环互补)
    _asyncio.create_task(_live_odds_mini_loop())
    # 实时比分轮询 (每 30s 拉一次 getMatchDetailPB, 仅对正在进行的比赛)
    # 2026-08-27: 雷速已删除, ws_collector 秒级推流(events.db matches.score/minute) 已覆盖,
    # 不再需要 leisu_live_scores 后台轮询.

    # 自主巡航 Agent (后台常驻: 扫描临场窗口+滚盘 → 硬阈值判定 → qwen3 写人话 → WS 推告警)
    try:
        from agent_cruise import start_cruise
        _asyncio.create_task(start_cruise(
            broadcast=ws_manager.broadcast,
            analyze=_live_operator_card_compute,
            cross_book=_get_cross_book_signal,
        ))
        logger.info("[cruise] autonomous cruise agent started")
    except Exception as e:
        logger.warning(f"[cruise] cruise agent start failed: {e}")

    # 2026-08-27: 模型引擎预热 — ModelRouter/unified_predictor v7.4 首次调用懒加载 ~29s,
    # 导致 bridge 重启后首个前端 analyze 请求 29s+ → 前端 30s 超时("timeout of 30000ms exceeded")。
    # 启动时后台预热引擎, 首个 analyze 请求命中已加载引擎 → 毫秒级。
    try:
        def _warm_model_engine():
            from pipeline.predictors.model_router import ModelRouter
            ModelRouter.analyze(
                home="预热引擎", away="预热引擎",
                h=2.0, d=3.2, a=3.8,
                ou_line=None, ah_line=None, ah_home=None, ah_away=None,
            )
            logger.info("[飞轮] model engine warmed (analyze 首请求不再 29s)")

        async def _warm():
            await _asyncio.to_thread(_warm_model_engine)
        _asyncio.create_task(_warm())
        logger.info("[飞轮] model engine warmup started (后台)")
    except Exception as e:
        logger.warning(f"[飞轮] model engine warmup failed: {e}")


async def _live_odds_mini_loop():
    """每30分钟轻度拉取活跃联赛 (补充 daily_odds_loop, 捕捉临场变盘).

    原 10 分钟过于频繁, 在 2万/月套餐下一天可烧 ~144+ 次调用.
    改为 30 分钟 + 预算前置检查(护栏硬闸兜底), 彻底止血.
    """
    import asyncio
    while True:
        try:
            from pipeline.collectors.api_budget import get_guard
            guard = get_guard()
            if not guard.can_spend(1):
                logger.warning(f"[飞轮] 日配额耗尽({guard.daily_used()}/{guard.daily_cap}), "
                               f"跳过 mini 拉取")
            else:
                from pipeline.collectors.daily_collector import DailyCollector
                dc = DailyCollector()
                await _asyncio.to_thread(dc.collect_daily_odds)  # 非 force_full, 只拉活跃联赛
        except Exception:
            pass
        await asyncio.sleep(1800)  # 30分钟


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
        return FileResponse(index_path, headers={"Cache-Control": "no-cache"})
    return {
        "service": "FootballAI Bridge",
        "version": "7.4.0",
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
    """健康检查 + 依赖就绪度 (ECC mle-workflow 监控: 引擎/DB/量化/预算)"""
    ok = ENGINE is not None
    checks: Dict[str, Any] = {}

    # DB 连通性 (实际查询, 不只看文件存在)
    db_path = os.path.join(PROJECT_ROOT, "data", "football_data.db")
    if os.path.exists(db_path):
        try:
            import sqlite3
            c = sqlite3.connect(db_path, timeout=5)
            c.execute("SELECT 1")
            c.close()
            checks["db"] = "connected"
        except Exception as e:
            checks["db"] = f"error: {e}"
            ok = False
    else:
        checks["db"] = "missing"

    # API 预算剩余
    try:
        from pipeline.collectors.api_budget import get_guard
        checks["api_budget_remaining"] = get_guard().budget_status().get("month_estimate_remaining")
    except Exception:
        checks["api_budget_remaining"] = None

    return {
        "ok": ok,
        "status": "healthy" if ok else "degraded",
        "engine": ENGINE.description if ENGINE else "未加载",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "checks": checks,
    }


@app.get("/ready")
async def ready():
    """K8s readiness probe: 引擎+DB+赔率库全部就绪才200"""
    checks = {"engine": False, "db": False, "odds_db": False}

    # 引擎
    if ENGINE is None:
        raise HTTPException(status_code=503, detail="引擎未加载")
    checks["engine"] = True

    # DB 就绪 (football_data.db 存在且可读)
    db_path = os.path.join(PROJECT_ROOT, "data", "football_data.db")
    if os.path.exists(db_path) and os.access(db_path, os.R_OK):
        checks["db"] = True

    # 赔率库就绪 (odds_db/index.json 存在)
    odds_index = os.path.join(PROJECT_ROOT, "odds_db", "index.json")
    if os.path.exists(odds_index):
        checks["odds_db"] = True

    all_ready = all(checks.values())
    status = "ready" if all_ready else "degraded"
    if not all_ready:
        missing = [k for k, v in checks.items() if not v]
        raise HTTPException(status_code=503, detail=f"未就绪: {missing}")

    return {"ok": True, "status": status, "checks": checks, "engine": ENGINE.description}

# ── WebSocket 实时更新 ──
@app.websocket("/ws/realtime")
async def ws_realtime(ws: WebSocket):
    """WebSocket 实时推送 — 心跳保持连接, 接收终端订阅"""
    await ws_manager.connect(ws)
    try:
        while True:
            data = await ws.receive_text()
            msg = json.loads(data)
            if msg.get("type") == "ping":
                await ws.send_text(json.dumps({
                    "type": "pong",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }))
            elif msg.get("type") == "subscribe":
                # 终端订阅特定比赛实时更新
                logger.info(f"[WS] 客户端订阅: {msg.get('match_key','?')}")
    except Exception as e:
        logger.warning(f"[WS] 连接异常: {e}")
    finally:
        ws_manager.disconnect(ws)


@app.websocket("/ws/odds_ingest")
async def ws_odds_ingest(ws: WebSocket):
    """浏览器插件赔率摄入端点 — 接收博彩网站DOM抓取赔率, >=2家触发实时分析
    消息格式: {"home": "球队A", "away": "球队B", "source": "williamhill",
              "h": 2.80, "d": 3.40, "a": 2.55, "score": "1-0", "minute": 65}"""
    await ws.accept()
    logger.info("[WS-Ingest] 赔率摄入客户端已连接")
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send_text(json.dumps({"error": "invalid json"}))
                continue

            home = msg.get("home", "").strip()
            away = msg.get("away", "").strip()
            source = msg.get("source", "unknown")
            h = msg.get("h")
            d = msg.get("d")
            a = msg.get("a")
            score = msg.get("score", "")
            minute = msg.get("minute")

            if not home or not away or None in (h, d, a):
                await ws.send_text(json.dumps({"error": "missing fields: home/away/h/d/a"}))
                continue

            # 确认收到
            await ws.send_text(json.dumps({
                "status": "received",
                "match": f"{home} vs {away}",
                "source": source,
                "book_count": 0,
            }))

            # 累积到缓存
            match_key = f"{home.lower()}|{away.lower()}"
            book_entry = {"source": source, "h": h, "d": d, "a": a,
                          "score": score, "minute": minute,
                          "captured_at": datetime.now(timezone.utc).isoformat()}
            accum = _ODDS_INGEST_CACHE.setdefault(match_key, [])
            # 同来源去重 (保留最新)
            accum = [b for b in accum if b["source"] != source]
            accum.append(book_entry)
            _ODDS_INGEST_CACHE[match_key] = accum

            # >=2庄触发实时分析
            if len(accum) >= 2:
                try:
                    # 取最优价
                    best_h = min(b["h"] for b in accum)
                    best_d = min(b["d"] for b in accum)  # 取最低赔=最看好
                    best_a = min(b["a"] for b in accum)
                    extra = [[b["source"], b["h"], b["d"], b["a"]] for b in accum]

                    # In-play 条件概率: 浏览器扩展已带实时比分(score/minute), 透传给模型做条件裁剪
                    # (否则 WebSocket 推送的 live_decision 仍是赛前初盘, 与前端 terminal/analyze 行为不一致)
                    _ig_h = _ig_a = _ig_t = None
                    if score and "-" in str(score):
                        try:
                            _ig_h, _ig_a = (int(x) for x in str(score).split("-")[:2])
                        except (ValueError, TypeError):
                            _ig_h = _ig_a = None
                    if minute is not None:
                        try:
                            _ig_t = int(minute)
                        except (ValueError, TypeError):
                            _ig_t = None

                    result = _live_predict(home, away, best_h, best_d, best_a,
                                           extra_bookmakers=extra,
                                           date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                                           league=None,
                                           home_goals=_ig_h, away_goals=_ig_a, elapsed=_ig_t)
                    result["ingest_source"] = "browser_extension"
                    result["books_sources"] = [b["source"] for b in accum]
                    result["live_score"] = score or None
                    result["live_minute"] = minute

                    # Broadcast到所有终端
                    await ws_manager.broadcast({
                        "type": "live_decision",
                        "data": result,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    })

                    # 落库保存
                    try:
                        from pipeline.collectors.sp_odds_api import SPOddsAPI
                        api = SPOddsAPI()
                        for b in accum:
                            api.save_to_db({
                                "home_team": home, "away_team": away,
                                "best_h2h": {"home": b["h"], "draw": b["d"], "away": b["a"]},
                                "bookmakers_detail": [{"name": b["source"], "h": b["h"],
                                                       "d": b["d"], "a": b["a"]}],
                                "commence_time": datetime.now(timezone.utc).isoformat(),
                                "sport_key": "soccer_unknown",
                                "captured_at": b["captured_at"],
                            })
                    except Exception:
                        pass

                    # 发送结果给插件
                    await ws.send_text(json.dumps({
                        "status": "analyzed",
                        "match": f"{home} vs {away}",
                        "books": len(accum),
                        "direction": result.get("direction", ""),
                        "decision": result.get("value_layer", {}).get("decision", "PASS"),
                    }))
                except Exception as e:
                    logger.error(f"[WS-Ingest] 实时分析失败: {e}")
                    await ws.send_text(json.dumps({"status": "error", "detail": str(e)}))
    except Exception as e:
        logger.warning(f"[WS-Ingest] 连接关闭: {e}")
    finally:
        # 清理过期缓存 (保留最近100场)
        if len(_ODDS_INGEST_CACHE) > 100:
            keys = list(_ODDS_INGEST_CACHE.keys())
            for k in keys[:-100]:
                del _ODDS_INGEST_CACHE[k]


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
    # 推导模型概率 (pH, pD, pA) — v7.4-opt 使用 v7_rule 链
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

    # 修H5(2026-07-30): 高平局概率下 pH/pA 可能为负且不归一(如 draw_prob=0.7 → pH=1-0.4-0.7=-0.1).
    # 这里 clamp 到 [0,1] 后重新归一化, 保证概率合法且和为 1.
    pH = max(0.0, min(1.0, pH))
    pD = max(0.0, min(1.0, pD))
    pA = max(0.0, min(1.0, pA))
    _p_sum = pH + pD + pA
    if _p_sum > 0:
        pH, pD, pA = pH / _p_sum, pD / _p_sum, pA / _p_sum

    # 安全解析 best_score (格式 "2-0", 容错无横杠/非数字/缺字段)
    _sc = fv.get("best_score") or "0-0"
    try:
        _sh, _sa = _sc.split("-", 1)
        _score = {"home": int(_sh), "away": int(_sa)}
    except (ValueError, TypeError):
        _score = {"home": 0, "away": 0}

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
        "score": _score,
        "score_prediction": {
            "primary": fv.get("best_score", "0-0"),
            "top_scores": [{"score": fv.get("best_score", "0-0"), "prob": 0.3, "outcome": pred_code}] +
                          [{"score": s, "prob": 0.15, "outcome": pred_code} for s in fv.get("alt_scores", [])],
        },
        "confidence": fv.get("confidence", 0),
        "prediction_mode": "哨响AI-v7.4-opt+DrawExpert",
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
        # ── WC校准 OU/让球建议 (v7.4 rules-layer 新增) ──
        "ou_recommend": raw.get("v7_raw", {}).get("ou_recommend"),
        "hcp_recommend": raw.get("v7_raw", {}).get("hcp_recommend"),
        # Phase A: ReverseOddsEngine 赔率逆向分析
        "odds_intel": _odds_intel(match, raw, getattr(match, 'match_id', None)),
    }


# ── 策略层 + 组合层 (P0 #19): 多场候选 → 受约束 BetPlan ──
class PortfolioRequest(BaseModel):
    """组合层计划请求 — 多场候选信号聚合为受约束 BetPlan。

    signals 元素字段: mid, home, away, market, selection, odds, model_prob,
    edge_pct, ev_pct, kelly_half, decision(BET/PASS), strategy_id, note
    """
    signals: List[Dict[str, Any]] = Field(..., description="价值信号列表(来自策略/价值层)")
    bankroll: float = Field(3000.0, gt=0, description="本金基准")
    constraints: Optional[Dict[str, Any]] = Field(None, description="组合约束(覆盖默认)")
    gate: bool = Field(True, description="分歧闸门(经 bet_core 生效)")
    mode: str = Field("sim", description="执行模式: sim=模拟自动结算 / real=真实手动确认(不落库, 返回 plan_id)")
    results: Optional[Dict[str, Dict[str, Any]]] = Field(None, description="赛果映射 {mid: {winner/won/home_goals..}}, sim 结算用")


@app.post("/api/portfolio")
async def portfolio_plan(req: PortfolioRequest):
    """组合层 + 执行层消费点 (策略层 SSoT build_portfolio → 执行层 SSoT pipeline.execution)。

    - mode=sim: 构建 BetPlan 后立即模拟执行 (有 results 则自动结算落库, 无则 dry-run 摘要)
    - mode=real: 提交手动确认闸, 返回 plan_id + requires_confirmation=True, **不落库**
      真实注必须经 /api/execute/confirm 显式确认后才写 database
    """
    from pipeline.strategy import build_portfolio, Constraints, ValueSignal
    from pipeline.execution import _SIM, _GATE
    try:
        signals = [ValueSignal.from_dict(s) for s in req.signals]
    except (KeyError, TypeError, ValueError) as e:
        raise HTTPException(status_code=422, detail=f"信号格式错误: {e}")
    cons = Constraints.from_dict(req.constraints) if req.constraints else None
    plan = build_portfolio(signals, bankroll=req.bankroll, constraints=cons, gate=req.gate)

    if req.mode == "real":
        plan_id = _GATE.submit(plan, bankroll=req.bankroll)
        return _wrap_data({
            **plan.to_dict(),
            "requires_confirmation": True,
            "plan_id": plan_id,
        })
    # sim: 执行 + (有 results 则) 结算
    exec_res = _SIM.execute(plan, results=req.results)
    return _wrap_data({**plan.to_dict(), "execution": exec_res})


# ── 执行层 (A #20): 手动确认闸 + 结算 ──

class ConfirmRequest(BaseModel):
    plan_id: str = Field(..., description="提交确认闸返回的 plan_id")


@app.post("/api/execute/confirm")
async def execute_confirm(req: ConfirmRequest):
    """真实盘手动确认: 仅此端点落库 (写 database, result=pending 待结算)。"""
    from pipeline.execution import _GATE
    from database import db
    return _wrap_data(_GATE.confirm(req.plan_id, db=db))


@app.get("/api/execute/pending")
async def execute_pending():
    """列出待确认的真实盘计划 (操作员复核用)。"""
    from pipeline.execution import _GATE
    return _wrap_data(_GATE.list_pending())


class SettleRequest(BaseModel):
    bet_id: int = Field(..., description="database.bets.id (real 模式 confirm 后落库的待结算注)")
    result: str = Field(..., description="win / loss / void")
    pnl: Optional[float] = Field(None, description="可选, 缺省由 odds/stake 反推")


@app.post("/api/execute/settle")
async def execute_settle(req: SettleRequest):
    """结算一笔真实待确认注 (confirm 落库后, 赛果已知时调用)。"""
    from database import db
    ok = db.settle_bet(req.bet_id, req.result, req.pnl)
    if not ok:
        raise HTTPException(status_code=404, detail=f"bet_id={req.bet_id} 不存在")
    return _wrap_data({"ok": True, "bet_id": req.bet_id, "result": req.result})


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
        engine = _get_reverse_engine()
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
        engine = _get_reverse_engine()

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


def _build_cs_score_odds(books, min_books: int = 2):
    """[score_str, odds] 或 [book, score_str, odds] 列表 → {(i,j): 跨庄最优十进制赔率}。
    取同一比分跨庄的最高赔率(最优价)。要求 ≥min_books 家独立庄才返回(单源无法交叉验证edge, 返回空)。
    2026-08-12 反推复盘: 单源CS价(如GQ单源)会产出+773%伪edge, 加庄数门槛回归诚实SCAN."""
    if not books:
        return {}
    best = {}
    seen_books = set()
    for entry in books:
        try:
            if len(entry) >= 3:
                bk, s, o = entry[0], entry[1], float(entry[2])
                seen_books.add(bk)
            elif len(entry) == 2:
                s, o = entry[0], float(entry[1])
                seen_books.add("__single__")
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
    if len(seen_books) < min_books:
        return {}
    return best


# WC 波胆命中率校准 (canon源: wc_all_matches 313场, 2014-2026, 20×70/30 OOS):
# 调参仅在train/eval仅在test → goal_scale=1.35 使 top3 命中率 29.7%→34.4%(+4.7pp),
# 优于旧值1.199(31.5%)。仅WC生效; 经验收缩α/Dixon-Colesρ会拉低top3, 不采用。
# OIP λ 缩放常量(WC_OIP_GOAL_SCALE / GENERAL_OIP_GOAL_SCALE)已统一收敛到
# pipeline.score_model 作为单一事实源(SSoT), 本文件从 score_model 导入(见 _live_predict 内 import)。

# WC 波胆过自信修正 (来源: data/wc_calibration.json overconfidence.ratio_x, 基于运行时goal_scale=1.35重测):
# 重测(2026-07-11): 模型TOP1均概率0.1306 vs 真实命中0.1136 → 把握被高估~1.15倍。
# (旧1.93是在goal_scale=1.0低估总进球、概率堆在少数比分上造成的假象, 已废弃。)
# 仅WC生效: 传给 correct_score_value 做温度收缩(p_eff=p/overconf)后再算EV,
# 把"小edge假价值"压成负EV→PASS, 避免WC上"EV>0即BET"亏钱。非WC联赛=None(不收缩)。
WC_CS_OVERCONF = 1.15

# ═══ 赛事目录 (34 项 · The Odds API sport_key → 中文名 + category: 'cup'|'league') ═══
# category 语义化分类单一真相源: 'cup'=杯赛/锦标赛(用 WC OIP goal_scale=1.35),
# 'league'=常规联赛(用通用 OIP goal_scale=1.2)。模型路由一律读 classify_cup(), 前端不自分类。
LEAGUE_CATALOG: Dict[str, Dict[str, str]] = {
    # ── 联赛类 (league): 常规联赛, 用通用 OIP goal_scale=1.2 ──
    # 五大联赛 (核心)
    "soccer_epl":                     {"name": "英超",       "category": "league"},
    "soccer_spain_la_liga":           {"name": "西甲",       "category": "league"},
    "soccer_italy_serie_a":           {"name": "意甲",       "category": "league"},
    "soccer_germany_bundesliga":      {"name": "德甲",       "category": "league"},
    "soccer_france_ligue_one":        {"name": "法甲",       "category": "league"},
    # 英格兰联赛
    "soccer_efl_champ":               {"name": "英冠",       "category": "league"},
    "soccer_england_league1":         {"name": "英甲",       "category": "league"},
    "soccer_england_league2":         {"name": "英乙",       "category": "league"},
    # 德国联赛
    "soccer_germany_bundesliga2":     {"name": "德乙",       "category": "league"},
    "soccer_germany_liga3":           {"name": "德丙",       "category": "league"},
    # 北欧
    "soccer_sweden_allsvenskan":      {"name": "瑞典超",     "category": "league"},
    "soccer_sweden_superettan":       {"name": "瑞典甲",     "category": "league"},
    "soccer_norway_eliteserien":      {"name": "挪威超",     "category": "league"},
    "soccer_denmark_superliga":       {"name": "丹麦超",     "category": "league"},
    "soccer_finland_veikkausliiga":   {"name": "芬兰超",     "category": "league"},
    # 苏格兰 / 瑞士 / 奥地利 (原错标为杯赛/国际, 实为联赛)
    "soccer_scotland_premiership":    {"name": "苏格兰超",   "category": "league"},
    "soccer_switzerland_superleague": {"name": "瑞士超",     "category": "league"},
    "soccer_austria_bundesliga":      {"name": "奥地利超",   "category": "league"},
    # 美洲 (联赛)
    "soccer_brazil_serie_a":          {"name": "巴甲",       "category": "league"},
    "soccer_brazil_serie_b":          {"name": "巴乙",       "category": "league"},
    "soccer_argentina_primera_division": {"name": "阿根廷",  "category": "league"},
    "soccer_mexico_ligamx":           {"name": "墨西哥",     "category": "league"},
    "soccer_usa_mls":                 {"name": "MLS",        "category": "league"},
    # 亚洲/其他
    "soccer_china_superleague":       {"name": "中超",       "category": "league"},
    "soccer_korea_kleague1":          {"name": "韩K联",      "category": "league"},
    "soccer_ireland_premier":         {"name": "爱尔兰超",   "category": "league"},
    "soccer_japan_j1_league":         {"name": "日职联",     "category": "league"},
    # ── 杯赛类 (cup): 杯赛/锦标赛, 用 WC OIP goal_scale=1.35 (部分未独立校准) ──
    # 世界杯 / 欧战
    "soccer_fifa_world_cup":          {"name": "世界杯",     "category": "cup"},
    "soccer_uefa_champs_league":      {"name": "欧冠",       "category": "cup"},
    "soccer_uefa_europa_league":      {"name": "欧联杯",     "category": "cup"},
    # 英格兰 / 德国国内杯
    "soccer_england_efl_cup":         {"name": "联赛杯",     "category": "cup"},
    "soccer_germany_dfb_pokal":       {"name": "德国杯",     "category": "cup"},
    # 南美解放者杯 / 南美杯
    "soccer_conmebol_copa_libertadores":  {"name": "解放者杯", "category": "cup"},
    "soccer_conmebol_copa_sudamericana":  {"name": "南美杯",   "category": "cup"},
}

def classify_cup(sport_key=None, league=None):
    """赛事模型分类单一真相源: 返回 'cup'(杯赛类) 或 'league'(联赛)。
    优先级: LEAGUE_CATALOG.category > sport_key关键字兜底 > league关键字 > 默认league(安全兜底)。
    """
    sk = str(sport_key or '').lower()
    cat = LEAGUE_CATALOG.get(sport_key, {}).get('category') if sport_key else None
    if cat == 'cup':
        return 'cup'
    if cat == 'league':
        return 'league'
    if any(k in sk for k in ['world_cup', 'champions_league', 'europa_league', 'cup', 'pokal', 'copa', 'liber']):
        return 'cup'
    if league and ('WC' in str(league).upper() or '杯' in str(league) or '杯赛' in str(league)):
        return 'cup'
    return 'league'

# 联赛赛程缓存 (sport_key → {fetched_at, fixtures}), 1小时过期
_LEAGUE_FIXTURE_CACHE: Dict[str, Dict] = {}


def _annotate_scores(top3, top3_prob, ah_line=None, ou_line=None, overconf=None):
    """波胆 × 让球 × 大小球 交叉标注 + 操盘纪律。

    对每个 top 波胆 (h,a) 附加:
      - handicap: 让球结果 (赢/输/走/半赢/半输), 由 (h + ah_line) vs a 推算
      - ou: 大小球结果 (大/小/走)
      - direction: 1X2 方向 (H/D/A)
      - fair_decimal: 模型公允赔率 1/p
      - fair_eff_decimal: 过自信收缩后公允赔率 1/p_eff
      - long_tail: 是否长尾负EV (fair>33 或 prob<3%)
    返回标注后的 list + 方向分布 dict + 纪律标记。
    """
    def _parse_line(v):
        """解析盘口为数值。支持 split/quarter 盘:
        '0/0.5'->0.25, '-0/0.5'->-0.25, '+0.5/1'->0.75, '3/3.5'->3.25。"""
        if v is None:
            return None
        if isinstance(v, (int, float)):
            return float(v)
        s = str(v).strip()
        if '/' in s:
            toks = [t.strip() for t in s.split('/') if t.strip()]
            if not toks:
                return None
            ctx_sign = -1.0 if toks[0].startswith('-') else 1.0
            vals = []
            for t in toks:
                core = t.lstrip('+').lstrip('-')
                try:
                    x = float(core)
                except ValueError:
                    return None
                if t.startswith('-'):
                    x = -abs(x)
                elif t.startswith('+'):
                    x = abs(x)
                else:
                    x = ctx_sign * abs(x)
                vals.append(x)
            return sum(vals) / len(vals)
        try:
            return float(s)
        except ValueError:
            return None

    ah = _parse_line(ah_line)
    ou = _parse_line(ou_line)
    annotated = []
    dir_count = {"H": 0, "D": 0, "A": 0}
    for (h, a), p in zip(top3, top3_prob):
        # 1X2 方向
        direction = "H" if h > a else ("D" if h == a else "A")
        dir_count[direction] += 1
        # 让球结果: 主队净胜 = h - a; 让球盘 ah>0 主队受让, ah<0 主队让球
        # 调整后净胜 = (h - a) + ah
        if ah is not None:
            adj = (h - a) + ah
            if adj > 0.25:
                handicap = "赢"
            elif adj > 0:
                handicap = "半赢"
            elif abs(adj) < 1e-6:
                handicap = "走"
            elif adj < -0.25:
                handicap = "输"
            else:
                handicap = "半输"
        else:
            handicap = None
        # 大小球结果
        if ou is not None:
            tg = h + a
            if tg > ou:
                ou_r = "大"
            elif abs(tg - ou) < 1e-6:
                ou_r = "走"
            else:
                ou_r = "小"
        else:
            ou_r = None
        # 公允赔率
        p_eff = p / overconf if (overconf and overconf > 0) else p
        fair = round(1.0 / p, 2) if p > 0 else None
        fair_eff = round(1.0 / p_eff, 2) if p_eff > 0 else None
        long_tail = (fair is not None and fair > 33) or (p > 0 and p < 0.03)
        annotated.append({
            "score": f"{h}-{a}", "prob": round(p, 4), "prob_eff": round(p_eff, 4),
            "direction": direction, "handicap": handicap, "ou": ou_r,
            "fair_decimal": fair, "fair_eff_decimal": fair_eff,
            "long_tail": long_tail,
        })
    # 纪律标记: top 波胆是否跨多方向 (≥2 个方向)
    active_dirs = sum(1 for c in dir_count.values() if c > 0)
    discipline = {
        "multi_direction": active_dirs >= 2,
        "direction_count": dir_count,
        "best_direction": max(dir_count, key=dir_count.get),
    }
    return annotated, discipline


# ── P1b 辅助函数 (模块级, 带模块缓存) ──
_TEAM_CANON_CACHE = {}
def _normalize_team_p1b(name):
    """队名归一化: 中文/别名 -> 英文 canonical (走 team_canonical 表, SSoT). 带模块缓存."""
    if not name:
        return name
    n = name.strip()
    if n in _TEAM_CANON_CACHE:
        return _TEAM_CANON_CACHE[n]
    out = n
    try:
        import sqlite3 as _sq, re as _re
        _db = os.path.join(PROJECT_ROOT, "data", "football_data.db")
        con = _sq.connect(_db)
        # 取所有候选 (canonical 精确匹配 + 别名 LIKE). team_canonical 存在重复行
        # (同一队既有 canonical='Brazil' 也有 canonical='巴西'), 必须优先选非 CJK 的英文 canonical.
        rows = con.execute(
            "SELECT canonical FROM team_canonical WHERE canonical=? OR aliases_json LIKE ?",
            (n, '%"' + n + '"%')
        ).fetchall()
        con.close()
        for (c,) in rows:
            if c and not _re.search(r"[一-鿿]", c):
                out = c
                break
        else:
            if rows:
                out = rows[0][0]
    except Exception:
        pass
    _TEAM_CANON_CACHE[n] = out
    return out

def _resolve_wc_extra_bookmakers(home_canon, away_canon):
    """P1b④: 从 The Odds API 快照取 >=2 家 1X2 盘口 (英文canonical队名匹配), 解单源拒注."""
    import json as _json
    _f = os.path.join(PROJECT_ROOT, "data", "oddsapi_wc_raw_latest.json")
    if not os.path.exists(_f):
        return None
    try:
        with open(_f, "r", encoding="utf-8") as _fh:
            data = _json.load(_fh)
    except Exception:
        return None
    for g in data:
        ht, at = g.get("home_team"), g.get("away_team")
        if not (ht and at):
            continue
        if (ht == home_canon and at == away_canon) or (ht == away_canon and at == home_canon):
            books = []
            for b in g.get("bookmakers", []):
                for m in b.get("markets", []):
                    if m.get("key") != "h2h":
                        continue
                    price = {o.get("name"): o.get("price") for o in m.get("outcomes", [])}
                    oh = price.get(ht); od = price.get("Draw"); oa = price.get(at)
                    if oh and od and oa:
                        try:
                            books.append((str(b.get("key")), float(oh), float(od), float(oa)))
                        except (TypeError, ValueError):
                            continue
            if len(books) >= 2:
                return books
    return None


def _reconcile_conclusions(direction, draw_alert, m_pd, strategy_signals,
                            tier, market_conf, overround, top_score):
    """结论一致性仲裁层 (2026-07-31).

    多个分析模块独立计算, 可能给出方向相反的建议(如 R2防平 vs FadeDraw排除平局)。
    本函数在 verdict 拼接前做冲突检测+按数据裁决。

    裁决依据 (master_dataset 31.5万行回测, R2与FadeDraw同时触发时实际平局率):
      league层: 0.281 vs 基线0.259 (+2.3pp) → 信R2(防平)
      cup层:   0.282 vs 基线0.240 (+4.2pp) → 信R2(防平)
      结论: 冲突时一律信R2(防平), 抑制FadeDraw(其obscure校准在高平区不准)。

    Returns:
      {fade_draw_suppressed: bool, conflicts: [str], verdict_suffix: str}
    """
    conflicts = []
    fade_draw_suppressed = False
    # 冲突1: R2(防平) vs FadeDraw(排除平局) — 同一个P(平)信号方向相反
    has_fade = any('Fade Draw' in str(s.get('name', '')) for s in (strategy_signals or []))
    if draw_alert and has_fade:
        # 数据裁决: m_pd>=DRAW_ALERT 时实际平局率高于基线, FadeDraw"排除平局"是错的 → 抑制FadeDraw
        fade_draw_suppressed = True
        conflicts.append("平局信号矛盾: 防平(实际平局率+2~4pp) vs 做空平局 → 采信防平,抑制做空平局")
    # 冲突2(信息标注, 不裁决): direction vs 波胆top1
    if top_score and direction and direction != '平局':
        try:
            th, ta = str(top_score).split('-')
            if int(th) == int(ta):  # top1是平局比分(如0-0/1-1)但direction非平
                conflicts.append(f"方向({direction})与波胆top1({top_score}平局)不一致")
        except Exception:
            pass
    # 冲突3(信息标注): R6高抽水 — 不裁决, 由调用方加降权前缀
    verdict_suffix = ""
    if conflicts:
        verdict_suffix = " | ⚠️" + "; ".join(conflicts)
    return {"fade_draw_suppressed": fade_draw_suppressed, "conflicts": conflicts, "verdict_suffix": verdict_suffix}


def _live_predict(home, away, oh, od, oa,
                  home_norm=None, away_norm=None, date=None, league=None,
                  sport_key=None,
                  extra_bookmakers=None, correct_score_books=None,
                  hcp_line=None, hcp_home_odds=None, hcp_away_odds=None,
                  ou_line=None, over_water=None, under_water=None,
                  home_goals=None, away_goals=None, elapsed=None,
                  mid: Optional[str] = None) -> Dict[str, Any]:
    """真实1X2赔率 -> 全链路预测 (与 scripts/predict_live.py 同构)。返回结构化 dict。"""
    from pipeline.score_model import (predict_score, deoverround,
                                      WC_OIP_GOAL_SCALE, GENERAL_OIP_GOAL_SCALE)
    from pipeline.draw_signal import market_draw_prob, consensus_draw_signal, draw_alert_with_booster
    if extra_bookmakers:
        from pipeline.draw_signal import multi_bookmaker_consensus
    import numpy as np
    from pipeline.compute_value_layer import compute_value_layer
    from pipeline.deep_report import (consensus_probs,
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

    # 联赛零配置反查: 前端/内部调用未显式传 league 时, 按主客队从 events.db 反查,
    # 让"联赛进球先验 + 杯赛识别"对真实比赛自动生效 (无匹配则 league 保持 None, 零回归).
    if not league:
        league = _lookup_league(home, away)

    # ③ OIP 比分 / 大小球
    # WC 识别: 优先用原始 sport_key(英文, 含 world_cup) → 修正旧逻辑用中文 league 判定永远 False 的 bug;
    # goal_scale: WC=1.35 (修正OIP低估WC总进球) / 通用联赛=1.2 (140k真实赛果walkforward校准, 2026-07-18)。
    is_wc = bool(sport_key and "world_cup" in str(sport_key).lower()) or \
            bool(league and "WC" in str(league).upper())
    is_cup = classify_cup(sport_key, league) == 'cup'
    # P1b② 队名归一化 -> 英文 canonical, 供 OIP 与平局共识使用 (调用方没传 home_norm 也兜底)
    home_canon = _normalize_team_p1b(home_norm or home)
    away_canon = _normalize_team_p1b(away_norm or away)
    # 2026-08-30 修复 λ 反推缺陷(根源): solve_oip 用独立泊松硬匹配平局概率,
    #   势均力敌(平局率>0.25)时方程无解 → λ 被压到 1.76(应 2.6+), 低 32%。
    #   改用 OU 盘口(诚实锚)反推 implied_total, 交给 predict_score 锚定 λ_total。
    # 2026-08-30 二次修正(半场回测 1781 场): OU 锚定仅对**赛前(比分未知)**成立——
    #   赛前比分未知, OU 是唯一诚实锚, solve_oip 压 λ 是缺陷(博多 0-0@45 判小错);
    #   但**半场/滚球(比分已定)**时, 市场滚球 1X2 已反映领先/落后, 此时 solve_oip
    #   反推 λ 反而更准(半场 top1 22.46% vs OU锚定 21.45%, top3 52.05% vs 50.59%)。
    #   故: 有比分传入(in-play) → 回退 solve_oip; 赛前 → OU 锚定。
    _is_inplay = home_goals is not None and away_goals is not None
    _implied_total = None
    if (not _is_inplay) and ou_line and over_water and under_water:
        try:
            _po = (1.0 / float(over_water)) / (1.0 / float(over_water) + 1.0 / float(under_water))
            _implied_total = float(ou_line) + 2.0 * (_po - 0.5)
            if not (1.0 < _implied_total < 6.0):
                _implied_total = None   # 异常值回退 solve_oip
        except Exception:
            _implied_total = None
    r = predict_score(home_canon, away_canon, oh, od, oa,
                      goal_scale=WC_OIP_GOAL_SCALE if is_cup else GENERAL_OIP_GOAL_SCALE,
                      implied_total=_implied_total)
    M = r["matrix"]; mg = M.shape[0] - 1

    # ── 联赛/赛事进球水平先验 (2026-08-12: 独立特征+校准+零回归) ──
    # 把"赛事/联赛场均总球"作为合法先验收缩混合进 OIP 中心 λ(lh+la), 重建 Poisson 矩阵。
    # 仅当 league 提供时生效; 无 league → 完全跳过 (零回归)。
    # 不影响 1X2 verdict(方向=市场argmax, 来自 oh/od/oa); 仅修正 OU/CS/平局的中心 λ。
    league_scoring = None
    oip_raw_total = float(r.get("lh") or 0.0) + float(r.get("la") or 0.0)
    if league:
        try:
            from pipeline.league_scoring_prior import blend_total_with_league as _ls_blend
            if oip_raw_total > 0:
                _ls = _ls_blend(oip_raw_total, league)
                _adj = _ls["adjusted"]
                _s = _adj / oip_raw_total
                _lh2, _la2 = float(r.get("lh") or 0.0) * _s, float(r.get("la") or 0.0) * _s
                from math import exp as _exp, factorial as _fac
                def _poi(k, lam):
                    if k < 0 or lam <= 0:
                        return 0.0
                    return _exp(-lam) * (lam ** k) / _fac(k)
                _mg = M.shape[0] - 1
                _M2 = np.zeros_like(M)
                for _i in range(_mg + 1):
                    for _j in range(_mg + 1):
                        _M2[_i, _j] = _poi(_i, _lh2) * _poi(_j, _la2)
                _M2sum = _M2.sum()
                if _M2sum > 1e-12:
                    M = _M2 / _M2sum
                    r["lh"] = _lh2
                    r["la"] = _la2
                league_scoring = {
                    "prior_total": _ls["prior_mean"], "prior_n": _ls["prior_n"],
                    "matched_league": _ls["matched_league"], "liquidity": _ls["liquidity"],
                    "weight": _ls["weight"], "method": _ls["method"],
                    "raw_total": round(oip_raw_total, 3), "adjusted_total": round(_adj, 3),
                }
        except Exception as _lse:
            logger.warning(f"[league_scoring] 先验注入失败(跳过): {_lse}")

    # ── In-play 条件概率裁剪 ──
    # 当传入当前比分时, 对 OIP Poisson 矩阵做条件概率更新:
    #   1) 已不可能比分(h<H or a<A) 概率归零
    #   2) 可能比分用剩余时间条件 Poisson 重算 (λ 按 elapsed 缩放)
    #   3) 重新归一化使总和=1.0
    #   无比分传入时(赛前/None) → 行为与改动前完全一致, 不裁剪
    inplay_applied = False
    inplay_info = None
    # 2026-08-12 反推复盘: 仅当 当前比分 + 有效 elapsed(0<_elapsed<90) 齐备才做条件裁剪.
    # 原 else 分支 elapsed 缺失仍 T_ratio=1.0 静默裁剪 -> 复现"83min 4-1 +773% edge"虚假高概率.
    # 现: elapsed 缺省/无效/终场 -> 不裁剪, 保留全场矩阵, 不标 inplay.
    _elapsed = int(elapsed) if elapsed is not None else None
    if home_goals is not None and away_goals is not None and _elapsed is not None and 0 < _elapsed < 90:
        try:
            from math import exp as _mexp, factorial as _mfac
            H, A = int(home_goals), int(away_goals)
            T_ratio = max(0.05, (90.0 - _elapsed) / 90.0)
            # 原始 λ (OIP 模型输出; obscure 联赛可能无 lh/la → 取默认值)
            _lam_h = float(r.get("lh", 1.2) or 1.2)
            _lam_a = float(r.get("la", 0.9) or 0.9)
            # 剩余时间 λ (越接近终场, 预期再进球数越少)
            _rem_h = max(0.01, _lam_h * T_ratio)
            _rem_a = max(0.01, _lam_a * T_ratio)

            def _poi_c(k, lam):
                """标准库 Poisson PMF (避免 numpy 依赖, 纯 math)"""
                if k < 0 or lam <= 0:
                    return 0.0
                return (_mexp(-lam) * lam ** k) / _mfac(k)

            M_cond = np.zeros_like(M)
            mg_a = M.shape[1] - 1  # 客队最大进球(矩阵宽度)
            for _h in range(H, mg + 1):
                for _a in range(A, mg_a + 1):
                    M_cond[_h, _a] = _poi_c(_h - H, _rem_h) * _poi_c(_a - A, _rem_a)

            _total = float(M_cond.sum())
            if _total > 1e-10:
                M = M_cond / _total
                inplay_applied = True
                inplay_info = {
                    "current_score": f"{H}-{A}",
                    "elapsed": _elapsed,
                    "time_ratio": round(T_ratio, 3),
                    "original_lambda_h": round(_lam_h, 3),
                    "original_lambda_a": round(_lam_a, 3),
                    "remaining_lambda_h": round(_rem_h, 4),
                    "remaining_lambda_a": round(_rem_a, 4),
                    "note": (f"In-play 条件裁剪: 当前 {H}-{A}"
                             f" @ {_elapsed}min, 剩余时间比例 {T_ratio:.1%}")
                }
                logger.info(f"[inplay] 条件概率裁剪生效: {inplay_info['note']}")
        except Exception as _ie:
            logger.warning(f"[inplay] 条件概率裁剪失败(回退初盘): {_ie}")

    ov25 = float(sum(M[i, j] for i in range(mg + 1) for j in range(mg + 1) if i + j >= 3))
    ov15 = float(sum(M[i, j] for i in range(mg + 1) for j in range(mg + 1) if i + j >= 2))
    ov35 = float(sum(M[i, j] for i in range(mg + 1) for j in range(mg + 1) if i + j >= 4))
    flat = M.flatten()

    # ── 终场冻结(elapsed>=88): 抑制所有多球比分, 当前比分≈终局 ──
    _hg = int(home_goals) if home_goals is not None else None
    _ag = int(away_goals) if away_goals is not None else None
    _el = int(elapsed) if elapsed is not None else 0
    if _hg is not None and _ag is not None and _el >= 88:
        flat = flat.copy()
        _T = max(0.02, (93 - _el) / 90.0)
        remain_chance = _T * 0.25
        for i in range(mg + 1):
            for j in range(mg + 1):
                if i < _hg or j < _ag:
                    # 分数不可能倒退
                    flat[i * (mg + 1) + j] = 0.0
                    continue
                goals_needed = (i - _hg) + (j - _ag)
                if goals_needed == 0:
                    flat[i * (mg + 1) + j] = 0.94
                elif goals_needed == 1:
                    flat[i * (mg + 1) + j] *= remain_chance
                else:
                    flat[i * (mg + 1) + j] *= 0.0005
        total = flat.sum()
        if total > 0:
            flat = flat / total

    order = np.argsort(-flat)[:5]
    top3 = [tuple(int(x) for x in divmod(int(k), mg + 1)) for k in order[:3]]
    top3_prob = [float(flat[k]) for k in order[:3]]
    top5 = [tuple(int(x) for x in divmod(int(k), mg + 1)) for k in order]
    top5_prob = [float(flat[k]) for k in order]

    # ⑩ 价值层 (L0 深度决策): 跨庄共识概率 vs 跨庄最优价 → edge/EV/凯利/情景PnL
    # 诚实约束(v6铁律): 模型对1X2无超额信息优势 → "模型概率"取跨庄共识隐含概率;
    # 真实 edge 仅来自跨庄价差(soft line)。
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

    value_layer = compute_value_layer(
        odds=best_odds,
        model_probs=cons,
        overround=overround,
    )
    value_layer["best_odds"] = [round(x, 3) for x in best_odds]
    value_layer["books_count"] = len(price_books)
    # soft-line 展示字段 (始终挂, 供人工复核; 决策是否采用由开关控制)
    value_layer["softline"] = _sl_display

    # ④ 平局信号 (操盘手一手定价)
    m_pd = market_draw_prob(oh, od, oa)
    draw_alert = m_pd >= DRAW_ALERT

    # ⑤ 跨庄家共识 (优先: extra_bookmakers > WH×IW > 回退市场P平)
    # P1b④ WC 场景: 调用方未提供 extra_bookmakers 时, 自动从 The Odds API 快照解析跨庄盘口
    if extra_bookmakers is None and sport_key and "world_cup" in str(sport_key).lower():
        try:
            extra_bookmakers = _resolve_wc_extra_bookmakers(home_canon, away_canon)
        except Exception:
            extra_bookmakers = None
    consensus = None
    if extra_bookmakers:
        try:
            consensus = multi_bookmaker_consensus(extra_bookmakers)
            consensus["source"] = "multi_bookmaker"
        except Exception:
            consensus = None
    if not consensus and home_norm and away_norm and date and league:
        try:
            consensus = consensus_draw_signal(home_canon, away_canon, oh, od, oa, date, league)
            consensus["source"] = "WH×IW"
        except Exception:
            consensus = None

    # G5 · consensus booster: 双庄共识 strong → 平局预警阈值 0.26→0.24 (设计见 draw_bookmaker_validation.md)
    # consensus 不可用(无多庄/WC无IW→available=False/strong=False)时回退纯市场 P 平
    draw_alert = draw_alert_with_booster(m_pd, consensus)
    # P1b① 双庄共识 strong -> 主verdict覆写为平局 (解"永不选平"); 无共识时仅保留"配防平"文本提示
    draw_verdict_override = bool(consensus and consensus.get("strong"))
    if draw_verdict_override:
        direction = "平局"
        _dp = consensus.get("consensus") if consensus.get("source") != "multi_bookmaker" else consensus.get("mean_pd")
        if _dp:
            market_conf = max(market_conf, float(_dp))

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
            if line == 0:
                hcp_dir = "平手"
                hcp_fav = None  # 平手盘: 无让球热门方向
            elif h_odds < a_odds:
                hcp_dir = "主让赢" if line < 0 else "受让赢"
                hcp_fav = "home"
            else:
                hcp_dir = "客让赢" if line > 0 else "受让赢"
                hcp_fav = "away"

            # 与1X2方向一致性检查
            dir_map = {"主胜": "home", "平局": "draw", "客胜": "away"}
            x12_fav = dir_map.get(direction, "")
            if hcp_fav is None:
                # 平手盘: 亚盘无让球热门方向, 不触发1X2分歧(历史1X2命中68%>亚盘)
                consistent = True
            else:
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

            if hcp_fav is None:
                advice = "平手盘: 亚盘无让球方向, 以1X2为准 (不触发分歧)"
            elif consistent:
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
        # 修(2026-07-29): 文案阈值跟随 DRAW_ALERT 常量(现0.24), 原硬编码26%与新阈值矛盾
        try:
            from pipeline.draw_signal import DRAW_ALERT as _DA
        except Exception:
            _DA = 0.24
        _da_pct = round(_DA * 100)
        op_rules.append({"id":"R2","label":"防平预警","detail":f"P(平)={round(m_pd*100,1)}% ≥ {_da_pct}% → 需防平局","rule":f"P(平)≥{_da_pct}%触发防平","color":"amber"})
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

    # 结论一致性仲裁-冲突标注 (2026-07-31): draw_alert触发时FadeDraw必然也在(pd>0.20)
    # 数据裁决: 此区实际平局率+2~4pp高于基线 → 信防平, 标注冲突(FadeDraw抑制在后段strategy_signals处理)
    if draw_alert:
        verdict.append("⚠️平局信号已仲裁: 采信防平(回测+2~4pp), 抑制做空平局")

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
    # 平局共识: 需跨庄/WH×IW 共识 P(平) (无共识时 consensus=None → 跳过, 不可证伪)
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
    # cs_score_odds 始终定义({(i,j): odds} 元组键), 供 correct_score_value 与三角引擎复用
    cs_score_odds = _build_cs_score_odds(correct_score_books) if correct_score_books else {}
    # Phase B: GQ 单源 CS 无跨庄验证, 不进 correct_score_value 的 BET 分支(仅做诚实概率扫描)
    # (2026-08-12 反推复盘: 单源CS价会产出+773%伪edge, 回归诚实 SCAN 才符铁律
    # "子市场edge只来自跨庄价差"). GQ CS 仍用于上方三时点时间线/三角引擎.
    if not cs_score_odds:
        cs_score_odds = {}
    # 三时点 CS 赔率时间线 (初盘/中场收盘/当前 + drift) — 仅 GQ 已采集比赛有值
    # 用于前端实时赔率面板 + 临场漂移陷阱识别。低级别联赛(采集器未覆盖)为 None。
    _cs_timeline = None
    try:
        from pipeline.cs_odds_resolver import resolve_cs_odds_timeline
        _cs_timeline = resolve_cs_odds_timeline(home, away)
    except Exception:
        _cs_timeline = None
    # ⑬c CS波胆跟庄信号 (基于初盘→当前赔率变动的 GREEN/AMBER/RED 三色分析)
    _cs_follow = None
    try:
        if _cs_timeline and _cs_timeline.get("open") and _cs_timeline.get("live"):
            from pipeline.cs_momentum import CSMomentumTracker
            _tracker = CSMomentumTracker()
            _live_sc = (int(home_goals), int(away_goals)) if (
                home_goals is not None and away_goals is not None) else None
            _cs_follow = _tracker.analyze_cs_movement(
                initial_odds=_cs_timeline["open"],
                current_odds=_cs_timeline["live"],
                live_score=_live_sc,
            )
    except Exception:
        _cs_follow = None
    try:
        sub_markets["correct_score"] = correct_score_value(
            M.tolist(), score_odds=cs_score_odds if cs_score_odds else None, top_n=3,
            overconf=WC_CS_OVERCONF if is_cup else None)
    except Exception as e_cs:
        # 模型崩溃 → 展示 prompt backfill 的简化波胆概率列表
        try:
            flat = [float(M[i][j]) for i in range(M.shape[0]) for j in range(M.shape[1])]
            rows = [{"score": f"{i}-{j}", "prob": round(float(M[i][j]), 4),
                     "edge": 0, "ev_pct": 0, "decision": "SCAN"}
                    for i in range(min(6, M.shape[0]))
                    for j in range(min(6, M.shape[1]))
                    if float(M[i][j]) > 0.005]
            rows.sort(key=lambda r: r["prob"], reverse=True)
            sub_markets["correct_score"] = {
                "decision": "SCAN", "edge_available": False,
                "decision_text": f"波胆模型暂不可用({e_cs}), 显示概率估计",
                "rows": rows[:20]}
        except Exception:
            pass

    # ⑫ 市场结构波胆三角定位 (涛哥亲授: OU×AH×1X2×CS 取交集, 输出可审计候选集)
    # cs_score_odds 元组键 → 字符串键 {'i-j': odds}; 无CS盘时传入 None(纯约束/Poisson)。
    # in-play 时透传 live_score/elapsed, 候选自动裁剪 h≥H,a≥A。
    cs_triangulation = None
    try:
        from pipeline.cs_triangulate import triangulate as _triangulate
        _cs_str = {f"{i}-{j}": o for (i, j), o in (cs_score_odds or {}).items()}
        _live = (home_goals, away_goals) if (home_goals is not None and away_goals is not None) else None
        cs_triangulation = _triangulate(
            ou_line=ou_line,
            ou_outcome=None,                # 只按线定软界; in-play 由 live_score 约束
            ah_line=hcp_line,
            h=oh, d=od, a=oa,
            cs_odds=_cs_str if _cs_str else None,
            league=league,
            live_score=_live,
            elapsed=elapsed,
            poisson_matrix=M,               # numpy 矩阵 (in-play 后已是条件裁剪矩阵)
        )
    except Exception as e_tri:
        logger.warning(f"[triangulate] 市场结构波胆定位失败(跳过): {e_tri}")

    # 波胆 × 让球 × 大小球 交叉标注 (供前端精确展示 + 操盘纪律)
    # 让球盘口: 优先函数参数 hcp_line, 其次已解析的 handicap dict
    oip_ah_line = hcp_line
    if oip_ah_line is None and isinstance(handicap, dict) and handicap.get("line") is not None:
        oip_ah_line = handicap.get("line")
    oip_ou_line = ou_line
    oip_overconf = WC_CS_OVERCONF if is_cup else None
    try:
        oip_annotated, oip_discipline = _annotate_scores(
            top3, top3_prob, ah_line=oip_ah_line, ou_line=oip_ou_line, overconf=oip_overconf)
    except Exception:
        oip_annotated, oip_discipline = [], {"multi_direction": False}

    # 波胆推荐行回填「让球/大小球」交叉标注: 让每行显示是否穿盘/大球。
    # sub_markets.correct_score.rows 来自 correct_score_value, 本身不带 handicap/ou,
    # 这里用同一 ah_line/ou_line 跑 _annotate_scores 回填, 前端波胆×让球交叉才完整准确。
    try:
        cs = sub_markets.get("correct_score")
        if isinstance(cs, dict) and cs.get("rows"):
            cs_rows = cs["rows"]
            cs_scores, cs_prob = [], []
            for crow in cs_rows:
                sc = str(crow.get("score", ""))
                if "-" in sc:
                    hh, aa = sc.split("-")[:2]
                    cs_scores.append((int(hh), int(aa)))
                    cs_prob.append(float(crow.get("prob", 0) or 0))
            if cs_scores:
                cs_annotated, _ = _annotate_scores(
                    cs_scores, cs_prob, ah_line=oip_ah_line, ou_line=oip_ou_line, overconf=oip_overconf)
                for crow, cann in zip(cs_rows, cs_annotated):
                    crow["handicap"] = cann.get("handicap")
                    crow["ou"] = cann.get("ou")
    except Exception:
        pass

    # OPT-B: OIP 长尾兜底 — 庄家CS列表未报的高概率比分(补覆盖率缺口, 供操作员看长尾)
    cs_longtail = []
    if cs_score_odds and M is not None:
        try:
            _mkt_scores = {f"{i}-{j}" for (i, j) in cs_score_odds}
            _M = M if hasattr(M, "flatten") else None
            if _M is not None:
                _flat = _M.flatten()
                _cols = _M.shape[1]
                for _k in np.argsort(-_flat)[:15]:
                    _i, _j = divmod(int(_k), _cols)
                    _sc = f"{_i}-{_j}"
                    if _sc not in _mkt_scores:
                        cs_longtail.append({"score": _sc, "prob": round(float(_flat[_k]), 4)})
                    if len(cs_longtail) >= 5:
                        break
        except Exception:
            cs_longtail = []

    # ── 平局高发区 bias (draw_zone) ── 两路触发带, 均因 OIP Poisson 反演系统性低估平局 → 保守boost修正 (诚实约束: 不改 verdict/direction)
    # 带1 [1.31,1.45]: 487场实证 平局率32-35%
    # 带2 [1.9,1.99]+平赔<=2.99: 平赔异常压低陷阱, 611场实证 平局率34.2% vs 对照27.8%, OIP低估~2.7pp (机构压低平局赔付=预判平局)
    draw_zone = False
    draw_zone_boost = 1.0
    draw_zone_signal = None  # 'low_fav' | 'flat_draw_trap' | None (审计/前端可显)
    try:
        _min_odds = min(oh, oa)  # 被看好方(主或客)的胜赔
        _draw_odds = od
        _dz_trigger = False
        if 1.31 <= _min_odds <= 1.45:
            _dz_trigger = True; draw_zone_signal = "low_fav"
        elif 1.9 <= _min_odds <= 1.99 and _draw_odds <= 2.99:
            # 平赔异常压低陷阱: 中热门+平赔被刻意压低(正常3.2+), 机构预判平局/压低赔付 = 主胜诱导, 不追主胜
            _dz_trigger = True; draw_zone_signal = "flat_draw_trap"
        if _dz_trigger:
            draw_zone = True
            import math as _math
            def _pois_pmf(_k, _l):
                # 纯 math 实现 Poisson PMF, 不依赖 scipy
                try:
                    _k = int(_k); _l = float(_l)
                except Exception:
                    return 0.0
                if _l <= 0 or _k < 0:
                    return 0.0
                return _math.exp(-_l) * (_l ** _k) / _math.factorial(_k)
            _lh = float(r.get("lh", 0) or 0)
            _la = float(r.get("la", 0) or 0)
            if _lh > 0 and _la > 0:
                _pd = sum(_pois_pmf(int(s.split("-")[0]), _lh) * _pois_pmf(int(s.split("-")[1]), _la)
                          for s in ("0-0", "1-1", "2-2"))
                # 实测平局率~0.33, 若OIP低估则保守boost(封顶1.5x)
                if 0 < _pd < 0.33:
                    draw_zone_boost = min(1.5, 0.33 / max(_pd, 0.05))
    except Exception:
        draw_zone = False
        draw_zone_boost = 1.0
        draw_zone_signal = None
    # 平局区标注: 在 cs_triangulation.ranked 中标记被 boost 的平局项 (不破坏原结构, ranked 为字符串列表无权重)
    try:
        if draw_zone and isinstance(cs_triangulation, dict) and isinstance(cs_triangulation.get("ranked"), list):
            _dz_scores = [s for s in ("0-0", "1-1", "2-2") if s in cs_triangulation.get("ranked", [])]
            if _dz_scores:
                cs_triangulation["draw_zone_applied"] = True
                cs_triangulation["draw_zone_boost"] = round(draw_zone_boost, 3)
                cs_triangulation["draw_zone_scores"] = _dz_scores
                cs_triangulation["draw_zone_signal"] = draw_zone_signal
                cs_triangulation.setdefault("notes", []).append(
                    f"平局bias[{draw_zone_signal}]: 对 {_dz_scores} 施加 boost={round(draw_zone_boost, 3)}")
    except Exception:
        pass

    # ── 策略方向信号 (2026-07-21 起解除联赛过滤: 全联赛触发; 面板提示级, 不自动下注/不改 verdict) ──
    # 三方向: 做空平局(Fade Draw) / 做多客队长尾(Back Away) / 看小(Fade Over)。
    # 校准: GQ match_outcomes 低流动性 obscure 层 N=254 -> 平局隐含25.2% vs 实际15.7%(+9.4pp);
    #       客队隐含<0.10 实际客胜76.9%(N=13)。OU 历史回测样本不足, 但运行时 GQ 已稳定采集
    #       OU(2026-07-21 修复解析 bug), 前端未传 OU 时由 resolve_ou_odds 兜底 → Fade Over 置信 medium。
    #       解除过滤后 elite 联赛同样输出, 信号附 tier 溯源字段供审慎加权。
    try:
        from pipeline.strategy_signals import compute_signals, classify_league_tier
        _strat_tier = 'cup' if is_cup else classify_league_tier(sport_key, league)
        _strat_ou = None
        if ou_line is not None and over_water and under_water:
            _strat_ou = (float(over_water), float(under_water), float(ou_line))
        else:
            # 前端未传 OU → 从 events.db 实时采集兜底 (采集器 2026-07-21 已修复 OU 解析 bug,
            # 现能稳定采集 OU; 兜底使 Fade Over 信号在有 OU 支撑时置信升级 medium)
            try:
                from pipeline.cs_odds_resolver import resolve_ou_odds
                _gq_ou = resolve_ou_odds(home, away)
                if _gq_ou:
                    _strat_ou = (float(_gq_ou[0]), float(_gq_ou[1]), float(_gq_ou[2]))
            except Exception as _ou_e:
                logger.warning(f"[strategy_signals] GQ OU 兜底失败: {_ou_e}")
        strategy_signals = compute_signals(oh=oh, od=od, oa=oa, ou=_strat_ou, tier=_strat_tier,
                                           model_p_over=ov25)
    except Exception as _ss_e:
        logger.warning(f"[strategy_signals] 计算失败: {_ss_e}")
        strategy_signals = []
        _strat_tier = 'cup' if is_cup else 'obscure'

    # ── 结论一致性仲裁 (2026-07-31): R2防平 vs FadeDraw冲突 → 数据裁决信R2, 抑制FadeDraw ──
    _top1 = f"{top3[0][0]}-{top3[0][1]}" if top3 else None
    _recon = _reconcile_conclusions(direction, draw_alert, m_pd, strategy_signals,
                                     _strat_tier, market_conf, overround, _top1)
    if _recon["fade_draw_suppressed"]:
        # 抑制FadeDraw: 加suppressed标记而非删除(保留透明性, 前端可灰显)
        for s in strategy_signals:
            if 'Fade Draw' in str(s.get('name', '')):
                s['suppressed'] = True
                s['suppress_reason'] = '与防平信号冲突, 回测实际平局率+2~4pp高于基线, 采信防平'

    # ── 2026-08-30 比分分析器三级判定 (定方向/软加权/观望) ──
    # 用户拍板: 比分分析器不是预测器; 方向服从市场 + 领先方。
    # 输出 {级别, 方向, 置信度, 概率分布, 分歧标注} 到 result.score_analysis。
    from pipeline.score_analyzer import analyze_score as _analyze_score
    _market_dir = {0: "home", 1: "draw", 2: "away"}[best[1]]
    _lead_side, _lead_goals = None, 0
    if home_goals is not None and away_goals is not None:
        try:
            _diff = int(home_goals) - int(away_goals)
            if _diff > 0:
                _lead_side, _lead_goals = "home", _diff
            elif _diff < 0:
                _lead_side, _lead_goals = "away", -_diff
        except Exception:
            pass
    _score_dist = {f"{h}-{a}": p for (h, a), p in zip(top5, top5_prob)}
    _score_analysis = _analyze_score(
        _market_dir, (ph, pd, pa), _lead_side, _lead_goals,
        int(elapsed or 0), _score_dist)

    result = {
        "home": home, "away": away,
        "odds": {"oh": oh, "od": od, "oa": oa},
        "market_prob": {"h": round(ph, 4), "d": round(pd, 4), "a": round(pa, 4)},
        "overround": round(overround, 4),
        "direction": direction,
        "model_type": "cup" if is_cup else "league",
        "model_calibrated_on": "world_cup" if is_wc else ("none" if is_cup else "league"),
        "market_conf": round(market_conf, 4),
        "oip": {
            "lambda_h": r.get("lh"), "lambda_a": r.get("la"),
            "top3_scores": [f"{h}-{a}" for (h, a) in top3],
            "top3_prob": [round(p, 4) for p in top3_prob],
            "top5_scores": [f"{h}-{a}" for (h, a) in top5],
            "top5_prob": [round(p, 4) for p in top5_prob],
            "over15": round(ov15, 4), "over25": round(ov25, 4), "over35": round(ov35, 4),
            # 波胆×让球×大小球交叉标注 + 操盘纪律 (供前端精确展示)
            "scores_annotated": oip_annotated,
            "discipline": oip_discipline,
            "ah_line": oip_ah_line, "ou_line": oip_ou_line,
            "cs_triangulation": cs_triangulation,  # ⑫ 市场结构波胆三角定位(可审计候选集)
            "cs_odds_timeline": _cs_timeline,      # ⑬ CS 三时点赔率(open/ht_close/live)+drift_live_open/drift_ht_open/drift_summary; 仅GQ已采集比赛
            "cs_drift_signal": (_cs_timeline or {}).get("drift_summary"),  # ⑬b 顺人性盘读数(初盘→中场收盘 drift 主导): follow_money/fade/neutral; 报告验证200↓/45↑
            "cs_follow_signal": _cs_follow,       # ⑬c CS波胆跟庄信号 (FOLLOW/FADE/WATCH) + 三色明细
            "cs_longtail": cs_longtail,            # OPT-B: 庄家未报的OIP高概率长尾比分(补覆盖缺口)
            "draw_zone": draw_zone,                # 平局高发区bias: 被看好方胜赔∈[1.31,1.45]时 True
            "draw_zone_boost": round(draw_zone_boost, 3),  # 平局概率保守修正系数(封顶1.5x), 不改verdict
            "draw_zone_signal": draw_zone_signal,  # 触发带标识: low_fav / flat_draw_trap / None
        },
        "draw_signal": {"market_pdraw": round(m_pd, 4), "draw_alert": draw_alert},
        "strategy_signals": strategy_signals,   # 三方向策略信号(全联赛触发, 面板提示级, 不改verdict; 每项附 tier 溯源)
        "strategy_tier": _strat_tier,           # 信号溯源标签: obscure / main / cup (不再门控触发)
        "consensus": consensus,
        "draw_override": draw_verdict_override,
        "risk": {"high_vig": high_vig},
        "handicap": handicap,
        "operator_view": operator_view,
        "value_layer": value_layer,
        "sub_markets": sub_markets,
        "inplay": inplay_info,  # In-play 条件概率信息 (None=赛前模式/未裁剪)
        "cross_book": _get_cross_book_signal(
            home=home, away=away,
            league=league or (sport_key if isinstance(sport_key, str) else "")),
        # 联赛/赛事进球水平先验 (2026-08-12 接入): 中心 λ 升级为联赛感知;
        # expected_total = 收缩混合后; expected_total_raw = 原 OIP(lh+la) (零回归审计用).
        "expected_total": (round(league_scoring["adjusted_total"], 3)
                           if league_scoring else round(oip_raw_total, 3)),
        "expected_total_raw": round(oip_raw_total, 3),
        "league_scoring": league_scoring,
        # 2026-08-30 比分分析器三级判定结果 (定方向/软加权/观望)
        "score_analysis": _score_analysis,
    }

    # ── V7.1 复盘链路: 赛前分析快照落库 (非致命, 失败仅 log, 绝不影响主预测返回) ──
    if _GQ_DB_OK and mid is not None:
        try:
            ensure_analysis_cache()
            save_analysis(mid, result)
        except Exception as _sa_e:
            logger.warning(f"[analysis_cache] 缓存写入失败(mid={mid}): {_sa_e}")

    return _json_safe(result)


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
        # 修(2026-07-30 体检): 幂等唯一索引, 防同场重预测插重复行污染 ROI/回撤统计
        cur.execute("""CREATE UNIQUE INDEX IF NOT EXISTS uq_bet_records
                       ON bet_records(home_team, away_team, match_date, bet_type, source)""")
        cur.execute(
            """INSERT OR IGNORE INTO bet_records
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

    # In-play 条件概率 (可选): 前端传入当前比分时, _live_predict 启用条件 Poisson 裁剪
    home_goals: Optional[int] = None       # 主队已进球数
    away_goals: Optional[int] = None       # 客队已进球数
    elapsed: Optional[int] = None          # 已赛分钟数

    # 决策闭环 (P0): record=True 时将本次价值层结论落库 bet_records, 供后续 ROI 回补
    record: bool = False
    # V7.1 复盘链路: 比赛ID, 用于关联 match_outcomes 做赛后复盘; 不传则不缓存
    mid: Optional[str] = None


@app.post("/api/predict/live")
async def predict_live_api(req: LivePredictRequest):
    """单场真实1X2赔率 -> 锁定架构全链路预测 (方向=市场argmax, OIP比分/OU, 平局信号, 让球分析)"""
    try:
        out = _live_predict(req.home, req.away, req.oh, req.od, req.oa,
                            home_norm=req.home_norm, away_norm=req.away_norm,
                            date=req.date, league=req.league, mid=req.mid,
                            extra_bookmakers=req.extra_bookmakers,
                            correct_score_books=req.correct_score_books,
                            hcp_line=req.hcp_line, hcp_home_odds=req.hcp_home_odds,
                            hcp_away_odds=req.hcp_away_odds,
                            ou_line=req.ou_line, over_water=req.over_water,
                            under_water=req.under_water,
                            home_goals=req.home_goals, away_goals=req.away_goals,
                            elapsed=req.elapsed)
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


def _lookup_op_cs(home: str, away: str):
    """从 data/events.db match_outcomes 按 home/away 查最新一场的 op_cs (操盘手CS赔率 JSON 串).
    前端不持有操盘手CS赔率, 后端自动回退查库; 查不到返回 None (ranked_predictor 降级纯OIP)."""
    try:
        import sqlite3, os
        db = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "events.db")
        if not os.path.exists(db):
            return None
        con = sqlite3.connect(db)
        cur = con.cursor()
        cur.execute(
            "SELECT op_cs FROM match_outcomes WHERE home=? AND away=? AND op_cs IS NOT NULL "
            "ORDER BY rowid DESC LIMIT 1",
            (home, away),
        )
        row = cur.fetchone()
        con.close()
        return row[0] if row else None
    except Exception:
        return None


# ── 联赛反查缓存 (主客队 → 联赛), 60s TTL, 避免每次预测都查库 ──
_LEAGUE_CACHE: Dict[str, tuple] = {}
_LEAGUE_CACHE_TS = 0.0
_LEAGUE_CACHE_TTL = 60.0


def _lookup_league(home: str, away: str):
    """按主客队从 events.db 反查联赛名 (供联赛进球先验/杯赛识别零配置激活).

    查 match_outcomes(历史归档, 含 league) → matches(实时). 命中即返回联赛名;
    查不到返回 None (调用方保持原行为, 先验不生效, 零回归). 带 60s 缓存.
    """
    global _LEAGUE_CACHE_TS
    import time as _t
    key = f"{home}|{away}"
    now = _t.time()
    if key in _LEAGUE_CACHE and (now - _LEAGUE_CACHE_TS) < _LEAGUE_CACHE_TTL:
        return _LEAGUE_CACHE[key]
    lg = None
    try:
        import sqlite3, os
        db = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "events.db")
        if os.path.exists(db):
            con = sqlite3.connect(db)
            cur = con.cursor()
            for tbl in ("match_outcomes", "matches"):
                cur.execute(
                    f"SELECT league FROM {tbl} WHERE home=? AND away=? "
                    f"AND league IS NOT NULL AND league != '' ORDER BY rowid DESC LIMIT 1",
                    (home, away),
                )
                row = cur.fetchone()
                if row and row[0]:
                    lg = row[0]
                    break
            con.close()
    except Exception:
        pass
    _LEAGUE_CACHE[key] = lg
    _LEAGUE_CACHE_TS = now
    return lg


class RankedPredictRequest(BaseModel):
    """概率排名编排器请求 (已接前端 MatchAnalysisModal)."""
    home: str
    away: str
    oh: float
    od: float
    oa: float
    ou_line: Optional[float] = None
    over_water: Optional[float] = None
    under_water: Optional[float] = None
    op_cs: Optional[str] = None  # JSON 串 [['1-1',8.3],...]; 缺省后端自动从 events.db 回退
    league: Optional[str] = None  # 联赛(可选); 缺省后端自动从 DB 反查


@app.post("/api/predict/ranked")
async def predict_ranked_api(req: RankedPredictRequest):
    """概率排名编排器 — 三市场去水锚定 + 跨市场统一排名 (OU不特权). 已接前端 MatchAnalysisModal."""
    try:
        from pipeline.ranked_predictor import predict as ranked_predict, to_api_contract
        op_cs = req.op_cs
        if not op_cs:
            op_cs = _lookup_op_cs(req.home, req.away)
        # 联赛零配置反查 (与 _live_predict 同源): 让排名编排器也吃到联赛进球先验
        lg = req.league or _lookup_league(req.home, req.away)
        r = ranked_predict(req.home, req.away, req.oh, req.od, req.oa,
                           ou_line=req.ou_line, ou_over=req.over_water, ou_under=req.under_water,
                           op_cs=op_cs, league=lg)
        return _wrap_data(to_api_contract(r))
    except Exception as e:
        logger.error(f"概率排名预测失败: {e}", exc_info=True)
        return _wrap_data({"error": f"概率排名预测失败: {e}"})


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



# ══════════════════════════════════════════════════════════════════════
# V7.1 复盘链路 — 赛事分析缓存 / 赛后修正 / 后端复盘
#   仅 API + 可导出 CSV, 不做前端页面。所有端点非致命包裹。
# ══════════════════════════════════════════════════════════════════════

def _analysis_rows_to_csv(rows: list) -> str:
    """将查询行转为 CSV 文本 (标准库 csv, 含表头)。"""
    import csv, io
    cols = ["analysis_id", "mid", "captured_at",
             "league", "kickoff", "home", "away",
             "verdict", "pred_score_home", "pred_score_away",
             "edge", "stake_suggestion", "stake_amount", "confidence",
             "odds_type", "snapshot_ref",
             "result_actual", "verdict_hit", "score_err",
             "stake_pnl", "deviation_note", "corrected_at"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    for r in rows:
        w.writerow([
            r.get("analysis_id"), r.get("mid"), r.get("captured_at"),
            r.get("o_league"), r.get("o_kickoff"), r.get("o_home"), r.get("o_away"),
            r.get("verdict"), r.get("pred_score_home"), r.get("pred_score_away"),
            r.get("edge"), r.get("stake_suggestion"), r.get("stake_amount"),
            r.get("confidence"), r.get("odds_type"), r.get("snapshot_ref"),
            r.get("result_actual"), r.get("verdict_hit"), r.get("score_err"),
            r.get("stake_pnl"), r.get("deviation_note"), r.get("corrected_at"),
        ])
    return buf.getvalue()


@app.get("/api/analysis/cache")
async def analysis_cache_api(date: str = "", league: str = "",
                            result: str = "", verdict_hit: str = ""):
    """分析缓存查询 (LEFT JOIN match_outcomes)。

    筛选: date(开赛日前缀) / league(模糊) / result(home|draw|away) / verdict_hit(hit|miss|miss_draw)。
    对「有赛果但未修正」的行即时懒修正后返回, 无需先手动 backfill。
    """
    if not _GQ_DB_OK:
        return _wrap_data({"error": "复盘链路未加载(gq.db 不可用)", "rows": []})
    try:
        rows = query_analysis_cache(date=date, league=league,
                                  result=result, verdict_hit=verdict_hit)
        return _wrap_data({"rows": rows, "total": len(rows)})
    except Exception as e:
        logger.error(f"[analysis_cache] 查询失败: {e}")
        return _wrap_data({"error": f"查询失败: {e}", "rows": []})


@app.get("/api/analysis/backfill")
async def analysis_backfill_api():
    """触发批量赛后修正: 遍历已缓存且 match_outcomes 已有赛果但未修正的 mid。
    返回补算条数 count。"""
    if not _GQ_DB_OK:
        return _wrap_data({"error": "复盘链路未加载(gq.db 不可用)", "count": 0})
    try:
        cnt = backfill_all()
        return _wrap_data({"count": cnt, "message": f"已补算 {cnt} 条赛后修正"})
    except Exception as e:
        logger.error(f"[analysis_cache] backfill 失败: {e}")
        return _wrap_data({"error": f"backfill 失败: {e}", "count": 0})


@app.get("/api/analysis/export")
async def analysis_export_api(date: str = "", league: str = "",
                             result: str = "", verdict_hit: str = ""):
    """导出 CSV (含表头, 供操盘手拉表复盘)。筛选同 /api/analysis/cache。"""
    if not _GQ_DB_OK:
        return Response(content="error,gq.db 不可用\n", media_type="text/csv",
                        headers={"Content-Disposition": "attachment; filename=analysis_export.csv"})
    try:
        rows = query_analysis_cache(date=date, league=league,
                                  result=result, verdict_hit=verdict_hit)
        csv_text = _analysis_rows_to_csv(rows)
        return Response(content=csv_text, media_type="text/csv",
                        headers={"Content-Disposition": "attachment; filename=analysis_cache.csv"})
    except Exception as e:
        logger.error(f"[analysis_cache] 导出失败: {e}")
        return Response(content=f"error,{e}\n", media_type="text/csv")


@app.get("/api/analysis/scan")
async def analysis_scan_api(top: int = 12, min_odds: float = 0.0, only_scheduled: bool = False):
    """扫描实时盘口结构，返回最强信号榜单（依据赔率结构评分，非投注建议）。

    数据源: events.db odds_snapshots，通过 pipeline.analysis_center.run_scan 实时分析。
    参数:
      min_odds       — 只看最小赔率≥该值的场次(以小博大, 默认0=不限)
      only_scheduled — 只看未开赛(scheduled)场次, 排除已开赛(live)小赔率热门
    返回: {ok, data: {stats, overview, top}}，每条匹配前端 ScanMatch 接口。
    """
    if not _ANALYSIS_CENTER_OK:
        return JSONResponse({"ok": False, "error": "分析中心引擎未加载", "data": None})
    try:
        data = _analysis_scan(limit=300, top_n=top, min_odds=min_odds, only_scheduled=only_scheduled)
        return {"ok": True, "data": data}
    except Exception as e:
        logger.error(f"[analysis_scan] 失败: {e}")
        return JSONResponse({"ok": False, "error": f"扫描失败: {e}", "data": None})


@app.get("/api/analysis/three-market")
async def three_market_api(match_key: str = "", mid: str = ""):
    """三盘联合分析 (滚球神器 v2 基准) — 胜平负+让球+大小球 联合盘定。

    数据源: match_outcomes (初盘 opening odds, 健康表, 不受 odds_snapshots 坏页影响)。
    平局信号: draw_module (初盘1X2去水p_d + 类型识别, 全量AUC=0.57/0.579)。
    返回: {ok, data: analyze_three_market 结果}。无初盘数据返回 available=False。
    """
    if not _LIVE_GOAL_OK or _analyze_three_market is None:
        return JSONResponse({"ok": False, "error": "滚球神器引擎未加载", "data": None})
    try:
        mk = match_key.strip() if match_key else ""
        midv = mid.strip() if mid else ""
        data = _analyze_three_market(match_key=mk or None, mid=midv or None)
        return {"ok": True, "data": data}
    except Exception as e:
        logger.error(f"[three_market] 失败: {e}")
        return JSONResponse({"ok": False, "error": f"三盘分析失败: {e}", "data": None})


@app.get("/api/analysis/three-market/list")
async def three_market_list_api(limit: int = 50):
    """三盘联合分析 — 可选比赛清单 (来自 match_outcomes 初盘, 健康表)。"""
    if not _LIVE_GOAL_OK or _list_three_market_candidates is None:
        return JSONResponse({"ok": False, "error": "滚球神器引擎未加载", "data": None})
    try:
        data = _list_three_market_candidates(limit=limit)
        return {"ok": True, "data": data}
    except Exception as e:
        logger.error(f"[three_market_list] 失败: {e}")
        return JSONResponse({"ok": False, "error": f"清单失败: {e}", "data": None})



@app.get("/api/cold-door")
async def cold_door_api(match_key: str = "", home: str = "", away: str = "", league: str = ""):
    """冷门波胆检测 — 赛前CS网格 + 正路盘口交叉验证闭环。

    流程(零终场泄露, 严格赛前特征):
      1) 取该场赛前冻结CS网格(pre_match_cs.odds_json)。
      2) 冷门模型 predict_for_grid -> 每比分 市场隐含/公平概率/模型P/edge/冷门候选。
      3) 正路盘口(analysis_center.cs_top3_from_market) 取常规预期Top分。
      4) 闭环校验: 冷门候选须 (edge>0 & 长赔方 & 模型P>市场) 且 正路盘口未将其列常规预期
         -> CONFIRMED(分歧成立) / REJECTED(正路盘口已预期) / NONE。
    注: 模型 out-of-time 验证显示 edge>0 长赔方命中率0%, 任何 CONFIRMED 仅作研究/护栏信号, 非投注依据。
    """
    if not _COLD_OK:
        return JSONResponse({"ok": False, "error": "冷门模型未加载", "data": None})
    try:
        gq = sqlite3.connect(os.path.join(PROJECT_ROOT, "data", "events.db"))
        grid = None; mk_used = match_key; h = home; a = away; lg = league; ko = ""
        if match_key:
            row = gq.execute(
                "SELECT odds_json,home,away,league,kickoff FROM pre_match_cs "
                "WHERE match_key LIKE ? ORDER BY frozen_at DESC LIMIT 1", (f"%{match_key}%",)
            ).fetchone()
            if row and row[0]:
                grid = json.loads(row[0]); h, a, lg, ko = row[1], row[2], row[3], row[4]
        if grid is None and home and away:
            row = gq.execute(
                "SELECT odds_json FROM pre_match_cs WHERE home LIKE ? AND away LIKE ? "
                "ORDER BY frozen_at DESC LIMIT 1", (f"%{home}%", f"%{away}%")
            ).fetchone()
            if row and row[0]:
                grid = json.loads(row[0])
        gq.close()
        if grid is None:
            return {"ok": True, "data": {"found": False,
                    "message": "该场无赛前CS冻结盘口(pre_match_cs), 无法做冷门检测"}}

        cold = _cold_predict(grid, h or "", a or "", lg or "", "")
        if cold is None:
            return {"ok": True, "data": {"found": True, "message": "CS网格去水失败"}}

        # 3) 正路盘口常规预期
        regular_top = []
        try:
            if mk_used:
                rt = _regular_cs_top3(mk_used)
                if rt:
                    regular_top = [r.get("score") for r in rt]
        except Exception:
            regular_top = []
        regular_set = set(regular_top)

        # 4) 闭环校验
        confirmed = []
        for s in cold["scores"]:
            s["regular_top"] = regular_top[:3]
            if s.get("cold_candidate"):
                if s["score"] in regular_set:
                    s["loop_verdict"] = "REJECTED"   # 正路盘口已将其列常规预期 -> 非冷门分歧
                else:
                    s["loop_verdict"] = "CONFIRMED"  # 公平>市场 且 正路盘口未预期 -> 分歧成立
                    confirmed.append(s["score"])
            else:
                s["loop_verdict"] = "NONE"
        n_cold = sum(1 for s in cold["scores"] if s.get("cold_candidate"))
        verdict = ("检出冷门分歧(待人工复核)" if confirmed
                   else "未检出冷门edge — 正路盘口已正确定价")
        return {"ok": True, "data": {
            "found": True, "match_key": mk_used, "home": h, "away": a,
            "league": lg, "exp_tot_goals": cold.get("exp_tot_goals"),
            "regular_top": regular_top[:3],
            "cold_signals": cold.get("cold_signals", []),
            "confirmed_cold": confirmed,
            "n_cold_candidates": n_cold,
            "loop_status": "CLOSED_OK",
            "verdict": verdict,
            "warning": "冷门模型 out-of-time 验证: edge>0 长赔方命中率0%, CONFIRMED 仅作研究/护栏信号, 非投注依据",
            "scores": cold["scores"],
        }}
    except Exception as e:
        logger.error(f"[cold-door] 失败: {e}")
        return JSONResponse({"ok": False, "error": f"冷门检测失败: {e}", "data": None})


def _is_virtual_league(name: str) -> bool:
    if not name:
        return False
    n = name.strip()
    if n.startswith("VS-") or "分钟" in n:
        return True
    return n in {"瓦尔哈拉杯 2026 (8分钟)", "瓦尔基里杯 2026 (8分钟)", "梦幻对垒"}


def _get_cs_grid(con, match_key, kickoff=None):
    """取一场比赛的 CS 网格：优先 pre_match_cs 冻结盘，否则取 odds_snapshots 最新完整批次。"""
    row = con.execute(
        "SELECT odds_json FROM pre_match_cs WHERE match_key=? AND odds_json IS NOT NULL LIMIT 1",
        (match_key,)
    ).fetchone()
    if row:
        try:
            grid = json.loads(row[0])
            if grid and len(grid) >= 5:
                return grid
        except Exception:
            pass
    # 回退：实时 CS 快照
    sql = """SELECT CAST(captured_at AS INT) AS sec, selection, odds
             FROM odds_snapshots WHERE match_key=? AND market='CS'"""
    params = [match_key]
    if kickoff:
        try:
            kt = datetime.fromisoformat(str(kickoff).replace("Z", "+00:00")).timestamp()
            sql += " AND captured_at <= ?"
            params.append(kt)
        except Exception:
            pass
    sql += " ORDER BY sec DESC"
    rows = con.execute(sql, params).fetchall()
    batches = {}
    for sec, sel, odds in rows:
        if odds is None or odds <= 2.0 or odds > 1000:
            continue
        nsel = _norm_cs(sel)          # '0-0'/'0.0' → '0:0'
        if nsel is None:
            continue
        batches.setdefault(sec, {})[nsel] = odds
    best = None
    for sec in sorted(batches.keys(), reverse=True):
        if best is None or len(batches[sec]) > len(best):
            best = batches[sec]
        if len(best) >= 5:
            break
    return best


# ── CS 波胆诱导标记 (庄家破绽检测) ──────────────────────────────
# 全样本实证基准 (cs_verification, N=5771 已验证比赛):
#   - 最便宜波胆历史命中率 13.9% (800/5771) → 无脑押最便宜波胆 86% 输
#   - 真实比分庄家根本未开赔率 65.8% (3800/5771)
#   - CS 盘口抽水 margin 中位 0.376 / 均值 0.536
# 结论: CS 盘口是资金引导器, 不是概率映射。低赔簇(普遍<10)是诱导密集区。
_CS_INDUCE_BASELINE = {
    "cheapest_hit_rate": 0.139,
    "true_not_priced_rate": 0.658,
    "margin_median": 0.376,
}

import re as _re
_CS_OTHER_RE = _re.compile(r'^(其他|other|any\s*other|others)$', _re.IGNORECASE)
_CS_PAIR_RE = _re.compile(r'^(\d{1,3})\s*[-:.]\s*(\d{1,3})$')

def _norm_cs(raw):
    """本地副本: 波胆比分标签归一为英文冒号 '0:0' (与 gq/db.normalize_cs_score 同义)。"""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    if _CS_OTHER_RE.match(s):
        return s
    m = _CS_PAIR_RE.match(s)
    if not m:
        return None
    return f"{int(m.group(1))}:{int(m.group(2))}"


def _cs_induce_analyze(grid: dict, actual_score: str = None) -> dict:
    """从 CS 网格计算庄家诱导标记。grid: {score: odds}。"""
    if not grid or len(grid) < 3:
        return {"has_cs_data": False}
    # 比分键归一: '0-0'/'0.0' → '0:0' (英文冒号格式)
    norm_grid = {}
    for k, v in grid.items():
        nk = _norm_cs(k)
        if nk is None:
            continue
        norm_grid[nk] = v
    grid = norm_grid
    base = _CS_INDUCE_BASELINE
    # 根据当前比分过滤已不可能的波胆: 当前主队已 sh 球/客队已 sa 球, 最终比分必须 >= 当前。
    # 过滤后若剩余项不足 3 则回退到不过滤, 避免误杀。
    # 若当前比分已超出赛前网格最大覆盖(如5-0但网格只到3:4), 诱导标记直接失效。
    if actual_score:
        try:
            ns = _norm_cs(actual_score)
            if ns and ':' in ns:
                sh_a, sa_a = map(int, ns.split(':'))
                # 当前比分超出赛前网格覆盖范围 → 诱导标记失效
                try:
                    max_home = max(int(k.split(':')[0]) for k in grid if ':' in k)
                    max_away = max(int(k.split(':')[1]) for k in grid if ':' in k)
                    if sh_a > max_home or sa_a > max_away:
                        return {
                            "has_cs_data": True,
                            "n_scores": len(grid),
                            "overround": round(sum(1.0 / float(v) for v in grid.values() if isinstance(v, (int, float)) and v > 0), 3),
                            "margin": round(sum(1.0 / float(v) for v in grid.values() if isinstance(v, (int, float)) and v > 0) - 1.0, 3),
                            "favorite_score": None,
                            "favorite_odds": None,
                            "cluster": [],
                            "induce_level": "NONE",
                            "induce_reasons": [f"当前比分 {ns} 超出赛前波胆网格覆盖范围(最大 {max_home}:{max_away}), 诱导标记失效"],
                            "historical_cheapest_hit_rate": base["cheapest_hit_rate"],
                            "historical_true_not_priced_rate": base["true_not_priced_rate"],
                            "actual_score": actual_score,
                            "actual_in_set": False,
                        }
                except Exception:
                    pass
                filtered = {}
                for k, v in grid.items():
                    try:
                        kh, ka = map(int, k.split(':'))
                        if kh >= sh_a and ka >= sa_a:
                            filtered[k] = v
                    except Exception:
                        continue
                if len(filtered) >= 3:
                    grid = filtered
        except Exception:
            pass
    items = [(k, float(v)) for k, v in grid.items()
             if isinstance(v, (int, float)) and v > 0]
    if len(items) < 3:
        return {"has_cs_data": False}
    items.sort(key=lambda x: x[1])
    inv = sum(1.0 / o for _, o in items)
    overround = inv
    margin = overround - 1.0
    favorite_score, favorite_odds = items[0]
    cluster = [{"score": s, "odds": round(o, 2)} for s, o in items[:5]]
    reasons = []
    level = "NONE"
    if margin > 0.4:
        level = "RED"
        reasons.append(f"CS 抽水 {margin*100:.0f}% 极高(庄家净赚>40%)")
    if favorite_odds < 8:
        level = "RED"
        reasons.append(f"最便宜波胆仅 {favorite_odds:.1f}(<8, 极密集诱导区)")
    elif favorite_odds < 12:
        if level == "NONE":
            level = "YELLOW"
        reasons.append(f"最便宜波胆 {favorite_odds:.1f}(<12, 低赔诱导簇)")
    if margin > 0.3 and level == "NONE":
        level = "YELLOW"
        reasons.append(f"CS 抽水 {margin*100:.0f}% 偏高")
    actual_in_set = None
    if actual_score:
        if actual_score in grid:
            actual_in_set = True
            ao = float(grid[actual_score])
            if ao > favorite_odds * 1.5:
                reasons.append(f"真实比分 {actual_score} 赔率 {ao:.1f} 远高于最便宜波胆 → 落入高赔漏开区")
            elif ao > favorite_odds:
                reasons.append(f"真实比分 {actual_score} 赔率 {ao:.1f} 高于最便宜波胆")
        else:
            actual_in_set = False
            reasons.append(f"真实比分 {actual_score} 庄家根本未开赔率(诱导漏开)")
    return {
        "has_cs_data": True,
        "n_scores": len(items),
        "overround": round(overround, 3),
        "margin": round(margin, 3),
        "favorite_score": favorite_score,
        "favorite_odds": round(favorite_odds, 2),
        "cluster": cluster,
        "induce_level": level,
        "induce_reasons": reasons,
        "historical_cheapest_hit_rate": base["cheapest_hit_rate"],
        "historical_true_not_priced_rate": base["true_not_priced_rate"],
        "actual_score": actual_score,
        "actual_in_set": actual_in_set,
    }


@app.get("/api/cs/induce-flag")
async def cs_induce_flag_api(match_key: str = "", home: str = "", away: str = "",
                             actual_score: str = ""):
    """CS 波胆诱导标记 — 检测庄家是否在用低赔波胆簇引导资金。

    数据来源: pre_match_cs 冻结盘 → odds_snapshots(CS) 实时快照(复用 _get_cs_grid 三级回退)。
    评级:
      RED    : 抽水>40% 或 最便宜波胆<8 (极密集诱导区)
      YELLOW : 最便宜波胆<12 或 抽水>30% (低赔诱导簇)
      NONE   : 无显著诱导信号
    提示: 该标记仅作"资金流向警示", 非投注依据。
    """
    try:
        match_key = _resolve_mk(match_key)
        gq = sqlite3.connect(os.path.join(PROJECT_ROOT, "data", "events.db"), timeout=30)
        gq.execute("PRAGMA busy_timeout=30000")
        grid = None
        mk_used = match_key
        if match_key:
            grid = _get_cs_grid(gq, match_key)
        if grid is None and home and away:
            rows = gq.execute(
                "SELECT odds_json FROM pre_match_cs WHERE home LIKE ? AND away LIKE ? "
                "ORDER BY frozen_at DESC LIMIT 1", (f"%{home}%", f"%{away}%")
            ).fetchone()
            if rows and rows[0]:
                try:
                    g = json.loads(rows[0])
                    if g and len(g) >= 5:
                        grid = g
                        mk_used = f"{home} vs {away}"
                except Exception:
                    pass
        gq.close()
        if not grid:
            return {"ok": True, "data": {
                "found": False, "has_cs_data": False,
                "match_key": mk_used,
                "message": "该场未采集波胆赔率(pre_match_cs/odds_snapshots 均无 CS 盘口) — 无法做诱导检测",
                "historical_cheapest_hit_rate": _CS_INDUCE_BASELINE["cheapest_hit_rate"],
                "historical_true_not_priced_rate": _CS_INDUCE_BASELINE["true_not_priced_rate"],
            }}
        res = _cs_induce_analyze(grid, actual_score or None)
        res["found"] = True
        res["match_key"] = mk_used
        return {"ok": True, "data": res}
    except Exception as e:
        logger.error(f"[cs/induce-flag] 失败: {e}")
        return JSONResponse({"ok": False, "error": f"CS诱导检测失败: {e}", "data": None})


# ── CS 信任卡 (结合初盘所有赔率的结构校准分布 + 庄家对照 + 诱导标记) ──
# 缓存范式对标 live_scores_api(5276): 全局 cache + asyncio.Lock 单飞 + TTL=5s,
# 根治并发打 7.9GB WAL 库导致的 30s 超时。
_CS_TRUST_CACHE = {"fetched_at": 0.0, "key": None, "data": None}
_CS_TRUST_LOCK = None


def _gather_opening_odds(gq, match_key: str, home: str, away: str) -> dict:
    """取初盘 1X2/OU/AH。优先 match_outcomes(op_*), 失败回退 odds_snapshots(最早冻结)。
    match_outcomes 无 match_key 列, 用 home/away 关联。"""
    res: dict = {}
    try:
        row = None
        if home and away:
            row = gq.execute(
                "SELECT op_1x2_h, op_1x2_d, op_1x2_a, op_ou_line, op_ou_over, op_ou_under, "
                "op_ah_line, op_ah_home, op_ah_away FROM match_outcomes "
                "WHERE home LIKE ? AND away LIKE ? ORDER BY kickoff DESC LIMIT 1",
                (f"%{home}%", f"%{away}%"),
            ).fetchone()
        if row:
            oh, od, oa, oul, ouo, ouu, ahl, ahh, aha = row
            if oh and od and oa:
                res["h"], res["d"], res["a"] = float(oh), float(od), float(oa)
            if oul and ouo and ouu:
                res["ou_line"], res["ou_over"], res["ou_under"] = float(oul), float(ouo), float(ouu)
            if ahl and ahh and aha:
                res["ah_line"], res["ah_home"], res["ah_away"] = float(ahl), float(ahh), float(aha)
    except Exception:
        pass
    # 回退: odds_snapshots 最早冻结的 1X2
    # 2026-08-27 修复: 原 match_key LIKE + 无时间窗 → 31M 行全表扫 50s(obscure 场卡死 momentum 端点).
    # 改精确 match_key + 90 天窗口, 命中 idx_odds_snapshot_mk_mkt_ts 索引毫秒级返回.
    if not (res.get("h") and res.get("d") and res.get("a")):
        try:
            mk = match_key or (f"%{home}%" if home else None)
            if mk:
                if match_key and "%" not in match_key:
                    rows = gq.execute(
                        "SELECT selection, odds FROM odds_snapshots WHERE match_key = ? AND market='1X2' "
                        "AND captured_at > strftime('%s','now','-90 day') "
                        "ORDER BY captured_at ASC LIMIT 10", (match_key,)
                    ).fetchall()
                else:
                    rows = gq.execute(
                        "SELECT selection, odds FROM odds_snapshots WHERE match_key LIKE ? AND market='1X2' "
                        "AND captured_at > strftime('%s','now','-90 day') "
                        "ORDER BY captured_at ASC LIMIT 10", (mk,)
                    ).fetchall()
                mp = {}
                for sel, od in rows:
                    mp[str(sel).lower()] = float(od)
                if "home" in mp and "draw" in mp and "away" in mp:
                    res["h"], res["d"], res["a"] = mp["home"], mp["draw"], mp["away"]
        except Exception:
            pass
    # ── OU/AH 开盘回退 (2026-08-28): 临场早盘补采后 odds_snapshots 有完整 OU/AH 线族 ──
    # 流内自洽取最早完整对(同一条流的 over+under/home+away), OU 选最接近 2.5、AH 最接近 0,
    # 供三盘 λμ 拟合与 DB 五维结构检索 (fit_sources 1X2 → 1X2+OU+AH)。
    if match_key:
        try:
            def _open_pair(prefix: str, not_likes, ref: float, sels):
                nl = " AND ".join(f"market NOT LIKE '{p}%'" for p in not_likes)
                rows = gq.execute(
                    f"SELECT market, selection, odds FROM odds_snapshots "
                    f"WHERE match_key=? AND market LIKE ? AND {nl} "
                    f"AND odds>1.01 AND odds<1000 "
                    f"AND captured_at > strftime('%s','now','-3 day') "
                    f"ORDER BY captured_at ASC LIMIT 120", (match_key, prefix + '%')).fetchall()
                streams = {}
                for mkt, sel, od in rows:
                    s = streams.setdefault(mkt, {})
                    if sel in sels and sel not in s:
                        s[sel] = float(od)
                cands = []
                for mkt, s in streams.items():
                    if all(k in s for k in sels):
                        try:
                            line = float(mkt.split('_')[1])
                        except Exception:
                            continue
                        cands.append((abs(line - ref), line, s))
                return min(cands)[1:] if cands else None
            if not (res.get("ou_line") and res.get("ou_over") and res.get("ou_under")):
                _ou = _open_pair('OU_', ['OU_1H', 'OU_2H'], 2.5, ('over', 'under'))
                if _ou:
                    res["ou_line"], _ou_s = _ou
                    res["ou_over"], res["ou_under"] = _ou_s['over'], _ou_s['under']
            if not (res.get("ah_line") is not None and res.get("ah_home") and res.get("ah_away")):
                _ah = _open_pair('AH_', ['AH_1H', 'AH_2H'], 0.0, ('home', 'away'))
                if _ah:
                    res["ah_line"], _ah_s = _ah
                    res["ah_home"], res["ah_away"] = _ah_s['home'], _ah_s['away']
        except Exception:
            pass
    return res


async def _cs_trust_card_compute(match_key: str, home: str, away: str, actual_score: str,
                                 current_score: str = "", minute: int = 0):
    from pipeline.cs_trust_model import build_trust_card
    gq = sqlite3.connect(os.path.join(PROJECT_ROOT, "data", "events.db"), timeout=5)
    gq.execute("PRAGMA busy_timeout=5000")
    # 从 match_key("主 vs 客") 反解 home/away, 使仅传 match_key 的前端调用也能解析初盘 1X2
    if (not home or not away) and match_key and " vs " in match_key:
        _h, _a = match_key.split(" vs ", 1)
        if not home:
            home = _h
        if not away:
            away = _a
    try:
        grid = None
        mk_used = match_key
        if match_key:
            grid = _get_cs_grid(gq, match_key)
        if grid is None and home and away:
            rows = gq.execute(
                "SELECT odds_json FROM pre_match_cs WHERE home LIKE ? AND away LIKE ? "
                "ORDER BY frozen_at DESC LIMIT 1", (f"%{home}%", f"%{away}%")
            ).fetchone()
            if rows and rows[0]:
                try:
                    g = json.loads(rows[0])
                    if g and len(g) >= 5:
                        grid = g
                        mk_used = f"{home} vs {away}"
                except Exception:
                    pass
        odds = _gather_opening_odds(gq, match_key, home, away)
        if not grid and not (odds.get("h") and odds.get("d") and odds.get("a")):
            return {"ok": True, "data": {
                "found": False,
                "message": "该场无初盘 CS 矩阵且无初盘 1X2, 无法构建信任卡",
                "historical_cheapest_hit_rate": _CS_INDUCE_BASELINE["cheapest_hit_rate"],
                "historical_true_not_priced_rate": _CS_INDUCE_BASELINE["true_not_priced_rate"],
            }}
        # ── 滚球即时盘 (2026-08-28, 用户需求: 波胆跟随开赛后的即时盘口) ──
        # 开赛后用当前滚盘 OU 反解剩余 λμ 并平移当前比分; 开盘三盘仍传入作强度比与
        # 对照。滚球判定: 该场存在 minute_at>0 的盘口快照(且调用方传了当前比分)。
        cur_score_tuple = None
        live_ou_t = None
        live_minute = None
        try:
            sh = sa = None
            if current_score:
                for _sep in ("-", ":"):
                    if _sep in current_score:
                        _h_, _a_ = current_score.split(_sep, 1)
                        sh, sa = int(_h_), int(_a_)
                        break
            max_min = gq.execute(
                "SELECT MAX(minute_at) FROM odds_snapshots WHERE match_key=?", (match_key,)
            ).fetchone()
            has_inplay = bool(max_min and max_min[0] and max_min[0] > 0)
            if sh is not None and sa is not None:
                live_minute = int(minute) if minute else (int(max_min[0]) if has_inplay else None)
                if has_inplay or (live_minute or 0) > 0:
                    cur_score_tuple = (sh, sa)
            if cur_score_tuple is not None and has_inplay:
                from analysis.live_goal_probe import _current_inplay_odds
                _cur = _current_inplay_odds(gq, match_key, live_minute or 1) or {}
                if _cur.get("ou"):
                    live_ou_t = tuple(float(x) for x in _cur["ou"])  # (line, over, under)
        except Exception as _e:
            logger.warning(f"[cs/trust-card] 滚球即时盘解析失败(回退开盘): {_e}")
            cur_score_tuple = live_ou_t = live_minute = None
        card = build_trust_card(
            cs_grid=grid,
            h=odds.get("h"), d=odds.get("d"), a=odds.get("a"),
            ou_line=odds.get("ou_line"), ou_over=odds.get("ou_over"), ou_under=odds.get("ou_under"),
            ah_line=odds.get("ah_line"), ah_home=odds.get("ah_home"), ah_away=odds.get("ah_away"),
            con=gq,
            current_score=cur_score_tuple,
            live_ou=live_ou_t,
            live_minute=live_minute,
        )
        # ── 统一 CS 推荐 (2026-08-30 SSoT): 合理比分卡/信任卡④栏/终场读数回退 全部
        # 消费 cs_db_match.unified_scoreline 同一函数 — 前端两卡比分零分歧。
        # 赛前=纯DB三盘匹配; 滚球=比分过滤+平移补位(与 cross_score 同源同参数)。
        try:
            from pipeline.cs_db_match import unified_scoreline
            _cs_str = ""
            _mn = 0
            if cur_score_tuple is not None:
                _cs_str = f"{cur_score_tuple[0]}-{cur_score_tuple[1]}"
                _mn = int(live_minute or 0)
            dm = unified_scoreline(h=odds.get("h"), d=odds.get("d"), a=odds.get("a"),
                                   ou_line=odds.get("ou_line"), ou_over=odds.get("ou_over"),
                                   ou_under=odds.get("ou_under"),
                                   ah_line=odds.get("ah_line"), ah_home=odds.get("ah_home"),
                                   ah_away=odds.get("ah_away"),
                                   current_score=_cs_str, current_minute=_mn)
            if dm and dm.get("found"):
                card["db_match"] = dm
        except Exception as _e:
            logger.warning(f"[cs/trust-card] 统一 CS 匹配失败(跳过): {_e}")
        card["match_key"] = mk_used
        if actual_score:
            card["actual_score"] = actual_score
        return {"ok": True, "data": card}
    finally:
        gq.close()


@app.get("/api/cs/trust-card")
async def cs_trust_card_api(match_key: str = "", home: str = "", away: str = "",
                             actual_score: str = "", current_score: str = "", minute: int = 0):
    """CS 信任卡 — 结合初盘所有赔率(1X2+OU+AH+完整CS矩阵)的结构校准分布 + 庄家盘口对照 + 诱导标记。

    形态(决定用户信任生死, 见 reports/cs_inducement_analysis.md):
      - 全比分概率分布(覆盖100%) + 庄家CS线对比(仅覆盖已列比分)
      - 主推 vs 结构概率 背离检测 → 诱导信号
      - 庄家诱导标记 RED/YELLOW/NONE
    2026-08-28: current_score+minute → 滚球模式(odds_phase=live), 剩余λμ由当前滚球OU
    即时盘反解并平移当前比分; 并附 db_match(开盘三盘结构 → DB 历史同结构真实波胆)。
    注意: 本卡为概率估计与盘口对照, 非"AI预测精确比分", 不输出单点答案(trap规避)。
    缓存: TTL=5s 单飞, 防并发打 WAL 库超时(对标 30s 超时修复)。
    """
    global _CS_TRUST_CACHE, _CS_TRUST_LOCK
    if _CS_TRUST_LOCK is None:
        _CS_TRUST_LOCK = asyncio.Lock()
    match_key = _resolve_mk(match_key)
    cache_key = f"{match_key}|{home}|{away}|{actual_score}|{current_score}|{minute}"
    now = time.time()
    if (_CS_TRUST_CACHE["data"] is not None and _CS_TRUST_CACHE.get("key") == cache_key
            and (now - _CS_TRUST_CACHE["fetched_at"]) < 5.0):
        return _CS_TRUST_CACHE["data"]
    async with _CS_TRUST_LOCK:
        if (_CS_TRUST_CACHE["data"] is not None and _CS_TRUST_CACHE.get("key") == cache_key
                and (now - _CS_TRUST_CACHE["fetched_at"]) < 5.0):
            return _CS_TRUST_CACHE["data"]
        result = await _cs_trust_card_compute(match_key, home, away, actual_score,
                                              current_score=current_score, minute=minute)
        _CS_TRUST_CACHE = {"fetched_at": time.time(), "key": cache_key, "data": result}
        return result


# ── 操盘手结论卡 (实时页用: 取初盘1X2 → _live_predict → 蒸馏一行结论) ──
# 同步重算较贵(会触发 _live_predict 全链路), 必须:
#   - asyncio.run_in_executor 跑, 不阻塞事件循环(防冻结, 见部署铁律)
#   - 全局 cache + asyncio.Lock 单飞 + TTL=5s(对标 30s 超时修复, 防并发打 WAL 库)
_LIVE_OP_CARD_CACHE = {"fetched_at": 0.0, "key": None, "data": None}
_LIVE_OP_CARD_LOCK = None


def _live_operator_card_compute(match_key: str, home: str, away: str, league: str | None):
    from pipeline.operator_output import distill_operator_card
    gq = sqlite3.connect(os.path.join(PROJECT_ROOT, "data", "events.db"), timeout=5)
    gq.execute("PRAGMA busy_timeout=5000")
    try:
        odds = _gather_opening_odds(gq, match_key, home, away)
    finally:
        gq.close()
    if not (odds.get("h") and odds.get("d") and odds.get("a")):
        return {"ok": True, "data": {
            "found": False,
            "message": "该场无初盘1X2赔率, 无法蒸馏操盘手结论",
        }}
    result = _live_predict(home, away, odds["h"], odds["d"], odds["a"], league=league)
    card = _distill_operator_card(result)
    card["match_key"] = match_key or (f"{home} vs {away}" if home and away else None)
    return {"ok": True, "data": card}


@app.get("/api/live/operator-card")
async def live_operator_card_api(match_key: str = "", home: str = "", away: str = "",
                                 league: str = None):
    """操盘手结论卡(实时页) — 取初盘初赔 → _live_predict 全链路 → 蒸馏一行结论。

    返回 distill_operator_card 结构: verdict/stake/confidence/evidence[≤3]/trap_score/decision。
    缓存: run_in_executor 防事件循环冻结 + 全局 cache + asyncio.Lock 单飞 + TTL=5s。
    """
    global _LIVE_OP_CARD_CACHE, _LIVE_OP_CARD_LOCK
    if _LIVE_OP_CARD_LOCK is None:
        _LIVE_OP_CARD_LOCK = asyncio.Lock()
    cache_key = f"{match_key}|{home}|{away}|{league}"
    now = time.time()
    if (_LIVE_OP_CARD_CACHE["data"] is not None and _LIVE_OP_CARD_CACHE.get("key") == cache_key
            and (now - _LIVE_OP_CARD_CACHE["fetched_at"]) < 5.0):
        return _LIVE_OP_CARD_CACHE["data"]
    async with _LIVE_OP_CARD_LOCK:
        if (_LIVE_OP_CARD_CACHE["data"] is not None and _LIVE_OP_CARD_CACHE.get("key") == cache_key
                and (now - _LIVE_OP_CARD_CACHE["fetched_at"]) < 5.0):
            return _LIVE_OP_CARD_CACHE["data"]
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(
            None, lambda: _live_operator_card_compute(match_key, home, away, league)
        )
        _LIVE_OP_CARD_CACHE = {"fetched_at": time.time(), "key": cache_key, "data": data}
        return data


@app.get("/api/cold-door/scan")
async def cold_door_scan_api(status: str = "live,scheduled", limit: int = 300,
                              min_odds: float = 12.0, min_edge: float = 0.0,
                              max_results: int = 100):
    """批量扫描 live/scheduled 比赛，返回有 CONFIRMED 冷门波胆信号的比赛列表。

    对每场比赛：
      1) 取 CS 网格（赛前冻结 或 实时 CS 快照）
      2) 冷门模型 predict_for_grid
      3) 正路盘口 top3 交叉验证
      4) 保留 edge>=min_edge & odds>=min_odds & 不在正路 top3 的比分
    """
    if not _COLD_OK:
        return JSONResponse({"ok": False, "error": "冷门模型未加载", "data": []})
    con = None
    try:
        con = sqlite3.connect(os.path.join(PROJECT_ROOT, "data", "events.db"))
        statuses = [s.strip() for s in status.split(",") if s.strip()]
        if not statuses:
            statuses = ["live", "scheduled"]
        in_clause = ",".join("?" * len(statuses))
        rows = con.execute(
            f"""SELECT match_key, home, away, league, kickoff, status,
                       COALESCE(score_home,0), COALESCE(score_away,0)
                FROM matches
                WHERE status IN ({in_clause})
                  AND league NOT LIKE 'VS-%'
                  AND league NOT LIKE '%分钟%'
                  AND league NOT IN ('瓦尔哈拉杯 2026 (8分钟)', '瓦尔基里杯 2026 (8分钟)', '梦幻对垒')
                ORDER BY status, kickoff DESC
                LIMIT ?""",
            (*statuses, limit)
        ).fetchall()

        results = []
        scanned = 0
        for mk, home, away, lg, ko, st, sh, sa in rows:
            scanned += 1
            try:
                grid = _get_cs_grid(con, mk, ko)
                if not grid or len(grid) < 5:
                    continue
                cold = _cold_predict(grid, home or "", away or "", lg or "", ko or "")
                if not cold:
                    continue
                # 正路盘口常规预期
                regular_top = []
                try:
                    rt = _regular_cs_top3(mk)
                    if rt:
                        regular_top = [r.get("score") for r in rt]
                except Exception:
                    regular_top = []
                regular_set = set(regular_top)

                confirmed = []
                for s in cold.get("scores", []):
                    if s.get("cold_candidate") and s["edge"] >= min_edge and s["odds"] >= min_odds:
                        if s["score"] not in regular_set:
                            confirmed.append(s)
                if not confirmed:
                    continue
                confirmed.sort(key=lambda x: x["model_p"], reverse=True)
                results.append({
                    "match_key": mk,
                    "home": home,
                    "away": away,
                    "league": lg,
                    "status": st,
                    "kickoff": ko,
                    "current_score": f"{sh}-{sa}" if st == "live" else None,
                    "exp_tot_goals": cold.get("exp_tot_goals"),
                    "regular_top": regular_top[:3],
                    "n_confirmed": len(confirmed),
                    "confirmed": confirmed[:5],
                })
            except Exception as e:
                logger.warning(f"[cold-door/scan] {mk} 失败: {e}")
                continue

        results.sort(key=lambda x: (-x["n_confirmed"], -max(s["model_p"] for s in x["confirmed"])))
        con.close()
        return {"ok": True, "data": results[:max_results], "count": len(results), "scanned": scanned,
                "warning": "冷门模型 out-of-time 验证显示 edge>0 长赔方命中率0%，任何 CONFIRMED 信号仅作研究/护栏，非投注依据。"}
    except Exception as e:
        logger.error(f"[cold-door/scan] 失败: {e}")
        if con:
            try:
                con.close()
            except Exception:
                pass
        return JSONResponse({"ok": False, "error": f"扫描失败: {e}", "data": []})


@app.get("/api/cold-door/matches")
async def cold_door_matches_api(limit: int = 200):
    """列出可用于冷门波胆检测的赛前CS冻结场次 (pre_match_cs)。"""
    try:
        gq = sqlite3.connect(os.path.join(PROJECT_ROOT, "data", "events.db"))
        rows = gq.execute(
            "SELECT match_key,league,kickoff,frozen_at FROM pre_match_cs "
            "WHERE odds_json IS NOT NULL ORDER BY frozen_at DESC LIMIT ?", (limit,)
        ).fetchall()
        gq.close()
        return {"ok": True, "data": [
            {"match_key": r[0], "league": r[1], "kickoff": r[2], "frozen_at": r[3]}
            for r in rows
        ]}
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"列表失败: {e}", "data": []})


@app.get("/api/live-goal-probe/matches")
async def live_goal_probe_matches_api(limit: int = 50, offset: int = 0):
    """列出当前进行中(live)与未开赛(scheduled)的比赛, 并按破蛋/进球潜力排序, 供滚球破蛋神器选择。
    支持 offset 分页 (live 场次多时避免被 limit 静默截断)。"""
    if not _LIVE_GOAL_OK:
        return JSONResponse({"ok": False, "error": "滚球破蛋模块未加载", "data": {"matches": [], "max_last_seen": None, "total_live": 0, "total_scheduled": 0, "offset": offset, "limit": limit, "server_now": time.time()}})
    try:
        out = await asyncio.to_thread(_live_goal_matches, limit=limit, offset=offset)
        # 过滤非足球(响应层兜底, 不改 _live_goal_matches 内部以免破坏 live_goal_probe 模块)
        try:
            if isinstance(out, dict) and isinstance(out.get('matches'), list):
                out['matches'] = _filter_football_matches(out['matches'])
        except Exception:
            pass
        return {"ok": True, "data": out}
    except Exception as e:
        logger.error(f"[live-goal-probe/matches] 失败: {e}")
        return JSONResponse({"ok": False, "error": f"列表失败: {e}", "data": {"matches": [], "max_last_seen": None, "total_live": 0, "total_scheduled": 0, "offset": offset, "limit": limit, "server_now": time.time()}})


@app.get("/api/live-odds/{match_key}")
async def live_odds_api(match_key: str):
    """6 维盘口聚合 (滚球实时): 1X2/AH/OU × 全场/半场, 含当前 line/odds + 相对开盘 drift。

    数据源 events.db.odds_snapshots (2026-08-27 雷速已删).
    market 命名: 1X2 / AH_<line> / OU_<line> (全场), *_1H_<line> (半场), _N 后缀=多庄家.
    主盘(无后缀 book=0)优先, 无主盘取最新庄家; drift = 最新 odds - 该 line 首次 odds."""
    try:
        match_key = _resolve_mk(match_key)
        import re as _re
        db = os.path.join(PROJECT_ROOT, "data", "events.db")
        c = sqlite3.connect(db)
        m = c.execute("SELECT score_home, score_away, minute, status FROM matches WHERE match_key=?", (match_key,)).fetchone()
        score = f"{m[0] or 0}-{m[1] or 0}" if m else None
        minute = m[2] if m else None
        status = m[3] if m else None
        rows = c.execute(
            "SELECT market, selection, odds, line, captured_at FROM odds_snapshots "
            "WHERE match_key=? AND captured_at > strftime('%s','now','-4 hour')", (match_key,)).fetchall()
        c.close()
        # 解析 market → (period, family, line_key, book, selection, odds, ts)
        P = []
        for mk, sel, od, ln, ts in rows:
            name, book = mk, 0
            mo = _re.match(r'^(.*)_(\d+)$', name)
            if mo:
                name, book = mo.group(1), int(mo.group(2))
            fam = period = None
            lk = None
            if name == "1X2":
                fam, period = "1X2", "full"
            elif name.startswith("1X2_1H"):
                fam, period = "1X2", "half"
            elif name.startswith("AH_1H_"):
                fam, period, lk = "AH", "half", name[6:]
            elif name.startswith("AH_"):
                fam, period, lk = "AH", "full", name[3:]
            elif name.startswith("OU_1H_"):
                fam, period, lk = "OU", "half", name[6:]
            elif name.startswith("OU_"):
                fam, period, lk = "OU", "full", name[3:]
            else:
                continue  # CS/角球/其他家族不进 6 维
            P.append((period, fam, lk, book, sel, od, ts))
        if not P:
            return _wrap_data({"match_key": match_key, "score": score, "minute": minute, "status": status,
                               "dimensions": [], "count": 0})

        def _cell(period, fam, lk, sel):
            cands = [p for p in P if p[0] == period and p[1] == fam and p[2] == lk and p[4] == sel]
            if not cands:
                return None, None
            for p in cands:
                if p[3] == 0:
                    return p[5], p[6]
            b = max(cands, key=lambda p: p[6])
            return b[5], b[6]

        def _drift(period, fam, lk, sel):
            """最新 odds - 该 line 首次 odds (同 book 主盘优先)."""
            cands = [p for p in P if p[0] == period and p[1] == fam and p[2] == lk and p[4] == sel]
            if not cands:
                return None
            first = min(cands, key=lambda p: p[6])
            last = max(cands, key=lambda p: p[6])
            try:
                return round(last[5] - first[5], 3)
            except Exception:
                return None

        dimensions = []
        for period, pname in (("full", "全场"), ("half", "半场")):
            for fam, fname in (("1X2", "独赢"), ("AH", "让球"), ("OU", "大小")):
                keys = sorted({p[2] for p in P if p[0] == period and p[1] == fam})
                if not keys:
                    continue
                # 滚球多线: 取主盘线(book=0 所在 line), 否则取最新 line
                main_lk = keys[0]
                for p in P:
                    if p[0] == period and p[1] == fam and p[3] == 0:
                        main_lk = p[2]
                        break
                rows_out = []
                for sel in ("home", "draw", "away", "over", "under"):
                    v, ts = _cell(period, fam, main_lk, sel)
                    if v is not None:
                        rows_out.append({
                            "selection": sel, "odds": round(v, 2),
                            "drift": _drift(period, fam, main_lk, sel),
                        })
                line_num = None
                try:
                    line_num = float(main_lk) if main_lk not in (None, "", "-0.00", "0.00") else 0.0
                except (TypeError, ValueError):
                    line_num = None
                dimensions.append({
                    "market": f"{fam}_{period}", "name": f"{pname}{fname}",
                    "line": line_num, "rows": rows_out,
                })
        return _wrap_data({"match_key": match_key, "score": score, "minute": minute, "status": status,
                           "dimensions": dimensions, "count": len(dimensions)})
    except Exception as e:
        logger.error(f"[live-odds] {match_key} 失败: {e}")
        return _wrap_data({"error": str(e), "match_key": match_key, "dimensions": [], "count": 0})


# ── 进球触发即时盘口补采 (2026-08-28, 用户诉求: 进球后 OU 即时更新并给出方向) ──
# 链路: 前端 5s 轮询带最新比分 → probe 端点发现"传入比分 ≠ 库内最后快照比分"(= 进球)
# → 后台线程立即拉一次乐鱼详情全市场盘口(不受采集器 45s 节流约束) → 5s 后的下一次
# 轮询即基于进球后新盘口重算方向。per-match 锁防并发, 同比分 60s 防重(水位波动不触发)。
_GOAL_BACKFILL_LAST: dict = {}
_GOAL_BACKFILL_LOCKS: dict = {}


def _goal_backfill_worker(match_key: str, mid: str, new_score: str):
    try:
        from gq import auto_collector as ac
        dec = ac.fetch_match_odds(str(mid))
        if dec:
            gq = sqlite3.connect(os.path.join(PROJECT_ROOT, "data", "events.db"), timeout=5)
            try:
                ko_row = gq.execute("SELECT kickoff FROM matches WHERE match_key=?", (match_key,)).fetchone()
            finally:
                gq.close()
            mgt = 0
            if ko_row and ko_row[0]:
                try:
                    from datetime import datetime as _dt
                    mgt = _dt.fromisoformat(ko_row[0]).timestamp() * 1000.0
                except Exception:
                    mgt = 0
            ac.record_match_odds(dec, {"mid": str(mid), "mgt": mgt})
            logger.info(f"[goal-backfill] {match_key} 进球({new_score}) → 盘口已即时补采")
    except Exception as e:
        logger.warning(f"[goal-backfill] {match_key} 补采失败: {e}")


def _maybe_goal_backfill(match_key: str, current_score: str):
    """进球检测 + fire-and-forget 即时补采。绝对不抛、不阻塞 probe 主响应。"""
    try:
        cur = (current_score or "").strip()
        if not cur or "-" not in cur:
            return
        now = time.time()
        sig = f"{match_key}|{cur}"
        if now - _GOAL_BACKFILL_LAST.get(sig, 0.0) < 60.0:
            return
        gq = sqlite3.connect(os.path.join(PROJECT_ROOT, "data", "events.db"), timeout=5)
        gq.execute("PRAGMA busy_timeout=3000")
        try:
            row = gq.execute(
                "SELECT score_at FROM odds_snapshots WHERE match_key=? AND score_at != '' "
                "ORDER BY id DESC LIMIT 1", (match_key,)).fetchone()
            mid_row = gq.execute("SELECT mid FROM matches WHERE match_key=?", (match_key,)).fetchone()
        finally:
            gq.close()
        last_score = (row[0] if row else "") or ""
        mid = (mid_row[0] if mid_row else "") or ""
        if not mid:
            return
        norm = lambda s: s.replace(":", "-").replace(" ", "")
        if norm(last_score) == norm(cur):
            _GOAL_BACKFILL_LAST[sig] = now   # 比分一致: 记签名防重复查询
            return
        # 比分不一致 → 进球事件: 锁该场, 后台补采
        lock = _GOAL_BACKFILL_LOCKS.setdefault(match_key, threading.Lock())
        if not lock.acquire(blocking=False):
            return   # 已有补采在跑
        _GOAL_BACKFILL_LAST[sig] = now
        threading.Thread(target=_goal_backfill_worker, args=(match_key, mid, cur),
                         daemon=True, name=f"goal-bf-{mid}").start()
    except Exception as e:
        logger.warning(f"[goal-backfill] 触发失败 {match_key}: {e}")


@app.get("/api/live-goal-probe")
async def live_goal_probe_api(match_key: str, score: str = "0-0", minute: int = 0, is_halftime: bool = False):
    """滚球破蛋概率仪: 对指定比赛输出半场/全场破蛋概率与信号方向。

    参数:
      match_key: 比赛键 (如 "主队 vs 客队")
      score: 当前比分 (默认 0-0)
      minute: 当前比赛分钟 (默认 0)
      is_halftime: 是否中场休息(半场结果已定, 不再分析半场破蛋)

    返回:
      {ok, data: {match_key, current_score, current_minute, half{}, full{}, reasons[], warning}}
    """
    if not _LIVE_GOAL_OK:
        return JSONResponse({"ok": False, "error": "滚球破蛋模块未加载", "data": None})
    match_key = _resolve_mk(match_key)
    # 进球触发即时盘口补采 (fire-and-forget, 不阻塞响应; 5s 后的下一次轮询吃到新盘)
    _maybe_goal_backfill(match_key, score)
    try:
        result = await asyncio.to_thread(_live_goal_probe, match_key, current_score=score, current_minute=minute, is_halftime=is_halftime)
        return {"ok": True, "data": result}
    except Exception as e:
        logger.error(f"[live-goal-probe] 失败: {e}")
        return JSONResponse({"ok": False, "error": f"探测失败: {e}", "data": None})


@app.get("/api/live-goal-probe/analyze")
async def live_goal_probe_analyze_api(match_key: str, score: str = "0-0", minute: int = 0,
                                       is_halftime: bool = False):
    """滚球神器 · 决策智能体卡片: 消费模型数据(开盘盘口结构) → 输出 决策/方案/合理比分。

    本地 qwen3 仅作背后推理引擎(系统服务); 卡片展示智能体的「决策和方案」, 非模型闲聊文本。
    返回 {ok, data:{match_key, has_real_open, score_hint, decision, plan, model_data, agent_used}}
    """
    if not match_key:
        return JSONResponse({"ok": False, "error": "match_key 为空", "data": None})
    match_key = _resolve_mk(match_key)
    try:
        def _worker():
            from analysis.model_match_analysis import analyze_match_with_model
            return analyze_match_with_model(match_key, current_score=score,
                                            current_minute=minute, is_halftime=is_halftime)
        # 2026-08-27: 25s 超时保护 — 防模型引擎冷加载(首请求 29s)/偶发慢拖死前端 30s 超时。
        # 超时返回降级(ok=True + 标记), 前端显示"分析超时请重试"而非白屏。
        try:
            out = await asyncio.wait_for(asyncio.to_thread(_worker), timeout=25.0)
        except asyncio.TimeoutError:
            logger.warning(f"[live-goal-probe/analyze] 超时(25s): {match_key}")
            return {"ok": True, "data": {
                "ok": False, "match_key": match_key, "has_real_open": False,
                "score_hint": None, "decision": None, "plan": "",
                "model_data": None, "decision_engine": "deterministic",
                "error": "模型引擎加载中/超时(25s)，请稍后重试", "timeout": True,
            }}
        return {"ok": True, "data": out}
    except Exception as e:
        logger.error(f"[live-goal-probe/analyze] 失败: {e}")
        return JSONResponse({"ok": False, "error": f"决策智能体失败: {e}", "data": None})


@app.get("/api/live-goal-probe/consensus")
async def live_goal_probe_consensus_api(
    match_key: str, score: str = "0-0", minute: int = 0, is_halftime: bool = False,
    home: str = "", away: str = "", league: str = None,
    over: float = None, under: float = None, line: float = None,
    opening_total: float = None, current_total: float = None,
):
    """决策仲裁层 (C 完整档): 聚合 决策智能体 / 操盘手卡 / OU破蛋决策 多路信号,
    产出 signal_consensus / discrepancy / closing_line_value / confidence_interval,
    供前端仲裁卡消除"多卡互相矛盾"体感。

    内部调用 analyze_match_with_model + _live_operator_card_compute (复用缓存/单飞),
    可选 over/under 时补 OU discrepancy (devig fair vs implied)。
    返回 {ok, data:{signal_consensus, discrepancy, closing_line_value, confidence_interval}}。
    """
    if not match_key:
        return JSONResponse({"ok": False, "error": "match_key 为空", "data": None})
    match_key = _resolve_mk(match_key)
    try:
        def _worker():
            from analysis.model_match_analysis import analyze_match_with_model
            from analysis.signal_consensus import build_signal_consensus
            h = home or (match_key.split(" vs ")[0] if " vs " in match_key else "")
            a = away or (match_key.split(" vs ")[1] if " vs " in match_key else "")
            analyze_out = analyze_match_with_model(
                match_key, current_score=score, current_minute=minute, is_halftime=is_halftime
            )
            op_raw = _live_operator_card_compute(match_key, h, a, league)
            operator_card = op_raw.get("data") if isinstance(op_raw, dict) else None

            ou_decision = None
            # 2026-08-27 滚球锚: line/opening_total 未传时, 用该场【当前滚球 OU 线 + 去水隐含总球】作锚,
            # 不再用固定 2.5 / 开盘锚 (用户: 锚点使用滚球数据, 不在固定数值)
            _line = line
            _open_total = opening_total
            if _line is None or _open_total is None:
                try:
                    _ll, _lt = _resolve_live_ou_anchor(match_key, minute)
                    if _line is None:
                        _line = _ll or 2.5
                    if _open_total is None:
                        _open_total = _lt
                except Exception as _e:
                    logger.warning(f"[consensus] 滚球锚解析失败: {_e}")
            # 前端未传 OU 实时赔率时, 自取 analyze_out.model_data.live.ou 的真实 over/under 赔率
            # (诚实边界: 仅用真实市场赔率 devig, 绝不拿模型概率冒充市场概率)
            # 注意: 用全新局部变量 _ov/_un/_ln/_ct 承载, 避免遮蔽闭包参数 over/under/line/current_total
            _ov = over
            _un = under
            _ln = _line
            if (_ov is None or _un is None) and analyze_out:
                try:
                    _lu = ((analyze_out or {}).get("model_data") or {}).get("live", {}) or {}
                    _ou = _lu.get("ou") or {}
                    _o = _ou.get("over_odds")
                    _u = _ou.get("under_odds")
                    _l = _ou.get("line")
                    if _o and _u and _o > 1.0 and _u > 1.0:
                        _ov, _un = float(_o), float(_u)
                        if _l:
                            _ln = float(_l)
                except Exception as e:
                    logger.warning(f"[consensus] 自取 live OU 赔率失败: {e}")
            if _ov is not None and _un is not None:
                try:
                    from pipeline.ou_breakegg_decision import decide_ou, derive_model_over_prob
                    # 未传 current_total 时, 尝试从 score 解析已进球数喂给模型大球概率基线
                    _ct = current_total
                    if _ct is None:
                        try:
                            _hs, _as = str(score).split("-")[:2]
                            _ct = float(int(_hs) + int(_as))
                        except Exception:
                            _ct = None
                    # 从 score 解析真实已进主/客, 对齐主端点语义(分支2 剩余期望=隐含期望-已进)
                    try:
                        _sh, _sa = (int(x) for x in str(score).split("-")[:2])
                    except Exception:
                        _sh = _sa = 0
                    # 2026-08-28: 初盘三盘 λ 交叉先验 → 即时 OU 模型 P(over) 优先用三盘分布,
                    # 无三盘数据时退回联赛泊松基线 (cross_score 已含滚球条件化+漂移验证)
                    mop = None
                    try:
                        from pipeline.cross_score import derive_score_cross
                        import sqlite3 as _sq
                        from analysis.model_match_analysis import DEFAULT_GQ_PATH
                        _cc = _sq.connect(DEFAULT_GQ_PATH, timeout=30)
                        try:
                            _cs = derive_score_cross(_cc, match_key, score, minute)
                        finally:
                            try:
                                _cc.close()
                            except Exception:
                                pass
                        if _cs and _cs.get('over_prob_at'):
                            _lk = min(_cs['over_prob_at'].keys(), key=lambda k: abs(k - (_ln or 2.5)))
                            mop = _cs['over_prob_at'].get(_lk)
                    except Exception:
                        mop = None
                    if mop is None:
                        mop = derive_model_over_prob(
                            _ln, current_home=_sh, current_away=_sa,
                            current_total=_ct, league=league)
                    drift_evidence = False
                    if _open_total is not None and _ct is not None:
                        try:
                            from analysis.live_goal_probe import anchor_gap_signal
                            ag = anchor_gap_signal(_open_total, _ct, minute, league)
                            drift_evidence = bool(ag and ag.get("overshrink"))
                        except Exception:
                            drift_evidence = False
                    ou_decision = decide_ou(
                        opening_total=_open_total, current_total=_ct,
                        ou_market={"line": _ln, "over": _ov, "under": _un},
                        minute=minute, league=league, model_over_prob=mop,
                        obscure_league=False, cross_book_evidence=False,
                        drift_evidence=drift_evidence, require_evidence=True,
                        is_halftime=is_halftime,
                    )
                except Exception as e:
                    logger.warning(f"[consensus] OU discrepancy 计算失败: {e}")
                    ou_decision = None

            return build_signal_consensus(analyze_out, operator_card, ou_decision)

        data = await asyncio.to_thread(_worker)
        return {"ok": True, "data": data}
    except Exception as e:
        logger.error(f"[live-goal-probe/consensus] 失败: {e}")
        return JSONResponse({"ok": False, "error": f"仲裁层失败: {e}", "data": None})


@app.get("/api/live-goal-probe/momentum")
async def live_goal_probe_momentum_api(
    match_key: str, score: str = "0-0", minute: int = 0, is_halftime: bool = False,
    home: str = "", away: str = "", league: str = None,
    over: float = None, under: float = None, line: float = None,
    opening_total: float = None, current_total: float = None,
    live_home: float = None, live_draw: float = None, live_away: float = None,
    ah_home: float = None, ah_away: float = None,
):
    """动态滚球决策系统 (Live Momentum Trader) 统一裁决卡端点.

    替代原 ModelAnalysisCard + ArbitrationCard 多卡平铺导致的"内容互相矛盾"体感:
    一次性聚合 决策智能体 / 操盘手 / OU破蛋 / 信号仲裁 / 回测, 输出五部分 JSON
    (市场校验 / 阶段判定 / 信号仲裁 / 动态价值 / 执行策略).

    复用 consensus 端点同款组装逻辑(analyze + operator_card + ou_decision + 自取真实 OU 赔率),
    并新增可选 live 1X2 / AH 真实盘口赔率参数(用于更严格的 AH↔1X2 市场对照).
    返回 {ok, data:{...五部分...}}; 所有建仓措辞标注"分析参考·需人工审批"(IR-20/IR-21).
    """
    if not match_key:
        return JSONResponse({"ok": False, "error": "match_key 为空", "data": None})
    match_key = _resolve_mk(match_key)
    try:
        def _worker():
            from analysis.model_match_analysis import analyze_match_with_model
            from analysis.live_momentum_trader import build_momentum_card
            h = home or (match_key.split(" vs ")[0] if " vs " in match_key else "")
            a = away or (match_key.split(" vs ")[1] if " vs " in match_key else "")
            analyze_out = analyze_match_with_model(
                match_key, current_score=score, current_minute=minute, is_halftime=is_halftime
            )
            op_raw = _live_operator_card_compute(match_key, h, a, league)
            operator_card = op_raw.get("data") if isinstance(op_raw, dict) else None

            # 2026-08-27 滚球锚: line/opening_total 未传时, 用当前滚球 OU 线+去水隐含总球作锚(不用固定数值)
            _line = line
            _open_total = opening_total
            if _line is None or _open_total is None:
                try:
                    _ll, _lt = _resolve_live_ou_anchor(match_key, minute)
                    if _line is None:
                        _line = _ll or 2.5
                    if _open_total is None:
                        _open_total = _lt
                except Exception as _e:
                    logger.warning(f"[momentum] 滚球锚解析失败: {_e}")

            # ── 复用 consensus: 自取真实 OU 赔率 + decide_ou ──
            _ov = over
            _un = under
            _ln = _line
            if (_ov is None or _un is None) and analyze_out:
                try:
                    _lu = ((analyze_out or {}).get("model_data") or {}).get("live", {}) or {}
                    _ou = _lu.get("ou") or {}
                    _o = _ou.get("over_odds")
                    _u = _ou.get("under_odds")
                    _l = _ou.get("line")
                    if _o and _u and _o > 1.0 and _u > 1.0:
                        _ov, _un = float(_o), float(_u)
                        if _l:
                            _ln = float(_l)
                except Exception as e:
                    logger.warning(f"[momentum] 自取 live OU 赔率失败: {e}")

            ou_decision = None
            if _ov is not None and _un is not None:
                try:
                    from pipeline.ou_breakegg_decision import decide_ou, derive_model_over_prob
                    _ct = current_total
                    if _ct is None:
                        try:
                            _hs, _as = str(score).split("-")[:2]
                            _ct = float(int(_hs) + int(_as))
                        except Exception:
                            _ct = None
                    try:
                        _sh, _sa = (int(x) for x in str(score).split("-")[:2])
                    except Exception:
                        _sh = _sa = 0
                    # 2026-08-28: 初盘三盘 λ 交叉先验 → 即时 OU 模型 P(over) 优先用三盘分布,
                    # 无三盘数据时退回联赛泊松基线 (cross_score 已含滚球条件化+漂移验证)
                    mop = None
                    try:
                        from pipeline.cross_score import derive_score_cross
                        import sqlite3 as _sq
                        from analysis.model_match_analysis import DEFAULT_GQ_PATH
                        _cc = _sq.connect(DEFAULT_GQ_PATH, timeout=30)
                        try:
                            _cs = derive_score_cross(_cc, match_key, score, minute)
                        finally:
                            try:
                                _cc.close()
                            except Exception:
                                pass
                        if _cs and _cs.get('over_prob_at'):
                            _lk = min(_cs['over_prob_at'].keys(), key=lambda k: abs(k - (_ln or 2.5)))
                            mop = _cs['over_prob_at'].get(_lk)
                    except Exception:
                        mop = None
                    if mop is None:
                        mop = derive_model_over_prob(
                            _ln, current_home=_sh, current_away=_sa,
                            current_total=_ct, league=league)
                    drift_evidence = False
                    if _open_total is not None and _ct is not None:
                        try:
                            from analysis.live_goal_probe import anchor_gap_signal
                            ag = anchor_gap_signal(_open_total, _ct, minute, league)
                            drift_evidence = bool(ag and ag.get("overshrink"))
                        except Exception:
                            drift_evidence = False
                    ou_decision = decide_ou(
                        opening_total=_open_total, current_total=_ct,
                        ou_market={"line": _ln, "over": _ov, "under": _un},
                        minute=minute, league=league, model_over_prob=mop,
                        obscure_league=False, cross_book_evidence=False,
                        drift_evidence=drift_evidence, require_evidence=True,
                        is_halftime=is_halftime,
                    )
                except Exception as e:
                    logger.warning(f"[momentum] OU 决策计算失败: {e}")
                    ou_decision = None

            # ── 真实盘口赔率 (用于 AH↔1X2 市场对照) ──
            # 参数显式传入优先; 未传则自取 live 1X2 / AH 真实赔率 (诚实边界: 仅真实市场赔率)
            live_1x2_odds = None
            if (live_home and live_draw and live_away
                    and live_home > 1.0 and live_draw > 1.0 and live_away > 1.0):
                live_1x2_odds = {"home": live_home, "draw": live_draw, "away": live_away}
            live_ah_odds = None
            if ah_home and ah_away and ah_home > 1.0 and ah_away > 1.0:
                live_ah_odds = {"home": ah_home, "away": ah_away}
            if live_1x2_odds is None or live_ah_odds is None:
                try:
                    import sqlite3
                    from analysis.model_match_analysis import DEFAULT_GQ_PATH
                    from analysis.live_goal_probe import _current_inplay_odds, _current_inplay_ah_odds
                    _con = sqlite3.connect(DEFAULT_GQ_PATH, timeout=30)
                    try:
                        if live_1x2_odds is None:
                            _cur = _current_inplay_odds(_con, match_key, minute)
                            if _cur and _cur.get("x2"):
                                _h, _d, _a = _cur["x2"]
                                live_1x2_odds = {"home": float(_h), "draw": float(_d), "away": float(_a)}
                        if live_ah_odds is None:
                            _ah = _current_inplay_ah_odds(_con, match_key, minute)
                            if _ah:
                                live_ah_odds = {"home": float(_ah[1]), "away": float(_ah[2])}
                    finally:
                        _con.close()
                except Exception as e:
                    logger.warning(f"[momentum] 自取 live 1X2/AH 赔率失败: {e}")

            # ── 组装五部分卡 ──
            return build_momentum_card(
                analyze_out, operator_card=operator_card, ou_decision=ou_decision,
                minute=minute, is_halftime=is_halftime, league=league,
                live_1x2_odds=live_1x2_odds,
                live_ou_odds=({"over": _ov, "under": _un, "line": _ln}
                              if (_ov and _un) else None),
                live_ah_odds=live_ah_odds, opening_total=opening_total,
            )

        data = await asyncio.wait_for(asyncio.to_thread(_worker), timeout=25)
        return {"ok": True, "data": data}
    except asyncio.TimeoutError:
        logger.error(f"[live-goal-probe/momentum] {match_key} 聚合超时>25s, 降级返回空")
        return {"ok": False, "error": "动态滚球决策卡聚合超时(该场数据不足, 已降级)", "data": None}
    except Exception as e:
        logger.error(f"[live-goal-probe/momentum] 失败: {e}")
        return JSONResponse({"ok": False, "error": f"动态滚球决策卡失败: {e}", "data": None})


@app.get("/api/match/ht-draw-structure")
async def ht_draw_structure_api(match_key: str):
    """上半场平局赔率结构诊断: 消费 1X2_1H draw + OU_1H 真实赔率时间序列,
    输出开盘→临场→HT临界→最新 四档 + 漂移曲线 + 与全场平局关系 + OU_1H 辅助.

    半场 1X2 draw 走低 = 庄家越来越确信上半场比分持平(典型 0-0 僵局).
    """
    if not match_key:
        return JSONResponse({"ok": False, "error": "match_key 为空", "data": None})
    match_key = _resolve_mk(match_key)
    try:
        def _worker():
            import sqlite3
            from analysis.model_match_analysis import DEFAULT_GQ_PATH
            from analysis.ht_draw_odds_structure import ht_draw_odds_diagnosis
            con = sqlite3.connect(DEFAULT_GQ_PATH, timeout=30)
            try:
                return ht_draw_odds_diagnosis(con, match_key)
            finally:
                con.close()
        data = await asyncio.to_thread(_worker)
        return {"ok": True, "data": data}
    except Exception as e:
        logger.error(f"[ht-draw-structure] 失败: {e}")
        return JSONResponse({"ok": False, "error": f"半场平局结构失败: {e}", "data": None})


@app.get("/api/live-goal-probe/backtest")
async def live_goal_probe_backtest_api(force: int = 0):
    """滚球破蛋模型回测摘要(历史数据验证 + 风险披露)。

    返回: {ok, data: {method, n_matches_with_odds, half{}, full{}, half_ge1_calibration{}}}
    force=1 时重算, 否则读取已生成的 JSON 缓存。
    """
    import os as _os
    _json_path = _os.path.join(_os.path.dirname(__file__), "analysis", "live_goal_probe_backtest.json")
    if force and _LIVE_GOAL_OK and _backtest_live_goal_probe is not None:
        try:
            summary = await asyncio.to_thread(_backtest_live_goal_probe)
            return {"ok": True, "data": summary, "recomputed": True}
        except Exception as _e:
            logger.error(f"[live-goal-probe/backtest] 重算失败: {_e}")
    if _os.path.exists(_json_path):
        try:
            with open(_json_path, encoding="utf-8") as _f:
                return {"ok": True, "data": json.load(_f), "recomputed": False}
        except Exception as _e:
            logger.error(f"[live-goal-probe/backtest] 读缓存失败: {_e}")
    return JSONResponse({"ok": False, "error": "回测数据不可用(请先运行 analysis/backtest_live_goal_probe.py)", "data": None})


@app.get("/api/match-minute-stream")
async def match_minute_stream_api(match_key: str, line: float = None, league: str = None,
                                   opening_total: float = None, estimate: int = 1):
    """分钟级数据流: 重建指定比赛的盘口+比分时间线、进球事件、逐分钟剩余破蛋曲线。

    2026-08-21 修复: 默认 estimate=1, 对缺少真实 minute_at 的旧数据用 kickoff+captured_at
    估算分钟并明确标注 estimated; 真实分钟数据存在时仍优先使用真实数据。

    参数:
      match_key:      比赛键 (如 "主队 vs 客队")
      line:           OU 线 (默认 2.5)
      league:         联赛 (可选, 用于泊松学派先验)
      opening_total:  开盘隐含总球 (可选; 不传则自动从赛前 OU 反推)
      estimate:       是否允许分钟估算 (1=是, 0=否; 默认 1)

    返回: {ok, data: {match_key, opening_total, n_snapshots_minute, timeline[], goal_events[],
                     remaining_break_curve[], has_real_minute_data, has_estimated_minute_data,
                     data_quality, reason, status}}
    """
    if not _MINUTE_STREAM_OK:
        return JSONResponse({"ok": False, "error": "分钟级数据流模块未加载", "data": None})
    try:
        import sqlite3 as _sq
        def _worker():
            # 2026-08-27 滚球锚: line/opening_total 未传时用当前滚球 OU 线+隐含总球(不用固定数值)
            _line = line
            _open_total = opening_total
            if _line is None or _open_total is None:
                try:
                    _ll, _lt = _resolve_live_ou_anchor(match_key, 0)
                    if _line is None:
                        _line = _ll or 2.5
                    if _open_total is None:
                        _open_total = _lt
                except Exception as _e:
                    logger.warning(f"[minute-stream] 滚球锚解析失败: {_e}")
            _db_path = os.path.join(PROJECT_ROOT, "data", "events.db")
            _db = _sq.connect(_db_path, timeout=30)
            try:
                    return _get_match_minute_stream(
                    _db, match_key,
                    opening_total=_open_total, line=_line, league=league,
                    estimate=bool(estimate))
            finally:
                _db.close()
        out = await asyncio.to_thread(_worker)
        return {"ok": True, "data": out}
    except Exception as _e:
        logger.error(f"[match-minute-stream] 失败: {_e}")
        return JSONResponse({"ok": False, "error": f"分钟级数据流失败: {_e}", "data": None})


@app.get("/api/live-goal-probe/decision")
async def live_goal_probe_decision_api(match_key: str, score: str = "0-0", minute: int = 0,
                                        opening_total: float = None, current_total: float = None,
                                        league: str = None, line: float = None,
                                        over: float = None, under: float = None,
                                        obscure_league: bool = False,
                                        cross_book: bool = False, require_evidence: bool = True,
                                        model_over_prob: float = None, is_halftime: bool = False):
    """滚球 OU 破蛋决策(带护栏): 在破蛋概率仪之上补 devig + 证据闸门 + obscure + 半凯利。
    模型大球概率优先级: 调用方传 model_over_prob > probe_match 1X2拟合期望 >
                        市场隐含剩余(current_total-已进) > 联赛泊松基线。
    漂移证据取自已加载的 analysis.live_goal_probe.anchor_gap_signal (开盘→活盘过度收缩)。
    返回 {ok, data:{line, over:{verdict,fair_prob,implied_prob,divergence,stake,kelly},
                     under:{...}, bettable, evidence, obscure_league, n_bet, model_over_prob_used}}。
    """
    from pipeline.ou_breakegg_decision import decide_ou, derive_model_over_prob
    if not _LIVE_GOAL_OK:
        return JSONResponse({"ok": False, "error": "滚球破蛋模块未加载", "data": None})
    match_key = _resolve_mk(match_key)
    try:
        if over is None or under is None:
            return JSONResponse({"ok": False, "error": "需提供 over/under 赔率", "data": None})

        def _worker():
            # 2026-08-27 滚球锚: line/opening_total 未传时用当前滚球 OU 线+隐含总球(不用固定数值)
            _line = line
            _open_total = opening_total
            if _line is None or _open_total is None:
                try:
                    _ll, _lt = _resolve_live_ou_anchor(match_key, minute)
                    if _line is None:
                        _line = _ll or 2.5
                    if _open_total is None:
                        _open_total = _lt
                except Exception as _e:
                    logger.warning(f"[decision] 滚球锚解析失败: {_e}")
            # 漂移证据: 开盘→活盘过度收缩 (anchor_gap_signal overshrink)
            drift_evidence = False
            if _open_total is not None and current_total is not None:
                try:
                    from analysis.live_goal_probe import anchor_gap_signal
                    ag = anchor_gap_signal(_open_total, current_total, minute, league)
                    drift_evidence = bool(ag and ag.get("overshrink"))
                except Exception:
                    drift_evidence = False
            # 模型大球概率: 优先 probe_match 1X2 拟合期望, 回退市场隐含剩余/联赛泊松
            mop = model_over_prob
            pr = None
            if mop is None:
                try:
                    sh, sa = (int(x) for x in str(score).split("-"))
                except Exception:
                    sh, sa = 0, 0
                eh = ea = None
                try:
                    if _live_goal_probe is not None:
                        pr = _live_goal_probe(match_key, current_score=score,
                                             current_minute=minute, league=league,
                                             is_halftime=is_halftime)
                        eh = pr.get("expected_home_goals")
                        ea = pr.get("expected_away_goals")
                except Exception:
                    eh = ea = None
                mop = derive_model_over_prob(
                    _line, current_home=sh, current_away=sa,
                    expected_home=eh, expected_away=ea,
                    current_total=current_total, league=league)
            res = decide_ou(
                opening_total=_open_total, current_total=current_total,
                ou_market={"line": _line, "over": over, "under": under},
                minute=minute, league=league, model_over_prob=mop,
                obscure_league=obscure_league, cross_book_evidence=cross_book,
                drift_evidence=drift_evidence, require_evidence=require_evidence,
                is_halftime=is_halftime,
            )
            res["match_key"] = match_key
            res["score"] = score
            res["model_over_prob_used"] = round(mop, 4) if mop is not None else None
            res["probe_summary"] = (pr.get("summary") if pr else None)
            return res

        result = await asyncio.to_thread(_worker)
        return {"ok": True, "data": result}
    except Exception as e:
        logger.error(f"[live-goal-probe/decision] 失败: {e}")
        return JSONResponse({"ok": False, "error": f"决策失败: {e}", "data": None})


@app.post("/api/focus")
async def register_focus_api(req: Request):
    """滚球破蛋神器 → 采集器: 注册当前可见比赛的 match_key 列表为秒级焦点。

    请求体: {"match_keys": ["主队 vs 客队", ...], "ttl_seconds": 300}
    写入 gq/focus_matches.json (覆盖写, 与采集器 --focus-file 契约一致); 目录不存在则创建.
    返回: {"success": true, "count": N}

    采集器每 30s 读取该文件, 把 match_keys 合并进 focus 并秒级(默认3s)轮询赔率快照,
    从而让前端左侧列表从 45-60s 刷新提速到 3-5s (全量轮仍按 -i 间隔, 不被拖慢).
    失败静默不抛: 异常返回 success=False, 不中断前端.
    """
    try:
        try:
            body = await req.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        match_keys = body.get("match_keys") or []
        ttl_seconds = int(body.get("ttl_seconds", 300) or 300)
        if not isinstance(match_keys, list):
            match_keys = []
        # 去空白/去重, 限制数量与单键长度, 防放大式 DoS / 磁盘写满
        seen = set()
        cleaned = []
        for k in match_keys[:100]:
            s = str(k).strip()
            if not s or s in seen or len(s) > 200:
                continue
            seen.add(s)
            cleaned.append(s)
            if len(cleaned) >= 50:
                break
        # ttl 兜底并限制在合理范围 (1min ~ 24h)
        try:
            ttl_seconds = int(ttl_seconds)
        except Exception:
            ttl_seconds = 300
        ttl_seconds = max(300, min(86400, ttl_seconds))
        focus_path = os.path.join(PROJECT_ROOT, "gq", "focus_matches.json")
        focus_dir = os.path.dirname(focus_path)
        os.makedirs(focus_dir, exist_ok=True)
        with open(focus_path, "w", encoding="utf-8") as f:
            json.dump({"match_keys": cleaned, "ttl_seconds": ttl_seconds},
                      f, ensure_ascii=False, indent=2)
        return JSONResponse({"success": True, "count": len(cleaned)})
    except Exception as e:
        logger.error(f"[api/focus] register failed: {e}")
        return JSONResponse(status_code=500, content={"success": False, "error": "focus register failed"})


@app.get("/api/template-deviation")
async def template_deviation_api(limit: int = 300, risk_level: str = "ALL"):
    """模板偏差实时扫描 — 赛事风险评级。

    返回每场当前比赛的模板弱区风险评分（L1赛事分类 + L3盘口异常），
    供前端 AnalysisCenter 做 risk 映射标注。
    """
    if not _TEMPLATE_DEV_OK:
        return JSONResponse(content=json.dumps({"matches": [], "total": 0, "error": "模板偏差模块未加载"}),
                            media_type="application/json")
    try:
        result = _template_scan(limit=limit, risk_level=risk_level)
        return {"matches": result.get("matches", []), "total": result.get("count", 0)}
    except Exception as e:
        logger.error(f"[template_deviation] 失败: {e}")
        return JSONResponse(content=json.dumps({"matches": [], "total": 0, "error": str(e)}),
                            media_type="application/json")


# ═══ 联赛赛程 (数据源: 微瑞/乐鱼 live feed) ═══
# 全局 feed 缓存: league_name -> [FixtureEntry], TTL 60s
_LEISU_FEED_CACHE: Dict[str, Any] = {"fetched_at": 0.0, "data": None}
_LEISU_FEED_LOCK = None
LEISU_FEED_TTL = int(os.getenv("LEISU_FEED_TTL", "60"))


async def _get_leisu_feed() -> Dict[str, Any]:
    """取/刷新实时比赛 feed (stale-while-revalidate: TTL 缓存 + 后台异步刷新, 请求路径永阻塞)。

    2026-08-27: 雷速已删除, 主源 = events.db 采集器数据(ws_collector 秒级推流落库).
    实现同旧(缓存未过期→直接返回; 过期有旧值→立即返回旧值+后台刷新; 仅冷启动同步等一次).
    """
    global _LEISU_FEED_CACHE, _LEISU_FEED_LOCK
    if _LEISU_FEED_LOCK is None:
        _LEISU_FEED_LOCK = asyncio.Lock()
    now = time.time()
    cached = _LEISU_FEED_CACHE.get("data")
    if cached and (now - _LEISU_FEED_CACHE["fetched_at"]) < LEISU_FEED_TTL:
        return cached
    # 冷启动(无任何缓存): 只能同步等待这一次
    if cached is None:
        return await _refresh_leisu_feed()
    # 过期但有旧缓存: 立即返回旧值, 后台触发刷新(请求零等待)
    if not _LEISU_FEED_LOCK.locked():
        asyncio.create_task(_refresh_leisu_feed())
    return cached


async def _refresh_leisu_feed() -> Dict[str, Any]:
    """后台/冷启动刷新 feed (2026-08-27 已删除雷速: 主源切 events.db 采集器数据).
    单飞锁防并发重复构建, 双重检查避免重复工作."""
    global _LEISU_FEED_CACHE, _LEISU_FEED_LOCK
    async with _LEISU_FEED_LOCK:
        now = time.time()
        c = _LEISU_FEED_CACHE.get("data")
        if c and (now - _LEISU_FEED_CACHE["fetched_at"]) < LEISU_FEED_TTL:
            return c  # 已被别的任务刷新好, 直接复用
        # 2026-08-27: 雷速已删除, 统一走 events.db 采集器数据(ws_collector 秒级推流 + HTTP 列表)
        data = await asyncio.to_thread(_gq_build_feed)
        _LEISU_FEED_CACHE = {"fetched_at": time.time(), "data": data}
        return data


# ── events.db 回退源 (当微瑞 feed 不可用/为空时, 用采集器已落库的比赛兜底) ──
# 铁律: 采集器持续写 events.db, 即便微瑞 feed/乐鱼 timeline 全部挂掉, 这里仍有真实的在跑比赛。
# 构造与微瑞 feed 同构的 {"leagues": {league: [fixture_dict, ...]}}, 让 leagues/fixtures 两个端点无缝复用下游逻辑。
_GQ_FB_CACHE = None
_GQ_FB_TIME = 0
def _gq_feed_fallback() -> Dict[str, Any]:
    """带 60s 缓存的 events.db 兜底 feed。返回 {leagues:{...}} 或 {error, leagues:{}}。"""
    global _GQ_FB_CACHE, _GQ_FB_TIME
    import time as _t
    now = _t.time()
    if _GQ_FB_CACHE is not None and (now - _GQ_FB_TIME) < 15:
        return _GQ_FB_CACHE
    data = _gq_build_feed()
    _GQ_FB_CACHE = data
    _GQ_FB_TIME = now
    return data

def _gq_build_feed() -> Dict[str, Any]:
    import sqlite3
    import time as _t
    try:
        db = os.path.join(PROJECT_ROOT, "data", "events.db")
        if not os.path.exists(db):
            return {"error": "events.db 不存在", "leagues": {}}
        c = sqlite3.connect(db); c.row_factory = sqlite3.Row
        rows = c.execute("""
            SELECT m.match_key, m.home, m.away, m.league, m.kickoff, m.status,
                   m.score_home, m.score_away, m.minute, m.mid, m.last_seen,
                   o.mid AS outcome_mid
            FROM matches m
            LEFT JOIN match_outcomes o ON m.mid = o.mid
            WHERE m.league NOT LIKE 'VS-%'   -- 排除电竞模拟盘
              AND m.league NOT LIKE '%瓦尔哈拉%'  -- 8分钟虚拟杯
              AND m.league NOT LIKE '%瓦尔基里%'
              AND m.league NOT LIKE '%梦幻对垒%'
              AND m.league NOT LIKE '%8分钟%'
          -- 僵尸清理(P0): status='live' 且 last_seen 超 3h 未更新 且 开赛已超 6h
          -- (采集器每180s刷一次, 真进行中必在3min内; 但采集器短暂掉线时, 开赛才几小时的真比赛
          --  last_seen 也会滞后, 不应误杀) → 视为卡盘不进入实时feed; 避免07-18起的1437场老僵尸淹没。
          -- 开赛 6h 内的 live 比赛一律保留(交由前端按时间兜底判进行中/已结束), 仅"开赛很久+滞后"才判僵尸。
          AND (m.status != 'live' OR m.last_seen >= strftime('%s','now','-3 hours') OR m.kickoff >= datetime('now','-6 hours'))
            ORDER BY m.last_seen DESC
        """).fetchall()
        leagues: Dict[str, list] = {}
        for r in rows:
            lg = (r["league"] or "其他").strip()
            if not lg:
                continue
            st = r["status"]
            # 僵尸二次判定: 标 live 但 match_outcomes 已有终场比分 → 实际已结束, 不显示进行中
            is_zombie = False
            if st == "live":
                now_ts = _t.time()
                ls = float(r["last_seen"]) if r["last_seen"] else now_ts
                age_min = (now_ts - ls) / 60
                # 开赛距今(分钟): 开赛 6h 内的 live 比赛即便 last_seen 滞后, 也大概率是真进行中/
                # 刚结束(采集器短暂掉线), 不应误判僵尸; 只有"开赛很久(>6h) 且 last_seen 滞后"才是卡盘。
                ko_raw = (r["kickoff"] or "").strip()
                ko = ko_raw.replace(" ", "T")
                ko_ts = None
                if ko:
                    # 兼容 'YYYY-MM-DD HH:MM' 与 'YYYY-MM-DD HH:MM:SS'
                    iso = ko + ":00" if ko.count(":") == 1 else ko
                    try:
                        ko_ts = _t.mktime(_t.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S"))
                    except Exception:
                        try:
                            ko_ts = float(ko)
                        except Exception:
                            ko_ts = None
                ko_age_min = (now_ts - ko_ts) / 60 if ko_ts else 9999
                recent_kickoff = ko_age_min <= 360
                minute = r["minute"]
                # 仅对"开赛很久"的比赛做 last_seen 僵尸判定, 避免误杀采集器短暂掉线导致的滞后场
                if not recent_kickoff and age_min > 60:
                    is_zombie = True
                # 半场/全场节点停滞过久 → 已结束(常规比赛半场≤20min, 90'+≤15min).
                # 不依赖 recent_kickoff: 即便开赛不久, 45' 节点卡>30min 也必定是数据脏/已结束。
                if not is_zombie and minute in (45, 90) and age_min > 30:
                    is_zombie = True
                if not is_zombie and minute in (46, 47, 48, 49, 50) and age_min > 45:
                    is_zombie = True
                # 兜底: 按开赛时间推断, 45' 已开赛超过 75min / 90' 超过 105min 必已结束
                # (规避 obscure 联赛 minute 字段长期不更新但 last_seen 仍刷新的脏数据)
                if not is_zombie and ko_ts:
                    elapsed_min = (now_ts - ko_ts) / 60
                    if minute == 45 and elapsed_min > 75:
                        is_zombie = True
                    elif minute == 90 and elapsed_min > 105:
                        is_zombie = True
            ko = (r["kickoff"] or "").strip()
            iso = ""
            ko_ts = None
            if ko:
                # 'YYYY-MM-DD HH:MM' -> 'YYYY-MM-DDTHH:MM:00' (Safari 兼容, 供 new Date 解析)
                iso = ko.replace(" ", "T") + ":00" if " " in ko else ko
                try:
                    ko_ts = _t.mktime(_t.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S"))
                except Exception:
                    ko_ts = None
            # 关键修正: 开赛时间在未来 → 不可能是进行中/已结束 (GQ 偶发把未来赛程标成 live+minute=45)
            # 强制视为未开赛, 避免全部显示为"中场休息"的乌龙。
            is_upcoming = ko_ts is not None and ko_ts > _t.time() + 60
            if is_upcoming:
                ms = 0
            elif st == "live" and r["outcome_mid"]:
                ms = -1
            elif st == "live" and is_zombie:
                ms = -1
            else:
                ms = -1 if st == "finished" else 1 if st == "live" else 0
            # 修正 GQ 脏 minute: 标 45' 但开赛还不到 45min (或未来) → 不可能是中场休息,
            # 清空 minute 让前端按 elapsed 显示为上半场; 同理 90' 但未到 90min。
            minute = r["minute"]
            if ms == 1 and ko_ts and minute in (45, 90):
                elapsed_min = (_t.time() - ko_ts) / 60
                if (minute == 45 and elapsed_min < 45) or (minute == 90 and elapsed_min < 90):
                    minute = None
            fx = {
                "id": r["match_key"] or r["mid"] or f"{r['home']}|{r['away']}",
                "match_key": r["match_key"],
                "home": r["home"], "away": r["away"],
                "league": lg, "sport_key": lg,
                "commence_time": iso,
                "match_state": ms,
                "score_home": r["score_home"] if not is_upcoming else None,
                "score_away": r["score_away"] if not is_upcoming else None,
                "match_minute": "" if is_upcoming else (minute if minute else ""),
                "mid": r["mid"],
            }
            leagues.setdefault(lg, []).append(fx)
        c.close()
        if not leagues:
            return {"error": "events.db 暂无比赛", "leagues": {}}
        return {"leagues": leagues}
    except Exception as e:
        return {"error": f"GQ 回退构建失败: {e}", "leagues": {}}

def _attach_gq_1x2(fixtures: list) -> list:
    """对缺 1X2 赔率的 fixture, 从 events.db odds_snapshots(market='1X2') 按最新快照补齐。
    兼容 match_key 或 mid 任一字段作为盘口关联键(防止调用方只填 mid 导致全盘口静默丢失)。"""
    # 统一关联键: 优先 match_key, 回退 mid (live_scores 注入段曾只用 mid)
    for f in fixtures:
        if not f.get("match_key") and f.get("mid"):
            f["match_key"] = f.get("mid")
    need = [f for f in fixtures if f.get("odds_h") in (None, "") and f.get("match_key")]
    if not need:
        return fixtures
    try:
        import sqlite3
        db = os.path.join(PROJECT_ROOT, "data", "events.db")
        keys = [f["match_key"] for f in need]
        c = sqlite3.connect(db)
        q = (f"SELECT match_key, selection, odds FROM odds_snapshots "
             f"WHERE match_key IN ({','.join('?'*len(keys))}) AND market='1X2' "
             f"ORDER BY captured_at DESC")
        rows = c.execute(q, keys).fetchall(); c.close()
        latest: Dict[tuple, float] = {}
        for mk, sel, odds in rows:
            latest.setdefault((mk, sel), odds)
        for f in need:
            h = latest.get((f["match_key"], "home"))
            d = latest.get((f["match_key"], "draw"))
            a = latest.get((f["match_key"], "away"))
            if h is not None and d is not None and a is not None:
                f["odds_h"], f["odds_d"], f["odds_a"] = h, d, a
        # 初盘 (固定, 永不漂移)
        opening_map = _gq_opening_odds(keys)
        for f in need:
            op = opening_map.get(f["match_key"])
            if op:
                f["opening_h"], f["opening_d"], f["opening_a"] = op["h"], op["d"], op["a"]
        # AH/OU 赔率 (让球/大小球, GQ 多线取最佳线) — 所有 fixture 皆补
        all_keys = [f["match_key"] for f in fixtures if f.get("match_key")]
        if all_keys:
            ah_ou_map = _gq_best_ah_ou(all_keys)
            for f in fixtures:
                ao = ah_ou_map.get(f.get("match_key"))
                if ao:
                    for k in ("ah_line","ah_home","ah_away","ah_op_home","ah_op_away",
                              "ou_line","ou_over","ou_under","ou_op_over","ou_op_under"):
                        if k in ao:
                            f[k] = ao[k]
    except Exception:
        pass
    return fixtures


@app.get("/api/match-results")
async def match_results_api(league: str = "", q: str = "",
                            date_from: str = "", date_to: str = "",
                            limit: int = 50):
    """赛果查询 — 已完赛比分 (含 WC2026)。

    数据源 data/football_data.db 两表 UNION:
      - matches: 近期(含WC2026), 英文队名 + 半场比分, status='finished'
      - historical_matches: 深历史(2012-2025), 中文队名 + 开/收盘赔率
    过滤: league(模糊) / q(队名模糊) / date_from~date_to, 按日期倒序。
    """
    import sqlite3
    try:
        db_path = os.path.join(PROJECT_ROOT, "data", "football_data.db")
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        # 两表列名不同: matches=home_team_name, historical_matches=home_team; matches 有 status, hist 无
        # 分别构建 WHERE 子句 + 别名统一
        def build_where(team_col_h, team_col_a, has_status):
            clauses, params = [], []
            if q:
                clauses.append(f"(LOWER({team_col_h}) LIKE ? OR LOWER({team_col_a}) LIKE ?)")
                params += [f"%{q.lower()}%", f"%{q.lower()}%"]
            if league:
                clauses.append("LOWER(league_name) LIKE ?")
                params.append(f"%{league.lower()}%")
            if date_from:
                clauses.append("match_date >= ?")
                params.append(date_from)
            if date_to:
                clauses.append("match_date <= ?")
                params.append(date_to)
            prefix = " WHERE home_score IS NOT NULL" + (" AND status='finished'" if has_status else "")
            return prefix + (" AND " + " AND ".join(clauses) if clauses else ""), params
        where1, params1 = build_where("home_team_name", "away_team_name", True)
        where2, params2 = build_where("home_team", "away_team", False)
        # matches (近期, 含WC2026, 英文队名 + 半场)
        sql1 = (f"SELECT home_team_name AS home, away_team_name AS away, league_name AS league, "
                f"match_date AS date, home_score, away_score, final_result AS result, "
                f"halftime_home AS ht_h, halftime_away AS ht_a, 'recent' AS source FROM matches{where1}")
        # historical_matches (深历史, 中文队名)
        sql2 = ("SELECT home_team AS home, away_team AS away, league_name AS league, "
                "match_date AS date, home_score, away_score, final_result AS result, "
                "NULL AS ht_h, NULL AS ht_a, 'historical' AS source FROM historical_matches"
                + where2)
        # SQLite 不支持直接 (q1) UNION (q2) ORDER BY ... LIMIT — 包一层子查询
        sql = f"SELECT * FROM ({sql1} UNION ALL {sql2}) ORDER BY date DESC LIMIT ?"
        params_final = params1 + params2 + [min(max(limit, 1), 200)]
        rows = [dict(r) for r in cur.execute(sql, params_final).fetchall()]
        conn.close()
        return _wrap_data({"results": rows, "total": len(rows)})
    except Exception as e:
        return _wrap_data({"error": f"赛果查询失败: {e}", "results": []})


# ── GQ 覆盖联赛集 (用于前端只显示有赔率覆盖的联赛/比赛) ──
_GQ_COVERED_CACHE = None
_GQ_COVERED_TIME = 0
def _norm_league(s):
    """归一化联赛名: 去 VS-/PANDA/EAFC/独家/空格/括号/联赛杯赛后缀, 便于跨源匹配."""
    if not s:
        return ""
    import re
    s = s.replace("VS-", "").replace("PANDA", "").replace("EAFC", "").replace("独家", "")
    s = re.sub(r"[（）()\s]", "", s)
    s = s.replace("联赛", "").replace("杯", "").replace("赛", "")
    return s.lower()
def _gq_covered_leagues():
    """返回 events.db 已采集(有赔率)的真实联赛归一化集合 (缓存 300s). 空=采集器未覆盖任何→安全返回空."""
    global _GQ_COVERED_CACHE, _GQ_COVERED_TIME
    import time as _t
    now = _t.time()
    if _GQ_COVERED_CACHE is not None and (now - _GQ_COVERED_TIME) < 300:
        return _GQ_COVERED_CACHE
    try:
        import sqlite3
        c = sqlite3.connect(os.path.join(PROJECT_ROOT, "data", "events.db"))
        rows = c.execute("SELECT DISTINCT league FROM matches WHERE league NOT LIKE 'VS-%'").fetchall()
        s = set()
        for (lg,) in rows:
            if lg:
                s.add(_norm_league(lg))
        c.close()
        _GQ_COVERED_CACHE = s
        _GQ_COVERED_TIME = now
        return s
    except Exception:
        return set()
def _is_gq_covered(league):
    """该联赛是否被 GQ 采集器覆盖(有赔率). 失败开放: 未知/覆盖集空→视为覆盖(不隐藏), 避免误杀."""
    nl = _norm_league(league)
    if not nl:
        return True
    cov = _gq_covered_leagues()
    if not cov:
        return True
    if nl in cov:
        return True
    for cl in cov:
        if nl and cl and (nl in cl or cl in nl):
            return True
    return False

def _fixture_should_show(f):
    """比赛是否应在赛程页展示.

    展示条件(满足任一即展示):
      1. 有可分析赔率 (odds_h 为 >0 的数值) — 未开赛/进行中均可能;
      2. 已结束/盘口关闭 (match_state<0 或 已有比分) — 庄家因一方已不可能赢而封盘,
         这是正常市场行为, 不是数据缺失, 必须展示(带最终赛果).

    仅当 既无赔率 又 未结束(无赛果) 时才隐藏 = 真正的未覆盖联赛/无盘口。

    铁律(用户 2026-07-20 纠正): 盘口关闭 ≠ 没采集到数据, 而是比赛已分胜负、庄家封盘。
    绝不能把已结束比赛按"无赔率"误杀隐藏。
    """
    # 1) 有赔率
    try:
        h = f.get("odds_h")
        if h is not None:
            try:
                if float(h) > 0:
                    return True
            except (TypeError, ValueError):
                pass
    except Exception:
        pass
    # 2) 已结束 / 封盘 (有赛果即可展示, 盘口关闭是正常市场行为)
    st = f.get("match_state")
    if st is not None:
        try:
            if isinstance(st, (int, float)) and st < 0:
                return True
        except Exception:
            pass
    sh, sa = f.get("score_home"), f.get("score_away")
    if sh is not None and sa is not None:
        try:
            if str(sh).strip() != "" and str(sa).strip() != "":
                return True
        except Exception:
            pass
    return False

@app.get("/api/leagues")
async def leagues_api(days: int = 7):
    """联赛目录 (动态, 来自微瑞 live feed / events.db 回退; 按联赛名分组)。

    days=N: 只保留近 N 天内有赛程的联赛, fixture_count 改为近 N 天的场数(0=全部).
    """
    feed = await _get_leisu_feed()
    if feed.get("error") or not feed.get("leagues"):
        feed = await asyncio.to_thread(_gq_feed_fallback)
    if feed.get("error"):
        return _wrap_data({"error": feed["error"], "categories": [], "total_leagues": 0})
    leagues = feed["leagues"]
    # days>0: 按 commence_time 过滤 (近 N 天 GMT+8, 含今天 + 昨天)
    if days and days > 0:
        from datetime import datetime, timedelta, timezone
        # GMT+8 当天 + 前 (days-1) 天 = [today-(days-1), today+1)
        tz8 = timezone(timedelta(hours=8))
        today8 = datetime.now(tz8).date()
        cutoff = today8 - timedelta(days=days - 1)  # 含 today 和 days-1 天前
        cutoff_iso = cutoff.isoformat()
        cutoff_ms = datetime.combine(cutoff, datetime.min.time(), tzinfo=tz8).timestamp() * 1000
        def _recent_fx(fx_list):
            keep = []
            for f in fx_list:
                ct = f.get("commence_time") or ""
                # 优先用 ISO 字符串前缀比 (YYYY-MM-DD), 兼容两种格式
                if isinstance(ct, str) and len(ct) >= 10 and ct[:10] >= cutoff_iso:
                    keep.append(f)
                else:
                    # 退化: 用 epoch_ms 字段
                    ms = f.get("commence_time_ms") or f.get("kickoff_ms")
                    if ms is not None and ms >= cutoff_ms:
                        keep.append(f)
            return keep
        filtered = {n: _recent_fx(fx) for n, fx in leagues.items()}
        leagues = {n: fx for n, fx in filtered.items() if fx}  # 剔除近 N 天无场次的联赛
    entries = [
        {"sport_key": name, "name": name, "available": True, "fixture_count": len(fx)}
        for name, fx in leagues.items() if is_football(name)
    ]
    entries.sort(key=lambda e: -e["fixture_count"])
    categories = [{"category": "实时赛事", "leagues": entries}]
    return _wrap_data({"categories": categories, "total_leagues": len(entries), "days_filter": days})


# ── 赔率快照辅助 (Req2: 初始快照捕获 + 漂移计算) ──
def _capture_initial_odds(fx: dict) -> dict:
    """首次见到某场比赛时记录开盘赔率, 返回 {initial, drift} 供前端展示。"""
    from time import time as _time
    key = f"{fx.get('home','')}|{fx.get('away','')}|{fx.get('commence_time','')}"
    now = _time()
    # 提取当前有效赔率字段
    current = {
        k: fx.get(k) for k in (
            "odds_h", "odds_d", "odds_a",
            "ah_line", "ah_home", "ah_away",
            "ou_line", "ou_over", "ou_under",
        ) if fx.get(k) is not None
    }
    # 首次出现 → 存为初始快照
    if key not in _INITIAL_ODDS_SNAPSHOT and current:
        _INITIAL_ODDS_SNAPSHOT[key] = {
            **current,
            "_snapshot_at": now,
            "_match_key": key,
        }
    # 计算漂移 (current - initial)
    snap = _INITIAL_ODDS_SNAPSHOT.get(key)
    drift = {}
    if snap and current:
        for field in ("odds_h", "odds_d", "odds_a", "ah_home", "ah_away", "ou_over", "ou_under"):
            if field in current and field in snap:
                try:
                    delta = float(current[field]) - float(snap[field])
                    if abs(delta) > 0.001:
                        drift[field] = round(delta, 3)
                except (TypeError, ValueError):
                    pass
    return {
        "initial": snap or None,
        "drift": drift or None,
        "has_snapshot": bool(snap),
    }


# ── 自动赛果记录 (Req3) ──
def _auto_record_result(fx: dict) -> Optional[dict]:
    """检测比赛结束(match_state<0), 自动记录赛果+开盘/收盘赔率。"""
    from time import time as _time
    ms = fx.get("match_state")
    if ms is None or (isinstance(ms, (int, float)) and ms >= 0):
        return None  # 未结束或状态未知
    sh = fx.get("score_home")
    sa = fx.get("score_away")
    if isinstance(sh, (int, float)) and isinstance(sa, (int, float)):
        result = "H" if sh > sa else "D" if sh == sa else "A"
    else:
        result = None
    date_key = (fx.get("commence_time") or "")[:10]
    rkey = f"{fx.get('home','')}|{fx.get('away','')}|{date_key}"
    # 开盘赔率从快照取 (用与 _capture_initial_odds 一致的完整 key)
    snap_key = f"{fx.get('home','')}|{fx.get('away','')}|{fx.get('commence_time','')}"
    snap = _INITIAL_ODDS_SNAPSHOT.get(snap_key)
    closing = {
        k: fx.get(k) for k in (
            "odds_h", "odds_d", "odds_a",
            "ah_line", "ah_home", "ah_away",
            "ou_line", "ou_over", "ou_under",
        ) if fx.get(k) is not None
    }
    entry = {
        "home": fx.get("home"),
        "away": fx.get("away"),
        "league": fx.get("league") or fx.get("sport_key"),
        "date": date_key,
        "score_home": sh,
        "score_away": sa,
        "result": result,
        "opening_odds": {k: snap[k] for k in ("odds_h","odds_d","odds_a","ah_home","ah_away","ou_over","ou_under") if snap and snap.get(k) is not None} or None,
        "closing_odds": closing or None,
        "recorded_at": _time(),
        "match_minute_end": fx.get("match_minute"),
    }
    # 只记录一次 (首次检测到结束)
    if rkey not in _AUTO_RESULTS:
        _AUTO_RESULTS[rkey] = entry
        print(f"[auto-result] {entry['home']} vs {entry['away']} → {sh}-{sa} ({result})")
    return entry


@app.get("/api/leagues/{sport_key}/fixtures")
async def league_fixtures_api(sport_key: str):
    """获取指定联赛赛程 (数据源: 微瑞 live feed, 全局缓存 60s)。
    每场 fixture 附带 initial_odds (开盘快照) 和 drift (赔率漂移) 供前端对比 (Req2)。
    比赛结束(match_state<0)时自动记录赛果 + 开/收盘赔率 (Req3)。
    """
    feed = await _get_leisu_feed()
    if feed.get("error") or not feed.get("leagues"):
        feed = await asyncio.to_thread(_gq_feed_fallback)
    if feed.get("error"):
        return _wrap_data({"error": feed["error"], "fixtures": []})
    leagues = feed["leagues"]
    fx = leagues.get(sport_key) or leagues.get(sport_key.strip()) or []
    if fx:
        _attach_gq_1x2(fx)
    # Drift 推断比分 (弥补 leyu 上游 msc 滞后, 详见 _gq_live_scores 同款实现)
    for f in fx if isinstance(fx, list) else []:
        try:
            mk = f.get("match_key") or f.get("id")
            if not mk: continue
            db_sh = f.get("score_home") if isinstance(f.get("score_home"), int) else 0
            db_sa = f.get("score_away") if isinstance(f.get("score_away"), int) else 0
            drift_g = await asyncio.to_thread(_gq_drift_infer_goals, mk, f.get("league"))
            if drift_g:
                # 漂移推断只作「DB无真实比分(0-0)」时的兜底补充, 绝不覆盖已有真实分
                if db_sh == 0 and db_sa == 0:
                    f["score_home"] = drift_g[0]
                    f["score_away"] = drift_g[1]
                    if (f["score_home"], f["score_away"]) != (db_sh, db_sa):
                        f["score_inferred"] = True
                # 否则: DB已有真实比分(1-0等) → 信任DB, 不覆盖
        except Exception:
            pass
    # 为每场比赛附加初始快照 + 漂移 (Req2), 并自动记录已结束赛果 (Req3)
    enriched = []
    for f in (fx if isinstance(fx, list) else []):
        snap_info = _capture_initial_odds(f)
        enriched.append({**f, "_snapshot": snap_info})
        _auto_record_result(f)  # Req3: 静默自动记录 (仅结束态落盘)
    # 仅显示应展示的比赛: 有赔率 / 已结束(封盘带赛果) / 未开赛(赛程页必须展示未来赛程)。
    # 失败开放: 若过滤后为空(该联赛全无赔率且无赛果)则保留全部, 防 UI 空白。
    covered = [f for f in enriched if _fixture_should_show(f) or f.get("match_state") in (0, "0")]
    if covered:
        out = covered
        hidden = len(enriched) - len(covered)
        if hidden > 0:
            print(f"[filter] {sport_key}: 隐藏 {hidden} 场(既无赔率又无赛果=未覆盖)")
    else:
        out = enriched
        print(f"[warn] {sport_key}: 过滤后全空, 退化显示全部 (安全)")
    return _wrap_data({
        "sport_key": sport_key,
        "name": sport_key,
        "category": "实时赛事",
        "fixtures": out,
        "cached": False,
    })


# ── 全量赛程聚合端点 (修复"赛事不全": 前端原 Promise.all 并发逐联赛抓取触发全局限流 120/min → 部分联赛 fixtures 被 429 丢弃) ──
@app.get("/api/all-fixtures")
async def all_fixtures_api(days: int = 7):
    """全量赛程聚合端点.

    修复前端 LiveScores.fetchAll 用 Promise.all 并发抓取所有联赛 fixtures 时,
    因全局限流 key=client_ip|api (120/min) 在 days≥7 时 233 个请求远超限制,
    大量联赛 fixtures 被 429 丢弃 → "赛事不全". 此端点后端内部一次取 feed,
    前端仅发 1 请求, 彻底绕开限流.

    days: 仅保留 commence_time 在近[-1,+days]天窗口内的比赛(默认7), 防历史过期堆积.
    返回扁平 fixtures 数组(含 sport_key/league 标注), drift 仅在无真实分时兜底.
    """
    # SSoT 修正(2026-07-31): 旧逻辑 leisu OR GQ —— 当 leisu 返回部分脏数据(78 场无开赛时间)时,
    # events.db 真实比赛的 172+ 场被整体丢弃 → 前端"赛事不全". 改为:
    #   events.db 已采集比赛作基准全集(采集器持续写入, 覆盖全量真实在跑/将跑比赛)
    #   + 微瑞 leisu 实时 feed 叠加"进行中"的比分/分钟/状态(leisu 实时性更好)
    #   + 给基准补 1X2/AH/OU 赔率(否则前端永远不显示赔率、无法点分析)
    gq_fb = await asyncio.to_thread(_gq_feed_fallback)
    gq_leagues = gq_fb.get("leagues", {}) if not gq_fb.get("error") else {}
    now = time.time()
    lo, hi = now - 1 * 86400, now + days * 86400
    def _in_window(ct):
        if ct is None:
            return True  # 无开赛时间(leisu 实时场)视为当场, 不丢
        try:
            if isinstance(ct, (int, float)):
                t = float(ct)
            else:
                s = str(ct).replace("Z", "+00:00")
                t = time.mktime(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")) if "T" in s else float(s)
            return lo <= t <= hi
        except Exception:
            return True
    # 1) GQ 基准 → 按窗口过滤 → 补赔率
    gq_flat: list = []
    for _lg, _fx in gq_leagues.items():
        for _f in (_fx if isinstance(_fx, list) else []):
            _ff = dict(_f)
            _ff["sport_key"] = _lg
            if not _ff.get("league"):
                _ff["league"] = _lg
            if _in_window(_ff.get("commence_time")):
                gq_flat.append(_ff)
    if gq_flat:
        gq_flat = await asyncio.to_thread(_attach_gq_1x2, gq_flat)
    base: Dict[str, dict] = {f"{f.get('home')}|{f.get('away')}": f for f in gq_flat}
    # 2) leisu 实时叠加 (失败/空则跳过, 不阻断基准)
    try:
        leisu = await _get_leisu_feed()
        leisu_leagues = leisu.get("leagues", {}) if not leisu.get("error") else {}
        for _lg, _fx in leisu_leagues.items():
            for _f in (_fx if isinstance(_fx, list) else []):
                if not isinstance(_f, dict):
                    continue
                _key = f"{_f.get('home')}|{_f.get('away')}"
                if _key in base:
                    # leisu 实时字段覆盖 GQ (比分/分钟/状态更实时)
                    for _k in ("score_home", "score_away", "match_minute", "match_state"):
                        if _f.get(_k) is not None:
                            base[_key][_k] = _f[_k]
                else:
                    _nf = dict(_f)
                    _nf["sport_key"] = _lg
                    if not _nf.get("league"):
                        _nf["league"] = _lg
                    if _in_window(_nf.get("commence_time")):
                        base[_key] = _nf
    except Exception:
        pass
    # 3) 漂移推断(仅 DB 无真实分 0-0 时兜底) + 组装
    out = []
    for f in base.values():
        try:
            f = dict(f)
            mk = f.get("match_key") or f.get("id")
            if mk:
                db_sh = f.get("score_home") if isinstance(f.get("score_home"), int) else 0
                db_sa = f.get("score_away") if isinstance(f.get("score_away"), int) else 0
                if db_sh == 0 and db_sa == 0:
                    dg = await asyncio.to_thread(_gq_drift_infer_goals, mk, f.get("league"))
                    if dg:
                        f["score_home"], f["score_away"] = dg[0], dg[1]
                        f["score_inferred"] = True
            out.append(f)
        except Exception:
            continue
    out = _filter_football_matches(out)
    return _wrap_data({"fixtures": out, "count": len(out), "days": days})


# ── 自动赛果查询 (Req3: 替代手动赛果查询, 直接对接盘口自动记录) ──
@app.get("/api/auto-results")
async def auto_results_api(league: str = "", date: str = "", limit: int = 100):
    """返回自动记录的赛果 (检测 match_state<0 时落盘)。
    每条含 胜平负 + 让球(AH) + 大小球(OU) 的开盘/收盘赔率。
    注: 波胆(CS)赔率当前 feed 源未提供, opening_odds/closing_odds 仅含 1X2/AH/OU。
    """
    rows = list(_AUTO_RESULTS.values())
    rows.sort(key=lambda r: r.get("recorded_at", 0), reverse=True)
    if league:
        rows = [r for r in rows if league.lower() in (r.get("league") or "").lower()]
    if date:
        rows = [r for r in rows if (r.get("date") or "").startswith(date)]
    if limit and limit > 0:
        rows = rows[:limit]
    return _wrap_data({"results": rows, "total": len(rows), "missing_markets": ["cs"]})


# ═══ 水位信号 (前后两次快照差值, 跌水/升水=资金动向) ═══
@app.get("/api/water-signals")
async def water_signals_api(limit: int = 30, min_delta_pct: float = 1.0):
    """返回最近 24h 内水位信号, 按 |delta_pct| 倒序。
    - down = 跌水 (赔率降, 资金涌入此侧)
    - up   = 升水 (赔率升, 资金撤出此侧)
    - market 全名见 pipeline/collectors/leisu_live.py HPID_* 字段
    """
    from pipeline.leisu_store import get_recent_signals, init_db
    try:
        init_db()
        sigs = get_recent_signals(limit=limit, min_delta_pct=min_delta_pct)
        return _wrap_data({"signals": sigs, "count": len(sigs), "min_delta_pct": min_delta_pct})
    except Exception as e:
        return _wrap_data({"error": str(e), "signals": [], "count": 0})


# ═══ 跨庄软线偏离信号 (真 edge 源: 多机构赔率 consensus 偏离检测) ═══
@app.get("/api/cross-book/signals")
async def cross_book_signals_api(limit: int = 50, min_spread_pp: float = 3.0,
                                 source: str = "long_images",
                                 min_severity: str = "any",
                                 actionable_only: bool = False,
                                 market: str = "1X2"):
    """跨庄软线信号 — 多机构赔率逐庄去水, 共识偏离 ≥ min_spread_pp 标记.

    数据源:
      - long_images: data/long_images.db.cross_book_odds (OCR 截图, obscure 联赛)
      - leisu:       data/football_data.db.leisu_odds (雷速多庄实时源, 真 edge 主源)
    min_severity: any/LOW/MED/HIGH — 仅返回达到该严重度的场次.
    actionable_only: 只返回 gate 结果(scan_actionable, 默认仅 HIGH≥15pp 放注级可下注).
    market: 1X2 / OU / AH — 检测市场(仅 source=leisu 生效; long_images 仅 1X2).
    """
    try:
        from pipeline.cross_book_edge import (
            analyze_all, to_report, analyze_all_leisu, to_report_leisu,
            scan_actionable,
        )
        if source == "leisu":
            edges = analyze_all_leisu(market=market)
            report = to_report_leisu(edges, with_actionable=True)
        else:
            edges = analyze_all(market="1X2")
            report = to_report(edges, with_actionable=True)
        # gate: 仅 HIGH(≥15pp) 放注级可下注
        if actionable_only:
            scan = scan_actionable(edges, min_severity="HIGH")
            return _wrap_data({"source": source, "market": market, "actionable": scan,
                               "n_actionable": len(scan)})
        # 按 max_spread_pp 筛选
        report["matches"] = [m for m in report["matches"] if m["max_spread_pp"] >= min_spread_pp]
        if min_severity != "any":
            report["matches"] = [m for m in report["matches"]
                                 if m.get("severity") == min_severity]
        if limit and len(report["matches"]) > limit:
            report["matches"] = report["matches"][:limit]
        report["filter_min_spread_pp"] = min_spread_pp
        report["filter_min_severity"] = min_severity
        report["source"] = source
        report["market"] = market
        return _wrap_data(report)
    except Exception as e:
        return _wrap_data({"error": str(e), "matches": [], "n_matches": 0})


@app.get("/api/cross-book/lookup")
async def cross_book_lookup_api(home: str = "", away: str = "", league: str = ""):
    """按主客队/联赛查跨庄软线."""
    try:
        from pipeline.cross_book_edge import load_matches, analyze_match
        all_matches = load_matches()
        candidates = []
        for m in all_matches:
            if home and home.lower() not in m["home"].lower(): continue
            if away and away.lower() not in m["away"].lower(): continue
            if league and league.lower() not in (m.get("league") or "").lower(): continue
            candidates.append(m)
        results = [asdict(analyze_match(m)) for m in candidates]
        for r in results:
            r.pop("books", None)  # 精简输出
        return _wrap_data({"matches": results, "count": len(results)})
    except Exception as e:
        return _wrap_data({"error": str(e), "matches": [], "count": 0})


def _get_cross_book_signal(home: str = "", away: str = "", league: str = ""):
    """从 cross_book_edge 查单场软线信号 (供 _live_predict 内联调用)."""
    try:
        from pipeline.cross_book_edge import load_matches, analyze_match
        all_matches = load_matches()
        for m in all_matches:
            if home.lower() not in m["home"].lower(): continue
            if away.lower() not in m["away"].lower(): continue
            if league and league.lower() not in (m.get("league") or "").lower(): continue
            edge = analyze_match(m)
            if edge.soft_lines or edge.n_books >= 2:
                return {
                    "n_books": edge.n_books,
                    "consensus": edge.consensus,
                    "best": edge.best,
                    "max_spread_pp": edge.max_spread_pp,
                    "n_soft_lines": len(edge.soft_lines),
                    "soft_lines": edge.soft_lines,
                }
        return None
    except Exception:
        return None


def _lookup_multibook_consensus(home: str, away: str):
    """从 leisu_odds 查多庄 sharp/retail 共识 (供 terminal_analyze_api 内联).

    模糊匹配 home/away 队名, 找到后跑 pipeline.multibook_consensus.analyze_match.
    返回 dict 或 None (leisu 无该比赛数据时).
    """
    try:
        import sqlite3, os
        db = os.path.join(PROJECT_ROOT, "data", "football_data.db")
        if not os.path.exists(db):
            return None
        conn = sqlite3.connect(db); conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT DISTINCT home_raw, away_raw FROM leisu_odds WHERE market='1X2'
              AND ((home_raw LIKE ? AND away_raw LIKE ?) OR (home_raw LIKE ? AND away_raw LIKE ?))
            LIMIT 5
        """, (f"%{home}%", f"%{away}%", f"%{away}%", f"%{home}%")).fetchall()
        if not rows:
            conn.close(); return None
        matches = [(r['home_raw'], r['away_raw']) for r in rows]
        conn.close()

        from pipeline.multibook_consensus import load_leisu_groups, analyze_match
        groups = load_leisu_groups(market="1X2")
        for g in groups:
            for mh, ma in matches:
                if mh == g['home'] and ma == g['away']:
                    res = analyze_match(g)
                    if res.n_books < 2:
                        return None
                    dv = res.divergences or []
                    return {
                        "n_books": res.n_books,
                        "n_sharp": res.n_sharp,
                        "has_true_sharp": res.has_true_sharp,
                        "sharp_books": res.sharp_books,
                        "sharp_consensus": {
                            "h": round(res.sharp_consensus['h'] * 100, 1),
                            "d": round(res.sharp_consensus['d'] * 100, 1),
                            "a": round(res.sharp_consensus['a'] * 100, 1),
                        },
                        "retail_mean": {
                            "h": round(res.retail_mean['h'] * 100, 1),
                            "d": round(res.retail_mean['d'] * 100, 1),
                            "a": round(res.retail_mean['a'] * 100, 1),
                        },
                        "value_side": {"outcome": res.value_side['outcome'], "pp": res.value_side['pp']},
                        "fade_side": {"outcome": res.fade_side['outcome'], "pp": res.fade_side['pp']},
                        "max_spread_pp": res.max_spread_pp,
                        "divergences": dv[:5],
                    }
        return None
    except Exception:
        return None


def _get_operator_signals(home: str, away: str,
                          live_h: float = None, live_d: float = None, live_a: float = None,
                          home_goals: int = None, away_goals: int = None, elapsed: int = None):
    """从 events.db 取开盘赔率 + live 赔率, 调用操盘手逆转模型输出信号。
    若前端未传赔率 → None. 若无盘中开盘价 → 用 live 赔率做零漂移基准(仍输出模型分数).
    home_goals/away_goals/elapsed: in-play 比分与分钟(可选), 传入后逆转信号按翻盘可行性缩放。
    """
    if not live_h or not live_d or not live_a:
        return None
    try:
        import sqlite3
        db = os.path.join(PROJECT_ROOT, "data", "events.db")
        oh, od_d, oa = live_h, live_d, live_a  # 默认零漂移
        if os.path.exists(db):
            conn = sqlite3.connect(db); conn.row_factory = sqlite3.Row
            odds = conn.execute("""
                SELECT s.selection, s.odds as first_odds
                FROM odds_snapshots s
                JOIN (SELECT match_key, selection, MIN(captured_at) as earliest
                      FROM odds_snapshots WHERE market='1X2' GROUP BY match_key, selection) e
                  ON s.match_key=e.match_key AND s.selection=e.selection
                 AND s.captured_at=e.earliest
                WHERE s.match_key IN (SELECT match_key FROM matches WHERE home LIKE ? AND away LIKE ?)
                  AND s.market='1X2'
            """, (f"%{home}%", f"%{away}%")).fetchall()
            od_map = {row['selection']: row['first_odds'] for row in odds}
            conn.close()
            if all(s in od_map for s in ('home', 'draw', 'away')):
                oh, od_d, oa = od_map['home'], od_map['draw'], od_map['away']

        from pipeline.operator_signals import operator_signal
        cur_score = f"{home_goals}-{away_goals}" if (home_goals is not None and away_goals is not None) else "0-0"
        cur_min = int(elapsed) if elapsed is not None else 0
        return operator_signal(oh=oh, od=od_d, oa=oa, ch=live_h, cd=live_d, ca=live_a,
                               current_score=cur_score, current_minute=cur_min)
    except Exception:
        return None


@app.get("/api/snapshot/{mid}")
async def get_snapshot_history(mid: str, limit: int = 20):
    """取某 mid 最近 N 份快照, 用于看历史水位曲线。"""
    import sqlite3 as _sq
    db_path = os.path.join(PROJECT_ROOT, "data", "leisu_odds.db")
    if not os.path.exists(db_path):
        return _wrap_data({"error": "暂无快照数据", "snapshots": []})
    c = _sq.connect(db_path)
    rows = c.execute("""
        SELECT snapshot_at, odds_h, odds_d, odds_a,
               ah_home, ah_away, ou_over, ou_under
        FROM odds_snapshots WHERE mid=?
        ORDER BY snapshot_at DESC LIMIT ?
    """, (mid, limit)).fetchall()
    c.close()
    snaps = [{
        "ts": r[0], "odds_h": r[1], "odds_d": r[2], "odds_a": r[3],
        "ah_home": r[4], "ah_away": r[5], "ou_over": r[6], "ou_under": r[7]
    } for r in rows]
    return _wrap_data({"mid": mid, "snapshots": snaps, "count": len(snaps)})



# ═══ 实时比分 ═══
_GQ_OPENING_CACHE = {}

def _gq_opening_odds(match_keys):
    """初盘赔率 = 该 match 的第一条 1X2 采集 (captured_at 最小). 结果按 match_key 永久缓存(初盘不变)."""
    missing = [k for k in match_keys if k not in _GQ_OPENING_CACHE]
    if missing:
        try:
            import sqlite3, os
            db = os.path.join(PROJECT_ROOT, "data", "events.db")
            c = sqlite3.connect(db)
            q = ("SELECT s.match_key, s.selection, s.odds FROM odds_snapshots s "
                 "JOIN (SELECT match_key, MIN(captured_at) AS fc FROM odds_snapshots "
                 "WHERE market='1X2' AND match_key IN (%s) GROUP BY match_key) f "
                 "ON f.match_key=s.match_key AND s.captured_at BETWEEN f.fc AND f.fc + 1.0 "
                 "AND s.market='1X2'"
                 % ','.join('?' * len(missing)))
            raw = {}
            for mk, sel, odds in c.execute(q, missing).fetchall():
                raw.setdefault(mk, {})[sel] = odds
            for mk in missing:
                v = raw.get(mk)
                _GQ_OPENING_CACHE[mk] = (
                    {"h": v.get("home"), "d": v.get("draw"), "a": v.get("away")}
                    if v else None
                )
            c.close()
        except Exception:
            for mk in missing:
                _GQ_OPENING_CACHE[mk] = None
    return {k: _GQ_OPENING_CACHE[k] for k in match_keys if isinstance(_GQ_OPENING_CACHE.get(k), dict)}


_GQ_AHOU_CACHE = {}

def _gq_best_ah_ou(match_keys):
    """取每场最佳 AH 线(让球最接近 0)和 OU 线(大小最接近 2.5)的当前+初盘赔率.

    返回 {mk: {ah_line, ah_home, ah_away, ah_op_home, ah_op_away,
              ou_line, ou_over, ou_under, ou_op_over, ou_op_under}}
    按 match_key 永久缓存(初盘/线不变, 当前值取最新采集).
    """
    missing = [k for k in match_keys if k not in _GQ_AHOU_CACHE]
    if missing:
        try:
            import sqlite3, os
            db = os.path.join(PROJECT_ROOT, "data", "events.db")
            c = sqlite3.connect(db)
            q = ("SELECT match_key, market, selection, line, odds, captured_at "
                 "FROM odds_snapshots "
                 "WHERE match_key IN (%s) AND (market LIKE 'AH_%%' OR market LIKE 'OU_%%') "
                 "ORDER BY captured_at ASC" % ','.join('?' * len(missing)))
            rows = c.execute(q, missing).fetchall(); c.close()
            # 按 (match_key, market) 分组, 每组时间排序(ASC)
            groups = {}
            for mk, mkt, sel, ln, odds, ts in rows:
                groups.setdefault((mk, mkt), []).append((sel, ln, odds, ts))
            result = {}
            for mk in missing:
                ah_candidates = {}  # line_val -> dict
                ou_candidates = {}
                for (mmk, mkt), snaps in groups.items():
                    if mmk != mk: continue
                    if not snaps: continue
                    first_ts = snaps[0][3]
                    cur = {}; op = {}
                    line_float = None
                    parts = mkt.split('_', 1)
                    if len(parts) == 2:
                        try: line_float = float(parts[1])
                        except ValueError: pass
                    for sel, ln, odds, ts in snaps:
                        # 更新当前值(最后出现的覆盖)
                        cur[sel] = (ln, odds)
                        # 初盘: 第一采集窗口 (±1s)
                        if ts <= first_ts + 1.0 and sel not in op:
                            op[sel] = odds
                    if not cur or line_float is None: continue
                    if mkt.startswith('AH_'):
                        d = {'ah_line': str(line_float),
                             'ah_home': cur.get('home', (None, None))[1],
                             'ah_away': cur.get('away', (None, None))[1]}
                        if 'home' in op and 'away' in op:
                            d['ah_op_home'] = op['home']; d['ah_op_away'] = op['away']
                        ah_candidates[abs(line_float)] = d
                    elif mkt.startswith('OU_'):
                        d = {'ou_line': str(line_float),
                             'ou_over': cur.get('over', (None, None))[1],
                             'ou_under': cur.get('under', (None, None))[1]}
                        if 'over' in op and 'under' in op:
                            d['ou_op_over'] = op['over']; d['ou_op_under'] = op['under']
                        ou_candidates[abs(line_float - 2.5)] = d
                entry = {}
                # AH: 最接近 0 的线
                if ah_candidates:
                    best_key = min(ah_candidates.keys())
                    entry.update(ah_candidates[best_key])
                # OU: 最接近 2.5 的线
                if ou_candidates:
                    best_key = min(ou_candidates.keys())
                    entry.update(ou_candidates[best_key])
                _GQ_AHOU_CACHE[mk] = entry if entry else None
        except Exception:
            for mk in missing:
                _GQ_AHOU_CACHE[mk] = None
    return {k: _GQ_AHOU_CACHE.get(k) for k in match_keys if _GQ_AHOU_CACHE.get(k)}


def _gq_drift_infer_goals(match_key, league=None):
    """用 1X2 odds 漂移检测进球 (初盘假定为 0:0 状态).

    1球门槛: 主客赔率反向移动 >35% (单方大跌+另一方大涨).
    2球门槛(2026-07-30 收紧): 大跌方 >50% 且 大涨方 >+100% (即非得分方赔率翻倍+) 才判2球.
      仅用单一阈值(原-0.55)会把"深盘一边倒"误判成2球; 加 +100% 守卫可区分"真屠杀"与"市场正常偏向".
    阈值依据: 进球后1X2赔率通常漂移 40-60%; 2球屠杀场非得分方赔率可涨数倍.
    修BUG#2(2026-07-29): 原25%阈值过低, 友谊赛/表演赛开盘→临场正常漂移25%+被误判进球.
      - 友谊赛/表演赛/热身赛禁用drift推断(赔率波动无规律)
      - 阈值从25%/45%提高到35%剔除噪声; 2球再加 +100% 守卫防误判
    修BUG#3(2026-07-30): 删除交叉误判(客队进球时主队赔率大涨被当成主队进球→凭空塞球).
    实测: 国防2-0大胜列斯特拉, 主赔-53.7%/客赔+1030% → 旧-0.55阈值判1球, 新-0.50+da>1.0守卫判2球.
    Returns (goals_home, goals_away) 或 None.
    """
    # 友谊赛/表演赛/热身赛: 赔率波动大且无规律, 禁用drift推断
    if league and any(k in str(league) for k in ("友谊", "表演", "热身", "Friend", "friendly")):
        return None
    try:
        import sqlite3, os
        db = os.path.join(PROJECT_ROOT, "data", "events.db")
        if not os.path.exists(db):
            return None
        c = sqlite3.connect(db); c.row_factory = sqlite3.Row
        # 第一采集 (开赛前后)
        first = {}
        for r in c.execute(
            "SELECT selection, odds FROM odds_snapshots WHERE match_key=? AND market='1X2' "
            "ORDER BY captured_at ASC LIMIT 10", (match_key,)).fetchall():
            first[r['selection']] = r['odds']
        # 最新采集
        last = {}
        for r in c.execute(
            "SELECT selection, odds FROM odds_snapshots WHERE match_key=? AND market='1X2' "
            "ORDER BY captured_at DESC LIMIT 10", (match_key,)).fetchall():
            if r['selection'] not in last:
                last[r['selection']] = r['odds']
        c.close()
        if not first or not last:
            return None
        if not all(s in first and s in last for s in ('home', 'draw', 'away')):
            return None
        dh = (last['home'] - first['home']) / first['home']
        da = (last['away'] - first['away']) / first['away']
        inf_h = inf_a = 0
        # 客队进球: away 跌 + home 涨 (1球门槛35%; 2球需 away大跌<-0.50 且 主队赔率暴涨>+100%)
        # 注(2026-07-30 修BUG): 原 if dh>0.55: inf_h+=1 已删除 — 客队进球时主队大涨只是市场反应,
        #   不代表主队进球. 同理主队分支删 if da>0.55: inf_a+=1. 否则大胜场会凭空捏造非得分方进球
        #   (实测: 国防2-0大胜列斯特拉, 客赔+1030%被误判成客队1球→前端显示1-1).
        #   2球守卫 da>1.0/dh>1.0: 仅当非得分方赔率翻倍+才认2球, 避免深盘一边倒被误判成屠杀.
        if da < -0.35 and dh > 0.35:
            inf_a = 2 if (da < -0.50 and dh > 1.0) else 1
        # 主队进球: home 跌 + away 涨
        elif dh < -0.35 and da > 0.35:
            inf_h = 2 if (dh < -0.50 and da > 1.0) else 1
        else:
            return None
        return (inf_h, inf_a)
    except Exception:
        return None


def _gq_live_scores(limit: int = 50):
    """events.db 实时比赛回退 (leisu_live_scores 未采集时)。

    返回与 leisu get_live_matches 同构的列表, 供 LiveScores 前端消费。
    仅取 status='live' (进行中) 比赛; 无 AH/OU 盘口(line=None) 时前端显示 '——'。
    kickoff 转 ISO 供前端 stateOf 滞后兜底判定。
    附带 opening_h/d/a (初盘, 固定不变) + odds_h/d/a (滚动, 实时)。
    比分优先用 events.db matches.score, 若 drift 强烈反向移动 → 推断有进球, 取 max(db, inferred).
    """
    try:
        import sqlite3, os
        db = os.path.join(PROJECT_ROOT, "data", "events.db")
        if not os.path.exists(db):
            return []
        c = sqlite3.connect(db); c.row_factory = sqlite3.Row
        import time as _time
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        cutoff = _time.time() - 10800  # 修BUG#3: 4h→3h(含中场+补时+缓冲, 超过基本已结束)
        # 修复: 纳入"实际已开赛但 status 仍=scheduled 滞后"的比赛(开赛 0~180min 窗口),
        # 否则盘口快照已采到、前端却看不到这场新比赛(开赛前~10分钟不同步窗口, 用户报"盘口有新比赛但列表没有")。
        # mststi 仍由下方 kickoff 时间逻辑动态判定(开赛未来→0 / >130min→-1 / 否则→1), 不硬编码。
        _gmt8 = _dt.now(_tz(_td(hours=8)))
        _now_iso = _gmt8.strftime('%Y-%m-%d %H:%M')   # kickoff 为 GMT+8 naive 分钟级字符串
        _cut_ko = (_gmt8 - _td(minutes=180)).strftime('%Y-%m-%d %H:%M')
        rows = c.execute("""
            SELECT match_key, home, away, league, kickoff, status,
                   score_home, score_away, minute, mid, last_seen
            FROM matches
            WHERE league NOT LIKE 'VS-%' AND last_seen > ?
              AND (status = 'live'
                   OR (status = 'scheduled' AND kickoff < ? AND kickoff >= ?))
            ORDER BY last_seen DESC
            LIMIT ?
        """, (cutoff, _now_iso, _cut_ko, max(int(limit), 300))).fetchall()
        keys = [r['match_key'] for r in rows]
        odds_map = {}
        if keys:
            od = c.execute(
                "SELECT match_key, selection, odds FROM odds_snapshots "
                "WHERE match_key IN (%s) AND market='1X2'" % ','.join('?' * len(keys)), keys).fetchall()
            for mk, sel, odds in od:
                odds_map.setdefault(mk, {})[sel] = odds
        c.close()
        opening_map = _gq_opening_odds(keys)
        ah_ou_map = _gq_best_ah_ou(keys)
        out = []
        for r in rows:
            ko = (r['kickoff'] or '').strip()
            iso = ko.replace(' ', 'T') + ':00' if ' ' in ko else ko
            mk = r['match_key']
            om = odds_map.get(mk, {})
            h = om.get('home'); d = om.get('draw'); a = om.get('away')
            op = opening_map.get(mk)
            ao = ah_ou_map.get(mk) or {}
            # Drift 推断比分 (弥补 leyu 上游 msc 滞后). 取 max 防止误判退步.
            db_sh = r['score_home'] if isinstance(r['score_home'], int) else 0
            db_sa = r['score_away'] if isinstance(r['score_away'], int) else 0
            drift_g = _gq_drift_infer_goals(mk, r['league'])
            score_inferred = False
            if drift_g:
                # 漂移推断只作「DB无真实比分(0-0)」时的兜底补充, 绝不覆盖已有真实分
                if db_sh == 0 and db_sa == 0:
                    final_sh, final_sa = drift_g[0], drift_g[1]
                    score_inferred = (final_sh, final_sa) != (db_sh, db_sa)
                else:
                    final_sh, final_sa = db_sh, db_sa
            else:
                final_sh, final_sa = db_sh, db_sa
            # 修BUG#3: 不硬编码mststi=1. 用status+kickoff时间动态判定:
            # status=finished→已结束; 否则按kickoff距今>130min兜底为已结束
            import time as _t2
            # 关键修正: 开赛时间在未来 → 不可能是 live/finished, 强制未开赛(规避 GQ 脏数据 live+minute=45)
            _is_upcoming = False
            if ko:
                try:
                    from datetime import datetime
                    ko_dt = datetime.fromisoformat(iso.replace('Z',''))
                    ko_age_min = (_t2.time() - ko_dt.timestamp()) / 60
                    if ko_age_min < -1:
                        _is_upcoming = True
                    elif not _is_finished and ko_age_min > 130:
                        _is_finished = True
                    # 半场/全场节点卡死检测 (obscure 联赛 minute 不更新但 last_seen 仍刷新)
                    if not _is_finished and not _is_upcoming:
                        minute = r['minute']
                        if minute == 45 and ko_age_min > 75:
                            _is_finished = True
                        elif minute == 90 and ko_age_min > 105:
                            _is_finished = True
                except Exception:
                    pass
            _mstate = 0 if _is_upcoming else (-1 if _is_finished else 1)
            if _is_upcoming:
                final_sh, final_sa, score_inferred = None, None, False
            # 修正 GQ 脏 minute: 45' 但开赛<45min / 90' 但开赛<90min → 不可能是中场
            _mm = r['minute']
            if _mstate == 1 and ko_dt:
                elapsed_min = (_t2.time() - ko_dt.timestamp()) / 60
                if (_mm == 45 and elapsed_min < 45) or (_mm == 90 and elapsed_min < 90):
                    _mm = None
            out.append({
                "mid": mk, "home": r['home'], "away": r['away'], "league": r['league'],
                "mststi": _mstate, "match_state": _mstate,
                "score_home": final_sh, "score_away": final_sa,
                "score_inferred": score_inferred,
                "match_minute": "" if _is_upcoming else (_mm if _mm else ""),
                "mlet": None, "events": [],
                "snapshot_at": r['last_seen'],
                "is_live": not _is_finished,
                "odds_h": h, "odds_d": d, "odds_a": a,
                "opening_h": (op or {}).get("h"), "opening_d": (op or {}).get("d"), "opening_a": (op or {}).get("a"),
                "ah_line": ao.get("ah_line"), "ah_home": ao.get("ah_home"), "ah_away": ao.get("ah_away"),
                "ah_op_home": ao.get("ah_op_home"), "ah_op_away": ao.get("ah_op_away"),
                "ou_line": ao.get("ou_line"), "ou_over": ao.get("ou_over"), "ou_under": ao.get("ou_under"),
                "ou_op_over": ao.get("ou_op_over"), "ou_op_under": ao.get("ou_op_under"),
                "commence_time": iso,
            })
            if len(out) >= int(limit):
                break
        return out
    except Exception:
        return []


def _apply_drift_score(f: dict) -> bool:
    """对单个 fixture 应用 GQ drift 推断比分 (与 league_fixtures_api 同源). 直接改 f, 返回是否触发推断."""
    try:
        mk = f.get("match_key") or f.get("id")
        if not mk:
            return False
        db_sh = f.get("score_home") if isinstance(f.get("score_home"), int) else 0
        db_sa = f.get("score_away") if isinstance(f.get("score_away"), int) else 0
        drift_g = _gq_drift_infer_goals(mk, f.get("league"))
        if drift_g:
            # 漂移推断只作「DB无真实比分(0-0)」时的兜底补充, 绝不覆盖已有真实分
            if db_sh == 0 and db_sa == 0:
                f["score_home"] = drift_g[0]
                f["score_away"] = drift_g[1]
                if (f["score_home"], f["score_away"]) != (db_sh, db_sa):
                    f["score_inferred"] = True
                    return True
    except Exception:
        pass
    return False


@app.get("/api/matches/state")
async def matches_state_api(slim: int = 1, days: int = 7):
    """WS1 统一比赛状态权威合并锚点.

    修复(2026-08-13): 此前缺失此端点, 前端 api.ts getMatchStates 每5s 调它 → 404,
    WS1 重合并层形同虚设, 前端只能靠 all-fixtures 自带 match_state 筛(僵尸场难剔除)。

    逻辑: 取 days 窗口内全部比赛(与 /api/live-scores 同源: 雷速 feed + GQ fallback),
    逐场用 pipeline.match_state.enrich_match_state 计算权威 match_state + _resolved_status,
    返回精简锚点供前端 5s 轮询合并. 不注入比分/赔率(那是 live-scores 职责), 只给状态判定.
    """
    try:
        from pipeline.match_state import enrich_match_state
        now = datetime.now(timezone.utc)
        feed = await _get_leisu_feed()
        if feed.get("error") or not feed.get("leagues"):
            feed = await asyncio.to_thread(_gq_feed_fallback)
        if feed.get("error"):
            return _wrap_data({"fixtures": [], "count": 0})
        fixtures = []
        cutoff = datetime.now() - timedelta(days=days)
        for lg, fx_list in feed["leagues"].items():
            for f in (fx_list if isinstance(fx_list, list) else []):
                ko = f.get("commence_time") or f.get("kickoff")
                if days and ko:
                    try:
                        ko_dt = datetime.fromisoformat(ko.replace("Z", "+00:00")).replace(tzinfo=None)
                        if ko_dt < cutoff:
                            continue
                    except Exception:
                        pass
                en = enrich_match_state(f, now)
                fixtures.append({
                    "home": en.get("home"),
                    "away": en.get("away"),
                    "match_state": en.get("match_state"),
                    "_resolved_status": en.get("_resolved_status"),
                })
        return _wrap_data({"fixtures": fixtures, "count": len(fixtures)})
    except Exception as e:
        return _wrap_error("matches_state_error", str(e), status=500)


_LIVE_SCORES_RESULT_CACHE = {"fetched_at": 0.0, "data": None}
_LIVE_SCORES_RESULT_LOCK = None

@app.get("/api/live-scores")
async def live_scores_api(limit: int = 5000):
    """实时比分 (带 TTL=3s 单飞缓存, 根治并发打 7.9GB WAL 库退化导致的 30s 超时)。"""
    global _LIVE_SCORES_RESULT_CACHE, _LIVE_SCORES_RESULT_LOCK
    if _LIVE_SCORES_RESULT_LOCK is None:
        _LIVE_SCORES_RESULT_LOCK = asyncio.Lock()
    now = time.time()
    if _LIVE_SCORES_RESULT_CACHE["data"] is not None and (now - _LIVE_SCORES_RESULT_CACHE["fetched_at"]) < 3.0:
        return _LIVE_SCORES_RESULT_CACHE["data"]
    async with _LIVE_SCORES_RESULT_LOCK:
        if _LIVE_SCORES_RESULT_CACHE["data"] is not None and (now - _LIVE_SCORES_RESULT_CACHE["fetched_at"]) < 3.0:
            return _LIVE_SCORES_RESULT_CACHE["data"]
        result = await _live_scores_compute(limit)
        _LIVE_SCORES_RESULT_CACHE = {"fetched_at": time.time(), "data": result}
        return result


async def _live_scores_compute(limit: int = 5000):
    """实时比分 (与 fixtures 端点同源: 雷速 feed + GQ drift 修正)。

    早期版本回退 events.db status='live' (仅乐鱼覆盖的 ~50 场), 与主列表 fixtures(雷速 feed, ~360 场)
    数据源/覆盖不一致 → 前端 5s 轮询按 home|away 匹配时仅 ~10% 命中, 其余进行中比赛比分停滞/缺失
    (即"前端比分没匹配好")。现统一为与 league_fixtures_api 完全相同的 feed 源 + drift 修正,
    使 key/覆盖/比分三者一致, 匹配率→~100%。
    """
    try:
        feed = await _get_leisu_feed()
        if feed.get("error") or not feed.get("leagues"):
            feed = await asyncio.to_thread(_gq_feed_fallback)
        if feed.get("error"):
            return _wrap_data({"matches": [], "count": 0})
        matches = []
        for lg, fx_list in feed["leagues"].items():
            for f in (fx_list if isinstance(fx_list, list) else []):
                try:
                    ms = f.get("match_state")
                    # 仅保留真正进行中: match_state 1-5 (整数), 或 feed 状态落后但 minute>0
                    minute = f.get("match_minute")
                    minute_num = None
                    if minute is not None and minute != "" and minute != "PA":
                        try:
                            minute_num = float(minute) if isinstance(minute, (int, float)) else float(str(minute))
                        except (ValueError, TypeError):
                            minute_num = None
                    is_live = (isinstance(ms, int) and not isinstance(ms, bool) and 1 <= ms <= 5)
                    # 兜底: state=0 但开赛已过 10-180min → 视为进行中 (前端 stateOf 同源)
                    if not is_live:
                        ko_str = f.get("commence_time") or f.get("kickoff") or ""
                        if ko_str:
                            try:
                                ko_dt = datetime.fromisoformat(ko_str.replace("Z", "+00:00"))
                                elapsed_min = (datetime.utcnow() - ko_dt.replace(tzinfo=None)).total_seconds() / 60
                                # 修复: 兜底窗口下探到开赛即生效(0<el<180), 覆盖"刚开赛0~10min但feed状态滞后"的新比赛,
                                # 否则前端看不到这场已在进行的新比赛(用户报"盘口有但列表没有")
                                if 0 < elapsed_min < 180:
                                    is_live = True
                            except (ValueError, TypeError):
                                pass
                    # 排除已结束(-1)和异常(>=6)
                    if not is_live or (isinstance(ms, int) and not isinstance(ms, bool) and (ms < 0 or ms >= 6)):
                        continue  # 仅进行中
                    await asyncio.to_thread(_apply_drift_score, f)
                    matches.append({
                        "mid": f.get("match_key") or f.get("id"),
                        "match_key": f.get("match_key"),
                        "home": f.get("home"), "away": f.get("away"),
                        "league": f.get("league"),
                        "mststi": ms, "match_state": ms,
                        "score_home": f.get("score_home"), "score_away": f.get("score_away"),
                        "score_inferred": f.get("score_inferred", False),
                        "match_minute": f.get("match_minute") if f.get("match_minute") is not None else "",
                        "mlet": None, "events": [],
                        "snapshot_at": time.time(),
                        "is_live": True,
                        "odds_h": f.get("odds_h"), "odds_d": f.get("odds_d"), "odds_a": f.get("odds_a"),
                        "opening_h": f.get("opening_h"), "opening_d": f.get("opening_d"), "opening_a": f.get("opening_a"),
                        "ah_line": f.get("ah_line"), "ah_home": f.get("ah_home"), "ah_away": f.get("ah_away"),
                        "ah_op_home": f.get("ah_op_home"), "ah_op_away": f.get("ah_op_away"),
                        "ou_line": f.get("ou_line"), "ou_over": f.get("ou_over"), "ou_under": f.get("ou_under"),
                        "ou_op_over": f.get("ou_op_over"), "ou_op_under": f.get("ou_op_under"),
                        "commence_time": f.get("commence_time"),
                    })
                except Exception:
                    continue
        # 有比分的排前面, 提升有效信息密度
        # ── GQ 比分 merge: 根治实时比分滞后 (2026-08-26) ──
        # 旧逻辑仅在 leisu 比分为 None 时兜底, 导致 leisu 180s 缓存里的陈旧比分
        # 反被保留(主源滞后≈3分钟). 新逻辑: events.db 近期(<120s)有更新的同场,
        # 其比分/分钟覆盖 leisu 陈旧值 → 重叠场滞后从 ≤180s 降到 GQ 自身 5-30s 粒度.
        try:
            import sqlite3 as _sq, time as _t
            _now = _t.time()
            _gq = _sq.connect(os.path.join(PROJECT_ROOT, "data", "events.db"))
            _gq_rows = _gq.execute(
                "SELECT home, away, score_home, score_away, minute, last_seen "
                "FROM matches WHERE status='live' AND score_home IS NOT NULL"
            ).fetchall()
            _gq.close()
            # 同 home|away 可能多行, 取 last_seen 最新的一行
            _gq_map = {}
            for r in _gq_rows:
                k = f"{r[0]}|{r[1]}"
                if k not in _gq_map or (r[5] or 0) > (_gq_map[k][5] or 0):
                    _gq_map[k] = r
            for _m in matches:
                _key = f"{_m['home']}|{_m['away']}"
                _g = _gq_map.get(_key)
                if not _g:
                    continue
                _gq_age = _now - (float(_g[5]) if _g[5] else 0)
                _gq_recent = _gq_age <= 120  # GQ 近期活跃 → 其比分比 180s leisu 缓存更可信
                _gq_score = (_g[2], _g[3])
                _gq_goals = (_g[2] or 0) + (_g[3] or 0)  # GQ 总进球数
                _leisu_score = (_m.get("score_home"), _m.get("score_away"))
                # 1) leisu 无比分 → 直接补 GQ (原兜底逻辑)
                if _m.get("score_home") is None:
                    _m["score_home"] = _g[2]
                    _m["score_away"] = _g[3]
                # 2) GQ 近期且自身记录到进球(goals>0)、与 leisu 分歧 → GQ 检测到 leisu 漏报的进球, 覆盖.
                #    ⚠ 安全闸(防"假0-0"): GQ 的 0-0 绝不覆盖 leisu 的非零真实比分——
                #    避免 GQ 比分解析滞后但时间戳新鲜时, 把 leisu 的 2-1 误改成 0-0 (绿色live标+全场00平局).
                #    仅当 GQ 确实记录到进球(>0)才允许覆盖 leisu; 0-0 只作为 leisu 也为 0-0 时的共存值.
                elif _gq_recent and _gq_goals > 0 and _gq_score != _leisu_score:
                    _m["score_home"] = _g[2]
                    _m["score_away"] = _g[3]
                # 3) 分钟: leisu 为空且 GQ 近期有值 → 补权威分钟 (修复 leisu minute 不落地)
                if not _m.get("match_minute") and _g[4] is not None and _g[4] != "" and _gq_recent:
                    _m["match_minute"] = _g[4]
        except Exception:
            pass
        matches.sort(key=lambda m: ((m.get("score_home") or 0) + (m.get("score_away") or 0)), reverse=True)

        # ── 直接注入 events.db 有比分的 live 比赛 (雷速 feed 和 GQ 覆盖不同联赛, 互相不重叠) ──
        try:
            import sqlite3 as _sq
            _gqdb = os.path.join(PROJECT_ROOT, "data", "events.db")
            if os.path.exists(_gqdb):
                _gq = _sq.connect(_gqdb)
                # 修复: 纳入"实际已开赛但 status 仍=scheduled 滞后"的比赛(开赛 0~180min, 盘口已采到),
                # 否则 GQ 有盘口/前端却看不到这场新比赛(开赛前~10min不同步窗口, 用户报"盘口有新比赛但列表没有").
                # mststi 在下方注入时硬编码 1 (进行中), 仅用于已确认开赛的比赛.
                from datetime import datetime as _dt2, timezone as _tz2, timedelta as _td2
                _g8 = _dt2.now(_tz2(_td2(hours=8)))
                _now_iso = _g8.strftime('%Y-%m-%d %H:%M')
                _cut_ko = (_g8 - _td2(minutes=180)).strftime('%Y-%m-%d %H:%M')
                _gq_rows = _gq.execute(
                    "SELECT match_key, home, away, league, score_home, score_away, minute, kickoff, last_seen"
                    " FROM matches WHERE score_home IS NOT NULL AND last_seen > ?"
                    " AND (status = 'live'"
                    "      OR (status = 'scheduled' AND kickoff < ? AND kickoff >= ?))",
                    (time.time() - 10800, _now_iso, _cut_ko)
                ).fetchall()
                _gq_keys = [r[0] for r in _gq_rows]
                # 当前 1X2 赔率: 取 odds_snapshots 最新一条(与 _gq_live_scores 同口径),
                # 修复"前端有比赛却无盘口"导致 盘口比赛数目≠前端比赛数目 的不匹配
                _odds_map = {}
                if _gq_keys:
                    _od = _gq.execute(
                        "SELECT match_key, selection, odds FROM odds_snapshots "
                        "WHERE match_key IN (%s) AND market='1X2'" % ','.join('?' * len(_gq_keys)), _gq_keys
                    ).fetchall()
                    for _mk, _sel, _o in _od:
                        _odds_map.setdefault(_mk, {})[_sel] = _o
                _gq.close()
                # 初盘 + AH/OU 用既有缓存辅助函数(各自开连接), 与 _gq_live_scores 一致
                _opening_map = _gq_opening_odds(_gq_keys)
                _ah_ou_map = _gq_best_ah_ou(_gq_keys)
                _existing = {f"{m['home']}|{m['away']}" for m in matches}
                for _r in _gq_rows:
                    _home, _away = _r[1], _r[2]
                    if f"{_home}|{_away}" in _existing:
                        continue
                    _mk = _r[0]
                    _om = _odds_map.get(_mk, {})
                    _op = _opening_map.get(_mk)
                    _ao = _ah_ou_map.get(_mk) or {}
                    matches.append({
                        "mid": _mk, "match_key": _mk, "home": _home, "away": _away, "league": _r[3],
                        "mststi": 1, "match_state": 1,
                        "score_home": _r[4], "score_away": _r[5],
                        "score_inferred": False, "match_minute": str(_r[6] or ""),
                        "mlet": None, "events": [], "snapshot_at": time.time(), "is_live": True,
                        "odds_h": _om.get("home"), "odds_d": _om.get("draw"), "odds_a": _om.get("away"),
                        "opening_h": (_op or {}).get("h"), "opening_d": (_op or {}).get("d"), "opening_a": (_op or {}).get("a"),
                        "ah_line": _ao.get("ah_line"), "ah_home": _ao.get("ah_home"), "ah_away": _ao.get("ah_away"),
                        "ou_line": _ao.get("ou_line"), "ou_over": _ao.get("ou_over"), "ou_under": _ao.get("ou_under"),
                        "ah_op_home": _ao.get("ah_op_home"), "ah_op_away": _ao.get("ah_op_away"),
                        "ou_op_over": _ao.get("ou_op_over"), "ou_op_under": _ao.get("ou_op_under"),
                        "commence_time": _r[7],
                    })
        except Exception:
            pass
        # 关键修复(2026-08-15): 回退 feed(乐鱼/GQ) 的 fixture 本身不含盘口字段,
        # 统一从 odds_snapshots 补齐 1X2 当前/初盘 + AH/OU, 否则"前端有比赛却无盘口"
        # → 盘口比赛数目(带赔率) ≠ 前端比赛数目(展示场次) 的不匹配。
        try:
            _attach_gq_1x2(matches)
        except Exception:
            pass
        matches = _filter_football_matches(matches)
        if limit and len(matches) > limit:
            matches = matches[:limit]
        return _wrap_data({"matches": matches, "count": len(matches)})
    except Exception as e:
        return _wrap_data({"error": str(e), "matches": [], "count": 0})


@app.get("/api/live-score/{mid}")
async def live_score_history_api(mid: str, limit: int = 60):
    """某 mid 的比分时序 (快照历史)。
    2026-08-27: 雷速已删, 改查 events.db.odds_snapshots(score_at/minute_at 秒级落库)."""
    try:
        import sqlite3 as _sq
        db = os.path.join(PROJECT_ROOT, "data", "events.db")
        c = _sq.connect(db)
        mk_row = c.execute("SELECT match_key FROM matches WHERE mid=?", (mid,)).fetchone()
        if not mk_row or not mk_row[0]:
            c.close()
            return _wrap_data({"mid": mid, "history": [], "count": 0})
        rows = c.execute("""
            SELECT captured_at, score_at, minute_at FROM odds_snapshots
            WHERE match_key=? AND score_at IS NOT NULL AND score_at != ''
            ORDER BY captured_at DESC LIMIT ?
        """, (mk_row[0], limit)).fetchall()
        c.close()
        history = []
        for ts, sa, ma in rows:
            sh = sa_ = None
            if sa and "-" in sa:
                parts = sa.split("-")
                try:
                    sh, sa_ = int(parts[0]), int(parts[1])
                except (ValueError, IndexError):
                    continue
            history.append({
                "ts": ts, "score_home": sh, "score_away": sa_,
                "match_minute": ma, "mststi": None,
            })
        return _wrap_data({"mid": mid, "history": history, "count": len(history)})
    except Exception as e:
        return _wrap_data({"error": str(e), "history": [], "count": 0})


# ═══ 单场实时刷新 (前端 5s 轮询用, 进球后比分/赔率同步) ═══
_LIVE_UPDATE_CACHE: Dict[str, Dict[str, Any]] = {}
_LIVE_UPDATE_LOCK = None
LIVE_UPDATE_TTL = float(os.getenv("LEISU_LIVE_UPDATE_TTL", "5"))


async def _get_leisu_feed_fresh():
    """强制重新构建 feed (绕开 60s cache), 给 live-update 用。
    2026-08-27: 雷速已删, 直接重建 events.db feed."""
    return await asyncio.to_thread(_gq_build_feed)


@app.get("/api/live-update/{mid}")
async def live_update_api(mid: str, force: bool = False):
    """单场实时更新: 返回最新 score + 6 大市场赔率.
    - 5s 内重复请求走 cache; force=true 强制刷新.
    - 前端 5s 轮询 → 进球后最多 5-15s 反映到 UI.
    """
    global _LIVE_UPDATE_LOCK
    if _LIVE_UPDATE_LOCK is None:
        _LIVE_UPDATE_LOCK = asyncio.Lock()
    now = time.time()
    cached = _LIVE_UPDATE_CACHE.get(mid)
    if not force and cached and (now - cached.get("fetched_at", 0)) < LIVE_UPDATE_TTL:
        return _wrap_data(cached["data"])

    async with _LIVE_UPDATE_LOCK:
        cached = _LIVE_UPDATE_CACHE.get(mid)
        if not force and cached and (now - cached.get("fetched_at", 0)) < LIVE_UPDATE_TTL:
            return _wrap_data(cached["data"])
        try:
            feed = await _get_leisu_feed_fresh()
        except Exception as e:
            return _wrap_data({"error": str(e), "mid": mid})
        target = None
        for lg, fx in feed.get("leagues", {}).items():
            for m in fx:
                if str(m.get("id")) == str(mid):
                    target = dict(m)
                    target["league"] = lg
                    break
            if target:
                break
        if not target:
            return _wrap_data({"error": "未找到该 mid", "mid": mid})
        _LIVE_UPDATE_CACHE[mid] = {"data": target, "fetched_at": now}
        return _wrap_data(target)




# ═══ GQ 今日比赛时间轴 (乐鱼体育) ═══
@app.get("/api/timeline/today")
async def timeline_today_api(limit: int = None):
    """今日(GMT+8)比赛时间轴: 聚合全部今日比赛, 按开赛升序, 附比分/状态/赔率.

    - ?limit=N 可选, 默认不限.
    - 内部为阻塞 IO(urllib + sqlite3), 用 asyncio.to_thread 避免阻塞事件循环.
    - 若 GQ API 整体不可达, 返回 {date, count:0, matches:[], error:"gq_api_unreachable"}
      而非 500.
    """
    tz8 = timezone(timedelta(hours=8))
    dstr = datetime.now(tz8).strftime("%Y-%m-%d")
    if not gqt:
        return {"date": dstr, "tz": "GMT+8", "count": 0,
                "matches": [], "error": "gq_module_unavailable"}
    try:
        tl = await asyncio.to_thread(gqt.get_today_timeline, limit)
        return tl
    except Exception as e:
        logger.warning("GQ 时间轴获取失败: %s", e)
        return {"date": dstr, "tz": "GMT+8", "count": 0,
                "matches": [], "error": "gq_api_unreachable"}


@app.get("/api/timeline/match/{mid}")
async def timeline_match_api(mid: str):
    """单场完整信息 (按需): 比分/状态/分钟/实时赔率."""
    if not gqt:
        return {"mid": mid, "found": False, "error": "gq_module_unavailable"}
    try:
        return await asyncio.to_thread(gqt.get_match_detail_api, mid)
    except Exception as e:
        logger.warning("GQ 单场获取失败 mid=%s: %s", mid, e)
        return {"mid": mid, "found": False, "error": "gq_api_unreachable"}


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


class PlaceBetRequest(BaseModel):
    home_team: str = Field(..., min_length=1)
    away_team: str = Field(..., min_length=1)
    league: str = ""
    home_odds: float = Field(..., gt=1.0)
    draw_odds: float = Field(..., gt=1.0)
    away_odds: float = Field(..., gt=1.0)
    bet_side: str
    stake_amount: float = 0.0
    confidence: float = 0.0


@app.post("/api/bets")
async def bets_place_api(request: Request):
    """手动模拟下注 (赛程页内嵌触发)。
    请求体 JSON: {home_team, away_team, league, home_odds, draw_odds, away_odds,
             bet_side('H'/'D'/'A'), stake_amount, confidence?}
    写入 bet_records (source='manual', bet_type='paper_bet')。
    """
    import sqlite3
    try:
        body = await request.json()
    except Exception:
        return _wrap_error("invalid_json", "请求体不是合法 JSON", status=400)
    try:
        req = PlaceBetRequest(**body)
    except ValidationError as e:
        return _wrap_error("validation_error", "参数校验失败", details=e.errors(), status=422)

    home, away, league = req.home_team, req.away_team, req.league
    oh, od, oa = req.home_odds, req.draw_odds, req.away_odds
    side = req.bet_side
    stake = req.stake_amount
    confidence = req.confidence

    if side not in ("H", "D", "A"):
        return _wrap_error("invalid_bet_side", "bet_side 必须为 H/D/A")

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
        # 修H2(2026-07-30): 幂等补 stake 列, 让真实注码可追溯 (原 INSERT 漏 stake 列 → 注码丢失)
        try:
            cur.execute("ALTER TABLE bet_records ADD COLUMN stake REAL DEFAULT 0")
        except Exception:
            pass
        # 修(2026-07-30 体检): 幂等唯一索引 + INSERT OR IGNORE 防重复行污染 ROI
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_bet_records ON bet_records(home_team, away_team, match_date, bet_type, source)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_bet_records_date ON bet_records(match_date, bet_type)")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute(
            """INSERT OR IGNORE INTO bet_records
               (match_id, home_team, away_team, league, bet_type, source,
                predicted_result, confidence, home_prob, draw_prob, away_prob,
                home_odds, draw_odds, away_odds, kelly, expected_value, stake, notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (None, home, away, league, "executed", "manual",
             side, confidence, ph, pd, pa,
             oh, od, oa, round(kelly_half, 4), round(kelly_full, 4), stake,
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


# ═══════════════════════════════════════════════
# 操盘终端 API (按方案第4章: 4个新接口 + 1个WebSocket)
# ═══════════════════════════════════════════════

class TerminalAnalyzeRequest(BaseModel):
    """终端分析请求 — 直接用盘口赔率(赛事列表同源), 不调 The Odds API"""
    home: str
    away: str
    sport_key: str = "soccer_fifa_world_cup"
    # 可选: 前端直接传入赔率(点击赛事卡片时带 odds_h/d/a), 避免二次查询
    odds_h: Optional[float] = None
    odds_d: Optional[float] = None
    odds_a: Optional[float] = None
    # 让球 (亚盘): 盘口 + 主客赔率。前端 fixture 已有, 一并传入 → 分析时波胆×让球交叉标注才准确
    # 注意: feed 存的 ah_line/ou_line 是字符串 (如 "+0.5/1" / "2.5"), 故用 Union[float,str] 接收,
    # 由端点内 _parse_handicap_line 解析为数值
    ah_line: Optional[Union[float, str]] = None
    ah_home: Optional[float] = None
    ah_away: Optional[float] = None
    # 大小球: 盘口 + 大小赔率
    ou_line: Optional[Union[float, str]] = None
    ou_over: Optional[float] = None
    ou_under: Optional[float] = None
    # In-play 条件概率 (可选): 前端传入当前比分时, _live_predict 启用条件 Poisson 裁剪
    home_goals: Optional[int] = None       # 主队已进球数
    away_goals: Optional[int] = None       # 客队已进球数
    elapsed: Optional[int] = None          # 已赛分钟数 (用于缩放剩余时间 λ)


class TerminalIngestRequest(BaseModel):
    """插件赔率摄入 (HTTP降级版, WebSocket优先)"""
    home: str
    away: str
    source: str = "browser_ext"


class StrategyToggleRequest(BaseModel):
    """量化模拟: 策略启用/停用"""
    strategy_id: str
    enabled: bool
    h: float
    d: float
    a: float
    score: Optional[str] = None
    minute: Optional[int] = None


@app.get("/api/terminal/matches")
async def terminal_matches_api():
    """当天可决策比赛列表 — 从 live_odds_raw 筛选有多庄赔率的比赛"""
    import sqlite3
    try:
        db_path = os.path.join(PROJECT_ROOT, "data", "football_data.db")
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        rows = conn.execute(
            """SELECT home_team, away_team, sport_key, commence_time,
                      best_h2h, bookmakers_detail, captured_at
               FROM live_odds_raw
               WHERE captured_at LIKE ? AND bookmakers_detail IS NOT NULL
               ORDER BY commence_time ASC""",
            (f"{today}%",)
        ).fetchall()
        mode = "today"
        if not rows:
            # 当日无实时采集 -> 诚实回退到最近有赔率的真实样本(非今日, 标注清楚)
            rows = conn.execute(
                """SELECT home_team, away_team, sport_key, commence_time,
                          best_h2h, bookmakers_detail, captured_at
                   FROM live_odds_raw
                   WHERE bookmakers_detail IS NOT NULL
                   ORDER BY captured_at DESC LIMIT 30"""
            ).fetchall()
            mode = "sample"
        conn.close()

        matches = []
        for r in rows:
            try:
                bm = json.loads(r['bookmakers_detail'] or '[]')
            except Exception:
                bm = []
            if len(bm) < 2:
                continue  # 必须多庄
            h2h = json.loads(r['best_h2h'] or '{}')
            league_name = LEAGUE_CATALOG.get(r['sport_key'], {}).get('name', r['sport_key'])
            matches.append({
                "home": r['home_team'], "away": r['away_team'],
                "league": league_name,
                "sport_key": r['sport_key'],
                "commence_time": r['commence_time'],
                "odds_h": h2h.get("home"), "odds_d": h2h.get("draw"), "odds_a": h2h.get("away"),
                "bookmakers_count": len(bm),
                "bookmakers": [b.get("name", "?") for b in bm[:5]],
            })

        return _wrap_data({
            "date": today,
            "matches": matches,
            "total": len(matches),
            "note": (f"当日实时比赛 (共{len(matches)}场)"
                     if mode == "today"
                     else f"实时采集暂停, 展示最近真实样本 (共{len(matches)}场, 最新 {(matches[0]['commence_time'] or 'N/A')[:10] if matches else 'N/A'})"),
        })
    except Exception as e:
        return _wrap_data({"error": f"获取失败: {e}", "matches": [], "total": 0})



def _gq_lookup_1x2(home: str, away: str):
    """events.db 按 home/away 查 1X2 最新盘口 (obscure 联赛的主数据源)。

    返回 {"h":float,"d":float,"a":float,"league":str} 或 None。
    仅当三方盘口齐全且为正时才返回, 否则 None(交给上层其他回退)。
    """
    try:
        import sqlite3, os
        db = os.path.join(PROJECT_ROOT, "data", "events.db")
        if not os.path.exists(db):
            return None
        c = sqlite3.connect(db)
        mk = c.execute("SELECT match_key, league FROM matches WHERE home=? AND away=?",
                       (home, away)).fetchone()
        if not mk:  # 主客颠倒也试一次
            mk = c.execute("SELECT match_key, league FROM matches WHERE home=? AND away=?",
                           (away, home)).fetchone()
        if not mk:
            c.close()
            return None
        match_key, league = mk[0], mk[1]
        rows = c.execute(
            "SELECT selection, odds FROM odds_snapshots WHERE match_key=? AND market='1X2' "
            "ORDER BY captured_at DESC", (match_key,)).fetchall()
        c.close()
        latest = {}
        for sel, odds in rows:
            latest.setdefault(sel, odds)
        h = latest.get("home"); d = latest.get("draw"); a = latest.get("away")
        if h and d and a and float(h) > 0 and float(d) > 0 and float(a) > 0:
            return {"h": float(h), "d": float(d), "a": float(a), "league": league}
        return None
    except Exception:
        return None


# 操盘手结论蒸馏层 (2026-08-22): 把 _live_predict 巨响应压成一行结论 + 三层支撑
from pipeline.operator_output import distill_operator_card as _distill_operator_card


def _infer_drift_intent(home: str, away: str, oh: float, od: float, oa: float):
    """单庄诱盘识别 (2026-08-30): 从 events.db 查开盘 1X2, 对比当前赔率算 drift,
    用 reverse_odds_engine.classify_intent 判定 诚实防 vs 诱盘假防。

    ⚠ 诱盘识别**不需要跨庄源**: classify_intent 靠 drift(开盘→当前漂移)三方向模式
    (H↓D↑A↑=诚实防主 / H↓D↓A↑=诱盘假防主 / ...), 单庄雷速数据有开盘+当前即够。
    """
    try:
        import sqlite3
        from analysis.live_goal_probe import _open_1x2_from_snapshots
        con = sqlite3.connect(os.path.join(PROJECT_ROOT, "data", "events.db"), timeout=30)
        try:
            o = _open_1x2_from_snapshots(con, f"{home} vs {away}")
        finally:
            con.close()
        if not o or len(o) < 3:
            return None
        o_h, o_d, o_a = float(o[0]), float(o[1]), float(o[2])
        if not (o_h > 1.01 and o_d > 1.01 and o_a > 1.01):
            return None
        from pipeline.reverse_odds_engine import OddsInput
        eng = _get_reverse_engine()
        inp = OddsInput(open_h=o_h, open_d=o_d, open_a=o_a,
                        close_h=float(oh), close_d=float(od), close_a=float(oa))
        intent, conf, pattern = eng.classify_intent(inp)
        intent_s = intent.value if hasattr(intent, 'value') else str(intent)
        is_fake = 'fake' in intent_s.lower()
        return {
            "intent": intent_s,
            "intent_confidence": round(conf, 3),
            "drift_pattern": pattern,
            "drift_h": round(float(oh) - o_h, 3),
            "drift_d": round(float(od) - o_d, 3),
            "drift_a": round(float(oa) - o_a, 3),
            "is_induce": is_fake,          # 诱盘标记(FAKE_DEF_H/A)
            "induce_label": "诱盘" if is_fake else ("诚实防" if "honest" in intent_s.lower() else "中性"),
        }
    except Exception:
        return None


@app.post("/api/terminal/analyze")
async def terminal_analyze_api(req: TerminalAnalyzeRequest):
    """赛事分析 — 直接用盘口赔率(与赛事列表同源), 不调 The Odds API.

    赔率来源优先级:
      1. 前端直接传入 (odds_h/d/a, 点击卡片时带, 零查询零延迟)
      2. events.db 按 home/away 查 1X2 (obscure 联赛主源, 覆盖 list 未带赔率的场)
      3. live_odds_raw 表模糊匹配 (按 home/away 查最近一条多庄记录, 主流联赛)
    完全去掉 The Odds API 调用 — 赛事列表有什么赔率, 分析就用什么.
    """
    import sqlite3
    try:
        oh, od, oa = req.odds_h, req.odds_d, req.odds_a
        extra_books = None
        league_name = LEAGUE_CATALOG.get(req.sport_key, {}).get('name', req.sport_key)
        commence = None

        if not (oh and od and oa and oh > 0 and od > 0 and oa > 0):
            # 前端未传赔率 → 回退链:
            #   (a) events.db 按 home/away 查 1X2 (obscure 联赛主源, 覆盖 list 未带赔率的场)
            #   (b) live_odds_raw 模糊匹配最近一条多庄记录 (主流联赛)
            gq = _gq_lookup_1x2(req.home, req.away)
            if gq:
                oh, od, oa = gq["h"], gq["d"], gq["a"]
                # GQ 单源 → 无 cross-book, extra_books 留 None (需跨庄验证edge才接盘)
                if gq.get("league"):
                    league_name = gq["league"]
            else:
                # (b) 主流联赛: 从 live_odds_raw 查最近一条多庄记录
                db_path = os.path.join(PROJECT_ROOT, "data", "football_data.db")
                conn = sqlite3.connect(db_path)
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    """SELECT home_team, away_team, sport_key, commence_time,
                              best_h2h, bookmakers_detail
                       FROM live_odds_raw
                       WHERE bookmakers_detail IS NOT NULL AND (
                           (LOWER(home_team) LIKE ? AND LOWER(away_team) LIKE ?) OR
                           (LOWER(home_team) LIKE ? AND LOWER(away_team) LIKE ?))
                       ORDER BY captured_at DESC LIMIT 1""",
                    (f"%{req.home.lower()}%", f"%{req.away.lower()}%",
                     f"%{req.away.lower()}%", f"%{req.home.lower()}%")
                ).fetchone()
                conn.close()
                if not row:
                    return _wrap_data({"error": f"未找到盘口赔率: {req.home} vs {req.away} (赛事列表无此场或赔率未采集)", "decision": None})
                h2h = json.loads(row['best_h2h'] or '{}')
                oh, od, oa = h2h.get("home"), h2h.get("draw"), h2h.get("away")
                if not (oh and od and oa):
                    return _wrap_data({"error": f"盘口赔率不完整: {req.home} vs {req.away}", "decision": None})
                try:
                    bm = json.loads(row['bookmakers_detail'] or '[]')
                    extra_books = [[b["name"], b["h"], b["d"], b["a"]] for b in bm
                                   if all(k in b for k in ("name", "h", "d", "a"))]
                except Exception:
                    extra_books = None
                if extra_books and len(extra_books) < 2:
                    extra_books = None
                req.home = row['home_team']
                req.away = row['away_team']
                commence = row['commence_time']
                req.sport_key = row['sport_key']
                league_name = LEAGUE_CATALOG.get(row['sport_key'], {}).get('name', row['sport_key'])

        # 用盘口赔率直接跑全链路模型 (不再调任何外部 API)
        # 让球/大小球: 前端 fixture 同源传入 (赛事卡片已展示的 ah_line/ou_line),
        # 喂给 _live_predict → 波胆×让球×大小球交叉标注才准确。
        # ah_line 可能是字符串盘口 "0/0.5", 取主盘数值; None 时 _live_predict 用 None
        def _parse_handicap_line(v):
            """解析盘口为数值。支持 split/quarter 盘:
            '0/0.5'->0.25, '-0/0.5'->-0.25, '+0.5/1'->0.75, '3/3.5'->3.25。
            无 '/' 时直接转 float。符号约定: 负=主让(-0.5), 正=主受让(+0.5)。"""
            if v is None:
                return None
            if isinstance(v, (int, float)):
                return float(v)
            s = str(v).strip()
            if '/' in s:
                toks = [t.strip() for t in s.split('/') if t.strip()]
                if not toks:
                    return None
                ctx_sign = -1.0 if toks[0].startswith('-') else 1.0
                vals = []
                for t in toks:
                    core = t.lstrip('+').lstrip('-')
                    try:
                        x = float(core)
                    except ValueError:
                        return None
                    if t.startswith('-'):
                        x = -abs(x)
                    elif t.startswith('+'):
                        x = abs(x)
                    else:
                        x = ctx_sign * abs(x)
                    vals.append(x)
                return sum(vals) / len(vals)
            try:
                return float(s)
            except ValueError:
                return None
        hcp_line = _parse_handicap_line(req.ah_line)
        ou_line_val = _parse_handicap_line(req.ou_line)
        result = _live_predict(
            req.home, req.away, oh, od, oa,
            home_norm=req.home, away_norm=req.away,
            date=commence, league=league_name,
            sport_key=req.sport_key,
            extra_bookmakers=extra_books,
            hcp_line=hcp_line,
            hcp_home_odds=req.ah_home,
            hcp_away_odds=req.ah_away,
            ou_line=ou_line_val,
            over_water=req.ou_over,
            under_water=req.ou_under,
            home_goals=req.home_goals, away_goals=req.away_goals, elapsed=req.elapsed,
        )

        vl = result.get("value_layer", {})
        # 单庄诱盘识别 (2026-08-30): drift 判 诚实防 vs 诱盘假防, 供前端 + 三级判定降级
        _drift_intent = _infer_drift_intent(req.home, req.away, oh, od, oa)
        _score_analysis = result.get("score_analysis")
        if _drift_intent and _drift_intent.get("is_induce") and _score_analysis:
            # 诱盘: 市场方向不可信 → 三级判定降级(定方向→软加权, 软加权→观望)并标注
            _sa = dict(_score_analysis)
            _sa["诱盘标记"] = _drift_intent
            _lv = _sa.get("级别")
            if _lv == "定方向":
                _sa["级别"] = "软加权"
                _sa["分歧标注"] = "诱盘信号, 置信度降级一档"
            elif _lv == "软加权":
                _sa["级别"] = "观望"
                _sa["方向"] = None
                _sa["分歧标注"] = "诱盘信号: 观望, 信息不足以支撑方向判断"
            _score_analysis = _sa
        card = {
            "fixture": {"home": req.home, "away": req.away,
                        "commence_time": commence, "sport_key": req.sport_key},
            "odds": result.get("odds"),
            "market_prob": result.get("market_prob"),
            "direction": result.get("direction"),
            "decision": vl.get("decision", "PASS"),
            "decision_text": vl.get("decision_text", ""),
            "best_direction": vl.get("best_direction"),
            "best_edge_pct": vl.get("best_edge_pct"),
            "rows": vl.get("rows", []),
            "softline": vl.get("softline"),
            "books_count": vl.get("books_count", 0),
            "draw_alert": result.get("draw_signal", {}).get("draw_alert"),
            "operator_view": result.get("operator_view"),
            "operator_card": _distill_operator_card(result),  # 2026-08-22 新增: 一行结论蒸馏层(治"分析太复杂/操盘手识别不了")
            "oip": result.get("oip"),
            "sub_markets": result.get("sub_markets"),
            "inplay": result.get("inplay"),  # In-play 条件概率信息
            "model_type": result.get("model_type"),
            "model_calibrated_on": result.get("model_calibrated_on"),
            "strategy_signals": result.get("strategy_signals"),  # 三方向策略信号(全联赛触发, 面板提示级)
            "strategy_tier": result.get("strategy_tier"),         # 信号溯源标签: obscure/main/cup
            "cs_drift_signal": (result.get("oip") or {}).get("cs_drift_signal"),  # 顺人性盘读数(初盘→中场收盘 drift, 全联赛纯赔率信号)
            "multibook_consensus": _lookup_multibook_consensus(req.home, req.away),  # 多庄 sharp/retail 共识(leisu 数据可用时)
            "operator_signals": _get_operator_signals(req.home, req.away, req.odds_h, req.odds_d, req.odds_a,
                                                       home_goals=req.home_goals, away_goals=req.away_goals, elapsed=req.elapsed),  # 操盘手逆转信号(已按比分条件化)
            "score_analysis": _score_analysis,     # 比分分析器三级判定(定方向/软加权/观望)
            "drift_intent": _drift_intent,         # 单庄诱盘识别(诚实防/诱盘假防)
        }
        safe_card = _json_safe(card)
        # ── 2026-08-30: 分析快照落库 (用户指令: 记录前端所有分析 → 结合赛果回训) ──
        #   快照方向/比分top3/三级判定/诱盘/置信度, 供赛后 resolve 标注命中 + 回训。
        #   不可变: 同 (match_key, phase, score) 仅首写; 失败绝不影响分析返回。
        try:
            from pipeline.analysis_snapshot import record_snapshot as _rec_snap
            _oip = result.get("oip") or {}
            _sa = _score_analysis or {}
            _phase = "live" if (req.home_goals is not None and req.away_goals is not None) else "pre"
            _cur_score = (f"{int(req.home_goals)}-{int(req.away_goals)}"
                          if (req.home_goals is not None and req.away_goals is not None) else "")
            _top3 = _oip.get("top3_scores")
            _top3p = _oip.get("top3_prob")
            _rec_snap(
                None,
                match_key=f"{req.home} vs {req.away}",
                home=req.home, away=req.away, league=league_name,
                phase=_phase, current_score=_cur_score,
                current_minute=int(req.elapsed or 0),
                odds_h=oh, odds_d=od, odds_a=oa,
                ou_line=ou_line_val, ou_over=req.ou_over, ou_under=req.ou_under,
                direction=result.get("direction"),
                market_direction=result.get("direction"),  # 模型方向=市场argmax(赛前无alpha)
                score_top1=(_top3[0] if _top3 else None),
                score_top3=([str(s) for s in _top3] if _top3 else None),
                score_top3_prob=([float(p) for p in _top3p] if _top3p else None),
                sa_level=_sa.get("级别"),
                sa_direction=_sa.get("方向"),
                sa_confidence=_sa.get("置信度"),
                sa_note=_sa.get("分歧标注"),
                induce_label=(_drift_intent or {}).get("induce_label"),
                model_tag="_live_predict",
            )
        except Exception as _se:
            logger.warning(f"[analysis_snapshot] 快照失败(不影响分析): {_se}")
        return _wrap_data(safe_card)
    except Exception as e:
        logger.error(f"终端分析失败: {e}", exc_info=True)
        return _wrap_data({"error": f"分析失败: {e}", "decision": None})


@app.post("/api/terminal/ingest")
async def terminal_ingest_api(req: TerminalIngestRequest):
    """HTTP降级版赔率摄入 (WebSocket /ws/odds_ingest 优先, 此接口为降级方案)"""
    home = req.home.strip()
    away = req.away.strip()
    match_key = f"{home.lower()}|{away.lower()}"

    book_entry = {"source": req.source, "h": req.h, "d": req.d, "a": req.a,
                  "score": req.score, "minute": req.minute,
                  "captured_at": datetime.now(timezone.utc).isoformat()}
    accum = _ODDS_INGEST_CACHE.setdefault(match_key, [])
    accum = [b for b in accum if b["source"] != req.source]
    accum.append(book_entry)
    _ODDS_INGEST_CACHE[match_key] = accum

    if len(accum) < 2:
        return _wrap_data({
            "status": "accumulating",
            "match": f"{home} vs {away}",
            "books": len(accum),
            "sources": [b["source"] for b in accum],
            "note": f"需要 >=2 家 (当前{len(accum)}家)",
        })

    try:
        best_h = min(b["h"] for b in accum)
        best_d = min(b["d"] for b in accum)
        best_a = min(b["a"] for b in accum)
        extra = [[b["source"], b["h"], b["d"], b["a"]] for b in accum]

        result = _live_predict(home, away, best_h, best_d, best_a,
                               extra_bookmakers=extra,
                               date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                               league=None)
        vl = result.get("value_layer", {})
        return _wrap_data({
            "status": "analyzed",
            "match": f"{home} vs {away}",
            "books": len(accum),
            "direction": result.get("direction"),
            "decision": vl.get("decision", "PASS"),
            "decision_text": vl.get("decision_text", ""),
            "softline": vl.get("softline"),
        })
    except Exception as e:
        logger.error(f"终端摄入分析失败: {e}")
        return _wrap_data({"status": "error", "detail": str(e)})


@app.get("/api/data-growth/stats")
async def data_growth_stats_api():
    """数据增长统计 — 行数/配额/活跃联赛/比赛覆盖"""
    try:
        from pipeline.collectors.daily_collector import DailyCollector
        dc = DailyCollector()
        stats = dc.get_growth_stats()
        return _wrap_data(stats)
    except Exception as e:
        return _wrap_data({
            "quota_remaining": None,
            "today_collected": 0,
            "active_leagues": 0,
            "odds_features_total": 0,
            "live_odds_raw_with_result": 0,
            "error": f"获取失败: {e}",
        })


@app.get("/api/quota")
async def api_quota_api():
    """The Odds API 配额与预算护栏状态 — 供操盘终端实时显示"""
    try:
        from pipeline.collectors.api_budget import get_guard
        guard = get_guard()
        status = guard.budget_status()
        # 合并两个客户端的实时 remaining (若有近期调用)
        try:
            from pipeline.collectors.sp_odds_api import SPOddsAPI
            live_rem = SPOddsAPI().get_remaining_requests()
            if live_rem and live_rem > 0:
                status["live_remaining"] = live_rem
        except Exception:
            pass
        return _wrap_data(status)
    except Exception as e:
        return _wrap_data({"error": f"配额查询失败: {e}"})


# ═══ 风控 + 报表 ═══
@app.post("/api/bet/record")
async def bet_record_api(payload: dict):
    """记录一笔下注结果"""
    try:
        from database import db
        bid = db.add_bet(
            match=payload.get("match", ""),
            outcome=payload.get("outcome", ""),
            odds=float(payload.get("odds", 0)),
            stake=float(payload.get("stake", 0)),
            result=payload.get("result", ""),
            pnl=float(payload.get("pnl", 0)),
            kelly=float(payload.get("kelly", 0)),
            ev=float(payload.get("ev", 0)),
        )
        return _wrap_data({"ok": True, "bet_id": bid})
    except Exception as e:
        return _wrap_data({"error": str(e)})


@app.get("/api/risk/status")
async def risk_status_api():
    try:
        from database import db
        eq = db.equity()
        dd = db.max_drawdown()
        ls = db.lost_streak()
        allow = dd < 0.15 and ls < 3
        reason = "OK"
        if dd >= 0.15:
            reason = f"回撤 {dd:.1%}, 停止下注"
        elif ls >= 3:
            reason = f"连黑 {ls} 场, 强制停手"
        return {
            "allow": allow, "reason": reason,
            "equity": eq, "drawdown": dd, "lost_streak": ls,
        }
    except Exception as e:
        return {"allow": True, "reason": str(e), "equity": 0, "drawdown": 0, "lost_streak": 0}


@app.get("/api/report/stats")
async def report_stats_api():
    try:
        from database import db
        return db.get_stats()
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/report/equity")
async def report_equity_api():
    try:
        from database import db
        return db.get_equity_curve()
    except Exception as e:
        return []


@app.get("/api/report/bets")
async def report_bets_api(limit: int = 100, offset: int = 0):
    try:
        from database import db
        return db.get_bets(limit=limit, offset=offset)
    except Exception as e:
        return []


@app.get("/api/report/export")
async def export_csv_api():
    try:
        import tempfile
        from database import db
        path = os.path.join(tempfile.gettempdir(), "shaoxiang_report.csv")
        db.export_csv(path)
        from fastapi.responses import FileResponse
        return FileResponse(path, filename="哨响AI_操盘报表.csv", media_type="text/csv")
    except Exception as e:
        return {"error": str(e)}


# ── 历史快照 ──
HISTORY_DIR = os.path.join(PROJECT_ROOT, "history")
os.makedirs(HISTORY_DIR, exist_ok=True)

@app.get("/api/history/list")
async def history_list_api():
    try:
        files = sorted(os.listdir(HISTORY_DIR), reverse=True)
        return files[:50]
    except Exception:
        return []


@app.post("/api/history/snapshot")
async def history_snapshot_api(payload: dict):
    try:
        match = f"{payload.get('home','?')}_{payload.get('away','?')}".replace(" ","_")
        ts = time.strftime("%Y%m%d_%H%M%S")
        path = os.path.join(HISTORY_DIR, f"{match}_{ts}.json")
        with open(path, "w", encoding="utf-8") as f:
            import json as _json
            _json.dump(payload, f, ensure_ascii=False, indent=2)
        return {"ok": True, "path": path}
    except Exception as e:
        return {"error": str(e)}


# ═══ 多盘口综合历史匹配 (Multi-Market Matcher) ═══
@app.get("/api/multi-market-match")
async def multi_market_match_api(
    h: float, d: float, a: float,
    ah_line: float | None = None, ah_h: float | None = None, ah_a: float | None = None,
    ou_line: float | None = None, ou_over: float | None = None, ou_under: float | None = None,
    top_k: int = 50,
):
    """综合 1X2+AH+OU+CS 加权历史匹配。

    返回:
      - results: top-K MatchResult 列表
      - aggregate: OutcomeAggregate (H/D/A实际率 / ROI / OU / AH方向)
      - corpus_stats: 语料统计
    """
    import json as _json
    from pipeline.multi_market_match import (
        MatchTarget, query_similar, aggregate_outcomes, load_full_corpus, corpus_stats, _sanitize,
    )
    df = load_full_corpus()
    st = corpus_stats(df)

    # 尝试从CS real-time 快照中取 top-3 CS
    cs_scores, cs_odds = [], []
    if ah_line is not None:
        import sqlite3
        try:
            c = sqlite3.connect(os.path.join(PROJECT_ROOT, "data", "events.db"))
            cs_rows = c.execute(
                "SELECT selection, odds FROM odds_snapshots WHERE market='CS' AND odds > 2.0 "
                "ORDER BY captured_at DESC LIMIT 10"
            ).fetchall()
            c.close()
            cs_scores, cs_odds = [r[0] for r in cs_rows[:3]], [r[1] for r in cs_rows[:3]]
        except Exception:
            pass

    target = MatchTarget(
        h_1x2=h, d_1x2=d, a_1x2=a,
        ah_line=ah_line, ah_h=ah_h, ah_a=ah_a,
        ou_line=ou_line, ou_over=ou_over, ou_under=ou_under,
        cs_top3_scores=cs_scores, cs_top3_odds=cs_odds,
    )
    results = query_similar(target, df=df, top_k=top_k)
    agg = aggregate_outcomes(results, target)

    return _json.loads(_json.dumps(_sanitize({
        "corpus": {"n_total": st.n_total, "n_4market": st.n_4market,
                   "n_ah": st.n_ah_present, "n_cs": st.n_cs_present,
                   "top_leagues": st.leagues},
        "target": {"h": h, "d": d, "a": a, "ah_line": ah_line, "ou_line": ou_line},
        "n_matched": len(results),
        "results": [r.__dict__ for r in results[:10]],  # 只返回前10详细
        "aggregate": {
            "n_matched": agg.n_matched, "avg_dist": agg.avg_total_dist,
            "h_rate": agg.h_rate, "d_rate": agg.d_rate, "a_rate": agg.a_rate,
            "imp_h": agg.imp_h, "imp_d": agg.imp_d, "imp_a": agg.imp_a,
            "roi_h": agg.roi_h, "roi_d": agg.roi_d, "roi_a": agg.roi_a,
            "ou_over_rate": agg.ou_over_rate, "ah_home_win_rate": agg.ah_home_win_rate,
            "top_leagues": agg.top_leagues,
            "detail_rows": agg.detail_rows,
        },
    })))


# ═══ 乐鱼实时价值投注信号 (LIVE 落地) ═══
# 2026-08-12 反推复盘: 此前该端点从未注册 handler → 前端 POST 命中 SPA GET-only 兜底 → 405,
# 导致全链路 PASS 实为工程故障. 补齐 POST handler 让信号跑通.
@app.post("/api/leyu/value-signal")
async def leyu_value_signal_api(request: Request):
    """乐鱼实时价值投注信号. evaluate 如实 PASS(分歧≈0时), 不再 405."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    home = body.get("home", "")
    away = body.get("away", "")
    oh = body.get("odds_h"); od = body.get("odds_d"); oa = body.get("odds_a")
    league = body.get("league", "")
    ah_line = body.get("ah_line"); ah_home = body.get("ah_home"); ah_away = body.get("ah_away")
    ou_line = body.get("ou_line"); ou_over = body.get("ou_over")
    use_sharp = bool(body.get("use_sharp", False))
    if not (home and away and oh and od and oa):
        return {"decision": "PASS", "reason": "missing odds/home/away", "signals": []}
    from pipeline.leyu_value_signal import evaluate
    import asyncio
    try:
        res = await asyncio.to_thread(
            evaluate, home, away, float(oh), float(od), float(oa),
            league=league, ah_line=ah_line, ah_home=ah_home, ah_away=ah_away,
            ou_line=ou_line, ou_over=ou_over, use_sharp=use_sharp)
    except Exception as _e:
        return {"decision": "ERROR", "reason": str(_e), "signals": []}
    return res


# ═══ 自主巡航 Agent 告警 — 前端轮询展示(最新在前) ═══
@app.get("/api/agent/alerts")
async def agent_alerts_api(limit: int = 50):
    """返回最近 N 条 Agent 告警(内存队列, Agent 后台循环产生)。"""
    try:
        from agent_cruise import get_recent_alerts
        alerts = get_recent_alerts(limit)
        return _wrap_data({"alerts": alerts, "count": len(alerts)})
    except Exception as e:
        return _wrap_data({"alerts": [], "count": 0, "error": str(e)})


# ═══ SPA fallback — 必须注册在所有显式路由之后 ═══



if os.path.exists(FRONTEND_DIR):
    @app.get("/{full_path:path}")
    async def serve_frontend(full_path: str = ""):
        """SPA fallback — 仅对前端路由返回 index.html; API/WS 未匹配返回 404 JSON 防前端白屏"""
        if full_path.startswith(("api/", "ws/", "docs", "openapi.json", "redoc")):
            raise HTTPException(status_code=404, detail=f"未找到 API 路径: /{full_path}")
        index_path = os.path.join(FRONTEND_DIR, "index.html")
        if os.path.exists(index_path):
            from fastapi.responses import FileResponse
            return FileResponse(index_path, headers={"Cache-Control": "no-cache"})
        return {"error": "frontend not built"}


if __name__ == "__main__":
    import uvicorn
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 9000
    host = os.getenv("API_HOST", "0.0.0.0")
    logger.info(f"启动 FootballAI Bridge @ {host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="info")
