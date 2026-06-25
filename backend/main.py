"""
哨响AI (ShXAI) - FastAPI 主应用入口 v5.0
==========================================
功能概述:
  - D-Gate v5.0 多维度平局检测系统 (四模式: A/B/C/D)
  - 操盘手模拟集成 (BookmakerTrapDetector 16引擎)
  - 世界杯/杯赛高平局率自适应阈值
  - 统一后端架构: FastAPI (原生路由) + Flask (WSGI 兼容层)
  - 微服务组件: SQLAlchemy + Celery + MLflow + Prometheus

Runtime 修复: RTX 5070 Ti CUDA sm_120 不兼容 → 强制 CPU 模式
"""
import os as _os
_os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")
import sys
import os
from datetime import datetime  # WebSocket健康推送时间戳用
# 确保项目根在sys.path中
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
import time
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
# 前端路由: v4.0 控制面板 + WebSocket 健康检查端点
from fastapi.responses import HTMLResponse
from starlette.websockets import WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, FileResponse
from a2wsgi import WSGIMiddleware

# 路径设置 — backend/ 优先（FastAPI 的 api/ 包），其次 footballAI/（Flask legacy）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # 后端目录/
sys.path.insert(1, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # 项目根目录/

from core.config import settings
from core.database import engine, Base

# ── 日志 ──────────────────────────────────
logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ── P1-2: FIFA 排名数据加载 (2026年6月官方排名) ──────────────
_FIFA_RANKINGS = {}
try:
    import json as _json_rank
    _rank_path = os.path.join(_project_root, 'config', 'fifa_rankings_2026.json')
    if os.path.exists(_rank_path):
        with open(_rank_path, 'r', encoding='utf-8') as _rf:
            _rank_data = _json_rank.load(_rf)
            _FIFA_RANKINGS = {k: v for k, v in _rank_data.items() if not k.startswith('_')}
        logger.info(f"[P1-2] FIFA排名加载: {len(_FIFA_RANKINGS)}支球队")
except Exception as _re:
    logger.warning(f"[P1-2] FIFA排名加载失败: {_re}")

def _get_fifa_rank_diff(home: str, away: str) -> int:
    """查询两队FIFA排名差 (abs值), 找不到返回None"""
    if not _FIFA_RANKINGS or not home or not away:
        return None
    r_h = _FIFA_RANKINGS.get(home) or _FIFA_RANKINGS.get(home.strip())
    r_a = _FIFA_RANKINGS.get(away) or _FIFA_RANKINGS.get(away.strip())
    if r_h is None or r_a is None:
        return None
    return abs(r_h - r_a)

# ── P3: D-Gate 统一引擎 (赛事参数分离) ──────────────────────────
from rules.d_gate_engine import apply_dgate, detect_match_type


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期 — 统一 FastAPI + Flask legacy 启动"""
    logger.info(f"[启动] {settings.APP_NAME} v{settings.APP_VERSION} 正在初始化...")
    logger.info(f"   数据库: {settings.DATABASE_URL}")
    logger.info(f"   模型目录: {settings.MODEL_DIR}")
    try:
        from core.model_registry_helper import get_active_model_version
        logger.info(f"   活跃模型版本: {get_active_model_version()}")
    except (ImportError, AttributeError):
        pass  # 模型注册表初始化失败不影响主服务启动

    # 初始化默认管理员用户（从环境变量）
    try:
        from core.security import _init_default_user
        _init_default_user()
        logger.info("   用户模块初始化完成")
    except (ValueError, KeyError, FileNotFoundError) as e:
        logger.warning(f"   用户初始化失败: {e}")

    # 启动 Prometheus 指标服务
    try:
        from utils.metrics_exporter import get_metrics_exporter
        get_metrics_exporter().start()
        logger.info("   Prometheus 指标导出已启动")
    except (ValueError, KeyError, FileNotFoundError) as e:
        logger.warning(f"   Prometheus 指标导出失败: {e}")

    # Flask 兼容层启动（数据库连接、同步线程等遗留组件）
    # Runtime修复: RTX 5070 Ti CUDA 不兼容, 改为非阻塞启动
    import threading
    flask_init = threading.Thread(target=_init_flask_startup, daemon=True)
    flask_init.start()
    # 不 join — 后台线程崩溃不影响主服务

    yield

    logger.info("👋 应用关闭中...")
    from core.database import engine
    engine.dispose()


def _init_flask_startup():
    """在后台线程执行 Flask 启动逻辑（避免阻塞异步事件循环）"""
    try:
        from flask_bridge import run_flask_startup
        run_flask_startup()
    except (ValueError, KeyError, FileNotFoundError) as e:
        logger.error(f"Flask startup 失败: {e}")


# ── 创建应用 ──────────────────────────────
app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="智能足球预测决策系统 — 微服务架构",
    docs_url=f"{settings.API_V1_PREFIX}/docs",
    redoc_url=f"{settings.API_V1_PREFIX}/redoc",
    openapi_url=f"{settings.API_V1_PREFIX}/openapi.json",
    lifespan=lifespan,
)

# ── 中间件 ────────────────────────────────

# 跨域资源共享 (CORS) 配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-Request-ID"],
)

# 可信主机白名单配置
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=["*"] if settings.DEBUG else ["localhost", "127.0.0.1"],
)


# ── 请求计时中间件 ────────────────────────
@app.middleware("http")
async def add_process_time_header(request: Request, call_next):
    start_time = time.time()
    response = await call_next(request)
    process_time = time.time() - start_time
    response.headers["X-Process-Time"] = str(round(process_time, 4))
    return response


# ── 全局异常处理 ──────────────────────────
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"未处理异常: {exc}", exc_info=True)
    if settings.DEBUG:
        return JSONResponse(
            status_code=500,
            content={"detail": str(exc), "path": str(request.url)},
        )
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )


# ── 路由注册 ──────────────────────────────
from api.v1.router import api_router
app.include_router(api_router, prefix=settings.API_V1_PREFIX)

# ── v4.1 OCR端点 (豆包火山引擎) ────────────
try:
    import importlib.util
    _ocr_spec = importlib.util.spec_from_file_location(
        'api_ocr',
        os.path.join(_project_root, 'api', 'ocr.py')
    )
    _ocr_mod = importlib.util.module_from_spec(_ocr_spec)
    sys.modules['api_ocr'] = _ocr_mod
    _ocr_spec.loader.exec_module(_ocr_mod)
    app.include_router(_ocr_mod.ocr_router)
    logger.info("OCR routes registered: POST /api/v1/ocr/upload")
except Exception as e:
    logger.warning(f"OCR routes not available: {e}")

# ── v4.1 对话/图片端点 (直接装饰器注册) ──
import json as _json, asyncio as _asyncio, re as _re
from fastapi.responses import StreamingResponse as _StreamingResponse
from fastapi import APIRouter as _APIRouter

# ── 庄家操盘视角推演生成器 ──────────────

def _build_bookmaker_report(home: str, away: str, odds: dict,
                             engine_h: float, engine_d: float, engine_a: float) -> str:
    """基于赔率+引擎概率, 生成庄家四段式操盘推演报告"""
    oh, od, oa = odds.get('home',2.0), odds.get('draw',3.2), odds.get('away',3.5)
    inv_sum = 1/oh + 1/od + 1/oa
    imp_h = (1/oh) / inv_sum
    imp_d = (1/od) / inv_sum
    imp_a = (1/oa) / inv_sum
    overround = inv_sum - 1

    # 模块1: 概率判断 — 赔率隐含 vs 引擎判断
    def _bias(eng, imp):
        gap = imp - eng
        if gap > 0.08: return '**市场严重高估**'
        if gap > 0.03: return '市场高估'
        if gap < -0.08: return '**市场严重低估**'
        if gap < -0.03: return '市场低估'
        return '基本一致'
    lines = [
        f'# 🏦 庄家操盘视角: {home} vs {away}',
        '',
        '## 🎯 模块1: 真实概率判断',
        '| 赛果 | 庄家真实判断 | 市场隐含概率 | 偏差 |',
        '|------|:---:|:---:|------|',
        f'| {home}胜 | **{engine_h:.0%}** | {imp_h:.1%} | {_bias(engine_h, imp_h)} |',
        f'| 平局 | **{engine_d:.0%}** | {imp_d:.1%} | {_bias(engine_d, imp_d)} |',
        f'| {away}胜 | **{engine_a:.0%}** | {imp_a:.1%} | {_bias(engine_a, imp_a)} |',
        f'| **抽水率** | | **{overround:.1%}** | |',
        '',
    ]

    # 模块2: 三套赔率方案
    # 方案A: 保守平衡 (引擎概率微调)
    margin_a = 0.048
    a_h = round(1/(engine_h * (1+margin_a)), 2)
    a_d = round(1/(engine_d * (1+margin_a)), 2)
    a_a = round(1/(engine_a * (1+margin_a)), 2)
    # 方案B: 激进收割 (主胜压低, 平局客胜拉高)
    margin_b = 0.087
    b_h = round(1/(engine_h * 1.15 * (1+margin_b)), 2)  # 压低主胜
    b_d = round(1/(engine_d * 0.80 * (1+margin_b)), 2)  # 压低平局
    b_a = round(1/(engine_a * 0.85 * (1+margin_b)), 2)
    # 方案C: 诱平 (主胜极低, 平局超高)
    margin_c = 0.065
    c_h = round(1/(engine_h * 1.30 * (1+margin_c)), 2)
    c_d = round(1/(engine_d * 0.55 * (1+margin_c)), 2)  # 超高平局赔率
    c_a = round(1/(engine_a * 0.70 * (1+margin_c)), 2)

    lines += [
        '## 📊 模块2: 三套赔率操盘方案',
        '',
        '### 方案A: 保守平衡 (✅ 推荐)',
        f'| 选项 | 赔率 | 隐含概率 | 设计意图 |',
        '|------|:---:|:---:|------|',
        f'| {home} | **{a_h}** | {1/a_h:.1%} | 小幅抬高赔率吸引散户, 利用市场热度制造盈利垫 |',
        f'| 平局 | **{a_d}** | {1/a_d:.1%} | 贴近真实概率, 压低平局赔付压力 |',
        f'| {away} | **{a_a}** | {1/a_a:.1%} | 承接少量专业玩家, 对冲主胜赔付 |',
        f'| **抽水率** | | **{margin_a:.1%}** | |',
        '',
        f'**心理诱导**: {home}{a_h}高于市场{oh}, 散户觉得性价比高大量买入; 平局{a_d}偏低抑制投注; {away}{a_a}保留冷门空间。',
        '',
        '### 方案B: 激进收割',
        f'| 选项 | 赔率 | 隐含概率 | 设计意图 |',
        '|------|:---:|:---:|------|',
        f'| {home} | **{b_h}** | {1/b_h:.1%} | 极度压低赔率制造强队假象, 吸引大量散户 |',
        f'| 平局 | **{b_d}** | {1/b_d:.1%} | 压缩赔率弱化平局价值, 让市场忽略平局 |',
        f'| {away} | **{b_a}** | {1/b_a:.1%} | 分流极小部分冷资金 |',
        f'| **抽水率** | | **{margin_b:.1%}** | |',
        '',
        f'**心理诱导**: 平局{b_d}视觉回报低, 玩家认定{home}稳赢; 一旦平局/{away}取胜, 平台通吃全部主胜投注。',
        '',
        '### 方案C: 激进诱平 (⚠️ 高风险)',
        f'| 选项 | 赔率 | 隐含概率 | 设计意图 |',
        '|------|:---:|:---:|------|',
        f'| {home} | **{c_h}** | {1/c_h:.1%} | 压低主胜赔率弱化吸引力 |',
        f'| 平局 | **{c_d}** | {1/c_d:.1%} | 大幅抬高平局赔率, 引诱博弈型玩家重仓 |',
        f'| {away} | **{c_a}** | {1/c_a:.1%} | 同步抬高客胜, 边缘化客胜投注 |',
        f'| **抽水率** | | **{margin_c:.1%}** | |',
        '',
        f'**心理诱导**: 平局{c_d}超高回报吸引追求高赔玩家集中买入; {home}真实胜率{engine_h:.0%}, 一旦打出主胜, 平台吃掉全部平局投注获得巨额盈余。',
        '',
    ]

    # 模块3: 三方案横向对比
    lines += [
        '## 🧠 模块3: 我会怎么选？',
        '',
        '**选择方案A**。理由:',
        '',
        '| 维度 | 方案A | 方案B | 方案C |',
        '|------|:---:|:---:|:---:|',
        '| 长期稳定盈利 | ✅ | ✅ | ❌ 平局打出亏损大 |',
        '| 不被市场识破 | ✅ | ⚠️ 平局赔率过低 | ❌ 超高平局=标准诱盘 |',
        '| 资金分布可控 | ✅ 三方均衡 | ⚠️ 倾斜主胜 | ⚠️ 集中平局 |',
        '| 任何赛果都赚 | ✅ | ✅ | ❌ 平局会大额亏损 |',
        '',
    ]

    # 模块4: 核心逻辑
    lines += [
        '## 🔑 模块4: 核心操盘底层逻辑',
        '',
        f'1. **{home}{a_h}**: 高于市面{oh}主流赔率, 利用价格优势从其他平台抢夺主胜流量;',
        f'2. **平局{a_d}**: 隐含概率{1/a_d:.1%}接近引擎{engine_d:.0%}真实判断, 严格控制平局赔付上限;',
        f'3. **{away}{a_a}**: 保留客胜博弈空间, 少量承接冷单对冲主胜巨额投注, 平衡整体赔付结构。',
        '',
        f'最终盈利来源: 固定{margin_a:.1%}抽水 + 市场与庄家真实概率的不对称盈余。',
        f'本窗口对外观感赔率更优, 长期风险更低, 三类赛果均留存稳定盈利空间。',
        '',
        '---',
        '> ⚠️ 以上为庄家操盘视角推演, 不构成投注建议。仅供理解博彩市场运作机制。',
    ]

    return '\n'.join(lines)


def _build_bookmaker_card(home: str, away: str, odds: dict,
                           engine_h: float, engine_d: float, engine_a: float,
                           d_gate_mode: str = "", ou_line: float = None,
                           handicap: float = None) -> dict:
    """构建结构化庄家操盘数据 (用于前端网格卡片渲染)
    
    v4.7: 让球盘口驱动操盘方案过滤 — 浅让盘(-0.25/-0.5)强制平局高危标记
    """
    oh, od, oa = odds.get('home',2.0), odds.get('draw',3.2), odds.get('away',3.5)
    inv_sum = 1/oh + 1/od + 1/oa
    imp_h = (1/oh) / inv_sum
    imp_d = (1/od) / inv_sum
    imp_a = (1/oa) / inv_sum
    overround = inv_sum - 1
    spread = abs(imp_h - imp_a)  # 从赔率直接计算

    def _bias(eng, imp):
        gap = imp - eng
        if gap > 0.08: return ('严重高估','danger')
        if gap > 0.03: return ('市场高估','warn')
        if gap < -0.08: return ('严重低估','safe')
        if gap < -0.03: return ('市场低估','safe')
        return ('一致','')

    # v4.7: 浅让盘平局高危检测 — 让球≤0.5=平局概率显著偏高 (必须在方案定价之前)
    is_shallow_hcap = handicap is not None and abs(handicap) <= 0.5
    dgate_active = bool(d_gate_mode)                   # D-Gate已触发
    dgate_draw_risk = d_gate_mode in ('A', 'C')        # 翻车风险(中热门A/超热门C)
    high_draw_risk = is_shallow_hcap or dgate_draw_risk  # 平局高危综合判定
    max_home_cut = 0.05 if (is_shallow_hcap or dgate_active) else 0.25  # 主胜下调约束

    # 方案A: 保守平衡
    margin_a = 0.048
    a_h, a_d, a_a = round(1/(engine_h*(1+margin_a)), 2), round(1/(engine_d*(1+margin_a)), 2), round(1/(engine_a*(1+margin_a)), 2)
    # v4.7: 平局高危场次 → 主胜赔率不得低于市场基准×0.95 (防止人为制造热度)
    if high_draw_risk and a_h < oh * (1 - max_home_cut):
        a_h = round(oh * (1 - max_home_cut), 2)
        a_d = round(1/(engine_d * (1 + margin_a)), 2)  # 平/客保持引擎驱动
        a_a = round(1/(engine_a * (1 + margin_a)), 2)
    # 方案B: 激进收割
    margin_b = 0.087
    b_h, b_d, b_a = round(1/(engine_h*1.15*(1+margin_b)), 2), round(1/(engine_d*0.80*(1+margin_b)), 2), round(1/(engine_a*0.85*(1+margin_b)), 2)
    # 方案C: 诱平
    margin_c = 0.065
    c_h, c_d, c_a = round(1/(engine_h*1.30*(1+margin_c)), 2), round(1/(engine_d*0.55*(1+margin_c)), 2), round(1/(engine_a*0.70*(1+margin_c)), 2)

    # v4.6.1: 根据分析上下文动态风险评估
    is_hot_fav = imp_h > 0.50                          # 中热门+
    is_super_fav = imp_h > 0.60                        # 大热门 (冷门收割区)
    is_balanced = abs(imp_h - imp_a) < 0.20            # 均衡赛

    # v4.7: 冷门收割场景 — 大热门+D-Gate翻车风险 → 收割逻辑优先
    cold_harvest = is_super_fav and dgate_draw_risk

    # 方案C风险: D-Gate触发时 → 诱平方案极其危险
    scheme_c_risk = 'high'
    if dgate_draw_risk:
        scheme_c_risk = 'extreme'
    elif is_balanced:
        scheme_c_risk = 'high'

    # 方案B风险: 浅让盘平局高危 → 激进收割禁用
    scheme_b_risk = 'medium'
    if is_shallow_hcap:
        scheme_b_risk = 'extreme'   # 浅让盘收割=爆亏
    elif cold_harvest:
        scheme_b_risk = 'low'
    elif is_balanced and dgate_active:
        scheme_b_risk = 'high'

    # 决策矩阵: 动态构建
    matrix_headers = ['长期盈利','不被识破','资金可控','全赛果赚']
    if dgate_active:
        matrix_headers.append('平局覆盖')
    if cold_harvest:
        matrix_headers.append('收割效率')
    if is_shallow_hcap:
        matrix_headers.append('浅让安全')

    # 浅让高危 → 强制方案A, B/C禁选
    if is_shallow_hcap:
        matrix_rows = [
            {'name':'方案A','values':['✅','✅ 贴市','✅ 均衡','✅'],'cls':'rec'},
            {'name':'方案B','values':['❌ 爆亏','❌ 浅让收割','❌ 倾斜','❌'],'cls':'danger'},
            {'name':'方案C','values':['❌ 平局亏','❌ 标准诱盘','❌ 集中','❌'],'cls':'danger'},
        ]
        if dgate_active:
            matrix_rows[0]['values'].append('✅ 覆盖')
            matrix_rows[1]['values'].append('❌ 禁忌')
            matrix_rows[2]['values'].append('❌ 致命')
        if is_shallow_hcap:
            matrix_rows[0]['values'].append('✅ 安全')
            matrix_rows[1]['values'].append('❌ 爆亏')
            matrix_rows[2]['values'].append('❌ 暴亏')
    elif cold_harvest:
        matrix_rows = [
            {'name':'方案A', 'values':['✅','✅','✅ 均衡','✅'], 'cls':'warn'},
            {'name':'方案B', 'values':['✅ 收割','✅ 热队掩护','✅ 倾斜','✅ 翻车通吃'], 'cls':'rec'},
            {'name':'方案C', 'values':['❌ 平局亏','❌ 标准诱盘','⚠️ 集中','❌'], 'cls':'danger'},
        ]
        if dgate_active:
            matrix_rows[0]['values'].append('⚠️ 被动')
            matrix_rows[1]['values'].append('✅ 主动收割')
            matrix_rows[2]['values'].append('❌ 致命')
        if cold_harvest:
            matrix_rows[0]['values'].append('⚠️ 保守')
            matrix_rows[1]['values'].append('✅ 高效')
            matrix_rows[2]['values'].append('❌ 暴亏')
    else:
        matrix_rows = [
            {'name':'方案A', 'values':['✅','✅','✅ 均衡','✅'], 'cls':'rec'},
            {'name':'方案B', 'values':['✅','⚠️ 偏低','⚠️ 倾斜','✅'], 'cls':'warn'},
            {'name':'方案C', 'values':['❌ 平局亏','❌ 标准诱盘','⚠️ 集中','❌'], 'cls':'danger'},
        ]
        if dgate_active:
            matrix_rows[0]['values'].append('✅ 保持')
            matrix_rows[1]['values'].append('⚠️ 风险')
            matrix_rows[2]['values'].append('❌ 致命')

    # 选择理由: 融合分析上下文 + 冷门收割 + 让球盘口过滤
    if is_shallow_hcap:
        choice = '方案A(约束版)'
        reasons = [
            f'浅让盘({handicap:+.1f})强制标记平局高危: 主胜定价不得低于{oh}×0.95={oh*0.95:.2f}',
            f'方案A: {home}{a_h}贴近市场{oh}, 避免人为制造热度集中',
            f'方案B/C已自动禁用: 激进压低主胜/诱平在浅让盘下必然爆亏',
        ]
    elif cold_harvest:
        choice = '方案B'
        reasons = [
            f'大热门场次(imp_H={imp_h:.0%}) + D-Gate翻车风险 → 收割窗口已打开',
            f'方案B: 压低{home}赔率至{b_h}锁死散户资金, 一旦翻车庄家通吃全部主胜投注',
            f'方案A过于保守: {margin_a:.1%}固定抽水 vs {margin_b:.1%}收割盈余, 差额{margin_b-margin_a:.1%}',
        ]
    else:
        choice = '方案A'
        reasons = [f'长期稳定盈利, 任何赛果均留存{margin_a:.1%}抽水+概率不对称盈余']
    if dgate_active:
        mode_label = {'A': '中热门翻车风险', 'B': '均衡赛平局风险', 'C': '超热门翻车风险'}.get(d_gate_mode, '平局风险')
        if cold_harvest:
            reasons.append(f'D-Gate[{mode_label}]确认翻车可能: {home}赔率{b_h}锁仓, 翻车收益最大化')
        else:
            reasons.append(f'D-Gate[{mode_label}]已激活: 方案C诱平在平局高发场景下极其危险')
    if is_hot_fav and not cold_harvest:
        reasons.append(f'中热门场次(spread={spread:.3f}): 方案A的均衡性保证庄家稳定抽水')
    if ou_line is not None and ou_line <= 2.5 and not cold_harvest:
        reasons.append(f'低OU环境(≤{ou_line}): 利好方案A控制赔付上限')
    choice_reason = '; '.join(reasons)

    # 方案风险标签: 根据上下文动态调整
    scheme_a_risk = 'low'
    scheme_a_rec = not cold_harvest  # 冷门收割时A退居备选
    scheme_b_rec = cold_harvest and not is_shallow_hcap  # 浅让盘禁用B
    scheme_b_disabled = is_shallow_hcap  # 浅让盘B方案不可用
    scheme_c_disabled = is_shallow_hcap or dgate_draw_risk

    # 冷门收割场景 → B方案文案增强
    b_psych = f'平局{b_d}视觉回报低，玩家认定{home}稳赢'
    b_home_intent = '极度压低制造假象'
    if cold_harvest:
        b_home_intent = f'压低至{b_h}锁死散户资金'
        b_psych = f'{home}{b_h}赔率极低诱使散户重仓"稳赢"幻觉; 一旦翻车, 全部主胜投注被庄家通吃'

    schemes = [
        {'id':'A','name':'保守平衡','icon':'🛡️','rec': scheme_a_rec,
         'odds':{'home':a_h,'draw':a_d,'away':a_a},'margin':f'{margin_a:.1%}',
         'home_intent':'小幅抬高吸引散户','draw_intent':'贴近真实压低赔付','away_intent':'承接冷单对冲',
         'psych':f'{home}{a_h}高于市场{oh}，散户觉得性价比高大量买入' if not is_shallow_hcap else f'{home}{a_h}贴市定价{oh}, 避免人为制造热度; 平局赔付可控',
         'risk': scheme_a_risk},
        {'id':'B','name':'激进收割','icon':'⚡','rec': scheme_b_rec, 'disabled': scheme_b_disabled,
         'odds':{'home':b_h,'draw':b_d,'away':b_a},'margin':f'{margin_b:.1%}',
         'home_intent': b_home_intent,'draw_intent':'压缩赔率弱化平局','away_intent':'分流极少冷资金',
         'psych': b_psych if not is_shallow_hcap else f'🚫 浅让盘禁用: 压低主胜在平局高危场景下必然爆亏',
         'risk': scheme_b_risk},
        {'id':'C','name':'激进诱平','icon':'⚠️','rec': False, 'disabled': scheme_c_disabled,
         'odds':{'home':c_h,'draw':c_d,'away':c_a},'margin':f'{margin_c:.1%}',
         'home_intent':'压低主胜弱化吸引','draw_intent':'大幅抬高引诱重仓','away_intent':'边缘化客胜投注',
         'psych':f'平局{c_d}超高回报吸引博弈型玩家集中买入' if not scheme_c_disabled else (
             f'🚫 已禁用: 本场{home}浅让{handicap:+.1f}, 诱平等于送钱' if handicap is not None 
             else f'🚫 已禁用: D-Gate检测到翻车风险, 诱平致命'),
         'risk': scheme_c_risk},
    ]

    # 核心逻辑: 根据场景调整
    if is_shallow_hcap:
        logic_text = (
            f'🛡️ **浅让盘防守模式**: {home}让球{handicap:+.1f}属浅让, 平局概率显著偏高。'
            f'方案A主胜定价{a_h}贴市{oh}(下调≤5%), 避免人为制造{home}热度集中。'
            f'平局{a_d}贴近市场{od}控制赔付; {away}{a_a}承接分流。'
            f'方案B/C已自动禁用 — 浅让盘激进操盘=爆亏。'
        )
    elif cold_harvest:
        logic_text = (
            f'🔥 **收割逻辑激活**: {home}隐含胜率{imp_h:.0%}, D-Gate确认翻车风险。'
            f'方案B压低{home}赔率至{b_h}制造"稳赢"幻觉锁死散户资金; '
            f'一旦打出平局或{away}胜, 庄家通吃全部{b_h}赔率仓位。'
            f'收割效率远高于方案A的{margin_a:.1%}固定抽水。'
        )
    else:
        logic_text = f'{home}{a_h}利用价格优势抢夺主胜流量; 平局{a_d}严格控制赔付上限; {away}{a_a}保留博弈空间对冲主胜投注。'
        if dgate_active:
            mode_label = {'A': '中热门翻车', 'B': '均衡赛平局', 'C': '超热门翻车'}.get(d_gate_mode, '平局')
            logic_text += f' ⚠️ D-Gate[{mode_label}]激活: 此场平局风险高于常规, 方案A的保守设计恰好规避该风险。'
        logic_text += f'最终盈利=固定{margin_a:.1%}抽水+信息不对称盈余。'

    return {
        'module1': {
            'title': '真实概率判断',
            'overround': f'{overround:.1%}',
            'rows': [
                {'outcome':f'{home}胜','engine':f'{engine_h:.0%}','implied':f'{imp_h:.1%}','bias':_bias(engine_h,imp_h)[0],'tag':_bias(engine_h,imp_h)[1]},
                {'outcome':'平局','engine':f'{engine_d:.0%}','implied':f'{imp_d:.1%}','bias':_bias(engine_d,imp_d)[0],'tag':_bias(engine_d,imp_d)[1]},
                {'outcome':f'{away}胜','engine':f'{engine_a:.0%}','implied':f'{imp_a:.1%}','bias':_bias(engine_a,imp_a)[0],'tag':_bias(engine_a,imp_a)[1]},
            ]
        },
        'module2': {
            'title': '三套赔率操盘方案',
            'schemes': schemes
        },
        'module3': {
            'title': '决策矩阵',
            'choice': choice,
            'choice_reason': choice_reason,
            'dgate_context': d_gate_mode if dgate_active else '',
            'handicap_context': f'shallow_{handicap}' if is_shallow_hcap else '',
            'matrix': {
                'headers': matrix_headers,
                'rows': matrix_rows,
            }
        },
        'module4': {
            'title': '核心操盘逻辑',
            'text': logic_text
        }
    }


def _build_analysis_card(home: str, away: str, odds: dict, 
                          h_prob: float, d_prob: float, a_prob: float,
                          handicap: float = None, ou_line: float = None,
                          water_level: float = None,
                          fifa_rank_diff: int = None, group_round: int = None,
                          match_type: str = "tournament") -> dict:
    """构建 v4.3 分析卡片 (1X2赔率 + 大小球 + 庄家信号，禁用亚赔)

    v4.9 优化 (P0+P1) + P3 赛事分离:
      - P0-1: 模式A上限 70%→60% + 必须叠加庄家风险信号
      - P0-2: 模式B 增加庄家诱盘信号要求 (原条件触发2场全错)
      - P1-1: 新增模式C 超热门翻车识别 (用庄家信号替代拿不到的近2场胜率)
      - P1-2: 预留 fifa_rank_diff/group_round 接口 (数据暂不可用)
      - P3:   赛事参数分离 tournament/league (D-Gate引擎统一调用)
    """
    oh, od, oa = odds.get('home',2), odds.get('draw',3.2), odds.get('away',3.5)
    inv_sum = 1/oh + 1/od + 1/oa
    imp_h, imp_d, imp_a = (1/oh)/inv_sum, (1/od)/inv_sum, (1/oa)/inv_sum
    spread = abs(imp_h - imp_a)
    
    # 信号分析 (仅OU + 水位, 禁止亚赔)
    signals = []
    risk = 'low'
    
    # 大小球信号
    ou_signal = None
    if ou_line is not None:
        if ou_line <= 2.0:
            ou_signal = {'type': 'draw', 'text': f'大小球极低 {ou_line:.1f}', 'detail': '极度低比分环境，利好平局'}
            signals.append('D-Boost')
        elif ou_line <= 2.5:
            ou_signal = {'type': 'draw', 'text': f'大小球偏低 {ou_line:.1f}', 'detail': '低比分环境'}
    
    # 水位信号
    water_signal = None
    if water_level is not None and water_level >= 2.0:
        water_signal = {'type': 'warn', 'text': f'水位偏高 {water_level:.2f}', 'detail': '庄家引诱下注嫌疑'}
        if risk == 'low': risk = 'medium'
    
    # 热门翻车风险 (基于赔率+低大小球)
    if oh < 1.60 and ou_signal and ou_signal['type'] == 'draw':
        signals.append('翻车风险')
        risk = 'high'
    
    # D预测 v4.3
    WC_D_RATE = 0.268
    DEFAULT_D = 0.257
    d_boosted = imp_d * (WC_D_RATE / DEFAULT_D)
    
    # 主客隐含概率差值区间判断
    if spread > 0.50: d_boosted *= 0.60
    elif 0.03 <= spread < 0.08: d_boosted *= 1.15
    else: d_boosted *= 1.08
    
    # bm skepticism (仅大小球 + 水位, 禁止亚赔)
    bm_skep = 0
    if ou_line and ou_line <= 2.0: bm_skep += 0.15
    elif ou_line and ou_line <= 2.5: bm_skep += 0.09
    if water_signal: bm_skep += 0.07
    if spread < 0.25 and ou_line and ou_line <= 2.5: bm_skep += 0.12
    
    if bm_skep > 0.15:
        d_boosted *= (1 + bm_skep * 0.5)
        h_adj = imp_h * (1 - bm_skep * 0.4)
        a_adj = imp_a * (1 - bm_skep * 0.4)
    else:
        h_adj, a_adj = imp_h, imp_a
    
    # ═════════════════════════════════════════════════════════════
    # v4.9 D-Gate (P3统一引擎: rules/d_gate_engine.py)
    # 消除L618→与L963→两处代码重复, 支持 tournament/league 参数分离
    # ═════════════════════════════════════════════════════════════
    dg = apply_dgate(
        imp_h, imp_d, imp_a, odds,
        handicap=handicap, ou_line=ou_line, water_level=water_level,
        fifa_rank_diff=fifa_rank_diff, group_round=group_round,
        match_type=match_type,
        h_adj=h_adj, a_adj=a_adj, d_boosted=d_boosted,
    )
    d_gate_active = dg['d_gate_active']
    verdict = dg['verdict']
    d_boosted = dg['d_boosted']
    
    # 核心分析点 (禁止亚赔)
    analysis_points = []
    if oh < 1.30:
        analysis_points.append({'tag': '强队', 'text': f'{home}赔率{oh}，庄家极度看好。实力碾压型比赛，但穿盘不易。', 'color': 'safe'})
    elif oh < 2.0:
        analysis_points.append({'tag': '热门', 'text': f'{home}赔率{oh}，热门但不稳。世界杯小组赛此类赔率翻车率超30%。', 'color': 'warn'})
    
    if spread < 0.16 and ou_line and ou_line <= 2.5:
        analysis_points.append({'tag': '平局候选', 'text': f'均衡对战(spread={spread:.2f})+低比分环境(OU{ou_line})，经典平局候选。', 'color': 'draw'})
    
    if ou_signal:
        analysis_points.append({'tag': '大小球', 'text': ou_signal['detail'], 'color': 'draw'})
    
    if water_signal:
        analysis_points.append({'tag': '水位', 'text': water_signal['detail'], 'color': 'warn'})
    
    if signals:
        analysis_points.append({'tag': '信号', 'text': ' | '.join(signals), 'color': 'warn' if '翻车风险' in signals else 'info'})
    
    # ── 庄家动机 (禁止亚赔，仅1X2+OU+水位分析) ──
    motives = []
    
    # 模式1: 强队低大小球 — 庄家通过压低大小球暗示沉闷比赛
    if oh < 1.60 and ou_line and ou_line <= 2.5:
        motives.append(
            f'{home}赔率{oh}表面强势，但庄家把大小球压低到{ou_line}，暴露了真实判断。'
            f'赔率给散户信心，大小球线给庄家自己留后路——万一{home}陷入苦战打铁，'
            f'低进球预期确保庄家在大小球方向有利润。\"赔率看好、进球数不看好\"的矛盾信号值得警惕。'
        )
    
    # 模式2: 低大小球+均衡 — 庄家预判沉闷平局
    if ou_line and ou_line <= 2.5 and spread < 0.25:
        motives.append(
            f'大小球仅开{ou_line}，是庄家对沉闷比赛的直接预判。'
            f'两队实力均衡(spread={spread:.1%})，庄家将进球预期压到{ou_line}，'
            f'暗示这将是一场低比分的消耗战——而低比分是平局的温床。'
            f'如果庄家真的看好一方获胜，进球线不会这么低。'
        )
    
    # 模式3: 高水位诱盘 — 庄家引诱单边投注
    if water_level and water_level >= 2.0:
        motives.append(
            f'水位定在{water_level}是一个微妙的信号。高水位意味着庄家需要更多投注来平衡风险，'
            f'或者庄家本身对热门方缺乏信心。正常情况下信心充足的盘口水位应在1.85-1.95区间。'
            f'{water_level}的水位=庄家在说\"快来买这个方向\"——通常不是好事。'
        )
    
    # 模式4 (v4.5): 均衡赛事平局陷阱 — 庄家无法明确看好任一方
    if spread < 0.16 and 3.0 <= od <= 4.5 and ou_line and ou_line <= 2.5:
        motives.append(
            f'⚖️ 均衡赛平局信号: spread={spread:.2f}（高度接近），'
            f'主赔{oh} vs 客赔{oa}差距极小，庄家无法明确看好任何一方。'
            f'平赔{od}处于中位区间[3.0,4.5]，说明庄家对平局结果有合理预期。'
            f'在世界杯/大赛小组赛阶段，此类"势均力敌"的比赛平局率显著高于普通赛事——'
            f'因为双方都倾向于"保平争胜"的保守策略，尤其是首轮比赛。'
        )

    # 默认: 标准盘
    if not motives:
        if oh < 2.5:
            motives.append(
                f'庄家对这场比赛的赔率结构较为标准，{home}赔率{oh}、平赔{od}、客赔{oa}，'
                f'抽水率约{(1/oh+1/od+1/oa-1)*100:.1f}%。赔率没有明显异常信号，'
                f'庄家主要依靠自然的市场投注分布来维持账面平衡。'
            )
        else:
            motives.append(
                f'这场比赛的赔率结构较为均衡，三线赔率差距不大，'
                f'庄家没有明显倾向性，主要通过精细的赔率微调来平衡各方投注。'
                f'此类比赛庄家利润最薄，但也最安全——任何结果都不会造成巨大损失。'
            )
    
    # 构建最终卡片
    verdict_map = {'H': '主胜', 'D': '平局', 'A': '客胜'}
    return {
        'verdict': verdict,
        'verdict_cn': verdict_map.get(verdict, '?'),
        'risk': risk,
        'signals': signals,
        'probs': {
            'home': round(imp_h, 3), 'draw': round(imp_d, 3), 'away': round(imp_a, 3),
            'd_boosted': round(d_boosted, 3), 'h_adj': round(h_adj, 3), 'a_adj': round(a_adj, 3),
        },
        'odds': {'home': oh, 'draw': od, 'away': oa},
        'ou_line': ou_line,
        'water_level': water_level,
        'ou_signal': ou_signal,
        'water_signal': water_signal,
        'analysis': analysis_points,
        'skepticism': round(bm_skep, 2),
        'spread': round(spread, 3),
        'd_gate_active': d_gate_active,
        'motives': motives,
    }


@app.post("/api/v1/chat")
async def chat_endpoint(request: Request):
    """文本对话 — SSE流式"""
    try:
        raw_body = await request.body()
        body = _json.loads(raw_body.decode('utf-8'))
    except (ValueError, TypeError, UnicodeDecodeError):
        body = {}
    msg = body.get("message", "")
    _match_type = detect_match_type(msg)  # P3: 赛事类型检测 (tournament/league)
    logger.info(f"[Chat] Received msg={msg[:40]!r} len={len(msg)} match_type={_match_type}")

    async def generate():
        _init_msg = _json.dumps({'type':'text','content':'🔍 哨响AI v4.1 分析中...\n\n'})
        yield f"data: {_init_msg}\n\n"

        # ════════════════════════════════════════════════
        # 实时数据查询意图识别 (v4.8新增)
        # 用户问"今晚有什么比赛"/"赛程"/"积分榜"/"射手榜"时直接返回API数据
        # ════════════════════════════════════════════════
        _msg_lower = msg.lower().strip()
        _live_keywords = ['今晚', '今天', '赛程', 'fixtures', 'fixture', '什么比赛', '有哪些比赛', '接下来', 'scheduled', 'upcoming']
        _standings_keywords = ['积分', '积分榜', 'standings', '排名', '小组排名', 'group']
        _scorers_keywords = ['射手', '射手榜', 'scorers', '进球榜', '谁进', 'top scorer']
        _live_now_keywords = ['直播', 'live', '正在进行', 'in play']

        if any(kw in _msg_lower for kw in _live_keywords + _standings_keywords + _scorers_keywords + _live_now_keywords):
            try:
                from data_collector.football_data_live import FootballDataLive
                fdl = FootballDataLive()
                reply_parts = []

                if any(kw in _msg_lower for kw in _live_now_keywords):
                    live = fdl.get_live_scores()
                    reply_parts.append(f"📡 实时直播 ({len(live)}场)\n")
                    for m in live[:10]:
                        sc = m.get('score', {})
                        ft = sc.get('fullTime', {})
                        ht = sc.get('halfTime', {})
                        h = m.get('homeTeam', {}).get('name', '?')
                        a = m.get('awayTeam', {}).get('name', '?')
                        reply_parts.append(f"  {h} {ft.get('home','?')}-{ft.get('away','?')} {a} (HT {ht.get('home','?')}-{ht.get('away','?')})\n")

                if any(kw in _msg_lower for kw in _live_keywords):
                    fixtures = fdl.get_wc2026_fixtures()
                    reply_parts.append(f"\n📅 待赛赛程 ({len(fixtures)}场)\n")
                    for f in fixtures[:15]:
                        date = f.get('utcDate', '')[:16].replace('T', ' ')
                        h = f.get('homeTeam', {}).get('name', '?')
                        a = f.get('awayTeam', {}).get('name', '?')
                        group = f.get('group', '').replace('GROUP_', '') if f.get('group') else ''
                        reply_parts.append(f"  {date} {h} vs {a} {group}\n")

                if any(kw in _msg_lower for kw in _standings_keywords):
                    standings = fdl.get_wc2026_standings()
                    reply_parts.append(f"\n🏆 积分榜 ({len(standings)}个组)\n")
                    for s in standings:
                        group_name = s.get('group', '?')
                        reply_parts.append(f"  [{group_name}]\n")
                        for t in s.get('table', [])[:4]:
                            team = t.get('team', {}).get('name', '?')
                            pts = t.get('points', 0)
                            gd = t.get('goalDifference', 0)
                            reply_parts.append(f"    {t.get('position','?')}. {team} {pts}pts (GD{gd:+d})\n")

                if any(kw in _msg_lower for kw in _scorers_keywords):
                    scorers = fdl.get_wc2026_scorers()
                    reply_parts.append(f"\n⚽ 射手榜 Top 10\n")
                    for i, s in enumerate(scorers[:10], 1):
                        name = s.get('player', {}).get('name', '?')
                        team = s.get('team', {}).get('name', '?')
                        goals = s.get('goals', 0)
                        reply_parts.append(f"  {i}. {name} ({team}) {goals}球\n")

                if reply_parts:
                    full_reply = ''.join(reply_parts)
                    for chunk in [full_reply[i:i+300] for i in range(0, len(full_reply), 300)]:
                        yield f"data: {_json.dumps({'type':'text','content':chunk})}\n\n"
                        await _asyncio.sleep(0.02)
                    yield f"data: {_json.dumps({'type':'done'})}\n\n"
                    return
            except Exception as e:
                logger.warning(f"[Chat] 实时数据查询失败: {e}")

        # ════════════════════════════════════════════════
        # Trend/Form 自动注入: 当识别到vs对阵时, 自动获取真实战绩+λ
        # ════════════════════════════════════════════════
        _form_teams = _re.findall(r'(.+?)\s+(?:vs|VS|对)\s+(.+?)(?:\s+\d|$)', msg)
        if _form_teams:
            _fh = _form_teams[0][0].strip()
            _fa = _form_teams[0][1].strip()
            # 去掉赔率数字
            _fh = _re.sub(r'\s*\d+\.\d+.*$', '', _fh).strip()
            _fa = _re.sub(r'\s*\d+\.\d+.*$', '', _fa).strip()
            try:
                from data_collector.football_data_live import FootballDataLive
                _fdl = FootballDataLive()
                _form_report = _fdl.format_form_report(_fh, _fa)
                if '战绩数据不足' not in _form_report and 'error' not in _form_report.lower():
                    yield f"data: {_json.dumps({'type':'text','content':_form_report + chr(10) + chr(10)})}\n\n"
                    await _asyncio.sleep(0.01)
            except Exception as _fe:
                logger.debug(f"[Chat] Trend/Form获取失败: {_fe}")

        try:
            # 临时移除 backend 路径, 避免 shadow 项目级 core/ 包
            _saved_path = [p for p in sys.path if 'backend' in p and 'Architecture' in p]
            for _p in _saved_path: sys.path.remove(_p)
            from six_layer_conversation import SixLayerConversationEngine
            # 恢复路径
            for _p in _saved_path: sys.path.insert(0, _p)
            # 队名解析: 支持两种格式
            # 格式1: "巴西 vs 阿根廷 2.10 3.30 3.60" (标准)
            # 格式2: "荷兰 1.55 2.72 4.35" (OCR单队名+三元组)

            # 先提取所有数字 (必须在此处定义, 后续两个分支都依赖)
            odds_match = _re.findall(r'(\d+\.\d+)', msg)

            teams = _re.findall(r'(.+?)\s+(?:vs|VS|对)\s+(.+?)(?:\s|\$|，|,)', msg)
            home = teams[0][0].strip() if teams else ""
            away = teams[0][1].strip() if teams else ""

            # 格式2兜底: 无 vs 但有"中文/英文单词 + 3个数字"模式
            if not home:
                single_team = _re.match(r'^([\u4e00-\u9fffA-Za-z\s]{1,20})\s+(\d+\.\d+)\s+(\d+\.\d+)\s+(\d+\.\d+)', msg.strip())
                if single_team:
                    home = single_team.group(1).strip()
                    away = "?"
                    # 覆盖为精确三元组(避免混入其他数字如水位/日期)
                    odds_match = [single_team.group(2), single_team.group(3), single_team.group(4)]

            odds = None
            if len(odds_match) >= 3:
                odds = {'home': float(odds_match[0]), 'draw': float(odds_match[1]), 'away': float(odds_match[2])}
            
            # 提取亚盘/大小球/水位 (可选字段)
            handicap = None
            ou_line = None
            water_level = None
            over_water = 1.90  # 默认大小球水位
            under_water = 1.92
            # 路径1: 从数字序列直接提取 (需6+数字: H D A HC OU WL)
            if len(odds_match) >= 6:
                handicap = float(odds_match[3])
                ou_line = float(odds_match[4])
                water_level = float(odds_match[5])
            # 路径1b: 5个数字 (H D A HC OU, 无水位)
            elif len(odds_match) >= 5:
                handicap = float(odds_match[3])
                ou_line = float(odds_match[4])
            # 路径1c: 4个数字 (H D A HC, 无OU无水位) — 仅当消息含"让"关键字时
            elif len(odds_match) >= 4 and '让' in msg:
                handicap = float(odds_match[3])
            # 路径1d: 从"让X.X"关键字解析 (覆盖让球盘口, 优先级高于数字序列)
            if '让' in msg:
                _hc_match = _re.search(r'让\s*(\d+\.?\d*)', msg)
                if _hc_match:
                    try:
                        handicap = float(_hc_match.group(1))
                    except (ValueError, TypeError):
                        pass
            # 路径2: 从 | 分隔的辅助信息中解析 "大2.5" / "小2.5" / "大2/2.5" 等格式
            if ou_line is None:
                # 优先匹配: 大X.X / 大2/2.5 → 取/后的X.X
                ou_match = _re.search(r'[大小].*?/(\d+\.\d+)', msg) or _re.search(r'[大小]\D*?(\d+\.\d+)', msg)
                if ou_match:
                    try:
                        ou_line = float(ou_match.group(1))
                    except (ValueError, TypeError):
                        pass
                if ou_line is None:
                    ou_match = _re.search(r'[大小]\D*?(\d+)', msg)
                    if ou_match:
                        try:
                            ou_line = float(ou_match.group(1))
                        except (ValueError, TypeError):
                            pass
            # v4.7: 将让球/大小球注入odds dict → six_layer → UnifiedPredictor
            if odds:
                odds['_handicap'] = handicap or 0.0
                odds['_ou_line'] = ou_line or 2.5
                odds['_over_water'] = over_water
                odds['_under_water'] = under_water
            engine = SixLayerConversationEngine(enable_l6=False)
            result = engine.process(msg, home, away, "世界杯" if not home else "", odds)

            # 基础6层报告
            report = result.analysis_report
            for chunk in [report[i:i+300] for i in range(0, len(report), 300)]:
                yield f"data: {_json.dumps({'type':'text','content':chunk})}\n\n"
                await _asyncio.sleep(0.02)

            # 庄家操盘视角推演 (当有完整 odds + vs 时触发)
            if home and away and odds and len(odds) >= 3:
                bm_report = _build_bookmaker_report(home, away, odds, result.h_prob, result.d_prob, result.a_prob)
                for chunk in [bm_report[i:i+300] for i in range(0, len(bm_report), 300)]:
                    yield f"data: {_json.dumps({'type':'text','content':chunk})}\n\n"
                    await _asyncio.sleep(0.01)
            # ════════════════════════════════════════════════
            # D-Gate v4.7 (独立陷阱检测层 — 不改变预测,仅标记提示)
            # ════════════════════════════════════════════════
            hp, dp, ap = result.h_prob, result.d_prob, result.a_prob
            d_gate_active = False
            d_gate_mode = ""

            if odds and len(odds) >= 3:
                oh_p, od_p, oa_p = odds.get('home',2), odds.get('draw',3.2), odds.get('away',3.5)
                inv_p = 1/oh_p + 1/od_p + 1/oa_p
                imp_h = 1/oh_p/inv_p; imp_d = 1/od_p/inv_p; imp_a = 1/oa_p/inv_p
                max_imp = max(imp_h, imp_a)
                _spread_p = abs(imp_h - imp_a)
                # P3: D概率boost估算 (与_build_analysis_card一致)
                _d_boost_est = imp_d * (0.268 / 0.257)
                if _spread_p > 0.50: _d_boost_est *= 0.60
                elif 0.03 <= _spread_p < 0.08: _d_boost_est *= 1.15
                else: _d_boost_est *= 1.08

                # ════════════════════════════════════════════════
                # v4.9 D-Gate (P3统一引擎: rules/d_gate_engine.py)
                # ════════════════════════════════════════════════
                dg = apply_dgate(
                    imp_h, imp_d, imp_a, odds,
                    handicap=handicap, ou_line=ou_line, water_level=water_level,
                    fifa_rank_diff=None, group_round=None,
                    match_type=_match_type,
                    h_adj=None, a_adj=None, d_boosted=_d_boost_est,
                )
                d_gate_active = dg['d_gate_active']
                d_gate_mode = dg['d_gate_mode']
                prediction = (
                    "平局" if dg['verdict'] == 'D'
                    else "主胜" if dg['verdict'] == 'H'
                    else "客胜"
                )
            else:
                # 无赔率数据时回退到模型概率判型
                if dp > 0.28 and dp > max(hp, ap) * 0.85:
                    prediction = "平局"
                elif hp > ap:
                    prediction = "主胜"
                else:
                    prediction = "客胜"

            # 构建预测卡片
            card = {"home":home or "?","away":away or "?",
                    "h_prob":round(hp,4),"d_prob":round(dp,4),"a_prob":round(ap,4),
                    "d_gate":result.d_gate_result or "","time_ms":round(result.total_time_ms,1),
                    "prediction": prediction, "d_gate_active": d_gate_active,
                    "d_gate_mode": d_gate_mode,
                    "match_type": _match_type,  # P3: 赛事类型
                   }

            # v4.6.1: 冷启动降级 — 当模型概率为0时用赔率反推
            if hp == 0 and dp == 0 and ap == 0 and odds:
                oh2, od2, oa2 = odds.get('home',2.0), odds.get('draw',3.2), odds.get('away',3.5)
                inv_fb = 1/oh2 + 1/od2 + 1/oa2
                card['h_prob'] = round(1/oh2/inv_fb, 4)
                card['d_prob'] = round(1/od2/inv_fb, 4)
                card['a_prob'] = round(1/oa2/inv_fb, 4)
                logger.info(f"[Chat] 冷启动降级: H={card['h_prob']:.3f} D={card['d_prob']:.3f} A={card['a_prob']:.3f}")

            # D-Gate覆盖时, 重写d_gate描述 + 修正risk_tags
            if d_gate_active and d_gate_mode:
                from six_layer_conversation import SixLayerConversationEngine
                engine_ref = SixLayerConversationEngine.__new__(SixLayerConversationEngine)
                if hp == 0 and dp == 0 and ap == 0 and odds:
                    oh2, od2, oa2 = odds.get('home',2.0), odds.get('draw',3.2), odds.get('away',3.5)
                    inv_use = 1/oh2 + 1/od2 + 1/oa2
                    hp_use = 1/oh2/inv_use; dp_use = 1/od2/inv_use; ap_use = 1/oa2/inv_use
                else:
                    hp_use, dp_use, ap_use = hp, dp, ap
                card['d_gate'] = engine_ref._apply_d_gate(
                    hp_use, dp_use, ap_use,
                    d_gate_override=True, gate_mode=d_gate_mode
                )
                card['risk_tags'] = [f'd_gate_{d_gate_mode}']
            else:
                d_margin = dp - max(hp, ap)
                if d_margin < 0.02:
                    card['risk_tags'] = ['d_gate_junk']
                elif d_margin < 0.05:
                    card['risk_tags'] = ['d_gate_fuzzy']
                else:
                    card['risk_tags'] = []

            # ════════════════════════════════════════════════
            # 独立陷阱检测层 (检测→标记→提示, 不改变预测)
            # ════════════════════════════════════════════════
            trap_warnings = []
            if home and away and odds and len(odds) >= 3:
                try:
                    from bookmaker_sim.bookmaker_trap_detector import BookmakerTrapDetector as _BTD
                    _trap_det = _BTD()
                    _trap_rpt = _trap_det.detect({
                        "home": home, "away": away, "league": "世界杯",
                        "odds_h": odds.get('home', 2.0),
                        "odds_d": odds.get('draw', 3.2),
                        "odds_a": odds.get('away', 3.5),
                        "asian_handicap": handicap,
                        "water_level": water_level or 0.92,
                    })
                    # 提取所有信号, 转为可读警告
                    for sig in _trap_rpt.signals:
                        trap_warnings.append({
                            "type": sig.trap_type.value,
                            "confidence": round(sig.confidence, 2),
                            "direction": sig.direction,
                            "description": sig.description,
                        })
                    # 陷阱总分
                    card['trap_score'] = round(_trap_rpt.aggregate_score, 1)
                    card['trap_recommendation'] = _trap_rpt.recommendation
                    logger.info(f"[Trap] {home} vs {away}: {len(trap_warnings)}个信号, 总分={_trap_rpt.aggregate_score:.1f}")
                except (ImportError, AttributeError, ValueError) as e:
                    logger.debug(f"[Trap] 检测跳过: {e}")
                    pass
            card['trap_warnings'] = trap_warnings

            # ════════════════════════════════════════════════
            # v4.2: 风控联动标签 risk_tag (统一上层风控判定)
            # ════════════════════════════════════════════════
            # 优先级 (高→低):
            #   1. 陷阱检测 ignore_draw + draw_margin弱 → ignore_draw (庄家意图压倒一切)
            #   2. 陷阱检测 ignore_draw → weak_ignore_draw
            #   3. D-Gate 激活 → favor_draw (模型看好平局)
            #   4. draw_margin < -0.10 → weak_draw
            #   5. 其他 → neutral
            # 关键设计: 庄家意图(陷阱检测) > 赔率结构统计(D-Gate), 因为
            # 当庄家在诱平时, D-Gate 的"均衡赔率利好平局"恰恰是陷阱的表现
            has_ignore_draw_trap = any(t.get('direction') == 'ignore_draw' for t in trap_warnings)
            _draw_margin = dp - max(hp, ap)

            if has_ignore_draw_trap and _draw_margin < -0.05:
                card['risk_tag'] = 'ignore_draw'
                card['draw_punish_rate'] = 0.3  # 强惩罚：原始概率×0.3
                card['risk_tag_reason'] = (f"陷阱检测诱平信号(置信度"
                    f"{max([t.get('confidence',0) for t in trap_warnings if t.get('direction')=='ignore_draw'], default=0)*100:.0f}%)"
                    f"+平局边际({_draw_margin:+.3f})弱, 平局价值极低 — 庄家意图压倒D-Gate赔率统计")
                # 关键: 即使D-Gate激活, 也要覆盖prediction为非平局
                if d_gate_active:
                    # D-Gate被陷阱压倒, 切回模型原始预测
                    if hp > ap:
                        prediction = "主胜"
                    else:
                        prediction = "客胜"
                    d_gate_active = False  # 取消D-Gate激活标记
                    card['d_gate_active'] = False
                    card['d_gate'] = f"[陷阱压倒] D-Gate原判断被诱平信号覆盖, 切回{prediction}"
                    logger.info(f"[RiskTag] D-Gate被陷阱压倒, 切回prediction={prediction}")
            elif has_ignore_draw_trap:
                card['risk_tag'] = 'weak_ignore_draw'
                card['draw_punish_rate'] = 0.5  # 中度惩罚
                card['risk_tag_reason'] = f"陷阱检测诱平信号, 平局边际{_draw_margin:+.3f}, 平局价值偏低"
                if d_gate_active:
                    # 弱惩罚场景下D-Gate仍可保留, 但标注冲突
                    card['d_gate'] += " [⚠️与诱平信号冲突, 平局比分已降权]"
            elif d_gate_active:
                card['risk_tag'] = 'favor_draw'
                card['draw_punish_rate'] = 1.0  # 看好平局，不惩罚
                card['risk_tag_reason'] = f"D-Gate v4.7 模式{d_gate_mode}激活, 模型看好平局"
            elif _draw_margin < -0.10:
                card['risk_tag'] = 'weak_draw'
                card['draw_punish_rate'] = 0.7  # 轻度惩罚
                card['risk_tag_reason'] = f"平局边际{_draw_margin:+.3f}显著为负, 平局概率偏低"
            else:
                card['risk_tag'] = 'neutral'
                card['draw_punish_rate'] = 1.0
                card['risk_tag_reason'] = ""
            logger.info(f"[RiskTag] {home} vs {away}: tag={card['risk_tag']}, punish={card['draw_punish_rate']}, margin={_draw_margin:+.3f}, dgate={d_gate_active}")

            # 市场隐含概率 (从赔率反推)
            if odds and len(odds) >= 3:
                oh2, od2, oa2 = odds.get('home',2), odds.get('draw',3.2), odds.get('away',3.5)
                inv = 1/oh2 + 1/od2 + 1/oa2
                card['implied'] = {
                    'home': round(1/oh2/inv, 3), 'draw': round(1/od2/inv, 3), 'away': round(1/oa2/inv, 3)
                }

            # 泊松比分预测 (v4.2: 风控联动惩罚)
            if result.h_prob + result.d_prob + result.a_prob > 0:
                try:
                    from optimize.poisson_predictor import PoissonPredictor
                    pp = PoissonPredictor()
                    scores_raw = pp.predict_scores(result.h_prob, result.d_prob, result.a_prob, "default", 3)
                    if scores_raw:
                        # v4.2: 风控联动 — 对平局比分施加惩罚系数
                        _punish = card.get('draw_punish_rate', 1.0)
                        _risk_tag = card.get('risk_tag', 'neutral')
                        _processed = []
                        for s in scores_raw[:5]:  # 取前5个候选
                            raw_p = s.get('probability', 0)
                            score_str = str(s.get('score', '?'))
                            try:
                                _h, _a = score_str.split('-')
                                _is_draw = (_h == _a)
                            except (ValueError, AttributeError):
                                _is_draw = False

                            if _is_draw and _risk_tag in ('ignore_draw', 'weak_ignore_draw'):
                                # 平局惩罚: 原始概率×punish_rate
                                eff_p = raw_p * _punish
                                _tag = '风控低参考'
                                _star = 0  # 强制降星
                            elif _is_draw and _risk_tag == 'weak_draw':
                                eff_p = raw_p * _punish
                                _tag = '平局概率偏低'
                                _star = max(0, 1 if raw_p > 0.08 else 0)
                            elif _is_draw and _risk_tag == 'favor_draw':
                                eff_p = raw_p  # 看好平局，不惩罚
                                _tag = '模型看好'
                                _star = 3
                            else:
                                eff_p = raw_p
                                _tag = ''
                                _star = 0

                            _processed.append({
                                'score': score_str,
                                'prob': f"{eff_p:.1%}",
                                'raw_prob': raw_p,
                                'eff_prob': eff_p,
                                'outcome': s.get('outcome', '?'),
                                'is_draw': _is_draw,
                                'tag': _tag,
                                'star': _star,
                            })

                        # 按修正后概率重新降序排序
                        _processed.sort(key=lambda x: x['eff_prob'], reverse=True)

                        # 重新计算星级: Top3 给3/2/1星 (除非已被风控降星)
                        for idx, item in enumerate(_processed[:3]):
                            if item['star'] == 0 and not item.get('tag'):
                                item['star'] = 3 - idx

                        card['scores'] = _processed[:3]
                        logger.info(f"[ScorePred] 风控联动完成: risk_tag={_risk_tag}, punish={_punish}, "
                                    f"top3={[(s['score'], s['prob'], s.get('tag','')) for s in card['scores']]}")
                except (ImportError, ValueError, KeyError) as e:
                    logger.debug(f"[ScorePred] 比分预测跳过: {e}")
                    pass

            # v4.3 分析卡片 (含盘口/OU/水位信号)
            if home and away and odds and len(odds) >= 3:
                try:
                    # P1-2: 查询FIFA排名差 (小组轮次暂不接入, 需赛事上下文)
                    _fifa_diff = _get_fifa_rank_diff(home, away)
                    card['analysis'] = _build_analysis_card(home, away, odds, 
                        result.h_prob, result.d_prob, result.a_prob,
                        handicap, ou_line, water_level,
                        fifa_rank_diff=_fifa_diff, match_type=_match_type)
                    card['bookmaker'] = _build_bookmaker_card(
                        home, away, odds, result.h_prob, result.d_prob, result.a_prob,
                        d_gate_mode=d_gate_mode, ou_line=ou_line, handicap=handicap)
                except Exception as e:
                    logger.warning(f"[Chat] bookmaker_card build failed: {e}", exc_info=True)
                    pass

            yield f"data: {_json.dumps({'type':'predict_card','data':card})}\n\n"
        except Exception as e:
            yield f"data: {_json.dumps({'type':'error','content':str(e)})}\n\n"
        yield f"data: {_json.dumps({'type':'done'})}\n\n"
    return _StreamingResponse(generate(), media_type="text/event-stream")

@app.get("/api/v1/chat/health")
async def chat_health():
    return {"status":"ok","version":"v4.1"}

# ── v5.0 JEPA 预测端点 ──
@app.post("/api/v1/v5/predict")
async def v5_predict(request: Request):
    """v5.0 JEPA World Model 预测 (Phase 1: 仅JEPA, 后续整合Stacking)"""
    try:
        body = await request.json()
        home_odds = float(body.get('home_odds', 2.0))
        draw_odds = float(body.get('draw_odds', 3.5))
        away_odds = float(body.get('away_odds', 3.0))
        home_team = body.get('home_team', '')
        away_team = body.get('away_team', '')
        league = body.get('league', '')
        
        # 动态加载 v5 predictor (已合并到 v4.0 项目内)
        from predictors.jepa_predictor import quick_predict
        
        result = quick_predict(home_team, away_team, league, home_odds, draw_odds, away_odds)
        
        return JSONResponse({
            'success': True,
            'version': 'v5.0-alpha',
            'prediction': result['prediction'],
            'probabilities': {
                'home': round(float(result['probabilities'][0]), 4),
                'draw': round(float(result['probabilities'][1]), 4),
                'away': round(float(result['probabilities'][2]), 4),
            },
            'confidence': round(result['confidence'], 4),
            'source': result['source'],
            'draw_signal': result['draw_signal'],
        })
    except Exception as e:
        return JSONResponse({'success': False, 'error': str(e)}, status_code=500)

@app.get("/api/v1/v5/health")
async def v5_health():
    """v5.0 健康检查"""
    try:
        from models.jepa import FootballJEPA
        from models.jepa import FootballJEPA
        m = FootballJEPA()
        return {"status": "ok", "version": "v5.0-alpha", "params": sum(p.numel() for p in m.parameters())}
    except Exception as e:
        return {"status": "degraded", "error": str(e)}

@app.get("/api/v1/fixtures/upcoming")
async def upcoming_fixtures():
    """获取今天+明天的世界杯赛程 (前端快速按钮)
    
    分界线: 北京时间 12:00 (每日中午刷新)
    - "今天" = 当前时刻 → 最近一个12:00
    - "明天" = 最近12:00 → 下下个12:00 (覆盖约38h)
    """
    try:
        from data_collector.football_data_live import FootballDataLive
        from datetime import datetime, timezone, timedelta
        fdl = FootballDataLive()
        fixtures = fdl.get_wc2026_fixtures()
        finished = fdl.get_wc2026_finished()

        # 已完赛比赛 (id → score 映射)
        finished_scores = {}
        for m in finished:
            mid = m.get('id')
            sc = m.get('score', {})
            ft = sc.get('fullTime', {}) if sc else {}
            finished_scores[mid] = {
                'home': ft.get('home'),
                'away': ft.get('away'),
                'status': m.get('status', 'FINISHED'),
            }

        # ── 北京时间 12:00 每日刷新 ──
        BJT = timezone(timedelta(hours=8))
        now_bjt = datetime.now(timezone.utc).astimezone(BJT)
        # 今天 = 当天12:00 → 次日12:00 (严格日历日)
        today_12_bjt = now_bjt.replace(hour=12, minute=0, second=0, microsecond=0)
        # 明天 = 次日12:00 → 再次日12:00 (覆盖2天)
        tomorrow_12_bjt = today_12_bjt + timedelta(days=2)

        # 转回 UTC 用于比较 (API 数据是 UTC)
        # 今天 = 当天12:00 → 次日12:00
        today_end_utc = (today_12_bjt + timedelta(days=1)).astimezone(timezone.utc)
        # 明天 = 次日12:00 → 再次日12:00 (today_12+1 → today_12+2)
        tomorrow_end_utc = tomorrow_12_bjt.astimezone(timezone.utc)

        # 合并 fixtures + finished (finished含比分), 但只保留时间窗口内的
        all_matches = {m['id']: m for m in fixtures}
        for m in finished:
            all_matches[m['id']] = m

        result = {"today": [], "tomorrow": [], "upcoming_count": 0}
        for mid, m in all_matches.items():
            utc_str = m.get('utcDate', '')
            try:
                match_time = datetime.fromisoformat(utc_str.replace('Z', '+00:00'))
            except (ValueError, TypeError):
                continue

            # 时间窗口: 今天12:00 → 明天12:00 (今天) or 明天12:00 → 后天12:00 (明天)
            # 已完赛放宽12h下限 (显示前一天的完赛比赛)
            today_start_utc = today_12_bjt.astimezone(timezone.utc)
            is_finished = mid in finished_scores
            if not is_finished and match_time < today_start_utc:
                continue  # 未开始的不显示今天12:00之前的
            if is_finished and match_time < today_start_utc - timedelta(hours=12):
                continue  # 已完赛的最多显示12h前
            if match_time > tomorrow_end_utc:
                continue  # 后天12:00之后不显示

            # BJT 显示时间
            match_time_bjt = match_time.astimezone(BJT)
            fs = finished_scores.get(mid, {})
            entry = {
                "id": mid,
                "home": m.get('homeTeam', {}).get('name', '?'),
                "away": m.get('awayTeam', {}).get('name', '?'),
                "time": utc_str,
                "time_local": match_time_bjt.strftime('%H:%M'),
                "group": m.get('group', '').replace('GROUP_', '') if m.get('group') else '',
                "status": fs.get('status') or m.get('status', ''),
                "score_home": fs.get('home'),
                "score_away": fs.get('away'),
            }

            if match_time <= today_end_utc:
                result["today"].append(entry)
            elif match_time <= tomorrow_end_utc:
                result["tomorrow"].append(entry)

        result["upcoming_count"] = len(result["today"]) + len(result["tomorrow"])
        return result
    except Exception as e:
        logger.warning(f"[Fixtures] 获取失败: {e}")
        return {"today": [], "tomorrow": [], "upcoming_count": 0, "error": str(e)}

logger.info("Chat routes: POST /api/v1/chat, GET /api/v1/chat/health")

# ── 图片预测端点 ──────────────────────────
from fastapi import UploadFile, File as _File
import tempfile as _tempfile

@app.post("/api/v1/predict/image")
async def predict_image(file: UploadFile = _File(...)):
    """图片上传 → OCR识别 → 6层引擎分析 → SSE流式"""
    async def generate():
        # 1. 校验文件类型
        if not file.content_type or not file.content_type.startswith("image/"):
            yield f"data: {_json.dumps({'type':'error','content':'只支持 jpg/png/webp 图片格式'})}\n\n"
            yield f"data: {_json.dumps({'type':'done'})}\n\n"
            return

        content = await file.read()
        size_mb = len(content) / (1024 * 1024)
        _img_msg = _json.dumps({'type':'text','content':f'📸 图片已接收 ({file.filename}, {size_mb:.1f}MB)\n'})
        yield f"data: {_img_msg}\n\n"

        if size_mb > 10:
            yield f"data: {_json.dumps({'type':'error','content':'图片超过10MB限制'})}\n\n"
            yield f"data: {_json.dumps({'type':'done'})}\n\n"
            return

        # 2. 写入临时文件
        _suffix = os.path.splitext(file.filename or "img.png")[1] or ".png"
        with _tempfile.NamedTemporaryFile(suffix=_suffix, delete=False) as _tf:
            _tf.write(content)
            _tmp_path = _tf.name

        try:
            # 3. OCR识别
            yield f"data: {_json.dumps({'type':'status','content':'🔍 正在识别图片中的比赛信息...'})}\n\n"

            ocr_text = ""
            ocr_used = False
            try:
                from PIL import Image as _PILImage
                _img = _PILImage.open(_tmp_path)
                try:
                    import pytesseract
                    ocr_text = pytesseract.image_to_string(_img, lang="chi_sim+eng")
                    ocr_used = True
                except (ImportError, Exception):
                    ocr_text = "[OCR未安装] 请用文字描述比赛信息，或安装: pip install pytesseract pillow"
            except ImportError:
                ocr_text = "[PIL未安装] 图片识别需要 pillow 库"

            if ocr_used:
                _ocr_done_msg = _json.dumps({'type':'text','content':f'✅ 识别完成 ({len(ocr_text)}字符)\n'})
                yield f"data: {_ocr_done_msg}\n\n"
                # 4. 文本解析
                try:
                    from modules.image_input import ImageInputParser
                    parser = ImageInputParser()
                    parse_result = parser.parse(ocr_text)
                    if parse_result.valid_count > 0:
                        m = parse_result.matches[0]
                        _parse_msg = _json.dumps({'type':'text','content':f'📋 解析: {m.home} vs {m.away}\n  赔率: {m.odds_h}/{m.odds_d}/{m.odds_a}\n'})
                        yield f"data: {_parse_msg}\n\n"

                        # 5. 6层引擎分析
                        yield f"data: {_json.dumps({'type':'status','content':'⚙️ 6层AI引擎分析中...'})}\n\n"
                        _saved_path = [p for p in sys.path if 'backend' in p and 'Architecture' in p]
                        for _p in _saved_path: sys.path.remove(_p)
                        from six_layer_conversation import SixLayerConversationEngine
                        for _p in _saved_path: sys.path.insert(0, _p)

                        engine = SixLayerConversationEngine(enable_l6=False)
                        _odds = {'home': m.odds_h, 'draw': m.odds_d, 'away': m.odds_a} if m.odds_h else None
                        result = engine.process(
                            f"{m.home} vs {m.away}", m.home, m.away,
                            m.league or "未知", _odds)
                        report = result.analysis_report
                        for chunk in [report[i:i+300] for i in range(0, len(report), 300)]:
                            yield f"data: {_json.dumps({'type':'text','content':chunk})}\n\n"
                            await _asyncio.sleep(0.02)
                        card = {"home": m.home, "away": m.away,
                                "h_prob": round(result.h_prob,4), "d_prob": round(result.d_prob,4),
                                "a_prob": round(result.a_prob,4),
                                "d_gate": result.d_gate_result or "",
                                "time_ms": round(result.total_time_ms,1)}
                        yield f"data: {_json.dumps({'type':'predict_card','data':card})}\n\n"
                    else:
                        _no_match_msg = _json.dumps({'type':'text','content':'⚠️ 未能从图片中提取到比赛信息\n\n建议: 直接用文字描述比赛，例如:\n"巴西 vs 阿根廷 2.10 3.30 3.60"\n'})
                        yield f"data: {_no_match_msg}\n\n"
                except Exception as e:
                    _parse_err_msg = _json.dumps({'type':'text','content':f'⚠️ 解析失败: {e}\n\n原始识别文本:\n{ocr_text[:500]}'})
                    yield f"data: {_parse_err_msg}\n\n"
            else:
                _ocr_suggest_msg = _json.dumps({'type':'text','content':f'{ocr_text}\n\n💡 建议: 将赔率截图中的文字直接粘贴到输入框分析。'})
                yield f"data: {_ocr_suggest_msg}\n\n"
        finally:
            try:
                os.unlink(_tmp_path)
            except (OSError, PermissionError):
                pass  # 临时文件清理失败不影响主流程

        yield f"data: {_json.dumps({'type':'done'})}\n\n"
    return _StreamingResponse(generate(), media_type="text/event-stream")

logger.info("Image endpoint: POST /api/v1/predict/image")

# ── 静态文件挂载 (conversation.html) ──────
_static_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'static')
if not os.path.exists(_static_dir):
    os.makedirs(_static_dir, exist_ok=True)
try:
    from fastapi.staticfiles import StaticFiles
    app.mount("/static", StaticFiles(directory=_static_dir), name="static")

    @app.get("/chat")
    async def chat_page():
        from fastapi.responses import FileResponse
        chat_html = os.path.join(_static_dir, 'conversation.html')
        if os.path.exists(chat_html):
            return FileResponse(chat_html)
        return {"message": "conversation.html not found in static/"}
    logger.info(f"Static files mounted: /static -> {_static_dir}")
    logger.info(f"Chat page: http://localhost:{os.getenv('API_PORT', '8000')}/chat")
except ImportError:
    logger.info("StaticFiles not available")


# ── 根路径: Chat 界面 ───────────────────
_CHAT_HTML_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'static', 'conversation.html')

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """哨响AI v4.0 — AI 对话界面 (Agent 交互入口)"""
    if os.path.exists(_CHAT_HTML_PATH):
        with open(_CHAT_HTML_PATH, 'r', encoding='utf-8') as f:
            return f.read()
    return "<h2>哨响AI v4.0</h2><p>对话界面文件未找到，请访问 /chat</p>"


# ── WebSocket 健康推送 ───────────────────

@app.websocket("/ws/health")
async def websocket_health(websocket: WebSocket):
    """实时系统健康推送"""
    await websocket.accept()
    try:
        while True:
            try:
                from modules.auto_optimizer import get_optimizer
                opt = get_optimizer()
                status = opt.status_summary()
                await websocket.send_text(_json.dumps({
                    "type": "health_update",
                    "timestamp": datetime.now().isoformat(),
                    "health": status["health"],
                    "performance": status["performance"]["current"],
                    "trend": status["performance"]["trend"]["direction"],
                    "advice": status["health_advice"],
                }))
            except Exception as e:
                await websocket.send_text(_json.dumps({
                    "type": "health_update", "health": "unknown", "error": str(e)
                }))
            await _asyncio.sleep(30)  # 每30秒推送
    except WebSocketDisconnect:
        pass


# ── API v1 根路径 ─────────────────────────
@app.get(settings.API_V1_PREFIX, include_in_schema=False)
async def api_v1_root():
    """API v1 根路径 — 返回可用端点列表"""
    return {
        "service": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "docs": f"{settings.API_V1_PREFIX}/docs",
        "endpoints": {
            "predict": f"{settings.API_V1_PREFIX}/predict",
            "models": f"{settings.API_V1_PREFIX}/models",
            "monitor": f"{settings.API_V1_PREFIX}/monitor",
            "training": f"{settings.API_V1_PREFIX}/training",
            "data_quality": f"{settings.API_V1_PREFIX}/data-quality",
            "auth": f"{settings.API_V1_PREFIX}/auth",
            "ab_test": f"{settings.API_V1_PREFIX}/ab-test",
            "alerts": f"{settings.API_V1_PREFIX}/alerts",
            "historical": f"{settings.API_V1_PREFIX}/historical",
        },
    }






@app.get("/generate.html", include_in_schema=False)
async def generate_legacy():
    """旧 generate.html — 前端已删除"""
    return {"detail": "前端已删除，请使用 /docs API文档"}


@app.get("/api/monitor/health")
async def health_legacy():
    """兼容旧版 Flask API 的健康检查端点"""
    from api.v1.endpoints.monitor import health_check
    return await health_check()


# ── Prometheus 指标端点 ──────────────────
@app.get("/metrics")
async def metrics():
    """Prometheus 指标端点"""
    try:
        from utils.metrics_exporter import get_metrics_exporter
        exporter = get_metrics_exporter()
        return exporter.render()
    except ImportError:
        return JSONResponse(
            status_code=501,
            content={"error": "metrics_exporter not installed"},
        )


# ── Flask Legacy WSGI 挂载 ────────────────
# FastAPI 路由优先匹配机制，未命中时回退到Flask（用于处理 /api/* 传统接口）
try:
    from flask_bridge import get_flask_app
    flask_wsgi = get_flask_app()
    app.mount("/", WSGIMiddleware(flask_wsgi))
    logger.info("[挂载] Flask legacy API 已成功挂载 (WSGI 兼容层)")
except ImportError as e:
    logger.warning(f"[警告] Flask legacy API 未挂载: {e}")
except (ValueError, KeyError, FileNotFoundError) as e:
    logger.error(f"[错误] Flask WSGI 挂载失败: {e}")


# ── 启动 ──────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.DEBUG,
        log_level=settings.LOG_LEVEL.lower(),
    )
