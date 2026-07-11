"""
哨响AI v7.1 — 规则流水线预测引擎 (支持双模式)
================================================
基于 WC26 42场回测 + 17份报告构建。

模式:
  - rule (默认): 纯规则6步流水线 (无ML依赖)
  - optimized: +WC主模型 wc_main_v1 (Stacking: LightGBM+XGBoost→LogisticRegression)
               + DrawExpert v3_focal (Focal Loss二分类, Youden J=0.688, 需DB 77维特征)

流水线:
  输入端 → [1]赔率解析 → [2]战绩分析 → [3]战意分析
       → [4]区间过滤/ML信号层(opt) → [5]误定价叠加 → [6]最终决策 → 输出端
  OU联动: predict_ou_wc() WC实测校准大小球 (场均3.01球, ≥3球占53%)
  让球联动: predict_hcp_wc() WC校准让球推荐 (让2球不穿律/让1球冷门律/深让双推)

关键发现 (WC26 实测, 非旧报告):
  - WC 场均 3.01 球, ≥3 球占比 53%, 小组赛穿盘(≥3净胜)25.6%
  - WC 平局率实测 27.6% (旧报告38.5%偏高, 已修正)
  - 屠杀/碾压信号压倒赔率指向
  - 主模型CV准确率74.1% (vs 57.7%基线, +16.4%), 平局F1 0.623
"""

import os, sys, json, math, logging
from typing import Dict, Optional, Tuple
from dataclasses import dataclass
import numpy as np

logger = logging.getLogger(__name__)

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

# ═══ ML覆盖市场/规则决策的全局安全护栏 ═══
# 跨届 walk-forward 验证(wc_cross_tournament_wf.py): WC主模型跨届泛化≈0.54 ≈ 赔率argmax,
# drawF1=0, 模型外推失效(尤其实时未入库→optimized静默退规则, ML off)。
# 在样本外验证通过前, 默认禁止主模型以 ml_conf>=0.60 翻转规则/市场决策 —— 仅允许"佐证"
# (与规则同向时提升置信, 不改变选择)。环境变量 WC_ENABLE_ML_OVERRIDE=1 可临时开启(受控实验)。
ENABLE_ML_MARKET_OVERRIDE = os.environ.get('WC_ENABLE_ML_OVERRIDE', '0') == '1'

# ═══ DrawExpert v3_focal + WC主模型 (lazy load, 仅 optimized 模式使用) ═══
_DE_PKG = None       # dict: {model, calibrator, threshold}
_DE_LOADED = False
_MAIN_PKG = None      # dict: {lgb, xgb, meta}
_MAIN_LOADED = False

def _load_de():
    """惰性加载 DrawExpert v3_focal (dict格式, 含calibrator+threshold)"""
    global _DE_PKG, _DE_LOADED
    if _DE_LOADED:
        return True
    try:
        import joblib, warnings
        saved_root = os.path.dirname(ROOT)
        with warnings.catch_warnings():
            # 抑制 XGBoost/LightGBM 跨版本 pickle 噪声(向后兼容预测, 已知低风险)
            warnings.filterwarnings('ignore', message='.*serialized model.*')
            warnings.filterwarnings('ignore', message='.*InconsistentVersionWarning.*')
            _DE_PKG = joblib.load(os.path.join(saved_root, 'saved_models', 'draw_expert_v3_focal.joblib'))
        _DE_LOADED = isinstance(_DE_PKG, dict) and 'model' in _DE_PKG
    except Exception as e:
        logger.warning("[wc_engine] DrawExpert 模型加载失败, optimized 将退化为纯规则: %s", e)
        _DE_PKG = None
        _DE_LOADED = False
    return _DE_LOADED

def _load_main():
    """惰性加载 WC主模型 wc_main_v1 (Stacking LGB+XGB)"""
    global _MAIN_PKG, _MAIN_LOADED
    if _MAIN_LOADED:
        return True
    try:
        import joblib, warnings
        saved_root = os.path.dirname(ROOT)
        with warnings.catch_warnings():
            # 抑制 XGBoost/LightGBM 跨版本 pickle 噪声(向后兼容预测, 已知低风险)
            warnings.filterwarnings('ignore', message='.*serialized model.*')
            warnings.filterwarnings('ignore', message='.*InconsistentVersionWarning.*')
            _MAIN_PKG = joblib.load(os.path.join(saved_root, 'saved_models', 'wc_main_v1.joblib'))
        _MAIN_LOADED = isinstance(_MAIN_PKG, dict) and 'meta' in _MAIN_PKG
    except Exception as e:
        logger.warning("[wc_engine] WC主模型加载失败, optimized 将退化为纯规则: %s", e)
        _MAIN_PKG = None
        _MAIN_LOADED = False
    return _MAIN_LOADED

# ═══ 队名归一化 (DB 同时存有中文/英文队名, 但 _FORM_DB 仅中文) ═══
# 调用方可能传中文或英文队名; 归一化到 _FORM_DB 的中文键, 避免
# analyze_form 静默退化为中性实力(弱队默认值)。特征查询侧也复用。
_TEAM_ALIAS = {
    'spain': '西班牙', 'argentina': '阿根廷', 'france': '法国', 'england': '英格兰',
    'brazil': '巴西', 'mexico': '墨西哥', 'portugal': '葡萄牙', 'usa': '美国',
    'united states': '美国', 'belgium': '比利时', 'austria': '奥地利',
    'colombia': '哥伦比亚', 'switzerland': '瑞士', 'croatia': '克罗地亚',
    'morocco': '摩洛哥', 'egypt': '埃及', 'japan': '日本', 'senegal': '塞内加尔',
    'sweden': '瑞典', 'congo dr': '民主刚果', 'dr congo': '民主刚果',
    'bosnia-h.': '波黑', 'bosnia': '波黑', 'ecuador': '厄瓜多尔',
    'ivory coast': '科特迪瓦', "côte d'ivoire": '科特迪瓦', 'ghana': '加纳',
    'algeria': '阿尔及利亚', 'south africa': '南非', 'australia': '澳大利亚',
    'canada': '加拿大', 'paraguay': '巴拉圭', 'panama': '巴拿马', 'norway': '挪威',
    'cape verde': '佛得角', 'korea republic': '韩国', 'south korea': '韩国',
    'korea': '韩国', 'czechia': '捷克', 'czech republic': '捷克', 'qatar': '卡塔尔',
    'haiti': '海地', 'scotland': '苏格兰', 'turkey': '土耳其', 'germany': '德国',
    'netherlands': '荷兰', 'uruguay': '乌拉圭', 'iran': '伊朗', 'iraq': '伊拉克',
    'jordan': '约旦', 'uzbekistan': '乌兹别克斯坦', 'new zealand': '新西兰',
    'tunisia': '突尼斯', 'curaçao': '库拉索', 'curacao': '库拉索',
    'saudi arabia': '沙特阿拉伯', 'saudi': '沙特阿拉伯',
}

# 中文 -> 英文 canonical (由 _TEAM_ALIAS 反向得到, 覆盖全部参赛队).
# 用于 "中英同步": 无论调用方传中文还是英文, 都归一到同一 canonical key,
# 从而命中同一场比赛行, 避免任一种语言 "查不到".
# key 统一 str(v).lower(): _TEAM_ALIAS 英文 value 大小写混乱(如 'brazil' vs 'Panama'),
# 若不归一, _canon_team 用 zh.lower() 查表时大小写不匹配, 致 'Panama'!='panama' 不对称.
# first-wins: 同一中文 value 对应多个英文 key(如 'usa'/'united states'->'美国')时,
#   保留最先出现的英文 key 作 canonical, 使 中文'美国' 与 英文'USA' 收敛到同一 'usa'.
_ZH2EN = {}
for _k, _v in _TEAM_ALIAS.items():
    if _v is not None:
        _zk = str(_v).lower()
        if _zk not in _ZH2EN:
            _ZH2EN[_zk] = _k

def _norm_team(name: str) -> str:
    """队名归一化: 英文/别名 → _FORM_DB 中文键(若命中), 否则原样返回。"""
    if name is None:
        return name
    key = name.strip().lower()
    return _TEAM_ALIAS.get(key, name)


def _canon_team(name: str) -> str:
    """中英同步核心: 任意 中文/英文/别名 输入 -> 稳定的 英文小写 canonical 队名。
    始终收敛到 英文小写 canonical, 使 DB英文('Cape Verde') / 源小写('cape verde') /
    中文('佛得角'/'佛得角共和国') / 同队双份写法('乌拉圭'与'Uruguay') 三者全等。
    修复(2026-07-07) 根治:
      (a) 大小写不对称 'Panama'!='panama' (此前致 ETL 漏匹配3场真实完赛);
      (b) 中文/英文落到不同 canonical space (原实现中文返回中文、英文返回中文value);
      (c) 缺失英文 key (Panama 等) 致中文'巴拿马'无法收敛到 'panama'。
    路径: 1)已是英文canonical key -> 直接返回; 2)中文 -> 经 _ZH2EN 转英文;
          3)英文别名/变体 -> 先 _TEAM_ALIAS 取中文 -> 再 _ZH2EN 转英文;
          4)兜底: 英文原样小写(未收录队)。"""
    if name is None:
        return name
    key = name.strip().lower()
    if key in _TEAM_ALIAS:                 # 1) 英文 canonical key -> 经中文取 canonical 英文
        zh = _TEAM_ALIAS[key]
        if zh is not None and zh.lower() in _ZH2EN:
            return _ZH2EN[zh.lower()]      # 别名/变体(saudi/saudi arabia, usa/united states)统一到同一 key
        return key
    if key in _ZH2EN:                      # 2) 中文 -> 英文 canonical
        return _ZH2EN[key]
    return key                             # 3)兜底: 英文原样小写(未收录队)

DE_THRESHOLD = 0.688  # 保留兼容(实际未使用); 真门控阈值取包内 _DE_PKG['threshold']=0.375
# ── DrawGate 平局专家门控 (攻克DE核心, 2026-07-07) ──
# 机制: DrawExpert 是专门平局二分类器(非主模型3-class argmax), 其 Isotonic 校准后的
#   平局概率 de_prob 在平局边界有真实信号(诊断: 平局场均值0.387 vs 非平局0.099)。
# 门控: de_prob>=DRAW_GATE 且 市场未强烈排除平局(odds['imp_d']>=DRAW_GATE_MIN_IMP_D)
#   → 强制 prediction='D', 且不被护栏OFF的市场argmax兜底覆盖。
# 与"主模型override市场"的区别: 后者跨届OOS失效(argmax结构性缺陷); 本门控是平局专项裁决,
#   不翻转H/A, 仅当平局专家高置信且市场未强烈否认时才判D。
# 基于68场in-sample诊断(特征与训练重叠, 偏乐观):
#   de_prob 双峰极稀疏: 平局场中位数0.027, 仅~7场冲到0.688+; 非平局场仅4个误报。
#   门控甜点 = 包内校准阈值 0.688(Youden J), imp_d 下限仅作轻量护栏:
#   GATE=0.688/imp_d>=0.10 → 整体~71%(+~6% vs 市场64.7%), 平局命中~7/18(precision~64% >> 基率26.5%)
#   注: imp_d下限0.18会误杀2场真平局(纯减分), 故降至0.10。
# 结构性天花板: DrawExpert 对 de_prob≈0 的~11场平局无信号(特征分不出), 门控无法补救;
#   需模型/特征重训或融合"赔率隐含平局概率"才有望覆盖剩余平局。
# 诚实标注: in-sample偏乐观; 跨届OOS需补赔率(wc_all_matches跨届oh/od/oa=None)后验证。
DRAW_GATE = 0.688
DRAW_GATE_MIN_IMP_D = 0.10

# ── DrawTightGate: 胶着+战意 平局补充门控 (选择性修正边缘局, 2026-07-07) ──
# 机制: 小组MD3生死战(或淘汰赛均势 survival_clash)中, 若平局隐含概率紧贴热门
#   (fav-imp_d <= DRAW_TIGHT_GAP), 说明比赛胶着且双方有抢分/保平动机 →
#   即便市场(argmax)及"强队实力分支"选H/A, 也翻D。
# 与DrawGate区别: DrawGate依赖DE(de_prob, 对爆冷平局给~0); 本门控依赖"赔率胶着度+战意",
#   专门覆盖 DE 漏判、但确属"强队略优+末轮生死胶着"的爆冷平局。
# 诊断(68场in-sample, 脚本 verify_tight_motivation.py): GAP扫描 0.16~0.22 平台区 →
#   判D18/命中11/误判7, 平局召回 38.9%→61.1%, 整体 72.1%→75.0%; 精确61.1%>>基率26.5%。
# 取平台中位 0.18 留裕度。诚实标注: 阈值在该集扫描选出(in-sample偏乐观), 跨届OOS待验。
DRAW_TIGHT_GAP = 0.18

_TEAM_FEATURE_MAP = None  # 归一化队名键 -> 有match_features的世界杯英文行队名

def _load_team_feature_map():
    """自动构建 (norm_h, norm_a) -> (db_h, db_a) 映射, 仅含 JOIN match_features 的世界杯行。
    解决同一赛事中英文双副本、特征只挂英文副本时, 归一化命中中文无特征副本导致
    ML 静默降级的问题。调用方传中文/英文/别名均可优先命中有特征行。"""
    global _TEAM_FEATURE_MAP
    if _TEAM_FEATURE_MAP is not None:
        return
    m = {}
    try:
        import sqlite3
        saved_root = os.path.dirname(ROOT)
        db = os.path.join(saved_root, 'data', 'football_data.db')
        c = sqlite3.connect(db); cur = c.cursor()
        rows = cur.execute(
            "SELECT m.home_team_name, m.away_team_name FROM matches m "
            "JOIN match_features f ON m.match_id=f.match_id "
            "WHERE m.league_name='世界杯' AND m.home_team_name IS NOT NULL AND m.away_team_name IS NOT NULL"
        ).fetchall()
        for h, a in rows:
            m[(_canon_team(h), _canon_team(a))] = (h, a)
        c.close()
    except Exception as e:
        logger.warning("[wc_engine] 构建队名特征映射失败: %s", e)
    _TEAM_FEATURE_MAP = m


def _get_wc_features(home: str, away: str):
    """从DB获取该场WC比赛的77维match_features (供ML模型使用)。
    查不到(未来比赛未入库)返回None，调用方fallback到纯规则。

    队名兼容: DB 中同一队可能以中文或英文存储, 且特征只挂在英文副本上。
    优先用「有 match_features 的英文副本」队名查询, 避免归一化命中中文无特征
    副本导致 ML 静默降级。
    """
    try:
        import sqlite3, numpy as np
        saved_root = os.path.dirname(ROOT)
        db = os.path.join(saved_root, 'data', 'football_data.db')
        conn = sqlite3.connect(db)
        cur = conn.cursor()

        # 候选队名: 优先用有 match_features 的英文副本; 同时支持中/英双向解析
        _load_team_feature_map()
        key = (_canon_team(home), _canon_team(away))
        db_pair = _TEAM_FEATURE_MAP.get(key)
        cand_set = []
        if db_pair:
            cand_set.append(db_pair)                      # 有特征的 canonical 英文副本
        cand_set.append((home, away))                    # 原始输入
        cand_set.append((_norm_team(home), _norm_team(away)))    # 英文->中文 (form/中文库)
        cand_set.append((_canon_team(home), _canon_team(away)))  # 中文/英文 -> 英文 canonical
        candidates = []
        for c in cand_set:                                # 去重保序
            if c not in candidates:
                candidates.append(c)

        # 先查 JOIN match_features 的有特征行 (优先)
        mid = None
        for h, a in candidates:
            cur.execute(
                "SELECT m.match_id FROM matches m JOIN match_features f ON m.match_id=f.match_id "
                "WHERE m.league_name='世界杯' AND m.home_team_name=? AND m.away_team_name=? LIMIT 1", (h, a)
            )
            row = cur.fetchone()
            if row:
                mid = row[0]; break
        # 兜底: 无特征行 (保留原行为, 未来比赛占位)
        if mid is None:
            for h, a in candidates:
                cur.execute(
                    "SELECT match_id FROM matches WHERE league_name='世界杯' "
                    "AND home_team_name=? AND away_team_name=? LIMIT 1", (h, a)
                )
                row = cur.fetchone()
                if row:
                    mid = row[0]; break
        if mid is None:
            conn.close()
            logger.info("[wc_engine] 未找到WC比赛(home=%s away=%s), 退化为规则", home, away)
            return None
        # 取特征列顺序 (与主模型训练一致)
        # 防御性加固: 确保模型已加载, 避免独立调用时 feature_cols=None 即便有特征也返None(顺序依赖脆弱性)
        _load_main(); _load_de()
        feat_cols = None
        if _MAIN_LOADED and isinstance(_MAIN_PKG, dict):
            feat_cols = _MAIN_PKG.get('feature_cols')
        if feat_cols is None and _DE_LOADED and isinstance(_DE_PKG, dict):
            feat_cols = _DE_PKG.get('feature_cols')
        if feat_cols is None:
            conn.close()
            logger.warning("[wc_engine] feature_cols 未就绪(模型未加载), 无法取特征")
            return None
        if len(feat_cols) != 77:
            conn.close()
            logger.warning("[wc_engine] feature_cols 长度=%d(期望77), 模型/特征版本不一致, 跳过ML", len(feat_cols))
            return None
        ph = ','.join('?' * len(feat_cols))
        cur.execute(
            f"SELECT {','.join(feat_cols)} FROM match_features WHERE match_id=?", (mid,)
        )
        r = cur.fetchone()
        conn.close()
        if not r:
            logger.info("[wc_engine] match_id=%s 无 match_features 行, 退化为规则", mid)
            return None
        arr = np.array([0.0 if v is None else float(v) for v in r], dtype=float)
        if arr.shape[0] != len(feat_cols):
            logger.warning("[wc_engine] 特征维度=%d 与 feature_cols=%d 不一致", arr.shape[0], len(feat_cols))
            return None
        return arr
    except Exception as e:
        logger.warning("[wc_engine] 读取WC特征异常(home=%s away=%s): %s", home, away, e)
        return None


# ═══════════════════════════════════════════════════════════
# 数据类
# ═══════════════════════════════════════════════════════════

@dataclass
class MatchInput:
    home: str
    away: str
    odds_h: float
    odds_d: float
    odds_a: float
    hcp: float = 0.0
    ou_line: float = 2.5
    stage: str = "group"          # group / knockout
    matchday: int = 3
    r3_rotation: bool = False

@dataclass
class PipelineResult:
    prediction: str               # H / D / A
    confidence: float             # 0.0-1.0
    best_score: str               # "2-0"
    alt_scores: list
    market_baseline: str          # 市场argmax对比
    market_probs: dict            # {H, D, A} 隐含概率
    mid_range_filtered: bool      # 中概率区间是否被过滤
    mispricing_overlay: bool      # 误定价是否覆盖
    massacre_triggered: bool      # 屠杀预警
    survival_clash: bool          # 生死战
    rationale: str                # 可读的决策理由
    confidence_level: str         # high / medium / low / skip
    ou_recommend: dict = None     # WC校准大小球建议 {recommend, line, expected_total, confidence, wc_calibrated}
    hcp_recommend: dict = None    # WC校准让球建议 {recommend, hcp, confidence, note, wc_calibrated}


# ═══════════════════════════════════════════════════════════
# Step 1: 赔率解析
# ═══════════════════════════════════════════════════════════

def parse_odds(odds_h: float, odds_d: float, odds_a: float) -> dict:
    """解析赔率 → 隐含概率 + 力度"""
    if odds_h is None or odds_d is None or odds_a is None:
        raise ValueError("parse_odds: odds_h/d/a 不能为 None")
    if odds_h <= 0 or odds_d <= 0 or odds_a <= 0:
        raise ValueError("parse_odds: odds_h/d/a 必须 > 0")
    implied_sum = 1/odds_h + 1/odds_d + 1/odds_a
    imp_h = (1/odds_h) / implied_sum
    imp_d = (1/odds_d) / implied_sum
    imp_a = (1/odds_a) / implied_sum
    vigorish = implied_sum - 1.0  # overround
    
    # 市场基线
    if imp_h > imp_d and imp_h > imp_a:
        market = 'H'
    elif imp_d > imp_h and imp_d > imp_a:
        market = 'D'
    else:
        market = 'A'
    
    # 概率区间分类 (基于 WC26 42场回测最优分割)
    # 注: strong 阈值与 optimized 决策树首分支(max_imp>0.68)对齐, 消除 0.68-0.70 盲区分歧 (reviewer P3)
    max_imp = max(imp_h, imp_d, imp_a)
    if max_imp > 0.68:
        zone = 'strong'           # 80%准确率 (最佳区间)
    elif max_imp > 0.55:
        zone = 'mid_safe'         # 55-70%: 市场方向有效(65%)
    elif max_imp > 0.45:
        zone = 'mid_danger'       # 45-55%: 平局率50%, 反向操作
    else:
        zone = 'weak'             # <45%: 市场方向为主
    
    return {
        'imp_h': imp_h, 'imp_d': imp_d, 'imp_a': imp_a,
        'vigorish': vigorish, 'market': market, 'zone': zone,
    }


# ═══════════════════════════════════════════════════════════
# Step 2: 战绩分析 (数据源: odds_db + round32_predictions)
# ═══════════════════════════════════════════════════════════

# 预赛战绩数据库 (世界杯小组赛阶段)
_FORM_DB = {
    # 强队 (净胜>1.5)
    "西班牙": {"gf": 2.0, "ga": 0.0, "n": 4, "momentum": 0.82},
    "阿根廷": {"gf": 2.6, "ga": 0.2, "n": 10, "momentum": 0.50},
    "法国":   {"gf": 2.8, "ga": 0.4, "n": 5, "momentum": 1.00},
    "英格兰": {"gf": 2.1, "ga": 0.4, "n": 10, "momentum": 0.50},
    "巴西":   {"gf": 2.0, "ga": 0.8, "n": 5, "momentum": 0.70},
    "墨西哥": {"gf": 2.0, "ga": 0.0, "n": 4, "momentum": 1.00},
    "葡萄牙": {"gf": 2.4, "ga": 0.8, "n": 10, "momentum": 0.50},
    "美国":   {"gf": 2.5, "ga": 1.0, "n": 4, "momentum": 0.79},
    "比利时": {"gf": 2.25, "ga": 1.0, "n": 4, "momentum": 0.68},
    "奥地利": {"gf": 2.5, "ga": 0.7, "n": 10, "momentum": 0.50},
    "哥伦比亚":{"gf": 2.0, "ga": 0.8, "n": 10, "momentum": 0.50},
    "瑞士":   {"gf": 2.25, "ga": 0.75, "n": 4, "momentum": 0.82},
    
    # 中游
    "克罗地亚":{"gf": 1.3, "ga": 1.4, "n": 10, "momentum": 0.50},
    "摩洛哥":  {"gf": 2.0, "ga": 0.8, "n": 5, "momentum": 0.80},
    "埃及":    {"gf": 1.5, "ga": 1.0, "n": 4, "momentum": 0.64},
    "日本":    {"gf": 1.3, "ga": 1.3, "n": 4, "momentum": 0.50},
    "塞内加尔":{"gf": 2.5, "ga": 2.25, "n": 4, "momentum": 0.21},
    "瑞典":    {"gf": 1.0, "ga": 1.0, "n": 1, "momentum": 0.50},
    
    # 弱队 (净胜<0)
    "民主刚果":{"gf": 1.0, "ga": 0.6, "n": 10, "momentum": 0.50},
    "波黑":    {"gf": 1.25, "ga": 2.0, "n": 4, "momentum": 0.39},
    "厄瓜多尔":{"gf": 1.25, "ga": 1.0, "n": 4, "momentum": 0.32},
    "科特迪瓦":{"gf": 1.0, "ga": 1.0, "n": 3, "momentum": 0.33},
    "加纳":    {"gf": 1.0, "ga": 1.3, "n": 10, "momentum": 0.50},
    "阿尔及利亚":{"gf": 1.9, "ga": 0.7, "n": 10, "momentum": 0.50},
    "南非":    {"gf": 0.5, "ga": 1.0, "n": 4, "momentum": 0.36},
    "澳大利亚":{"gf": 0.75, "ga": 0.75, "n": 4, "momentum": 0.54},
    "加拿大":  {"gf": 1.8, "ga": 1.2, "n": 5, "momentum": 0.50},
    "巴拉圭":  {"gf": 1.0, "ga": 1.5, "n": 2, "momentum": 0.25},
    "挪威":    {"gf": 2.5, "ga": 2.0, "n": 4, "momentum": 0.79},
    "佛得角":  {"gf": 1.0, "ga": 1.25, "n": 4, "momentum": 0.43},
}


def analyze_form(home: str, away: str) -> dict:
    """战绩分析 → 实力差距 + 屠杀预警"""
    hk = _norm_team(home)
    ak = _norm_team(away)
    hf = _FORM_DB.get(hk, _FORM_DB.get(home, {"gf": 1.0, "ga": 1.0, "n": 1, "momentum": 0.5}))
    af = _FORM_DB.get(ak, _FORM_DB.get(away, {"gf": 1.0, "ga": 1.0, "n": 1, "momentum": 0.5}))
    
    h_net = hf["gf"] - hf["ga"]
    a_net = af["gf"] - af["ga"]
    net_diff = h_net - a_net
    
    if net_diff > 2.0:
        strength_gap = 'massacre_home'
        massacre_triggered = True
    elif net_diff < -2.0:
        strength_gap = 'massacre_away'
        massacre_triggered = True
    elif net_diff > 1.0:
        strength_gap = 'edge_home'
        massacre_triggered = False
    elif net_diff < -1.0:
        strength_gap = 'edge_away'
        massacre_triggered = False
    else:
        strength_gap = 'even'
        massacre_triggered = False
    
    # 防线崩盘检测
    defense_collapse = (hf["ga"] >= 2.0 or af["ga"] >= 2.0)
    weak_attack = (hf["gf"] < 1.0 and af["gf"] < 1.0)
    
    return {
        'net_diff': net_diff,
        'strength_gap': strength_gap,
        'massacre_triggered': massacre_triggered,
        'defense_collapse': defense_collapse,
        'weak_attack': weak_attack,
        'home_momentum': hf["momentum"],
        'away_momentum': af["momentum"],
    }


# ═══════════════════════════════════════════════════════════
# Step 3: 战意分析
# ═══════════════════════════════════════════════════════════

def analyze_context(stage: str, matchday: int, r3_rotation: bool, 
                    odds: dict, form: dict) -> dict:
    """战意/情境分析 → 动机倍率"""
    survival_clash = False
    dead_rubber = False
    motivation_mult = 1.0
    
    if stage == 'knockout':
        # 淘汰赛: 单场生死战
        survival_clash = True
        if odds['zone'] in ('mid_safe', 'mid_danger'):
            motivation_mult = 1.15            # 中概率淘汰赛 → 波动更大
        elif odds['zone'] == 'strong':
            motivation_mult = 1.0             # 强热正常
    elif stage == 'group':
        if r3_rotation:
            dead_rubber = True
            motivation_mult = 0.7             # R3轮换, 参考价值低
        elif matchday == 3:
            survival_clash = True
            motivation_mult = 1.1             # MD3生死战
    
    # 弱攻双求生 → 平局优先
    weak_both_survival = (
        survival_clash and form['weak_attack'] 
        and odds['zone'] in ('mid_safe', 'mid_danger')
    )
    
    return {
        'survival_clash': survival_clash,
        'dead_rubber': dead_rubber,
        'motivation_mult': motivation_mult,
        'weak_both_survival': weak_both_survival,
    }


# ═══════════════════════════════════════════════════════════
# Step 4: 中概率区间过滤
# ═══════════════════════════════════════════════════════════

def mid_range_filter(odds: dict, form: dict, context: dict) -> dict:
    """
    多区间处理 (基于 WC26 42场 + 17份回测报告优化)
    
    strong   (>70%): 市场基线, 80%准确率, 无需过滤
    mid_safe (55-70%): 市场方向有效(65%), 有实力优势保留方向
    mid_danger(45-55%): 平局率50%的死亡区间, 强制平局
    weak     (<45%): 市场方向为主, 保守
    
    最优阈值 (from p0_backtest_v2): Draw=0.32, D-Gate=0.27/0.35
    """
    zone = odds['zone']
    if zone == 'strong':
        return {'filtered': False, 'override': None, 'reason': '强热区间(>70%), 市场可靠'}
    
    if zone == 'mid_safe':
        # 屠杀必胜
        if form['massacre_triggered']:
            side = 'H' if form['net_diff'] > 0 else 'A'
            return {'filtered': True, 'override': side,
                    'reason': f'中高位+屠杀 → {("主胜" if side=="H" else "客胜")}'}
        # 实力优势 → 保留市场
        if form['strength_gap'] in ('edge_home','massacre_home') and odds['market'] == 'H':
            return {'filtered': False, 'override': None, 'reason': '实力优势+市场一致'}
        if form['strength_gap'] in ('edge_away','massacre_away') and odds['market'] == 'A':
            return {'filtered': False, 'override': None, 'reason': '实力优势+市场一致'}
        # 淘汰赛均势 → 平局倾向
        if context['survival_clash']:
            return {'filtered': True, 'override': 'D',
                    'reason': '中高位均势淘汰赛 → 平局(淘汰赛平局率31%)'}
        return {'filtered': False, 'override': None, 'reason': '中高位, 市场有效'}
    
    if zone == 'mid_danger':
        # 45-55%区间: draw rate ~35%, 不宜强制D
        if form['massacre_triggered']:
            side = 'H' if form['net_diff'] > 0 else 'A'
            return {'filtered': True, 'override': side,
                    'reason': '死亡区间+屠杀 → 跟屠杀方'}
        if context['weak_both_survival']:
            return {'filtered': True, 'override': 'D',
                    'reason': '弱攻双求生 → 平局'}
        # 淘汰赛+均势 → 平局倾向
        if context['survival_clash'] and form['strength_gap'] == 'even':
            return {'filtered': True, 'override': 'D',
                    'reason': '均势淘汰赛 → 平局(淘汰赛平局率31%)'}
        # 有实力优势 → 保留市场
        if form['strength_gap'] not in ('even',):
            return {'filtered': False, 'override': None, 'reason': '实力倾向, 市场方向'}   
        # 小组赛均势 → 保持市场
        return {'filtered': False, 'override': None, 'reason': '均势小组赛, 市场指向'}
    
    # weak zone
    if form['massacre_triggered'] and form['strength_gap'] in ('massacre_home','massacre_away'):
        side = 'H' if form['net_diff'] > 0 else 'A'
        return {'filtered': True, 'override': side, 'reason': '弱势区间+屠杀 → 跟屠杀方'}
    return {'filtered': False, 'override': None, 'reason': '弱势区间, 市场指向'}


# ═══════════════════════════════════════════════════════════
# Step 5: 误定价叠加
# ═══════════════════════════════════════════════════════════

def mispricing_overlay(prediction: str, odds: dict, context: dict) -> dict:
    """
    回测结论: 爆冷场次 mispricing=0.621 vs 正路=0.405 (差值+0.22)
    当前无法实时计算 mispricing(需要 ReverseOddsEngine),
    但基于回测规律: 中概率区间 + 淘汰赛 = 高爆冷概率
    """
    upset_likely = (
        odds['zone'] in ('mid_safe', 'mid_danger') and 
        context['survival_clash']
    )
    return {
        'overlay': upset_likely,
        'override': 'D' if upset_likely and prediction == 'H' else None,
        'reason': '中概率淘汰赛 → 爆冷风险, 建议保守' if upset_likely else '',
    }


# ═══════════════════════════════════════════════════════════
# Step 6: 市场基线 + 最终决策
# ═══════════════════════════════════════════════════════════

def final_decision(odds: dict, form: dict, context: dict,
                   mid_filter: dict, mispricing: dict) -> dict:
    """最终决策 — 基于最优阈值 (17份回测报告)
    
    优先级: 屠杀 > 死亡区间 > 中位过滤 > 强热基线 > 弱势保守
    置信度: 0.80(屠杀/强热) 0.65(中位) 0.50(死亡区间) 0.45(弱势)
    """
    
    # Level 0: 屠杀碾压 (80%信度)
    if form['massacre_triggered']:
        prediction = 'H' if form['net_diff'] > 0 else 'A'
        confidence = 0.80
        rationale = f'屠杀预警(净胜差{abs(form["net_diff"]):.1f}球) → {("主胜" if prediction=="H" else "客胜")}'
        confidence_level = 'high'
    
    # Level 1: 死亡区间 (45-55%) — 保守, 不强制D
    elif odds['zone'] == 'mid_danger':
        prediction = mid_filter.get('override') or odds['market']
        confidence = 0.45
        rationale = mid_filter.get('reason', '低概率区间, 保守')
        confidence_level = 'low'
    
    # Level 2: 中概率过滤
    elif mid_filter['filtered'] and mid_filter['override']:
        prediction = mid_filter['override']
        confidence = 0.65
        rationale = mid_filter['reason']
        confidence_level = 'medium'
    
    # Level 3: 强热区间 (>70%) — 80%准确, 最高信度
    elif odds['zone'] == 'strong':
        prediction = odds['market']
        confidence = 0.80
        rationale = f'强热方({max(odds["imp_h"],odds["imp_d"],odds["imp_a"])*100:.0f}%) → 市场基线(80%可靠)'
        confidence_level = 'high'
    
    # Level 4: 中高位 (55-70%) — 市场有效
    elif odds['zone'] == 'mid_safe':
        prediction = odds['market']
        confidence = 0.65
        rationale = f'中高区间({max(odds["imp_h"],odds["imp_d"],odds["imp_a"])*100:.0f}%) → 市场有效'
        confidence_level = 'medium'
    
    # Level 5: 弱势区间
    else:
        prediction = odds['market']
        confidence = 0.45
        rationale = '弱势区间, 市场指向为主'
        confidence_level = 'low'
    
    # 误定价叠加 — 降信度但不变方向
    if mispricing['overlay']:
        confidence *= 0.85
        rationale += '; ⚠️高爆冷风险'
        if confidence_level == 'high':
            confidence_level = 'medium'
    
    # 死球降级
    if context['dead_rubber']:
        confidence *= 0.7
        rationale += '; 轮换降级'
        confidence_level = 'low'
    
    # 比分预测
    if prediction == 'H':
        if form['massacre_triggered'] and form['net_diff'] > 0:
            best_score = f"{min(int(abs(form['net_diff']) + 1), 5)}-{max(int(form['net_diff']*0.3), 0)}"
            alt_scores = [f"{min(int(abs(form['net_diff']) + 2), 5)}-{max(int(form['net_diff']*0.2), 0)}"]
        else:
            best_score = "2-0"
            alt_scores = ["2-1", "1-0"]
    elif prediction == 'A':
        if form['massacre_triggered'] and form['net_diff'] < 0:
            diff = abs(form['net_diff'])
            best_score = f"{max(int(diff*0.3), 0)}-{min(int(diff + 1), 5)}"
            alt_scores = [f"{max(int(diff*0.2), 0)}-{min(int(diff + 2), 5)}"]
        else:
            best_score = "0-2"
            alt_scores = ["1-2", "0-1"]
    else:  # D
        if form['weak_attack']:
            best_score = "0-0"
            alt_scores = ["1-1"]
        else:
            best_score = "1-1"
            alt_scores = ["0-0", "2-2"]
    
    return {
        'prediction': prediction,
        'confidence': min(confidence, 0.95),
        'best_score': best_score,
        'alt_scores': alt_scores,
        'rationale': rationale,
        'confidence_level': confidence_level,
    }


# ═══════════════════════════════════════════════════════════
# 主流水线
# ═══════════════════════════════════════════════════════════

def predict(match: MatchInput, mode: str = "rule") -> PipelineResult:
    """运行完整流水线

    Args:
        match: 比赛输入
        mode: "rule" (纯规则) 或 "optimized" (DrawExpert + 17报告决策树)
    """
    if mode == "optimized":
        return _predict_optimized(match)
    return _predict_rule(match)


def _predict_rule(match: MatchInput) -> PipelineResult:
    """纯规则模式 (6步流水线)"""
    # Step 1: 赔率解析
    odds = parse_odds(match.odds_h, match.odds_d, match.odds_a)
    
    # Step 2: 战绩分析
    form = analyze_form(match.home, match.away)
    
    # Step 3: 战意分析
    context = analyze_context(match.stage, match.matchday, match.r3_rotation, odds, form)
    
    # Step 4: 中概率过滤
    mid_filter = mid_range_filter(odds, form, context)
    
    # Step 5: 误定价叠加
    mispricing = mispricing_overlay(
        mid_filter.get('override') or odds['market'],
        odds, context,
    )
    
    # Step 6: 最终决策
    decision = final_decision(odds, form, context, mid_filter, mispricing)
    prediction = decision['prediction']
    confidence = decision['confidence']
    confidence_level = decision['confidence_level']
    rationale = decision['rationale']

    return PipelineResult(
        prediction=prediction,
        confidence=min(confidence, 0.95),
        best_score=decision['best_score'],
        alt_scores=decision['alt_scores'],
        market_baseline=odds['market'],
        market_probs={'H': round(odds['imp_h'], 4), 'D': round(odds['imp_d'], 4), 'A': round(odds['imp_a'], 4)},
        mid_range_filtered=mid_filter['filtered'],
        mispricing_overlay=mispricing['overlay'],
        massacre_triggered=form['massacre_triggered'],
        survival_clash=context['survival_clash'],
        rationale=rationale,
        confidence_level=confidence_level,
        ou_recommend=predict_ou_wc(match),
        hcp_recommend=predict_hcp_wc(match, form, context),
    )


def _predict_optimized(match: MatchInput) -> PipelineResult:
    """优化模式 — DrawExpert v2_focal + 17报告决策树 (5层分档)"""
    # Step 1-3: 复用基础函数
    odds = parse_odds(match.odds_h, match.odds_d, match.odds_a)
    form = analyze_form(match.home, match.away)
    context = analyze_context(match.stage, match.matchday, match.r3_rotation, odds, form)
    
    # Step 4: ML信号层 (主模型 wc_main_v1 + DrawExpert v3_focal, 需DB 77维特征)
    # 必须先加载模型以获取 feature_cols, 否则 _get_wc_features 会因 feature_cols=None 死锁返回 None
    # (predict(optimized) 必须自给自足, 不应依赖调用方手动预加载)
    _load_main(); _load_de()
    features = _get_wc_features(match.home, match.away)
    main_proba = None
    main_pred = None
    main_conf = 0.0
    de_signal = False
    de_prob = 0.0
    if features is not None:
        if _load_main():
            try:
                import pandas as pd
                # 构造带特征名的输入, 消除 LGBM 'X does not have valid feature names' 警告(规范预测接口)
                _fc = _MAIN_PKG.get('feature_cols')
                Xm = pd.DataFrame([features], columns=_fc) if (_fc and len(_fc) == len(features)) else np.array([features])
                lgb_p = _MAIN_PKG['lgb'].predict_proba(Xm)[0]
                xgb_p = _MAIN_PKG['xgb'].predict_proba(Xm)[0]
                main_proba = _MAIN_PKG['meta'].predict_proba(np.hstack([lgb_p, xgb_p])[None])[0]
                main_pred = ['H', 'D', 'A'][int(np.argmax(main_proba))]
                main_conf = float(main_proba.max())
            except Exception as e:
                logger.warning("[wc_engine] 主模型预测异常: %s", e)
                main_proba = None
        if _load_de():
            try:
                import pandas as pd
                _fc = _DE_PKG.get('feature_cols')
                Xd = pd.DataFrame([features], columns=_fc) if (_fc and len(_fc) == len(features)) else [features]
                raw = _DE_PKG['model'].predict_proba(Xd)[0][1]
                cal = float(_DE_PKG['calibrator'].predict([raw])[0])
                de_prob = cal
                de_signal = cal >= _DE_PKG['threshold']
            except Exception as e:
                logger.warning("[wc_engine] DrawExpert 预测异常: %s", e)
                de_signal = False
    else:
        logger.info("[wc_engine] optimized 未取到DB特征(home=%s, away=%s) → 退化为纯规则决策树",
                    match.home, match.away)
    
    # Step 5: 5层分档决策 (17份报告最优)
    max_imp = max(odds['imp_h'], odds['imp_d'], odds['imp_a'])
    
    # ── 屠杀优先 (80%信度) ──
    if form['massacre_triggered']:
        prediction = 'H' if form['net_diff'] > 0 else 'A'
        confidence = 0.80
        rationale = f'屠杀预警(净胜差{abs(form["net_diff"]):.1f}球)'
        confidence_level = 'high'
    
    # ── 强热区间 (>68%) ──
    elif max_imp > 0.68:
        prediction = odds['market']
        confidence = 0.80
        rationale = f'强热方({max_imp*100:.0f}%)'
        confidence_level = 'high'
    
    # ── 中高区间 (55-68%) ──
    elif max_imp > 0.55:
        if de_signal and odds['imp_d'] > 0.22:
            prediction = 'D'
            confidence = 0.55
            rationale = f'中高区间+DE双确认(D_prob>{DE_THRESHOLD:.2f})'
            confidence_level = 'medium'
        elif form['strength_gap'] in ('edge_home','massacre_home'):
            prediction = 'H'; confidence = 0.65; rationale = '中高区间+主队优势'; confidence_level = 'medium'
        elif form['strength_gap'] in ('edge_away','massacre_away'):
            prediction = 'A'; confidence = 0.65; rationale = '中高区间+客队优势'; confidence_level = 'medium'
        elif context['survival_clash'] and form['strength_gap'] == 'even':
            prediction = 'D'; confidence = 0.50; rationale = '均势淘汰赛'; confidence_level = 'low'
        else:
            prediction = odds['market']; confidence = 0.65; rationale = '中高区间, 市场有效'; confidence_level = 'medium'
    
    # ── 中低区间 (45-55%) ──
    elif max_imp > 0.45:
        if de_signal and odds['imp_d'] > 0.25:
            prediction = 'D'; confidence = 0.50; rationale = '中低+双D信号'; confidence_level = 'low'
        elif form['massacre_triggered']:
            prediction = 'H' if form['net_diff'] > 0 else 'A'; confidence = 0.60; rationale = '中低+屠杀'; confidence_level = 'medium'
        elif form['strength_gap'] != 'even':
            prediction = 'H' if 'home' in form['strength_gap'] else ('A' if 'away' in form['strength_gap'] else odds['market'])
            confidence = 0.55; rationale = f'中低+{form["strength_gap"]}'; confidence_level = 'medium'
        elif context['survival_clash']:
            prediction = 'D'; confidence = 0.45; rationale = '均势淘汰赛'; confidence_level = 'low'
        elif context.get('weak_both_survival'):
            prediction = 'D'; confidence = 0.45; rationale = '弱攻双求生'; confidence_level = 'low'
        else:
            prediction = odds['market']; confidence = 0.45; rationale = '低概率区间, 保守'; confidence_level = 'low'
    
    # ── 弱势区间 (<45%) ──
    else:
        if form['massacre_triggered']:
            prediction = 'H' if form['net_diff'] > 0 else 'A'; confidence = 0.60; rationale = '弱势+屠杀'; confidence_level = 'medium'
        else:
            prediction = odds['market']; confidence = 0.45; rationale = '弱势区间, 市场指向'; confidence_level = 'low'
    
    # ── ML融合层: 主模型作为决策参考 (规则硬信号已优先处理) ──
    if main_proba is not None and not form['massacre_triggered']:
        ml_pred = ['H', 'D', 'A'][int(np.argmax(main_proba))]
        ml_conf = float(main_proba.max())
        if ml_pred != prediction and ml_conf >= 0.60:
            if ENABLE_ML_MARKET_OVERRIDE:
                # 主模型高置信且与规则冲突 → 采用主模型 (保留规则理由备查)
                prediction = ml_pred
                confidence = max(confidence, min(ml_conf * 0.90, 0.88))
                rationale += f'; 主模型校准→{ml_pred}({ml_conf:.0%})'
            else:
                # 护栏默认开启: 禁止ML翻转规则/市场决策, 仅记录分歧备查
                # (跨届walk-forward证明ML外推≈argmax, 翻转只会引入错误选择)
                rationale += f'; 主模型分歧({ml_pred} {ml_conf:.0%}, 已锁定规则/市场)'
        elif ml_pred == prediction:
            # 主模型与规则一致 → 提升置信度 (佐证, 不改变决策)
            confidence = min(confidence * 1.05, 0.92)
            rationale += f'; 主模型佐证({ml_conf:.0%})'

    # ── DrawGate: 平局专家门控 (攻克DE核心, 护栏OFF下也生效) ──
    # DrawExpert 是专门平局二分类器, 其校准平局概率 de_prob 在平局边界有真实信号。
    # 门控触发时强制判D, 优先级高于主模型override与市场兜底(平局专项裁决)。
    _gate_fired = (de_prob >= DRAW_GATE and odds['imp_d'] >= DRAW_GATE_MIN_IMP_D)
    if _gate_fired:
        if prediction != 'D':
            rationale += f'; DrawGate平局门控(de_prob={de_prob:.2f},imp_d={odds["imp_d"]:.2f})'
        prediction = 'D'
        confidence = max(confidence, 0.55)
        if confidence_level != 'high':
            confidence_level = 'medium'

    # ── DrawTightGate: 胶着+战意 平局补充门控 (覆盖DE漏判的爆冷平局) ──
    # 仅当当前未判D且市场也未选D时, 对"胶着+MD3生死/淘汰赛均势"比赛翻D。
    # 优先级: 高于护栏OFF市场兜底(平局专项裁决), 但低于已触发的DrawGate(不重复翻)。
    _tight_fired = False
    if prediction != 'D' and odds['market'] != 'D':
        _fav = max(odds['imp_h'], odds['imp_a'])
        _tight = (_fav - odds['imp_d']) <= DRAW_TIGHT_GAP
        _md3 = (match.stage == 'group' and match.matchday == 3)
        if _tight and (_md3 or context.get('survival_clash')):
            prediction = 'D'
            _tight_fired = True
            rationale += f'; 胶着+战意翻D(gap={_fav-odds["imp_d"]:.2f},md3={_md3},survival={context.get("survival_clash")})'
            confidence = max(confidence, 0.50)
            if confidence_level == 'low':
                confidence_level = 'medium'

    # ── 护栏OFF(默认安全): 兜底回市场(argmax) ──
    # 跨届walk-forward证明主模型ML不外推(OOS optimized 0.613<argmax 0.710);
    # 规则层80场回测55%<纯赔率66.2%, ML锁定后唯一可信锚点是市场赔率。
    # 护栏OFF时optimized默认取市场argmax; 但DrawGate/DrawTightGate已触发时跳过(平局专家裁决优先)。
    if not ENABLE_ML_MARKET_OVERRIDE and not _gate_fired and not _tight_fired:
        if prediction != odds['market']:
            rationale += f'; 护栏OFF→兜底市场({odds["market"]})'
        prediction = odds['market']
        _zone_conf = {'strong': 0.80, 'mid_safe': 0.65, 'mid_danger': 0.45, 'weak': 0.45}
        confidence = max(confidence, _zone_conf.get(odds['zone'], 0.45))

    # 死球降级
    if context.get('dead_rubber'):
        confidence *= 0.7
        confidence_level = 'low'
        rationale += '; 轮换降级'
    
    # 比分预测
    if prediction == 'H':
        if form['massacre_triggered'] and form['net_diff'] > 0:
            d = max(int(abs(form['net_diff']) + 1), 2)
            best_score = f"{min(d, 5)}-{max(int(d*0.3), 0)}"
        else:
            best_score = "2-0"
        alt_scores = ["2-1", "1-0"]
    elif prediction == 'A':
        if form['massacre_triggered'] and form['net_diff'] < 0:
            d = max(int(abs(form['net_diff']) + 1), 2)
            best_score = f"{max(int(d*0.3), 0)}-{min(d, 5)}"
        else:
            best_score = "0-2"
        alt_scores = ["1-2", "0-1"]
    else:
        best_score = "1-1" if not form.get('weak_attack') else "0-0"
        alt_scores = ["0-0", "2-2"] if not form.get('weak_attack') else ["1-1"]
    
    return PipelineResult(
        prediction=prediction, confidence=min(confidence, 0.95),
        best_score=best_score, alt_scores=alt_scores,
        market_baseline=odds['market'],
        market_probs={'H': round(odds['imp_h'],4), 'D': round(odds['imp_d'],4), 'A': round(odds['imp_a'],4)},
        mid_range_filtered=(max_imp <= 0.68),
        mispricing_overlay=False,
        massacre_triggered=form['massacre_triggered'],
        survival_clash=context['survival_clash'],
        rationale=rationale, confidence_level=confidence_level,
        ou_recommend=predict_ou_wc(match),
        hcp_recommend=predict_hcp_wc(match, form, context),
    )


def predict_ou_wc(match: MatchInput) -> dict:
    """WC 校准大小球预测 — 诊断修复 P0-2/P1-2
    实测: WC 场均 3.01 球, ≥3 球占比 53% (96/181), 小组赛穿盘(≥3净胜)25.6%
    旧逻辑误判'进球偏少' → 现按盘口分级给大/小建议
    """
    line = match.ou_line if match.ou_line is not None else 2.5
    if line <= 2.25:
        rec, exp, conf = '大', 3.10, 0.58
    elif line <= 2.5:
        rec, exp, conf = '大', 3.00, 0.55
    elif line <= 2.75:
        rec, exp, conf = '大', 2.90, 0.52
    elif line <= 3.0:
        rec, exp, conf = '小', 2.70, 0.51
    elif line <= 3.25:
        rec, exp, conf = '小', 2.50, 0.53
    else:
        rec, exp, conf = '小', 2.30, 0.56
    return {
        'recommend': rec, 'line': line, 'expected_total': exp,
        'confidence': conf, 'wc_calibrated': True,
        'note': 'WC实测场均3.01球, ≥3球占53%',
    }


def predict_hcp_wc(match: MatchInput, form: dict, context: dict) -> dict:
    """WC 校准让球推荐 — 诊断修复 P1-1 (让球律分阶段, 旧逻辑完全未用 hcp)
    铁律: 让2球不穿律(WC小组赛穿盘仅25.6%), 让1球冷门律(穿盘率35.7%),
          深让双推(TaoGe), 屠杀队(巴西/德国/荷兰)+非R3可穿
    返回独立让球市场建议, 不改动 1X2 判决
    """
    hcp = match.hcp if match.hcp is not None else 0.0
    gap = form['strength_gap']
    massacre = form['massacre_triggered']
    r3 = context.get('dead_rubber', False) or match.r3_rotation
    massacre_team = match.home in ('巴西', '德国', '荷兰')

    if hcp <= -2.0:
        # 让2球: 默认不穿盘
        if massacre_team and massacre and not r3:
            rec, conf = '双选(让胜+让平)', 0.55
            note = '屠杀队+非R3 → 可穿, 双选让胜+让平'
        else:
            rec, conf = '让平', 0.58
            note = '让2球不穿律(WC小组赛穿盘仅25.6%) → 让平优先'
    elif hcp <= -1.5:
        # 让1.5球
        if massacre and massacre_team and not r3:
            rec, conf = '让胜', 0.52
            note = '屠杀队深让 → 让胜'
        else:
            rec, conf = '双选(让胜+让平)', 0.50
            note = '让1.5球: 穿盘率约50%, 双选覆盖'
    elif hcp <= -1.0:
        # 让1球: 冷门律, 穿盘率仅35.7%
        rec, conf = '让平/让负', 0.55
        note = '让1球冷门律(穿盘率35.7%) → 防让平/让负'
    elif hcp <= -0.5:
        # 让0.5球: 浅让, 双选胜+平
        rec, conf = '双选(胜+平)', 0.52
        note = '浅让0.5 → 双选胜+平'
    else:
        rec, conf = '主队不败', 0.50
        note = '平手/受让 → 主队不败'
    return {'recommend': rec, 'hcp': hcp, 'confidence': conf, 'wc_calibrated': True, 'note': note}


def predict_score_wc(match: MatchInput) -> dict:
    """真比分概率预测 (OIP 赔率隐含 Poisson) — 生产级, 非破坏性钩子
    补上系统'无真比分模型'天花板. 跨届OOS验证: logloss=2.83 / Top3=50% / H-D-A=66.7%.
    详见 pipeline/score_model.py (DC 在小样本下过拟合, 已弃用).
    返回: 期望进球 lh/la, 胜平负隐含概率, 前5可能比分 [(i,j,p),...]
    """
    try:
        from score_model import predict_score
    except Exception as e:  # pragma: no cover
        logger.warning("[wc_engine] score_model 导入失败: %s", e)
        return {'available': False, 'error': str(e)}
    res = predict_score(match.home, match.away, match.odds_h, match.odds_d, match.odds_a)
    res['available'] = True
    return res


# ═══════════════════════════════════════════════════════════
# CLI 自检
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    # 测试所有阶段
    test_matches = [
        MatchInput("巴西", "挪威", 1.85, 3.85, 3.90, -0.5, 3.0, "knockout"),
        MatchInput("葡萄牙", "西班牙", 3.80, 3.60, 1.92, 0.5, 2.75, "knockout"),
        MatchInput("荷兰", "摩洛哥", 2.25, 3.30, 3.80, -0.25, 2.5, "knockout"),
        MatchInput("德国", "巴拉圭", 1.38, 5.60, 10.27, -1.5, 3.0, "knockout"),
    ]
    
    print(f"{'═'*70}")
    print(f"  哨响AI v7.0 规则流水线 — 自检 ({len(test_matches)}场)")
    print(f"{'═'*70}")
    
    for m in test_matches:
        r = predict(m)
        print(f"\n  {m.home} vs {m.away}")
        print(f"  赔率: {m.odds_h}/{m.odds_d}/{m.odds_a}  hcp={m.hcp:+.2f}")
        print(f"  市场: {r.market_baseline} (H={r.market_probs['H']*100:.0f}% D={r.market_probs['D']*100:.0f}% A={r.market_probs['A']*100:.0f}%)")
        print(f"  判决: {r.prediction}  |  信度: {r.confidence*100:.0f}% ({r.confidence_level})")
        print(f"  比分: {r.best_score}  |  备选: {r.alt_scores}")
        print(f"  理由: {r.rationale}")
        print(f"  标志: 屠杀={r.massacre_triggered} 生死战={r.survival_clash} 中概率过滤={r.mid_range_filtered} 误定价={r.mispricing_overlay}")
        print(f"  OU(WC校准): {r.ou_recommend}")
        print(f"  让球(WC校准): {r.hcp_recommend}")
